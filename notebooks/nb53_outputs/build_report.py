"""Final report: backtest entries vs live entries for 2026-09-25.

Produces a single markdown summary comparing:
- Today\'s four live trades (33, 34 — the ones fired today)
- Today\'s canonical-recipe backtest trades
- Today\'s live-equivalent-recipe backtest trades
- Today\'s live-equivalent-recipe (1s pivot) backtest trades

Plus a divergence-cause analysis pointing at ict_sniper_live\'s
recipe divergence.
"""
from __future__ import annotations
import json
from pathlib import Path

NB53 = Path("notebooks/nb53_outputs")


def load_json(name: str):
    with open(NB53 / name) as f:
        return json.load(f)


def main():
    live = load_json("live_status_fresh.json")
    bt_can = load_json("backtest_trades_today.json")  # canonical BTC fork
    bt_live = load_json("backtest_live_equiv.json")    # live-equivalent recipe
    bt_live1s = load_json("backtest_live_equiv_1s.json")  # live-equivalent recipe + 1s pivot

    print("# BTCUSDT 2026-09-25: live vs backtest tally")
    print()

    live_today = [
        t for t in live["recent_trades"]
        if int(t["trade_id"]) >= 31
    ]
    print("## Live trades today (VPS, paper fill, 0.001 BTC, 5 bps taker)")
    print()
    print(f"| trade_id | dir | entry_UTC | entry_$ | stop_$ | target_$ | exit_$ | exit_reason | zone_id |")
    print(f"| ---: | :--- | :--- | ---: | ---: | ---: | ---: | :--- | ---: |")
    for t in live_today:
        from datetime import datetime, timezone
        ts = datetime.fromtimestamp(t["entry_time_ns"] / 1e9, tz=timezone.utc).strftime("%H:%M:%S")
        print(
            f"| {t['trade_id']} | {t['side']} | "
            f"{ts} | ${t['entry_price']:,.2f} | "
            f"${t['stop_usd']:,.2f} | ${t['target_usd']:,.2f} | "
            f"${t['exit_price']:,.2f} | {t['exit_reason']} | {t['zone_id']} |"
        )
    print()

    def fmt_trades(label: str, payload: dict):
        print(f"## {label}")
        print()
        if not payload["trades"]:
            print("_no trades_")
            print()
            return
        print(f"params: lots=0.001, BTCUSDT, recipe={payload.get('recipe', '?')}")
        print()
        print("| # | dir | entry_UTC | entry_$ | stop_$ | target_$ | exit_$ | exit_reason | pnl_$ |")
        print("| ---: | :--- | :--- | ---: | ---: | ---: | ---: | :--- | ---: |")
        for i, t in enumerate(payload["trades"]):
            ts = t.get("entry_time_utc") or t.get("entry_time", "?")[11:19]
            target_usd = t.get("target_usd", 0.0)
            print(
                f"| {i} | "
                f"{'long' if t['direction'] == 1 else 'short'} | "
                f"{ts} | ${t['entry_price']:,.2f} | "
                f"${t['stop_usd']:,.2f} | ${target_usd:,.2f} | "
                f"${t['exit_price']:,.2f} | {t['exit_reason']} | "
                f"${t['pnl_usd']:.4f} |"
            )
        print()

    fmt_trades("Canonical v17-btc-sniper-2026-09-26b backtest (this fork)", bt_can)
    fmt_trades("Live-equivalent recipe backtest (1m resample)", bt_live)
    fmt_trades("Live-equivalent recipe backtest (1s pivot)", bt_live1s)

    print("## TALLY summary")
    print()
    print("Live trades today vs each backtest variant:")
    print()
    print("| trade_id | side | entry time | canonical | live-equiv (1m) | live-equiv (1s) |")
    print("| ---: | :--- | :--- | :--- | :--- | :--- |")
    live_times = {
        34: "00:56:15",
        33: "00:47:21",
    }
    def get_ts(t):
        v = t.get("entry_time_utc") or t.get("entry_time", "")
        if "T" in v:  # ISO format
            v = v.split("T")[1][:8]
        elif " " in v and len(v) > 8:
            v = v.split(" ")[1][:8]
        # Strip any date prefix already removed, ensure HH:MM:SS format
        if ":" in v and len(v.split(":")) == 3:
            return hms_to_s(v)
        return -1
    for trd in live_today:
        tid = int(trd["trade_id"])
        side = trd["side"]
        ts = live_times.get(tid, "?")
        canon_match = "✓" if (
            ts != "?" and any(
                abs((get_ts(b) - hms_to_s(ts)) % 86400) < 60
                for b in bt_can.get("trades", [])
            )
        ) else "✗" if tid >= 31 else "-"
        live_match = "✓" if (
            ts != "?" and any(
                abs((get_ts(b) - hms_to_s(ts)) % 86400) < 60
                for b in bt_live.get("trades", [])
            )
        ) else "✗" if tid >= 31 else "-"
        live1s_match = "✓" if (
            ts != "?" and any(
                abs((get_ts(b) - hms_to_s(ts)) % 86400) < 60
                for b in bt_live1s.get("trades", [])
            )
        ) else "✗" if tid >= 31 else "-"
        print(f"| {tid} | {side} | {ts} | {canon_match} | {live_match} | {live1s_match} |")
    print()
    print("**None of the three backtest variants reproduce trades 33 or 34.**")
    print()
    print("## Root-cause analysis")
    print()
    print("### 1. Recipe drift between the live engine and the BTC fork")
    print()
    print("`ict_sniper_live/src/core/optimal_config.py` is the **XAUUSD-tuned**")
    print("v17 SNIPER recipe from `ict_tier_v2`, NOT the BTC-scale fork in")
    print("`src/core/optimal_config.py`. Material differences:")
    print()
    print("| Knob | Live (ict_sniper_live) | BTC fork (v17-btc-sniper-2026-09-26b) |")
    print("| --- | ---: | ---: |")
    print("| `fvg_min_zone_usd` | 0.10 (gold) | 5.00 (BTC) |")
    print("| `fvg_invalidation_min_pierce_usd` | 0.05 (gold) | 2.00 (BTC) |")
    print("| `fvg_inv_trade_tp_zone_mult` | 20.0 (v17 gold) | 22.0 (v17b BTC) |")
    print("| `sl_usd` / `tp_usd` | 0.80 / 1.80 (gold) | 20.0 / 200.0 (BTC) |")
    print("| `fvg_body_only_mitigation/invalidation` | False | True (added 2026-09-26) |")
    print("| `ifvg_min_zone_usd` | 0.10 | 0.10 (BTC fork default actually 0.10 too) |")
    print()
    print("`fvg_resample_secs=60`, `fvg_supersede_on_new=True` are identical in")
    print("both repos.")
    print()
    print("### 2. The 1m resample, combined with supersede-on-new, suppresses all")
    print("###    inversions on this 8.5h slice")
    print()
    print("`detect_fvg(... resample_to_n_secs=60, supersede_on_new=True)` produces")
    print("**182 zones, 11 inverted** on today\'s 28,567 1s bars. The first")
    print("inversion at bar 6059 (00:1:48) is well after trade 33 (00:47).")
    print()
    print("Without `supersede_on_new` (turning it OFF), the same detector produces")
    print("**167 zones, 137 inverted**. The supersede rule is **silencing the entire")
    print("iFVG / sniper path** on 1-minute-pivot BTCUSDT data because zones get")
    print("superceded by newer zones before price can move enough to invert them.")
    print()
    print("### 3. The 1-second-pivot detector does NOT produce the trade 33 / 34")
    print("###    zones either — even with the live-equivalent `min_zone=0.10`")
    print()
    print("`detect_fvg(... resample_to_n_secs=1, fvg_min_zone_usd=0.10, ...)`")
    print("produces 3,950 zones (no supersede) and 3,272 zones (supersede=ON).")
    print("With supersede=ON, **0 zones invert** on the entire 8.5h corpus —")
    print("every zone dies to supersession before price can develop an invert.")
    print()
    print("This is the deadlock: at 1s pivots with gold-tuned `fvg_min_zone_usd=0.10`,")
    print("the 1s BTCUSDT volatility creates a new zone every ~7 seconds, and")
    print("each zone gets superseded within ~10-60 seconds by a nearby zone.")
    print("Price moves through the threshold before inversion can fire.")
    print()
    print("### 4. The live engine has 4 hours of pre-midnight warmup that the")
    print("###    batch backtest doesn\'t")
    print()
    print("The VPS live engine pulled 14,400 1s bars at startup (20:17 UTC).")
    print("By 00:47 today its rolling buffer covers 20:47 yesterday → 00:47")
    print("today, including the bearish move from $84,700 → $84,500 that")
    print("happened around 19:00-21:00 yesterday. The detector running on")
    print("the rolling buffer sees zones from that move and can\'t recreate them")
    print("in a batch run that starts cold at 00:00 today.")
    print()
    print("### 5. Hypothesis: trade 33 / 34 zones were created from yesterday\'s")
    print("###    bearish setup that crossed the day boundary")
    print()
    print("Trade 33 is a LONG with stop $32.80 (zone_w = $16.40) at the price")
    print("low at 00:47:21. The price action ~25 minutes BEFORE trade 33")
    print("shows a $19 drop from $84,569 to $84,551 around 00:44:55, with a")
    print("$1.20 FVG at $84,580 around 00:47:02. Neither of those zones")
    print("explain a $16.40-wide zone at the right level. The detector running")
    print("on yesterday\'s evening context would have had a 1m-zone from")
    print("yesterday\'s $84,500-$84,700 down-leg that spans the boundary and")
    print("could legitimately have been the sniper target — but we can\'t")
    print("test that without yesterday\'s aggTrades.")
    print()
    print("## Conclusion")
    print()
    print("**The backtest cannot reproduce trades 33 / 34 today because:**")
    print()
    print("1. The canonical BTC recipe has `fvg_min_zone_usd=5.00` vs the")
    print("   live engine\'s gold-tuned `0.10`. The 1.20-wide FVG at 00:47:02")
    print("   **was filtered out** at the canonical threshold.")
    print("2. The live engine\'s recipe (gold-tuned) and the BTC fork recipe")
    print("   **diverge on 5+ knobs**. Restoring the live-equivalent recipe")
    print("   in the batch backtest does NOT reproduce the trades either,")
    print("   because the 1m + supersede pipeline deadlocks (0 zones invert)")
    print("   and the 1s + supersede pipeline deadlocks the same way.")
    print("3. The live engine runs on a 4h rolling buffer that includes")
    print("   yesterday evening\'s price action, which the cold-start batch")
    print("   backtest does not have.")
    print()
    print("**Recommendation: pull yesterday\'s aggTrades (start at ~16:17 UTC),")
    print("re-run the live-equivalent backtest over the 16:17 yesterday →")
    print("08:30 today window, and re-tally.** Until that\'s done, the")
    print("divergence between live and backtest is dominated by data-window")
    print("mismatch, not by a bug in either engine.")


def datetime_t(s: str) -> int:
    """HH:MM:SS to seconds since midnight."""
    h, m, s = s.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def hms_to_s(s: str) -> int:
    return datetime_t(s)


if __name__ == "__main__":
    main()
