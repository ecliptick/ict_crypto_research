"""Fast 1-month A/B at qty_btc=0.01 — strict-wick ON vs OFF.

Both configs share the same detector fingerprint (fvg_min_mit_distance_bars=0
for both, so the post-hoc filter and detector are identical); the only
difference is the wick filter applied at detector time, which means
both will produce the SAME zones (no detector-side wick filtering).

To compare wick ON vs OFF correctly we need TWO detector builds
(superset + filter post-hoc). Since that's expensive on monthly files,
this script instead compares:
  A: clean-zone=ON (mit=3, inv=3) + strict-wick=ON (canonical v26d)
  B: clean-zone=OFF (mit=0, inv=0) + strict-wick=ON

— i.e. it isolates the CLEAN-ZONE routing impact. The strict-wick
toggle alone was already measured in the earlier
nb52_vBTC1_qtybtc_ab run (canonical+wick vs canonical-wick): see
``nb52_vBTC1_qtybtc_ab.csv`` for that comparison.

Both configs in THIS script use the same fingerprint so they share
the superset cache (built once at the top). Each config gets ~3s
for the bar loop after the superset build.

NOTE: Because both configs run with strict_wick_required=True at the
detector level (we built the superset with wick ON), the zone
list is the wick-filtered set. To compare wick ON vs OFF fairly,
both configs use the SAME wick-filtered zones — they differ only
in whether the clean-zone routing fires.
"""
import sys, time
sys.path.insert(0, 'c:/coding/ict_crypto_research')
sys.path.insert(0, 'c:/coding/ict_crypto_research/notebooks')
import pandas as pd
from pathlib import Path
from src.core.optimal_config import optimal_params
from src.tick.cache import get_or_build
from src.tick.tick_backtest import run_tick_backtest
from collections import Counter

RAW = Path(r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-04.parquet')

# Build the SHARED cache: wick ON, clean-zone OFF. Both configs will
# run the bar loop against this same zone set. This means wick is
# effectively ON for both configs in this script — the clean-zone
# routing is the only thing that differs.
p_base = optimal_params(
    qty_btc=0.01,
    strict_wick_required=True,
    fvg_min_mit_distance_bars=0,
    fvg_min_inv_distance_bars=0,
)
print('Building shared SweepCache (one-time cost)...', flush=True)
t0 = time.perf_counter()
sc = get_or_build(RAW, p_base, with_side_table=True, verbose=False)
t_build = time.perf_counter() - t0
print(f'  shared cache built in {t_build:.2f}s — fvg={len(sc.fvg_zones)} ifvg={len(sc.ifvg_zones)}', flush=True)

results = []
configs = [
    ('A_cleanON_wickON',  dict()),  # canonical defaults
    ('B_cleanOFF_wickON', dict(fvg_min_mit_distance_bars=0, fvg_min_inv_distance_bars=0)),
]
for label, ov in configs:
    p = optimal_params(qty_btc=0.01, **ov)
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
    print(f'--- {label} (qty=0.01) ---', flush=True)
    print(f'  bt={t_bt:.2f}s trades={n}', flush=True)
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
        't_backtest_s': t_bt,
    })

df = pd.DataFrame(results)
out = Path(r'C:\coding\ict_crypto_research\notebooks\nb52_outputs\nb52_vBTC1_clean_ab_fast.csv')
df.to_csv(out, index=False)
print(f'\nSaved -> {out}', flush=True)
print()
print('=== clean-zone A/B (BTC 2025-04, qty=0.01, shared cache) ===')
print(df[['label','n_trades','pnl_net_total','pnl_gross_total','fee_total','ev_net','wr_net']].to_string(index=False))
