"""Parity test: research backtest vs live engine on Sep 25 BTCUSDT 1s bars.

Uses ``fast_live_replay.fast_replay`` for the live engine path — that
module precomputes zones/structure once instead of re-running the
detector on every bar (which would take ~50min for a full UTC day).

Outputs
=======
* ``parity_research_trades.csv``           — research backtest trades
* ``parity_live_trades.csv``               — live engine with its OWN canonical (XAUUSD-inherited)
* ``parity_live_btc_recipe_trades.csv``    — live engine with BTC fork recipe
* ``parity_report_research_vs_live_canonical.md``
* ``parity_report_research_vs_live_btc_recipe.md``
"""
from __future__ import annotations

import csv
import os
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import numpy as np
import pandas as pd

# ── Imports ──
sys.path.insert(0, r"C:\coding\ict_crypto_research")

from src.tick.aggtrade_aggregator import (
    aggregate_ticks_to_1s_bars,
    load_raw_aggtrades,
)
from src.tick.tick_backtest import run_tick_backtest
from src.core.optimal_config import optimal_params as fork_optimal_params

from fast_live_replay import fast_replay as _live_fast_replay, _load_live_engine

OUTPUT_DIR = Path(r"C:\coding\ict_crypto_research\notebooks\nb53_outputs")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TICKS_PATH = OUTPUT_DIR / "BTCUSDT-aggTrades-2026-09-25.parquet"


def bars_from_aggtrades(ticks_path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    print(f"[step 1] Loading raw aggTrades from {ticks_path} ...")
    raw = load_raw_aggtrades(ticks_path)
    print(f"  rows: {len(raw):,}, range {raw['ts'].min()} -> {raw['ts'].max()}")
    print(f"[step 2] Aggregating to 1s OHLCV bars ...")
    bars = aggregate_ticks_to_1s_bars(raw)
    print(f"  bars: {len(bars):,}, range {bars['time'].min()} -> {bars['time'].max()}")
    return bars, raw


def slice_raw_ticks(raw: pd.DataFrame, t_start: pd.Timestamp, t_end: pd.Timestamp) -> pd.DataFrame:
    return raw[(raw["ts"] >= t_start) & (raw["ts"] < t_end)].reset_index(drop=True)


def run_research_backtest(raw: pd.DataFrame, label: str = "research_v17_btc"):
    print(f"[step 3] Running RESEARCH backtest ({label}) ...")
    p = fork_optimal_params()
    t0 = time.time()
    res = run_tick_backtest(raw_df=raw, p=p, strategy_label=label)
    dt = time.time() - t0
    print(f"  {len(res.trades)} trades in {dt:.1f}s")
    return res


def research_trades_to_dicts(res) -> list[dict]:
    out = []
    for tr in res.trades:
        entry_iso = pd.Timestamp(tr.entry_time, unit="ns", tz="UTC").isoformat()
        exit_iso = pd.Timestamp(tr.exit_time, unit="ns", tz="UTC").isoformat()
        out.append({
            "entry_ts_utc": entry_iso,
            "exit_ts_utc": exit_iso,
            "direction": int(tr.direction),
            "entry_price": float(tr.entry_price),
            "exit_price": float(tr.exit_price),
            "exit_reason": str(tr.exit_reason),
            "triggered_by": str(getattr(tr, "entry_triggered_by", "")),
            "net_pnl_usd": float(tr.net_pnl_usd()),
            "fee_usd": float(tr.fee_usd),
            "zone_id": int(getattr(tr, "zone_id", -1)),
            "rank_tier": str(getattr(tr, "rank_tier", "")),
            "entry_alignment": str(getattr(tr, "entry_alignment", "")),
        })
    return out


def save_trades_csv(rows: list[dict], path: Path):
    if not rows:
        path.write_text("(empty)\n")
        print(f"  (no trades) wrote {path}")
        return
    keys = list(rows[0].keys())
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    print(f"  wrote {len(rows)} trades to {path}")


def run_live_engine(bars: pd.DataFrame, params, label: str) -> list[dict]:
    """Replay bars through the live engine via the fast harness."""
    print(f"[step 4] Running LIVE engine ({label}) ...")
    t0 = time.time()
    fired = _live_fast_replay(bars, params=params, warmup_bars=14_400, recipe_name=label)
    dt = time.time() - t0
    print(f"  {len(fired)} snipers in {dt:.1f}s")
    return fired


def compare_trades(research_rows: list[dict], live_rows: list[dict], tolerance_secs: int = 5) -> str:
    lines = []
    lines.append(f"# Parity report — research backtest vs live engine (tol=+/- {tolerance_secs}s)")
    lines.append("")
    lines.append(f"- Research trades: **{len(research_rows)}**")
    lines.append(f"- Live engine snipers fired: **{len(live_rows)}**")
    lines.append("")

    def rk(r):
        return pd.Timestamp(r["entry_ts_utc"]).floor("s")
    def lk(r):
        return pd.Timestamp(int(r["bar_ts_ms"]), unit="ms", tz="UTC").floor("s")

    # Greedy match within tolerance, same direction.
    matched_rows = []
    used_live = set()
    for r in research_rows:
        rt = rk(r)
        for j, l in enumerate(live_rows):
            if j in used_live:
                continue
            if int(r["direction"]) != int(l["direction"]):
                continue
            if abs((lk(l) - rt).total_seconds()) <= tolerance_secs:
                matched_rows.append((r, l))
                used_live.add(j)
                break
    matched = len(matched_rows)
    research_only_count = len(research_rows) - matched
    live_only_count = len(live_rows) - matched

    lines.append(f"## Headline parity (within +/- {tolerance_secs}s, same direction)")
    lines.append("")
    lines.append(f"- Matched: **{matched}**")
    lines.append(f"- Research-only: **{research_only_count}**")
    lines.append(f"- Live-only: **{live_only_count}**")
    if research_rows:
        lines.append(f"- Parity rate: **{matched / len(research_rows) * 100:.1f}%** (research->live)")
    lines.append("")

    lines.append("## Matched entries (first 20)")
    lines.append("")
    if matched_rows:
        lines.append("| entry_ts_research | entry_ts_live | dt_sec | dir | research_px | live_px | research_exit | research_pnl |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for r, l in matched_rows[:20]:
            dt = (lk(l) - rk(r)).total_seconds()
            lines.append(
                f"| {rk(r)} | {lk(l)} | {dt:+.1f} | {r['direction']:+d} | "
                f"{r['entry_price']:.2f} | {l['entry_price_hint']:.2f} | "
                f"{r['exit_reason']} | {r['net_pnl_usd']:.2f} |"
            )
    else:
        lines.append("_(no matched entries)_")
    lines.append("")

    lines.append("## Research-only (first 20)")
    lines.append("")
    if research_only_count:
        only_rows = []
        used_now = set()
        for r in research_rows:
            rt = rk(r)
            claimed = False
            for j, l in enumerate(live_rows):
                if j in used_now:
                    continue
                if int(r["direction"]) != int(l["direction"]):
                    continue
                if abs((lk(l) - rt).total_seconds()) <= tolerance_secs:
                    claimed = True
                    used_now.add(j)
                    break
            if not claimed:
                only_rows.append(r)
        lines.append("| entry_ts | dir | entry_px | exit_reason | pnl |")
        lines.append("|---|---|---|---|---|")
        for r in only_rows[:20]:
            lines.append(
                f"| {rk(r)} | {r['direction']:+d} | {r['entry_price']:.2f} | "
                f"{r['exit_reason']} | {r['net_pnl_usd']:.2f} |"
            )
    else:
        lines.append("_(none)_")
    lines.append("")

    lines.append("## Live-only (first 20)")
    lines.append("")
    if live_only_count:
        live_only_rows = [l for j, l in enumerate(live_rows) if j not in used_live]
        lines.append("| entry_ts | dir | entry_price_hint | zone_id |")
        lines.append("|---|---|---|---|")
        for r in live_only_rows[:20]:
            lines.append(
                f"| {lk(r)} | {r['direction']:+d} | {r['entry_price_hint']:.2f} | "
                f"{r['zone_id']} |"
            )
    else:
        lines.append("_(none)_")
    lines.append("")
    return "\n".join(lines)


def main():
    print(f"=== Parity test: research backtest vs live engine ===")
    print(f"Ticks: {TICKS_PATH}")
    if not TICKS_PATH.exists():
        raise FileNotFoundError(f"Missing {TICKS_PATH}; pull aggTrades first")

    bars_full, raw_full = bars_from_aggtrades(TICKS_PATH)
    bars_full.to_parquet(OUTPUT_DIR / "BTCUSDT-1s-bars-2026-09-25.parquet")
    print(f"  saved 1s bars -> {OUTPUT_DIR / 'BTCUSDT-1s-bars-2026-09-25.parquet'}")

    # Default: full Sep 25 (00:00 to 20:00 UTC, 68k bars).
    # Set PARITY_SKIP_HOURS / PARITY_HOURS to subsample.
    SKIP_HOURS = int(os.environ.get("PARITY_SKIP_HOURS", "0"))
    N_HOURS = int(os.environ.get("PARITY_HOURS", "0"))  # 0 = no slice
    if SKIP_HOURS > 0 or N_HOURS > 0:
        start_idx = SKIP_HOURS * 3600
        end_idx = start_idx + N_HOURS * 3600 if N_HOURS > 0 else len(bars_full)
        bars = bars_full.iloc[start_idx:end_idx].reset_index(drop=True)
        t_start = bars["time"].iloc[0]
        t_end = bars["time"].iloc[-1]
        raw = slice_raw_ticks(raw_full, t_start, t_end + pd.Timedelta(seconds=1))
        print(f"  subsampled to {len(bars)} bars ({t_start} -> {t_end}), {len(raw):,} ticks")
    else:
        bars = bars_full
        raw = raw_full
        print(f"  using full {len(bars)} bars, {len(raw):,} ticks")

    # Research backtest
    res_btc = run_research_backtest(raw, label="research_v17_btc_fork")
    research_rows = research_trades_to_dicts(res_btc)
    save_trades_csv(research_rows, OUTPUT_DIR / "parity_research_trades.csv")

    # Live engine with its OWN canonical recipe (XAUUSD-inherited)
    mods = _load_live_engine()
    p_live = mods["optimal_params"]()
    fired_live = run_live_engine(bars, p_live, "live_canonical")
    save_trades_csv(fired_live, OUTPUT_DIR / "parity_live_trades.csv")

    # Live engine with BTC fork recipe
    p_btc = fork_optimal_params()
    # Adapt position_mode / position_size to live engine schema (no
    # `lots` / `contract_size` on the live engine's TrendStrategyParams).
    # The fork's lots=0.01, contract_size=1.0 = 0.01 BTC ≈ $840. The live
    # engine uses position_mode="fixed_btc", position_size=0.001 for the
    # 0.001 BTC lot. For a fair test we override to match the fork's
    # 0.01 BTC size.
    p_btc.position_mode = "fixed_btc"
    p_btc.position_size = 0.01
    fired_live_btc = run_live_engine(bars, p_btc, "live_with_btc_recipe")
    save_trades_csv(fired_live_btc, OUTPUT_DIR / "parity_live_btc_recipe_trades.csv")

    # Compare: research (BTC fork) vs live (live_canonical)
    report_a = compare_trades(research_rows, fired_live, tolerance_secs=600)
    (OUTPUT_DIR / "parity_report_research_vs_live_canonical.md").write_text(report_a, encoding="utf-8")

    # Compare: research (BTC fork) vs live (BTC fork)
    report_b = compare_trades(research_rows, fired_live_btc, tolerance_secs=600)
    (OUTPUT_DIR / "parity_report_research_vs_live_btc_recipe.md").write_text(report_b, encoding="utf-8")

    print(f"\n=== Done ===")
    print(f"  Research trades : {len(research_rows)}")
    print(f"  Live (XAU inherited) snipers : {len(fired_live)}")
    print(f"  Live (BTC fork recipe) snipers : {len(fired_live_btc)}")
    print(f"\nReports:")
    print(f"  {OUTPUT_DIR / 'parity_report_research_vs_live_canonical.md'}")
    print(f"  {OUTPUT_DIR / 'parity_report_research_vs_live_btc_recipe.md'}")


if __name__ == "__main__":
    main()
