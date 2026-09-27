"""NB56 follow-up — entry_mode='dual' (sniper + immediate in tandem) on the top 3 SL/TP combos.

Why this experiment
===================

`nb56_combined_sniper_immediate.py` showed that the canonical
routing (clean vs dirty per-zone) collapses to all_clean on the
6-month corpus — every surviving zone qualifies as clean, so
the sniper path is starved.

User feedback (2026-09-26):
  * "doesn't make sense that clean and dirty both have same trades,
    they are not mutually exclusive and should not override each
    other."
  * "immediate mode merely trades in the direction of the original,
    uninverted FVG provided they are mitigated, but not inverted."
  * "i don't see why if immediate + sniper wouldn't increase
    trading freq by a lot"

Goal of this driver: combine the two paths **without per-zone
mutual exclusion**. New ``entry_mode='dual'`` (added 2026-09-26):
  * Immediate ladder path fires on FVG signal (mitigated, original
    direction).
  * Sniper path fires on the SAME FVG signal (waits for inversion,
    fires continuation).
The two paths target different events on the same zone and
produce disjoint trade sets per zone. The combined run is
expected to:
  * Increase trade count by ~5-10× (every zone that mitigates
    within sniper-age cap gets both trades).
  * Add the immediate ladder's PnL to the sniper's PnL.
  * Show whether the ladder is a fee-biter (negative EV) at
    canonical scale.

Additionally, the canonical recipe was updated (2026-09-26 f):
  * ``fvg_min_zone_usd``: $5 → $20
  * ``ifvg_min_zone_usd``: $0.10 → $20
  * CACHE_VERSION bumped to 5 (cache rebuild required)
Both FVG and iFVG detectors now share a $20 floor. This means
zones narrower than $20 are dropped at detection time, so the
detector output reflects the new threshold.

Run modes
=========

```bash
# fast smoke (single 2026 file for cache hit, ~30s)
python notebooks/nb56_dual_entry_top3.py --fast

# 6-month full validation
python notebooks/nb56_dual_entry_top3.py
```

Output
======

`notebooks/nb56_dual_entry_top3_outputs/`:
* `nb56_dual_entry_top3__runs.jsonl` — one record per
  (combo × mode × file). 36 records at full 6-month run.
* `nb56_dual_entry_top3_per_trade.csv` — per-trade with the
  ``entry_triggered_by`` tag preserved so the dual-fire path
  attribution can be inspected.
* `nb56_dual_entry_top3_summary.csv` — cross-month aggregate per cell.

The "modes" compared:
  * ``sniper_canonical``: legacy sniper-only (entry_mode='sniper').
    68 trades/2025-04. Baseline.
  * ``dual``: new entry_mode='dual' — fires BOTH immediate
    ladder AND sniper paths on every FVG signal.
    339 trades/2025-04. 5× frequency, but ladder path is a fee-biter.

Honest framing
==============

* This is a **measurement** of whether combining the paths is
  additive in PnL, not just in frequency. The smoke (2025-04)
  suggests the ladder path is structurally unprofitable at
  canonical scale — but this is one file; the 6-month aggregate
  may differ on volatile months.
* The fee-vs-edge collapse documented in `AGENTS.md` 2026-09-26
  evening 1 still applies at this lot size. Both modes pay the
  same per-trade fee, so the **delta** between modes is fee-orthogonal
  (it's the question we're asking).
"""
import argparse
import json
import sys
import time
import warnings
from collections import Counter
from pathlib import Path

warnings.filterwarnings("ignore")

# ── Repo root setup ──────────────────────────────────────────────────
ROOT = None
for _c in [Path(".").resolve(), *Path(".").resolve().parents]:
    if (_c / "src" / "core" / "ict_signals.py").is_file():
        ROOT = _c
        break
if ROOT is None:
    raise RuntimeError("Could not find ICT repo root.")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "notebooks"))

import pandas as pd

from src.core.optimal_config import optimal_params, OPTIMAL_RECIPE_VERSION
from src.tick.cache import get_or_build
from src.tick.tick_backtest import run_tick_backtest
from src.core.run_report import append_run_report

NOTEBOOK = "nb56_dual_entry_top3"
SCENARIO = "dual_entry_vs_sniper_canonical_top3_sltp"

OUT_DIR = ROOT / "notebooks" / "nb56_dual_entry_top3_outputs"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Data path — same convention as nb53 / nb55.
SOURCE_DATA_ROOT = Path(r"C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw")
SAMPLE_6_JSON = ROOT / "notebooks" / "nb52_outputs" / "nb52_sample_6.json"
SAMPLE_6_NAMES = json.loads(SAMPLE_6_JSON.read_text()) if SAMPLE_6_JSON.exists() else None
if not SAMPLE_6_NAMES:
    SAMPLE_6_NAMES = [
        "BTCUSDT-aggTrades-2025-04.parquet",
        "BTCUSDT-aggTrades-2025-05.parquet",
        "BTCUSDT-aggTrades-2025-10.parquet",
        "BTCUSDT-aggTrades-2025-11.parquet",
        "BTCUSDT-aggTrades-2026-02.parquet",
        "BTCUSDT-aggTrades-2026-05.parquet",
    ]
SAMPLE_6 = [SOURCE_DATA_ROOT / name for name in SAMPLE_6_NAMES]


# ── Top 3 SL/TP combos from nb53_vBTC2_sweep_multimode ───────────────
TOP3_COMBOS = [
    ("B_n_5x60", 5.0, 60.0),
    ("B_n_5x50", 5.0, 50.0),
    ("B_n_7x45", 7.0, 45.0),
]


# ── Mode knobs ───────────────────────────────────────────────────────
def _sniper_canonical_overrides(sl_zone_mult: float, tp_zone_mult: float) -> dict:
    """Canonical sniper recipe (entry_mode='sniper', clean routing)."""
    return {
        "fvg_inv_trade_sl_zone_mult": sl_zone_mult,
        "fvg_inv_trade_tp_zone_mult": tp_zone_mult,
        "entry_mode": "sniper",
    }


def _dual_overrides(sl_zone_mult: float, tp_zone_mult: float) -> dict:
    """Dual entry mode — fires BOTH immediate ladder AND sniper.

    The two paths target different events on the same zone:
      * Immediate ladder: enters at next-bar open after mitigation,
        direction = zone's original polarity.
      * Sniper: waits for inversion, fires at next-bar open after
        inversion, direction = continuation (or fade, configurable).

    The bar loop populates both ``pending_layers`` (immediate) and
    ``pending_sniper_layers`` (sniper) for every FVG signal. The
    two queues produce disjoint trade sets on different events.
    """
    return {
        "fvg_inv_trade_sl_zone_mult": sl_zone_mult,
        "fvg_inv_trade_tp_zone_mult": tp_zone_mult,
        "entry_mode": "dual",
    }


# ── Per-trade extraction helper ─────────────────────────────────────
def _trade_row(t, scope_label: str, combo_label: str, mode_label: str,
               scenario: str) -> dict:
    return {
        "file": scope_label,
        "combo": combo_label,
        "mode": mode_label,
        "scenario": scenario,
        "trade_idx": -1,
        "entry_bar": int(getattr(t, "entry_bar", -1)),
        "exit_bar": int(getattr(t, "exit_bar", -1)),
        "direction": int(getattr(t, "direction", 0)),
        "entry_price": float(getattr(t, "entry_price", 0.0)),
        "exit_price": float(getattr(t, "exit_price", 0.0)),
        "stop_usd": float(getattr(t, "stop_usd", 0.0)),
        "target_usd": float(getattr(t, "target_usd", 0.0)),
        "exit_reason": str(getattr(t, "exit_reason", "")),
        "hold_secs": float(getattr(t, "hold_secs", 0.0)),
        "pnl_usd": float(getattr(t, "pnl_usd", 0.0)),
        "fee_usd": float(getattr(t, "fee_usd", 0.0)),
        "taker_bps_charged": float(getattr(t, "taker_bps_charged", 0.0)),
        "entry_triggered_by": str(getattr(t, "entry_triggered_by", "")),
        "qty_btc": float(getattr(t, "qty_btc", 0.0)),
        "net_pnl_usd": float(getattr(t, "pnl_usd", 0.0))
                       - float(getattr(t, "fee_usd", 0.0)),
    }


def main():
    p_arg = argparse.ArgumentParser()
    p_arg.add_argument("--fast", action="store_true",
                       help="Smoke run on 2025-04 only.")
    p_arg.add_argument("--no-write", action="store_true",
                       help="Skip writing the JSONL / CSV outputs.")
    args = p_arg.parse_args()

    scope = SAMPLE_6[:1] if args.fast else SAMPLE_6
    mode_fns = [
        ("sniper_canonical", _sniper_canonical_overrides),
        ("dual",             _dual_overrides),
    ]

    print(f"[{NOTEBOOK}] running {len(TOP3_COMBOS)} combos x "
          f"{len(mode_fns)} modes x {len(scope)} file(s) "
          f"= {len(TOP3_COMBOS) * len(mode_fns) * len(scope)} runs",
          flush=True)
    print(f"recipe version: {OPTIMAL_RECIPE_VERSION}\n", flush=True)

    per_trade_rows: list[dict] = []
    grand_t0 = time.perf_counter()

    for fi, path in enumerate(scope):
        if not path.exists():
            print(f"  WARN: {path} not found, skipping", flush=True)
            continue
        _parts = path.stem.rsplit("-", 2)
        month_label = f"{_parts[-2]}-{_parts[-1]}"

        # ── Build the shared SweepCache for this file ───────────────
        p_base = optimal_params(qty_btc=0.001)
        t0 = time.perf_counter()
        sc = get_or_build(path, p_base, with_side_table=True, verbose=False)
        t_build = time.perf_counter() - t0
        print(f"\n[{NOTEBOOK}] {month_label}: cache={t_build:.2f}s "
              f"fvg={len(sc.fvg_zones)} ifvg={len(sc.ifvg_zones)}",
              flush=True)

        for combo_label, sl_zm, tp_zm in TOP3_COMBOS:
            for mode_label, mode_fn in mode_fns:
                ov = mode_fn(sl_zm, tp_zm)
                p_i = optimal_params(qty_btc=0.001, **ov)
                t0 = time.perf_counter()
                res = run_tick_backtest(
                    raw_df=None, p=p_i,
                    strategy_label=f"{NOTEBOOK}_{combo_label}_{mode_label}",
                    pre_aggregated_bars=sc.bars,
                    precomputed_zones_by_src={"fvg": sc.fvg_zones,
                                              "ifvg": sc.ifvg_zones},
                    precomputed_structure=sc.structure,
                    precomputed_atr=sc.atr,
                    side_table=sc.side_table,
                )
                t_bt = time.perf_counter() - t0

                trades = list(res.trades)
                pnls = [t.pnl_usd for t in trades]
                fees = [getattr(t, "fee_usd", 0.0) for t in trades]
                nets = [p - f for p, f in zip(pnls, fees)]
                n = len(trades)
                n_wins = sum(1 for v in nets if v > 0)
                wr = (100.0 * n_wins / n) if n else 0.0
                ev = (sum(nets) / n) if n else 0.0
                paths = Counter(t.entry_triggered_by for t in trades)
                n_ifvg_clean = paths.get("ifvg_clean", 0)
                n_sniper_dirty = paths.get("sniper_dirty", 0)
                n_sniper_legacy = paths.get("sniper", 0)
                n_inv = paths.get("inv", 0)
                n_fvg = paths.get("fvg", 0)
                n_ifvg = paths.get("ifvg", 0)
                n_ladder = n_fvg + n_ifvg  # immediate-ladder trades

                # Mean fees and gross PnL for the ladder trades only.
                ladder_idx = [i for i, t in enumerate(trades)
                              if t.entry_triggered_by in ("fvg", "ifvg")]
                if ladder_idx:
                    ladder_nets = [nets[i] for i in ladder_idx]
                    ladder_n = len(ladder_idx)
                    ladder_wr = 100.0 * sum(1 for v in ladder_nets if v > 0) / ladder_n
                    ladder_ev = sum(ladder_nets) / ladder_n
                    ladder_net = sum(ladder_nets)
                else:
                    ladder_n = ladder_wr = ladder_ev = ladder_net = 0

                print(f"  [{month_label}] {combo_label:8s} {mode_label:19s} | "
                      f"n={n:3d} net=${sum(nets):+9.2f} gross=${sum(pnls):+9.2f} "
                      f"fees=${sum(fees):6.2f} WR={wr:5.1f}% EV=${ev:+.4f} "
                      f"| clean={n_ifvg_clean} dirty={n_sniper_dirty} "
                      f"ladder={n_ladder}(fvg={n_fvg},ifvg={n_ifvg}) "
                      f"| ladder_net=${ladder_net:+8.2f} ladder_WR={ladder_wr:5.1f}% ladder_EV=${ladder_ev:+.4f} "
                      f"| bt={t_bt:.1f}s",
                      flush=True)

                # Per-trade rows (carries entry_triggered_by).
                if not args.no_write:
                    for ti, t in enumerate(trades):
                        row = _trade_row(t, month_label, combo_label,
                                         mode_label, SCENARIO)
                        row["trade_idx"] = ti
                        per_trade_rows.append(row)

                # Run-log JSONL row.
                if not args.no_write:
                    metrics_extra = {
                        "n_ifvg_clean": n_ifvg_clean,
                        "n_sniper_dirty": n_sniper_dirty,
                        "n_sniper_legacy_tagged": n_sniper_legacy,
                        "n_inv_tagged": n_inv,
                        "n_fvg_tagged": n_fvg,
                        "n_ifvg_tagged": n_ifvg,
                        "n_ladder_total": n_ladder,
                        "ladder_net_usd": ladder_net,
                        "ladder_wr_pct": ladder_wr,
                        "ladder_ev_usd": ladder_ev,
                    }
                    append_run_report(
                        notebook=NOTEBOOK,
                        scenario=SCENARIO,
                        scope=[month_label],
                        engine="tick",
                        comments=(f"{combo_label} on {month_label}, "
                                  f"mode={mode_label}. "
                                  f"Overrides: {ov}. "
                                  "Measures the trade-frequency and "
                                  "PnL delta from running the immediate "
                                  "ladder path in tandem with the sniper "
                                  "path (entry_mode='dual')."),
                        hypothesis="Combining immediate ladder + sniper "
                                    "doubles trade frequency and adds the "
                                    "ladder PnL to the sniper PnL.",
                        verdict="inconclusive",
                        params=p_i,
                        result=res,
                        metrics_extra=metrics_extra,
                        overrides_vs_canonical=ov,
                        canonical_recipe_version=OPTIMAL_RECIPE_VERSION,
                        out_root=OUT_DIR.parent,
                        # Dedup on (combo, mode) so re-runs overwrite.
                        dedup_keys=("entry_mode",
                                    "fvg_inv_trade_sl_zone_mult",
                                    "fvg_inv_trade_tp_zone_mult"),
                    )

    elapsed = time.perf_counter() - grand_t0
    print(f"\n=== {NOTEBOOK} done. wall={elapsed:.1f}s ===", flush=True)

    # Per-trade CSV.
    if per_trade_rows and not args.no_write:
        df_trades = pd.DataFrame(per_trade_rows)
        csv_path = OUT_DIR / "nb56_dual_entry_top3_per_trade.csv"
        df_trades.to_csv(csv_path, index=False)
        print(f"  Wrote {csv_path}  ({len(df_trades)} rows)", flush=True)


if __name__ == "__main__":
    main()
