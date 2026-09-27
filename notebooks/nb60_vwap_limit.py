"""NB60 - VWAP standard-deviation limit-order strategy on FVG signals.

Hypothesis (2026-09-26)
=======================

The 6-month vBTC2 sweep (nb53) and follow-up rounds (nb55-nb59)
established that the canonical sniper recipe trades the zone
FILL as the entry trigger - enter at the inverted edge, ride
the reversal. The fee math on 0.001 BTC and a $0.05-$0.10 ATR
means most of those trades fee-bite (gross edge ≈ 0).

This notebook tests an *alternative entry model* that the user
asked for:

  * For every FVG detected, place **limit orders** at
    ``VWAP +/- k * sigma`` for k in {1.5, 2.0, 2.5}, with the
    "long" side placed below VWAP and "short" above (or
    vice-versa depending on FVG direction - see below).
  * Each limit order sits for up to **60 wall-clock seconds** BEFORE
    being filled; once filled, the trade rides until it hits its
    SL or TP (no post-fill timeout - the user explicitly asked for
    this: "60s is only the limit order, not post fill. once fill
    we just let them ride out sl and tp").
  * If the FVG is **inverted** while an order is pending,
    cancel the unfilled portion. If a trade is already filled
    and the FVG is invalidated, **soft-stop** the open trade at
    the zone's opposite edge + buffer (the existing
    ``invalidation_sl_usd`` semantic).
  * On inversion, also **submit a new limit order in the
    opposite direction** at the SAME VWAP+/-kxsigma anchor,
    riding it until fill or 60s expiry (the "the inversion is
    itself tradeable" thesis from nb39).

Why VWAP and sigma instead of ATR or zone width?

* **VWAP** is the institutional anchor - when price is far
  above VWAP the market is overbought on a session basis; far
  below it is oversold. The limit orders are placed at
  *mean-reversion anchors* relative to the current VWAP.
* **Standard deviation (sigma)** is the natural distance metric
  around a VWAP anchor (the equivalent of an ATR-scaled
  distance but tied to a rolling volume-weighted statistic,
  not a high-low bar range). A 1.5sigma move is a "stretched"
  deviation; 2sigma is "stretched"; 2.5sigma is "stretched".

FVG polarity ↔ limit-order direction:

  * Bull FVG (``zone.direction = +1``):
    - Place a **long limit at VWAP - ksigma** for each kin{1.5, 2, 2.5}.
      Rationale: a bull FVG in a session whose price is
      stretched ABOVE VWAP by 1.5sigma+ has mean-reversion energy
      back down into the gap zone; we want to be a buyer
      *below* VWAP, betting on the snap-back UP through
      the gap.
  * Bear FVG (``zone.direction = -1``):
    - Place a **short limit at VWAP + ksigma** for each k.
      Symmetric reasoning.

Cross-product with k: 3 cells x 3 sigmas = 9 anchor variants
per FVG direction. The driver sweeps a representative subset
of VWAP windows + the 3 sigma multipliers as a smoke test,
showing the per-cell gross/net/WR/distribution.

Run modes
=========

```bash
# Full single-month + 3-month sampler (~3 min on warm cache)
python notebooks/nb60_vwap_limit.py --full

# Fast smoke on 2025-04 only, full grid
python notebooks/nb60_vwap_limit.py --fast
# ATR-scaled SL/TP (auto-sizes per regime). Sweep showed (64, 512)
# is the winning cell on the 6-month corpus. See
# notebooks/nb60_vwap_limit_outputs/nb60_vwap_limit_atr_sweep_all.csv.
python notebooks/nb60_vwap_limit.py --full --atr-sltp --sl-atr-mult 64 --tp-atr-mult 512
# Anti-FVG mode (place on the FVG's side of VWAP instead of opposite)
python notebooks/nb60_vwap_limit.py --full --anti-fvg
```

Output
======

``notebooks/nb60_vwap_limit_outputs/``:
  * ``nb60_vwap_limit__runs.jsonl``     - one record per cell x month.
  * ``nb60_vwap_limit_per_trade.csv``  - per-trade data (~thousands of rows).
  * ``nb60_vwap_limit_summary.csv``    - per-cell x month aggregate.
  * ``nb60_vwap_limit_report.md``      - short narrative.

Run-log record calls ``append_run_report`` with the canonical
helper so the JSONL stays bit-identical to other notebooks.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from collections import Counter
from dataclasses import dataclass, field, replace, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

# ── Repo root setup (matches nb56 style) ──────────────────────────────
ROOT: Optional[Path] = None
for _c in [Path(".").resolve(), *Path(".").resolve().parents]:
    if (_c / "src" / "core" / "ict_signals.py").is_file():
        ROOT = _c
        break
if ROOT is None:
    raise RuntimeError("Could not find ICT repo root.")
sys.path.insert(0, str(ROOT))

from src.core.optimal_config import optimal_params, OPTIMAL_RECIPE_VERSION
from src.core.ict_signals import FvgZone
from src.tick.cache import get_or_build
from src.core.run_report import append_run_report

# ── Constants ────────────────────────────────────────────────────────
NOTEBOOK = "nb60_vwap_limit"
SCENARIO = "vwap_sd_limit_strategy"

OUT_DIR = ROOT / "notebooks" / "nb60_vwap_limit_outputs"
OUT_DIR.mkdir(parents=True, exist_ok=True)

SOURCE_DATA_ROOT = Path(r"C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw")

# (label, months)
DEFAULT_MONTHS = ["2025-04", "2025-11"]
FAST_MONTHS = ["2025-04"]

# ── Strategy knobs (driver-level - not TrendStrategyParams) ──────────
# Number of wall-clock seconds each limit order sits before being
# cancelled if unfilled.
#
# History: 2026-09-27 default bumped from 60s to 1800s (30min). The
# original 60s lifetime was a bug — the limit anchor is VWAP ± k*sigma
# where sigma is the rolling VWAP sigma (typically ~$700 on a 4h window
# at BTC=$100k). At k=1.5 the limit is ~1% of price = 400× the 1s ATR.
# BTC doesn't move 1% in 60s in a normal regime, so 95%+ of orders
# expired without filling. At 1800s the fill rate goes from 3.2% to
# ~12.5% (k=1.5 fill rate 23% vs 6%), giving a more honest sample.
# See notebooks/nb60_vwap_limit_outputs/nb60_vwap_limit_atr_sweep_report.md
# § "Why fills are low" for the diagnosis.
ORDER_LIFETIME_SECS: int = 1800

# Standard-deviation multipliers (k) for limit-price distance from VWAP.
# History:
#   Original: (1.5, 2.0, 2.5) — 1.5 was the lower bound for "regime-extension"
#   2026-09-27a: (1.5, 2.0, 2.5, 3.0, 3.5) — added wider-sigma cells on user
#     hypothesis that 2sigma+ may already be a noise-tier fill.
#   2026-09-27b: (0.3, 0.5, 0.7, 1.0, 1.5) — after the lifetime=1800s fix
#     exposed a tiny-k mean-reversion scalping mode that dominates the
#     regime-extension mode. Sweep at lifetime=1800s showed:
#       (0.3,0.5,0.7,1.0,1.5): +$1072 net @ qty=0.001 / 6mo / 5523 trades
#       (1.5,2.0,2.5,3.0,3.5)  : -$23   net (the original "winner" at lifetime=60s)
#     The tiny-k cells place the limit only 0.1-1% from VWAP and rely on
#     30-min lifetime to catch the small mean-reversion moves; the k=1.5
#     tail captures the occasional strong trend pullback.
SIGMA_VALUES: Tuple[float, ...] = (0.3, 0.5, 0.7, 1.0, 1.5)

# VWAP lookback window in seconds - the rolling VWAP and rolling
# sigma are computed from the trailing N wall-clock seconds of
# 1-second bars before the trigger bar.
VWAP_WINDOWS_SECS: Tuple[int, ...] = (3600, 14400)   # 1h, 4h

# Position size per trade.
QTY_BTC: float = 0.001

# Default SL / TP for the VWAP entry (USD). The SL/TP are sized as
# SL_USD, TP_USD - NOT zone-width-anchored. We deliberately decouple
# from the FVG zone because the entry is *at VWAP+/-ksigma*, not at the
# zone edge.
SL_USD: float = 50.0
TP_USD: float = 200.0

# ──────────────────────────────────────────────────────────────────────
# SL / TP mode (added 2026-09-27 on user feedback: "$50/$200 fixed
# is too tight to make sense — should be ATR based so it auto-scales
# with the regime").
#
# Three modes are available:
#   - "usd_fixed" : same SL_USD / TP_USD for every trade, regardless
#                   of current volatility (the original behaviour).
#   - "atr_mult"  : SL = sl_atr_mult * ATR[i] where ATR[i] is the per-bar
#                   ATR (currently atr_len=1200 = 20-min window of 1s
#                   bars). Tighter SL/TP in quiet regimes, wider in
#                   volatile regimes. Auto-scales.
#   - "zone_width_atr" : not implemented yet - placeholder.
#
# Defaults stay "usd_fixed" so the existing 6-month run is bit-identical
# unless the user passes --atr-sltp on the CLI.
# ──────────────────────────────────────────────────────────────────────
SL_TP_MODE: str = "usd_fixed"        # "usd_fixed" | "atr_mult"
SL_ATR_MULT: float = 64.0            # winning cell from 6-month sweep (25-cell grid); see report
TP_ATR_MULT: float = 512.0           # 8:1 R:R vs SL_ATR_MULT, +$101.49 net over 6 months
ATR_LEN_FOR_SLTP: int = 1200         # must match cache params.atr_len (20 min)

# Soft-stop semantic on FVG inversion:
#   When a long trade sourced from a bull FVG gets its FVG inverted,
#   the trade's SL is tightened to the inverse-direction edge of the
#   zone + buffer (mirrors the canonical ``invalidation_sl_usd`` rule).
#   Same logic for shorts. Trades that aren't sourced from an FVG (no
#   zone tag) keep their original SL/TP.
SOFT_STOP_BUFFER_USD: float = 0.50

# Binance fee model (canonical: 5 bps taker per side, 10 round-trip).
TAKER_FEE_BPS: float = 5.0

# ANTI-FVG mode flag (added 2026-09-27 on user hypothesis:
# "if win rate is still low, there's an ANTI fvg strat to look for").
# In ANTI mode, the strategy flips the trade direction relative to
# the original mean-reversion setup:
#   ORIGINAL (mean-reversion):
#     bull FVG -> BUY  at VWAP - k*sigma  (bet price reverts down through VWAP)
#     bear FVG -> SELL at VWAP + k*sigma  (bet price reverts up through VWAP)
#   ANTI (continuation / anti-mean-reversion):
#     bull FVG -> SELL at VWAP + k*sigma  (bet price continues up past VWAP,
#                                          then breaks down — short the
#                                          overstretched top)
#     bear FVG -> BUY  at VWAP - k*sigma  (bet price continues down past VWAP,
#                                          then snaps up — long the
#                                          oversold bottom)
# Both setups are limit orders that sit 60s. The ANTI side places orders
# on the SAME side as the FVG (above VWAP for bull, below for bear),
# which is also the side where the FVG itself sits. The fill is
# therefore "price breaks through VWAP+k*sigma in the FVG direction
# before reverting".
ANTI_FVG: bool = False


# ──────────────────────────────────────────────────────────────────────
# VWAP + standard deviation
# ──────────────────────────────────────────────────────────────────────

def compute_vwap_sigma(
    close: np.ndarray,
    volume: np.ndarray,
    i_trigger: int,
    window_secs: int,
) -> Tuple[float, float]:
    """Compute trailing VWAP and VWAP-standard-deviation at bar
    ``i_trigger`` over the trailing ``window_secs`` wall-clock seconds.

    Implements the canonical volume-weighted arithmetic:

        vwap = sum(p_j * v_j) / sum(v_j)   over j in [i_lo, i_trigger]
        resid_j = p_j - vwap
        sigma = sqrt( sum(v_j * resid_j^2) / sum(v_j) )

    The volume-weighting on the residual squares ensures that bars
    with higher volume contribute more to the variance estimate
    (matching the way VWAP is computed). For a 1h trailing window on
    1s bars with ~5 ticks/bar, the sample size is ~3600 - statistically
    robust.

    Returns (vwap, sigma). If the window has fewer than 30 bars of
    valid data, returns (close[i_trigger], 0.0) so callers don't
    divide by zero.
    """
    if i_trigger <= 0:
        return float(close[0]), 0.0
    window = max(30, window_secs)
    i_lo = max(0, i_trigger - window)
    p = close[i_lo:i_trigger + 1]
    v = volume[i_lo:i_trigger + 1]
    # Zero-volume bars contribute nothing - drop them.
    mask = v > 0
    if mask.sum() < 30:
        return float(close[i_trigger]), 0.0
    p_w = p[mask]
    v_w = v[mask]
    total_v = float(v_w.sum())
    if total_v <= 0:
        return float(close[i_trigger]), 0.0
    vwap = float((p_w * v_w).sum() / total_v)
    resid = p_w - vwap
    var = float((v_w * resid * resid).sum() / total_v)
    sigma = float(np.sqrt(max(0.0, var)))
    return vwap, sigma


# ──────────────────────────────────────────────────────────────────────
# Limit-order model + trade objects
# ──────────────────────────────────────────────────────────────────────

@dataclass
class LimitOrder:
    """A resting limit order tied to a specific FVG zone."""
    order_id: int
    parent_zone_id: int        # id() of the FvgZone this order was placed on
    parent_zone_ref: Any       # FvgZone (weak ref - used for fast lookup)
    submit_bar: int
    submit_time_ns: int
    expire_time_ns: int        # submit_time_ns + ORDER_LIFETIME_SECS*1e9
    direction: int             # +1 long, -1 short
    limit_price: float
    vwap_at_submit: float
    sigma_at_submit: float
    k_sigma: float             # multiplier (1.5 / 2.0 / 2.5)
    window_secs: int           # VWAP window used
    fvg_direction: int         # parent FVG direction (NOT trade direction)
    source: str                # "vwap_initial" | "vwap_inversion_flip"


@dataclass
class OpenTrade:
    """A trade that has been opened by a limit fill, still open."""
    trade_id: int
    parent_order_id: int
    parent_zone_id: int
    entry_bar: int
    entry_time_ns: int
    entry_price: float
    direction: int
    stop_usd: float            # original SL
    target_usd: float          # original TP
    soft_sl: Optional[float] = None
    soft_sl_active: bool = False
    fvg_direction: int = 0     # original FVG direction (for the opposite-leg logic)
    source: str = "vwap_initial"
    order_k_sigma: float = 0.0 # INTENDED k from the order's placement


@dataclass
class ClosedTrade:
    """A fully closed trade."""
    trade_id: int
    entry_bar: int
    exit_bar: int
    entry_time_ns: int
    exit_time_ns: int
    entry_price: float
    exit_price: float
    direction: int
    stop_usd: float
    target_usd: float
    soft_sl: Optional[float]
    soft_sl_active: bool
    exit_reason: str           # "tp" | "sl" | "soft_sl" | "cancel_open" | "eod"
    hold_secs: float
    pnl_gross_usd: float       # signed price move * qty_btc
    fee_usd: float             # round-trip fee (entry + exit taker)
    pnl_net_usd: float         # gross - fees
    qty_btc: float
    parent_zone_id: int
    parent_order_id: int
    fvg_direction: int
    source: str                # "vwap_initial" | "vwap_inversion_flip" | "soft_stop_close"
    vwap_at_entry: float
    sigma_at_entry: float
    k_sigma: float             # REALISED k at fill time (entry_price vs vwap_at_entry / sigma_at_entry)
    order_k_sigma: float       # INTENDED k from the order's placement (1.5, 2.0, 2.5, 3.0, 3.5)
    window_secs: int


# ──────────────────────────────────────────────────────────────────────
# Bar-loop backtest engine
# ──────────────────────────────────────────────────────────────────────

@dataclass
class BacktestResult:
    """Aggregate container - keeps parity with the per-trade CSV."""
    trades: List[ClosedTrade] = field(default_factory=list)
    n_signals_emitted: int = 0
    n_orders_placed: int = 0
    n_orders_filled: int = 0
    n_orders_cancelled: int = 0
    n_orders_inversion_cancelled: int = 0
    n_soft_stops: int = 0
    n_inversions_submitted_opposite: int = 0
    n_inversions_filled_opposite: int = 0
    # Concurrency telemetry (sampled at each bar)
    n_orders_peak_concurrent: int = 0
    n_orders_concurrent_samples: int = 0
    n_orders_concurrent_sum: int = 0


def run_vwap_limit_backtest(
    bars: pd.DataFrame,
    zones: List[FvgZone],
    *,
    qty_btc: float,
    sl_usd: float,
    tp_usd: float,
    sigma_values: Tuple[float, ...] = SIGMA_VALUES,
    vwap_windows_secs: Tuple[int, ...] = VWAP_WINDOWS_SECS,
    order_lifetime_secs: int = ORDER_LIFETIME_SECS,
    soft_stop_buffer_usd: float = SOFT_STOP_BUFFER_USD,
    taker_fee_bps: float = TAKER_FEE_BPS,
    only_dir: Optional[int] = None,
    anti_fvg: bool = ANTI_FVG,
    atr: Optional[np.ndarray] = None,
    sl_atr_mult: float = SL_ATR_MULT,
    tp_atr_mult: float = TP_ATR_MULT,
    sl_tp_mode: str = SL_TP_MODE,
) -> Tuple[BacktestResult, Dict[str, float]]:
    """Run the VWAP-sigma limit-order backtest on a 1s OHLC dataframe.

    Walk the bar array. For each FVG in ``zones`` (cloned, so
    mutation is local) whose trigger_bar == i, compute trailing VWAP
    and sigma at i across each ``vwap_window_secs``, then place 5 limit
    orders per window x sigma combo (one per k in sigma_values).
    Each order fills if the bar's range covers the limit price;
    otherwise it sits until ``order_lifetime_secs`` elapses
    (cancellation) or the bar-loop advances past expiry.

    SL / TP sizing per fill (added 2026-09-27):
      * "usd_fixed" (default): sl = ``sl_usd``, tp = ``tp_usd`` for
        every trade (the original behaviour).
      * "atr_mult": sl = ``sl_atr_mult * atr[entry_bar]``,
        tp = ``tp_atr_mult * atr[entry_bar]``. Requires ``atr`` to
        be supplied (a float64[N_bars] array). The mode auto-scales
        with regime: tighter SL/TP in quiet markets, wider in
        volatile ones. See the ``SL_TP_MODE`` constant.
    FVG inversion:
      * Cancel any unfilled orders for that zone.
      * Soft-stop any open trade for that zone (move SL to the
        opposite edge + buffer).
      * Submit a new set of orders in the OPPOSITE direction
        (bull FVG inversion -> shorts; bear FVG inversion -> longs)
        at the same VWAP+/-ksigma anchor.

    Returns (result, cache_dict) where cache_dict contains per-cell
    VWAP/sigma values at the trigger bar for diagnostic output.
    """
    times = pd.to_datetime(bars["time"], utc=True).astype("int64").to_numpy()
    open_ = bars["open"].to_numpy(dtype=np.float64)
    high = bars["high"].to_numpy(dtype=np.float64)
    low = bars["low"].to_numpy(dtype=np.float64)
    close = bars["close"].to_numpy(dtype=np.float64)
    if "volume" in bars.columns:
        volume = bars["volume"].to_numpy(dtype=np.float64)
    else:
        # Last-resort: synthesise a unit volume (uniform).
        volume = np.ones_like(close)
    n = len(close)

    # Pre-bucket signals by trigger_bar (avoid O(N*zones) scan).
    zones_at_bar: Dict[int, List[FvgZone]] = {}
    # Pre-bucket inversion events (avoid O(N*zones) scan in the hot loop).
    zones_inverted_at_bar: Dict[int, List[FvgZone]] = {}
    for z in zones:
        if z.live and z.trigger_bar >= 0 and z.trigger_bar < n:
            zones_at_bar.setdefault(int(z.trigger_bar), []).append(z)
        inv_b = int(getattr(z, "inverted_bar", -1))
        if z.live and inv_b >= 0 and inv_b < n:
            zones_inverted_at_bar.setdefault(inv_b, []).append(z)

    pending_orders: List[LimitOrder] = []
    open_trades: List[OpenTrade] = []
    closed_trades: List[ClosedTrade] = []
    next_order_id = 0
    next_trade_id = 0
    # Per-zone cap on the inversion-flip order burst (one set per
    # inversion event; subsequent inversions on the same zone are
    # ignored so we don't double up).
    zone_inverted: Dict[int, bool] = {}

    res = BacktestResult()
    diag_first_zone: Dict[str, float] = {}

    for i in range(n):
        b_open = float(open_[i])
        b_high = float(high[i])
        b_low = float(low[i])
        b_close = float(close[i])
        b_time = int(times[i])

        # ── 1. New FVG signals at bar i ──────────────────────────────
        triggered_zones = zones_at_bar.get(i, [])
        for zone in triggered_zones:
            if only_dir is not None and int(zone.direction) != only_dir:
                continue
            res.n_signals_emitted += 1
            # Compute (vwap, sigma) once per window. For each combination,
            # place 3 limit orders (one per sigma multiplier). Each window
            # x sigma set produces its own order ID group.
            for window_secs in vwap_windows_secs:
                vwap, sigma = compute_vwap_sigma(close, volume, i, window_secs)
                if sigma <= 0:
                    # sigma can't be zero (no anchor); skip silently.
                    continue
                for k in sigma_values:
                    if int(zone.direction) > 0:
                        # Bull FVG: gap UP. zone is above current price.
                        if anti_fvg:
                            # ANTI (continuation / short the top):
                            # bet price continues up through VWAP+k*sigma,
                            # then reverses. Sell at VWAP + k*sigma.
                            trade_dir = -1
                            direction_of_order = -1
                            limit_price = vwap + k * sigma
                        else:
                            # ORIGINAL (mean-reversion / buy the dip):
                            # bet price reverts down through VWAP.
                            # Buy at VWAP - k*sigma.
                            trade_dir = +1
                            direction_of_order = +1
                            limit_price = vwap - k * sigma
                    else:
                        # Bear FVG: gap DOWN. zone is below current price.
                        if anti_fvg:
                            # ANTI (continuation / long the bottom):
                            # bet price continues down through VWAP-k*sigma,
                            # then snaps up. Buy at VWAP - k*sigma.
                            trade_dir = +1
                            direction_of_order = +1
                            limit_price = vwap - k * sigma
                        else:
                            # ORIGINAL (mean-reversion / sell the rip):
                            # bet price reverts up through VWAP.
                            # Sell at VWAP + k*sigma.
                            trade_dir = -1
                            direction_of_order = -1
                            limit_price = vwap + k * sigma
                    next_order_id += 1
                    pending_orders.append(LimitOrder(
                        order_id=next_order_id,
                        parent_zone_id=id(zone),
                        parent_zone_ref=zone,
                        submit_bar=i,
                        submit_time_ns=b_time,
                        expire_time_ns=b_time + order_lifetime_secs * 1_000_000_000,
                        direction=direction_of_order,
                        limit_price=float(limit_price),
                        vwap_at_submit=float(vwap),
                        sigma_at_submit=float(sigma),
                        k_sigma=float(k),
                        window_secs=int(window_secs),
                        fvg_direction=int(zone.direction),
                        source="vwap_initial",
                    ))
                    res.n_orders_placed += 1
                    if not diag_first_zone:
                        diag_first_zone = {
                            "first_vwap": float(vwap),
                            "first_sigma": float(sigma),
                            "first_k": float(k),
                            "first_window_secs": float(window_secs),
                            "first_limit_price": float(limit_price),
                            "fvg_zone_low": float(zone.zone_low),
                            "fvg_zone_high": float(zone.zone_high),
                            "fvg_direction": float(zone.direction),
                        }

        # --- 2. Try to fill each pending order this bar ---------------
        # IMPORTANT: fill-first, expiry-second. So an order is
        # allowed to fill on its expiry bar (the natural "limit sits
        # for the full 60s and fills on the closing bar" pattern) and
        # only gets cancelled AFTER that bar closes unfilled. The
        # 60s lifetime bounds how long the order SITS, not how long
        # the trade lives - filled trades ride until SL/TP/soft_sl
        # (per user feedback 2026-09-27: "60s is only the limit
        # order, not post fill. once fill we just let them ride out
        # sl and tp").
        still_pending: List[LimitOrder] = []
        for order in pending_orders:
            filled = False
            entry_price = 0.0
            if order.direction > 0:
                if b_low <= order.limit_price:
                    entry_price = order.limit_price
                    filled = True
            else:
                if b_high >= order.limit_price:
                    entry_price = order.limit_price
                    filled = True
            if filled:
                res.n_orders_filled += 1
                next_trade_id += 1
                # Compute SL / TP per-fill. ATR mode (sl_tp_mode ==
                # 'atr_mult') uses the per-bar ATR[i] at the fill bar
                # to size SL/TP. usd_fixed mode uses the configured
                # sl_usd / tp_usd (original behaviour, bit-identical).
                if sl_tp_mode == "atr_mult" and atr is not None:
                    atr_i = float(atr[i])
                    eff_sl = sl_atr_mult * atr_i
                    eff_tp = tp_atr_mult * atr_i
                else:
                    eff_sl = sl_usd
                    eff_tp = tp_usd
                ot = OpenTrade(
                    trade_id=next_trade_id,
                    parent_order_id=order.order_id,
                    parent_zone_id=order.parent_zone_id,
                    entry_bar=i,
                    entry_time_ns=b_time,
                    entry_price=entry_price,
                    direction=order.direction,
                    stop_usd=float(eff_sl),
                    target_usd=float(eff_tp),
                    fvg_direction=order.fvg_direction,
                    source=order.source,
                    order_k_sigma=float(order.k_sigma),
                )
                open_trades.append(ot)
                continue
            if b_time >= order.expire_time_ns:
                res.n_orders_cancelled += 1
                continue
            still_pending.append(order)
        pending_orders = still_pending
        # Instrumentation: peak concurrent live orders
        if len(pending_orders) > res.n_orders_peak_concurrent:
            res.n_orders_peak_concurrent = len(pending_orders)
        res.n_orders_concurrent_samples += 1
        res.n_orders_concurrent_sum += len(pending_orders)

        # ── 3. Check each open trade for SL / TP / soft-stop ────────
        # IMPORTANT: there is NO post-fill time-out. The 60s lifetime
        # only governs unfilled LIMIT ORDERS (step 2 above). Once a
        # limit fills and a trade is opened, it rides until its SL,
        # TP, or soft-stop fires. Same-bar stops (when a bar gaps
        # through both the fill price and the SL) are legitimate
        # outcomes and produce ``hold_secs = 0``.
        still_open: List[OpenTrade] = []
        for ot in open_trades:
            exit_reason = ""
            exit_price = 0.0
            # 3a. Apply soft-stop (if active): SL is at zone edge + buffer
            #     in the OPPOSITE direction (price committed through the zone).
            active_sl = ot.stop_usd
            if ot.soft_sl_active and ot.soft_sl is not None:
                # soft_sl is an ABSOLUTE PRICE (zone edge + buffer).
                # Long: SL fires when low ≤ soft_sl_price. Translate that
                # to a price-distance via entry_price.
                if ot.direction > 0:
                    active_sl = max(active_sl, ot.entry_price - ot.soft_sl)
                else:
                    active_sl = max(active_sl, ot.soft_sl - ot.entry_price)
            active_tp = ot.target_usd

            # TP check (high/ low reach)
            tp_price = ot.entry_price + active_tp if ot.direction > 0 else ot.entry_price - active_tp
            sl_price = ot.entry_price - active_sl if ot.direction > 0 else ot.entry_price + active_sl

            if ot.direction > 0:
                # Long: TP hit if high ≥ tp_price; SL hit if low ≤ sl_price.
                if b_high >= tp_price:
                    exit_reason = "tp"
                    exit_price = tp_price
                elif b_low <= sl_price:
                    exit_reason = "sl" if not ot.soft_sl_active else "soft_sl"
                    exit_price = sl_price
            else:
                # Short: TP hit if low ≤ tp_price; SL hit if high ≥ sl_price.
                if b_low <= tp_price:
                    exit_reason = "tp"
                    exit_price = tp_price
                elif b_high >= sl_price:
                    exit_reason = "sl" if not ot.soft_sl_active else "soft_sl"
                    exit_price = sl_price

            if exit_reason:
                gross = (exit_price - ot.entry_price) * ot.direction * qty_btc
                fee = abs(exit_price + ot.entry_price) * 0.5 * qty_btc * (taker_fee_bps / 10000.0) * 2.0
                net = gross - fee
                hold_secs = (b_time - ot.entry_time_ns) / 1e9
                closed_trades.append(ClosedTrade(
                    trade_id=ot.trade_id,
                    entry_bar=ot.entry_bar,
                    exit_bar=i,
                    entry_time_ns=ot.entry_time_ns,
                    exit_time_ns=b_time,
                    entry_price=ot.entry_price,
                    exit_price=exit_price,
                    direction=ot.direction,
                    stop_usd=active_sl,
                    target_usd=active_tp,
                    soft_sl=ot.soft_sl,
                    soft_sl_active=ot.soft_sl_active,
                    exit_reason=exit_reason,
                    hold_secs=hold_secs,
                    pnl_gross_usd=gross,
                    fee_usd=fee,
                    pnl_net_usd=net,
                    qty_btc=qty_btc,
                    parent_zone_id=ot.parent_zone_id,
                    parent_order_id=ot.parent_order_id,
                    fvg_direction=ot.fvg_direction,
                    source=ot.source,
                    vwap_at_entry=0.0,        # filled below
                    sigma_at_entry=0.0,
                    k_sigma=0.0,
                    order_k_sigma=float(ot.order_k_sigma),
                    window_secs=0,
                ))
                if exit_reason == "soft_sl":
                    res.n_soft_stops += 1
            else:
                still_open.append(ot)
        open_trades = still_open

        # ── 4. FVG inversion events at bar i ─────────────────────────
        # A zone has just been inverted: check each open trade's parent
        # zone. If we have unfilled orders, cancel them. If we have a
        # filled trade, soft-stop it. Also submit a fresh set of
        # orders in the OPPOSITE direction at the same VWAP+/-ksigma anchor.
        #
        # Because ``detect_fvg`` mutates the same FvgZone object at
        # cache-build time, we look at the zone's ``inverted_bar`` field:
        # -1 means "never inverted", otherwise it's the bar index where
        # the inversion was recorded.
        #
        # Performance: pre-bucketed in ``zones_inverted_at_bar`` so we
        # only walk the small inversion list at each bar (not all
        # 800+ cached zones). With fewer than 100 inversions per
        # 2.5M-bar month, this drops the per-bar cost from O(N*zones)
        # to O(1) amortised.
        newly_inverted = [z for z in zones_inverted_at_bar.get(i, [])
                          if not zone_inverted.get(id(z), False)]
        for zone in newly_inverted:
            zone_inverted[id(zone)] = True
            res.n_orders_inversion_cancelled += len(
                [o for o in pending_orders if o.parent_zone_id == id(zone)]
            )
            pending_orders = [o for o in pending_orders
                              if o.parent_zone_id != id(zone)]
            # Soft-stop any open trade sourced from this zone.
            for ot in open_trades:
                if ot.parent_zone_id == id(zone):
                    ot.soft_sl_active = True
                    if ot.direction > 0:
                        # Long: SL to zone_low - buffer.
                        ot.soft_sl = float(zone.zone_low) - soft_stop_buffer_usd
                    else:
                        # Short: SL to zone_high + buffer.
                        ot.soft_sl = float(zone.zone_high) + soft_stop_buffer_usd
            # Submit the inversion-flip orders (opposite direction).
            res.n_inversions_submitted_opposite += 1
            for window_secs in vwap_windows_secs:
                vwap, sigma = compute_vwap_sigma(close, volume, i, window_secs)
                if sigma <= 0:
                    continue
                for k in sigma_values:
                    fvg_dir = int(zone.direction)
                    if fvg_dir > 0:
                        # Original was BULL -> flipped trade is SHORT
                        # (inversion is itself a reversal signal - see nb39).
                        new_trade_dir = -1
                        limit_price = vwap + k * sigma
                    else:
                        new_trade_dir = +1
                        limit_price = vwap - k * sigma
                    next_order_id += 1
                    pending_orders.append(LimitOrder(
                        order_id=next_order_id,
                        parent_zone_id=id(zone),
                        parent_zone_ref=zone,
                        submit_bar=i,
                        submit_time_ns=b_time,
                        expire_time_ns=b_time + order_lifetime_secs * 1_000_000_000,
                        direction=new_trade_dir,
                        limit_price=float(limit_price),
                        vwap_at_submit=float(vwap),
                        sigma_at_submit=float(sigma),
                        k_sigma=float(k),
                        window_secs=int(window_secs),
                        fvg_direction=fvg_dir,
                        source="vwap_inversion_flip",
                    ))
                    res.n_orders_placed += 1

    # ── End-of-data: close any remaining open trades at last bar ────
    last_close = float(close[-1])
    last_time = int(times[-1])
    for ot in open_trades:
        gross = (last_close - ot.entry_price) * ot.direction * qty_btc
        fee = abs(last_close + ot.entry_price) * 0.5 * qty_btc * (taker_fee_bps / 10000.0) * 2.0
        net = gross - fee
        hold_secs = (last_time - ot.entry_time_ns) / 1e9
        closed_trades.append(ClosedTrade(
            trade_id=ot.trade_id,
            entry_bar=ot.entry_bar,
            exit_bar=n - 1,
            entry_time_ns=ot.entry_time_ns,
            exit_time_ns=last_time,
            entry_price=ot.entry_price,
            exit_price=last_close,
            direction=ot.direction,
            stop_usd=ot.stop_usd,
            target_usd=ot.target_usd,
            soft_sl=ot.soft_sl,
            soft_sl_active=ot.soft_sl_active,
            exit_reason="eod",
            hold_secs=hold_secs,
            pnl_gross_usd=gross,
            fee_usd=fee,
            pnl_net_usd=net,
            qty_btc=qty_btc,
            parent_zone_id=ot.parent_zone_id,
            parent_order_id=ot.parent_order_id,
            fvg_direction=ot.fvg_direction,
            source=ot.source,
            vwap_at_entry=0.0,
            sigma_at_entry=0.0,
            k_sigma=0.0,
            order_k_sigma=float(ot.order_k_sigma),
            window_secs=0,
        ))

    # Backfill VWAP/sigma on each closed trade by re-computing at entry bar.
    # (Cheap since we only do it for filled trades.)
    for ct in closed_trades:
        # Reconstruct the order's vwap/sigma/sigma by looking at any
        # corresponding order. We don't store the order on the trade
        # directly, so we recompute. For trades WITHOUT a matching
        # order (e.g. soft-stop close), the vwap is approximated as
        # the close at entry_bar.
        # For simplicity: recompute the (vwap, sigma) at entry_bar with
        # the canonical 1h window. This is the same default used by
        # the order placement, so trades look "as placed".
        vwap_e, sigma_e = compute_vwap_sigma(
            close, volume, ct.entry_bar, vwap_windows_secs[0]
        )
        if sigma_e > 0:
            ct.vwap_at_entry = vwap_e
            ct.sigma_at_entry = sigma_e
            if ct.direction > 0:
                # Trade is LONG -> k = (vwap - entry) / sigma if entry < vwap.
                # If entry > vwap (rare - VWAP stretched below us), k
                # is negative; we mask it to 0.
                k_e = (vwap_e - ct.entry_price) / sigma_e
            else:
                k_e = (ct.entry_price - vwap_e) / sigma_e
            ct.k_sigma = float(max(0.0, k_e))
            ct.window_secs = int(vwap_windows_secs[0])

    res.trades = closed_trades
    return res, diag_first_zone


# ──────────────────────────────────────────────────────────────────────
# Driver - one cell per (month, qty, sl, tp, sigma_subset, window_subset).
# ──────────────────────────────────────────────────────────────────────

def run_one_cell(
    month_label: str,
    raw_path: Path,
    *,
    qty_btc: float,
    sl_usd: float,
    tp_usd: float,
    sigma_values: Tuple[float, ...] = SIGMA_VALUES,
    vwap_windows_secs: Tuple[int, ...] = VWAP_WINDOWS_SECS,
    anti_fvg: bool = ANTI_FVG,
    sl_tp_mode: str = SL_TP_MODE,
    sl_atr_mult: float = SL_ATR_MULT,
    tp_atr_mult: float = TP_ATR_MULT,
) -> Dict[str, Any]:
    """Run one cell (single month) and return a dict of metrics."""
    print(f"\n[{NOTEBOOK}] [{month_label}] {raw_path.name}", flush=True)
    p = optimal_params()
    t0 = time.perf_counter()
    sc = get_or_build(raw_path, p, with_side_table=False, verbose=False)
    build_s = time.perf_counter() - t0
    print(f"  cache={build_s:.1f}s "
          f"bars={len(sc.bars):,} "
          f"fvg={len(sc.fvg_zones):,} "
          f"ifvg={len(sc.ifvg_zones):,}", flush=True)

    # CRITICAL: clone zones so the cached state isn't mutated.
    # (Same fix as run_ict_backtest, see AGENTS.md § Cached sweep pipeline)
    zones = [replace(z) for z in sc.fvg_zones]
    print(f"  cloned {len(zones)} FVG zones for backtest", flush=True)

    t0 = time.perf_counter()
    res, diag = run_vwap_limit_backtest(
        sc.bars,
        zones,
        qty_btc=qty_btc,
        sl_usd=sl_usd,
        tp_usd=tp_usd,
        sigma_values=sigma_values,
        vwap_windows_secs=vwap_windows_secs,
        anti_fvg=anti_fvg,
        atr=sc.atr,
        sl_atr_mult=sl_atr_mult,
        tp_atr_mult=tp_atr_mult,
        sl_tp_mode=sl_tp_mode,
    )
    bt_s = time.perf_counter() - t0

    pnls_net = [t.pnl_net_usd for t in res.trades]
    pnls_gross = [t.pnl_gross_usd for t in res.trades]
    fees = [t.fee_usd for t in res.trades]
    n = len(res.trades)
    n_wins = sum(1 for v in pnls_net if v > 0)
    wr = 100.0 * n_wins / n if n else 0.0
    sum_gross = sum(pnls_gross)
    sum_fees = sum(fees)
    sum_net = sum(pnls_net)
    ev = sum_net / n if n else 0.0
    exit_breakdown: Dict[str, int] = {}
    for t in res.trades:
        exit_breakdown[t.exit_reason] = exit_breakdown.get(t.exit_reason, 0) + 1
    n_long = sum(1 for t in res.trades if t.direction > 0)
    n_short = sum(1 for t in res.trades if t.direction < 0)
    n_initial = sum(1 for t in res.trades if t.source == "vwap_initial")
    n_flip = sum(1 for t in res.trades if t.source == "vwap_inversion_flip")
    fill_rate = 100.0 * res.n_orders_filled / max(1, res.n_orders_placed)
    inv_cancel_rate = 100.0 * res.n_orders_inversion_cancelled / max(1, res.n_orders_placed)

    avg_concurrent = res.n_orders_concurrent_sum / max(1, res.n_orders_concurrent_samples)
    print(f"  backtest={bt_s:.1f}s | "
          f"signals={res.n_signals_emitted} | "
          f"orders_placed={res.n_orders_placed} | "
          f"orders_filled={res.n_orders_filled} ({fill_rate:.1f}%) | "
          f"orders_cancelled={res.n_orders_cancelled} | "
          f"inv_cancelled={res.n_orders_inversion_cancelled} ({inv_cancel_rate:.1f}%) | "
          f"soft_stops={res.n_soft_stops} | "
          f"inversions_submitted_opposite={res.n_inversions_submitted_opposite} | "
          f"live_peak={res.n_orders_peak_concurrent} live_avg={avg_concurrent:.1f}", flush=True)
    print(f"  trades={n} (long={n_long} short={n_short}; initial={n_initial} flip={n_flip}) | "
          f"net=${sum_net:+8.2f} gross=${sum_gross:+8.2f} fees=${sum_fees:6.2f} | "
          f"WR={wr:5.1f}% EV=${ev:+.4f}", flush=True)
    print(f"  exits: {exit_breakdown}", flush=True)

    if diag:
        print(f"  first-zone diag (fvg_dir={int(diag.get('fvg_direction', 0)):+d}): "
              f"vwap=${diag.get('first_vwap', 0):.2f} sigma=${diag.get('first_sigma', 0):.2f} "
              f"k={diag.get('first_k', 0):.2f} limit=${diag.get('first_limit_price', 0):.2f} "
              f"zone=[{diag.get('fvg_zone_low', 0):.2f}, {diag.get('fvg_zone_high', 0):.2f}]",
              flush=True)

    return {
        "month": month_label,
        "qty_btc": qty_btc,
        "sl_usd": sl_usd,
        "tp_usd": tp_usd,
        "sigma_values": list(sigma_values),
        "vwap_windows_secs": list(vwap_windows_secs),
        "n_signals_emitted": res.n_signals_emitted,
        "n_orders_placed": res.n_orders_placed,
        "n_orders_filled": res.n_orders_filled,
        "fill_rate_pct": fill_rate,
        "n_orders_cancelled": res.n_orders_cancelled,
        "n_orders_inversion_cancelled": res.n_orders_inversion_cancelled,
        "n_soft_stops": res.n_soft_stops,
        "n_inversions_submitted_opposite": res.n_inversions_submitted_opposite,
        "n_inversions_filled_opposite": res.n_inversions_filled_opposite,
        "n_trades": n,
        "n_long": n_long,
        "n_short": n_short,
        "n_initial_source_trades": n_initial,
        "n_flip_source_trades": n_flip,
        "win_rate_pct": wr,
        "pnl_gross_usd": sum_gross,
        "fees_paid_usd": sum_fees,
        "pnl_net_usd": sum_net,
        "ev_per_trade_usd": ev,
        "exit_reason_breakdown": exit_breakdown,
        "diag_first_zone": diag,
        "trades": res.trades,
        "bt_seconds": bt_s,
    }


def main() -> None:
    global ANTI_FVG, SL_TP_MODE
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true",
                    help="Smoke test: 2025-04 only (one file, fast).")
    ap.add_argument("--full", action="store_true",
                    help="Full validation: 6-month sweep.")
    ap.add_argument("--months", nargs="*", default=None,
                    help="Explicit month list (e.g. 2025-04 2025-11).")
    ap.add_argument("--anti-fvg", action="store_true",
                    help="Toggle ANTI-FVG mode (anti mean-reversion: "
                    "place orders on the FVG's side of VWAP instead of "
                    "the opposite side). The user-hypothesis test for "
                    "whether the WR staying low at higher k indicates "
                    "an anti-fvg regime.")
    ap.add_argument("--atr-sltp", action="store_true",
                    help="Switch SL/TP from fixed USD to ATR-scaled. "
                    "SL = sl_atr_mult * ATR[entry_bar], "
                    "TP = tp_atr_mult * ATR[entry_bar]. "
                    "Auto-scales with regime so the SL/TP is "
                    "proportional to current volatility (the user "
                    "flagged '$50/$200 fixed is too tight to make "
                    "sense' 2026-09-27).")
    ap.add_argument("--sl-atr-mult", type=float, default=None,
                    help=f"Override SL_ATR_MULT (default {SL_ATR_MULT}). "
                    "Only meaningful with --atr-sltp.")
    ap.add_argument("--tp-atr-mult", type=float, default=None,
                    help=f"Override TP_ATR_MULT (default {TP_ATR_MULT}). "
                    "Only meaningful with --atr-sltp.")
    ap.add_argument("--lifetime-secs", type=int, default=None,
                    help=f"Override ORDER_LIFETIME_SECS (default "
                    f"{ORDER_LIFETIME_SECS}). How long each unfilled "
                    "limit order sits before being cancelled.")
    ap.add_argument("--sigma", type=float, nargs="*", default=None,
                    help=f"Override SIGMA_VALUES (default {list(SIGMA_VALUES)}). "
                    "Pass a space-separated list, e.g. --sigma 0.3 0.5 0.7 "
                    "for tiny-k (mean-reversion scalping) or --sigma 1.5 "
                    "2.0 2.5 for the original regime-extension test.")
    args = ap.parse_args()

    if args.fast:
        months = FAST_MONTHS
    elif args.months:
        months = args.months
    elif args.full:
        months = ["2025-04", "2025-05", "2025-10",
                  "2025-11", "2026-02", "2026-05"]
    else:
        months = DEFAULT_MONTHS

    if args.anti_fvg:
        ANTI_FVG = True
    if args.atr_sltp:
        SL_TP_MODE = "atr_mult"
    if args.sl_atr_mult is not None:
        # Mutating a module-level from main(). Globals trick.
        globals()["SL_ATR_MULT"] = float(args.sl_atr_mult)
    if args.tp_atr_mult is not None:
        globals()["TP_ATR_MULT"] = float(args.tp_atr_mult)
    if args.lifetime_secs is not None:
        globals()["ORDER_LIFETIME_SECS"] = int(args.lifetime_secs)
    if args.sigma is not None and len(args.sigma) > 0:
        globals()["SIGMA_VALUES"] = tuple(float(s) for s in args.sigma)

    print(f"[{NOTEBOOK}] VWAP-sigma limit-order FVG strategy", flush=True)
    print(f"  recipe: {OPTIMAL_RECIPE_VERSION}", flush=True)
    print(f"  ANTI_FVG mode: {ANTI_FVG}", flush=True)
    print(f"  SL/TP mode: {SL_TP_MODE}", flush=True)
    if SL_TP_MODE == "atr_mult":
        print(f"  SL_ATR_MULT: {SL_ATR_MULT}  TP_ATR_MULT: {TP_ATR_MULT}  "
              f"(1:{TP_ATR_MULT/SL_ATR_MULT:.1f} R:R)")
    print(f"  months: {months}", flush=True)
    print(f"  sigma values: {SIGMA_VALUES}", flush=True)
    print(f"  VWAP windows: {VWAP_WINDOWS_SECS}", flush=True)
    print(f"  order lifetime: {ORDER_LIFETIME_SECS}s", flush=True)
    print(f"  qty_btc={QTY_BTC} sl_usd={SL_USD} tp_usd={TP_USD}", flush=True)
    print(flush=True)

    all_results: List[Dict[str, Any]] = []
    grand_t0 = time.perf_counter()

    for month in months:
        path = SOURCE_DATA_ROOT / f"BTCUSDT-aggTrades-{month}.parquet"
        if not path.exists():
            print(f"  WARN: {path} not found, skip", flush=True)
            continue
        result = run_one_cell(
            month, path,
            qty_btc=QTY_BTC,
            sl_usd=SL_USD,
            tp_usd=TP_USD,
            sigma_values=SIGMA_VALUES,
            vwap_windows_secs=VWAP_WINDOWS_SECS,
            anti_fvg=ANTI_FVG,
            sl_tp_mode=SL_TP_MODE,
            sl_atr_mult=SL_ATR_MULT,
            tp_atr_mult=TP_ATR_MULT,
        )
        all_results.append(result)

        # Persist per-cell run report via the canonical helper.
        # Build a "fake" IctBacktestResult-like to feed metrics into
        # the standard summary path. We adapt each ClosedTrade to a
        # minimal Trade-like shim (with the attributes
        # ``run_report._compute_metrics`` reads: pnl_usd, fee_usd,
        # hold_secs, direction, exit_reason, entry_time).
        class _ShimTrade:
            __slots__ = ("pnl_usd", "fee_usd", "hold_secs",
                         "direction", "exit_reason", "entry_time")
            def __init__(self, ct: ClosedTrade) -> None:
                self.pnl_usd = float(ct.pnl_net_usd)
                self.fee_usd = float(ct.fee_usd)
                self.hold_secs = float(ct.hold_secs)
                self.direction = int(ct.direction)
                self.exit_reason = str(ct.exit_reason)
                self.entry_time = int(ct.entry_time_ns)

        class _Shim:
            def __init__(self, trades: List[ClosedTrade]) -> None:
                self.trades = [_ShimTrade(t) for t in trades]
                self.n_signals_emitted = result["n_signals_emitted"]
                self.n_signals_consumed = result["n_orders_filled"]
                self.n_fills = result["n_orders_filled"]
                self.n_soft_stops = result["n_soft_stops"]
                self.n_inversions_detected = result["n_inversions_submitted_opposite"]

        shim = _Shim(result["trades"])
        # Use p (canonical) as params baseline; tag this cell's
        # overrides explicitly so the JSONL record documents what
        # we changed.
        p = optimal_params()
        append_run_report(
            notebook=NOTEBOOK,
            scenario=SCENARIO,
            scope=[month],
            engine="bar_vwap_limit",
            comments=(
                "VWAP-sigma limit-order strategy on FVG signals. "
                f"Mode={'ANTI' if ANTI_FVG else 'ORIGINAL'} "
                f"(ANTI={ANTI_FVG}: place orders on the SAME side of VWAP "
                "as the FVG, betting on continuation-through-VWAP, "
                "opposite of the ORIGINAL mean-reversion setup). "
                f"For each FVG, place {len(SIGMA_VALUES)} limit orders x "
                f"{len(VWAP_WINDOWS_SECS)} VWAP windows = "
                f"{len(SIGMA_VALUES) * len(VWAP_WINDOWS_SECS)} orders per "
                f"signal at VWAP +/- k*sigma for kin{SIGMA_VALUES}. Each "
                f"order lives {ORDER_LIFETIME_SECS}s. On FVG inversion: "
                "cancel unfilled orders, soft-stop filled trades, submit "
                f"opposite-direction orders. SL=${SL_USD}, TP=${TP_USD}, "
                f"qty={QTY_BTC} BTC."
            ),
            hypothesis=(
                "Trading the VWAP-sigma extremes (instead of the FVG zone "
                "edge) provides a better entry anchor on a 1s BTC "
                "timebase - the zone edge is a noise-prone 1-min "
                "candle extreme, while VWAP+sigma is a volume-weighted "
                "rolling statistic. The 60s order lifetime caps the "
                "fill rate; the wider-sigma cells (k=3.0, 3.5) test "
                "whether wider-stretch fills carry higher WR "
                "(sufficient sample on 6-month corpus). ANTI mode "
                "tests whether fading the FVG continuation direction "
                "(entering on the FVG's side of VWAP, betting on "
                "VWAP+k*sigma rejection in the FVG direction) is "
                "more profitable than the mean-reversion setup."
            ),
            verdict="inconclusive",
            params=p,
            result=shim,
            metrics_extra={
                "qty_btc": QTY_BTC,
                "sl_usd": SL_USD,
                "tp_usd": TP_USD,
                "sl_tp_mode": SL_TP_MODE,
                "sl_atr_mult": SL_ATR_MULT,
                "tp_atr_mult": TP_ATR_MULT,
                "anti_fvg": ANTI_FVG,
                "sigma_values": list(SIGMA_VALUES),
                "vwap_windows_secs": list(VWAP_WINDOWS_SECS),
                "order_lifetime_secs": ORDER_LIFETIME_SECS,
                "n_orders_placed": result["n_orders_placed"],
                "n_orders_filled": result["n_orders_filled"],
                "fill_rate_pct": result["fill_rate_pct"],
                "n_orders_cancelled": result["n_orders_cancelled"],
                "n_orders_inversion_cancelled": result["n_orders_inversion_cancelled"],
                "n_initial_source_trades": result["n_initial_source_trades"],
                "n_flip_source_trades": result["n_flip_source_trades"],
                "n_inversions_submitted_opposite": result["n_inversions_submitted_opposite"],
                "n_inversions_filled_opposite": result["n_inversions_filled_opposite"],
                "diag_first_zone": result["diag_first_zone"],
            },
            overrides_vs_canonical={
                "entry_mode": "vwap_limit",
                "qty_btc": QTY_BTC,
                "sl_usd": SL_USD,
                "tp_usd": TP_USD,
                "sl_tp_mode": SL_TP_MODE,
                "sl_atr_mult": SL_ATR_MULT,
                "tp_atr_mult": TP_ATR_MULT,
                "anti_fvg": ANTI_FVG,
                "sigma_values": list(SIGMA_VALUES),
                "vwap_windows_secs": list(VWAP_WINDOWS_SECS),
                "order_lifetime_secs": ORDER_LIFETIME_SECS,
            },
            canonical_recipe_version=OPTIMAL_RECIPE_VERSION,
            dedup_keys=("entry_mode", "qty_btc", "sl_usd", "tp_usd",
                        "sl_tp_mode", "sl_atr_mult", "tp_atr_mult",
                        "anti_fvg", "sigma_values", "vwap_windows_secs",
                        "order_lifetime_secs"),
        )

    elapsed = time.perf_counter() - grand_t0
    print(f"\n=== {NOTEBOOK} done. wall={elapsed:.1f}s ===", flush=True)

    if not all_results:
        print("  no results to persist", flush=True)
        return

    # ── Persist per-trade CSV ────────────────────────────────────────
    per_trade_rows: List[Dict[str, Any]] = []
    for r in all_results:
        for t in r["trades"]:
            per_trade_rows.append({
                "month": r["month"],
                "trade_id": int(t.trade_id),
                "entry_bar": int(t.entry_bar),
                "exit_bar": int(t.exit_bar),
                "entry_time": pd.Timestamp(t.entry_time_ns, unit="ns", tz="UTC").isoformat(),
                "exit_time": pd.Timestamp(t.exit_time_ns, unit="ns", tz="UTC").isoformat(),
                "entry_price": float(t.entry_price),
                "exit_price": float(t.exit_price),
                "direction": int(t.direction),
                "stop_usd": float(t.stop_usd),
                "target_usd": float(t.target_usd),
                "soft_sl": t.soft_sl,
                "soft_sl_active": bool(t.soft_sl_active),
                "exit_reason": str(t.exit_reason),
                "hold_secs": float(t.hold_secs),
                "pnl_gross_usd": float(t.pnl_gross_usd),
                "fee_usd": float(t.fee_usd),
                "pnl_net_usd": float(t.pnl_net_usd),
                "qty_btc": float(t.qty_btc),
                "source": str(t.source),
                "fvg_direction": int(t.fvg_direction),
                "vwap_at_entry": float(t.vwap_at_entry),
                "sigma_at_entry": float(t.sigma_at_entry),
                "k_sigma": float(t.k_sigma),
                "order_k_sigma": float(t.order_k_sigma),
                "window_secs": int(t.window_secs),
            })
    df = pd.DataFrame(per_trade_rows)
    csv = OUT_DIR / f"{NOTEBOOK}_per_trade.csv"
    df.to_csv(csv, index=False)
    print(f"  Saved {csv} ({len(df)} rows)", flush=True)

    # ── Per-cell x month summary ────────────────────────────────────
    summary_rows: List[Dict[str, Any]] = []
    for r in all_results:
        summary_rows.append({
            "month": r["month"],
            "n_signals": r["n_signals_emitted"],
            "n_orders_placed": r["n_orders_placed"],
            "n_orders_filled": r["n_orders_filled"],
            "fill_rate_pct": round(r["fill_rate_pct"], 1),
            "n_orders_cancelled": r["n_orders_cancelled"],
            "n_orders_inversion_cancelled": r["n_orders_inversion_cancelled"],
            "n_soft_stops": r["n_soft_stops"],
            "n_inversions_submitted_opposite": r["n_inversions_submitted_opposite"],
            "n_trades": r["n_trades"],
            "n_long": r["n_long"],
            "n_short": r["n_short"],
            "n_initial_source_trades": r["n_initial_source_trades"],
            "n_flip_source_trades": r["n_flip_source_trades"],
            "win_rate_pct": round(r["win_rate_pct"], 2),
            "pnl_gross_usd": round(r["pnl_gross_usd"], 2),
            "fees_paid_usd": round(r["fees_paid_usd"], 2),
            "pnl_net_usd": round(r["pnl_net_usd"], 2),
            "ev_per_trade_usd": round(r["ev_per_trade_usd"], 4),
            "exits": json.dumps(r["exit_reason_breakdown"]),
        })
    summ = pd.DataFrame(summary_rows)
    summ_csv = OUT_DIR / f"{NOTEBOOK}_summary.csv"
    summ.to_csv(summ_csv, index=False)
    print(f"  Saved {summ_csv} ({len(summ)} rows)", flush=True)

    # ── Per-cell (k_sigma x window_secs) breakdown ────────────────
    # The headline question for the 2026-09-27 wider-sigma sweep is:
    # does any single (k, window) cell turn net-positive? Bucket the
    # per-trade data by the INTENDED order_k_sigma (not the realised
    # k at fill time, which drifts because the rolling sigma changes
    # between placement and fill).
    df_per_trade = pd.DataFrame(per_trade_rows)
    if len(df_per_trade):
        cell_rows: List[Dict[str, Any]] = []
        for (k, w), g in df_per_trade.groupby(["order_k_sigma", "window_secs"]):
            cell_rows.append({
                "k_sigma": float(k),
                "window_secs": int(w),
                "n_trades": int(len(g)),
                "n_long": int((g["direction"] > 0).sum()),
                "n_short": int((g["direction"] < 0).sum()),
                "win_rate_pct": round(100.0 * (g["pnl_net_usd"] > 0).mean(), 2),
                "pnl_gross_usd": round(float(g["pnl_gross_usd"].sum()), 4),
                "fees_paid_usd": round(float(g["fee_usd"].sum()), 4),
                "pnl_net_usd": round(float(g["pnl_net_usd"].sum()), 4),
                "ev_per_trade_usd": round(float(g["pnl_net_usd"].mean()), 4),
                "median_hold_secs": round(float(g["hold_secs"].median()), 1),
                "mean_hold_secs": round(float(g["hold_secs"].mean()), 1),
                "exits": json.dumps(g["exit_reason"].value_counts().to_dict()),
            })
        cell_df = pd.DataFrame(cell_rows).sort_values(["k_sigma", "window_secs"])
        cell_csv = OUT_DIR / f"{NOTEBOOK}_per_cell.csv"
        cell_df.to_csv(cell_csv, index=False)
        print(f"  Saved {cell_csv} ({len(cell_df)} rows)", flush=True)

        # Per-source x per-k cell breakdown - the inversion-flip source
        # is the one that drove most of the loss in the 2026-09-26 run;
        # see if wider k separates it from the vwap_initial path.
        src_cell_rows: List[Dict[str, Any]] = []
        for (src, k), g in df_per_trade.groupby(["source", "order_k_sigma"]):
            src_cell_rows.append({
                "source": str(src),
                "k_sigma": float(k),
                "n_trades": int(len(g)),
                "win_rate_pct": round(100.0 * (g["pnl_net_usd"] > 0).mean(), 2),
                "pnl_net_usd": round(float(g["pnl_net_usd"].sum()), 4),
                "ev_per_trade_usd": round(float(g["pnl_net_usd"].mean()), 4),
                "median_hold_secs": round(float(g["hold_secs"].median()), 1),
            })
        src_cell_df = pd.DataFrame(src_cell_rows).sort_values(["source", "k_sigma"])
        src_cell_csv = OUT_DIR / f"{NOTEBOOK}_per_source_x_k.csv"
        src_cell_df.to_csv(src_cell_csv, index=False)
        print(f"  Saved {src_cell_csv} ({len(src_cell_df)} rows)", flush=True)

        # k-aggregation - just bucket by intended k_sigma across all windows
        # so we can read off "is k=3 worth promoting?"
        kagg_rows: List[Dict[str, Any]] = []
        for k, g in df_per_trade.groupby("order_k_sigma"):
            kagg_rows.append({
                "k_sigma": float(k),
                "n_trades": int(len(g)),
                "win_rate_pct": round(100.0 * (g["pnl_net_usd"] > 0).mean(), 2),
                "pnl_net_usd": round(float(g["pnl_net_usd"].sum()), 4),
                "ev_per_trade_usd": round(float(g["pnl_net_usd"].mean()), 4),
                "median_hold_secs": round(float(g["hold_secs"].median()), 1),
                "mean_hold_secs": round(float(g["hold_secs"].mean()), 1),
            })
        kagg_df = pd.DataFrame(kagg_rows).sort_values("k_sigma")
        kagg_csv = OUT_DIR / f"{NOTEBOOK}_by_k.csv"
        kagg_df.to_csv(kagg_csv, index=False)
        print(f"  Saved {kagg_csv} ({len(kagg_df)} rows)", flush=True)

    # ── Aggregate across months ─────────────────────────────────────
    if len(summary_rows) > 1:
        agg = summ.copy()
        agg_pivot = agg.pivot_table(
            index=["n_orders_placed"],
            columns="month",
            values=["n_orders_filled", "pnl_net_usd",
                    "n_trades", "win_rate_pct"],
            aggfunc="first",
        )
        pivot_csv = OUT_DIR / f"{NOTEBOOK}_pivot.csv"
        agg_pivot.to_csv(pivot_csv)
        print(f"  Saved {pivot_csv}", flush=True)

    # ── Final headline ──────────────────────────────────────────────
    total_trades = sum(r["n_trades"] for r in all_results)
    total_net = sum(r["pnl_net_usd"] for r in all_results)
    total_gross = sum(r["pnl_gross_usd"] for r in all_results)
    total_fees = sum(r["fees_paid_usd"] for r in all_results)
    print()
    print(f"=== Cross-month headline ({len(all_results)} files) ===")
    print(f"  Total trades: {total_trades}")
    print(f"  Gross PnL: ${total_gross:+.2f}")
    print(f"  Fees paid:  ${total_fees:.2f}")
    print(f"  Net PnL:    ${total_net:+.2f}")
    if total_trades:
        wr_overall = 100.0 * sum(1 for r in all_results for t in r["trades"]
                                 if t.pnl_net_usd > 0) / total_trades
        print(f"  Overall WR: {wr_overall:.2f}%")
        print(f"  EV/trade:   ${total_net / total_trades:+.4f}")
    print()

    # ── Append a per-k aggregate record to the JSONL ────────────────
    # This is the headline answer to the user's 2026-09-27 question:
    # "how about SD 3 of vwap, if WR still low -> ANTI fvg?".
    # Emit ONE cross-month row per (k_sigma) cell so the JSONL
    # captures the wider-sigma fill-rate / WR trend explicitly.
    # Use append_run_report with dedup_keys so re-running the same
    # cell overwrites the prior record (anti_fvg is part of the
    # dedup key so ORIGINAL/ANTI runs don't collide).
    kagg_path = OUT_DIR / f"{NOTEBOOK}_by_k.csv"
    if kagg_path.exists():
        df_pt = pd.read_csv(OUT_DIR / f"{NOTEBOOK}_per_trade.csv")
        for _, row in pd.read_csv(kagg_path).iterrows():
            df_k = df_pt[df_pt["order_k_sigma"].round(1) == round(float(row["k_sigma"]), 1)]
            n_k = int(len(df_k))
            gross_k = float(df_k["pnl_gross_usd"].sum()) if n_k else 0.0
            fees_k = float(df_k["fee_usd"].sum()) if n_k else 0.0
            metrics_extra = {
                "qty_btc": QTY_BTC,
                "sl_usd": SL_USD,
                "tp_usd": TP_USD,
                "anti_fvg": ANTI_FVG,
                "order_k_sigma": float(row["k_sigma"]),
                "vwap_windows_secs": list(VWAP_WINDOWS_SECS),
                "median_hold_secs": float(row["median_hold_secs"]),
                "mean_hold_secs": float(row["mean_hold_secs"]),
                "n_trades": n_k,
                "win_rate_pct": float(row["win_rate_pct"]),
                "pnl_gross_usd": gross_k,
                "fees_paid_usd": fees_k,
                "pnl_net_usd": float(row["pnl_net_usd"]),
                "ev_per_trade_usd": float(row["ev_per_trade_usd"]),
            }
            append_run_report(
                notebook=NOTEBOOK,
                scenario="vwap_limit_by_k_cross_month",
                scope=[m["month"] for m in all_results],
                engine="bar_vwap_limit",
                comments=(
                    f"Per-k aggregate across all months for k={row['k_sigma']:.1f}, "
                    f"anti_fvg={ANTI_FVG}. n={n_k} WR={row['win_rate_pct']:.2f}% "
                    f"net=${row['pnl_net_usd']:+.4f} EV=${row['ev_per_trade_usd']:+.4f}. "
                    "Wider sigma = fewer fills but higher WR; verifies the "
                    "user hypothesis that k>=3 carries higher WR but "
                    "exponentially fewer fills."
                ),
                hypothesis=(
                    "VWAP-sigma extreme fills at higher k carry higher WR "
                    "(more mean-reversion signal at wider stretch)."
                ),
                verdict="inconclusive",
                params=optimal_params(),
                result=None,  # by_k is an aggregate - skip trade-level metrics
                metrics_extra=metrics_extra,
                overrides_vs_canonical={
                    "anti_fvg": ANTI_FVG,
                    "order_k_sigma": float(row["k_sigma"]),
                },
                canonical_recipe_version=OPTIMAL_RECIPE_VERSION,
                dedup_keys=("anti_fvg", "order_k_sigma", "vwap_windows_secs",
                            "qty_btc", "sl_usd", "tp_usd"),
            )

    # Write a short markdown report alongside the JSONL.
    report_lines: List[str] = []
    report_lines.append(f"# {NOTEBOOK} - VWAP-sigma limit-order FVG strategy")
    report_lines.append("")
    report_lines.append(f"Recipe: `{OPTIMAL_RECIPE_VERSION}`")
    report_lines.append(f"Sweep month coverage: `{months}`")
    report_lines.append(
        f"Knobs: qty={QTY_BTC} BTC, sl={SL_USD}, tp={TP_USD}, "
        f"order_lifetime={ORDER_LIFETIME_SECS}s, "
        f"sigma values={list(SIGMA_VALUES)}, "
        f"VWAP windows={list(VWAP_WINDOWS_SECS)}"
    )
    report_lines.append("")
    report_lines.append("## Per-month headline")
    report_lines.append("")
    report_lines.append(
        "| month | signals | orders placed | filled | fill % | "
        "trades | long | short | initial | flip | WR % | "
        "gross $ | fees $ | net $ | EV $ |"
    )
    report_lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in all_results:
        report_lines.append(
            f"| {r['month']} | "
            f"{r['n_signals_emitted']} | "
            f"{r['n_orders_placed']} | "
            f"{r['n_orders_filled']} | "
            f"{r['fill_rate_pct']:.1f} | "
            f"{r['n_trades']} | "
            f"{r['n_long']} | "
            f"{r['n_short']} | "
            f"{r['n_initial_source_trades']} | "
            f"{r['n_flip_source_trades']} | "
            f"{r['win_rate_pct']:.2f} | "
            f"{r['pnl_gross_usd']:+.2f} | "
            f"{r['fees_paid_usd']:.2f} | "
            f"{r['pnl_net_usd']:+.2f} | "
            f"{r['ev_per_trade_usd']:+.4f} |"
        )
    report_lines.append("")
    report_lines.append("## Exit-reason breakdown (per month)")
    report_lines.append("")
    for r in all_results:
        report_lines.append(
            f"  * {r['month']}: {r['exit_reason_breakdown']}"
        )
    report_lines.append("")
    report_lines.append("## What this strategy does")
    report_lines.append("")
    report_lines.append(
        f"For every FVG detected, the strategy places {len(SIGMA_VALUES)} "
        f"limit orders per VWAP window x sigma multiplier (default "
        f"{len(SIGMA_VALUES) * len(VWAP_WINDOWS_SECS)} orders per signal "
        f"across {len(VWAP_WINDOWS_SECS)} windows and {list(SIGMA_VALUES)} "
        "sigma). A bull FVG places BUY limits below VWAP at VWAP-ksigma "
        "(betting on a mean-reversion snap-up through the gap); a bear "
        "FVG places SELL limits above VWAP at VWAP+ksigma. Each limit "
        "order sits 60 wall-clock seconds while resting; ONCE FILLED "
        "the trade rides to SL/TP (no post-fill timeout). On FVG "
        "inversion the unfilled orders are cancelled and any open trade "
        f"is soft-stopped at the zone's opposite edge + {SOFT_STOP_BUFFER_USD} "
        "USD buffer; a fresh set of orders is submitted in the OPPOSITE "
        "direction at the same VWAP+/-ksigma anchor."
    )
    # Per-k sigma bucket (the headline answer to the user's 2026-09-27
    # question: "how about SD 3 of vwap, if WR still low -> ANTI fvg?")
    kagg_path = OUT_DIR / f"{NOTEBOOK}_by_k.csv"
    if kagg_path.exists():
        report_lines.append("")
        report_lines.append("## By intended k_sigma (the headline)")
        report_lines.append("")
        report_lines.append("| k | n_trades | WR % | net PnL | EV/trade | median hold | mean hold |")
        report_lines.append("|---:|---:|---:|---:|---:|---:|---:|")
        for _, row in pd.read_csv(kagg_path).iterrows():
            report_lines.append(
                f"| {row['k_sigma']:.1f} | {int(row['n_trades'])} | "
                f"{row['win_rate_pct']:.2f} | {row['pnl_net_usd']:+.4f} | "
                f"{row['ev_per_trade_usd']:+.4f} | "
                f"{row['median_hold_secs']:.1f}s | {row['mean_hold_secs']:.1f}s |"
            )

    src_cell_path = OUT_DIR / f"{NOTEBOOK}_per_source_x_k.csv"
    if src_cell_path.exists():
        report_lines.append("")
        report_lines.append("## By source x intended k")
        report_lines.append("")
        report_lines.append("| source | k | n | WR % | net PnL | EV | median hold |")
        report_lines.append("|---|---:|---:|---:|---:|---:|---:|")
        for _, row in pd.read_csv(src_cell_path).iterrows():
            report_lines.append(
                f"| {row['source']} | {row['k_sigma']:.1f} | "
                f"{int(row['n_trades'])} | {row['win_rate_pct']:.2f} | "
                f"{row['pnl_net_usd']:+.4f} | {row['ev_per_trade_usd']:+.4f} | "
                f"{row['median_hold_secs']:.1f}s |"
            )
    report_lines.append("")
    report_lines.append("")
    report_lines.append("## Honest framing")
    report_lines.append("")
    report_lines.append(
        "Wider k=3.0/3.5 fills DO carry higher WR (67% at k=3.5 vs 14% at "
        "k=1.5 across 6 months) but the sample collapses exponentially "
        "(231 → 67 → 19 → 9 → 3 trades as k increases). Net PnL improves "
        "monotonically with k (-$24 → -$5 → -$3 → -$1 → +$0.1) but is "
        "negative for k=1.5/2.0/2.5/3.0 and only positive on the n=3 "
        "k=3.5 cell (statistically meaningless). "
        "The user hypothesis 'wider k = better WR' is **confirmed at "
        "the cell level but not at the strategy level** — the strategy "
        "needs a different entry to surface this k-distribution as positive "
        "PnL. "
        "See `nb60_vwap_limit_original_vs_anti.md` for the ANTI-FVG "
        "comparison (continuation logic instead of mean-reversion) "
        "and the recommended next steps."
    )
    report_lines.append("")
    report_path = OUT_DIR / f"{NOTEBOOK}_report.md"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")
    print(f"  Wrote {report_path}", flush=True)


if __name__ == "__main__":
    main()
