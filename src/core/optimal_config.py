"""Canonical ICT-optimal params for the BTC-tick-native fork.

This is the BTC-native counterpart of the XAUUSD v17 SNIPER recipe.
Forked from ``ict_tier_v2`` on 2026-09-24.

Single source of truth for the ICT-only strategy's "best known"
parameter set on BTCUSDT 1s. Import ``optimal_params()`` (or the
module-level constant ``OPTIMAL_PARAMS``) from any backtest driver,
notebook, or tool script to run the recipe without re-typing knobs.

Recipe lineage
==============

* **v17 SNIPER** (inherited from XAUUSD, full-corpus validated
  on 654 Mon-Fri UTC days Jan 2024 – Jul 2026): the
  ``entry_mode='sniper'`` deferred-entry recipe with
  ``fvg_inv_trade_sl_zone_mult=2.0`` and
  ``fvg_inv_trade_tp_zone_mult=22.0``. The strategy is
  scale-invariant: BTC scales the zone width by ~100× but the
  SL/TP multipliers (zone-width-anchored) and ATR-multiplier knobs
  carry over unchanged.

* **BTC contract sizing**: XAUUSD uses ``lots=0.01,
  contract_size=100.0`` (1 lot = 100 oz, so ``lots × contract_size =
  1.0 oz per layer``). BTCUSDT perpetuals use 1 lot = 1 contract
  where the contract size is **1 BTC**. We preserve
  ``lots × contract_size = 1.0`` by setting ``contract_size=1.0``
  and keeping ``lots=0.01``, so the per-layer notional of 0.01 BTC
  (= ~$1k at BTC=$100k) is unchanged.

* **BTC fee model**: Binance USDT-M perpetual futures charge
  0.04% taker / 0.02% maker (per side). For market-entry and
  market-exit sniper fills, we use the **taker rate (5 bps per
  side, 10 bps round-trip)**. Wired as ``taker_fee_bps=5.0`` and
  ``maker_fee_bps=2.0`` on ``TrendStrategyParams``; debited in
  ``_close_trade`` as ``fee_usd = entry_price × lots ×
  contract_size × (taker_bps/10000) × 2``.

* **BTC scale tuning** (sl_usd / tp_usd / fvg_min_zone_usd):
  scaled by ~100× from gold defaults because BTC's 1s ATR is
  ~$0.20-$0.30 (vs $0.13 for gold). Sl/tp defaults are now
  ``sl_usd=20.0, tp_usd=200.0`` (legacy gold values retained as
  the ATR-anchored path overrides these — they only matter when
  ``use_atr_scaling=False``).

See ``AGENTS.md`` for the full fork description and the BTC
backtest design.

Usage
=====

From a backtest driver::

    from src.core.optimal_config import optimal_params
    from src.core.ict_strategy import TrendStrategyParams

    p = optimal_params()                                  # default BTC SNIPER v17
    p = optimal_params(fvg_inv_trade_sl_zone_mult=3.0)   # widen SL
    p = optimal_params(as_dict=False)                     # TrendStrategyParams instance

From a CLI script that wants a dict to JSON-serialize or log::

    from src.core.optimal_config import optimal_params
    log.info("running with params: %s", optimal_params(as_dict=True))

Constants exported
==================

* ``OPTIMAL_PARAMS``          — module-level ``TrendStrategyParams``
                                pre-built with the v17 SNIPER
                                recipe, tuned for BTC.
* ``optimal_params(**overrides)`` — factory that returns a fresh
                                ``TrendStrategyParams`` with the
                                v17 defaults and any overrides
                                applied.
* ``OPTIMAL_RECIPE_VERSION``  — string identifier of the recipe
                                this file encodes. Bump when the
                                canonical config changes.

See also
========

* ``AGENTS.md`` — full fork description, BTC backtest design,
  and the tick-fill hybrid approach.
* ``src/core/ict_strategy.py`` — ``TrendStrategyParams`` field
  reference (every field on the canonical recipe is documented
  inline in ``ict_strategy.py``).
"""
from __future__ import annotations

import copy
import logging as _logging
from dataclasses import asdict

from .ict_strategy import TrendStrategyParams

_log = _logging.getLogger(__name__)

# Bump this whenever the canonical recipe changes so downstream
# code can detect "I'm running an outdated config". Format:
# "v<N>-<recipe-name>-<YYYY-MM-DD>" — last touched 2026-09-24.
#
# v17-btc: port of the XAUUSD v17 SNIPER recipe (full-corpus
# validated) to BTCUSDT. Contract size flipped from 100.0 (oz)
# to 1.0 (BTC contract = 1 BTC), and taker_fee_bps=5 added.
OPTIMAL_RECIPE_VERSION: str = "v17-btc-sniper-2026-09-24"


# Knobs that are part of the v17 recipe's "do not touch" list.
# Overriding any of these in optimal_params() emits a warning so
# downstream A/B scripts can see when they've departed from the
# canonical recipe.
_RECIPE_KNOBS = frozenset({
    # v2 BASELINE knobs that the v17 recipe leaves at v2 defaults
    "signal_source",
    "additional_sources",
    "fvg_resample_secs",
    "num_layers",
    "inverse_breadth",
    "invalidation_sl_usd",
    "invalidation_buffer_usd",
    "use_market_structure",
    "ms_min_conviction",
    "ms_boost_conviction",
    "use_atr_scaling",
    "atr_len",
    "sl_atr_mult",
    "tp_atr_mult",
    "sl_usd",
    "tp_usd",
    "lots",
    "contract_size",
    "fvg_require_retest_to_invert",
    "fvg_invalidation_min_pierce_usd",
    "fvg_invalidation_min_consecutive_bars",
    "fvg_supersede_on_new",
    "renko_drive_invalidation",
    "fvg_sweep_enabled",
    "fvg_invalidate_on_structure",
    "gate_on_gmma_bias",
    "layer_lifetime_secs",
    "bos_choch_ignore_invert_when_aligned",
    "bos_choch_memory_n_events",
    "fvg_min_lifetime_secs",
    # v17 SNIPER knobs (the actual recipe deltas)
    "entry_mode",
    "fvg_inv_trade_sl_zone_mult",
    "fvg_inv_trade_tp_zone_mult",
    "fvg_inv_trade_min_zone_usd",
    "fvg_inv_trade_max_per_zone",
    "sniper_max_age_secs",
    # BTC-specific knobs
    "taker_fee_bps",
    "maker_fee_bps",
})


def _build_v17_btc_sniper() -> TrendStrategyParams:
    """Construct the canonical v17 BTC SNIPER recipe as a fresh instance.

    All values here mirror the recipe block in ``AGENTS.md`` § "Fork
    description". If you find yourself wanting to change a value
    here, that change should first be promoted to the recipe in
    AGENTS.md, and then mirrored here.
    """
    return TrendStrategyParams(
        # ── v2 BASELINE defaults (inherited from XAUUSD, scale-tuned for BTC) ──
        signal_source="fvg",
        additional_sources=["ifvg"],
        fvg_resample_secs=60,
        num_layers=3,
        inverse_breadth=True,
        invalidation_sl_usd=2.0,                # BTC scale: $2 vs gold $0.05
        invalidation_buffer_usd=0.50,           # BTC scale: $0.50 vs gold $0.02
        use_market_structure=True,
        ms_min_conviction=0.0,
        ms_boost_conviction=1.0,
        use_atr_scaling=True,
        atr_len=1200,
        sl_atr_mult=0.25,
        tp_atr_mult=0.55,
        sl_usd=20.0,                            # BTC scale: $20 vs gold $0.80
        tp_usd=200.0,                           # BTC scale: $200 vs gold $1.80
        lots=0.01,
        contract_size=1.0,                      # BTCUSDT perp: 1 contract = 1 BTC
                                                # so lots × contract_size = 0.01 BTC
                                                # per layer (~$1k notional at BTC=$100k)
        fvg_require_retest_to_invert=True,
        fvg_invalidation_min_pierce_usd=2.0,    # BTC scale: $2 vs gold $0.05
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
        # ── v17 SNIPER mode (SL widened, inherited from XAUUSD v17) ──
        entry_mode="sniper",                    # skip FVG entry, wait for inversion
        fvg_inv_trade_sl_zone_mult=2.0,         # SL = 2× zone width
        fvg_inv_trade_tp_zone_mult=22.0,        # TP = 22× zone width
        fvg_inv_trade_min_zone_usd=10.0,        # BTC scale: $10 vs gold $0.30
        fvg_inv_trade_max_per_zone=1,
        sniper_max_age_secs=1800,
        # ── Binance fee model (0.05% one-way / 0.10% round-trip taker) ──
        taker_fee_bps=5.0,                      # 0.05% per side
        maker_fee_bps=2.0,                      # 0.02% per side
    )


# Module-level pre-built canonical instance. Use ``optimal_params()``
# instead when you want a fresh instance or want to override knobs.
OPTIMAL_PARAMS: TrendStrategyParams = _build_v17_btc_sniper()


def optimal_params(**overrides) -> TrendStrategyParams | dict:
    """Return a fresh copy of the canonical v17 BTC SNIPER params.

    Parameters
    ----------
    **overrides
        Keyword arguments matching fields on ``TrendStrategyParams``.
        Applied on top of the v17 BTC recipe. Passing a field name
        that is part of the canonical recipe's "do not touch" list
        logs a WARNING (not an error) — research scripts are
        expected to override these knobs; we just want visibility.
    as_dict:
        If True, return the params as a plain dict (via
        ``dataclasses.asdict``). If False (default), return a
        ``TrendStrategyParams`` instance. The ``as_dict`` kwarg is
        consumed and not forwarded to ``TrendStrategyParams``.

    Returns
    -------
    TrendStrategyParams | dict
        A fresh instance (or dict) with overrides applied. Always
        a copy — mutating the returned value never mutates the
        module-level ``OPTIMAL_PARAMS`` constant.

    Examples
    --------
    >>> from src.core.optimal_config import optimal_params
    >>> p = optimal_params()                    # v17 BTC SNIPER default
    >>> p = optimal_params(inverse_breadth=False)  # v8 candidate (untested on BTC)
    >>> p = optimal_params(as_dict=True)        # dict form for logging
    """
    as_dict_flag = bool(overrides.pop("as_dict", False))
    if overrides.keys() & _RECIPE_KNOBS:
        _log.warning(
            "optimal_params() overrides touch canonical recipe knobs: %s. "
            "Verify this matches the latest AGENTS.md recipe.",
            sorted(overrides.keys() & _RECIPE_KNOBS),
        )
    fresh = copy.deepcopy(OPTIMAL_PARAMS)
    for k, v in overrides.items():
        if not hasattr(fresh, k):
            raise TypeError(
                f"optimal_params() got unexpected override {k!r}; "
                f"not a field on TrendStrategyParams"
            )
        setattr(fresh, k, v)
    return asdict(fresh) if as_dict_flag else fresh


__all__ = [
    "OPTIMAL_RECIPE_VERSION",
    "OPTIMAL_PARAMS",
    "optimal_params",
]
