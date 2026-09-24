"""nb51_sniper_viz.py — SNIPER mechanism visualization (BTC tick-native).

BTC fork of the XAUUSD nb51 sniper-viz. Differences vs the gold
version:

1. **Hardcoded raw aggTrades path** — points at the BTCUSDT raw
   tick files in the SOURCE repo (ict_tier_v2). NOT copied to
   the destination. Configure via ``RAW_TICKS_PATH`` below.

2. **Tick-fill hybrid backtest** — uses
   ``src.tick.tick_backtest.run_tick_backtest`` instead of
   ``run_ict_backtest`` directly. Bars are aggregated from raw
   ticks inside the call.

3. **Candles + tick scatter overlay** — each panel renders 1m
   candles (from aggregated 1s bars) PLUS a scatter overlay of
   the raw aggTrades within the panel window. The ticks are
   coloured by ``is_buyer_maker`` (red = taker-sold, green =
   taker-bought) so the directional microstructure is visible
   alongside the sniper entry.

4. **BTC-scale defaults** — the panel SL/TP lines reflect the
   canonical v17 BTC recipe (SL=2x zone, TP=22x zone, scaled to
   the BTC zone widths seen in the sampled days). The
   ``VIZ_SL_MULT`` and ``VIZ_TP_MULT`` overrides are still
   honoured if set.

Picks VIZ_N_WINNERS guaranteed TP-exit winners + fills the rest
with random exits so panels aren't all losers.

Loops through seeds until it finds one with >= VIZ_N_TRADES sniper
trades (so an unlucky seed doesn't produce empty/insufficient
panels).

Usage:
    python notebooks/nb51_sniper_viz.py
"""
import os, sys
os.environ.setdefault('MPLBACKEND', 'Agg')
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.dates as mdates
import matplotlib.ticker as mticker
import matplotlib.patheffects as pe
import random as _r

ROOT = None
for _candidate in [Path('.').resolve(), *Path('.').resolve().parents]:
    if (_candidate / 'src' / 'core' / 'ict_signals.py').is_file():
        ROOT = _candidate
        break
if ROOT is None:
    raise RuntimeError('Could not find ICT repo root (no src/core/ict_signals.py)')
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / 'notebooks'

from src.core.optimal_config import optimal_params
from src.tick.tick_backtest import run_tick_backtest, load_raw_aggtrades
from src.tick.aggtrade_aggregator import aggregate_ticks_to_1s_bars
import pyarrow.dataset as ds
import pyarrow.parquet as pq

# ─── HARDCODED DATA PATH ───────────────────────────────────────────
# Raw BTCUSDT aggTrades parquet files live in the SOURCE repo
# (ict_tier_v2/data/binance_um_aggtrades/raw/). We do NOT copy
# tick data into the destination repo. Change this constant if
# the data is relocated.
SOURCE_DATA_ROOT = Path(r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw')
# Pick a single monthly file as the corpus for the viz (smaller
# files are fastest). The daily files in 2026-09 are ~30MB each
# and the monthly files are ~500MB-2GB. We start with the smallest
# monthly file for fast iteration.
RAW_TICKS_PATH = SOURCE_DATA_ROOT / 'BTCUSDT-aggTrades-2025-06.parquet'

# ─── knobs ──────────────────────────────────────────────────────────
VIZ_N_TRADES = 5
VIZ_WINDOW_HOURS = 3.0     # ±1.5h each side = 3h total per panel
VIZ_N_HOURS = 36           # load this many hours of raw ticks for the sample
VIZ_SEED = 20260918        # change for fresh random sample
VIZ_MAX_SEED_TRIES = 30
# SNIPER SL/TP multipliers — v17 BTC recipe defaults (2x SL, 22x TP)
VIZ_SL_MULT = 2.0
VIZ_TP_MULT = 22.0
VIZ_N_WINNERS = 2          # guaranteed TP-exit winners in sample

# ─── locate trades in the corpus ───────────────────────────────────
ds_ = ds.dataset(str(RAW_TICKS_PATH), format='parquet')

# Read the ts column to find the date range of the file
_ts_table = ds_.to_table(columns=['ts']).to_pandas()
_file_min_ts = _ts_table['ts'].min()
_file_max_ts = _ts_table['ts'].max()
_file_n_rows = len(_ts_table)
del _ts_table

# Build hour-resolution grid for the file's date range
all_hours = pd.date_range(
    _file_min_ts.ceil('h'),
    _file_max_ts.floor('h'),
    freq='h',
)


def _pick_eligible_hours(seed: int, n_hours: int) -> list:
    """Return the first ``n_hours`` trading hours under ``seed``."""
    _r.seed(seed)
    hours_shuffled = list(pd.DatetimeIndex(all_hours))
    _r.shuffle(hours_shuffled)
    eligible = []
    for h in hours_shuffled:
        n = ds_.to_table(
            columns=['price'],
            filter=(ds.field('ts') >= h) & (ds.field('ts') < h + pd.Timedelta(hours=1)),
        ).num_rows
        if n >= 100:        # 100 ticks minimum per hour to be eligible
            eligible.append(h)
        if len(eligible) >= n_hours:
            break
    return eligible


def _load_hours(hours: list) -> pd.DataFrame:
    """Concatenate ``hours`` into a single raw tick DataFrame.

    Returns the aggTrade schema (ts, price, quantity, is_buyer_maker, ...).
    """
    chunks = []
    for h in hours:
        t = ds_.to_table(
            columns=['ts', 'price', 'quantity', 'is_buyer_maker'],
            filter=(ds.field('ts') >= h) & (ds.field('ts') < h + pd.Timedelta(hours=1)),
        ).to_pandas()
        chunks.append(t)
    df = pd.concat(chunks, ignore_index=True).sort_values('ts').reset_index(drop=True)
    return df


def _count_snipers(raw_df: pd.DataFrame) -> int:
    """Run a quick tick backtest, return the sniper-trade count."""
    p = optimal_params(
        fvg_inv_trade_sl_zone_mult=VIZ_SL_MULT,
        fvg_inv_trade_tp_zone_mult=VIZ_TP_MULT,
    )
    res = run_tick_backtest(raw_df, p, strategy_label='seed_trial')
    return sum(1 for t in res.trades
               if t.entry_triggered_by == 'sniper' and t.fvg_zone is not None)


# ─── find a seed with enough sniper trades ──────────────────────────
print(f'Searching for a seed with >= {VIZ_N_TRADES} sniper trades '
      f'(max tries: {VIZ_MAX_SEED_TRIES})...')
print(f'Raw tick corpus: {RAW_TICKS_PATH.name} ({_file_n_rows:,} ticks, '
      f'{_file_min_ts} -> {_file_max_ts})')

found_seed = None
found_n = 0
for _seed_offset in range(VIZ_MAX_SEED_TRIES):
    _trial_seed = VIZ_SEED + _seed_offset
    _trial_hours = _pick_eligible_hours(_trial_seed, VIZ_N_HOURS)
    if len(_trial_hours) < VIZ_N_HOURS:
        continue
    _df_trial = _load_hours(_trial_hours)
    _n_snipers = _count_snipers(_df_trial)
    if _n_snipers >= VIZ_N_TRADES:
        found_seed = _trial_seed
        found_n = _n_snipers
        print(f'  Seed {found_seed} (try #{_seed_offset+1}): '
              f'{_n_snipers} sniper trades — using it.')
        break
    else:
        print(f'  Seed {VIZ_SEED + _seed_offset} (try #{_seed_offset+1}): '
              f'only {_n_snipers} sniper trades, retrying...')

if found_seed is None:
    raise RuntimeError(
        f'No seed produced >= {VIZ_N_TRADES} sniper trades in '
        f'{VIZ_MAX_SEED_TRIES} tries. Increase VIZ_N_HOURS or '
        f'VIZ_MAX_SEED_TRIES.')

# ─── reload with confirmed seed ─────────────────────────────────────
print(f'\nUsing seed {found_seed}, hours '
      f'[{_pick_eligible_hours(found_seed, VIZ_N_HOURS)[0]} -> '
      f'{_pick_eligible_hours(found_seed, VIZ_N_HOURS)[-1]}]')
eligible_hours = _pick_eligible_hours(found_seed, VIZ_N_HOURS)
raw_df = _load_hours(eligible_hours)
print(f'Loaded {len(raw_df):,} raw ticks across {len(eligible_hours)} hours')

# ─── run tick-fill hybrid backtest ──────────────────────────────────
print(f'Running BTC SNIPER tick-fill backtest '
      f'(SL={VIZ_SL_MULT}x zone, TP={VIZ_TP_MULT}x zone)...')
p = optimal_params(
    fvg_inv_trade_sl_zone_mult=VIZ_SL_MULT,
    fvg_inv_trade_tp_zone_mult=VIZ_TP_MULT,
)
res = run_tick_backtest(raw_df, p, strategy_label='nb51_sniper_btc')
s = res.summary()
print(f'Total trades: {len(res.trades)}, '
      f'sniper_submitted={res.n_sniper_submitted}, '
      f'sniper_triggered={res.n_sniper_triggered}, '
      f'sniper_cancelled={res.n_sniper_cancelled}, '
      f'sniper_expired={res.n_sniper_expired}')
print(f'PnL: ${s["pnl_total"]:+.2f}, EV/trade: ${s["ev_per_trade"]:+.4f}, '
      f'WR: {s["win_rate"]:.1%}')

# Aggregate the bars once for the per-panel candle rendering
# (the backtest did this internally but didn't expose the bars).
print(f'Aggregating ticks for candle rendering...')
bars_df = aggregate_ticks_to_1s_bars(raw_df)
print(f'  -> {len(bars_df):,} 1s bars')
# Stash on res so the per-panel render can pick it up
res._bars_df = bars_df

# Build per-trade DataFrame with live FvgZone reference.
trade_rows = []
for t in res.trades:
    trade_rows.append({
        'entry_bar':    int(t.entry_bar),
        'exit_bar':     int(t.exit_bar),
        'entry_time':   pd.to_datetime(t.entry_time, unit='ns', utc=True),
        'exit_time':    pd.to_datetime(t.exit_time, unit='ns', utc=True),
        'direction':    int(t.direction),
        'entry_price':  float(t.entry_price),
        'exit_price':   float(t.exit_price),
        'stop_usd':     float(t.stop_usd),
        'target_usd':   float(t.target_usd),
        'exit_reason':  str(t.exit_reason),
        'pnl_usd':      float(t.pnl_usd),
        'fee_usd':      float(getattr(t, 'fee_usd', 0.0)),
        'taker_bps_charged': float(getattr(t, 'taker_bps_charged', 0.0)),
        'hold_secs':    float(t.hold_secs),
        'lots':         float(t.lots),
        'layer_idx':    int(t.layer_idx),
        'entry_triggered_by': str(t.entry_triggered_by),
        'fvg_zone':     t.fvg_zone,
        'entry_alignment': str(getattr(t, 'entry_alignment', '')),
    })
trades_df = pd.DataFrame(trade_rows)

sniper_df = trades_df[trades_df['entry_triggered_by'] == 'sniper'].reset_index(drop=True)
_has_anchor = sniper_df['fvg_zone'].apply(lambda z: z is not None)
candidates = sniper_df[_has_anchor].reset_index(drop=True)
print(f'Sniper trades with anchor: {len(candidates)}')
print('Exit reasons:', candidates['exit_reason'].value_counts().to_dict())

# ─── pick N sniper trades ──────────────────────────────────────────
# Guarantee VIZ_N_WINNERS winners (TP exits) so the panels aren't
# all losers; fill the remaining slots with random picks across all
# sniper exits.
assert len(candidates) >= VIZ_N_TRADES, (
    f'Seed retry succeeded but candidate count dropped to {len(candidates)} '
    f'(should be >= {VIZ_N_TRADES}); investigate the trial counter.')
_r.seed(found_seed + 1)

winners = candidates[candidates['exit_reason'] == 'tp']
losers = candidates[candidates['exit_reason'] != 'tp']

n_winners_take = min(VIZ_N_WINNERS, len(winners))
n_losers_take = VIZ_N_TRADES - n_winners_take
if n_losers_take > len(losers):
    # Not enough losers; backfill with the remaining winners.
    n_losers_take = len(losers)
    n_winners_take = min(VIZ_N_TRADES - n_losers_take, len(winners))

win_idx = _r.sample(range(len(winners)), n_winners_take) if n_winners_take > 0 else []
lose_idx = _r.sample(range(len(losers)), n_losers_take) if n_losers_take > 0 else []
winner_picks = winners.iloc[win_idx]
loser_picks = losers.iloc[lose_idx]
picks = pd.concat([winner_picks, loser_picks], ignore_index=True)
picks = picks.sample(frac=1, random_state=found_seed + 2).reset_index(drop=True)  # shuffle winners/losers
picks = picks.sort_values('entry_time').reset_index(drop=True)  # sort chronologically for the panels
print(f'Picked {len(picks)} sniper trades ({len(winner_picks)} winners, {len(loser_picks)} losers):')
print(picks[['entry_time', 'direction', 'entry_price', 'exit_price',
             'stop_usd', 'target_usd', 'exit_reason', 'pnl_usd', 'hold_secs']]
      .to_string(index=False))

# ─── build 1m candles ──────────────────────────────────────────────
df_1m = (bars_df.set_index('time')[['open', 'high', 'low', 'close']]
         .resample('1min')
         .agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'})
         .dropna())
if df_1m.index.tz is not None:
    df_1m.index = df_1m.index.tz_localize(None)
print(f'\n1m candles: {len(df_1m):,}')

# ─── color palette ─────────────────────────────────────────────────
BULL_FVG_FACE, BULL_FVG_EDGE = 'lightgreen', 'darkgreen'
BEAR_FVG_FACE, BEAR_FVG_EDGE = 'lightcoral', 'darkred'
BULL_IFVG_FACE, BULL_IFVG_EDGE = 'red', 'darkred'
BEAR_IFVG_FACE, BEAR_IFVG_EDGE = 'limegreen', 'darkgreen'
SNIPER_LONG_COLOR  = 'limegreen'
SNIPER_SHORT_COLOR = 'darkorange'

# Soft-stop colors — distinct from SL (red) and TP (dodgerblue) so the
# user can SEE the soft-SL as a separate price level on the chart.
# Purple matches the 'inv' exit marker.
SOFT_SL_COLOR = 'purple'
SOFT_SL_BUFFER_USD = 0.02  # mirrors v17 `invalidation_buffer_usd`

EXIT_COLORS = {
    'tp':  'blue',
    'sl':  'orange',
    'inv': 'purple',
    'eod': 'gray',
}
EXIT_MARKERS = {
    # Marker shapes by exit reason. Size is matplotlib scatter 's' (area in
    # points^2). Bigger than the entry marker (220) so the EXIT is visible
    # even when it sits inside a candle body of similar color.
    'tp':  ('*', 360),   # blue asterisk — TP hit (largest so wins pop)
    'sl':  ('X', 320),   # bold X — SL hit (visible against any candle)
    'inv': ('D', 240),   # purple diamond — soft-stop hit
    'eod': ('s', 220),   # gray square — EOD exit
    '':    ('X', 320),
}

# When drawing exit markers we wrap them with a thin white edge so they
# stay visible against teal/red candle bodies of similar luminance.
EXIT_EDGE_COLOR = 'white'
EXIT_EDGE_WIDTH = 1.6


def ts_to_1m_x(ts: pd.Timestamp) -> float:
    """Convert a tz-aware UTC Timestamp to a matplotlib x (date2num) for 1m candles."""
    ts_naive = ts.tz_localize(None) if ts.tz is not None else ts
    return mdates.date2num(ts_naive.to_pydatetime())


def draw_sniper_chart(trade_row, panel_idx):
    """Draw one sniper trade chart.

    BTC fork: also renders a scatter overlay of the raw aggTrades
    within the panel window. The ticks are coloured by
    ``is_buyer_maker`` (red = taker-sold, green = taker-bought)
    so the directional microstructure is visible alongside the
    sniper entry.
    """
    entry_bar    = int(trade_row['entry_bar'])
    direction    = int(trade_row['direction'])
    entry_price = float(trade_row['entry_price'])
    exit_price  = float(trade_row['exit_price'])
    sl_usd      = float(trade_row['stop_usd'])
    tp_usd      = float(trade_row['target_usd'])
    er          = str(trade_row['exit_reason'])
    pnl         = float(trade_row['pnl_usd'])
    fee_usd     = float(trade_row.get('fee_usd', 0.0))
    net_pnl     = pnl - fee_usd
    hold        = float(trade_row['hold_secs'])
    zone        = trade_row['fvg_zone']
    entry_t     = pd.Timestamp(trade_row['entry_time']).tz_localize(None)
    exit_t      = pd.Timestamp(trade_row['exit_time']).tz_localize(None)

    # Window: ±half of VIZ_WINDOW_HOURS around the entry bar.
    win_secs = int(VIZ_WINDOW_HOURS * 1800)   # half-window in 1s bars
    win_start_bar = max(0, entry_bar - win_secs)
    win_end_bar   = min(len(bars_df) - 1, entry_bar + win_secs)

    # 1m candles in the window. Use timestamp lookups to avoid
    # out-of-range bar-index problems when the window extends beyond
    # the loaded data range (it won't, but the mapping is robust).
    win_start_ts = bars_df['time'].iloc[win_start_bar]
    win_end_ts   = bars_df['time'].iloc[win_end_bar]
    df_win = df_1m.loc[
        (df_1m.index >= win_start_ts.tz_localize(None))
        & (df_1m.index <= win_end_ts.tz_localize(None))
    ].copy()

    fig, ax = plt.subplots(figsize=(20, 10))

    # ── Raw tick scatter (BTC fork) ─────────────────────────────
    # Overlay the raw aggTrades within the panel window so the
    # within-bar microstructure is visible. Coloured by
    # is_buyer_maker: green = aggressive buy (lifted offer),
    # red = aggressive sell (hit bid). Rendered BEFORE candles
    # so the candles overlay on top, but with zorder=1 so the
    # ticks stay visible.
    _raw_in_win = raw_df[
        (raw_df['ts'] >= win_start_ts.tz_convert('UTC'))
        & (raw_df['ts'] <= win_end_ts.tz_convert('UTC'))
    ]
    if not _raw_in_win.empty:
        _x_ticks = mdates.date2num(
            _raw_in_win['ts'].dt.tz_localize(None).to_pydatetime()
        )
        _prices = _raw_in_win['price'].to_numpy()
        _is_buy = (~_raw_in_win['is_buyer_maker']).to_numpy()  # taker-buy
        # Subsample if there are too many ticks (>5k) — keeps the
        # chart legible without losing the microstructure feel
        if len(_x_ticks) > 5_000:
            step = max(1, len(_x_ticks) // 5_000)
            _x_ticks = _x_ticks[::step]
            _prices = _prices[::step]
            _is_buy = _is_buy[::step]
        ax.scatter(
            _x_ticks[_is_buy], _prices[_is_buy],
            s=2, color='#4caf50', alpha=0.35, marker='.',
            zorder=1, label=f'aggressive-buy ({_is_buy.sum():,})',
        )
        ax.scatter(
            _x_ticks[~_is_buy], _prices[~_is_buy],
            s=2, color='#f44336', alpha=0.35, marker='.',
            zorder=1, label=f'aggressive-sell ({(~_is_buy).sum():,})',
        )

    # ── Candles ────────────────────────────────────────────────
    x_dates = mdates.date2num(df_win.index.to_pydatetime())
    ohl = df_win[['open', 'high', 'low', 'close']].to_numpy()
    opens, highs, lows, closes = ohl[:, 0], ohl[:, 1], ohl[:, 2], ohl[:, 3]
    half_w = 0.45 / 2 / (24 * 60)  # ~32s body half-width
    up_color, dn_color = '#26a69a', '#ef5350'

    for x, lo, hi in zip(x_dates, lows, highs):
        ax.vlines(x, lo, hi, color='black', linewidth=0.7, zorder=2)
    for x, o, c in zip(x_dates, opens, closes):
        bottom = min(o, c)
        height = max(abs(o - c), 0.001)
        ax.add_patch(mpatches.Rectangle(
            (x - half_w, bottom), 2 * half_w, height,
            facecolor=up_color if c >= o else dn_color,
            edgecolor='black', linewidth=0.4, zorder=3))

    # ── Anchor FVG zone drawn as TWO segments ─────────────────────
    # When the zone has inverted inside the window, draw:
    #   segment 1: trigger_bar → inverted_bar in the ORIGINAL FVG color (faded)
    #   segment 2: inverted_bar → right-edge in the INVERTED iFVG color
    # The inversion bar itself sits at the boundary between the two
    # segments — visually clear "flip from green to red" / "flip
    # from red to green" that the user asked for.
    if zone is not None:
        zl = float(zone.zone_low)
        zh = float(zone.zone_high)
        zw = zh - zl
        is_bull_orig = int(zone.direction) > 0
        trigger_bar_z = int(zone.trigger_bar)
        inverted_bar_z = int(getattr(zone, 'inverted_bar', -1))

        # Original-FVG color pair (faded)
        orig_face = BULL_FVG_FACE if is_bull_orig else BEAR_FVG_FACE
        orig_edge = BULL_FVG_EDGE if is_bull_orig else BEAR_FVG_EDGE
        # iFVG (inverted) color pair — saturated, the "polarity flipped" hue
        inv_face = BULL_IFVG_FACE if is_bull_orig else BEAR_IFVG_FACE
        inv_edge = BULL_IFVG_EDGE if is_bull_orig else BEAR_IFVG_EDGE

        inv_in_window = (inverted_bar_z >= 0
                         and win_start_bar <= inverted_bar_z <= win_end_bar)

        if inv_in_window:
            seg1_right = inverted_bar_z
            seg2_right = min(win_end_bar, seg1_right + 3600)  # 1h of post-inversion context
        else:
            seg1_right = min(trigger_bar_z + 3600, win_end_bar)
            seg2_right = None

        # Segment 1: original FVG (faded) — trigger_bar → seg1_right
        if seg1_right >= trigger_bar_z:
            xl_x1 = ts_to_1m_x(df['time'].iloc[trigger_bar_z])
            xr_x1 = ts_to_1m_x(df['time'].iloc[seg1_right])
            if xr_x1 > xl_x1:
                ax.add_patch(mpatches.Rectangle(
                    (xl_x1, zl), xr_x1 - xl_x1, zw,
                    facecolor=orig_face, edgecolor=orig_edge,
                    alpha=0.18, linewidth=0.5, zorder=2.5))

        # Segment 2: iFVG (saturated) — only when inversion is in the window
        if seg2_right is not None and seg2_right >= inverted_bar_z:
            xl_x2 = ts_to_1m_x(df['time'].iloc[inverted_bar_z])
            xr_x2 = ts_to_1m_x(df['time'].iloc[seg2_right])
            if xr_x2 > xl_x2:
                ax.add_patch(mpatches.Rectangle(
                    (xl_x2, zl), xr_x2 - xl_x2, zw,
                    facecolor=inv_face, edgecolor=inv_edge,
                    alpha=0.40, linewidth=0.8, zorder=2.6))

        # Single combined zone label at the top-left of segment 1
        if seg1_right >= trigger_bar_z:
            xl_x = ts_to_1m_x(df['time'].iloc[trigger_bar_z])
            tag_base = ('FVG bull' if is_bull_orig else 'FVG bear')
            tag_state = (' → iFVG (inverted) at inversion bar'
                         if inv_in_window else ' (not inverted in window)')
            ax.text(xl_x, zh,
                    f' {tag_base}{tag_state}  [{zl:.2f}..{zh:.2f}] w={zw:.2f}',
                    color=orig_edge, fontsize=7, ha='left', va='bottom',
                    path_effects=[pe.withStroke(linewidth=1.5, foreground='white',
                                            alpha=0.85)])

        # Inversion marker bar (dashed vertical line) — drawn at the
        # boundary between the two segments so the "color flip" is
        # visually unambiguous.
        if inv_in_window:
            inv_x = ts_to_1m_x(df['time'].iloc[inverted_bar_z])
            ax.axvline(inv_x, color='black', linestyle=':', linewidth=1.6,
                       alpha=0.85, zorder=4)
            ax.text(inv_x, float(df_win['high'].max()),
                    '  inversion\n  (FVG → iFVG)',
                    color='black', fontsize=8, fontweight='bold',
                    ha='left', va='top',
                    path_effects=[pe.withStroke(linewidth=2, foreground='white',
                                            alpha=0.85)])

    # ── "Wait" connector: inversion bar → entry bar ─────────────
    if (zone is not None
            and int(getattr(zone, 'inverted_bar', -1)) >= 0):
        inv_bar_z = int(zone.inverted_bar)
        if win_start_bar <= inv_bar_z <= win_end_bar:
            inv_x   = ts_to_1m_x(df['time'].iloc[inv_bar_z])
            entry_x = ts_to_1m_x(pd.Timestamp(trade_row['entry_time']))
            ax.annotate(
                '', xy=(entry_x, entry_price), xytext=(inv_x, entry_price),
                arrowprops=dict(
                    arrowstyle='->', color='darkviolet',
                    linewidth=1.6, alpha=0.85, linestyle=(0, (4, 2))),
                zorder=6)

    # ── Entry triangle ──────────────────────────────────────────
    # Just the marker on the chart — no inline text overlay that would
    # cover the candles. The entry details are summarised in:
    #   (a) the chart's TITLE (entry/exit prices, R:R, PnL)
    #   (b) a compact semi-transparent info box at the chart top-left
    #       that floats ABOVE the candles (using ax.text with zorder so
    #       it's always readable even when the entry price crosses SL/TP)
    #   (c) the exit marker label (which carries the SL/TP price too)
    entry_color = SNIPER_LONG_COLOR if direction > 0 else SNIPER_SHORT_COLOR
    entry_marker = '^' if direction > 0 else 'v'
    ax.scatter(entry_t, entry_price, marker=entry_marker,
               color=entry_color, s=220, zorder=8,
               edgecolor='black', linewidth=1.2)

    # ── SL + TP horizontal lines + floating info box ──────────
    # Lines stay full-strength so the user can SEE the levels where the
    # trade will exit. The line-edge text labels go SEMI-TRANSPARENT
    # (alpha=0.55) so when SL/TP are tight (e.g. the trade is fast and
    # the lines visually overlap near the entry), the labels don't fight.
    # The actual numeric prices live in:
    #   - the chart title ("SL=$0.84=zone_w×2, TP=$8.40=zone_w×20")
    #   - the floating info box (top-left corner, semi-transparent)
    #   - the exit marker label (only shows when exit fires)
    # so the right-edge labels are decoration, not the source of truth.
    if direction > 0:
        sl_price = entry_price - sl_usd
        tp_price = entry_price + tp_usd
    else:
        sl_price = entry_price + sl_usd
        tp_price = entry_price - tp_usd

    ax.axhline(sl_price, color='red', linestyle=(0, (1, 2)),
               linewidth=1.2, alpha=0.80, zorder=5)
    ax.axhline(tp_price, color='dodgerblue', linestyle=(0, (1, 2)),
               linewidth=1.2, alpha=0.80, zorder=5)

    # Right-edge labels — semi-transparent so they don't overlap each
    # other when SL/TP are visually close (fast SL/TP setups).
    x_right = x_dates[-1]
    ax.text(x_right, sl_price, f' SL ${sl_usd:.2f}  ',
            color='red', fontsize=8.5, fontweight='bold', alpha=0.55,
            ha='right', va='center',
            path_effects=[pe.withStroke(linewidth=2.0, foreground='white',
                                   alpha=0.95)])
    ax.text(x_right, tp_price, f' TP ${tp_usd:.2f}  ',
            color='dodgerblue', fontsize=8.5, fontweight='bold', alpha=0.55,
            ha='right', va='center',
            path_effects=[pe.withStroke(linewidth=2.0, foreground='white',
                                   alpha=0.95)])

    # ── Floating info box (top-left of chart) ─────────────────
    # All entry/SL/TP/R:R numbers in a single semi-transparent box that
    # floats above the candle chart. Anchored to the top-left in axes
    # coords (transform=ax.transAxes) so it stays put regardless of the
    # entry x/y position. Box has alpha=0.78 so the candles peek through
    # faintly — the user sees the box AND the candles underneath.
    info_lines = [
        f'ENTRY  {entry_price:7.3f}',
        f'SL     {sl_price:7.3f}    (×{VIZ_SL_MULT})',
        f'TP     {tp_price:7.3f}    (×{VIZ_TP_MULT})',
        f'R:R    1:{tp_usd/max(sl_usd,0.01):.1f}',
    ]
    info_text = '\n'.join(info_lines)
    ax.text(0.012, 0.975, info_text,
            transform=ax.transAxes,
            color='black', fontsize=8.5, fontweight='bold',
            ha='left', va='top', family='monospace',
            bbox=dict(
                facecolor='white', edgecolor='lightgray',
                boxstyle='round,pad=0.45', alpha=0.82,
            ),
            zorder=9)

    # ── Soft-SL horizontal line (the "preceding state" that fires the
    # 'inv' exit). Reconstruction logic mirrors ``ict_backtest.py``
    # step 3 (lines 1391-1407): the soft-SL is set to
    # ``zone.zone_high + buffer`` (shorts) or ``zone.zone_low - buffer``
    # (longs) the moment ``zone.inverted`` becomes True.
    #
    # For SNIPER trades: the zone was already inverted BEFORE entry
    # (that's what triggered the sniper). So the soft-SL is set on the
    # entry bar — the bar the sniper fires.
    # For IMMEDIATE trades exiting via 'inv': the soft-SL was set at
    # the inversion bar (later than entry).
    soft_sl_info = None
    if zone is not None and bool(getattr(zone, 'inverted', False)):
        zl = float(zone.zone_low)
        zh = float(zone.zone_high)
        if direction > 0:
            soft_sl_price = zl - SOFT_SL_BUFFER_USD
            soft_sl_label_edge = 'zone_low'
        else:
            soft_sl_price = zh + SOFT_SL_BUFFER_USD
            soft_sl_label_edge = 'zone_high'
        # Activation bar: for sniper, the entry bar (zone was inverted
        # before the sniper fired). For non-sniper, the inversion bar
        # (the bar where zone.inverted flipped True).
        if str(trade_row.get('entry_triggered_by', '')) == 'sniper':
            soft_sl_activation_bar = int(entry_bar)
            soft_sl_activation_kind = 'on entry bar (sniper)'
        else:
            inv_bar = int(getattr(zone, 'inverted_bar', -1))
            soft_sl_activation_bar = inv_bar if inv_bar >= 0 else int(entry_bar)
            soft_sl_activation_kind = 'on inversion bar'
        # Only draw if the activation bar is in the panel window.
        # The soft-SL price should be near the zone so it's typically
        # in the y-range anyway (the ylim is computed from the
        # candles + SL + TP + soft-SL all together below).
        if (win_start_bar <= soft_sl_activation_bar <= win_end_bar):
            # Make the soft-SL line visually distinct: solid purple
            # with a slightly thicker stroke so it stands out from
            # the dotted/dashed SL (red) and TP (dodgerblue) lines.
            ax.axhline(soft_sl_price, color=SOFT_SL_COLOR,
                       linestyle=(0, (4, 2)), linewidth=2.0, alpha=0.95,
                       zorder=5.5)
            ax.text(x_right, soft_sl_price,
                    f' SOFT-SL ${soft_sl_price:.2f}  ',
                    color=SOFT_SL_COLOR, fontsize=8.5, fontweight='bold',
                    ha='right', va='center',
                    path_effects=[pe.withStroke(linewidth=2.0,
                                            foreground='white', alpha=0.95)])
            soft_sl_info = {
                'price': soft_sl_price,
                'activation_bar': soft_sl_activation_bar,
                'kind': soft_sl_activation_kind,
            }

    # ── Exit marker ─────────────────────────────────────────────
    # Drawn with a white edge so the marker stays visible against
    # teal/red candle bodies of similar luminance. SL exits on 1m
    # candles frequently sit inside the candle body, so the white
    # border is what makes the marker "pop" against the candle fill.
    ex_color, (ex_marker, ex_size) = EXIT_COLORS.get(er, 'black'), EXIT_MARKERS.get(er, ('X', 320))
    ax.scatter(exit_t, exit_price, marker=ex_marker, color=ex_color,
               s=ex_size, zorder=7, linewidths=EXIT_EDGE_WIDTH,
               edgecolors=EXIT_EDGE_COLOR)

    # ── Entry→exit line ───────────────────────────────────────
    ax.plot([entry_t, exit_t], [entry_price, exit_price],
            color=ex_color, linewidth=1.8, alpha=0.80, zorder=6.5,
            linestyle='-' if pnl >= 0 else '--')

    # ── Exit label (compact annotation next to the marker) ─────
    # The label makes the exit unambiguously visible — addresses the
    # "SL exits don't seem to plot on some panels" observation. Even
    # if the marker itself is partially obscured by a candle body,
    # the label text is always rendered on top.
    exit_label = {
        'tp':  f' TP @ {exit_price:.2f}',
        'sl':  f' SL @ {exit_price:.2f}',
        'inv': f' INV @ {exit_price:.2f}  (soft-SL)',
        'eod': f' EOD @ {exit_price:.2f}',
    }.get(er, f' EXIT @ {exit_price:.2f}')
    # Offset label up for SL/INV (downside exits) and down for TP (upside wins)
    label_dy = 0.35 if pnl < 0 else -0.35
    ax.text(exit_t, exit_price + label_dy, exit_label,
            color=ex_color, fontsize=8.5, fontweight='bold',
            ha='center', va='center',
            path_effects=[pe.withStroke(linewidth=2.2,
                                    foreground='white', alpha=0.95)],
            zorder=8.5)

    # ── Soft-SL activation marker (vertical line + label) ────
    # This is the bar where the soft-SL was actually SET — the
    # "preceding position" event that turned on the soft-stop.
    # Drawn AFTER the exit marker so it stays on top in zorder.
    if soft_sl_info is not None:
        act_bar = int(soft_sl_info['activation_bar'])
        act_x = ts_to_1m_x(df['time'].iloc[act_bar])
        ax.axvline(act_x, color=SOFT_SL_COLOR, linestyle='--',
                   linewidth=1.3, alpha=0.70, zorder=4.5)
        # Label placed at the top of the chart
        ax.text(act_x, float(df_win['low'].min()),
                f' soft-SL activated\n ({soft_sl_info["kind"]})',
                color=SOFT_SL_COLOR, fontsize=7.5, fontweight='bold',
                ha='left', va='bottom',
                path_effects=[pe.withStroke(linewidth=1.8,
                                        foreground='white', alpha=0.95)])

    # ── Title ─────────────────────────────────────────────────
    # Add a "soft-SL status" note so the user can tell at a glance
    # whether the soft-SL is active, suppressed by alignment, or fired.
    soft_sl_status = ''
    if soft_sl_info is not None and er == 'inv':
        soft_sl_status = '  |  SOFT-SL HIT (zone inversion)'
    elif soft_sl_info is not None and er in ('sl', 'tp', 'eod'):
        # Soft-SL was would-be active but didn't fire — either
        # alignment suppressed it OR price never reached it.
        ea = str(trade_row.get('entry_alignment', ''))
        if ea == 'aligned':
            soft_sl_status = '  |  Soft-SL SUPPRESSED (aligned)'
        else:
            soft_sl_status = '  |  Soft-SL not hit'
    title = (
        f'Panel {panel_idx}/{VIZ_N_TRADES} — SNIPER trade (BTC tick-fill)  '
        f'@ {entry_t.strftime("%Y-%m-%d %H:%M UTC")}  '
        f'(dir={direction:+d}, entry_alignment={str(trade_row.get("entry_alignment", ""))})\n'
        f'Entry {entry_price:.2f} → Exit {exit_price:.2f} via "{er}"  |  '
        f'PnL ${pnl:+.2f} − fee ${fee_usd:.2f} = net ${net_pnl:+.2f}  |  '
        f'hold {hold:.1f}s ({hold/60:.2f}min)  |  '
        f'R:R 1:{tp_usd/max(sl_usd,0.01):.1f}  (SL=${sl_usd:.2f}=zone_w×{VIZ_SL_MULT}, '
        f'TP=${tp_usd:.2f}=zone_w×{VIZ_TP_MULT}){soft_sl_status}'
    )
    ax.set_title(title, fontsize=10)
    ax.set_ylabel('Price (USD)')
    ax.set_xlabel('Time (UTC)')
    ax.grid(True, alpha=0.3)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f'{v:.2f}'))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=10))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M\n%b %d'))

    # Ylim: show candles + SL + TP with padding
    y_candles = [float(df_win['low'].min()), float(df_win['high'].max())]
    y_all = y_candles + [sl_price, tp_price]
    if soft_sl_info is not None:
        y_all.append(soft_sl_info['price'])
    y_min = min(y_all) - (max(y_all) - min(y_all)) * 0.05
    y_max = max(y_all) + (max(y_all) - min(y_all)) * 0.12
    ax.set_ylim(y_min, y_max)
    ax.set_xlim(df_win.index[0], df_win.index[-1])

    # ── Legend ──────────────────────────────────────────────────
    rr = tp_usd / max(sl_usd, 0.01)
    legend_handles = [
        plt.Line2D([0], [0], color=SNIPER_LONG_COLOR, marker='^', linestyle='',
                   markersize=12, markeredgecolor='black', markeredgewidth=1.2,
                   label=f'SNIPER LONG (bull FVG inverted → short, entered long)'),
        plt.Line2D([0], [0], color=SNIPER_SHORT_COLOR, marker='v', linestyle='',
                   markersize=12, markeredgecolor='black', markeredgewidth=1.2,
                   label=f'SNIPER SHORT (bear FVG inverted → long, entered short)'),
        mpatches.Patch(facecolor=BULL_FVG_FACE, edgecolor=BULL_FVG_EDGE,
                       alpha=0.18,
                       label='FVG zone (pre-inversion, faded)'),
        mpatches.Patch(facecolor=BULL_IFVG_FACE, edgecolor=BULL_IFVG_EDGE,
                       alpha=0.40,
                       label='iFVG zone (post-inversion, saturated)'),
        plt.Line2D([0], [0], color='red', linestyle=(0, (1, 2)), linewidth=1.4,
                   label=f'SL ${sl_usd:.2f}  (entry - SL = zone_w × {VIZ_SL_MULT})'),
        plt.Line2D([0], [0], color=SOFT_SL_COLOR, linestyle=(0, (4, 2)),
                   linewidth=2.0,
                   label='Soft-SL (zone inversion tightens SL — '
                         'fires "inv" exit)'),
        plt.Line2D([0], [0], color='dodgerblue', linestyle=(0, (1, 2)),
                   linewidth=1.4,
                   label=f'TP ${tp_usd:.2f}  (entry + TP = zone_w × {VIZ_TP_MULT})'),
        plt.Line2D([0], [0], color='darkviolet', linestyle=(0, (4, 2)),
                   linewidth=1.4,
                   label='Wait gap (inversion bar → next-bar open, anti-lookahead)'),
        plt.Line2D([0], [0], color='black', linestyle=':', linewidth=1.4,
                   label='Inversion bar (FVG → iFVG flip)'),
        plt.Line2D([0], [0], color=SOFT_SL_COLOR, linestyle='--',
                   linewidth=1.4,
                   label='Soft-SL activated (vertical — the "preceding state")'),
        plt.Line2D([0], [0], color='blue', marker='*', linestyle='',
                   markersize=12, label='TP exit'),
        plt.Line2D([0], [0], color='orange', marker='x', linestyle='',
                   markersize=10, markeredgewidth=2, label='SL exit'),
        plt.Line2D([0], [0], color=SOFT_SL_COLOR, marker='D', linestyle='',
                   markersize=10, label='Soft-stop exit (hit soft-SL)'),
        plt.Line2D([0], [0], color='gray', marker='s', linestyle='',
                   markersize=10, label='EOD exit'),
    ]
    ax.legend(handles=legend_handles, loc='upper left', fontsize=7,
              framealpha=0.92, ncol=2)
    fig.tight_layout()

    out = OUT_DIR / f'nb51_panel_{panel_idx}.png'
    fig.savefig(out, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f'  Saved panel {panel_idx}: {out}')
    return out


# ─── render charts ─────────────────────────────────────────────────
print(f'\nRendering {VIZ_N_TRADES} sniper trade panels...')
for i, (_, row) in enumerate(picks.iterrows(), start=1):
    draw_sniper_chart(row, panel_idx=i)

print('\nDone! Charts saved to notebooks/nb51_panel_*.png')
print(f'\nPick summary:')
print(picks[['entry_time', 'direction', 'stop_usd', 'target_usd',
             'exit_reason', 'pnl_usd', 'hold_secs']]
      .rename(columns={
          'entry_time': 'entry', 'direction': 'dir',
          'stop_usd': 'SL', 'target_usd': 'TP',
          'exit_reason': 'exit', 'pnl_usd': 'PnL', 'hold_secs': 'hold(s)'
      })
      .to_string(index=False))
