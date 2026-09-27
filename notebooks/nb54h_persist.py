"""Re-persist pivot=9 vs pivot=50 comparison into a clean CSV report."""
import json
import csv
from pathlib import Path
from collections import defaultdict

# Use the log file (which has all the cells)
log_path = Path('notebooks/nb54h_pivot50_validation_outputs/nb54h.log')
out_dir = Path('notebooks/nb54h_pivot50_validation_outputs')

cell_data = defaultdict(dict)
current_cell = None
for line in log_path.read_text().splitlines():
    line = line.strip()
    if line.startswith('==='):
        current_cell = line.replace('===', '').strip()
    elif 'sum=$' in line and current_cell:
        # [2025-04] cache=1.0s bt=2.9s n=68 sum=$+113.29 EV=$+1.6661 WR=16.2%
        parts = line.split()
        month = parts[0].strip('[]')
        # find 'n=' and 'sum=$' in parts
        n = None
        s = None
        ev = None
        wr = None
        for p in parts:
            if p.startswith('n='):
                n = int(p[2:])
            elif p.startswith('sum=$'):
                s_str = p[len('sum=$'):].replace('+','')
                s = float(s_str)
            elif p.startswith('EV=$'):
                ev_str = p[len('EV=$'):].replace('+','')
                ev = float(ev_str)
            elif p.startswith('WR='):
                wr_str = p[len('WR='):].rstrip('%')
                wr = float(wr_str)
        cell_data[current_cell][month] = {'n': n, 'sum': s, 'EV': ev, 'WR': wr}

# Write summary CSV
months = sorted(set(m for cell in cell_data.values() for m in cell.keys()))
cells = list(cell_data.keys())
out_csv = out_dir / 'nb54h_pivot_comparison.csv'
with out_csv.open('w', newline='') as f:
    w = csv.writer(f)
    w.writerow(['cell', 'ms_pivot_len'] + [f'{m}_n' for m in months] +
                [f'{m}_sum' for m in months] + [f'{m}_WR' for m in months] +
                ['total_n', 'total_sum', 'WR_avg', 'EV_avg'])
    for cell in cells:
        pivot = 9 if 'pivot_9' in cell else 50
        n_cols = []; sum_cols = []; wr_cols = []
        for mo in months:
            d = cell_data[cell].get(mo, {})
            n_cols.append(d.get('n', ''))
            sum_cols.append(f"{d.get('sum', 0):+.2f}" if d else '')
            wr_cols.append(f"{d.get('WR', 0):.1f}" if d else '')
        total_n = sum(d.get('n', 0) for d in cell_data[cell].values())
        total_sum = sum(d.get('sum', 0) for d in cell_data[cell].values())
        wr_avg = sum(d.get('WR', 0) for d in cell_data[cell].values()) / max(1, len(cell_data[cell]))
        ev_avg = total_sum / max(1, total_n)
        w.writerow([cell, pivot] + n_cols + sum_cols + wr_cols +
                   [total_n, f"{total_sum:+.2f}", f"{wr_avg:.1f}", f"{ev_avg:.4f}"])

# Per-month pivot comparison
out_csv2 = out_dir / 'nb54h_pivot_per_month.csv'
with out_csv2.open('w', newline='') as f:
    w = csv.writer(f)
    w.writerow(['month', 'pivot9_n', 'pivot9_sum', 'pivot9_WR',
                'pivot50_n', 'pivot50_sum', 'pivot50_WR',
                'pivot50_minus_pivot9'])
    cell9 = cell_data.get('A_pivot_9_override', {})
    cell50 = cell_data.get('B_pivot_50_new_canon', {})
    for mo in months:
        d9 = cell9.get(mo, {})
        d50 = cell50.get(mo, {})
        delta = d50.get('sum', 0) - d9.get('sum', 0)
        w.writerow([mo, d9.get('n', ''), f"{d9.get('sum', 0):+.2f}" if d9 else '',
                    f"{d9.get('WR', 0):.1f}" if d9 else '',
                    d50.get('n', ''), f"{d50.get('sum', 0):+.2f}" if d50 else '',
                    f"{d50.get('WR', 0):.1f}" if d50 else '',
                    f"{delta:+.2f}" if delta != 0 else ''])

print(f'Wrote: {out_csv}')
print(f'Wrote: {out_csv2}')

# Print summary
print('\n=== Headline (canonical v17-btc-sniper-2026-09-26g, qty=0.01, 6mo) ===')
print(f'{"Month":<12} {"pivot=9":>14} {"pivot=50":>14} {"delta":>12}')
print('-' * 56)
for mo in months:
    d9 = cell9.get(mo, {})
    d50 = cell50.get(mo, {})
    s9 = d9.get('sum', 0); s50 = d50.get('sum', 0)
    print(f'{mo:<12} ${s9:+9.2f} ({d9.get("n", 0)}t) ${s50:+9.2f} ({d50.get("n", 0)}t) ${(s50-s9):+9.2f}')
total9 = sum(d.get('sum', 0) for d in cell9.values())
total50 = sum(d.get('sum', 0) for d in cell50.values())
n_total = sum(d.get('n', 0) for d in cell50.values())
print('-' * 56)
print(f'{"TOTAL":<12} ${total9:+9.2f} ({sum(d.get("n",0) for d in cell9.values())}t) ${total50:+9.2f} ({n_total}t) ${(total50-total9):+9.2f}')
print(f'\nNet uplift: ${total50-total9:+.2f} over 6mo ({(total50-total9)/abs(total9)*100:+.1f}%)')
