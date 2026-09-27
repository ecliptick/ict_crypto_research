"""Backtest using live-equivalent recipe with full 1s pivot detection.

Tries two configurations:
1. Cold start (no warmup) — what we did before
2. Same as 1 but with the live engine's MAJOR differences:
   - more relaxed `fvg_min_zone_usd` (0.10 vs 5.00)
   - body_only_mitigation=False, body_only_invalidation=False
   - 1-second pivot detection (no fvg_resample_secs aggregation)
   - everything else matches live

The hypothesis being tested: does the live engine's relaxed zone filter
let it detect tiny 1s-pivot FVGs ($1-2 wide) that the BTC-fork canonical
filter ($5+) silences? Trade 33 was preceded by a $1.20-zone gap that was
filtered out at fvg_min_zone_usd=5 but kept at fvg_min_zone_usd=0.10.
"""
from __future__ import annotations
import sys, time, json
from pathlib import Path

sys.path.insert(0, '.')
import pandas as pd
import numpy as np

from src.tick.aggtrade_aggregator import load_raw_aggtrades_columns, aggregate_ticks_to_1s_bars
from src.backtest.ict_backtest import run_ict_backtest
from src.core.ict_strategy import TrendStrategyParams


def main():
    print("Loading 2026-09-25 BTC aggTrades…")
    raw = load_raw_aggtrades_columns(
        "notebooks/nb53_outputs/today_aggtrades.parquet"
    )
    bars = aggregate_ticks_to_1s_bars(raw)
    print(f"{len(bars)} 1s bars from {bars.time.iloc[0]} → {bars.time.iloc[-1]}")

    # LIVE-EQUIVALENT recipe BUT with 1-second pivot detection (not 60s)
    live_p = TrendStrategyParams(
        signal_source="fvg",
        additional_sources=["ifvg"],
        fvg_min_zone_usd=0.10,
        ifvg_min_zone_usd=0.10,
        fvg_resample_secs=0,         # 1-second pivot (LIVE engine uses 60s but we'll test 1s)
        num_layers=3,
        inverse_breadth=True,
        invalidation_sl_usd=0.05,
        invalidation_buffer_usd=0.02,
        use_market_structure=True,
        ms_min_conviction=0.0,
        ms_boost_conviction=1.0,
        use_atr_scaling=True,
        atr_len=1200,
        sl_atr_mult=0.25,
        tp_atr_mult=0.55,
        sl_usd=0.80,
        tp_usd=1.80,
        lots=0.001,
        contract_size=1.0,
        fvg_require_retest_to_invert=True,
        fvg_invalidation_min_pierce_usd=0.05,
        fvg_invalidation_min_consecutive_bars=2,
        fvg_supersede_on_new=True,
        renko_drive_invalidation=False,
        fvg_sweep_enabled=False,
        fvg_invalidate_on_structure=False,
        gate_on_gmma_bias=False,
        layer_lifetime_secs=7200,
        bos_choch_ignore_invert_when_aligned=True,
        bos_choch_memory_n_events=5,
        fvg_min_lifetime_secs=3,
        entry_mode="sniper",
        fvg_inv_trade_sl_zone_mult=2.0,
        fvg_inv_trade_tp_zone_mult=20.0,
        fvg_inv_trade_min_zone_usd=0.30,
        fvg_inv_trade_max_per_zone=1,
        sniper_max_age_secs=1800,
    )
    print(f"Recipe: fvg_min_zone_usd={live_p.fvg_min_zone_usd}, "
          f"resample_secs={live_p.fvg_resample_secs} (1s), "
          f"tp_mult={live_p.fvg_inv_trade_tp_zone_mult}, "
          f"qty={(live_p.lots * live_p.contract_size):.4f}")

    t0 = time.time()
    res = run_ict_backtest(bars, live_p, strategy_label="live_equiv_1s")
    print(f"Done in {time.time()-t0:.1f}s — {len(res.trades)} trades\n")

    summary = {
        "recipe": "live_equiv_1s",
        "n_bars": len(bars),
        "n_trades": len(res.trades),
        "trades": [],
    }

    print(f"{'idx':>3} {'dir':>5} {'entry_time':>20} {'entry_$':>10} "
          f"{'reason':>5} {'exit_$':>10} {'pnl_$':>9}")
    for i, tr in enumerate(res.trades):
        ts = pd.Timestamp(tr.entry_time, tz="UTC").strftime("%H:%M:%S")
        side = "long" if tr.direction == 1 else "short"
        print(f"{i:>3} {side:>5} {ts:>20} {tr.entry_price:>10.2f} "
              f"{tr.exit_reason:>5} {tr.exit_price:>10.2f} {tr.pnl_usd:>9.4f}")
        summary["trades"].append({
            "direction": int(tr.direction),
            "entry_time_utc": ts,
            "entry_price": float(tr.entry_price),
            "exit_price": float(tr.exit_price),
            "exit_reason": str(tr.exit_reason),
            "pnl_usd": float(tr.pnl_usd),
            "stop_usd": float(tr.stop_usd),
        })

    out = Path("notebooks/nb53_outputs/backtest_live_equiv_1s.json")
    with open(out, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\n→ wrote {out}")


if __name__ == "__main__":
    main()
