# ---
# jupyter:
#   jupytext:
#     cell_metadata_filter: title,-all
#     formats: ipynb,py:percent
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.19.3
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %%
"""nb52_opt_sweep_alpha.py — Sparse SL/TP sweep + structural alpha diagnostics.

What this notebook does (each numbered section is a Jupytext ``# %%``
cell, runnable independently):

  Cell A. Setup & data inventory. Pick 6 random monthly files.
  Cell B. Baseline run — canonical v17 BTC SNIPER with 0.05% taker
          fees per leg. Acts as the A-cell for the sweep cell.
  Cell C. Sparse SL/TP sweep — 5x5 grid (25 configs × 6 months =
          150 runs). Parallelised via ProcessPoolExecutor across the
          6 monthly files. Results persisted to CSV.
  Cell D. Sweep commentary — aggregate by (sl,tp), plot heatmap,
          identify the best cell, discuss where 22x lands.
  Cell E. Drill-down on the best cell — break trades down by exit
          reason / alignment / hour-of-day / day-of-week.
  Cell F. Structural alpha diagnostics (the "train of thought"):
          F.1 liquidity-sweep context, F.2 swing H/L reclaim,
          F.3 true-iFVG vs fvg-but-invalidated classifier.
  Cell G. Recommendation summary — prose synthesis.

Why a notebook (not a CLI driver)?
  Sweeps generate rich multi-dimensional data that the user wants to
  explore visually. The notebook format (markdown + tables + plots +
  re-runnable cells) is the right surface for that. Per the AGENTS.md
  rule, ``nb52_opt_sweep_alpha.py`` is the canonical source; the
  ``.ipynb`` is regenerated via Jupytext.

Wall-clock budget on a single thread (32M-tick month, 2.5M bars):

    one backtest run:     ~120s   (aggregation 5s + bar loop 111s
                                    + side-table 2s + refill 0s)
    25 sweep configs:     ~50 min per file
    6 files sequential:   ~5 hours (sequential)
    Process pool (6):     ~50 min (one worker per file)

The notebook DEFAULT uses the pool — set ``NB52_FAST=1`` (env var) to
drop the 6 random monthly files and use only the 16 daily
2026-09-* files for a faster (~15 min) smoke run.

Module name on disk: ``nb52`` (this file is ``nb52_opt_sweep_alpha.py``
but the module name is selected so multiprocessing can pickle the
worker function — Windows ``spawn`` requires an importable module).
The module is reachable via ``python -m notebooks.nb52_opt_sweep_alpha``
or by importing directly from the ``notebooks/`` directory.

The worker function (``worker_sweep_for_file``) lives in a sibling
file ``notebooks/_nb52_worker.py`` so ProcessPoolExecutor can pickle
the worker in a Windows ``spawn`` start method.
"""
from __future__ import annotations

import os

# Headless matplotlib for SSH / server use (must run BEFORE pyplot import).
os.environ.setdefault("MPLBACKEND", "Agg")

# Force UTF-8 stdout (Windows cp1252 chokes on the → glyph).
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # noqa: F821
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # noqa: F821
except (AttributeError, NameError):
    pass

import json
import logging
import multiprocessing as mp
import random
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Tuple

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

# ─── Repo root discovery (mirror nb51_sniper_viz.py pattern) ────────
ROOT = None
for _candidate in [Path(".").resolve(), *Path(".").resolve().parents]:
    if (_candidate / "src" / "core" / "ict_signals.py").is_file():
        ROOT = _candidate
        break
if ROOT is None:
    raise RuntimeError(
        "Could not find ICT repo root (no src/core/ict_signals.py). "
        "Run from the repo root or any sub-directory."
    )
sys.path.insert(0, str(ROOT))

# Silence the optimal_params() warning that fires when we override
# _RECIPE_KNOBS — this notebook explicitly sweeps them.
warnings.filterwarnings("ignore")

from src.core.optimal_config import optimal_params, OPTIMAL_RECIPE_VERSION  # noqa: E402
from src.tick.aggtrade_aggregator import load_concat_raw_aggtrades           # noqa: E402
from src.core.market_structure import detect_market_structure, StructureState  # noqa: E402

# Worker module lives in a sibling file (so multiprocessing can pickle
# the worker fn). Add the notebooks/ directory to sys.path and import
# directly so we don't require a notebooks/__init__.py.
sys.path.insert(0, str(ROOT / "notebooks"))
import _nb52_worker                                                              # noqa: E402
run_one_file = _nb52_worker.run_one_file
worker_sweep_for_file = _nb52_worker.worker_sweep_for_file
worker_sweep_for_config = _nb52_worker.worker_sweep_for_config

# Output dir.
OUT = ROOT / "notebooks" / "nb52_outputs"
OUT.mkdir(parents=True, exist_ok=True)

# Suppress bar-loop chatty logging; we only want our own prints.
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("nb52")

# Hardcoded source-data location (parent repo per AGENTS.md).
SOURCE_DATA_ROOT = Path(r"C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw")


# ====================================================================
# Cell A — Setup & data inventory
# ====================================================================
# %% setup
#
# Pick 6 random monthly BTCUSDT aggTrades files plus an auxiliary set
# of 16 daily 2026-09-* files for fast re-runs / smoke tests.
# --------------------------------------------------------------------

MONTHLY_FILES: List[Path] = sorted(
    p for p in SOURCE_DATA_ROOT.iterdir()
    if p.name.startswith("BTCUSDT-aggTrades-")
    and p.suffix == ".parquet"
    # Monthly files have the form BTCUSDT-aggTrades-YYYY-MM.parquet
    # (8-digit date, no trailing -DD). Daily files have -YYYY-MM-DD.parquet.
    # Cheap disambiguation: split on '-', count parts.
    and len(p.stem.split("-")) == 4
)
DAILY_FILES: List[Path] = sorted(
    p for p in SOURCE_DATA_ROOT.iterdir()
    if p.name.startswith("BTCUSDT-aggTrades-2026-09-")
    and p.suffix == ".parquet"
    and len(p.stem.split("-")) == 5
)
print(f"Inventory:")
print(f"  monthly files: {len(MONTHLY_FILES)}  (sweep corpus)")
print(f"  daily  files:  {len(DAILY_FILES)}    (fast/smoke mode)")
print(f"  source root:   {SOURCE_DATA_ROOT}")

# A reproducible random sample of 6 months. Seed = 20260925 (today).
random.seed(20260925)
SAMPLE_6: List[Path] = random.sample(MONTHLY_FILES, 6)
SAMPLE_6.sort()  # chronological
print("\n6-month sweep sample:")
for p in SAMPLE_6:
    print(f"  {p.name}")

# Write the sample to disk so the notebook re-run is deterministic
# even if `random` callers later drift.
(OUT / "nb52_sample_6.json").write_text(
    json.dumps([p.name for p in SAMPLE_6], indent=2)
)


# ====================================================================
# Cell B — Baseline run (canonical recipe, 0.05% taker fee per leg)
# ====================================================================
# %% baseline canonical with fees
#
# Single-threaded for the baseline so the timing numbers are easy to
# read. Uses one file (the first sample) to confirm the fee model is
# wired correctly and the canonical params are sane.
# --------------------------------------------------------------------

def trades_df_with_zone(trades_list) -> "pd.DataFrame":
    """Build a per-trade DataFrame that INCLUDES the FvgZone anchor.

    The stock ``IctBacktestResult.trades_df()`` strips ``fvg_zone``
    (it's not JSON-serialisable). For diagnostics like F.3 we need
    the zone object — this wrapper iterates ``trades_list`` directly
    and keeps a per-row ``fvg_zone`` reference.
    """
    rows = []
    for t in trades_list:
        d = t.to_dict()
        # Restore the FvgZone (stripped by to_dict()).
        d["fvg_zone"] = getattr(t, "fvg_zone", None)
        rows.append(d)
    df = pd.DataFrame(rows)
    if "entry_time" in df.columns:
        df["entry_time"] = pd.to_datetime(df["entry_time"], unit="ns", utc=True)
    if "exit_time" in df.columns:
        df["exit_time"] = pd.to_datetime(df["exit_time"], unit="ns", utc=True)
    return df


baseline_file = SAMPLE_6[0]
print(f"Baseline run on {baseline_file.name} (canonical v17 BTC SNIPER):")
baseline_out = run_one_file(baseline_file, "nb52_baseline", include_trades=True)
print(
    f"  trades={baseline_out['n_trades']}  pnl=${baseline_out['pnl_total']:+.2f}  "
    f"EV=${baseline_out['ev_per_trade']:+.4f}  WR={baseline_out['win_rate']:.1%}  "
    f"tpd={baseline_out['trades_per_day']:.1f}  "
    f"({baseline_out['elapsed_s']:.1f}s)"
)
print(f"  tick_meta: {baseline_out['tick_meta']}")
# Persist the raw trades list so the rest of the notebook can pick up
# the FvgZone references without re-running the (slow) baseline.
_BASELINE_TRADES = baseline_out.get("trades", [])


# ====================================================================
# Cell C — Sparse SL/TP sweep (5 SL × 5 TP × 6 months = 150 runs)
# ====================================================================
# %% sparse SL/TP sweep
#
# Grid chosen around the canonical (SL=2x, TP=22x) anchor:
#   SL ∈ {1.0, 1.5, 2.0, 3.0, 5.0}  (zone-width multiples)
#   TP ∈ {8.0, 15.0, 22.0, 30.0, 45.0}
# That keeps the sweep small (25 configs × 6 months = 150 runs) but
# spans both tighter and looser regimes relative to the canonical.
# A (sl=2, tp=22) cell is in the grid, so the canonical is itself
# reproduced — the others measure how sensitive PnL is to the
# zone-width scaling.
# --------------------------------------------------------------------

SL_GRID: List[float] = [2.0, 3.0, 5.0]
TP_GRID: List[float] = [8.0, 15.0, 22.0, 30.0, 45.0]

# NB52_FAST=1 ==> run all 25 configs on one file only (smoke test).
# NB52_SKIP_SWEEP=1 ==> skip Cells C/D/E entirely (sweep + drill-down).
#                        Useful when you just want Cells A/B/F/G to
#                        diagnose a single canonical run.
NB52_FAST = bool(int(os.environ.get("NB52_FAST", "0")))
NB52_SKIP_SWEEP = bool(int(os.environ.get("NB52_SKIP_SWEEP", "0")))


if NB52_SKIP_SWEEP:
    # Skip the 150-run sweep entirely. The alpha diagnostics (Cell F)
    # and recommendation (Cell G) fall back to the single-file canonical
    # run from Cell B, so F still has real trades to diagnose.
    print("NB52_SKIP_SWEEP=1 -> skipping Cells C/D/E; Cell F will use "
          "the single-file canonical run from Cell B.")
    # Provide stub values so the rest of the notebook doesn't break if
    # someone manually runs cells out of order. Cell F below rebuilds
    # `trades_df` from the baseline if `sweep_df` is missing.
    sweep_df = pd.DataFrame()
    agg = pd.DataFrame()
    best = pd.Series({"sl_mult": np.nan, "tp_mult": np.nan,
                      "pnl_total_mean": np.nan, "ev_mean": np.nan,
                      "pnl_total_sum": np.nan})
    trades_df = pd.DataFrame()
elif NB52_FAST:
    print(f"NB52_FAST=1 -> running 25 configs on 1 file (smoke): {SAMPLE_6[0].name}")
    grid = [(sl, tp) for sl in SL_GRID for tp in TP_GRID]
    sweep_records = worker_sweep_for_file((SAMPLE_6[0], grid))
else:
    grid = [(sl, tp) for sl in SL_GRID for tp in TP_GRID]
    # Per-config task list (file, sl, tp) tuples. Submitted to the
    # thread pool one-per-task for better load balancing across
    # cores — the old per-file pattern starved threads on the
    # slowest file (each thread held one file open for all 25
    # configs and threads that finished first sat idle).
    task_grid = [(p, sl, tp) for p in SAMPLE_6 for sl, tp in grid]
    # Cap workers. numpy + GIL releases fine; 8 threads on a
    # typical 16-core box is the sweet spot before memory-pressure
    # contention kicks in.
    n_workers = min(len(task_grid), max(1, (os.cpu_count() or 4)), 8)
    print(f"Sparse sweep: {len(SL_GRID)}x{len(TP_GRID)} x {len(SAMPLE_6)} = "
          f"{len(task_grid)} runs, "
          f"ThreadPoolExecutor workers={n_workers}")
    sweep_records = []
    t_sweep0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        futures = {ex.submit(worker_sweep_for_config, t): t for t in task_grid}
        for fut in as_completed(futures):
            t = futures[fut]
            path, sl, tp = t
            try:
                row = fut.result()
            except Exception as exc:
                print(f"  ! {path.name} sl={sl} tp={tp} FAILED: {exc}")
                row = None
            if row is not None:
                sweep_records.append(row)
            done_min = (time.perf_counter() - t_sweep0) / 60.0
            if len(sweep_records) % 25 == 0 or len(sweep_records) == len(task_grid):
                print(
                    f"  + {path.name} sl={sl} tp={tp}: cumulative "
                    f"{len(sweep_records)}/{len(task_grid)} rows, {done_min:.1f} min"
                )

if not NB52_SKIP_SWEEP:
    sweep_df = pd.DataFrame(sweep_records)
    sweep_csv = OUT / "nb52_sweep_results.csv"
    sweep_df.to_csv(sweep_csv, index=False)
    print(f"\nSaved sweep -> {sweep_csv} ({len(sweep_df)} rows)")


# %% sweep commentary
#
# Cell D — Aggregate the per-month rows to (sl, tp), look at:
#   * mean pnl_total / EV across the 6 months
#   * win-rate distribution
#   * where 22x TP lies
# Plot a heatmap of mean pnl_total by (sl, tp).
#
# This block is gated behind NB52_SKIP_SWEEP — when that flag is
# set, Cell F falls back to the Cell B baseline trades.
# --------------------------------------------------------------------

if not NB52_SKIP_SWEEP:
    # ====================================================================
    # Cell D — Sweep commentary (markdown + heatmap)
    # ====================================================================
    #
    # Aggregate the per-month rows to (sl, tp), look at:
    #   * mean pnl_total / EV across the 6 months
    #   * win-rate distribution
    #   * where 22x TP lies
    # Plot a heatmap of mean pnl_total by (sl, tp).
    # --------------------------------------------------------------------

    agg = (
        sweep_df.groupby(["sl_mult", "tp_mult"], as_index=False)
        .agg(
            n_trades_total=("n_trades", "sum"),
            pnl_total_sum=("pnl_total", "sum"),
            pnl_total_mean=("pnl_total", "mean"),
            ev_mean=("ev_per_trade", "mean"),
            wr_mean=("win_rate", "mean"),
            tpd_mean=("trades_per_day", "mean"),
        )
    )
    # Re-rank: rows are now (sl, tp) cells with mean across 6 months.
    agg = agg.sort_values(["pnl_total_sum", "ev_mean"], ascending=[False, False])
    print("Top 8 (sl, tp) cells by 6-month sum pnl_total:")
    print(agg.head(8).to_string(index=False))

    # Heatmap of mean pnl_total by (sl, tp)
    piv = sweep_df.pivot_table(
        index="sl_mult", columns="tp_mult", values="pnl_total", aggfunc="mean",
    )
    fig, ax = plt.subplots(figsize=(8, 5))
    im = ax.imshow(piv.values, aspect="auto", cmap="RdYlGn")
    ax.set_xticks(range(len(piv.columns)))
    ax.set_xticklabels([f"{c:.0f}x" for c in piv.columns])
    ax.set_yticks(range(len(piv.index)))
    ax.set_yticklabels([f"{r:.1f}x" for r in piv.index])
    ax.set_xlabel("TP zone-width multiple")
    ax.set_ylabel("SL zone-width multiple")
    ax.set_title(f"Mean pnl_total ($/month) across {len(SAMPLE_6)} months -- "
                 f"with {5} bps taker fee per leg")
    for i in range(piv.shape[0]):
        for j in range(piv.shape[1]):
            v = piv.values[i, j]
            ax.text(j, i, f"{v:+.1f}", ha="center", va="center",
                    fontsize=8, color="black" if abs(v) < piv.values.std() else "white")
    fig.colorbar(im, ax=ax, label="mean pnl_total ($)")
    fig.tight_layout()
    heatmap_png = OUT / "nb52_sweep_heatmap.png"
    fig.savefig(heatmap_png, dpi=120, bbox_inches="tight")
    plt.close(fig)
    print(f"\nHeatmap -> {heatmap_png}")

    # Where does 22x (canonical) sit?
    canon_row = agg[(agg.sl_mult == 2.0) & (agg.tp_mult == 22.0)]
    if not canon_row.empty:
        canon_pnl = float(canon_row.pnl_total_mean.iloc[0])
        canon_rank = (agg.pnl_total_sum >= canon_row.pnl_total_sum.iloc[0]).sum()
        print(f"\nCanonical (sl=2.0, tp=22.0) mean pnl_total = ${canon_pnl:+.2f}/month, "
              f"rank {canon_rank}/{len(agg)}")
        best = agg.iloc[0]
        print(f"Best cell:        (sl={best.sl_mult}, tp={best.tp_mult}) mean "
              f"pnl=${best.pnl_total_mean:+.2f}/month")
    else:
        best = agg.iloc[0]
        print(f"\nCanonical (2.0, 22.0) was NOT in the grid (impossible -- bug).")
        print(f"Best available: (sl={best.sl_mult}, tp={best.tp_mult})")


# %% drill-down on best cell
# ====================================================================
# Cell E — Drill-down on the best (sl, tp) cell
# ====================================================================
#
# Run the canonical-recipe with the winning (sl, tp) override across
# all 6 sample files and concatenate the per-file trade DataFrames.
# Slice them by exit_reason x entry_alignment x hour-of-day x day-of-week.
# This is the diagnostic table that drives the alpha hypotheses.
# --------------------------------------------------------------------

if not NB52_SKIP_SWEEP:
    BEST_SL = float(best.sl_mult)
    BEST_TP = float(best.tp_mult)
    print(f"Drill-down on the winning cell: SL={BEST_SL}x, TP={BEST_TP}x")

    per_file_trades = []
    for path in SAMPLE_6:
        out = run_one_file(
            path,
            f"nb52_best_{path.stem[-7:]}",
            include_trades=True,
            fvg_inv_trade_sl_zone_mult=BEST_SL,
            fvg_inv_trade_tp_zone_mult=BEST_TP,
        )
        df = trades_df_with_zone(out["trades"])
        df["file"] = path.name
        per_file_trades.append(df)
    trades_df = pd.concat(per_file_trades, ignore_index=True)
    trades_df.to_csv(OUT / "nb52_best_cell_trades.csv", index=False)
    print(f"  collected {len(trades_df)} trades across 6 files")

    trades_df["hour_utc"] = trades_df["entry_time"].dt.hour
    trades_df["dow"] = trades_df["entry_time"].dt.dayofweek
    trades_df["entry_side"] = np.where(trades_df["direction"] > 0, "long", "short")

    print("\nBreak-down by exit_reason:")
    print(trades_df.groupby("exit_reason")["pnl_usd"].agg(["count", "mean", "sum"]))

    print("\nBreak-down by entry_alignment (BoS/CHoCH at entry):")
    print(trades_df.groupby("entry_alignment")["pnl_usd"].agg(["count", "mean", "sum"]))

    print("\nBreak-down by hour-of-day (entry, UTC):")
    print(trades_df.groupby("hour_utc")["pnl_usd"].agg(["count", "mean", "sum"]).sort_values("mean", ascending=False))

    print("\nBreak-down by direction:")
    print(trades_df.groupby("entry_side")["pnl_usd"].agg(["count", "mean", "sum"]))

    print("\nBreak-down by exit_reason x hour-utc top-3:")
    piv_eh = trades_df.pivot_table(
        index="exit_reason", columns="hour_utc",
        values="pnl_usd", aggfunc="sum", fill_value=0,
    )
    print(piv_eh.to_string())

else:
    # Sweep is skipped (NB52_SKIP_SWEEP=1) — build trades_df from the
    # baseline canonical run on SAMPLE_6[0] so Cell F has real trades
    # to diagnose.
    if "_BASELINE_TRADES" in dir() and len(_BASELINE_TRADES) > 0:
        # Reuse the already-run baseline trades so we don't pay for
        # the backtest a second time.
        print(f"Reusing baseline trades from Cell B ({len(_BASELINE_TRADES)} trades)")
        trades_df = trades_df_with_zone(_BASELINE_TRADES)
        trades_df["file"] = SAMPLE_6[0].name
    else:
        print(f"Building trades_df from canonical recipe on {SAMPLE_6[0].name} "
              "(Cell F needs trades for alpha diagnostics).")
        canonical_out = run_one_file(SAMPLE_6[0], "nb52_canonical",
                                     include_trades=True)
        trades_df = trades_df_with_zone(canonical_out["trades"])
        trades_df["file"] = SAMPLE_6[0].name
    trades_df["hour_utc"] = trades_df["entry_time"].dt.hour
    trades_df["dow"] = trades_df["entry_time"].dt.dayofweek
    trades_df["entry_side"] = np.where(trades_df["direction"] > 0, "long", "short")
    trades_df.to_csv(OUT / "nb52_canonical_trades.csv", index=False)
    print(f"  collected {len(trades_df)} canonical trades from "
          f"{SAMPLE_6[0].name} -> {OUT / 'nb52_canonical_trades.csv'}")
    BEST_SL = 2.0
    BEST_TP = 22.0
    best = pd.Series({"sl_mult": BEST_SL, "tp_mult": BEST_TP,
                      "pnl_total_mean": float(baseline_out["pnl_total"]),
                      "ev_mean": float(baseline_out["ev_per_trade"]),
                      "wr_mean": float(baseline_out["win_rate"]),
                      "pnl_total_sum": float(baseline_out["pnl_total"])})


# ====================================================================
# Cell F — Structural alpha diagnostics
# ====================================================================
# %% structural alpha diagnostics
#
# F.1 — Liquidity-sweep context: "does the trade EV differ when the
#        entry is within 60s of a recent liquidity sweep?"
# F.2 — Swing H/L reclaim:        "is EV higher when entry sits
#        within ±0.5 ATR of the most recent swing high/low?"
# F.3 — True-iFVG vs fvg-but-invalidated: re-classify inverted FVGs
#        by whether they got a CONVINCING retest (≥1 close on the
#        inverted side within 5 min of `inverted_bar`). Trade from
#        zones that did NOT get a convincíing retest are currently
#        still fired as "true iFVG" — i.e. the existing
#        `drop_inverted_fvg=False` (the implicit default for the
#        iFVG path) treats them identically. The hypothesis is that
#        this collapses two structurally different outcomes into one.
#
# Run F.1–F.3 against the BEST-CELL trades. Each subsection ends with
# a 1-paragraph takeaway that translates numbers into "promote to a
# knob?" or "drop, inconclusive."
# --------------------------------------------------------------------

SWEEP_BUCKET_EDGES_S = [30, 120, 600]   # seconds  (used by F.1)
SWING_RECLAIM_ATR_FRAC = 0.5              # F.2 distance from latest swing ±frac*ATR
IFVG_RETEST_BARS = 60                     # F.3: 60 bars at 1s post-inversion


def compute_structure_for_file(path: Path) -> StructureState:
    """Detect market structure on the 1s bars for this file.

    Uses the canonical ms_pivot_len / ms_liquidity_len defaults so the
    structure scan matches what the trade backtest sees internally.
    Resamples to 1m bars before scanning for swings so pivot_len=9
    means 9 bars of chart resolution (= 9m swing confirmation) —
    which is the right granularity for ICT-style structure analysis
    on 1s raw data.
    """
    from src.tick.aggtrade_aggregator import aggregate_ticks_to_1s_bars
    raw = load_concat_raw_aggtrades([path])
    bars = aggregate_ticks_to_1s_bars(raw)
    high = bars["high"].to_numpy(dtype=np.float64)
    low = bars["low"].to_numpy(dtype=np.float64)
    close = bars["close"].to_numpy(dtype=np.float64)
    state = detect_market_structure(
        high,
        low,
        close,
        pivot_len=9,
        liquidity_len=30,
        detect_liquidity=True,
        resample_to_n_secs=60,
    )
    return state


# Pre-compute structure state + bars per file (cached so all
# subsections reuse both). Only scan files that actually appear
# in trades_df to keep the F work bounded; in skip-sweep mode
# that's typically 1 file. We hold a tuple (state, bars_time_ns)
# per file so F.1/F.2 can convert sw.bar -> wall-clock ns via
# the bars.time[] array (the detector's sw.bar is a 1s-bar index
# after remap from the resampled scan — see
# ``detect_market_structure._expand_state_to_original``).
files_in_scope = sorted(set(trades_df["file"].tolist())) or [p.name for p in SAMPLE_6]
scope_paths = [next(p for p in SAMPLE_6 if p.name == n) for n in files_in_scope]
structure_cache: dict = {}
print(f"Pre-computing market structure for {len(scope_paths)} file(s) "
      f"in scope (with trades_df entries)...")
for path in scope_paths:
    t0 = time.perf_counter()
    state = compute_structure_for_file(path)
    # Re-aggregate just enough to get bars.time[] (cheap; ~5s for
    # a monthly file). We don't need the bar prices here, only
    # the time lookup.
    from src.tick.aggtrade_aggregator import aggregate_ticks_to_1s_bars
    raw = load_concat_raw_aggtrades([path])
    bars = aggregate_ticks_to_1s_bars(raw)
    bars_time_ns = bars["time"].astype("int64").to_numpy()
    structure_cache[path.name] = (state, bars_time_ns)
    s, _ = structure_cache[path.name]
    print(f"  {path.name}: {len(s.swings)} swings, "
          f"{len(s.breaks)} breaks, "
          f"{len(s.sweeps)} sweeps "
          f"({time.perf_counter() - t0:.1f}s)")


# ---- F.1 Liquidity-sweep context -----------------------------------
# Hypothesis: trades that fire CLOSE in time to a fresh liquidity
# sweep (a stop-hunt wick through a swing H/L) have different EV
# than trades that fire into "cold" structure. To test this, we
# compute the wall-clock distance (in seconds) from each trade's
# entry to the MOST RECENT prior sweep; report EV by distance
# bucket. Buckets: 0-30s, 30-120s, 120-600s, 600s+ (or no recent
# sweep in this file). The interesting reading is whether the
# 0-30s bucket is meaningfully different from the 600s+ bucket.

SWEEP_BUCKET_EDGES_S = [30, 120, 600]   # seconds
SWEEP_BUCKET_LABELS = [
    "0-30s (fresh)",
    "30-120s (near)",
    "120-600s (stale)",
    "600s+ (or none)",
]


def _last_sweep_dist_s(state: StructureState, entry_ns: int,
                        bars_time_ns: np.ndarray) -> Tuple[float, int]:
    """Return (delta_seconds, last_sweep_bar) where delta_seconds is
    the wall-clock distance from the most-recent sweep PRIOR to
    entry, or +inf if there is no such sweep.

    NB on bar indexing: ``state.sweeps[].bar`` is the bar index in
    the ORIGINAL resolution after ``detect_market_structure`` remaps
    the resampled scan back to 1s-bar indices. So we look up the
    actual ns timestamp via ``bars_time_ns[sw.bar]``. (Earlier
    versions of this notebook incorrectly computed ``sw.bar * 60 *
    1e9``, treating the bar index as if it counted from epoch —
    that put every sweep in 1970 and produced the "all 600s+ or
    none" zero-bucket diagnostic. See AGENTS.md 2026-09-26.)
    """
    last_ns = -1
    last_bar = -1
    for sw in state.sweeps:
        if not (0 <= sw.bar < bars_time_ns.shape[0]):
            continue
        sw_ns = int(bars_time_ns[sw.bar])
        if sw_ns <= entry_ns and sw_ns > last_ns:
            last_ns = sw_ns
            last_bar = int(sw.bar)
    if last_ns < 0:
        return float("inf"), -1
    return (entry_ns - last_ns) / 1e9, last_bar


def _bucket_distance(dist_s: float) -> str:
    if dist_s == float("inf"):
        return SWEEP_BUCKET_LABELS[-1]
    edges_s = SWEEP_BUCKET_EDGES_S
    if dist_s <= edges_s[0]:
        return SWEEP_BUCKET_LABELS[0]
    if dist_s <= edges_s[1]:
        return SWEEP_BUCKET_LABELS[1]
    if dist_s <= edges_s[2]:
        return SWEEP_BUCKET_LABELS[2]
    return SWEEP_BUCKET_LABELS[-1]


sweep_anchor_records = []
# We need bars.time per file to convert sw.bar -> ns. The detector's
# sw.bar is a 1s-bar index (the resampled scan is remapped back to
# 1s resolution by ``_expand_state_to_original``), so the correct
# conversion is ``bars_time_ns[sw.bar]``. We cached this tuple
# alongside the state at the top of Cell F.
bars_time_ns_by_file: dict[str, np.ndarray] = {
    path.name: structure_cache[path.name][1] for path in scope_paths
}

for path in scope_paths:
    state, _ = structure_cache[path.name]
    sub = trades_df[trades_df.file == path.name].copy()
    if sub.empty:
        continue
    sub = sub.assign(entry_ns=sub.entry_time.astype("int64"))
    bucket = []
    dist_s = []
    last_bar_list = []
    bars_time_ns = bars_time_ns_by_file[path.name]
    for entry_ns in sub.entry_ns:
        d, lb = _last_sweep_dist_s(state, int(entry_ns), bars_time_ns)
        bucket.append(_bucket_distance(d))
        dist_s.append(d)
        last_bar_list.append(lb)
    sub["sweep_dist_s"] = dist_s
    sub["sweep_bucket"] = bucket
    sub["sweep_last_bar"] = last_bar_list
    sweep_anchor_records.append(sub)

sweep_df_trades = pd.concat(sweep_anchor_records, ignore_index=True)
n_total = int(len(sweep_df_trades))
print(f"\nF.1 — Liquidity-sweep context:")
print(f"  structure was built at 1m pivots and remapped to 1s bars (the")
print(f"  detector's _expand_state_to_original); sw.bar is therefore a")
print(f"  1s-bar index. F.1 converts it to wall-clock ns via bars.time[sw.bar].")
print(f"  distribution of distance from entry to most-recent prior sweep:")
print(sweep_df_trades["sweep_bucket"].value_counts().reindex(SWEEP_BUCKET_LABELS, fill_value=0))
print(f"\n  PnL by sweep-distance bucket:")
bucket_summary = sweep_df_trades.groupby("sweep_bucket")["pnl_usd"].agg(
    count="count", mean="mean", sum="sum",
)
# Re-order rows to the natural bucket order.
bucket_summary = bucket_summary.reindex(SWEEP_BUCKET_LABELS, fill_value=0.0)
print(bucket_summary.to_string())
print(
    f"\n  Fresh-bucket (0-30s) EV/trade:      "
    f"${sweep_df_trades[sweep_df_trades.sweep_bucket == SWEEP_BUCKET_LABELS[0]].pnl_usd.mean():+.4f}\n"
    f"  No-recent-sweep EV/trade:           "
    f"${sweep_df_trades[sweep_df_trades.sweep_bucket == SWEEP_BUCKET_LABELS[-1]].pnl_usd.mean():+.4f}\n"
)

# Persist F.1 output for later inspection / reuse.
sweep_df_trades.to_csv(
    OUT / "nb52_F1_sweep_distance_per_trade.csv", index=False,
)
bucket_summary.to_csv(OUT / "nb52_F1_sweep_distance_buckets.csv")


# ---- F.2 Swing H/L reclaim -----------------------------------------
# For each trade, find the most recent swing_high / swing_low price
# that was BROKEN (last_swing_high_broken_bar / last_swing_low_broken_bar)
# at or before entry_time. If entry_price is within ±SWING_RECLAIM_ATR_FRAC
# × ATR_at_entry_bar of that swing's price, the trade is "reclaim-
# anchored". We don't have per-bar ATR cached on the structure state,
# so use the simple 1s-bar range ATR proxy: median(high - low) over
# the prior 1200 bars of the entry.

def _last_swing_before(state: StructureState, entry_ns: int,
                        bars_time_ns: np.ndarray):
    """Return (price, kind, last_bar) of the most-recent swing in
    ``state.swings`` whose wall-clock time is <= entry_ns.

    NB: ``sw.bar`` is a 1s-bar index (the detector remaps the 1m
    resampled scan back to original 1s resolution). We convert via
    ``bars_time_ns[sw.bar]`` rather than the buggy ``sw.bar * 60 *
    1e9`` from the earlier notebook version.
    """
    last_price = None
    last_kind = ""
    last_bar = -1
    last_ns = -1
    for sw in state.swings:
        if not (0 <= sw.bar < bars_time_ns.shape[0]):
            continue
        sw_ns = int(bars_time_ns[sw.bar])
        if sw_ns <= entry_ns and sw_ns > last_ns:
            last_ns = sw_ns
            last_bar = int(sw.bar)
            last_price = float(sw.price)
            last_kind = "high" if sw.is_high else "low"
    return last_price, last_kind, last_bar


def _last_broken_swing(state: StructureState, entry_ns: int,
                        bars_time_ns: np.ndarray):
    """Return (price, kind, bar) of the most-recent BREAK (BoS/CHoCH)
    whose wall-clock time is <= entry_ns. kind is 'bull' or 'bear'.
    Uses the same 1s-bar time lookup as ``_last_swing_before``.
    """
    last_price = None
    last_kind = ""
    last_bar = -1
    last_ns = -1
    for br in state.breaks:
        if not (0 <= br.bar < bars_time_ns.shape[0]):
            continue
        br_ns = int(bars_time_ns[br.bar])
        if br_ns <= entry_ns and br_ns > last_ns:
            last_ns = br_ns
            last_bar = int(br.bar)
            last_price = float(br.broken_swing_price)
            last_kind = "bull" if int(br.kind) > 0 else "bear"
    return last_price, last_kind, last_bar


swing_records = []
# Cache per-file 1s-bar median range for the ATR proxy.
atr_cache = {}
for path in scope_paths:
    from src.tick.aggtrade_aggregator import aggregate_ticks_to_1s_bars
    raw = load_concat_raw_aggtrades([path])
    bars = aggregate_ticks_to_1s_bars(raw)
    rng = (bars.high - bars.low).to_numpy()
    # Median of the window is a robust proxy.
    atr_proxy = float(np.median(rng)) if rng.size else 0.0
    atr_cache[path.name] = atr_proxy

for path in scope_paths:
    state, _ = structure_cache[path.name]
    sub = trades_df[trades_df.file == path.name].copy()
    if sub.empty:
        continue
    sub = sub.assign(entry_ns=sub.entry_time.astype("int64"))
    rows = []
    bars_time_ns = bars_time_ns_by_file[path.name]
    for _, row in sub.iterrows():
        ns = int(row.entry_ns)
        px, kind, _ = _last_broken_swing(state, ns, bars_time_ns)
        swing_dist = None
        reclaim = False
        if px is not None:
            swing_dist = abs(float(row.entry_price) - px)
            atr_p = atr_cache[path.name]
            if atr_p > 0:
                reclaim = swing_dist <= SWING_RECLAIM_ATR_FRAC * atr_p
        rows.append({
            "swing_anchor_price": px,
            "swing_kind": kind,
            "swing_distance_usd": swing_dist,
            "swing_atr_proxy": atr_cache[path.name],
            "swing_reclaim": reclaim,
        })
    extra = pd.DataFrame(rows, index=sub.index)
    swing_records.append(pd.concat([sub, extra], axis=1))

swing_df_trades = pd.concat(swing_records, ignore_index=True)
n_reclaim = int(swing_df_trades.swing_reclaim.sum(skipna=True))
n_swinged = int(swing_df_trades.swing_anchor_price.notna().sum())
print(f"\nF.2 — Swing H/L reclaim:")
print(f"  sw.bar converted to wall-clock ns via bars.time[sw.bar];")
print(f"  the most-recent broken swing H/L is queried causally at entry.")
print(f"  {n_reclaim}/{n_swinged} swing-touched trades are within ±{SWING_RECLAIM_ATR_FRAC}×ATR of the most-recent swing H/L")
print(f"  (out of {n_total} trades total)")
print(swing_df_trades.groupby("swing_reclaim")["pnl_usd"].agg(["count", "mean", "sum"]))

# Persist F.2 output.
swing_df_trades.to_csv(
    OUT / "nb52_F2_swing_reclaim_per_trade.csv", index=False,
)
swing_df_trades.groupby("swing_reclaim")["pnl_usd"].agg(
    ["count", "mean", "sum"]
).to_csv(OUT / "nb52_F2_swing_reclaim_buckets.csv")


# ---- F.3 True-iFVG vs fvg-but-invalidated classifier ---------------
# For each trade's anchor FVG zone, check:
#   true-iFVG          : inverted_bar is set, AND in the 60 bars AFTER
#                        inverted_bar there is at least one bar whose
#                        CLOSE settled on the inverted side (close
#                        above zone_high for bear→bull inversion or
#                        close below zone_low for bull→bear inversion)
#                        by at least fvg_invalidation_min_pierce_usd.
#   fvg-but-invalidated: inverted_bar is set but NO such retest
#                        close in the 60 bars after.
# Trades whose anchor is None, or that did not enter via iFVG / sniper
# from a zone that got inverted, are excluded.
#
# We read the 1s bars once per file and reuse the same window.

def _classify_ifvg_buckets(path: Path, sub: pd.DataFrame) -> pd.DataFrame:
    """Classify each trade's anchor zone into one of:
       'true_ifvg' | 'fvg_invalidated' | 'not_applicable'.

    A zone qualifies if ``inverted_bar >= 0`` AND the iFVG entry path
    fired the trade (i.e. entry_triggered_by in {'ifvg','sniper'}).
    Buckets defined per header above.
    """
    from src.tick.aggtrade_aggregator import aggregate_ticks_to_1s_bars
    raw = load_concat_raw_aggtrades([path])
    bars = aggregate_ticks_to_1s_bars(raw)

    # Build close + bar→time lookup
    close = bars.close.to_numpy()
    times = bars.time.astype("int64").to_numpy()
    # Index bars by their ts (ns) for fast lookup of inverted_bar position
    bar_ts_to_idx = {int(t // 1_000_000_000): i for i, t in enumerate(times)}
    bar_starts = np.array(list(bar_ts_to_idx.keys()), dtype=np.int64)

    out = []
    # We need the user's parameter for `fvg_invalidation_min_pierce_usd`.
    p = optimal_params(fvg_inv_trade_sl_zone_mult=BEST_SL, fvg_inv_trade_tp_zone_mult=BEST_TP)
    pierce_usd = float(getattr(p, "fvg_invalidation_min_pierce_usd", 2.0))

    for _, row in sub.iterrows():
        etb = str(row["entry_triggered_by"])
        # ``fvg_zone`` may be missing if the trade row came from
        # res.trades_df() (which strips the zone). The
        # trades_df_with_zone wrapper restores it.
        zone = row.get("fvg_zone", None) if "fvg_zone" in row.index else None
        if zone is None or etb not in ("ifvg", "sniper"):
            out.append("not_applicable")
            continue
        inv_bar = int(getattr(zone, "inverted_bar", -1))
        if inv_bar < 0:
            out.append("not_applicable")
            continue
        # Look at bars [inv_bar+1, inv_bar+IFVG_RETEST_BARS]
        lo = inv_bar + 1
        hi = min(len(close), inv_bar + 1 + IFVG_RETEST_BARS)
        if lo >= hi:
            out.append("fvg_invalidated")
            continue
        zl = float(zone.zone_low)
        zh = float(zone.zone_high)
        direction = int(getattr(zone, "direction", 0))
        retested = False
        for j in range(lo, hi):
            c = float(close[j])
            if direction > 0:    # original bull FVG, killed bearishly
                if c < zl - pierce_usd:
                    retested = True
                    break
            elif direction < 0:  # original bear FVG, killed bullishly
                if c > zh + pierce_usd:
                    retested = True
                    break
        out.append("true_ifvg" if retested else "fvg_invalidated")
    return pd.DataFrame({"ifvg_class": out}, index=sub.index)


ifvg_records = []
for path in scope_paths:
    sub = trades_df[trades_df.file == path.name].copy()
    if sub.empty:
        continue
    cls = _classify_ifvg_buckets(path, sub)
    ifvg_records.append(pd.concat([sub, cls], axis=1))

ifvg_df_trades = pd.concat(ifvg_records, ignore_index=True)
print(f"\nF.3 — True-iFVG vs fvg-but-invalidated:")
print(ifvg_df_trades.groupby(["ifvg_class", "exit_reason"]).size().to_string())
print("\nPnL by class:")
print(ifvg_df_trades.groupby("ifvg_class")["pnl_usd"].agg(["count", "mean", "sum"]))

# Persist F.3 output — this is the headline finding from the alpha trail.
ifvg_df_trades.to_csv(
    OUT / "nb52_F3_ifvg_classifier_per_trade.csv", index=False,
)
ifvg_df_trades.groupby("ifvg_class")["pnl_usd"].agg(
    ["count", "mean", "sum"]
).to_csv(OUT / "nb52_F3_ifvg_classifier_buckets.csv")
ifvg_df_trades.groupby(["ifvg_class", "exit_reason"]).size().to_csv(
    OUT / "nb52_F3_ifvg_x_exit_counts.csv",
)


# ====================================================================
# Cell G — Recommendation summary (markdown)
# ====================================================================
# %% recommendation summary
#
# (Manual / markdown cell — print the summary below when reviewing.)
# --------------------------------------------------------------------

print("\n" + "=" * 72)
print("NB52 — RECOMMENDATION SUMMARY")
print("=" * 72)
print()
print(f"6-month sample: {[p.name for p in SAMPLE_6]}")
print(f"In-scope files for diagnostics: {[p.name for p in scope_paths]}")
if NB52_SKIP_SWEEP:
    print(f"Best (sl, tp) cell: SKIPPED (NB52_SKIP_SWEEP=1); using canonical "
          f"SL={BEST_SL}x, TP={BEST_TP}x for F diagnostics.")
    print(f"  canonical PnL=${best.pnl_total_sum:+.2f}  EV=${best.ev_mean:+.4f}")
else:
    print(f"Best (sl, tp) cell: SL={best.sl_mult}x, TP={best.tp_mult}x  "
          f"mean pnl=${best.pnl_total_mean:+.2f}/month, "
          f"mean WR={best.wr_mean:.1%}")
    canon_rank = (agg.pnl_total_sum >= canon_row.pnl_total_sum.iloc[0]).sum()
    print(f"Canonical (SL=2x, TP=22x) rank: {canon_rank}/{len(agg)}  "
          f"(mean pnl=${float(canon_row.pnl_total_mean.iloc[0]):+.2f}/month)")
print()
print("F.1 sweep-distance bucket EV:")
fresh_ev = sweep_df_trades[sweep_df_trades.sweep_bucket == SWEEP_BUCKET_LABELS[0]].pnl_usd.mean()
cold_ev = sweep_df_trades[sweep_df_trades.sweep_bucket == SWEEP_BUCKET_LABELS[-1]].pnl_usd.mean()
fresh_n = int((sweep_df_trades.sweep_bucket == SWEEP_BUCKET_LABELS[0]).sum())
cold_n = int((sweep_df_trades.sweep_bucket == SWEEP_BUCKET_LABELS[-1]).sum())
fresh_str = f"${fresh_ev:+.4f} (n={fresh_n})" if fresh_n > 0 else "n/a (0 trades)"
cold_str = f"${cold_ev:+.4f} (n={cold_n})" if cold_n > 0 else "n/a (0 trades)"
print(f"  Fresh (0-30s post-sweep):  EV/trade = {fresh_str}")
print(f"  Cold  (600s+ post-sweep):  EV/trade = {cold_str}")
print("F.2 swing-reclaim EV:")
if n_reclaim > 0:
    r_ev = swing_df_trades[swing_df_trades.swing_reclaim].pnl_usd.mean()
    nr_ev = swing_df_trades[~swing_df_trades.swing_reclaim].pnl_usd.mean()
    print(f"  Reclaim trades:    EV/trade = ${r_ev:+.4f} (n={int(swing_df_trades.swing_reclaim.sum())})")
    print(f"  Non-reclaim trades: EV/trade = ${nr_ev:+.4f} (n={int((~swing_df_trades.swing_reclaim).sum())})")
else:
    # Distinguish "no trades at all" from "no reclaim-anchored trades"
    if n_swinged == 0:
        print("(no swing-touched trades in scope — every trade is "
              "FVG-anchored away from any broken swing H/L)")
    else:
        print(f"(no reclaim-anchored trades: 0/{n_swinged} swing-touched "
              f"trades land within ±{SWING_RECLAIM_ATR_FRAC}×ATR of the most-"
              f"recent broken swing H/L — sniper entries consistently sit "
              f"OUTSIDE the broken-swing reclaim band)")
print("F.3 ifvg-classifier PnL by bucket:")
print(ifvg_df_trades.groupby("ifvg_class")["pnl_usd"].agg(["count", "mean", "sum"]).to_string())
print()
print("Files written:")
print(f"  {OUT}")
print("  - nb52_sweep_results.csv        (150-row sweep grid)")
print("  - nb52_best_cell_trades.csv     (all trades on winning (sl,tp))")

print("  - nb52_sweep_heatmap.png        (mean pnl heatmap)")
print()
print("Next experimental knobs the data may justify (NOT YET ADDED):")
print("  1. sniper_sweep_lookback_secs (default 60s)")
print("  2. sniper_swing_reclaim_atr  (default 0.5)")
print("  3. ifvg_retest_min_close_offset (replace drop_inverted_fvg)")

# Final consolidated summary CSV — one row, the headline numbers.
# Use explicit None sentinels for any branch that wasn't reached
# (e.g. fresh_bucket empty or no reclaim trades).
_r_ev = None
_nr_ev = None
if n_reclaim > 0:
    _r_ev = float(swing_df_trades[swing_df_trades.swing_reclaim].pnl_usd.mean())
    _nr_ev = float(swing_df_trades[~swing_df_trades.swing_reclaim].pnl_usd.mean())

_summary_row = {
    "scope_files": ",".join(p.name for p in scope_paths),
    "n_trades": int(len(trades_df)),
    "total_pnl_usd": float(trades_df["pnl_usd"].sum()) if len(trades_df) else 0.0,
    "ev_per_trade_usd": float(trades_df["pnl_usd"].mean()) if len(trades_df) else 0.0,
    "win_rate_pct": (100.0 * float((trades_df["pnl_usd"] > 0).mean()))
                    if len(trades_df) else 0.0,
    "f1_fresh_ev_usd": float(fresh_ev) if fresh_n > 0 else float("nan"),
    "f1_fresh_n": fresh_n,
    "f1_cold_ev_usd": float(cold_ev) if cold_n > 0 else float("nan"),
    "f1_cold_n": cold_n,
    "f2_reclaim_ev_usd": _r_ev if _r_ev is not None else float("nan"),
    "f2_reclaim_n": int(n_reclaim),
    "f3_true_ifvg_ev_usd": float(
        ifvg_df_trades[ifvg_df_trades.ifvg_class == "true_ifvg"].pnl_usd.mean()
    ) if (len(ifvg_df_trades) and "true_ifvg" in ifvg_df_trades.ifvg_class.values) else float("nan"),
    "f3_fvg_invalidated_ev_usd": float(
        ifvg_df_trades[ifvg_df_trades.ifvg_class == "fvg_invalidated"].pnl_usd.mean()
    ) if (len(ifvg_df_trades) and "fvg_invalidated" in ifvg_df_trades.ifvg_class.values) else float("nan"),
    "skipped_sweep": bool(NB52_SKIP_SWEEP),
    "best_sl": float(best.sl_mult) if not NB52_SKIP_SWEEP else float("nan"),
    "best_tp": float(best.tp_mult) if not NB52_SKIP_SWEEP else float("nan"),
    "canonical_sl": 2.0,
    "canonical_tp": 22.0,
    "out_dir": str(OUT),
}
pd.DataFrame([_summary_row]).to_csv(
    OUT / "nb52_recommendation_summary.csv", index=False,
)


# ====================================================================
# Cell H — Hypothesis scenarios from the user (added 2026-09-26)
# ====================================================================
# %% hypothesis scenarios
#
# Seven scenarios the user explicitly asked for in chat. Each
# scenario re-runs the canonical recipe on SAMPLE_6[0] (2025-04)
# with ONE knob flipped, then persists a per-trade CSV and a
# bucket/summary CSV. The scenarios are intentionally cheap (one
# file each) so they all finish in a single NB52_SKIP_SWEEP run.
#
# The scenarios are:
#   H.1 — Flip sniper direction (continuation vs fade_displacement)
#   H.2 — Sniper + immediate: fire on mitigation, not just inversion
#   H.3 — Strict-wick FVG: candle 3 low/high wick must extend past
#          candle 1 high/low by >= strict_wick_min_wick_usd
#   H.4 — Mitigation-distance filter: drop FVG signals whose
#          mitigation landed < min_dist_bars from trigger
#   H.5 — MFE / MAE analysis on the canonical trade list
#   H.6 — Min zone breadth sweep (drop zones narrower than $X)
#   H.7 — Conviction: count unsuperseded counter-direction FVGs at
#          entry as a structural pull-strength proxy
#
# Each scenario prints a one-paragraph takeaway so the user can read
# the results inline without opening the CSVs.
# --------------------------------------------------------------------

SCENARIO_FILE = SAMPLE_6[0]
print(f"\nH — Hypothesis scenarios on {SCENARIO_FILE.name} "
      f"(canonical v17 SNIPER baseline):\n")


def _summary_block(label: str, out: dict) -> dict:
    """Return a flat dict of headline numbers for the scenario table."""
    return {
        "label": label,
        "n_trades": int(out["n_trades"]),
        "pnl_total_usd": float(out["pnl_total"]),
        "ev_per_trade_usd": float(out["ev_per_trade"]),
        "win_rate_pct": 100.0 * float(out["win_rate"]),
        "trades_per_day": float(out["trades_per_day"]),
        "fees_total_usd": float(out.get("fees_total", 0.0)),
        "elapsed_s": float(out["elapsed_s"]),
    }


# ---- H.1 — Flip sniper direction ----------------------------------
# Canonical = continuation (sniper enters in original FVG gap
# direction upon inversion). Alternate = fade_displacement (sniper
# enters OPPOSITE the original gap, like the live engine in the
# sibling repo). The two paths produce OPPOSITE directions on the
# same setup; this is a known divergence (AGENTS.md § Sniper
# direction on inversion).
print("H.1 — Flip sniper direction:")
h1_canon = run_one_file(SCENARIO_FILE, "nb52_h1_continuation",
                         include_trades=True)
h1_flip = run_one_file(
    SCENARIO_FILE, "nb52_h1_fade_displacement",
    include_trades=True,
    sniper_inv_direction_mode="fade_displacement",
)
h1_summary = pd.DataFrame([
    _summary_block("continuation (canonical)", h1_canon),
    _summary_block("fade_displacement", h1_flip),
])
h1_summary["delta_pnl_usd"] = h1_summary.pnl_total_usd.diff()
print(h1_summary.to_string(index=False))

# Per-trade dump for both runs (continuation is the canonical baseline;
# fade_displacement is the alternate path).
pd.DataFrame([t.to_dict() for t in h1_canon["trades"]]).assign(
    file=SCENARIO_FILE.name,
    scenario="continuation",
).to_csv(OUT / "nb52_H1_flip_sniper_per_trade.csv", index=False)
pd.DataFrame([t.to_dict() for t in h1_flip["trades"]]).assign(
    file=SCENARIO_FILE.name,
    scenario="fade_displacement",
).to_csv(OUT / "nb52_H1_flip_sniper_per_trade.csv", mode="a",
          header=False, index=False)
h1_summary.to_csv(OUT / "nb52_H1_flip_sniper_summary.csv", index=False)
print(f"  -> wrote {OUT / 'nb52_H1_flip_sniper_per_trade.csv'}")
print(f"  -> wrote {OUT / 'nb52_H1_flip_sniper_summary.csv'}\n")


# ---- H.2 — Sniper + immediate (fire on mitigation) -----------------
# Canonical sniper fires only on `zone.inverted`. We flip
# `fvg_inv_trade_enabled=False` and `drop_inverted_fvg=False` so
# the bar-loop's iFVG path fires on the FIRST mitigation (body
# cross of zone_low/zone_high) instead of waiting for inversion.
# Note: this disables the sniper queue and routes trades through
# the retest queue.
print("H.2 — Fire on mitigation (sniper off, immediate on mitigation):")
h2_canon = h1_canon  # reuse canonical
h2_imm = run_one_file(
    SCENARIO_FILE, "nb52_h2_immediate_on_mit",
    include_trades=True,
    fvg_inv_trade_enabled=False,
    drop_inverted_fvg=False,
)
h2_summary = pd.DataFrame([
    _summary_block("sniper (canonical)", h2_canon),
    _summary_block("immediate_on_mitigation", h2_imm),
])
h2_summary["delta_pnl_usd"] = h2_summary.pnl_total_usd.diff()
print(h2_summary.to_string(index=False))

pd.DataFrame([t.to_dict() for t in h2_imm["trades"]]).assign(
    file=SCENARIO_FILE.name,
    scenario="immediate_on_mit",
).to_csv(OUT / "nb52_H2_sniper_immediate_per_trade.csv", index=False)
h2_summary.to_csv(OUT / "nb52_H2_sniper_immediate_summary.csv", index=False)
print(f"  -> wrote {OUT / 'nb52_H2_sniper_immediate_per_trade.csv'}")
print(f"  -> wrote {OUT / 'nb52_H2_sniper_immediate_summary.csv'}\n")


# ---- H.3 — Strict-wick FVG detection ------------------------------
# Bullish FVG: candle3.low must extend > candle1.high by at least
# `strict_wick_min_wick_usd`. Bearish: candle3.high must extend <
# candle1.low by the same. The detector already supports this via
# `fvg_strict_wick_required=True` + `fvg_strict_wick_min_wick_usd=X`.
# We sweep X over {0.50, 1.00, 5.00, 20.00, 100.00} (USD on 1s bars).
print("H.3 — Strict-wick FVG (candle 3 must extend past candle 1):")
H3_WICK_FLOORS = [0.50, 1.00, 5.00, 20.00, 100.00]
h3_records = []
h3_per_trade_rows = []
for w in H3_WICK_FLOORS:
    res = run_one_file(
        SCENARIO_FILE, f"nb52_h3_wick_{w}",
        include_trades=True,
        fvg_strict_wick_required=True,
        fvg_strict_wick_min_wick_usd=w,
    )
    row = _summary_block(f"strict_wick>${w}", res)
    row["wick_floor_usd"] = w
    h3_records.append(row)
    for t in res["trades"]:
        d = t.to_dict()
        d["wick_floor_usd"] = w
        d["scenario"] = f"strict_wick>${w}"
        h3_per_trade_rows.append(d)
h3_summary = pd.DataFrame(h3_records)
h3_summary["delta_vs_canon_pnl"] = h3_summary.pnl_total_usd - h1_canon["pnl_total"]
print(h3_summary.to_string(index=False))
pd.DataFrame(h3_per_trade_rows).to_csv(
    OUT / "nb52_H3_strict_wick_per_trade.csv", index=False,
)
h3_summary.to_csv(OUT / "nb52_H3_strict_wick_summary.csv", index=False)
print(f"  -> wrote {OUT / 'nb52_H3_strict_wick_per_trade.csv'}")
print(f"  -> wrote {OUT / 'nb52_H3_strict_wick_summary.csv'}\n")


# ---- H.4 — Mitigation distance filter -----------------------------
# Drop FVG signals whose mitigation landed within N bars of the
# trigger candle. The detector supports `fvg_min_mit_distance_bars`
# in `generate_ict_pending_signals` — we sweep N over {0, 1, 3, 5, 10}.
# NB: sniper mode bypasses this filter because it fires on
# `zone.inverted`, not on mitigation. To get the filter to bite
# we also disable sniper (H.4 trades via the retest queue).
print("H.4 — Mitigation distance filter (sniper bypassed):")
H4_MIT_DISTS = [0, 1, 3, 5, 10]
h4_records = []
h4_per_trade_rows = []
for d in H4_MIT_DISTS:
    res = run_one_file(
        SCENARIO_FILE, f"nb52_h4_mitdist_{d}",
        include_trades=True,
        fvg_inv_trade_enabled=False,    # route via retest queue
        fvg_min_mit_distance_bars=d,
    )
    row = _summary_block(f"mit_dist>={d}", res)
    row["mit_distance_bars"] = d
    h4_records.append(row)
    for t in res["trades"]:
        tr = t.to_dict()
        tr["mit_distance_bars"] = d
        tr["scenario"] = f"mit_dist>={d}"
        h4_per_trade_rows.append(tr)
h4_summary = pd.DataFrame(h4_records)
h4_summary["delta_vs_canon_pnl"] = h4_summary.pnl_total_usd - h1_canon["pnl_total"]
print(h4_summary.to_string(index=False))
pd.DataFrame(h4_per_trade_rows).to_csv(
    OUT / "nb52_H4_mit_distance_per_trade.csv", index=False,
)
h4_summary.to_csv(OUT / "nb52_H4_mit_distance_summary.csv", index=False)
print(f"  -> wrote {OUT / 'nb52_H4_mit_distance_per_trade.csv'}")
print(f"  -> wrote {OUT / 'nb52_H4_mit_distance_summary.csv'}\n")


# ---- H.5 — MFE / MAE analysis -------------------------------------
# For each canonical trade, walk the 1s bars from entry to exit
# and record (a) the peak unrealized PnL (MFE) and (b) the trough
# unrealized PnL (MAE). Bucket by exit_reason. If TP-exits cluster
# near the SL side and leave MFE on the table, that's a TP-too-low
# problem. If SL-exits cluster with MFE > 0 before reversing, the
# stop is too tight. Both inform the SL/TP sweep in Cells C-E.
#
# NB: Trade does not store contract_size — that's read from the
# params object. We use BTC = 1.0 (canonical) here; adjust if a
# non-BTC corpus is ever added.
print("H.5 — MFE / MAE analysis on canonical trades:")
_BTC_CONTRACT_SIZE = 1.0

def _mfe_mae(trades_list, bars):
    """For each Trade, walk 1s bars from entry to exit and record
    peak/trough unrealized PnL in USD. Returns a list of dicts."""
    if len(trades_list) == 0:
        return []
    high = bars["high"].to_numpy(dtype=np.float64)
    low = bars["low"].to_numpy(dtype=np.float64)
    time_arr = bars["time"].astype("int64").to_numpy()
    rows = []
    for idx, t in enumerate(trades_list):
        # Trade.entry_time is int (ns since epoch); pandas Timestamp.value
        # only exists on Timestamp objects. Convert via pd.Timestamp +
        # .value when needed; here just use the int directly.
        entry_ns = int(t.entry_time)
        exit_ns = int(t.exit_time)
        # bars.time is the bar START; find the entry bar via searchsorted.
        i0 = int(np.searchsorted(time_arr, entry_ns, side="left"))
        i1 = int(np.searchsorted(time_arr, exit_ns, side="right"))
        i0 = max(0, min(i0, len(high) - 1))
        i1 = max(i0 + 1, min(i1, len(high)))
        if i1 <= i0:
            i1 = min(i0 + 1, len(high))
        if t.direction > 0:  # LONG
            best = float(high[i0:i1].max())
            worst = float(low[i0:i1].min())
        else:                # SHORT
            best = float(low[i0:i1].min())
            worst = float(high[i0:i1].max())
        ep = float(t.entry_price)
        sign = float(t.direction)
        lots = float(t.lots)
        cs = _BTC_CONTRACT_SIZE
        mfe_usd = (best - ep) * sign * lots * cs
        mae_usd = (worst - ep) * sign * lots * cs
        rows.append({
            "trade_idx": int(idx),
            "exit_reason": str(t.exit_reason),
            "direction": int(t.direction),
            "entry_price": ep,
            "exit_price": float(t.exit_price),
            "pnl_usd_net": float(t.pnl_usd),
            "fee_usd": float(getattr(t, "fee_usd", 0.0)),
            "mfe_usd": float(mfe_usd),
            "mae_usd": float(mae_usd),
            "mfe_left_on_table_usd": float(mfe_usd - t.pnl_usd),
            "n_bars_in_trade": int(i1 - i0),
        })
    return rows


# Get bars once for H.5.
from src.tick.aggtrade_aggregator import aggregate_ticks_to_1s_bars
_h5_raw = load_concat_raw_aggtrades([SCENARIO_FILE])
_h5_bars = aggregate_ticks_to_1s_bars(_h5_raw)

h5_rows = _mfe_mae(h1_canon["trades"], _h5_bars)
h5_df = pd.DataFrame(h5_rows)
h5_df["file"] = SCENARIO_FILE.name
if not h5_df.empty:
    h5_df["scenario"] = "canonical"
    h5_df.to_csv(OUT / "nb52_H5_mfe_per_trade.csv", index=False)
    by_exit = h5_df.groupby("exit_reason").agg(
        n=("pnl_usd_net", "count"),
        mfe_mean=("mfe_usd", "mean"),
        mfe_med=("mfe_usd", "median"),
        mae_mean=("mae_usd", "mean"),
        mae_med=("mae_usd", "median"),
        left_on_table_mean=("mfe_left_on_table_usd", "mean"),
        pnl_mean=("pnl_usd_net", "mean"),
        pnl_sum=("pnl_usd_net", "sum"),
    )
    print("\nMFE / MAE summary by exit_reason:")
    print(by_exit.to_string())
    by_exit.to_csv(OUT / "nb52_H5_mfe_by_exit.csv")
    print(f"\nTotal MFE left on table by TP-exits:  "
          f"${h5_df[h5_df.exit_reason == 'tp'].mfe_left_on_table_usd.sum():+.2f}")
    print(f"Total MFE captured by SL-exits before reversal: "
          f"${h5_df[h5_df.exit_reason == 'sl'].mfe_usd.clip(lower=0.0).sum():+.2f}")
    # MFE by intended-stop bucket.
    h5_df["sl_bucket"] = pd.cut(
        h5_df["mae_usd"].abs(),
        bins=[0, 5, 10, 20, 50, 1e6],
        labels=["$0-5", "$5-10", "$10-20", "$20-50", "$50+"],
    )
    by_sl = h5_df.groupby("sl_bucket", observed=True).agg(
        n=("pnl_usd_net", "count"),
        mfe_mean=("mfe_usd", "mean"),
        mae_mean=("mae_usd", "mean"),
        pnl_sum=("pnl_usd_net", "sum"),
    )
    print("\nMFE / MAE by |MAE| bucket (proxy for SL floor):")
    print(by_sl.to_string())
    by_sl.to_csv(OUT / "nb52_H5_mfe_by_stop.csv")
    # MFE by intended-TP bucket.
    h5_df["tp_bucket"] = pd.cut(
        h5_df["mfe_usd"],
        bins=[-1e6, 5, 10, 20, 50, 200, 1e6],
        labels=["<$5", "$5-10", "$10-20", "$20-50", "$50-200", "$200+"],
    )
    by_tp = h5_df.groupby("tp_bucket", observed=True).agg(
        n=("pnl_usd_net", "count"),
        mfe_mean=("mfe_usd", "mean"),
        pnl_sum=("pnl_usd_net", "sum"),
    )
    print("\nPnL by MFE bucket (proxy for TP floor):")
    print(by_tp.to_string())
    by_tp.to_csv(OUT / "nb52_H5_mfe_by_target.csv")
else:
    print("(no canonical trades; H.5 skipped)")
print()


# ---- H.6 — Min zone breadth sweep ----------------------------------
# Sweep `fvg_min_zone_usd` ∈ {$5, $10, $20, $30, $50, $100} (USD).
# Floor of $5 is the canonical default. $30 was the user's quote
# ("30$ just instant SL me"). The detector filters FVG zones by
# `fvg_min_zone_usd`; iFVG zones use the same knob by default.
# Goal: find the smallest zone breadth where round-trip fees
# (10 bps × notional) don't dominate the SL.
print("H.6 — Min zone breadth sweep (fee-aware floor):")
H6_FLOORS = [5.0, 10.0, 20.0, 30.0, 50.0, 100.0]
h6_records = []
h6_per_trade_rows = []
for f in H6_FLOORS:
    res = run_one_file(
        SCENARIO_FILE, f"nb52_h6_min_zone_{int(f)}",
        include_trades=True,
        fvg_min_zone_usd=f,
        ifvg_min_zone_usd=f,
    )
    row = _summary_block(f"zone_floor=${int(f)}", res)
    row["zone_floor_usd"] = float(f)
    # Add notional-based fee ratio: fee / |pnl| on losers
    losers = [t for t in res["trades"] if t.pnl_usd < 0]
    if losers:
        avg_loss = float(np.mean([t.pnl_usd for t in losers]))
        avg_fee = float(np.mean([getattr(t, "fee_usd", 0.0) for t in losers]))
        row["fee_over_loss_pct"] = (
            100.0 * avg_fee / abs(avg_loss) if avg_loss != 0 else float("nan")
        )
    else:
        row["fee_over_loss_pct"] = float("nan")
    h6_records.append(row)
    for t in res["trades"]:
        d = t.to_dict()
        d["zone_floor_usd"] = float(f)
        d["scenario"] = f"zone_floor=${int(f)}"
        h6_per_trade_rows.append(d)
h6_summary = pd.DataFrame(h6_records)
print(h6_summary.to_string(index=False))
pd.DataFrame(h6_per_trade_rows).to_csv(
    OUT / "nb52_H6_zone_breadth_per_trade.csv", index=False,
)
h6_summary.to_csv(OUT / "nb52_H6_zone_breadth_summary.csv", index=False)
print(f"  -> wrote {OUT / 'nb52_H6_zone_breadth_per_trade.csv'}")
print(f"  -> wrote {OUT / 'nb52_H6_zone_breadth_summary.csv'}\n")


# ---- H.7 — Conviction via unsuperseded counter-direction FVG count
# For each trade, count the number of LIVE (un-mitigated,
# un-inverted, un-superseded) bull FVGs and bear FVGs at the
# entry second. The user's framing: many unsuperseded counter-
# direction FVGs imply a structural pull toward those zones,
# i.e. high conviction for a mean-reversion move that will take
# price through the anchor zone before resuming.
print("H.7 — Conviction via unsuperseded FVG count:")
def _unsuperseded_count(zones: list, entry_ns: int,
                          direction_sign: int,
                          bars_time_ns: np.ndarray) -> int:
    """Count zones that are LIVE at entry_ns — i.e. zone created
    before entry, not mitigated, not inverted, not superseded."""
    n = 0
    for z in zones:
        if z.trigger_bar < 0 or z.trigger_bar >= len(bars_time_ns):
            continue
        trigger_ns = int(bars_time_ns[z.trigger_bar])
        if trigger_ns >= entry_ns:
            continue  # zone didn't exist yet
        if z.direction != direction_sign:
            continue
        if z.mitigated_bar >= 0:
            continue
        if z.inverted_bar >= 0:
            continue
        if z.superseded_bar >= 0:
            continue
        n += 1
    return n


# Use the cached bars.time[] for SCENARIO_FILE (we built it earlier).
h7_bars_time_ns = bars_time_ns_by_file.get(SCENARIO_FILE.name)
if h7_bars_time_ns is None:
    h7_bars_time_ns = _h5_bars["time"].astype("int64").to_numpy()
# Pull live zones from the cache (NOT trade zones — these are the
# full detector-side list filtered only by zone_min_usd, no
# trade-side mutation since this is the first config in the
# process).
from src.tick.cache import get_or_build
from src.core.optimal_config import optimal_params as _optp_for_h7
_h7_p = _optp_for_h7()
_h7_sc = get_or_build(SCENARIO_FILE, _h7_p, with_side_table=True, verbose=False)
bull_zones = list(_h7_sc.fvg_zones) + list(_h7_sc.ifvg_zones)

h7_rows = []
for idx, t in enumerate(h1_canon["trades"]):
    entry_ns = int(t.entry_time)  # Trade.entry_time is int (ns since epoch)
    # "Counter direction" = opposite of trade direction.
    counter = -int(t.direction)
    # "Same direction" = aligned with trade direction.
    same = int(t.direction)
    counter_count = _unsuperseded_count(
        bull_zones, entry_ns, counter, h7_bars_time_ns,
    )
    same_count = _unsuperseded_count(
        bull_zones, entry_ns, same, h7_bars_time_ns,
    )
    h7_rows.append({
        "trade_idx": int(idx),
        "entry_time": t.entry_time,
        "direction": int(t.direction),
        "entry_price": float(t.entry_price),
        "pnl_usd_net": float(t.pnl_usd),
        "exit_reason": str(t.exit_reason),
        "unsup_count_counter": int(counter_count),
        "unsup_count_same": int(same_count),
        "net_unsup": int(same_count - counter_count),
    })
h7_df = pd.DataFrame(h7_rows)
h7_df["file"] = SCENARIO_FILE.name
h7_df["scenario"] = "canonical"
h7_df.to_csv(OUT / "nb52_H7_conviction_per_trade.csv", index=False)

# Decile the counter-direction count and report EV per decile.
if not h7_df.empty and h7_df["unsup_count_counter"].nunique() > 1:
    h7_df["counter_decile"] = pd.qcut(
        h7_df["unsup_count_counter"], 10,
        labels=False, duplicates="drop",
    )
    by_dec = h7_df.groupby("counter_decile").agg(
        n=("pnl_usd_net", "count"),
        mean_pnl=("pnl_usd_net", "mean"),
        sum_pnl=("pnl_usd_net", "sum"),
        mean_counter=("unsup_count_counter", "mean"),
    )
    print("Per-decile PnL by counter-direction unsuperseded FVG count:")
    print(by_dec.to_string())
    by_dec.to_csv(OUT / "nb52_H7_conviction_per_decile.csv")
else:
    by_dec = h7_df.groupby("unsup_count_counter").agg(
        n=("pnl_usd_net", "count"),
        mean_pnl=("pnl_usd_net", "mean"),
        sum_pnl=("pnl_usd_net", "sum"),
    )
    print("Per-counter-count PnL (deciles collapsed because counts are low):")
    print(by_dec.to_string())
    by_dec.to_csv(OUT / "nb52_H7_conviction_per_decile.csv")
print(f"  -> wrote {OUT / 'nb52_H7_conviction_per_trade.csv'}")
print(f"  -> wrote {OUT / 'nb52_H7_conviction_per_decile.csv'}\n")

print("=" * 72)
print("NB52 — Hypothesis scenario sweep complete. All H.1-H.7 outputs")
print("persisted under notebooks/nb52_outputs/nb52_H*.csv")
print("=" * 72)
