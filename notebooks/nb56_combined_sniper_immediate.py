"""NB56 — combined sniper + immediate-mode validation on the top 3 SL/TP combos.

Goal
====

The canonical recipe (`v17-btc-sniper-2026-09-26e`) already runs a
**per-zone routing** in the bar loop (see
`src/backtest/ict_backtest.py` lines 1269-1420):

* **Clean zone** (``mitigated_bar - trigger_bar >= fvg_min_mit_distance_bars``
  AND ``inverted_bar - trigger_bar >= fvg_min_inv_distance_bars``):
  iFVG immediate entry, ATR-anchored SL/TP. Tag = ``"ifvg_clean"``.
* **Dirty zone** (mit or inv within 3 bars of trigger): sniper
  deferred entry, zone-anchored SL/TP. Tag = ``"sniper_dirty"``.

The previous sweeps (`nb53_vBTC2_sweep_multimode`,
`nb55_rr_sweep`) ran the canonical recipe as-is but could not
isolate the per-path PnL split — the dirty path was silently
tagged ``"sniper"`` (same as the legacy-tagged entries).

NB56 measures the **delta** the clean routing adds, and the
**delta** the sniper path adds in isolation:

* **Combined mode** (canonical): both paths run; clean zones fire
  ``ifvg_clean`` immediate, dirty zones fire ``sniper_dirty``.
* **All-clean mode**: every zone is routed to the immediate path
  via the routing-only override knobs
  (``fvg_route_min_*_distance_bars=0``). Measures the clean-path
  PnL in isolation. The detector still applies its canonical
  filter (``fvg_min_*_distance_bars=3``), so the SweepCache
  fingerprint is unchanged.
* **All-dirty mode**: every zone is routed to the sniper path via
  the routing-only override knobs
  (``fvg_route_min_*_distance_bars=1_000_000``). Measures the
  sniper-path PnL in isolation. Equivalent to the legacy v17 b
  "sniper-only" behaviour.

The differences:

* combined − all_clean = sniper-path contribution when the clean
  gate would have routed the zone to immediate
* combined − all_dirty = clean-path contribution when the sniper
  path would have routed it instead

Top 3 SL/TP combos (from `nb53_vBTC2_sweep_multimode` 6-month
aggregate, sorted by total PnL):

| Label | SL mult | TP mult | 6mo PnL | All positive? |
|---|---:|---:|---:|:---:|
| B_n_5x60 | 5.0× zone | 60.0× zone | +$780.52 | yes |
| B_n_5x50 | 5.0× zone | 50.0× zone | +$724.23 | yes |
| B_n_7x45 | 7.0× zone | 45.0× zone | +$705.58 | yes |

Run modes
=========

```bash
# fast smoke (single 2026 file for cache hit, ~30s)
python notebooks/nb56_combined_sniper_immediate.py --fast

# 6-month full validation
python notebooks/nb56_combined_sniper_immediate.py
```

Output
======

`notebooks/nb56_outputs/`:
* `nb56_combined_sniper_immediate__runs.jsonl` — one record per
  (combo × mode × file). 36 records at full 6-month run.
* `nb56_combined_summary.csv` — cross-month aggregate per cell.
* `nb56_combined_per_trade.csv` — per-trade with the
  ``entry_triggered_by`` tag preserved so the clean / dirty /
  pure-sniper buckets can be split downstream.
* `nb56_combined_report.md` — short markdown verdict per cell.

Honest framing
==============

* This experiment is a **measurement**, not a config change. It
  does NOT propose a new canonical recipe.
* The user picked qty_btc=0.001 (the canonical default) — the
  fee-vs-edge collapse documented in `AGENTS.md` 2026-09-26
  evening 1 still applies at this lot size, so absolute PnL is
  expected to be small or negative. The headline is the **delta**
  combined-vs-pure-sniper, which is fee-orthogonal (both modes
  pay the same per-trade fee).
* The tag fix (`"sniper"` → `"sniper_dirty"` for the dirty path)
  is required for the per-bucket split to be observable. It is a
  labelling change only, no detector or backtest-loop behaviour
  is altered. Downstream consumers that filter on
  `entry_triggered_by == "sniper"` (`nb51_sniper_viz`,
  `nb52_opt_sweep_alpha`) now also need to include
  `"sniper_dirty"`. See `nb56_combined_report.md` for the list.
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

NOTEBOOK = "nb56_combined_sniper_immediate"
SCENARIO = "combined_vs_pure_sniper_top3_sltp"

OUT_DIR = ROOT / "notebooks" / "nb56_outputs"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Data path — same convention as nb53 / nb55.
SOURCE_DATA_ROOT = Path(r"C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw")
SAMPLE_6_JSON = ROOT / "notebooks" / "nb52_outputs" / "nb52_sample_6.json"
SAMPLE_6_NAMES = json.loads(SAMPLE_6_JSON.read_text()) if SAMPLE_6_JSON.exists() else None  # noqa: F821
# Fallback to the canonical 6-month scope if the JSON isn't present.
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
# Each tuple: (label, sl_zone_mult, tp_zone_mult, sl_mode, tp_mode)
# sl_mode / tp_mode are explicit for symmetry with the immediate path.
TOP3_COMBOS = [
    ("B_n_5x60", 5.0, 60.0),
    ("B_n_5x50", 5.0, 50.0),
    ("B_n_7x45", 7.0, 45.0),
]


# ── Mode knobs ───────────────────────────────────────────────────────
def _combined_overrides(sl_zone_mult: float, tp_zone_mult: float) -> dict:
    """Canonical routing: clean → ifvg_clean, dirty → sniper_dirty.

    Both the detector floor and the routing floor default to the
    canonical ``fvg_min_mit_distance_bars=fvg_min_inv_distance_bars=3``
    values. This is the production behaviour.
    """
    return {
        "fvg_inv_trade_sl_zone_mult": sl_zone_mult,
        "fvg_inv_trade_tp_zone_mult": tp_zone_mult,
    }


def _all_clean_overrides(sl_zone_mult: float, tp_zone_mult: float) -> dict:
    """All-clean routing: every zone routes to the immediate path.

    Uses the routing-only override knobs
    (``fvg_route_min_*_distance_bars``) set to 0 — the bar loop's
    routing gate becomes a no-op (``mit_dist >= 0`` is always True)
    so every zone takes the clean (immediate ATR-anchored) branch.
    The detector still drops zones whose mit/inv was <3 bars from
    trigger, so we keep the canonical detector floor.
    """
    return {
        "fvg_inv_trade_sl_zone_mult": sl_zone_mult,
        "fvg_inv_trade_tp_zone_mult": tp_zone_mult,
        "fvg_route_min_mit_distance_bars": 0,
        "fvg_route_min_inv_distance_bars": 0,
    }


def _all_dirty_overrides(sl_zone_mult: float, tp_zone_mult: float) -> dict:
    """All-dirty routing: every zone routes to the sniper path.

    Uses the routing-only override knobs set to 1_000_000 — the bar
    loop's routing gate always fails so every zone takes the dirty
    (deferred sniper, zone-anchored SL/TP) branch. Mirrors the
    legacy v17 b behaviour where every inverted zone went through
    the sniper path. Detector floor stays at canonical (3).
    """
    return {
        "fvg_inv_trade_sl_zone_mult": sl_zone_mult,
        "fvg_inv_trade_tp_zone_mult": tp_zone_mult,
        "fvg_route_min_mit_distance_bars": 1_000_000,
        "fvg_route_min_inv_distance_bars": 1_000_000,
    }


# ── Per-trade extraction helper ─────────────────────────────────────
def _trade_row(t, scope_label: str, combo_label: str, mode_label: str,
               scenario: str) -> dict:
    return {
        "file": scope_label,
        "combo": combo_label,
        "mode": mode_label,
        "scenario": scenario,
        "trade_idx": -1,  # filled by caller
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
        ("combined",   _combined_overrides),
        ("all_clean",  _all_clean_overrides),
        ("all_dirty",  _all_dirty_overrides),
    ]

    print(f"[{NOTEBOOK}] running {len(TOP3_COMBOS)} combos × "
          f"{len(mode_fns)} modes × {len(scope)} file(s) "
          f"= {len(TOP3_COMBOS) * len(mode_fns) * len(scope)} runs",
          flush=True)

    # SweepCache is keyed on detector-side params (zone mults are
    # NOT detector-side, they're bar-loop geometry), so a single
    # cache per file is shared across all cells.
    per_trade_rows: list[dict] = []
    grand_t0 = time.perf_counter()

    for fi, path in enumerate(scope):
        if not path.exists():
            print(f"  WARN: {path} not found, skipping", flush=True)
            continue
        month = path.stem.split("-")[-2:]  # e.g. ['2025','04']
        month_label = "-".join(month)

        # ── Build the shared SweepCache for this file ───────────────
        # Use the canonical mode selectors so the cache is shared
        # across all 6 cells (the override knobs are bar-loop only).
        p_base = optimal_params(qty_btc=0.001)
        t0 = time.perf_counter()
        sc = get_or_build(path, p_base, with_side_table=True, verbose=False)
        t_build = time.perf_counter() - t0
        # Filename stem is "BTCUSDT-aggTrades-YYYY-MM"; pull YYYY-MM
        # off the end for a stable month label that matches nb52's
        # convention (e.g. "2025-04").
        _parts = path.stem.rsplit("-", 2)
        month_label = f"{_parts[-2]}-{_parts[-1]}"
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

                print(f"  [{month_label}] {combo_label:8s} {mode_label:11s} | "
                      f"n={n:3d} net=${sum(nets):+9.2f} gross=${sum(pnls):+9.2f} "
                      f"fees=${sum(fees):6.2f} WR={wr:5.1f}% EV=${ev:+.4f} "
                      f"| clean={n_ifvg_clean} dirty={n_sniper_dirty} "
                      f"sniper={n_sniper_legacy} inv={n_inv} fvg={n_fvg} "
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
                    }
                    append_run_report(
                        notebook=NOTEBOOK,
                        scenario=SCENARIO,
                        scope=[month_label],
                        engine="tick",
                        comments=(f"{combo_label} on {month_label}, "
                                  f"mode={mode_label}. "
                                  f"Overrides: {ov}. "
                                  "Measures the per-path PnL split between "
                                  "the canonical combined routing and the "
                                  "isolated all_clean / all_dirty extremes."),
                        hypothesis="Combined sniper+immediate routing "
                                    "is dominated by the clean (immediate) "
                                    "path on this corpus; the dirty (sniper) "
                                    "path's contribution is structurally small.",
                        verdict="inconclusive",
                        params=p_i,
                        result=res,
                        metrics_extra=metrics_extra,
                        overrides_vs_canonical=ov,
                        canonical_recipe_version=OPTIMAL_RECIPE_VERSION,
                        out_root=OUT_DIR.parent,  # notebooks/  — run_report
                                                  # appends notebook name as suffix
                        # Dedup on (combo, mode) so re-runs overwrite.
                        dedup_keys=("fvg_inv_trade_sl_zone_mult",
                                    "fvg_inv_trade_tp_zone_mult",
                                    "fvg_route_min_mit_distance_bars",
                                    "fvg_route_min_inv_distance_bars"),
                    )

    elapsed = time.perf_counter() - grand_t0
    print(f"\n=== {NOTEBOOK} done. wall={elapsed:.1f}s ===", flush=True)

    # ── Per-trade CSV (always written — small) ─────────────────────
    if per_trade_rows and not args.no_write:
        df_trades = pd.DataFrame(per_trade_rows)
        csv_path = OUT_DIR / "nb56_combined_per_trade.csv"
        df_trades.to_csv(csv_path, index=False)
        print(f"  Wrote {csv_path}  ({len(df_trades)} rows)", flush=True)

    # ── Summary CSV (one row per combo × mode) ─────────────────────
    if not args.no_write:
        # run_report appends NOTEBOOK as the suffix, so the JSONL
        # lives at <ROOT>/notebooks/<NOTEBOOK>_outputs/<NOTEBOOK>__runs.jsonl.
        jsonl = OUT_DIR.parent / f"{NOTEBOOK}_outputs" / f"{NOTEBOOK}__runs.jsonl"
        if jsonl.exists():
            rows = []
            for line in jsonl.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    import json as _json
                    r = _json.loads(line)
                except Exception:
                    continue
                scope0 = (r.get("scope") or [""])[0]
                ov = r.get("overrides_vs_canonical") or {}
                # Extract the mode label from the comments prefix.
                comments = r.get("comments") or ""
                parts = comments.split(",")
                combo = parts[0].strip() if parts else ""
                mode = ""
                for tok in parts:
                    if tok.strip().startswith("mode="):
                        mode = tok.strip().split("=", 1)[1]
                m = r.get("metrics") or {}
                rows.append({
                    "file": scope0,
                    "combo": combo,
                    "mode": mode,
                    "sl_zone_mult": ov.get("fvg_inv_trade_sl_zone_mult"),
                    "tp_zone_mult": ov.get("fvg_inv_trade_tp_zone_mult"),
                    "min_mit_bars": ov.get("fvg_min_mit_distance_bars"),
                    "min_inv_bars": ov.get("fvg_min_inv_distance_bars"),
                    "n_trades": m.get("n_trades"),
                    "n_ifvg_clean": m.get("n_ifvg_clean"),
                    "n_sniper_dirty": m.get("n_sniper_dirty"),
                    "n_sniper_legacy": m.get("n_sniper_legacy_tagged"),
                    "n_inv": m.get("n_inv_tagged"),
                    "n_fvg": m.get("n_fvg_tagged"),
                    "win_rate_pct": m.get("win_rate_pct"),
                    "pnl_gross_usd": m.get("pnl_gross_usd"),
                    "fees_paid_usd": m.get("fees_paid_usd"),
                    "pnl_net_usd": m.get("pnl_net_usd"),
                    "ev_per_trade_usd": m.get("ev_per_trade_usd"),
                    "profit_factor": m.get("profit_factor"),
                    "max_drawdown_usd": m.get("max_drawdown_usd"),
                    "median_hold_secs": m.get("median_hold_secs"),
                })
            df_summary = pd.DataFrame(rows)
            # Cross-month aggregate per (combo, mode).
            agg = (df_summary.groupby(["combo", "mode"])
                              .agg(n_files=("file", "count"),
                                   sum_n_trades=("n_trades", "sum"),
                                   sum_n_ifvg_clean=("n_ifvg_clean", "sum"),
                                   sum_n_sniper_dirty=("n_sniper_dirty", "sum"),
                                   sum_n_sniper_legacy=("n_sniper_legacy", "sum"),
                                   total_pnl_gross_usd=("pnl_gross_usd", "sum"),
                                   total_fees_usd=("fees_paid_usd", "sum"),
                                   total_pnl_net_usd=("pnl_net_usd", "sum"),
                                   mean_wr_pct=("win_rate_pct", "mean"),
                                   median_hold_secs=("median_hold_secs", "median"))
                              .reset_index()
                              .sort_values(["combo", "mode"]))
            out_csv = OUT_DIR / "nb56_combined_summary.csv"
            agg.to_csv(out_csv, index=False)
            print(f"  Wrote {out_csv}", flush=True)

            # ── Headline print: per-mode delta vs combined ─────────
            print("\n" + "=" * 110)
            print("HEADLINE — combined vs all_clean vs all_dirty, per combo (6-month sum)")
            print("=" * 110)
            for combo in sorted({r["combo"] for r in rows}):
                combo_rows = [r for r in rows if r["combo"] == combo]
                combined = [r for r in combo_rows if r["mode"] == "combined"]
                clean = [r for r in combo_rows if r["mode"] == "all_clean"]
                dirty = [r for r in combo_rows if r["mode"] == "all_dirty"]
                if not combined:
                    continue
                net_combined = sum(r["pnl_net_usd"] or 0 for r in combined)
                n_combined_clean = sum(r["n_ifvg_clean"] or 0 for r in combined)
                n_combined_dirty = sum(r["n_sniper_dirty"] or 0 for r in combined)
                line = (f"  {combo:8s}: combined=${net_combined:+9.2f} "
                        f"(clean={n_combined_clean}, dirty={n_combined_dirty})")
                if clean:
                    net_clean = sum(r["pnl_net_usd"] or 0 for r in clean)
                    line += f"  | all_clean=${net_clean:+9.2f} (Δ vs combined=${net_clean-net_combined:+9.2f})"
                if dirty:
                    net_dirty = sum(r["pnl_net_usd"] or 0 for r in dirty)
                    line += f"  | all_dirty=${net_dirty:+9.2f} (Δ vs combined=${net_dirty-net_combined:+9.2f})"
                print(line, flush=True)
            print()


if __name__ == "__main__":
    main()
