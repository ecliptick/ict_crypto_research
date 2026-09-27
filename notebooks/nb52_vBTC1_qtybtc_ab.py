"""1-month A/B on BTC 2025-04 to verify qty_btc refactor + new canonical.

Three configs:
  A: v26d canonical (strict-wick ON, clean-zone ON, zone-mult SL/TP)
  B: strict-wick OFF only
  C: clean-zone OFF only
"""
import sys, time
sys.path.insert(0, 'c:/coding/ict_crypto_research')
import pandas as pd
from pathlib import Path
from src.core.optimal_config import optimal_params
from src.tick.cache import get_or_build, CACHE_VERSION
from src.tick.tick_backtest import run_tick_backtest
from collections import Counter

RAW = Path(r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-04.parquet')

p_a = optimal_params()
p_b = optimal_params(strict_wick_required=False)
p_c = optimal_params(fvg_min_mit_distance_bars=0, fvg_min_inv_distance_bars=0)
print(f'CACHE_VERSION={CACHE_VERSION}', flush=True)

results = []
for label, p in [
    ('A_v26d_canonical', p_a),
    ('B_wick_OFF',       p_b),
    ('C_clean_OFF',      p_c),
]:
    print(f'--- {label} ---', flush=True)
    t0 = time.perf_counter()
    sc = get_or_build(RAW, p, with_side_table=True, verbose=False)
    t_cache = time.perf_counter() - t0
    t0 = time.perf_counter()
    res = run_tick_backtest(
        None, p, strategy_label=label,
        pre_aggregated_bars=sc.bars,
        precomputed_zones_by_src={'fvg': sc.fvg_zones, 'ifvg': sc.ifvg_zones},
        precomputed_structure=sc.structure,
        precomputed_atr=sc.atr,
        side_table=sc.side_table,
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
    print(f'  cache={t_cache:.2f}s bt={t_bt:.2f}s trades={n}', flush=True)
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
out = Path(r'C:\coding\ict_crypto_research\notebooks\nb52_outputs\nb52_vBTC1_qtybtc_ab.csv')
df.to_csv(out, index=False)
print(f'\nSaved -> {out}', flush=True)
print()
print('=== qty_btc=0.001 A/B (BTC 2025-04 monthly) ===')
print(df[['label','n_trades','pnl_net_total','pnl_gross_total','fee_total','ev_net','wr_net']].to_string(index=False))
