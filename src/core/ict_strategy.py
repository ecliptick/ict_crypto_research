"""ICT strategy params + PendingSignal.

Forked from ``src.core.gmma_signals`` in the parent repo. The GMMA
trend signal generator (``generate_pending_signals``) and all the
GMMA/TEMA-specific knobs have been removed. Only the fields
``ict_signals.generate_ict_pending_signals`` and
``ict_backtest.run_ict_backtest`` consume are kept.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class PendingSignal:
    """A signal that fired on bar ``trigger_bar`` but won't be filled
    until the next bar's open (industry-standard anti-look-ahead)."""

    trigger_bar: int
    direction: int                       # +1 long, -1 short
    triggered_by: str = "fvg"            # "fvg" | "ifvg" | "orb" | "wyckoff" | "sweep"
    stop_usd: float = 0.0
    target_usd: float = 0.0
    # Per-signal conviction multiplier (0.0 = drop, 1.0 = neutral, up to
    # ~1.5 = boost from a fresh BoS/CHoCH+ in the trade direction).
    conviction: float = 1.0
    # Anchor FVG zone (None for non-FVG sources). The bar-loop uses
    # this to detect zone invalidation while the trade is open and
    # tighten the SL to the zone edge as a "soft stop".
    fvg_zone: object = None
    # Whether the signal originates from an *inverted* FVG. iFVG
    # entries are special: when the underlying FVG gets *re-inverted*
    # (i.e. price trades back through the zone, restoring its original
    # direction), all open iFVG positions must be tightened / closed.
    is_ifvg: bool = False
    # Price-rank tier assigned by ``compute_fvg_price_ranks`` (added
    # 2026-08-20). Values: "A" (top of the day's range for this
    # direction — most extreme), "B" (middle), "C" (weak end — most
    # likely to fail), or "" if the rank-treatment feature is off.
    # "rank_percentile" is the zone's 0..1 position within its
    # direction group for the day (0 = top bull / bottom bear,
    # 1 = bottom bull / top bear). The bar loop uses the tier to
    # scale SL/TP and optionally skip the deepest layers.
    rank_tier: str = ""
    rank_percentile: float = 0.0
    # NOTE: candle_quality (HUNT/MARGINAL/STRONG/TRUE) was REMOVED
    # 2026-09-16 because the underlying classifier required
    # future-looking zone fields (pierced_bar / inverted_bar /
    # mitigated_depth_pct) that aren't known at signal-emission time.
    # The rank_tier (price-rank percentile) is the only tier field
    # with a causal definition.
    # Explicit fill price override (added 2026-09-05). When set, the
    # bar loop uses this as the layer's fill_price directly instead
    # of computing it as ``trigger_close + layer_offset``. Used by
    # the liquidity-sweep signal: the sweep signal places a STOP
    # ORDER at the sweep price, so the fill price is fixed (not a
    # layer-offset above the trigger). The bar loop's existing
    # stop-order fill rules (b_low ≤ target for longs, b_high ≥
    # target for shorts) handle the fill naturally. When ``None``
    # (default), the legacy "trigger_close + layer_offset" semantic
    # applies.
    fill_price: float | None = None


@dataclass
class TrendStrategyParams:
    """ICT-only strategy params (no GMMA, no TEMA)."""

    # ── Trade geometry ────────────────────────────────────────────────
    # NOTE (2026-09-24, BTC fork): defaults are tuned for BTC scale.
    # With ``use_atr_scaling=True`` (the default) these are OVERRIDDEN
    # by ``sl_atr_mult * ATR`` (= ~$0.05-$0.075 on 1s BTC) and
    # ``tp_atr_mult * ATR`` (= ~$0.11-$0.165 on 1s BTC). The USD values
    # here only matter when ``use_atr_scaling=False`` is set explicitly.
    sl_usd: float = 20.0               # BTC scale: $20 vs gold $0.80
    tp_usd: float = 200.0              # BTC scale: $200 vs gold $1.80
    # ── Position size (BTC-native, added 2026-09-26) ─────────────
    # Direct BTC quantity per trade layer. On Binance USDT-M perps
    # there is no contract multiplier — 1 contract IS 1 BTC, and
    # position size is just BTC quantity. Notional at fill = qty_btc
    # × entry_price. Fee is debited as notional × taker_bps × 2 sides.
    # Default 0.001 BTC ≈ $80 notional at BTC=$80k; matches a
    # small-account micro-lot sizing for the canonical recipe.
    # PnL = signed price move × qty_btc (no multiplier).
    qty_btc: float = 0.001
    entry_on: str = "open"            # "open" | "close"
    exit_on: str = "close"            # SL/TP touch evaluated against bar high/low
    # ── Entry mode (added 2026-09-17, v6+ Innovation #1) ───────────────
    # "immediate" — legacy: FVG/iFVG signals enter a multi-layer ladder
    #   at the next bar's open. Trade dies in 1-3 bars when an SL hunt
    #   fires at the worst price in the move.
    # "sniper" — defer FVG/iFVG entries until the zone is INVERTED, then
    #   enter the iFVG at the zone's opposite edge. The hypothesis
    #   (Innovation #1 in AGENTS.md v6+ section): the inversion itself
    #   is tradeable (nb39 finding, INV_TRADE config replicates this
    #   on a clean corpus). Sniper mode SKIPS the loss-making original
    #   trade entirely and only takes the positive-EV reversal.
    # "dual" — fire BOTH paths on every FVG/iFVG signal. The
    #   immediate ladder enters on first mitigation (original
    #   direction); the sniper waits for inversion and enters on
    #   the iFVG (continuation/fade direction). The two paths
    #   target different events on the same zone (mit vs inv) and
    #   produce disjoint trade sets per zone. Doubles trade
    #   frequency on zones that both mitigate AND invert within
    #   the sniper age cap; single-fire on zones that only
    #   mitigate (immediate path) or only invert (sniper path).
    #   Default OFF ("immediate") — switch on via A/B; only fires
    #   for FVG/iFVG sources (ORB / Wyckoff / sweep are unaffected).
    entry_mode: str = "immediate"

    # ── Signal source selection ───────────────────────────────────────
    signal_source: str = "fvg"         # "fvg" | "ifvg" | "orb" | "wyckoff"
    additional_sources: List[str] = field(default_factory=list)
    # 2026-09-15 v2: default flipped to False. The bias gate
    # (``_bias(bar) != 0 and direction != _bias(bar)`` reject) was
    # silently dropping signals when the bias was neutral or in the
    # opposite direction of the FVG. The empirical finding (nb40):
    # directional-gating-by-trend-classifier underperforms the
    # counterfactual because the classifier is reactive (it changes
    # AFTER the move has happened). Conviction (used as a TP
    # multiplier) is the right way to use BoS/CHoCH — not as a gate.
    gate_on_gmma_bias: bool = False   # legacy — kept as a no-op flag for backward compat
    use_ict_signals: bool = True       # always True in this fork

    # ── FVG detector ──────────────────────────────────────────────────
    fvg_resample_secs: int = 60        # 0=1s native, 60=1m, 300=5m
    # NOTE (2026-09-24, BTC fork): defaults are tuned for BTC scale.
    # On 1s BTC the 1-bar ATR is ~$0.20-$0.30. A 1s FVG zone width
    # is typically $0.50-$5.00. The 0.10 USD floor that worked for
    # XAUUSD would filter everything on BTC; the 5.00 floor here
    # captures the medium-width zones that drive sniper trades.
    fvg_min_zone_usd: float = 5.00     # BTC: $5 minimum zone width
    fvg_max_zone_age_bars: int = 3600  # 0 = unlimited
    fvg_max_age_secs: int = 0          # wall-clock cap; if > 0, takes precedence
    fvg_max_age_bars: int = 0          # legacy bar-count fallback
    fvg_max_cache_size: int = 0        # 0 = unlimited; 64-256 caps 1s-native cache
    fvg_displacement_ratio: float = 0.0  # 0=disabled; require c2.body >= RATIO * max(c1, c3)
    fvg_body_definition: str = "body"  # "body"=abs(close-open) | "range"=high-low
    fvg_breadth_floor_usd: float = 5.00      # BTC: $5 minimum breadth-scaled SL/TP
    fvg_breadth_skips_atr_override: bool = True
    fvg_min_zone_atr_mult: float = 0.0       # 0=disabled; drop zones narrower than mult*ATR
    fvg_min_zone_atr: float = 0.0            # ATR scalar used by the above (set by caller)
    fvg_min_mitigation_pct: float = 0.0      # 0=any touch; 1.0=full fill required before entry
    fvg_retest_lookback: int = 0       # 0=unlimited; cap FVG retests to those mitigated/inverted in last K bars
    fvg_sl_per_breadth: float = 0.0    # 0=use sl_usd; else SL = k * zone_width
    fvg_tp_per_breadth: float = 0.0    # 0=use tp_usd; else TP = k * zone_width
    fvg_sl_breadth_invert: bool = False  # invert semantics for SL
    fvg_tp_breadth_invert: bool = False  # invert semantics for TP
    # ── Immediate-mode SL/TP mode selector (added 2026-09-26 d) ─────────
    # Parallel to ``sniper_sl_mode`` / ``sniper_tp_mode``. Drives the
    # non-sniper (``entry_mode='immediate'``) signal path so the
    # immediate entry can pick SL/TP from any of three regimes:
    #   * "usd_fixed"  — fixed USD (``sl_usd``/``tp_usd``).
    #   * "atr_mult"   — regime-adaptive (sl_atr_mult*ATR, tp_atr_mult*ATR).
    #   * "zone_mult"  — zone-anchored (immediate_sl_zone_mult*zone_width,
    #                     immediate_tp_zone_mult*zone_width), fed through
    #                     the existing ``fvg_sl_per_breadth`` /
    #                     ``fvg_tp_per_breadth`` breath path so the FVG
    #                     iFVG breath-scaling code picks them up
    #                     transparently.
    # Defaults preserve the pre-26d behaviour: "usd_fixed" if
    # use_atr_scaling is False (the legacy BTC default); "atr_mult"
    # if use_atr_scaling is True. The new keys only take effect when
    # the user explicitly opts in via ``optimal_params(..., immediate_sl_mode=...)``.
    immediate_sl_mode: str = ""   # "" = inherit from use_atr_scaling
    immediate_tp_mode: str = ""   # "" = inherit from use_atr_scaling
    immediate_sl_zone_mult: float = 0.0   # only used if immediate_sl_mode == "zone_mult"
    immediate_tp_zone_mult: float = 0.0   # only used if immediate_tp_mode == "zone_mult"
    # When True, the new mode selectors apply to BOTH the sniper and
    # the immediate path. Default off — sniper keeps its own dedicated
    # knobs (sniper_sl_mode, sniper_tp_mode) for back-compat.
    sxt_share_mode_with_immediate: bool = False
    # ── Conviction-aware SL/TP widening on the immediate ladder (nb57) ──
    # The immediate-ladder path submits N layers per signal at
    # ``compute_layer_sl_tp``-derived SL/TP. ``sig.conviction`` is the
    # pre-existing structural scalar (1.0 = neutral, up to ~1.5 for a
    # fresh BoS/CHoCH+ in the trade direction). These knobs use
    # conviction to widen the SL/TP post-hoc, so high-conviction
    # trades get more room to ride the structural move and low-
    # conviction trades are not penalized further.
    #
    # Formula (when widen > 0 and cv > 1.0):
    #   scaled_sl *= 1.0 + (cv - 1.0) * widen_sl
    #   scaled_tp *= 1.0 + (cv - 1.0) * widen_tp
    #
    # Defaults are identity (1.0) so canonical behavior is bit-identical.
    ladder_conviction_sl_widen: float = 1.0
    ladder_conviction_tp_widen: float = 1.0
    # Optional conviction floor: skip the layer entirely when
    # ``sig.conviction < ladder_min_conviction``. Default 0.0 (off).
    ladder_min_conviction: float = 0.0
    # Cap on num_layers used by the immediate-ladder path. With
    # default 3, all layers fire; setting to 1 drops the two
    # innermost layers (which structurally SL-hunt same-bar).
    ladder_num_layers_max: int = 3
    drop_inverted_fvg: bool = True     # FVG path drops zones that are already inverted
    # ── FVG supersession on new-zone-in-range (added 2026-08-20) ──────────
    # When a NEW FVG's [zone_low, zone_high] overlaps an existing live
    # FVG's [zone_low, zone_high] by any amount, the older zone is
    # superseded (marked dead at ``superseded_bar``) and will NOT fire
    # a retest entry. The hypothesis: only the freshest level in a
    # price region is structurally interesting — older zones in the
    # same region have been overwritten by the new displacement and
    # are no longer "the FVG to trade".
    #
    # Knob:
    #   fvg_supersede_on_new (bool, default True): turn the rule on/off.
    #     ``False`` keeps the legacy "every live zone can fire" semantic.
    #   The overlap check is on PRICE RANGE only (any amount of overlap),
    #     not on direction: a new bull FVG inside an old bull FVG's range
    #     supersedes the old one; a new bear FVG inside an old bull
    #     FVG's range also supersedes (the price region has been
    #     re-displaced, the old level is no longer authoritative).
    fvg_supersede_on_new: bool = True

    # ── FVG minimum lifetime filter (added 2026-09-17) ───────────────
    # Empirically, ~9% of FVGs on 1s XAUUSD get mitigated or inverted
    # on bar+1 (the very next 1s bar after the consequent candle).
    # These "born-dead" zones have no tradeable follow-through — the
    # market filled the gap faster than any human or algorithmic entry
    # could react. When ``fvg_min_lifetime_secs > 0``, the detector
    # DROPS any zone whose FIRST end event (mitigated / inverted /
    # superseded / played-out / structure-invalidated) happens at a
    # wall-clock delta smaller than this many seconds from the zone's
    # ``trigger_bar``. Zones that survive past the threshold pass
    # through unchanged.
    #
    # Knob:
    #   fvg_min_lifetime_secs (int, default 0):
    #     0 = disabled (legacy: every detected zone is returned, even
    #         those that died within the same second).
    #     3 = drop zones whose first end event was ≤ 3 wall-clock
    #         seconds after trigger (matches the chart's "sub-5s"
    #         noise tier on 1s XAUUSD).
    #     10 = drop zones whose first end event was ≤ 10 seconds
    #          after trigger (the median FVG lifetime is 37s on
    #          1s data, so 10s is a noise filter that keeps ~70%
    #          of zones).
    #     30 = conservative: only keep zones that survived ≥ 30s.
    #
    # This filter is wall-clock-aware (uses ``times_utc_ns`` from the
    # data) so it works correctly under ``resample_to_n_secs`` —
    # ``fvg_min_lifetime_secs=3`` means 3 wall-clock seconds regardless
    # of detection cadence.
    fvg_min_lifetime_secs: int = 0

    # ── Rolling FVG percentile tier (added 2026-09-17) ──────────────────
    # Causal replacement for the removed look-forward price-rank tier.
    # Each FVG zone is ranked against the past N same-direction zones.
    # Only PAST zones are used — no future information, no look-ahead bias.
    #
    # Anchor prices: bull → zone_high, bear → zone_low. A zone's
    # percentile tells you whether its anchor is extreme vs the window:
    #   pct=0.0: highest bull zone_high (or lowest bear zone_low) in window
    #   pct=1.0: lowest bull zone_high (or highest bear zone_low) in window
    #
    # Tier labels:
    #   A tier (top a_pct × 100%): most extreme anchors — best TP/scale
    #   C tier (bottom c_pct × 100%): least extreme anchors — tight SL/TP
    #   B tier: everything in between — neutral scales
    #
    # The tier drives SL/TP scaling and optional layer skipping in the
    # bar loop (see ``fvg_rolling_*_sl_scale`` etc.).
    #
    # Performance: O(N log N) per zone for a sort + bisect on a window
    # of size ``fvg_rolling_window_n`` (default 20). ~20*log2(20) ≈ 86
    # ops/signal. Negligible vs 700k bars/day.
    #
    # Knobs (default all off — feature is a no-op until tuned):
    #   fvg_rolling_treatment_enabled: bool  master switch
    #   fvg_rolling_window_n: int          past-N-zones window (default 20)
    #   fvg_rolling_a_pct: float          A-tier cutoff (default 0.20 = top 20%)
    #   fvg_rolling_c_pct: float          C-tier cutoff (default 0.20 = bottom 20%)
    #   fvg_rolling_a_sl_scale: float     SL multiplier for A tier (default 1.0)
    #   fvg_rolling_a_tp_scale: float     TP multiplier for A tier (default 1.0)
    #   fvg_rolling_b_sl_scale: float     SL multiplier for B tier (default 1.0)
    #   fvg_rolling_b_tp_scale: float     TP multiplier for B tier (default 1.0)
    #   fvg_rolling_c_sl_scale: float     SL multiplier for C tier (default 0.7)
    #   fvg_rolling_c_tp_scale: float     TP multiplier for C tier (default 0.5)
    #   fvg_rolling_c_layer_skip_pct: float  drop deepest N% layers on C tier
    #                                         (e.g. 0.33 with 3 layers → drop
    #                                         outermost; default 0.0 = no drop)
    #   fvg_rolling_skip_pct_above: float   skip trade if pct > this (default 1.0)
    fvg_rolling_treatment_enabled: bool = False
    fvg_rolling_window_n: int = 20
    fvg_rolling_a_pct: float = 0.20
    fvg_rolling_c_pct: float = 0.20
    fvg_rolling_a_sl_scale: float = 1.0
    fvg_rolling_a_tp_scale: float = 1.0
    fvg_rolling_b_sl_scale: float = 1.0
    fvg_rolling_b_tp_scale: float = 1.0
    fvg_rolling_c_sl_scale: float = 0.7
    fvg_rolling_c_tp_scale: float = 0.5
    fvg_rolling_c_layer_skip_pct: float = 0.0
    fvg_rolling_skip_pct_above: float = 1.0

    # ── iFVG detector ─────────────────────────────────────────────────
    ifvg_resample_secs: int = 60
    ifvg_min_zone_usd: float = 0.10
    ifvg_max_zone_age_bars: int = 3600
    ifvg_max_age_secs: int = 0
    ifvg_max_age_bars: int = 0
    ifvg_max_cache_size: int = 0
    ifvg_reverses_bias: bool = True    # iFVG inversion flips the bias gate

    # ── ORB detector ──────────────────────────────────────────────────
    orb_session_hour_utc: int = 7      # London open
    orb_duration_mins: int = 15

    # ── 3-layer orders across the FVG/iFVG zone (new in this fork) ─────
    # The default behaviour in this fork is to spread ``num_layers`` orders
    # EVENLY across the FVG zone (the zone's vertical extent, not a
    # horizontal-time ladder). The legacy "horizontal ladder around
    # trigger_bar" semantic still applies for non-FVG sources.
    num_layers: int = 3
    # SL shrink per layer: layer N SL = sl_usd * shrink^N
    layer_sl_shrink_factor: float = 1.0
    # Per-layer lifetime in seconds. Layers that don't fill within this
    # window are cancelled.
    #
    # 2026-09-15 v2: default raised from 1800 (30 min) to 7200 (2 h).
    # The 30-min cap was the binding constraint on the legacy backtest
    # (a fresh FVG's retest signal could be submitted, then immediately
    # expire before price came back to fill it). 2 h is a more honest
    # default for 1s XAUUSD — most FVGs have a complete lifecycle
    # (mitigate → retest → fill or invert) within ~30 min, but the
    # extreme tail is 1-2 h. Set to 0 for unlimited.
    layer_lifetime_secs: int = 7200
    # alpha-curve for non-FVG sources (kept for backward compat).
    alpha: float = 1.0
    min_layer_offset_usd: float = 0.0
    max_layer_offset_usd: float = 0.0  # 0 = use tp_usd
    use_dynamic_alpha: bool = False
    atr_lookback: int = 60
    atr_n_bars: float = 1.5
    dynamic_min_offset_usd: float = 0.0
    dynamic_max_offset_usd: float = 0.0

    # ── Dynamic SL/TP (new in this fork) ──────────────────────────────
    # When True, SL and TP scale INVERSELY with FVG breadth (wider
    # zone = tighter SL/TP; the "displacement is the move" intuition).
    # Set ``inverse_breadth=False`` to flip the relationship (wider
    # zone = wider SL/TP). The TP is then a function of recent
    # structure-break density; see ``TP_RULES`` in the AGENTS.md for
    # the rule table.
    inverse_breadth: bool = True
    # Floor on dynamic SL/TP — never go below $X (so a 1-cent zone
    # doesn't produce a 0.1-cent stop).
    dynamic_sl_floor_usd: float = 0.10
    dynamic_tp_floor_usd: float = 0.20
    # ATR-anchor SL/TP (added 2026-09-17 v6). When True, the SL and TP
    # are anchored DIRECTLY to recent ATR instead of the zone-edge.
    # Replaces the breadth-scaled SL/TP ("inverse_breadth") with
    #   SL = sl_atr_mult × ATR(atr_len)  (clamped to dynamic_sl_floor_usd)
    #   TP = tp_atr_mult × ATR(atr_len)  (clamped to dynamic_tp_floor_usd)
    # On 1s XAUUSD the typical 1s bar range is ~$0.13 and 20-min ATR is
    # ~$0.17, so sl_atr_mult=2.5 → SL ≈ $0.42 (3× the noise scale,
    # large enough to avoid sub-second random SLs). tp_atr_mult=5.5 →
    # TP ≈ $0.94 for a clean 1:2.25 R:R. Master switch:
    #   atr_anchor_sl_tp: bool = False (default OFF — preserves legacy)
    atr_anchor_sl_tp: bool = False
    # ATR is still used for the TP rule table (rule 2/3 use N*ATR
    # multipliers). This is a simple rolling-ATR of the recent bars.
    use_atr_scaling: bool = True
    atr_len: int = 1200
    sl_atr_mult: float = 0.25
    tp_atr_mult: float = 0.55

    # ── FVG inversion → counter-bias close (new in this fork) ─────────
    # When a position is open and the FVG it was based on gets
    # *inverted* (price trades through the zone, violating the
    # original direction), the open position(s) get a tightened
    # ``invalidation_sl_usd`` set as a soft-stop:
    #
    #   * Long position on a bull FVG: SL moves to
    #     ``zone_low - invalidation_buffer_usd`` (just under the
    #     invalidated zone — the position is closed the next time
    #     price retests from below).
    #   * Short position on a bear FVG: SL moves to
    #     ``zone_high + invalidation_buffer_usd`` (just over the
    #     invalidated zone).
    #
    # Set ``invalidation_sl_usd = 0`` to disable (legacy: position
    # runs to its original SL/TP).
    invalidation_sl_usd: float = 2.0       # BTC scale: $2 vs gold $0.05
    invalidation_buffer_usd: float = 0.50  # BTC scale: $0.50 vs gold $0.02
    # Whether iFVG re-inversions also tighten (i.e. a bull FVG
    # that was inverted to a bear iFVG, then re-inverted back to
    # a bull FVG, also fires the soft-stop on the bear iFVG's
    # open positions).
    re_inversion_also_tightens: bool = True
    # ── FVG inversion: require meaningful pierce (added 2026-08-20) ─────
    # The legacy inversion rule fires on a SINGLE bar close past the
    # zone edge (one tick of `c < zone_low` for a bull FVG, or
    # `c > zone_high` for a bear FVG). On 1s data this is too sensitive
    # — a single noisy bar can flip the zone's polarity and force a
    # soft-stop on every open position sourced from that zone.
    #
    # The fix: require the closing price to extend BEYOND the zone edge
    # by at least ``fvg_invalidation_min_pierce_usd`` USD before
    # declaring the zone inverted. A bull FVG is only inverted when
    # ``c < zone_low - fvg_invalidation_min_pierce_usd`` (the close
    # commits to the OTHER side by a real distance, not a tick), and
    # symmetrically for bear FVGs.
    #
    # Knob:
    #   fvg_invalidation_min_pierce_usd (float, default 0.0):
    #     0.0 = legacy single-tick pierce (NO-OP, feature off).
    #     0.05 = require a 5-cent close past the zone edge.
    #     0.10 = require a 10-cent close past the zone edge.
    # Set to 0 to keep the legacy behaviour. A reasonable default for
    # BTC 1s is $2-$5 (one ATR past the zone edge gets ignored).
    fvg_invalidation_min_pierce_usd: float = 2.0   # BTC scale: $2 vs gold $0.05
    # ── FVG inversion: require sustained pierce (added 2026-08-20) ─────
    # The single-tick pierce + single-bar confirmation is still too
    # sensitive on 1s data: a 1-bar close past the zone edge can fire
    # the soft-stop even when the close immediately returns inside
    # the zone on the next bar (a wick-shaped excursion). This knob
    # requires the pierce to hold for K CONSECUTIVE closing bars
    # before declaring the zone inverted. K=1 is legacy behaviour
    # (single-tick pierce counts). K=2 requires 2 back-to-back
    # closes past the edge. K=3 is the strictest ("commitment" — the
    # close has been on the wrong side for ≥3 consecutive seconds).
    #
    # Both ``fvg_invalidation_min_pierce_usd`` AND
    # ``fvg_invalidation_min_consecutive_bars`` must be satisfied
    # for an inversion to register. A pierce that is too shallow OR
    # that doesn't persist for K bars is ignored.
    #
    # Knob:
    #   fvg_invalidation_min_consecutive_bars (int, default 1):
    #     1 = legacy single-tick behaviour (NO-OP for this knob).
    #     2 = require 2 consecutive closes past the zone.
    #     3 = require 3 consecutive closes past the zone.
    fvg_invalidation_min_consecutive_bars: int = 1
    # ── FVG inversion: require pierce+RETEST, not pierce alone (added 2026-08-20)
    # On 1s XAUUSD the market routinely "probes" an FVG zone (a stop-loss
    # hunt) by closing many ticks past the zone edge and then continuing
    # one-way through the zone WITHOUT retesting. The legacy single-tick
    # pierce semantic flips polarity on the probe, forcing a soft-stop on
    # every open position sourced from the zone — at the worst price in
    # the move. A real iFVG requires BOTH the pierce AND a follow-up
    # retest from the OTHER side. This knob splits them:
    #
    #   True (recommended for 1s XAUUSD): the detector records the pierce
    #     on ``FvgZone.pierced_bar`` but does NOT mark the zone
    #     ``inverted``. The zone only flips to iFVG on the FIRST bar
    #     where price RETURNS to the original side of the zone (a bull
    #     FVG's iFVG fires on the first bar whose close is back above
    #     ``zone_high``). A one-way probe leaves the zone alive; the
    #     open trade rides the move.
    #
    #   False (legacy): sustained pierce alone flips the zone. Kept as
    #     an opt-out for callers that want the legacy behaviour.
    #
    # Knob:
    #   fvg_require_retest_to_invert (bool, default True)
    fvg_require_retest_to_invert: bool = True
    # ── FVG invalidation via market structure (added 2026-09-05) ─────
    # When ``fvg_invalidate_on_structure=True``, the detector ALSO
    # invalidates an FVG when a market-structure event in the
    # OPPOSING direction fires after the zone was formed. Concretely:
    #   * A BEAR structure event (BoS_BEAR, CHoCH_BEAR) invalidates
    #     bull FVGs (turns them into iFVGs).
    #   * A BULL structure event (BoS_BULL, CHoCH_BULL) invalidates
    #     bear FVGs.
    # The rationale: a bearish structure event rejects the structural
    # thesis of any still-live bull FVG; the zone is unlikely to fill
    # profitably now that the trend has flipped. This is a STRUCTURAL
    # invalidation — no price-side pierce required, so it can fire
    # BEFORE the 1s-pierce-based rules. The detector computes its own
    # StructureState (matching ``ms_pivot_len`` / ``ms_liquidity_len``
    # / ``ms_resample_secs`` from the strategy params) and walks its
    # per-bar events array forward from each zone's trigger_bar.
    #
    # Knobs (defaults are NO-OP until fvg_invalidate_on_structure=True):
    #   fvg_invalidate_on_structure: bool   master switch.
    #   fvg_structure_invalidation_age_secs: int  max zone age (wall-
    #     clock seconds) for the rule to fire. ``0`` (default) =
    #     unlimited age (any still-live zone is invalidated by the
    #     first opposing structure event).
    fvg_invalidate_on_structure: bool = False
    # Default 300s (5 min): only FRESH FVGs are killed by opposing
    # structure events. Rationale: a structure event that fires
    # 4 hours after an FVG was formed doesn't really "kill" the
    # FVG's thesis — the FVG already had its window. Setting this
    # to 0 makes the rule maximally aggressive (any still-live FVG
    # of opposing direction is killed by the first opposing BoS/CHoCH
    # in the data), which empirically kills ALL signals on busy
    # 1s XAUUSD days. 300s is the conservative default; tune up
    # (e.g. 1800 = 30 min) for a slightly looser rule, or down
    # (e.g. 60 = 1 min) for a tighter one.
    fvg_structure_invalidation_age_secs: int = 300
    # ── FVG played-out: cut short once direction has run (added 2026-08-20)
    # Once the price has moved PAST the zone in the FAVORABLE direction
    # by a meaningful distance, the gap-fill thesis is gone. A bull FVG
    # is "played out" when price has closed ABOVE the zone's top edge by
    # ``played_out_min_extension_usd`` USD; a bear FVG is "played out"
    # when price has closed BELOW the zone's bottom edge by the same
    # distance.
    #
    # Re-entering on a played-out zone is reopening on a stale level —
    # the market has already moved past where the FVG was the "freshest
    # level to trade". The detector marks the zone dead at
    # ``played_out_bar`` so neither the FVG nor the iFVG retest
    # scanner can open new positions on it. The zone is still drawn
    # on the chart (with a faded "played out" visual) so the user can
    # audit the lifecycle.
    #
    # The check is on the bar's CLOSE, not its high, so a brief wick
    # past the zone doesn't count (same anti-noise philosophy as the
    # inverse pierce filter).
    #
    # Knob:
    #   played_out_min_extension_usd (float, default 0.0):
    #     0.0 = disabled (NO-OP, feature off, legacy behaviour).
    #     0.30 = require the close to extend 30 cents past the zone edge.
    #     0.50 = require the close to extend 50 cents past the zone edge.
    # A reasonable default for XAUUSD 1s is 0.30–0.50 (multiple
    # ticks past the zone — the gap has clearly filled "for good").
    played_out_min_extension_usd: float = 0.0
    # ── Soft-stop grace period after entry (added 2026-08-20) ─────────
    # The soft-stop is suppressed for the first
    # ``invalidation_grace_secs`` seconds of the trade's life so a
    # soft-stop can't fire on the same bar / next bar as the entry
    # (the common noise pattern — entry fires on a retest, FVG inverts
    # on the very next bar, trade dies in 1 second). After the grace
    # window, normal soft-stop behaviour resumes.
    #
    # The grace period is measured against ``trade.entry_bar`` (in 1s
    # space), so a trade entered at bar N can have its soft-stop
    # engaged starting at bar N + grace_secs.
    #
    # Knob:
    #   invalidation_grace_secs (int, default 0):
    #     0 = no grace (legacy behaviour, NO-OP).
    #     5 = suppress soft-stop for first 5 seconds of trade life.
    #     30 = suppress soft-stop for first 30 seconds of trade life.
    invalidation_grace_secs: int = 0

    # NOTE (2026-09-17): the FVG price-rank treatment block has
    # been REMOVED. The price-rank tier system ranked zones within
    # each UTC day by their price position, which required future
    # zones in the same day to rank against — pure look-forward
    # bias. All ``fvg_rank_*`` knobs are gone. The
    # ``rank_tier`` / ``rank_percentile`` fields on
    # ``PendingSignal`` and ``Trade`` remain as empty-default
    # fields for backward compat with old Trade rows but are
    # never populated.

    # NOTE (2026-09-17): the candle-quality classification was
    # REMOVED 2026-09-16 because the underlying classifier
    # required future-looking zone fields (pierced_bar /
    # inverted_bar / mitigated_depth_pct) that aren't known at
    # signal-emission time. The ``fvg_drop_qualities`` /
    # ``fvg_emit_quality_metadata`` parameters were already
    # removed at that time. ``fvg_drop_tiers`` is removed now
    # for consistency.

    # ── Renko-driven FVG invalidation (added 2026-09-05) ─────────────────
    # The existing FVG-inversion rules fire on 1s-close-based pierce
    # events. On 1s XAUUSD that produces SL-hunt false positives (a
    # single wick past the zone flips the polarity even though price
    # never really committed). Renko gives a STRUCTURAL, time-decoupled
    # "the market has actually committed to the OTHER side" signal:
    # a brick in the opposing direction that holds for K consecutive
    # bricks. This knob turns the renko-based rule ON in addition to
    # the existing 1s-based rules (they STACK — both can fire).
    #
    # The renko rule:
    #   1. Compute renko bricks at ``renko_brick_size_usd`` resolution.
    #   2. For each open trade sourced from a live FVG, on every bar:
    #      if the current renko brick's direction OPPOSES the FVG's
    #      direction AND the renko close has been on the wrong side
    #      of the zone edge for ≥ ``renko_invalidation_min_bricks``
    #      consecutive bricks, fire the soft-stop.
    #
    # The renko bricks are also used to draw the chart's "brick
    # staircase" overlay so the user can see WHY a zone was held valid
    # vs. invalidated (a 5-brick streak on the right side is visually
    # unmistakable).
    #
    # Knobs (defaults are NO-OP until renko_drive_invalidation=True):
    #   renko_drive_invalidation: bool      master switch
    #   renko_brick_size_usd: float         brick height (0.10-0.50 sensible
    #                                       for XAUUSD 1s)
    #   renko_invalidation_min_bricks: int  consecutive opposing bricks
    #                                       required before the renko
    #                                       rule fires (2-3 is sensible)
    #   renko_invalidation_buffer_usd: float  additional buffer beyond
    #                                       the zone edge for the soft-
    #                                       stop (separate from
    #                                       invalidation_buffer_usd)
    renko_drive_invalidation: bool = False
    renko_brick_size_usd: float = 0.30
    renko_invalidation_min_bricks: int = 2
    renko_invalidation_buffer_usd: float = 0.05

    # ── FVG liquidity-sweep stop-order (added 2026-09-05) ────────────────
    # The classic "retail SL hunt" pattern: price sweeps THROUGH an FVG
    # zone (taking out retail stops), then reverses back THROUGH the
    # zone in the original direction. The existing soft-stop logic kills
    # our open FVG trades at the WORST price in the move (the sweep
    # bar's close). The sweep signal turns those -$X trades into
    # profitable entries by placing a STOP ORDER past the zone edge
    # BEFORE the sweep happens — so when the sweep fires, we enter AT
    # the worst price (the same price the soft-stop was using), but
    # our TP is on the OTHER side of the zone where the reversal heads.
    #
    # The sweep signal fires ONLY against an existing LIVE FVG that
    # hasn't yet been swept / inverted. It's a NEW signal source
    # ("sweep") in the signal-source list — to enable, add it to
    # ``additional_sources`` (e.g. ``additional_sources=['sweep']``).
    #
    # Concretely:
    #   * Bull FVG (long thesis): place a STOP BUY at
    #     ``zone_low - fvg_sweep_distance_usd``. When price dips below
    #     that level (the sweep) the buy fills; SL is at
    #     ``zone_low - fvg_sweep_atr_mult * ATR`` (room for the
    #     retest); TP is the standard zone-far-edge target.
    #   * Bear FVG (short thesis): mirror — stop SELL at
    #     ``zone_high + fvg_sweep_distance_usd``, SL above the zone.
    #
    # Each sweep signal respects the rank-tier rules, breadth-scaled
    # SL/TP, structure conviction, and grace periods just like a
    # regular FVG signal.
    #
    # Knobs (defaults are NO-OP until added to additional_sources):
    #   fvg_sweep_enabled: bool             master switch (off by default
    #                                       so legacy callers are
    #                                       unaffected). Set True when
    #                                       'sweep' is in additional_sources.
    #   fvg_sweep_distance_usd: float       distance past the zone edge
    #                                       for the stop order (0.10
    #                                       sensible for XAUUSD 1s).
    #   fvg_sweep_min_zone_usd: float       minimum zone width for the
    #                                       sweep signal to fire (small
    #                                       zones are noise).
    #   fvg_sweep_max_age_secs: int         only sweep-trade FVGs younger
    #                                       than this many seconds
    #                                       (default 30 min — fresh FVGs
    #                                       only).
    #   fvg_sweep_atr_mult: float           SL distance as multiple of
    #                                       ATR (0.5 sensible — gives
    #                                       the trade room to retest).
    #   fvg_sweep_min_distance_from_zone: float  the sweep stop order
    #                                       must be ≥ this many USD past
    #                                       the zone edge. 0 disables.
    fvg_sweep_enabled: bool = False
    fvg_sweep_distance_usd: float = 0.10
    fvg_sweep_min_zone_usd: float = 0.20
    fvg_sweep_max_age_secs: int = 1800
    fvg_sweep_atr_mult: float = 0.5
    fvg_sweep_min_distance_from_zone: float = 0.05

    # ── Body-only mitigation (added 2026-09-15) ──────────────────────────
    # When True, mitigation fires only when the candle BODY (open↔close
    # range) crosses the zone edge — NOT a wick. This kills the
    # "wick-into-zone-then-reject" false mitigation (a candle grazed
    # the zone with its wick but the close stayed outside; the legacy
    # detector with this off would treat that as a real mitigation).
    # The default ``False`` keeps the legacy close-inside-zone semantic.
    #
    # Effect on retest entries: with body-only ON, more zones fall back
    # to the "live FVG, first touch" path (start = trigger_bar + 1)
    # because the close never settles inside the zone — but the chart
    # in nb34 still draws a mitigation dot at the bar with the deepest
    # overlap. The trade is then entered on the first bar that
    # genuinely retests the zone (a body into the zone), which is
    # closer to "enter on mitigation" without losing the wick entries.
    #
    # Knob:
    #   fvg_body_only_mitigation (bool, default False): body-only
    #     mitigation depth check.
    fvg_body_only_mitigation: bool = False

    # ── Body-only inversion / invalidation (added 2026-09-15) ────────────
    # When True, a candle's wick past the zone edge does NOT count
    # as a pierce / inversion. Only CLOSE-based commits flip the zone.
    # This is the inverse of the current "pierce on close" semantic —
    # it's stricter on the CLOSE (must settle beyond the edge by the
    # configured pierce USD), not on the wick. The full state machine:
    #
    #   false (legacy): ``c < zone_low - pierce`` flips the zone.
    #   true (this): ALSO requires the bar's BODY (close vs open) to
    #     cross the zone edge in the same direction as the close. A
    #     bar that closes past the zone but opens on the original side
    #     (a wick-and-recover pattern) is NOT a pierce.
    #
    # Empirical motivation: on 1s XAUUSD, "wick SL hunts" are common —
    # a single tick past the zone that immediately reverses inside on
    # the next bar. The current ``fvg_invalidation_min_consecutive_bars=1``
    # fires on those. Body-only invalidation rejects them at the
    # detector level.
    #
    # Knob:
    #   fvg_body_only_invalidation (bool, default False): body-only
    #     pierce / inversion check.
    fvg_body_only_invalidation: bool = False

    # ── iFVG min inversion age (added 2026-09-15) ────────────────────────
    # For iFVG (``only_inverted=True``) retest entries: skip the
    # signal if the source zone was inverted within
    # ``fvg_ifvg_min_inversion_age_secs`` seconds of trigger. This
    # filters "immediate-inversion" zones — the D-tier signature
    # from nb38 (100% inverted, ~92% of D trades exit via soft-stop).
    # Zones that hold for at least N seconds before inverting have
    # proved themselves as a level; their iFVG reversal is structural
    # (a real flip), not an SL hunt.
    #
    # The bar-loop KNOB does NOT affect the soft-stop path — the
    # soft-stop still fires on any inversion. The age filter is
    # applied at the RETEST SIGNAL level: iFVG retest entries on
    # zones that flipped too fast are not submitted. The original
    # FVG trades on those zones still get the soft-stop treatment.
    #
    # Knob:
    #   fvg_ifvg_min_inversion_age_secs (int, default 0):
    #     0 = disabled (NO-OP, fire iFVG entries on every inversion).
    #     30 = require ≥ 30s of zone lifetime before inversion.
    #     60 = require ≥ 60s of zone lifetime before inversion.
    fvg_ifvg_min_inversion_age_secs: int = 0

    # ── Trade-the-D-inversion edge (added 2026-09-15) ───────────────────
    # When a live FVG zone gets inverted (via any of the existing
    # inversion rules), and the inversion fires the soft-stop on an
    # OPEN position sourced from that zone, this knob ALSO opens a
    # NEW trade in the OPPOSITE direction at the next bar's open.
    # The rationale: per nb39, the D-tier inversion is tradeable in
    # its own right — entering the opposite direction after the
    # polarity flip earns +$3.63/trade on average (60s horizon, 1:1.8
    # R:R). Without this knob, the inversion is wasted (the soft-stop
    # closes the original trade but doesn't open the inverse one).
    #
    # Mechanics:
    #   * Long trade sourced from bull FVG → zone inverts (bear)
    #     → soft-stop fires → NEW short trade at next-bar open,
    #     SL = zone_width × ``fvg_inv_trade_sl_zone_mult``,
    #     TP = zone_width × ``fvg_inv_trade_tp_zone_mult``.
    #   * Short trade sourced from bear FVG → zone inverts (bull)
    #     → soft-stop fires → NEW long trade at next-bar open, mirror.
    #
    # The inverse trade is marked with ``entry_triggered_by='inv'``
    # in the diagnostics so it can be analysed separately from the
    # baseline trades.
    #
    # Knobs (defaults are NO-OP until fvg_inv_trade_enabled=True):
    #   fvg_inv_trade_enabled: bool            master switch.
    #   fvg_inv_trade_sl_zone_mult: float     SL as multiple of zone width.
    #                                           1.0 = SL = zone_width.
    #   fvg_inv_trade_tp_zone_mult: float     TP as multiple of zone width.
    #                                           1.8 = TP = 1.8 × zone_width.
    #   fvg_inv_trade_min_zone_usd: float     minimum zone width to fire
    #                                           (avoids tight zones).
    #   fvg_inv_trade_max_per_zone: int       cap on the number of
    #                                           inverse trades sourced
    #                                           from one zone (1 = once).
    fvg_inv_trade_enabled: bool = False
    fvg_inv_trade_sl_zone_mult: float = 1.0
    fvg_inv_trade_tp_zone_mult: float = 1.8
    # ── ATR-scaled TP for sniper / INV_TRADE (added 2026-09-17) ─────────────
    # When ``fvg_inv_trade_tp_atr_mult > 0``, the sniper / INV_TRADE
    # TP distance is ``ATR_at_entry_bar × fvg_inv_trade_tp_atr_mult``
    # instead of ``zone_w × fvg_inv_trade_tp_zone_mult``. This makes the
    # TP regime-adaptive — it scales with the day's volatility rather
    # than the zone's static width. ATR is computed at the entry bar
    # (1s ATR with ``atr_len`` bars) and is known at fill time (the
    # sniper triggers on the inversion bar and fills at next-bar open).
    #
    # ``fvg_inv_trade_tp_zone_mult`` and ``fvg_inv_trade_tp_atr_mult``
    # are mutually exclusive — only ONE is non-zero at a time. Both
    # default to non-zero values; the ATR mult takes precedence when
    # its knob is > 0. Set ``fvg_inv_trade_tp_zone_mult=0`` to disable
    # the zone-width path explicitly.
    #
    # The motivation: zone-width scaling makes a $2-wide zone (volatile
    # regime) get a $44 TP, while a $0.30 zone (calm regime) gets $6.60
    # TP. ATR scaling inverts this — both zones get the same TP
    # volatility-budget, regardless of zone size.
    fvg_inv_trade_tp_atr_mult: float = 0.0   # 0=disabled; >0 = use ATR scaling
    fvg_inv_trade_min_zone_usd: float = 0.30
    fvg_inv_trade_max_per_zone: int = 1
    # ── SL/TP sizing mode for the sniper path (added 2026-09-26, c) ────
    # Independent toggles for SL and TP sizing. Each can be:
    #   * ``"zone_mult"``  — size = zone_w × fvg_inv_trade_sl_zone_mult
    #                         (or tp_zone_mult). Default for both.
    #   * ``"atr_mult"``   — size = ATR_at_entry_bar × fvg_inv_trade_sl_atr_mult
    #                         (or tp_atr_mult). ATR-anchored = regime-adaptive.
    # Two knobs because SL and TP have independent R:R roles — a
    # regime-adaptive SL with a zone-anchored TP is a sensible combo.
    # The legacy ``fvg_inv_trade_tp_atr_mult > 0`` path is honoured as
    # "atr_mult" if ``sniper_tp_mode == "zone_mult"`` (default) — the
    # legacy behaviour stays bit-identical until the user opts into the
    # new mode explicitly.
    sniper_sl_mode: str = "zone_mult"   # "zone_mult" | "atr_mult"
    sniper_tp_mode: str = "zone_mult"   # "zone_mult" | "atr_mult"
    # Companion ATR mults (only used when the matching mode == "atr_mult").
    # Defaults match the canonical ``sl_atr_mult=0.25`` / ``tp_atr_mult=0.55``.
    fvg_inv_trade_sl_atr_mult: float = 0.25
    fvg_inv_trade_tp_atr_mult_v2: float = 0.55  # 2026-09-26 c: separate from
                                                # the legacy tp_atr_mult knob
    # ── Mitigation-distance filter (added 2026-09-26, nb53 alpha) ─────
    # Only emit a retest signal if the zone's mitigation / inversion
    # bar is at least ``fvg_min_mit_distance_bars`` away from the
    # trigger bar. A 1-bar-apart mitigation is a drive-through, not
    # a meaningful fill — it produces a "mitigated" tag but no real
    # commitment to the inverted side. Setting this to 3 (or higher)
    # filters drive-throughs and keeps only zones that took real
    # time to fill. ``0`` = no distance filter (legacy behaviour).
    fvg_min_mit_distance_bars: int = 0
    # ── Strict-wick FVG detection (added 2026-09-26, nb53 alpha) ─────
    # When True, only emit an FVG if BOTH outer candles have a visible
    # wick (their non-gap extreme is at least
    # ``fvg_strict_wick_min_wick_usd`` USD away from their respective
    # body). Default False — preserves the canonical detector which
    # only checks for the gap (c1.H < c3.L for bull), regardless of
    # wick visibility. nb53 alpha Scenario C explores whether
    # "non-wicked candles" (where c1 or c3 are essentially flat-body)
    # produce noisier zones.
    fvg_strict_wick_required: bool = False
    fvg_strict_wick_min_wick_usd: float = 0.0
    # ── Sniper-in mode age cap (added 2026-09-17, v6+ Innovation #1) ─────
    # When ``entry_mode="sniper"``, pending sniper layers that have NOT
    # been triggered (zone not yet inverted) within this many wall-clock
    # seconds are dropped. Rationale: an FVG that survives past its age
    # cap without inverting is a "level that held" — its iFVG reversal
    # is unlikely now. Drop the stale sniper. ``0`` = unlimited.
    sniper_max_age_secs: int = 1800
    # ── Sniper direction on inversion (added 2026-09-26, nb53 alpha;
    #    renamed to enum 2026-09-26) ───────────────────────────────
    # The sniper waits for the FVG zone to be inverted, then opens a
    # trade. This knob controls which way that trade goes:
    #
    #   "continuation"      (canonical, default — preserves v17 BTC SNIPER)
    #     Enter in the SAME direction as the original FVG's gap
    #     polarity. The inversion is treated as a liquidity sweep
    #     that the original displacement thesis survives; the sniper
    #     is "patient" and rides the move after the shakeout.
    #
    #   "fade_displacement" (legacy-live-equivalent)
    #     Enter OPPOSITE the original FVG's gap polarity. The
    #     inversion is treated as a failed breakout; the sniper
    #     fades the original move.
    #
    # Mathematically: with the scanner's iFVG direction already
    # carrying one flip (-z.direction), the backtest computes
    #   inv_dir_continuation      = -sp["direction"]
    #   inv_dir_fade_displacement = -inv_dir_continuation
    #                             = +sp["direction"]
    #
    # **Live engine divergence (2026-09-26)**: the live engine in
    # the sibling ``ict_sniper_live`` repo currently implements
    # ``fade_displacement`` semantics (it stores ``direction =
    # z.direction`` at submit, then flips once on fire). The
    # canonical backtest recipe defaults to ``continuation`` to
    # match the parent ``ict_tier_v2`` v17 SNIPER full-corpus
    # validation. The two paths WILL disagree on the same setup
    # until the live engine is rewired — see AGENTS.md § "Sniper
    # direction on inversion" for the divergence table and the
    # planned reconciliation.
    sniper_inv_direction_mode: str = "continuation"

    # ── Strict-wick FVG filter (added 2026-09-26, BTC v17 SNIPER c) ─
    # When ``strict_wick_required=True`` AND
    # ``strict_wick_min_wick_price_pct > 0``, the detector requires
    # both outer candles (c1 and c3) to have a visible wick on the
    # side AWAY from the gap. The required wick length is
    # ``strict_wick_min_wick_price_pct × mid_price_at_zone_trigger``,
    # where mid_price is the rolling hourly median of the close
    # recomputed at ``strict_wick_recompute_secs`` (default 3600s).
    # A bar that opens inside the zone and closes outside still
    # counts (body crosses); a bar that closes inside but opened
    # outside is rejected (only wick touched).
    #
    # Default values: 0.025% of mid-price, hourly recompute.
    # At BTC=$100k → $25 floor; at BTC=$30k → $7.50 floor. Keeps
    # the filter price-relative without per-bar recompute cost.
    strict_wick_required: bool = True
    strict_wick_min_wick_price_pct: float = 0.00025  # 0.025% of mid-price
    strict_wick_recompute_secs: int = 3600          # hourly bucket

    # ── "Clean" zone gates (added 2026-09-26, BTC v17 SNIPER c) ──────
    # A zone is considered "clean" / "organic" iff both its mitigation
    # and inversion events occur at least N 1-second bars AFTER the
    # zone's trigger bar. Zones that get mitigated/inverted within
    # 1-2 bars of formation are treated as "drive-through" noise
    # (immediate single-tick pierce followed by quick re-entry) and
    # the sniper path is the appropriate response.
    #
    # When a zone's entry triggers (via the iFVG retest scanner) and
    # BOTH ``mitigated_bar - trigger_bar >= fvg_min_mit_distance_bars``
    # AND ``inverted_bar - trigger_bar >= fvg_min_inv_distance_bars``,
    # the bar loop routes the entry to the NON-sniper ("normal")
    # path — immediate entry at next-bar open, ATR-anchored SL/TP.
    # Otherwise the sniper path runs (deferred entry, zone-anchored
    # SL/TP). Both paths are evaluated on every triggered zone; the
    # routing decision is per-zone, made at entry-bar time.
    fvg_min_mit_distance_bars: int = 3
    fvg_min_inv_distance_bars: int = 3
    # ── Routing-floor overrides (added 2026-09-26, nb56) ───────────
    # The clean-path / dirty-path routing decision in the bar loop
    # uses the same ``fvg_min_*_distance_bars`` knobs as the detector
    # by default. These knobs override ONLY the bar-loop routing
    # floor — the detector keeps using ``fvg_min_*_distance_bars`` so
    # the SweepCache fingerprint is unaffected (no cache rebuild on
    # override). ``None`` (default) ⇒ fall through to the canonical
    # detector floor. Set to an explicit integer to force the
    # routing decision in isolation from detection.
    #
    # Usage: ``optimal_params(fvg_route_min_mit_distance_bars=0)``
    # routes every zone to the clean (immediate) path; the value 0
    # disables the clean gate in the bar loop only. ``...=1_000_000``
    # routes every zone to the dirty (sniper) path.
    fvg_route_min_mit_distance_bars: int | None = None
    fvg_route_min_inv_distance_bars: int | None = None

    # ── DEPRECATED: sniper_flip_direction (added 2026-09-26, kept
    #    until 2026-12-31 as a back-compat shim) ─────────────────────
    # The boolean ``sniper_flip_direction=True`` mapped to the new
    # ``sniper_inv_direction_mode="fade_displacement"`` semantics. New
    # code should use the enum directly. The bool is honoured here
    # only by ``__post_init__`` so old ``optimal_params(...,
    # sniper_flip_direction=True)`` calls keep working until the
    # callers are migrated.
    sniper_flip_direction: bool = False

    # ── Cooldown after a losing streak (kept from parent) ─────────────
    cooldown_bars: int = 0
    streak_threshold: int = 2

    # ── Market-structure conviction (kept from v2.2) ──────────────────
    use_market_structure: bool = True
    ms_min_conviction: float = 0.0
    ms_boost_conviction: float = 1.0
    ms_max_boost_age_bars: int = 60
    ms_choch_caution_age_bars: int = 60
    ms_apply_to_fvg: bool = True
    ms_apply_to_ifvg: bool = True
    ms_apply_to_orb: bool = True
    ms_apply_to_wyckoff: bool = True
    ms_pivot_len: int = 9
    ms_liquidity_len: int = 30
    ms_resample_secs: int = 60   # 0=raw 1s bars (legacy); 60=1m pivots (recommended for 1s data)
    ms_draw_order_blocks: bool = True
    ms_draw_liquidity_sweeps: bool = True

    # ── BoS/CHoCH memory + alignment-based inversion suppression
    #    (added 2026-09-16) ────────────────────────────────────────────
    # The user's revised rules (2026-09-16): BoS/CHoCH events are
    # NOT a hard gate. The state machine retains the last N events
    # (default 5) and walks forward:
    #   * BoS continues the prior thesis (no flip).
    #   * CHoCH flips the thesis (the character of the market has
    #     changed — previously bull BoS, now bear CHoCH → next
    #     move is bearish).
    #
    # The signal pipeline always emits the signal (no rejection).
    # The gate's only effect is on the FVG-inversion soft-stop:
    #   * When the trade is **aligned** with the most-recent-event
    #     thesis (e.g. long after bull BoS or bull CHoCH), we
    #     **suppress** the inversion soft-stop. The aligned thesis
    #     is the dominant force; a temporary inversion is more
    #     likely a SL hunt than a real reversal. Let the trade
    #     ride to its original SL/TP.
    #   * When the trade is **opposed** to the most-recent-event
    #     thesis (e.g. long after bear CHoCH — the trend has
    #     flipped against you), the inversion soft-stop fires as
    #     normal — the trade is fighting structure and the soft-stop
    #     is the right exit.
    #
    # The "ignore_invert_when_aligned" master switch controls this
    # semantic; default True because the soft-stop is the dominant
    # loss path for aligned trades.
    bos_choch_ignore_invert_when_aligned: bool = True
    # How many recent BoS/CHoCH events the memory retains. The
    # trade thesis is computed by walking this memory in chronological
    # order: the FIRST event sets the initial thesis, each
    # subsequent CHoCH flips it. BoS continues the prior thesis.
    bos_choch_memory_n_events: int = 5
    # ── Structural-alpha gate (added 2026-09-26, vBTC3 finding) ──────
    # Drop signals whose direction doesn't agree with the structure
    # trend at the trigger bar. Validated on the 6-month BTC sample:
    #   baseline (no gate): n=346 sum=$+624.20 EV=$+1.8041 WR=15.9%
    #   trend-aligned only: n=184 sum=$+416.38 EV=$+2.2630 WR=20.7%
    # Note: the filter raises per-trade EV but lowers total PnL by
    # dropping ~half the trades; the user-visible benefit is more
    # selective trades with less drawdown. Disabled by default.
    gate_trend_aligned: bool = False
    # Apply the inversion suppression to all four sources by default.
    # iFVG is a reversal entry; with `ifvg_reverses_bias=True` (default)
    # the iFVG direction is the INVERSE of the source FVG's direction,
    # so the alignment check uses the post-inversion direction.
    bos_choch_ignore_invert_apply_fvg: bool = True
    bos_choch_ignore_invert_apply_ifvg: bool = True
    bos_choch_ignore_invert_apply_orb: bool = True
    bos_choch_ignore_invert_apply_wyckoff: bool = True
    bos_choch_ignore_invert_apply_sweep: bool = True

    # ── Trade management ──────────────────────────────────────────────
    one_position: bool = True
    flip_on_invalidation: bool = False
    breakeven_after_tp: bool = False
    # ── Same-bar SL/TP tiebreak (added 2026-09-17) ─────────────────
    # When a bar's range covers both the SL and TP in the same bar,
    # this knob controls which fires first (the other is missed).
    #   "sl_first"  — assume SL fires first (conservative default)
    #   "tp_first"  — assume TP fires first (lets winners run)
    #   "tp_if_wider" — if TP distance > SL distance, assume TP fires;
    #                   otherwise SL fires (symmetry-based heuristic)
    # The default ("sl_first") preserves legacy behavior. For swing-style
    # strategies with wide TPs (e.g. sniper TP=22x), "tp_first" or
    # "tp_if_wider" may better reflect the intent: let the winner run.
    sl_tp_tiebreak: str = "sl_first"

    # ── PIVOT F: multi-bar entry confirmation (added 2026-09-17) ───────
    # When set to N > 0, the FVG/iFVG retest scanner requires N consecutive
    # bars where price's range overlaps the zone before emitting the retest
    # signal. Filters out single-bar noise touches where the bar enters the
    # zone briefly but doesn't settle. The filter is applied AFTER the
    # standard retest-bar scan (so it doesn't affect WHEN the retest fires,
    # only whether the retest is ACCEPTED). The detector's n_touches field
    # tracks this. Default 0 = legacy (accept the first qualifying retest).
    fvg_entry_confirm_bars: int = 0

    # ── PIVOT I: minimum retest count before signal fires (added 2026-09-17) ─
    # When set to N > 0, the retest scanner only fires for zones that have
    # had at least N total bars with range-overlap into the zone (counted
    # in FvgZone.n_touches). Filters "single-touch" FVGs where the zone was
    # barely touched and immediately reversed. Default 0 = legacy (all live
    # zones produce retests regardless of touch count).
    fvg_min_retest_count: int = 0

    # ── PIVOT A: regime filter for sniper (added 2026-09-17) ────────────
    # When set to a value > 0, sniper trades only fire when current ATR is above
    # this percentile of the last 24h ATR distribution. Filters sniper
    # trades in low-vol range sessions where inversions are SL-hunt noise.
    # Default 0 = disabled (fire all sniper trades regardless of regime).
    # A reasonable value is 70.0 (fire only when ATR is in the top 30%).
    sniper_regime_atr_pct: float = 0.0

    # ── PIVOT E: trailing SL (added 2026-09-17) ─────────────────────────
    # When enabled, the SL is lifted ("trailed") once price moves enough in
    # the trade's favor. The trail activates when price reaches
    # trailing_activation_atr_mult * ATR in profit, then trails at
    # trailing_atr_mult * ATR behind the best price seen.
    trailing_sl_enabled: bool = False
    trailing_atr_mult: float = 0.5    # SL = best_price - trail_mult * ATR
    trailing_activation_atr_mult: float = 1.0   # activate trail when in profit by this × ATR

    # ── PIVOT G: SL time-decay schedule (added 2026-09-17) ──────────────
    # A list of (bar_offset, sl_multiplier) tuples that tighten the SL
    # as the trade ages without moving in favor. Each entry means:
    # "at bar_offset, multiply the original stop_usd by this factor".
    # Applied after trailing_sl (if enabled). Default [] = legacy (no decay).
    # Example: [(60, 0.5), (180, 0.2), (300, 0.0)] means:
    #   at 60 bars: SL = 50% of original
    #   at 180 bars: SL = 20% of original
    #   at 300 bars: SL = breakeven
    sl_decay_schedule: list = field(default_factory=list)

    # ── Binance fee model (added 2026-09-24, BTC fork) ───────────────
    # USDT-M perpetual futures charge on the NOTIONAL × bps at entry
    # AND at exit. Round-trip = 2 × per-side. Default values reflect
    # the published Binance VIP0 retail rate WITHOUT BNB discount:
    #   taker 0.04% / maker 0.02% per side
    # With BNB discount (recommended for live): 0.039% / 0.019%.
    # The canonical BTC recipe (``optimal_params()``) uses 5 bps as a
    # conservative round number that lies between 4 (raw taker) and
    # 5 (the user-quoted 0.05% rate that already includes some
    # slippage).
    #
    # Snipers fill via MARKET orders, so they pay the TAKER rate on
    # both entry and exit. If you switch to limit-entry in the live
    # engine, set ``taker_fee_bps`` lower for entry and use
    # ``maker_fee_bps`` for the exit if it also exits via limit.
    # The backtest currently charges taker on both sides (sniper-only).
    #
    # Set BOTH to 0.0 to disable fees entirely (back-compat with
    # the v17 XAUUSD recipe, which had no fee model).
    taker_fee_bps: float = 0.0       # 0.05% per side = 5.0 bps
    maker_fee_bps: float = 0.0       # 0.02% per side = 2.0 bps

    # ── Post-init validation + deprecated-shim resolution ─────────
    # Implemented as a method (not __post_init__) because dataclass
    # ``__post_init__`` requires every inherited field to be supplied
    # on construction — inconvenient for the factory pattern in
    # ``optimal_config.optimal_params``. Callers should invoke
    # ``self._resolve_deprecated()`` once after any manual edits to
    # ``sniper_flip_direction`` (the canonical factory path does this
    # automatically).
    def _resolve_deprecated(self) -> None:
        """Reconcile the deprecated ``sniper_flip_direction`` bool
        with the canonical ``sniper_inv_direction_mode`` enum.

        The legacy boolean maps as:
            ``sniper_flip_direction=True``
                → ``sniper_inv_direction_mode = "fade_displacement"``
            ``sniper_flip_direction=False``
                → no override (the enum's own value wins).

        Emits a ``DeprecationWarning`` if the bool is set to True
        so callers know to migrate.
        """
        # Validate enum value first
        valid = ("continuation", "fade_displacement")
        if self.sniper_inv_direction_mode not in valid:
            raise ValueError(
                f"sniper_inv_direction_mode must be one of {valid}, "
                f"got {self.sniper_inv_direction_mode!r}"
            )
        # Bool shim
        if self.sniper_flip_direction:
            import warnings as _w
            _w.warn(
                "sniper_flip_direction=True is deprecated; use "
                "optimal_params(sniper_inv_direction_mode='fade_displacement') "
                "instead. The bool is honoured until 2026-12-31.",
                DeprecationWarning,
                stacklevel=3,
            )
            self.sniper_inv_direction_mode = "fade_displacement"

    def resolve_sl_usd(self, atr_value: float | None = None) -> float:
        if self.use_atr_scaling and atr_value is not None and atr_value > 0:
            return self.sl_atr_mult * atr_value
        return self.sl_usd

    def resolve_tp_usd(self, atr_value: float | None = None) -> float:
        if self.use_atr_scaling and atr_value is not None and atr_value > 0:
            return self.tp_atr_mult * atr_value
        return self.tp_usd
