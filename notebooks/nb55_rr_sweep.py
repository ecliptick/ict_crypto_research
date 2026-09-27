"""NB55 — quick RR sweep on a single BTC month (2025-04).

The dominant entry path on the canonical recipe is ``ifvg_clean``,
which uses ATR-anchored SL/TP:
  stop_usd  = atr × sl_atr_mult
  target_usd = atr × tp_atr_mult
  RR        = tp_atr_mult / sl_atr_mult

The user asked: "with enforce tick and $20 bar breadth, does 5:45
RR work?" The answer is: 5:45 RR is the SNIPER path's zone-width
multipliers, but on this month's data NO sniper entries fire
(all 54 trades are ``ifvg_clean``). So the meaningful RR knob
is the ATR-anchored pair ``sl_atr_mult`` / ``tp_atr_mult``.

Sparse grid: 4×7 = 28 configs covering RRs from 1.5 to 7.0:

  sl_atr_mult ∈ {0.10, 0.15, 0.25, 0.40}
  tp_atr_mult ∈ {0.25, 0.40, 0.55, 0.75, 1.10, 1.50, 2.00}

The grid covers RR ratios (tp/sl) ∈ {1.5, 1.83, 2.2, 2.5, 2.75,
3.0, 3.67, 4.0, 4.4, 5.0, 5.5, 6.25, 7.0, 7.33, 8.0, 10.0, 11.0,
13.33, 15.0, 20.0}.

Single file: BTCUSDT-aggTrades-2025-04.parquet.

Run modes:
  * default: 28 configs on 2025-04 (~3 min wall; first config
    cold cache, rest warm).
  * ``NB55_FILE=<name>``: run on a different file from the
    6-month nb52 sample.

Output: ``notebooks/nb55_outputs/``:
  * ``nb55_rr_sweep_per_config.csv`` — per-config summary
  * ``nb55_rr_sweep_summary.csv`` — one-row headline with best RR
"""
from __future__ import annotations

import json
import os
import sys
import time
import warnings
from pathlib import Path

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

warnings.filterwarnings("ignore")

ROOT = None
for _c in [Path(".").resolve(), *Path(".").resolve().parents]:
    if (_c / "src" / "core" / "ict_signals.py").is_file():
        ROOT = _c
        break
if ROOT is None:
    raise RuntimeError("Could not find ICT repo root.")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "notebooks"))

import numpy as np
import pandas as pd

from src.core.optimal_config import optimal_params
from src.tick.cache import get_or_build
from src.tick.tick_backtest import run_tick_backtest

SOURCE_DATA_ROOT = Path(r"C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw")
OUT = ROOT / "notebooks" / "nb55_outputs"
OUT.mkdir(parents=True, exist_ok=True)

# Use the same 6-month sample nb52 chose, default to 2025-04.
SAMPLE_6_JSON = ROOT / "notebooks" / "nb52_outputs" / "nb52_sample_6.json"
sample_names = json.loads(SAMPLE_6_JSON.read_text())
SAMPLE_6 = [SOURCE_DATA_ROOT / name for name in sample_names]

FILE_OVERRIDE = os.environ.get("NB55_FILE", "").strip()
if FILE_OVERRIDE:
    SAMPLE = [SOURCE_DATA_ROOT / FILE_OVERRIDE]
    if not SAMPLE[0].exists():
        raise FileNotFoundError(f"NB55_FILE={FILE_OVERRIDE} not found in raw dir")
else:
    SAMPLE = SAMPLE_6  # all 6 months

FAST = os.environ.get("NB55_FAST", "0") == "1"
if FAST:
    SAMPLE = SAMPLE[:1]

print(f"\nNB55 RR sweep on {len(SAMPLE)} file(s):")
for p in SAMPLE:
    print(f"  {p.name}")

# Grid: ATR-anchored SL/TP (canonical: 0.25 / 0.55 → RR 2.2)
SL_GRID = [0.10, 0.15, 0.25, 0.40]
TP_GRID = [0.25, 0.40, 0.55, 0.75, 1.10, 1.50, 2.00]

# Filter to combos that produce RR >= 1.5 and <= 20 (sanity bounds)
configs = []
for sl in SL_GRID:
    for tp in TP_GRID:
        rr = tp / sl
        if rr < 1.4 or rr > 20:
            continue
        configs.append((sl, tp, rr))
print(f"Grid: {len(configs)} configs "
      f"(SL x TP grid = {len(SL_GRID)}x{len(TP_GRID)})")

# Run all configs across all files. Cached zone structure is shared
# (the ATR mults are not in _DETECTOR_PARAM_KEYS — they're bar-loop
# geometry). So only the FIRST config on EACH file needs a cold
# cache build; subsequent configs on the same file reuse the cache.
results: list[dict] = []
t_overall = time.perf_counter()
cache_state: dict = {}  # path.name -> SweepCache

for fi, path in enumerate(SAMPLE):
    sc = None
    for i, (sl, tp, rr) in enumerate(configs):
        cfg_id = f"sl{sl:.2f}_tp{tp:.2f}_rr{rr:.2f}"
        try:
            t0 = time.perf_counter()
            p = optimal_params(sl_atr_mult=sl, tp_atr_mult=tp)
            if sc is None:
                sc = get_or_build(path, p, with_side_table=True, verbose=False)
                print(f"  [{fi+1}/{len(SAMPLE)}] cold cache build "
                      f"({path.name}): {time.perf_counter() - t0:.1f}s", flush=True)
                t0 = time.perf_counter()
            res = run_tick_backtest(
                raw_df=None, p=p, strategy_label=f"nb55_rr_{cfg_id}",
                pre_aggregated_bars=sc.bars, precomputed_structure=sc.structure,
                precomputed_atr=sc.atr,
                precomputed_zones_by_src={"fvg": sc.fvg_zones,
                                           "ifvg": sc.ifvg_zones},
                side_table=sc.side_table,
            )
            bt_s = time.perf_counter() - t0
            trades = res.trades
            pnls = [t.pnl_usd for t in trades]
            fees = [t.fee_usd for t in trades]
            n = len(trades)
            from collections import Counter
            paths = Counter(t.entry_triggered_by for t in trades)
            row = {
                "file": path.name,
                "config_id": cfg_id,
                "sl_atr_mult": sl,
                "tp_atr_mult": tp,
                "rr": rr,
                "n_trades": n,
                "total_pnl_usd": float(sum(pnls)) if pnls else 0.0,
                "ev_per_trade_usd": float(sum(pnls) / n) if n else 0.0,
                "win_rate_pct": 100.0 * sum(1 for p in pnls if p > 0) / n if n else 0.0,
                "net_pnl_usd": float(sum(p - f for p, f in zip(pnls, fees))) if pnls else 0.0,
                "n_tp": sum(1 for t in trades if t.exit_reason == "tp"),
                "n_sl": sum(1 for t in trades if t.exit_reason == "sl"),
                "n_inv": sum(1 for t in trades if t.exit_reason == "inv"),
                "n_ifvg_clean": paths.get("ifvg_clean", 0),
                "n_sniper": paths.get("sniper", 0) + paths.get("sniper_dirty", 0),
                "elapsed_s": bt_s,
            }
            results.append(row)
            elapsed = time.perf_counter() - t_overall
            print(f"  [{fi+1}/{len(SAMPLE)}.{i+1:2d}/{len(configs):2d}] "
                  f"{path.name} {cfg_id:<30s} "
                  f"n={n:3d} pnl=${row['total_pnl_usd']:+9.4f} "
                  f"EV=${row['ev_per_trade_usd']:+7.4f} "
                  f"WR={row['win_rate_pct']:5.1f}% "
                  f"tp={row['n_tp']:2d} sl={row['n_sl']:2d} inv={row['n_inv']:2d} "
                  f"({bt_s:.2f}s bt) [wall {elapsed:.1f}s]", flush=True)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            print(f"  ! {path.name} cfg={cfg_id} FAILED: {exc}", flush=True)

t_total = time.perf_counter() - t_overall
df = pd.DataFrame(results)
df.to_csv(OUT / "nb55_rr_sweep_per_config.csv", index=False)
print(f"\n  Wrote {OUT / 'nb55_rr_sweep_per_config.csv'}")

# Aggregate across files per config
agg = (df.groupby(["config_id", "sl_atr_mult", "tp_atr_mult", "rr"])
         .agg(n_files=("file", "count"),
              total_trades=("n_trades", "sum"),
              total_pnl_usd=("total_pnl_usd", "sum"),
              mean_pnl_usd=("total_pnl_usd", "mean"),
              sum_ev_per_trade_usd=("ev_per_trade_usd",
                                    lambda x: float(np.average(x, weights=df.loc[x.index, "n_trades"]))),
              mean_wr_pct=("win_rate_pct", "mean"),
              total_tp=("n_tp", "sum"),
              total_sl=("n_sl", "sum"),
              total_inv=("n_inv", "sum"))
         .reset_index()
         .sort_values("total_pnl_usd", ascending=False))

print("\n" + "=" * 110)
print("PER-CONFIG 6-MONTH SUMMARY (sorted by total PnL)")
print("=" * 110)
display_cols = ["config_id", "rr", "n_files", "total_trades",
                "total_pnl_usd", "mean_pnl_usd",
                "sum_ev_per_trade_usd", "mean_wr_pct",
                "total_tp", "total_sl", "total_inv"]
print(agg[display_cols].to_string(index=False))
print()

# Headline: canonical row (sl=0.25 tp=0.55 rr=2.20) + best rows
CANONICAL_SL = 0.25
CANONICAL_TP = 0.55
canonical_mask = (np.isclose(df["sl_atr_mult"], CANONICAL_SL) &
                  np.isclose(df["tp_atr_mult"], CANONICAL_TP))
canonical_df = df[canonical_mask]
canonical_total_pnl = float(canonical_df["total_pnl_usd"].sum())
canonical_total_trades = int(canonical_df["n_trades"].sum())
canonical_mean_wr = float(canonical_df["win_rate_pct"].mean())

best_by_pnl = agg.sort_values("total_pnl_usd", ascending=False).head(1)
best_by_ev = agg.sort_values("sum_ev_per_trade_usd", ascending=False).head(1)

summary = pd.DataFrame([{
    "n_configs": len(agg),
    "n_files": len(SAMPLE),
    "canonical_sl_atr_mult": CANONICAL_SL,
    "canonical_tp_atr_mult": CANONICAL_TP,
    "canonical_rr": round(CANONICAL_TP / CANONICAL_SL, 4),
    "canonical_total_trades": canonical_total_trades,
    "canonical_total_pnl_usd": canonical_total_pnl,
    "canonical_mean_wr_pct": canonical_mean_wr,
    "best_pnl_config_id": str(best_by_pnl["config_id"].iloc[0]),
    "best_pnl_sl_atr_mult": float(best_by_pnl["sl_atr_mult"].iloc[0]),
    "best_pnl_tp_atr_mult": float(best_by_pnl["tp_atr_mult"].iloc[0]),
    "best_pnl_rr": float(best_by_pnl["rr"].iloc[0]),
    "best_pnl_total_pnl_usd": float(best_by_pnl["total_pnl_usd"].iloc[0]),
    "best_pnl_total_trades": int(best_by_pnl["total_trades"].iloc[0]),
    "best_pnl_mean_wr_pct": float(best_by_pnl["mean_wr_pct"].iloc[0]),
    "best_ev_config_id": str(best_by_ev["config_id"].iloc[0]),
    "best_ev_sl_atr_mult": float(best_by_ev["sl_atr_mult"].iloc[0]),
    "best_ev_tp_atr_mult": float(best_by_ev["tp_atr_mult"].iloc[0]),
    "best_ev_rr": float(best_by_ev["rr"].iloc[0]),
    "best_ev_total_pnl_usd": float(best_by_ev["total_pnl_usd"].iloc[0]),
    "best_ev_total_trades": int(best_by_ev["total_trades"].iloc[0]),
    "best_ev_mean_wr_pct": float(best_by_ev["mean_wr_pct"].iloc[0]),
    "best_ev_sum_ev_per_trade_usd": float(best_by_ev["sum_ev_per_trade_usd"].iloc[0]),
    "wall_time_s": float(t_total),
}])
summary.to_csv(OUT / "nb55_rr_sweep_summary.csv", index=False)

print("=" * 110)
print("HEADLINE")
print("=" * 110)
print(f"  canonical: SL={CANONICAL_SL} TP={CANONICAL_TP} RR=2.20  "
      f"n={canonical_total_trades}  pnl=${canonical_total_pnl:+.4f}  "
      f"WR={canonical_mean_wr:.1f}%")
print(f"  best by total PnL: {summary['best_pnl_config_id'].iloc[0]}  "
      f"SL={summary['best_pnl_sl_atr_mult'].iloc[0]} "
      f"TP={summary['best_pnl_tp_atr_mult'].iloc[0]} "
      f"RR={summary['best_pnl_rr'].iloc[0]:.2f}  "
      f"n={summary['best_pnl_total_trades'].iloc[0]}  "
      f"pnl=${summary['best_pnl_total_pnl_usd'].iloc[0]:+.4f}  "
      f"WR={summary['best_pnl_mean_wr_pct'].iloc[0]:.1f}%")
print(f"  best by EV/trade:  {summary['best_ev_config_id'].iloc[0]}  "
      f"SL={summary['best_ev_sl_atr_mult'].iloc[0]} "
      f"TP={summary['best_ev_tp_atr_mult'].iloc[0]} "
      f"RR={summary['best_ev_rr'].iloc[0]:.2f}  "
      f"n={summary['best_ev_total_trades'].iloc[0]}  "
      f"pnl=${summary['best_ev_total_pnl_usd'].iloc[0]:+.4f}  "
      f"EV=${summary['best_ev_sum_ev_per_trade_usd'].iloc[0]:+.4f}  "
      f"WR={summary['best_ev_mean_wr_pct'].iloc[0]:.1f}%")
print()

print(f"Done. Wall clock: {t_total:.1f}s.")
