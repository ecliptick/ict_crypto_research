# nb60_vwap_limit - VWAP-sigma limit-order FVG strategy

Recipe: `v17-btc-sniper-2026-09-26g`
Sweep month coverage: `['2025-04', '2025-05', '2025-10', '2025-11', '2026-02', '2026-05']`
Knobs: qty=0.001 BTC, sl=50.0, tp=200.0, order_lifetime=60s, sigma values=[1.5, 2.0, 2.5, 3.0, 3.5], VWAP windows=[3600, 14400]

## Per-month headline

| month | signals | orders placed | filled | fill % | trades | long | short | initial | flip | WR % | gross $ | fees $ | net $ | EV $ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2025-04 | 128 | 2090 | 187 | 8.9 | 187 | 82 | 105 | 139 | 48 | 11.23 | -4.10 | 15.76 | -19.86 | -0.1062 |
| 2025-05 | 96 | 1620 | 134 | 8.3 | 134 | 71 | 63 | 111 | 23 | 11.94 | -2.70 | 13.80 | -16.50 | -0.1232 |
| 2025-10 | 122 | 2110 | 150 | 7.1 | 150 | 91 | 59 | 115 | 35 | 7.33 | -4.75 | 17.32 | -22.07 | -0.1471 |
| 2025-11 | 179 | 3030 | 307 | 10.1 | 307 | 162 | 145 | 228 | 79 | 9.45 | -8.10 | 29.30 | -37.40 | -0.1218 |
| 2026-02 | 117 | 1960 | 212 | 10.8 | 212 | 129 | 83 | 150 | 62 | 8.49 | -6.10 | 14.94 | -21.04 | -0.0992 |
| 2026-05 | 73 | 1220 | 107 | 8.8 | 107 | 62 | 45 | 77 | 30 | 14.02 | -1.60 | 8.33 | -9.93 | -0.0928 |

## Exit-reason breakdown (per month)

  * 2025-04: {'sl': 161, 'tp': 21, 'soft_sl': 5}
  * 2025-05: {'sl': 115, 'tp': 16, 'soft_sl': 3}
  * 2025-10: {'sl': 136, 'tp': 11, 'soft_sl': 3}
  * 2025-11: {'sl': 275, 'tp': 29, 'soft_sl': 3}
  * 2026-02: {'sl': 188, 'tp': 18, 'soft_sl': 6}
  * 2026-05: {'tp': 15, 'sl': 87, 'soft_sl': 5}

## What this strategy does

For every FVG detected, the strategy places 5 limit orders per VWAP window x sigma multiplier (default 10 orders per signal across 2 windows and [1.5, 2.0, 2.5, 3.0, 3.5] sigma). A bull FVG places BUY limits below VWAP at VWAP-ksigma (betting on a mean-reversion snap-up through the gap); a bear FVG places SELL limits above VWAP at VWAP+ksigma. Each limit order sits 60 wall-clock seconds while resting; ONCE FILLED the trade rides to SL/TP (no post-fill timeout). On FVG inversion the unfilled orders are cancelled and any open trade is soft-stopped at the zone's opposite edge + 0.5 USD buffer; a fresh set of orders is submitted in the OPPOSITE direction at the same VWAP+/-ksigma anchor.

## By intended k_sigma (the headline)

| k | n_trades | WR % | net PnL | EV/trade | median hold | mean hold |
|---:|---:|---:|---:|---:|---:|---:|
| 1.5 | 667 | 7.80 | -81.4413 | -0.1221 | 0.0s | 87.3s |
| 2.0 | 300 | 15.00 | -30.5510 | -0.1018 | 13.0s | 118.7s |
| 2.5 | 95 | 8.42 | -11.1146 | -0.1170 | 15.0s | 125.5s |
| 3.0 | 27 | 11.11 | -3.0578 | -0.1133 | 32.0s | 148.3s |
| 3.5 | 8 | 25.00 | -0.6430 | -0.0804 | 35.5s | 181.9s |

## By source x intended k

| source | k | n | WR % | net PnL | EV | median hold |
|---|---:|---:|---:|---:|---:|---:|
| vwap_initial | 1.5 | 484 | 5.79 | -61.6677 | -0.1274 | 0.0s |
| vwap_initial | 2.0 | 237 | 13.50 | -25.2187 | -0.1064 | 13.0s |
| vwap_initial | 2.5 | 76 | 10.53 | -8.5897 | -0.1130 | 17.5s |
| vwap_initial | 3.0 | 18 | 11.11 | -2.1262 | -0.1181 | 23.5s |
| vwap_initial | 3.5 | 5 | 0.00 | -0.7505 | -0.1501 | 4.0s |
| vwap_inversion_flip | 1.5 | 183 | 13.11 | -19.7736 | -0.1081 | 11.0s |
| vwap_inversion_flip | 2.0 | 63 | 20.63 | -5.3323 | -0.0846 | 12.0s |
| vwap_inversion_flip | 2.5 | 19 | 0.00 | -2.5249 | -0.1329 | 12.0s |
| vwap_inversion_flip | 3.0 | 9 | 11.11 | -0.9315 | -0.1035 | 123.0s |
| vwap_inversion_flip | 3.5 | 3 | 66.67 | +0.1075 | +0.0358 | 327.0s |


## Honest framing

This is the first cut at a *VWAP-anchored* entry. The hypothesis (volume-weighted anchors have stronger mean-reversion than zone edges) is **not yet validated** - this is a measurement, not a promotion. The next step would be to break out EV by (sigma multiplier x VWAP window) and check whether any cell turns net-positive without a heavy fee bite. The fee-vs-edge collapse documented in AGENTS.md (2026-09-26 evening 1) applies here too: with qty=0.001 BTC at BTC=$100k, the round-trip fee is ~$1.00 per trade and the median gross edge on a 60s order lifetime is much smaller.
