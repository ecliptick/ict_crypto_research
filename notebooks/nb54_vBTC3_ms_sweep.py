"""vBTC3 — sparse market-structure param sweep on BTC.

Vary the knobs that flow into ``detect_market_structure`` and the
structure-driven conviction/timing filter on FVG/iFVG signals:

* ms_pivot_len          (default 9)
* ms_liquidity_len      (default 30)
* ms_resample_secs      (default 60)
* ms_boost_conviction   (default 1.0)
* ms_min_conviction     (default 0.0)
* bos_choch_ignore_invert_when_aligned (default True)
* use_market_structure  (default True)

Performance:
  * The ms_pivot_len / ms_liquidity_len / ms_resample_secs knobs
    are in the cache fingerprint, so changing them forces a full
    structure rebuild (~96s on a monthly file). For cells that
    DON'T vary these, the canonical SweepCache is reused.
  * Cells that DO vary them get their own cache (one per unique
    triple). 96s rebuild × unique triples × 6 months.

Per-cell backtest attaches structural tags (BoS/CHoCH kind+age,
swing, sweep, trend, etc.) to each closed trade so the structural
alpha fallback (post-trade classification) has the raw signal.

Output:
    notebooks/nb54_vBTC3_ms_sweep_outputs/nb54_vBTC3_ms_sweep__runs.jsonl
"""
import argparse, sys, time, json
sys.path.insert(0, 'c:/coding/ict_crypto_research')
sys.path.insert(0, 'c:/coding/ict_crypto_research/notebooks')
from pathlib import Path
from src.core.optimal_config import optimal_params, OPTIMAL_RECIPE_VERSION
from src.tick.cache import get_or_build, SweepCache
from src.tick.tick_backtest import run_tick_backtest
from src.core.run_report import append_run_report

NOTEBOOK = "nb54_vBTC3_ms_sweep"
SCENARIO = "ms_param_sweep"

# 6-month sample (same as nb53_vBTC2_sweep_multimode).
DEFAULT_SCOPE = [
    ('2025-04', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-04.parquet'),
    ('2025-05', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-05.parquet'),
    ('2025-10', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-10.parquet'),
    ('2025-11', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2025-11.parquet'),
    ('2026-02', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2026-02.parquet'),
    ('2026-05', r'C:\coding\ict_tier_v2\data\binance_um_aggtrades\raw\BTCUSDT-aggTrades-2026-05.parquet'),
]


def _build_cells():
    """Sparse single-knob perturbations around the canonical baseline.

    Each cell is annotated with ``rebuild`` — True if the cell's
    overrides change a cache-fingerprint param (ms_pivot_len /
    ms_liquidity_len / ms_resample_secs / detect_order_blocks /
    detect_liquidity). False otherwise (signal-loop only).
    """
    cells = []
    # A — baseline (canonical) — reuses canonical SweepCache.
    cells.append(('A_baseline', {}, False))
    # B — pivot_len (swing detection) — REBUILDS structure.
    cells.append(('B_pivot_5',    {'ms_pivot_len': 5}, True))
    cells.append(('B_pivot_15',   {'ms_pivot_len': 15}, True))
    cells.append(('B_pivot_25',   {'ms_pivot_len': 25}, True))
    # C — liquidity_len (sweep detection) — REBUILDS structure.
    cells.append(('C_liq_15',     {'ms_liquidity_len': 15}, True))
    cells.append(('C_liq_60',     {'ms_liquidity_len': 60}, True))
    cells.append(('C_liq_120',    {'ms_liquidity_len': 120}, True))
    # D — ms_resample_secs (pivot timeframe) — REBUILDS structure.
    cells.append(('D_res_0',      {'ms_resample_secs': 0}, True))
    cells.append(('D_res_300',    {'ms_resample_secs': 300}, True))
    cells.append(('D_res_1800',   {'ms_resample_secs': 1800}, True))
    # E — conviction boost scale — signal-loop only.
    cells.append(('E_boost_0',    {'ms_boost_conviction': 0.0}, False))
    cells.append(('E_boost_1.5',  {'ms_boost_conviction': 1.5}, False))
    # F — min conviction floor — signal-loop only.
    cells.append(('F_min_0.5',    {'ms_min_conviction': 0.5}, False))
    cells.append(('F_min_0.85',   {'ms_min_conviction': 0.85}, False))
    # G — ignore_invert_when_aligned — signal-loop only.
    cells.append(('G_keep_invert',{'bos_choch_ignore_invert_when_aligned': False}, False))
    # H — turn market structure off entirely — signal-loop only.
    cells.append(('H_no_ms',      {'use_market_structure': False}, False))
    return cells


CELLS = _build_cells()
DEDUP_KEYS = (
    'ms_pivot_len',
    'ms_liquidity_len',
    'ms_resample_secs',
    'ms_boost_conviction',
    'ms_min_conviction',
    'bos_choch_ignore_invert_when_aligned',
    'use_market_structure',
)


def _per_trade_structural_tags(res, structure_state) -> list:
    """For each closed trade, tag with structure metadata at entry.

    Used by the structural-alpha fallback analysis if no cell moves
    the needle.
    """
    trades = list(getattr(res, 'trades', []))
    if not trades or structure_state is None:
        return []
    trend = structure_state.trend
    last_break_kind = structure_state.last_break_kind
    last_break_age = structure_state.last_break_age_bars
    last_choch_kind = structure_state.last_choch_kind
    last_choch_age = structure_state.last_choch_age_bars
    events = structure_state.events
    swings = list(getattr(structure_state, 'swings', []))
    # Pre-sort swings by bar so the binary search per trade is O(log n).
    swings_sorted = sorted(swings, key=lambda s: s.bar)

    def _last_swing_before(bar):
        # Binary-search for the rightmost swing with bar <= bar.
        lo, hi = 0, len(swings_sorted)
        while lo < hi:
            mid = (lo + hi) // 2
            if swings_sorted[mid].bar <= bar:
                lo = mid + 1
            else:
                hi = mid
        if lo == 0:
            return None
        return swings_sorted[lo - 1]

    tags = []
    for t in trades:
        eb = int(t.entry_bar)
        if eb < 0 or eb >= trend.shape[0]:
            continue
        last_sw = _last_swing_before(eb)
        swing_age_bars = (eb - last_sw.bar) if last_sw is not None else -1
        if last_sw is None:
            swing_kind = 'none'
        elif last_sw.is_high:
            swing_kind = 'high'
        else:
            swing_kind = 'low'

        tags.append({
            'entry_bar': eb,
            'direction': int(t.direction),
            'pnl_usd': float(t.pnl_usd),
            'exit_reason': str(t.exit_reason),
            'entry_triggered_by': str(getattr(t, 'entry_triggered_by', '')),
            'trend_at_entry': int(trend[eb]),
            'last_break_kind': int(last_break_kind[eb]),
            'last_break_age_bars': int(last_break_age[eb]),
            'last_choch_kind': int(last_choch_kind[eb]),
            'last_choch_age_bars': int(last_choch_age[eb]),
            'event_at_entry': int(events[eb]),
            'last_swing_kind': swing_kind,
            'last_swing_age_bars': int(swing_age_bars),
        })
    return tags


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fast", action="store_true",
                   help="Single-file smoke (2025-04).")
    p.add_argument("--no-write", action="store_true",
                   help="Skip writing the JSONL.")
    p.add_argument("--cells", type=int, default=None,
                   help="Cap the cell count for hand-test runs.")
    p.add_argument("--cells-only", type=str, default=None,
                   help="Comma-separated cell-label substring filter.")
    args = p.parse_args()

    scope = DEFAULT_SCOPE
    if args.fast:
        scope = DEFAULT_SCOPE[:1]

    cells = list(CELLS)
    if args.cells is not None:
        cells = cells[:args.cells]
    if args.cells_only:
        filt = [s.strip() for s in args.cells_only.split(',')]
        cells = [c for c in cells if any(f in c[0] for f in filt)]

    print(f"[{NOTEBOOK}] sweeping {len(cells)} cells × {len(scope)} files "
          f"({len(cells) * len(scope)} runs total)...", flush=True)

    grand_t0 = time.perf_counter()
    for month, raw_path in scope:
        raw = Path(raw_path)
        if not raw.exists():
            print(f"  WARN: {raw} not found, skipping {month}", flush=True)
            continue

        # Per-file cache map: fingerprint-key -> SweepCache (or None).
        cache_pool: dict = {}
        # Always have a canonical SweepCache pre-built for reuse on
        # the non-rebuild cells.
        canonical_p = optimal_params(qty_btc=0.01)
        print(f"\n[{NOTEBOOK}] {month}: building canonical SweepCache "
              f"({raw.name})...", flush=True)
        t0 = time.perf_counter()
        canonical_cache = get_or_build(raw, canonical_p, with_side_table=True, verbose=False)
        cache_pool['__canon__'] = canonical_cache
        print(f"  canonical cache={time.perf_counter() - t0:.1f}s "
              f"fvg={len(canonical_cache.fvg_zones)} ifvg={len(canonical_cache.ifvg_zones)}",
              flush=True)

        for j, (label, ov, rebuild) in enumerate(cells):
            p_i = optimal_params(qty_btc=0.01, **ov)
            t0 = time.perf_counter()
            if rebuild:
                # Use a cache key based on the triple (pivot, liq, resample).
                key = (p_i.ms_pivot_len, p_i.ms_liquidity_len, p_i.ms_resample_secs)
                if key not in cache_pool:
                    print(f"  [{month}] {label:18s}: rebuilding SweepCache "
                          f"(pivot={key[0]}, liq={key[1]}, resample={key[2]})...",
                          flush=True)
                    cache_pool[key] = get_or_build(raw, p_i, with_side_table=True, verbose=False)
                sc = cache_pool[key]
            else:
                sc = canonical_cache
            res = run_tick_backtest(
                None, p_i, strategy_label=label,
                pre_aggregated_bars=sc.bars,
                precomputed_zones_by_src={'fvg': sc.fvg_zones, 'ifvg': sc.ifvg_zones},
                precomputed_structure=sc.structure,
                precomputed_atr=sc.atr,
                side_table=sc.side_table,
            )
            t_bt = time.perf_counter() - t0
            pnls_net = [float(t.pnl_usd) for t in res.trades]
            fees = [float(getattr(t, 'fee_usd', 0.0)) for t in res.trades]
            sum_net = sum(pnls_net)
            sum_gross = sum_net + sum(fees)
            n = len(pnls_net)
            ev = sum_net / n if n else 0.0
            wr = (sum(1 for v in pnls_net if v > 0) / n * 100) if n else 0.0
            tags = _per_trade_structural_tags(res, sc.structure)
            # Persist tags as a sidecar JSONL (one line per trade) so
            # the structural-alpha fallback analysis has raw input
            # even if append_run_report is later rotated.
            tag_path = None
            if not args.no_write and tags:
                tag_path = (
                    Path('notebooks/nb54_vBTC3_ms_sweep_outputs')
                    / f'nb54_vBTC3_ms_sweep__trade_tags__{month}.jsonl'
                )
                with tag_path.open('a') as f:
                    for tg in tags:
                        rec = dict(tg)
                        rec['cell_label'] = label
                        rec['month'] = month
                        rec['overrides'] = ov
                        f.write(json.dumps(rec) + '\n')
            print(f"  [{month}] {label:18s} | n={n:3d} net=${sum_net:+8.2f} "
                  f"WR={wr:4.1f}% EV=${ev:+.4f} bt={t_bt:.1f}s "
                  f"tags={len(tags)}", flush=True)
            if not args.no_write:
                metrics_extra = {'n_structural_tags': len(tags)}
                append_run_report(
                    notebook=NOTEBOOK,
                    scenario=SCENARIO,
                    scope=[month],
                    engine="tick",
                    comments=(f"vBTC3 ms-param sweep on {month}: {label}. "
                              f"Overrides: {ov}. n_structural_tags={len(tags)}."),
                    hypothesis="Sparse MS-param sweep — does any knob "
                                "improve over canonical?",
                    verdict="inconclusive",
                    params=p_i,
                    result=res,
                    overrides_vs_canonical=ov,
                    canonical_recipe_version=OPTIMAL_RECIPE_VERSION,
                    metrics_extra=metrics_extra,
                    dedup_keys=DEDUP_KEYS,
                )

    print(f"\n=== {NOTEBOOK} done. wall={time.perf_counter() - grand_t0:.1f}s ===",
          flush=True)


if __name__ == "__main__":
    main()
