"""nb54e — Combined-filter exploration.

Try the strongest single alpha signals COMBINED:
  1. bear-short only (single biggest EV bucket)
  2. aligned_no_fresh_bos (broad trend-aligned filter)
  3. aligned + age filter combinations
  4. bear-short + trend-aligned-no-bos intersection
"""
import json
from collections import defaultdict
from pathlib import Path

TAGS_DIR = Path('notebooks/nb54_vBTC3_ms_sweep_outputs')
TAG_FILES = sorted(TAGS_DIR.glob('nb54b_canonical_trade_tags__*.jsonl'))

all_trades = []
for f in TAG_FILES:
    rows = [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    all_trades.extend(rows)

print(f'Loaded {len(all_trades)} trades')

def summarize(lst, label):
    if not lst:
        return
    n = len(lst); s = sum(lst); ev = s/n
    wr = 100 * sum(1 for p in lst if p > 0) / n
    print(f'  {label:42s}  n={n:3d}  EV=${ev:+.4f}  sum=${s:+.2f}  WR={wr:.1f}%')

# 1. bear-short + no-fresh-bos
def bear_short_no_fresh_bos(t, max_age=300):
    if not (int(t.get('trend_at_entry', 0)) < 0 and int(t.get('direction', 0)) < 0):
        return False
    k = int(t.get('last_break_kind', 0))
    a = int(t.get('last_break_age_bars', -1))
    bos_fresh_in_dir = (0 <= a <= max_age) and (
        (int(t.get('direction', 0)) < 0 and k == -1))
    return not bos_fresh_in_dir

print('\n=== bear-short + no-fresh-bos ===')
filt = [t['pnl_usd'] for t in all_trades if bear_short_no_fresh_bos(t, 300)]
summarize(filt, 'bear-short + no-fresh-bos (<300)')

# 2. aligned-only
def aligned(t):
    tr = int(t.get('trend_at_entry', 0))
    d = int(t.get('direction', 0))
    if tr == 0:
        return False
    return (tr > 0 and d > 0) or (tr < 0 and d < 0)

print('\n=== aligned only ===')
filt = [t['pnl_usd'] for t in all_trades if aligned(t)]
summarize(filt, 'aligned only')

# 3. aligned + no-fresh-bos-against (drop BoS against direction even if aligned)
def aligned_no_bos_against(t, max_age=300):
    if not aligned(t):
        return False
    k = int(t.get('last_break_kind', 0))
    a = int(t.get('last_break_age_bars', -1))
    d = int(t.get('direction', 0))
    bos_against = (0 <= a <= max_age) and (
        (d > 0 and k == -1) or (d < 0 and k == 1))
    return not bos_against

print('\n=== aligned + drop BoS-against (fresh) ===')
filt = [t['pnl_usd'] for t in all_trades if aligned_no_bos_against(t, 300)]
summarize(filt, 'aligned + drop-bos-against (<300)')

# 4. aligned + drop both bos_against AND choch_against (caution flag)
def aligned_drop_caution(t, max_choch_age=600):
    if not aligned(t):
        return False
    ck = int(t.get('last_choch_kind', 0))
    ca = int(t.get('last_choch_age_bars', -1))
    d = int(t.get('direction', 0))
    choch_against = (0 <= ca <= max_choch_age) and (
        (d > 0 and ck == -2) or (d < 0 and ck == 2))
    return not choch_against

print('\n=== aligned + drop CHoCH-against ===')
filt = [t['pnl_usd'] for t in all_trades if aligned_drop_caution(t, 600)]
summarize(filt, 'aligned + drop-choch-against (<600)')

# 5. triple filter: aligned + no-bos-against + no-choch-against
def aligned_safe(t):
    return aligned_no_bos_against(t, 300) and aligned_drop_caution(t, 600)

print('\n=== aligned + drop-bos-against + drop-choch-against ===')
filt = [t['pnl_usd'] for t in all_trades if aligned_safe(t)]
summarize(filt, 'aligned + no-bos-against + no-choch-against')

# 6. Per-month robustness for aligned_safe
print('\n=== aligned_safe per-month ===')
month_buckets = defaultdict(list)
for t in all_trades:
    if aligned_safe(t):
        month_buckets[t['month']].append(t['pnl_usd'])
for mo, lst in sorted(month_buckets.items()):
    s = sum(lst); n = len(lst); ev = s/n if n else 0
    wr = 100 * sum(1 for p in lst if p > 0) / n if n else 0
    print(f'  {mo}: n={n:3d}  EV=${ev:+.4f}  sum=${s:+.2f}  WR={wr:.1f}%')

# 7. bear-short per-month (single biggest signal)
print('\n=== bear-short per-month (single biggest alpha) ===')
month_buckets = defaultdict(list)
for t in all_trades:
    if int(t.get('trend_at_entry', 0)) < 0 and int(t.get('direction', 0)) < 0:
        month_buckets[t['month']].append(t['pnl_usd'])
for mo, lst in sorted(month_buckets.items()):
    s = sum(lst); n = len(lst); ev = s/n if n else 0
    wr = 100 * sum(1 for p in lst if p > 0) / n if n else 0
    print(f'  {mo}: n={n:3d}  EV=${ev:+.4f}  sum=${s:+.2f}  WR={wr:.1f}%')

# 8. bull-short per-month (the other direction)
print('\n=== bull-short per-month (counter-trend short — second-biggest bucket) ===')
month_buckets = defaultdict(list)
for t in all_trades:
    if int(t.get('trend_at_entry', 0)) > 0 and int(t.get('direction', 0)) < 0:
        month_buckets[t['month']].append(t['pnl_usd'])
for mo, lst in sorted(month_buckets.items()):
    s = sum(lst); n = len(lst); ev = s/n if n else 0
    wr = 100 * sum(1 for p in lst if p > 0) / n if n else 0
    print(f'  {mo}: n={n:3d}  EV=${ev:+.4f}  sum=${s:+.2f}  WR={wr:.1f}%')

# 9. bull-long per-month (also counter-trend long)
print('\n=== bull-long per-month ===')
month_buckets = defaultdict(list)
for t in all_trades:
    if int(t.get('trend_at_entry', 0)) > 0 and int(t.get('direction', 0)) > 0:
        month_buckets[t['month']].append(t['pnl_usd'])
for mo, lst in sorted(month_buckets.items()):
    s = sum(lst); n = len(lst); ev = s/n if n else 0
    wr = 100 * sum(1 for p in lst if p > 0) / n if n else 0
    print(f'  {mo}: n={n:3d}  EV=${ev:+.4f}  sum=${s:+.2f}  WR={wr:.1f}%')

# 10. bear-long per-month (counter-trend long in bear regime — the WORST)
print('\n=== bear-long per-month (counter-trend long in bear) ===')
month_buckets = defaultdict(list)
for t in all_trades:
    if int(t.get('trend_at_entry', 0)) < 0 and int(t.get('direction', 0)) > 0:
        month_buckets[t['month']].append(t['pnl_usd'])
for mo, lst in sorted(month_buckets.items()):
    s = sum(lst); n = len(lst); ev = s/n if n else 0
    wr = 100 * sum(1 for p in lst if p > 0) / n if n else 0
    print(f'  {mo}: n={n:3d}  EV=${ev:+.4f}  sum=${s:+.2f}  WR={wr:.1f}%')
