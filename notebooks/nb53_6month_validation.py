"""NB53 6-month validation — body-only A/B.

Validates the body-only mitigation + invalidation canonical flip
(``v17-btc-sniper-2026-09-26a`` → ``v17-btc-sniper-2026-09-26b``)
across the same 6-month sample nb52 used for its SL/TP sweep.

For each month:
  * Run the canonical recipe (``body_only=True/True``) on the
    cached zones + structure + side-table.
  * Run the legacy recipe (``body_only=False/False``) on the
    cached zones + structure + side-table.

The two recipes share NO cached files — the body-only flags are
in the cache fingerprint, so each recipe produces its own
detector output. Both caches are built lazily by the worker.

Run modes:
  * default: full 6-month A/B (12 tasks; ~5-10 min wall on
    8-thread pool — first 5 months × 2 configs = 10 cold-cache
    builds, ~110s each; backtests are sub-second after cache).
  * ``NB53_6MO_FAST=1``: run on the FIRST file only (~3-4 min
    wall for 1 cold build + 2 backtests).
"""
from __future__ import annotations

import json
import os
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
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

from notebooks._nb53_worker import worker_body_only_ab  # noqa: E402

# Raw data root (mirror of nb52's hardcoded path)
SOURCE_DATA_ROOT = Path(r"C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw")
OUT = ROOT / "notebooks" / "nb53_outputs"
OUT.mkdir(parents=True, exist_ok=True)

# Reproducible 6-month sample — read the same one nb52 chose
# (persisted at notebooks/nb52_outputs/nb52_sample_6.json with seed
# 20260925 on the parent notebook run). Hardcoding to a different
# sample would make this validation incomparable with nb52's
# sweep numbers.
SAMPLE_6_JSON = ROOT / "notebooks" / "nb52_outputs" / "nb52_sample_6.json"
sample_names = json.loads(SAMPLE_6_JSON.read_text())
SAMPLE_6 = [SOURCE_DATA_ROOT / name for name in sample_names]
assert all(p.exists() for p in SAMPLE_6), f"missing files: {[p for p in SAMPLE_6 if not p.exists()]}"

# Persist a copy here for reproducibility if nb52's file moves.
(OUT / "nb53_6mo_sample.json").write_text(
    json.dumps([p.name for p in SAMPLE_6], indent=2)
)

FAST = os.environ.get("NB53_6MO_FAST", "0") == "1"
if FAST:
    SAMPLE_6 = SAMPLE_6[:1]
    print("NB53_6MO_FAST=1 -> running on the first file only")

print(f"\n6-month validation sample ({len(SAMPLE_6)} files):")
for p in SAMPLE_6:
    print(f"  {p.name}")

# Build the (file, mode) task grid: 2 tasks per file.
tasks = [(p, mode) for p in SAMPLE_6 for mode in ("new", "old")]

print(f"\nTotal tasks: {len(tasks)}")
print(f"  Wall-clock estimate (8 workers): {len(tasks) * 7 // 8}+ seconds")
print(f"  (cold cache build ~110s × {len(SAMPLE_6)} files × 2 configs,")
print(f"   amortized across 8 threads; backtest ~3s after cache)")
print()

t_overall_start = time.perf_counter()
results: list[dict] = []

# Cap workers at 8 to avoid memory-pressure contention.
N_WORKERS = min(8, len(tasks), os.cpu_count() or 4)
print(f"  workers: {N_WORKERS}")
print()

with ThreadPoolExecutor(max_workers=N_WORKERS) as ex:
    futures = {ex.submit(worker_body_only_ab, t): t for t in tasks}
    n_done = 0
    for fut in as_completed(futures):
        t = futures[fut]
        try:
            row = fut.result()
        except Exception as exc:
            print(f"  ! {t[0].name} mode={t[1]} raised: {exc}", flush=True)
            continue
        results.append(row)
        n_done += 1
        elapsed = time.perf_counter() - t_overall_start
        cache_s = float(row.get("cache_build_s", 0.0))
        bt_s = float(row.get("elapsed_s", 0.0))
        print(f"  [{n_done:2d}/{len(tasks)}] {row['file']:>40s} "
              f"mode={row['body_only']:>3s} "
              f"n={row['n_trades']:3d} "
              f"pnl=${row['total_pnl_usd']:+8.4f} "
              f"EV=${row['ev_per_trade_usd']:+7.4f} "
              f"WR={row['win_rate_pct']:5.1f}% "
              f"({cache_s:5.1f}s cache + {bt_s:.2f}s bt) "
              f"[wall {elapsed:.1f}s]", flush=True)

t_total = time.perf_counter() - t_overall_start
print(f"\n  total wall: {t_total:.1f}s")

# Pivot to one-row-per-file with new-vs-old columns
df = pd.DataFrame(results)
df.to_csv(OUT / "nb53_6month_validation_per_run.csv", index=False)

print(f"\n  Wrote {OUT / 'nb53_6month_validation_per_run.csv'}")

# Per-file deltas (new - old)
new_df = df[df["body_only"] == "ON"].set_index("file")
old_df = df[df["body_only"] == "OFF"].set_index("file")
files = sorted(set(new_df.index) | set(old_df.index))

per_file = []
for f in files:
    new_row = new_df.loc[f] if f in new_df.index else None
    old_row = old_df.loc[f] if f in old_df.index else None
    if new_row is None or old_row is None:
        continue
    delta_pnl = float(new_row["total_pnl_usd"]) - float(old_row["total_pnl_usd"])
    delta_ev = float(new_row["ev_per_trade_usd"]) - float(old_row["ev_per_trade_usd"])
    per_file.append({
        "file": f,
        "n_trades_new": int(new_row["n_trades"]),
        "n_trades_old": int(old_row["n_trades"]),
        "delta_n_trades": int(new_row["n_trades"] - old_row["n_trades"]),
        "pnl_old_usd": float(old_row["total_pnl_usd"]),
        "pnl_new_usd": float(new_row["total_pnl_usd"]),
        "delta_pnl_usd": delta_pnl,
        "ev_old_usd": float(old_row["ev_per_trade_usd"]),
        "ev_new_usd": float(new_row["ev_per_trade_usd"]),
        "delta_ev_usd": delta_ev,
        "wr_old_pct": float(old_row["win_rate_pct"]),
        "wr_new_pct": float(new_row["win_rate_pct"]),
        "delta_wr_pp": float(new_row["win_rate_pct"] - old_row["win_rate_pct"]),
        "n_sl_old": int(old_row["n_sl"]),
        "n_sl_new": int(new_row["n_sl"]),
        "n_tp_old": int(old_row["n_tp"]),
        "n_tp_new": int(new_row["n_tp"]),
        "n_inv_old": int(old_row["n_inv"]),
        "n_inv_new": int(new_row["n_inv"]),
        "pnl_improvement_pct": (100.0 * delta_pnl / abs(float(old_row["total_pnl_usd"]))
                                 if abs(float(old_row["total_pnl_usd"])) > 1e-9
                                 else float("nan")),
    })

per_file_df = pd.DataFrame(per_file)
per_file_df.to_csv(OUT / "nb53_6month_validation_per_file.csv", index=False)

print("\n" + "=" * 90)
print("PER-FILE A/B: body-only ON (new) vs OFF (legacy)")
print("=" * 90)
print(per_file_df.to_string(index=False))
print()

# Aggregate across files
agg = {
    "n_files": len(per_file),
    "n_files_new_wins": int((per_file_df["delta_pnl_usd"] > 0).sum()),
    "n_files_old_wins": int((per_file_df["delta_pnl_usd"] < 0).sum()),
    "n_files_flat": int((per_file_df["delta_pnl_usd"] == 0).sum()),
    "sum_pnl_old_usd": float(per_file_df["pnl_old_usd"].sum()),
    "sum_pnl_new_usd": float(per_file_df["pnl_new_usd"].sum()),
    "sum_delta_pnl_usd": float(per_file_df["delta_pnl_usd"].sum()),
    "mean_pnl_old_usd": float(per_file_df["pnl_old_usd"].mean()),
    "mean_pnl_new_usd": float(per_file_df["pnl_new_usd"].mean()),
    "mean_delta_pnl_usd": float(per_file_df["delta_pnl_usd"].mean()),
    "mean_ev_old_usd": float(per_file_df["ev_old_usd"].mean()),
    "mean_ev_new_usd": float(per_file_df["ev_new_usd"].mean()),
    "mean_wr_old_pct": float(per_file_df["wr_old_pct"].mean()),
    "mean_wr_new_pct": float(per_file_df["wr_new_pct"].mean()),
    "total_trades_old": int(per_file_df["n_trades_old"].sum()),
    "total_trades_new": int(per_file_df["n_trades_new"].sum()),
    "total_tp_old": int(per_file_df["n_tp_old"].sum()),
    "total_tp_new": int(per_file_df["n_tp_new"].sum()),
    "total_sl_old": int(per_file_df["n_sl_old"].sum()),
    "total_sl_new": int(per_file_df["n_sl_new"].sum()),
    "total_inv_old": int(per_file_df["n_inv_old"].sum()),
    "total_inv_new": int(per_file_df["n_inv_new"].sum()),
}
agg_row = pd.DataFrame([agg])
agg_row.to_csv(OUT / "nb53_6month_validation_summary.csv", index=False)

print("=" * 90)
print("AGGREGATE ACROSS 6 MONTHS")
print("=" * 90)
print(f"  files where new wins: {agg['n_files_new_wins']}/{agg['n_files']}")
print(f"  files where old wins: {agg['n_files_old_wins']}/{agg['n_files']}")
print(f"  files flat:           {agg['n_files_flat']}/{agg['n_files']}")
print(f"  total trades: old={agg['total_trades_old']}, new={agg['total_trades_new']}, "
      f"delta={agg['total_trades_new'] - agg['total_trades_old']:+d}")
print(f"  TP exits:    old={agg['total_tp_old']:3d}, new={agg['total_tp_new']:3d}, "
      f"delta={agg['total_tp_new'] - agg['total_tp_old']:+d}")
print(f"  SL exits:    old={agg['total_sl_old']:3d}, new={agg['total_sl_new']:3d}, "
      f"delta={agg['total_sl_new'] - agg['total_sl_old']:+d}")
print(f"  INV exits:   old={agg['total_inv_old']:3d}, new={agg['total_inv_new']:3d}, "
      f"delta={agg['total_inv_new'] - agg['total_inv_old']:+d}")
print(f"  sum pnl: old=${agg['sum_pnl_old_usd']:+.4f}, "
      f"new=${agg['sum_pnl_new_usd']:+.4f}, "
      f"delta=${agg['sum_delta_pnl_usd']:+.4f}")
print(f"  mean pnl/month: old=${agg['mean_pnl_old_usd']:+.4f}, "
      f"new=${agg['mean_pnl_new_usd']:+.4f}, "
      f"delta=${agg['mean_delta_pnl_usd']:+.4f}")
print(f"  mean EV/trade: old=${agg['mean_ev_old_usd']:+.4f}, "
      f"new=${agg['mean_ev_new_usd']:+.4f}")
print(f"  mean WR:       old={agg['mean_wr_old_pct']:.1f}%, "
      f"new={agg['mean_wr_new_pct']:.1f}%")

print(f"\n  Wrote {OUT / 'nb53_6month_validation_summary.csv'}")
print(f"\nDone. Wall clock: {t_total:.1f}s.")
