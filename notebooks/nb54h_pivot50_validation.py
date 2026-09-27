"""Validate the new canonical (ms_pivot_len=50) vs old canonical (pivot=9)
across the 6-month sample using the new fast detector.
"""
import sys, time
sys.path.insert(0, 'c:/coding/ict_crypto_research')
sys.path.insert(0, 'c:/coding/ict_crypto_research/notebooks')
from pathlib import Path

from src.core.optimal_config import optimal_params, OPTIMAL_RECIPE_VERSION
from src.tick.cache import get_or_build
from src.tick.tick_backtest import run_tick_backtest
from src.core.run_report import append_run_report

NOTEBOOK = "nb54h_pivot50_validation"
SCENARIO = "ms_pivot_canonical_validation"

SCOPE = [
    ('2025-04', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-04.parquet'),
    ('2025-05', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-05.parquet'),
    ('2025-10', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-10.parquet'),
    ('2025-11', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-11.parquet'),
    ('2026-02', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2026-02.parquet'),
    ('2026-05', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2026-05.parquet'),
]

# Use just pivot=9 (override) vs canonical pivot=50
CELLS = [
    ('A_pivot_9_override',  {'ms_pivot_len': 9}),
    ('B_pivot_50_new_canon', {}),  # use canonical pivot=50
]

DEDUP_KEYS = ('ms_pivot_len', 'ms_liquidity_len', 'ms_resample_secs',
              'fvg_min_zone_usd', 'ifvg_min_zone_usd', 'fvg_inv_trade_min_zone_usd')

OUT_DIR = Path(f"notebooks/{NOTEBOOK}_outputs")
OUT_DIR.mkdir(parents=True, exist_ok=True)


def run_cell(cell_name, overrides):
    print(f"\n=== {cell_name} ===")
    for month, raw_path in SCOPE:
        if not Path(raw_path).exists():
            print(f"  [{month}] SKIP (file not found: {raw_path})")
            continue
        raw = Path(raw_path)
        p = optimal_params(qty_btc=0.01, **overrides)
        t0 = time.perf_counter()
        sc = get_or_build(raw, p, with_side_table=True, verbose=False)
        cache_time = time.perf_counter() - t0
        t0 = time.perf_counter()
        res = run_tick_backtest(None, p, strategy_label=f"{cell_name}_{month}",
                                  pre_aggregated_bars=sc.bars,
                                  precomputed_zones_by_src={'fvg': sc.fvg_zones, 'ifvg': sc.ifvg_zones},
                                  precomputed_structure=sc.structure, precomputed_atr=sc.atr,
                                  side_table=sc.side_table)
        bt_time = time.perf_counter() - t0
        n = len(res.trades)
        s = sum(float(t.pnl_usd) for t in res.trades)
        ev = s/n if n else 0
        wr = 100*sum(1 for t in res.trades if t.pnl_usd>0)/n if n else 0
        print(f"  [{month}] cache={cache_time:.1f}s bt={bt_time:.1f}s n={n} sum=${s:+.2f} EV=${ev:+.4f} WR={wr:.1f}%")
        # Persist
        append_run_report(
            notebook=NOTEBOOK, scenario=SCENARIO,
            scope=[month], engine='tick',
            comments=f'{cell_name} on {month}.',
            hypothesis='Validate ms_pivot_len=50 as new canonical (5mo partial sweep showed +$107 vs pivot=9).',
            verdict='inconclusive',
            params=p, result=res,
            out_root=Path(f"notebooks/{NOTEBOOK}_outputs"),
            dedup_keys=list(DEDUP_KEYS),
        )


if __name__ == '__main__':
    print(f'recipe: {OPTIMAL_RECIPE_VERSION}')
    for cell_name, overrides in CELLS:
        run_cell(cell_name, overrides)
    print('\nDone.')
