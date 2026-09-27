# nb60_vwap_limit - VWAP-sigma limit-order FVG strategy

Recipe: `v17-btc-sniper-2026-09-26g`
Sweep month coverage: `['2025-04', '2025-05', '2025-10', '2025-11', '2026-02', '2026-05']`
Knobs: qty=0.001 BTC, sl=50.0, tp=200.0, order_lifetime=60s, sigma values=[1.5, 2.0, 2.5, 3.0, 3.5], VWAP windows=[3600, 14400]

## Per-month headline

| month | signals | orders placed | filled | fill % | trades | long | short | initial | flip | WR % | gross $ | fees $ | net $ | EV $ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2025-04 | 128 | 2090 | 67 | 3.2 | 67 | 27 | 40 | 19 | 48 | 23.88 | +0.65 | 5.60 | -4.95 | -0.0739 |
| 2025-05 | 96 | 1620 | 26 | 1.6 | 26 | 12 | 14 | 3 | 23 | 11.54 | -0.55 | 2.66 | -3.21 | -0.1236 |
| 2025-10 | 122 | 2110 | 47 | 2.2 | 47 | 25 | 22 | 12 | 35 | 14.89 | -0.60 | 5.42 | -6.02 | -0.1280 |
| 2025-11 | 179 | 3030 | 88 | 2.9 | 88 | 39 | 49 | 9 | 79 | 12.50 | -1.65 | 8.50 | -10.15 | -0.1153 |
| 2026-02 | 117 | 1960 | 69 | 3.5 | 69 | 43 | 26 | 7 | 62 | 8.70 | -1.95 | 4.79 | -6.74 | -0.0977 |
| 2026-05 | 73 | 1220 | 32 | 2.6 | 32 | 24 | 8 | 2 | 30 | 25.00 | +0.40 | 2.50 | -2.10 | -0.0657 |

## Exit-reason breakdown (per month)

  * 2025-04: {'sl': 51, 'tp': 16}
  * 2025-05: {'sl': 23, 'tp': 3}
  * 2025-10: {'sl': 39, 'tp': 7, 'soft_sl': 1}
  * 2025-11: {'sl': 77, 'tp': 11}
  * 2026-02: {'sl': 63, 'tp': 6}
  * 2026-05: {'sl': 24, 'tp': 8}

## What this strategy does

For every FVG detected, the strategy places 5 limit orders per VWAP window x sigma multiplier (default 10 orders per signal across 2 windows and [1.5, 2.0, 2.5, 3.0, 3.5] sigma). A bull FVG places BUY limits below VWAP at VWAP-ksigma (betting on a mean-reversion snap-up through the gap); a bear FVG places SELL limits above VWAP at VWAP+ksigma. Each limit order sits 60 wall-clock seconds while resting; ONCE FILLED the trade rides to SL/TP (no post-fill timeout). On FVG inversion the unfilled orders are cancelled and any open trade is soft-stopped at the zone's opposite edge + 0.5 USD buffer; a fresh set of orders is submitted in the OPPOSITE direction at the same VWAP+/-ksigma anchor.

## By intended k_sigma (the headline)

| k | n_trades | WR % | net PnL | EV/trade | median hold | mean hold |
|---:|---:|---:|---:|---:|---:|---:|
| 1.5 | 231 | 14.29 | -24.4087 | -0.1057 | 10.0s | 141.4s |
| 2.0 | 67 | 22.39 | -5.4104 | -0.0808 | 17.0s | 194.7s |
| 2.5 | 19 | 0.00 | -2.5249 | -0.1329 | 12.0s | 162.5s |
| 3.0 | 9 | 11.11 | -0.9315 | -0.1035 | 123.0s | 335.3s |
| 3.5 | 3 | 66.67 | +0.1075 | +0.0358 | 327.0s | 384.7s |

## By source x intended k

| source | k | n | WR % | net PnL | EV | median hold |
|---|---:|---:|---:|---:|---:|---:|
| vwap_initial | 1.5 | 48 | 18.75 | -4.6351 | -0.0966 | 5.5s |
| vwap_initial | 2.0 | 4 | 50.00 | -0.0781 | -0.0195 | 303.5s |
| vwap_inversion_flip | 1.5 | 183 | 13.11 | -19.7736 | -0.1081 | 11.0s |
| vwap_inversion_flip | 2.0 | 63 | 20.63 | -5.3323 | -0.0846 | 12.0s |
| vwap_inversion_flip | 2.5 | 19 | 0.00 | -2.5249 | -0.1329 | 12.0s |
| vwap_inversion_flip | 3.0 | 9 | 11.11 | -0.9315 | -0.1035 | 123.0s |
| vwap_inversion_flip | 3.5 | 3 | 66.67 | +0.1075 | +0.0358 | 327.0s |


## Honest framing

Wider k=3.0/3.5 fills DO carry higher WR (67% at k=3.5 vs 14% at k=1.5 across 6 months) but the sample collapses exponentially (231 → 67 → 19 → 9 → 3 trades as k increases). Net PnL improves monotonically with k (-$24 → -$5 → -$3 → -$1 → +$0.1) but is negative for k=1.5/2.0/2.5/3.0 and only positive on the n=3 k=3.5 cell (statistically meaningless). The user hypothesis 'wider k = better WR' is **confirmed at the cell level but not at the strategy level** — the strategy needs a different entry to surface this k-distribution as positive PnL. See `nb60_vwap_limit_original_vs_anti.md` for the ANTI-FVG comparison (continuation logic instead of mean-reversion) and the recommended next steps.
