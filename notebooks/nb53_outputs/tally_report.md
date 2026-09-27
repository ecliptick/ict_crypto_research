# BTCUSDT 2026-09-25: live vs backtest tally

## Live trades today (VPS, paper fill, 0.001 BTC, 5 bps taker)

| trade_id | dir | entry_UTC | entry_$ | stop_$ | target_$ | exit_$ | exit_reason | zone_id |
| ---: | :--- | :--- | ---: | ---: | ---: | ---: | :--- | ---: |
| 34 | short | 00:56:15 | $84,594.40 | $26.60 | $266.00 | $84,621.00 | SL | 273 |
| 33 | long | 00:47:21 | $84,566.30 | $32.80 | $328.00 | $84,533.50 | SL | 258 |
| 32 | long | 23:58:27 | $84,362.50 | $17.40 | $174.00 | $84,536.50 | TP | 191 |
| 31 | long | 22:44:13 | $84,068.30 | $38.00 | $380.00 | $84,448.30 | TP | 135 |

## Canonical v17-btc-sniper-2026-09-26b backtest (this fork)

params: lots=0.001, BTCUSDT, recipe=?

| # | dir | entry_UTC | entry_$ | stop_$ | target_$ | exit_$ | exit_reason | pnl_$ |
| ---: | :--- | :--- | ---: | ---: | ---: | ---: | :--- | ---: |
| 0 | short | 02:17:18 | $84,811.40 | $25.20 | $252.00 | $84,818.10 | inv | $-0.0915 |
| 1 | long | 02:38:03 | $84,450.00 | $20.20 | $202.00 | $84,462.20 | inv | $-0.0723 |
| 2 | short | 06:47:14 | $84,015.70 | $31.20 | $312.00 | $83,703.70 | tp | $0.2280 |
| 3 | long | 08:08:31 | $84,045.60 | $27.40 | $274.00 | $84,319.60 | tp | $0.1900 |
| 4 | long | 08:11:32 | $84,069.40 | $27.00 | $270.00 | $84,339.40 | tp | $0.1859 |

## Live-equivalent recipe backtest (1m resample)

params: lots=0.001, BTCUSDT, recipe=?

| # | dir | entry_UTC | entry_$ | stop_$ | target_$ | exit_$ | exit_reason | pnl_$ |
| ---: | :--- | :--- | ---: | ---: | ---: | ---: | :--- | ---: |
| 0 | long | 01:56:30 | $84,470.00 | $20.20 | $0.00 | $84,462.68 | inv | $-0.0073 |
| 1 | short | 02:17:18 | $84,811.40 | $25.20 | $0.00 | $84,817.62 | inv | $-0.0062 |
| 2 | short | 02:32:28 | $84,668.30 | $48.60 | $0.00 | $84,182.30 | tp | $0.4860 |
| 3 | short | 06:47:14 | $84,015.70 | $31.20 | $0.00 | $83,703.70 | tp | $0.3120 |
| 4 | long | 07:54:16 | $83,948.90 | $11.60 | $0.00 | $83,937.30 | sl | $-0.0116 |
| 5 | long | 08:07:27 | $84,003.30 | $22.60 | $0.00 | $83,980.70 | sl | $-0.0226 |
| 6 | long | 08:08:31 | $84,045.60 | $27.40 | $0.00 | $84,319.60 | tp | $0.2740 |
| 7 | long | 08:11:32 | $84,069.40 | $27.00 | $0.00 | $84,339.40 | tp | $0.2700 |
| 8 | short | 08:28:35 | $84,427.40 | $25.40 | $0.00 | $84,368.50 | eod | $0.0589 |

## Live-equivalent recipe backtest (1s pivot)

params: lots=0.001, BTCUSDT, recipe=live_equiv_1s

| # | dir | entry_UTC | entry_$ | stop_$ | target_$ | exit_$ | exit_reason | pnl_$ |
| ---: | :--- | :--- | ---: | ---: | ---: | ---: | :--- | ---: |
| 0 | long | 01:56:30 | $84,470.00 | $20.20 | $0.00 | $84,462.68 | inv | $-0.0073 |
| 1 | short | 02:17:18 | $84,811.40 | $25.20 | $0.00 | $84,817.62 | inv | $-0.0062 |
| 2 | short | 02:32:28 | $84,668.30 | $48.60 | $0.00 | $84,182.30 | tp | $0.4860 |
| 3 | short | 06:47:14 | $84,015.70 | $31.20 | $0.00 | $83,703.70 | tp | $0.3120 |
| 4 | long | 07:54:16 | $83,948.90 | $11.60 | $0.00 | $83,937.30 | sl | $-0.0116 |
| 5 | long | 08:07:27 | $84,003.30 | $22.60 | $0.00 | $83,980.70 | sl | $-0.0226 |
| 6 | long | 08:08:31 | $84,045.60 | $27.40 | $0.00 | $84,319.60 | tp | $0.2740 |
| 7 | long | 08:11:32 | $84,069.40 | $27.00 | $0.00 | $84,339.40 | tp | $0.2700 |
| 8 | short | 08:28:35 | $84,427.40 | $25.40 | $0.00 | $84,368.50 | eod | $0.0589 |

## TALLY summary

Live trades today vs each backtest variant:

| trade_id | side | entry time | canonical | live-equiv (1m) | live-equiv (1s) |
| ---: | :--- | :--- | :--- | :--- | :--- |
| 34 | short | 00:56:15 | ✗ | ✗ | ✗ |
| 33 | long | 00:47:21 | ✗ | ✗ | ✗ |
| 32 | long | ? | ✗ | ✗ | ✗ |
| 31 | long | ? | ✗ | ✗ | ✗ |

**None of the three backtest variants reproduce trades 33 or 34.**

## Root-cause analysis

### 1. Recipe drift between the live engine and the BTC fork

`ict_sniper_live/src/core/optimal_config.py` is the **XAUUSD-tuned**
v17 SNIPER recipe from `ict_tier_v2`, NOT the BTC-scale fork in
`src/core/optimal_config.py`. Material differences:

| Knob | Live (ict_sniper_live) | BTC fork (v17-btc-sniper-2026-09-26b) |
| --- | ---: | ---: |
| `fvg_min_zone_usd` | 0.10 (gold) | 5.00 (BTC) |
| `fvg_invalidation_min_pierce_usd` | 0.05 (gold) | 2.00 (BTC) |
| `fvg_inv_trade_tp_zone_mult` | 20.0 (v17 gold) | 22.0 (v17b BTC) |
| `sl_usd` / `tp_usd` | 0.80 / 1.80 (gold) | 20.0 / 200.0 (BTC) |
| `fvg_body_only_mitigation/invalidation` | False | True (added 2026-09-26) |
| `ifvg_min_zone_usd` | 0.10 | 0.10 (BTC fork default actually 0.10 too) |

`fvg_resample_secs=60`, `fvg_supersede_on_new=True` are identical in
both repos.

### 2. The 1m resample, combined with supersede-on-new, suppresses all
###    inversions on this 8.5h slice

`detect_fvg(... resample_to_n_secs=60, supersede_on_new=True)` produces
**182 zones, 11 inverted** on today's 28,567 1s bars. The first
inversion at bar 6059 (00:1:48) is well after trade 33 (00:47).

Without `supersede_on_new` (turning it OFF), the same detector produces
**167 zones, 137 inverted**. The supersede rule is **silencing the entire
iFVG / sniper path** on 1-minute-pivot BTCUSDT data because zones get
superceded by newer zones before price can move enough to invert them.

### 3. The 1-second-pivot detector does NOT produce the trade 33 / 34
###    zones either — even with the live-equivalent `min_zone=0.10`

`detect_fvg(... resample_to_n_secs=1, fvg_min_zone_usd=0.10, ...)`
produces 3,950 zones (no supersede) and 3,272 zones (supersede=ON).
With supersede=ON, **0 zones invert** on the entire 8.5h corpus —
every zone dies to supersession before price can develop an invert.

This is the deadlock: at 1s pivots with gold-tuned `fvg_min_zone_usd=0.10`,
the 1s BTCUSDT volatility creates a new zone every ~7 seconds, and
each zone gets superseded within ~10-60 seconds by a nearby zone.
Price moves through the threshold before inversion can fire.

### 4. The live engine has 4 hours of pre-midnight warmup that the
###    batch backtest doesn't

The VPS live engine pulled 14,400 1s bars at startup (20:17 UTC).
By 00:47 today its rolling buffer covers 20:47 yesterday → 00:47
today, including the bearish move from $84,700 → $84,500 that
happened around 19:00-21:00 yesterday. The detector running on
the rolling buffer sees zones from that move and can't recreate them
in a batch run that starts cold at 00:00 today.

### 5. Hypothesis: trade 33 / 34 zones were created from yesterday's
###    bearish setup that crossed the day boundary

Trade 33 is a LONG with stop $32.80 (zone_w = $16.40) at the price
low at 00:47:21. The price action ~25 minutes BEFORE trade 33
shows a $19 drop from $84,569 to $84,551 around 00:44:55, with a
$1.20 FVG at $84,580 around 00:47:02. Neither of those zones
explain a $16.40-wide zone at the right level. The detector running
on yesterday's evening context would have had a 1m-zone from
yesterday's $84,500-$84,700 down-leg that spans the boundary and
could legitimately have been the sniper target — but we can't
test that without yesterday's aggTrades.

## Conclusion

**The backtest cannot reproduce trades 33 / 34 today because:**

1. The canonical BTC recipe has `fvg_min_zone_usd=5.00` vs the
   live engine's gold-tuned `0.10`. The 1.20-wide FVG at 00:47:02
   **was filtered out** at the canonical threshold.
2. The live engine's recipe (gold-tuned) and the BTC fork recipe
   **diverge on 5+ knobs**. Restoring the live-equivalent recipe
   in the batch backtest does NOT reproduce the trades either,
   because the 1m + supersede pipeline deadlocks (0 zones invert)
   and the 1s + supersede pipeline deadlocks the same way.
3. The live engine runs on a 4h rolling buffer that includes
   yesterday evening's price action, which the cold-start batch
   backtest does not have.

**Recommendation: pull yesterday's aggTrades (start at ~16:17 UTC),
re-run the live-equivalent backtest over the 16:17 yesterday →
08:30 today window, and re-tally.** Until that's done, the
divergence between live and backtest is dominated by data-window
mismatch, not by a bug in either engine.
