"""nb54g — Trend-alignment gate ON vs OFF on the current canonical.

Tests the new gate_trend_aligned knob on the 6-month sample.
Single-knob change: gate_trend_aligned ∈ {False, True}.

Expected:
  gate_off:  canonical baseline (current recipe, 2025-04 saw +$113)
  gate_on:   drop counter-trend signals at submit time

Concern: the prior post-trade bucket analysis predicted the
filter would RAISE per-trade EV but LOWER total PnL (because
it filters out trades that are net-positive even when they're
counter-trend). The actual run will validate.
"""
import sys, time
sys.path.insert(0, 'c:/coding/ict_crypto_research')
sys.path.insert(0, 'c:/coding/ict_crypto_research/notebooks')
from pathlib import Path
from src.core.optimal_config import optimal_params, OPTIMAL_RECIPE_VERSION
from src.tick.cache import get_or_build
from src.tick.tick_backtest import run_tick_backtest
from src.core.run_report import append_run_report

NOTEBOOK = "nb54g_trend_gate"
SCENARIO = "trend_alignment_gate_sweep"

DEFAULT_SCOPE = [
    ('2025-04', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-04.parquet'),
    ('2025-05', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-05.parquet'),
    ('2025-10', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-10.parquet'),
    ('2025-11', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-11.parquet'),
    ('2026-02', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2026-02.parquet'),
    ('2026-05', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2026-05.parquet'),
]

CELLS = [
    ('T_gate_off', {}),  # baseline
    ('T_gate_on',  {'gate_trend_aligned': True}),
]

DEDUP_KEYS = ('gate_trend_aligned', 'ms_pivot_len', 'ms_liquidity_len',
              'ms_resample_secs')


def main():
    print(f'[{NOTEBOOK}] sweeping {len(CELLS)} cells × {len(DEFAULT_SCOPE)} files '
          f'({len(CELLS) * len(DEFAULT_SCOPE)} runs total)...', flush=True)
    grand_t0 = time.perf_counter()

    by_label = {}
    for month, raw_path in DEFAULT_SCOPE:
        raw = Path(raw_path)
        if not raw.exists():
            print(f'  WARN: {raw} not found, skipping {month}', flush=True)
            continue
        cache_pool = {}
        for label, ov in CELLS:
            p_i = optimal_params(qty_btc=0.01, **ov)
            t0 = time.perf_counter()
            sc = get_or_build(raw, p_i, with_side_table=True, verbose=False)
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
            print(f'  [{month}] {label:12s} | n={n:3d} sum=${s:+8.2f} '
                  f'WR={wr:4.1f}% EV=${ev:+.4f} wall={t_bt:.1f}s', flush=True)
            by_label.setdefault(label, []).append((month, n, s, wr, ev))
            append_run_report(
                notebook=NOTEBOOK,
                scenario=SCENARIO,
                scope=[month],
                engine='tick',
                comments=(f'nb54g trend-alignment gate sweep on {month}: {label}. '
                          f'Overrides: {ov}.'),
                hypothesis='Trend-aligned signals win more often (WR 20.7% vs 15.9% '
                            'in post-trade bucket analysis); does gating at submit time '
                            'replicate the lift without sacrificing total PnL?',
                verdict='inconclusive',
                params=p_i,
                result=res,
                overrides_vs_canonical=ov,
                canonical_recipe_version=OPTIMAL_RECIPE_VERSION,
                metrics_extra={'n_structural_tags': 0},
                dedup_keys=DEDUP_KEYS,
            )

    print(f'\n=== Trend gate totals ===')
    print(f'  {"label":18s} {"n":>4s} {"sum_pnl":>10s} {"EV":>9s} {"WR%":>5s}')
    for label, items in by_label.items():
        n_total = sum(n for _, n, _, _, _ in items)
        s_total = sum(s for _, _, s, _, _ in items)
        ev_total = s_total / n_total if n_total else 0
        wr_avg = sum(wr for _, _, _, wr, _ in items) / len(items)
        print(f'  {label:18s} {n_total:4d} ${s_total:+9.2f} ${ev_total:+.4f} {wr_avg:4.1f}')

    print(f'\n=== {NOTEBOOK} done. wall={time.perf_counter() - grand_t0:.1f}s ===', flush=True)


if __name__ == '__main__':
    main()
