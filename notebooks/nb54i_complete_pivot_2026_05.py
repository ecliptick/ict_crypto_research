"""nb54i — complete the missing 2026-05 cells for pivot {50, 75, 100, 150}."""
import sys, time
sys.path.insert(0, 'c:/coding/ict_crypto_research')
sys.path.insert(0, 'c:/coding/ict_crypto_research/notebooks')
from pathlib import Path

from src.core.optimal_config import optimal_params, OPTIMAL_RECIPE_VERSION
from src.tick.cache import get_or_build
from src.tick.tick_backtest import run_tick_backtest
from src.core.run_report import append_run_report

NOTEBOOK = "nb54i_pivot_2026_05_complete"
SCENARIO = "ms_pivot_2026_05_completion"

CELLS = [
    ('P_pivot_50',  {'ms_pivot_len': 50}),
    ('P_pivot_75',  {'ms_pivot_len': 75}),
    ('P_pivot_100', {'ms_pivot_len': 100}),
    ('P_pivot_150', {'ms_pivot_len': 150}),
]
RAW_PATH = Path(r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2026-05.parquet')
MONTH = '2026-05'

DEDUP_KEYS = ('ms_pivot_len', 'ms_liquidity_len', 'ms_resample_secs',
              'fvg_min_zone_usd', 'ifvg_min_zone_usd', 'fvg_inv_trade_min_zone_usd')

for cell_name, overrides in CELLS:
    p = optimal_params(qty_btc=0.01, **overrides)
    t0 = time.perf_counter()
    sc = get_or_build(RAW_PATH, p, with_side_table=True, verbose=False)
    cache_time = time.perf_counter() - t0
    t0 = time.perf_counter()
    res = run_tick_backtest(None, p, strategy_label=f"{cell_name}_{MONTH}",
                              pre_aggregated_bars=sc.bars,
                              precomputed_zones_by_src={'fvg': sc.fvg_zones, 'ifvg': sc.ifvg_zones},
                              precomputed_structure=sc.structure, precomputed_atr=sc.atr,
                              side_table=sc.side_table)
    bt_time = time.perf_counter() - t0
    n = len(res.trades)
    s = sum(float(t.pnl_usd) for t in res.trades)
    ev = s/n if n else 0
    wr = 100*sum(1 for t in res.trades if t.pnl_usd>0)/n if n else 0
    print(f"[{MONTH}] {cell_name:14s} cache={cache_time:5.1f}s bt={bt_time:4.1f}s n={n:3d} sum=${s:+8.2f} EV=${ev:+.4f} WR={wr:4.1f}%")
    append_run_report(
        notebook=NOTEBOOK, scenario=SCENARIO,
        scope=[MONTH], engine='tick',
        comments=f'{cell_name} on {MONTH}.',
        hypothesis='Complete missing 2026-05 cells from killed pivot sweep.',
        verdict='inconclusive',
        params=p, result=res,
        overrides_vs_canonical=overrides,
        out_root=Path(f"notebooks/{NOTEBOOK}_outputs"),
        dedup_keys=list(DEDUP_KEYS),
    )
print('\nDone.')
