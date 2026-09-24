"""ICT-only backtest — 3-layer orders, dynamic SL/TP, inversion soft-stop.

This is a clean-room rewrite of the parent repo's
``src/backtest/trend_backtest.py`` for the ICT-only fork
(``gmma_guppy_ict``). It contains *only* the FVG / iFVG / ORB /
Wyckoff signal paths; GMMA trend logic and TEMA filters are removed.

Three new behaviours are implemented in this module (vs the parent
repo):

1. **3-layer orders spread evenly across the FVG/iFVG zone.**
   When a signal fires, the bar-loop splits it into
   ``params.num_layers`` (=3 by default) limit orders placed
   evenly across the FVG's vertical extent (zone_low ↔ zone_high).
   Layer 0 = closest to the trigger (innermost), layer N-1 = deepest.
   Each layer has its own fill bar, lots = ``lots / num_layers``,
   and a per-layer SL.

2. **Dynamic SL/TP based on FVG breadth (inverse-by-default) and
   structure-break density.** The SL is anchored to the *zone* of
   the FVG (not the trigger-bar close), so wider zones naturally
   give wider zones of acceptable entry. The TP is a function of
   the SL and a structure-density multiplier (see
   ``TP_RULES`` in the AGENTS.md for the rule table). With
   ``inverse_breadth=True`` (default), wider zones produce
   tighter SL/TP multipliers; with ``False``, wider zones produce
   wider SL/TP.

3. **FVG inversion → soft-stop on all open positions.** When the
   FVG that the trade was based on gets *inverted* (price
   trades through the zone, violating the original direction),
   every open position sourced from that FVG (or any iFVG that
   was itself a child of that FVG) has its SL tightened to the
   zone's opposing edge + ``invalidation_buffer_usd`` (i.e. just
   outside the invalidated zone). The position is *not* closed
   immediately — the next bar that retests the zone from the
   wrong side triggers the soft-stop. This is the proper ICT
   semantic for an invalidated FVG.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from ..core.ict_strategy import PendingSignal, TrendStrategyParams
from ..core.ict_signals import (
    IctSeries,
    compute_simple_atr,
    generate_ict_pending_signals,
)
from ..core.market_structure import (
    StructureState,
    detect_market_structure,
    annotate_choch_plus,
    structure_conviction,
    BosChochMemory,  # imported here (was inside _bos_choch_entry_alignment — moved for speed 2026-09-17)
)


# ────────────────────────────────────────────────────────────────────────────
# Result types
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    """One closed trade."""
    entry_bar: int
    exit_bar: int
    entry_time: int                  # ns since epoch utc
    exit_time: int                   # ns since epoch utc
    direction: int                   # +1 long, -1 short
    entry_price: float
    exit_price: float
    stop_usd: float                  # SL distance at entry
    target_usd: float                # TP distance at entry
    exit_reason: str                 # sl | tp | inv | eod | cancel
    hold_secs: float
    pnl_usd: float                   # signed, price move * lots (gross, before fees)
    # ── Binance fees (added 2026-09-24, BTC fork) ─────────────────────
    # Total commission debited in USDT for this round-trip trade
    # (entry taker fee + exit taker fee). Computed in ``_close_trade``
    # from ``TrendStrategyParams.taker_fee_bps`` / ``maker_fee_bps``.
    # For market-entry + market-exit sniper fills the entry uses the
    # taker rate (5 bps). The fee is debited from ``pnl_usd`` so the
    # reported PnL is the **NET** (after-fee) amount. Set
    # ``taker_fee_bps=0.0`` on ``TrendStrategyParams`` to disable.
    #
    # Formula: ``fee_usd = entry_price * lots * contract_size *
    #                   (taker_fee_bps / 10000) * 2`` (entry + exit).
    fee_usd: float = 0.0
    # Per-side bps charged (echo of the recipe at execution time so
    # the CSV round-trip doesn't lose the fee-rate context).
    taker_bps_charged: float = 0.0
    entry_triggered_by: str = "fvg"
    lots: float = 0.0
    layer_idx: int = 0               # which of the N layers this trade came from
    n_layers_signal: int = 1         # total layers for the parent signal
    signal_id: int = -1
    # FVG zone reference (for soft-stop / inversion tracking)
    fvg_zone: object = None          # FvgZone, or None for non-FVG sources
    is_ifvg: bool = False
    # The TP rule that fired (1-4) — see TP_RULES in the AGENTS.md.
    tp_rule: int = 0
    # NOTE (2026-09-17): the price-rank tier / percentile fields
    # stay as empty defaults on Trade for backward compat with
    # old Trade rows. They are never populated (the underlying
    # classifier is look-forward biased and has been removed).
    rank_tier: str = ""
    rank_percentile: float = 0.0
    # NOTE (2026-09-17): candle_quality (HUNT/STRONG/TRUE/MARGINAL)
    # field was REMOVED 2026-09-16. ``rank_tier`` above is the
    # price-rank percentile system, which is now also REMOVED for
    # look-forward bias (2026-09-17) but kept as an empty default
    # field for backward compat.
    # BoS/CHoCH alignment at entry (added 2026-09-16): "aligned" /
    # "opposed" / "unknown". Used for diagnostics — the soft-stop
    # bypass already happened at the bar loop, but this records
    # what the alignment was for post-hoc analysis.
    entry_alignment: str = ""
    # NOTE (2026-09-17): ``n_layers_skipped`` removed (it was
    # populated by the removed rank-treatment C-tier skip logic).

    def net_pnl_usd(self) -> float:
        """Return PnL AFTER fees (gross - fee_usd)."""
        return self.pnl_usd - self.fee_usd

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("fvg_zone", None)      # FvgZone is not JSON-serialisable
        d["net_pnl_usd"] = d["pnl_usd"] - d["fee_usd"]
        return d


@dataclass
class IctBacktestResult:
    """Output of ``run_ict_backtest``."""
    strategy: str
    params: dict
    trades: List[Trade] = field(default_factory=list)
    open_lots_at_eod: float = 0.0
    equity_curve: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float64))
    # ── Tick-fill metadata (added 2026-09-24, BTC fork) ──────────────
    # Populated by ``src/tick/tick_backtest.run_tick_backtest`` with
    # the timing/throughput of the tick-fill hybrid run. ``None`` for
    # bar-only backtests (legacy ``run_ict_backtest``).
    tick_metadata: Optional[Dict[str, Any]] = None
    n_signals_emitted: int = 0
    n_signals_consumed: int = 0
    n_fills: int = 0
    n_soft_stops: int = 0             # closes caused by FVG inversion
    n_inversions_detected: int = 0
    # NOTE (2026-09-17): ``n_signals_rank_a`` / ``_b`` / ``_c`` /
    # ``_skipped`` removed (the rank-treatment counters depended
    # on the look-forward biased price-rank classifier).
    # Liquidity-sweep diagnostics (added 2026-09-05): how many sweep
    # signals fired and how many filled.
    n_sweep_signals: int = 0
    n_sweep_fills: int = 0
    # Renko-driven invalidation diagnostics (added 2026-09-05):
    # how many soft-stops fired specifically via the renko rule.
    n_renko_invalidations: int = 0
    # Structure-driven FVG invalidation diagnostics (added 2026-09-05):
    # how many live FVGs were flipped to iFVGs by an opposing BoS/CHoCH.
    # This is a STRUCTURAL count — these zones didn't have a price-side
    # pierce, they were killed by a structure event rejecting their thesis.
    n_structure_invalidations: int = 0
    # NOTE (2026-09-17): the candlestick-quality counters and the
    # A/B/C/D tier drop counters have been REMOVED entirely —
    # both classifier systems were look-forward biased. The
    # ``fvg_drop_qualities`` and ``fvg_drop_tiers`` knobs are
    # gone; the ``fvg_drop_tiers=['D']`` legacy alias used to map
    # to ``fvg_drop_qualities=['HUNT']`` and is no longer relevant.
    # BoS/CHoCH alignment diagnostics (added 2026-09-16): how many
    # FVG-inversion events were SUPPRESSED because the trade was
    # aligned with the BoS/CHoCH thesis at entry. These are trades
    # that would have soft-stopped under the legacy rule but were
    # allowed to ride to their original SL/TP under the new
    # ``bos_choch_ignore_invert_when_aligned=True`` semantic.
    n_alignment_skipped_inversions: int = 0
    # Number of trade entries that were aligned / opposed / unknown
    # at entry time. Useful for tuning the memory window and
    # understanding the population.
    n_entry_alignment_aligned: int = 0
    n_entry_alignment_opposed: int = 0
    n_entry_alignment_unknown: int = 0
    # iFVG min-age filter diagnostics (added 2026-09-15): how many
    # iFVG retest entries were dropped because their source zone
    # was inverted within ``fvg_ifvg_min_inversion_age_secs`` of
    # trigger (the D-tier "immediate inversion" pattern).
    n_ifvg_age_dropped: int = 0
    # Body-only mitigation / invalidation counters (added 2026-09-15):
    # how many FVGs the detector flagged as mitigated / inverted
    # under the body-only semantics (i.e. when
    # ``fvg_body_only_mitigation=True`` and/or
    # ``fvg_body_only_invalidation=True``).
    n_body_mitigations: int = 0
    n_body_inversions: int = 0
    # Trade-the-D-inversion edge diagnostics (added 2026-09-15):
    # how many inverse-direction trades the soft-stop path
    # submitted / filled (when ``fvg_inv_trade_enabled=True``).
    n_inv_trades_submitted: int = 0
    n_inv_trades_filled: int = 0
    # Sniper-in mode diagnostics (added 2026-09-17, v6+ Innovation #1):
    # how many FVG/iFVG signals were DEFERRED (n_sniper_submitted),
    # how many fired via zone inversion (n_sniper_triggered), how
    # many were dropped by supersede / played_out / structure-invalid
    # / per-zone cap (n_sniper_cancelled), and how many expired by
    # sniper_max_age_secs (n_sniper_expired). Only nonzero when
    # ``entry_mode="sniper"``.
    n_sniper_submitted: int = 0
    n_sniper_triggered: int = 0
    n_sniper_cancelled: int = 0
    n_sniper_expired: int = 0
    elapsed_secs: float = 0.0

    def trades_df(self) -> pd.DataFrame:
        if not self.trades:
            return pd.DataFrame()
        df = pd.DataFrame([t.to_dict() for t in self.trades])
        df['entry_time'] = pd.to_datetime(df['entry_time'], unit='ns', utc=True)
        df['exit_time'] = pd.to_datetime(df['exit_time'], unit='ns', utc=True)
        return df

    def summary(self) -> dict:
        df = self.trades_df()
        base = {
            "n_trades": 0, "pnl_total": 0.0, "pnl_per_trade": 0.0,
            "win_rate": 0.0, "trades_per_day": 0.0, "ev_per_trade": 0.0,
            "n_soft_stops": int(self.n_soft_stops),
            # NOTE (2026-09-17): candlestick-quality and tier
            # diagnostics removed from summary dict (the
            # underlying classifiers are look-forward biased).
        }
        if len(df) == 0:
            return base
        pnl = df['pnl_usd']
        wins = df[df['pnl_usd'] > 0]
        losses = df[df['pnl_usd'] <= 0]
        wr = len(wins) / len(df) if len(df) else 0.0
        avg_win = wins['pnl_usd'].mean() if len(wins) else 0.0
        avg_loss = losses['pnl_usd'].mean() if len(losses) else 0.0
        ev = wr * avg_win - (1 - wr) * (-avg_loss)
        days = max(1, (df['exit_time'].max() - df['entry_time'].min()).total_seconds() / 86400.0)
        return {
            "n_trades": int(len(df)),
            "pnl_total": float(pnl.sum()),
            "pnl_per_trade": float(pnl.mean()),
            "win_rate": float(wr),
            "avg_win": float(avg_win),
            "avg_loss": float(avg_loss),
            "trades_per_day": float(len(df) / days),
            "ev_per_trade": float(ev),
            "n_soft_stops": int(self.n_soft_stops),
        }


# ────────────────────────────────────────────────────────────────────────────
# BoS/CHoCH entry alignment helper (added 2026-09-16)
# ────────────────────────────────────────────────────────────────────────────

def _bos_choch_entry_alignment(
    p: TrendStrategyParams,
    layer: dict,
    fill_bar: int,
    structure_state,
) -> str:
    """Determine the trade's BoS/CHoCH alignment at entry.

    Used by the bar loop's soft-stop block to decide whether to
    suppress the inversion soft-stop. The semantic (per the user's
    2026-09-16 directive):

    * **aligned**  — the trade thesis (most recent BoS/CHoCH in
      memory) agrees with the trade direction. The soft-stop is
      suppressed; the aligned thesis is the dominant force.
    * **opposed**  — the trade thesis disagrees with the trade
      direction. The soft-stop fires as normal; the trade is
      fighting structure.
    * **unknown**  — no BoS/CHoCH events yet (no thesis). The
      soft-stop fires as normal (the conservative default).

    Anti-lookahead: only events with ``event_bar <= fill_bar`` are
    considered (the structure detector's break list is in the past
    at the entry bar).

    Returns:
        ``"aligned"`` / ``"opposed"`` / ``"unknown"``.
    """
    # Master switch off → no alignment rule applied. Return "unknown"
    # so the soft-stop logic treats the trade as "no special case".
    if not bool(getattr(p, "bos_choch_ignore_invert_when_aligned", True)):
        return "unknown"
    # Per-source switch.
    src = str(layer.get("triggered_by", "fvg")).lower()
    src_apply_map = {
        "fvg": bool(getattr(p, "bos_choch_ignore_invert_apply_fvg", True)),
        "ifvg": bool(getattr(p, "bos_choch_ignore_invert_apply_ifvg", True)),
        "orb": bool(getattr(p, "bos_choch_ignore_invert_apply_orb", True)),
        "wyckoff": bool(getattr(p, "bos_choch_ignore_invert_apply_wyckoff", True)),
        "sweep": bool(getattr(p, "bos_choch_ignore_invert_apply_sweep", True)),
    }
    if not src_apply_map.get(src, True):
        return "unknown"
    if structure_state is None:
        return "unknown"
    # Build the memory from the structure state's breaks list.
    # BosChochMemory imported at module top (was previously inside
    # this function — moved 2026-09-17 to avoid re-importing on
    # every call).
    max_n = max(1, int(getattr(p, "bos_choch_memory_n_events", 5)))
    mem = BosChochMemory(max_events=max_n)
    for be in structure_state.breaks:
        if be.bar <= fill_bar:  # anti-lookahead
            mem.push(be.bar, int(be.kind))
    direction = int(layer.get("direction", 0))
    if direction == 0:
        return "unknown"
    return mem.alignment_at(fill_bar, direction)


# ────────────────────────────────────────────────────────────────────────────
# Layer placement
# ────────────────────────────────────────────────────────────────────────────

def _fvg_layer_offsets(
    zone_low: float, zone_high: float,
    direction: int, trigger_price: float,
    num_layers: int, alpha: float = 1.0,
) -> List[float]:
    """Return the offset (USD from trigger) for each of N layers.

    Layers are spread EVENLY across the FVG zone's vertical extent
    (``zone_low → zone_high``), not the time axis. The convention:

    * **Long FVG** (direction=+1): the zone sits ABOVE the trigger
      (we're buying *into* the gap from below). Layer anchors go
      from ``zone_low`` (innermost) to ``zone_high`` (outermost).
      Offsets are positive (= we wait for price to rise TO the zone).
    * **Short FVG** (direction=-1): the zone sits BELOW the trigger
      (we're selling *into* the gap from above). Layer anchors go
      from ``zone_high`` (innermost) to ``zone_low`` (outermost).
      Offsets are negative (= we wait for price to fall TO the zone).

    The fill price is ``trigger_price + offset``. The bar loop opens
    the trade at the fill price and tracks SL/TP from there.
    """
    if num_layers <= 0:
        return [0.0]
    if direction > 0:
        # Long: layers go from zone_low (innermost) to zone_high (outermost)
        anchors = np.linspace(zone_low, zone_high, num_layers)
    else:
        # Short: layers go from zone_high (innermost) to zone_low (outermost)
        anchors = np.linspace(zone_high, zone_low, num_layers)
    return [float(a - trigger_price) for a in anchors]


# ────────────────────────────────────────────────────────────────────────────
# Dynamic SL/TP
# ────────────────────────────────────────────────────────────────────────────

def compute_layer_sl_tp(
    zone_low: float,
    zone_high: float,
    direction: int,
    trigger_price: float,
    layer_offset: float,
    layer_idx: int,
    num_layers: int,
    sl_usd: float,
    tp_usd: float,
    breadth: float,
    inverse_breadth: bool,
    *,
    sl_floor: float = 0.10,
    tp_floor: float = 0.20,
    layer_sl_shrink: float = 1.0,
    consecutive_structure_breaks: int = 0,
    atr: float = 0.0,
    atr_anchor: bool = False,
    sl_atr_mult: float = 0.25,
    tp_atr_mult: float = 0.55,
) -> tuple[float, float, int]:
    """Compute SL and TP for a single layer.

    The SL is anchored to the *zone edge* (zone_low for long,
    zone_high for short), so the position's risk is bounded by the
    zone's vertical extent. The TP is a function of SL × a
    structure-density multiplier (see TP_RULES in AGENTS.md).

    Parameters
    ----------
    zone_low, zone_high : float
        FVG zone bounds.
    direction : int
        +1 long, -1 short.
    trigger_price : float
        The trigger-bar close. Layers are placed at
        ``trigger + layer_offset``.
    layer_offset : float
        The offset (USD) of this layer from the trigger.
    layer_idx, num_layers : int
        0-indexed layer within the signal's N layers.
    sl_usd, tp_usd : float
        Fallback fixed SL/TP (used when breadth-based calc is
        degenerate, e.g. zero-width zone).
    breadth : float
        Zone width (zone_high - zone_low) in USD.
    inverse_breadth : bool
        If True, wider zones → tighter SL/TP multipliers.
        If False, wider zones → wider SL/TP.
    sl_floor, tp_floor : float
        Never go below these USD distances.
    layer_sl_shrink : float
        Per-layer SL shrink: layer N SL = base_sl * shrink^N.
    consecutive_structure_breaks : int
        Number of BoS/CHoCH/CHoCH+ events in the trade direction
        within the last ``ms_max_boost_age_bars`` of the trigger.
        Used by the TP rule table.
    atr : float
        Current ATR. Used by TP rules 2 and 3.
    """
    # Base SL: anchored to the zone's far edge.
    # Long: SL = layer_offset - (zone_low - trigger)  → negative offset
    #   from the fill price to the zone's bottom edge.
    # Short: SL = (zone_high - trigger) - layer_offset
    #   → positive offset from the fill price to the zone's top edge.
    if direction > 0:
        # The fill price is trigger + layer_offset (always ≤ zone_high).
        # SL distance = fill_price - zone_low (the zone's bottom edge).
        zone_anchor_sl = (trigger_price + layer_offset) - zone_low
    else:
        # Fill = trigger + layer_offset (always ≥ zone_low).
        # SL distance = zone_high - fill_price.
        zone_anchor_sl = zone_high - (trigger_price + layer_offset)

    # Apply inverse/direct breadth scaling. We treat a *bigger* zone
    # as a *stronger* displacement signal. With ``inverse_breadth``,
    # the SL is multiplied by ``1 / (1 + breadth * k)`` so wider
    # zones produce tighter SLs (capped at zone_anchor_sl). With
    # ``not inverse_breadth``, the SL is multiplied by
    # ``(1 + breadth * k)`` so wider zones produce wider SLs.
    # The constant k = 1.0 means a 1-USD zone doubles/halves the
    # default SL; k is per-config below.
    #
    # ATR-anchor mode (added 2026-09-17 v6): when ``atr_anchor=True``,
    # bypass the zone-edge-anchor SL entirely and pin the SL/TP to
    # recent ATR. SL = max(sl_floor, sl_atr_mult × ATR), scaled by
    # the per-layer shrink factor. TP = max(tp_floor, tp_atr_mult × ATR),
    # with the structure-density multiplier on top (same rule table).
    # This avoids the sub-second SL problem because the SL is now
    # bounded below by the recent noise scale (1s bar range ~$0.13
    # on XAUUSD, so even a modest sl_atr_mult=2.5 → SL ≈ $0.42).
    if atr_anchor and atr > 0:
        scaled_sl = max(sl_floor, sl_atr_mult * atr)
        scaled_tp = max(tp_floor, tp_atr_mult * atr)
        scaled_sl = scaled_sl * (layer_sl_shrink ** layer_idx)
        tp_rule = 0  # rule-0 = ATR-anchor
        return float(scaled_sl), float(scaled_tp), tp_rule

    breadth_k = 1.0
    if breadth > 0:
        if inverse_breadth:
            # Wider zone → tighter SL. floor at ``sl_floor``.
            breadth_mult = 1.0 / (1.0 + breadth * breadth_k)
        else:
            breadth_mult = 1.0 + breadth * breadth_k
        scaled_sl = zone_anchor_sl * breadth_mult
    else:
        scaled_sl = sl_usd

    # Per-layer SL shrink (deeper layers = tighter SL).
    scaled_sl = scaled_sl * (layer_sl_shrink ** layer_idx)
    # Apply floor (so a 0.5-cent zone doesn't produce a 0.1-cent stop).
    scaled_sl = max(scaled_sl, sl_floor)
    # NOTE: no upper cap. With ``inverse_breadth=False`` and a 4-USD
    # zone, the SL can legitimately be 5× the zone_anchor_sl. The
    # floor is the only bound on the bottom; the top is bounded by
    # the user-configured max via ``sl_usd`` / ``tp_usd`` (which the
    # function falls back to when ``breadth == 0``).

    # ── TP rule table (see TP_RULES in AGENTS.md) ─────────────────────
    # Rule 1: TP = sl × base_payoff
    # Rule 2: if 1+ structure break in trade direction, TP = sl × base_payoff × 1.3
    # Rule 3: if 2+ structure breaks, TP = max(sl × base_payoff × 1.6, N × ATR)
    # Rule 4: if 3+ structure breaks, TP = max(sl × base_payoff × 2.0, N × ATR)
    base_payoff = tp_usd / sl_usd if sl_usd > 0 else 2.0
    base_payoff = max(1.0, base_payoff)  # never < 1:1
    if consecutive_structure_breaks >= 3 and atr > 0:
        tp_rule = 4
        scaled_tp = max(scaled_sl * 2.0, 3.0 * atr)
    elif consecutive_structure_breaks >= 2 and atr > 0:
        tp_rule = 3
        scaled_tp = max(scaled_sl * 1.6, 2.0 * atr)
    elif consecutive_structure_breaks >= 1:
        tp_rule = 2
        scaled_tp = scaled_sl * 1.3 * base_payoff
    else:
        tp_rule = 1
        scaled_tp = scaled_sl * base_payoff
        scaled_tp = max(scaled_tp, tp_floor)

    return float(scaled_sl), float(scaled_tp), tp_rule


def _zone_id_set(cache: list | None, pending_layers: list) -> set[int]:
    """Lazy initializer for ``_existing_pending_zone_ids``.

    ``cache`` is a 1-element list used as a mutable container so
    callers can store the built set back without an explicit
    reassignment. Used by the bar loop (2026-09-17 hot-path
    optimization) to skip the ``pending_layers`` walk on bars
    where no signal fires (which is the common case — only ~50
    signal bars out of ~70k bars/day on 1s XAUUSD).
    """
    if cache and cache[0] is not None:
        return cache[0]
    new_set: set[int] = set()
    for pl in pending_layers:
        z = pl.get("fvg_zone")
        if z is not None:
            new_set.add(id(z))
    if cache is not None:
        cache[0] = new_set
    return new_set


# ────────────────────────────────────────────────────────────────────────────
# Main backtest
# ────────────────────────────────────────────────────────────────────────────

def run_ict_backtest(
    df: pd.DataFrame,
    p: TrendStrategyParams,
    *,
    strategy_label: str = "ict",
) -> IctBacktestResult:
    """Run the ICT-only backtest on a 1s OHLC dataframe.

    Required columns: ``time`` (tz-aware UTC), ``open``, ``high``,
    ``low``, ``close``. ``volume`` is optional.

    The bar-loop is single-pass over the dataframe:

    1. Pre-compute ATR and BoS/CHoCH structure state.
    2. Generate the FVG / iFVG / ORB / Wyckoff PendingSignal stream.
    3. Walk bars:
       a. Self-heal the signal iterator (so a layer submission on
          bar i doesn't cause a missed signal on bar i+1).
       b. If a signal's trigger_bar == i and no pending layers,
          split into N layers at the FVG zone anchors and submit.
       c. Walk pending layers: for each layer, check if bar i's
          range covers the layer's offset price → fill → open
          a trade (with per-layer SL/TP).
       d. For each open trade, check SL / TP / soft-stop / EOD.
       e. For each open trade with an attached FVG zone, check
          if the FVG has been inverted this bar → tighten SL.
    """
    t0 = time.time()
    n = len(df)
    if n < 2:
        return IctBacktestResult(strategy=strategy_label, params=asdict(p))

    # ── Build per-bar arrays (Rule 2: int64 ns for hot path) ──────────
    time_col = pd.to_datetime(df['time'], utc=True)
    unit = str(time_col.dtype).split('[')[1].split(',')[0].rstrip(']').strip() if '[' in str(time_col.dtype) else 'ns'
    if unit not in ('ns', 'us', 'ms', 's'):
        unit = 'ns'
    scale = {'ns': 1, 'us': 1_000, 'ms': 1_000_000, 's': 1_000_000_000}[unit]
    times_ns = time_col.astype('int64').to_numpy() * scale
    open_ = df['open'].to_numpy(dtype=np.float64)
    high = df['high'].to_numpy(dtype=np.float64)
    low = df['low'].to_numpy(dtype=np.float64)
    close = df['close'].to_numpy(dtype=np.float64)

    # ── Compute IctSeries: ATR + structure-state trend ────────────────
    atr_arr = compute_simple_atr(high, low, close, length=int(p.atr_len))
    structure = detect_market_structure(
        high, low, close,
        pivot_len=int(p.ms_pivot_len),
        liquidity_len=int(p.ms_liquidity_len),
        detect_order_blocks=bool(p.ms_draw_order_blocks),
        detect_liquidity=bool(p.ms_draw_liquidity_sweeps),
        resample_to_n_secs=int(getattr(p, 'ms_resample_secs', 0)),
    )
    ict_series = IctSeries(
        trend=structure.trend,
        atr=atr_arr,
    )
    # ── PIVOT A: precompute 24h ATR rolling percentile for sniper regime filter ─
    # We precompute the rolling percentile at each bar so the sniper queue walk
    # can do a O(1) lookup instead of recomputing. The window is 86400 bars
    # (24h at 1s cadence). At each bar i, percentile[i] = where atr_arr[i]
    # sits in the distribution of atr_arr[i-86399:i+1].
    _sniper_regime_pct = float(getattr(p, "sniper_regime_atr_pct", 0.0))
    _sniper_regime_enabled = _sniper_regime_pct > 0.0
    _atr_rolling_pct: np.ndarray | None = None
    if _sniper_regime_enabled:
        _atr_rolling_pct = np.zeros(n, dtype=np.float64)
        _window_24h = min(n, 86400)
        for i in range(n):
            w_start = max(0, i - _window_24h + 1)
            window = atr_arr[w_start:i + 1]
            if window.shape[0] < 2:
                _atr_rolling_pct[i] = 50.0
            else:
                sorted_win = np.sort(window)
                idx = int(np.searchsorted(sorted_win, atr_arr[i]))
                _atr_rolling_pct[i] = float(idx) / float(sorted_win.shape[0]) * 100.0
    # 2026-09-15: stash the full StructureState on the IctSeries so
    # ``generate_ict_pending_signals`` can reuse it instead of
    # recomputing ``detect_market_structure`` (~1s/call × 2 sources
    # = ~2s/day wasted per backtest).
    ict_series._structure_state = structure

    # ── Renko bricks for FVG invalidation (added 2026-09-05) ─────────
    # When ``renko_drive_invalidation=True``, compute a renko brick
    # series once and use it to drive the soft-stop on FVG-sourced
    # trades. The renko state gives a STRUCTURAL, time-decoupled
    # "the market has committed to the OTHER side" signal that's much
    # less noisy than 1s-close pierces. The renko computation is O(N)
    # so it adds no asymptotic cost.
    renko = None
    if bool(getattr(p, "renko_drive_invalidation", False)):
        from src.core.ict_signals import compute_renko_bars
        renko = compute_renko_bars(
            close,
            brick_size_usd=float(getattr(p, "renko_brick_size_usd", 0.30)),
        )

    # ── Populate the ATR scalar used by the ATR-relative FVG filter ──────
    # The retest semantic knob (``fvg_min_mitigation_pct``) and the
    # ATR-relative zone filter (``fvg_min_zone_atr_mult``) both need a
    # recent ATR scalar. ``fvg_min_zone_atr`` is the user-facing knob;
    # we set it to the **resampled** regime ATR (matching the FVG
    # detector's timeframe) so the filter adapts to volatility on the
    # same scale the detector sees. The 1s-bar ATR is way too small to
    # be useful as a zone-width filter (e.g. $0.05 ATR vs $0.30 min
    # zone would always pass); the resampled ATR matches what the
    # detector's resample_to_n_secs knob sees.
    if bool(getattr(p, "fvg_min_zone_atr_mult", 0.0)) > 0:
        rs = int(getattr(p, "fvg_resample_secs", 60))
        if rs > 1 and close.shape[0] >= rs:
            # Resample the 1s close to N-second bars and compute ATR.
            n_buckets = close.shape[0] // rs
            trimmed = close[: n_buckets * rs].reshape(n_buckets, rs)
            highs = high[: n_buckets * rs].reshape(n_buckets, rs).max(axis=1)
            lows = low[: n_buckets * rs].reshape(n_buckets, rs).min(axis=1)
            closes = trimmed[:, -1]
            atr_resampled = compute_simple_atr(highs, lows, closes, length=20)
            atr_now = float(atr_resampled[-1]) if atr_resampled.shape[0] > 0 else 0.0
        else:
            atr_now = float(atr_arr[-1]) if atr_arr.shape[0] > 0 else 0.0
        p.fvg_min_zone_atr = atr_now

    # ── Generate signal stream (FVG + iFVG + any additional_sources) ─
    srcs = [p.signal_source] + [s for s in (p.additional_sources or []) if s != p.signal_source]
    signals: List[PendingSignal] = []
    for src in srcs:
        if src in ("gmma",):
            continue
        signals.extend(generate_ict_pending_signals(
            close, open_, high, low, times_ns, ict_series, p, src=src,
        ))
    # Sort by (trigger_bar, direction) so the bar-loop sees them in
    # deterministic order. Stable sort preserves source order.
    signals.sort(key=lambda s: (s.trigger_bar, s.direction))
    sig_iter = iter(signals)
    sig = next(sig_iter, None)

    # Capture structure-driven invalidation count from the detector
    # (added 2026-09-05): the IctSeries carries the count after
    # ``generate_ict_pending_signals`` runs. We move it onto the
    # result below when constructing the IctBacktestResult.

    # ── Bar loop ──────────────────────────────────────────────────────
    pending_layers: List[dict] = []
    # Inverse-trade queue (added 2026-09-15): trades sourced from
    # ``fvg_inv_trade_enabled`` paths are appended here and submitted
    # at ``layer["submit_bar"]``. They use a different fill model
    # (next-bar open, anti-look-ahead) than the FVG ladder limit
    # orders in ``pending_layers``, so they live in their own queue.
    pending_inv_layers: List[dict] = []
    # Sniper-in queue (added 2026-09-17, v6+ Innovation #1): when
    # ``entry_mode="sniper"``, FVG/iFVG signals are deferred here.
    # Each sniper waits for its anchor zone to be inverted; on
    # inversion it queues a real iFVG trade into pending_inv_layers
    # (the same path INV_TRADE uses). On supersession / played-out /
    # structure-invalidation / age expiry, the sniper is dropped.
    pending_sniper_layers: List[dict] = []
    open_trades: List[dict] = []   # {trade, soft_sl_override, soft_sl_active}
    closed_trades: List[Trade] = []
    n_fills = 0
    n_consumed = 0
    n_soft_stops = 0
    n_inversions = 0
    # NOTE (2026-09-17): n_signals_rank_a/b/c/skipped counters
    # removed (the rank-treatment system is gone).
    n_sweep_signals = 0
    n_sweep_fills = 0
    n_renko_invalidations = 0
    # Sniper-in mode diagnostics (added 2026-09-17, v6+ Innovation #1).
    # When entry_mode="sniper": how many FVG/iFVG signals were DEFERRED
    # (n_sniper_submitted), how many triggered via zone inversion
    # (n_sniper_triggered), how many were cancelled by supersede /
    # played_out / structure-invalidation (n_sniper_cancelled), how
    # many expired by sniper_max_age_secs (n_sniper_expired).
    n_sniper_submitted = 0
    n_sniper_triggered = 0
    n_sniper_cancelled = 0
    n_sniper_expired = 0
    # BUG FIX (2026-09-17): signal_id was set to bar index ``i`` which
    # collides when multiple signals fire on the same bar. We now use a
    # simple incrementing counter so each signal gets a unique ID.
    signal_id_counter = 0
    # BUG FIX (2026-09-17): n_inversions was incremented once per trade
    # per bar (e.g. 3 layers from the same zone each increment n_inversions).
    # The inversion is one zone event; we track it once per (zone, bar) pair.
    # Per-bar zone inversion sets are declared inside the bar loop (fresh
    # per bar, shared across all trades that bar). 
    # BoS/CHoCH alignment-based soft-stop suppression diagnostics
    # (added 2026-09-16): how many inversions were bypassed because
    # the trade was aligned with the structure thesis at entry.
    n_alignment_skipped_inversions = 0
    # Per-entry alignment counters. The bar loop increments these
    # in step 1 when a trade is opened (the entry_alignment field
    # already records the alignment in the open_trade dict; these
    # tallies make them queryable from the result).
    n_entry_alignment_aligned = 0
    n_entry_alignment_opposed = 0
    n_entry_alignment_unknown = 0
    # Per-zone inverse-trade submission cap tracker (added 2026-09-15).
    # Keyed by ``id(zone)`` because the soft-stop block in step 3 sees
    # the same zone object that the inverse-trade logic in the same
    # block submits a new layer from. Capped by
    # ``fvg_inv_trade_max_per_zone`` (default 1).
    inv_zone_count: dict[int, int] = {}
    equity = np.zeros(n, dtype=np.float64)
    cum_pnl = 0.0

    # Hot-path-local cache of common params.getattr() reads (added
    # 2026-09-17). The bar loop reads ``p.X`` dozens of times via
    # ``getattr(p, "X", default)``; caching once removes ~250k
    # ``getattr`` calls per 30-day run.
    _ms_boost_cutoff = int(getattr(p, "ms_max_boost_age_bars", 60))
    _inv_sl_mult = float(getattr(p, 'fvg_inv_trade_sl_zone_mult', 1.0))
    _inv_tp_mult = float(getattr(p, 'fvg_inv_trade_tp_zone_mult', 1.8))
    _inv_min_zone = float(getattr(p, 'fvg_inv_trade_min_zone_usd', 0.30))
    _inv_max_per = max(1, int(getattr(p, 'fvg_inv_trade_max_per_zone', 1)))
    _inv_trade_enabled = bool(getattr(p, 'fvg_inv_trade_enabled', False))
    # ── Grace period in seconds (fixed 2026-09-17) ────────────────────
    # We now measure in SECONDS, not bars. The docstring says
    # "suppress soft-stop for first N seconds", so the implementation
    # must compare wall-clock delta. On 1s data bars==seconds so this
    # is a no-op change; on higher-timeframe data this now works correctly.
    _grace_secs = float(getattr(p, 'invalidation_grace_secs', 0))
    _bos_ignore = bool(getattr(p, "bos_choch_ignore_invert_when_aligned", True))
    _entry_mode = str(getattr(p, "entry_mode", "immediate"))
    _ms_boost = float(getattr(p, "ms_boost_conviction", 1.0))
    _lots = float(p.lots)
    _atr_anchor = bool(getattr(p, "atr_anchor_sl_tp", False))
    _sl_atr_mult = float(getattr(p, "sl_atr_mult", 0.25))
    _tp_atr_mult = float(getattr(p, "tp_atr_mult", 0.55))
    _tiebreak = str(getattr(p, "sl_tp_tiebreak", "sl_first"))
    # ── PIVOT E: trailing SL params ────────────────────────────────────
    _trailing_enabled = bool(getattr(p, "trailing_sl_enabled", False))
    _trailing_atr_mult = float(getattr(p, "trailing_atr_mult", 0.5))
    _trailing_activation = float(getattr(p, "trailing_activation_atr_mult", 1.0))
    # ── PIVOT G: SL decay schedule ────────────────────────────────────
    _sl_decay: list = list(getattr(p, "sl_decay_schedule", []) or [])
    # ── Per-bar signal ID counter (fixed 2026-09-17) ────────────────────
    # signal_id must be globally unique across all signals in a backtest.
    # Using bar index alone causes collisions when multiple signals fire on
    # the same bar. Increment a counter so each submission gets a unique ID.
    # Per-bar ATR scalar (used by the TP rule table). 2026-09-17:
    # ``atr_arr[i]`` is the common case; use direct numpy indexing
    # to skip the Python-level ``float()`` and bounds-check on hot path.
    atr_arr_local = atr_arr
    n_atr = atr_arr.shape[0]

    def _atr(i: int) -> float:
        if 0 <= i < n_atr:
            return atr_arr_local[i]
        return 0.0

    # Pre-compute per-bar cumulative bull / bear break counts (added
    # 2026-09-17 for hot-path performance). The old implementation
    # walked ``structure.breaks`` for every signal (O(breaks × signals)
    # = ~50 × 1700/day = 85k iterations/day). The cumulative approach
    # is O(1) per call after a one-time O(N) precompute:
    #   n_bull_breaks_up_to_bar[b] = # of bull breaks (kind in {1,2})
    #                                  with break_bar <= b
    # To get "breaks within last K bars of bar X" we use:
    #   count = n_bull_breaks_up_to_bar[X]
    #           - n_bull_breaks_up_to_bar[X - K - 1]
    # The structure.breaks list is assumed sorted by bar (the detector
    # emits events in chronological order — verified by inspect below).
    n_bull_cum = np.zeros(n + 1, dtype=np.int64)
    n_bear_cum = np.zeros(n + 1, dtype=np.int64)
    for ev_b in structure.breaks:
        b = int(ev_b.bar)
        if 0 <= b <= n:
            if int(ev_b.kind) in (1, 2):  # bull
                n_bull_cum[b + 1:] += 1
            elif int(ev_b.kind) in (-1, -2):  # bear
                n_bear_cum[b + 1:] += 1

    def _consecutive_structure_breaks(bar: int, direction: int) -> int:
        """Count BoS/CHoCH/CHoCH+ events in ``direction`` within
        ``ms_max_boost_age_bars`` of ``bar`` (used by the TP rule
        table). O(1) per call (2026-09-17 hot-path optimization).
        """
        if bar < 0 or bar >= n:
            return 0
        # Bar 0 of cumulative arrays = # of breaks up to & including bar -1.
        cum = n_bull_cum if direction > 0 else n_bear_cum
        up_to = cum[bar + 1]                          # breaks with bar <= ``bar``
        cutoff_bar = max(0, bar - _ms_boost_cutoff - 1)
        below_cutoff = cum[cutoff_bar]                # breaks with bar <= bar-cutoff-1
        return int(up_to - below_cutoff)

    # 2026-09-17 hot-path: bind array references to local names so
    # the bar loop skips attribute lookup on the module-level
    # names (``open_``, ``high``, etc.) on every bar. We still
    # ``float()`` the values because numpy scalars add overhead in
    # tight Python arithmetic (faster than ``np.float64`` ops).
    open_arr = open_
    high_arr = high
    low_arr = low
    close_arr = close
    time_arr = times_ns
    for i in range(n):
        b_open = float(open_arr[i])
        b_high = float(high_arr[i])
        b_low = float(low_arr[i])
        b_close = float(close_arr[i])
        b_time = int(time_arr[i])

        # Per-bar zone inversion set (FIX 2026-09-17 BUG #5).
        # Track which zones have already fired their soft-stop THIS bar
        # so we increment n_inversions once per zone, not once per trade.
        # Multiple layers on the same inverted zone would otherwise each
        # increment n_inversions, inflating the counter.
        _zones_inverted_this_bar: set[int] = set()

        # ── 0. Iterator self-heal ──────────────────────────────────────
        while sig is not None and sig.trigger_bar < i:
            sig = next(sig_iter, None)

        # ── 1. Submit layers for a signal whose trigger bar is i ──────
        # 2026-09-15 v2 fix: the legacy "not pending_layers" gate
        # blocked ALL new signals while ANY layer was pending (this
        # throttled trades to ~30/day max because layers had a 30-min
        # lifetime). The new gate is per-zone — a signal whose zone
        # is already in flight is dropped, but signals for other
        # zones submit in parallel. This unlocks 100s of parallel
        # limit orders across independent FVG zones.
        #
        # 2026-09-17 hot-path: ``_existing_pending_zone_ids`` is
        # now a 1-element list used as a mutable cache. The helper
        # ``_zone_id_set`` populates it lazily, so the ~70k-bar/day
        # walk over ``pending_layers`` only happens on signal bars.
        _existing_pending_zone_ids: list = [None]
        # ── 1a. Sniper-in intercept (added 2026-09-17, v6+ Innovation #1) ──
        # When ``p.entry_mode == "sniper"`` and the current signal is
        # an FVG/iFVG, do NOT enter a normal ladder. Instead, push a
        # sniper into ``pending_sniper_layers`` to wait for the anchor
        # zone to be inverted. On inversion the sniper queues a real
        # iFVG trade into ``pending_inv_layers`` (the INV_TRADE path)
        # so the bar loop fills it at next-bar open. Non-FVG sources
        # (ORB / Wyckoff / sweep) and entry_mode="immediate" are
        # unaffected. We intercept HERE (before the sweep/elif paths)
        # because both existing paths consume the signal via
        # ``sig = next(sig_iter, None)`` — there is no second chance.
        if (
            sig is not None
            and sig.trigger_bar == i
            and _entry_mode == "sniper"
            and str(getattr(sig, "triggered_by", "")) in ("fvg", "ifvg")
        ):
            sig_zone = getattr(sig, "fvg_zone", None)
            if sig_zone is not None:
                n_consumed += 1
                n_sniper_submitted += 1
                _sig_id = signal_id_counter
                signal_id_counter += 1
                pending_sniper_layers.append({
                    "submit_bar": i,
                    "submit_time_ns": b_time,
                    "signal_id": _sig_id,
                    "direction": int(sig.direction),
                    "fvg_zone": sig_zone,
                    "triggered_by": str(getattr(sig, "triggered_by", "")),
                    "trigger_price": b_close,
                })
                sig = next(sig_iter, None)
        if (
            sig is not None
            and sig.trigger_bar == i
            and not pending_layers  # sweeps are single-shot; keep the legacy gate
            and str(getattr(sig, "triggered_by", "")) == "sweep"
            and getattr(sig, "fill_price", None) is not None
        ):
            n_consumed += 1
            n_sweep_signals += 1
            zone = getattr(sig, "fvg_zone", None)
            zl = float(zone.zone_low) if zone is not None else b_close - 0.25
            zh = float(zone.zone_high) if zone is not None else b_close + 0.25
            # Conviction TP boost (same as regular FVG).
            cv = float(getattr(sig, "conviction", 1.0))
            ms_boost = float(getattr(p, "ms_boost_conviction", 1.0))
            scaled_sl = float(getattr(sig, "stop_usd", p.sl_usd))
            scaled_tp = float(getattr(sig, "target_usd", p.tp_usd))
            if cv > 1.0 and ms_boost > 1.0:
                scaled_tp = scaled_tp * (
                    1.0 + (ms_boost - 1.0) * (cv - 1.0) / 0.5
                )
            # NOTE (2026-09-17): rolling-tier SL/TP scaling (carry
            # from parent zone). Causal: uses only past zones.
            rank_tier_s = str(getattr(sig, "rank_tier", ""))
            tier_sl_scale = 1.0
            tier_tp_scale = 1.0
            if rank_tier_s == "A":
                tier_sl_scale = float(getattr(p, "fvg_rolling_a_sl_scale", 1.0))
                tier_tp_scale = float(getattr(p, "fvg_rolling_a_tp_scale", 1.0))
            elif rank_tier_s == "B":
                tier_sl_scale = float(getattr(p, "fvg_rolling_b_sl_scale", 1.0))
                tier_tp_scale = float(getattr(p, "fvg_rolling_b_tp_scale", 1.0))
            elif rank_tier_s == "C":
                tier_sl_scale = float(getattr(p, "fvg_rolling_c_sl_scale", 0.7))
                tier_tp_scale = float(getattr(p, "fvg_rolling_c_tp_scale", 0.5))
            scaled_sl = scaled_sl * tier_sl_scale
            scaled_tp = scaled_tp * tier_tp_scale
            _sweep_sig_id = signal_id_counter
            signal_id_counter += 1
            pending_layers.append({
                "layer_idx": 0,
                "offset_usd": float(sig.fill_price) - b_close,
                "fill_price": float(sig.fill_price),
                "submit_bar": i,
                "submit_time_ns": b_time,
                "lots": float(p.lots),
                "direction": sig.direction,
                "stop_usd": scaled_sl,
                "target_usd": scaled_tp,
                "tp_rule": 1,
                "triggered_by": "sweep",
                "trigger_price": b_close,
                "signal_id": _sweep_sig_id,
                "fvg_zone": zone,
                "is_ifvg": bool(getattr(sig, "is_ifvg", False)),
                "anchor_zl": zl,
                "anchor_zh": zh,
                "rank_tier": str(getattr(sig, "rank_tier", "")),
                "rank_percentile": float(getattr(sig, "rank_percentile", 0.0)),
            })
            sig = next(sig_iter, None)

        elif (
            sig is not None
            and sig.trigger_bar == i
            and (
                not pending_layers
                # 2026-09-15 v2: parallel signal submission across
                # different zones. Signals for the SAME zone+direction
                # are still dropped if a layer is already pending.
                # 2026-09-17 hot-path: lazily build the zone-id set
                # only when needed (saves ~70k dict comprehensions
                # per day on 1s data when no signal fires).
                or id(getattr(sig, "fvg_zone", None))
                not in _zone_id_set(_existing_pending_zone_ids, pending_layers)
            )
        ):
            n_consumed += 1
            zone = getattr(sig, "fvg_zone", None)
            # Spread layers evenly across the FVG zone (or a 0.5-USD
            # fallback for non-FVG sources like ORB / Wyckoff).
            if zone is not None and hasattr(zone, "zone_low") and hasattr(zone, "zone_high"):
                zl = float(zone.zone_low)
                zh = float(zone.zone_high)
                breadth = abs(zh - zl)
            else:
                zl = b_close - 0.25
                zh = b_close + 0.25
                breadth = abs(zh - zl)
            num_layers = max(1, int(p.num_layers))
            # ── Rolling FVG percentile tier (added 2026-09-17) ─────────
            # Causal rank of this zone's anchor vs the past N same-direction
            # zones. Drives SL/TP scales and optional outermost-layer skip.
            rank_tier = str(getattr(sig, "rank_tier", ""))
            rank_pct = float(getattr(sig, "rank_percentile", 0.0))
            tier_sl_scale = 1.0
            tier_tp_scale = 1.0
            n_skip_outer = 0
            if rank_tier == "A":
                tier_sl_scale = float(getattr(p, "fvg_rolling_a_sl_scale", 1.0))
                tier_tp_scale = float(getattr(p, "fvg_rolling_a_tp_scale", 1.0))
            elif rank_tier == "B":
                tier_sl_scale = float(getattr(p, "fvg_rolling_b_sl_scale", 1.0))
                tier_tp_scale = float(getattr(p, "fvg_rolling_b_tp_scale", 1.0))
            elif rank_tier == "C":
                tier_sl_scale = float(getattr(p, "fvg_rolling_c_sl_scale", 0.7))
                tier_tp_scale = float(getattr(p, "fvg_rolling_c_tp_scale", 0.5))
                skip_pct = float(getattr(p, "fvg_rolling_c_layer_skip_pct", 0.0))
                n_skip_outer = int(round(skip_pct * num_layers))
            # Hard skip threshold: drop trade if rolling percentile is
            # beyond the cutoff (e.g. drop the most-extreme C tier).
            skip_above = float(getattr(p, "fvg_rolling_skip_pct_above", 1.0))
            if rank_tier and rank_pct > skip_above:
                sig = next(sig_iter, None)
                n_consumed += 1
                continue
            offsets = _fvg_layer_offsets(
                zl, zh, sig.direction, b_close,
                num_layers, alpha=float(p.alpha),
            )
            # Apply C-tier outermost-layer skip: drop the LAST N layers
            # (deepest offsets from the trigger) so the trade only fills
            # closest to the trigger.
            if n_skip_outer > 0 and len(offsets) > n_skip_outer:
                offsets = offsets[:-n_skip_outer]
                if len(offsets) == 0:
                    # All layers skipped — drop the signal.
                    sig = next(sig_iter, None)
                    n_consumed += 1
                    continue
                # Recompute num_layers so per-layer SL scaling matches.
                num_layers = len(offsets)
            # Resolve the conviction into a TP boost.
            cv = float(getattr(sig, "conviction", 1.0))
            ms_boost = _ms_boost
            atr_v = _atr(i)
            n_struct_breaks = _consecutive_structure_breaks(i, sig.direction)
            lots_per_layer = _lots / num_layers
            # BUG FIX (2026-09-17): pre-compute unique signal_id so we can use it
            # inside the dict literal (can't increment inside {}).
            _layer_sig_id = signal_id_counter
            signal_id_counter += 1
            for li, off in enumerate(offsets):
                scaled_sl, scaled_tp, tp_rule = compute_layer_sl_tp(
                    zl, zh, sig.direction, b_close, off, li, num_layers,
                    float(p.sl_usd), float(p.tp_usd), breadth,
                    bool(p.inverse_breadth),
                    sl_floor=float(p.dynamic_sl_floor_usd),
                    tp_floor=float(p.dynamic_tp_floor_usd),
                    layer_sl_shrink=float(p.layer_sl_shrink_factor),
                    consecutive_structure_breaks=n_struct_breaks,
                    atr=atr_v,
                    atr_anchor=_atr_anchor,
                    sl_atr_mult=_sl_atr_mult,
                    tp_atr_mult=_tp_atr_mult,
                )
                # Apply tier-driven SL/TP scaling on top of breadth scaling.
                scaled_sl = scaled_sl * tier_sl_scale
                scaled_tp = scaled_tp * tier_tp_scale
                if cv > 1.0 and ms_boost > 1.0:
                    scaled_tp = scaled_tp * (1.0 + (ms_boost - 1.0) * (cv - 1.0) / 0.5)
                pending_layers.append({
                    "layer_idx": li,
                    "offset_usd": off,
                    "fill_price": b_close + off,  # limit price
                    "submit_bar": i,
                    "submit_time_ns": b_time,
                    "lots": lots_per_layer,
                    "direction": sig.direction,
                    "stop_usd": scaled_sl,
                    "target_usd": scaled_tp,
                    "tp_rule": tp_rule,
                    "triggered_by": sig.triggered_by,
                    "trigger_price": b_close,
                    "signal_id": _layer_sig_id,
                    "fvg_zone": zone,
                    "is_ifvg": bool(getattr(sig, "is_ifvg", False)),
                    "anchor_zl": zl,
                    "anchor_zh": zh,
                    "rank_tier": rank_tier,
                    "rank_percentile": rank_pct,
                })
            sig = next(sig_iter, None)

        # ── 1c. Sniper queue walk (added 2026-09-17, v6+ Innovation #1) ─────
        # Walk pending_sniper_layers: each sniper checks for inversion
        # of its anchor zone. On inversion, queue an iFVG trade into
        # pending_inv_layers (the same path INV_TRADE uses, with SL/TP
        # set by fvg_inv_trade_sl_zone_mult / fvg_inv_trade_tp_zone_mult).
        # On supersede / played_out / structure-invalidation / age /
        # per-zone cap, the sniper is dropped. The "c" suffix (vs
        # "1b" the interceptor) keeps the comment numbering monotonic;
        # ordering: 1a intercepts signal → 1b is the existing ladder
        # path (sweep / non-sweep) → 1c walks the sniper queue.
        if pending_sniper_layers:
            still_sniper: List[dict] = []
            sniper_max_age = int(getattr(p, "sniper_max_age_secs", 1800))
            sniper_min_zone = float(getattr(p, "fvg_inv_trade_min_zone_usd", 0.30))
            sniper_sl_mult = float(getattr(p, "fvg_inv_trade_sl_zone_mult", 1.0))
            sniper_tp_mult = float(getattr(p, "fvg_inv_trade_tp_zone_mult", 1.8))
            # ATR-scaled TP option (added 2026-09-17, v7+): when
            # ``fvg_inv_trade_tp_atr_mult > 0``, TP is computed as
            # ``ATR_at_entry_bar × atr_mult`` instead of
            # ``zone_w × zone_mult``. The two knobs are mutually
            # exclusive — ATR mult wins when > 0.
            sniper_tp_atr_mult = float(getattr(p, "fvg_inv_trade_tp_atr_mult", 0.0))
            sniper_use_atr_tp = sniper_tp_atr_mult > 0.0
            sniper_max_per = max(1, int(getattr(p, "fvg_inv_trade_max_per_zone", 1)))
            sniper_zone_count: dict[int, int] = {}
            for sp in pending_sniper_layers:
                zone = sp.get("fvg_zone")
                if zone is None:
                    continue
                # Check cancellation conditions (drop stale snipers
                # before checking inversion — supersession/played_out
                # may have flipped the zone dead without inversion).
                dropped = False
                if getattr(zone, "superseded_bar", -1) >= 0:
                    dropped = True
                    n_sniper_cancelled += 1
                elif getattr(zone, "played_out_bar", -1) >= 0:
                    dropped = True
                    n_sniper_cancelled += 1
                elif (
                    getattr(zone, "expired_bar", -1) >= 0
                    and not getattr(zone, "inverted", False)
                ):
                    dropped = True
                    n_sniper_cancelled += 1
                if not dropped and sniper_max_age > 0:
                    age_secs = (b_time - int(sp["submit_time_ns"])) / 1e9
                    if age_secs > sniper_max_age:
                        dropped = True
                        n_sniper_expired += 1
                # PIVOT A (2026-09-17): regime filter — only fire sniper in trending
                # (high-vol) sessions. When ATR is in the bottom percentile of the
                # last 24h distribution, the market is in a range/chop session — inversions
                # are SL-hunt noise. Skip firing this bar but keep the sniper pending
                # so it re-checks on the next bar when regime may have changed.
                if not dropped and _sniper_regime_enabled:
                    regime_pct = float(_atr_rolling_pct[i]) if _atr_rolling_pct is not None else 50.0
                    if regime_pct < _sniper_regime_pct:
                        still_sniper.append(sp)  # keep pending — re-check next bar
                        continue
                if dropped:
                    continue
                # Fire on inversion. The zone.inverted flag flips to
                # True on the inversion bar (causal — known at this
                # bar). Queue the resulting iFVG trade for next bar's
                # open via the existing pending_inv_layers path; it
                # carries entry_triggered_by='sniper' so it is
                # distinguishable from INV_TRADE entries.
                if getattr(zone, "inverted", False):
                    zone_w = float(zone.zone_high - zone.zone_low)
                    if zone_w < sniper_min_zone:
                        n_sniper_cancelled += 1
                        continue
                    zid = id(zone)
                    # Combine the local-sniper cap with the
                    # INV_TRADE step-3 cap (same zone, same trade shape).
                    n_so_far = sniper_zone_count.get(zid, 0) + inv_zone_count.get(zid, 0)
                    if n_so_far >= sniper_max_per:
                        n_sniper_cancelled += 1
                        continue
                    sniper_zone_count[zid] = n_so_far + 1
                    # The entry direction is opposite the signal direction.
                    # The retest scanner already flips for inverted zones
                    # (d = -z.direction if z.inverted), so an ifvg signal
                    # already carries the "trade the inversion" direction.
                    # Flipping once here gives: fvg signal -> SHORT (correct
                    # reversal), ifvg signal -> LONG (correct continuation).
                    # This is the patient sniper: wait for the inversion to
                    # resolve, then enter in the SAME direction as the
                    # original FVG. The hypothesis: the inversion was a
                    # liquidity sweep, the original thesis survives, enter
                    # on the retest of the now-inverted zone.
                    inv_dir = -int(sp["direction"])
                    # TP choice: ATR-scaled when ``sniper_tp_atr_mult > 0``,
                    # otherwise zone-width-scaled. Both are known at
                    # this bar (ATR via ``atr_arr[i]``, zone_w via the
                    # zone object) — causal, no look-ahead.
                    if sniper_use_atr_tp:
                        atr_v = float(atr_arr[i]) if 0 <= i < atr_arr.shape[0] else 0.0
                        target_usd_v = atr_v * sniper_tp_atr_mult
                    else:
                        target_usd_v = zone_w * sniper_tp_mult
                    pending_inv_layers.append({
                        "submit_bar": i + 1,
                        "submit_time_ns": int(times_ns[i + 1]) if i + 1 < n else b_time,
                        "direction": inv_dir,
                        "stop_usd": zone_w * sniper_sl_mult,
                        "target_usd": target_usd_v,
                        "lots": float(p.lots),
                        "fvg_zone": zone,
                        "is_ifvg": True,
                        "tp_rule": 1,
                        "rank_tier": "",
                        "rank_percentile": 0.0,
                        "entry_triggered_by": "sniper",
                    })
                    ict_series.n_inv_trades_submitted += 1
                    n_sniper_triggered += 1
                    continue
                # Default: still waiting for inversion.
                still_sniper.append(sp)
            pending_sniper_layers = still_sniper

        # ── 2. Fill pending layers if the bar's range covers the price ─
        if pending_layers:
            still_pending = []
            for layer in pending_layers:
                if i <= layer["submit_bar"]:
                    still_pending.append(layer)
                    continue
                # Layer lifetime check (drop if expired)
                if (p.layer_lifetime_secs > 0 and
                        (b_time - layer["submit_time_ns"]) / 1e9 > p.layer_lifetime_secs):
                    continue
                target_price = layer["fill_price"]
                filled = False
                # BUG FIX (2026-09-17): correct limit-order fill model.
                # A LIMIT BUY at P only fills at P (or better = lower; but
                # without sub-bar data the safe assumption is P, NOT
                # b_open which assumes free price improvement that limit
                # orders cannot receive). The legacy code used
                # ``b_open if b_open <= target_price else target_price``
                # which gave the strategy a fictitious 1-bar of price
                # improvement on every long fill where the bar opened
                # below the limit. Same mirror logic for shorts.
                # SWEEPS are stop-orders (not limits), so they DO get
                # filled at the worse-of-target-or-open when the bar
                # gaps past the stop.
                is_sweep = (layer["triggered_by"] == "sweep")
                if layer["direction"] > 0:
                    if b_low <= target_price:
                        if is_sweep:
                            # Buy-stop: fills at max(target, open) —
                            # gap-throughs get the worse price.
                            ep = max(target_price, b_open)
                        else:
                            # Limit-buy: fill at the limit, never better.
                            ep = target_price
                        trade = _open_trade(layer, ep, i, b_time, p)
                        entry_align = _bos_choch_entry_alignment(
                            p, layer, i, structure,
                        )
                        if entry_align == "aligned":
                            n_entry_alignment_aligned += 1
                        elif entry_align == "opposed":
                            n_entry_alignment_opposed += 1
                        else:
                            n_entry_alignment_unknown += 1
                        open_trades.append({
                            "trade": trade,
                            "soft_sl": None,
                            "soft_sl_active": False,
                            "entry_alignment": entry_align,
                            # PIVOT E: trailing SL — track best price seen
                            "best_price": ep,
                        })
                        n_fills += 1
                        if is_sweep:
                            n_sweep_fills += 1
                        filled = True
                else:
                    if b_high >= target_price:
                        if is_sweep:
                            # Sell-stop: fills at min(target, open) —
                            # gap-throughs get the worse price.
                            ep = min(target_price, b_open)
                        else:
                            # Limit-sell: fill at the limit, never better.
                            ep = target_price
                        trade = _open_trade(layer, ep, i, b_time, p)
                        entry_align = _bos_choch_entry_alignment(
                            p, layer, i, structure,
                        )
                        if entry_align == "aligned":
                            n_entry_alignment_aligned += 1
                        elif entry_align == "opposed":
                            n_entry_alignment_opposed += 1
                        else:
                            n_entry_alignment_unknown += 1
                        open_trades.append({
                            "trade": trade,
                            "soft_sl": None,
                            "soft_sl_active": False,
                            "entry_alignment": entry_align,
                            # PIVOT E: trailing SL — track best price seen
                            "best_price": ep,
                        })
                        n_fills += 1
                        if is_sweep:
                            n_sweep_fills += 1
                        filled = True
                if not filled:
                    still_pending.append(layer)
            pending_layers = still_pending

        # ── 2b. Inverse-trade fills (added 2026-09-15) ────────────────
        # Trades queued by the soft-stop block via
        # ``fvg_inv_trade_enabled``: enter at THIS bar's open (the
        # bar AFTER the inversion). Anti-look-ahead: the open is
        # known at entry time but not before. There's no fill
        # condition — the trade always opens at the bar's open
        # because the inversion itself is the entry signal.
        #
        # FIX (2026-09-17): BUG #7 was forcing ``entry_alignment="opposed"``
        # for ALL inverse/sniper trades, denying them the soft-stop suppression
        # benefit. We now compute the ACTUAL alignment: the inverse trade's
        # direction IS the direction we pass to the alignment function
        # (``layer["direction"]`` is already the inverse direction — short for
        # a bull FVG inversion, long for a bear FVG inversion). If the most
        # recent structure event's thesis matches the inverse direction, the
        # trade is "aligned" with structure and gets suppressed soft-stop
        # treatment. This is structurally correct: a sniper short entered after
        # a bearish CHoCH (thesis = bear) IS aligned and should ride.
        if pending_inv_layers:
            still_pending_inv: List[dict] = []
            for layer in pending_inv_layers:
                if layer["submit_bar"] != i:
                    still_pending_inv.append(layer)
                    continue
                ep = float(b_open)
                trade = _open_trade(layer, ep, i, b_time, p)
                # Compute actual structure alignment for this inverse trade.
                # ``layer["direction"]`` is already the flipped (inverse) direction.
                inv_align = _bos_choch_entry_alignment(
                    p, layer, i, structure,
                )
                if inv_align == "aligned":
                    n_entry_alignment_aligned += 1
                elif inv_align == "opposed":
                    n_entry_alignment_opposed += 1
                else:
                    n_entry_alignment_unknown += 1
                open_trades.append({
                    "trade": trade,
                    "soft_sl": None,
                    "soft_sl_active": False,
                    "entry_alignment": inv_align,
                    # PIVOT E: trailing SL — track best price seen
                    "best_price": ep,
                })
                n_fills += 1
                ict_series.n_inv_trades_filled += 1
            pending_inv_layers = still_pending_inv

        # ── 3. FVG inversion soft-stop: tighten SL on open positions ──
        # For each open trade with an attached FVG zone, check whether
        # the zone was inverted *this* bar. If so, set a soft-stop at
        # the zone's opposing edge (just outside, by the buffer).
        #
        # Grace period (added 2026-08-20): the soft-stop is suppressed
        # for the first ``invalidation_grace_secs`` of the trade's life
        # so a soft-stop can't fire on the SAME bar as the entry (the
        # common noise pattern — entry fires on a retest, FVG inverts
        # on the very next bar, trade dies in 1 second). After the
        # grace period, normal soft-stop behaviour resumes.
        #
        # We measure grace against the trade's ``entry_bar`` (in 1s
        # space), not the trade's open-time, because the trade is held
        # open across many bars and the grace period applies to the
        # entire trade lifecycle.
        # Per-zone cap on inverse-trade submissions (added 2026-09-15).
        # The ``inv_zone_count`` dict is initialized once outside the
        # bar loop and tracks how many inverse-trade layers have been
        # submitted per FVG zone (capped by
        # ``fvg_inv_trade_max_per_zone``). Keyed by ``id(zone)``
        # because the soft-stop block and inverse-trade logic share
        # the same zone object.
        if p.invalidation_sl_usd > 0 and open_trades:
            # Hot-path: cached local vars (see ``_ms_boost_cutoff``
            # block above) — 2026-09-17.
            # Grace period now in seconds (fixed 2026-09-17).
            grace_secs = _grace_secs
            # Inverse-trade edge (added 2026-09-15): when an FVG zone
            # flips inverted this bar AND an open position sourced
            # from that zone just got the soft-stop, also submit a
            # NEW trade in the OPPOSITE direction at the NEXT bar's
            # open (per nb39, the inversion itself is tradeable).
            # The new trade uses zone_width × ``fvg_inv_trade_sl_zone_mult``
            # as SL and zone_width × ``fvg_inv_trade_tp_zone_mult``
            # as TP (default 1.0/1.8 = 1:1.8 R:R).
            inv_trade_enabled = _inv_trade_enabled
            inv_sl_mult = _inv_sl_mult
            inv_tp_mult = _inv_tp_mult
            inv_min_zone = _inv_min_zone
            inv_max_per = _inv_max_per
            for ot in open_trades:
                tr = ot["trade"]
                zone = getattr(tr, "fvg_zone", None)
                if zone is None:
                    continue
                # Suppress soft-stop during the grace window.
                in_grace = grace_secs > 0 and (b_time - tr.entry_time) / 1e9 < grace_secs
                if in_grace:
                    continue
                # BoS/CHoCH alignment-based soft-stop suppression
                # (added 2026-09-16): when the trade is "aligned" with
                # the structure memory's current thesis, we let it
                # ride through the inversion rather than close at the
                # inversion bar (which is the worst price in the move).
                # The aligned thesis is the dominant force; the
                # inversion is more likely an SL hunt that will revert
                # than a real structural rejection.
                alignment = str(ot.get("entry_alignment", "unknown"))
                if (alignment == "aligned" and _bos_ignore):
                    # Skip the soft-stop entirely. The trade continues
                    # to ride its original SL/TP. The inversion is
                    # recorded as "skipped" for diagnostics but does
                    # NOT trigger the soft-stop path. Re-inversions are
                    # also bypassed for aligned trades.
                    n_alignment_skipped_inversions += 1
                    continue
                if not ot["soft_sl_active"] and bool(getattr(zone, "inverted", False)):
                    zid = id(zone)
                    if zid in _zones_inverted_this_bar:
                        continue  # another layer on the same zone already fired soft-stop this bar
                    _zones_inverted_this_bar.add(zid)
                    # Set the soft-stop.
                    if tr.direction > 0:
                        # Long: SL moves to just under the zone's
                        # bottom edge (price has come back through
                        # the zone, so the zone is now acting as
                        # resistance).
                        ot["soft_sl"] = float(zone.zone_low) - float(p.invalidation_buffer_usd)
                    else:
                        # Short: SL moves to just over the zone's top
                        # edge.
                        ot["soft_sl"] = float(zone.zone_high) + float(p.invalidation_buffer_usd)
                    ot["soft_sl_active"] = True
                    # FIX (2026-09-17 BUG #5): count inversions per ZONE not per trade.
                    # Previously this incremented once per open trade; 3 layers on the same
                    # inverted zone would inflate n_inversions by 3×. Now we track
                    # _zones_inverted_this_bar so n_inversions counts unique zone-inversion
                    # events (one per zone, regardless of how many trades are open on it).
                    n_inversions += 1
                    # ── Submit inverse trade (added 2026-09-15) ──────
                    # Conditions:
                    #  * inv_trade_enabled is on.
                    #  * Zone is wide enough (>= inv_min_zone).
                    #  * Per-zone submission cap not exceeded.
                    #  * The soft-stop just fired (this is the FIRST
                    #    bar the trade has ``soft_sl_active=True`` --
                    #    the bar the inversion triggered the stop).
                    #  * No grace window active (we already exited
                    #    the grace branch above via ``continue``).
                    if (
                        inv_trade_enabled
                        and not ot.get("inv_trade_emitted", False)
                        and (zone.zone_high - zone.zone_low) >= inv_min_zone
                    ):
                        zid = id(zone)
                        n_so_far = inv_zone_count.get(zid, 0)
                        if n_so_far < inv_max_per:
                            inv_zone_count[zid] = n_so_far + 1
                            ot["inv_trade_emitted"] = True
                            zone_w = float(zone.zone_high - zone.zone_low)
                            inv_dir = -tr.direction   # opposite of original
                            # TP choice: ATR-scaled when the ATR mult
                            # knob is > 0 (added 2026-09-17). The two
                            # knobs are mutually exclusive.
                            _inv_tp_atr_mult = float(
                                getattr(p, "fvg_inv_trade_tp_atr_mult", 0.0)
                            )
                            if _inv_tp_atr_mult > 0.0:
                                _atr_v = float(
                                    atr_arr[i]
                                    if 0 <= i < atr_arr.shape[0] else 0.0
                                )
                                target_usd_v = _atr_v * _inv_tp_atr_mult
                            else:
                                target_usd_v = zone_w * inv_tp_mult
                            # Queue the inverse trade for the NEXT bar
                            # (mirrors nb39 finding: enter at next-bar
                            # open, anti-look-ahead). Stored in
                            # pending_inv_layers which the bar loop
                            # drains at the top of step 2.
                            pending_inv_layers.append({
                                "submit_bar": i + 1,
                                "submit_time_ns": int(times_ns[i + 1]) if i + 1 < n else b_time,
                                "direction": inv_dir,
                                "stop_usd": zone_w * inv_sl_mult,
                                "target_usd": target_usd_v,
                                "lots": float(p.lots),
                                "fvg_zone": zone,
                                "is_ifvg": True,  # the entry is conceptually an iFVG (entered against the original)
                                "tp_rule": 1,
                                "rank_tier": "",
                                "rank_percentile": 0.0,
                                "entry_triggered_by": "inv",
                            })
                            ict_series.n_inv_trades_submitted += 1
                # Optional re-inversion: if the zone is *re-inverted*
                # back to its original polarity, also tighten.
                # Bypassed for aligned trades (see above) — re-inversion
                # is a stronger "polarity flipped" signal but the
                # aligned-thesis semantic still says "let it ride".
                if (alignment == "aligned"
                        and bool(getattr(
                            p, "bos_choch_ignore_invert_when_aligned", True,
                        ))):
                    pass  # skip re-inversion logic for aligned trades
                elif (p.re_inversion_also_tightens and
                        not ot["soft_sl_active"] and
                        zone.inverted_bar > 0 and
                        not bool(getattr(zone, "inverted", False))):
                    # Re-inversion: the zone flipped and flipped back.
                    # Use the same soft-stop rule.
                    if tr.direction > 0:
                        ot["soft_sl"] = float(zone.zone_low) - float(p.invalidation_buffer_usd)
                    else:
                        ot["soft_sl"] = float(zone.zone_high) + float(p.invalidation_buffer_usd)
                    ot["soft_sl_active"] = True
                    n_inversions += 1

        # ── 3b. Renko-driven FVG invalidation (added 2026-09-05) ───────
        # When ``renko_drive_invalidation=True`` AND the renko state is
        # available, augment the soft-stop check: if the renko brick
        # direction OPPOSES the trade's direction AND the renko close
        # has been on the WRONG SIDE of the zone edge for at least
        # ``renko_invalidation_min_bricks`` consecutive bricks, set the
        # soft-stop. This stacks on top of the 1s-based rules — both
        # can fire, and whichever fires first wins.
        #
        # The renko close is "on the wrong side" for a long FVG when
        # ``renko.close_per_bar[i] < zone.zone_low`` (a sustained move
        # below the zone's bottom edge); for a short FVG, when
        # ``renko.close_per_bar[i] > zone.zone_high``.
        if (
            renko is not None
            and p.invalidation_sl_usd > 0
            and open_trades
        ):
            # 2026-09-17 hot-path: cache outside the per-bar loop
            # (these don't change between bars).
            renko_buffer = float(getattr(p, "renko_invalidation_buffer_usd", 0.05))
            min_bricks = max(1, int(getattr(p, "renko_invalidation_min_bricks", 2)))
            # Per-zone brick-streak count of consecutive opposing
            # bricks at this bar. We track this per-trade (each trade
            # has its own zone) — but to avoid per-bar O(trades) cost,
            # we compute the streak from the renko state once.
            renko_dir = int(renko.direction_per_bar[i])
            renko_close = float(renko.close_per_bar[i])
            brick_count = int(renko.brick_count_per_bar[i])
            for ot in open_trades:
                if ot["soft_sl_active"]:
                    continue  # already tightened by an earlier rule
                tr = ot["trade"]
                zone = getattr(tr, "fvg_zone", None)
                if zone is None:
                    continue
                # BoS/CHoCH aligned-trade semantic (added 2026-09-16):
                # the renko-based invalidation is a form of "the
                # market has committed to the wrong side". For
                # aligned trades we let the trade ride — the renko
                # streak is just an SL hunt against the dominant
                # thesis.
                ot_alignment = str(ot.get("entry_alignment", "unknown"))
                if (ot_alignment == "aligned"
                        and bool(getattr(
                            p, "bos_choch_ignore_invert_when_aligned", True,
                        ))):
                    continue
                # Renko direction must oppose the trade direction.
                # A long trade on a bull FVG → renko must be -1.
                # A short trade on a bear FVG → renko must be +1.
                if tr.direction > 0 and renko_dir >= 0:
                    continue
                if tr.direction < 0 and renko_dir <= 0:
                    continue
                # Renko close must be on the wrong side of the zone
                # edge by a real distance.
                if tr.direction > 0 and renko_close >= float(zone.zone_low):
                    continue
                if tr.direction < 0 and renko_close <= float(zone.zone_high):
                    continue
                # Count consecutive opposing-direction bricks ending at
                # this bar. Walk backwards through the brick arrays.
                streak = 0
                if brick_count >= 0:
                    for bi in range(brick_count, -1, -1):
                        if int(renko.brick_direction[bi]) != renko_dir:
                            break
                        streak += 1
                        if streak >= min_bricks:
                            break
                if streak < min_bricks:
                    continue
                # Fire the soft-stop with the renko-specific buffer.
                if tr.direction > 0:
                    ot["soft_sl"] = float(zone.zone_low) - renko_buffer
                else:
                    ot["soft_sl"] = float(zone.zone_high) + renko_buffer
                ot["soft_sl_active"] = True
                n_inversions += 1
                n_renko_invalidations += 1

        # ── 4. SL / TP / soft-stop exits for open trades ──────────────
        if open_trades:
            still_open = []
            for ot in open_trades:
                tr = ot["trade"]
                exit_reason = None
                exit_price = None
                # BUG FIX (2026-09-17): grace period now measured in
                # SECONDS (wall-clock), not bars. The docstring says
                # "suppress for first N seconds" but the old code measured
                # ``(i - tr.entry_bar) < grace_bars`` which equals seconds
                # only on 1s data. We now use ``(b_time - tr.entry_time)
                # / 1e9 < _grace_secs`` which works correctly at any
                # bar cadence.

                # PIVOT E (trailing SL) + PIVOT G (SL decay) — compute effective SL ─
                # The effective SL is the MOST PROTECTIVE of all active SL modifiers:
                # original SL < soft-stop SL < trailing SL < decay SL.
                # For longs: highest price = most protective. For shorts: lowest.
                soft_sl_price = ot["soft_sl"] if ot["soft_sl_active"] else None
                # PIVOT E: trailing SL. Once in profit by activation_atr_mult * ATR,
                # trail the SL at best_price - trailing_atr_mult * ATR.
                trail_sl_price = None
                if _trailing_enabled:
                    atr_v = _atr(i)
                    activation_distance = _trailing_activation * atr_v
                    if atr_v > 0:
                        if tr.direction > 0:
                            unrealized = b_close - tr.entry_price
                        else:
                            unrealized = tr.entry_price - b_close
                        if unrealized >= activation_distance:
                            trail_sl_price = (
                                (b_close - _trailing_atr_mult * atr_v)
                                if tr.direction > 0
                                else (b_close + _trailing_atr_mult * atr_v)
                            )
                # PIVOT G: SL decay. The decay schedule is a list of (bar_offset, mult)
                # tuples — at bar_offset bars, the SL distance is multiplied by mult.
                decay_sl_price = None
                if _sl_decay:
                    bars_held = i - tr.entry_bar
                    for offset_bars, mult in _sl_decay:
                        if bars_held >= offset_bars:
                            decay_sl_price = (
                                (tr.entry_price - tr.stop_usd * mult)
                                if tr.direction > 0
                                else (tr.entry_price + tr.stop_usd * mult)
                            )
                            break  # use the first matching step
                # SL/TP prices
                if tr.direction > 0:
                    tp_price = tr.entry_price + tr.target_usd
                    orig_sl = tr.entry_price - tr.stop_usd
                    sl_candidates = [orig_sl]
                    if soft_sl_price is not None:
                        sl_candidates.append(soft_sl_price)
                    if trail_sl_price is not None:
                        sl_candidates.append(trail_sl_price)
                    if decay_sl_price is not None:
                        sl_candidates.append(decay_sl_price)
                    sl_price = max(sl_candidates)  # highest = most protective for longs
                    sl_hit = b_low <= sl_price
                    tp_hit = b_high >= tp_price
                else:
                    tp_price = tr.entry_price - tr.target_usd
                    orig_sl = tr.entry_price + tr.stop_usd
                    sl_candidates = [orig_sl]
                    if soft_sl_price is not None:
                        sl_candidates.append(soft_sl_price)
                    if trail_sl_price is not None:
                        sl_candidates.append(trail_sl_price)
                    if decay_sl_price is not None:
                        sl_candidates.append(decay_sl_price)
                    sl_price = min(sl_candidates)  # lowest = most protective for shorts
                    sl_hit = b_high >= sl_price
                    tp_hit = b_low <= tp_price

                # Tiebreak + exit logic (shared for both directions)
                if sl_hit and tp_hit:
                    # Both targets hit in same bar — use tiebreak.
                    if _tiebreak == "tp_first":
                        exit_price = tp_price
                        exit_reason = "tp"
                    elif _tiebreak == "tp_if_wider" and tr.target_usd >= tr.stop_usd:
                        exit_price = tp_price
                        exit_reason = "tp"
                    else:
                        # Default: sl_first (conservative, legacy).
                        exit_price = sl_price
                        exit_reason = "inv" if ot["soft_sl_active"] else "sl"
                elif sl_hit:
                    exit_price = sl_price
                    exit_reason = "inv" if ot["soft_sl_active"] else "sl"
                elif tp_hit:
                    exit_price = tp_price
                    exit_reason = "tp"

                if exit_reason is not None:
                    if exit_reason == "inv":
                        n_soft_stops += 1
                    _close_trade(tr, exit_price, i, b_time, exit_reason, p, closed_trades)
                    cum_pnl += tr.pnl_usd
                else:
                    still_open.append(ot)
            open_trades = still_open

        equity[i] = cum_pnl

    # ── EOD: close any remaining open trades at bar close ────────────
    if open_trades:
        last_close = float(close[-1])
        last_time = int(times_ns[-1])
        for ot in open_trades:
            _close_trade(ot["trade"], last_close, n - 1, last_time, "eod", p, closed_trades)
            cum_pnl += ot["trade"].pnl_usd
    open_lots = sum(ot["trade"].lots for ot in open_trades)

    return IctBacktestResult(
        strategy=strategy_label,
        params=asdict(p),
        trades=closed_trades,
        open_lots_at_eod=open_lots,
        equity_curve=equity,
        n_signals_emitted=len(signals),
        n_signals_consumed=n_consumed,
        n_fills=n_fills,
        n_soft_stops=n_soft_stops,
        n_inversions_detected=n_inversions,
        # NOTE (2026-09-17): n_signals_rank_a/b/c/skipped removed
        # with the look-forward biased price-rank classifier.
        n_sweep_signals=n_sweep_signals,
        n_sweep_fills=n_sweep_fills,
        n_renko_invalidations=n_renko_invalidations,
        n_structure_invalidations=int(getattr(ict_series, "n_structure_invalidations", 0)),
        # NOTE (2026-09-17): candlestick-quality + tier drop
        # counters REMOVED. ``n_fvg_quality_*`` and
        # ``n_fvg_tier_*`` were already zero (the classifiers were
        # deleted); they're gone entirely now.
        # iFVG min-age filter (added 2026-09-15).
        n_ifvg_age_dropped=int(getattr(ict_series, "n_ifvg_age_dropped", 0)),
        # Body-only counters (added 2026-09-15).
        n_body_mitigations=int(getattr(ict_series, "n_body_mitigations", 0)),
        n_body_inversions=int(getattr(ict_series, "n_body_inversions", 0)),
        # Trade-the-inversion edge (added 2026-09-15).
        n_inv_trades_submitted=int(getattr(ict_series, "n_inv_trades_submitted", 0)),
        n_inv_trades_filled=int(getattr(ict_series, "n_inv_trades_filled", 0)),
        # BoS/CHoCH alignment-based soft-stop suppression (added 2026-09-16).
        n_alignment_skipped_inversions=n_alignment_skipped_inversions,
        n_entry_alignment_aligned=n_entry_alignment_aligned,
        n_entry_alignment_opposed=n_entry_alignment_opposed,
        n_entry_alignment_unknown=n_entry_alignment_unknown,
        # Sniper-in mode diagnostics (added 2026-09-17, v6+ Innovation #1).
        n_sniper_submitted=n_sniper_submitted,
        n_sniper_triggered=n_sniper_triggered,
        n_sniper_cancelled=n_sniper_cancelled,
        n_sniper_expired=n_sniper_expired,
        elapsed_secs=time.time() - t0,
    )


def _open_trade(layer: dict, ep: float, bar: int, b_time: int, p: TrendStrategyParams) -> Trade:
    return Trade(
        entry_bar=bar,
        exit_bar=-1,
        entry_time=b_time,
        exit_time=0,
        direction=layer["direction"],
        entry_price=ep,
        exit_price=0.0,
        stop_usd=layer["stop_usd"],
        target_usd=layer["target_usd"],
        exit_reason="",
        hold_secs=0.0,
        pnl_usd=0.0,
        entry_triggered_by=layer.get("triggered_by") or layer.get("entry_triggered_by", "fvg"),
        lots=layer["lots"],
        layer_idx=layer.get("layer_idx", 0),
        n_layers_signal=p.num_layers,
        signal_id=layer.get("signal_id", -1),
        fvg_zone=layer.get("fvg_zone"),
        is_ifvg=layer.get("is_ifvg", False),
        tp_rule=layer.get("tp_rule", 1),
        rank_tier=layer.get("rank_tier", ""),
        rank_percentile=layer.get("rank_percentile", 0.0),
        # NOTE (2026-09-17): ``n_layers_skipped`` removed from
        # Trade dataclass with the rank-treatment C-tier skip.
    )


def _close_trade(
    tr: Trade, exit_price: float, bar: int, b_time: int, reason: str,
    p: TrendStrategyParams, sink: List[Trade],
) -> None:
    tr.exit_bar = bar
    tr.exit_time = b_time
    tr.exit_price = float(exit_price)
    tr.exit_reason = reason
    if tr.entry_time > 0:
        tr.hold_secs = (b_time - tr.entry_time) / 1e9
    # PnL = signed price move × lots × contract_size
    # Default contract_size=100.0 = XAUUSD (1 lot = 100 oz, $1 move = $100/lot).
    # For BTC Binance perps the canonical config sets contract_size=1.0
    # so 1 lot × $1 move = $1 PnL (1 contract = 1 BTC).
    contract_size = float(getattr(p, "contract_size", 100.0))
    if tr.direction > 0:
        tr.pnl_usd = (tr.exit_price - tr.entry_price) * tr.lots * contract_size
    else:
        tr.pnl_usd = (tr.entry_price - tr.exit_price) * tr.lots * contract_size

    # ── Binance fee debit (added 2026-09-24, BTC fork) ─────────────
    # USDT-M perpetual: taker 0.04% / maker 0.02% per side (BNB
    # discount path: 0.039% / 0.019%). Market-order entries + exits
    # (sniper path) use the TAKER rate. Fee is debited as NOTIONAL ×
    # bps on ENTRY and on EXIT (so 2 sides × 5 bps = 10 bps round-trip
    # at the default).
    #
    # For symmetric notional we charge on the ENTRY price (the
    # position size is fixed at entry); the exit is a flat taker fee
    # at the closing notional — practically equal for the small R:R
    # ranges of this strategy.
    taker_bps = float(getattr(p, "taker_fee_bps", 0.0))
    if taker_bps > 0.0 and tr.lots > 0 and contract_size > 0:
        # Notional at entry (the fixed position size). Round-trip =
        # 2 sides × taker rate.
        notional_usd = abs(tr.entry_price) * tr.lots * contract_size
        tr.fee_usd = notional_usd * (taker_bps / 10_000.0) * 2.0
        tr.taker_bps_charged = taker_bps
        tr.pnl_usd -= tr.fee_usd
    sink.append(tr)
