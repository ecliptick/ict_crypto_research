# Parity report — research backtest vs live engine (tol=+/- 600s)

- Research trades: **5**
- Live engine snipers fired: **8**

## Headline parity (within +/- 600s, same direction)

- Matched: **5**
- Research-only: **0**
- Live-only: **3**
- Parity rate: **100.0%** (research->live)

## Matched entries (first 20)

| entry_ts_research | entry_ts_live | dt_sec | dir | research_px | live_px | research_exit | research_pnl |
|---|---|---|---|---|---|---|---|
| 2026-09-25 10:31:38+00:00 | 2026-09-25 10:29:41+00:00 | -117.0 | +1 | 84663.40 | 84677.50 | inv | -1.93 |
| 2026-09-25 11:35:53+00:00 | 2026-09-25 11:31:19+00:00 | -274.0 | -1 | 84886.30 | 84851.30 | inv | -1.74 |
| 2026-09-25 13:16:43+00:00 | 2026-09-25 13:08:39+00:00 | -484.0 | -1 | 84463.80 | 84454.00 | tp | 9.97 |
| 2026-09-25 17:31:25+00:00 | 2026-09-25 17:33:29+00:00 | +124.0 | +1 | 83614.10 | 83755.90 | inv | -1.86 |
| 2026-09-25 13:36:36+00:00 | 2026-09-25 13:26:54+00:00 | -582.0 | -1 | 84132.70 | 84106.90 | eod | -0.13 |

## Research-only (first 20)

_(none)_

## Live-only (first 20)

| entry_ts | dir | entry_price_hint | zone_id |
|---|---|---|---|
| 2026-09-25 13:25:54+00:00 | -1 | 84130.20 | 245 |
| 2026-09-25 15:59:12+00:00 | +1 | 83718.00 | 288 |
| 2026-09-25 19:07:22+00:00 | +1 | 83962.50 | 344 |
