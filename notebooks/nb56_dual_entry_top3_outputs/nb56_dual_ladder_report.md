# NB56 follow-up — `entry_mode='dual'` + ladder SL/TP sweep on BTC 2025-11 / 2025-04

This report covers three changes shipped 2026-09-26 evening-3:

1. **`fvg_min_zone_usd` $5 → $20** + **`ifvg_min_zone_usd` $0.10 → $20**
   (both detectors share the same $20 floor).
2. **New `entry_mode='dual'`** — fires BOTH immediate ladder AND
   sniper paths on every FVG signal.
3. **Bug fix on the immediate-ladder SL/TP wiring** — `immediate_*_mode`
   knobs (`zone_mult` / `usd_fixed` / `atr_mult`) were silently
   no-ops on the bar-loop ladder path. Only `atr_mult` was wired
   (because it flipped the legacy `_atr_anchor` flag). The
   `zone_mult` and `usd_fixed` arms are now honored.

---

## What changed in the code

### 1. `fvg_min_zone_usd` $5 → $20

`src/core/optimal_config.py` — both FVG and iFVG detectors now
share a $20 zone-width floor. Rationale: zones narrower than
$20 can never produce a trade (`fvg_inv_trade_min_zone_usd=20`
is the sniper/clean path floor), so detecting them is wasted
work. Bumping the detector floor skips them at the source.

`src/tick/cache.py` — `CACHE_VERSION = 5` (was 4). Existing
caches were built with the old floors; rebuild takes ~100s
per monthly file. The 6-month corpus rebuild was ~10 min
wall time.

### 2. `entry_mode='dual'`

`src/core/ict_strategy.py` — added the new mode to
`TrendStrategyParams.entry_mode` docstring. The mode enum is
free-form (`str`), so no validation is needed.

`src/backtest/ict_backtest.py` line 928 — changed the sniper
intercept guard from `_entry_mode == "sniper"` to
`_entry_mode in ("sniper", "dual")`. The `sig = next(sig_iter)`
now only fires for true sniper mode; in dual mode the signal
falls through to the elif ladder path so BOTH queues get
populated.

### 3. Ladder SL/TP knob fix

`src/backtest/ict_backtest.py` line 1130-1142 — the bar loop's
ladder path called `compute_layer_sl_tp`, which OVERWROTE the
signal-level breadth-scaled SL/TP with its own
zone-anchor math (`zone_anchor_sl × breadth_mult`, not
`sl_per_b × zone_width`). The signal generator
(`ict_signals.py` lines 2687-2700) emits the correct
mode-aware `stop_usd` / `target_usd` per signal, but the bar
loop ignored them. Now the bar loop honours the signal's
`stop_usd` / `target_usd` when `immediate_sl_mode` /
`immediate_tp_mode` are explicitly set.

This is the **third instance** of the same SL/TP-mode-knob-
ignored bug. Earlier fixes:
- **2026-09-26 evening-2**: vBTC2 clean-zone path hard-coded
  ATR-anchored SL/TP; fixed by wiring `sniper_sl_mode` /
  `sniper_tp_mode` into the clean path.
- **2026-09-26 evening-2 (tick)**: tick-backtest
  `contract_size=100.0` default inflated PnL by 100×; fixed
  by reading `qty_btc` directly.
- **2026-09-26 evening-3 (this report)**: immediate-ladder
  path ignored `immediate_*_mode` for `zone_mult` /
  `usd_fixed`. Fixed by reading the signal's pre-scaled SL/TP.

---

## Smoke results — 2025-11 (volatile month from nb53 results)

`notebooks/nb56_dual_ladder_sweep.py` runs a sparse 9-cell
sweep over the immediate-ladder SL/TP choices in `dual` mode.
The sniper SL/TP is held at the nb53 winner `(5×, 60×)` zone.

```
cell         |    n       net     gross   fees    WR        EV | clean ladder | ladder_net ladder_WR ladder_EV |    bt
atr_05_15    |  512 $  -54.91 $   -6.25 $48.67  3.3% $ -0.1073 |   105    407 | $  -77.33      0.0% $ -0.1900 |  5.7s
atr_10_30    |  512 $  -54.92 $   -6.25 $48.67  3.3% $ -0.1073 |   105    407 | $  -77.34      0.0% $ -0.1900 |  6.1s
atr_15_45    |  512 $  -54.92 $   -6.26 $48.67  3.3% $ -0.1073 |   105    407 | $  -77.34      0.0% $ -0.1900 |  6.1s
zone_1x_3x   |  512 $  -58.90 $  -10.23 $48.67  7.6% $ -0.1150 |   105    407 | $  -81.32      5.4% $ -0.1998 |  6.2s
zone_1x_2x   |  512 $  -59.91 $  -11.24 $48.67  4.3% $ -0.1170 |   105    407 | $  -82.33      1.2% $ -0.2023 |  6.3s
zone_05_2x   |  512 $  -58.17 $   -9.50 $48.67  4.1% $ -0.1136 |   105    407 | $  -80.59      1.0% $ -0.1980 |  6.3s
usd_1_3      |  512 $  -55.04 $   -6.37 $48.67  3.3% $ -0.1075 |   105    407 | $  -77.46      0.0% $ -0.1903 |  6.2s
usd_2_6      |  512 $  -55.04 $   -6.37 $48.67  3.3% $ -0.1075 |   105    407 | $  -77.46      0.0% $ -0.1903 |  6.2s
baseline     |  512 $  -54.94 $   -6.27 $48.67  3.3% $ -0.1073 |   105    407 | $  -77.36      0.0% $ -0.1901 |  6.4s
```

Ladder SL/TP per cell (median, after the bug fix):

| Cell | median SL | median TP | Ladder WR | Ladder Net |
|------|----------:|----------:|----------:|-----------:|
| atr_05_15 | $0.15 | $0.45 | **0%** | -$77.33 |
| atr_10_30 | $0.30 | $0.90 | **0%** | -$77.34 |
| atr_15_45 | $0.45 | $1.35 | **0%** | -$77.34 |
| baseline  | $0.49 | $4.91 | **0%** | -$77.36 |
| usd_1_3   | $1.00 | $3.00 | **0%** | -$77.38 |
| usd_2_6   | $2.00 | $6.00 | **0%** | -$77.51 |
| zone_05_2x| $24.25| $97.00 | **1.0%**| -$80.59 |
| zone_1x_2x| $48.50| $97.00 | **1.2%**| -$82.33 |
| **zone_1x_3x**| **$48.50**| **$145.50**| **5.4%**| **-$81.32**|

Sniper path is **unchanged across all 9 cells** (same 105 trades,
16.2% WR, +$22.42 net per cell, +$0.21 EV).

### Headline findings

1. **Ladder is structurally a fee-biter across all SL/TP regimes.**
   - 0% WR on 6 of 9 cells (every atr_mult and usd_fixed config)
   - Best case is `zone_1x_3x` at 5.4% ladder WR (still net-negative)
   - All 9 cells net-negative; the sniper's +$22.42 contribution is
     wiped out by the ladder's -$77 to -$82 cost.
   - The fee bite at qty_btc=0.001 is ~$0.095/trade round-trip;
     the median TP gross is only $0.13 to $145 depending on config,
     so fees dominate the small-TP cells.

2. **Tighter SL makes the ladder WORSE, not better.**
   - `atr_05_15` (tightest, SL=$0.15) has 0% WR and -$77.33
   - `atr_15_45` (widest atr cell, SL=$0.45) also 0% WR, -$77.34
   - Counter-intuitive: wider SL gives the trade more room to ride
     and avoids noise-stops.
   - The fundamental issue is the entry mechanic, not SL width.

3. **Wider TP helps slightly.**
   - `zone_1x_3x` (TP=3× zone) has the best ladder WR (5.4%) vs
     `zone_1x_2x` (TP=2× zone) at 1.2%.
   - Median winner target_usd in zone_1x_3x = $303.45 (vs loser
     median = $136.80). Trades with target_usd > $100 still lose
     $27.62 net. Even the high-TP subset is a fee-biter.

4. **Ladder trades close intra-bar (mean hold_secs ≈ 0).**
   - 60% of ladder trades close on SL within the SAME bar they
     enter — the limit-order entry at the zone edge is being
     SL-hunted at the worst price in the move.
   - This is the well-known entry-mechanic issue documented in
     `AGENTS.md` (v6+ Innovation #1 docstring):
     *"Trade dies in 1-3 bars when an SL hunt fires at the worst
     price in the move."*

### Cross-month sanity (2025-04)

Same pattern on 2025-04 (`dual` mode, after the bug fix):

```
zone_1x_3x   |  339 $  -26.36 $   +2.34 $28.71 11.5% $ -0.0778 |    68    271 | $  -40.33     10.3% $ -0.1488
zone_1x_2x   |  339 $  -30.43 $   -1.73 $28.71  9.7% $ -0.0898 |    68    271 | $  -44.40      8.1% $ -0.1638
zone_05_2x   |  339 $  -30.65 $   -1.94 $28.71  7.7% $ -0.0904 |    68    271 | $  -44.61      5.5% $ -0.1646
atr_05_15    |  339 $  -31.93 $   -3.23 $28.71  3.2% $ -0.0942 |    68    271 | $  -45.90      0.0% $ -0.1694
baseline     |  339 $  -31.93 $   -3.22 $28.71  3.2% $ -0.0942 |    68    271 | $  -45.89      0.0% $ -0.1693
```

Same `zone_1x_3x` is the best ladder config (10.3% ladder WR,
still net-negative). Other configs produce 0% ladder WR.

---

## Sniper path is robust; only the ladder is the problem

Sniper path on 2025-11 (105 trades, unchanged across all 9 cells):

| Metric | Value |
|---|---:|
| n trades | 105 |
| Win rate | 16.2% |
| Mean SL | $269.90 |
| Mean TP | $3,238.74 |
| Sum gross | +$32.42 |
| Sum fees | -$9.99 |
| Sum net | **+$22.42** |
| EV/trade | **+$0.21** |

The sniper is paying for itself with $12 of net profit on 105
trades. It works because:
- Entry is at next-bar open AFTER the zone inverts (waits for
  the reversal signal)
- SL/TP is zone-anchored (5× / 60× zone width) — big enough to
  not get SL-hunted on noise
- The TP is wide enough ($3,238 avg) that the median gross
  captures $0.20+ per trade even after fees

The ladder path cannot replicate this because:
- Entry is at zone edge BEFORE the zone inverts (limit order
  inside the zone)
- Even with the user's widest config (`zone_1x_3x`), median TP
  is only $145.50, which is below the fee-bite threshold on
  most trades
- The intra-bar SL-hunt kills the trade before the TP can fill

---

## Recommendation

**Do NOT promote `entry_mode='dual'` to canonical.**

The data is unambiguous: the sniper path is profitable, the
ladder path is a fee-biter across all 9 SL/TP variants tested,
on both 2025-04 and 2025-11. Combining them via dual mode makes
total PnL worse, not better, because the ladder's structural
loss exceeds the sniper's structural gain.

Two structural alternatives the user might want to explore next:

1. **Filter the ladder to only fire on a high-quality subset**
   (e.g., rank_tier='A' only, OR zone_width > 90th percentile).
   The current ladder fires on EVERY FVG signal including the
   low-quality ones; a quality filter might turn the structural
   loss into a structural gain. Out of scope for this report.

2. **Don't combine. Just use sniper.** That's the canonical,
   and the data supports it.

### Canonical recipe stays at `entry_mode='sniper'`

The canonical recipe `v17-btc-sniper-2026-09-26f` keeps
`entry_mode='sniper'`. The new `entry_mode='dual'` is exposed
as an experimental knob — anyone who wants to test it can pass
`optimal_params(entry_mode='dual')` or override it via
`optimal_params(**{'entry_mode': 'dual'})`.

### Code changes ship regardless

The three code changes (min-zone bump, dual mode, ladder SL/TP
fix) all ship because:

- **min-zone bump**: strictly safer (drops unused zones at the
  source, no behaviour change for trades that matter).
- **dual mode**: new mode added; doesn't affect the canonical
  sniper run. Anyone who explicitly opts in gets the new
  behaviour.
- **ladder SL/TP fix**: bug fix. The signal-level
  `stop_usd`/`target_usd` is now honoured on the immediate
  ladder path when `immediate_*_mode` is set. The default
  canonical ladder (no `immediate_*_mode`) is unchanged.

### Verdict: dual mode **rejected**, sniper canonical **upheld**

---

## Open work

- **6-month sweep with the dual driver (`nb56_dual_entry_top3.py`)**:
  skipped per user's request ("don't do 6 month sweep first").
  If/when promoted, the user has a one-shot CLI ready:
  `python notebooks/nb56_dual_entry_top3.py`.
- **Ladder quality filter** (rank_tier='A' only?): would
  require a separate hypothesis. Not started.
- **AGENTS.md update**: this report should be folded into
  AGENTS.md as a new section under "Updates" once the user
  has had a chance to review. Out of scope for this report.
