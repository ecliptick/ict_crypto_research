"""NB56 follow-up — 1:2 RR with bigger absolute moves.

User correction (2026-09-26):
  "Round-trip fee is $0.10/trade - wtf no, its 0.10% of price"

User is right. The fee is 0.10% of notional (= 10bps round-trip),
which equals the SL distance at 1:2 RR. That's why 1:2 RR
needs 100%+ WR at qty_btc=0.001 with (1x, 2x) zone_mult.

But: as BTC price moves up, fees scale proportionally AND the
absolute SL/TP widths scale proportionally (because they're
in USD, not %). So the WR threshold is geometric, not affected
by price level.

The way to make 1:2 RR actually work is to widen the absolute
moves. Two ways:

  A. Bigger qty_btc. Scales both fees and gross proportionally.
     At qty=0.01 with (1x, 2x) zone, fee=0.10%*notional still,
     but absolute SL=$0.45 vs fee=$0.84 — fees are STILL bigger
     than SL. So this doesn't fix the problem at narrow zone
     mults. Need wider zone_mult too.

  B. Wider zone_mult. At qty=0.001 with (5x, 10x), SL=$2.27
     and TP=$4.54 vs fee=$0.084 — TP gross $0.0045 vs fee
     $0.0008 → still fee-biter (TP < fee in absolute gross).

  C. Combine. At qty=0.01 with (5x, 10x) zone, fee=$0.84 vs
     SL gross=$2.27 / TP gross=$4.54 → fees are 18% of TP,
     37% of SL. Breakeven WR ≈ 31%.

So regime C is the first combination where 1:2 RR actually
beats fees. Let's test it.

This experiment tests 6 cells × 2 months:
  Months: 2025-04 (range, ~$83k BTC), 2025-11 (volatile, ~$90k BTC)
  Cells:
    A1: qty=0.001, (1x, 2x) zone_mult  -- baseline (FEE-BITER)
    A2: qty=0.001, (5x, 10x) zone_mult -- wider zone, qty=1
    B1: qty=0.01, (1x, 2x) zone_mult   -- bigger qty, narrow zone
    B2: qty=0.01, (5x, 10x) zone_mult  -- bigger qty, wider zone
    C1: qty=0.01, (10x, 20x) zone_mult -- bigger qty, even wider
    baseline: qty=0.001, (5x, 60x) zone_mult -- canonical sniper
"""
import sys
import time
import warnings
from collections import Counter
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT = None
for _c in [Path(".").resolve(), *Path(".").resolve().parents]:
    if (_c / "src" / "core" / "ict_signals.py").is_file():
        ROOT = _c
        break
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "notebooks"))

import pandas as pd

from src.core.optimal_config import optimal_params, OPTIMAL_RECIPE_VERSION
from src.tick.cache import get_or_build
from src.tick.tick_backtest import run_tick_backtest

NOTEBOOK = "nb56_rr_sweep_qty"
SCENARIO = "rr_sweep_with_qty_bump"

OUT_DIR = ROOT / "notebooks" / "nb56_rr_qty_outputs"
OUT_DIR.mkdir(parents=True, exist_ok=True)

SOURCE_DATA_ROOT = Path(r"C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw")

# (label, qty_btc, sl_zone_mult, tp_zone_mult, sniper_inv_direction_mode)
CELLS = [
    # ── Regime A: qty=0.001 (canonical), widen zone_mult
    ("A1_qty001_1x2x",  0.001,  1.0,  2.0,  "continuation"),
    ("A2_qty001_5x10x", 0.001,  5.0, 10.0, "continuation"),
    # ── Regime B: qty=0.01 (10x bigger), narrow zone_mult
    ("B1_qty01_1x2x",   0.01,   1.0,  2.0,  "continuation"),
    ("B2_qty01_5x10x",  0.01,   5.0, 10.0, "continuation"),
    # ── Regime C: qty=0.01 + wider zone_mult (1:2 RR with bigger moves)
    ("C1_qty01_10x20x", 0.01,  10.0, 20.0, "continuation"),
    # ── Baseline: canonical sniper 5x/60x at qty=0.001
    ("baseline_qty001_5x60x", 0.001, 5.0, 60.0, "continuation"),
]

MONTHS = ["2025-04", "2025-11"]


def main():
    print(f"[{NOTEBOOK}] 1:2 RR with bigger absolute moves")
    print(f"recipe version: {OPTIMAL_RECIPE_VERSION}")
    print(f"months: {MONTHS}\n")

    per_trade_rows = []
    grand_t0 = time.perf_counter()

    for month in MONTHS:
        path = SOURCE_DATA_ROOT / f"BTCUSDT-aggTrades-{month}.parquet"
        if not path.exists():
            print(f"  WARN: {path} not found, skipping", flush=True)
            continue

        # All cells share detector-side params (no SL/TP zone_mult in
        # fingerprint). Cache is built once per (month, detector params).
        # Detector params depend on (qty_btc is irrelevant — detector
        # only reads fvg_min_zone_usd, strict_wick, etc.). So one cache
        # build per month suffices for all 6 cells.
        p_cache = optimal_params(qty_btc=0.001)
        t0 = time.perf_counter()
        sc = get_or_build(path, p_cache, with_side_table=True, verbose=False)
        print(f"[{month}] cache={time.perf_counter()-t0:.2f}s "
              f"fvg={len(sc.fvg_zones)} ifvg={len(sc.ifvg_zones)}\n", flush=True)

        for label, qty, sl_zm, tp_zm, mode in CELLS:
            ov = {
                "fvg_inv_trade_sl_zone_mult": sl_zm,
                "fvg_inv_trade_tp_zone_mult": tp_zm,
                "sniper_inv_direction_mode": mode,
            }
            p_i = optimal_params(qty_btc=qty, **ov)

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
            fees = [getattr(t, "fee_usd", 0.0) for t in trades]
            nets = [p - f for p, f in zip(pnls, fees)]
            n = len(trades)
            n_wins = sum(1 for v in nets if v > 0)
            wr = (100.0 * n_wins / n) if n else 0.0
            ev = (sum(nets) / n) if n else 0.0
            paths = Counter(t.entry_triggered_by for t in trades)
            n_long = sum(1 for t in trades if t.direction > 0)
            n_short = sum(1 for t in trades if t.direction < 0)
            med_fee = pd.Series(fees).median() if fees else 0.0

            print(f"  [{month}] {label:24s} | n={n:3d} | "
                  f"net=${sum(nets):+8.2f} gross=${sum(pnls):+8.2f} "
                  f"med_fee=${med_fee:.4f} WR={wr:5.1f}% EV=${ev:+.4f} | "
                  f"long={n_long} short={n_short} | "
                  f"paths={dict(paths)} | bt={t_bt:.1f}s", flush=True)

            for ti, t in enumerate(trades):
                per_trade_rows.append({
                    "file": month,
                    "cell": label,
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
                })

    elapsed = time.perf_counter() - grand_t0
    print(f"\n=== {NOTEBOOK} done. wall={elapsed:.1f}s ===", flush=True)

    if per_trade_rows:
        df = pd.DataFrame(per_trade_rows)
        csv_path = OUT_DIR / "nb56_rr_qty_per_trade.csv"
        df.to_csv(csv_path, index=False)
        print(f"  Wrote {csv_path} ({len(df)} rows)", flush=True)


if __name__ == "__main__":
    main()
