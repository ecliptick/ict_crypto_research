"""BTC-native tick utilities.

This subpackage contains the new tick-fill hybrid backtest
engine and the vectorized tick aggregator. Both are stock
additions for this BTC fork — they do not exist in the parent
``ict_tier_v2`` repo.
"""
from .aggtrade_aggregator import (
    AggTrade,
    aggregate_ticks_to_1s_bars,
    load_raw_aggtrades,
    load_concat_raw_aggtrades,
)
from .tick_backtest import (
    TickData,
    TickFillResult,
    build_tick_index,
    resolve_tick_fill,
    resolve_limit_fill,
    resolve_stop_fill,
    run_tick_backtest,
)

__all__ = [
    "AggTrade",
    "aggregate_ticks_to_1s_bars",
    "load_raw_aggtrades",
    "load_concat_raw_aggtrades",
    "TickData",
    "TickFillResult",
    "build_tick_index",
    "resolve_tick_fill",
    "resolve_limit_fill",
    "resolve_stop_fill",
    "run_tick_backtest",
]
