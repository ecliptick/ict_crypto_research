"""nb54b — dump per-trade structural tags for the canonical recipe.

Reads each monthly file, runs the canonical recipe, and writes
each closed trade's structural context (BoS/CHoCH kind+age,
swing kind+age, sweep, trend alignment, etc.) to a sidecar
JSONL. This is the raw input for the structural-alpha fallback
analysis (bucket trades by structural tags, look for EV split).

Fast path: uses the existing SweepCache (canonical ms_* params),
so re-running this script after the sweep finishes takes ~30s/file
(canonical backtest only).
"""
import sys, json, time
sys.path.insert(0, 'c:/coding/ict_crypto_research')
sys.path.insert(0, 'c:/coding/ict_crypto_research/notebooks')
from pathlib import Path
from src.core.optimal_config import optimal_params
from src.tick.cache import get_or_build
from src.tick.tick_backtest import run_tick_backtest

# Reuse the tag-builder from nb54_vBTC3_ms_sweep.
sys.path.insert(0, 'c:/coding/ict_crypto_research/notebooks')
from nb54_vBTC3_ms_sweep import _per_trade_structural_tags

DEFAULT_SCOPE = [
    ('2025-04', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-04.parquet'),
    ('2025-05', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-05.parquet'),
    ('2025-10', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-10.parquet'),
    ('2025-11', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-11.parquet'),
    ('2026-02', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2026-02.parquet'),
    ('2026-05', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2026-05.parquet'),
]

OUT_DIR = Path('notebooks/nb54_vBTC3_ms_sweep_outputs')
OUT_DIR.mkdir(parents=True, exist_ok=True)


def main():
    p = optimal_params(qty_btc=0.01)
    print(f'Canonical recipe: {p}')
    grand_t0 = time.perf_counter()
    for month, raw_path in DEFAULT_SCOPE:
        raw = Path(raw_path)
        if not raw.exists():
            print(f'  SKIP {month}: {raw} not found')
            continue
        t0 = time.perf_counter()
        sc = get_or_build(raw, p, with_side_table=True, verbose=False)
        res = run_tick_backtest(
            None, p, strategy_label='canonical_tag_dump',
            pre_aggregated_bars=sc.bars,
            precomputed_zones_by_src={'fvg': sc.fvg_zones, 'ifvg': sc.ifvg_zones},
            precomputed_structure=sc.structure,
            precomputed_atr=sc.atr,
            side_table=sc.side_table,
        )
        tags = _per_trade_structural_tags(res, sc.structure)
        # Write to sidecar — one line per trade.
        out_path = OUT_DIR / f'nb54b_canonical_trade_tags__{month}.jsonl'
        with out_path.open('w') as f:
            for tg in tags:
                rec = dict(tg)
                rec['cell_label'] = 'A_baseline'
                rec['month'] = month
                f.write(json.dumps(rec) + '\n')
        # Summary
        pnls = [t['pnl_usd'] for t in tags]
        print(f'  [{month}] {len(tags)} trades, ${sum(pnls):+.2f} net, '
              f'{time.perf_counter()-t0:.1f}s wall, → {out_path.name}')
    print(f'\n=== canonical tag dump done. wall={time.perf_counter()-grand_t0:.1f}s ===')


if __name__ == '__main__':
    main()
