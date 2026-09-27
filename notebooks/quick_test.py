"""Quick smoke on full 6h window."""
import sys
sys.path.insert(0, r'C:\coding\ict_crypto_research')
import pandas as pd
import time
from notebooks.fast_live_replay import _load_live_engine, fast_replay

bars = pd.read_parquet(r'C:\coding\ict_crypto_research\notebooks\nb53_outputs\BTCUSDT-1s-bars-2026-09-25.parquet')
bars_6h = bars.iloc[:6*3600].reset_index(drop=True)
print(f'Testing 6h: {len(bars_6h)} bars')

mods = _load_live_engine()
p = mods['optimal_params']()
print(f'Live params: pierce={p.fvg_invalidation_min_pierce_usd} min_zone={p.fvg_inv_trade_min_zone_usd} consec={p.fvg_invalidation_min_consecutive_bars}')

t0 = time.time()
fired = fast_replay(bars_6h, params=p, warmup_bars=14400, recipe_name='live_canonical')
print(f'\n{len(fired)} snipers in {time.time()-t0:.1f}s')
for f in fired[:10]:
    print(f"  bar_idx={f['bar_idx']} dir={f['direction']:+d} px={f['entry_price_hint']:.2f} zone={f['zone_id']} stop={f['stop_usd']:.2f} target={f['target_usd']:.2f}")
