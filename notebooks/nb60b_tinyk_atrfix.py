"""NB60b - tiny-k + ATR-mult sweep on corrected ATR, with cache reuse.

Tests the hypothesis: NB60's tiny-k cell ((0.3, 0.5, 0.7, 1.0, 1.5)) is the
original winner from nb60_vwap_limit.py. With corrected ATR ($62 median),
the historical (48, 384) multipliers map to dollar distances that are
~30x too wide. Sweep (sl_atr, tp_atr) where resulting distances are
bounded by reasonable BTC 1s SL/TP.

Key optimizations:
  * Pre-warm all 6 monthly caches ONCE (no rebuilds per cell).
  * Cap TP/sl_atr at sensible dollar distances to avoid 30+min hangs.
  * Skip cells where tp_atr > 100 (mapped to > $6,000 TP distance).
  * Unbuffered stdout for live progress.
  * Per-cell wall-clock timing.
"""

import sys
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace as dc_replace
from pathlib import Path

# Force unbuffered stdout (Windows + python -X utf8)
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)
os.environ["PYTHONUNBUFFERED"] = "1"

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

RAW_ROOT = Path(r"C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw")
MONTHS = ["2025-04", "2025-05", "2025-10", "2025-11", "2026-02", "2026-05"]
OUT_DIR = REPO_ROOT / "notebooks" / "nb60_atrfix_outputs"


def prewarm_caches(verbose: bool = True) -> dict[str, "SweepCache"]:  # noqa: F821
    t0 = time.perf_counter()
    caches = {}
    for m in MONTHS:
        raw = RAW_ROOT / f"BTCUSDT-aggTrades-{m}.parquet"
        p = optimal_params()
        sc = get_or_build(raw, p, with_side_table=False, verbose=False)
        caches[m] = sc
        if verbose:
            print(f"  {m}: cache warm, ATR median=${np.median(sc.atr):.2f}, "
                  f"zones={len(sc.fvg_zones)}")
    if verbose:
        print(f"All 6 caches warm in {time.perf_counter()-t0:.1f}s\n")
    return caches


def run_cell(
    caches: dict,
    sl_atr: float,
    tp_atr: float,
    sigma_values=SIGMA_VALUES,
    lifetime=1800,
) -> list[dict]:
    rows = []
    for m, sc in caches.items():
        zones = [dc_replace(z) for z in sc.fvg_zones]
        res, _ = run_vwap_limit_backtest(
            sc.bars, zones,
            qty_btc=0.001, sl_usd=20.0, tp_usd=200.0,
            atr=sc.atr,
            sl_atr_mult=sl_atr, tp_atr_mult=tp_atr,
            sl_tp_mode="atr_mult",
            sigma_values=sigma_values,
            order_lifetime_secs=lifetime,
        )
        n = len(res.trades)
        pnls_net = [t.pnl_net_usd for t in res.trades]
        pnls_gross = [t.pnl_gross_usd for t in res.trades]
        fees = [t.fee_usd for t in res.trades]
        rows.append({
            "month": m, "n": n,
            "gross": sum(pnls_gross),
            "fees": sum(fees),
            "net": sum(pnls_net),
            "wr": 100.0 * sum(1 for v in pnls_net if v > 0) / max(1, n),
            "ev": sum(pnls_net) / max(1, n),
        })
    return rows


def main():
    # Cap TP at $5,000 nominal at median ATR $62.24. That's tp_atr_mult <= 80.
    # Cap SL at $2,000 nominal (per-trade risk on 0.001 BTC). That's sl_atr_mult <= 32.
    # Tested timing on warm cache (2025-11):
    #   (4, 35.2)  → 6.5s   ← current sweep winner
    #   (8, 70.4)  → 21s    ← 3x slower due to long hold times
    #   (32, 80)   → >5min  ← pathological, skip
    sl_grid = [2, 4, 8]
    tp_grid = [4, 8, 16, 32, 64]  # 5 cells
    combos = [(s, t) for s in sl_grid for t in tp_grid]
    print(f"Grid: {len(combos)} (sl, tp) cells × 6 months = {len(combos)*6} runs")
    print(f"SIGMA_VALUES = {SIGMA_VALUES} (tiny-k)")
    print(f"lifetime = {ORDER_LIFETIME_SECS}s")
    print(f"Estimated wall time: {len(combos)*6*8/4/60:.1f} min (8s/cell, 4 workers)\n")

    # 1) Pre-warm caches ONCE.
    caches = prewarm_caches()

    # 2) Run all combos in parallel, 4 workers.
    print("Running sweep ...")
    t0 = time.perf_counter()
    all_results = []
    completed = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(run_cell, caches, sl, tp): (sl, tp)
                   for sl, tp in combos}
        for fut in as_completed(futures):
            sl, tp = futures[fut]
            rows = fut.result()
            all_results.append((sl, tp, rows))
            completed += 1
            elapsed = time.perf_counter() - t0
            print(f"  [{completed}/{len(combos)}] (sl={sl}, tp={tp}) done "
                  f"@ {elapsed:.1f}s", flush=True)

    elapsed = time.perf_counter() - t0
    print(f"Sweep done in {elapsed:.1f}s ({elapsed/len(combos):.1f}s/cell avg)\n")

    # 3) Aggregate and print.
    print(f"{'sl':>5} {'tp':>6} {'RR':>5} {'total_n':>8} {'avg_WR%':>8} "
          f"{'gross':>9} {'fees':>7} {'net':>9} {'pos_mo':>6}")
    agg_rows = []
    for sl, tp, rows in all_results:
        total_n = sum(r["n"] for r in rows)
        avg_wr = np.mean([r["wr"] for r in rows])
        total_gross = sum(r["gross"] for r in rows)
        total_fees = sum(r["fees"] for r in rows)
        total_net = sum(r["net"] for r in rows)
        pos_mo = sum(1 for r in rows if r["net"] > 0)
        print(f"{sl:>5} {tp:>6} {tp/sl:>5.2f} {total_n:>8} {avg_wr:>7.1f} "
              f"${total_gross:>+8.2f} ${total_fees:>6.2f} ${total_net:>+8.2f} "
              f"{pos_mo:>6}")
        agg_rows.append({
            "sl_atr": sl, "tp_atr": tp, "rr": tp/sl,
            "total_n": total_n, "avg_wr": avg_wr,
            "total_gross": total_gross, "total_fees": total_fees,
            "total_net": total_net, "pos_mo": pos_mo,
        })

    df = pd.DataFrame(agg_rows).sort_values("total_net", ascending=False)
    out = OUT_DIR / "nb60b_tinyk_atrfix_summary.csv"
    df.to_csv(out, index=False)
    print(f"\nTop 5 cells by 6mo net:")
    print(df.head(5).to_string(index=False))
    print(f"\nSaved to {out}")


if __name__ == "__main__":
    main()
