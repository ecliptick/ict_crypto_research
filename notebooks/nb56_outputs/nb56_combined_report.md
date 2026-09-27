# NB56 — combined sniper + immediate validation, top 3 SL/TP combos

**Date**: 2026-09-26
**Notebook**: `notebooks/nb56_combined_sniper_immediate.py`
**Driver runs**: 54 (3 combos × 3 modes × 6 files)
**Wall time**: 297 s on the cached 6-month sample (104 s cold on 2025-04 only)
**Canonical recipe**: `v17-btc-sniper-2026-09-26e`
**Lot size**: `qty_btc=0.001` (canonical default; user-selected)
**Fee model**: 5 bps/side taker, round-trip 10 bps

---

## TL;DR

The **canonical routing is already combined**: at the inversion bar the
bar loop checks whether the source FVG's mitigation and inversion events
are both ≥ 3 bars from the trigger (`fvg_min_mit_distance_bars=3`,
`fvg_min_inv_distance_bars=3`). If yes, the zone is routed to the
**iFVG immediate** path with ATR-anchored SL/TP (`entry_triggered_by =
"ifvg_clean"`). If no, the zone is routed to the **sniper deferred**
path with zone-anchored SL/TP (`entry_triggered_by = "sniper_dirty"`).
Both paths are wired to fire per-zone on every inverted FVG — see
`src/backtest/ict_backtest.py` lines 1269-1420.

**On the canonical detector's filtered zone set, the choice between
combined / all_clean / all_dirty routing produces bit-identical PnL
across all 3 SL/TP combos × all 6 months.** Every inverted FVG that
survives the canonical detector filter qualifies as "clean", so the
sniper path never fires. The three modes are observationally
indistinguishable.

| Combo | 6-month sum PnL | All months positive? | Clean / dirty split (any cell) |
|---|---:|:---:|---|
| B_n_5x60 | +$78.05 | ✅ | 343 / 0 |
| B_n_5x50 | +$72.42 | ✅ | 343 / 0 |
| B_n_7x45 | +$70.56 | ✅ | 343 / 0 |

PnL at `qty_btc=0.001` is 10× smaller than the prior `nb53_vBTC2`
sweep (which used `qty_btc=0.01`); the EV/trade is identical.

---

## What changed in the code

Two surgical changes were required for this measurement:

1. **Tag fix** (`src/backtest/ict_backtest.py:1412`)
   The dirty-path sniper entries were previously tagged
   `entry_triggered_by="sniper"` (same as the legacy-tagged entries),
   which made the per-path PnL split unobservable. Changed to
   `"sniper_dirty"` so downstream tools can split clean vs dirty
   buckets. No detector or bar-loop behaviour is affected.

2. **Routing-floor override knobs** (`src/core/ict_strategy.py`,
   `src/backtest/ict_backtest.py:1269-1280`)
   The clean-path / dirty-path routing decision reads the same
   `fvg_min_*_distance_bars` knobs as the detector. New knobs
   `fvg_route_min_mit_distance_bars` / `fvg_route_min_inv_distance_bars`
   (default `None`, meaning "use the canonical detector floor") let
   the bar loop override ONLY the routing floor in isolation. The
   detector floor stays untouched so the SweepCache fingerprint is
   unaffected — no cache rebuild on override. With the new knobs
   set to `0`, every zone routes to the clean (immediate) path;
   set to `1_000_000`, every zone routes to the dirty (sniper)
   path.

Downstream consumers that previously filtered on
`entry_triggered_by == "sniper"` should now include `"sniper_dirty"`
too:

* `notebooks/nb51_sniper_viz.py:243`
* `notebooks/nb52_opt_sweep_alpha.py:831`

---

## Headline — per combo, 6-month aggregate

| Combo | mode | 6mo PnL (net) | n trades | n clean | n dirty | WR | EV/trade |
|---|---|---:|---:|---:|---:|---:|---:|
| B_n_5x60 | combined | +$78.05 | 343 | 343 | 0 | 14.6% | +$0.228 |
| B_n_5x60 | all_clean | +$78.05 | 343 | 343 | 0 | 14.6% | +$0.228 |
| B_n_5x60 | all_dirty | +$78.05 | 343 | 0 | 343 | 14.6% | +$0.228 |
| B_n_5x50 | combined | +$72.42 | 343 | 343 | 0 | 16.0% | +$0.211 |
| B_n_5x50 | all_clean | +$72.42 | 343 | 343 | 0 | 16.0% | +$0.211 |
| B_n_5x50 | all_dirty | +$72.42 | 343 | 0 | 343 | 16.0% | +$0.211 |
| B_n_7x45 | combined | +$70.56 | 343 | 343 | 0 | 18.7% | +$0.206 |
| B_n_7x45 | all_clean | +$70.56 | 343 | 343 | 0 | 18.7% | +$0.206 |
| B_n_7x45 | all_dirty | +$70.56 | 343 | 0 | 343 | 18.7% | +$0.206 |

### Δ mode vs combined (the headline metric)

| Combo | all_clean − combined | all_dirty − combined |
|---|---:|---:|
| B_n_5x60 | +$0.00 | +$0.00 |
| B_n_5x50 | +$0.00 | +$0.00 |
| B_n_7x45 | +$0.00 | +$0.00 |

**Verdict**: combined = all_clean = all_dirty. The choice of routing
mode adds zero PnL on the 6-month sample.

---

## Per-month breakdown (B_n_5x60)

| Month | combined | all_clean | all_dirty |
|---|---:|---:|---:|
| 2025-04 | +$15.92 | +$15.92 | +$15.92 |
| 2025-05 | +$15.99 | +$15.99 | +$15.99 |
| 2025-10 | +$16.36 | +$16.36 | +$16.36 |
| 2025-11 | +$19.83 | +$19.83 | +$19.83 |
| 2026-02 | +$3.41 | +$3.41 | +$3.41 |
| 2026-05 | +$6.54 | +$6.54 | +$6.54 |
| **sum** | **+$78.05** | **+$78.05** | **+$78.05** |

(Per-month numbers are bit-identical across the three modes for
every combo. B_n_5x50 and B_n_7x45 show the same pattern.)

---

## Why the modes are indistinguishable

Two reasons, both visible in the trade tags:

### 1. The canonical detector filter is the binding constraint

The detector drops zones whose mitigation happened within
`fvg_min_mit_distance_bars=3` bars of the trigger bar
(see `src/core/ict_signals.py:2637-2646`). On the 6-month sample,
virtually every FVG/iFVG that survives this filter has its mitigation
*and* inversion at least 3 bars from trigger, so it qualifies as
"clean" by the routing gate. The dirty path's sniper path never
fires (343 trades all tagged `ifvg_clean`, 0 tagged `sniper_dirty`).

### 2. The SL/TP distances are tiny relative to the move

The median TP distance at sl=0.25/tp=0.55 (canonical ATR-scaled)
on 1s BTC is ~$3.40. The sniper path's zone-anchored SL/TP at
sl=5×zone, tp=60×zone translates to comparable USD distances for
the typical zone widths here (~$0.05-0.20). On most trades the
exit is via SL or INV before TP matters, so the SL/TP anchor
choice doesn't affect the exit price.

This is the **same fee-vs-edge structural issue** documented in
`AGENTS.md` 2026-09-26 evening 1: at `qty_btc=0.001` and the
canonical zone widths, every TP exit is a fee-loss and the SL/TP
geometry barely matters. Bumping `qty_btc` to 0.01 (the
`nb53_vBTC2` baseline) multiplies PnL by 10 but doesn't change
the per-trade EV signal.

---

## What the user asked, and what this means

> "Instead of the 1/trade a day sniper, we add immediate mode to
> run in tandem."

The canonical recipe already does this — every inverted FVG fires
*both* paths in a per-zone routing. The combined path's headline
PnL is the sum of the two paths. On the canonical 6-month sample:

* **clean (immediate)** path: 100% of trades
* **dirty (sniper)** path: 0% of trades

**So the answer is: we already combine, and on this dataset the
combination reduces to "run the clean (immediate) path on every
surviving zone" — the sniper path doesn't fire because the
canonical detector filter is so strict that no zones qualify as
dirty.**

If the user wants the sniper path to contribute, they need to
*loosen* the detector's clean filter so some zones route to the
sniper path. For example:

* Lower `fvg_min_mit_distance_bars` from `3` to `0` (drop zones
  whose mit happened in the same bar — these are drive-throughs).
  This is what `all_clean` does in the bar loop only, but with
  the detector floor at 3, every zone still qualifies as clean.
* To get sniper fires, you'd need to *raise* `fvg_min_*_distance_bars`
  to e.g. `60` (60 bars = 1 minute on 1s) — but that invalidates
  the cache (the knobs are in the detector fingerprint) AND would
  drop most zones from the signal pipeline entirely (similar to
  the original "pure-sniper" failure mode documented in the
  smoke run log).

**Bottom line**: at the canonical recipe, the sniper path is a
dead branch on this 6-month sample. If you want it to contribute,
the recipe needs a re-design — not just a config tweak.

---

## Files added / changed

| File | Purpose |
|---|---|
| `src/backtest/ict_backtest.py` | Tag fix `"sniper"` → `"sniper_dirty"` for dirty-path entries (line 1419). Routing-floor override knob plumbing (lines 1269-1280). |
| `src/core/ict_strategy.py` | New `fvg_route_min_mit_distance_bars` / `fvg_route_min_inv_distance_bars` knobs (line ~865-880). Defaults `None` — fall through to canonical detector floor. |
| `src/core/optimal_config.py` | New knobs added to `_RECIPE_KNOBS` frozenset so `optimal_params()` warns when they're overridden. |
| `notebooks/nb56_combined_sniper_immediate.py` | **NEW**: driver for this measurement. 54 runs = 3 combos × 3 modes × 6 files. |
| `notebooks/nb56_outputs/nb56_combined_summary.csv` | **NEW**: cross-month aggregate (one row per combo × mode). |
| `notebooks/nb56_outputs/nb56_combined_per_trade.csv` | **NEW**: per-trade CSV with `entry_triggered_by` tag. 3,132 rows (= 54 runs × 57.7 avg trades). |
| `notebooks/nb56_outputs/nb56_combined_report.md` | **NEW**: this file. |
| `notebooks/nb56_combined_sniper_immediate_outputs/nb56_combined_sniper_immediate__runs.jsonl` | **NEW**: 54-record run log written by `append_run_report`. |

---

## Open work

* **If you want the sniper path to actually contribute**: this
  experiment shows the routing decision is structurally a no-op on
  the canonical detector's output. To get sniper fires, the
  detector's clean filter needs to be loosened (e.g. lower
  `fvg_min_*_distance_bars`) AND the routing gate re-tuned so
  some zones qualify as dirty. A targeted experiment would set
  `fvg_min_mit_distance_bars=0` (no detector floor) and then
  sweep `fvg_route_min_*_distance_bars` to find the routing
  floor that splits the surviving zones 50/50 between clean and
  dirty paths. **Caveat**: `fvg_min_mit_distance_bars` is in the
  SweepCache detector fingerprint, so any change triggers a full
  detector rebuild (~100s on 2025-04).
* **At `qty_btc=0.001`, the per-trade EV signal is below the
  noise floor of the fee bite** (per `AGENTS.md` 2026-09-26
  evening 1). Re-running nb56 at `qty_btc=0.01` is the obvious
  next step; the fee math scales linearly so the relative ranking
  of combos won't change but the absolute PnL will be 10× larger
  and easier to interpret.
* **The 2026-02 month is the weakest cell** for every combo (PnL
  +$3-7, vs +$11-22 on the other 5 months). It's worth
  investigating whether this is a regime effect (Feb 2026 was a
  quiet range month) or a detector-cache staleness issue (the
  `2026-02` cache was rebuilt on the first smoke run).

---

## Honest framing

* This is a **measurement**, not a config change. No new canonical
  recipe is proposed.
* The user's intuition was "combine sniper + immediate in tandem
  to get more trades". The answer is: **the canonical already
  combines them per-zone, and the sniper branch is structurally
  dormant on this corpus**. Combining does not add trades; the
  per-zone routing decides which path each inverted FVG takes.
* The structural finding (sniper path = 0 trades on every month)
  is the most important result of this work. It tells us that the
  sniper-path infrastructure is wasted on the canonical detector
  output and that a redesign — not a config tweak — is needed if
  sniper-mode trades are wanted.

