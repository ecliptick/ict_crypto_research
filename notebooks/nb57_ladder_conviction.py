"""NB57 — conviction-aware SL/TP widening on the immediate-ladder path.

Goal: find a configuration that makes the immediate-ladder path
profitable (or at least break-even) when running with the sniper in
``entry_mode='dual'``. The structural hypothesis is from the bucket
analysis in nb54c: ``sig.conviction`` (1.0 neutral, ~1.5 with a fresh
BoS/CHoCH+ in trade direction) is the single best post-hoc predictor
of which ladder trades win. We use it to widen the SL/TP post-fill so
high-conviction signals get more room and let winners run.

Knobs added (defaults identity, so canonical recipe is bit-identical):
  * ladder_conviction_sl_widen  (float, default 1.0)
  * ladder_conviction_tp_widen  (float, default 1.0)
  * ladder_min_conviction       (float, default 0.0)
  * ladder_num_layers_max       (int,   default 3)

Formula (bar loop, after compute_layer_sl_tp and tier scaling):
    if cv > 1.0:
        scaled_sl *= 1.0 + (cv - 1.0) * ladder_conviction_sl_widen
        scaled_tp *= 1.0 + (cv - 1.0) * ladder_conviction_tp_widen
    if ladder_min_conviction > 0 and cv < ladder_min_conviction:
        continue  # drop layer

Cells (12, all on 2025-11 only, single file = tiny POC):

  baseline (no-op widening)               1.0 / 1.0 / 0.0 / 3
  widen_only_sl_2x                        2.0 / 1.0 / 0.0 / 3
  widen_only_sl_4x                        4.0 / 1.0 / 0.0 / 3
  widen_only_tp_2x                        1.0 / 2.0 / 0.0 / 3
  widen_both_2x                           2.0 / 2.0 / 0.0 / 3
  widen_both_4x                           4.0 / 4.0 / 0.0 / 3
  widen_sl_4x_tp_1x                       4.0 / 1.0 / 0.0 / 3
  floor_cv_1.2                            1.0 / 1.0 / 1.2 / 3
  floor_cv_1.2_widen_2x                   2.0 / 2.0 / 1.2 / 3
  layers_1_only                           1.0 / 1.0 / 0.0 / 1
  layers_1_widen_2x                       2.0 / 2.0 / 0.0 / 1
  layers_2_widen_2x                       2.0 / 2.0 / 0.0 / 2

Each cell runs the full backtest with the canonical sniper (5x/60x
zone-mult, the nb53 winner) and ``entry_mode='dual'``. The success
bar is per-cell ladder WR >= 12% and ladder EV >= -$0.05.

Output: append-only JSONL at
``notebooks/nb57_ladder_conviction_outputs/nb57_ladder_conviction__runs.jsonl``
plus a per-trade CSV.
"""
import argparse
import json
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
from src.core.run_report import append_run_report
from src.tick.cache import get_or_build
from src.tick.tick_backtest import run_tick_backtest

NOTEBOOK = "nb57_ladder_conviction"

_argp = argparse.ArgumentParser(add_help=False)
_argp.add_argument("--month", default="2025-11")
_ns, _rest = _argp.parse_known_args()

SOURCE_DATA_ROOT = Path(r"C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw")
PATH = SOURCE_DATA_ROOT / f"BTCUSDT-aggTrades-{_ns.month}.parquet"

# Sniper SL/TP — keep at the nb53 winner (5x/60x zone-mult).
SNIPER_SL_ZM = 5.0
SNIPER_TP_ZM = 60.0

# Conviction-aware ladder cells (12).
# Tuples: (label, sl_widen, tp_widen, min_conviction, num_layers_max)
LADDER_CELLS = [
    ("baseline",         1.0, 1.0, 0.0, 3),
    ("widen_only_sl_2x", 2.0, 1.0, 0.0, 3),
    ("widen_only_sl_4x", 4.0, 1.0, 0.0, 3),
    ("widen_only_tp_2x", 1.0, 2.0, 0.0, 3),
    ("widen_both_2x",    2.0, 2.0, 0.0, 3),
    ("widen_both_4x",    4.0, 4.0, 0.0, 3),
    ("widen_sl_4x_tp_1x", 4.0, 1.0, 0.0, 3),
    ("floor_cv_1.2",     1.0, 1.0, 1.2, 3),
    ("floor_cv_1.2_widen_2x", 2.0, 2.0, 1.2, 3),
    ("layers_1_only",    1.0, 1.0, 0.0, 1),
    ("layers_1_widen_2x", 2.0, 2.0, 0.0, 1),
    ("layers_2_widen_2x", 2.0, 2.0, 0.0, 2),
]


def _trade_row(t, scope_label, cell_label, ti):
    return {
        "file": scope_label,
        "cell": cell_label,
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
        "pnl_usd": float(getattr(t, "pnl_usd", 0.0)),
        "fee_usd": float(getattr(t, "fee_usd", 0.0)),
        "taker_bps_charged": float(getattr(t, "taker_bps_charged", 0.0)),
        "entry_triggered_by": str(getattr(t, "entry_triggered_by", "")),
        "qty_btc": float(getattr(t, "qty_btc", 0.0)),
        "net_pnl_usd": float(getattr(t, "pnl_usd", 0.0))
                       - float(getattr(t, "fee_usd", 0.0)),
    }


def _ladder_split(trades):
    """Split trades by entry path. Returns (ladder_n, ladder_net, ladder_wr, ladder_ev).

    The 'ladder' path is the immediate-ladder submissions (entry_triggered_by
    in {'fvg','ifvg','orb','wyckoff','sweep'}); everything else is the
    sniper/INV clean path.
    """
    ladder_idx = [
        i for i, t in enumerate(trades)
        if t.entry_triggered_by in ("fvg", "ifvg", "orb", "wyckoff", "sweep")
    ]
    if not ladder_idx:
        return 0, 0.0, 0.0, 0.0
    nets = [
        float(getattr(trades[i], "pnl_usd", 0.0))
        - float(getattr(trades[i], "fee_usd", 0.0))
        for i in ladder_idx
    ]
    n = len(nets)
    wins = sum(1 for v in nets if v > 0)
    return n, sum(nets), 100.0 * wins / n, sum(nets) / n


def _header_line():
    return (f"{'cell':22s} | {'n':>4s} {'net':>9s} {'gross':>9s} {'fees':>6s} "
            f"{'WR':>5s} {'EV':>9s} | {'ladder_n':>7s} {'ladder_net':>10s} "
            f"{'ladder_WR':>9s} {'ladder_EV':>9s} | {'bt':>5s}")


def main():
    print(f"[{NOTEBOOK}] conviction-aware ladder sweep on {PATH.name}", flush=True)
    print(f"recipe version: {OPTIMAL_RECIPE_VERSION}\n", flush=True)

    # Build cache (warm hit likely since nb56 already ran on 2025-11).
    p_base = optimal_params(qty_btc=0.001)
    t0 = time.perf_counter()
    sc = get_or_build(PATH, p_base, with_side_table=True, verbose=False)
    t_build = time.perf_counter() - t0
    print(f"cache={t_build:.2f}s fvg={len(sc.fvg_zones)} ifvg={len(sc.ifvg_zones)}\n",
          flush=True)

    OUT_DIR = ROOT / "notebooks" / "nb57_ladder_conviction_outputs"
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    per_trade_rows = []
    summary_rows = []  # for the report CSV
    t_start = time.perf_counter()

    print(_header_line())
    print("-" * 145)

    for cell_label, sl_w, tp_w, cv_floor, n_max in LADDER_CELLS:
        ov = {
            "fvg_inv_trade_sl_zone_mult": SNIPER_SL_ZM,
            "fvg_inv_trade_tp_zone_mult": SNIPER_TP_ZM,
            "entry_mode": "dual",
            "ladder_conviction_sl_widen": sl_w,
            "ladder_conviction_tp_widen": tp_w,
            "ladder_min_conviction": cv_floor,
            "ladder_num_layers_max": n_max,
        }
        p_i = optimal_params(qty_btc=0.001, **ov)
        t0 = time.perf_counter()
        res = run_tick_backtest(
            raw_df=None, p=p_i,
            strategy_label=f"{NOTEBOOK}_{cell_label}",
            pre_aggregated_bars=sc.bars,
            precomputed_zones_by_src={"fvg": sc.fvg_zones, "ifvg": sc.ifvg_zones},
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

        ladder_n, ladder_net, ladder_wr, ladder_ev = _ladder_split(trades)

        print(f"{cell_label:22s} | {n:4d} ${sum(nets):+8.2f} ${sum(pnls):+8.2f} "
              f"${sum(fees):5.2f} {wr:4.1f}% ${ev:+8.4f} | {ladder_n:7d} "
              f"${ladder_net:+9.2f} {ladder_wr:8.1f}% ${ladder_ev:+8.4f} | "
              f"{t_bt:4.1f}s", flush=True)

        # Capture per-trade rows
        for ti, t in enumerate(trades):
            per_trade_rows.append(_trade_row(t, _ns.month, cell_label, ti))

        # Capture summary row
        summary_rows.append({
            "cell": cell_label,
            "sl_widen": sl_w,
            "tp_widen": tp_w,
            "min_conviction": cv_floor,
            "num_layers_max": n_max,
            "n_trades": n,
            "net_pnl_usd": sum(nets),
            "gross_pnl_usd": sum(pnls),
            "fees_usd": sum(fees),
            "win_rate_pct": wr,
            "ev_per_trade_usd": ev,
            "ladder_n": ladder_n,
            "ladder_net_usd": ladder_net,
            "ladder_wr_pct": ladder_wr,
            "ladder_ev_usd": ladder_ev,
            "backtest_secs": t_bt,
            "passed_success_bar": (ladder_wr >= 12.0 and ladder_ev >= -0.05),
        })

        # Append to JSONL run log
        from src.core.run_report import append_run_report
        append_run_report(
            notebook=NOTEBOOK,
            scenario="conviction_widen_ladder",
            scope=[_ns.month],
            engine="tick",
            comments=(f"ladder_conviction_sl_widen={sl_w}, "
                      f"ladder_conviction_tp_widen={tp_w}, "
                      f"ladder_min_conviction={cv_floor}, "
                      f"ladder_num_layers_max={n_max}. "
                      f"Sniper kept at 5x/60x zone-mult. "
                      f"Ladder WR={ladder_wr:.1f}% EV=${ladder_ev:+.4f}."),
            hypothesis=("Conviction-aware SL/TP widening lets high-conviction "
                        "ladder trades ride structural moves (wider SL, wider TP) "
                        "while low-conviction trades are unaffected."),
            verdict="promoted" if (ladder_wr >= 12.0 and ladder_ev >= -0.05) else "inconclusive",
            params=p_i,
            result=res,
            metrics_extra={
                "ladder_n": ladder_n,
                "ladder_net_usd": ladder_net,
                "ladder_wr_pct": ladder_wr,
                "ladder_ev_usd": ladder_ev,
                "sl_widen": sl_w,
                "tp_widen": tp_w,
                "min_conviction": cv_floor,
                "num_layers_max": n_max,
            },
            out_root=OUT_DIR.parent,
        )

    wall = time.perf_counter() - t_start
    print(f"\nwall={wall:.1f}s")

    # Per-trade CSV
    if per_trade_rows:
        df = pd.DataFrame(per_trade_rows)
        csv_path = OUT_DIR / f"{NOTEBOOK}_per_trade.csv"
        df.to_csv(csv_path, index=False)
        print(f"Wrote {csv_path} ({len(df)} rows)")

    # Summary CSV
    if summary_rows:
        sdf = pd.DataFrame(summary_rows)
        summary_path = OUT_DIR / f"{NOTEBOOK}_summary.csv"
        sdf.to_csv(summary_path, index=False)
        print(f"Wrote {summary_path} ({len(sdf)} rows)")

    # Best-cell callout
    if summary_rows:
        best = max(summary_rows, key=lambda r: (r["ladder_wr_pct"], r["ladder_ev_usd"]))
        print("\n=== Best ladder cell on this file ===")
        print(f"  cell             = {best['cell']}")
        print(f"  sl_widen / tp_widen / min_cv / n_max = "
              f"{best['sl_widen']} / {best['tp_widen']} / {best['min_conviction']} / {best['num_layers_max']}")
        print(f"  ladder_n         = {best['ladder_n']}")
        print(f"  ladder_net       = ${best['ladder_net_usd']:+.2f}")
        print(f"  ladder_WR        = {best['ladder_wr_pct']:.1f}%")
        print(f"  ladder_EV        = ${best['ladder_ev_usd']:+.4f}")
        print(f"  success bar met? = {best['passed_success_bar']}")


if __name__ == "__main__":
    main()
