"""nb54 worker module — sparse market-structure sweep.

Sweeps the four market-structure knobs that change trade selection
on 1s BTC bars:

  * ``ms_pivot_len`` — right-side pivot window for swing detection
  * ``ms_liquidity_len`` — pivot window for liquidity-sweep detection
  * ``ms_resample_secs`` — 0 (raw 1s) vs 60 (1m) vs higher (the
    resolution-mismatch open work item from AGENTS.md § "Update
    (2026-09-25)")
  * ``fvg_invalidate_on_structure`` — master switch for the
    structure-event-driven FVG kill rule
  * ``fvg_structure_invalidation_age_secs`` — wall-clock cap on
    the rule

Same ThreadPoolExecutor + per-file SweepCache pattern as
``_nb52_worker.py``. The in-process cache key uses the MINIMAL
detector fingerprint so that all configs sharing a structure
fingerprint share a single SweepCache (and don't pay for a
fresh detector build).

Wall time: ~3s/config once cache is warm; cold cache build
~110s per unique (file, fingerprint). With the sparse 48-config
× 6-file grid, wall is dominated by the cold builds (one per
unique fingerprint per file): 48 unique fingerprints × 6 files
× ~110s / 8 workers = ~66 min worst-case, ~25 min warm.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Tuple

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

from src.core.optimal_config import optimal_params  # noqa: E402
from src.tick.cache import get_or_build  # noqa: E402
from src.tick.tick_backtest import run_tick_backtest  # noqa: E402


# Minimal fingerprint — includes the structure knobs we sweep so
# each combo gets its own cache, but EXCLUDES zone-creation filters
# (none of those change in this sweep — the structure params live
# entirely upstream of zone creation in the detector).
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

# In-process cache: keyed on (file_path, minimal-fingerprint).
_INPROCESS_CACHE: dict = {}


def _fp_key(file_path: Path, p):
    """Build a hashable minimal-fingerprint key for (file, params)."""
    from src.tick.cache import _fingerprint
    full_fp = _fingerprint(p, file_path)
    minimal = {k: full_fp.get(k) for k in _MINIMAL_DETECTOR_KEYS}
    minimal["_raw_mtime_ns"] = full_fp["_raw_mtime_ns"]
    minimal["_raw_size_bytes"] = full_fp["_raw_size_bytes"]
    minimal["_cache_version"] = full_fp["_cache_version"]
    return (file_path, tuple(sorted(minimal.items())))


def _get_sc(file_path: Path, p, with_side_table: bool = True):
    """Return the SweepCache for (file, params), building on miss."""
    key = _fp_key(file_path, p)
    sc = _INPROCESS_CACHE.get(key)
    if sc is not None:
        return sc
    sc = get_or_build(file_path, p, with_side_table=with_side_table,
                      verbose=False)
    _INPROCESS_CACHE[key] = sc
    return sc


def _summary_row(trades, file_name: str, config_id: str,
                 elapsed_s: float, params: dict) -> dict:
    """Build a compact summary dict from a list of Trade."""
    pnls = [t.pnl_usd for t in trades]
    fees = [t.fee_usd for t in trades]
    n = len(trades)
    by_reason: dict = {}
    for t in trades:
        by_reason.setdefault(t.exit_reason, []).append(t.pnl_usd)
    row = {
        "file": file_name,
        "config_id": config_id,
        "ms_pivot_len": int(params["ms_pivot_len"]),
        "ms_liquidity_len": int(params["ms_liquidity_len"]),
        "ms_resample_secs": int(params["ms_resample_secs"]),
        "fvg_invalidate_on_structure": bool(params["fvg_invalidate_on_structure"]),
        "fvg_structure_invalidation_age_secs": int(params["fvg_structure_invalidation_age_secs"]),
        "n_trades": n,
        "total_pnl_usd": float(sum(pnls)) if pnls else 0.0,
        "ev_per_trade_usd": float(sum(pnls) / n) if n else 0.0,
        "win_rate_pct": 100.0 * sum(1 for p in pnls if p > 0) / n if n else 0.0,
        "total_fees_usd": float(fees) if False else (float(sum(fees)) if fees else 0.0),
        "net_pnl_usd": float(sum(p - f for p, f in zip(pnls, fees))) if pnls else 0.0,
        "elapsed_s": float(elapsed_s),
        "n_sl": len(by_reason.get("sl", [])),
        "n_tp": len(by_reason.get("tp", [])),
        "n_inv": len(by_reason.get("inv", [])),
        "n_eod": len(by_reason.get("eod", [])),
    }
    return row


def worker_structure_sweep(args) -> dict:
    """Run a single (file, config) task.

    ``args`` is a tuple ``(file_path, config_id, params)`` where
    ``params`` is a dict of knob overrides to apply on top of the
    canonical recipe. The worker:

      1. Builds ``optimal_params(**params)`` (logs a WARNING for
         recipe-knob overrides; expected for ``ms_*`` and
         ``fvg_invalidate_on_structure``).
      2. Looks up / builds the per-(file, fingerprint) SweepCache.
      3. Runs ``run_tick_backtest`` with all precomputed
         structures supplied — only the bar loop + exit refill
         run per config.
      4. Returns the per-run summary row.
    """
    path, config_id, params = args
    try:
        t0 = time.perf_counter()
        p = optimal_params(**params)
        sc = _get_sc(path, p, with_side_table=True)
        cache_build_s = time.perf_counter() - t0
        t1 = time.perf_counter()
        res = run_tick_backtest(
            raw_df=None, p=p, strategy_label=f"nb54_ms_{config_id}",
            pre_aggregated_bars=sc.bars,
            precomputed_structure=sc.structure,
            precomputed_atr=sc.atr,
            precomputed_zones_by_src={"fvg": sc.fvg_zones,
                                       "ifvg": sc.ifvg_zones},
            side_table=sc.side_table,
        )
        backtest_s = time.perf_counter() - t1
        row = _summary_row(res.trades, path.name,
                           config_id, backtest_s, params)
        row["cache_build_s"] = float(cache_build_s)
        return row
    except Exception as exc:
        print(f"  ! {path.name} cfg={config_id} FAILED: {exc}",
              flush=True)
        import traceback
        traceback.print_exc()
        return {
            "file": path.name,
            "config_id": config_id,
            "ms_pivot_len": int(params["ms_pivot_len"]),
            "ms_liquidity_len": int(params["ms_liquidity_len"]),
            "ms_resample_secs": int(params["ms_resample_secs"]),
            "fvg_invalidate_on_structure": bool(params["fvg_invalidate_on_structure"]),
            "fvg_structure_invalidation_age_secs": int(params["fvg_structure_invalidation_age_secs"]),
            "n_trades": 0, "total_pnl_usd": 0.0, "ev_per_trade_usd": 0.0,
            "win_rate_pct": 0.0, "total_fees_usd": 0.0, "net_pnl_usd": 0.0,
            "elapsed_s": 0.0, "n_sl": 0, "n_tp": 0, "n_inv": 0, "n_eod": 0,
            "cache_build_s": 0.0,
        }
