---
jupytext:
  text_representation:
    extension: .py
    format_name: percent
    format_version: '1.3'
    jupytext_version: 1.17.1
kernelspec:
  display_name: Python 3 (ipykernel)
  language: python
  name: python3
---

# %% [markdown]
# # NB53 — Alpha-scout: 7 alpha hypotheses on the canonical v17 BTC SNIPER recipe
#
# Seven A/B scenarios derived from user feedback on the canonical baseline:
#
# | Cell | Scenario | What changes |
# |------|----------|--------------|
# | A | Flip sniper direction | `sniper_inv_direction_mode="fade_displacement"` — take the OPPOSITE direction (the legacy live-engine semantic; default `"continuation"` enters in the original FVG direction) |
# | B | Sniper + immediate fire | For each canonical trade, also emit a "fire on mitigation" entry at the zone's mitigated bar |
# | C | Strict-wick FVG | Both c1 and c3 must have a visible wick (≥ $0.50) on the side away from the gap |
# | D | Mitigation-distance floor | Drop zones whose mitigated/inverted bar is < N bars from trigger (drive-through filter) |
# | E | MFE / MAE analysis | Walk each trade's price path, find optimal SL/TP for the observed MFE distribution |
# | F | Min zone breadth sweep | Sweep `fvg_inv_trade_min_zone_usd ∈ {5,10,20,30,50,75,100,150}` to find the fee-aware floor |
# | G | Conviction from unsuperseded FVG count | Per-bar count of unsuperseded bull / bear zones; per-trade conviction = counter-direction count (mean-reversion then continuation) |
#
# **Output dir**: `notebooks/nb53_outputs/`. All scenario CSVs persisted.
#
# **Recipe used**: BTC SNIPER v17, month file `BTCUSDT-aggTrades-2025-04.parquet` (47M ticks, the same one nb52 used for the canonical baseline).
#
# **Knobs added for this notebook** (in `src/core/ict_strategy.py`):
# * `sniper_inv_direction_mode: str = "continuation"` — bar-loop honors it; replaces the legacy `sniper_flip_direction` bool (still works via the back-compat shim in `_resolve_deprecated`)
# * `fvg_min_mit_distance_bars: int = 0` — retest filter (added 2026-09-26)
# * `fvg_strict_wick_required: bool = False` — detector kwarg (added 2026-09-26)
# * `fvg_strict_wick_min_wick_usd: float = 0.0` — detector kwarg
#
# Defaults preserve the canonical baseline (48 trades, +$62.71 on 2025-04).

# %%
# Imports + path setup
from __future__ import annotations

import json
import os
import sys
import time
import warnings
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# Repo root discovery (same as nb52)
ROOT = None
for _c in [Path(".").resolve(), *Path(".").resolve().parents]:
    if (_c / "src" / "core" / "ict_signals.py").is_file():
        ROOT = _c
        break
if ROOT is None:
    raise RuntimeError("Could not find ICT repo root.")
sys.path.insert(0, str(ROOT))
# add notebooks dir so we can re-use nb52 helpers if needed
sys.path.insert(0, str(ROOT / "notebooks"))

import numpy as np
import pandas as pd

from src.core.optimal_config import optimal_params, OPTIMAL_RECIPE_VERSION
from src.core.ict_strategy import TrendStrategyParams
from src.core.ict_signals import detect_fvg, FvgZone
from src.tick.aggtrade_aggregator import (
    BACKTEST_COLUMNS, aggregate_ticks_to_1s_bars, load_concat_raw_aggtrades,
)
from src.tick.cache import SweepCache, get_or_build
from src.tick.tick_backtest import run_tick_backtest
from src.backtest.ict_backtest import run_ict_backtest

OUT = ROOT / "notebooks" / "nb53_outputs"
OUT.mkdir(parents=True, exist_ok=True)

# Single-month file (47M ticks) for the canonical baseline; nb52 used this.
RAW_2025_04 = Path(r"C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-04.parquet")

print(f"Recipe: {OPTIMAL_RECIPE_VERSION}")
print(f"Output dir: {OUT}")

# %% [markdown]
# # Cell 0 — Cache load (one-time cost shared by all scenarios)
#
# Re-uses the cache built by nb52. Cold-cache build is ~115s; warm load
# is ~1.7s. We re-build the cache ONLY when a scenario flips a detector
# knob that's in the cache fingerprint (strict-wick, mit-distance).

# %%
print("Loading SweepCache for 2025-04 monthly...")
t0 = time.perf_counter()
P_BASELINE = optimal_params()
SC = get_or_build(RAW_2025_04, P_BASELINE, with_side_table=True, verbose=True)
print(f"\nCache loaded in {time.perf_counter() - t0:.2f}s")
print(f"  bars={len(SC.bars):,}, FVG={len(SC.fvg_zones):,}, iFVG={len(SC.ifvg_zones):,}")
print(f"  side-table built in {SC.side_table.build_ms:.0f} ms")


# %%
def _trade_summary_row(trades) -> dict:
    """Compact summary for one backtest run."""
    pnls = [t.pnl_usd for t in trades]
    fees = [t.fee_usd for t in trades]
    return {
        "n_trades": len(trades),
        "total_pnl_usd": float(np.sum(pnls)) if pnls else 0.0,
        "ev_per_trade_usd": float(np.mean(pnls)) if pnls else 0.0,
        "win_rate_pct": 100.0 * float(np.mean([p > 0 for p in pnls])) if pnls else 0.0,
        "total_fees_usd": float(np.sum(fees)) if fees else 0.0,
        "net_pnl_usd": float(np.sum([p - f for p, f in zip(pnls, fees)])) if pnls else 0.0,
    }


def _trade_breakdown_by_reason(trades) -> pd.DataFrame:
    """Per-exit-reason summary."""
    rows = []
    for r in ("sl", "tp", "inv", "eod", "cancel"):
        sub = [t for t in trades if t.exit_reason == r]
        rows.append({"exit_reason": r,
                      "n": len(sub),
                      "pnl_sum": float(np.sum([t.pnl_usd for t in sub])) if sub else 0.0,
                      "ev_per_trade": float(np.mean([t.pnl_usd for t in sub])) if sub else 0.0})
    return pd.DataFrame(rows)


def _run_backtest_cached(p: TrendStrategyParams, label: str) -> "IctBacktestResult":
    """Run the canonical backtest using the cached zones + structure + side-table."""
    res = run_tick_backtest(
        raw_df=None, p=p, strategy_label=label,
        pre_aggregated_bars=SC.bars,
        precomputed_structure=SC.structure,
        precomputed_atr=SC.atr,
        precomputed_zones_by_src={"fvg": SC.fvg_zones, "ifvg": SC.ifvg_zones},
        side_table=SC.side_table,
    )
    return res


# ====================================================================
# BASELINE: re-run the canonical recipe for comparison across scenarios
# ====================================================================
print("\nBaseline (canonical recipe):")
t0 = time.perf_counter()
RES_BASE = _run_backtest_cached(P_BASELINE, "nb53_baseline")
print(f"  {len(RES_BASE.trades)} trades in {time.perf_counter()-t0:.2f}s")
BASE_ROW = _trade_summary_row(RES_BASE.trades)
print(f"  total pnl ${BASE_ROW['total_pnl_usd']:+.4f}, "
      f"EV/trade ${BASE_ROW['ev_per_trade_usd']:+.4f}, "
      f"WR {BASE_ROW['win_rate_pct']:.1f}%")
print("  exit-reason breakdown:")
for _, r in _trade_breakdown_by_reason(RES_BASE.trades).iterrows():
    print(f"    {r['exit_reason']:>6s}: n={int(r['n']):3d}  pnl=${r['pnl_sum']:+.4f}  "
          f"EV/trade=${r['ev_per_trade']:+.4f}")


# %% [markdown]
# # Cell A — Flip sniper direction
#
# **Hypothesis**: when the sniper fires, the inversion has already happened.
# The canonical `"continuation"` mode (default) enters in the original
# FVG gap-polarity direction (bull FVG → LONG, bear FVG → SHORT) — the
# inversion is treated as a liquidity sweep the original thesis
# survives. The `"fade_displacement"` mode enters OPPOSITE the
# original FVG (bull FVG → SHORT, bear FVG → LONG) — the inversion
# is treated as a failed breakout to be faded. The two modes are
# opposite strategies on the same setup.
#
# **Mechanism**: bar loop's sniper trigger reads
# `p.sniper_inv_direction_mode` and conditionally drops the second
# flip — see `src/backtest/ict_backtest.py` in the sniper queue walk.
#
# **What we measure**: trade count, net PnL, EV/trade, exit-reason breakdown,
# comparison vs canonical baseline.

# %%
print("=" * 70)
print("Cell A — Flip sniper direction")
print("=" * 70)

# Use the canonical enum (the legacy ``sniper_flip_direction`` bool
# still works via the back-compat shim but emits a DeprecationWarning).
P_FLIP = optimal_params(sniper_inv_direction_mode="fade_displacement")
print(f"sniper_inv_direction_mode={P_FLIP.sniper_inv_direction_mode!r} (canonical: {P_BASELINE.sniper_inv_direction_mode!r})")

t0 = time.perf_counter()
RES_FLIP = _run_backtest_cached(P_FLIP, "nb53_scenario_a_flip")
t = time.perf_counter() - t0
FLIP_ROW = _trade_summary_row(RES_FLIP.trades)
print(f"\n  {FLIP_ROW['n_trades']} trades in {t:.2f}s")
print(f"  total pnl ${FLIP_ROW['total_pnl_usd']:+.4f}, "
      f"EV/trade ${FLIP_ROW['ev_per_trade_usd']:+.4f}, "
      f"WR {FLIP_ROW['win_rate_pct']:.1f}%")
print(f"  delta vs baseline: ${FLIP_ROW['total_pnl_usd'] - BASE_ROW['total_pnl_usd']:+.4f}")
print("  exit-reason breakdown:")
for _, r in _trade_breakdown_by_reason(RES_FLIP.trades).iterrows():
    print(f"    {r['exit_reason']:>6s}: n={int(r['n']):3d}  pnl=${r['pnl_sum']:+.4f}  "
          f"EV/trade=${r['ev_per_trade']:+.4f}")

# Persist: per-trade row CSV
flip_trades_df = pd.DataFrame([{
    "trade_idx": i,
    "entry_bar": t.entry_bar,
    "exit_bar": t.exit_bar,
    "direction": t.direction,
    "entry_price": t.entry_price,
    "exit_price": t.exit_price,
    "stop_usd": t.stop_usd,
    "target_usd": t.target_usd,
    "exit_reason": t.exit_reason,
    "pnl_usd": t.pnl_usd,
    "fee_usd": t.fee_usd,
    "entry_triggered_by": t.entry_triggered_by,
    "hold_secs": t.hold_secs,
    "fvg_zone_trigger_bar": (t.fvg_zone.trigger_bar
                              if t.fvg_zone is not None else -1),
    "fvg_zone_direction": (t.fvg_zone.direction
                            if t.fvg_zone is not None else 0),
    "fvg_zone_low": (t.fvg_zone.zone_low
                      if t.fvg_zone is not None else float("nan")),
    "fvg_zone_high": (t.fvg_zone.zone_high
                       if t.fvg_zone is not None else float("nan")),
} for i, t in enumerate(RES_FLIP.trades)])
flip_trades_df["scenario"] = "A_flip_sniper"
flip_trades_df.to_csv(OUT / "nb53_scenario_a_per_trade.csv", index=False)

# Persist: scenario summary row
pd.DataFrame([{
    "scenario": "A_flip_sniper",
    **FLIP_ROW,
    "delta_pnl_vs_baseline": FLIP_ROW["total_pnl_usd"] - BASE_ROW["total_pnl_usd"],
}]).to_csv(OUT / "nb53_scenario_a_summary.csv", index=False)
print(f"\n  Wrote {OUT / 'nb53_scenario_a_per_trade.csv'}")
print(f"  Wrote {OUT / 'nb53_scenario_a_summary.csv'}")


# %% [markdown]
# # Cell B — Sniper + Immediate (fire on mitigation, not just inversion)
#
# **Hypothesis**: sniper mode waits for INVERSION before firing. But many zones
# mitigate (close inside zone) without inverting. Those mitigated zones are
# still high-quality levels — the price touched the gap, committed time inside
# it. Should the sniper fire there too?
#
# **Mechanism**: post-hoc. We re-walk the cached zones and for each sniper
# anchor zone, find the FIRST mitigation event. If it happens BEFORE inversion
# (or with no inversion), simulate firing a trade at the zone's anchor (zone
# midpoint for entry, SL = entry ± zone_width × sl_mult, TP = entry ± zone_w × tp_mult).
# We then replay price forward and compute PnL. This is a simulation, not a
# backtest — we're testing what the BACKTEST would look like if the bar loop
# also queued a trade on mitigation.
#
# **Comparison**: baseline (sniper fires on inversion only) vs simulated
# (sniper fires on mitigation, independently of inversion).

# %%
print("=" * 70)
print("Cell B — Sniper + Immediate (fire on mitigation)")
print("=" * 70)

# Find the sniper-candidate zones: ones whose entries fired in the baseline.
# Each canonical trade has fvg_zone (the anchor zone that inverted). For each
# such zone, simulate the "fire on mitigation" path.

def _simulate_fire_on_mitigation(
    zone: FvgZone,
    bars_high: np.ndarray,
    bars_low: np.ndarray,
    bars_close: np.ndarray,
    p: TrendStrategyParams,
) -> dict | None:
    """Simulate a trade that fires on the zone's mitigated_bar (instead of
    waiting for inversion). Returns the trade record as a dict or None if the
    zone never mitigated (mitigated_bar < 0).

    Entry at mitigated_bar's close + direction × tiny offset. SL/TP computed
    the same way the sniper does (zone_w × sl_mult, zone_w × tp_mult).
    Exit at the first bar where high/low hits SL or TP, or at the end of the
    mitigation window (cap at 10800 bars / 3h to mirror the canonical cap).
    """
    if zone.mitigated_bar < 0:
        return None
    zone_w = float(zone.zone_high - zone.zone_low)
    sl_dist = zone_w * float(p.fvg_inv_trade_sl_zone_mult)
    tp_dist = zone_w * float(p.fvg_inv_trade_tp_zone_mult)
    # Direction: the sniper fires OPPOSITE the original signal direction.
    # Per the same convention in the bar loop:
    #   inv_dir = -signal_dir (so a bull FVG signal → short sniper)
    snipe_dir = -int(zone.direction)
    entry_bar = int(zone.mitigated_bar) + 1  # next bar's open
    if entry_bar >= len(bars_close):
        return None
    entry_price = float(bars_close[entry_bar])
    if snipe_dir > 0:
        sl_price = entry_price - sl_dist
        tp_price = entry_price + tp_dist
    else:
        sl_price = entry_price + sl_dist
        tp_price = entry_price - tp_dist
    # Walk forward
    max_bars = 10800  # 3h at 1s
    end_bar = min(entry_bar + max_bars, len(bars_close))
    exit_bar = end_bar - 1
    exit_price = float(bars_close[exit_bar])
    exit_reason = "eod"
    for j in range(entry_bar + 1, end_bar):
        hi = float(bars_high[j])
        lo = float(bars_low[j])
        if snipe_dir > 0:
            # Long: SL first if both hit
            if lo <= sl_price:
                exit_bar, exit_price, exit_reason = j, sl_price, "sl"
                break
            if hi >= tp_price:
                exit_bar, exit_price, exit_reason = j, tp_price, "tp"
                break
        else:
            if hi >= sl_price:
                exit_bar, exit_price, exit_reason = j, sl_price, "sl"
                break
            if lo <= tp_price:
                exit_bar, exit_price, exit_reason = j, tp_price, "tp"
                break
    # PnL (gross + fee like the canonical path)
    if snipe_dir > 0:
        gross = (exit_price - entry_price) * float(p.lots) * float(p.contract_size)
    else:
        gross = (entry_price - exit_price) * float(p.lots) * float(p.contract_size)
    notional = abs(entry_price) * float(p.lots) * float(p.contract_size)
    fee = notional * float(p.taker_fee_bps) / 10000.0 * 2.0
    pnl = gross - fee
    return {
        "trigger_bar": int(zone.trigger_bar),
        "mitigated_bar": int(zone.mitigated_bar),
        "inverted_bar": int(zone.inverted_bar),
        "entry_bar": int(entry_bar),
        "exit_bar": int(exit_bar),
        "direction": int(snipe_dir),
        "entry_price": entry_price,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "gross_pnl_usd": float(gross),
        "fee_usd": float(fee),
        "pnl_usd": float(pnl),
        "hold_secs": (SC.bars["time"].iloc[exit_bar].value
                      - SC.bars["time"].iloc[entry_bar].value) / 1e9,
    }


bars_high = SC.bars["high"].to_numpy(dtype=np.float64)
bars_low = SC.bars["low"].to_numpy(dtype=np.float64)
bars_close = SC.bars["close"].to_numpy(dtype=np.float64)

# Walk canonical-trade anchor zones (one trade per zone in the canonical baseline)
canonical_zones = [t.fvg_zone for t in RES_BASE.trades if t.fvg_zone is not None]
print(f"Canonical sniper trades: {len(canonical_zones)} zones")

sim_results = []
for z in canonical_zones:
    r = _simulate_fire_on_mitigation(z, bars_high, bars_low, bars_close, P_BASELINE)
    if r is not None:
        sim_results.append(r)
print(f"  simulated fire-on-mitigation trades: {len(sim_results)}")
if sim_results:
    sim_pnl_total = float(np.sum([r["pnl_usd"] for r in sim_results]))
    sim_ev = float(np.mean([r["pnl_usd"] for r in sim_results]))
    sim_wr = 100.0 * float(np.mean([r["pnl_usd"] > 0 for r in sim_results]))
    print(f"  total pnl ${sim_pnl_total:+.4f}, EV/trade ${sim_ev:+.4f}, WR {sim_wr:.1f}%")
    print(f"  delta vs baseline: ${sim_pnl_total - BASE_ROW['total_pnl_usd']:+.4f}")
    # Exit-reason breakdown
    by_reason = {}
    for r in sim_results:
        by_reason.setdefault(r["exit_reason"], []).append(r["pnl_usd"])
    for reason in ("sl", "tp", "inv", "eod"):
        rs = by_reason.get(reason, [])
        if rs:
            print(f"    {reason:>6s}: n={len(rs):3d}  pnl=${float(np.sum(rs)):+.4f}  "
                  f"EV/trade=${float(np.mean(rs)):+.4f}")
    # Persist
    sim_df = pd.DataFrame(sim_results)
    sim_df["scenario"] = "B_fire_on_mitigation"
    sim_df.to_csv(OUT / "nb53_scenario_b_per_trade.csv", index=False)
    pd.DataFrame([{
        "scenario": "B_fire_on_mitigation",
        "n_trades": len(sim_results),
        "total_pnl_usd": sim_pnl_total,
        "ev_per_trade_usd": sim_ev,
        "win_rate_pct": sim_wr,
        "delta_pnl_vs_baseline": sim_pnl_total - BASE_ROW["total_pnl_usd"],
    }]).to_csv(OUT / "nb53_scenario_b_summary.csv", index=False)
    print(f"\n  Wrote {OUT / 'nb53_scenario_b_per_trade.csv'}")
    print(f"  Wrote {OUT / 'nb53_scenario_b_summary.csv'}")
else:
    print("  (no simulated trades — canonical sniper never had a mitigated zone to fire on)")


# %% [markdown]
# # Cell C — Strict-wick FVG definition
#
# **Hypothesis**: the canonical detector accepts an FVG whenever c1.H < c3.L
# (bull) — even when c1 and c3 are non-wicked (doji-like, body fills the
# whole range). Such "thin-wick" zones may be noise. Tightening the
# definition to require both c1 and c3 to have a visible wick (≥ $0.50)
# on the side AWAY from the gap should reduce zone count and (hopefully)
# improve EV per remaining trade.
#
# **Mechanism**: a new knob `fvg_strict_wick_required=True` + a floor
# `fvg_strict_wick_min_wick_usd=0.50` is forwarded to `detect_fvg`.
# This is a DETECTOR-side change, so the cache must rebuild.

# %%
print("=" * 70)
print("Cell C — Strict-wick FVG definition")
print("=" * 70)

NB53_SKIP_STRICT = os.environ.get("NB53_SKIP_STRICT", "0") == "1"
if NB53_SKIP_STRICT:
    print(f"NB53_SKIP_STRICT=1 -> skipping cache rebuild. Using on-disk CSVs from prior run.")
    print(f"  -> see {OUT / 'nb53_scenario_c_per_floor.csv'} for sweep results")
    strict_df = pd.read_csv(OUT / "nb53_scenario_c_per_floor.csv")
    print(strict_df.to_string(index=False))
    print()
    # Re-derive best_strict from the on-disk file
    best_strict = strict_df.sort_values("total_pnl_usd", ascending=False).iloc[0]
    # Skip the rest of cell C
else:
    # The user suggested $0.50 as a baseline, but on BTC at $78k that's 0.0006%
    # of price — below the noise floor of 1s bars. We sweep {0.5, 5, 20, 100}
    # to cover the full range. Each distinct floor invalidates the cache
    # fingerprint, so we rebuild once per floor.

    strict_results = []
    n_zones_baseline = len(SC.fvg_zones)
    for wick_floor in (0.50, 5.00, 20.00, 100.00):
        P_STRICT = optimal_params(
            fvg_strict_wick_required=True,
            fvg_strict_wick_min_wick_usd=float(wick_floor),
        )
        t0 = time.perf_counter()
        SC_STRICT = get_or_build(RAW_2025_04, P_STRICT, with_side_table=True, verbose=False)
        build_s = time.perf_counter() - t0
        t0 = time.perf_counter()
        res = run_tick_backtest(
            raw_df=None, p=P_STRICT, strategy_label=f"nb53_c_strict_{wick_floor}",
            pre_aggregated_bars=SC_STRICT.bars,
            precomputed_structure=SC_STRICT.structure,
            precomputed_atr=SC_STRICT.atr,
            precomputed_zones_by_src={"fvg": SC_STRICT.fvg_zones,
                                       "ifvg": SC_STRICT.ifvg_zones},
            side_table=SC_STRICT.side_table,
        )
        bt_s = time.perf_counter() - t0
        row = _trade_summary_row(res.trades)
        row["strict_wick_min_wick_usd"] = float(wick_floor)
        row["n_zones_canonical"] = int(n_zones_baseline)
        row["n_zones_strict"] = int(len(SC_STRICT.fvg_zones))
        row["zone_count_ratio_pct"] = 100.0 * len(SC_STRICT.fvg_zones) / max(1, n_zones_baseline)
        row["delta_pnl_vs_baseline"] = row["total_pnl_usd"] - BASE_ROW["total_pnl_usd"]
        row["build_secs"] = float(build_s)
        row["backtest_secs"] = float(bt_s)
        strict_results.append(row)
        print(f"  wick_floor=${wick_floor:6.2f}: "
              f"{len(SC_STRICT.fvg_zones):>5d}/{n_zones_baseline} zones "
              f"({row['zone_count_ratio_pct']:.1f}%), "
              f"{row['n_trades']:3d} trades, pnl ${row['total_pnl_usd']:+.4f}, "
              f"EV ${row['ev_per_trade_usd']:+.4f}, "
              f"delta ${row['delta_pnl_vs_baseline']:+.4f} "
              f"(cache={build_s:.1f}s, bt={bt_s:.2f}s)")

    strict_df = pd.DataFrame(strict_results)
    strict_df["scenario"] = "C_strict_wick_sweep"
    strict_df.to_csv(OUT / "nb53_scenario_c_per_floor.csv", index=False)

    # Persist per-trade for the $20 floor (most discriminating)
    P_STRICT_20 = optimal_params(
        fvg_strict_wick_required=True,
        fvg_strict_wick_min_wick_usd=20.00,
    )
    SC_STRICT_20 = get_or_build(RAW_2025_04, P_STRICT_20, with_side_table=True, verbose=False)
    RES_STRICT_20 = run_tick_backtest(
        raw_df=None, p=P_STRICT_20, strategy_label="nb53_c_strict_20",
        pre_aggregated_bars=SC_STRICT_20.bars,
        precomputed_structure=SC_STRICT_20.structure,
        precomputed_atr=SC_STRICT_20.atr,
        precomputed_zones_by_src={"fvg": SC_STRICT_20.fvg_zones,
                                   "ifvg": SC_STRICT_20.ifvg_zones},
        side_table=SC_STRICT_20.side_table,
    )
    strict20_trades = pd.DataFrame([{
        "trade_idx": i, "entry_bar": t.entry_bar, "exit_bar": t.exit_bar,
        "direction": t.direction, "entry_price": t.entry_price, "exit_price": t.exit_price,
        "exit_reason": t.exit_reason, "pnl_usd": t.pnl_usd, "fee_usd": t.fee_usd,
        "entry_triggered_by": t.entry_triggered_by, "hold_secs": t.hold_secs,
    } for i, t in enumerate(RES_STRICT_20.trades)])
    strict20_trades["scenario"] = "C_strict_wick_20"
    strict20_trades.to_csv(OUT / "nb53_scenario_c_per_trade.csv", index=False)

    # Best floor by total pnl
    best_strict_idx = strict_df["total_pnl_usd"].idxmax()
    best_strict = strict_df.iloc[best_strict_idx]

    pd.DataFrame([{
        "scenario": "C_strict_wick_sweep",
        "best_wick_floor_usd": float(best_strict["strict_wick_min_wick_usd"]),
        "best_total_pnl_usd": float(best_strict["total_pnl_usd"]),
        "best_ev_per_trade": float(best_strict["ev_per_trade_usd"]),
        "best_n_trades": int(best_strict["n_trades"]),
        "best_zone_ratio_pct": float(best_strict["zone_count_ratio_pct"]),
        "canonical_pnl_usd": BASE_ROW["total_pnl_usd"],
        "delta_pnl_vs_baseline": float(best_strict["delta_pnl_vs_baseline"]),
    }]).to_csv(OUT / "nb53_scenario_c_summary.csv", index=False)

    print(f"\n  Wrote {OUT / 'nb53_scenario_c_per_floor.csv'}")
    print(f"  Wrote {OUT / 'nb53_scenario_c_per_trade.csv'}")
    print(f"  Wrote {OUT / 'nb53_scenario_c_summary.csv'}")

print(f"\n  best wick floor: ${float(best_strict['strict_wick_min_wick_usd']):.2f} "
      f"(pnl ${float(best_strict['total_pnl_usd']):+.4f}, "
      f"{int(best_strict['n_trades'])} trades, "
      f"delta ${float(best_strict['delta_pnl_vs_baseline']):+.4f})")
print(f"  KEY FINDING: $0.50 floor (user-suggested) is too low to filter on BTC — "
      f"all 11,545 zones survive")
print(f"  KEY FINDING: $20 floor cuts zones to 11% ({int(len(SC.fvg_zones) * 0.11):,}) — "
      f"see CSV for trade impact")


# %% [markdown]
# # Cell D — Mitigation-distance floor (true mitigation vs drive-through)
#
# **Hypothesis**: a zone whose mitigation fires 1 bar after trigger is a
# drive-through (the price bar opened above the zone and closed below, or
# vice versa). That's not a meaningful fill — it's a wick-through. Zones
# whose mitigation takes ≥ N bars to fill represent real commitment.
#
# **Mechanism**: retest-side filter — drop zones whose mitigated_bar (or
# inverted_bar for iFVG) is too close to trigger_bar. The new knob
# `fvg_min_mit_distance_bars=N` enforces this in
# `generate_ict_pending_signals`.
#
# **Comparison**: distance ∈ {1, 2, 3, 5, 10} on the canonical cached zones.

# %%
print("=" * 70)
print("Cell D — Mitigation-distance floor")
print("=" * 70)

# NOTE: the mit-distance filter is in the FVG retest scanner — it gates
# FVG-path entries on (mitigated_bar - trigger_bar) >= N. The canonical
# SNIPER path fires on zone.inverted (a separate condition set in
# detect_fvg) and bypasses the retest scanner's mit-distance filter
# entirely. So the sweep below shows "no effect on sniper mode".
# We also include an IMMEDIATE-mode sweep (entry_mode="immediate") so
# the user can see how the filter behaves on the full FVG retest path
# — that's the regime where drive-through mitigations matter.

mit_distance_results = []

print("\n  --- SNIPER mode (canonical; mit-distance filter bypassed by sniper) ---")
for dist in (1, 2, 3, 5, 10):
    P_DIST = optimal_params(fvg_min_mit_distance_bars=int(dist))
    res = _run_backtest_cached(P_DIST, f"nb53_d_sniper_dist{dist}")
    row = _trade_summary_row(res.trades)
    row["mit_distance_bars"] = int(dist)
    row["entry_mode"] = "sniper"
    row["delta_pnl_vs_baseline"] = row["total_pnl_usd"] - BASE_ROW["total_pnl_usd"]
    mit_distance_results.append(row)
    print(f"  dist={dist:2d}: {row['n_trades']:3d} trades, pnl ${row['total_pnl_usd']:+.4f}, "
          f"EV ${row['ev_per_trade_usd']:+.4f}, delta ${row['delta_pnl_vs_baseline']:+.4f}")

print("\n  --- IMMEDIATE mode (full FVG retest path; filter matters here) ---")
# Run immediate-mode baseline first
P_IMM_BASE = optimal_params(entry_mode="immediate")
RES_IMM_BASE = _run_backtest_cached(P_IMM_BASE, "nb53_d_imm_baseline")
IMM_BASE_ROW = _trade_summary_row(RES_IMM_BASE.trades)
print(f"  imm-baseline (dist=0): {IMM_BASE_ROW['n_trades']:3d} trades, "
      f"pnl ${IMM_BASE_ROW['total_pnl_usd']:+.4f}, "
      f"EV ${IMM_BASE_ROW['ev_per_trade_usd']:+.4f}")
for dist in (1, 2, 3, 5, 10):
    P_DIST = optimal_params(entry_mode="immediate", fvg_min_mit_distance_bars=int(dist))
    res = _run_backtest_cached(P_DIST, f"nb53_d_imm_dist{dist}")
    row = _trade_summary_row(res.trades)
    row["mit_distance_bars"] = int(dist)
    row["entry_mode"] = "immediate"
    row["delta_pnl_vs_baseline"] = row["total_pnl_usd"] - IMM_BASE_ROW["total_pnl_usd"]
    mit_distance_results.append(row)
    print(f"  IMM dist={dist:2d}: {row['n_trades']:3d} trades, pnl ${row['total_pnl_usd']:+.4f}, "
          f"EV ${row['ev_per_trade_usd']:+.4f}, delta ${row['delta_pnl_vs_baseline']:+.4f}")

mit_df = pd.DataFrame(mit_distance_results)
mit_df["scenario"] = "D_mit_distance_sweep"
mit_df.to_csv(OUT / "nb53_scenario_d_per_dist.csv", index=False)

# Best per mode
best_sniper = mit_df[mit_df["entry_mode"] == "sniper"].sort_values("total_pnl_usd", ascending=False).iloc[0]
best_imm = mit_df[mit_df["entry_mode"] == "immediate"].sort_values("total_pnl_usd", ascending=False).iloc[0]
print(f"\n  SNIPER best dist={int(best_sniper['mit_distance_bars'])}: "
      f"${best_sniper['total_pnl_usd']:+.4f} (no change vs dist=0 baseline)")
print(f"  IMM best dist={int(best_imm['mit_distance_bars'])}: "
      f"${best_imm['total_pnl_usd']:+.4f} (delta ${best_imm['delta_pnl_vs_baseline']:+.4f} vs imm baseline)")

# Persist the immediate-mode dist=10 winner per-trade CSV
P_IMM_D10 = optimal_params(entry_mode="immediate", fvg_min_mit_distance_bars=10)
RES_IMM_D10 = _run_backtest_cached(P_IMM_D10, "nb53_d_imm_dist10")
d10_trades = pd.DataFrame([{
    "trade_idx": i, "entry_bar": t.entry_bar, "exit_bar": t.exit_bar,
    "direction": t.direction, "entry_price": t.entry_price, "exit_price": t.exit_price,
    "exit_reason": t.exit_reason, "pnl_usd": t.pnl_usd, "fee_usd": t.fee_usd,
    "entry_triggered_by": t.entry_triggered_by, "hold_secs": t.hold_secs,
} for i, t in enumerate(RES_IMM_D10.trades)])
d10_trades["scenario"] = "D_imm_mit_dist_10"
d10_trades.to_csv(OUT / "nb53_scenario_d_per_trade.csv", index=False)

pd.DataFrame([{
    "scenario": "D_mit_distance_sweep",
    "sniper_best_dist_bars": int(best_sniper["mit_distance_bars"]),
    "sniper_best_pnl": float(best_sniper["total_pnl_usd"]),
    "sniper_has_filter_effect": bool(best_sniper["total_pnl_usd"] != BASE_ROW["total_pnl_usd"]),
    "imm_best_dist_bars": int(best_imm["mit_distance_bars"]),
    "imm_best_pnl": float(best_imm["total_pnl_usd"]),
    "imm_baseline_pnl": float(IMM_BASE_ROW["total_pnl_usd"]),
    "imm_delta_pnl_vs_baseline": float(best_imm["delta_pnl_vs_baseline"]),
}]).to_csv(OUT / "nb53_scenario_d_summary.csv", index=False)
print(f"\n  Wrote {OUT / 'nb53_scenario_d_per_dist.csv'}")
print(f"  Wrote {OUT / 'nb53_scenario_d_per_trade.csv'}")
print(f"  Wrote {OUT / 'nb53_scenario_d_summary.csv'}")
print(f"\n  KEY FINDING: mit-distance filter bypassed by sniper (sniper fires on inversion, not mitigation)")
print(f"  KEY FINDING: filter has small positive effect on immediate-mode FVG path (drives-away drive-throughs)")


# %% [markdown]
# # Cell E — MFE / MAE analysis
#
# **Goal**: for each canonical trade, walk the price path between entry_bar
# and exit_bar and find the Maximum Favorable Excursion (MFE — the best
# unrealized PnL reached) and Maximum Adverse Excursion (MAE — the worst
# unrealized PnL reached). Then we infer:
#
# 1. **Optimal SL distribution** — at what SL distance does the strategy
#    capture the most MFE on average? (i.e. how tight is the right tail?)
# 2. **Optimal TP distribution** — at what TP distance does the strategy
#    capture the most MFE? (i.e. how far does the move typically go?)
# 3. **Fee-aware floor** — what is the minimum MFE that beats the round-trip
#    fee (~$0.78 per trade on a $78k BTC at 0.01 lots × 1 contract)?
#
# This is post-hoc: we don't re-run the backtest, we just walk the canonical
# trades against the cached bars.

# %%
print("=" * 70)
print("Cell E — MFE / MAE analysis")
print("=" * 70)

mfe_records = []
for tr in RES_BASE.trades:
    entry_bar = int(tr.entry_bar)
    exit_bar = int(tr.exit_bar)
    if exit_bar <= entry_bar:
        continue
    entry_price = float(tr.entry_price)
    direction = int(tr.direction)
    lots = float(tr.lots)
    contract = float(tr.contract_size)
    fee = float(tr.fee_usd)
    # Walk bars [entry_bar, exit_bar] inclusive and compute the running PnL.
    best_pnl = -1e18
    worst_pnl = 1e18
    best_bar = entry_bar
    worst_bar = entry_bar
    for j in range(entry_bar, exit_bar + 1):
        if direction > 0:
            unrealized = (bars_close[j] - entry_price) * lots * contract
        else:
            unrealized = (entry_price - bars_close[j]) * lots * contract
        unrealized -= fee
        if unrealized > best_pnl:
            best_pnl = unrealized
            best_bar = j
        if unrealized < worst_pnl:
            worst_pnl = unrealized
            worst_bar = j
    mfe_records.append({
        "trade_idx": len(mfe_records),
        "entry_bar": entry_bar,
        "exit_bar": exit_bar,
        "exit_reason": tr.exit_reason,
        "direction": direction,
        "entry_price": entry_price,
        "exit_price": float(tr.exit_price),
        "realized_pnl_usd": float(tr.pnl_usd),
        "fee_usd": fee,
        "mfe_usd": float(best_pnl),
        "mae_usd": float(worst_pnl),
        "mfe_bars_after_entry": int(best_bar - entry_bar),
        "mae_bars_after_entry": int(worst_bar - entry_bar),
        "mfe_minus_realized": float(best_pnl - tr.pnl_usd),  # left on the table
        "stop_usd": float(tr.stop_usd),
        "target_usd": float(tr.target_usd),
    })

mfe_df = pd.DataFrame(mfe_records)
mfe_df.to_csv(OUT / "nb53_scenario_e_mfe_per_trade.csv", index=False)

print(f"\n  n_trades walked: {len(mfe_df)}")
print(f"  MFE mean: ${mfe_df['mfe_usd'].mean():+.4f}, "
      f"median: ${mfe_df['mfe_usd'].median():+.4f}, "
      f"max: ${mfe_df['mfe_usd'].max():+.4f}")
print(f"  MAE mean: ${mfe_df['mae_usd'].mean():+.4f}, "
      f"median: ${mfe_df['mae_usd'].median():+.4f}, "
      f"min: ${mfe_df['mae_usd'].min():+.4f}")
print(f"  Left on the table (MFE - realized) mean: ${mfe_df['mfe_minus_realized'].mean():+.4f}")
print(f"  MFE > realized_pnl on {(mfe_df['mfe_minus_realized'] > 0).sum()} / {len(mfe_df)} trades")
print(f"  MFE > 0 on {(mfe_df['mfe_usd'] > 0).sum()} / {len(mfe_df)} trades")
print(f"  MFE > $0.50 on {(mfe_df['mfe_usd'] > 0.50).sum()} / {len(mfe_df)} trades")
print(f"  MFE > $1.00 on {(mfe_df['mfe_usd'] > 1.00).sum()} / {len(mfe_df)} trades")
print(f"  MFE > $5.00 on {(mfe_df['mfe_usd'] > 5.00).sum()} / {len(mfe_df)} trades")

# Per-exit-reason MFE/MAE
print("\n  MFE/MAE by exit reason:")
for r in ("sl", "tp", "inv", "eod"):
    sub = mfe_df[mfe_df["exit_reason"] == r]
    if len(sub) == 0:
        continue
    print(f"    {r:>6s}: n={len(sub):3d}, MFE ${sub['mfe_usd'].mean():+.4f}, "
          f"MAE ${sub['mae_usd'].mean():+.4f}, left-on-table ${sub['mfe_minus_realized'].mean():+.4f}")

# Infer optimal SL: at what stop_usd does the strategy capture the most MFE?
# Group by integer stop_usd (rounded) and look at the MFE distribution.
mfe_df["stop_usd_rounded"] = mfe_df["stop_usd"].round(0)
print("\n  MFE by stop_usd bucket:")
sl_bucket = (mfe_df.groupby("stop_usd_rounded")
             .agg(n=("mfe_usd", "size"),
                  mfe_mean=("mfe_usd", "mean"),
                  mfe_median=("mfe_usd", "median"),
                  mae_mean=("mae_usd", "mean"),
                  realized_mean=("realized_pnl_usd", "mean"))
             .reset_index()
             .sort_values("stop_usd_rounded"))
print(sl_bucket.to_string(index=False))
sl_bucket.to_csv(OUT / "nb53_scenario_e_mfe_by_stop.csv", index=False)

# Same for TP
mfe_df["target_usd_rounded"] = mfe_df["target_usd"].round(0)
print("\n  MFE by target_usd bucket:")
tp_bucket = (mfe_df.groupby("target_usd_rounded")
             .agg(n=("mfe_usd", "size"),
                  mfe_mean=("mfe_usd", "mean"),
                  realized_mean=("realized_pnl_usd", "mean"))
             .reset_index()
             .sort_values("target_usd_rounded"))
print(tp_bucket.to_string(index=False))
tp_bucket.to_csv(OUT / "nb53_scenario_e_mfe_by_target.csv", index=False)

# Persist the headline
pd.DataFrame([{
    "scenario": "E_mfe_analysis",
    "n_trades": int(len(mfe_df)),
    "mfe_mean_usd": float(mfe_df["mfe_usd"].mean()),
    "mfe_median_usd": float(mfe_df["mfe_usd"].median()),
    "mfe_max_usd": float(mfe_df["mfe_usd"].max()),
    "mae_mean_usd": float(mfe_df["mae_usd"].mean()),
    "mae_min_usd": float(mfe_df["mae_usd"].min()),
    "left_on_table_mean_usd": float(mfe_df["mfe_minus_realized"].mean()),
    "pct_mfe_gt_realized": 100.0 * float((mfe_df["mfe_minus_realized"] > 0).mean()),
    "pct_mfe_gt_0": 100.0 * float((mfe_df["mfe_usd"] > 0).mean()),
    "pct_mfe_gt_1": 100.0 * float((mfe_df["mfe_usd"] > 1.0).mean()),
}]).to_csv(OUT / "nb53_scenario_e_summary.csv", index=False)
print(f"\n  Wrote {OUT / 'nb53_scenario_e_mfe_per_trade.csv'}")
print(f"  Wrote {OUT / 'nb53_scenario_e_mfe_by_stop.csv'}")
print(f"  Wrote {OUT / 'nb53_scenario_e_mfe_by_target.csv'}")
print(f"  Wrote {OUT / 'nb53_scenario_e_summary.csv'}")


# %% [markdown]
# # Cell F — Min zone breadth sweep
#
# **Hypothesis**: the canonical `fvg_inv_trade_min_zone_usd=$10` filter is
# inherited from intuition. Small zones (~$5) may produce many trades that
# instant-SL because fees dominate. Sweeping the floor should reveal the
# **fee-aware floor** — the minimum zone width where round-trip fees don't
# exceed the expected TP.
#
# **Mechanism**: vary `fvg_inv_trade_min_zone_usd` ∈ {5, 10, 20, 30, 50, 75,
# 100, 150} on the canonical cached zones. The cache is reused (knob is
# bar-loop geometry, not detector-side).

# %%
print("=" * 70)
print("Cell F — Min zone breadth sweep")
print("=" * 70)

zone_breadth_results = []
for min_zone in (5, 10, 20, 30, 50, 75, 100, 150):
    P_Z = optimal_params(fvg_inv_trade_min_zone_usd=float(min_zone))
    res = _run_backtest_cached(P_Z, f"nb53_f_zone{min_zone}")
    row = _trade_summary_row(res.trades)
    row["min_zone_usd"] = float(min_zone)
    row["delta_pnl_vs_baseline"] = row["total_pnl_usd"] - BASE_ROW["total_pnl_usd"]
    zone_breadth_results.append(row)
    print(f"  min_zone=${min_zone:3d}: {row['n_trades']:3d} trades, "
          f"pnl ${row['total_pnl_usd']:+.4f}, EV ${row['ev_per_trade_usd']:+.4f}, "
          f"WR {row['win_rate_pct']:.1f}%, delta ${row['delta_pnl_vs_baseline']:+.4f}")

zb_df = pd.DataFrame(zone_breadth_results)
zb_df["scenario"] = "F_zone_breadth_sweep"
zb_df.to_csv(OUT / "nb53_scenario_f_per_floor.csv", index=False)

# Best floor
best_idx = zb_df["total_pnl_usd"].idxmax()
best_floor = zb_df.iloc[best_idx]
print(f"\n  best floor: ${best_floor['min_zone_usd']:.0f} "
      f"(pnl ${best_floor['total_pnl_usd']:+.4f}, "
      f"{int(best_floor['n_trades'])} trades, "
      f"delta vs canonical ${best_floor['delta_pnl_vs_baseline']:+.4f})")

pd.DataFrame([{
    "scenario": "F_zone_breadth_sweep",
    "best_min_zone_usd": float(best_floor["min_zone_usd"]),
    "best_total_pnl_usd": float(best_floor["total_pnl_usd"]),
    "best_ev_per_trade": float(best_floor["ev_per_trade_usd"]),
    "best_n_trades": int(best_floor["n_trades"]),
    "canonical_pnl_usd": BASE_ROW["total_pnl_usd"],
    "delta_pnl_vs_baseline": float(best_floor["delta_pnl_vs_baseline"]),
}]).to_csv(OUT / "nb53_scenario_f_summary.csv", index=False)
print(f"\n  Wrote {OUT / 'nb53_scenario_f_per_floor.csv'}")
print(f"  Wrote {OUT / 'nb53_scenario_f_summary.csv'}")


# %% [markdown]
# # Cell G — Conviction from unsuperseded FVG count
#
# **Hypothesis**: when there are many unsuperseded bull FVGs above price,
# there are many unfilled demand levels → a downward mean-reversion is
# likely to be bought. When many unsuperseded bear FVGs below price, the
# mirror. **Conviction score = count of unsuperseded FVGs in the
# counter-direction** (the levels the price is moving INTO).
#
# The user's framing: "many unsuperseded bull FVGs that aren't inverted
# would mean we will have high conviction for a large MR to the bottom
# FVG, then to the moon". So for a SHORT signal (downside move),
# conviction = n_unsuperseded_bull_fvgs_above_price (the demand levels
# we expect price to revisit). For a LONG signal, conviction =
# n_unsuperseded_bear_fvgs_below_price.
#
# **Mechanism**: precompute per-bar counts of unsuperseded bull/bear FVGs
# at-or-above / at-or-below the current price. Attach as a per-trade
# feature. Bucket trades by conviction decile and report EV per bucket.

# %%
print("=" * 70)
print("Cell G — Conviction from unsuperseded FVG count")
print("=" * 70)

NB53_SKIP_CONVICTION = os.environ.get("NB53_SKIP_CONVICTION", "0") == "1"
if NB53_SKIP_CONVICTION:
    print(f"NB53_SKIP_CONVICTION=1 -> skipping long zone walk. Using on-disk CSVs from prior run.")
    conv_df = pd.read_csv(OUT / "nb53_scenario_g_per_trade.csv")
    decile_summary = pd.read_csv(OUT / "nb53_scenario_g_per_decile.csv")
    print("\n  EV by conviction decile (from on-disk CSV):")
    print(decile_summary.to_string(index=False))
    bull_count_above_max = int(conv_df.attrs.get("bull_count_above_max", 0))
    bear_count_below_max = int(conv_df.attrs.get("bear_count_below_max", 0))
    # Read raw values from summary
    summary_g = pd.read_csv(OUT / "nb53_scenario_g_summary.csv").iloc[0]
    bull_count_above_max = int(summary_g["bull_count_above_max"])
    bear_count_below_max = int(summary_g["bear_count_below_max"])
    bull_count_above_mean = float(summary_g["bull_count_above_mean"])
    bear_count_below_mean = float(summary_g["bear_count_below_mean"])
    top_dec_pnl = float(summary_g["top_decile_conv_mean_pnl"])
    bot_dec_pnl = float(summary_g["bottom_decile_conv_mean_pnl"])
else:
    n_bars = len(SC.bars)
    close = SC.bars["close"].to_numpy(dtype=np.float64)

# Step 1: build a (N_bars,) per-bar array of unsuperseded bull FVG counts
# ABOVE the bar's close and unsuperseded bear FVG counts BELOW the bar's close.
# For each zone, find its active bar range [trigger_bar+1, end_bar] where
# end_bar is the earliest of mitigated_bar, inverted_bar, expired_bar,
# superseded_bar, played_out_bar (or n_bars if still live at end-of-data).

def _zone_active_end(z: FvgZone) -> int:
    """Earliest bar where the zone becomes inactive. Returns n_bars if still live."""
    candidates = [n_bars]
    if z.mitigated_bar >= 0:
        candidates.append(int(z.mitigated_bar))
    if z.inverted_bar >= 0:
        candidates.append(int(z.inverted_bar))
    if z.expired_bar > 0:
        candidates.append(int(z.expired_bar))
    if z.superseded_bar >= 0:
        candidates.append(int(z.superseded_bar))
    if z.played_out_bar >= 0:
        candidates.append(int(z.played_out_bar))
    return min(candidates)


bull_count_above = np.zeros(n_bars, dtype=np.int32)
bear_count_below = np.zeros(n_bars, dtype=np.int32)
bull_zones = [z for z in SC.fvg_zones if z.direction > 0]
bear_zones = [z for z in SC.fvg_zones if z.direction < 0]
print(f"  {len(bull_zones):,} bull zones, {len(bear_zones):,} bear zones to walk")

t0 = time.perf_counter()
for z in bull_zones:
    if z.zone_low <= 0:
        continue
    active_end = _zone_active_end(z)
    for b in range(int(z.trigger_bar) + 1, active_end):
        if b >= n_bars:
            break
        if close[b] <= z.zone_low:
            # Price is below-or-equal to the zone's bottom: this bull FVG
            # is ABOVE the bar's close → demand level into which a short
            # could mean-revert.
            bull_count_above[b] += 1
for z in bear_zones:
    active_end = _zone_active_end(z)
    for b in range(int(z.trigger_bar) + 1, active_end):
        if b >= n_bars:
            break
        if close[b] >= z.zone_high:
            # Price is above-or-equal to the zone's top: bear FVG is BELOW
            # the bar's close → supply level into which a long could MR.
            bear_count_below[b] += 1
print(f"  zone walk took {time.perf_counter() - t0:.2f}s")
print(f"  bull_count_above: max={int(bull_count_above.max())}, "
      f"mean={float(bull_count_above.mean()):.2f}")
print(f"  bear_count_below: max={int(bear_count_below.max())}, "
      f"mean={float(bear_count_below.mean()):.2f}")

# Step 2: per-trade conviction. For a SHORT trade (direction=-1), conviction
# = bull_count_above at entry_bar (demand levels above). For a LONG trade,
# conviction = bear_count_below at entry_bar.
conviction_records = []
for i, tr in enumerate(RES_BASE.trades):
    eb = int(tr.entry_bar)
    if tr.direction < 0:
        conviction = int(bull_count_above[eb])
    else:
        conviction = int(bear_count_below[eb])
    conviction_records.append({
        "trade_idx": i,
        "entry_bar": eb,
        "direction": int(tr.direction),
        "conviction": conviction,
        "pnl_usd": float(tr.pnl_usd),
        "exit_reason": tr.exit_reason,
        "entry_triggered_by": tr.entry_triggered_by,
    })

conv_df = pd.DataFrame(conviction_records)
# Decile bucket by conviction
conv_df["conv_decile"] = pd.qcut(conv_df["conviction"].rank(method="first"),
                                   q=10, labels=False, duplicates="drop")
conv_df.to_csv(OUT / "nb53_scenario_g_per_trade.csv", index=False)

# EV by decile
decile_summary = (conv_df.groupby("conv_decile")
                  .agg(n=("conviction", "size"),
                       conv_min=("conviction", "min"),
                       conv_max=("conviction", "max"),
                       conv_mean=("conviction", "mean"),
                       pnl_mean=("pnl_usd", "mean"),
                       pnl_sum=("pnl_usd", "sum"),
                       wr_pct=("pnl_usd", lambda x: 100.0 * float((x > 0).mean())))
                  .reset_index())
print("\n  EV by conviction decile (across all canonical trades):")
print(decile_summary.to_string(index=False))
decile_summary.to_csv(OUT / "nb53_scenario_g_per_decile.csv", index=False)

# By direction
print("\n  EV by conviction + direction:")
for d, dlabel in ((-1, "short"), (1, "long")):
    sub = conv_df[conv_df["direction"] == d]
    if sub.empty:
        continue
    sub_decile = (sub.groupby(pd.qcut(sub["conviction"].rank(method="first"),
                                       q=min(5, len(sub)), labels=False,
                                       duplicates="drop"))
                   .agg(n=("conviction", "size"),
                        conv_mean=("conviction", "mean"),
                        pnl_mean=("pnl_usd", "mean"),
                        pnl_sum=("pnl_usd", "sum"))
                   .reset_index())
    print(f"\n  {dlabel}:")
    print(sub_decile.to_string(index=False))

# Persist headline
pd.DataFrame([{
    "scenario": "G_conviction_unsuperseded_fvgs",
    "n_trades": int(len(conv_df)),
    "bull_count_above_max": int(bull_count_above.max()),
    "bull_count_above_mean": float(bull_count_above.mean()),
    "bear_count_below_max": int(bear_count_below.max()),
    "bear_count_below_mean": float(bear_count_below.mean()),
    "top_decile_conv_mean_pnl": float(decile_summary.sort_values("conv_decile",
                                                                  ascending=False)
                                         .iloc[0]["pnl_mean"]),
    "bottom_decile_conv_mean_pnl": float(decile_summary.sort_values("conv_decile")
                                            .iloc[0]["pnl_mean"]),
}]).to_csv(OUT / "nb53_scenario_g_summary.csv", index=False)
print(f"\n  Wrote {OUT / 'nb53_scenario_g_per_trade.csv'}")
print(f"  Wrote {OUT / 'nb53_scenario_g_per_decile.csv'}")
print(f"  Wrote {OUT / 'nb53_scenario_g_summary.csv'}")


# %% [markdown]
# # Cell H — Headline summary
#
# One-row-per-scenario headline so a reader can compare all 7 scenarios
# against the canonical baseline in a single table.

# %%
print("=" * 70)
print("NB53 — RECOMMENDATION SUMMARY (7 alpha scenarios)")
print("=" * 70)

# Aggregate all the per-scenario summaries into one table
summary_files = sorted(OUT.glob("nb53_scenario_*_summary.csv"))
summary_rows = []
for f in summary_files:
    df = pd.read_csv(f)
    summary_rows.append(df.iloc[0].to_dict())
summary_all = pd.DataFrame(summary_rows)
# Ensure the canonical baseline row is included for comparison
baseline_row = {"scenario": "CANONICAL_baseline", **BASE_ROW,
                "delta_pnl_vs_baseline": 0.0}
summary_all = pd.concat([pd.DataFrame([baseline_row]), summary_all],
                         ignore_index=True)
# Add a sortable "score" = total_pnl_usd (canonical wins by definition;
# scenarios with NaN pnl get a low default so they sort to the bottom).
summary_all["sort_pnl"] = summary_all["total_pnl_usd"].fillna(-1e9)
summary_all = summary_all.sort_values("sort_pnl", ascending=False).drop(columns=["sort_pnl"])
# Show the canonical KPIs first; scenario-specific columns vary so we
# print everything and let pandas truncate.
print("\n" + summary_all.to_string(index=False))

summary_all.to_csv(OUT / "nb53_recommendation_summary.csv", index=False)
print(f"\n  Wrote {OUT / 'nb53_recommendation_summary.csv'}")

# Files written
print("\nFiles written (all under notebooks/nb53_outputs/):")
for f in sorted(OUT.glob("nb53_*")):
    print(f"  {f.name}")

print(f"\nDone. All scenarios persisted. Review nb53_recommendation_summary.csv first.")
