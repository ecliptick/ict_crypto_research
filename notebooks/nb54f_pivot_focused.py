"""nb54f — Focused pivot_len sweep on the 6-month sample.

Findings so far (2025-04 only, partial sweep):
  - A_baseline (pivot=9):                +$92.68
  - B_pivot_5:                           +$49.28
  - B_pivot_15:                          +$91.40
  - B_pivot_25:                          +$107.47  <-- winner

Pivot_len=25 looks like a clean alpha. Test it across the full
6-month sample. Also include pivot_len ∈ {20, 30, 40, 50} to
find the local optimum.

Pivot 30 / 40 / 50 will be slower (longer lookback = slower
detector) but the rebuild cost should still be sub-minute.
"""
import sys, time
sys.path.insert(0, 'c:/coding/ict_crypto_research')
sys.path.insert(0, 'c:/coding/ict_crypto_research/notebooks')
from pathlib import Path
from src.core.optimal_config import optimal_params, OPTIMAL_RECIPE_VERSION
from src.tick.cache import get_or_build
from src.tick.tick_backtest import run_tick_backtest
from src.core.run_report import append_run_report

NOTEBOOK = "nb54f_pivot_focused"
SCENARIO = "ms_pivot_focused_sweep"

DEFAULT_SCOPE = [
    ('2025-04', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-04.parquet'),
    ('2025-05', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-05.parquet'),
    ('2025-10', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-10.parquet'),
    ('2025-11', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-11.parquet'),
    ('2026-02', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2026-02.parquet'),
    ('2026-05', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2026-05.parquet'),
]

CELLS = [
    ('A_baseline',   {'ms_pivot_len': 9}),    # canonical
    ('P_pivot_15',   {'ms_pivot_len': 15}),
    ('P_pivot_20',   {'ms_pivot_len': 20}),
    ('P_pivot_25',   {'ms_pivot_len': 25}),
    ('P_pivot_30',   {'ms_pivot_len': 30}),
    ('P_pivot_40',   {'ms_pivot_len': 40}),
    ('P_pivot_50',   {'ms_pivot_len': 50}),
    ('P_pivot_75',   {'ms_pivot_len': 75}),
    ('P_pivot_100',  {'ms_pivot_len': 100}),
    ('P_pivot_150',  {'ms_pivot_len': 150}),
]

DEDUP_KEYS = ('ms_pivot_len', 'ms_liquidity_len', 'ms_resample_secs',
              'ms_boost_conviction', 'ms_min_conviction',
              'bos_choch_ignore_invert_when_aligned', 'use_market_structure')


def main():
    print(f'[{NOTEBOOK}] sweeping {len(CELLS)} cells × {len(DEFAULT_SCOPE)} files '
          f'({len(CELLS) * len(DEFAULT_SCOPE)} runs total)...', flush=True)
    grand_t0 = time.perf_counter()

    # Per-file cache pool.
    results_summary = {}  # (label, month) -> (n, pnl, wr)
    for month, raw_path in DEFAULT_SCOPE:
        raw = Path(raw_path)
        if not raw.exists():
            print(f'  WARN: {raw} not found, skipping {month}', flush=True)
            continue

        cache_pool = {}
        for j, (label, ov) in enumerate(CELLS):
            p_i = optimal_params(qty_btc=0.01, **ov)
            t0 = time.perf_counter()
            key = (p_i.ms_pivot_len, p_i.ms_liquidity_len, p_i.ms_resample_secs)
            if key not in cache_pool:
                print(f'  [{month}] {label:18s}: rebuilding SweepCache '
                      f'(pivot={key[0]}, liq={key[1]}, resample={key[2]})...',
                      flush=True)
                cache_pool[key] = get_or_build(raw, p_i, with_side_table=True, verbose=False)
            sc = cache_pool[key]
            res = run_tick_backtest(
                None, p_i, strategy_label=label,
                pre_aggregated_bars=sc.bars,
                precomputed_zones_by_src={'fvg': sc.fvg_zones, 'ifvg': sc.ifvg_zones},
                precomputed_structure=sc.structure,
                precomputed_atr=sc.atr,
                side_table=sc.side_table,
            )
            t_bt = time.perf_counter() - t0
            pnls = [float(t.pnl_usd) for t in res.trades]
            n = len(pnls); s = sum(pnls); ev = s/n if n else 0
            wr = 100 * sum(1 for p in pnls if p > 0) / n if n else 0
            print(f'  [{month}] {label:18s} pivot={p_i.ms_pivot_len:3d} | '
                  f'n={n:3d} net=${s:+8.2f} WR={wr:4.1f}% EV=${ev:+.4f} '
                  f'wall={t_bt:.1f}s', flush=True)
            results_summary[(label, month)] = (n, s, wr, ev)
            append_run_report(
                notebook=NOTEBOOK,
                scenario=SCENARIO,
                scope=[month],
                engine='tick',
                comments=(f'nb54f focused ms_pivot_len sweep on {month}: {label}. '
                          f'Overrides: {ov}.'),
                hypothesis='Pivot 25 won the 2025-04 single-file test; '
                            'does it generalize across 6 months?',
                verdict='inconclusive',
                params=p_i,
                result=res,
                overrides_vs_canonical=ov,
                canonical_recipe_version=OPTIMAL_RECIPE_VERSION,
                metrics_extra={'n_structural_tags': 0},
                dedup_keys=DEDUP_KEYS,
            )

    # Per-cell totals.
    print(f'\n=== Pivot sweep totals ===')
    print(f'  {"label":18s} {"n_total":>7s} {"sum_pnl":>10s} {"EV":>9s} {"WR%":>5s}')
    by_label = {}
    for (label, month), (n, s, wr, ev) in results_summary.items():
        if label not in by_label:
            by_label[label] = [0, 0.0, []]
        by_label[label][0] += n
        by_label[label][1] += s
        by_label[label][2].append(wr)
    items = []
    for label, (n_total, sum_pnl, wrs) in by_label.items():
        ev = sum_pnl / n_total if n_total else 0
        mean_wr = sum(wrs) / len(wrs) if wrs else 0
        items.append((label, n_total, sum_pnl, ev, mean_wr))
    items.sort(key=lambda x: -x[2])
    for label, n_total, sum_pnl, ev, mean_wr in items:
        print(f'  {label:18s} {n_total:7d} ${sum_pnl:+9.2f} ${ev:+.4f} {mean_wr:4.1f}')

    print(f'\n=== {NOTEBOOK} done. wall={time.perf_counter() - grand_t0:.1f}s ===', flush=True)


if __name__ == '__main__':
    main()
