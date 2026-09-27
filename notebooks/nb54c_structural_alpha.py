"""nb54c — Structural-alpha bucket analysis on the canonical trade tags.

Reads the per-trade structural tags dumped by nb54b and buckets
trades by:

  1. event_at_entry  (NONE / BOS_BULL / BOS_BEAR / CHOCH_BULL /
                       CHOCH_BEAR / LIQ_SWEEP_HIGH / LIQ_SWEEP_LOW)
  2. last_break_kind × last_break_age_bars buckets
  3. last_choch_kind × last_choch_age_bars buckets
  4. trend_at_entry × direction (long/short × bull/bear trend)
  5. last_swing_kind × last_swing_age_bars buckets
  6. Combinations:
     a. direction-aligned-with-trend (long+bull / short+bear) vs not
     b. BoS-in-direction (last_break_kind agrees with trade direction
        AND age < threshold) vs not
     c. CHoCH-against (caution flag fired at entry) vs not
     d. Sweep-in-direction (LIQ_SWEEP_HIGH for shorts, LIQ_SWEEP_LOW
        for longs) vs not

For each bucket: n, mean pnl, EV, WR, sum pnl. Goal: find a bucket
split that's both meaningful (|EV split| > 50% of mean) and large
enough (each side has 30+ trades) to be tradeable.
"""
import json
from collections import defaultdict
from pathlib import Path

TAGS_DIR = Path('notebooks/nb54_vBTC3_ms_sweep_outputs')
TAG_FILES = sorted(TAGS_DIR.glob('nb54b_canonical_trade_tags__*.jsonl'))

print(f'Loading {len(TAG_FILES)} tag files...')
all_trades = []
for f in TAG_FILES:
    rows = [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    print(f'  {f.name}: {len(rows)} trades')
    all_trades.extend(rows)

print(f'Total: {len(all_trades)} trades')
pnls = [t['pnl_usd'] for t in all_trades]
print(f'  net = ${sum(pnls):+.2f}  EV = ${sum(pnls)/len(pnls):+.4f}  '
      f'WR = {100*sum(1 for p in pnls if p>0)/len(pnls):.1f}%')

# Helper to print bucket tables.
def bucket_report(name, keyfn):
    print(f'\n=== Bucket: {name} ===')
    print(f'  {"bucket":30s} {"n":>4} {"EV_net":>8} {"sum_pnl":>10} {"WR_pct":>6}')
    buckets = defaultdict(list)
    for t in all_trades:
        try:
            k = keyfn(t)
        except Exception:
            k = '?'
        buckets[k].append(t['pnl_usd'])
    items = []
    for k, lst in buckets.items():
        n = len(lst); s = sum(lst); ev = s/n; wr = 100*sum(1 for p in lst if p>0)/n
        items.append((k, n, ev, s, wr))
    items.sort(key=lambda x: -x[3])  # by sum pnl desc
    for k, n, ev, s, wr in items:
        print(f'  {str(k):30s} {n:4d} ${ev:+7.3f} ${s:+9.2f} {wr:5.1f}%')
    return items

# 1. event_at_entry
EVENT_LABELS = {0:'NONE', 1:'BOS_BULL', -1:'BOS_BEAR', 2:'CHOCH_BULL',
                -2:'CHOCH_BEAR', 3:'LIQ_SWEEP_HIGH', -3:'LIQ_SWEEP_LOW'}
bucket_report('event_at_entry',
              lambda t: EVENT_LABELS.get(int(t.get('event_at_entry', 0)), '?'))

# 2. last_break_kind × age
def break_bucket(t):
    k = int(t.get('last_break_kind', 0))
    a = int(t.get('last_break_age_bars', -1))
    if k == 0 or a < 0:
        return 'no_break'
    if a < 60:
        return f'{EVENT_LABELS.get(k, "?")}_fresh(<60)'
    elif a < 300:
        return f'{EVENT_LABELS.get(k, "?")}_near(<300)'
    elif a < 1800:
        return f'{EVENT_LABELS.get(k, "?")}_stale(<1800)'
    else:
        return f'{EVENT_LABELS.get(k, "?")}_cold(>=1800)'
bucket_report('last_break_kind x age', break_bucket)

# 3. last_choch_kind × age
def choch_bucket(t):
    k = int(t.get('last_choch_kind', 0))
    a = int(t.get('last_choch_age_bars', -1))
    if k == 0 or a < 0:
        return 'no_choch'
    if a < 60:
        return f'{EVENT_LABELS.get(k, "?")}_fresh(<60)'
    elif a < 600:
        return f'{EVENT_LABELS.get(k, "?")}_stale(<600)'
    else:
        return f'{EVENT_LABELS.get(k, "?")}_cold(>=600)'
bucket_report('last_choch_kind x age', choch_bucket)

# 4. trend_at_entry × direction
def trend_dir(t):
    tr = int(t.get('trend_at_entry', 0))
    d = int(t.get('direction', 0))
    trend_s = 'bull' if tr > 0 else ('bear' if tr < 0 else 'unk')
    dir_s = 'long' if d > 0 else 'short'
    return f'{trend_s}__{dir_s}'
bucket_report('trend x direction', trend_dir)

# 5. last_swing_kind × age
def swing_bucket(t):
    k = str(t.get('last_swing_kind', 'none'))
    a = int(t.get('last_swing_age_bars', -1))
    if k == 'none' or a < 0:
        return 'no_swing'
    if a < 60:
        return f'{k}_fresh(<60)'
    elif a < 300:
        return f'{k}_near(<300)'
    elif a < 1800:
        return f'{k}_stale(<1800)'
    else:
        return f'{k}_cold(>=1800)'
bucket_report('last_swing x age', swing_bucket)

# 6. Composite buckets — designed for tradeability (filter size).

# 6a. Trend-aligned vs counter-trend
def trend_aligned(t):
    tr = int(t.get('trend_at_entry', 0))
    d = int(t.get('direction', 0))
    if tr == 0:
        return 'trend_unk'
    return 'aligned' if (tr > 0 and d > 0) or (tr < 0 and d < 0) else 'counter'
print()
print('=== Composite: trend_aligned vs counter_trend ===')
agg = defaultdict(list)
for t in all_trades:
    agg[trend_aligned(t)].append(t['pnl_usd'])
for k, lst in sorted(agg.items(), key=lambda x: -sum(x[1])):
    n = len(lst); s = sum(lst); ev = s/n; wr = 100*sum(1 for p in lst if p>0)/n
    print(f'  {k:18s}  n={n:3d}  EV=${ev:+.4f}  sum=${s:+.2f}  WR={wr:.1f}%')

# 6b. BoS in trade direction AND fresh (<N bars)
def bos_in_dir_fresh(t, max_age=300):
    d = int(t.get('direction', 0))
    k = int(t.get('last_break_kind', 0))
    a = int(t.get('last_break_age_bars', -1))
    if a < 0 or a > max_age:
        return 'no_fresh_bos'
    if (d > 0 and k == 1) or (d < 0 and k == -1):
        return 'bos_in_dir'
    return 'bos_against'
print()
print(f'=== Composite: BoS in trade direction (fresh <300 bars) ===')
agg = defaultdict(list)
for t in all_trades:
    agg[bos_in_dir_fresh(t, 300)].append(t['pnl_usd'])
for k, lst in sorted(agg.items(), key=lambda x: -sum(x[1])):
    n = len(lst); s = sum(lst); ev = s/n; wr = 100*sum(1 for p in lst if p>0)/n
    print(f'  {k:18s}  n={n:3d}  EV=${ev:+.4f}  sum=${s:+.2f}  WR={wr:.1f}%')

# 6c. CHoCH against trade (caution flag) — fresh
def choch_against(t, max_age=600):
    d = int(t.get('direction', 0))
    k = int(t.get('last_choch_kind', 0))
    a = int(t.get('last_choch_age_bars', -1))
    if a < 0 or a > max_age:
        return 'no_recent_choch'
    if (d > 0 and k == -2) or (d < 0 and k == 2):
        return 'choch_against'
    return 'choch_with_or_none'
print()
print('=== Composite: CHoCH against trade (fresh <600 bars) — caution flag ===')
agg = defaultdict(list)
for t in all_trades:
    agg[choch_against(t, 600)].append(t['pnl_usd'])
for k, lst in sorted(agg.items(), key=lambda x: -sum(x[1])):
    n = len(lst); s = sum(lst); ev = s/n; wr = 100*sum(1 for p in lst if p>0)/n
    print(f'  {k:20s}  n={n:3d}  EV=${ev:+.4f}  sum=${s:+.2f}  WR={wr:.1f}%')

# 6d. Sweep in trade direction (the liquidity-grab-then-trade-with-it pattern)
def sweep_in_dir(t, max_age=1800):
    d = int(t.get('direction', 0))
    ev = int(t.get('event_at_entry', 0))
    # event_at_entry is the per-bar event tag. Look at the recent sweep
    # by checking last_break_kind + age: LIQ_SWEEP_HIGH = +3, LOW = -3.
    # We have to look at the last_break_kind instead because that's
    # the most-recent BoS/CHoCH, which may or may not be a sweep.
    # Use the convention: treat "last_break_kind" as the most recent
    # structure event regardless of type, but specifically look at the
    # raw event_at_entry value: if it's a sweep and it's IN THE
    # trade direction (bull sweep for longs, bear for shorts), the
    # entry is "sweep-confirmed".
    k = int(t.get('last_break_kind', 0))
    a = int(t.get('last_break_age_bars', -1))
    if a < 0 or a > max_age:
        return 'no_recent_sweep'
    # Liquidity sweeps use values 3 (LIQ_SWEEP_HIGH) and -3
    # (LIQ_SWEEP_LOW). The last_break_kind field tracks the BoS/CHoCH
    # only — for sweep detection we need a different tag. We don't
    # have a dedicated sweep-age field in the sidecar dump; for now
    # skip this bucket.
    return 'sweep_check_unavailable'

# 6d alt: combine trend-aligned AND BoS-in-direction
def trend_aligned_x_bos_in_dir(t):
    tr = int(t.get('trend_at_entry', 0))
    d = int(t.get('direction', 0))
    k = int(t.get('last_break_kind', 0))
    a = int(t.get('last_break_age_bars', -1))
    if tr == 0:
        return 'trend_unk'
    aligned = (tr > 0 and d > 0) or (tr < 0 and d < 0)
    bos_fresh_in_dir = (0 <= a <= 300) and (
        (d > 0 and k == 1) or (d < 0 and k == -1))
    if aligned and bos_fresh_in_dir:
        return 'aligned_AND_bos_in_dir'
    if aligned and not bos_fresh_in_dir:
        return 'aligned_no_fresh_bos'
    if not aligned:
        return 'counter_trend'
print()
print('=== Composite: trend-aligned × fresh BoS in direction ===')
agg = defaultdict(list)
for t in all_trades:
    agg[trend_aligned_x_bos_in_dir(t)].append(t['pnl_usd'])
for k, lst in sorted(agg.items(), key=lambda x: -sum(x[1])):
    n = len(lst); s = sum(lst); ev = s/n; wr = 100*sum(1 for p in lst if p>0)/n
    print(f'  {k:30s}  n={n:3d}  EV=${ev:+.4f}  sum=${s:+.2f}  WR={wr:.1f}%')

# 6e. exit_reason × composite
def exit_reason_x_aligned(t):
    aligned = trend_aligned(t)
    return f'{aligned}__{t.get("exit_reason", "?")}'
print()
print('=== Composite: trend_aligned × exit_reason ===')
agg = defaultdict(list)
for t in all_trades:
    agg[exit_reason_x_aligned(t)].append(t['pnl_usd'])
items = sorted(agg.items(), key=lambda x: -sum(x[1]))
for k, lst in items[:12]:
    n = len(lst); s = sum(lst); ev = s/n
    print(f'  {k:30s}  n={n:3d}  EV=${ev:+.4f}  sum=${s:+.2f}')
