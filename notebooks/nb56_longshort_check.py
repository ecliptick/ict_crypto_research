"""Quick A/B for the long/short wins question.

User hypothesis: "do we win shorts or are long hold times only
because BTC is in a bull run?"

The canonical 5:60 zone_mult has median TP hold ~5-7h.
Question: does this TP hold mostly come from longs (riding
the BTC uptrend) or shorts?

This script runs canonical (5x/60x) on 6 monthly files and
reports n_long, n_short, n_long_tp, n_short_tp + their
respective PnL sums. No overrides — pure canonical.

Output: a single small CSV that proves/disproves the
"long TP bias from BTC bull run" question.
"""
import sys
import warnings
import time
from collections import Counter, defaultdict
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

from src.core.optimal_config import optimal_params
from src.tick.cache import get_or_build
from src.tick.tick_backtest import run_tick_backtest

NOTEBOOK = "nb56_longshort_check"
OUT_DIR = ROOT / "notebooks" / "nb56_longshort_check_outputs"
OUT_DIR.mkdir(parents=True, exist_ok=True)

SRC = Path(r"C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw")
MONTHS = ["2025-04", "2025-05", "2025-10", "2025-11", "2026-02", "2026-05"]


def main():
    p = optimal_params(qty_btc=0.001)
    p_longshort = optimal_params(
        qty_btc=0.001,
        fvg_inv_trade_sl_zone_mult=5.0,
        fvg_inv_trade_tp_zone_mult=60.0,
    )

    grand = defaultdict(lambda: Counter())
    print(f"[{NOTEBOOK}] {p_longshort.entry_mode} {p_longshort.fvg_inv_trade_sl_zone_mult}x/{p_longshort.fvg_inv_trade_tp_zone_mult}x qty=0.001")
    print(f"months: {MONTHS}\n")

    t_grand = time.perf_counter()
    per_trade = []
    for month in MONTHS:
        path = SRC / f"BTCUSDT-aggTrades-{month}.parquet"
        if not path.exists():
            print(f"  skip {month}: missing")
            continue
        sc = get_or_build(path, p, with_side_table=True, verbose=False)

        t0 = time.perf_counter()
        res = run_tick_backtest(
            raw_df=None, p=p_longshort,
            strategy_label=f"{NOTEBOOK}_{month}",
            pre_aggregated_bars=sc.bars,
            precomputed_zones_by_src={"fvg": sc.fvg_zones, "ifvg": sc.ifvg_zones},
            precomputed_structure=sc.structure,
            precomputed_atr=sc.atr,
            side_table=sc.side_table,
        )
        t_bt = time.perf_counter() - t0

        trades = list(res.trades)
        n_long = sum(1 for t in trades if t.direction > 0)
        n_short = sum(1 for t in trades if t.direction < 0)
        n_long_tp = sum(1 for t in trades if t.direction > 0 and t.exit_reason == "tp")
        n_short_tp = sum(1 for t in trades if t.direction < 0 and t.exit_reason == "tp")
        long_pnl = sum(t.pnl_usd for t in trades if t.direction > 0)
        short_pnl = sum(t.pnl_usd for t in trades if t.direction < 0)
        long_tp_pnl = sum(t.pnl_usd for t in trades if t.direction > 0 and t.exit_reason == "tp")
        short_tp_pnl = sum(t.pnl_usd for t in trades if t.direction < 0 and t.exit_reason == "tp")
        long_holds = sorted([t.hold_secs for t in trades if t.direction > 0])
        short_holds = sorted([t.hold_secs for t in trades if t.direction < 0])
        long_med_hold = pd.Series(long_holds).median() if long_holds else 0
        short_med_hold = pd.Series(short_holds).median() if short_holds else 0
        longs_only_wins = sum(1 for t in trades if t.direction > 0 and (t.pnl_usd - getattr(t, 'fee_usd', 0.0)) > 0)
        shorts_only_wins = sum(1 for t in trades if t.direction < 0 and (t.pnl_usd - getattr(t, 'fee_usd', 0.0)) > 0)
        print(f"  {month}: trades={len(trades):3d} long={n_long:3d} short={n_short:3d} | "
              f"long_TP={n_long_tp:2d} short_TP={n_short_tp:2d} | "
              f"long_PnL=${long_pnl:+.2f} (TP ${long_tp_pnl:+.2f}) "
              f"short_PnL=${short_pnl:+.2f} (TP ${short_tp_pnl:+.2f}) | "
              f"long_wins={longs_only_wins} short_wins={shorts_only_wins} | "
              f"med_hold long={long_med_hold:.0f}s short={short_med_hold:.0f}s | bt={t_bt:.1f}s")

        # Per-trade export
        for t in trades:
            per_trade.append({
                "file": month,
                "direction": "long" if t.direction > 0 else "short",
                "exit_reason": str(t.exit_reason),
                "pnl_usd": float(t.pnl_usd),
                "fee_usd": float(getattr(t, "fee_usd", 0.0)),
                "net_pnl_usd": float(t.pnl_usd) - float(getattr(t, "fee_usd", 0.0)),
                "hold_secs": float(t.hold_secs),
                "stop_usd": float(getattr(t, "stop_usd", 0.0)),
                "target_usd": float(getattr(t, "target_usd", 0.0)),
            })

    print(f"\n  wall={time.perf_counter()-t_grand:.1f}s\n")

    # Aggregate cross-month
    df = pd.DataFrame(per_trade)
    if df.empty:
        print("no data"); return

    print("=== 6-month long vs short aggregate ===")
    by_dir = df.groupby("direction").agg(
        n=("net_pnl_usd", "count"),
        n_long_wins=("net_pnl_usd", lambda s: int((s > 0).sum())),
        long_wr=("net_pnl_usd", lambda s: 100 * (s > 0).mean()),
        sum_pnl=("net_pnl_usd", "sum"),
        sum_gross=("pnl_usd", "sum"),
        sum_fee=("fee_usd", "sum"),
        med_hold=("hold_secs", "median"),
        mean_hold=("hold_secs", "mean"),
    ).round(3)
    print(by_dir.to_string())

    print("\n=== By direction × exit reason ===")
    by_dir_exit = df.groupby(["direction", "exit_reason"]).agg(
        n=("net_pnl_usd", "count"),
        sum_pnl=("net_pnl_usd", "sum"),
        med_hold=("hold_secs", "median"),
    ).round(3)
    print(by_dir_exit.to_string())

    csv = OUT_DIR / "nb56_longshort_per_trade.csv"
    df.to_csv(csv, index=False)
    print(f"\nSaved {csv} ({len(df)} rows)")

if __name__ == "__main__":
    main()
