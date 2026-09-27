"""vBTC2 SL/TP sweep on BTC 2025-04.

Recovers the historical baseline PnL (~$82 net on 62 trades, AGENTS.md
2026-09-25 update) by sweeping SL/TP zone-mult pairs on the current
canonical recipe. Designed to run FAST by sharing one SweepCache build
across all sweep configs — only the bar loop runs per (sl, tp) cell.

Setup:
  * Build the superset SweepCache once with the canonical recipe
    (body-only ON, strict-wick ON). Detector-side params are fixed.
  * For each (sl, tp), only the bar-loop geometry changes — same
    zones, same structure, same ATR. The bar loop is ~3s/config.

Output:
  * Per-cell trades are not persisted (sweep is exploratory).
  * One row per (sl, tp) is appended to
    ``nb52_vBTC2_sltp_sweep__runs.jsonl`` (AGENTS.md § Where).

Note:
  * Cache rebuild only happens once per file. Subsequent cells = <3s.
  * Use ``--n-cells N`` to clip the grid for fast smoke runs.
"""
import argparse, sys, time
sys.path.insert(0, 'c:/coding/ict_crypto_research')
sys.path.insert(0, 'c:/coding/ict_crypto_research/notebooks')
from pathlib import Path
from src.core.optimal_config import optimal_params, OPTIMAL_RECIPE_VERSION
from src.tick.cache import get_or_build
from src.tick.tick_backtest import run_tick_backtest
from src.core.run_report import append_run_report
from itertools import product

RAW = Path(r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-04.parquet')

# Grid: matches AGENTS.md "vBTC2 SL/TP sweep on BTC" guidance.
# SL ∈ {1, 1.5, 2, 3, 5} × TP ∈ {8, 15, 22, 30, 45} → 25 cells.
DEFAULT_SL_VALUES = [1.0, 1.5, 2.0, 3.0, 5.0]
DEFAULT_TP_VALUES = [8.0, 15.0, 22.0, 30.0, 45.0]
NOTEBOOK = "nb52_vBTC2_sltp_sweep"
SCENARIO = "sl_tp_zone_mult_sweep"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sl", type=float, nargs='+', default=DEFAULT_SL_VALUES,
                   help="SL zone-mult values to sweep.")
    p.add_argument("--tp", type=float, nargs='+', default=DEFAULT_TP_VALUES,
                   help="TP zone-mult values to sweep.")
    p.add_argument("--n-cells", type=int, default=None,
                   help="Cap the number of cells (for smoke tests).")
    p.add_argument("--file", type=str, default=str(RAW),
                   help="Path to a monthly aggTrades parquet.")
    p.add_argument("--month", type=str, default="2025-04",
                   help="Label for the run scope (used in comments + scope[]).")
    p.add_argument("--no-write", action="store_true",
                   help="Skip writing the JSONL (useful for warm-cache runs).")
    args = p.parse_args()

    raw = Path(args.file)
    month = args.month

    # ── Build the shared superset SweepCache ONCE ─────────────
    # Canonical detector params (body-only ON, strict-wick ON).
    # SL/TP zone-width overrides are NOT in the cache fingerprint
    # — they live downstream in the bar loop.
    p_base = optimal_params(qty_btc=0.01)
    print(f"[{NOTEBOOK}] building superset SweepCache for {raw.name}...", flush=True)
    t0 = time.perf_counter()
    sc = get_or_build(raw, p_base, with_side_table=True, verbose=False)
    t_build = time.perf_counter() - t0
    print(f"  cache={t_build:.2f}s fvg={len(sc.fvg_zones)} ifvg={len(sc.ifvg_zones)}", flush=True)

    cells = list(product(args.sl, args.tp))
    if args.n_cells is not None:
        cells = cells[:args.n_cells]
    print(f"[{NOTEBOOK}] sweeping {len(cells)} (sl, tp) cells on {month}...", flush=True)
    summary = []
    for i, (sl, tp) in enumerate(cells):
        p_i = optimal_params(qty_btc=0.01,
                             fvg_inv_trade_sl_zone_mult=sl,
                             fvg_inv_trade_tp_zone_mult=tp)
        t0 = time.perf_counter()
        res = run_tick_backtest(
            None, p_i, strategy_label=f"sl={sl}_tp={tp}",
            pre_aggregated_bars=sc.bars,
            precomputed_zones_by_src={'fvg': sc.fvg_zones, 'ifvg': sc.ifvg_zones},
            precomputed_structure=sc.structure,
            precomputed_atr=sc.atr,
            side_table=sc.side_table,
        )
        t_bt = time.perf_counter() - t0
        pnls_net = [float(t.pnl_usd) for t in res.trades]
        fees = [float(t.fee_usd) for t in res.trades]
        sum_net = sum(pnls_net)
        sum_gross = sum(pnls_net) + sum(fees)
        n = len(pnls_net)
        ev = sum_net / n if n else 0.0
        ev_gross = sum_gross / n if n else 0.0
        wr = (sum(1 for v in pnls_net if v > 0) / n * 100) if n else 0.0
        cell_str = f"sl={sl:>4.1f} tp={tp:>5.1f}"
        print(f"  [{i+1:2d}/{len(cells)}] {cell_str} | n={n:3d} net=${sum_net:+7.2f} gross=${sum_gross:+7.2f} WR={wr:4.1f}% EV_net=${ev:+.3f} bt={t_bt:.1f}s", flush=True)
        summary.append({
            "sl_mult": sl, "tp_mult": tp,
            "n_trades": n, "pnl_net_usd": sum_net,
            "pnl_gross_usd": sum_gross, "ev_net_usd": ev,
            "ev_gross_usd": ev_gross, "wr_pct": wr,
            "t_backtest_s": t_bt,
        })
        if not args.no_write:
            append_run_report(
                notebook=NOTEBOOK,
                scenario=SCENARIO,
                scope=[month],
                engine="tick",
                comments=(f"vBTC2 SL/TP sweep on {month}: "
                          f"fvg_inv_trade_sl_zone_mult={sl}, "
                          f"fvg_inv_trade_tp_zone_mult={tp}. "
                          f"qty_btc=0.01."),
                hypothesis=(f"(sl={sl}, tp={tp}) recovers the pre-26c "
                            f"baseline PnL of ~$82 net."),
                verdict="inconclusive",
                params=p_i,
                result=res,
                overrides_vs_canonical={
                    "fvg_inv_trade_sl_zone_mult": sl,
                    "fvg_inv_trade_tp_zone_mult": tp,
                },
                canonical_recipe_version=OPTIMAL_RECIPE_VERSION,
            )

    # Final summary
    print()
    print(f"=== {NOTEBOOK} summary ({month}, qty=0.01, canonical detector) ===")
    summary_sorted = sorted(summary, key=lambda r: r["pnl_net_usd"], reverse=True)
    for r in summary_sorted[:5]:
        print(f"  TOP: sl={r['sl_mult']:>4.1f} tp={r['tp_mult']:>5.1f} | n={r['n_trades']:3d} "
              f"net=${r['pnl_net_usd']:+7.2f} gross=${r['pnl_gross_usd']:+7.2f} "
              f"WR={r['wr_pct']:4.1f}% EV_net=${r['ev_net_usd']:+.3f}")
    print()
    print(f"  Bottom:")
    for r in summary_sorted[-3:]:
        print(f"  BOT: sl={r['sl_mult']:>4.1f} tp={r['tp_mult']:>5.1f} | n={r['n_trades']:3d} "
              f"net=${r['pnl_net_usd']:+7.2f} gross=${r['pnl_gross_usd']:+7.2f} "
              f"WR={r['wr_pct']:4.1f}% EV_net=${r['ev_net_usd']:+.3f}")


if __name__ == "__main__":
    main()
