"""Fast 1-month A/B using the in-process cache pattern from _nb52_worker.

This reuses one SweepCache build across multiple configs by:
  1. Loading the per-file SweepCache with SUPERSET detector params
     (no strict-wick filter at detect time).
  2. Applying zone-creation filters post-hoc per config (zone-breadth,
     strict-wick wick length, etc.).
  3. Running the bar loop with the per-config filtered zones.

This brings each config down from ~100s (cold detector rebuild) to
~3s (cache load + bar loop + post-hoc filter).
"""
import sys, time
sys.path.insert(0, 'c:/coding/ict_crypto_research')
sys.path.insert(0, 'c:/coding/ict_crypto_research/notebooks')
import pandas as pd
from pathlib import Path
from src.core.optimal_config import optimal_params
from src.tick.cache import get_or_build, _fingerprint
from src.tick.tick_backtest import run_tick_backtest
from _nb52_worker import _fp_key, _INPROCESS_CACHE, _filter_zones_post_hoc
from collections import Counter

RAW = Path(r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-04.parquet')

# Build the SUPERSET cache once with strict-wick OFF + min-mit-distance=0
# so all zones are in the cached list (no zone-creation filtering at
# detect time). Then per-config filters happen post-hoc.
p_superset = optimal_params(
    strict_wick_required=False,
    fvg_min_mit_distance_bars=0,
    fvg_min_inv_distance_bars=0,
)
print('Building superset SweepCache (one-time cost)...', flush=True)
t0 = time.perf_counter()
sc = get_or_build(RAW, p_superset, with_side_table=True, verbose=False)
t_build = time.perf_counter() - t0
# Manually populate the worker in-process cache so subsequent configs reuse this
_INPROCESS_CACHE[_fp_key(RAW, p_superset)] = sc
print(f'  superset cache built in {t_build:.2f}s — fvg={len(sc.fvg_zones)} ifvg={len(sc.ifvg_zones)}', flush=True)

# Now run the A/B with full filters applied per-config.
results = []
configs = [
    ('A_wickON',  dict(strict_wick_required=True)),
    ('B_wickOFF', dict(strict_wick_required=False)),
]
for label, ov in configs:
    p = optimal_params(qty_btc=0.01, **ov)
    # Get/build per-config SweepCache via the worker (which uses the
    # _INPROCESS_CACHE). Since all configs share the same minimal
    # fingerprint, the worker returns the SAME sc object.
    p_dict = optimal_params(qty_btc=0.01, **ov)
    from _nb52_worker import _get_sc
    t0 = time.perf_counter()
    sc_i = _get_sc(RAW, p_dict, with_side_table=True)
    t_cache = time.perf_counter() - t0
    # Post-hoc filter
    fvg_filt = _filter_zones_post_hoc(sc_i.fvg_zones, p, 'fvg')
    ifvg_filt = _filter_zones_post_hoc(sc_i.ifvg_zones, p, 'ifvg')
    t0 = time.perf_counter()
    res = run_tick_backtest(
        None, p, strategy_label=label,
        pre_aggregated_bars=sc_i.bars,
        precomputed_zones_by_src={'fvg': fvg_filt, 'ifvg': ifvg_filt},
        precomputed_structure=sc_i.structure,
        precomputed_atr=sc_i.atr,
        side_table=sc_i.side_table,
    )
    t_bt = time.perf_counter() - t0
    pnls_net = [float(t.pnl_usd) for t in res.trades]
    fees = [float(t.fee_usd) for t in res.trades]
    pnls_gross = [n + f for n, f in zip(pnls_net, fees)]
    n = len(pnls_net)
    sum_net = sum(pnls_net); sum_gross = sum(pnls_gross); sum_fee = sum(fees)
    wins = [x for x in pnls_net if x > 0]
    losses = [x for x in pnls_net if x <= 0]
    wr = len(wins)/n if n else 0
    ev_net = sum_net/n if n else 0
    ev_gross = sum_gross/n if n else 0
    avg_win = sum(wins)/len(wins) if wins else 0
    avg_loss = sum(losses)/len(losses) if losses else 0
    print(f'--- {label} (qty=0.01) ---', flush=True)
    print(f'  cache={t_cache:.2f}s bt={t_bt:.2f}s trades={n}', flush=True)
    print(f'  filtered_zones: fvg={len(fvg_filt)} ifvg={len(ifvg_filt)}', flush=True)
    print(f'  net=${sum_net:+.2f} gross=${sum_gross:+.2f} fees=${sum_fee:.2f}', flush=True)
    print(f'  EV_net=${ev_net:+.3f} EV_gross=${ev_gross:+.3f} WR_net={wr*100:.1f}%', flush=True)
    print(f'  avg_win=${avg_win:+.3f} avg_loss=${avg_loss:+.3f}', flush=True)
    er = Counter(t.exit_reason for t in res.trades)
    ep = Counter(getattr(t, 'entry_triggered_by', '?') for t in res.trades)
    print(f'  exits: {dict(er)}', flush=True)
    print(f'  entry_paths: {dict(ep)}', flush=True)
    results.append({
        'label': label,
        'n_trades': n,
        'pnl_net_total': sum_net,
        'pnl_gross_total': sum_gross,
        'fee_total': sum_fee,
        'ev_net': ev_net,
        'ev_gross': ev_gross,
        'wr_net': wr,
        'avg_win_net': avg_win,
        'avg_loss_net': avg_loss,
        't_cache_s': t_cache,
        't_backtest_s': t_bt,
    })

df = pd.DataFrame(results)
out = Path(r'C:\coding\ict_crypto_research\notebooks\nb52_outputs\nb52_vBTC1_qty001_ab_fast.csv')
df.to_csv(out, index=False)
print(f'\nSaved -> {out}', flush=True)
print()
print('=== qty=0.01 fast A/B (BTC 2025-04, in-process cache) ===')
print(df[['label','n_trades','pnl_net_total','pnl_gross_total','fee_total','ev_net','wr_net']].to_string(index=False))
