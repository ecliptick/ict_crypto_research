"""nb52 worker module — ThreadPoolExecutor + per-file SweepCache.

Why ThreadPoolExecutor instead of ProcessPoolExecutor?

* numpy releases the GIL on ufunc calls; the bar loop spends
  ~95% of its wall time inside numpy ops. Threads run those
  ops in parallel WITHOUT the 6× RAM duplication of processes.
* Windows `spawn` start-method had been OOM-crashing the 6-worker
  pool (each process duplicated the ~5 GB monthly working set).
* The detector cache (added 2026-09-26) means each thread only
  re-runs the bar loop; the heavy detector + side-table build
  happens ONCE per file (in the parent process via
  ``get_or_build``). Workers just open the pickle files.

Worker signature is per-config now (not per-file):
    worker_sweep_for_config(args) -> dict
where args is a tuple of (file_path, sl, tp).

The parent process owns the cache; each worker takes the file
path, builds/loads the SweepCache (cache hit on disk = fast),
and runs ``run_tick_backtest`` with the precomputed zones +
structure + side-table. The side-table is shared by reference
across worker invocations on the same file (since they share
the parent's heap).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import List, Tuple

# Force UTF-8 in worker (Windows cp1252 default)
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# Repo root discovery
_ROOT = None
for _candidate in [Path(".").resolve(), *Path(".").resolve().parents]:
    if (_candidate / "src" / "core" / "ict_signals.py").is_file():
        _ROOT = _candidate
        break
if _ROOT is None:
    raise RuntimeError("Could not find ICT repo root in worker process.")
sys.path.insert(0, str(_ROOT))

from src.core.optimal_config import optimal_params                                    # noqa: E402
from src.tick.aggtrade_aggregator import load_concat_raw_aggtrades, BACKTEST_COLUMNS  # noqa: E402
from src.tick.cache import get_or_build                                               # noqa: E402
from src.tick.tick_backtest import run_tick_backtest                                  # noqa: E402


# In-process cache: file_path -> SweepCache. Lets the same worker
# process reuse a SweepCache across multiple configs on the same
# file without reloading from disk.
_INPROCESS_CACHE: dict = {}


# Minimal fingerprint for cache invalidation. ONLY structural and
# resampling params — NOT zone-creation filters like strict-wick
# or zone-breadth. The detector emits a SUPERSET of zones (no
# zone-breadth filter, no strict-wick filter); zone filters are
# applied downstream in ``_filter_zones_post_hoc``. This way every
# scenario config shares ONE cached zone list per file, restoring
# the advertised 2-3s/config sweep speed (vs. 5+ minutes/config
# when each param change forces a fresh detector rebuild).
_MINIMAL_DETECTOR_KEYS: tuple = (
    "fvg_resample_secs",
    "ifvg_resample_secs",
    "ms_pivot_len",
    "ms_liquidity_len",
    "ms_draw_order_blocks",
    "ms_draw_liquidity_sweeps",
    "ms_resample_secs",
    "fvg_displacement_ratio",
    "fvg_body_definition",
    "fvg_supersede_on_new",
    "fvg_invalidation_min_pierce_usd",
    "fvg_invalidation_min_consecutive_bars",
    "fvg_require_retest_to_invert",
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
)


def _fp_key(file_path: Path, p):
    """Build a hashable fingerprint key for ``(file_path, params)``.

    The key uses the MINIMAL detector fingerprint — structural and
    resampling params only — so that zone-creation filters
    (zone-breadth, strict-wick, mit-distance) do NOT invalidate the
    cache. The full detector fingerprint in ``src.tick.cache`` still
    invalidates on the FULL param set; we deliberately use a subset
    here so the in-process cache stays hot across the nb52 sweep
    configs. Zone filters are applied downstream in
    ``_filter_zones_post_hoc``.
    """
    from src.tick.cache import _fingerprint
    full_fp = _fingerprint(p, file_path)
    minimal = {k: full_fp.get(k) for k in _MINIMAL_DETECTOR_KEYS}
    minimal["_raw_mtime_ns"] = full_fp["_raw_mtime_ns"]
    minimal["_raw_size_bytes"] = full_fp["_raw_size_bytes"]
    minimal["_cache_version"] = full_fp["_cache_version"]
    return (file_path, tuple(sorted(minimal.items())))


def _get_sc(file_path: Path, p, with_side_table: bool = True):
    """Return the SweepCache for ``(file_path, params)``, building on miss.

    Per-file and per-(minimal-fingerprint). Zone-creation filters
    (zone-breadth, strict-wick, mit-distance) do NOT invalidate the
    cache — those filters are applied post-hoc in
    ``_filter_zones_post_hoc``. See ``_fp_key`` for the rationale.

    The disk cache is built with the FULL params on first miss, but
    the in-process cache key is the minimal fingerprint — so once a
    given (file, minimal-fingerprint) is built, every subsequent
    config shares the same SweepCache object without touching disk.
    """
    key = _fp_key(file_path, p)
    sc = _INPROCESS_CACHE.get(key)
    if sc is not None:
        return sc
    # First miss for this minimal fingerprint. Build the SweepCache
    # with the actual params (so the disk fingerprint matches the
    # on-disk schema). Subsequent in-process hits skip this entirely.
    sc = get_or_build(file_path, p, with_side_table=with_side_table,
                      verbose=False)
    _INPROCESS_CACHE[key] = sc
    return sc


def _filter_zones_post_hoc(zones: list, p, source: str) -> list:
    """Apply zone-creation filters post-hoc to cached zones.

    The SweepCache stores zones with the SUPERSET filter (no
    zone-breadth filter, no strict-wick filter, no mit-distance
    filter). To produce per-config zone lists we walk the cached
    zones and apply the per-config filters here.

    Filters applied:
      * ``fvg_min_zone_usd`` / ``ifvg_min_zone_usd`` — drop zones
        narrower than the floor. For source='fvg' use fvg_*;
        for source='ifvg' use ifvg_*.
      * ``fvg_strict_wick_required`` / ``fvg_strict_wick_min_wick_usd``
        — drop zones whose outer candles (c1, c3) lack a visible
        wick of the required length. The detector emits a
        ``zone.strict_wick_wick_usd`` annotation when strict-wick
        was off at detect time; we compare against the threshold.
      * ``fvg_min_mit_distance_bars`` — this is a SIGNAL-side
        filter (not zone-side) so we don't apply it here. The
        bar loop's signal generator still reads it via ``p``.

    Notes:
      * This is a PER-CONFIG post-filter; it does NOT mutate the
        cached SweepCache object (a shallow copy of the zones
        list is returned).
      * Each zone object carries its creation-time strict-wick
        wick length as ``zone.wick_length_usd`` (added 2026-09-26);
        we read that attribute here. Zones that the detector ran
        with strict-wick=on were already filtered, so for them
        ``zone.wick_length_usd`` is >= the detect-time threshold.
    """
    if source == "fvg":
        zone_floor = float(getattr(p, "fvg_min_zone_usd", 0.0))
    elif source == "ifvg":
        zone_floor = float(getattr(p, "ifvg_min_zone_usd", 0.0))
    else:
        zone_floor = 0.0
    strict_wick = bool(getattr(p, "fvg_strict_wick_required", False))
    wick_min = float(getattr(p, "fvg_strict_wick_min_wick_usd", 0.0))

    out = []
    for z in zones:
        # Zone-breadth filter.
        if zone_floor > 0:
            width = float(z.zone_high) - float(z.zone_low)
            if width < zone_floor:
                continue
        # Strict-wick filter (only meaningful if detector was run
        # with strict-wick_required=False; otherwise the zone list
        # is already filtered).
        if strict_wick and wick_min > 0:
            wick_avail = float(getattr(z, "wick_length_usd", 0.0))
            if wick_avail < wick_min:
                continue
        out.append(z)
    return out


def run_one_file(path: Path, label: str, *, include_trades: bool = False,
                 use_cache: bool = True,
                 with_side_table: bool = True,
                 **param_overrides) -> dict:
    """Run the tick-fill hybrid backtest on a single file.

    ``label`` is the ``strategy_label`` written to the IctBacktestResult.
    ``include_trades`` (default False): when True, return the raw
    ``trades`` list (Trade instances) inside the result dict so
    downstream code can access the non-JSON-serialisable ``fvg_zone``
    attribute. Leave False in sweep workers to avoid pickling the
    trade list back across process boundaries.
    ``use_cache`` (default True): when True, use the SweepCache
    (one detector pass per file, reused across sweep configs).
    ``with_side_table`` (default True): only meaningful when
    ``use_cache=True``; controls whether the side-table is part
    of the cache build.
    ``**param_overrides`` are forwarded to ``optimal_params()``.
    """
    p = optimal_params(**param_overrides)

    t0 = time.perf_counter()
    if use_cache:
        # Load (or build) the per-file cache. Detector work is done
        # once per file; SL/TP overrides only affect the bar loop.
        sc = _get_sc(path, p, with_side_table=with_side_table)
        # Apply post-hoc zone filters so the per-config zone list
        # matches what the detector would have produced for THIS
        # param set (without rebuilding the detector from scratch).
        fvg_zones_filtered = _filter_zones_post_hoc(sc.fvg_zones, p, "fvg")
        ifvg_zones_filtered = _filter_zones_post_hoc(sc.ifvg_zones, p, "ifvg")
        res = run_tick_backtest(
            raw_df=None, p=p, strategy_label=label,
            pre_aggregated_bars=sc.bars,
            precomputed_structure=sc.structure,
            precomputed_atr=sc.atr,
            precomputed_zones_by_src={"fvg": fvg_zones_filtered,
                                       "ifvg": ifvg_zones_filtered},
            side_table=sc.side_table if with_side_table else None,
        )
    else:
        # Legacy path: load raw ticks + run full pipeline.
        raw = load_concat_raw_aggtrades([path], columns=BACKTEST_COLUMNS)
        res = run_tick_backtest(raw, p, strategy_label=label)
    elapsed = time.perf_counter() - t0
    s = res.summary()
    s.update({
        "file": path.name,
        "elapsed_s": float(elapsed),
        "tick_meta": res.tick_metadata,
    })
    if include_trades:
        s["trades"] = res.trades
    return s


def worker_sweep_for_config(
    args: Tuple[Path, float, float],
) -> dict:
    """Run ONE (file, sl, tp) config. Returns one result dict.

    Args is a tuple ``(file_path, sl_mult, tp_mult)``. This is
    the per-config worker signature; the parent process builds
    the full list of (file, sl, tp) tuples and submits them to
    the executor as one task per tuple — gives much better load
    balancing than the old per-file worker (which held one file
    open for 25 configs and starved threads on the slow file).
    """
    path, sl, tp = args
    try:
        row = run_one_file(
            path,
            f"nb52_sweep_sl{sl}_tp{tp}",
            fvg_inv_trade_sl_zone_mult=sl,
            fvg_inv_trade_tp_zone_mult=tp,
        )
        row["sl_mult"] = sl
        row["tp_mult"] = tp
        return row
    except Exception as exc:
        print(
            f"  ! {path.name} sl={sl} tp={tp} FAILED: {exc}",
            flush=True,
        )
        return {
            "file": path.name, "sl_mult": sl, "tp_mult": tp,
            "n_trades": 0, "pnl_total": 0.0, "ev_per_trade": 0.0,
            "win_rate": 0.0, "trades_per_day": 0.0,
        }


def worker_sweep_for_file(args: Tuple[Path, List[Tuple[float, float]]]) -> List[dict]:
    """Legacy per-file worker — kept for back-compat.

    Iterates over the (sl, tp) grid sequentially. Use
    ``worker_sweep_for_config`` instead for ThreadPoolExecutor
    parallelism.
    """
    path, grid = args
    out_rows: List[dict] = []
    for sl, tp in grid:
        out_rows.append(worker_sweep_for_config((path, sl, tp)))
    return out_rows
