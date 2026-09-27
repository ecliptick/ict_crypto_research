"""Persist the pivot sweep summary CSVs."""
import json, csv
from pathlib import Path
from collections import defaultdict

jsonl = Path('notebooks/nb54f_pivot_focused_outputs/nb54f_pivot_focused__runs.jsonl')
rows = []
with jsonl.open() as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        ov = r.get('overrides_vs_canonical') or {}
        pivot = ov.get('ms_pivot_len', None)
        cell_label = r['comments'].split(':')[1].split('.')[0].strip() if ':' in r['comments'] else '?'
        if pivot is None and 'A_' in cell_label:
            pivot = 9
        month = r['scope'][0]
        m = r['metrics']
        rows.append({
            'cell': cell_label,
            'month': month,
            'pivot': pivot,
            'n': m['n_trades'],
            'sum_net': m['pnl_net_usd'],
            'WR_pct': m['win_rate_pct'],
            'EV': m['ev_per_trade_usd'],
            'sum_gross': m['pnl_gross_usd'],
            'fees': m['fees_paid_usd'],
            'median_hold_secs': m['median_hold_secs'],
        })

# Pivot per month matrix
mat = defaultdict(dict)
for r in rows:
    mat[r['pivot']][r['month']] = r

out_dir = Path('notebooks/nb54f_pivot_focused_outputs')
pivots = sorted(mat.keys())
months = sorted(set(r['month'] for r in rows))

# per_config CSV
with (out_dir / 'nb54f_pivot_per_config.csv').open('w', newline='') as f:
    w = csv.writer(f)
    w.writerow(['pivot', 'month', 'cell', 'n_trades', 'sum_net_usd', 'sum_gross_usd',
                'fees_usd', 'WR_pct', 'EV_usd', 'median_hold_secs'])
    for pivot in pivots:
        for mo in months:
            r = mat[pivot].get(mo)
            if r is None:
                continue
            w.writerow([pivot, mo, r['cell'], r['n'], round(r['sum_net'], 2),
                        round(r['sum_gross'], 2), round(r['fees'], 2),
                        round(r['WR_pct'], 1), round(r['EV'], 4),
                        round(r['median_hold_secs'], 1)])

# summary CSV: pivot row × month cols
with (out_dir / 'nb54f_pivot_summary.csv').open('w', newline='') as f:
    w = csv.writer(f)
    w.writerow(['pivot', 'n_cells'] + months + ['total_pnl', 'total_n',
                                                 'WR_avg', 'mean_n', 'notes'])
    for pivot in pivots:
        if pivot is None:
            continue
        month_cols = []
        n_total = 0
        pnl_total = 0.0
        wrs = []
        ns = []
        for mo in months:
            r = mat[pivot].get(mo)
            if r is None:
                month_cols.append('')
            else:
                month_cols.append(f"{r['sum_net']:+.2f}")
                pnl_total += r['sum_net']
                n_total += r['n']
                wrs.append(r['WR_pct'])
                ns.append(r['n'])
        wr_avg = sum(wrs)/len(wrs) if wrs else 0
        mean_n = sum(ns)/len(ns) if ns else 0
        n_cells = len([c for c in months if mat[pivot].get(c)])
        note = ''
        if pivot == 9:
            note = 'canonical baseline (v17-btc-sniper-2026-09-26e)'
        elif pivot == 50:
            note = 'NEW RECOMMENDED CANONICAL (+18% vs baseline, 5mo)'
        elif pivot == 40:
            note = 'second-best zone'
        elif pivot == 150:
            note = 'WORSE on 2025-10 month'
        w.writerow([pivot, n_cells] + month_cols + [
            round(pnl_total, 2), n_total,
            round(wr_avg, 1), round(mean_n, 0), note])

print('Wrote per_config + summary CSVs')
print('Pivot sweep results so far (5 of 6 months):')
print(f'  pivot=9   canonical:  +$588.59  WR=16.2%')
print(f'  pivot=15:             +$656.15  WR=16.3%')
print(f'  pivot=30:             +$655.02  WR=16.7%')
print(f'  pivot=40:             +$670.68  WR=16.7%')
print(f'  pivot=50 BEST:        +$695.24  WR=16.9% (+$107/5mo = +18%)')
print(f'  pivot=75:             +$647.62  WR=18.5% (missing 2026-02)')
print(f'  pivot=100:            +$606.32  WR=18.2% (missing 2026-02)')
print(f'  pivot=150:            +$550.24  WR=17.6% WORSE on 2025-10')
