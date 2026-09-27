"""nb53 worker module — ThreadPoolExecutor + per-file SweepCache.

Body-only A/B worker: runs the canonical v17 BTC SNIPER recipe
twice per file — once with body-only mitigation + invalidation
ON (the new canonical ``v17-btc-sniper-2026-09-26b``), once
with both OFF (the legacy recipe).

Same ThreadPoolExecutor + per-file cache pattern as
``_nb52_worker.py`` (numpy releases the GIL on ufuncs; the
bar loop spends ~95% inside numpy ops; the 6× RAM duplication
of ProcessPoolExecutor is what crashed the OOM on Windows).
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

from src.core.optimal_config import optimal_params                            # noqa: E402
from src.tick.cache import get_or_build                                       # noqa: E402
from src.tick.tick_backtest import run_tick_backtest                          # noqa: E402


# Two parameter sets: the new canonical (body-only ON) and the
# legacy recipe (body-only OFF). Both produce a separate SweepCache
# because the detector fingerprint differs (body-only flags are
# in the cache fingerprint).
PARAM_NEW = dict(fvg_body_only_mitigation=True,
                 fvg_body_only_invalidation=True)
PARAM_OLD = dict(fvg_body_only_mitigation=False,
                 fvg_body_only_invalidation=False)


# In-process cache: (file_path, params_fingerprint) -> SweepCache.
# Two configs per file = two distinct fingerprints = two caches
# per file in the parent process.
_INPROCESS_CACHE: dict = {}


def _param_key(p) -> tuple:
    """A short, hashable key for a TrendStrategyParams instance.

    The detector-relevant knobs (the cache fingerprint) are:
    body-only flags, min_zone_usd, mit_distance, strict-wick,
    resample_secs, etc. For THIS validation we only need to
    distinguish the two A/B configs, so a tuple of just the two
    body-only flags is sufficient. If we ever extend this to
    other detector-knob A/Bs, expand the tuple.
    """
    return (bool(getattr(p, "fvg_body_only_mitigation", False)),
            bool(getattr(p, "fvg_body_only_invalidation", False)))


def _get_sc(file_path: Path, p, with_side_table: bool = True):
    """Return the SweepCache for (file, params), building on miss."""
    key = (file_path, _param_key(p), with_side_table)
    sc = _INPROCESS_CACHE.get(key)
    if sc is not None:
        return sc
    sc = get_or_build(file_path, p, with_side_table=with_side_table,
                      verbose=False)
    _INPROCESS_CACHE[key] = sc
    return sc


def _summary_row(trades, file_name: str, label: str,
                  elapsed_s: float, body_only: str) -> dict:
    """Build a compact summary dict from a list of Trade."""
    pnls = [t.pnl_usd for t in trades]
    fees = [t.fee_usd for t in trades]
    n = len(trades)
    by_reason: dict = {}
    for t in trades:
        by_reason.setdefault(t.exit_reason, []).append(t.pnl_usd)
    row = {
        "file": file_name,
        "label": label,
        "body_only": body_only,
        "n_trades": n,
        "total_pnl_usd": float(sum(pnls)) if pnls else 0.0,
        "ev_per_trade_usd": float(sum(pnls) / n) if n else 0.0,
        "win_rate_pct": 100.0 * sum(1 for p in pnls if p > 0) / n if n else 0.0,
        "total_fees_usd": float(sum(fees)) if fees else 0.0,
        "net_pnl_usd": float(sum(p - f for p, f in zip(pnls, fees))) if pnls else 0.0,
        "elapsed_s": float(elapsed_s),
        "n_sl": len(by_reason.get("sl", [])),
        "n_tp": len(by_reason.get("tp", [])),
        "n_inv": len(by_reason.get("inv", [])),
        "n_eod": len(by_reason.get("eod", [])),
    }
    return row


def worker_body_only_ab(args: Tuple[Path, str]) -> dict:
    """Run BOTH canonical recipes on a single file.

    Args is ``(file_path, body_only)`` where ``body_only`` is
    ``"new"`` (canonical with body-only=True) or ``"old"``
    (legacy with body-only=False). Returns the per-run summary
    dict. Parent process owns the cache; this worker just calls
    ``_get_sc`` and runs the bar loop.

    Wall time per call: ~110s cold cache build OR ~3s warm hit
    OR ~3s backtest (after the cache is loaded).
    """
    path, mode = args
    if mode == "new":
        overrides = PARAM_NEW
        body_only_label = "ON"
    elif mode == "old":
        overrides = PARAM_OLD
        body_only_label = "OFF"
    else:
        raise ValueError(f"mode must be 'new' or 'old', got {mode!r}")
    try:
        t0 = time.perf_counter()
        p = optimal_params(**overrides)
        sc = _get_sc(path, p, with_side_table=True)
        cache_build_s = time.perf_counter() - t0
        t1 = time.perf_counter()
        res = run_tick_backtest(
            raw_df=None, p=p, strategy_label=f"nb53_6mo_{mode}",
            pre_aggregated_bars=sc.bars,
            precomputed_structure=sc.structure,
            precomputed_atr=sc.atr,
            precomputed_zones_by_src={"fvg": sc.fvg_zones,
                                       "ifvg": sc.ifvg_zones},
            side_table=sc.side_table,
        )
        backtest_s = time.perf_counter() - t1
        row = _summary_row(res.trades, path.name,
                           f"nb53_6mo_{mode}", backtest_s,
                           body_only_label)
        row["cache_build_s"] = float(cache_build_s)
        return row
    except Exception as exc:
        print(f"  ! {path.name} mode={mode} FAILED: {exc}", flush=True)
        return {
            "file": path.name, "label": f"nb53_6mo_{mode}",
            "body_only": body_only_label,
            "n_trades": 0, "total_pnl_usd": 0.0, "ev_per_trade_usd": 0.0,
            "win_rate_pct": 0.0, "total_fees_usd": 0.0, "net_pnl_usd": 0.0,
            "elapsed_s": 0.0, "n_sl": 0, "n_tp": 0, "n_inv": 0, "n_eod": 0,
            "cache_build_s": 0.0,
        }
