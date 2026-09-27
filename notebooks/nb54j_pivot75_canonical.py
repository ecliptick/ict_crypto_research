"""Validate pivot=75 on the new canonical (qty=0.01) to compare with pivot=50."""
import sys, time
sys.path.insert(0, 'c:/coding/ict_crypto_research')
sys.path.insert(0, 'c:/coding/ict_crypto_research/notebooks')
from pathlib import Path

from src.core.optimal_config import optimal_params, OPTIMAL_RECIPE_VERSION
from src.tick.cache import get_or_build
from src.tick.tick_backtest import run_tick_backtest
from src.core.run_report import append_run_report

NOTEBOOK = "nb54j_pivot75_canonical"
SCENARIO = "ms_pivot75_canonical_validation"

SCOPE = [
    ('2025-04', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-04.parquet'),
    ('2025-05', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-05.parquet'),
    ('2025-10', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-10.parquet'),
    ('2025-11', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-11.parquet'),
    ('2026-02', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2026-02.parquet'),
    ('2026-05', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2026-05.parquet'),
]

# pivot=75 only on the new canonical
OVERRIDES = {'ms_pivot_len': 75}

DEDUP_KEYS = ('ms_pivot_len', 'ms_liquidity_len', 'ms_resample_secs',
              'fvg_min_zone_usd', 'ifvg_min_zone_usd', 'fvg_inv_trade_min_zone_usd')

print(f'recipe: {OPTIMAL_RECIPE_VERSION}')
for month, raw_path in SCOPE:
    if not Path(raw_path).exists():
        print(f'  [{month}] SKIP (file not found)')
        continue
    raw = Path(raw_path)
    p = optimal_params(qty_btc=0.01, **OVERRIDES)
    t0 = time.perf_counter()
    sc = get_or_build(raw, p, with_side_table=True, verbose=False)
    cache_time = time.perf_counter() - t0
    t0 = time.perf_counter()
    res = run_tick_backtest(None, p, strategy_label=f'pivot75_{month}',
                              pre_aggregated_bars=sc.bars,
                              precomputed_zones_by_src={'fvg': sc.fvg_zones, 'ifvg': sc.ifvg_zones},
                              precomputed_structure=sc.structure, precomputed_atr=sc.atr,
                              side_table=sc.side_table)
    bt_time = time.perf_counter() - t0
    n = len(res.trades)
    s = sum(float(t.pnl_usd) for t in res.trades)
    ev = s/n if n else 0
    wr = 100*sum(1 for t in res.trades if t.pnl_usd>0)/n if n else 0
    print(f'  [{month}] cache={cache_time:5.1f}s bt={bt_time:4.1f}s n={n:3d} sum=${s:+8.2f} EV=${ev:+.4f} WR={wr:4.1f}%')
    append_run_report(
        notebook=NOTEBOOK, scenario=SCENARIO,
        scope=[month], engine='tick',
        comments=f'pivot75 on {month} — validating pivot=75 on new canonical.',
        hypothesis='Pivot=75 won 6mo sweep under OLD canonical (+$784 vs pivot=9\'s +$624). Test on NEW canonical.',
        verdict='inconclusive',
        params=p, result=res,
        overrides_vs_canonical=OVERRIDES,
        out_root=Path(f"notebooks/{NOTEBOOK}_outputs"),
        dedup_keys=list(DEDUP_KEYS),
    )
print('\nDone.')
