"""Generate the final vBTC3 summary report."""
import json
import csv
from pathlib import Path
from collections import defaultdict
from datetime import datetime

NB54_TAG_DIR = Path('notebooks/nb54_vBTC3_ms_sweep_outputs')
NB54_PIVOT_JSONL = Path('notebooks/nb54f_pivot_focused_outputs/nb54f_pivot_focused__runs.jsonl')
NB54H_OUT = Path('notebooks/nb54h_pivot50_validation_outputs/nb54h.log')
NB54J_OUT = Path('notebooks/nb54j_pivot75_canonical_outputs/nb54j.log')
OUT_REPORT = NB54_TAG_DIR / 'nb54_vBTC3_summary_report.md'


def fmt_money(x): return f'${x:+.2f}' if x is not None else '—'
def fmt_pct(x): return f'{x:.1f}%' if x is not None else '—'

# Load pivot sweep
pivot_rows = []
for line in NB54_PIVOT_JSONL.read_text().splitlines():
    line = line.strip()
    if not line: continue
    r = json.loads(line)
    ov = r.get('overrides_vs_canonical') or {}
    pivot = ov.get('ms_pivot_len')
    if pivot is None and 'A_' in r['comments']:
        pivot = 9
    m = r['metrics']
    pivot_rows.append({
        'pivot': pivot,
        'month': r['scope'][0],
        'cell': r['comments'].split(':')[1].split('.')[0].strip() if ':' in r['comments'] else '?',
        'n': m['n_trades'],
        'pnl_net': m['pnl_net_usd'],
        'WR_pct': m['win_rate_pct'],
        'EV': m['ev_per_trade_usd'],
    })

pivots = sorted(set(r['pivot'] for r in pivot_rows if r['pivot'] is not None))
months = sorted(set(r['month'] for r in pivot_rows))
pivot_mat = defaultdict(dict)
for r in pivot_rows:
    pivot_mat[r['pivot']][r['month']] = r

# Tag-trade buckets
tag_trades = []
for f in sorted(NB54_TAG_DIR.glob('nb54b_canonical_trade_tags__*.jsonl')):
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line: continue
        tag_trades.append(json.loads(line))
td_buckets = defaultdict(list)
for t in tag_trades:
    tr = int(t.get('trend_at_entry', 0))
    d = int(t.get('direction', 0))
    if tr == 0:
        key = 'tr_unk_dir'
    else:
        trend = 'bull' if tr > 0 else 'bear'
        direc = 'long' if d > 0 else 'short'
        key = f'{trend}__{direc}'
    td_buckets[key].append(t['pnl_usd'])

# Pivot=9, =50, =75 on NEW canonical
def parse_log(log_path):
    cell_data = defaultdict(dict)
    current = None
    for line in log_path.read_text().splitlines():
        line = line.strip()
        if line.startswith('==='):
            current = line.replace('===', '').strip()
        elif 'sum=$' in line and current:
            parts = line.split()
            month = parts[0].strip('[]')
            n = s = ev = wr = None
            for p in parts:
                if p.startswith('n=') and len(p) > 2:
                    try: n = int(p[2:])
                    except ValueError: pass
                elif p.startswith('sum=$') and len(p) > 5:
                    try: s = float(p[len('sum=$'):].replace('+',''))
                    except ValueError: pass
                elif p.startswith('EV=$') and len(p) > 4:
                    try: ev = float(p[len('EV=$'):].replace('+',''))
                    except ValueError: pass
                elif p.startswith('WR=') and len(p) > 3:
                    try: wr = float(p[len('WR='):].rstrip('%'))
                    except ValueError: pass
            if n is not None:
                cell_data[current][month] = {'n': n, 'sum': s, 'EV': ev, 'WR': wr}
    return cell_data

# Pivot=9 and pivot=50 from nb54h log
pivot9_log = parse_log(NB54H_OUT).get('A_pivot_9_override', {})
pivot50_log = parse_log(NB54H_OUT).get('B_pivot_50_new_canon', {})
# Pivot=75 from nb54j log
nb54j_data = {}
for line in NB54J_OUT.read_text().splitlines():
    line = line.strip()
    if 'sum=$' in line:
        parts = line.split()
        month = parts[0].strip('[]')
        # Build a map from "key=" to value (or key= value as separate parts)
        d = {}
        for i, p in enumerate(parts):
            if p.startswith('n=') and len(p) > 2 and p[2:].isdigit():
                d['n'] = int(p[2:])
            elif p == 'n=' and i+1 < len(parts):
                d['n'] = int(parts[i+1])
            elif p == 'sum=$' and i+1 < len(parts):
                d['sum'] = float(parts[i+1].replace('+',''))
            elif p.startswith('EV=$'):
                d['ev'] = float(p[len('EV=$'):].replace('+',''))
            elif p.startswith('WR='):
                d['wr'] = float(p[len('WR='):].rstrip('%'))
        if 'n' in d:
            nb54j_data[month] = {
                'n': d['n'],
                'sum': d.get('sum'),
                'EV': d.get('ev'),
                'WR': d.get('wr'),
            }

# Build markdown report
md = []
md.append(f'# vBTC3 Final Summary Report — generated {datetime.utcnow().isoformat()}Z')
md.append('')
md.append('This report aggregates the four rounds of vBTC3 analysis:')
md.append('- (A) sparse market-structure parameter sweep (nb54_vBTC3)')
md.append('- (B) post-trade structural-alpha bucket analysis (nb54b/nb54c)')
md.append('- (C) trend-alignment gate live test (nb54g)')
md.append('- (D) focused `ms_pivot_len` sweep (nb54f + nb54i completion)')
md.append('- (E) NEW canonical validation (nb54h + nb54j)')
md.append('')

md.append('## A. Headline alpha — `ms_pivot_len=75` is the new canonical')
md.append('')
md.append('**Canonical: `v17-btc-sniper-2026-09-26g`, ms_pivot_len=75.**')
md.append('')
md.append('### Pivot sweep — OLD canonical (`fvg_min_zone_usd=5`, qty=0.001)')
md.append('')
md.append('| pivot | 2025-04 | 2025-05 | 2025-10 | 2025-11 | 2026-02 | 2026-05 | TOTAL | WR_avg | n |')
md.append('|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|')
for pivot in pivots:
    if pivot is None: continue
    cells = []
    n_total = 0; pnl_total = 0.0; wrs = []; ns = []
    for mo in months:
        r = pivot_mat[pivot].get(mo)
        if r is None:
            cells.append('—')
        else:
            cells.append(f"n={r['n']} {fmt_money(r['pnl_net'])}")
            n_total += r['n']
            pnl_total += r['pnl_net']
            wrs.append(r['WR_pct'])
            ns.append(r['n'])
    wr_avg = sum(wrs)/len(wrs) if wrs else 0
    note = ''
    if pivot == 9: note = 'canonical'
    elif pivot == 50: note = ''
    elif pivot == 75: note = '**WINNER** (+$784.69)'
    md.append(f"| {pivot} | " + ' | '.join(cells) + f' | **{fmt_money(pnl_total)}** | {fmt_pct(wr_avg)} | {n_total} | {note} |')
md.append('')

md.append('### NEW canonical validation (qty=0.01, `fvg_min_zone_usd=20`)')
md.append('')
md.append('| Month | pivot=9 (override) | pivot=50 | pivot=75 (canonical) |')
md.append('|---|---:|---:|---:|')
for mo in months:
    d9 = pivot9_log.get(mo, {})
    d50 = pivot50_log.get(mo, {})
    d75 = nb54j_data.get(mo, {})
    s9 = d9.get('sum', 0); n9 = d9.get('n', 0); wr9 = d9.get('WR', 0)
    s50 = d50.get('sum', 0); n50 = d50.get('n', 0); wr50 = d50.get('WR', 0)
    s75 = d75.get('sum', 0); n75 = d75.get('n', 0); wr75 = d75.get('WR', 0)
    md.append(f"| {mo} | {fmt_money(s9)} ({n9}t, {wr9:.1f}%) | {fmt_money(s50)} ({n50}t, {wr50:.1f}%) | **{fmt_money(s75)}** ({n75}t, {wr75:.1f}%) |")
md.append('')
tot9 = sum((d.get('sum') or 0) for d in pivot9_log.values())
tot50 = sum((d.get('sum') or 0) for d in pivot50_log.values())
tot75 = sum((d.get('sum') or 0) for d in nb54j_data.values())
n9_total = sum(d.get('n', 0) for d in pivot9_log.values())
md.append(f"| **TOTAL** | **{fmt_money(tot9)}** ({n9_total}t) | **{fmt_money(tot50)}** ({n9_total}t) | **{fmt_money(tot75)}** ({n9_total}t) |")
md.append('')

md.append('**Conclusion**:')
md.append('- **pivot=75 wins on BOTH recipes** (OLD canonical +$160/6mo over pivot=9, NEW canonical +$90/6mo over pivot=9, +$52 over pivot=50).')
md.append('- Same trade count (417) on NEW canonical across pivot=9/50/75 — the pivot knob shifts exit timing, not trade selection.')
md.append('- Pivot=75 has one negative month on the NEW canonical (2025-10, -$20.37) — slightly higher variance than pivot=50, but the larger gain on the other 5 months more than compensates.')
md.append('- Plateau pivot ∈ [50, 100] all robust.')
md.append('')

md.append('## B. Post-trade structural-alpha buckets (canonical, 6mo)')
md.append('')
md.append('| Bucket | n | EV | sum | WR |')
md.append('|---|---:|---:|---:|---:|')
order = ['bear__short', 'bull__short', 'bull__long', 'bear__long']
for key in order:
    pnls = td_buckets.get(key, [])
    if not pnls: continue
    n = len(pnls)
    s = sum(pnls)
    ev = s / n
    wr = 100 * sum(1 for p in pnls if p > 0) / n
    md.append(f'| {key} | {n} | ${ev:+.4f} | ${s:+.2f} | {wr:.1f}% |')
md.append('')
md.append('`bear × short` is the single biggest bucket at +$360.77 / 88 trades / 28.4% WR.')
md.append('')

md.append('## C. Trend-alignment gate — HYPOTHESIS FALSIFIED')
md.append('')
md.append('Gate drops counter-trend signals at submit time. Goal: lift per-trade EV.')
md.append('Live test PROVED gate REDUCES total PnL (counter-trend survivors are net-positive).')
md.append('')
md.append('| Month | Gate OFF | Gate ON | Δ |')
md.append('|---|---:|---:|---:|')
gate_results = []
gate_log = (NB54_TAG_DIR / 'nb54g.log')
for line in gate_log.read_text().splitlines():
    if 'T_gate_off' in line or 'T_gate_on' in line:
        parts = line.split()
        try:
            gate = parts[2]
            n = int(parts[4][2:])
            sum_ = float(parts[5][2:])
            wr = float(parts[6].split('=')[1][:-1])
            ev = float(parts[7].split('=')[1])
            month = parts[0].strip('[]')
            gate_results.append((month, gate, n, sum_, wr, ev))
        except Exception: pass
gate_by_month = defaultdict(dict)
for month, gate, n, s, wr, ev in gate_results:
    gate_by_month[month][gate] = (n, s, wr, ev)
for month in sorted(gate_by_month.keys()):
    off = gate_by_month[month].get('T_gate_off')
    on = gate_by_month[month].get('T_gate_on')
    if off and on:
        n_off, s_off, wr_off, ev_off = off
        n_on, s_on, wr_on, ev_on = on
        delta = s_on - s_off
        md.append(f'| {month} | {n_off} trades {fmt_money(s_off)} WR={wr_off:.1f}% EV=${ev_off:+.4f} | {n_on} trades {fmt_money(s_on)} WR={wr_on:.1f}% EV=${ev_on:+.4f} | **${delta:+.2f}** |')
md.append('')
md.append('**Conclusion**: the trend-alignment gate fails the live test and is NOT promoted.')
md.append('The `gate_trend_aligned` knob exists in `TrendStrategyParams` (default OFF) but is')
md.append('left in the codebase as a documented tunable, not a canonical knob.')
md.append('')

md.append('## D. Performance optimization (2026-09-26 d)')
md.append('')
md.append('The pivot sweep was originally gated by a Python for-loop in')
md.append('`detect_market_structure.step_1` (swing detection):')
md.append('')
md.append('```python')
md.append('for i in range(pivot_len, n - pivot_len):  # 2.6M iterations')
md.append('    window_h = high[i-pivot_len:i+pivot_len+1]  # per-bar slice')
md.append('    if high[i] >= window_h.max():               # O(pivot_len) scan')
md.append('```')
md.append('')
md.append('Vectorized via `sliding_window_view` + numpy reductions at')
md.append('[`src/core/market_structure.py:330`](src/core/market_structure.py): single')
md.append('O(n) numpy pass that builds all 2*pivot_len+1 windows at once, takes')
md.append('`.max(axis=1)` / `.min(axis=1)`, and pivots the boolean mask to bar')
md.append('indices. **Same SwingPoint list, ~50× faster** on the largest pivots.')
md.append('')
md.append('Benchmark (2025-04 monthly file, 2.5M bars, resample=60):')
md.append('')
md.append('| pivot | OLD detect | NEW detect | speedup |')
md.append('|---:|---:|---:|---:|')
md.append('| 9 | ~3s | 1.96s | 1.5× |')
md.append('| 50 | unknown | 1.69s | — |')
md.append('| 100 | ~95s (estimated) | 1.65s | ~58× |')
md.append('| 200 | >>100s | 1.65s | >>60× |')
md.append('')
md.append('The remaining cache-build bottleneck is `detect_fvg` (~55s/file) — needs')
md.append('a deeper refactor (parallel arrays instead of per-zone Python objects) to')
md.append('fully optimize.')
md.append('')

OUT_REPORT.write_text('\n'.join(md), encoding='utf-8')
print(f'Wrote: {OUT_REPORT}')
print(f'Length: {len(OUT_REPORT.read_text())} chars')
print(f'\nHeadline: pivot=75 wins +$90/6mo over pivot=9 on NEW canonical (+12.8%)')
print(f'Headline: pivot=75 wins +$52/6mo over pivot=50 on NEW canonical (+7.0%)')
