# %% [markdown]
"""
NB59 — FVG direction-bias measured on 1-MINUTE bars (matching detector cadence)

NB58 measured FVG direction-bias on the 1-second execution timebase
and found direction-neutrality (~49% positive at all horizons,
median signed displacement negative for bull FVGs at 5-15m).

Two interpretations:

(A) "Fractal mismatch" — the detector finds zones on 1-min candles
    but execution lives on 1-second noise. Zones are real but the
    1s execution adds random walks that wash out the signal.

(B) "FVGs are direction-neutral" — even on the same timebase the
    detector runs on, the next 15 min of 1-min candles don't
    preferentially continue in the FVG's direction.

This notebook runs the NB58 measurement again, but on 1-MINUTE
resampled bars. If interpretation (A) is correct, %positive jumps
materially (say >55% on 15-min horizon). If (B) is correct, the
stats stay near 50/50.
"""

# %% [markdown]
# ## Setup

# %%
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

# ── Repo root setup ──────────────────────────────────────────────────
ROOT = None
for _c in [Path(".").resolve(), *Path(".").resolve().parents]:
    if (_c / "src" / "core" / "ict_signals.py").is_file():
        ROOT = _c
        break
if ROOT is None:
    raise RuntimeError("Could not find ICT repo root.")
sys.path.insert(0, str(ROOT))

from src.core.optimal_config import optimal_params
from src.tick.cache import get_or_build

# %%
NB_OUT = ROOT / "notebooks" / "nb59_fvg_direction_bias_1min_outputs"
NB_OUT.mkdir(parents=True, exist_ok=True)

SOURCE_DATA_ROOT = Path(r"C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw")

FILES = [
    ("2025-04", SOURCE_DATA_ROOT / "BTCUSDT-aggTrades-2025-04.parquet"),
    ("2025-10", SOURCE_DATA_ROOT / "BTCUSDT-aggTrades-2025-10.parquet"),
    ("2025-11", SOURCE_DATA_ROOT / "BTCUSDT-aggTrades-2025-11.parquet"),
]

# Horizons in 1-MINUTE bars (1m, 5m, 10m, 15m, 30m, 60m).
HORIZONS_BARS_1MIN = (1, 5, 10, 15, 30, 60)
HORIZON_LABELS = ["1m", "5m", "10m", "15m", "30m", "60m"]


# %% [markdown]
# ## Resample 1s bars → 1m bars, snapshot prices at each horizon

# %%
def _resample_1s_to_1m(bars_1s: pd.DataFrame) -> pd.DataFrame:
    """Standard OHLCV resample from 1-second to 1-minute bars.

    Input bars have columns time, open, high, low, close, volume, ...
    Output: same columns, indexed by 1-min boundaries (UTC), open=first,
    high=max, low=min, close=last, volume=sum.
    """
    out = bars_1s.set_index("time").resample("1min").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "notional": "sum",
        "n_trades": "sum",
        "n_taker_sell": "sum",
        "taker_sell_vol": "sum",
        "taker_buy_vol": "sum",
    }).dropna(subset=["open"]).reset_index()
    return out


def _atr_per_zone_1m(atr_1s: np.ndarray, trigger_bar_1s: int) -> float:
    """ATR expressed in 1-min units at the trigger.

    Use the 60s mean of 1s ATR scaled by sqrt(60) to convert 1s ATR
    to 1-min ATR (per the variance-additivity property). This gives
    us a comparable ATR normalization for the displacement stats.
    """
    a = atr_1s[max(0, trigger_bar_1s - 30): trigger_bar_1s + 30]
    if a.size == 0:
        return 0.0
    return float(np.mean(a) * np.sqrt(60))


def analyze_file(path: Path) -> dict:
    p = optimal_params()
    sc = get_or_build(path, p, with_side_table=False, verbose=False)
    bars_1s = sc.bars
    closes_1s = bars_1s["close"].to_numpy(dtype=np.float64)
    n_bars_1s = closes_1s.shape[0]
    atr_1s = sc.atr

    # Resample to 1-min bars
    t0 = time.perf_counter()
    bars_1m = _resample_1s_to_1m(bars_1s)
    closes_1m = bars_1m["close"].to_numpy(dtype=np.float64)
    highs_1m = bars_1m["high"].to_numpy(dtype=np.float64)
    lows_1m = bars_1m["low"].to_numpy(dtype=np.float64)
    n_bars_1m = closes_1m.shape[0]
    print(f"  1s→1m resample: {n_bars_1s} → {n_bars_1m} bars "
          f"in {time.perf_counter() - t0:.2f}s", flush=True)

    # Map each FVG's 1s trigger_bar to a 1m trigger_bar by floor-dividing
    # by 60 (since 1s bar i lives at second i, and 1m bar j aggregates 1s
    # bars [60*j, 60*j+59]).
    rows = []
    for z in sc.fvg_zones:
        tb_1s = int(z.trigger_bar)
        if tb_1s < 0 or tb_1s >= n_bars_1s - 2:
            continue
        tb_1m = tb_1s // 60
        if tb_1m >= n_bars_1m - 1:
            continue  # skip zones in the last 1m bar of the month
        # Entry at next 1m bar's OPEN. (mirrors PendingSignal.trigger_bar semantics)
        entry_bar = tb_1m + 1
        if entry_bar >= n_bars_1m:
            continue
        entry_close = float(closes_1m[entry_bar])
        atr_1m = _atr_per_zone_1m(atr_1s, tb_1s)
        zone_width = float(z.zone_high - z.zone_low)

        row = {
            "trigger_bar_1s": tb_1s,
            "trigger_bar_1m": tb_1m,
            "direction": int(z.direction),
            "zone_low": float(z.zone_low),
            "zone_high": float(z.zone_high),
            "zone_width": zone_width,
            "entry_close": entry_close,
            "atr_1min": atr_1m,
        }

        for h_bars, h_label in zip(HORIZONS_BARS_1MIN, HORIZON_LABELS):
            snap_bar = entry_bar + h_bars
            if snap_bar >= n_bars_1m:
                snap_close = float(closes_1m[-1])
                snap_high = float(highs_1m[entry_bar:n_bars_1m].max())
                snap_low = float(lows_1m[entry_bar:n_bars_1m].min())
                truncated = True
            else:
                snap_close = float(closes_1m[snap_bar])
                snap_high = float(highs_1m[entry_bar:snap_bar + 1].max())
                snap_low = float(lows_1m[entry_bar:snap_bar + 1].min())
                truncated = False

            disp_signed = z.direction * (snap_close - entry_close)
            row[f"disp_{h_label}"] = disp_signed
            row[f"max_up_{h_label}"] = z.direction * (snap_high - entry_close)
            row[f"max_dn_{h_label}"] = z.direction * (snap_low - entry_close)
            row[f"disp_{h_label}_atr"] = disp_signed / atr_1m if atr_1m > 0 else 0.0
            row[f"truncated_{h_label}"] = truncated

        rows.append(row)

    df = pd.DataFrame(rows)
    label = path.stem.split("-")[-2] + "-" + path.stem.split("-")[-1]
    print(f"  {label}: n_zones={len(df)} "
          f"bull={(df.direction == 1).sum()} bear={(df.direction == -1).sum()}",
          flush=True)
    return {"label": label, "df": df, "bars_1m": bars_1m}


# %%
t0 = time.perf_counter()
per_file = []
for label, path in FILES:
    if not path.exists():
        print(f"  WARN: {path} not found, skipping", flush=True)
        continue
    print(f"\n[{label}] loading cache for {path.name}", flush=True)
    t_file = time.perf_counter()
    res = analyze_file(path)
    print(f"  done in {time.perf_counter() - t_file:.2f}s", flush=True)
    per_file.append(res)

print(f"\nTotal: {time.perf_counter() - t0:.2f}s", flush=True)

# %% [markdown]
# ## Per-file + aggregate stats on 1-min timebase

# %%
def horizon_stats(df: pd.DataFrame, h_label: str) -> dict:
    disp = df[f"disp_{h_label}"]
    disp_atr = df[f"disp_{h_label}_atr"]
    n = len(df)
    n_pos = int((disp > 0).sum())
    return {
        "n": n,
        f"pct_positive_{h_label}": 100.0 * n_pos / n if n else 0.0,
        f"mean_disp_usd_{h_label}": float(disp.mean()) if n else 0.0,
        f"median_disp_usd_{h_label}": float(disp.median()) if n else 0.0,
        f"p25_disp_usd_{h_label}": float(disp.quantile(0.25)) if n else 0.0,
        f"p75_disp_usd_{h_label}": float(disp.quantile(0.75)) if n else 0.0,
        f"mean_disp_atr_{h_label}": float(disp_atr.mean()) if n else 0.0,
        f"median_disp_atr_{h_label}": float(disp_atr.median()) if n else 0.0,
    }


for pf in per_file:
    df = pf["df"]
    label = pf["label"]
    stats = {"file": label}
    for h_label in HORIZON_LABELS:
        stats.update(horizon_stats(df, h_label))
    print(f"\n=== {label} (n={stats['n']}) ===")
    print(f"  {'horizon':>6} {'%pos':>7} {'mean$':>10} {'median$':>10} "
          f"{'p25$':>9} {'p75$':>9} {'meanATR':>9} {'medATR':>9}")
    for h_label in HORIZON_LABELS:
        print(f"  {h_label:>6} "
              f"{stats[f'pct_positive_{h_label}']:>6.1f}% "
              f"{stats[f'mean_disp_usd_{h_label}']:>+10.2f} "
              f"{stats[f'median_disp_usd_{h_label}']:>+10.2f} "
              f"{stats[f'p25_disp_usd_{h_label}']:>+9.2f} "
              f"{stats[f'p75_disp_usd_{h_label}']:>+9.2f} "
              f"{stats[f'mean_disp_atr_{h_label}']:>+9.3f} "
              f"{stats[f'median_disp_atr_{h_label}']:>+9.3f}")

all_df = pd.concat([pf["df"] for pf in per_file], ignore_index=True)
print(f"\n=== ALL FILES AGGREGATED (n={len(all_df)}, on 1-min bars) ===")
print(f"  {'horizon':>6} {'%pos':>7} {'mean$':>10} {'median$':>10} "
      f"{'p25$':>9} {'p75$':>9} {'meanATR':>9} {'medATR':>9}")
agg_stats = {"scope": "all_1min"}
for h_label in HORIZON_LABELS:
    s = horizon_stats(all_df, h_label)
    agg_stats.update(s)
    print(f"  {h_label:>6} "
          f"{s[f'pct_positive_{h_label}']:>6.1f}% "
          f"{s[f'mean_disp_usd_{h_label}']:>+10.2f} "
          f"{s[f'median_disp_usd_{h_label}']:>+10.2f} "
          f"{s[f'p25_disp_usd_{h_label}']:>+9.2f} "
          f"{s[f'p75_disp_usd_{h_label}']:>+9.2f} "
          f"{s[f'mean_disp_atr_{h_label}']:>+9.3f} "
          f"{s[f'median_disp_atr_{h_label}']:>+9.3f}")

# %% [markdown]
# ## Bull vs Bear split on 1-min timebase

# %%
for direction, name in [(+1, "BULL_FVG"), (-1, "BEAR_FVG")]:
    sub = all_df[all_df.direction == direction]
    print(f"\n=== {name} (n={len(sub)}) ===")
    print(f"  {'horizon':>6} {'%pos':>7} {'mean$':>10} {'median$':>10} "
          f"{'p25$':>9} {'p75$':>9} {'meanATR':>9} {'medATR':>9}")
    for h_label in HORIZON_LABELS:
        disp = sub[f"disp_{h_label}"]
        n = len(sub)
        n_pos = int((disp > 0).sum())
        mean_atr = float(sub[f"disp_{h_label}_atr"].mean())
        med_atr = float(sub[f"disp_{h_label}_atr"].median())
        print(f"  {h_label:>6} "
              f"{100.0*n_pos/n:>6.1f}% "
              f"{float(disp.mean()):>+10.2f} "
              f"{float(disp.median()):>+10.2f} "
              f"{float(disp.quantile(0.25)):>+9.2f} "
              f"{float(disp.quantile(0.75)):>+9.2f} "
              f"{mean_atr:>+9.3f} "
              f"{med_atr:>+9.3f}")

# %% [markdown]
# ## Save outputs

# %%
out_csv = NB_OUT / "nb59_per_fvg_snapshot_1min.csv"
all_df.to_csv(out_csv, index=False)
print(f"\nWrote {len(all_df)} per-FVG rows to {out_csv}", flush=True)

run_record = {
    "ts_utc": pd.Timestamp.now(tz="UTC").isoformat(),
    "notebook": "nb59_fvg_direction_bias_1min",
    "scenario": "fvg_direction_bias_on_1min_bars",
    "scope": [pf["label"] for pf in per_file],
    "engine": "measurement_only",
    "comments": (
        "Re-runs NB58 on the 1-min execution timebase. "
        "Same FVG detector output; bars resampled to 1-min before snapshot. "
        "If fractal-mismatch (1s noise) was the dominant problem, %positive "
        "should jump materially above NB58's ~49% baseline."
    ),
    "hypothesis": (
        "If 1-second execution noise was masking the FVG signal, "
        "running the same direction-bias measurement on 1-min bars "
        "(matching detector cadence) should show >55% direction-positive "
        "at 15-min horizon. NB58 showed ~49% on 1s; ~50/50 means "
        "fractal mismatch is NOT the dominant problem."
    ),
    "verdict": "inconclusive",  # set after inspection
    "canonical_recipe_version": "v17-btc-sniper-2026-09-26f",
    "horizons_1min_bars": list(HORIZONS_BARS_1MIN),
    "metrics": agg_stats,
}
with open(NB_OUT / "nb59_fvg_direction_bias_1min__runs.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps(run_record) + "\n")
print(f"Wrote run record to {NB_OUT / 'nb59_fvg_direction_bias_1min__runs.jsonl'}", flush=True)
