# NB57 — conviction-aware SL/TP widening on the immediate ladder

**Status**: hypothesis **FALSIFIED** at the data level. Conviction scalar
(`sig.conviction`) is essentially never set on the surviving FVG/iFVG
signals — 504 of 512 (98.4%) on 2025-11 have conviction exactly 1.0
(neutral). The structural conviction widening has zero effect by
construction.

## Headline

| Metric | Result |
|---|---|
| Cells tested | 12 |
| File | 2025-11 (volatile month from nb53) |
| Wall time | 51.7s |
| Cache state | warm hit (nb56 cache reuse) |
| Best ladder cell | `floor_cv_1.2` (zeroes out the ladder entirely; only 105 sniper trades left, no ladder) |
| Best non-zero-ladder cell | `layers_1_only` (ladder_n=128, ladder_net=-$24.26, ladder_WR=0%) |
| Success bar met? | **NO** — no cell achieves ladder WR ≥ 12% AND ladder EV ≥ -$0.05 |

## Cell table

| Cell | SL widen | TP widen | min_cv | n_layers_max | n_trades | net | WR | ladder_n | ladder_net | ladder_WR | ladder_EV |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| baseline | 1.0 | 1.0 | 0.0 | 3 | 512 | -$54.94 | 3.3% | 407 | -$77.36 | 0.0% | -$0.190 |
| widen_only_sl_2x | 2.0 | 1.0 | 0.0 | 3 | 512 | -$54.94 | 3.3% | 407 | -$77.36 | 0.0% | -$0.190 |
| widen_only_sl_4x | 4.0 | 1.0 | 0.0 | 3 | 512 | -$54.94 | 3.3% | 407 | -$77.36 | 0.0% | -$0.190 |
| widen_only_tp_2x | 1.0 | 2.0 | 0.0 | 3 | 512 | -$54.94 | 3.3% | 407 | -$77.36 | 0.0% | -$0.190 |
| widen_both_2x | 2.0 | 2.0 | 0.0 | 3 | 512 | -$54.94 | 3.3% | 407 | -$77.36 | 0.0% | -$0.190 |
| widen_both_4x | 4.0 | 4.0 | 0.0 | 3 | 512 | -$54.94 | 3.3% | 407 | -$77.36 | 0.0% | -$0.190 |
| widen_sl_4x_tp_1x | 4.0 | 1.0 | 0.0 | 3 | 512 | -$54.94 | 3.3% | 407 | -$77.36 | 0.0% | -$0.190 |
| floor_cv_1.2 | 1.0 | 1.0 | 1.2 | 3 | 105 | +$22.42 | 16.2% | **0** | $0.00 | — | — |
| floor_cv_1.2_widen_2x | 2.0 | 2.0 | 1.2 | 3 | 105 | +$22.42 | 16.2% | **0** | $0.00 | — | — |
| layers_1_only | 1.0 | 1.0 | 0.0 | 1 | 233 | -$1.84 | 7.3% | 128 | -$24.26 | 0.0% | -$0.190 |
| layers_1_widen_2x | 2.0 | 2.0 | 0.0 | 1 | 233 | -$1.84 | 7.3% | 128 | -$24.26 | 0.0% | -$0.190 |
| layers_2_widen_2x | 2.0 | 2.0 | 0.0 | 2 | 376 | -$29.17 | 4.5% | 271 | -$51.59 | 0.0% | -$0.190 |

## Why widening is a no-op — manual conviction check

Computed `structure_conviction` manually against `sc.structure` for all
512 baseline trades:

| Conviction | Count | % of total | Net PnL | WR | EV |
|---:|---:|---:|---:|---:|---:|
| 0.50 | 4 | 0.8% | -$0.85 | 0.0% | -$0.213 |
| **1.00** | **504** | **98.4%** | **-$52.48** | **3.4%** | **-$0.104** |
| 1.25 | 3 | 0.6% | -$1.17 | 0.0% | -$0.388 |
| 1.50 | 1 | 0.2% | -$0.44 | 0.0% | -$0.439 |

**98.4% of signals have cv = 1.0 (neutral).** The remaining 1.6% are
non-neutral but **all losers** — high-conviction trades (cv > 1.0) have
0% WR. This means:

* Conviction widening is a no-op by construction: `(1.0 - 1.0) × X = 0`
  for every widening multiplier on 98.4% of signals.
* The high-conviction trades that DO fire are all losers — even if
  widening worked, it would be widening losers.
* This is consistent with the AGENTS.md "Update 2026-09-26 evening #2"
  finding that bucket analysis (post-hoc) showed `aligned-only` was
  WR 20.7%, but at submit time it lost money because the detector's
  filters already remove most counter-trend trades — what's left is
  mostly noise.

## What `layers_1_only` actually moved

Capping `num_layers_max=1` drops n_trades from 512 to 233 (cuts
`pending_layers` submissions roughly in half — the innermost two
layers of every signal are dropped before fill). Total net goes from
-$54.94 to -$1.84 — **a 96% reduction in the loss** — but still net
negative. The ladder path itself is unchanged (-$24 vs -$77 because
half as many trades; ladder WR still 0%, ladder EV still -$0.190).

This is **variance reduction, not alpha**. The strategy still loses;
it just loses more slowly with one layer. The `widen_both_2x` cell
on `layers_1_only` is bit-identical to `layers_1_only`, again because
cv is 1.0 for every signal.

## What `floor_cv_1.2` reveals

Setting `ladder_min_conviction=1.2` zeroes out **every** ladder
signal. The remaining 105 trades are all sniper-side (`ifvg_clean`,
`sniper_dirty`, or `inv`). Net PnL on this cell is +$22.42 with
16.2% WR — **exactly the sniper-only baseline** from the nb56 dual
entry top3 sweep. This is the cleanest confirmation that the
immediate ladder contributes nothing to PnL on this corpus, AND
that `sig.conviction` is dead in the water.

## Conclusion

* **Conviction-aware SL/TP widening does not work** because
  `sig.conviction` is essentially never set (98.4% of signals are
  cv = 1.0). The widening knobs (`ladder_conviction_sl_widen`,
  `ladder_conviction_tp_widen`) and the floor knob
  (`ladder_min_conviction`) are wired correctly but their input
  signal is too sparse to drive alpha.
* **The ladder itself is structurally unprofitable.** Even with
  layer-count reduction (the only knob that materially affects trade
  count), every cell with ladder_n > 0 has ladder_WR = 0% and
  ladder_EV ≈ -$0.190. The 1s-bar noise regime on BTC SL-hunts
  ladder fills reliably.
* **The structural conviction proxy needs to be replaced.** The
  `sig.conviction` scalar is sourced from `structure_conviction()`
  in `src/core/market_structure.py`, which only fires when there's
  a fresh BoS/CHoCH+ within 60 bars of the signal — and the FVG
  detector's filters (3-bar min-mit, 3-bar min-inv, strict-wick)
  leave very few signals firing into fresh structure events.

## What would actually move the needle (next round, NOT this plan)

1. **Custom conviction proxy**: build a per-signal conviction that
   uses different criteria — e.g. the **WIDTH of the FVG zone** in
   ATR units (wider zones = stronger displacement), or the
   **distance of the trigger bar from the recent swing**, or the
   **magnitude of the bar-2 displacement**. None of these are in
   the existing `structure_conviction` function.
2. **Try `ladder_min_conviction` with the FVG zone-width conviction**
   from H.6 of nb52 (zones >= $20 are already canonical; zones
   >= $80 are the rare extreme). If wide zones are correlated with
   the strong-displacement trades that the bucket analysis showed
   win, the conviction floor on zone-width might cut the noise.
3. **Or: bypass `sig.conviction` entirely.** Use a new
   `ladder_zone_width_min_usd` knob that drops ladder signals with
   zone_width < floor. With canonical fvg_min_zone_usd = $20, this
   would gate the ladder to zones >= $X. This is a structural
   filter, not a conviction filter — and it's the cheapest test
   for the "wide-zone trades are stronger" hypothesis from the
   bucket analysis.
4. **Or: stop trying to make the ladder profitable.** The ladder
   is a fee-biter at the structural level (1s-bar noise regime).
   The sniper alone is positive. `entry_mode='dual'` is genuinely
   not promotable with the current detector family.

## Files

| File | Purpose |
|---|---|
| [`src/core/ict_strategy.py`](src/core/ict_strategy.py) | 4 new knobs added (default identity) |
| [`src/backtest/ict_backtest.py`](src/backtest/ict_backtest.py) | Conviction-aware SL/TP widening in ladder layer build |
| [`notebooks/nb57_ladder_conviction.py`](notebooks/nb57_ladder_conviction.py) | Sweep driver (12 cells, single file) |
| `notebooks/nb57_ladder_conviction_outputs/nb57_ladder_conviction__runs.jsonl` | Run log (12 records) |
| `notebooks/nb57_ladder_conviction_outputs/nb57_ladder_conviction_per_trade.csv` | Per-trade output (4636 rows) |
| `notebooks/nb57_ladder_conviction_outputs/nb57_ladder_conviction_summary.csv` | Cell-level summary (12 rows) |
| `notebooks/nb57_ladder_conviction_outputs/nb57_ladder_conviction_report.md` | This file |

## Status

Hypothesis **falsified**. The new knobs (`ladder_conviction_*`) are
wired correctly but their input is structurally too sparse to matter.
**Do NOT promote** the conviction-aware widening knobs to canonical.
**Do NOT promote** `entry_mode='dual'` — it remains net-negative in
every cell tested here, consistent with prior sweeps.
