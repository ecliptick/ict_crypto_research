"""Preliminary fee-vs-move analysis for a hypothetical 1:2 RR sniper flip."""
import pandas as pd
import numpy as np

df = pd.read_csv('notebooks/nb56_dual_entry_top3_outputs/nb56_dual_entry_top3_per_trade.csv')
sniper = df[(df['entry_triggered_by'] == 'ifvg_clean') & (df['combo'] == 'B_n_5x60')].copy()

print('=== Existing sniper SL/TP on 2025-04 (canonical 5x SL / 60x TP zone) ===')
print(f"  median SL = ${sniper['stop_usd'].median():.2f}")
print(f"  median TP = ${sniper['target_usd'].median():.2f}")
median_zone = sniper['stop_usd'].median() / 5.0
print(f"  implied median zone width = ${median_zone:.2f}")
print()

fee_per_trade = 0.001 * 100000 * 0.0005 * 2  # 5bps taker, 2 sides
print(f'Per-trade round-trip fee at qty_btc=0.001: ${fee_per_trade:.4f}')
print()

# Try several 1:2 RR configs anchored to median zone width
print('=== 1:2 RR using zone_mult arithmetic ===')
print('Format: (sl_zone_mult, tp_zone_mult) | (median SL $, median TP $, TP_gross $)')

for sl_zm, tp_zm in [(1.0, 2.0), (2.0, 4.0), (5.0, 10.0), (10.0, 20.0), (15.0, 30.0)]:
    sl = sl_zm * median_zone
    tp = tp_zm * median_zone
    tp_gross = tp * 0.001
    sl_cost = sl * 0.001
    print(f"  ({sl_zm:.1f}x, {tp_zm:.1f}x) | "
          f"SL=${sl:.2f} TP=${tp:.2f} | "
          f"SL_cost=${sl_cost:.4f} TP_gross=${tp_gross:.4f} | "
          f"TP-net=${tp_gross - fee_per_trade:+.4f} "
          f"SL-net=${-sl_cost - fee_per_trade:+.4f}")

print()
print('=== Fee-break-even threshold ===')
# To clear fees on a TP win: TP_gross > fees
# TP = tp_zone_mult * median_zone; qty_btc=0.001
# TP_gross = tp_zone_mult * median_zone * 0.001
# Required: tp_zone_mult * 43.60 * 0.001 > 0.10
# 0.10 = tp_zone_mult * 43.60 * 0.001 -> tp_zone_mult > 2.29
min_tp_to_clear_fees = fee_per_trade / (median_zone * 0.001)
print(f"  Minimum TP zone_mult to clear ${fee_per_trade:.4f} fees per trade: {min_tp_to_clear_fees:.2f}x")
print(f"  Equivalently, minimum TP distance in USD: ${min_tp_to_clear_fees * median_zone:.2f}")
print()

# What if we use bigger qty_btc to scale fee-vs-gross?
print('=== Sensitivity to lot size ===')
for qty in [0.001, 0.005, 0.01, 0.05, 0.1]:
    fee = qty * 100000 * 0.0005 * 2
    # At 1:2 RR with 5x/10x zone (still tight)
    sl = 5 * median_zone
    tp = 10 * median_zone
    sl_cost = sl * qty
    tp_gross = tp * qty
    print(f"  qty_btc={qty:.3f} | fee=${fee:.4f} | "
          f"SL_cost=${sl_cost:.4f} TP_gross=${tp_gross:.4f} | "
          f"TP_net=${tp_gross-fee:+.4f}")

print()
print('=== What WR does 1:2 RR need at fee-dominance? ===')
# If fees dominate, even 50% WR loses.
# E.g., SL_cost + fee_per_trade = TP_gross - fee_per_trade (break-even)
# p * TP - (1-p) * SL - 2*fee = 0
# p * (TP + SL) = SL + 2*fee
# p = (SL + 2*fee) / (TP + SL)
for sl_zm, tp_zm in [(1.0, 2.0), (5.0, 10.0)]:
    sl = sl_zm * median_zone
    tp = tp_zm * median_zone
    sl_cost = sl * 0.001
    tp_gross = tp * 0.001
    breakeven_wr = (sl_cost + fee_per_trade) / (tp_gross + sl_cost)
    print(f"  ({sl_zm:.1f}x, {tp_zm:.1f}x): need WR={breakeven_wr*100:.1f}% to break even (50% gross-edge case)")
