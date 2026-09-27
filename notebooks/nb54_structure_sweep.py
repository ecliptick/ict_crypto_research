"""NB54 — sparse market-structure sweep.

Sparse grid over the four market-structure knobs that affect
trade selection on 1s BTC bars:

  * ``ms_pivot_len`` ∈ {5, 9, 15}
  * ``ms_liquidity_len`` ∈ {30, 60}
  * ``ms_resample_secs`` ∈ {0, 60}   (1s vs 1m pivots — the
                                      resolution-mismatch open
                                      work item from AGENTS.md)
  * ``fvg_invalidate_on_structure`` ∈ {False, True}
  * ``fvg_structure_invalidation_age_secs`` ∈ {300, 900}

3 × 2 × 2 × 2 × 2 = 48 configs.

Run on the same 6-month sample nb52 chose
(``nb52_outputs/nb52_sample_6.json``, seed 20260925) so this
sweep is directly comparable to nb52's results.

Run modes:
  * default: 48 configs × 6 files = 288 tasks (~25-30 min wall
    on 8 threads; cold cache build dominates the first file's
    configs).
  * ``NB54_FAST=1``: 48 configs × 1 file = 48 tasks (~5-7 min
    wall for the smoke test).

Outputs to ``notebooks/nb54_outputs/``:

  * ``nb54_structure_sweep_per_run.csv`` — one row per (file, cfg)
  * ``nb54_structure_sweep_per_config.csv`` — aggregate by config
    across the 6 files (mean / median / sum / std of PnL + EV)
  * ``nb54_structure_sweep_summary.csv`` — one-row headline
    (canonical row + best-by-EV row)
  * ``nb54_structure_sweep_heatmap.png`` — heatmap of mean PnL by
    (ms_pivot_len, ms_resample_secs) for the canonical
    invalidate/age axis choice
"""
from __future__ import annotations

import json
import os
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import product
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

from notebooks._nb54_worker import worker_structure_sweep  # noqa: E402

# Raw data root (mirror of nb52/nb53)
SOURCE_DATA_ROOT = Path(r"C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw")
OUT = ROOT / "notebooks" / "nb54_outputs"
OUT.mkdir(parents=True, exist_ok=True)

# Reproducible 6-month sample — same as nb52 / nb53.
SAMPLE_6_JSON = ROOT / "notebooks" / "nb52_outputs" / "nb52_sample_6.json"
sample_names = json.loads(SAMPLE_6_JSON.read_text())
SAMPLE_6 = [SOURCE_DATA_ROOT / name for name in sample_names]
assert all(p.exists() for p in SAMPLE_6), \
    f"missing files: {[p for p in SAMPLE_6 if not p.exists()]}"

FAST = os.environ.get("NB54_FAST", "0") == "1"
if FAST:
    SAMPLE_6 = SAMPLE_6[:1]
    print("NB54_FAST=1 -> running on the first file only")

print(f"\nSparse market-structure sweep sample ({len(SAMPLE_6)} files):")
for p in SAMPLE_6:
    print(f"  {p.name}")

# Build the (pivot, liq, resample, invalidate, age_secs) grid.
PIVOT_LENS = [5, 9, 15]
LIQ_LENS = [30, 60]
RESAMPLE_SECS = [0, 60]
INVALIDATE_ON_STRUCTURE = [False, True]
STRUCT_AGE_SECS = [300, 900]

# The 48-config grid (label per axis dimension for the heatmap).
GRID = list(product(
    PIVOT_LENS, LIQ_LENS, RESAMPLE_SECS,
    INVALIDATE_ON_STRUCTURE, STRUCT_AGE_SECS,
))
CONFIG_IDS = [f"p{p}_l{l}_r{r}_inv{int(inv)}_age{a}"
              for (p, l, r, inv, a) in GRID]
print(f"\nGrid: {len(GRID)} configs ({len(PIVOT_LENS)}×{len(LIQ_LENS)}×"
      f"{len(RESAMPLE_SECS)}×{len(INVALIDATE_ON_STRUCTURE)}×"
      f"{len(STRUCT_AGE_SECS)})")

# Build the (file, config) task grid.
tasks = []
for path in SAMPLE_6:
    for (p, l, r, inv, a), cid in zip(GRID, CONFIG_IDS):
        params = dict(
            ms_pivot_len=p,
            ms_liquidity_len=l,
            ms_resample_secs=r,
            fvg_invalidate_on_structure=inv,
            fvg_structure_invalidation_age_secs=a,
        )
        tasks.append((path, cid, params))

print(f"Total tasks: {len(tasks)}")
print(f"  unique detector fingerprints: {len(GRID)} "
      f"(1 cold cache build per fingerprint per file)")
print(f"  Wall estimate (8 workers): ~{len(GRID) * len(SAMPLE_6) * 7 // 8}s "
      f"cold, ~{len(tasks) * 3 // 8}s warm")
print()

t_overall_start = time.perf_counter()
results: list[dict] = []

N_WORKERS = min(8, len(tasks), os.cpu_count() or 4)
print(f"  workers: {N_WORKERS}")
print()

with ThreadPoolExecutor(max_workers=N_WORKERS) as ex:
    futures = {ex.submit(worker_structure_sweep, t): t for t in tasks}
    n_done = 0
    for fut in as_completed(futures):
        t = futures[fut]
        try:
            row = fut.result()
        except Exception as exc:
            print(f"  ! {t[0].name} cfg={t[1]} raised: {exc}", flush=True)
            continue
        results.append(row)
        n_done += 1
        elapsed = time.perf_counter() - t_overall_start
        cache_s = float(row.get("cache_build_s", 0.0))
        bt_s = float(row.get("elapsed_s", 0.0))
        print(f"  [{n_done:3d}/{len(tasks)}] {row['file']:>40s} "
              f"cfg={row['config_id']:<30s} "
              f"n={row['n_trades']:3d} "
              f"pnl=${row['total_pnl_usd']:+8.4f} "
              f"EV=${row['ev_per_trade_usd']:+7.4f} "
              f"WR={row['win_rate_pct']:5.1f}% "
              f"({cache_s:5.1f}s cache + {bt_s:.2f}s bt) "
              f"[wall {elapsed:.1f}s]", flush=True)

t_total = time.perf_counter() - t_overall_start
print(f"\n  total wall: {t_total:.1f}s")

df = pd.DataFrame(results)
df.to_csv(OUT / "nb54_structure_sweep_per_run.csv", index=False)
print(f"\n  Wrote {OUT / 'nb54_structure_sweep_per_run.csv'}")

# Aggregate across files by config
agg_by_cfg = (df.groupby("config_id")
                .agg(n_files=("file", "count"),
                     total_trades=("n_trades", "sum"),
                     mean_pnl_usd=("total_pnl_usd", "mean"),
                     sum_pnl_usd=("total_pnl_usd", "sum"),
                     std_pnl_usd=("total_pnl_usd", "std"),
                     median_pnl_usd=("total_pnl_usd", "median"),
                     mean_ev_usd=("ev_per_trade_usd", "mean"),
                     mean_wr_pct=("win_rate_pct", "mean"),
                     total_tp=("n_tp", "sum"),
                     total_sl=("n_sl", "sum"),
                     total_inv=("n_inv", "sum"))
                .reset_index())
# Re-attach the grid metadata so the per-config CSV is self-describing.
meta = pd.DataFrame([dict(
    config_id=cid,
    ms_pivot_len=p, ms_liquidity_len=l, ms_resample_secs=r,
    fvg_invalidate_on_structure=inv,
    fvg_structure_invalidation_age_secs=a,
) for (p, l, r, inv, a), cid in zip(GRID, CONFIG_IDS)])
per_config = meta.merge(agg_by_cfg, on="config_id", how="left")
per_config.to_csv(OUT / "nb54_structure_sweep_per_config.csv", index=False)

print("\n" + "=" * 110)
print("PER-CONFIG ACROSS-FILES SUMMARY (sorted by mean PnL)")
print("=" * 110)
display_cols = ["config_id", "n_files", "total_trades", "mean_pnl_usd",
                "sum_pnl_usd", "mean_ev_usd", "mean_wr_pct",
                "total_tp", "total_sl", "total_inv"]
print(per_config.sort_values("mean_pnl_usd", ascending=False)
                [display_cols].to_string(index=False))
print()

# Headline: canonical row + best rows
CANONICAL_CID = "p9_l30_r60_inv0_age300"   # ms_pivot_len=9, ms_liquidity_len=30, ms_resample_secs=60, invalidate=False, age=300
canonical_row = per_config[per_config["config_id"] == CANONICAL_CID]
if len(canonical_row) == 0:
    canonical_row = pd.DataFrame([{
        "config_id": CANONICAL_CID, "n_files": 0, "total_trades": 0,
        "mean_pnl_usd": float("nan"), "sum_pnl_usd": float("nan"),
        "std_pnl_usd": float("nan"), "median_pnl_usd": float("nan"),
        "mean_ev_usd": float("nan"), "mean_wr_pct": float("nan"),
        "total_tp": 0, "total_sl": 0, "total_inv": 0,
    }])

best_by_pnl = per_config.sort_values("mean_pnl_usd", ascending=False).head(1)
best_by_ev = per_config.sort_values("mean_ev_usd", ascending=False).head(1)

summary = pd.DataFrame([{
    "n_configs": len(per_config),
    "n_files": len(SAMPLE_6),
    "n_total_tasks": len(tasks),
    "canonical_config_id": CANONICAL_CID,
    "canonical_mean_pnl_usd": float(canonical_row["mean_pnl_usd"].iloc[0])
        if pd.notna(canonical_row["mean_pnl_usd"].iloc[0]) else float("nan"),
    "canonical_sum_pnl_usd": float(canonical_row["sum_pnl_usd"].iloc[0])
        if pd.notna(canonical_row["sum_pnl_usd"].iloc[0]) else float("nan"),
    "canonical_total_trades": int(canonical_row["total_trades"].iloc[0]),
    "best_config_id": str(best_by_pnl["config_id"].iloc[0]),
    "best_mean_pnl_usd": float(best_by_pnl["mean_pnl_usd"].iloc[0]),
    "best_sum_pnl_usd": float(best_by_pnl["sum_pnl_usd"].iloc[0]),
    "best_total_trades": int(best_by_pnl["total_trades"].iloc[0]),
    "best_ev_config_id": str(best_by_ev["config_id"].iloc[0]),
    "best_ev_mean_ev_usd": float(best_by_ev["mean_ev_usd"].iloc[0]),
    "best_ev_total_trades": int(best_by_ev["total_trades"].iloc[0]),
    "wall_time_s": float(t_total),
}])
summary.to_csv(OUT / "nb54_structure_sweep_summary.csv", index=False)

print("=" * 110)
print("HEADLINE")
print("=" * 110)
print(f"  canonical: {CANONICAL_CID}  "
      f"mean_pnl=${summary['canonical_mean_pnl_usd'].iloc[0]:+.4f}  "
      f"sum_pnl=${summary['canonical_sum_pnl_usd'].iloc[0]:+.4f}  "
      f"trades={summary['canonical_total_trades'].iloc[0]}")
print(f"  best by mean PnL: {summary['best_config_id'].iloc[0]}  "
      f"mean_pnl=${summary['best_mean_pnl_usd'].iloc[0]:+.4f}  "
      f"sum_pnl=${summary['best_sum_pnl_usd'].iloc[0]:+.4f}  "
      f"trades={summary['best_total_trades'].iloc[0]}")
print(f"  best by mean EV:  {summary['best_ev_config_id'].iloc[0]}  "
      f"mean_ev=${summary['best_ev_mean_ev_usd'].iloc[0]:+.4f}  "
      f"trades={summary['best_ev_total_trades'].iloc[0]}")
print()

# Heatmap: mean PnL by (pivot, resample) for canonical invalidate/age axis.
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # Canonical: invalidate=False, age=300 — that's the default
    # recipe state, so the heatmap uses GRID rows matching that.
    sub = per_config[(per_config["fvg_invalidate_on_structure"] == False) &
                     (per_config["fvg_structure_invalidation_age_secs"] == 300)]
    pivot_vals = sorted(sub["ms_pivot_len"].unique())
    resample_vals = sorted(sub["ms_resample_secs"].unique())
    liq_vals = sorted(sub["ms_liquidity_len"].unique())
    fig, axes = plt.subplots(1, len(liq_vals),
                              figsize=(5 * len(liq_vals), 5),
                              sharey=True)
    if len(liq_vals) == 1:
        axes = [axes]
    for ax, lv in zip(axes, liq_vals):
        sub2 = sub[sub["ms_liquidity_len"] == lv]
        grid_pivot = np.full((len(pivot_vals), len(resample_vals)), np.nan)
        for _, r in sub2.iterrows():
            i = pivot_vals.index(int(r["ms_pivot_len"]))
            j = resample_vals.index(int(r["ms_resample_secs"]))
            grid_pivot[i, j] = float(r["mean_pnl_usd"])
        im = ax.imshow(grid_pivot, cmap="RdYlGn", aspect="auto",
                        origin="lower")
        ax.set_xticks(range(len(resample_vals)))
        ax.set_xticklabels([str(r) for r in resample_vals])
        ax.set_yticks(range(len(pivot_vals)))
        ax.set_yticklabels([str(p) for p in pivot_vals])
        ax.set_xlabel("ms_resample_secs")
        ax.set_ylabel("ms_pivot_len" if lv == liq_vals[0] else "")
        ax.set_title(f"liq_len={lv}\nmean PnL/month ($)")
        for i in range(len(pivot_vals)):
            for j in range(len(resample_vals)):
                v = grid_pivot[i, j]
                if np.isfinite(v):
                    ax.text(j, i, f"{v:+.1f}", ha="center", va="center",
                            fontsize=8,
                            color="black" if abs(v) < grid_pivot[~np.isnan(grid_pivot)].std() * 0.6 else "white")
        plt.colorbar(im, ax=ax, fraction=0.046)
    fig.suptitle(f"NB54 market-structure sweep — invalidate=False, age=300\n"
                 f"({len(SAMPLE_6)} files, {len(GRID)} configs)",
                 fontsize=12)
    fig.tight_layout()
    fig.savefig(OUT / "nb54_structure_sweep_heatmap.png", dpi=110,
                bbox_inches="tight")
    plt.close(fig)
    print(f"  Wrote {OUT / 'nb54_structure_sweep_heatmap.png'}")
except Exception as e:
    print(f"  heatmap skipped: {e}")

print(f"\nDone. Wall clock: {t_total:.1f}s.")
