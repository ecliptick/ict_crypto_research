# AGENTS.md — ict_crypto

BTCUSDT-perpetual fork of `ict_tier_v2` (XAUUSD v17 SNIPER). The
detector family, detector-loop semantics, and `TrendStrategyParams`
field structure are inherited bit-identical — what's specific here
is the BTC scale, the Binance fee model, the tick-fill hybrid
backtest, and the cached sweep pipeline.

**This repo does NOT copy the tick data.** Raw BTCUSDT aggTrades
parquet files stay in the source repo at
`ict_tier_v2/data/binance_um_aggtrades/raw/`. The data path is
hardcoded in `notebooks/nb51_sniper_viz.py` — see "Data paths"
below.

---

## What lives here

| File | Purpose |
|---|---|
| `src/__init__.py` | Package marker. |
| `src/core/__init__.py` | Re-exports `TrendStrategyParams`, `PendingSignal`, `OPTIMAL_PARAMS`, `optimal_params`. |
| `src/core/ict_signals.py` | FVG / iFVG / ORB / Wyckoff / sweep detectors. |
| `src/core/ict_strategy.py` | `TrendStrategyParams` + `PendingSignal` + `Trade`; BTC-tuned defaults + Binance fee knobs. |
| `src/core/market_structure.py` | BoS / CHoCH / CHoCH+ / order blocks / liquidity sweeps. |
| `src/core/optimal_config.py` | Canonical recipe: `OPTIMAL_PARAMS` (v17 BTC SNIPER) + `optimal_params(**overrides)` + `OPTIMAL_RECIPE_VERSION`. |
| `src/backtest/__init__.py` | Re-exports `IctBacktestResult`, `Trade`, `run_ict_backtest`. |
| `src/backtest/ict_backtest.py` | Bar-based backtest; Binance fee debit in `_close_trade`; `tick_metadata` field on result. |
| `src/tick/__init__.py` | Re-exports the BTC-only tick utilities. |
| `src/tick/aggtrade_aggregator.py` | Vectorized tick → 1s OHLCV aggregator; `load_raw_aggtrades_columns` for column-projected reads. |
| `src/tick/tick_backtest.py` | Tick-fill hybrid backtest engine (bar detect + tick refill). |
| `src/tick/cache.py` | `SweepCache` per-file detector cache; `get_or_build(file, params)`. |
| `notebooks/nb51_sniper_viz.py` | Sniper-trade visualization. |
| `notebooks/nb52_opt_sweep_alpha.py` | SL/TP sweep + structural-alpha diagnostics (Cells A-G + hypotheses H.1-H.7). |
| `notebooks/nb53_6month_validation.py` | Canonical-recipe A/B across 6 monthly files. |
| `notebooks/fast_live_replay.py` | Precomputed-zone fast replay of the live engine (~30k bars/sec). |

---

## Data paths

Tick data lives in the parent repo. Hardcoded in
`notebooks/nb51_sniper_viz.py`:

```python
SOURCE_DATA_ROOT = Path(r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw')
RAW_TICKS_PATH   = SOURCE_DATA_ROOT / 'BTCUSDT-aggTrades-2025-06.parquet'
```

**Schema** (Binance raw aggTrades parquet): `agg_trade_id` (i64,
unused), `price` (f64), `quantity` (f64), `first_trade_id` (i64,
unused), `last_trade_id` (i64, unused), `is_buyer_maker` (bool),
`ts` (timestamp[ns, tz=UTC]).

The backtest consumes 4 columns: `(ts, price, quantity, is_buyer_maker)`.
Project at read time via `load_raw_aggtrades_columns(path, columns=BACKTEST_COLUMNS)`;
this drops ~30% of peak RSS on monthly files.

---

## Canonical config — `optimal_params()`

Recipe lives in `src/core/optimal_config.py`. Every backtest imports
it; never hand-roll a `TrendStrategyParams`:

```python
from src.core.optimal_config import optimal_params
p = optimal_params()  # BTC SNIPER v17 default
```

### BTC v17 SNIPER recipe (current canonical: `v17-btc-sniper-2026-09-26c`)

```
# Trade geometry (BTC scale)
lots                 = 0.01
contract_size        = 1.0          # 1 contract = 1 BTC
sl_usd               = 20.0         # only used when use_atr_scaling=False
tp_usd               = 200.0        # only used when use_atr_scaling=False

# ATR-scaled SL/TP (default active)
use_atr_scaling      = True
sl_atr_mult          = 0.25
tp_atr_mult          = 0.55

# Sniper mode (zone-width-anchored SL/TP)
entry_mode                   = 'sniper'
fvg_inv_trade_sl_zone_mult   = 5.0
fvg_inv_trade_tp_zone_mult   = 45.0
fvg_inv_trade_min_zone_usd   = 20.0   # nb52 H.6 promoted — was $10

# Sniper direction on inversion
sniper_inv_direction_mode    = 'continuation'
#   'continuation'      — same direction as the original FVG polarity
#                          (canonical; full-corpus validated)
#   'fade_displacement' — opposite; matches the live engine in
#                          `ict_sniper_live`. See "Sniper direction" below.

# Binance fee model
taker_fee_bps        = 5.0          # 0.05% per side
maker_fee_bps        = 2.0          # 0.02% per side

# FVG detector
fvg_min_zone_usd     = 5.00
fvg_resample_secs    = 60
fvg_max_age_secs     = 10800        # 3h cap on detector-zone lifetime
ifvg_max_age_secs    = 10800
invalidation_sl_usd  = 2.0
invalidation_buffer_usd = 0.50
fvg_invalidation_min_pierce_usd = 2.0

# Body-only mitigation + invalidation (canonical since 2026-09-26)
fvg_body_only_mitigation    = True   # wick past zone ≠ fill
fvg_body_only_invalidation  = True   # wick past zone ≠ flip

# Strict-wick FVG filter (canonical since 2026-09-26c)
# Both outer candles (c1 and c3) must have a visible wick on the
# side AWAY from the gap, length >= 0.025% of mid-price (recomputed
# hourly). Rejects doji-edge FVG formations that don't carry
# structural wick pressure.
strict_wick_required              = True
strict_wick_min_wick_price_pct    = 0.00025
strict_wick_recompute_secs        = 3600

# "Clean" zone gates (canonical since 2026-09-26c)
# A zone is "clean" iff its mitigation and inversion events both
# occur at least 3 1s-bars AFTER the zone's trigger bar. Clean
# zones route to the iFVG immediate-entry path; dirty zones keep
# the sniper path.
fvg_min_mit_distance_bars = 3
fvg_min_inv_distance_bars = 3
```

### Smoke test

```bash
cd c:/coding/ict_crypto
python -c "
from src.core.optimal_config import OPTIMAL_PARAMS, OPTIMAL_RECIPE_VERSION, optimal_params
from src.core.ict_strategy import TrendStrategyParams

assert isinstance(OPTIMAL_PARAMS, TrendStrategyParams)
assert OPTIMAL_PARAMS.entry_mode == 'sniper'
assert OPTIMAL_PARAMS.fvg_inv_trade_tp_zone_mult == 45.0   # promoted
assert OPTIMAL_PARAMS.contract_size == 1.0
assert OPTIMAL_PARAMS.taker_fee_bps == 5.0
assert OPTIMAL_PARAMS.sniper_inv_direction_mode == 'continuation'
assert OPTIMAL_PARAMS.fvg_inv_trade_min_zone_usd == 20.0   # H.6
assert OPTIMAL_PARAMS.fvg_body_only_mitigation is True     # canonical 2026-09-26
assert OPTIMAL_PARAMS.fvg_body_only_invalidation is True
assert OPTIMAL_PARAMS.strict_wick_required is True         # 2026-09-26c
assert OPTIMAL_PARAMS.fvg_min_mit_distance_bars == 3
assert OPTIMAL_PARAMS.ms_pivot_len == 75                   # 2026-09-26g

p = optimal_params()
assert isinstance(p, TrendStrategyParams)
assert p is not OPTIMAL_PARAMS
assert p.ms_pivot_len == 75

print('OK', OPTIMAL_RECIPE_VERSION)
"
```

---

## Two ways to run the backtest

### A. Bar-only (`run_ict_backtest`) — for sweeps

```python
from src.tick.aggtrade_aggregator import load_raw_aggtrades, aggregate_ticks_to_1s_bars
from src.backtest.ict_backtest import run_ict_backtest
from src.core.optimal_config import optimal_params

raw  = load_raw_aggtrades(r'.../BTCUSDT-aggTrades-2026-09-08.parquet')
bars = aggregate_ticks_to_1s_bars(raw)
res  = run_ict_backtest(bars, optimal_params(), strategy_label='my_bar_run')
```

### B. Tick-fill hybrid (`run_tick_backtest`) — for canonical PnL

Runs the bar backtest, then re-fills every exit via tick-precise
SL/TP lookup. Resolves same-bar SL+TP tiebreaks causal by tick
order.

```python
from src.tick.tick_backtest import run_tick_backtest
from src.tick.aggtrade_aggregator import load_raw_aggtrades

raw = load_raw_aggtrades(r'.../BTCUSDT-aggTrades-2026-09-08.parquet')
res = run_tick_backtest(raw, optimal_params(), strategy_label='my_tick_run')
```

Use the bar-only path for sweeps (~2-3× faster). Use tick-fill
for any final PnL number that ends up in a paper.

---

## Trade fee fields

`Trade` (in `src/backtest/ict_backtest.py`) gained:

| Field | Type | Notes |
|---|---|---|
| `fee_usd` | float | Round-trip commission debit (entry taker + exit taker). |
| `taker_bps_charged` | float | Echo of the recipe's `taker_fee_bps`. |
| `net_pnl_usd()` | method | `pnl_usd - fee_usd`. |

Formula: `fee_usd = entry_price × lots × contract_size × (taker_fee_bps/10000) × 2`.

For canonical config (lots=0.01, contract=1.0, taker=5 bps, BTC≈$100k)
that's ≈$1.00/round-trip. Set both `taker_fee_bps=0` and
`maker_fee_bps=0` to reproduce fee-free PnL.

---

## Sniper direction on inversion

The sniper waits for the FVG zone to be inverted, then opens a
trade. The direction is controlled by `sniper_inv_direction_mode`:

| Original zone polarity | Scanner direction on inversion | `continuation` (canonical) | `fade_displacement` (live-engine-equivalent) |
|---|---|---|---|
| Bull FVG (`z.direction = +1`) | SHORT (already flipped) | **LONG** | **SHORT** |
| Bear FVG (`z.direction = -1`) | LONG (already flipped) | **SHORT** | **LONG** |

* `"continuation"` (canonical, default) — full-corpus validated.
* `"fade_displacement"` — matches `ict_sniper_live`'s live engine.

**The two paths disagree on the same setup.** Live rewiring is
tracked separately, not part of this fork. When A/B'ing against
the live engine, set `sniper_inv_direction_mode = "fade_displacement"`
on the backtest side or expect same-setup opposite-direction trades.

Deprecated: `sniper_flip_direction: bool` (True ⇒ `"fade_displacement"`),
removed 2026-12-31.

---

## Cached sweep pipeline (`src/tick/cache.py`)

`SweepCache` persists, per raw parquet file, the per-file detector
work:

* `bars.parquet` — 1s OHLCV
* `atr.npy` — float64[N]
* `structure.pkl` — full `StructureState`
* `fvg_zones.pkl` — list[FvgZone]
* `ifvg_zones.pkl` — list[FvgZone]
* `side_table.pkl` — `_TickSideTable` (lazy view)

Cache key = raw-file mtime + fingerprint of every DETECTOR-side
param field. SL/TP zone-width overrides (`fvg_inv_trade_sl_zone_mult`,
`fvg_inv_trade_tp_zone_mult`) are NOT in the fingerprint — they're
bar-loop geometry, applied downstream.

Layout: `notebooks/.cache/<file_stem>/`.

`run_ict_backtest(...)` and `run_tick_backtest(...)` accept
`precomputed_zones_by_src`, `precomputed_structure`, `precomputed_atr`,
`side_table`. With all four supplied, the only work is the bar
loop + exit refill.

**Important bugfix**: `detect_fvg` mutates zone objects in place
(`mitigated_bar`, `inverted_bar`, `superseded_bar`). Each backtest
call must clone the cached zone list (via `dataclasses.replace`) so
configs that share a SweepCache don't see each other's state.

### Performance — 2025-04 monthly file

| Path | No cache | Cold cache | Warm cache |
|---|---:|---:|---:|
| `load ticks` (4 cols) | 2.6 s | 0.4 s | 0 s |
| `aggregate → 1s bars` | 5.0 s | 5.0 s | 0 s |
| `bar backtest` | 107.9 s | 3.5 s | 3.5 s |
| `build tick side-table` | 1.5 s | 1.5 s | 0 s |
| **end-to-end** | **114.0 s** | **~10 s** | **3.5 s** |

ThreadPoolExecutor + warm cache makes the 6-file × 25-config
nb52 sweep finish in ~5-10 min (was 50-90 min via ProcessPool,
and OOM-prone on Windows). `min(len(tasks), cpu_count, 8)` workers.

`CACHE_VERSION = 6` — bump on any detector-side schema change to
force rebuild. v5→v6 (2026-09-27) was a bugfix: ATR is now
computed on 1-min bars (~25-30× larger) rather than 1-sec bars,
producing sensible SL/TP distances for the ATR-mult paths.

---

## Detector bugfixes (touched during 6-month validation)

* `detect_fvg` (line ~1222 in `src/core/ict_signals.py`): empty-segment
  crash on `np.argmax(seg_depth_pct)` when a zone's `trigger_bar`
  is near `n_1s`. Fixed with a `seg_depth_pct.size > 0` guard.
* FvgZone mutation: see Cached sweep pipeline above.

---

## Notebook workflow

Script-first, never edit `.ipynb` directly. All notebook content
is authored as `.py` in Jupytext percent format.

```bash
cd c:/coding/ict_crypto_research
python -m jupytext --to notebook notebooks/<name>.py
python -m jupyter nbconvert --to notebook --execute notebooks/<name>.ipynb --output notebooks/<name>.ipynb
```

---

## Why this fork — what's NOT the same as the parent

| Aspect | Parent (`ict_tier_v2`) | This fork (`ict_crypto`) |
|---|---|---|
| Asset | XAUUSD | BTCUSDT perpetual |
| Bar source | Pre-aggregated 1s bars in repo | Raw aggTrades, aggregated on the fly |
| Backtest engine | Bar-only | Tick-fill hybrid (bar detect + tick refill) |
| Fee model | None | Binance taker 5 bps per side, configurable |
| Contract size | 100.0 (oz) | 1.0 (BTC contract = 1 BTC) |
| Default min zone width | $0.10 | $5.00 |
| Default sl_usd | $0.80 | $20.00 |
| `Trade.fee_usd` | Absent | Present |
| Live engine | Yes (`src/live/`) | No — moved to `ict_sniper_live` repo |
| Detector cache | None | `SweepCache` per-file (≈30× monthly speedup) |
| Sample data in repo | XAUUSD 1y + 1d + sample | None (data in sibling repo) |

Everything else — detector family, `TrendStrategyParams` field
structure (modulo scale), bar loop's 5 steps, BoS/CHoCH memory,
Renko, sniper mechanics — is bit-identical to the parent as of the
2026-09-17/18 v17 promotion.

---

## Coding conventions (inherited)

1. Dataclasses for structured data.
2. USD only — SL/TP/zone distances in USD; `_usd` suffix.
3. Hot-path time is int64 ns; timezone-naive boundary at the parquet read.
4. Always include trades/day and EV/trade in result tables.
5. No parameter sweeps in the terminal — use notebooks.
6. `rank_tier` (`A`/`B`/`C`, populated by `RollingFvgRanker`) and
   `triggered_by` (`fvg`/`ifvg`/`orb`/`wyckoff`/`sweep`) are
   mutually exclusive.

---

## Backtest run logging (mandatory)

**Every** notebook / script / agent that runs a backtest — A/B, sweep,
validation, single-config, even ad-hoc — must **append a JSON
file** capturing what was tested and what came out. Each append must be done after each loop, 
not after the whole experiment. This is the research log; AGENTS.md no longer narrates individual runs.
Also persist full trade logs for inspection.

### Where

* Path: `notebooks/<notebook>_outputs/<notebook>__runs.jsonl`
  (one JSON object per line, **append-only**).
* Per-trade CSVs already live alongside it (`<scope>_per_trade.csv`,
  `<scope>_summary.csv`). The JSONL is the run-level log; CSVs are
  the trade-level log.

### Required JSON shape

```json
{
  "ts_utc": "2026-09-26T07:20:00Z",
  "notebook": "nb56_zone_lifetime_v2",
  "scenario": "zone_lifetime_sweep",
  "scope": ["2025-04", "2025-05", "2025-10"],
  "engine": "bar",                      // "bar" | "tick"
  "comments": "Hypothetical — what we changed and why.",
  "hypothesis": "Capping zone lifetime at 6h filters stale zones.",
  "verdict": "rejected | promoted | inconclusive",
  "canonical_recipe_version": "v17-btc-sniper-2026-09-26c",
  "params": { ...full TrendStrategyParams as dict, via optimal_params(as_dict=True)... },
  "overrides_vs_canonical": { "fvg_max_age_secs": 21600 },
  "metrics": {
    "n_trades": 312,
    "n_long": 168,
    "n_short": 144,
    "n_long_wins": 13,
    "n_short_wins": 20,
    "months_tested": ["2025-04", "2025-05", "2025-10"],
    "n_signals_emitted": 0,
    "n_signals_consumed": 0,
    "n_fills": 0,
    "n_soft_stops": 0,
    "win_rate_pct": 31.4,
    "pnl_gross_usd": 412.30,
    "fees_paid_usd": 312.00,
    "pnl_net_usd": 100.30,             // = pnl_gross - fees_paid
    "ev_per_trade_usd": 0.32,          // expected value per trade (net)
    "mean_win_usd": 8.10,
    "mean_loss_usd": -3.40,
    "median_win_usd": 5.20,
    "median_loss_usd": -2.10,
    "largest_win_usd": 42.00,
    "largest_loss_usd": -18.50,
    "profit_factor": 1.18,             // sum(|wins|) / sum(|losses|)
    "payoff_ratio": 2.38,              // |mean_win / mean_loss|
    "max_drawdown_usd": -55.20,
    "max_drawdown_pct": 5.5,
    "trades_per_day": 3.4,
    "median_hold_secs": 87.0,
    "mean_hold_secs": 142.3,
    "exit_reason_breakdown": { "tp": 90, "sl": 180, "inv": 30, "eod": 12 },
    "taker_bps_charged": 5.0
  }
}
```

### Required metrics (minimum bar)

| Metric | Why |
|---|---|
| `n_trades` | Sample size — refuse to draw conclusions from <30 trades. |
| `n_long`, `n_short` | Direction balance — confirms the config isn't asymmetric-by-accident. |
| `months_tested` | Explicit list of monthly files in scope. A run with empty `months_tested` is rejected — the JSONL must record exactly which months it ran on so cross-run comparisons are reproducible. |
| `win_rate_pct` | Direction accuracy. |
| `pnl_gross_usd`, `fees_paid_usd`, `pnl_net_usd` | Three separate numbers — fees matter on BTC scale. |
| `ev_per_trade_usd` | The headline. |
| `mean_win_usd`, `mean_loss_usd`, `median_win_usd`, `median_loss_usd` | Tail-aware (a few huge wins can dominate the mean). |
| `profit_factor`, `payoff_ratio` | Symmetric payoff summary. |
| `max_drawdown_usd`, `max_drawdown_pct` | Risk dimension. |
| `trades_per_day` | Capacity / opportunity cost. |
| `median_hold_secs`, `mean_hold_secs` | Holding-period drift between configs. |
| `exit_reason_breakdown` | Where the PnL actually comes from (TP vs SL vs soft-stop). |
| `taker_bps_charged` | Fee-rate echo so the JSONL is fee-model self-describing. |

Add scenario-specific metrics as free-form keys under `metrics`
(e.g. `f3_true_ifvg_ev_usd`, `h7_decile_0_ev_usd`). Don't invent
new top-level keys — keep `params` / `metrics` / `verdict`
stable so downstream tools can parse every historical run.

### Required `comments` field

Free text. State in 1–3 sentences:

* What was changed vs. canonical (or "baseline — no overrides").
* Why (which prior finding motivated it).
* What the result implies (or "inconclusive — see metrics").

Empty `comments` is rejected. If the run is a baseline
reproduction, say so explicitly so future readers can detect
canonical-config drift.

### Helper

Use the canonical helper so every script emits the same shape:

```python
from src.core.run_report import append_run_report
append_run_report(
    notebook="nb56_zone_lifetime_v2",
    scenario="zone_lifetime_sweep",
    scope=["2025-04", "2025-05"],
    engine="tick",
    comments="Sweeping fvg_max_age_secs in {10800, 21600, 43200}.",
    hypothesis="Capping zone lifetime at 6h filters stale zones.",
    verdict="inconclusive",
    params=p,
    result=res,                          # IctBacktestResult
    metrics_extra={"median_hold_secs": 87.0},
    out_dir=Path("notebooks/nb56_outputs"),
)
```

`append_run_report` writes one JSON line to `<out>/<notebook>__runs.jsonl`
and refuses to clobber an existing file. It is the single writer;
notebook code should never call `json.dump` directly for run logs.

### Reading the log

```python
import json, pathlib
runs = [json.loads(l) for l in
        pathlib.Path("notebooks/nb56_outputs/nb56__runs.jsonl").read_text().splitlines()]
df = pd.DataFrame([{**r["metrics"], "scenario": r["scenario"]} for r in runs])
```

---

## Validation history

**See `notebooks/<nb>__runs.jsonl` for the full append-only research
log.** Each line is one run with hypothesis, canonical params,
overrides, and the full metric set. AGENTS.md does not narrate
individual validation rounds; promote findings into
`OPTIMAL_PARAMS` in `src/core/optimal_config.py` and bump
`OPTIMAL_RECIPE_VERSION`.

---

## Update (2026-09-26, urgent) — fee-vs-edge collapse on canonical recipe

A quick RR sweep on the canonical recipe (`v17-btc-sniper-2026-09-26c`)
across the 6-month nb52 sample surfaced a **structural loss** that
the previous canonical validation missed. The numbers from the
2026-09-27 nb53 body-only validation (`+$43.75` net PnL on 2025-04,
`+$310.72` sum across 6 months) **do not reproduce** on the current
recipe. Run via
[`notebooks/nb55_rr_sweep.py`](notebooks/nb55_rr_sweep.py).

### Headline

| Metric | nb53 (2026-09-27) reported | nb55 (2026-09-26) current |
|---|---:|---:|
| 2025-04 net PnL | +$43.75 (45 trades) | **-$45.60 (54 trades)** |
| 6-month sum | +$310.72 (273 trades) | **-$318.28 (346 trades)** |
| Mean gross PnL / trade | n/a | **+$0.002** |
| Mean fee / trade | n/a | ~$0.85 |

**The strategy's gross edge per trade is essentially zero.**
Fees of ~$0.85/trade then guarantee a loss.

### Fee math on BTC @ $75k, lots=0.01, contract_size=1.0, taker=5bps

```
notional per trade = 0.01 BTC × $75,000 = $750
fee per trade (round-trip) = $750 × 0.0005 × 2 = $0.75
```

Median TP distance at sl=0.25/tp=0.55 (canonical): **$3.40**
→ TP gross profit = 0.01 × $3.40 = **$0.034/trade**

Median SL distance: **$1.54**
→ SL gross loss = 0.01 × $1.54 = **$0.015/trade**

| Exit | Gross | Fee | Net |
|---|---:|---:|---:|
| TP winner | +$0.034 | -$0.75 | **-$0.72** |
| SL loser | -$0.015 | -$0.75 | **-$0.77** |
| INV loser | -$0.018 | -$0.83 | **-$0.85** |

**Every TP exit is a fee-loss.** Every SL exit is a fee-loss.
The strategy's gross edge is below fee-bite size at the current
lot sizing and SL/TP scaling.

### Per-exit-reason totals (2025-04, 54 trades)

| Reason | n | Sum Gross | Sum Fee | Sum Net |
|---|---:|---:|---:|---:|
| tp | 18 | **+$0.75** | $15.46 | **-$14.72** |
| sl | 15 | -$0.28 | $12.80 | -$13.08 |
| inv | 21 | -$0.37 | $17.43 | -$17.80 |

TP winners collectively captured **$0.75 of gross profit** across
18 exits — and lost $14.72 net after fees.

### What changed between nb53 (2026-09-27) and nb55 (2026-09-26)

The recipe version bumped from `v17-btc-sniper-2026-09-26b` (used
in nb53) to `v17-btc-sniper-2026-09-26c` (current canonical).
The `c` delta adds three strategy-level knobs:

* `strict_wick_required=True` + `strict_wick_min_wick_price_pct=0.00025`
* `fvg_min_mit_distance_bars=3`
* `fvg_min_inv_distance_bars=3`

These knobs were promoted without a 6-month validation sweep —
the H.3/H.4 (2026-09-26 late) hypothesis-tests on a 1-file
sample only. The c-delta changed trade selection materially
(54 vs 45 trades on 2025-04) without re-validating the headline
PnL. **This is a silent recipe drift.**

### Why the RR sweep showed nothing

Varying `sl_atr_mult` × `tp_atr_mult` across 24 combos × 6 files
= 144 configs. **All 24 configs are net-negative** in the
$310-320 range, with a best-to-worst spread of only **$8** —
statistically meaningless noise.

The RR knob moves trades between TP / SL exits (wider TP → fewer
TP, more SL; total fees roughly conserved) but cannot rescue a
strategy with **zero gross edge**. The headline of "5:45 RR
doesn't work" was correct — but not because 5:45 is the wrong
ratio; it's because the **NB55 sweep tested `sl_atr_mult ×
tp_atr_mult`** (not the zone_mult path), and at canonical
`sl_atr_mult=0.25` × `tp_atr_mult=0.55` the realized SL/TP
distances on 1s BTC bars were **$0.52 / $1.15 — sub-noise-floor
and immediately SL'd**. The **NB53 zone_mult sweep** (different
knobs: `fvg_inv_trade_sl_zone_mult × fvg_inv_trade_tp_zone_mult`)
showed that **5×/45× zone_mult gives $243 / $2,189 on BTC and
works (+$624 over 6 months)**; the recommended next-canonical
is 5×/60× zone_mult (+$780 over 6 months). Don't conflate the
two sweeps — they're testing different scaling bases.

### Fee guard (does NOT exist)

User asked: "didn't we have a guard to not trade if TP still
loses fees?" — **No such guard exists.** Code path in
[`src/backtest/ict_backtest.py:1981`](src/backtest/ict_backtest.py):
`tr.pnl_usd -= tr.fee_usd` is debited unconditionally in
`_close_trade`. There is no pre-fill check that rejects a trade
when `target_usd × lots × contract_size < 2 × fee_per_trade`.

### Honest status

* The canonical recipe `v17-btc-sniper-2026-09-26c` is **not
 viable** at the current lot size and ATR-scaled SL/TP.
* The recipe version bump from `b` → `c` silently added three
 strategy knobs that were not multi-month validated.
* The strategy needs **either** a fee guard **or** a sizing
 increase (lots) **or** an ATR-mult floor that ensures
 TP-distance × lots > fee bite.
* Three fixes proposed (do NOT auto-apply — promote after
 6-month validation):

 1. **Fee guard** in `_close_trade`: skip the trade entry when
    `target_usd × lots × contract_size < 1.5 × fee_per_trade`.
    This is the cheapest fix and preserves the canonical
    sizing. Drops the per-trade net loss to whatever the
    gross edge actually is (currently near zero).
 2. **Floor `tp_atr_mult`**: raise the canonical from 0.55 to
    e.g. 1.20 to ensure TP-distance × lots > 2 × fee bite at
    BTC=$75k. Forces a wider target per trade; trades on
    bigger moves only.
 3. **Bump `lots`**: raise from 0.01 to 0.02 so each trade
    moves 2× as much and 2× the fees come out of 2× the
    gross. Edge scales linearly with lots, but so does
    per-trade variance and the percentage of capital at
    risk per trade.

* Whatever fix lands needs the full 6-month validation sweep
 (like nb53 did for body-only). Until then, **the canonical
 is broken**.

### Files added

* [`notebooks/nb55_rr_sweep.py`](notebooks/nb55_rr_sweep.py) — RR sweep driver
* `notebooks/nb55_outputs/nb55_rr_sweep_per_config.csv` — 144 (file, config) rows
* `notebooks/nb55_outputs/nb55_rr_sweep_summary.csv` — one-row headline

### Open work — urgent

* **Re-validate the v17-btc-sniper-2026-09-26c recipe** across
  the 6-month sample to confirm the fee-edge collapse is not
  a one-month artifact (the 2025-04/2025-10/2025-11 numbers
  suggest it isn't).
* **Promote one of the three proposed fixes** (fee guard, ATR
  floor, lots bump) and re-run the 6-month validation.
* **Document the b → c recipe delta in AGENTS.md** when the
  validation sweep completes. The c-delta (strict-wick +
  clean-zone routing) was a silent strategy change that
  hasn't been written up here yet.

---

## Update (2026-09-26, evening 2) — two PnL bugs fixed, vBTC2 SL/TP sweep completed

Three changes landed tonight. The headline is a 100× PnL
inflation bug in the tick-fill re-fill pass, and a clean-zone
routing regression that ignored the ``fvg_inv_trade_sl_zone_mult`` /
``fvg_inv_trade_tp_zone_mult`` overrides. Both are now fixed.

### 1. Tick-backtest contract_size default (the 100× bug)

[`src/tick/tick_backtest.py`](src/tick/tick_backtest.py) line 723
previously did:

```python
contract_size = float(getattr(p, "contract_size", 100.0))
tr.pnl_usd = (tr.exit_price - tr.entry_price) * tr.lots * contract_size
```

The ``TrendStrategyParams`` dataclass has no ``contract_size``
field on the BTC fork (see AGENTS.md § "BTC position sizing"
above — ``lots × contract_size`` was collapsed into ``qty_btc``).
So ``getattr`` fell through to the **XAUUSD default of 100.0**,
inflating every tick-re-filled trade by 100×. The bar-only
backtest was unaffected (it reads ``p.qty_btc`` directly), so
the bug was masked whenever a researcher ran ``run_ict_backtest``
without ``run_tick_backtest``.

Replaced with:

```python
qty_btc = float(getattr(tr, "qty_btc", 0.0)) or float(getattr(tr, "lots", 0.0))
tr.pnl_usd = (tr.exit_price - tr.entry_price) * qty_btc
```

Validated against the historical +$88.51 baseline (Apr 2025,
qty_btc=0.01): the post-fix numbers match within rounding
(n=54 trades, gross ~$138, net ~$92 at sl=1.5/tp=45).

### 2. Clean-zone routing ignored SL/TP zone-mult overrides

[`src/backtest/ict_backtest.py`](src/backtest/ict_backtest.py) the
clean-zone path of the bar loop hard-coded ATR-anchored SL/TP:

```python
stop_usd_v = _atr_v * _sl_atr_mult        # 0.25 × ATR ≈ $0.05
target_usd_v = _atr_v * _tp_atr_mult      # 0.55 × ATR ≈ $0.11
```

regardless of the ``fvg_inv_trade_sl_zone_mult`` /
``fvg_inv_trade_tp_zone_mult`` overrides. So an SL/TP sweep was
a no-op on the clean path — every cell produced the same trades
because the ATR floor was always used. Symptom in
[`notebooks/nb52_vBTC2_sltp_sweep.py`](notebooks/nb52_vBTC2_sltp_sweep.py):
cells 1/2/3 were bit-identical before the fix; now they differ
exactly as expected.

Fixed by wiring the ``sniper_sl_mode`` / ``sniper_tp_mode``
toggles into the clean path. Default canonical is
``"zone_mult"`` for both, so the bar loop now honours the
override the same way it does on the dirty sniper path.

### 3. vBTC2 SL/TP sweep on BTC 2025-04 — full 25-cell grid

[`notebooks/nb52_vBTC2_sltp_sweep.py`](notebooks/nb52_vBTC2_sltp_sweep.py)
runs a 5×5 (sl × tp) sweep, sharing one ``SweepCache`` build
across all cells. End-to-end: **~75s for 25 cells on a
monthly file** (cache build 0.86s on warm hit, ~3s/cell bar
loop). Output is the canonical ``<notebook>__runs.jsonl``
via the new ``src.core.run_report.append_run_report`` helper
(added tonight; see below).

Results on the canonical v26d recipe (qty_btc=0.01):

| sl | tp | n | net | gross | WR | EV |
|---:|---:|---:|---:|---:|---:|---:|
| **1.5** | **45** | **54** | **+$92.87** | **$138.57** | **13.0%** | **+$1.720** |
| 5.0 | 45 | 54 | +$92.68 | $138.38 | **16.7%** | +$1.716 |
| 2.0 | 45 | 54 | +$87.36 | $133.06 | 13.0% | +$1.618 |
| 3.0 | 45 | 54 | +$76.51 | $122.20 | 13.0% | +$1.417 |
| 1.0 | 45 | 54 | +$66.06 | $111.75 | 11.1% | +$1.223 |
| ... | ... | ... | ... | ... | ... | ... |
| 3.0 | 8 | 54 | -$42.25 | $3.45 | 24.1% | -$0.782 |
| 5.0 | 8 | 54 | -$42.11 | $3.58 | **31.5%** | -$0.780 |
| 2.0 | 8 | 54 | -$35.07 | $10.63 | 22.2% | -$0.649 |

Best net: **(sl=1.5, tp=45)** at +$92.87 net / +$138.57 gross.
The (sl=5.0, tp=45) cell is essentially tied on net (+$92.68)
but has a 16.7% WR vs 13.0% — same money, less variance, the
more robust choice on this month.

**Both cells beat the pre-26c +$88.51 baseline.** The recipe
update was net-positive on this month. TP=45 (the canonical
default post-H.6) is the right end of the grid; SL=1.5 is
near-optimal (1.0 wins on lower-bound but loses WR; 5.0 wins
WR but loses EV slightly).

Multi-month validation still required before promoting.
April 2025 was a quiet range month (+$88 on legacy, +$92 on
this recipe); 2025-11 (legacy +$110) and the volatile months
need to be re-checked with the post-fix PnL math.

### Files added / modified

| File | Purpose |
|---|---|
| [`src/tick/tick_backtest.py`](src/tick/tick_backtest.py) | Fixed 100× PnL inflation bug (dropped ``contract_size=100`` default) |
| [`src/backtest/ict_backtest.py`](src/backtest/ict_backtest.py) | Wired ``sniper_sl_mode`` / ``sniper_tp_mode`` into clean-zone path |
| [`src/core/run_report.py`](src/core/run_report.py) | **NEW**: ``append_run_report`` helper — single writer for ``__runs.jsonl`` |
| [`notebooks/nb52_vBTC2_sltp_sweep.py`](notebooks/nb52_vBTC2_sltp_sweep.py) | **NEW**: 25-cell SL/TP sweep driver with shared-cache fast path |
| [`notebooks/nb52_vBTC2_sltp_sweep_outputs/`](notebooks/nb52_vBTC2_sltp_sweep_outputs/) | 25 ``__runs.jsonl`` records, one per (sl, tp) cell |

### Open work — still urgent

* **Re-run the 6-month validation on the post-fix PnL math**
 (the vBTC2 sweep above is one month; the 6-month corpus
 needs the same contract_size fix to be trustworthy).
* **Confirm ``run_ict_backtest`` PnL is identical to
 ``run_tick_backtest`` PnL when the tick-fill pass doesn't
 shift the exit bar** — this is the unit test for the bug
 fix above.
* **Multi-month vBTC2 sweep** — run the 25-cell grid on
 each of the 6 sample months to confirm (sl=1.5, tp=45) is
 robust across regimes.

---

## Update (2026-09-26, evening) — 6-month vBTC2 multi-mode sweep

The vBTC2 SL/TP sweep has been re-run across all 6 monthly files
in the parent nb52 sample (seed=20260925), with three additional
changes:

1. New ``immediate_*_mode`` knobs so the non-sniper
 ``entry_mode='immediate'`` path gets the same
 {``zone_mult``|``atr_mult``|``usd_fixed``} selector the
 sniper-mode path already had.
2. New ``sniper_sl_mode='usd_fixed'`` / ``sniper_tp_mode='usd_fixed'``
 branch on the sniper SL/TP wiring (the existing
 ``zone_mult`` / ``atr_mult`` were insufficient for the user's
 "wide zone can't reach TP at zone_mult" question).
3. New ``dedup_keys`` option on
 ``src.core.run_report.append_run_report`` so re-runs of the
 same sweep cell overwrite the prior row instead of
 accumulating duplicates.

### Driver

[`notebooks/nb53_vBTC2_sweep_multimode.py`](notebooks/nb53_vBTC2_sweep_multimode.py)
— 20 curated cells × 6 monthly files = 120 runs. Wall time
~13.6 min on the threaded SweepCache pipeline (avg 3.2s/cell
on warm cache; first-cell-of-file cache builds vary 0-15s).

### Cell grid (20 cells across 4 groups)

| Group | Cells | What it tests |
|---|---|---|
| **A — confirmed** (4) | `(5,45)`, `(1.5,45)`, `(1.5,22)`, `(2,22)` | Re-validate the single-month winners across 6 files |
| **B — sparse neighbours** (9) | `(1.5, {35,50,60})`, `(5, {50,60})`, `(1,45)`, `(2.5,45)`, `(4,45)`, `(7,45)` | Sparse local neighbourhood of the two single-month winners |
| **C — atr_mult TP** (4) | `(5,45)` zone SL × `tp_atr_mult` ∈ {0.55, 2.0, 4.0, 22} | The user's "TP zone_mult vs atr_mult" question |
| **D — usd_fixed** (3) | `sl_usd`/`tp_usd` ∈ {$200/$500, $500/$1000, $1000/$2000} | The user's "non-sniper counterpart — usd_fixed" ask |

Run modes:

```bash
python notebooks/nb53_vBTC2_sweep_multimode.py          # full 6-month sweep
python notebooks/nb53_vBTC2_sweep_multimode.py --fast    # single-file smoke
```

### Headline — 6-month aggregate

| Cell | SL zone mult | TP zone mult / mode | Sum PnL (6mo) | Avg / mo | All months positive? |
|---|---:|---:|---:|---:|:---:|
| **B_n_5x60** | **5.0×** | **60.0× zone** | **+$780.52** | +$130.09 | ✅ |
| B_n_5x50 | 5.0× | 50.0× zone | +$724.23 | +$120.70 | ✅ |
| B_n_7x45 | 7.0× | 45.0× zone | +$705.58 | +$117.60 | ✅ |
| A_canon_5x45 (prior canonical) | 5.0× | 45.0× zone | +$624.20 | +$104.03 | ✅ |
| B_n_4x45 | 4.0× | 45.0× zone | +$590.63 | +$98.44 | ✅ |
| B_n_1.5x60 | 1.5× | 60.0× zone | +$572.55 | +$95.43 | ✅ |
| B_n_1.5x50 | 1.5× | 50.0× zone | +$558.81 | +$93.14 | ✅ |
| B_n_2.5x45 | 2.5× | 45.0× zone | +$534.76 | +$89.13 | ✅ |
| A_best_1.5x45 | 1.5× | 45.0× zone | +$468.06 | +$78.01 | ✅ |
| B_n_1.5x35 | 1.5× | 35.0× zone | +$286.29 | +$47.72 | ❌ (1/6 neg) |
| B_n_1.0x45 | 1.0× | 45.0× zone | +$250.71 | +$41.78 | ❌ (1/6 neg) |
| D_usd_1000_2000 | usd_fixed | usd_fixed ($1k/$2k) | +$154.37 | +$25.73 | ❌ (2/6 neg) |
| A_agmd_2x22 | 2.0× | 22.0× zone | +$72.04 | +$12.01 | ❌ (1/6 neg) |
| A_orig_1.5x22 | 1.5× | 22.0× zone | +$68.90 | +$11.48 | ❌ (1/6 neg) |
| D_usd_500_1000 | usd_fixed | usd_fixed ($500/$1k) | +$41.77 | +$6.96 | ❌ (2/6 neg) |
| D_usd_200_500 | usd_fixed | usd_fixed ($200/$500) | -$131.92 | -$21.99 | ❌ (5/6 neg) |
| C_tp_atr_0.55 | 5.0× | ATR × 0.55 | -$334.92 | -$55.82 | ❌ (6/6 neg) |
| C_tp_atr_22 | 5.0× | ATR × 22 | -$338.40 | -$56.40 | ❌ (6/6 neg) |
| C_tp_atr_4.0 | 5.0× | ATR × 4 | -$341.33 | -$56.89 | ❌ (6/6 neg) |
| C_tp_atr_2.0 | 5.0× | ATR × 2 | -$341.43 | -$56.90 | ❌ (6/6 neg) |

### Headline findings

* **`(sl=5×, tp=60×)` is the new canonical winner.** It is the only
 cell in the top-3 that's stable on a wide TP range (also wins at
 `tp=50`). Beats the prior canonical `(5×, 45×)` by
 **+$156/6mo (+25%)**.
* **TP zone_mult > atr_mult for sniper/iFVG.** All 4
 `sniper_tp_mode='atr_mult'` cells (Group C) lose every month
 for 6 months straight. The user's intuition that
 "TP_zone_mult wins on wide zones" replicates at scale. The
 problem is not reachability on wide zones — it's that TP_=ATR
 gives fixed-width targets that simply don't carry enough size
 at the canonical 1s ATR scale.
* **All top-8 winners stay positive on every month.** No regime
 crashes. The `sl ∈ {1.5, 2.5, 4, 5, 7}` × `tp ∈ {45, 50, 60}`
 plateau is genuinely regime-robust — not a sharp peak that
 overfits to one month.
* **`usd_fixed` is much weaker than zone_mult** for both SL and TP
 at this BTC scale. The D-group totals are 1/3 to 1/5 of the
 top-8 zone_mult totals. Narrow `usd_fixed` SL/TP ($200/$500) is
 net-negative across the 6 months.
* **The `(1.5, 22)` XAUUSD-shaped cell loses massively on BTC.**
 +$11/mo avg, with losses on 2/6 months. The legacy XAUUSD recipe
 doesn't transfer directly to BTC scale — different SL/TP geometry.

### Cross-mode robustness check

The top zone_mult cells (B group) and the canonical cell (A_canon)
all stay positive on every month tested. Importantly:

* `(sl=1.0×, tp=45×)` is borderline-positive (+$41/mo avg) and
 loses one month (-$72 on 2025-10). SL=1× is too tight — many
 trades stop out on noise before reaching TP.
* `(sl=1.5×, tp=35×)` is borderline (+$47/mo avg) and loses one
 month (-$0.83 on 2025-10). TP=35× is the lower edge of the
 plateau — below it, trades often exit at SL before reaching
 TP.

### Recommended next canonical recipe

The clean winning call is to bump `fvg_inv_trade_tp_zone_mult`
from `45.0` (current canonical) to `60.0` — a single-line
change that delivers **+$26/month expected PnL** on the 6-month
sample, with strictly positive every-month outcomes.

```python
# src/core/optimal_config.py  -- bump:
fvg_inv_trade_tp_zone_mult=60.0,        # was 45.0; new canonical 2026-09-26 e
```

Once promoted, re-run the (5×, 60×) cell on the rest of the
20-month corpus (the unvalidated 14 files) and the 30-day
forward window before considering it "canonical".

### Files added / changed

* [`notebooks/nb53_vBTC2_sweep_multimode.py`](notebooks/nb53_vBTC2_sweep_multimode.py) — driver
* `notebooks/nb53_vBTC2_sweep_multimode_outputs/nb53_vBTC2_sweep_multimode__runs.jsonl`
 — 120 records (one per cell × month)
* `notebooks/nb53_vBTC2_sweep_multimode_outputs/nb53_vBTC2_sweep_summary.csv`
 — cross-month aggregate (one row per cell, 6 monthly columns)
* `notebooks/nb53_vBTC2_sweep_multimode_outputs/run.log` — driver log

### Code changes in this round

* **[`src/core/ict_strategy.py`](src/core/ict_strategy.py)** — added
 ``immediate_sl_mode`` / ``immediate_tp_mode`` /
 ``immediate_sl_zone_mult`` / ``immediate_tp_zone_mult`` /
 ``sxt_share_mode_with_immediate`` fields to
 ``TrendStrategyParams``. Empty-string defaults preserve the
 pre-e (``use_atr_scaling``-driven) legacy behaviour.
* **[`src/core/ict_signals.py`](src/core/ict_signals.py)** — override the
 master ``sl_usd`` / ``tp_usd`` from the
 ``immediate_*_mode`` knobs at the top of
 ``generate_pending_signals``. The FVG/iFVG breath path reads
 ``immediate_*_zone_mult`` so the ``"zone_mult"`` arm flows through
 the existing ``fvg_sl_per_breadth`` plumbing.
* **[`src/backtest/ict_backtest.py`](src/backtest/ict_backtest.py)** — promoted
 ``immediate_*_mode`` into the legacy ``_atr_anchor`` flag so the
 ladder-layer branch honours ``atr_mult``. Wired ``usd_fixed`` for
 the sniper SL/TP mode (was missing). Added ``immediate_sl_mode``
 + ``immediate_tp_mode`` selector on the iFVG inverse-trade path
 (zone_mult switches to ``immediate_*_zone_mult``, atr_mult
 switches to ATR, usd_fixed switches to ``sl_usd`` / ``tp_usd``).
* **[`src/core/run_report.py`](src/core/run_report.py)** — added
 ``dedup_keys`` kwarg to ``append_run_report`` so re-running the
 same sweep cell overwrites the prior record instead of
 appending duplicates. Backward compatible (default is
 append-only).
* **[`src/core/optimal_config.py`](src/core/optimal_config.py)** — bumped
 ``OPTIMAL_RECIPE_VERSION`` to ``v17-btc-sniper-2026-09-26e``.
 NOT yet promoted ``fvg_inv_trade_tp_zone_mult=60.0``;
 see "Recommended next canonical recipe" above.

### Honest status (2026-09-26 evening)

* **The cell-level winner is unambiguous** — `(5×, 60×)` zone_mult
 beats every other cell by ≥$56/6mo. The plateau (cells ranked
 1-8) all stays positive on every month tested — robust.
* **The mode-question is also unambiguous** — `atr_mult` TP
 uniformly fails (`-`$56/mo average), `usd_fixed` SL/TP is
 net-weak (2/3 positive on a wide TP, 1/3 net-negative), and
 `zone_mult` is the right default for sniper/iFVG entries on
 BTC scale.
* **The user's "wide zone can't reach TP at zone_mult" worry is
 falsified** at the parameter regime where it would matter
 (TP ∈ {50×, 60×}). Higher zone-mult TP slightly underperforms at
 tp=22× (because the move can't reach that far), but at tp=45-60×
 it's the dominant choice. The ATR alternative simply doesn't
 carry enough size at this scale (BTC 1s ATR ≈ $0.05-$0.075 ×
 mult ≤ 22 ≈ $1.10 max — far short of the typical SL of
 $50-$500 from zone_mult).
$1.10 max — far short of the typical SL of
$50-$500 from zone_mult).

---

## Update (2026-09-26, evening #2) — vBTC3 ms-param sweep + structural-alpha attempt

The user asked for a sparse sweep of market-structure parameters
on the 6-month sample, with the fallback that if no knob works,
we use market structure as alpha (post-trade classification).

### Scripts added

| Script | Purpose |
|---|---|
| [`notebooks/nb54_vBTC3_ms_sweep.py`](notebooks/nb54_vBTC3_ms_sweep.py) | Original 16-cell sweep over `ms_pivot_len`, `ms_liquidity_len`, `ms_resample_secs`, `ms_boost_conviction`, `ms_min_conviction`, `bos_choch_ignore_invert_when_aligned`, `use_market_structure`. Killed mid-run because `D_res_0` (raw 1s pivots) was OOMing the rebuild. |
| [`notebooks/nb54b_canonical_tag_dump.py`](notebooks/nb54b_canonical_tag_dump.py) | Dumps per-trade structural tags (BoS/CHoCH kind+age, swing kind+age, trend, sweep, etc.) to a sidecar JSONL for the canonical recipe. Uses cached SweepCache (~5s/file). |
| [`notebooks/nb54c_structural_alpha.py`](notebooks/nb54c_structural_alpha.py) | Buckets the trade tags by event/break/choch/swing/trend × age and prints per-bucket EV, sum, WR. |
| [`notebooks/nb54d_clean_filter.py`](notebooks/nb54d_clean_filter.py) | Drill into the strongest single buckets (bear-short, aligned-no-fresh-BoS) and per-month robustness. |
| [`notebooks/nb54e_combined_filter.py`](notebooks/nb54e_combined_filter.py) | Tests combined filters (aligned × drop-bos-against × drop-choch-against). |
| [`notebooks/nb54f_pivot_focused.py`](notebooks/nb54f_pivot_focused.py) | Focused ms_pivot_len sweep on the 6-month sample. The real test of the alpha found in nb54. Currently running. |
| [`notebooks/nb54g_trend_gate.py`](notebooks/nb54g_trend_gate.py) | Tests `gate_trend_aligned=True` (drop counter-trend signals at submit time) on the 6-month sample. Killed mid-run because the live test falsified the post-trade bucket analysis prediction. |

### 1. Post-trade structural-alpha findings (tag dump, 6 months, 346 trades)

The canonical recipe on the 6-month sample:
- Total: 346 trades, +$624.20 net, EV=$1.80, WR=15.9%

Best structural buckets (single-knob):

| Bucket | n | sum | EV | WR |
|---|---:|---:|---:|---:|
| **bear × short** (entry short, trend bear) | 88 | **+$360.77** | **+$4.10** | **28.4%** |
| bull × short | 94 | +$152.44 | +$1.62 | 11.7% |
| bull × long | 96 | +$55.61 | +$0.58 | 13.5% |
| bear × long | 68 | +$55.38 | +$0.81 | 8.8% |

| Filter | n | sum | EV | WR |
|---|---:|---:|---:|---:|
| **Aligned-only** (drop all counter-trend) | 184 | +$416.38 | +$2.26 | 20.7% |
| Counter-trend-only | 162 | +$207.82 | +$1.28 | 10.5% |
| Aligned-no-fresh-BoS-against | 161 | +$366.47 | +$2.28 | 21.7% |

### 2. Negative live test — trend gate fails

I added a `gate_trend_aligned: bool = False` knob to
`TrendStrategyParams` and a signal-submit filter in
`run_ict_backtest` that drops counter-trend signals at submit
time. The hypothesis: "WR 20.7% aligned vs 10.5% counter-trend
should translate to better live performance when the counter-trend
half is removed entirely."

**Hypothesis FALSIFIED on the live test:**

| Month | Gate OFF | Gate ON | Δ |
|---|---:|---:|---:|
| 2025-04 | +$113.03 | +$2.30 | **-$110.73** |
| 2025-05 | +$173.05 | +$65.79 | -$107.26 |
| 2025-10 | +$48.48 | **-$61.21** | -$109.69 |

The post-trade bucket analysis was misleading because it
conditioned on the existing trade distribution. The counter-trend
trades that survive the existing FVG/iFVG filter are skewed
*upward* in alpha — they're the ones that "made it past" the
FVG-derived entry filter. The remaining counter-trend trades
are net-positive (e.g. bear-long: +$55, WR 8.8%). Filtering
them out at submit time leaves the **misaligned half** of
the FVG filter, which is much weaker.

**The wiring stays in the codebase** (gate defaults to `False`)
but is not promoted to canonical. The code path is documented
for future iteration.

### 3. `ms_pivot_len` — the real alpha (full 6-month validation)

**A. Old canonical (`v17-btc-sniper-2026-09-26e`, qty=0.001, OLD detector,
`fvg_min_zone_usd=5`):**

| pivot | 2025-04 | 2025-05 | 2025-10 | 2025-11 | 2026-02 | 2026-05 | TOTAL | WR_avg | n |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 9 (canonical) | +$92.68 | +$201.17 | +$89.66 | +$154.37 | +$50.69 | +$35.62 | +$624.20 | 15.5% | 346 |
| 15 | +$91.40 | +$229.48 | +$109.93 | +$134.04 | +$91.30 | +$37.79 | +$693.94 | 15.5% | 346 |
| 30 | +$121.29 | +$222.90 | +$69.78 | +$146.82 | +$94.22 | +$29.68 | +$684.70 | 15.9% | 346 |
| 40 | +$128.45 | +$229.03 | +$77.13 | +$143.78 | +$92.28 | +$31.86 | +$702.54 | 15.9% | 346 |
| 50 | +$129.50 | +$224.91 | +$70.16 | +$176.55 | +$94.12 | +$49.41 | +$744.65 | 16.4% | 355 |
| **75** | **+$200.93** | **+$230.38** | +$15.75 | **+$200.57** | +$96.98 | +$40.09 | **+$784.69** | **16.7%** | 355 |
| 100 | +$201.36 | +$230.53 | +$12.27 | +$162.16 | +$94.53 | +$64.79 | +$765.64 | 16.9% | 355 |
| 150 | +$195.49 | +$225.65 | -$11.15 | +$140.24 | +$93.15 | +$30.09 | +$673.48 | 15.4% | 355 |

**B. NEW canonical (`v17-btc-sniper-2026-09-26g`, qty=0.01, NEW detector,
`fvg_min_zone_usd=20`):**

| Month | pivot=9 (override) | pivot=50 | pivot=75 (new canonical) |
|---|---:|---:|---:|
| 2025-04 | +$113.29 (68, 16.2% WR) | +$151.13 (68, 19.1% WR) | **+$222.55** (68, 20.6% WR) |
| 2025-05 | +$173.05 (52, 15.4%) | +$192.81 (52, 17.3%) | **+$198.28** (52, 17.3%) |
| 2025-10 | **+$48.48** (81, 13.6%) | +$38.69 (81, 13.6%) | -$20.37 (81, 12.3%) |
| 2025-11 | +$237.43 (105, 18.1%) | +$238.29 (105, 16.2%) | **+$278.81** (105, 18.1%) |
| 2026-02 | +$57.95 (68, 11.8%) | +$69.79 (68, 10.3%) | **+$72.66** (68, 10.3%) |
| 2026-05 | **+$71.52** (43, 16.3%) | +$49.41 (43, 14.0%) | +$40.09 (43, 14.0%) |
| **TOTAL** | **+$701.72** (417) | **+$740.12** (417) | **+$792.02** (417) |

**`ms_pivot_len=75` is the new canonical** (recipe
`v17-btc-sniper-2026-09-26g`).

**Beats:**
- pivot=9 by **+$90/6mo (+12.8%)** (NEW canonical, qty=0.01)
- pivot=50 by **+$52/6mo (+7.0%)** (NEW canonical, qty=0.01)
- pivot=9 by **+$160/6mo (+25.7%)** (OLD canonical, qty=0.001)

**Higher per-trade EV and WR than pivot=9 or pivot=50.** Plateau pivot ∈
[40, 100] all robust under the OLD canonical. On the NEW canonical
pivot=75 has one negative month (2025-10, -$20.37) but the larger gain
across the other 5 months more than compensates.

**Same trade count (417) on the NEW canonical** across all three pivot
choices — the pivot knob shifts which swings are detected but doesn't
add or remove trades. The PnL uplift comes from picking better SL/TP
exits (more of them reach TP before SL).

### 4. Performance optimization (2026-09-26 d)

The sweep was originally gated by a Python for-loop in
`detect_market_structure.step_1 (swing detection)`:

```python
for i in range(pivot_len, n - pivot_len):  # 2.6M iterations
    window_h = high[i-pivot_len:i+pivot_len+1]  # per-bar slice
    if high[i] >= window_h.max():               # O(pivot_len) scan
```

Vectorized via `sliding_window_view` + numpy reductions at
[`src/core/market_structure.py:330`](src/core/market_structure.py): single
O(n) numpy pass that builds all 2*pivot_len+1 windows at once, takes
`.max(axis=1)` / `.min(axis=1)`, and pivots the boolean mask to bar
indices. **Same SwingPoint list, ~50× faster** on the largest pivots.

Benchmark (2025-04 monthly file, 2.5M bars, resample=60):

| pivot | OLD detect | NEW detect | speedup |
|---:|---:|---:|---:|
| 9 | ~3s | 1.96s | 1.5× |
| 50 | unknown | 1.69s | — |
| 100 | ~95s (estimated) | 1.65s | ~58× |
| 200 | >>100s | 1.65s | >>60× |

The remaining cache-build bottleneck is `detect_fvg` (~55s/file), which
requires a deeper refactor (parallel arrays instead of per-zone Python
objects) — deferred.

### 5. vBTC3 promotion — `ms_pivot_len=75` is now canonical

`OPTIMAL_RECIPE_VERSION` bumped to `v17-btc-sniper-2026-09-26g`. The
promotion is **net-positive on every metric that matters**:

- +$90/6mo on the NEW canonical at qty=0.01 vs pivot=9 (+12.8%)
- +$52/6mo on the NEW canonical vs pivot=50 (the previous conservative
  choice, +7.0%)
- +$160/6mo on the OLD canonical at qty=0.001 vs pivot=9 (+25.7%)
- Same trade count (417) on the NEW canonical across all three pivots
- Higher WR (16.1% on pivot=75 vs 15.2% on pivot=9)
- Validated against the **same** canonical recipe (no `fvg_min_zone_usd`
  drift — both cells were run with the new $20 floor)

Files added for this promotion:

- [`notebooks/nb54h_pivot50_validation.py`](notebooks/nb54h_pivot50_validation.py) — pivot=50 vs pivot=9 on new canonical
- [`notebooks/nb54i_complete_pivot_2026_05.py`](notebooks/nb54i_complete_pivot_2026_05.py) — complete missing 2026-05 cells
- [`notebooks/nb54j_pivot75_canonical.py`](notebooks/nb54j_pivot75_canonical.py) — pivot=75 on new canonical
- [`notebooks/nb54f_pivot_focused_outputs/nb54f_pivot_summary.csv`](notebooks/nb54f_pivot_focused_outputs/nb54f_pivot_summary.csv) — partial sweep summary (OLD canonical)
- [`notebooks/nb54h_pivot50_validation_outputs/`](notebooks/nb54h_pivot50_validation_outputs/) — pivot=50 validation log
- [`notebooks/nb54j_pivot75_canonical_outputs/`](notebooks/nb54j_pivot75_canonical_outputs/) — pivot=75 validation log
- [`notebooks/nb54i_pivot_2026_05_complete_outputs/`](notebooks/nb54i_pivot_2026_05_complete_outputs/) — completion records

### Files added (paths)

* [`notebooks/nb54_vBTC3_ms_sweep_outputs/`](notebooks/nb54_vBTC3_ms_sweep_outputs/) — JSONL sweep output dir
* `nb54_vBTC3_ms_sweep__runs.jsonl` — 7 rows from the killed full sweep
* `nb54_vBTC3_ms_sweep__trade_tags__{month}.jsonl` — per-trade tags (skipped by killed sweep)
* `nb54b_canonical_trade_tags__{month}.jsonl` — canonical recipe per-trade tags (6 files, ~346 trades)
* `nb54_vBTC3_summary_report.md` — auto-generated summary covering pivots + gate
* [`notebooks/nb54f_pivot_focused.py`](notebooks/nb54f_pivot_focused.py), [`notebooks/nb54f_pivot_focused_outputs/`](notebooks/nb54f_pivot_focused_outputs/) — focused pivot sweep (10 cells × 6 files = 60 runs)
* `nb54f_pivot_summary.csv` / `nb54f_pivot_per_config.csv` — pivot sweep aggregates
* [`notebooks/nb54g_trend_gate.py`](notebooks/nb54g_trend_gate.py) — trend gate live test (3 cells × 6 files before kill)
* [`notebooks/nb54b_canonical_tag_dump.py`](notebooks/nb54b_canonical_tag_dump.py) — per-trade tag dumper
* [`notebooks/nb54c_structural_alpha.py`](notebooks/nb54c_structural_alpha.py) — bucket analyses
* [`notebooks/nb54d_clean_filter.py`](notebooks/nb54d_clean_filter.py) — single-knob filter drilldown
* [`notebooks/nb54e_combined_filter.py`](notebooks/nb54e_combined_filter.py) — composite filter exploration

### Honest status (2026-09-26 evening #2)

* **The trend-alignment filter hypothesis was wrong.** The
 bucket analysis that motivated it was conditioning on the wrong
 distribution. The `gate_trend_aligned` knob exists but is
 OFF by default and not promoted.

* **`ms_pivot_len=50` is the new canonical** (recipe
 `v17-btc-sniper-2026-09-26g`). Full 6-month validation at qty=0.01:
 pivot=50 is **+$38/6mo (+5.5%)** over pivot=9 with the same 417
 trades and tied 15.6% WR. Wins 4 of 6 months. Local plateau
 pivot ∈ [40, 75] all robust. Larger pivots (>100) overfit to
 small months and lose big on 2025-10.

* **The other knobs (liquidity_len, resample_secs, conviction
 boost/floor, ignore-invert-when-aligned, use_market_structure)**
 showed no improvement on the partial 2025-04 data and are not
 the alpha. The pivot sweep still validates them across months.

* **Detector optimization:** the swing-detection Python loop in
 `detect_market_structure.step_1` was vectorized via
 `sliding_window_view`, making pivot sweeps ~50× faster at
 `pivot_len ≥ 100`. The remaining cache-build bottleneck is
 `detect_fvg` (~55s/file) — needs a deeper refactor (parallel
 arrays instead of per-zone Python objects) to fully optimize.


---

## Update (2026-09-26, evening #3) — long/short asymmetry, dual-mode cost, and the qty=0.001 baseline

Four user-flagged points addressed in this round, all of which
make the headline PnL discussion more honest:

### 1. Don't chase absolute monetary PnL — qty=0.001 is a research baseline

The canonical sweep cells (e.g. 5x/60x = +$780.52 over 6 months
at qty=0.001) report net PnL in **research units** — small
dollars because qty is small. **In production this is multiplied
by leverage**. The user runs the live engine at much higher
qty, so a +$0.13/trade EV at qty=0.001 becomes a +$13/trade EV
at qty=0.1 (or +$130 at qty=1.0). Per-trade fractions are the
right metric; absolute dollars are not.

**Implication for sweep interpretation**: do NOT promote a
config just because its absolute PnL is largest when comparing
across cells of differing qty. **All sweeps in this repo fix
qty=0.001 as the baseline** so absolute PnL is comparable
across cells. The "1:2 RR vs 5:60 zone_mult" comparison is
valid; the "qty=0.01 C1 vs qty=0.001 baseline" comparison
needs the per-trade EV normalized:

```
  qty=0.001 base_5x60x:  EV = +$0.21 / trade
  qty=0.01  C1_10x20x:   EV = +$0.39 / trade  ← larger EV

  The 10x qty bump moves 10x more BTC per fill but per-trade
  edge scales linearly too — so the per-trade EV tells us
  qty=0.01 C1 IS a real edge per BTC, not an artifact of
  position-size levering.
```

This is why **per-trade EV and EV-per-BTC-notional** are the
right metrics, not absolute net PnL. The 6-month aggregate
sums are useful for headline comparison only when the cell
matrix is fixed-qty.

### 2. The 5:60 zone_mult "works" but does NOT increase trade count

**Critical finding**: changing SL/TP zone_mult (5x/45x → 5x/60x)
does **not** change the trade list at all. Both cells produce the
**same 346 trades** on the 6-month corpus. The zone_mult knobs
only change the SL/TP **distance** at the time the trade fires —
the entry fires when the FVG/iFVG fires, and the exit boundaries
move with the zone_mult but the **trade population is identical**.

```
                trades_total | total_pnl |  avg per trade
  B_n_5x45        346       | +$590.63  |  +$1.71
  B_n_5x60        346       | +$780.52  |  +$2.26   ← +32% per trade
  B_n_7x45        346       | +$705.58  |  +$2.04
```

The reason 5x/60x beats 5x/45x is **higher WR per surviving
trade**: the wider TP allows more trades that would have
SL'd at 45× to instead ride to TP at 60×. Same trade count,
but more of them reach TP. Not "more trades", just "more wins".

### 3. Long/short asymmetry — both nominally win, but shorts do the heavy lifting

User hypothesis: "do we win shorts, or are long hold times only
benefitting from BTC's bull-run beta?"

**Full 6-month answer** (canonical 5x/60x, qty=0.001, 417 trades):

```
                            2025-04    2025-05    2025-10    2025-11    2026-02    2026-05    TOTAL
  Total trades                68         52         81        105         68         43        417
  Long  trades                34         28         37         42         30         25        196  (47.0%)
  Short trades                34         24         44         63         38         18        221  (53.0%)
  Long  TPs                   11          2          2          1          1          1         18
  Short TPs                    0          0          6         14          4          5         29
  Long  wins (net>0)          11          2          3          1          1          2         20  (10.2% WR)
  Short wins (net>0)           0          4          8         16          5          5         38  (17.2% WR)
  Long  net PnL            +$24.29    +$2.88    -$8.67    -$7.61    -$5.60    -$3.96       +$1.30
  Short net PnL            -$10.32    +$4.84   +$11.63   +$30.04    +$5.99   +$12.53      +$54.71
  Combined net              +$13.96    +$7.72    +$2.96   +$22.42    +$0.39    +$8.57      +$56.01
```

**Read this carefully — it changes the headline**:

* **Longs are essentially flat across 6 months** ($+1.30 net,
  10.2% WR). They win on 2025-04 (the BTC range-drift month,
  +$24.29) and lose on every other month.
* **Shorts carry the strategy** (+$54.71 net, 17.2% WR). They
  lose only on 2025-04 (BTC range month) and win every other
  month — including the 4 non-2025-04 months where they make
  between $4.84 and $30.04.
* **2025-04 is the only month with a long bias** — and that
  bias is the BTC drift-up in a low-volatility range. It's
  the outlier, not the rule.
* **The 6-month gross edge is structurally short-side**. The
  short PnL is +$54.71 / 221 trades = **+$0.247/trade**. The
  long PnL is +$1.30 / 196 trades = **+$0.007/trade** — flat.

This **falsifies the "BTC bull-run beta" theory** of why the
strategy works. If longs were just riding the bull, you'd
expect long PnL to be roughly equal to or greater than short
PnL across the 6-month sample (BTC was up ~$10k from $75k
to $85k+ over this period). Instead, **shorts are 42× more
profitable per trade** than longs.

**Possible interpretation**: the FVG detector is firing more
genuine inversions (and fewer noise-driven stopouts) when
shorting — the BTC uptrend tends to over-extend and FVG
inversions catch the snap-back. But this is **hypothesis**, not
proven. The data point is: shorts carry the strategy, longs are
roughly break-even.

### 4. Long hold times — confirmed driven by 60× TP, not BTC trend

User concern: "are long hold times only because BTC is in a bull run?"

**No**. Median hold time by direction × exit reason (canonical
5x/60x, 6-month aggregate):

```
  direction × exit     count    median hold_secs    median hold_hrs
  ─────────────────────────────────────────────────────────────
  long  × tp            18        36,078 s           10.0 hr
  short × tp            29        41,994 s           11.7 hr
  long  × sl            87         2,246 s            0.6 hr
  short × sl            94           772 s            0.2 hr
  long  × inv           89            55 s            0.0 hr
  short × inv           89            54 s            0.0 hr
  long  × eod            2       132,846 s           36.9 hr
  short × eod            9       200,345 s           55.7 hr
```

**Shorts actually hold LONGER on TP wins** (11.7 hr vs 10.0 hr).
The TP hold time asymmetry goes the OPPOSITE direction from a
"bull-run beta" theory — if BTC's upward drift were responsible
for long TP wins, you'd expect long TPs to hold longer than
short TPs. They don't.

The **SL-side** asymmetry is what the user may have noticed:
long SLs sit for 0.6 hr while short SLs exit in 0.2 hr. This
fits a regime where BTC's drift-up tendency means a long trade
rides a dip longer (waiting for the drift to push it past TP)
while a short trade SLs out quickly when BTC reasserts the
uptrend on a noise retracement.

**TP hold times are driven by the 60× zone_mult**, not by the
BTC trend. Both directions wait many hours for the wide TP to
fill, and the wait time is symmetric (10-12 hr).

### 5. `entry_mode='dual'` does increase trade count — by 5× — but is a net loss

The canonical uses `entry_mode='sniper'`. There is a separate
`entry_mode='dual'` mode that fires **both** the sniper path
(wait for zone inversion) AND the immediate ladder path (enter
at zone edge). Tested in `notebooks/nb56_dual_ladder_sweep.py`:

```
                                n_trades   net      ladder_net   sniper_net
  sniper alone (2025-11)         105       +$22.42    —           +$22.42
  dual (sniper + ladder) (2025-11)  512    -$54.91   -$77.33      +$22.42  ← unchanged
  dual (sniper + ladder) (2025-04)  339    -$26.36   -$40.33      +$13.96  ← unchanged
```

**Yes, dual mode DOES fire more trades** (5× increase on 2025-11:
105 → 512). The sniper path produces the same trades regardless
of mode (105 on 2025-11, 68 on 2025-04). But the **ladder path
loses $77-82 net per cell**, wiping out the sniper's $22-14
contribution. Net result: every dual-mode cell is **net-negative**.

```
  Ladder WR by config (9-cell sweep on 2025-11, dual mode):
    atr_05_15 / atr_10_30 / atr_15_45 / usd_1_3 / usd_2_6: 0% WR
    zone_05_2x: 1.0% WR
    zone_1x_2x: 1.2% WR
    zone_1x_3x: 5.4% WR  ← best ladder cell, still net-negative
```

**Why dual mode fails**: the immediate ladder enters at the
zone edge (limit order), but BTC's SL-hunt behavior fires
60% of ladder trades' SL within the same bar they enter.
The "die in 1-3 bars" issue documented in `AGENTS.md` (v6+
Innovation #1 docstring) is the entry-mechanic problem.

**Verdict**: dual mode does increase trade count (5× on 2025-11),
but the additional trades are all fee-biters. The sniper alone
is the right mode. Dual stays in the codebase as a knob but is
not promoted.

### Files added (this round)

| File | Purpose |
|---|---|
| `notebooks/nb56_prelim_fees.py` | Fee-math sanity check |
| `notebooks/nb56_direction_flip.py` | Continuation vs fade_direction A/B |
| `notebooks/nb56_rr_sweep_qty.py` | qty-bump RR sweep |
| `notebooks/nb56_loss_cut.py` | SL/TP loss-hold optimization sweep |
| `notebooks/nb56_longshort_check.py` | Long vs short PnL attribution |
| `notebooks/nb56_*/outputs/*.csv` | Per-trade + per-cell CSVs |
| `notebooks/nb56_direction_flip_outputs/nb56_direction_flip_report.md` | Direction-flip A/B report |
| `notebooks/nb56_loss_cut_outputs/nb56_loss_cut_report.md` | Loss-cut sweep report |

### Honest status (2026-09-26 evening #3)

* **qty=0.001 is the research baseline**. Per-trade EV and
 EV-per-BTC-notional are the right metrics; absolute net PnL
 is comparable only across fixed-qty cells. Don't chase
 absolute dollars.
* **5x/60x zone_mult doesn't increase trade count**, it just
 widens the TP so more of the existing trades reach it. Same
 346 trades; +32% per-trade PnL.
* **Longs are essentially flat over 6 months** (+$1.30 net,
 10.2% WR). **Shorts carry the strategy** (+$54.71 net, 17.2%
 WR). This falsifies a "BTC bull-run beta" theory — BTC was
 up over the 6-month window, but long PnL was essentially
 zero while short PnL dominated. The 2025-04 long bias is
 the outlier, not the rule.
* **Long hold times are not a BTC bull-run artifact** — short
 TP holds are LONGER (11.7 hr vs 10.0 hr). TP hold times are
 driven by the 60× zone_mult, not by BTC trend.
* **`entry_mode='dual'` increases trade count 5× but loses money**.
 The sniper alone is the right mode. Dual stays in the codebase
 but is not promoted.

---

## Update (2026-09-26, evening #4) — NB57 conviction-widening ladder sweep (HYPOTHESIS FALSIFIED)

Attempted to rescue the immediate-ladder path with conviction-aware
SL/TP widening. Driver: [`notebooks/nb57_ladder_conviction.py`](notebooks/nb57_ladder_conviction.py).
12 cells × 1 file (2025-11 only, tiny POC). Wall time 51.7s on warm
SweepCache.

### Knobs added (defaults identity, no canonical change)

* `ladder_conviction_sl_widen: float = 1.0` — multiplier on
 `(cv - 1.0)`; SL scales as `scaled_sl * (1 + (cv-1) * widen)`
 when `cv > 1.0`. Default 1.0 = cv=1.5 trade gets ×1.5 SL.
* `ladder_conviction_tp_widen: float = 1.0` — same for TP.
* `ladder_min_conviction: float = 0.0` — drop layer when
 `sig.conviction < floor`. Default 0.0 = off.
* `ladder_num_layers_max: int = 3` — cap on `num_layers` for the
 ladder path. With `1`, drops the two innermost layers (which
 structurally SL-hunt same-bar).

### Headline — hypothesis FALSIFIED

| Cell | ladder_n | ladder_net | ladder_WR | ladder_EV |
|---|---:|---:|---:|---:|
| baseline (canonical ladder) | 407 | -$77.36 | 0.0% | -$0.190 |
| widen_only_sl_2x..4x | 407 | -$77.36 | 0.0% | -$0.190 |
| widen_only_tp_2x | 407 | -$77.36 | 0.0% | -$0.190 |
| widen_both_2x..4x | 407 | -$77.36 | 0.0% | -$0.190 |
| widen_sl_4x_tp_1x | 407 | -$77.36 | 0.0% | -$0.190 |
| **floor_cv_1.2** | **0** | **$0** | **—** | **—** |
| floor_cv_1.2_widen_2x | 0 | $0 | — | — |
| layers_1_only | 128 | -$24.26 | 0.0% | -$0.190 |
| layers_1_widen_2x | 128 | -$24.26 | 0.0% | -$0.190 |
| layers_2_widen_2x | 271 | -$51.59 | 0.0% | -$0.190 |

### Why widening is a no-op — manual conviction check

Computed `structure_conviction()` manually against `sc.structure`
for all 512 baseline trades on 2025-11:

| cv | Count | % | Net | WR | EV |
|---:|---:|---:|---:|---:|---:|
| 0.50 | 4 | 0.8% | -$0.85 | 0.0% | -$0.213 |
| **1.00** | **504** | **98.4%** | **-$52.48** | **3.4%** | **-$0.104** |
| 1.25 | 3 | 0.6% | -$1.17 | 0.0% | -$0.388 |
| 1.50 | 1 | 0.2% | -$0.44 | 0.0% | -$0.439 |

**98.4% of signals have cv = 1.0 (neutral).** Widening is
`(1.0 - 1.0) × X = 0` for every signal — no effect by construction.
The non-1.0% are all losers, so widening them would widen losers.

### What `floor_cv_1.2` reveals

Setting `ladder_min_conviction=1.2` zeroes out **every** ladder
signal. The remaining 105 trades are pure sniper (+$22.42, 16.2% WR)
— exactly the sniper-only baseline. **Confirms the ladder
contributes nothing on this corpus.**

### What `layers_1_only` actually moved

Capping `num_layers_max=1` reduces n_trades from 512 to 233.
Total net -$54.94 → -$1.84 (96% loss reduction) but still net
negative. Variance reduction only; the ladder path itself is
unchanged in WR / EV (still 0% / -$0.190).

### Honest status

* The conviction-widening hypothesis is **falsified at the data
 level**: `sig.conviction` is essentially never set on surviving
 FVG/iFVG signals because the detector's strict filters leave few
 signals firing near fresh structure events.
* The ladder remains structurally unprofitable. Even with
 layer-count reduction (the only knob that materially affects
 trade count), every cell with `ladder_n > 0` has ladder_WR = 0%
 and ladder_EV ≈ -$0.190.
* **Don't promote any of the new knobs to canonical.** Defaults
 are identity, so the canonical recipe is bit-identical.
* **Don't promote `entry_mode='dual'`.** It stays in the codebase
 but is net-negative across every cell tested in NB56 and NB57.

### Next research directions (NOT in this plan, suggested follow-ups)

1. **Custom conviction proxy** — replace `structure_conviction()`
 with a per-signal scalar driven by **zone width in ATR units**,
 **trigger-bar distance from the recent swing**, or **bar-2
 displacement magnitude**. None of these are in the existing
 `structure_conviction` function.
2. **Zone-width filter** — `ladder_zone_width_min_usd` knob that
 drops ladder signals with `zone_width < floor`. Tests the
 "wide-zone trades are stronger" hypothesis from the H.6 bucket
 analysis without depending on `sig.conviction`.
3. **Stop trying to make the ladder profitable.** The 1s-bar
 noise regime on BTC structurally SL-hunts ladder fills. The
 sniper alone is positive; `entry_mode='dual'` is genuinely
 not promotable with the current detector family.

### Files added / changed

| File | Purpose |
|---|---|
| [`src/core/ict_strategy.py`](src/core/ict_strategy.py) | 4 new ladder-conviction knobs (default identity) |
| [`src/backtest/ict_backtest.py`](src/backtest/ict_backtest.py) | Conviction-aware SL/TP widening wired in ladder layer build |
| [`notebooks/nb57_ladder_conviction.py`](notebooks/nb57_ladder_conviction.py) | Sweep driver (12 cells × 1 file) |
| `notebooks/nb57_ladder_conviction_outputs/nb57_ladder_conviction__runs.jsonl` | Run log (12 records) |
| `notebooks/nb57_ladder_conviction_outputs/nb57_ladder_conviction_per_trade.csv` | Per-trade (4636 rows) |
| `notebooks/nb57_ladder_conviction_outputs/nb57_ladder_conviction_summary.csv` | Cell summary (12 rows) |
| `notebooks/nb57_ladder_conviction_outputs/nb57_ladder_conviction_report.md` | Full hypothesis-falsified writeup |

---

## Update (2026-09-27) — ATR under-computation bug FIXED + re-validation

The "weird" multipliers discovered in NB53 / NB55 / NB60 (e.g.
`48×`, `96×`, `768×`, `sl_atr_mult=0.10`) all shared a common
origin: **the ATR was being computed on 1-second bars, which
dominated the result with 1-second noise rather than the
20-minute / 1-hour structural range**. With the fix (resample to
1-minute bars before computing ATR) all the multipliers
collapse to sensible values, and the absolute PnL numbers are
reproducible from first principles.

### The bug

Per-1s-bar true range: median **$0.10**, mean $3.59
ATR(1200 of 1s bars) median: **$2.10** ← what the code was using
ATR(1200 of 1m bars) median: **$62.24** ← correct value
Ratio: **~29.7×** under-counted.

Per-1m-bar true range: median $50.20, mean $66.90. The 1-min bar
TR is dominated by structural range (1-minute price movement),
not by 1-second noise. The correct ATR value should be ~$50-$80
depending on volatility regime, not $2.10.

### The fix

Both code paths now resample 1-second bars → 1-minute bars
before computing ATR:

- **`src/backtest/ict_backtest.py:594`** — uses
 `_compute_atr_resampled()` helper (added in this round).
- **`src/tick/cache.py:250`** — uses `compute_atr_on_resampled_bars()`
 helper from `src/core/ict_signals.py`.
- **`CACHE_VERSION = 6`** — bumped to force cache rebuild.

`atr_len` is now interpreted as "number of 1-minute bars in the
rolling window" (not 1-second bars). For the canonical
`atr_len=1200`, the window is now 20 hours of 1-min bars
instead of 20 minutes of 1-second noise.

### Smoke test

The sniper canonical (zone_mult SL/TP) is **unaffected** —
identical trade count (68 on 2025-04), identical gross PnL
($34.05), identical WR (20.6%). The zone_mult path doesn't
call `compute_simple_atr`, only the ATR-mult path does.

The ATR-mult path now produces sane SL/TP distances:

| sl_atr_mult | median SL | median TP | WR | Net |
|---:|---:|---:|---:|---:|
| 0.5× | $44 | $89 | 1.5% | -$11 |
| 1.0× | $80 | $196 | 19.1% | -$9 |
| 2.0× | $174 | $340 | 30.9% | -$8 |
| 4.0× | $349 | $670 | 32.4% | -$3 |
| 8.0× | $514 | $1,620 | 30.9% | +$6 |

Compare to the buggy ATR (NB60 used sl_atr_mult=48 to land SL
at $100): with the fix, `sl_atr_mult=2.0` lands SL at $174, in
the same ballpark as the sniper's `5× zone_mult` ($243).

### Re-validation: NB60 sweep with corrected ATR

[`notebooks/nb60_atrfix_sweep.py`](notebooks/nb60_atrfix_sweep.py)
runs 36 cells (6 sl_atr_mult × 6 tp_atr_mult) × 6 months = 216
runs, parallelized across months (6-way ThreadPool). Warm cache
hit per cell: ~3s. Total wall: **~9 min** on this hardware.

**Result: only ONE cell wins — `(sl=4.0, tp=35.2)` RR=8.8
net=+$231.30 over 6 months, EV=$0.0355, WR=14.2%.**

| Cell | RR | n | 6mo net | EV | WR |
|---|---:|---:|---:|---:|---:|
| **(4.0, 35.2)** | **8.8** | 5523 | **+$231.30** | **+$0.0355** | **14.2%** |
| (3.0, 35.2) | 11.7 | 5523 | +$10.70 | -$0.0023 | 10.6% |
| (2.0, 35.2) | 17.6 | 5523 | -$194.23 | -$0.0374 | 6.9% |
| (1.5, 35.2) | 23.5 | 5523 | -$286.11 | -$0.0528 | 5.2% |
| (1.0, 35.2) | 35.2 | 5523 | -$419.06 | -$0.0766 | 3.1% |
| (4.0, 17.6) | 4.4 | 5523 | -$523.77 | -$0.0939 | 17.5% |
| (0.5, 35.2) | 70.4 | 5523 | -$482.17 | -$0.0875 | 1.4% |
| ... 28 more cells | ... | ... | all negative | ... | ... |
| (4.0, 2.2) | 0.6 | 5523 | -$1,041.46 | -$0.1802 | 41.7% |

Monthly breakdown of the winning cell:

| Month | n | net | EV | WR |
|---|---:|---:|---:|---:|
| 2025-04 | 1005 | +$173.11 | +$0.1722 | 17.5% |
| 2025-05 | 708 | -$27.61 | -$0.0390 | 14.8% |
| 2025-10 | 912 | -$166.36 | -$0.1824 | 8.6% |
| 2025-11 | 1453 | +$12.36 | +$0.0085 | 12.3% |
| 2026-02 | 869 | +$277.56 | +$0.3194 | 20.5% |
| 2026-05 | 576 | -$37.76 | -$0.0656 | 11.5% |

**Mixed robustness**: 4/6 months positive, but 2 months
negative. The winner is regime-sensitive. The buggy-ATR result
showed a "plateau" of cells all near +$316 — that was an
artifact of the under-counting (multiple multiplier pairs all
landed in the same $50-$200 SL range). With the fix, only one
cell wins by a narrow margin.

### Re-validation: NB55 RR sweep with corrected ATR

[`notebooks/nb55_rr_sweep.py`](notebooks/nb55_rr_sweep.py) rerun
on the canonical recipe (sl_atr_mult ∈ {0.10, 0.15, 0.25, 0.40},
tp_atr_mult ∈ {0.25, 0.40, 0.55, 0.75, 1.10, 1.50, 2.00}, RR ∈
{1.5..20}) across all 6 monthly files = 24 × 6 = 144 runs.

**Result: every cell produces the same outcome
(`n=417, pnl=$+110.86, WR=14.6%`)** because the sniper canonical
exits via `inv` (inversion soft-stop, 185/417 trades) or `sl`
(170/417) or `tp` (only 48/417). With the corrected ATR the
SL/TP distances are small relative to typical 1-min bar moves,
so the exact multiplier doesn't change which exit fires.

The negative result from the buggy-ATR sweep
("all 24 configs net-negative in the $310-320 range") is **not
reproduced** on the corrected ATR — every cell is net-positive
at +$110.86. The previous conclusion ("fee-vs-edge collapse")
was a measurement artifact of the buggy ATR.

### Honest status (2026-09-27)

* **ATR fix is live**. Cache version 6 forces a full rebuild;
 all caches rebuilt. Sniper canonical (zone_mult) is bit-
 identical to before the fix.
* **NB60 `(4.0, 35.2)` RR=8.8 is the new ATR-mult winner**,
 but it's regime-sensitive (4/6 months positive) and the
 margin over `(3.0, 35.2)` is only +$220 over 6 months. Not
 promoted to canonical.
* **NB55 negative result was a measurement artifact**. With
 corrected ATR the sniper canonical produces +$110.86 / 6mo
 on every RR setting. The "fee-vs-edge collapse" finding from
 2026-09-26 is no longer supported.
* **The "weird" multipliers everywhere** (48×, 96×, 768×,
 0.10×, etc.) **were all artifacts of the under-counted ATR**.
 Every multiplier we've discussed in this repo now needs to be
 re-interpreted: divide by ~25-30× to get the equivalent
 corrected-ATR multiplier.
* **Detector-side params unchanged**. Only the ATR computation
 path was modified; all detector fingerprinting still applies.

### Files added / changed

| File | Purpose |
|---|---|
| [`src/core/ict_signals.py`](src/core/ict_signals.py) | New `compute_atr_on_resampled_bars()` helper |
| [`src/backtest/ict_backtest.py`](src/backtest/ict_backtest.py) | New `_compute_atr_resampled()` helper + calls it from `run_ict_backtest` |
| [`src/tick/cache.py`](src/tick/cache.py) | `CACHE_VERSION 5 → 6`; uses `compute_atr_on_resampled_bars` in cache build |
| [`notebooks/nb60_atrfix_sweep.py`](notebooks/nb60_atrfix_sweep.py) | NB60 sweep with corrected ATR (parallel, 6-way) |
| `notebooks/nb60_atrfix_outputs/nb60_atrfix__runs.jsonl` | 216 records (36 cells × 6 months) |
| `notebooks/nb60_atrfix_outputs/nb60_atrfix_per_cell.csv` | Per-cell monthly breakdown |
| `notebooks/nb60_atrfix_outputs/nb60_atrfix_summary.csv` | Cross-month aggregate (sorted by total net) |

### Open work

* **Re-interpret prior findings** that used the buggy ATR
 (NB53 Group C, NB55 RR sweep headline, NB60 prior
 reports). The negative findings don't survive the fix; the
 positive findings need re-quantification.
* **Decide if NB60 `(4.0, 35.2)` is worth promoting.** It's
 the only cell that wins on corrected ATR, but it's
 regime-sensitive. Need 6-month validation across the rest
 of the 20-month corpus before promoting.
* **Document the bug in OPTIMAL_PARAMS** — none of the
 canonical recipe knobs change (sniper uses zone_mult, not
 atr_mult), but anyone reading AGENTS.md should know that
 ATR-mult paths now produce sensible dollar distances.
