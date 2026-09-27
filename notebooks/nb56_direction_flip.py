"""NB56 follow-up — `sniper_inv_direction_mode` A/B on 2025-11.

User question (2026-09-26):
  "if win rate is so low, why not just 1:2 RR and flip the direction?"

Preliminary fee check (qty_btc=0.001, BTC ~$100k, 5bps taker):
  * Median zone width on sniper = $45.35
  * Round-trip fee per trade = $0.10
  * At 1:2 RR (1x zone SL / 2x zone TP):
    - SL = $45.35, TP = $90.70
    - SL cost = $0.045, TP gross = $0.091
    - TP-net if win: +$0.091 - $0.10 = -$0.009 (fee-biter)
    - SL-net if loss: -$0.045 - $0.10 = -$0.145
  * Need WR > 100% to break even at 1:2 RR
  * Minimum TP zone_mult to clear fees: 2.21x zone width (~$100 TP)

User's "1:2 RR" hypothesis FAILS the fee check at qty_btc=0.001.
But the direction-flip part of the question is well-formed and
testable cheaply:
  * canonical ('continuation'): enters same direction as inverted zone
    polarity (e.g., bull FVG inverts -> enter LONG continuation)
  * 'fade_displacement': enters OPPOSITE direction
    (bull FVG inverts -> enter SHORT, fade the move)

Both modes use the SAME zone-anchored SL/TP (5x / 60x), which
IS wide enough to clear fees. So this test isolates the
direction question.

Test plan: A/B on a single non-2025-04 month (2025-11 = volatile
month from nb53 results) for apples-to-apples comparison.
  Cell A: sniper canonical ('continuation')  - baseline
  Cell B: sniper ('fade_displacement')       - same knobs, flipped direction

Outputs to a report file with the answer to the user's question.
"""
import sys
import time
import warnings
from collections import Counter
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT = None
for _c in [Path(".").resolve(), *Path(".").resolve().parents]:
    if (_c / "src" / "core" / "ict_signals.py").is_file():
        ROOT = _c
        break
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "notebooks"))

import pandas as pd

from src.core.optimal_config import optimal_params, OPTIMAL_RECIPE_VERSION
from src.tick.cache import get_or_build
from src.tick.tick_backtest import run_tick_backtest
from src.core.run_report import append_run_report

NOTEBOOK = "nb56_direction_flip"
SCENARIO = "sniper_inv_direction_mode_flip_ab"

OUT_DIR = ROOT / "notebooks" / "nb56_direction_flip_outputs"
OUT_DIR.mkdir(parents=True, exist_ok=True)

SOURCE_DATA_ROOT = Path(r"C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw")

CELLS = [
    # (label, sniper_inv_direction_mode, scope_label)
    ("continuation_canonical", "continuation"),
    ("fade_displacement_flip", "fade_displacement"),
]

# Months to test
MONTHS = ["2025-11"]  # 2025-11 = volatile month from nb53


def main():
    print(f"[{NOTEBOOK}] sniper_inv_direction_mode A/B")
    print(f"recipe version: {OPTIMAL_RECIPE_VERSION}")
    print(f"months: {MONTHS}\n")

    per_trade_rows = []
    grand_t0 = time.perf_counter()

    for month in MONTHS:
        path = SOURCE_DATA_ROOT / f"BTCUSDT-aggTrades-{month}.parquet"
        if not path.exists():
            print(f"  WARN: {path} not found, skipping", flush=True)
            continue

        # SweepCache is keyed on detector-side params only.
        # SL/TP zone-mult and direction mode are not in the fingerprint.
        p_base = optimal_params(qty_btc=0.001)
        t0 = time.perf_counter()
        sc = get_or_build(path, p_base, with_side_table=True, verbose=False)
        print(f"[{NOTEBOOK}] {month}: cache={time.perf_counter()-t0:.2f}s "
              f"fvg={len(sc.fvg_zones)} ifvg={len(sc.ifvg_zones)}\n", flush=True)

        for label, mode in CELLS:
            ov = {
                "fvg_inv_trade_sl_zone_mult": 5.0,
                "fvg_inv_trade_tp_zone_mult": 60.0,
                "sniper_inv_direction_mode": mode,
            }
            p_i = optimal_params(qty_btc=0.001, **ov)

            t0 = time.perf_counter()
            res = run_tick_backtest(
                raw_df=None, p=p_i,
                strategy_label=f"{NOTEBOOK}_{label}",
                pre_aggregated_bars=sc.bars,
                precomputed_zones_by_src={"fvg": sc.fvg_zones,
                                          "ifvg": sc.ifvg_zones},
                precomputed_structure=sc.structure,
                precomputed_atr=sc.atr,
                side_table=sc.side_table,
            )
            t_bt = time.perf_counter() - t0

            trades = list(res.trades)
            pnls = [t.pnl_usd for t in trades]
            fees = [getattr(t, "fee_usd", 0.0) for t in trades]
            nets = [p - f for p, f in zip(pnls, fees)]
            n = len(trades)
            n_wins = sum(1 for v in nets if v > 0)
            wr = (100.0 * n_wins / n) if n else 0.0
            ev = (sum(nets) / n) if n else 0.0
            paths = Counter(t.entry_triggered_by for t in trades)
            n_long = sum(1 for t in trades if t.direction > 0)
            n_short = sum(1 for t in trades if t.direction < 0)

            print(f"  [{month}] {label:25s} | n={n:3d} | "
                  f"net=${sum(nets):+8.2f} gross=${sum(pnls):+8.2f} "
                  f"fees=${sum(fees):6.2f} WR={wr:5.1f}% EV=${ev:+.4f} | "
                  f"long={n_long} short={n_short} | "
                  f"paths={dict(paths)} | bt={t_bt:.1f}s", flush=True)

            for ti, t in enumerate(trades):
                per_trade_rows.append({
                    "file": month,
                    "cell": label,
                    "trade_idx": ti,
                    "entry_bar": int(getattr(t, "entry_bar", -1)),
                    "exit_bar": int(getattr(t, "exit_bar", -1)),
                    "direction": int(getattr(t, "direction", 0)),
                    "entry_price": float(getattr(t, "entry_price", 0.0)),
                    "exit_price": float(getattr(t, "exit_price", 0.0)),
                    "stop_usd": float(getattr(t, "stop_usd", 0.0)),
                    "target_usd": float(getattr(t, "target_usd", 0.0)),
                    "exit_reason": str(getattr(t, "exit_reason", "")),
                    "hold_secs": float(getattr(t, "hold_secs", 0.0)),
                    "pnl_usd": float(t.pnl_usd),
                    "fee_usd": float(getattr(t, "fee_usd", 0.0)),
                    "net_pnl_usd": float(t.pnl_usd) - float(getattr(t, "fee_usd", 0.0)),
                    "entry_triggered_by": str(getattr(t, "entry_triggered_by", "")),
                    "qty_btc": float(getattr(t, "qty_btc", 0.0)),
                })

            # Run-log
            n_clean = paths.get("ifvg_clean", 0)
            n_dirty = paths.get("sniper_dirty", 0)
            metrics_extra = {
                "n_ifvg_clean": n_clean,
                "n_sniper_dirty": n_dirty,
                "n_long": n_long,
                "n_short": n_short,
                "exit_reason_breakdown": dict(paths),
            }
            append_run_report(
                notebook=NOTEBOOK,
                scenario=SCENARIO,
                scope=[month],
                engine="tick",
                comments=(f"{label} on {month}. "
                          f"Overrides: {ov}. "
                          "User hypothesis: 'flip direction + 1:2 RR for higher WR'. "
                          "Preliminary fee check at qty_btc=0.001: 1:2 RR "
                          "needs 106.8% WR to break even (fees dominate at "
                          "$45 median zone width). This run keeps the canonical "
                          "5x/60x zone SL/TP (which DOES clear fees) and "
                          "isolates the DIRECTION effect — same path, "
                          "opposite side. 'fade_displacement' = OPPOSITE "
                          "zone polarity (fades the move); 'continuation' "
                          "(canonical) = same polarity."),
                hypothesis="Flipping direction ('fade_displacement') without "
                            "tightening SL/TP will produce different WR — "
                            "but since fees are dominated by TP size, the "
                            "expected delta is small on this month.",
                verdict="inconclusive",
                params=p_i,
                result=res,
                metrics_extra=metrics_extra,
                overrides_vs_canonical=ov,
                canonical_recipe_version=OPTIMAL_RECIPE_VERSION,
                out_root=OUT_DIR.parent,
                dedup_keys=("sniper_inv_direction_mode",),
            )

    elapsed = time.perf_counter() - grand_t0
    print(f"\n=== {NOTEBOOK} done. wall={elapsed:.1f}s ===", flush=True)

    if per_trade_rows:
        df = pd.DataFrame(per_trade_rows)
        csv_path = OUT_DIR / "nb56_direction_flip_per_trade.csv"
        df.to_csv(csv_path, index=False)
        print(f"  Wrote {csv_path} ({len(df)} rows)", flush=True)


if __name__ == "__main__":
    main()
