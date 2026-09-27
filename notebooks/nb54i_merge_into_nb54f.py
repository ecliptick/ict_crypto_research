"""Merge the 4 nb54i 2026-05 records into the nb54f JSONL."""
import json
from pathlib import Path

NB54F_JSONL = Path('notebooks/nb54f_pivot_focused_outputs/nb54f_pivot_focused__runs.jsonl')
NB54I_JSONL = Path('notebooks/nb54i_pivot_2026_05_complete_outputs/nb54i_pivot_2026_05_complete_outputs/nb54i_pivot_2026_05_complete__runs.jsonl')

# Read existing
existing = list(NB54F_JSONL.read_text().splitlines())

# Read new
new_records = []
for line in NB54I_JSONL.read_text().splitlines():
    line = line.strip()
    if not line: continue
    r = json.loads(line)
    # Convert to nb54f format
    pivot = r.get('overrides_vs_canonical', {}).get('ms_pivot_len')
    if pivot is None and 'A_' in r['comments']: pivot = 9
    if pivot == 50: cell_label = 'P_pivot_50'
    elif pivot == 75: cell_label = 'P_pivot_75'
    elif pivot == 100: cell_label = 'P_pivot_100'
    elif pivot == 150: cell_label = 'P_pivot_150'
    else: cell_label = '?'
    # Build a comment like the nb54f format
    new_comment = f"nb54f: {cell_label} pivot={pivot} | 6mo full sweep"
    new_records.append({
        **r,
        'notebook': 'nb54f_pivot_focused',
        'scenario': 'ms_pivot_focused_sweep',
        'comments': new_comment,
    })

# Filter existing for any 2026-05 records that already match pivots 50/75/100/150
filtered = []
for line in existing:
    line = line.strip()
    if not line: continue
    r = json.loads(line)
    ov = r.get('overrides_vs_canonical') or {}
    pivot = ov.get('ms_pivot_len')
    month = r['scope'][0]
    if month == '2026-05' and pivot in (50, 75, 100, 150):
        # Skip — being replaced
        continue
    filtered.append(r)

# Append new records
filtered.extend(new_records)

# Write back
with NB54F_JSONL.open('w', encoding='utf-8') as f:
    for r in filtered:
        f.write(json.dumps(r, ensure_ascii=False) + '\n')

print(f'nb54f JSONL: {len(filtered)} records (was {len(existing)})')
print(f'Added: {len(new_records)} new 2026-05 records')
