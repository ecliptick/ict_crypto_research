"""Live-equivalent backtest for the 8.5h of BTCUSDT 2026-09-25 data.

The live engine on the VPS uses the XAUUSD-tuned v17 SNIPER recipe
(``ict_sniper_live/src/core/optimal_config.py``):

- ``fvg_min_zone_usd = 0.10``
- ``fvg_resample_secs = 60``
- ``fvg_invalidation_min_pierce_usd = 0.05``
- ``fvg_supersede_on_new = True``
- ``fvg_inv_trade_tp_zone_mult = 20.0``  (gold default, BTC fork uses 22.0)
- ``lots/contract_size`` not in v17 → live uses ``position_mode='fixed_btc'``, ``position_size=0.001``

Run ``run_ict_backtest`` with these values and inspect trade timestamps.
"""
from __future__ import annotations

import sys
import time
import json
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

    # Reproduce the live engine's recipe from
    # ict_sniper_live/src/core/optimal_config.py (the XAUUSD-tuned v17 SNIPER).
    # Live recipes don't apply body-only flags (those are 2026-09-26 BTC fork
    # changes that the live engine hasn't picked up).
    live_p = TrendStrategyParams(
        signal_source="fvg",
        additional_sources=["ifvg"],
        fvg_min_zone_usd=0.10,           # LIVE engine XAUUSD default (gold-tuned)
        ifvg_min_zone_usd=0.10,
        fvg_resample_secs=60,
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
        # Live uses 0.001 BTC position per trade. Recipe above notes
        # ``lots=0.01, contract_size=0.1`` -> 0.001 BTC. Use lots=0.001
        # with contract_size=1.0 which evaluates to 0.001 BTC per layer.
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
          f"resample_secs={live_p.fvg_resample_secs}, "
          f"tp_mult={live_p.fvg_inv_trade_tp_zone_mult}, "
          f"qty={(live_p.lots * live_p.contract_size):.4f}")

    print("Running live-equivalent backtest…")
    t0 = time.time()
    res = run_ict_backtest(bars, live_p, strategy_label="live_equiv")
    dt = time.time() - t0
    print(f"Done in {dt:.1f}s — {len(res.trades)} trades\n")

    summary = {
        "n_bars": len(bars),
        "n_trades": len(res.trades),
        "total_pnl_usd": sum(t.pnl_usd for t in res.trades),
        "wall_secs": dt,
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

    out = Path("notebooks/nb53_outputs/backtest_live_equiv.json")
    with open(out, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\n→ wrote {out}")

    # Compare to live trades
    with open("notebooks/nb53_outputs/live_trades_today.json") as f:
        live = json.load(f)
    print(f"\n=== TALLY: backtest (live-equiv recipe) vs live ===")
    live_today = [
        t for t in live["all_today_trades"]
        if t["trade_id"] >= 31
    ]
    for t in live_today:
        ts = pd.Timestamp(t["entry_time_ns"], tz="UTC").strftime("%H:%M:%S")
        print(f"  live trade {t['trade_id']} {t['side']:>5} {ts:>10} "
              f"${t['entry_price']:>8.2f} zone={t['zone_id']} "
              f"stop=${t['stop_usd']:>6.2f}")


if __name__ == "__main__":
    main()
