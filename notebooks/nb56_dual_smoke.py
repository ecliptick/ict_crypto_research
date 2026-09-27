"""Quick smoke test: canonical (sniper) vs dual (sniper+immediate) on 2025-04.

Compares:
  A: canonical sniper recipe (entry_mode='sniper', qty_btc=0.001)
  B: dual recipe (entry_mode='dual', qty_btc=0.001)

Reports trade count, n per entry_triggered_by tag, and PnL for both.

Cache: this script uses CACHE_VERSION 5 (just bumped — fvg_min_zone_usd
went $5 → $20). The cache will rebuild for 2025-04 (~100s).
"""
import sys
import time
import warnings
from collections import Counter
from pathlib import Path

warnings.filterwarnings("ignore")

for _c in [Path(".").resolve(), *Path(".").resolve().parents]:
    if (_c / "src" / "core" / "ict_signals.py").is_file():
        ROOT = _c
        break
sys.path.insert(0, str(ROOT))

from src.core.optimal_config import optimal_params, OPTIMAL_RECIPE_VERSION
from src.tick.cache import get_or_build
from src.tick.tick_backtest import run_tick_backtest

PATH = Path(r"C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-04.parquet")
SL_ZM, TP_ZM = 5.0, 60.0  # B_n_5x60 from nb53_vBTC2 winner

print(f"recipe version: {OPTIMAL_RECIPE_VERSION}")
print(f"path: {PATH.name}\n")

# Build cache once (shared across both configs since detector knobs match).
p_canon = optimal_params(qty_btc=0.001)
print("Building SweepCache (cold, ~100s)...")
t0 = time.perf_counter()
sc = get_or_build(PATH, p_canon, with_side_table=True, verbose=False)
print(f"  cache build: {time.perf_counter() - t0:.1f}s, fvg={len(sc.fvg_zones)} ifvg={len(sc.ifvg_zones)}\n")

results = []
for label, mode in [("A_canonical_sniper", "sniper"), ("B_dual_sniper_plus_immediate", "dual")]:
    ov = {"fvg_inv_trade_sl_zone_mult": SL_ZM, "fvg_inv_trade_tp_zone_mult": TP_ZM, "entry_mode": mode}
    p_i = optimal_params(qty_btc=0.001, **ov)
    print(f"--- {label} (entry_mode={mode}) ---")
    t0 = time.perf_counter()
    res = run_tick_backtest(
        None, p_i, strategy_label=label,
        pre_aggregated_bars=sc.bars,
        precomputed_zones_by_src={"fvg": sc.fvg_zones, "ifvg": sc.ifvg_zones},
        precomputed_structure=sc.structure,
        precomputed_atr=sc.atr,
        side_table=sc.side_table,
    )
    bt = time.perf_counter() - t0
    trades = list(res.trades)
    pnls = [t.pnl_usd for t in trades]
    fees = [getattr(t, "fee_usd", 0.0) for t in trades]
    nets = [p - f for p, f in zip(pnls, fees)]
    n = len(trades)
    n_wins = sum(1 for v in nets if v > 0)
    wr = 100.0 * n_wins / n if n else 0.0
    ev = sum(nets) / n if n else 0.0
    paths = Counter(t.entry_triggered_by for t in trades)
    n_clean = paths.get("ifvg_clean", 0)
    n_dirty = paths.get("sniper_dirty", 0)
    n_fvg_ladder = sum(1 for t in trades if t.entry_triggered_by not in ("ifvg_clean", "sniper_dirty"))
    print(f"  n={n} net=${sum(nets):+.2f} gross=${sum(pnls):+.2f} fees=${sum(fees):.2f} WR={wr:.1f}% EV=${ev:+.4f} bt={bt:.1f}s")
    print(f"  paths: ifvg_clean={n_clean} sniper_dirty={n_dirty} other={n_fvg_ladder} ({dict(paths)})")
    results.append((label, mode, n, sum(nets), n_clean, n_dirty, n_fvg_ladder, wr, ev))

print("\n=== COMPARISON ===")
for label, mode, n, net, nc, nd, no, wr, ev in results:
    print(f"  {label:40s} n={n:3d} net=${net:+8.2f} clean={nc} dirty={nd} ladder={no} WR={wr:5.1f}% EV=${ev:+.4f}")
