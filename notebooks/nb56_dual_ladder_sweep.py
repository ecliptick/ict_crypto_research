"""NB56 follow-up — sparse ladder SL/TP sweep on a different month.

Goal: find SL/TP widths that make the immediate-ladder path
profitable when running in tandem with sniper (entry_mode='dual').

Diagnosis (from 2025-04 dual smoke):
  * Median ladder SL = $0.49, Median TP = $4.91, RR = 10:1
  * 60% of ladder trades close on SL within the SAME bar
  * 19% close on TP, 20% on INV — all exit reasons net-negative

The 10:1 RR isn't wide enough — even when the TP fires, the
gross PnL (lots × TP) is too small to beat fees + the noise
of being SL-hunted. Need to either tighten the SL (smaller
than typical 1s bar noise) or rethink the entry mechanic.

Sparse ladder SL/TP grid (3 modes × 3 combos × 1 month = 9 runs):
  * atr-anchored SL/TP: SL=0.05×ATR, TP=0.15×ATR (3:1 RR, ATR-anchored)
  * atr-anchored SL/TP: SL=0.10×ATR, TP=0.30×ATR (3:1 RR, ATR-anchored wider)
  * zone-anchored: SL=1.5×zone, TP=4.5×zone (3:1 RR, zone-anchored)
  * zone-anchored: SL=1.0×zone, TP=2.0×zone (2:1 RR, tight zone)
  * usd_fixed: SL=$1, TP=$3 (3:1 RR, absolute USD)

Each cell uses:
  * entry_mode = 'dual' (combined immediate + sniper)
  * The same sniper SL/TP (5× zone / 60× zone, the nb53 winner)
  * qty_btc = 0.001 (canonical default)
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

NOTEBOOK = "nb56_dual_ladder_sweep"

# Different month — pick 2025-11 (volatile month from nb53 results).
SOURCE_DATA_ROOT = Path(r"C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw")
# CLI override: --month 2025-04 to test on April too.
import argparse as _ap
_argp = _ap.ArgumentParser(add_help=False)
_argp.add_argument("--month", default="2025-11")
_ns, _rest = _argp.parse_known_args()
PATH = SOURCE_DATA_ROOT / f"BTCUSDT-aggTrades-{_ns.month}.parquet"

# Sniper SL/TP — keep at the nb53 winner.
SNIPER_SL_ZM = 5.0
SNIPER_TP_ZM = 60.0

# Sparse ladder SL/TP grid.
# Each tuple: (label, override_dict).
# Mode semantics (set via ``immediate_sl_mode`` / ``immediate_tp_mode``):
#   atr_mult: SL = sl_atr_mult * ATR, TP = tp_atr_mult * ATR
#     -> bar loop reads ``sl_atr_mult`` / ``tp_atr_mult`` from params.
#   zone_mult: SL = zone_w * immediate_sl_zone_mult,
#              TP = zone_w * immediate_tp_zone_mult
#     -> signal generator emits breadth-scaled SL/TP that the bar
#        loop now honours (post 2026-09-26 nb56 fix).
#   usd_fixed: SL = sl_usd, TP = tp_usd
#     -> signal generator emits the legacy fixed-USD values.
LADDER_CELLS = [
    # atr_mult cells (regime-adaptive, BTC-scale tight)
    ("atr_05_15",  {"immediate_sl_mode": "atr_mult",
                     "immediate_tp_mode": "atr_mult",
                     "sl_atr_mult": 0.05, "tp_atr_mult": 0.15}),
    ("atr_10_30",  {"immediate_sl_mode": "atr_mult",
                     "immediate_tp_mode": "atr_mult",
                     "sl_atr_mult": 0.10, "tp_atr_mult": 0.30}),
    ("atr_15_45",  {"immediate_sl_mode": "atr_mult",
                     "immediate_tp_mode": "atr_mult",
                     "sl_atr_mult": 0.15, "tp_atr_mult": 0.45}),
    # zone_mult cells (zone-anchored)
    ("zone_1x_3x",  {"immediate_sl_mode": "zone_mult", "immediate_sl_zone_mult": 1.0,
                     "immediate_tp_mode": "zone_mult", "immediate_tp_zone_mult": 3.0}),
    ("zone_1x_2x",  {"immediate_sl_mode": "zone_mult", "immediate_sl_zone_mult": 1.0,
                     "immediate_tp_mode": "zone_mult", "immediate_tp_zone_mult": 2.0}),
    ("zone_05_2x",  {"immediate_sl_mode": "zone_mult", "immediate_sl_zone_mult": 0.5,
                     "immediate_tp_mode": "zone_mult", "immediate_tp_zone_mult": 2.0}),
    # usd_fixed cells (absolute USD)
    ("usd_1_3",    {"immediate_sl_mode": "usd_fixed",
                     "immediate_tp_mode": "usd_fixed",
                     "use_atr_scaling": False, "sl_usd": 1.0, "tp_usd": 3.0}),
    ("usd_2_6",    {"immediate_sl_mode": "usd_fixed",
                     "immediate_tp_mode": "usd_fixed",
                     "use_atr_scaling": False, "sl_usd": 2.0, "tp_usd": 6.0}),
    # Baseline: dual-mode with canonical ladder (SL = 0.25×ATR, TP = 0.55×ATR)
    ("baseline",   {}),
]


def _trade_row(t, scope_label, cell_label) -> dict:
    return {
        "file": scope_label,
        "cell": cell_label,
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


def main():
    print(f"[{NOTEBOOK}] sparse ladder SL/TP sweep on {PATH.name}", flush=True)
    print(f"recipe version: {OPTIMAL_RECIPE_VERSION}\n", flush=True)

    # Build cache (warm hit likely, ~1-5s).
    p_base = optimal_params(qty_btc=0.001)
    t0 = time.perf_counter()
    sc = get_or_build(PATH, p_base, with_side_table=True, verbose=False)
    t_build = time.perf_counter() - t0
    print(f"cache={t_build:.2f}s fvg={len(sc.fvg_zones)} ifvg={len(sc.ifvg_zones)}\n", flush=True)

    OUT_DIR = ROOT / "notebooks" / "nb56_dual_entry_top3_outputs"
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    per_trade_rows = []

    print(f"{'cell':12s} | {'n':>4s} {'net':>9s} {'gross':>9s} {'fees':>6s} {'WR':>5s} {'EV':>9s} "
          f"| {'clean':>5s} {'ladder':>6s} | {'ladder_net':>9s} {'ladder_WR':>9s} {'ladder_EV':>9s} | {'bt':>5s}")
    print("-" * 130)

    for cell_label, cell_ov in LADDER_CELLS:
        ov = {
            "fvg_inv_trade_sl_zone_mult": SNIPER_SL_ZM,
            "fvg_inv_trade_tp_zone_mult": SNIPER_TP_ZM,
            "entry_mode": "dual",
            **cell_ov,
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
        paths = Counter(t.entry_triggered_by for t in trades)
        n_ifvg_clean = paths.get("ifvg_clean", 0)
        n_ladder = sum(v for k, v in paths.items() if k in ("fvg", "ifvg"))

        ladder_idx = [i for i, t in enumerate(trades)
                      if t.entry_triggered_by in ("fvg", "ifvg")]
        if ladder_idx:
            ladder_nets = [nets[i] for i in ladder_idx]
            ladder_n = len(ladder_idx)
            ladder_wr = 100.0 * sum(1 for v in ladder_nets if v > 0) / ladder_n
            ladder_ev = sum(ladder_nets) / ladder_n
            ladder_net = sum(ladder_nets)
        else:
            ladder_n = ladder_wr = ladder_ev = ladder_net = 0

        print(f"{cell_label:12s} | {n:4d} ${sum(nets):+8.2f} ${sum(pnls):+8.2f} ${sum(fees):5.2f} "
              f"{wr:4.1f}% ${ev:+8.4f} | {n_ifvg_clean:5d} {n_ladder:6d} "
              f"| ${ladder_net:+8.2f} {ladder_wr:8.1f}% ${ladder_ev:+8.4f} | {t_bt:4.1f}s",
              flush=True)

        for ti, t in enumerate(trades):
            row = _trade_row(t, "2025-11", cell_label)
            row["trade_idx"] = ti
            per_trade_rows.append(row)

    print()
    if per_trade_rows:
        df = pd.DataFrame(per_trade_rows)
        csv_path = OUT_DIR / "nb56_dual_ladder_sweep_per_trade.csv"
        df.to_csv(csv_path, index=False)
        print(f"Wrote {csv_path} ({len(df)} rows)", flush=True)


if __name__ == "__main__":
    main()
