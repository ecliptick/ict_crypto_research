"""nb54d — Drill into the bear-short alpha + look for a single clean filter."""
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

# 1. Bear-short deep dive: is it robust across months?
print('\n=== bear__short trades per month ===')
month_buckets = defaultdict(list)
for t in all_trades:
    if int(t.get('trend_at_entry', 0)) < 0 and int(t.get('direction', 0)) < 0:
        month_buckets[t['month']].append(t['pnl_usd'])
for mo, lst in sorted(month_buckets.items()):
    s = sum(lst); n = len(lst); ev = s/n if n else 0
    wr = 100 * sum(1 for p in lst if p > 0) / n if n else 0
    print(f'  {mo}: n={n:3d}  EV=${ev:+.4f}  sum=${s:+.2f}  WR={wr:.1f}%')

# 2. By exit reason
print('\n=== bear__short × exit_reason ===')
exit_buckets = defaultdict(list)
for t in all_trades:
    if int(t.get('trend_at_entry', 0)) < 0 and int(t.get('direction', 0)) < 0:
        exit_buckets[t['exit_reason']].append(t['pnl_usd'])
for k, lst in sorted(exit_buckets.items(), key=lambda x: -sum(x[1])):
    n = len(lst); s = sum(lst); ev = s/n if n else 0
    print(f'  {k:6s}: n={n:3d}  EV=${ev:+.4f}  sum=${s:+.2f}')

# 3. Aligned trade drill — try different "fresh BoS" thresholds
print('\n=== aligned_no_fresh_bos — sweep across BoS thresholds ===')
print(f'  {"max_age_bars":>13s}  {"n_aligned":>9s}  {"EV":>8s}  {"sum":>10s}  {"WR":>6s}')
for max_age in [60, 300, 600, 1800, 3600, 7200, 86400]:
    in_dir = []
    no_bos = []
    for t in all_trades:
        tr = int(t.get('trend_at_entry', 0))
        d = int(t.get('direction', 0))
        if tr == 0:
            continue
        aligned = (tr > 0 and d > 0) or (tr < 0 and d < 0)
        if not aligned:
            continue
        k = int(t.get('last_break_kind', 0))
        a = int(t.get('last_break_age_bars', -1))
        bos_fresh_in_dir = (0 <= a <= max_age) and (
            (d > 0 and k == 1) or (d < 0 and k == -1))
        if bos_fresh_in_dir:
            in_dir.append(t['pnl_usd'])
        else:
            no_bos.append(t['pnl_usd'])
    if in_dir and no_bos:
        ev_in = sum(in_dir)/len(in_dir)
        ev_no = sum(no_bos)/len(no_bos)
        wr_no = 100*sum(1 for p in no_bos if p>0)/len(no_bos)
        print(f'  {max_age:>13d}  {len(no_bos):>9d}  ${ev_no:+.4f}  ${sum(no_bos):+9.2f}  {wr_no:5.1f}%')
    else:
        print(f'  {max_age:>13d}  {len(no_bos):>9d}  -         -            -')

# 4. Aligned + CHoCH freshness
print('\n=== aligned × CHoCH freshness ===')
print(f'  {"ch_age_max":>10s}  {"n_aligned":>9s}  {"EV":>8s}  {"sum":>10s}  {"WR":>6s}')
for max_age in [-1, 60, 300, 600, 1800, 3600, 86400]:
    no_choch = []
    has_recent = []
    for t in all_trades:
        tr = int(t.get('trend_at_entry', 0))
        d = int(t.get('direction', 0))
        if tr == 0:
            continue
        aligned = (tr > 0 and d > 0) or (tr < 0 and d < 0)
        if not aligned:
            continue
        ck = int(t.get('last_choch_kind', 0))
        ca = int(t.get('last_choch_age_bars', -1))
        if max_age < 0:
            if ck != 0 and ca >= 0:
                has_recent.append(t['pnl_usd'])
            else:
                no_choch.append(t['pnl_usd'])
        else:
            if ck != 0 and 0 <= ca <= max_age:
                has_recent.append(t['pnl_usd'])
            else:
                no_choch.append(t['pnl_usd'])
    if no_choch:
        ev = sum(no_choch)/len(no_choch)
        wr = 100*sum(1 for p in no_choch if p>0)/len(no_choch)
        print(f'  {max_age:>10d}  {len(no_choch):>9d}  ${ev:+.4f}  ${sum(no_choch):+9.2f}  {wr:5.1f}%')

# 5. ALL trades: trend-aligned only (signal)
print('\n=== Signal: trade only trend-aligned entries ===')
# Compare aligned vs all
aligned = [t['pnl_usd'] for t in all_trades
           if (int(t.get('trend_at_entry', 0)) > 0 and int(t.get('direction', 0)) > 0) or
              (int(t.get('trend_at_entry', 0)) < 0 and int(t.get('direction', 0)) < 0)
           and int(t.get('trend_at_entry', 0)) != 0]
all_pnl = [t['pnl_usd'] for t in all_trades]
print(f'  all trades:    n={len(all_pnl):3d}  EV=${sum(all_pnl)/len(all_pnl):+.4f}  sum=${sum(all_pnl):+.2f}  WR={100*sum(1 for p in all_pnl if p>0)/len(all_pnl):.1f}%')
print(f'  aligned only:  n={len(aligned):3d}  EV=${sum(aligned)/len(aligned):+.4f}  sum=${sum(aligned):+.2f}  WR={100*sum(1 for p in aligned if p>0)/len(aligned):.1f}%')
print(f'  filtered out:  n={len(all_pnl)-len(aligned):3d}')

# 6. Single best filter: bear trend + short direction + aligned
print('\n=== Signal: bear-short ONLY (entry must be short in bear trend) ===')
bear_short = [t['pnl_usd'] for t in all_trades
              if int(t.get('trend_at_entry', 0)) < 0 and int(t.get('direction', 0)) < 0]
excluded = [t['pnl_usd'] for t in all_trades if t['pnl_usd'] not in bear_short]
print(f'  all trades:           n={len(all_pnl):3d}  EV=${sum(all_pnl)/len(all_pnl):+.4f}  sum=${sum(all_pnl):+.2f}')
print(f'  bear-short only:      n={len(bear_short):3d}  EV=${sum(bear_short)/len(bear_short):+.4f}  sum=${sum(bear_short):+.2f}  WR={100*sum(1 for p in bear_short if p>0)/len(bear_short):.1f}%')
print(f'  bear-long (excluded): n={len([1 for t in all_trades if int(t.get("trend_at_entry",0))<0 and int(t.get("direction",0))>0]):3d}  (these are the WORST bucket)')

# 7. Also check: drop bear-long entirely + drop bull-short? or just keep both bull-short + bear-short?
print('\n=== Signal: drop bear-long (counter-trend longs in bear regime) ===')
filtered = [t['pnl_usd'] for t in all_trades
            if not (int(t.get('trend_at_entry', 0)) < 0 and int(t.get('direction', 0)) > 0)]
print(f'  all trades:              n={len(all_pnl):3d}  sum=${sum(all_pnl):+.2f}')
print(f'  drop bear-long only:     n={len(filtered):3d}  sum=${sum(filtered):+.2f}  WR={100*sum(1 for p in filtered if p>0)/len(filtered):.1f}%')
diff = sum(filtered) - sum(all_pnl)
print(f'  delta: ${diff:+.2f}  ({100*diff/len(filtered):+.2f}/trade)')

# 8. Single cleanest: bear_short + bull_long (the natural-aligned trades)
print('\n=== Signal: only take aligned trades (drop ALL counter-trend) ===')
aligned_only = [t['pnl_usd'] for t in all_trades
                if (int(t.get('trend_at_entry', 0)) > 0 and int(t.get('direction', 0)) > 0) or
                   (int(t.get('trend_at_entry', 0)) < 0 and int(t.get('direction', 0)) < 0)]
print(f'  baseline:                n={len(all_pnl):3d}  sum=${sum(all_pnl):+.2f}  EV=${sum(all_pnl)/len(all_pnl):+.4f}  WR={100*sum(1 for p in all_pnl if p>0)/len(all_pnl):.1f}%')
print(f'  aligned-only (filter):   n={len(aligned_only):3d}  sum=${sum(aligned_only):+.2f}  EV=${sum(aligned_only)/len(aligned_only):+.4f}  WR={100*sum(1 for p in aligned_only if p>0)/len(aligned_only):.1f}%')
print(f'  lift: {100*(sum(aligned_only)/len(aligned_only) - sum(all_pnl)/len(all_pnl)):+.2f}% per-trade, ${sum(aligned_only)-sum(all_pnl):+.2f} total')

# 9. Sweep distance (per-trade age to most-recent BoS/CHoCH — let me see if a "freshness" filter matters)
print('\n=== Trade entry age to most-recent break ===')
fresh_breaks = []
stale_breaks = []
for t in all_trades:
    a = int(t.get('last_break_age_bars', -1))
    if a < 0 or a > 300:
        stale_breaks.append(t['pnl_usd'])
    else:
        fresh_breaks.append(t['pnl_usd'])
print(f'  fresh (<300 bars):  n={len(fresh_breaks):3d}  EV=${sum(fresh_breaks)/len(fresh_breaks):+.4f}  sum=${sum(fresh_breaks):+.2f}')
print(f'  stale (>=300 bars): n={len(stale_breaks):3d}  EV=${sum(stale_breaks)/len(stale_breaks):+.4f}  sum=${sum(stale_breaks):+.2f}')
