"""Per-file sweep cache — persist detector output so SL/TP sweeps
can skip the detector entirely on subsequent configs.

Added 2026-09-26 as part of the tick-backtest perf refactor
(see ``AGENTS.md`` — Update 2026-09-26). The cache key is the
file path + a fingerprint of the DETECTOR-side params (zone
min width, resample cadence, structure knobs, etc). SL/TP zone-
width overrides (``fvg_inv_trade_sl_zone_mult`` /
``fvg_inv_trade_tp_zone_mult``) are NOT in the fingerprint —
those are bar-loop geometry, not detector params, so they can
vary freely across sweep configs without invalidating the cache.

Cache layout (under ``notebooks/.cache/<file_stem>/``)::

    bars.parquet           # 1s OHLCV (~5 MB)
    atr.npy                # float64[N_bars] (~20 MB on monthly)
    structure.pkl          # StructureState (full pickle, ~5 MB)
    fvg_zones.pkl          # list[FvgZone] for src=fvg (~5 MB)
    ifvg_zones.pkl         # list[FvgZone] for src=ifvg (~5 MB)
    side_table.pkl         # _TickSideTable (~150 MB monthly — biggest)
    fingerprint.json       # detector-param fingerprint + mtime
    meta.json              # build timestamps, sizes, version

Building is gated by::

    1. mtime of the raw parquet matches the recorded fingerprint.
    2. detector-param fingerprint matches.
    3. cache version matches.

Mismatch -> rebuild from scratch.

Public API
==========

* ``SweepCache``           — typed container holding the loaded data.
* ``get_or_build(file, p)`` — returns the cache (build on miss).
* ``cache_root()``          — root directory for all caches.
"""
from __future__ import annotations

import json
import os
import pickle
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd

from .aggtrade_aggregator import (
    BACKTEST_COLUMNS,
    aggregate_ticks_to_1s_bars,
    load_raw_aggtrades_columns,
)
from .tick_backtest import _TickSideTable, build_tick_side_table


CACHE_VERSION = 6  # bumped 2026-09-27 — ATR is now computed on 1-min
              # bars (was 1-second bars); the resulting value is
              # ~25× larger and represents structural range rather
              # than 1-second noise. Existing caches must rebuild.


# Detector-side param fields whose change invalidates the cache.
# SL/TP zone-width overrides are intentionally NOT in this list
# — they're bar-loop geometry, applied downstream of the detector.
_DETECTOR_PARAM_KEYS: tuple = (
    "fvg_min_zone_usd",
    "fvg_resample_secs",
    "ifvg_min_zone_usd",
    "ifvg_resample_secs",
    "ms_pivot_len",
    "ms_liquidity_len",
    "ms_draw_order_blocks",
    "ms_draw_liquidity_sweeps",
    "ms_resample_secs",
    "fvg_displacement_ratio",
    "fvg_body_definition",
    "fvg_min_zone_atr_mult",
    "fvg_min_zone_atr",
    "fvg_supersede_on_new",
    "fvg_invalidation_min_pierce_usd",
    "fvg_invalidation_min_consecutive_bars",
    "fvg_require_retest_to_invert",
    "played_out_min_extension_usd",
    "fvg_body_only_mitigation",
    "fvg_body_only_invalidation",
    "fvg_invalidate_on_structure",
    "fvg_structure_invalidation_age_secs",
    "fvg_min_lifetime_secs",
    "fvg_max_age_secs",
    "fvg_max_age_bars",
    "fvg_max_cache_size",
    "ifvg_max_age_secs",
    "ifvg_max_age_bars",
    "ifvg_max_cache_size",
    "atr_len",
    "fvg_min_mit_distance_bars",      # 2026-09-26 c (clean-zone gate)
    "fvg_min_inv_distance_bars",      # 2026-09-26 c (clean-zone gate)
    "strict_wick_required",           # 2026-09-26 c (strict-wick FVG)
    "strict_wick_min_wick_price_pct", # 2026-09-26 c (dynamic wick floor)
    "strict_wick_recompute_secs",     # 2026-09-26 c (hourly bucket)
)


def cache_root() -> Path:
    """Return the root directory for caches. Created on demand."""
    root = Path("notebooks/.cache")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _fingerprint(params, raw_path: Path) -> dict:
    """Build a JSON-serialisable fingerprint for the cache key."""
    fp = {"_cache_version": CACHE_VERSION}
    for k in _DETECTOR_PARAM_KEYS:
        fp[k] = getattr(params, k, None)
    try:
        fp["_raw_mtime_ns"] = int(raw_path.stat().st_mtime_ns)
        fp["_raw_size_bytes"] = int(raw_path.stat().st_size)
    except OSError:
        fp["_raw_mtime_ns"] = -1
        fp["_raw_size_bytes"] = 0
    return fp


@dataclass
class SweepCache:
    """In-memory handle to a per-file cached detector output.

    Attributes
    ----------
    raw_path : Path
        The original aggTrades parquet.
    root : Path
        The cache directory on disk (e.g. ``notebooks/.cache/BTCUSDT-aggTrades-2025-04``).
    bars : pd.DataFrame
        1s OHLCV bars (same schema as ``aggregate_ticks_to_1s_bars``).
    atr : np.ndarray
        float64[N_bars] ATR array.
    structure : object
        StructureState (pickled, returned as-is).
    fvg_zones : list
        list[FvgZone] from ``detect_fvg(src='fvg')``.
    ifvg_zones : list
        list[FvgZone] from ``detect_fvg(src='ifvg')``.
    side_table : _TickSideTable | None
        The lazy tick side-table (can be ``None`` for cache-only sweeps
        that don't need tick-fill exits).
    build_ms : dict
        Per-phase build timings (load, agg, atr, structure, fvg,
        ifvg, side_table, total) in milliseconds.
    """
    raw_path: Path
    root: Path
    bars: pd.DataFrame
    atr: np.ndarray
    structure: object
    fvg_zones: list
    ifvg_zones: list
    side_table: Optional[object] = None
    wick_floor_per_bar: Optional[np.ndarray] = None  # added 2026-09-26 c
    build_ms: dict = field(default_factory=dict)


def _save_pickle(obj, path: Path) -> None:
    """High-protocol pickle that's still readable across Python versions.

    Falls back to protocol 4 if pickle.HIGHEST_PROTOCOL fails (only
    relevant when numpy arrays inside the object have exotic dtypes).
    """
    import pickle as _p
    try:
        with open(path, "wb") as fh:
            _p.dump(obj, fh, protocol=_p.HIGHEST_PROTOCOL)
    except Exception:
        with open(path, "wb") as fh:
            _p.dump(obj, fh, protocol=4)


def _load_pickle(path: Path):
    import pickle as _p
    with open(path, "rb") as fh:
        return _p.load(fh)


def _is_cache_valid(raw_path: Path, cache_dir: Path, fingerprint: dict) -> bool:
    """True iff all cache files exist and the fingerprint matches."""
    fp_path = cache_dir / "fingerprint.json"
    if not fp_path.exists():
        return False
    try:
        with open(fp_path, "r", encoding="utf-8") as fh:
            stored = json.load(fh)
    except Exception:
        return False
    if stored.get("_cache_version") != fingerprint["_cache_version"]:
        return False
    # Compare all detector-param keys (ignore extra in stored)
    for k in _DETECTOR_PARAM_KEYS:
        if stored.get(k) != fingerprint[k]:
            return False
    if stored.get("_raw_mtime_ns") != fingerprint["_raw_mtime_ns"]:
        return False
    if stored.get("_raw_size_bytes") != fingerprint["_raw_size_bytes"]:
        return False
    # All required files exist?
    required = ("bars.parquet", "atr.npy", "structure.pkl",
                "fvg_zones.pkl", "ifvg_zones.pkl", "meta.json")
    return all((cache_dir / name).exists() for name in required)


def _build_cache(raw_path: Path, params, *, with_side_table: bool = True) -> SweepCache:
    """Build a fresh SweepCache from ``raw_path``.

    Performs: load → aggregate → ATR → structure → FVG → iFVG →
    [side-table if requested]. Returns a SweepCache and writes the
    on-disk artefacts.
    """
    from src.core.ict_signals import compute_atr_on_resampled_bars
    from src.core.market_structure import detect_market_structure

    timings = {}
    t_total0 = time.perf_counter()

    cache_dir = cache_root() / raw_path.stem
    cache_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Load ticks (4 cols only) ──
    t0 = time.perf_counter()
    raw = load_raw_aggtrades_columns(raw_path)
    timings["load_ms"] = (time.perf_counter() - t0) * 1000

    # ── 2. Aggregate → 1s bars ──
    t0 = time.perf_counter()
    bars = aggregate_ticks_to_1s_bars(raw)
    timings["agg_ms"] = (time.perf_counter() - t0) * 1000
    bars.to_parquet(cache_dir / "bars.parquet", index=False)

    # ── 3. ATR (resampled to 1-min bars; bugfix 2026-09-27) ──
    t0 = time.perf_counter()
    atr = compute_atr_on_resampled_bars(
        bars, resample_secs=60, length=int(params.atr_len),
    )
    timings["atr_ms"] = (time.perf_counter() - t0) * 1000
    np.save(cache_dir / "atr.npy", atr)

    # ── 4. Structure ──
    t0 = time.perf_counter()
    structure = detect_market_structure(
        bars["high"].to_numpy(dtype=np.float64),
        bars["low"].to_numpy(dtype=np.float64),
        bars["close"].to_numpy(dtype=np.float64),
        pivot_len=int(params.ms_pivot_len),
        liquidity_len=int(params.ms_liquidity_len),
        detect_order_blocks=bool(params.ms_draw_order_blocks),
        detect_liquidity=bool(params.ms_draw_liquidity_sweeps),
        resample_to_n_secs=int(getattr(params, "ms_resample_secs", 0)),
    )
    timings["structure_ms"] = (time.perf_counter() - t0) * 1000
    _save_pickle(structure, cache_dir / "structure.pkl")

    # ── 5. FVG zones (regime) ──
    # detect_fvg returns zones filtered by fvg_min_zone_usd. The
    # iFVG path passes ifvg_min_zone_usd (which may differ). So
    # we call detect_fvg twice when those knobs differ; when
    # they match (the canonical config does), we call once.
    t0 = time.perf_counter()
    from src.core.ict_signals import detect_fvg, compute_dynamic_wick_floor
    times_utc_ns = pd.to_datetime(bars["time"], utc=True).astype("int64").to_numpy()
    open_np = bars["open"].to_numpy(dtype=np.float64)
    high_np = bars["high"].to_numpy(dtype=np.float64)
    low_np = bars["low"].to_numpy(dtype=np.float64)
    close_np = bars["close"].to_numpy(dtype=np.float64)

    # ── Per-bar dynamic wick floor (added 2026-09-26 c) ─────────────
    # When strict_wick_required=True and a price-relative pct is set,
    # compute the per-bar USD floor from hourly buckets of close. The
    # detector receives this array directly via
    # ``strict_wick_floor_usd_per_bar`` (overrides the static USD
    # value). Cost: ~10ms/month.
    _strict_wick_required = bool(getattr(params, "strict_wick_required", False))
    _strict_wick_pct = float(getattr(params, "strict_wick_min_wick_price_pct", 0.0))
    _strict_wick_secs = int(getattr(params, "strict_wick_recompute_secs", 3600))
    if _strict_wick_required and _strict_wick_pct > 0:
        wick_floor_per_bar = compute_dynamic_wick_floor(
            close_np, times_utc_ns,
            price_pct=_strict_wick_pct,
            recompute_secs=_strict_wick_secs,
        )
    else:
        wick_floor_per_bar = None

    def _detect(min_z_usd: float, rs: int, max_zones: int,
                age_bars: int) -> list:
        return detect_fvg(
            open_np, high_np, low_np, close_np,
            warmup=0,
            max_active_zones=max_zones,
            resample_to_n_secs=rs if rs > 0 else 0,
            fvg_min_zone_usd=float(min_z_usd),
            max_zone_age_bars=age_bars,
            fvg_displacement_ratio=float(params.fvg_displacement_ratio),
            fvg_body_definition=str(params.fvg_body_definition),
            fvg_min_zone_atr_mult=float(getattr(params, "fvg_min_zone_atr_mult", 0.0)),
            atr_for_min_zone=float(getattr(params, "fvg_min_zone_atr", 0.0)),
            supersede_on_new=bool(getattr(params, "fvg_supersede_on_new", True)),
            invalidation_min_pierce_usd=float(params.fvg_invalidation_min_pierce_usd),
            invalidation_min_consecutive_bars=int(params.fvg_invalidation_min_consecutive_bars),
            require_retest_to_invert=bool(params.fvg_require_retest_to_invert),
            played_out_min_extension_usd=float(params.played_out_min_extension_usd),
            body_only_mitigation=bool(getattr(params, "fvg_body_only_mitigation", False)),
            body_only_invalidation=bool(getattr(params, "fvg_body_only_invalidation", False)),
            times_utc_ns=times_utc_ns,
            fvg_min_lifetime_secs=int(getattr(params, "fvg_min_lifetime_secs", 0)),
            strict_wick_required=_strict_wick_required,
            strict_wick_min_wick_usd=float(getattr(params, "strict_wick_min_wick_usd", 0.0)),
            strict_wick_floor_usd_per_bar=wick_floor_per_bar,
        )

    fvg_zones = _detect(
        min_z_usd=float(params.fvg_min_zone_usd),
        rs=int(params.fvg_resample_secs),
        max_zones=int(params.fvg_max_cache_size),
        age_bars=_age_to_bars_fvg(params),
    )
    timings["fvg_ms"] = (time.perf_counter() - t0) * 1000
    _save_pickle(fvg_zones, cache_dir / "fvg_zones.pkl")

    # iFVG: same detector knobs but with ifvg_min_zone_usd and
    # ifvg_resample_secs / ifvg_max_age_secs. If those happen to
    # match the FVG knobs, reuse the same list (canonical config).
    fvg_minz = float(params.fvg_min_zone_usd)
    ifvg_minz = float(params.ifvg_min_zone_usd)
    if (ifvg_minz == fvg_minz
            and int(params.ifvg_resample_secs) == int(params.fvg_resample_secs)
            and int(params.ifvg_max_age_secs) == int(params.fvg_max_age_secs)
            and int(params.ifvg_max_age_bars) == int(params.fvg_max_age_bars)
            and int(params.ifvg_max_cache_size) == int(params.fvg_max_cache_size)):
        ifvg_zones = fvg_zones
        timings["ifvg_ms"] = 0.0
    else:
        t1 = time.perf_counter()
        # Build a custom _age_to_bars for ifvg knobs
        rs_ifvg = int(params.ifvg_resample_secs)
        if int(params.ifvg_max_age_secs) > 0:
            per_bar_secs = rs_ifvg if rs_ifvg > 0 else 1
            age_bars_ifvg = max(1, int(params.ifvg_max_age_secs) // per_bar_secs)
        elif int(params.ifvg_max_age_bars) > 0:
            age_bars_ifvg = int(params.ifvg_max_age_bars)
        else:
            age_bars_ifvg = 0
        ifvg_zones = _detect(
            min_z_usd=ifvg_minz,
            rs=rs_ifvg,
            max_zones=int(params.ifvg_max_cache_size),
            age_bars=age_bars_ifvg,
        )
        timings["ifvg_ms"] = (time.perf_counter() - t1) * 1000
    _save_pickle(ifvg_zones, cache_dir / "ifvg_zones.pkl")

    # ── 6b. Wick floor (added 2026-09-26 c) ───────────────────────
    # Persist the per-bar wick floor (or None if strict-wick OFF)
    # so warm-cache loads skip the hourly-bucket median computation.
    if wick_floor_per_bar is not None:
        np.save(cache_dir / "wick_floor.npy", wick_floor_per_bar)
    elif (cache_dir / "wick_floor.npy").exists():
        (cache_dir / "wick_floor.npy").unlink()

    # ── 7. Side-table (optional; large) ──
    side_table = None
    if with_side_table:
        t0 = time.perf_counter()
        side_table = build_tick_side_table(raw)
        timings["side_table_ms"] = (time.perf_counter() - t0) * 1000
        # The side-table is a lazy view; pickling it requires the
        # underlying arrays. Easiest: dump the raw arrays + the
        # bar_index dict separately. The object itself pickles ok
        # but slowly. We use protocol HIGHEST.
        _save_pickle(side_table, cache_dir / "side_table.pkl")

    timings["total_ms"] = (time.perf_counter() - t_total0) * 1000

    # Persist fingerprint + meta
    fp = _fingerprint(params, raw_path)
    with open(cache_dir / "fingerprint.json", "w", encoding="utf-8") as fh:
        json.dump(fp, fh, indent=2, sort_keys=True)
    meta = {
        "cache_version": CACHE_VERSION,
        "raw_path": str(raw_path),
        "n_raw_ticks": int(len(raw)),
        "n_bars": int(len(bars)),
        "n_fvg_zones": int(len(fvg_zones)),
        "n_ifvg_zones": int(len(ifvg_zones)),
        "with_side_table": bool(with_side_table),
        "timings_ms": {k: round(v, 1) for k, v in timings.items()},
    }
    with open(cache_dir / "meta.json", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, sort_keys=True)

    # Release raw reference — we no longer need it.
    del raw

    return SweepCache(
        raw_path=raw_path,
        root=cache_dir,
        bars=bars,
        atr=atr,
        structure=structure,
        fvg_zones=fvg_zones,
        ifvg_zones=ifvg_zones,
        side_table=side_table,
        wick_floor_per_bar=wick_floor_per_bar,
        build_ms=timings,
    )


def _age_to_bars_fvg(params) -> int:
    """Mirror the detector's age-cap conversion (see ict_signals.py L1991)."""
    rs = int(params.fvg_resample_secs)
    age_secs = int(params.fvg_max_age_secs)
    age_bars_legacy = int(params.fvg_max_age_bars)
    if age_secs > 0:
        per_bar_secs = rs if rs > 0 else 1
        return max(1, age_secs // per_bar_secs)
    if age_bars_legacy > 0:
        return age_bars_legacy
    return 0


def get_or_build(
    raw_path: Path,
    params,
    *,
    with_side_table: bool = True,
    force: bool = False,
    verbose: bool = True,
) -> SweepCache:
    """Return the SweepCache for ``raw_path``, building on miss.

    Parameters
    ----------
    raw_path : Path
        Path to the raw aggTrades parquet file.
    params : TrendStrategyParams
        Strategy params; only the detector-side subset is used for
        cache invalidation.
    with_side_table : bool, default True
        If True, build the tick side-table (the largest cache artefact).
        If False, omit it — useful for fast F.1/F.2/F.3 alpha work that
        doesn't need tick-fill exits.
    force : bool, default False
        Force a rebuild even if the cache is valid.
    verbose : bool, default True
        Print a one-liner summary of the build.

    Returns
    -------
    SweepCache
    """
    cache_dir = cache_root() / raw_path.stem
    fingerprint = _fingerprint(params, raw_path)

    if not force and _is_cache_valid(raw_path, cache_dir, fingerprint):
        # Load all artefacts
        t0 = time.perf_counter()
        bars = pd.read_parquet(cache_dir / "bars.parquet")
        atr = np.load(cache_dir / "atr.npy")
        structure = _load_pickle(cache_dir / "structure.pkl")
        fvg_zones = _load_pickle(cache_dir / "fvg_zones.pkl")
        ifvg_zones = _load_pickle(cache_dir / "ifvg_zones.pkl")
        side_table = None
        if with_side_table and (cache_dir / "side_table.pkl").exists():
            side_table = _load_pickle(cache_dir / "side_table.pkl")
        wick_floor_per_bar = None
        if (cache_dir / "wick_floor.npy").exists():
            wick_floor_per_bar = np.load(cache_dir / "wick_floor.npy")
        load_ms = (time.perf_counter() - t0) * 1000
        if verbose:
            print(f"  cache hit: {cache_dir.name} "
                  f"({len(bars):,} bars, {len(fvg_zones):,} FVG, "
                  f"{len(ifvg_zones):,} iFVG, "
                  f"{'side-table' if side_table else 'no-side-table'}) "
                  f"loaded in {load_ms:.0f} ms")
        return SweepCache(
            raw_path=raw_path,
            root=cache_dir,
            bars=bars,
            atr=atr,
            structure=structure,
            fvg_zones=fvg_zones,
            ifvg_zones=ifvg_zones,
            side_table=side_table,
            wick_floor_per_bar=wick_floor_per_bar,
            build_ms={"load_ms": load_ms, "total_ms": load_ms,
                      "cache_hit": True},
        )

    if verbose:
        print(f"  cache miss: building {cache_dir.name} ...")
    return _build_cache(raw_path, params, with_side_table=with_side_table)


__all__ = [
    "SweepCache",
    "get_or_build",
    "cache_root",
    "CACHE_VERSION",
]
