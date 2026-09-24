# ict_crypto

BTC-tick-native fork of the ICT-only backtest engine. Forked from
[ict_tier_v2](https://github.com/local/ict_tier_v2) on 2026-09-24 to:

- **Strip gold** — drop the XAUUSD-tuned defaults, replace with BTC-scale values.
- **Add Binance fees** — 0.05% per side, configurable, debited in `_close_trade`.
- **Tick-fill hybrid backtest** — bar detection + tick-precise exit fills.
- **Drop the live engine + deploy + research drivers** — this fork is
  pure backtest code.

The raw BTCUSDT aggTrades data lives in the parent repo
(`ict_tier_v2/data/binance_um_aggtrades/raw/`) — it is **not copied**
into this repo. The notebook (`notebooks/nb51_sniper_viz.py`) has a
hardcoded path constant.

## Quick start

```bash
python -m venv .venv
. .venv/Scripts/activate    # Windows
pip install -r requirements.txt

# Smoke test the canonical config:
python -c "
from src.core.optimal_config import optimal_params, OPTIMAL_RECIPE_VERSION
print(OPTIMAL_RECIPE_VERSION)
p = optimal_params()
print('SL zone mult:', p.fvg_inv_trade_sl_zone_mult)
print('TP zone mult:', p.fvg_inv_trade_tp_zone_mult)
print('Fee bps:', p.taker_fee_bps)
"

# Smoke test a full backtest on a BTC day (single-day):
python -c "
from pathlib import Path
from src.tick.aggtrade_aggregator import load_raw_aggtrades
from src.tick.tick_backtest import run_tick_backtest
from src.core.optimal_config import optimal_params

raw = load_raw_aggtrades(
    Path(r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2026-09-08.parquet')
)
res = run_tick_backtest(raw, optimal_params())
print(f'Trades: {len(res.trades)}, EV/trade: \${res.summary()[\"ev_per_trade\"]:+.4f}')
for tr in res.trades[:5]:
    print(f'  {tr.entry_triggered_by:8s} \${tr.entry_price:.0f} -> \${tr.exit_price:.0f} '
          f'gross=\${tr.pnl_usd:.4f} fee=\${tr.fee_usd:.4f} net=\${tr.net_pnl_usd():.4f} '
          f'exit={tr.exit_reason}')
"

# Render the 5 sniper-trade visualization panels (writes notebooks/nb51_panel_*.png):
python notebooks/nb51_sniper_viz.py
```

## Repo layout

```
src/
  core/
    ict_signals.py        # FVG / iFVG / ORB / Wyckoff detectors (bar-based)
    ict_strategy.py       # TrendStrategyParams + Trade (with fee fields)
    market_structure.py   # BoS / CHoCH / order blocks / sweeps
    optimal_config.py     # v17 BTC SNIPER canonical recipe
  backtest/
    ict_backtest.py       # Bar-based backtest engine (3-layer orders,
                          # inversion soft-stop, Renko, BoS/CHoCH memory)
                          # + Binance fee debit in _close_trade
  tick/
    aggtrade_aggregator.py  # Vectorized tick → 1s OHLCV bars
    tick_backtest.py        # Tick-fill hybrid backtest engine
notebooks/
  nb51_sniper_viz.py      # 5 sniper-trade visualization panels
                          # (candles + raw tick scatter overlay)
```

## Canonical config

The v17 BTC SNIPER recipe (full-corpus-validated on XAUUSD; ported
to BTC scale):

- `entry_mode = 'sniper'` (defer FVG entries until zone inverts)
- `fvg_inv_trade_sl_zone_mult = 2.0` (SL = 2× zone width)
- `fvg_inv_trade_tp_zone_mult = 22.0` (TP = 22× zone width)
- `contract_size = 1.0` (1 contract = 1 BTC)
- `lots = 0.01` (0.01 BTC per layer ≈ ~$1k notional at BTC=$100k)
- `taker_fee_bps = 5.0` (0.05% per side, 10 bps round-trip)

See [AGENTS.md](AGENTS.md) for the full config, the smoke tests, and
the recommended next experiments.

## License

Same as the parent `ict_tier_v2` repo. Internal use only.
