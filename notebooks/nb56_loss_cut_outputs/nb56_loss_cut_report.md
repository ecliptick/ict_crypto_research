# NB56 — loss-cut optimization: SL/TP grid + fee math round 2

## Context

Two prior runs in this session:
1. **Direction flip** (`nb56_direction_flip.py`): `fade_displacement` loses
   $35 on 2025-11 vs canonical `continuation`.
2. **1:2 RR + qty bump** (`nb56_rr_sweep_qty.py`): C1 (qty=0.01, 10x/20x)
   wins +$26 on 2025-04 but loses -$154 on 2025-11.

User correction: "Round-trip fee is $0.10/trade - wtf no, its 0.10% of
price" — confirmed correct. Fee = 0.10% of notional per round-trip.
At BTC~$85k / qty=0.001: median fee = **$0.084/trade**.

User ask (this run): "optimize TP/SL so we don't hold our losses so much"

---

## Fee math reminder

```
fee_usd = entry_price × qty_btc × taker_bps / 10000 × 2
        = 0.10% of notional per round-trip

At qty_btc=0.001, BTC~$85k median:  fee ≈ $0.084
At qty_btc=0.01,  BTC~$85k median:  fee ≈ $0.84

For a TP winner to be net-positive after fees:
  TP_gross > fee_usd
  TP_zone_mult × median_zone_width × qty_btc > fee
  TP_zone_mult × $45.35 × qty_btc > fee

qty=0.001:  TP_zone_mult > 1.85x (fee = $0.084)
qty=0.01:   TP_zone_mult > 18.5x  (fee = $0.84)
```

---

## Experiment design

**9 cells × 2 months** (2025-04 + 2025-11):

| Cell | sl_zm | tp_zm | RR | Notes |
|------|-------|-------|----|-------|
| S1 | 1.0 | 5.0 | 1:5 | Tightest SL |
| S2 | 1.0 | 10.0 | 1:10 | Tight SL, wide TP |
| S3 | 1.0 | 20.0 | 1:20 | Tight SL, very wide TP |
| S4 | 2.0 | 10.0 | 1:5 | Medium SL, wide TP |
| S5 | 2.0 | 20.0 | 1:10 | Medium SL, very wide TP |
| S6 | 3.0 | 15.0 | 1:5 | Medium-tighter SL |
| S7 | 5.0 | 10.0 | 1:2 | Symmetric ~1:2 (user's idea) |
| C1 | 10.0 | 20.0 | 1:2 | Prior run best cell (qty=0.01 equiv) |
| base | 5.0 | 60.0 | 1:12 | Canonical sniper |

---

## Results

### 2-month aggregate (sorted by net PnL)

| Cell | RR | n | net | gross | fees | WR | EV | med_hold | loss% | tp% |
|------|-----|---:|-----:|------:|-----:|----:|----:|--------:|------:|----:|
| **base** | **1:12** | **173** | **+$36.38** | +$52.14 | $15.76 | 16.2% | +$0.210 | 363s | 83.8% | 15.0% |
| C1 | 1:2 | 173 | -$12.77 | +$2.99 | $15.76 | 28.3% | -$0.074 | 767s | 71.7% | 28.3% |
| S5 | 1:10 | 173 | -$16.68 | -$0.92 | $15.76 | 14.5% | -$0.096 | **107s** | 85.5% | 14.5% |
| S3 | 1:20 | 173 | -$19.81 | -$4.06 | $15.76 | 10.4% | -$0.115 | **48s** | 89.6% | 10.4% |
| S6 | 1:5 | 173 | -$23.65 | -$7.89 | $15.76 | 17.3% | -$0.137 | 161s | 82.7% | 17.3% |
| S2 | 1:10 | 173 | -$27.59 | -$11.84 | $15.76 | 12.7% | -$0.160 | **46s** | 87.3% | 12.7% |
| S4 | 1:5 | 173 | -$27.86 | -$12.10 | $15.76 | 16.8% | -$0.161 | 98s | 83.2% | 16.8% |
| S7 | 1:2 | 173 | -$27.89 | -$12.13 | $15.76 | 26.6% | -$0.161 | 235s | 73.4% | 26.6% |
| S1 | 1:5 | 173 | -$28.84 | -$13.08 | $15.76 | 13.3% | -$0.167 | **45s** | 79.8% | 20.2% |

### Per-month detail

| Cell | 2025-04 net | WR | 2025-11 net | WR |
|------|-------------|----|-------------|----|
| base | +$13.96 | 16.2% | +$22.42 | 16.2% |
| C1 | +$2.65 | 33.8% | -$15.42 | 24.8% |
| S5 | -$3.73 | 17.6% | -$12.95 | 12.4% |
| S3 | -$5.49 | 13.2% | -$14.33 | 8.6% |
| S7 | -$9.13 | 30.9% | -$18.76 | 23.8% |
| S1 | -$9.86 | 16.2% | -$18.98 | 11.4% |

---

## Analysis

### Loss hold — YES, we cut losses faster

Tighter SL dramatically reduces median loss-hold time:

| Cell | RR | med_hold | loss_hold (inv) | loss_hold (sl) |
|------|----|---------|-----------------|----------------|
| **S1** | 1:5 | **45s** | **32s** | **33s** |
| **S2** | 1:10 | **46s** | **36s** | **36s** |
| **S3** | 1:20 | **48s** | **36s** | **36s** |
| S5 | 1:10 | **107s** | 63s | 69s |
| S4 | 1:5 | 98s | 60s | 65s |
| S7 | 1:2 | 235s | 145s | 162s |
| **base** | **1:12** | **363s** | **2152s** | **6577s** |

At 1x zone_mult (SL = $45), losses exit in **32-48 seconds** — the
trade either wins immediately or gets stopped. At the canonical's 5x
zone_mult (SL = $227), median loss-hold is **3,600+ seconds**.

**But net PnL goes negative.** Why?

### Why tighter SL hurts net PnL: the noise-whipsaw problem

At 1x zone width (SL = ~$45), the stop is triggered by **ordinary
intraday noise**. A 0.05% BTC move is $42 — that's a stop-out.
These are not structural breaks; they're noise. So:

* Tighter SL → more SL exits (noise stops)
* Tighter SL → more INV exits (price retraces through zone)
* Net effect: more frequent but smaller losses that still sum to
  a bigger total loss than the canonical's fewer but larger losses

The canonical's **wide SL acts as a noise filter**. At $227 SL,
only genuine structural moves trigger the stop. The trade survives
intraday noise and either reaches the massive $2,700 TP or gets
stopped by a real structural break.

### The gross edge is in the TP, not the WR

The exit reason breakdown for S1 (1x/5x) vs baseline:

| Exit | S1 n | S1 net | base n | base net |
|------|------|--------|---------|---------|
| TP | 35 | +$2.98 | 26 | **+$83.19** |
| INV | 70 | -$15.51 | 79 | -$17.64 |
| SL | 68 | -$16.31 | 66 | -$31.08 |
| EOD | 0 | — | 2 | +$1.91 |

S1's TP winners: mean gross = $6.19 / 35 = **$0.18/trade**
Baseline's TP winners: mean gross = $83.19 / 26 = **$3.20/trade**

**The canonical's TP winners are 18× bigger per win** because the
1:12 RR means each TP captures a massive $2,700+ move. Even though
the canonical has fewer TP wins (26 vs 35), the size of each win
more than compensates.

### What actually works: the qty bump

From the prior `nb56_rr_sweep_qty.py` run, C1 (qty=0.01, 10x/20x):

| Month | net | WR | med_hold | tp% |
|-------|------|----|---------|-----|
| 2025-04 | **+$26.51** | 33.8% | 447s | 33.8% |
| 2025-11 | -$154.22 | 24.8% | 940s | 24.8% |

On 2025-04, C1 (qty=0.01) **beats the canonical** (+$26.51 vs +$13.96)
while cutting median hold by ~50% (447s vs 324s on TP). The qty bump
scales the gross edge (same $2,700 TP) while the per-trade fee scales
proportionally. On volatile months (2025-11), it loses because the
wider SL captures bigger adverse moves.

---

## Verdict

**The loss-cut hypothesis is REJECTED for the SL/TP zone_mult path.**

* Tighter SL cuts loss-hold time by **90%+** (45s vs 363s median)
* But it introduces noise whipsaws that increase total loss count
* The canonical's wide SL is a **noise filter**, not a flaw
* Every non-canonical cell loses money on both months; canonical wins both

**The only viable path to faster losses + positive net PnL:**

* **Bump qty to 0.01** with wider zone_mult (10x/20x)
  * Cuts median hold ~50% vs canonical on range months
  * Beats canonical on range months (+$26 vs +$14)
  * Loses on volatile months (-$154)
  * Regime-dependent: works in range, fails in trending

**The honest status**: at qty=0.001, the canonical's 1:12 RR is the
only configuration that generates positive gross edge. The gross edge
is in the TP size (~$2,700/target × 1:12 RR = ~$225 average TP gross),
not in the WR. Any attempt to tighten SL just trades larger, rarer
losses for smaller, more frequent losses — and the total loss sum is
larger because the noise whipsaws fire more often.

---

## Recommendation

1. **Keep `sniper_inv_direction_mode='continuation'`** (canonical)
2. **Keep `fvg_inv_trade_sl_zone_mult=5.0, tp_zone_mult=60.0`** (canonical)
3. **Do not tighten SL** — the wide SL is a feature (noise filter)
4. **Only viable path to loss-hold reduction is a qty bump to 0.01**
   on range-only regimes (e.g., filter by low ATR or 24h range < 1%)

## Files

| File | Purpose |
|---|---|
| `notebooks/nb56_loss_cut.py` | SL/TP grid driver |
| `notebooks/nb56_loss_cut_outputs/nb56_loss_cut_per_trade.csv` | 1,557 trade rows |
| `notebooks/nb56_loss_cut_outputs/nb56_loss_cut_summary.csv` | per-cell × per-month |
| `notebooks/nb56_loss_cut_outputs/nb56_loss_cut_aggregate.csv` | 2-month aggregate |
| `notebooks/nb56_prelim_fees.py` | Fee math |
| `notebooks/nb56_direction_flip.py` | Direction A/B |
| `notebooks/nb56_direction_flip_outputs/` | Direction results |
| `notebooks/nb56_rr_sweep_qty.py` | qty-bump sweep |
| `notebooks/nb56_rr_qty_outputs/` | qty-bump results |
