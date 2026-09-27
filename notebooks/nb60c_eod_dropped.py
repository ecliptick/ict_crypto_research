"""NB60c — re-run tiny-k winner with EOD exits dropped (crypto has no EOD).

The nb60_vwap_limit.py backtest force-closes any trade still open at the
LAST BAR of the monthly file with exit_reason="eod". On crypto (24/7
trading, no daily settlement) there is no end-of-day: those trades would
have continued until SL/TP/soft-SL fired.

This script:
  * Re-runs the winner cells from nb60b_tinyk_atrfix.py
  * Drops the EOD-closed trades from the PnL tally
  * Reports the corrected net per month and 6mo aggregate
"""

import sys
import os
sys.stdout.reconfigure(line_buffering=True)
os.environ["PYTHONUNBUFFERED"] = "1"

import time
from dataclasses import replace as dc_replace
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(r"C:\coding\ict_crypto_research")
sys.path.insert(0, str(REPO_ROOT))

from src.core.optimal_config import optimal_params
from src.tick.cache import get_or_build
from notebooks.nb60_vwap_limit import (
    run_vwap_limit_backtest,
    SIGMA_VALUES,
    ORDER_LIFETIME_SECS,
)

MONTHS = ["2025-04", "2025-05", "2025-10", "2025-11", "2026-02", "2026-05"]
RAW_ROOT = Path(r"C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw")
OUT_DIR = REPO_ROOT / "notebooks" / "nb60_atrfix_outputs"


def run_cell_eod_dropped(sl: float, tp: float) -> list[dict]:
    rows = []
    for m in MONTHS:
        raw = RAW_ROOT / f"BTCUSDT-aggTrades-{m}.parquet"
        p = optimal_params()
        sc = get_or_build(raw, p, with_side_table=False, verbose=False)
        zones = [dc_replace(z) for z in sc.fvg_zones]
        res, _ = run_vwap_limit_backtest(
            sc.bars, zones,
            qty_btc=0.001, sl_usd=20.0, tp_usd=200.0,
            atr=sc.atr,
            sl_atr_mult=sl, tp_atr_mult=tp,
            sl_tp_mode="atr_mult",
            sigma_values=SIGMA_VALUES,
            order_lifetime_secs=1800,
        )
        kept = [t for t in res.trades if t.exit_reason != "eod"]
        gross = sum(t.pnl_gross_usd for t in kept)
        fees = sum(t.fee_usd for t in kept)
        net = sum(t.pnl_net_usd for t in kept)
        n_kept = len(kept)
        n_total = len(res.trades)
        wr = 100.0 * sum(1 for t in kept if t.pnl_net_usd > 0) / max(1, n_kept)
        rows.append({
            "month": m, "n_total": n_total, "n_kept": n_kept,
            "gross": gross, "fees": fees, "net": net, "wr": wr,
            "atr_median": float(np.median(sc.atr)),
            "sl_usd_at_med_atr": sl * float(np.median(sc.atr)),
            "tp_usd_at_med_atr": tp * float(np.median(sc.atr)),
        })
    return rows


def main():
    cells = [(8.0, 64.0), (8.0, 32.0), (4.0, 64.0), (4.0, 32.0), (2.0, 64.0)]
    print(f"NB60c — tiny-k cells, EOD exits dropped, 6mo aggregate\n"
          f"  Cells: {len(cells)}")
    print(f"  ATR-mult mode; SL/TP scales with per-bar ATR\n")

    all_rows = []
    t0 = time.perf_counter()
    for sl, tp in cells:
        print(f"\n=== (sl={sl}, tp={tp}) ===")
        print(f"{'month':<10} {'n_total':>7} {'n_kept':>7} {'gross':>9} "
              f"{'fees':>7} {'net':>9} {'WR%':>6}")
        rows = run_cell_eod_dropped(sl, tp)
        for r in rows:
            print(f"{r['month']:<10} {r['n_total']:>7} {r['n_kept']:>7} "
                  f"${r['gross']:>+8.2f} ${r['fees']:>6.2f} "
                  f"${r['net']:>+8.2f} {r['wr']:>5.1f}%")
            r["sl_atr"] = sl
            r["tp_atr"] = tp
            all_rows.append(r)
        elapsed = time.perf_counter() - t0
        print(f"  ({elapsed:.1f}s elapsed)", flush=True)

    df = pd.DataFrame(all_rows)
    out = OUT_DIR / "nb60c_eod_dropped_per_month.csv"
    df.to_csv(out, index=False)
    print(f"\nSaved {out}")

    # 6mo aggregate
    agg = df.groupby(["sl_atr", "tp_atr"]).agg(
        n_kept=("n_kept", "sum"),
        n_total=("n_total", "sum"),
        gross=("gross", "sum"),
        fees=("fees", "sum"),
        net=("net", "sum"),
        avg_wr=("wr", "mean"),
        atr_median=("atr_median", "median"),
    ).reset_index().sort_values("net", ascending=False)
    agg["pos_mo"] = df.groupby(["sl_atr", "tp_atr"])["net"].apply(
        lambda s: (s > 0).sum()
    ).reindex(agg.index)
    agg["sl_usd_at_med_atr"] = agg["sl_atr"] * agg["atr_median"]
    agg["tp_usd_at_med_atr"] = agg["tp_atr"] * agg["atr_median"]

    print(f"\n{'sl_atr':>7} {'tp_atr':>7} {'SL_USD':>8} {'TP_USD':>8} "
          f"{'n_kept':>7} {'avg_WR%':>8} {'gross':>10} {'fees':>7} "
          f"{'net':>10} {'pos_mo':>6}")
    for _, r in agg.iterrows():
        print(f"{r['sl_atr']:>7} {r['tp_atr']:>7} "
              f"${r['sl_usd_at_med_atr']:>7.2f} ${r['tp_usd_at_med_atr']:>7.2f} "
              f"{int(r['n_kept']):>7} {r['avg_wr']:>7.1f} "
              f"${r['gross']:>+9.2f} ${r['fees']:>6.2f} "
              f"${r['net']:>+9.2f} {int(r['pos_mo']):>6}")

    out2 = OUT_DIR / "nb60c_eod_dropped_summary.csv"
    agg.to_csv(out2, index=False)
    print(f"\nSaved {out2}")


if __name__ == "__main__":
    main()
