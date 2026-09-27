"""NB56 — SL/TP optimization targeting faster loss-cutting.

Problem from prior run:
  C1 (qty=0.01, 10x/20x) has mean hold_secs on SL = 6088s (~1.7h)
  on 2025-04. Losses are held too long.

User ask: "optimize TP/SL so we don't hold our losses so much"

Approach:
  * Tighten SL: 1x zone_mult (vs 10x on C1, 5x on baseline)
    -> cuts losses faster, but must still clear fees
  * Keep TP wide or widen it: 5x, 10x, 20x zone_mult
    -> give winners room to run
  * Resulting RR ratios are asymmetric: 1:5, 1:10, 1:20
    -> fewer wins needed to be profitable

Design principles:
  1. SL must be wide enough that TP_gross > fee_usd per trade
     (otherwise even TP winners are fee-biters)
  2. ATR-scaled SL/TP modes are excluded (atr_mult path was
     uniformly negative on the 6-month nb53 sweep)
  3. qty_btc=0.001 (canonical) to keep fee variance controlled;
     scale patterns are well understood

Grid (8 cells):
  Months: 2025-04, 2025-11

  Cell  | sl_zm | tp_zm | RR   | Notes
  ------|-------|-------|------|-------
  S1    |  1.0  |  5.0  | 1:5  | Tight SL, medium TP (fee check)
  S2    |  1.0  | 10.0  | 1:10 | Tight SL, wide TP
  S3    |  1.0  | 20.0  | 1:20 | Tight SL, very wide TP
  S4    |  2.0  | 10.0  | 1:5  | Medium SL, wide TP
  S5    |  2.0  | 20.0  | 1:10 | Medium SL, very wide TP
  S6    |  3.0  | 15.0  | 1:5  | Medium-tighter SL, medium TP
  S7    |  5.0  | 10.0  | 1:2  | Symmetric-ish (close to 1:2 user idea)
  C1    | 10.0  | 20.0  | 1:2  | prior run best (10x/20x, qty=0.01)
  base  |  5.0  | 60.0  | 1:12 | canonical sniper
"""
import sys
import time
import warnings
from collections import Counter
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT = Path(".").resolve()
for _p in [ROOT, *ROOT.parents]:
    if (_p / "src" / "core" / "ict_signals.py").is_file():
        ROOT = _p
        break
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "notebooks"))

import pandas as pd

from src.core.optimal_config import optimal_params, OPTIMAL_RECIPE_VERSION
from src.tick.cache import get_or_build
from src.tick.tick_backtest import run_tick_backtest

NOTEBOOK = "nb56_loss_cut"
SCENARIO = "sl_tp_loss_cut_optimization"

OUT_DIR = ROOT / "notebooks" / "nb56_loss_cut_outputs"
OUT_DIR.mkdir(parents=True, exist_ok=True)

SOURCE_DATA_ROOT = Path(r"C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw")

# (label, qty_btc, sl_zone_mult, tp_zone_mult)
CELLS = [
    # ── Tight-SL / wide-TP cells (primary test)
    ("S1_1x5x",   0.001,  1.0,  5.0),   # 1:5
    ("S2_1x10x",  0.001,  1.0, 10.0),   # 1:10
    ("S3_1x20x",  0.001,  1.0, 20.0),   # 1:20
    ("S4_2x10x",  0.001,  2.0, 10.0),   # 1:5
    ("S5_2x20x",  0.001,  2.0, 20.0),   # 1:10
    ("S6_3x15x",  0.001,  3.0, 15.0),   # 1:5
    ("S7_5x10x",  0.001,  5.0, 10.0),   # ~1:2
    # ── Reference cells
    ("C1_10x20x_qty001",  0.001, 10.0, 20.0),  # C1 equiv at qty=0.001
    ("base_5x60x",        0.001,  5.0, 60.0),  # canonical
]

MONTHS = ["2025-04", "2025-11"]


def main():
    print(f"[{NOTEBOOK}] SL/TP loss-cut optimization")
    print(f"recipe: {OPTIMAL_RECIPE_VERSION}")
    print(f"months: {MONTHS}\n")

    rows = []
    grand_t0 = time.perf_counter()

    for month in MONTHS:
        path = SOURCE_DATA_ROOT / f"BTCUSDT-aggTrades-{month}.parquet"
        if not path.exists():
            print(f"  WARN: {path} not found, skip", flush=True)
            continue

        # Build one cache per month (detector-only params; qty/sltp irrelevant)
        p_cache = optimal_params(qty_btc=0.001)
        t0 = time.perf_counter()
        sc = get_or_build(path, p_cache, with_side_table=True, verbose=False)
        print(f"[{month}] cache={time.perf_counter()-t0:.1f}s "
              f"fvg={len(sc.fvg_zones)} ifvg={len(sc.ifvg_zones)}\n", flush=True)

        for label, qty, sl_zm, tp_zm in CELLS:
            p_i = optimal_params(
                qty_btc=qty,
                fvg_inv_trade_sl_zone_mult=float(sl_zm),
                fvg_inv_trade_tp_zone_mult=float(tp_zm),
            )

            t0 = time.perf_counter()
            res = run_tick_backtest(
                raw_df=None, p=p_i,
                strategy_label=f"{NOTEBOOK}_{label}",
                pre_aggregated_bars=sc.bars,
                precomputed_zones_by_src={"fvg": sc.fvg_zones,
                                          "ifvg": sc.ifvg_zones},
                precomputed_structure=sc.structure,
                precomputed_atr=sc.atr,
                side_table=sc.side_table,
            )
            t_bt = time.perf_counter() - t0

            trades = list(res.trades)
            pnls = [t.pnl_usd for t in trades]
            fees  = [getattr(t, "fee_usd", 0.0) for t in trades]
            nets  = [p - f for p, f in zip(pnls, fees)]
            n = len(trades)
            n_wins = sum(1 for v in nets if v > 0)
            wr = 100.0 * n_wins / n if n else 0.0
            ev = sum(nets) / n if n else 0.0
            med_hold = pd.Series([getattr(t, "hold_secs", 0.0) for t in trades]).median()
            med_sl = pd.Series([getattr(t, "stop_usd", 0.0) for t in trades]).median()
            med_tp = pd.Series([getattr(t, "target_usd", 0.0) for t in trades]).median()
            exits = Counter(getattr(t, "exit_reason", "") for t in trades)
            n_long = sum(1 for t in trades if getattr(t, "direction", 0) > 0)
            n_short = sum(1 for t in trades if getattr(t, "direction", 0) < 0)
            med_fee = pd.Series(fees).median()
            rr = tp_zm / sl_zm

            print(f"  [{month}] {label:22s} | n={n:3d} | "
                  f"net=${sum(nets):+8.2f} gross=${sum(pnls):+8.2f} "
                  f"fees=${sum(fees):6.2f} | "
                  f"WR={wr:5.1f}% EV=${ev:+.4f} | "
                  f"RR=1:{rr:.1f} | "
                  f"med_SL=${med_sl:6.1f} med_TP=${med_tp:7.1f} | "
                  f"med_hold={med_hold:7.0f}s | "
                  f"bt={t_bt:.1f}s", flush=True)

            for ti, t in enumerate(trades):
                rows.append({
                    "file": month, "cell": label,
                    "trade_idx": ti,
                    "direction": int(getattr(t, "direction", 0)),
                    "entry_price": float(getattr(t, "entry_price", 0.0)),
                    "exit_price": float(getattr(t, "exit_price", 0.0)),
                    "stop_usd": float(getattr(t, "stop_usd", 0.0)),
                    "target_usd": float(getattr(t, "target_usd", 0.0)),
                    "exit_reason": str(getattr(t, "exit_reason", "")),
                    "hold_secs": float(getattr(t, "hold_secs", 0.0)),
                    "pnl_usd": float(t.pnl_usd),
                    "fee_usd": float(getattr(t, "fee_usd", 0.0)),
                    "net_pnl_usd": float(t.pnl_usd) - float(getattr(t, "fee_usd", 0.0)),
                    "entry_triggered_by": str(getattr(t, "entry_triggered_by", "")),
                    "qty_btc": float(getattr(t, "qty_btc", 0.0)),
                    "rr_ratio": rr,
                    "sl_zone_mult": sl_zm, "tp_zone_mult": tp_zm,
                })

    elapsed = time.perf_counter() - grand_t0
    print(f"\n=== {NOTEBOOK} done. wall={elapsed:.1f}s ===", flush=True)

    if rows:
        df = pd.DataFrame(rows)
        csv = OUT_DIR / f"{NOTEBOOK}_per_trade.csv"
        df.to_csv(csv, index=False)
        print(f"  Saved {csv} ({len(df)} rows)", flush=True)

        # Per-cell summary
        summ = (df.groupby(["file", "cell"])
                  .agg(
                      n=("net_pnl_usd", "count"),
                      net=("net_pnl_usd", "sum"),
                      gross=("pnl_usd", "sum"),
                      fees=("fee_usd", "sum"),
                      wr=("net_pnl_usd", lambda s: 100*s.gt(0).mean()),
                      ev=("net_pnl_usd", "mean"),
                      med_hold=("hold_secs", "median"),
                      med_sl=("stop_usd", "median"),
                      med_tp=("target_usd", "median"),
                      rr=("rr_ratio", "first"),
                      sl_zm=("sl_zone_mult", "first"),
                      tp_zm=("tp_zone_mult", "first"),
                  )
                  .reset_index())
        summ_csv = OUT_DIR / f"{NOTEBOOK}_summary.csv"
        summ.to_csv(summ_csv, index=False)
        print(f"  Saved {summ_csv} ({len(summ)} rows)", flush=True)

        # 2-month aggregate
        agg = (df.groupby("cell")
                 .agg(
                     n=("net_pnl_usd", "count"),
                     net=("net_pnl_usd", "sum"),
                     gross=("pnl_usd", "sum"),
                     fees=("fee_usd", "sum"),
                     wr=("net_pnl_usd", lambda s: 100*s.gt(0).mean()),
                     ev=("net_pnl_usd", "mean"),
                     med_hold=("hold_secs", "median"),
                     med_sl=("stop_usd", "median"),
                     med_tp=("target_usd", "median"),
                     rr=("rr_ratio", "first"),
                     sl_zm=("sl_zone_mult", "first"),
                     tp_zm=("tp_zone_mult", "first"),
                     n_pos_files=("file", lambda s: s.nunique()),
                 )
                 .reset_index()
                 .sort_values("net", ascending=False))
        agg_csv = OUT_DIR / f"{NOTEBOOK}_aggregate.csv"
        agg.to_csv(agg_csv, index=False)
        print(f"  Saved {agg_csv} ({len(agg)} rows)", flush=True)
        print()
        print("=== 2-month aggregate (sorted by net PnL) ===")
        print(agg[["cell", "n", "net", "gross", "fees", "wr", "ev",
                   "med_hold", "sl_zm", "tp_zm", "rr"]].to_string(index=False))


if __name__ == "__main__":
    main()
