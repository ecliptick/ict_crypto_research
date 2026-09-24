"""ICT-only fork of gmma_guppy_2. No GMMA, no TEMA, no ATR scaling.

The 4 submodules in this fork are::

    src.core.ict_signals        - FVG / iFVG / ORB / Wyckoff detectors
    src.core.market_structure   - BOS / CHoCH / CHoCH+ / order blocks / sweeps
    src.core.ict_strategy       - ICT-only TrendStrategyParams + PendingSignal
    src.backtest.ict_backtest   - 3-layer FVG/iFVG backtest with dynamic SL/TP

GMMA indicators and the trend strategy are *intentionally* not in this
fork. The ``gmma_signals`` module re-exports the bare dataclass
primitives (``TrendStrategyParams``, ``PendingSignal``) needed by the
ICT path, with the GMMA/TEMA-specific fields removed.
"""
from .ict_strategy import TrendStrategyParams, PendingSignal
from .optimal_config import OPTIMAL_PARAMS, OPTIMAL_RECIPE_VERSION, optimal_params

__all__ = [
    "TrendStrategyParams",
    "PendingSignal",
    "OPTIMAL_PARAMS",
    "OPTIMAL_RECIPE_VERSION",
    "optimal_params",
]
