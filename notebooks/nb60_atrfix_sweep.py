"""NB60 ATR-mult sweep with the 2026-09-27 ATR fix — parallelized.

Re-runs the NB60 SL/TP sweep with the corrected ATR (now on 1-min
bars, ~25-30x larger than the buggy 1-second-bar value).

Multipliers (small because corrected ATR is ~25-30x larger):
  sl_atr_mult in {0.5, 1.0, 1.5, 2.0, 3.0, 4.0}
  tp_atr_mult in {1.1, 2.2, 4.4, 8.8, 17.6, 35.2}

= 36 cells per month x 6 months = 216 runs.

Parallelism: 6-way across months (one thread per file). Within
each thread, cells are sequential (warm cache hit ~3s/cell).
NB60 doesn't need the tick side_table — pass with_side_table=False
to skip the 150MB artefact and speed up cache load.

Output: notebooks/nb60_atrfix_outputs/
"""
from __future__ import annotations

import sys
import time
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace as dc_replace, asdict

sys.path.insert(0, '.')
import numpy as np
import pandas as pd

from src.tick.cache import get_or_build
from src.core.optimal_config import optimal_params, OPTIMAL_RECIPE_VERSION

NOTEBOOK = "nb60_atrfix"
RAW_ROOT = Path(r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw')
MONTHS = ['2025-04', '2025-05', '2025-10', '2025-11', '2026-02', '2026-05']
OUT_DIR = Path(f"notebooks/{NOTEBOOK}_outputs")
OUT_DIR.mkdir(parents=True, exist_ok=True)
JSONL = OUT_DIR / f"{NOTEBOOK}__runs.jsonl"

SL_TP_GRID = []
for sl in [0.5, 1.0, 1.5, 2.0, 3.0, 4.0]:
    for tp in [1.1, 2.2, 4.4, 8.8, 17.6, 35.2]:
        SL_TP_GRID.append((sl, tp))


def _to_jsonable(obj):
    """Convert numpy scalars / dataclass dicts to JSON-safe types."""
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, float) and (obj != obj):  # NaN
        return None
    return obj


def write_run(sl_mult, tp_mult, month, res, params):
    n = len(res.trades)
    p = asdict(params) if hasattr(params, '__dataclass_fields__') else params
    if n == 0:
        record = _empty_record(sl_mult, tp_mult, month)
    else:
        pnls_net = [t.pnl_net_usd for t in res.trades]
        pnls_gross = [t.pnl_gross_usd for t in res.trades]
        fees = [t.fee_usd for t in res.trades]
        directions = [t.direction for t in res.trades]
        n_long = sum(1 for d in directions if d > 0)
        n_short = sum(1 for d in directions if d < 0)
        wins = [v for v in pnls_net if v > 0]
        losses = [v for v in pnls_net if v <= 0]
        median_win = float(np.median(wins)) if wins else 0.0
        median_loss = float(np.median(losses)) if losses else 0.0
        mean_win = float(np.mean(wins)) if wins else 0.0
        mean_loss = float(np.mean(losses)) if losses else 0.0
        largest_win = float(max(pnls_net)) if pnls_net else 0.0
        largest_loss = float(min(pnls_net)) if pnls_net else 0.0
        exit_breakdown = {}
        for t in res.trades:
            exit_breakdown[t.exit_reason] = exit_breakdown.get(t.exit_reason, 0) + 1

        record = {
            "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "notebook": NOTEBOOK,
            "scenario": f"sl={sl_mult}_tp={tp_mult}",
            "scope": [month],
            "engine": "bar",
            "comments": (f"NB60 ATR-mult sweep with corrected ATR (1m bars, 2026-09-27). "
                         f"sl_atr_mult={sl_mult}, tp_atr_mult={tp_mult}, RR={tp_mult/sl_mult:.1f}, month={month}."),
            "hypothesis": "Corrected ATR means sane SL/TP at sl_atr_mult in {0.5..4}.",
            "verdict": "inconclusive",
            "canonical_recipe_version": OPTIMAL_RECIPE_VERSION,
            "params": p,
            "overrides_vs_canonical": {"sl_atr_mult": sl_mult, "tp_atr_mult": tp_mult, "sl_tp_mode": "atr_mult"},
            "metrics": {
                "n_trades": n,
                "n_long": n_long,
                "n_short": n_short,
                "months_tested": [month],
                "n_signals_emitted": getattr(res, 'n_signals_emitted', 0),
                "win_rate_pct": 100.0 * len(wins) / n,
                "pnl_gross_usd": sum(pnls_gross),
                "fees_paid_usd": sum(fees),
                "pnl_net_usd": sum(pnls_net),
                "ev_per_trade_usd": sum(pnls_net) / n,
                "mean_win_usd": mean_win,
                "mean_loss_usd": mean_loss,
                "median_win_usd": median_win,
                "median_loss_usd": median_loss,
                "largest_win_usd": largest_win,
                "largest_loss_usd": largest_loss,
                "profit_factor": (sum(wins) / abs(sum(losses))) if losses else float('inf'),
                "payoff_ratio": abs(mean_win / mean_loss) if mean_loss else 0.0,
                "max_drawdown_usd": 0.0,
                "max_drawdown_pct": 0.0,
                "trades_per_day": n / 30.0,
                "median_hold_secs": float(np.median([t.hold_secs for t in res.trades])),
                "mean_hold_secs": float(np.mean([t.hold_secs for t in res.trades])),
                "exit_reason_breakdown": exit_breakdown,
                "taker_bps_charged": 5.0,
                "sl_atr_mult": sl_mult,
                "tp_atr_mult": tp_mult,
                "rr": tp_mult / sl_mult,
            }
        }
    with JSONL.open("a") as fh:
        fh.write(json.dumps(_to_jsonable(record)) + "\n")


def _empty_record(sl_mult, tp_mult, month):
    return {
        "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "notebook": NOTEBOOK,
        "scenario": f"sl={sl_mult}_tp={tp_mult}",
        "scope": [month],
        "engine": "bar",
        "comments": f"No trades for sl={sl_mult}, tp={tp_mult}, month={month}.",
        "hypothesis": "",
        "verdict": "inconclusive",
        "canonical_recipe_version": OPTIMAL_RECIPE_VERSION,
        "params": {},
        "overrides_vs_canonical": {"sl_atr_mult": sl_mult, "tp_atr_mult": tp_mult, "sl_tp_mode": "atr_mult"},
        "metrics": {
            "n_trades": 0, "n_long": 0, "n_short": 0,
            "months_tested": [month],
            "win_rate_pct": 0.0, "pnl_gross_usd": 0.0, "fees_paid_usd": 0.0, "pnl_net_usd": 0.0,
            "ev_per_trade_usd": 0.0, "mean_win_usd": 0.0, "mean_loss_usd": 0.0,
            "median_win_usd": 0.0, "median_loss_usd": 0.0, "largest_win_usd": 0.0, "largest_loss_usd": 0.0,
            "profit_factor": 0.0, "payoff_ratio": 0.0, "max_drawdown_usd": 0.0, "max_drawdown_pct": 0.0,
            "trades_per_day": 0.0, "median_hold_secs": 0.0, "mean_hold_secs": 0.0,
            "exit_reason_breakdown": {}, "taker_bps_charged": 5.0,
            "sl_atr_mult": sl_mult, "tp_atr_mult": tp_mult, "rr": tp_mult / sl_mult,
        }
    }


def run_month_thread(month, sl_tp_grid, p):
    """Run all cells for one month sequentially. Returns list of (month, sl, tp, net, n, wr)."""
    from notebooks.nb60_vwap_limit import run_vwap_limit_backtest
    raw_path = RAW_ROOT / f'BTCUSDT-aggTrades-{month}.parquet'

    t0 = time.perf_counter()
    sc = get_or_build(raw_path, p, with_side_table=False, verbose=False)
    load_s = time.perf_counter() - t0
    print(f"  [{month}] cache load: {load_s:.1f}s, ATR=${float(np.median(sc.atr)):.2f}, FVG={len(sc.fvg_zones)}", flush=True)

    rows = []
    for sl_mult, tp_mult in sl_tp_grid:
        # Need fresh zone clones each cell (in-place mutation by run_vwap_limit_backtest)
        zones = [dc_replace(z) for z in sc.fvg_zones]
        t0 = time.perf_counter()
        res, _ = run_vwap_limit_backtest(
            sc.bars, zones,
            qty_btc=0.001, sl_usd=20.0, tp_usd=200.0,
            atr=sc.atr, sl_atr_mult=sl_mult, tp_atr_mult=tp_mult,
            sl_tp_mode='atr_mult',
        )
        cell_s = time.perf_counter() - t0
        write_run(sl_mult, tp_mult, month, res, p)
        n = len(res.trades)
        if n > 0:
            pnls_net = [t.pnl_net_usd for t in res.trades]
            total_net = sum(pnls_net)
            wr = 100.0 * sum(1 for v in pnls_net if v > 0) / n
        else:
            total_net = 0.0; wr = 0.0
        rows.append({
            'month': month, 'sl_atr_mult': sl_mult, 'tp_atr_mult': tp_mult,
            'rr': tp_mult/sl_mult, 'n_trades': n,
            'wr': wr, 'net': total_net, 'ev': total_net / max(1, n),
            'cell_s': cell_s,
        })
    return rows


def main():
    JSONL.unlink(missing_ok=True)
    print(f"[{NOTEBOOK}] Parallel sweep: 6 months x {len(SL_TP_GRID)} cells = {len(SL_TP_GRID)*len(MONTHS)} runs")
    p = optimal_params()

    t_overall = time.perf_counter()
    all_rows = []
    # NB60 backtest doesn't need side_table (no tick fill). 6-way parallel across months.
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(run_month_thread, m, SL_TP_GRID, p): m for m in MONTHS}
        for fut in as_completed(futures):
            month = futures[fut]
            try:
                rows = fut.result()
                all_rows.extend(rows)
                total = sum(r['net'] for r in rows)
                print(f"  [{month}] DONE: {len(rows)} cells, total_net=${total:+.2f}", flush=True)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print(f"  [{month}] FAILED: {exc}", flush=True)
    elapsed = time.perf_counter() - t_overall
    print(f"\nTotal wall: {elapsed:.1f}s ({elapsed/60:.1f} min)")

    # Aggregate
    df = pd.DataFrame(all_rows)
    df.to_csv(OUT_DIR / "nb60_atrfix_per_cell.csv", index=False)

    summary = df.groupby(['sl_atr_mult', 'tp_atr_mult']).agg(
        total_n=('n_trades', 'sum'),
        avg_wr=('wr', 'mean'),
        total_net=('net', 'sum'),
        avg_ev=('ev', 'mean'),
        max_cell_s=('cell_s', 'max'),
    ).reset_index().sort_values('total_net', ascending=False)
    summary.to_csv(OUT_DIR / "nb60_atrfix_summary.csv", index=False)

    print("\n=== Summary (sorted by 6-month total net) ===")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
