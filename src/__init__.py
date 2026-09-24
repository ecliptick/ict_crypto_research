"""ICT crypto fork — BTCUSDT tick-native backtest engine.

Forked from the parent ``ict_tier_v2`` repo (XAUUSD-only) on
2026-09-24 to run on BTCUSDT raw aggTrades with Binance
perpetual fees and a tick-fill hybrid backtest engine.

Public surface
==============

* ``src.core``   — the ICT detector family + ``TrendStrategyParams`` +
                    the v17 BTC SNIPER canonical config.
* ``src.backtest`` — the bar-based backtest engine (inherited
                    from the parent; now with Binance fee support).
* ``src.tick``   — the new tick aggregator + tick-fill hybrid
                    backtest engine.

For the canonical recipe::

    from src.core.optimal_config import optimal_params
    p = optimal_params()                  # BTC SNIPER v17

For the bar-only backtest (legacy semantics, no tick refill)::

    from src.backtest.ict_backtest import run_ict_backtest
    res = run_ict_backtest(bars_df, p)

For the tick-fill hybrid backtest (recommended)::

    from src.tick.tick_backtest import run_tick_backtest
    from src.tick.aggtrade_aggregator import load_raw_aggtrades
    raw = load_raw_aggtrades('/path/to/BTCUSDT-aggTrades-2025-06.parquet')
    res = run_tick_backtest(raw, p)

See ``AGENTS.md`` for the full fork description.
"""
