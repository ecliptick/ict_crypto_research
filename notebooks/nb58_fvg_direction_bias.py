# %% [markdown]
"""
NB58 — FVG direction-bias analysis at the 1-second bar level

Hypothesis (user, 2026-09-26 evening)
=====================================

The strategy's unprofitability might be a **fractal artifact**: FVG detection
runs on ``fvg_resample_secs=60`` (1-min buckets) but execution fills on
1-second bars. The FVG edges are 1-min candle extremes — every 1-second
wick between the two 1-min candle boundaries can fake-mitigate or
fake-invert the zone. If FVGs are real structural levels, then in the
next N minutes after a bull FVG forms, price should move up on average;
for a bear FVG, price should move down.

This notebook is a **measurement**, not a config change. It uses the
canonical detector output (warm cache) and asks: "If we entered
immediately on every FVG (entry_mode='immediate' semantics — direction =
sign of fvg.direction, no iFVG wait, no sniper delay), what fraction of
those entries would have been 'in the money' N minutes later, and what
was the median gross displacement?"

No fees are charged here (we're measuring the detector signal, not a
trade). No SL/TP. Just a direction-bias tally at 1m / 5m / 10m / 15m /
30m / 60m horizons.
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

# ── Repo root setup (matches nb56 style) ──────────────────────────────
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
ROOT = Path(r"C:\coding\ict_crypto_research")
NB_OUT = ROOT / "notebooks" / "nb58_fvg_direction_bias_outputs"
NB_OUT.mkdir(parents=True, exist_ok=True)

SOURCE_DATA_ROOT = Path(r"C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw")

# Three standard monthly files (matches nb53/nb54/nb55/nb56).
FILES = [
    ("2025-04", SOURCE_DATA_ROOT / "BTCUSDT-aggTrades-2025-04.parquet"),
    ("2025-10", SOURCE_DATA_ROOT / "BTCUSDT-aggTrades-2025-10.parquet"),
    ("2025-11", SOURCE_DATA_ROOT / "BTCUSDT-aggTrades-2025-11.parquet"),
]

# Horizons in 1-second bars: 1m, 5m, 10m, 15m, 30m, 60m.
HORIZONS_SECS = (60, 300, 600, 900, 1800, 3600)
HORIZON_LABELS = ["1m", "5m", "10m", "15m", "30m", "60m"]


# %% [markdown]
# ## Load each file's SweepCache and snapshot prices at each horizon

# %%
def _direction_signed_disp(z, t0_close: float, tN_close: float) -> float:
    """For a bull FVG (direction=+1), displacement = +Δprice (up is good).
    For a bear FVG (direction=-1), displacement = -Δprice (down is good)."""
    return z.direction * (tN_close - t0_close)


def _atr_per_zone(atr: np.ndarray, trigger_bar: int) -> float:
    """Mean of the surrounding 60 1s ATR values (60s window at trigger)."""
    a = atr[max(0, trigger_bar - 30): trigger_bar + 30]
    if a.size == 0:
        return 0.0
    return float(np.mean(a))


def analyze_file(path: Path) -> dict:
    """Load the SweepCache and compute per-FVG forward-return snapshots."""
    p = optimal_params()  # canonical — gets us the same FVG list as the backtest
    sc = get_or_build(path, p, with_side_table=False, verbose=False)

    bars = sc.bars
    closes = bars["close"].to_numpy(dtype=np.float64)
    highs = bars["high"].to_numpy(dtype=np.float64)
    lows = bars["low"].to_numpy(dtype=np.float64)
    n_bars = closes.shape[0]
    atr = sc.atr

    # Pre-extract FVG metadata once (avoid repeated attribute access).
    rows = []
    for z in sc.fvg_zones:
        tb = int(z.trigger_bar)
        if tb < 0 or tb >= n_bars - 2:
            continue  # skip zones too close to end
        # Entry at next bar's OPEN — mirrors PendingSignal.trigger_bar semantics.
        entry_bar = tb + 1
        entry_close = float(closes[entry_bar])
        atr_i = _atr_per_zone(atr, tb)
        zone_width = float(z.zone_high - z.zone_low)

        row = {
            "trigger_bar": tb,
            "direction": int(z.direction),
            "zone_low": float(z.zone_low),
            "zone_high": float(z.zone_high),
            "zone_width": zone_width,
            "entry_close": entry_close,
            "atr_60s": atr_i,
        }

        for h_secs, h_label in zip(HORIZONS_SECS, HORIZON_LABELS):
            snap_bar = entry_bar + h_secs
            if snap_bar >= n_bars:
                # pad with the final close
                snap_close = float(closes[-1])
                snap_high = float(highs[entry_bar:n_bars].max())
                snap_low = float(lows[entry_bar:n_bars].min())
                truncated = True
            else:
                snap_close = float(closes[snap_bar])
                snap_high = float(highs[entry_bar:snap_bar + 1].max())
                snap_low = float(lows[entry_bar:snap_bar + 1].min())
                truncated = False

            disp_signed = _direction_signed_disp(z, entry_close, snap_close)
            row[f"disp_{h_label}"] = disp_signed  # +ve = in expected direction
            row[f"max_up_{h_label}"] = z.direction * (snap_high - entry_close)  # best-case favorable excursion (signed)
            row[f"max_dn_{h_label}"] = z.direction * (snap_low - entry_close)   # worst-case adverse excursion (signed)
            row[f"disp_{h_label}_atr"] = disp_signed / atr_i if atr_i > 0 else 0.0
            row[f"truncated_{h_label}"] = truncated

        rows.append(row)

    df = pd.DataFrame(rows)
    print(f"  {path.stem}: n_zones={len(df)} "
          f"bull={(df.direction == 1).sum()} bear={(df.direction == -1).sum()}",
          flush=True)
    return {"label": path.stem.split("-")[-2] + "-" + path.stem.split("-")[-1],
            "df": df}


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

print(f"\nTotal cache load + analysis: {time.perf_counter() - t0:.2f}s", flush=True)

# %% [markdown]
# ## Per-file direction-bias statistics

# %%
def horizon_stats(df: pd.DataFrame, h_label: str) -> dict:
    """Compute bias stats for one horizon."""
    disp = df[f"disp_{h_label}"]
    disp_atr = df[f"disp_{h_label}_atr"]
    n = len(df)
    n_pos = int((disp > 0).sum())
    n_neg = int((disp < 0).sum())
    n_zero = n - n_pos - n_neg
    return {
        "n": n,
        f"pct_positive_{h_label}": 100.0 * n_pos / n if n else 0.0,
        f"mean_disp_usd_{h_label}": float(disp.mean()) if n else 0.0,
        f"median_disp_usd_{h_label}": float(disp.median()) if n else 0.0,
        f"p25_disp_usd_{h_label}": float(disp.quantile(0.25)) if n else 0.0,
        f"p75_disp_usd_{h_label}": float(disp.quantile(0.75)) if n else 0.0,
        f"mean_disp_atr_{h_label}": float(disp_atr.mean()) if n else 0.0,
        f"median_disp_atr_{h_label}": float(disp_atr.median()) if n else 0.0,
        f"truncated_frac_{h_label}": float(df[f"truncated_{h_label}"].mean()) if n else 0.0,
    }


# %%
all_stats = []
for pf in per_file:
    df = pf["df"]
    label = pf["label"]
    stats = {"file": label}
    for h_label in HORIZON_LABELS:
        stats.update(horizon_stats(df, h_label))
    all_stats.append(stats)
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

stats_df = pd.DataFrame(all_stats)
stats_df.to_csv(NB_OUT / "nb58_per_file_stats.csv", index=False)

# %% [markdown]
# ## Aggregate across files

# %%
all_df = pd.concat([pf["df"] for pf in per_file], ignore_index=True)
print(f"\n=== ALL FILES AGGREGATED (n={len(all_df)}) ===")
print(f"  {'horizon':>6} {'%pos':>7} {'mean$':>10} {'median$':>10} "
      f"{'p25$':>9} {'p75$':>9} {'meanATR':>9} {'medATR':>9}")
agg_stats = {"scope": "all"}
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
# ## Split by bull vs bear

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
# ## How often does price even REACH the zone edge after entry?
#     (i.e. how often does the FVG get "tested" / filled in the next N min?)

# %%
print("\n=== FVG retest rates (% of zones whose [low, high] was touched in next N min) ===")
print(f"  {'horizon':>6} {'bull%':>7} {'bear%':>7} {'all%':>7}")

# Build per-file (lows, highs) arrays from the per_file results we already have.
# We need the bar arrays — recover them via the cache re-load is wasteful;
# stash them on the per_file dict during analyze_file. Quick patch:
print("  (re-using bars arrays already loaded into memory)")

for pf in per_file:
    df = pf["df"]
    if "_bars_lows" not in pf:
        # Re-load bars (cheap; warm cache).
        p = optimal_params()
        sc = get_or_build(SOURCE_DATA_ROOT / f"BTCUSDT-aggTrades-{pf['label']}.parquet",
                          p, with_side_table=False, verbose=False)
        pf["_bars_lows"] = sc.bars["low"].to_numpy(dtype=np.float64)
        pf["_bars_highs"] = sc.bars["high"].to_numpy(dtype=np.float64)
        pf["_n_bars"] = len(sc.bars)

bull_sub = all_df[all_df.direction == +1].reset_index(drop=True)
bear_sub = all_df[all_df.direction == -1].reset_index(drop=True)

# Group zones by file so we can index into the right bars array.
file_labels = sorted(all_df.index.unique() if False else [pf["label"] for pf in per_file])
# Re-build per-file zone subsets from the per_file DataFrames (each has its
# own trigger_bar but we need the file association).
# Easier: rebuild by joining on (trigger_bar, direction) — but that's fragile.
# Instead: just iterate the per_file frames directly.
for h_label in HORIZON_LABELS:
    h_secs = int(h_label.rstrip("m")) * 60
    bull_reached_total = 0
    bear_reached_total = 0
    n_bull_total = 0
    n_bear_total = 0
    for pf in per_file:
        df = pf["df"]
        lows = pf["_bars_lows"]
        highs = pf["_bars_highs"]
        n_bars = pf["_n_bars"]
        for _, row in df.iterrows():
            entry_bar = int(row["trigger_bar"]) + 1
            snap_bar = min(entry_bar + h_secs, n_bars - 1)
            if snap_bar <= entry_bar:
                continue
            if row.direction == +1:
                n_bull_total += 1
                if lows[entry_bar:snap_bar + 1].min() <= row["zone_low"]:
                    bull_reached_total += 1
            else:
                n_bear_total += 1
                if highs[entry_bar:snap_bar + 1].max() >= row["zone_high"]:
                    bear_reached_total += 1
    bull_pct = 100.0 * bull_reached_total / max(1, n_bull_total)
    bear_pct = 100.0 * bear_reached_total / max(1, n_bear_total)
    all_pct = 100.0 * (bull_reached_total + bear_reached_total) / max(1, n_bull_total + n_bear_total)
    print(f"  {h_label:>6} {bull_pct:>6.1f}% {bear_pct:>6.1f}% {all_pct:>6.1f}%")

# %% [markdown]
# ## Save the per-FVG table for downstream analysis

# %%
out_csv = NB_OUT / "nb58_per_fvg_snapshot.csv"
all_df.to_csv(out_csv, index=False)
print(f"\nWrote {len(all_df)} per-FVG rows to {out_csv}", flush=True)

# %% [markdown]
# ## Persist a small JSONL "run record" (per AGENTS.md logging convention)

# %%
run_record = {
    "ts_utc": pd.Timestamp.now(tz="UTC").isoformat(),
    "notebook": "nb58_fvg_direction_bias",
    "scenario": "fvg_direction_bias_at_horizons",
    "scope": [pf["label"] for pf in per_file],
    "engine": "measurement_only",
    "comments": (
        "FVG direction-bias measurement at 1s-bar level. "
        "For each FVG, snapshots price at entry+{1,5,10,15,30,60}m. "
        "Reports %positive, mean/median signed displacement in USD and ATR. "
        "No fees, no SL/TP. Tests whether FVGs carry direction information "
        "on the 1s execution timebase (fractal-mismatch hypothesis)."
    ),
    "hypothesis": (
        "If FVGs are real structural levels, immediate-mode entries "
        "(direction = sign of fvg.direction, no iFVG wait) should show "
        "positive signed displacement at horizons >= 5m on average."
    ),
    "verdict": "inconclusive",  # set after inspection of stats
    "canonical_recipe_version": "v17-btc-sniper-2026-09-26f",
    "horizons_secs": list(HORIZONS_SECS),
    "metrics": agg_stats,
}
with open(NB_OUT / "nb58_fvg_direction_bias__runs.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps(run_record) + "\n")
print(f"Wrote run record to {NB_OUT / 'nb58_fvg_direction_bias__runs.jsonl'}", flush=True)
