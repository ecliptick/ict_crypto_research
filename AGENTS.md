# AGENTS.md — ict_crypto

BTC-tick-native fork of the ICT-only backtest engine that lives in
``ict_tier_v2``. Forked 2026-09-24 to:

1. Strip everything gold-specific (XAUUSD-recipe defaults,
   the XAUUSD 1y corpus, the gold-tuned 1s SL/TP defaults).
2. Re-tune the canonical config (``optimal_params``) for BTCUSDT
   perpetuals.
3. Add Binance USDT-M fee modeling (0.05% per side, configurable).
4. Convert the OHLCV-only backtest to a **tick-fill hybrid** that
   runs the bar-based detector against the bar-aggregated ticks,
   then RE-FILLS every exit by walking the underlying aggTrades
   within the exit bar.
5. Drop the live engine, the deploy tooling, the research
   drivers (alpha sweeps, sniper SL/TP sweeps, Kelly, etc.), the
   XAUUSD test corpus, and all scratch artifacts.

**This repo does NOT copy the tick data.** Raw BTCUSDT aggTrades
parquet files stay in the source repo at
``ict_tier_v2/data/binance_um_aggtrades/raw/``. The data path is
hardcoded in ``notebooks/nb51_sniper_viz.py`` — see "Hardcoded
data paths" below.

---

## What lives here

| File | Purpose |
|---|---|
| ``src/__init__.py`` | Package marker. |
| ``src/core/__init__.py`` | Re-exports ``TrendStrategyParams``, ``PendingSignal``, ``OPTIMAL_PARAMS``, ``optimal_params``. |
| ``src/core/ict_signals.py`` | FVG / iFVG / ORB / Wyckoff / sweep detectors, ``IctSeries``, ``RollingFvgRanker`` — bar-based, identical to the parent. |
| ``src/core/ict_strategy.py`` | ``TrendStrategyParams`` + ``PendingSignal`` + ``Trade``. BTC-tuned defaults + new Binance fee knobs (``taker_fee_bps``, ``maker_fee_bps``). |
| ``src/core/market_structure.py`` | BoS / CHoCH / CHoCH+ / order blocks / liquidity sweeps — bar-based, identical to the parent. |
| ``src/core/optimal_config.py`` | Canonical recipe module: ``OPTIMAL_PARAMS`` (the v17 BTC SNIPER) + ``optimal_params(**overrides)`` + ``OPTIMAL_RECIPE_VERSION``. |
| ``src/backtest/__init__.py`` | Re-exports ``IctBacktestResult``, ``Trade``, ``run_ict_backtest``. |
| ``src/backtest/ict_backtest.py`` | Bar-based backtest with 3-layer orders, dynamic SL/TP, FVG-inversion soft-stop. **Now includes Binance fee debit in ``_close_trade``.** Also adds the ``tick_metadata`` field on ``IctBacktestResult``. |
| ``src/tick/__init__.py`` | Re-exports the BTC-only tick utilities. |
| ``src/tick/aggtrade_aggregator.py`` | Vectorized tick → 1s OHLCV aggregator (numpy + pandas). |
| ``src/tick/tick_backtest.py`` | **NEW**: tick-fill hybrid backtest engine — runs the bar backtest, then re-fills every exit via tick-precise SL/TP lookup. |
| ``notebooks/nb51_sniper_viz.py`` | Sniper-trade visualization (candles + raw tick scatter overlay). BTC-native. |

### What was NOT copied (vs the parent ``ict_tier_v2``)

| Skipped | Why |
|---|---|
| ``src/live/`` (47 files) | Live engine moved to a separate repo (``ict_sniper_live``); not needed for backtesting. |
| ``src/tools/`` (25 driver scripts) | Historical research drivers — alpha sweeps, sniper SL/TP sweeps, Kelly sizing, etc. Out of scope for this BTC fork. |
| ``deploy/`` (Terraform + Ansible + systemd) | Out of scope. |
| ``notebooks/nb34, 36, 38, 40-50, 52, 53`` | XAUUSD-specific studies or superseded by the BTC fork. |
| ``data/`` | User directive: do NOT copy data. Hardcoded path in viz notebook. |
| ``scratch/``, ``requirements-live.txt`` | Out of scope. |

---

## Hardcoded data paths

The tick data lives in the **parent repo** (``ict_tier_v2``) —
this fork points at it via a hardcoded ``Path`` constant in
``notebooks/nb51_sniper_viz.py``:

```python
SOURCE_DATA_ROOT = Path(r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw')
RAW_TICKS_PATH   = SOURCE_DATA_ROOT / 'BTCUSDT-aggTrades-2025-06.parquet'
```

To change the corpus:

1. Update ``RAW_TICKS_PATH`` to point at any monthly
   ``BTCUSDT-aggTrades-YYYY-MM.parquet`` file or any daily
   ``BTCUSDT-aggTrades-YYYY-MM-DD.parquet`` file.
2. Or load the parent file directly via
   ``src.tick.aggtrade_aggregator.load_raw_aggtrades(path)``.
3. Available monthly files in the parent repo cover
   2025-01 → 2026-08 (20 months, ~500MB-2GB each).
4. Available daily files cover 2026-09-01 → 2026-09-16
   (smaller, ~10-30MB each — good for smoke tests).

**Schema** (Binance raw aggTrades, raw parquet):

| Column | Type | Notes |
|---|---|---|
| ``agg_trade_id`` | int64 | Trade ID; unused by the backtest. |
| ``price`` | float64 | Trade price in USDT. |
| ``quantity`` | float64 | Base asset (BTC) quantity. |
| ``first_trade_id`` | int64 | Unused. |
| ``last_trade_id`` | int64 | Unused. |
| ``is_buyer_maker`` | bool | ``True`` = aggressive seller (hit the bid). |
| ``ts`` | timestamp[ns, tz=UTC] | Trade timestamp. |

---

## Canonical config — every backtest must import ``optimal_params``

The recipe lives in ``src/core/optimal_config.py``:

```python
from src.core.optimal_config import optimal_params

p = optimal_params()  # BTC SNIPER v17 default
```

### BTC v17 SNIPER recipe (current canonical)

```
OPTIMAL_RECIPE_VERSION = "v17-btc-sniper-2026-09-24"

# Trade geometry (BTC scale)
lots                 = 0.01         # 0.01 BTC ≈ ~$1k notional at BTC=$100k
contract_size        = 1.0          # 1 contract = 1 BTC
sl_usd               = 20.0         # only used when use_atr_scaling=False
tp_usd               = 200.0        # only used when use_atr_scaling=False

# ATR-scaled SL/TP (default active)
use_atr_scaling      = True
sl_atr_mult          = 0.25         # SL = 0.25 × ATR(atr_len=1200, 1s bars) ≈ $0.05-$0.075
tp_atr_mult          = 0.55         # TP = 0.55 × ATR ≈ $0.11-$0.165

# Sniper mode (SL/TP as zone-width multiples, OVERRIDES atr_anchored)
entry_mode                   = 'sniper'
fvg_inv_trade_sl_zone_mult   = 2.0
fvg_inv_trade_tp_zone_mult   = 22.0
fvg_inv_trade_min_zone_usd   = 10.0       # BTC-scale min zone for sniper

# Binance fee model
taker_fee_bps        = 5.0          # 0.05% per side
maker_fee_bps        = 2.0          # 0.02% per side

# FVG detector (BTC scale)
fvg_min_zone_usd     = 5.00         # BTC-scale min zone width
fvg_resample_secs    = 60           # 1m bars from 1s ticks
invalidation_sl_usd  = 2.0          # BTC-scale soft-stop
invalidation_buffer_usd = 0.50
fvg_invalidation_min_pierce_usd = 2.0

# All other knobs inherit from the v17 XAUUSD recipe (BoS/CHoCH,
# Renko, soft-stop, etc.) — see optimal_config.py for the full block.
```

### Smoke test

```bash
cd c:/coding/ict_crypto
python -c "
from src.core.optimal_config import OPTIMAL_PARAMS, OPTIMAL_RECIPE_VERSION, optimal_params
from src.core.ict_strategy import TrendStrategyParams

assert isinstance(OPTIMAL_PARAMS, TrendStrategyParams)
assert OPTIMAL_PARAMS.entry_mode == 'sniper'
assert OPTIMAL_PARAMS.fvg_inv_trade_tp_zone_mult == 22.0
assert OPTIMAL_PARAMS.contract_size == 1.0
assert OPTIMAL_PARAMS.taker_fee_bps == 5.0

p = optimal_params()
assert isinstance(p, TrendStrategyParams)
assert p is not OPTIMAL_PARAMS
assert p.contract_size == 1.0

d = optimal_params(as_dict=True)
assert isinstance(d, dict)
assert d['entry_mode'] == 'sniper'

try:
    optimal_params(this_is_not_a_field=True)
except TypeError:
    pass
else:
    raise AssertionError('bad override should raise')

print('OK', OPTIMAL_RECIPE_VERSION)
"
```

---

## Installation

```bash
python -m venv .venv
. .venv/Scripts/activate     # Windows
pip install -r requirements.txt
```

Dependencies (pinned in ``requirements.txt``): pandas, numpy,
pyarrow, matplotlib, pydantic-settings (used by the live engine
in the sibling repo, kept for parity).

---

## Two ways to run the backtest

### A. Bar-only backtest (legacy semantics, fast)

Use ``run_ict_backtest`` from ``src/backtest``. This is the pure
bar-based backtest the parent repo uses, with the Binance fee
model added. Inputs are 1s OHLCV bars (pre-aggregated):

```python
from src.tick.aggtrade_aggregator import load_raw_aggtrades, aggregate_ticks_to_1s_bars
from src.backtest.ict_backtest import run_ict_backtest
from src.core.optimal_config import optimal_params

raw  = load_raw_aggtrades(r'.../BTCUSDT-aggTrades-2026-09-08.parquet')
bars = aggregate_ticks_to_1s_bars(raw)

p = optimal_params()
res = run_ict_backtest(bars, p, strategy_label='my_bar_run')
```

### B. Tick-fill hybrid backtest (recommended)

Use ``run_tick_backtest`` from ``src/tick``. This runs the bar
backtest, then walks the raw ticks within each bar that produced
an exit, and re-fills the SL/TP with the **first tick that
crossed** the level. Same-bar SL+TP tiebreaks are now resolved
causal by tick order — no more ``sl_tp_tiebreak`` heuristic:

```python
from src.tick.tick_backtest import run_tick_backtest
from src.tick.aggtrade_aggregator import load_raw_aggtrades
from src.core.optimal_config import optimal_params

raw = load_raw_aggtrades(r'.../BTCUSDT-aggTrades-2026-09-08.parquet')

p = optimal_params()
res = run_tick_backtest(raw, p, strategy_label='my_tick_run')
# res.trades[i].exit_price now reflects the actual tick fill,
# not the bar-level fill. res.tick_metadata gives the breakdown
# of where the time went (agg vs bar vs refill).
```

### When to use which

* **Bar-only** — for fast iteration on parameter sweeps. ~2-3×
  faster than tick-fill on a single day.
* **Tick-fill hybrid** — for the canonical recipe validation and
  any final PnL number that ends up in a paper. Tick-fill gives
  more accurate PnL and resolves same-bar SL/TP correctly via
  tick arrival order.

---

## Trade dataclass — new fee fields

The ``Trade`` dataclass (in ``src/backtest/ict_backtest.py``) gained
two fields for the BTC fork:

| Field | Type | Notes |
|---|---|---|
| ``fee_usd`` | float | Total commission debited (entry taker + exit taker). Subtracted from ``pnl_usd`` at close. |
| ``taker_bps_charged`` | float | Echo of the recipe's ``taker_fee_bps`` at execution time. |
| ``net_pnl_usd()`` | method | Returns ``pnl_usd - fee_usd``. |

```python
for tr in res.trades:
    print(f'exit={tr.exit_reason} gross=${tr.pnl_usd:.4f} fee=${tr.fee_usd:.4f} net=${tr.net_pnl_usd():.4f}')
```

### Fee model formula

```
fee_usd = entry_price × lots × contract_size × (taker_fee_bps / 10_000) × 2
```

Where the leading ``× 2`` is for both sides (entry + exit).
For the canonical config (``lots=0.01``, ``contract_size=1.0``,
``taker_fee_bps=5.0``, BTC ≈ $78,000):

```
fee_usd ≈ $78,000 × 0.01 × 1.0 × 0.0005 × 2
       ≈ $0.78 per round-trip
```

Per-day fees on a busy sniper day (8 trades/day): **~$6.20/day**.
Material at small account sizes — see the smoke output for
real numbers.

### Disabling fees

Set both ``taker_fee_bps=0.0`` and ``maker_fee_bps=0.0`` on
``TrendStrategyParams`` to reproduce the legacy XAUUSD
fee-free PnL. The default ``taker_fee_bps=0.0`` on bare
``TrendStrategyParams()`` (not via ``optimal_params()``) keeps
backward compatibility.

---

## Tick-fill hybrid backtest — how it works

The ``run_tick_backtest`` engine has three steps:

1. **Aggregate ticks → 1s bars** — vectorized via
   ``src/tick/aggtrade_aggregator.py``. ~70ms for a typical
   1M-tick day.

2. **Run the bar-based backtest** on those bars (using
   ``run_ict_backtest`` unchanged). Same signals, same fills
   at ``b_open`` for entries and ``target_price`` for limit
   fills.

3. **Re-fill every exit** by walking the ticks in the bar where
   the bar-backtest closed the trade. For SL/TP exits:

   * Compute the SL price (from ``trade.entry_price ±
     trade.stop_usd``) and TP price (from
     ``trade.entry_price ± trade.target_usd``).
   * Walk the ticks in that bar in arrival order.
   * Find the first tick that crossed SL OR TP.
   * Update ``trade.exit_price`` and ``trade.exit_reason``
     accordingly.

   **Same-bar SL+TP tiebreaks** are now resolved by actual tick
   order — the heuristic ``sl_tp_tiebreak`` knob still exists
   for back-compat, but the tick engine overrides it with the
   correct answer.

INV/EOD exits are NOT re-filled (they're triggered by structural
events, not price crossings).

### Performance

On a busy BTC day (1M ticks → 69,563 bars → ~6 sniper trades):

* Aggregation: **~70ms**
* Bar backtest: **~210ms**
* Tick index build: **~430ms**
* Tick refill: **<1ms**
* **Total: ~700ms**

For longer corpora (multi-month walk-forward), the tick index
build can dominate. The current implementation pre-builds the
full index for simplicity. A future optimization can lazy-build
the index only for bars that participate in fills.

---

## Architecture diagram

```
                  raw BTCUSDT aggTrades parquet
                          (in ict_tier_v2/data/...)
                                  │
                                  ▼
           ┌──────────── src/tick/aggtrade_aggregator.py ────────────┐
           │  load_raw_aggtrades() → aggregate_ticks_to_1s_bars()    │
           │  → 1s OHLCV bars (open/high/low/close/volume/notional)  │
           └──────────────────────┬───────────────────────────────────┘
                                  │
                                  ▼
           ┌──────────── src/backtest/ict_backtest.py ───────────────┐
           │  run_ict_backtest(bars, p)                               │
           │  ┌────────── detector layer ──────────┐                  │
           │  │ detect_fvg                         │                  │
           │  │ detect_market_structure            │                  │
           │  │ fvg_retest_signals                 │                  │
           │  │ IctSeries + RollingFvgRanker       │                  │
           │  └────────────────────────────────────┘                  │
           │  ┌────────── bar loop (5 steps) ──────┐                  │
           │  │ 1. signal → pending_layers         │                  │
           │  │ 2. fill pending at b_open/target   │                  │
           │  │ 3. inversion soft-stop check       │                  │
           │  │ 4. SL / TP / decay / trailing exit │                  │
           │  │ 5. EOD sweep                       │                  │
           │  └────────────────────────────────────┘                  │
           │  ┌────────── _close_trade ────────────┐                  │
           │  │ net_pnl = price_move * lots *      │                  │
           │  │          contract_size - fee_usd   │                  │
           │  └────────────────────────────────────┘                  │
           │  → IctBacktestResult (trades, n_signals, etc.)           │
           └──────────────────────┬───────────────────────────────────┘
                                  │
                                  ▼
           ┌──────────── src/tick/tick_backtest.py ─────────────────┐
           │  run_tick_backtest(raw, p):                            │
           │  1. aggregate ticks → bars (re-use above)              │
           │  2. run_ict_backtest(bars, p) (re-use above)           │
           │  3. _refill_trades_with_ticks:                          │
           │     - for each closed trade,                           │
           │     - find the bar's TickData,                         │
           │     - resolve_tick_fill(sl_price, tp_price),           │
           │     - update exit_price + recompute pnl_usd,           │
           │     - leave fee_usd unchanged (entry notional is fixed)│
           │  → IctBacktestResult with .tick_metadata populated      │
           └─────────────────────────────────────────────────────────┘
```

---

## Notebook workflow

The repo follows the parent's "script-first, never edit .ipynb
directly" rule (see the parent AGENTS.md for the full text). All
notebook content is authored as ``.py`` in Jupytext percent format
and regenerated to ``.ipynb`` via ``jupytext``.

Currently only ``nb51_sniper_viz.py`` is shipped. To regenerate
the notebook:

```bash
cd c:/coding/ict_crypto
uv run jupytext --to notebook notebooks/nb51_sniper_viz.py
uv run jupyter nbconvert --to notebook --execute notebooks/nb51_sniper_viz.ipynb --output notebooks/nb51_sniper_viz.ipynb
```

To re-render the 5 panels:

```bash
python notebooks/nb51_sniper_viz.py
# writes notebooks/nb51_panel_{1..5}.png
```

---

## Why this fork — what's NOT the same as the parent

| Aspect | Parent (``ict_tier_v2``) | This fork (``ict_crypto``) |
|---|---|---|
| Asset | XAUUSD | BTCUSDT perpetual |
| Bar source | Pre-aggregated 1s bars in repo | Raw aggTrades, aggregated on the fly |
| Backtest engine | Bar-only (1s OHLCV) | Tick-fill hybrid (bar detect + tick refill) |
| Fee model | None (rebate partially modelled, then removed) | Binance taker 5 bps per side, configurable |
| Contract size | 100.0 (oz) | 1.0 (BTC contract = 1 BTC) |
| Default min zone width | $0.10 | $5.00 (BTC-scale) |
| Default sl_usd | $0.80 | $20.00 (BTC-scale) |
| Trade.fee_usd field | Absent | Present (round-trip commission) |
| Live engine | Yes (``src/live/``) | No — moved to ``ict_sniper_live`` repo |
| Deploy / infra tooling | Yes (``deploy/``) | No |
| Research drivers | 25 scripts under ``src/tools/`` | None — out of scope |
| Historical CSVs | Many (full corpus summaries) | None |
| Sample data in repo | XAUUSD 1y + 1d + sample | None (data in sibling repo) |

Everything else — the detector family, ``TrendStrategyParams``
field structure (modulo scale), the bar loop's 5 steps, BoS/CHoCH
memory, Renko, sniper mechanics — is **bit-identical** to the
parent as of the 2026-09-17/18 v17 promotion.

---

## Coding conventions (inherited from parent)

1. **Dataclasses for structured data** — signals, orders,
   positions, metrics.
2. **USD only** — SL/TP/zone distances are in USD. Variable
   names use ``_usd`` suffix.
3. **Hot-path time is int64 ns** — bar loop time arithmetic
   on int64 nanoseconds; timezone-naive boundary at the parquet
   read.
4. **Always include trades/day and EV/trade in result tables.**
5. **No parameter sweeps in the terminal** — use notebooks.
6. **Naming for tier/quality systems** — ``rank_tier``
   (``A``/``B``/``C``, populated causally by ``RollingFvgRanker``)
   and ``triggered_by`` (``fvg``/``ifvg``/``orb``/``wyckoff``/
   ``sweep``) are mutually exclusive.

---

## Performance notes — tick-fill hybrid

The hot-path optimizations from the parent repo (v6 perf pass)
are inherited bit-identically:

1. ``_consecutive_structure_breaks`` O(1) lookup via cumulative arrays
2. ``BosChochMemory`` imported at module top
3. Lazy ``_existing_pending_zone_ids`` set rebuild
4. ``_remap_bar`` vectorised numpy lookup
5. Hot-path param cache (``_underscore_prefixed`` locals)
6. OHLCV array references rebound to locals

New tick-specific optimizations:

1. **Per-bar TickData pre-built** — the ``build_tick_index``
   pass once at the start of ``run_tick_backtest``. Bars with
   zero trades inside are empty ``TickData`` (no allocation).
2. **TickData arrays are float64 sorted copies** — the per-bar
   tick arrays are sliced from the master sorted tick frame,
   so the per-trade fill lookup is a single ``np.argmax`` on
   a vector of 5-20 prices. O(N_ticks_in_bar), essentially O(1).
3. **No Python loop over bars in the refill pass** — only the
   closed trades (typically 1-10 per day) are walked.

Future optimizations (planned):

1. **Lazy tick-index** — only build TickData for bars that
   participated in a fill/exit (saves ~95% of the index build
   time on multi-month runs).
2. **Cython / numba JIT for the bar loop** — would deliver
   5-10× further speedup but is out of scope here.

---

## Honest status (2026-09-24)

This fork was created today. The smoke test on a busy BTC day
(2026-09-08, $77k-$79k range, 1M ticks) produced:

* **6 sniper trades** on the day, all with fee metadata
  populated.
* Total fees: **$4.71** (10 bps round-trip × average notional).
* The strategy is currently **net-negative** on this single-day
  sample (PnL $-1.82 gross, $-6.53 net of fees). This is **not
  a strategy verdict** — it's one day's data on a regime the
  gold-tuned recipe wasn't designed for.

**What needs validation:**

1. Full-corpus BTC walk-forward on the v17 BTC SNIPER recipe
   (analogous to the XAUUSD v17 full-corpus study).
2. SL/TP tuning on BTC scale (the XAUUSD SL=2x, TP=22x was
   validated on a different regime).
3. Risk-management parameters (``sniper_max_age_secs``,
   ``invalidation_grace_secs``, ``layer_lifetime_secs``).

See "Recommended next experiments" below.

### Files
- All code in this repo: 7,444 + 600 + ~250 lines added
  (modulo 7,400 from the parent).
- Smoke test log: not committed (re-runnable from the snippets
  in this file).

---

## Recommended next experiments

The XAUUSD v17 canonical recipe inherits ~80% of its structure
unchanged (ATR-scaled SL/TP, BoS/CHoCH memory, Renko, soft-stop,
sniper mechanics). The remaining ~20% — the scale-tuning knobs
and the fee model — need BTC validation:

1. **vBTC1 — Full-corpus BTC walk-forward on v17 recipe** —
   run ``run_tick_backtest`` over 20 monthly files (Jan 2025 –
   Aug 2026), aggregate into 1 walk-forward A/B vs the legacy
   ``entry_mode='immediate'`` baseline on BTC.

2. **vBTC2 — SL/TP sweep on BTC** — the gold-found SL=2x,
   TP=22x optimum may shift on BTC's higher ATR ($0.20-$0.30
   vs gold's $0.13). Sweep `fvg_inv_trade_sl_zone_mult`
   ∈ {1.0, 1.5, 2.0, 3.0, 5.0} and
   `fvg_inv_trade_tp_zone_mult` ∈ {4, 8, 15, 22, 30, 40} on
   5+ months of BTC data.

3. **vBTC3 — Fee sensitivity** — sweep
   ``taker_fee_bps`` ∈ {0, 2, 5, 8} (the user-quoted 0.05% is
   already in the middle; test whether the SL/TP optimum
   persists at low and high fee regimes).

4. **vBTC4 — Tick-fill accuracy audit** — compare tick-fill
   trade PnL to bar-only PnL on 100 BTC days; quantify the
   same-bar SL/TP tiebreak frequency and the EV impact of
   resolving it correctly via tick order.

5. **vBTC5 — `fvg_min_zone_usd` sweep on BTC** — the $5.00
   default was inherited from intuition; the BTC zone-width
   distribution may favour a different floor.

Run the existing parent-repo tools first (they work with the
BTC data unchanged) before adding new drivers:

* ``src.tick.tick_backtest.run_tick_backtest`` is the
  backtest engine.
* ``src.core.optimal_config.optimal_params(sl_zone_mult=X,
  tp_zone_mult=Y)`` is how to A/B test.
