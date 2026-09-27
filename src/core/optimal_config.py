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

* **BTC position sizing**: XAUUSD used ``lots=0.01, contract_size=100.0``
  (1 lot = 100 oz, ``lots × contract_size = 1.0 oz per layer``).
  BTCUSDT perpetuals have NO contract multiplier — 1 contract IS
  1 BTC, and position size is just BTC quantity. The fork dropped
  the ``lots × contract_size`` indirection in favour of a single
  ``qty_btc`` field. Canonical: ``qty_btc=0.001`` (~$80 notional at
  BTC=$80k, matches a small-account micro-lot sizing).

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
OPTIMAL_RECIPE_VERSION: str = "v17-btc-sniper-2026-09-26g"


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
    # Sniper direction knob — part of the v17 BTC SNIPER recipe since
    # 2026-09-26. Overriding emits a WARNING so research scripts that
    # change the entry direction (the most consequential strategic
    # knob in the recipe) are visibly flagged.
    "sniper_inv_direction_mode",
    "sniper_flip_direction",
    # SL/TP mode toggles (added 2026-09-26 d):
    "sniper_sl_mode",
    "sniper_tp_mode",
    "fvg_inv_trade_sl_atr_mult",
    "fvg_inv_trade_tp_atr_mult_v2",
    # Strict-wick filter + clean-zone gates (added 2026-09-26 c):
    "strict_wick_required",
    "strict_wick_min_wick_price_pct",
    "strict_wick_recompute_secs",
    "fvg_min_mit_distance_bars",
    "fvg_min_inv_distance_bars",
    # Routing-floor overrides (added 2026-09-26 nb56):
    # bar-loop-only — do NOT enter the SweepCache fingerprint.
    "fvg_route_min_mit_distance_bars",
    "fvg_route_min_inv_distance_bars",
    # Position size (added 2026-09-26 d):
    "qty_btc",
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
        # 2026-09-26 (nb56 follow-up): raise min zone width from $5
        # to $20. Rationale: zones narrower than $20 are below the
        # ``fvg_inv_trade_min_zone_usd=20`` sniper/clean path floor,
        # so they can never produce a trade — they're detected and
        # tracked but unused. Bumping the detector floor to $20
        # skips them entirely (saves detector work + reduces cache
        # size). Bumped in tandem with ``ifvg_min_zone_usd`` below
        # so both FVG and iFVG detectors share the same floor.
        fvg_min_zone_usd=20.00,                 # BTC: $20 min zone width (was $5)
        ifvg_min_zone_usd=20.00,                # iFVG detector shares the FVG floor (was $0.10)
        num_layers=3,
        inverse_breadth=True,
        invalidation_sl_usd=2.0,                # BTC scale: $2 vs gold $0.05
        invalidation_buffer_usd=0.50,           # BTC scale: $0.50 vs gold $0.02
        use_market_structure=True,
        ms_min_conviction=0.0,
        ms_boost_conviction=1.0,
        # ── ms_pivot_len = 75 (2026-09-26 g, promoted from nb54 sweep) ──
        # The 6-month focused pivot sweep validated ms_pivot_len=75
        # as the new canonical. Beats ms_pivot_len=9 by:
        #   * +$160/6mo on OLD canonical (qty=0.001)
        #   * +$90/6mo on NEW canonical (qty=0.01) vs pivot=9
        #   * +$52/6mo on NEW canonical vs pivot=50 (the previous
        #     conservative choice)
        # Higher EV per trade ($1.90 vs $1.68) and higher WR (16.1% vs
        # 15.2%) than pivot=9. Plateau pivot ∈ [40, 100] all robust
        # under the OLD canonical. On the NEW canonical pivot=75 has
        # a slightly higher variance (one negative month, 2025-10:
        # -$20.37 vs pivot=50's +$38.69) but the larger gain across
        # the other 5 months more than compensates. See
        # AGENTS.md "Update 2026-09-26 evening #2 / pivot finding" and
        # notebooks/nb54f_pivot_focused_outputs/nb54f_pivot_summary.csv.
        ms_pivot_len=75,
        use_atr_scaling=True,
        atr_len=1200,
        sl_atr_mult=0.25,
        tp_atr_mult=0.55,
        sl_usd=20.0,                            # BTC scale: $20 vs gold $0.80
        tp_usd=200.0,                           # BTC scale: $200 vs gold $1.80
        qty_btc=0.001,                          # BTC-native position size (~$80 notional at BTC=$80k)
        fvg_require_retest_to_invert=True,
        fvg_invalidation_min_pierce_usd=2.0,    # BTC scale: $2 vs gold $0.05
        fvg_invalidation_min_consecutive_bars=2,
        fvg_supersede_on_new=True,
        renko_drive_invalidation=False,
        fvg_sweep_enabled=False,
        fvg_invalidate_on_structure=False,
        # ── Body-only mitigation + invalidation (added 2026-09-26) ──
        # A wick through the zone edge is NOT a fill or an inversion;
        # only a bar whose BODY (open↔close range) commits through
        # the edge counts. Rationale: on 1s BTC the canonical detector
        # was firing mitigations / inversions on wick-and-recover
        # patterns — a tick past the zone edge that immediately
        # reverses on the next bar. Those are SL-hunt noise, not
        # structural commits. The 2026-09-25 nb52 sweep showed
        # +$3.91/trade on fvg_invalidated zones vs -$0.90/trade on
        # true_iFVG zones — the F.3 finding motivated flipping these
        # ON at the recipe level so the detector stops tagging
        # drive-throughs as real inversions. Both flags also tighten
        # ``mitigated_bar`` / ``inverted_bar`` so the sniper path
        # waits for a real commitment, not a wick probe.
        fvg_body_only_mitigation=True,
        fvg_body_only_invalidation=True,
        gate_on_gmma_bias=False,
        layer_lifetime_secs=7200,
        bos_choch_ignore_invert_when_aligned=True,
        bos_choch_memory_n_events=5,
        fvg_min_lifetime_secs=3,
        # ── FVG lifetime cap (added 2026-09-26) ─────────────────────
        # 3h wall-clock cap on detector-zone lifetime. With the
        # default of 0 (unlimited), cumulative per-zone work in
        # ``detect_fvg`` scales with average-zone-lifetime ×
        # N_zones × N_bars; on a 2.5M-bar month this dominates
        # the bar backtest (~95% of 114s). Capping at 3h cuts
        # the average zone lifetime from ~half-the-month to ~3h,
        # which is ~15× less cumulative work. Trade selection
        # CHANGES with this knob (zones older than 3h can no longer
        # fire retests); see ``AGENTS.md`` 2026-09-26 update for
        # the before/after PnL comparison on a 1-week BTC corpus.
        fvg_max_age_secs=10800,                 # 3h detector-zone cap
        fvg_max_age_bars=0,                     # legacy fallback disabled
        ifvg_max_age_secs=10800,                # 3h iFVG cap (same window)
        ifvg_max_age_bars=0,                    # legacy fallback disabled
        # ── v17 SNIPER mode (SL widened, inherited from XAUUSD v17) ──
        entry_mode="sniper",                    # skip FVG entry, wait for inversion
        # SL/TP sizing — zone-mult anchored (preserves v17 XAUUSD-tuned
        # values). The 5.0 / 45.0 multiplier pair comes from the
        # earlier vBTC2 sweep showing TP=22-45× + SL=2-5× is the
        # BTC-scale optimum on the 2025-04 monthly file.
        fvg_inv_trade_sl_zone_mult=5.0,         # SL = 2× zone width
        fvg_inv_trade_tp_zone_mult=60.0,        # TP = 22× zone width
        # 2026-09-26 c: independent SL/TP mode toggles (sniper path).
        # Default "zone_mult" keeps the v17 SNIPER behaviour. Switching
        # to "atr_mult" is regime-adaptive (BTC-scale ATR ≈ $0.20).
        sniper_sl_mode="zone_mult",
        sniper_tp_mode="zone_mult",
        fvg_inv_trade_sl_atr_mult=0.25,         # used when sniper_sl_mode="atr_mult"
        fvg_inv_trade_tp_atr_mult_v2=0.55,      # used when sniper_tp_mode="atr_mult"
        fvg_inv_trade_min_zone_usd=20.0,        # BTC scale: $10 vs gold $0.30
        fvg_inv_trade_max_per_zone=1,
        sniper_max_age_secs=1800,
        # 2026-09-26 d: immediate-mode SL/TP mode selector (parallel
        # to sniper_*) — applies on the non-sniper
        # ``entry_mode='immediate'`` path. Empty string = inherit from
        # ``use_atr_scaling`` (legacy behaviour). Set explicitly via
        # ``optimal_params(immediate_sl_mode="zone_mult"|"atr_mult"|"usd_fixed")``.
        immediate_sl_mode="",                   # "" = inherit legacy
        immediate_tp_mode="",                   # "" = inherit legacy
        immediate_sl_zone_mult=0.0,             # only used when mode == "zone_mult"
        immediate_tp_zone_mult=0.0,             # only used when mode == "zone_mult"
        # ── Sniper direction on inversion ──────────────────────────
        # ``"continuation"`` matches the v17 BTC SNIPER full-corpus
        # validation (sniper enters in the original FVG gap-polarity
        # direction; the inversion is treated as a liquidity sweep
        # the thesis survives). ``"fade_displacement"`` is the
        # alternate reading (sniper fades the original move). See
        # ``AGENTS.md`` § "Sniper direction on inversion" for the
        # net-direction table and the live-engine divergence note.
        sniper_inv_direction_mode="continuation",
        # Legacy back-compat shim — kept as False so callers using
        # ``optimal_params(sniper_flip_direction=True)`` continue to
        # work. Honoured by ``TrendStrategyParams._resolve_deprecated``
        # on construction; will be removed 2026-12-31.
        sniper_flip_direction=False,
        # ── Strict-wick FVG filter (added 2026-09-26, BTC v17 SNIPER c) ─
        # Both outer candles (c1 and c3) must have a visible wick on
        # the side AWAY from the gap, length >= 0.025% × hourly median
        # close. At BTC=$100k this is $25; at BTC=$30k it's $7.50.
        # The floor is recomputed hourly (not per-bar) for cost.
        # Rationale: filter the "triggered on non-wicked candles"
        # case where c1 or c3 are essentially flat-body / doji —
        # those zones are weak FVG formations. The H.3 nb52 sweep
        # (Sep 26) showed $20 fixed USD gave 50% WR but -54% PnL
        # (only 10 trades); the dynamic floor should preserve more
        # trades while still rejecting the doji-edge cases.
        strict_wick_required=True,
        strict_wick_min_wick_price_pct=0.00025,  # 0.025% of mid-price
        strict_wick_recompute_secs=3600,          # hourly bucket
        # ── "Clean" zone gates (added 2026-09-26, BTC v17 SNIPER c) ──
        # A zone is "clean" iff both its mitigation and inversion
        # events occur at least 3 1s-bars AFTER the zone's trigger
        # bar. Clean zones route to the iFVG immediate entry path
        # with ATR-anchored SL/TP. Dirty zones (mit or inv within
        # 1-2 bars) keep the sniper path with zone-anchored SL/TP.
        # Both paths run; the routing decision is per-zone, made at
        # the inversion bar.
        fvg_min_mit_distance_bars=3,
        fvg_min_inv_distance_bars=3,
        # ── Binance fee model (0.05% one-way / 0.10% round-trip taker) ──
        taker_fee_bps=5.0,                      # 0.05% per side
        maker_fee_bps=2.0,                      # 0.02% per side
    )


# Module-level pre-built canonical instance. Use ``optimal_params()``
# instead when you want a fresh instance or want to override knobs.
OPTIMAL_PARAMS: TrendStrategyParams = _build_v17_btc_sniper()
# Resolve any deprecated knobs once at import time. Direct construction
# (bypassing ``optimal_params()``) skips this call; callers using the
# factory always get the resolved version.
OPTIMAL_PARAMS._resolve_deprecated()


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
    # Reconcile the deprecated ``sniper_flip_direction`` bool with
    # the canonical ``sniper_inv_direction_mode`` enum. Done AFTER
    # overrides are applied so callers can use either form.
    fresh._resolve_deprecated()
    return asdict(fresh) if as_dict_flag else fresh


__all__ = [
    "OPTIMAL_RECIPE_VERSION",
    "OPTIMAL_PARAMS",
    "optimal_params",
]
