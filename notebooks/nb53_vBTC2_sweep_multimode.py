"""vBTC2 multi-month sweep with mode variants (zone_mult / atr_mult / usd_fixed).

Builds on `nb52_vBTC2_sltp_sweep.py` (single-file / 25-cell grid) by:

1. Running across the same 6 monthly files the parent vBTC1 used
   (the nb52 sample), so a cell that wins on one month has to win
   on the other five too.
2. Replacing the dense 5x5 grid with a curated ~20-cell set that
   covers:
   - A — confirmed cells from the single-month 25-cell sweep (5x45,
     1.5x45, 1.5x22, 2x22)
   - B — sparse neighbours of the best (1.5, 45) and (5, 45)
   - C — atr-mult TP variants (the user's "wide zone can't reach TP"
     question)
   - D — usd_fixed variants (the user's "non-sniper counterpart" ask
     — same {zone_mult | atr_mult | usd_fixed} selector on the
     immediate-mode path)
3. Logging via the canonical `run_report.append_run_report` helper,
   which has a `dedup_keys=` option now — re-runs of the same cell
   overwrite the prior row, so the JSONL stays clean.

Run modes:
    python notebooks/nb53_vBTC2_sweep_multimode.py        # full 6-month sweep
    python notebooks/nb53_vBTC2_sweep_multimode.py --fast  # single-file smoke

Output:
    notebooks/nb53_vBTC2_sweep_multimode_outputs/nb53_vBTC2_sweep_multimode__runs.jsonl
"""
import argparse, sys, time
sys.path.insert(0, 'c:/coding/ict_crypto_research')
sys.path.insert(0, 'c:/coding/ict_crypto_research/notebooks')
from pathlib import Path
from src.core.optimal_config import optimal_params, OPTIMAL_RECIPE_VERSION
from src.tick.cache import get_or_build
from src.tick.tick_backtest import run_tick_backtest
from src.core.run_report import append_run_report

NOTEBOOK = "nb53_vBTC2_sweep_multimode"
SCENARIO = "sl_tp_mode_multimonth"

# 6-month sample (same seed=20260925 used in nb52 — see AGENTS.md).
DEFAULT_SCOPE = [
    ('2025-04', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-04.parquet'),
    ('2025-05', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-05.parquet'),
    ('2025-10', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-10.parquet'),
    ('2025-11', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-11.parquet'),
    ('2026-02', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2026-02.parquet'),
    ('2026-05', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2026-05.parquet'),
]

# Cell grid.  Each entry is a tuple of (label, kwargs_for_optimal_params).
# Modes: zone_mult (zone-anchored via fvg_inv_trade_*_zone_mult),
#        atr_mult (regime-adaptive via fvg_inv_trade_*_atr_*),
#        usd_fixed (fixed-USD via sl_usd / tp_usd).
# Both the sniper path and the immediate path honour the sniper_*
# knobs; the immediate_* knobs are set explicitly to mirror them
# so the non-sniper path gets the same selector treatment.
def _build_cells():
    cells = []
    # Group A — confirmed single-month winners.
    cells.append(('A_canon_5x45',   {'fvg_inv_trade_sl_zone_mult': 5.0,  'fvg_inv_trade_tp_zone_mult': 45.0,}))
    cells.append(('A_best_1.5x45',  {'fvg_inv_trade_sl_zone_mult': 1.5,  'fvg_inv_trade_tp_zone_mult': 45.0,}))
    cells.append(('A_orig_1.5x22',  {'fvg_inv_trade_sl_zone_mult': 1.5,  'fvg_inv_trade_tp_zone_mult': 22.0,}))
    cells.append(('A_agmd_2x22',    {'fvg_inv_trade_sl_zone_mult': 2.0,  'fvg_inv_trade_tp_zone_mult': 22.0,}))
    # Group B — sparse neighbours of the two best cells.
    cells.append(('B_n_1.5x50',     {'fvg_inv_trade_sl_zone_mult': 1.5,  'fvg_inv_trade_tp_zone_mult': 50.0,}))
    cells.append(('B_n_1.5x60',     {'fvg_inv_trade_sl_zone_mult': 1.5,  'fvg_inv_trade_tp_zone_mult': 60.0,}))
    cells.append(('B_n_1.5x35',     {'fvg_inv_trade_sl_zone_mult': 1.5,  'fvg_inv_trade_tp_zone_mult': 35.0,}))
    cells.append(('B_n_1.0x45',     {'fvg_inv_trade_sl_zone_mult': 1.0,  'fvg_inv_trade_tp_zone_mult': 45.0,}))
    cells.append(('B_n_2.5x45',     {'fvg_inv_trade_sl_zone_mult': 2.5,  'fvg_inv_trade_tp_zone_mult': 45.0,}))
    cells.append(('B_n_5x50',       {'fvg_inv_trade_sl_zone_mult': 5.0,  'fvg_inv_trade_tp_zone_mult': 50.0,}))
    cells.append(('B_n_5x60',       {'fvg_inv_trade_sl_zone_mult': 5.0,  'fvg_inv_trade_tp_zone_mult': 60.0,}))
    cells.append(('B_n_4x45',       {'fvg_inv_trade_sl_zone_mult': 4.0,  'fvg_inv_trade_tp_zone_mult': 45.0,}))
    cells.append(('B_n_7x45',       {'fvg_inv_trade_sl_zone_mult': 7.0,  'fvg_inv_trade_tp_zone_mult': 45.0,}))
    # Group C — atr-mult TP variants (user's "wide zone can't reach TP").
    cells.append(('C_tp_atr_22',    {'fvg_inv_trade_sl_zone_mult': 5.0,
                                     'fvg_inv_trade_tp_zone_mult': 45.0,
                                     'sniper_tp_mode': 'atr_mult',
                                     'fvg_inv_trade_tp_atr_mult_v2': 22.0,
                                     'immediate_tp_mode': 'atr_mult',
                                     'tp_atr_mult': 22.0,}))
    cells.append(('C_tp_atr_2.0',   {'fvg_inv_trade_sl_zone_mult': 5.0,
                                     'fvg_inv_trade_tp_zone_mult': 45.0,
                                     'sniper_tp_mode': 'atr_mult',
                                     'fvg_inv_trade_tp_atr_mult_v2': 2.0,
                                     'immediate_tp_mode': 'atr_mult',
                                     'tp_atr_mult': 2.0,}))
    cells.append(('C_tp_atr_4.0',   {'fvg_inv_trade_sl_zone_mult': 5.0,
                                     'fvg_inv_trade_tp_zone_mult': 45.0,
                                     'sniper_tp_mode': 'atr_mult',
                                     'fvg_inv_trade_tp_atr_mult_v2': 4.0,
                                     'immediate_tp_mode': 'atr_mult',
                                     'tp_atr_mult': 4.0,}))
    cells.append(('C_tp_atr_0.55',  {'fvg_inv_trade_sl_zone_mult': 5.0,
                                     'fvg_inv_trade_tp_zone_mult': 45.0,
                                     'sniper_tp_mode': 'atr_mult',
                                     'fvg_inv_trade_tp_atr_mult_v2': 0.55,
                                     'immediate_tp_mode': 'atr_mult',
                                     'tp_atr_mult': 0.55,}))
    # Group D — usd-fixed SL/TP (user's "non-sniper counterpart" ask).
    cells.append(('D_usd_200_500',  {'sniper_sl_mode': 'usd_fixed', 'sniper_tp_mode': 'usd_fixed',
                                     'immediate_sl_mode': 'usd_fixed', 'immediate_tp_mode': 'usd_fixed',
                                     'sl_usd': 200.0, 'tp_usd': 500.0,
                                     'fvg_inv_trade_sl_zone_mult': 5.0, 'fvg_inv_trade_tp_zone_mult': 45.0,}))
    cells.append(('D_usd_500_1000', {'sniper_sl_mode': 'usd_fixed', 'sniper_tp_mode': 'usd_fixed',
                                     'immediate_sl_mode': 'usd_fixed', 'immediate_tp_mode': 'usd_fixed',
                                     'sl_usd': 500.0, 'tp_usd': 1000.0,
                                     'fvg_inv_trade_sl_zone_mult': 5.0, 'fvg_inv_trade_tp_zone_mult': 45.0,}))
    cells.append(('D_usd_1000_2000',{'sniper_sl_mode': 'usd_fixed', 'sniper_tp_mode': 'usd_fixed',
                                     'immediate_sl_mode': 'usd_fixed', 'immediate_tp_mode': 'usd_fixed',
                                     'sl_usd': 1000.0, 'tp_usd': 2000.0,
                                     'fvg_inv_trade_sl_zone_mult': 5.0, 'fvg_inv_trade_tp_zone_mult': 45.0,}))
    return cells

CELLS = _build_cells()
DEDUP_KEYS = (
    'fvg_inv_trade_sl_zone_mult',
    'fvg_inv_trade_tp_zone_mult',
    'sniper_sl_mode',
    'sniper_tp_mode',
    'immediate_sl_mode',
    'immediate_tp_mode',
    'sl_usd',
    'tp_usd',
    'fvg_inv_trade_sl_atr_mult',
    'fvg_inv_trade_tp_atr_mult_v2',
    'tp_atr_mult',
)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fast", action="store_true",
                   help="Run on a single file (2025-04) for smoke test.")
    p.add_argument("--no-write", action="store_true",
                   help="Skip writing the JSONL.")
    p.add_argument("--cells", type=int, default=None,
                   help="Clip the cell count for hand-test runs.")
    p.add_argument("--cells-only", type=str, default=None,
                   help="Comma-separated cell-label substring filter.")
    args = p.parse_args()

    scope = DEFAULT_SCOPE
    if args.fast:
        scope = DEFAULT_SCOPE[:1]
    cells = list(CELLS)
    if args.cells is not None:
        cells = cells[:args.cells]
    if args.cells_only:
        filt = [s.strip() for s in args.cells_only.split(',')]
        cells = [c for c in cells if any(f in c[0] for f in filt)]

    print(f"[{NOTEBOOK}] sweeping {len(cells)} cells × {len(scope)} files "
          f"({sum(1 for _ in cells) * len(scope)} runs total)...", flush=True)

    grand_t0 = time.perf_counter()
    for month, raw_path in scope:
        raw = Path(raw_path)
        if not raw.exists():
            print(f"  WARN: {raw} not found, skipping {month}", flush=True)
            continue

        # ── Build the shared superset SweepCache ONCE per file ─────
        # We use the canonical mode selectors as the cache key.
        p_base = optimal_params(qty_btc=0.01)
        # Drop the mode overrides from the canonical so we share
        # the superset cache across all cells (mode overrides only
        # live in the bar loop, not the detector).
        if 'sniper_sl_mode' in p_base.__dict__:
            p_base = optimal_params(qty_btc=0.01,
                                    sniper_sl_mode='zone_mult',
                                    sniper_tp_mode='zone_mult',
                                    immediate_sl_mode='',
                                    immediate_tp_mode='')
        print(f"\n[{NOTEBOOK}] {month}: building superset SweepCache "
              f"({raw.name})...", flush=True)
        t0 = time.perf_counter()
        sc = get_or_build(raw, p_base, with_side_table=True, verbose=False)
        t_build = time.perf_counter() - t0
        print(f"  cache={t_build:.2f}s fvg={len(sc.fvg_zones)} ifvg={len(sc.ifvg_zones)}",
              flush=True)

        for j, (label, ov) in enumerate(cells):
            p_i = optimal_params(qty_btc=0.01, **ov)
            t0 = time.perf_counter()
            res = run_tick_backtest(
                None, p_i, strategy_label=label,
                pre_aggregated_bars=sc.bars,
                precomputed_zones_by_src={'fvg': sc.fvg_zones, 'ifvg': sc.ifvg_zones},
                precomputed_structure=sc.structure,
                precomputed_atr=sc.atr,
                side_table=sc.side_table,
            )
            t_bt = time.perf_counter() - t0
            pnls_net = [float(t.pnl_usd) for t in res.trades]
            fees = [float(getattr(t, 'fee_usd', 0.0)) for t in res.trades]
            sum_net = sum(pnls_net)
            sum_gross = sum_net + sum(fees)
            n = len(pnls_net)
            ev = sum_net / n if n else 0.0
            wr = (sum(1 for v in pnls_net if v > 0) / n * 100) if n else 0.0
            print(f"  [{month}] {label:18s} | n={n:3d} net=${sum_net:+8.2f} "
                  f"WR={wr:4.1f}% EV=${ev:+.4f} bt={t_bt:.1f}s", flush=True)
            if not args.no_write:
                append_run_report(
                    notebook=NOTEBOOK,
                    scenario=SCENARIO,
                    scope=[month],
                    engine="tick",
                    comments=(f"vBTC2 multi-mode sweep on {month}: {label}. "
                              f"Overrides: {ov}."),
                    hypothesis="Multi-month validation of vBTC2 SL/TP sweep "
                                "with all three mode regimes.",
                    verdict="inconclusive",
                    params=p_i,
                    result=res,
                    overrides_vs_canonical=ov,
                    canonical_recipe_version=OPTIMAL_RECIPE_VERSION,
                    dedup_keys=DEDUP_KEYS,
                )

    print(f"\n=== {NOTEBOOK} done. wall={time.perf_counter() - grand_t0:.1f}s ===",
          flush=True)


if __name__ == "__main__":
    main()
