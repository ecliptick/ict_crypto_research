# nb60_vwap_limit — ORIGINAL vs ANTI comparison

Driver: `notebooks/nb60_vwap_limit.py` (added `--anti-fvg` flag on 2026-09-27).
Same backtest engine, same FVG signal list, same SL/TP / fee / qty. The only
change is the **direction the limit order is placed** relative to VWAP.

## Setup

- 6 monthly files: 2025-04, 2025-05, 2025-10, 2025-11, 2026-02, 2026-05
- qty_btc = 0.001, sl = $50, tp = $200, order_lifetime = 60s
- SIGMA_VALUES = (1.5, 2.0, 2.5, 3.0, 3.5)
- VWAP_WINDOWS = (3600, 14400)

## ORIGINAL mode (mean-reversion)

| FVG polarity | Order placed | Limit price | Bet |
|---|---|---|---|
| Bull (gap up) | BUY | VWAP - kσ | Price reverts down through VWAP |
| Bear (gap down) | SELL | VWAP + kσ | Price reverts up through VWAP |

## ANTI mode (continuation / anti-mean-reversion)

| FVG polarity | Order placed | Limit price | Bet |
|---|---|---|---|
| Bull (gap up) | SELL | VWAP + kσ | Price breaks up through VWAP, then rejects (short the top) |
| Bear (gap down) | BUY | VWAP - kσ | Price breaks down through VWAP, then snaps (long the bottom) |

The inversion-flip logic is the SAME in both modes (since "FVG inversion"
is a structural event, not a directional one).

## Cross-month headline (6 files)

| Metric | ORIGINAL | ANTI | Winner |
|---|---:|---:|:---:|
| Fills | 329 | 1097 | ANTI fills more (3.3×) |
| Win rate | 15.50% | 10.03% | **ORIGINAL** |
| Gross PnL | -$3.70 | -$27.35 | **ORIGINAL** |
| Fees paid | $29.47 | $99.46 | **ORIGINAL** (lower fee bleed) |
| Net PnL | -$33.17 | -$126.81 | **ORIGINAL** |
| EV/trade | -$0.1008 | -$0.1156 | **ORIGINAL** |

## By intended k_sigma

### ORIGINAL

| k | n | WR % | net PnL | EV | median hold |
|---:|---:|---:|---:|---:|---:|
| 1.5 | 231 | 14.29 | -$24.41 | -$0.1057 | 10s |
| 2.0 | 67 | 22.39 | -$5.41 | -$0.0808 | 17s |
| 2.5 | 19 | 0.00 | -$2.52 | -$0.1329 | 12s |
| 3.0 | 9 | 11.11 | -$0.93 | -$0.1035 | 123s |
| 3.5 | 3 | 66.67 | +$0.11 | +$0.0358 | 327s |

### ANTI

| k | n | WR % | net PnL | EV | median hold |
|---:|---:|---:|---:|---:|---:|
| 1.5 | 667 | 7.80 | -$81.44 | -$0.1221 | 0s |
| 2.0 | 300 | 15.00 | -$30.55 | -$0.1018 | 13s |
| 2.5 | 95 | 8.42 | -$11.11 | -$0.1170 | 15s |
| 3.0 | 27 | 11.11 | -$3.06 | -$0.1133 | 32s |
| 3.5 | 8 | 25.00 | -$0.64 | -$0.0804 | 36s |

## Why ANTI fails — fill-rate-vs-quality tradeoff

ANTI mode places orders **on the same side of VWAP as the FVG itself** —
i.e. where price is *already moving*. This makes them fill 3.3× more often
because price is more likely to reach the limit within 60s. But the
direction is *fighting* the FVG gap:

- Bull FVG fired (price gapped up) → we place a SELL above VWAP.
  We win if price keeps going up and then reverses; we lose if price
  reverts back down through VWAP first.
- On a 1s BTC timebase the "extends-and-reverses" pattern is rarer
  than the "snap-back-through-VWAP" pattern.

That asymmetry shows up cleanly in the **vwap_initial** source-x-k
breakdown:

### ORIGINAL: vwap_initial (k=1.5: 18.75% WR, k=2.0: 50% WR)

The ORIGINAL's initial BUY at VWAP-1.5σ catches genuine
mean-reversion dip-buy opportunities. The fill rate is low (3.2%) but
the WR is 18.75% on 48 fills — 5.5× the sample of k=3.5.

### ANTI: vwap_initial (k=1.5: 5.79% WR, k=2.0: 13.50% WR)

The ANTI's initial SELL at VWAP+1.5σ catches
trend-continuation reversals that don't materialise often enough.
Fill rate is high (8.9%) but the WR is 5.79% on 484 fills — most of
these are stop-hunts that hit SL within seconds (median hold = 0s).

## vwap_inversion_flip — same in both modes

The inversion-flip path is the SAME code in both modes (it places
opposite-direction orders at VWAP ± kσ on the inversion event). In
both runs the inversion_flip source produces **identical** numbers
(WR 13.11% at k=1.5, 20.63% at k=2.0). This is the structural
baseline — any difference between ORIGINAL and ANTI comes entirely
from the vwap_initial path.

## Verdict (2026-09-27)

**ANTI-FVG is not a viable reframing on BTC 1s.** The mean-reversion
setup (ORIGINAL) wins on every dimension despite filling 3.3× less
often. The wider-sigma trend (k=3.5 → 67% WR on 3 fills) holds in
both modes but is not enough to flip the sign.

**The strategy has zero gross edge at the current sizing.** Both
modes lose ~$3-27 gross across 6 months on $50 SL / $200 TP setups.
The fee-vs-edge collapse documented in AGENTS.md (2026-09-26) applies:
qty=0.001 BTC × $50 SL × 0.05% × 2 round-trip = ~$0.0005 SL-cost per
trade, but the typical $50 stop hit eats the full $50 distance at
WR < 20%. The fee bite (~0.085 per trade at BTC=$100k) is a rounding
error vs the $50 SL.

**What this tells us about the FVG detector + VWAP combo**:
- FVG zones do NOT generate a tradable VWAP-anchored signal.
- The mean-reversion setup is the *less bad* of the two framings.
- The wider-sigma fills (k=3.0, 3.5) carry higher WR but the
  sample collapses exponentially — a regime that's robust in
  theory but unreproducible in practice on a 6-month corpus.

## Recommended next step

1. **Drop the VWAP limit-order approach entirely** — the structural
   edge isn't there at the 1s timebase.
2. **Try a wider VWAP window** (4h, 24h) — currently testing 1h+4h;
   longer windows reduce sigma noise and produce more stable anchors.
3. **Try volume-gated FVG** — only fire the limit order if the FVG
   bar has above-median volume (real displacement vs noise).
4. **Try the post-inversion signal** — instead of placing orders
   *at* the FVG trigger, wait for the inversion and place orders
   only on inversion events (the inversion_flip source has 2× the
   WR of the initial path).

## Files

- `notebooks/nb60_vwap_limit.py` — driver (added `--anti-fvg` flag)
- `notebooks/nb60_vwap_limit_outputs/original/` — ORIGINAL outputs (329 trades)
- `notebooks/nb60_vwap_limit_outputs/anti/` — ANTI outputs (1097 trades)
- `notebooks/nb60_vwap_limit_outputs/nb60_vwap_limit_original_vs_anti.md` — this report
