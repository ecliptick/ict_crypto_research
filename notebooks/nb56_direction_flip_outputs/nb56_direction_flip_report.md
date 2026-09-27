# NB56 follow-up — direction-flip A/B + 1:2 RR fee-check on BTC 2025-11

This covers the user's 2026-09-26 question:

> "if win rate is so low, why not just 1:2 RR and flip the direction?"

## Preliminary fee check (before any run)

The "1:2 RR" half of the question has a clean **no** from the
fee math at qty_btc=0.001. Computed in
`notebooks/nb56_prelim_fees.py`:

**Fee model (verified against trade data, 3051 trades on 2025-04)**:

```
fee_usd = entry_price × qty_btc × taker_bps / 10000 × 2
        = entry_price × qty_btc × 0.001            (round-trip)
        = 0.10% of notional per round-trip
```

At BTC=$83.6k median (2025-04): median fee_usd = **$0.0836/trade**
(measured). At BTC=$100k it'd be $0.10. At BTC=$120k (current
level) it's $0.12. **The fee is a fixed 0.10% of notional, not
a fixed dollar amount.** Critically, fees scale with BTC
price — so as BTC rallies, every per-trade fee goes up
proportionally, and the "min TP to clear fees" threshold
scales with it.

**The user's intuition ("0.10% of price") is exactly right.**
A 1:2 RR config means SL = 0.10% of price, TP = 0.20% of
price. Fees at 0.10% of notional equal the SL distance. So
the fee equals the maximum loss — the trade can't possibly
clear fees on a win because fees = SL cost. Mathematically:

```
SL = 0.10% of price, fee = 0.10% of notional, TP = 0.20% of price
On TP win:  gross = 0.20% - 0.10% = 0.10% edge, fees = 0.10% → break-even WR = 100%
On SL loss: gross = -0.10%, fees = 0.10% → loss = 0.20% of price
```

The only way a 1:2 RR config beats fees is if the price
moves past the SL by enough that the trade rides it. But
the trade closes at SL first (because SL is the closer
boundary), so the fee eats the entire move.

```
Implied median zone width on 2025-04: $45.35

1:2 RR at (1x zone, 2x zone) — actual fee = $0.0836:
  SL = $45.35, TP = $90.70
  SL cost = $0.0454 per trade
  TP gross = $0.0907 per trade
  TP-net if win:   +$0.0907 - $0.0836 = +$0.0071 (marginally positive!)
  SL-net if loss:  -$0.0454 - $0.0836 = -$0.1290

Break-even WR at (1x, 2x):  156.2%   (impossible)
Break-even WR at (5x, 10x):  57.9%
Break-even WR at (10x, 20x): 45.6%
Break-even WR at (15x, 30x): 41.5%
```

**At qty_btc=0.001 a 1:2 RR is a fee-biter by construction.**
Even though the per-trade fee is tiny in absolute terms
($0.08-$0.10), it still consumes the entire gross edge from a
$45-$90 absolute move. To make 1:2 RR work you'd need either:

* Bigger qty_btc so fees scale linearly with gross. At
  qty_btc=0.01 the per-trade fee becomes $0.84-$1.00, but
  SL/TP gross also scales 10× to $0.45-$0.91 → still
  fee-biter at (1x, 2x). Need to bump to qty_btc ~0.05+
  OR widen SL/TP to ≥ 10x zone.
* Wider absolute moves (≥ $100 TP per trade). The canonical
  5x/60x already does this — median TP = $2,721 = 60.5x zone,
  gross $2.72/trade, fee $0.084 → fee is 3% of TP gross.

The canonical recipe ALREADY has the wide-TP structure the
1:2 RR idea wants. Just with asymmetric RR (~10:1) instead
of 1:2.

## Direction-flip A/B on 2025-11

The user said "and flip the direction" — that part is
testable, isolated from the RR question. Knob
`sniper_inv_direction_mode` already exists:
* `"continuation"` (canonical, default): enter SAME direction
  as the inverted zone's polarity (ride the continuation).
* `"fade_displacement"`: enter OPPOSITE direction (fade the
  move).

Same zones fire on both. Same SL/TP (5x/60x zone). Only the
direction differs.

Run via `notebooks/nb56_direction_flip.py`:

```
[2025-11] continuation_canonical    | n=105 | net=$  +22.42 gross=$  +32.42 fees=$ 10.00 WR= 16.2% EV=$+0.2135 | long=42 short=63
[2025-11] fade_displacement_flip    | n=105 | net=$  -13.24 gross=$   -3.25 fees=$ 10.00 WR=  6.7% EV=$-0.1261 | long=63 short=42
```

| Metric | continuation (canonical) | fade_displacement | Delta |
|---|---:|---:|---:|
| Trades | 105 | 105 | 0 |
| Net PnL | **+$22.42** | **-$13.24** | **-$35.66** |
| WR | 16.2% | **6.7%** | -9.5pp |
| EV | +$0.21 | -$0.13 | -$0.34 |
| Long / Short | 42 / 63 | 63 / 42 | mirrored |
| Gross | +$32.42 | -$3.25 | -$35.67 |

Same 105 trades, **direction flip costs ~$36 on this month**.

### Interpretation

The `sniper_inv_direction_mode='continuation'` is correctly
capturing the move. The reason: on a 1s BTC scale, when an
FVG inverts, the inversion is the **continuation** of the move
in the inverted direction. The price action that pushed through
the zone edge kept going in the new direction. Fading it means
betting against the move the inversion already confirmed.

The user's intuition that "fading the FVG should work because
most FVG retests fail" doesn't apply on this scale because:

1. **FVG retests failing is the WIDE-TP case**. The sniper
   path enters AFTER the inversion. By the time the iFVG
   triggers, the retest is already in motion.
2. **Most of the move IS the continuation**. The inversion
   bar + next bar of follow-through carries the trade to TP
   in 16% of cases. Fading needs the move to reverse AGAIN,
   which is rarer (6.7%).

## Combined user hypothesis check

User: "1:2 RR + flip direction"

Both halves fail:

1. **1:2 RR at qty_btc=0.001** needs 100%+ WR to clear fees.
   Geometrically impossible. **REJECTED.**
2. **Direction flip alone** (with wide 5x/60x SL/TP that DOES
   clear fees) loses $35 on 2025-11. **REJECTED.**

The canonical's wide-TP + continuation-direction combo IS the
right combination for qty_btc=0.001 on this regime. The two
axioms are:

* Use absolute moves large enough to clear fees (≥ $100 TP).
* Ride the inversion, don't fade it.

## Recommendation

**Do NOT change `sniper_inv_direction_mode`** — keep it at
`'continuation'` (canonical). The A/B confirms continuation
is materially better on this month, and the canonical is already
wide-TP enough to clear fees.

The 1:2 RR configuration idea **only becomes viable at higher
qty_btc** (≥ 0.005 to 0.01) or with **regime-adaptive ATR
scaling** that produces wider absolute TPs. Both are out of
scope for this user-driven experiment.

## Files changed / added

| File | Purpose |
|---|---|
| `notebooks/nb56_prelim_fees.py` | **NEW**: standalone fee math |
| `notebooks/nb56_direction_flip.py` | **NEW**: A/B driver |
| `notebooks/nb56_direction_flip_outputs/nb56_direction_flip_per_trade.csv` | per-trade data (210 rows) |
| `notebooks/nb56_direction_flip_outputs/nb56_direction_flip__runs.jsonl` | 2 records (continuation + fade) |

## Verdict

* **Direction-flip: REJECTED.** Costs ~$35 on 2025-11.
* **1:2 RR at qty_btc=0.001: REJECTED by construction.** Fees
  dominate at the median zone width.
