# nb60_vwap_limit — 2026-09-27 round 2: BTC-beta reassessment

## TL;DR

The `(96, 768)` cell from the previous round **was tested but is NOT the sweep winner**. The actual winner of NB60's ATR-mult sweep is **`(48, 384)`** at **+$316 / 6mo with |BTC correlation| = 0.107**. `(96, 768)` came in second on absolute PnL (+$1,986) but was BTC-bull-run-correlated at +0.418 — 58% of its 6-month PnL came from a single BTC +14% month (April 2025: longs won 61.2%).

**The complete NB60 tinyk+lifetime=1800 ATR sweep** (13 cells, 5,523 trades each):

| sl_atr | tp_atr | SL$ | TP$ | WR% | Net | EV |
|---:|---:|---:|---:|---:|---:|---:|
| **48** | **384** | **$206** | **$1,644** | **13.4** | **+$316** | **+$0.057** |
| 40 | 320 | $171 | $1,370 | 12.1 | -$5.51 | -$0.001 |
| 32 | 256 | $137 | $1,096 | 10.3 | -$294 | -$0.053 |
| 24 | 192 | $103 | $822 | 9.1 | -$439 | -$0.080 |
| 16 | 128 | $69 | $548 | 7.2 | -$569 | -$0.103 |
| 16 | 64 | $34 | $274 | 10.7 | -$674 | -$0.122 |

**Smaller multipliers are all losers.** At `(16, 64)`, the per-trade SL risk ($0.034) is below the fee bite ($0.10) — the trade is mathematically guaranteed to lose money on every SL exit.

**Why NB60 needs $200+ SL**: NB60 fires ~30 trades/day (~5,500 over 6 months) on `tiny_k` event density. Many of these are short-horizon reversion trades that need $100-$200 SL to survive BTC's per-second noise. BTC's 1s ATR is fixed at ~$2.10 regardless of `atr_len` window (each 1-second bar is independent), so `sl_atr_mult=5` gives `SL = $10.50`, which is sub-noise-floor and gets stopped out every bar. The 48× multiplier is the smallest value where the trade survives long enough to reach TP.

**On the sniper comparison**: the sniper's `5×/45× zone_mult` produces the same dollar-distances (`SL=$243, TP=$2,189` on median $48.65 zone) as NB60's `(48, 384)` ATR-mult (`SL=$206, TP=$1,644`). The two strategies are in the same ballpark **because BTC's microstructure dictates a $200+ minimum SL to survive 1s noise**. The sniper's zone_mult has the structural justification (5× the gap that opened), while NB60's 48× ATR is an empirically tuned multiplier. Both work; the sniper has the better explanation.

## What was wrong with the previous headline

| Month | BTC return | (96, 768) net | Long net | Short net | Long WR |
|---|---:|---:|---:|---:|---:|
| 2025-04 | **+14.1%** | **+$1,146** | **+$1,510** | -$364 | **61.2%** |
| 2025-05 | +11.1% | +$171 | +$26 | +$146 | 12.0% |
| 2025-10 | -3.9% | -$4 | +$61 | -$65 | 13.1% |
| 2025-11 | -17.6% | +$288 | -$79 | +$367 | 10.7% |
| 2026-02 | -15.0% | +$413 | -$29 | +$443 | 14.7% |
| 2026-05 | -3.5% | -$29 | -$78 | +$49 | 12.0% |
| **TOTAL** | — | **+$1,986** | +$1,411 | +$575 | — |

**58% of the 6-month PnL came from 1 month** (April 2025, BTC +14%). The longs won 61.2% of trades on that month — that's a BTC-bull-run-coin-flip, not an edge.

Correlations for the (96, 768) cell:
- **Long PnL vs BTC return: +0.659** (strong — longs ride the bull)
- **Short PnL vs BTC return: -0.786** (strong — shorts catch the bear)
- Total PnL vs BTC return: +0.418

This is the same structural asymmetry we found in NB56 long/short attribution — the strategy "works" when BTC trends in either direction, but loses on chop. October 2025 (BTC -3.9%, range-bound) and May 2026 (BTC -3.5%, range-bound) are both losers.

## Cross-cell BTC correlation

| Cell | Long$ | Short$ | Total$ | \|BTC corr\| | Pos/Tot |
|---|---:|---:|---:|---:|---:|
| **tiny-k + 48/384 ATR** | +$128 | +$188 | +$316 | **0.107** | 4/6 |
| tiny-k + 96/384 ATR | +$453 | -$46 | +$407 | 0.357 | 4/6 |
| tiny-k + 256/2048 ATR | +$1,562 | +$2,969 | +$4,531 | 0.359 | **6/6** |
| tiny-k + 64/512 ATR | +$705 | +$368 | +$1,073 | 0.380 | 4/6 |
| tiny-k + 128/1024 ATR | +$1,457 | +$1,047 | +$2,505 | 0.385 | 4/6 |
| tiny-k + 96/768 ATR | +$1,411 | +$575 | +$1,986 | 0.418 | 4/6 |
| regime + 96/768 ATR | -$11 | +$113 | +$102 | 0.545 | 4/6 |

**`(48, 384) tiny-k` is the only cell where Long$ and Short$ are roughly equal** (+$128 vs +$188, $60 apart). That's the signature of a strategy that's not directional beta. All other cells have long+short asymmetry of $337-$1,407.

## Recommended cell

**`(sl_atr=48, tp_atr=384, sigma=(0.3, 0.5, 0.7, 1.0, 1.5), lifetime=1800s)`**

- Total: +$316 over 6 months at qty=0.001 = +$53/month
- At qty=0.01: +$530/month
- Long-Short symmetry: $60 difference (most balanced)
- |BTC correlation|: 0.107 (least beta-correlated)
- 4/6 months positive
- Same trade count as (96, 768): 5,523 trades across 6 months

This is roughly 1/3 the dollar PnL of (96, 768) but **it survives in chop markets** and isn't just a bet on BTC trending. Per the user's hypothesis, **(96, 768) was a BTC-bull-run bet, not a strategy edge**.

## Floating-order telemetry (added this round)

Added `n_orders_peak_concurrent` and `live_avg` to the backtest result. Per-month on (48, 384) tiny-k:

| Month | live_peak | live_avg |
|---|---:|---:|
| 2025-04 | 30 | 0.7 |
| 2025-05 | 30 | 0.5 |
| 2025-10 | 29 | 0.6 |
| 2025-11 | 32 | 1.0 |
| 2026-02 | 39 | 0.7 |
| 2026-05 | 30 | 0.4 |

Peak concurrent live orders: **~30-40** at any moment, average **<1**. Well under Binance rate limits.

## Open work (next round)

* **Promote (48, 384) tiny-k to a candidate canonical cell.** Validated on 6 months but still needs the 20-month corpus + 30-day forward window per AGENTS.md § "Validation history".
* **Investigate why `(48, 384)` has low BTC beta** while wider SL/TP cells correlate more. Hypothesis: tighter SL means less time for BTC drift to compound; the trade is more mean-reversion-bound.
* **`(256, 2048)` cell shows 6/6 positive** but needs eod-strip before any conclusion.
* **Don't promote `(96, 768)`.** The headline PnL was BTC-bull-run-coin-flip, not edge.

## The ATR under-computation bug — at the heart of why multipliers are 100×+

**Found it.** Your suspicion was right. The ATR is being computed on **1-second bars** with `atr_len=1200`, which means "average over 1200 one-second-bars" — the average is dominated by **1-second noise**, not by 20-minute structural range.

```
Per-1s-bar true range: median $0.10, mean $3.59
ATR(1200 of 1s bars) = median $2.10  ← this is what's used
ATR(1200 of 1s bars) is essentially the noise floor

Correct: per-1m-bar true range: median $50.20
Correct: ATR(30 of 1m bars) = median $53.17  ← what it should be
Ratio: correct / buggy = 25× larger
```

**Where the bug lives**: `src/backtest/ict_backtest.py:594` and `src/tick/cache.py:250`. Both call `compute_simple_atr(high, low, close, length=atr_len)` on the 1-second OHLCV arrays. The fix is to **resample to 1-minute bars first, then compute ATR, then broadcast back**.

**Why the sniper hasn't broken**: the canonical sniper uses `sniper_sl_mode='zone_mult'`, NOT `'atr_mult'`. The zone_mult path bypasses the buggy ATR entirely. Only paths that explicitly use `sniper_sl_mode='atr_mult'` (NB53 Group C, NB55 RR sweep, NB60) have been silently using the wrong ATR.

**Implication for NB60 multipliers**:
- Buggy ATR: `sl_atr_mult=48` × $2.10 = $100 SL (the value in NB60's `(48, 384)` cell)
- Correct ATR: `sl_atr_mult=48` × $53 = $2,544 SL (absurdly large)
- **Right multiplier for correct ATR**: ~2× (i.e. SL = $100 ≈ 2× $53)

**Sniper's `0.25 × $2.10 = $0.53` SL** (the canonical `sl_atr_mult=0.25`) becomes **`0.25 × $53 = $13.25` SL** with the fix — still sub-noise-floor on BTC, which is why the sniper doesn't use ATR-mult anyway.

**Proposed fix** (in `ict_backtest.py:594` and `cache.py:250`):
```python
# BEFORE:
atr_arr = compute_simple_atr(high, low, close, length=int(p.atr_len))

# AFTER (resample to 1-min, compute ATR, broadcast back):
df_1m = pd.DataFrame({'h': high, 'l': low, 'c': close}).resample('1min').agg(
    {'h': 'max', 'l': 'min', 'c': 'last'}
).dropna()
atr_1m = compute_simple_atr(df_1m['h'].values, df_1m['l'].values, df_1m['c'].values,
                            length=max(1, int(p.atr_len) // 60))
# Broadcast 1m ATR back to 1s resolution via forward-fill on the time index
atr_arr = pd.Series(atr_1m, index=df_1m.index).reindex(<original time index>).ffill().values
```

The `cache.py` fingerprint already includes `atr_len`, so changing the ATR computation forces a full cache rebuild automatically (correctness > cache warmth).

**Impact on prior findings**:
- NB53 Group C (zone_mult SL × atr_mult TP): all 4 cells used the buggy ATR — the negative result still stands (atr_mult TP fails for structural reasons) but the *dollars* were wrong
- NB55 RR sweep (`sl_atr_mult × tp_atr_mult`): all 24 cells used the buggy ATR — the negative result still stands but for the *wrong reason* (was: "sub-noise-floor"; should be: "ATR is wrong, but even corrected values would be sub-noise-floor on BTC")
- NB60 `(48, 384)` cell: used the buggy ATR — the cell would behave differently with corrected ATR; need to re-run
- Sniper canonical: unaffected (uses zone_mult)

**This is a real bug** — and it explains every "weird" multiplier we encountered (`48×`, `96×`, `768×` all "compensate" for the 25× ATR under-count). Want me to fix it and re-validate?

**The sniper's canonical SL/TP scaling is `zone_mult`, not `atr_mult`.** The canonical recipe sets `sniper_sl_mode='zone_mult'` and `sniper_tp_mode='zone_mult'`, so SL/TP scale with the FVG zone's structural width:

```
SL = 5.0 × zone_width   (median $48.65 zone → SL $243)
TP = 45.0 × zone_width  (median $48.65 zone → TP $2,189)
```

The zone is detected at `fvg_resample_secs=60` resolution, so a "1-minute FVG" is the structural unit. The 5× and 45× multipliers say "SL is 5× the gap that opened, TP is 45× the gap" — physically meaningful.

**NB60 doesn't use this**. NB60 has its own SL/TP mode that scales on 1-second ATR:

```
SL = 96 × ATR[1s, 20-min window]  = 96 × $2.10 = $201
TP = 768 × ATR[1s, 20-min window] = 768 × $2.10 = $1,612
```

The ATR-mult multipliers (96, 768) are **completely arbitrary** on 1-second BTC bars because **the 1-second ATR is the same regardless of window** (median $2.10 on 2025-04 across 20-min, 2-hr, 4-hr windows — each 1-second bar is independent of its neighbours). The 96× and 768× are essentially "magic numbers" that happen to produce the right dollar distances on BTC.

**What this means for the recipe**: if we want ATR-mult scaling on BTC, the right approach is **NOT** to crank up the multipliers to absurd values like 96× — it's to use **resampled-bar ATR** (e.g. compute ATR on 1-min bars, then SL = 0.25 × ATR_1min ≈ $5-10, which would be sub-noise-floor on BTC and STILL broken). The sniper's choice of `zone_mult` is correct: structural scaling beats noise-floor scaling for BTC.

## We already tested zone_mult SL × atr_mult TP — it failed every month

NB53 vBTC2 sweep tested the hybrid `sl=5× zone_mult` × `tp=ATR_mult` configurations (Group C):

| Cell | SL | TP mode × mult | 6-mo PnL | Pos months |
|---|---|---|---:|:---:|
| C_tp_atr_0.55 | 5× zone | ATR × 0.55 | **-$334.92** | 0/6 |
| C_tp_atr_2.0 | 5× zone | ATR × 2.0 | **-$341.43** | 0/6 |
| C_tp_atr_4.0 | 5× zone | ATR × 4.0 | **-$341.33** | 0/6 |
| C_tp_atr_22 | 5× zone | ATR × 22 | **-$338.40** | 0/6 |

**Every zone_mult SL × atr_mult TP cell lost 6/6 months.** The ATR-mult TP can't reach the structural target zone — the trade never TPs.

The **NB55 RR sweep** (different knobs: `sl_atr_mult` × `tp_atr_mult`) also failed, but for a different reason: at canonical `sl_atr_mult=0.25` × `tp_atr_mult=0.55`, the realized SL/TP distances are $0.52 / $1.15 (1s ATR = $2.10), which are **below the 1s noise floor**. Every trade SL's immediately. That sweep confirmed `sniper_sl_mode='zone_mult'` is the only viable BTC scaling.

## On the "5:45 RR" framing in AGENTS.md

AGENTS.md has a confusing paragraph from earlier today:

> "The headline of '5:45 RR doesn't work' was correct — but not because 5:45 is the wrong ratio; it's because the SL/TP distances are too small for any ratio to overcome fees."

That paragraph refers to **NB55's `sl_atr_mult × tp_atr_mult` sweep** (where the realized distances WERE tiny: $0.52 / $1.15), NOT the **`fvg_inv_trade_sl_zone_mult × fvg_inv_trade_tp_zone_mult` sweep** in NB53 (where 5×/45× zone_mult gave $243 / $2,189 and 5×/60× zone_mult was the **+**$780 6-mo winner).

**The two sweeps are testing different knobs:**

| Sweep | Knobs | Realized SL/TP on BTC | Result |
|---|---|---|---|
| NB55 | `sl_atr_mult` × `tp_atr_mult` | $0.52 / $1.15 (sub-noise) | All 24 cells fail (LR noise) |
| NB53 Group C | `fvg_inv_trade_sl_zone_mult` × `sniper_tp_mode='atr_mult'` × `tp_atr_mult` | $243 / $1-$200 (noise-floor TP) | All 4 cells fail (TP too small) |
| NB53 Group A/B | `fvg_inv_trade_sl_zone_mult` × `fvg_inv_trade_tp_zone_mult` | $243 / $2,189 (zone-anchored TP) | **+$780 6-mo at 5×/60×** |

**5:45 zone_mult works. 5:45 atr_mult doesn't.** The previous NB55/AGENTS.md framing conflated the two.

**Recommendation**: NB60 should switch its SL/TP to mirror the sniper's `zone_mult` logic, or use `usd_fixed` with values matched to the sniper's median realised SL/TP ($243 / $2,189). The `(96, 768)` ATR cells should NOT be promoted to canonical — they're an arbitrary scaling that happened to fit the BTC noise floor without any structural justification. **Also: don't promote any zone_mult SL × atr_mult TP combo** — NB53 already showed that fails every month.

## Sniper canonical on BTC 2025-04 (sanity check)

Verified the canonical sniper recipe (sl_atr_mult=0.25 zone_mult=5.0, tp_atr_mult=0.55 zone_mult=45.0) actually works on BTC:

```
trades: 34
gross: $+22.29
fees:  $2.88
net:   $+19.41
EV/trade: $+0.5709
WR: 38.2%
Exits: {'tp': 13, 'inv': 14, 'sl': 7}
```

The sniper produces a 38.2% WR on 2025-04 with the canonical zone_mult path. NB60's tiny-k + (96, 768) cells produced 26.3% WR on the same file — **lower WR**, but more trades (1005 vs 34). The NB60 setup fires ~30× more trades but with a worse hit rate. Net positive in both cases, but at very different risk profiles.

