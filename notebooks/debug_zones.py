"""Debug zone inversion state."""
import sys
sys.path.insert(0, r'C:\coding\ict_crypto_research')
import pandas as pd
import numpy as np

from notebooks.fast_live_replay import _load_live_engine

mods = _load_live_engine()
detect_fvg = mods['detect_fvg']
compute_simple_atr = mods['compute_simple_atr']
detect_market_structure = mods['detect_market_structure']
p = mods['optimal_params']()

bars = pd.read_parquet(r'C:\coding\ict_crypto_research\notebooks\nb53_outputs\BTCUSDT-1s-bars-2026-09-25.parquet')
bars_6h = bars.iloc[:6*3600].reset_index(drop=True)
o = bars_6h['open'].to_numpy(dtype=np.float64)
h = bars_6h['high'].to_numpy(dtype=np.float64)
l = bars_6h['low'].to_numpy(dtype=np.float64)
c = bars_6h['close'].to_numpy(dtype=np.float64)
v = bars_6h['volume'].to_numpy(dtype=np.float64)
times_ns = bars_6h['time'].astype('int64').to_numpy()

atr = compute_simple_atr(h, l, c, length=int(p.atr_len))
print(f'ATR[1200] mean: {atr.mean():.2f}')

struct = detect_market_structure(
    h, l, c,
    pivot_len=int(p.ms_pivot_len),
    liquidity_len=int(p.ms_liquidity_len),
    detect_order_blocks=bool(p.ms_draw_order_blocks),
    detect_liquidity=bool(p.ms_draw_liquidity_sweeps),
    resample_to_n_secs=int(getattr(p, 'fvg_resample_secs', 60)),
)

zones = detect_fvg(
    open_=o, high=h, low=l, close=c,
    warmup=0,
    max_active_zones=10_000_000,
    resample_to_n_secs=int(p.fvg_resample_secs),
    fvg_min_zone_usd=float(p.fvg_min_zone_usd),
    max_zone_age_bars=int(getattr(p, 'fvg_max_age_bars', 0)),
    supersede_on_new=bool(p.fvg_supersede_on_new),
    invalidation_min_pierce_usd=float(p.fvg_invalidation_min_pierce_usd),
    invalidation_min_consecutive_bars=int(p.fvg_invalidation_min_consecutive_bars),
    require_retest_to_invert=bool(p.fvg_require_retest_to_invert),
    played_out_min_extension_usd=float(getattr(p, 'played_out_min_extension_usd', 0.0)),
    times_utc_ns=times_ns,
    fvg_min_lifetime_secs=int(getattr(p, 'fvg_min_lifetime_secs', 0)),
    structure_events_per_bar=struct.events,
    structure_invalidation_age_secs=int(getattr(p, 'fvg_structure_invalidation_age_secs', 0)),
)

print(f'\nTotal zones: {len(zones)}')
inverted = [z for z in zones if z.inverted]
print(f'Inverted zones: {len(inverted)}')
if inverted:
    for z in inverted[:5]:
        w = z.zone_high - z.zone_low
        print(f'  zone dir={z.direction:+d} low={z.zone_low:.2f} high={z.zone_high:.2f} inverted_bar={z.inverted_bar} width=${w:.2f}')

print('\nNon-inverted zones (first 5):')
for z in zones[:5]:
    w = z.zone_high - z.zone_low
    print(f'  dir={z.direction:+d} low={z.zone_low:.2f} high={z.zone_high:.2f} trigger_bar={z.trigger_bar} pierced_bar={z.pierced_bar} width=${w:.2f}')

# Check: how many zones have the inversion bar set?
inv_bars = [z for z in zones if z.inverted_bar >= 0]
print(f'\nZones with inverted_bar >= 0: {len(inv_bars)}')

# Check: what are the first few bars' close prices around inverted_bar?
for z in inv_bars[:3]:
    ib = z.inverted_bar
    zone_edges = (z.zone_low, z.zone_high)
    if ib < 5 or ib + 3 >= len(c):
        continue
    print(f'\nZone {z.zone_id} inverted_bar={ib}, dir={z.direction:+d}:')
    for b in range(max(0, ib-2), min(len(c), ib+5)):
        arrow = '>>>'
        print(f'  bar {b}: O={o[b]:.2f} H={h[b]:.2f} L={l[b]:.2f} C={c[b]:.2f} {arrow if b==ib else ""}')
    print(f'  zone edges: low={z.zone_low:.2f} high={z.zone_high:.2f}')
    print(f'  pierces: close must be < {z.zone_low - p.fvg_invalidation_min_pierce_usd:.2f} (bull) or > {z.zone_high + p.fvg_invalidation_min_pierce_usd:.2f} (bear) for 2 consec bars')
