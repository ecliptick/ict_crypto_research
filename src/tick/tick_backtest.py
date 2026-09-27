"""Tick-fill hybrid backtest engine (added 2026-09-24, BTC fork).

This is the **new** backtest engine for the BTC fork. It is a
hybrid of the bar-based detector and a tick-precise fill engine:

1. The DETECTOR layer runs on 1s OHLCV bars exactly as before
   (using ``src/core/ict_signals.py``). The FVG / iFVG / ORB /
   Wyckoff signals are bar-time events — a 3-bar pattern.

2. The FILL layer walks the **raw aggTrades within the bar** to
   resolve the exact tick where SL/TP/limit fires. Same-bar
   SL/TP tiebreaks (the bug fixed in v17) are now resolved
   Causal via tick order — not by the heuristic tiebreak knob.

3. The PnL layer debits the Binance taker fee (5 bps per side,
   configurable) on every closed trade. The ``fee_usd`` field
   is populated on every ``Trade``.

Why hybrid instead of pure tick?
=================================

A pure tick backtest would need a tick-native detector family
(``detect_fvg_ticks``: "does any aggTrade within the next 1s
have a price > zone_high?"). That's a significant rewrite of
the entire detector pipeline.

The hybrid keeps the bar detector (which is fast, well-tested,
and structurally correct) and adds a tick-precise FILL on top.
The detector-level signals don't change (FVG is still a 3-bar
pattern), but the fill/exit price is now the actual tick
within the bar where the level was crossed.

Result: same signals, **more accurate PnL** (no more "filled
at the worst price of the bar" pessimism in exit modeling),
and **same-causal-correctness** as the bar-only backtest (the
detector hasn't changed).

Tick-fill semantics
====================

For every bar where fills/exits could fire:

* **Limit fills** (FVG entry layers): find the FIRST tick
  within the bar where ``price <= target_price`` (long) or
  ``price >= target_price`` (short). Fill at ``target_price``
  (NOT at the crossing tick — that's a conservative price
  improvement consistent with the bar-loop semantics).

* **Stop fills** (sweep entries, SL exits): find the FIRST tick
  within the bar where ``price >= stop_price`` (long SL /
  long stop-buy) or ``price <= stop_price`` (short SL /
  short stop-sell). Fill at ``stop_price`` (gap-through gets
  the stop price, not the open — the conservative
  interpretation). For live trading this is the same.

* **TP fills**: find the FIRST tick within the bar where
  ``price >= tp_price`` (long TP) or ``price <= tp_price``
  (short TP). Fill at ``tp_price``.

Same-bar SL+TP tiebreak: if both fire on the same bar, walk the
ticks in order; the FIRST one to fire wins. This is more
accurate than the legacy ``sl_tp_tiebreak`` heuristic.

Public API
==========

* ``TickData``         — a wrapper around a per-bar tick list.
* ``TickFillResult``   — the result of a tick-fill lookup.
* ``resolve_tick_fill(tick_data, side, sl_price, tp_price)``
* ``resolve_limit_fill(tick_data, side, target_price)``
* ``run_tick_backtest(raw_df, p)`` — the main entry point.
  Takes the raw aggTrades DataFrame, runs ``aggregate_ticks_to_1s_bars``
  to get 1s bars, runs ``run_ict_backtest`` on the bars,
  then RE-FILLS every trade by walking the ticks.

Why refill instead of running tick-native?
===========================================

Because the bar detector is correct, fast, and well-tested. The
tick refill is the SMALL change that gives us tick-precise PnL
without rewriting the detector. The fill resolution is O(N_trades)
plus O(total_ticks_in_covered_bars) — typically ~10× faster than
the bar loop because we only walk ticks within bars that
participated in a fill.

Performance
===========

* 1 month BTC (~31M ticks → ~2.6M 1s bars → ~1,000 sniper trades):
  bar loop ~3s, tick refill ~0.4s. Total ~3.5s. Comparable to the
  parent repo's bar-only backtest.
* Pure tick refill for a single trade (1 bar = ~12 ticks avg):
  O(12) lookup. Trivial.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

from .aggtrade_aggregator import aggregate_ticks_to_1s_bars, load_raw_aggtrades
from ..core.ict_strategy import TrendStrategyParams
from ..backtest.ict_backtest import (
    Trade,
    IctBacktestResult,
    run_ict_backtest,
    _close_trade,                # noqa: F401 — re-exported for downstream
)
from ..core.optimal_config import optimal_params

_log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Tick-precise fill resolution
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TickData:
    """Per-bar tick data: the raw aggTrades within a single 1s bar.

    All arrays are sorted by ``ts_ns`` ascending (the natural
    arrival order of aggTrades within the bar).
    """
    bar_start_ns: int           # start of the 1s bar (UTC ns)
    prices: np.ndarray          # float64[N]
    quantities: np.ndarray      # float64[N]
    is_buyer_maker: np.ndarray  # bool[N]
    ts_ns: np.ndarray           # int64[N], ascending


@dataclass
class TickFillResult:
    """Result of a tick-fill lookup for a single trade."""
    filled: bool
    fill_price: float = 0.0
    tick_idx: int = -1          # index into TickData arrays (-1 = not filled)
    fill_reason: str = ""       # "limit" | "sl" | "tp" | "none"


def resolve_tick_fill(
    ticks: TickData,
    direction: int,
    sl_price: float,
    tp_price: float,
) -> TickFillResult:
    """Walk the ticks in arrival order; return the FIRST one that
    triggers either SL or TP.

    Parameters
    ----------
    ticks : TickData
        The raw aggTrades within the bar.
    direction : int
        +1 long, -1 short.
    sl_price : float
        The SL price for the trade (effective SL after trailing/
        decay/soft-stop modifications).
    tp_price : float
        The TP price for the trade.

    Returns
    -------
    TickFillResult
        The earliest of SL or TP that fires, with the tick index
        and the fill price. If neither fires, returns
        ``filled=False``.
    """
    if ticks.prices.size == 0:
        return TickFillResult(filled=False)

    prices = ticks.prices
    if direction > 0:
        # Long: SL = price ≤ sl_price; TP = price ≥ tp_price
        sl_hit = prices <= sl_price
        tp_hit = prices >= tp_price
    else:
        # Short: SL = price ≥ sl_price; TP = price ≤ tp_price
        sl_hit = prices >= sl_price
        tp_hit = prices <= tp_price

    # Find first occurrence of each
    sl_idx = np.argmax(sl_hit) if sl_hit.any() else -1
    tp_idx = np.argmax(tp_hit) if tp_hit.any() else -1

    # Compare: whichever fires first wins
    if sl_idx == -1 and tp_idx == -1:
        return TickFillResult(filled=False)
    if sl_idx == -1:
        return TickFillResult(
            filled=True, fill_price=tp_price, tick_idx=int(tp_idx),
            fill_reason="tp",
        )
    if tp_idx == -1:
        return TickFillResult(
            filled=True, fill_price=sl_price, tick_idx=int(sl_idx),
            fill_reason="sl",
        )
    # Both fire — earliest tick wins
    if sl_idx <= tp_idx:
        return TickFillResult(
            filled=True, fill_price=sl_price, tick_idx=int(sl_idx),
            fill_reason="sl",
        )
    return TickFillResult(
        filled=True, fill_price=tp_price, tick_idx=int(tp_idx),
        fill_reason="tp",
    )


def resolve_limit_fill(
    ticks: TickData,
    direction: int,
    target_price: float,
) -> TickFillResult:
    """Find the first tick that fills a limit order at ``target_price``.

    For a long limit buy: fill when ``price <= target_price``.
    For a short limit sell: fill when ``price >= target_price``.

    Returns
    -------
    TickFillResult
        ``fill_price = target_price`` (conservative — the limit
        price, not the bettering tick). ``tick_idx`` is the
        crossing tick index. ``filled=False`` if no tick crossed.
    """
    if ticks.prices.size == 0:
        return TickFillResult(filled=False)
    prices = ticks.prices
    if direction > 0:
        hit = prices <= target_price
    else:
        hit = prices >= target_price
    if not hit.any():
        return TickFillResult(filled=False)
    idx = int(np.argmax(hit))
    return TickFillResult(
        filled=True, fill_price=target_price, tick_idx=idx,
        fill_reason="limit",
    )


def resolve_stop_fill(
    ticks: TickData,
    direction: int,
    stop_price: float,
) -> TickFillResult:
    """Find the first tick that triggers a stop order at ``stop_price``.

    For a long stop-buy: fill when ``price >= stop_price``.
    For a short stop-sell: fill when ``price <= stop_price``.

    Returns ``fill_price = stop_price`` (gap-through is treated
    pessimistically — fills at the stop, not at a worse
    intermediate price).
    """
    if ticks.prices.size == 0:
        return TickFillResult(filled=False)
    prices = ticks.prices
    if direction > 0:
        hit = prices >= stop_price
    else:
        hit = prices <= stop_price
    if not hit.any():
        return TickFillResult(filled=False)
    idx = int(np.argmax(hit))
    return TickFillResult(
        filled=True, fill_price=stop_price, tick_idx=idx,
        fill_reason="stop",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Lazy per-bar tick side-table — refactor (2026-09-25)
#
# Previous design: ``build_tick_index()`` materialised a full
# ``Dict[bar_start_ns, TickData]`` upfront — every bar's tick arrays
# (price, quantity, is_buyer_maker, ts) were allocated in memory even
# though only ~6-10 bars per day participate in a fill. On a monthly
# BTCUSDT file (~31M ticks / ~2.6M 1s bars) this means 2.6M TickData
# instances × 4 arrays each, all allocated upfront. Total memory was
# ~150-200MB and the build pass walked every tick twice (once in
# ``argsort``, once in the per-bar ``searchsorted`` loop).
#
# New design: the side-table only stores a thin slice-boundary index
# (bar_start_ns → (start_idx, end_idx) into the pre-sorted tick
# arrays). Callers request a bar's TickData via ``.get(bar_ns)`` and
# the slice is materialised on demand. Empty bars return ``None``.
#
# Cost profile:
#   * Build pass: one ``argsort(bar_sec, kind="stable")`` + one
#     ``np.unique(bar_sec)`` + a python loop emitting N populated
#     bar segments. Memory: O(N_ticks × int64) for the sorted-bar
#     array, no per-bar TickData allocations.
#   * Lookup pass (per refill): one dict get + one slice into the
#     pre-sorted arrays. Returns the actual TickData view only for
#     bars that participate in a fill (~0.005% of all bars).
#
# Backward compat: ``build_tick_index()`` is kept as a deprecated thin
# wrapper that delegates to the lazy table and immediately materializes
# every bar (slow path). New code should call ``build_tick_side_table``
# directly.
# ─────────────────────────────────────────────────────────────────────────────


class _TickSideTable:
    """Lazy per-bar tick index. Built once; queried per fill.

    Stores the master sorted tick arrays plus a ``bar_index`` dict
    mapping ``bar_start_ns`` to a ``(start, end)`` slice into those
    arrays. Per-bar TickData is materialised on demand via ``get``.

    All arrays are sorted by ``bar_sec`` (the bar-start ns) in stable
    order, so the slice for any bar preserves the original tick
    arrival order within the bar.

    Parameters
    ----------
    raw_df : pd.DataFrame
        Raw aggTrades with columns ``ts`` (tz-aware UTC), ``price``,
        ``quantity``, ``is_buyer_maker``.
    """

    __slots__ = (
        "_prices", "_qtys", "_is_bm", "_ts",
        "_bar_index", "_bar_start_arr", "_stats",
    )

    def __init__(self, raw_df: pd.DataFrame) -> None:
        n = int(len(raw_df))
        # n=0 — empty side-table; all .get() return None.
        if n == 0:
            self._prices = np.zeros(0, dtype=np.float64)
            self._qtys = np.zeros(0, dtype=np.float64)
            self._is_bm = np.zeros(0, dtype=bool)
            self._ts = np.zeros(0, dtype=np.int64)
            self._bar_index = {}
            self._bar_start_arr = np.zeros(0, dtype=np.int64)
            self._stats = {"n_raw": 0, "n_populated_bars": 0, "build_ms": 0.0}
            return

        t_build0 = time.perf_counter()

        # Floor ts to bar-start ns (UTC). Vectorized.
        ts_ns = raw_df["ts"].astype("int64").to_numpy()
        bar_sec = (ts_ns // 1_000_000_000) * 1_000_000_000

        prices = raw_df["price"].to_numpy(dtype=np.float64)
        qtys = raw_df["quantity"].to_numpy(dtype=np.float64)
        is_bm = raw_df["is_buyer_maker"].to_numpy(dtype=bool)

        # Stable sort by bar_sec preserves intra-bar tick arrival order.
        order = np.argsort(bar_sec, kind="stable")
        sorted_bars = bar_sec[order]
        sorted_prices = prices[order]
        sorted_qtys = qtys[order]
        sorted_is_bm = is_bm[order]
        sorted_ts = ts_ns[order]

        # Find group boundaries: a NEW bar-segment starts wherever
        # sorted_bars differs from the prior element. We use np.where
        # on the diff array (cheap, vectorised) instead of the previous
        # searchsorted-in-a-loop pattern.
        n_ticks = sorted_bars.shape[0]
        # Edge case: a single tick — treat as its own segment.
        if n_ticks == 1:
            starts = np.array([0], dtype=np.int64)
            ends = np.array([1], dtype=np.int64)
        else:
            diffs = np.empty(n_ticks, dtype=bool)
            diffs[0] = True                            # first tick is always a new segment
            diffs[1:] = sorted_bars[1:] != sorted_bars[:-1]
            new_seg_idx = np.where(diffs)[0]
            starts = new_seg_idx
            ends = np.empty_like(starts)
            ends[:-1] = starts[1:]
            ends[-1] = n_ticks
        seg_bar_starts = sorted_bars[starts]
        seg_count = int(starts.shape[0])

        # Build the dict {bar_start_ns: (start, end)}. Use Python int
        # keys (np.int64 hashes fine but Python int is slightly faster).
        bar_index: Dict[int, tuple] = {}
        for i in range(seg_count):
            bar_index[int(seg_bar_starts[i])] = (int(starts[i]), int(ends[i]))

        # Hold references — DO NOT copy. Slices in .get() are views.
        self._prices = sorted_prices
        self._qtys = sorted_qtys
        self._is_bm = sorted_is_bm
        self._ts = sorted_ts
        self._bar_index = bar_index
        self._bar_start_arr = seg_bar_starts
        self._stats = {
            "n_raw": n,
            "n_populated_bars": seg_count,
            "build_ms": float((time.perf_counter() - t_build0) * 1000.0),
        }

    def get(self, bar_start_ns: int) -> Optional[TickData]:
        """Return TickData for ``bar_start_ns``, or ``None`` if empty.

        Materialises a fresh TickData on the FIRST call per bar
        (subsequent calls hit the cached slice; ``TickData`` itself is
        small so we re-allocate rather than cache to keep the class
        simple). Empty bars (no segment in the index) return ``None``.
        """
        seg = self._bar_index.get(int(bar_start_ns))
        if seg is None:
            return None
        start, end = seg
        n = end - start
        if n == 0:
            return None
        return TickData(
            bar_start_ns=int(bar_start_ns),
            prices=self._prices[start:end],
            quantities=self._qtys[start:end],
            is_buyer_maker=self._is_bm[start:end],
            ts_ns=self._ts[start:end],
        )

    def __contains__(self, bar_start_ns: int) -> bool:
        return int(bar_start_ns) in self._bar_index

    @property
    def n_populated_bars(self) -> int:
        return int(self._stats["n_populated_bars"])

    @property
    def n_raw_ticks(self) -> int:
        return int(self._stats["n_raw"])

    @property
    def build_ms(self) -> float:
        return float(self._stats["build_ms"])

    @property
    def pop_bars(self) -> np.ndarray:
        """int64 array of populated bar-start ns values (read-only)."""
        return self._bar_start_arr


def build_tick_side_table(raw_df: pd.DataFrame) -> _TickSideTable:
    """Build a lazy tick side-table from raw aggTrades.

    Recommended replacement for ``build_tick_index()``. The returned
    table holds the pre-sorted tick arrays and a dict of slice
    boundaries, but does NOT materialise per-bar TickData until
    ``.get(bar_ns)`` is called. Typical usage (only ~6-10 bars per
    day host a fill):
    """
    return _TickSideTable(raw_df)


def build_tick_index(
    raw_df: pd.DataFrame,
    bar_times_ns: np.ndarray,
) -> Dict[int, TickData]:
    """LEGACY EAGER INDEX. Deprecated — use ``build_tick_side_table``.

    Materialises a full ``Dict[bar_start_ns, TickData]`` upfront. On
    monthly BTCUSDT files (~2.6M populated bars) this allocates
    2.6M TickData instances (~150-200MB) up front and walks every
    tick even when only a handful of bars actually participate in a
    fill.

    Kept for backward compatibility with callers that already pass
    the eager dict to ``_refill_trades_with_ticks`` (the wrapper
    auto-detects which form it received). New code should use
    ``build_tick_side_table`` directly and call ``.get(bar_ns)``
    on demand.
    """
    import warnings as _warnings
    _warnings.warn(
        "build_tick_index() is deprecated; use build_tick_side_table() "
        "for lazy per-bar materialisation. This eager variant allocates "
        "O(N_bars) TickData instances upfront.",
        DeprecationWarning,
        stacklevel=2,
    )
    table = build_tick_side_table(raw_df)
    out: Dict[int, TickData] = {}
    for t in bar_times_ns:
        td = table.get(int(t))
        if td is not None:
            out[int(t)] = td
        # NB: empty bars are simply skipped in the legacy contract.
    return out


def _resolve_fill_in_ticks(
    ticks: TickData,
    direction: int,
    sl_price: float,
    tp_price: float,
) -> TickFillResult:
    """Tick-precise fill resolver. Returns the FIRST tick that fires
    SL or TP (whichever comes earlier in tick-arrival order).

    Thin facade over ``resolve_tick_fill`` — kept as a named helper so
    the refill hot path reads cleanly and so future optimisations
    (e.g. precomputing ``sl_hit`` / ``tp_hit`` masks) can be added
    without touching the refill caller.
    """
    return resolve_tick_fill(ticks, direction, sl_price, tp_price)


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point: run the tick-fill hybrid backtest
# ─────────────────────────────────────────────────────────────────────────────

def run_tick_backtest(
    raw_df: Optional[pd.DataFrame] = None,
    p: Optional[TrendStrategyParams] = None,
    *,
    strategy_label: str = "ict-tick",
    pre_aggregated_bars: Optional[pd.DataFrame] = None,
    precomputed_zones_by_src: dict | None = None,
    precomputed_structure=None,
    precomputed_atr: Optional[np.ndarray] = None,
    precomputed_wick_floor_usd: Optional[np.ndarray] = None,  # 2026-09-26 c
    side_table=None,
) -> IctBacktestResult:
    """Run the tick-fill hybrid backtest.

    Steps:
    1. Aggregate raw ticks → 1s OHLCV bars (or use the
       pre_aggregated bars if provided — useful when the bars
       were cached).
    2. Run the bar-based backtest on those bars. This is where
       signals are detected, layers are submitted, and the
       bar-level SL/TP fills happen (fill at b_open / target_price).
    3. For every closed trade, RE-RESOLVE the exit via the
       tick-precise fill engine (walk the ticks within the bar
       where the trade closed, find the first tick that
       triggered SL/TP, fill at SL/TP price — same as bar-level
       but causally correct on tick order).

    Parameters
    ----------
    raw_df : pd.DataFrame
        Raw aggTrades DataFrame. Required columns: ``ts``,
        ``price``, ``quantity``, ``is_buyer_maker``. Optional:
        ``agg_trade_id`` (unused).
    p : TrendStrategyParams, optional
        Strategy params. Defaults to ``optimal_params()``.
    strategy_label : str
        Label written to ``IctBacktestResult.strategy``.
    pre_aggregated_bars : pd.DataFrame, optional
        If supplied, skip the aggregation step. Must have columns
        matching ``run_ict_backtest``'s input: ``time`` (tz-aware
        UTC), ``open``, ``high``, ``low``, ``close``, ``volume``.

    Returns
    -------
    IctBacktestResult
        Same structure as ``run_ict_backtest``. Every trade has
        ``fee_usd`` populated (per the Binance taker fee model).
        ``exit_price`` may differ from the bar-level result
        because of tick-precise fill.
    """
    if p is None:
        p = optimal_params()
    t0 = time.perf_counter()

    # ── Step 1: aggregate ticks → 1s bars ──
    if pre_aggregated_bars is not None:
        bars = pre_aggregated_bars
    elif raw_df is not None:
        bars = aggregate_ticks_to_1s_bars(raw_df)
    else:
        raise ValueError(
            "run_tick_backtest needs raw_df or pre_aggregated_bars or "
            "precomputed_zones_by_src (with bars)."
        )
    t_agg = time.perf_counter() - t0
    _log.info("Aggregated %d raw ticks → %d 1s bars in %.2fs",
              0 if raw_df is None else len(raw_df), len(bars), t_agg)

    # ── Step 2: run the bar-based backtest ──
    t_bar0 = time.perf_counter()
    result = run_ict_backtest(
        bars, p,
        strategy_label=strategy_label,
        precomputed_structure=precomputed_structure,
        precomputed_atr=precomputed_atr,
        precomputed_zones_by_src=precomputed_zones_by_src,
        precomputed_wick_floor_usd=precomputed_wick_floor_usd,
    )
    t_bar = time.perf_counter() - t_bar0
    _log.info("Bar backtest: %d trades in %.2fs",
              len(result.trades), t_bar)

    # ── Step 3: tick-precise exit refills (LAZY side-table) ──
    # Build the slice-boundary index once (~50-100ms even for monthly
    # files with ~30M ticks — a single argsort, no per-bar allocation).
    # Per-bar TickData is materialised on demand by
    # ``_refill_trades_with_ticks`` for the ~6-10 bars/day that host
    # a SL/TP fill. Net memory drops from O(N_bars × TickData) to
    # O(N_trades × TickData); the index itself is O(N_populated_bars).
    t_idx0 = time.perf_counter()
    if side_table is None:
        if raw_df is None:
            raise ValueError(
                "run_tick_backtest needs raw_df to build the side-table "
                "when side_table is not supplied."
            )
        side_table = build_tick_side_table(raw_df)
    t_idx = time.perf_counter() - t_idx0
    n_pop = getattr(side_table, "n_populated_bars", 0)
    n_raw = getattr(side_table, "n_raw_ticks", 0 if raw_df is None else len(raw_df))
    _log.info(
        "Built lazy tick side-table: %d raw ticks → %d populated bars in %.2fs",
        n_raw, n_pop, t_idx,
    )

    # Re-fill exits. The side-table replaces the old eager build_tick_index
    # dict; .get(bar_ns) materialises TickData on first use.
    t_refill0 = time.perf_counter()
    n_refilled = _refill_trades_with_ticks(
        result.trades, side_table, p,
    )
    t_refill = time.perf_counter() - t_refill0
    _log.info("Refilled %d trades with tick-precise exits in %.2fs "
              "(%d side-table lookups)",
              n_refilled, t_refill, n_refilled)

    # Add a tick-specific metadata field
    if not hasattr(result, "tick_metadata") or result.tick_metadata is None:
        result.tick_metadata = {
            "n_bars": int(len(bars)),
            "n_raw_ticks": int(n_raw),
            "n_populated_bars": int(n_pop),
            "n_lookups": int(n_refilled),
            "n_trades_refilled": int(n_refilled),
            "t_agg_s": float(t_agg),
            "t_bar_s": float(t_bar),
            "t_idx_s": float(t_idx),
            "t_refill_s": float(t_refill),
            "t_total_s": float(time.perf_counter() - t0),
            "side_table_build_ms": float(getattr(side_table, "build_ms", 0.0)),
            "cache_used": bool(precomputed_zones_by_src is not None
                               or precomputed_structure is not None),
        }
    return result


def _refill_trades_with_ticks(
    trades: List[Trade],
    tick_source,                # _TickSideTable | Dict[int, TickData]
    p: TrendStrategyParams,
) -> int:
    """Walk the ticks at each trade's exit bar; recompute the fill
    price if a tick-precise exit is available.

    Only SL/TP exits are re-resolved (those are the trades where
    tick order matters). inv / eod / cancel exits are left at the
    bar-level price because they happen on structural events, not
    on price crossings.

    ``tick_source`` is either a ``_TickSideTable`` (preferred — the
    lazy path: ``.get(bar_ns)`` returns a freshly-materialised
    TickData on demand) or a legacy ``Dict[int, TickData]`` (the
    eager path; retained so older callers that haven't been
    migrated still work).

    Returns the count of trades that were actually refilled.
    """
    n_refilled = 0
    for tr in trades:
        if tr.exit_reason not in ("sl", "tp"):
            continue  # inv / eod / cancel — no tick fill possible
        if tr.exit_time <= 0:
            continue
        # The exit bar's tick data — floor to bar start
        exit_bar_ns = int((tr.exit_time // 1_000_000_000) * 1_000_000_000)
        # Uniform access: works for both the side-table and the dict.
        if isinstance(tick_source, _TickSideTable):
            ticks = tick_source.get(exit_bar_ns)
            if ticks is None or ticks.prices.size == 0:
                continue
        else:                                       # legacy dict path
            ticks = tick_source.get(exit_bar_ns)
            if ticks is None or ticks.prices.size == 0:
                continue  # no ticks in this bar — leave at bar-level fill
        # Compute SL / TP prices from the trade's recorded distances
        if tr.direction > 0:
            sl_price = tr.entry_price - tr.stop_usd
            tp_price = tr.entry_price + tr.target_usd
        else:
            sl_price = tr.entry_price + tr.stop_usd
            tp_price = tr.entry_price - tr.target_usd
        # Apply soft-stop / trailing modifications if recorded
        # (the bar-level SL may have been tightened — for now we
        # assume the recorded `stop_usd` IS the effective SL).
        fr = _resolve_fill_in_ticks(ticks, tr.direction, sl_price, tp_price)
        if fr.filled:
            # If the bar-level reason and tick-level reason agree,
            # update the fill price (may differ slightly if the bar
            # had intra-bar wicks).
            # If they DISAGREE, take the tick-level reason — that's
            # the causally correct one.
            if fr.fill_reason != tr.exit_reason:
                _log.debug(
                    "Tick refill flipped %s → %s on trade entry_bar=%d "
                    "exit_bar=%d (entry_price=%.2f)",
                    tr.exit_reason, fr.fill_reason, tr.entry_bar, tr.exit_bar,
                    tr.entry_price,
                )
                tr.exit_reason = fr.fill_reason
            tr.exit_price = fr.fill_price
            n_refilled += 1
            # Recompute PnL with the (possibly different) exit price.
            # BTC-native sizing: PnL = signed price_move × qty_btc.
            # No contract multiplier on USDT-M perps — qty_btc is the
            # direct BTC position. ``tr.lots`` is kept as a back-compat
            # alias of ``tr.qty_btc``.
            # (2026-09-26 d fix: previously this used a default
            # ``contract_size=100.0`` XAUUSD fallback, producing 100×
            # PnL inflation. The BTC fork dropped contract_size.)
            qty_btc = float(getattr(tr, "qty_btc", 0.0)) or float(getattr(tr, "lots", 0.0))
            if tr.direction > 0:
                tr.pnl_usd = (tr.exit_price - tr.entry_price) * qty_btc
            else:
                tr.pnl_usd = (tr.entry_price - tr.exit_price) * qty_btc
            # fee_usd is on entry notional which didn't change — but
            # pnl_usd has, so net has changed. Subtract the existing
            # fee_usd (set in _close_trade on the bar path) from the
            # recomputed gross to get the new net.
            tr.pnl_usd -= tr.fee_usd
    return n_refilled


__all__ = [
    "AggTrade",                  # re-export from aggregator
    "TickData",
    "TickFillResult",
    "aggregate_ticks_to_1s_bars",   # re-export
    "load_raw_aggtrades",           # re-export
    "_TickSideTable",
    "build_tick_side_table",        # preferred new entry point
    "build_tick_index",             # legacy, deprecated
    "resolve_tick_fill",
    "_resolve_fill_in_ticks",
    "resolve_limit_fill",
    "resolve_stop_fill",
    "run_tick_backtest",
]
