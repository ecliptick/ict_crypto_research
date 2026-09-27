"""Fast replay harness for LiveBacktest — precompute zones/structure once,
then walk bars via the live engine's on_bar path. ~100x faster than
the naive approach which calls detect_fvg + detect_market_structure
on every bar (O(N^2) in the bar count).

Approach
========
1. Build OHLCV arrays and run ``detect_fvg`` + ``detect_market_structure``
   + ``compute_simple_atr`` ONCE on the full array.
2. Build LiveBacktest, populate its ring buffer with the trailing warmup.
3. Seed bar_loop._zones with cloned detected zones (keep inverted zones live).
4. Map each zone_id -> inversion_bar (from the one-shot detector).
5. Walk every bar: push into ring buffer, queue snipers when i == inversion_bar,
   walk sniper queue.

Returns a list of dicts, one per fired sniper.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import types
import importlib.util as iu
from pathlib import Path

import numpy as np
import pandas as pd

LIVE_ROOT = Path(r"C:\coding\ict_sniper_live").resolve()


def _load_live_engine():
    pkg = types.ModuleType("src_live")
    pkg.__path__ = [str(LIVE_ROOT / "src")]
    sys.modules["src_live"] = pkg

    def _load(modname, relpath, is_pkg=False):
        full = f"src_live.{modname}"
        fpath = LIVE_ROOT / "src" / relpath
        if is_pkg:
            mod = types.ModuleType(full)
            mod.__path__ = [str(fpath.parent)]
            sys.modules[full] = mod
            init_path = fpath / "__init__.py"
            if init_path.exists():
                spec = iu.spec_from_file_location(full, str(init_path))
                inner = iu.module_from_spec(spec)
                inner.__package__ = full
                inner.__name__ = full
                sys.modules[full] = inner
                spec.loader.exec_module(inner)
                return inner
            return mod
        else:
            spec = iu.spec_from_file_location(full, str(fpath))
            mod = iu.module_from_spec(spec)
            mod.__package__ = ".".join(full.split(".")[:-1])
            sys.modules[full] = mod
            spec.loader.exec_module(mod)
            return mod

    _load("core", "core", is_pkg=True)
    _load("live", "live", is_pkg=True)
    _load("core.ict_strategy", "core/ict_strategy.py")
    _load("core.ict_signals", "core/ict_signals.py")
    _load("core.market_structure", "core/market_structure.py")
    _load("core.optimal_config", "core/optimal_config.py")
    _load("live.state_store", "live/state_store.py")
    _load("live.live_backtest", "live/live_backtest.py")

    return {
        "LiveBacktest": sys.modules["src_live.live.live_backtest"].LiveBacktest,
        "InFlightBar": sys.modules["src_live.live.live_backtest"].InFlightBar,
        "SniperSpec": sys.modules["src_live.live.live_backtest"].SniperSpec,
        "StateStore": sys.modules["src_live.live.state_store"].StateStore,
        "optimal_params": sys.modules["src_live.core.optimal_config"].optimal_params,
        "detect_fvg": sys.modules["src_live.core.ict_signals"].detect_fvg,
        "compute_simple_atr": sys.modules["src_live.core.ict_signals"].compute_simple_atr,
        "detect_market_structure": sys.modules["src_live.core.market_structure"].detect_market_structure,
        "annotate_choch_plus": sys.modules["src_live.core.market_structure"].annotate_choch_plus,
        "FvgZone": sys.modules["src_live.core.ict_signals"].FvgZone,
    }


def fast_replay(
    bars: pd.DataFrame,
    *,
    params,
    warmup_bars: int = 14_400,
    recipe_name: str = "live_canonical",
) -> list[dict]:
    mods = _load_live_engine()
    LiveBacktest = mods["LiveBacktest"]
    InFlightBar = mods["InFlightBar"]
    SniperSpec = mods["SniperSpec"]
    StateStore = mods["StateStore"]
    detect_fvg = mods["detect_fvg"]
    compute_simple_atr = mods["compute_simple_atr"]
    detect_market_structure = mods["detect_market_structure"]
    annotate_choch_plus = mods["annotate_choch_plus"]
    FvgZone = mods["FvgZone"]

    # 1. Convert to numpy
    o = bars["open"].to_numpy(dtype=np.float64)
    h = bars["high"].to_numpy(dtype=np.float64)
    l = bars["low"].to_numpy(dtype=np.float64)
    c = bars["close"].to_numpy(dtype=np.float64)
    v = bars["volume"].to_numpy(dtype=np.float64)
    times_ms = bars["time"].astype("int64").floordiv(1_000_000).to_numpy(dtype=np.int64)
    times_ns = times_ms * 1_000_000

    # 2. One-shot detector pass
    print(f"  [fast-replay] detect_fvg + structure on {len(o)} bars...")
    t0 = time.time()
    atr_arr = compute_simple_atr(h, l, c, length=int(params.atr_len))
    structure = detect_market_structure(
        h, l, c,
        pivot_len=int(params.ms_pivot_len),
        liquidity_len=int(params.ms_liquidity_len),
        detect_order_blocks=bool(params.ms_draw_order_blocks),
        detect_liquidity=bool(params.ms_draw_liquidity_sweeps),
        resample_to_n_secs=int(getattr(params, "fvg_resample_secs", 60)),
    )
    zones = detect_fvg(
        open_=o, high=h, low=l, close=c,
        warmup=0,
        max_active_zones=10_000_000,
        resample_to_n_secs=int(params.fvg_resample_secs),
        fvg_min_zone_usd=float(getattr(params, "fvg_min_zone_usd", 0.0)),
        max_zone_age_bars=int(getattr(params, "fvg_max_age_bars", 0)),
        supersede_on_new=bool(params.fvg_supersede_on_new),
        invalidation_min_pierce_usd=float(params.fvg_invalidation_min_pierce_usd),
        invalidation_min_consecutive_bars=int(params.fvg_invalidation_min_consecutive_bars),
        require_retest_to_invert=bool(params.fvg_require_retest_to_invert),
        played_out_min_extension_usd=float(getattr(params, "played_out_min_extension_usd", 0.0)),
        times_utc_ns=times_ns,
        fvg_min_lifetime_secs=int(getattr(params, "fvg_min_lifetime_secs", 0)),
        structure_events_per_bar=structure.events,
        structure_invalidation_age_secs=int(getattr(params, "fvg_structure_invalidation_age_secs", 0)),
    )
    print(f"  [fast-replay] {len(zones)} zones in {time.time()-t0:.1f}s")

    # 3. Build LiveBacktest
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    state = StateStore(db)
    fired: list[dict] = []
    cur = {"bar_idx": -1, "bar_ts_ms": 0, "close": 0.0}

    async def on_sniper(spec: SniperSpec) -> None:
        fired.append({
            "sniper_id": int(spec.sniper_id),
            "direction": int(spec.direction),
            "entry_price_hint": float(spec.entry_price_hint),
            "stop_usd": float(spec.stop_usd),
            "target_usd": float(spec.target_usd),
            "zone_id": int(spec.zone_id),
            "triggered_by": str(spec.triggered_by),
            "quantity": float(spec.quantity),
            "bar_idx": int(cur["bar_idx"]),
            "bar_ts_ms": int(cur["bar_ts_ms"]),
            "bar_close": float(cur["close"]),
        })

    bar_loop = LiveBacktest(
        state=state,
        symbol="BTCUSDT",
        on_sniper_trigger=on_sniper,
        params=params,
        warmup_bars=warmup_bars,
        taker_fee_bps=0.0,
    )

    # Monkey-patch DB mutations (we use in-memory state only)
    state.update_sniper_status = lambda *a, **kw: None
    state.wipe_bos_choch_events = lambda: 0
    state._publish = lambda *a, **kw: None
    # Point load_pending_snipers at our in-memory _pending
    state.load_pending_snipers = lambda: [
        sp for sp_list in bar_loop._pending.values() for sp in sp_list
    ]

    # 4. Seed zones
    seeded_zones = []
    next_id = 1
    for z in zones:
        zc = FvgZone(
            trigger_bar=int(z.trigger_bar),
            direction=int(z.direction),
            zone_low=float(z.zone_low),
            zone_high=float(z.zone_high),
        )
        zc.zone_id = next_id
        zc.inverted = bool(z.inverted)
        zc.mitigated_bar = int(z.mitigated_bar)
        zc.mitigated_depth_pct = float(z.mitigated_depth_pct)
        zc.pierced_bar = int(z.pierced_bar)
        zc.inverted_bar = int(z.inverted_bar)
        zc.consumed_bar = int(z.consumed_bar)
        zc.expired_bar = int(z.expired_bar)
        zc.superseded_bar = int(z.superseded_bar)
        zc.played_out_bar = int(z.played_out_bar)
        zc.live = bool(z.live)
        # Keep inverted zones live so sniper queue can find them
        if zc.inverted:
            zc.live = True
        if hasattr(z, "n_touches"):
            zc.n_touches = z.n_touches
        seeded_zones.append(zc)
        bar_loop._zones_by_id[next_id] = zc
        next_id += 1
    bar_loop._zones = seeded_zones
    bar_loop._next_zone_id = next_id

    # Seed structure
    bar_loop._last_structure = structure
    annotate_choch_plus(structure, bar_loop._zones)
    bar_loop._atr_cache = atr_arr

    # Preload warmup
    if len(bars) > warmup_bars:
        warm = bars.iloc[-warmup_bars - 1:-1]
    else:
        warm = bars.iloc[:-1]
    pre = [
        {
            "bar_ts_ms": int(times_ms[j]),
            "open": float(o[j]),
            "high": float(h[j]),
            "low": float(l[j]),
            "close": float(c[j]),
            "volume": float(v[j]),
        }
        for j in range(len(bars) - len(warm) - 1, len(bars) - 1)
    ]
    bar_loop.preload_bars(pre)
    bar_loop._atr_cache = atr_arr

    # 5. Map zone_id -> trigger_bar (sniper queued at zone birth per live engine pattern)
    trigger_bar_for_zone: dict[int, int] = {}
    inversion_bar_for_zone: dict[int, int] = {}
    for z in seeded_zones:
        trigger_bar_for_zone[z.zone_id] = int(z.trigger_bar)
        if z.inverted and z.inverted_bar >= 0:
            inversion_bar_for_zone[z.zone_id] = int(z.inverted_bar)

    sniper_min_zone = float(getattr(params, "fvg_inv_trade_min_zone_usd", 0.0))
    print(f"  [fast-replay] {len(inversion_bar_for_zone)} zones already inverted; "
          f"will queue snipers at their trigger_bar")

    # Monkey-patch time.time_ns so the live engine's age cap checks the
    # WALK clock instead of the wall clock (otherwise snipers queued at
    # Sep 25 would expire immediately when "now" is the test run time).
    walk_now_ns_holder = {"v": int(times_ns[0])}
    import time as _time_mod
    _real_time_ns = _time_mod.time_ns
    def _patched_time_ns():
        return walk_now_ns_holder["v"]
    _time_mod.time_ns = _patched_time_ns
    # The live engine imports time at module top — patch BOTH the module
    # we already bound and the time module on the live engine's namespace.
    live_backtest_mod = sys.modules["src_live.live.live_backtest"]
    live_backtest_mod.time.time_ns = _patched_time_ns
    print(f"  [fast-replay] walking {len(bars)} bars...")
    t0 = time.time()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _sniper_queued: set[int] = set()

    for i in range(len(bars)):
        # Push bar into ring buffer
        idx = bar_loop._cursor
        bar_loop._times_ns[idx] = int(times_ns[i])
        bar_loop._open[idx] = float(o[i])
        bar_loop._high[idx] = float(h[i])
        bar_loop._low[idx] = float(l[i])
        bar_loop._close[idx] = float(c[i])
        bar_loop._volume[idx] = float(v[i])
        bar_loop._cursor = (bar_loop._cursor + 1) % bar_loop._warmup
        if bar_loop._n < bar_loop._warmup:
            bar_loop._n += 1

        # Queue snipers on the trigger_bar (zone birth)
        for z in seeded_zones:
            zid = z.zone_id
            if zid in _sniper_queued:
                continue
            trig_bar = trigger_bar_for_zone.get(zid)
            if trig_bar is not None and i == trig_bar:
                # Queue the sniper at zone birth (live engine pattern)
                zone_w = float(z.zone_high - z.zone_low)
                if zone_w >= sniper_min_zone:
                    if not (zid in bar_loop._pending and bar_loop._pending[zid]):
                        bar_loop._next_signal_id += 1
                        qty = float(bar_loop._compute_quantity(entry_hint=float(c[i])))
                        SRow = sys.modules["src_live.live.state_store"].SniperRow
                        row = SRow(
                            sniper_id=-1,
                            signal_id=bar_loop._next_signal_id,
                            submit_bar=i,
                            submit_time_ns=int(times_ns[i]),
                            direction=int(z.direction),
                            zone_id=zid,
                            triggered_by="fvg",
                            trigger_price=float(c[i]),
                            stop_usd=0.0,
                            target_usd=0.0,
                            lots=qty,
                            status="pending",
                        )
                        try:
                            sid = state.insert_sniper(row)
                            row.sniper_id = sid
                        except Exception:
                            row.sniper_id = bar_loop._next_signal_id
                        bar_loop._pending.setdefault(zid, []).append(row)
                _sniper_queued.add(zid)

        # Walk sniper queue
        walk_now_ns_holder["v"] = int(times_ns[i])  # advance walk clock
        cur["bar_idx"] = i
        cur["bar_ts_ms"] = int(times_ms[i])
        cur["close"] = float(c[i])
        bar = InFlightBar(
            bar_idx=i,
            bar_ts_ms=int(times_ms[i]),
            open=float(o[i]),
            high=float(h[i]),
            low=float(l[i]),
            close=float(c[i]),
            volume=float(v[i]),
            is_carry_forward=False,
        )
        loop.run_until_complete(bar_loop._walk_pending_snipers(bar, atr_arr))

        if i % 10000 == 0 and i > 0:
            print(f"  [fast-replay] bar {i}/{len(bars)} ({time.time()-t0:.1f}s, {len(fired)} snipers)")

    state._conn.close()
    try:
        Path(db).unlink()
    except OSError:
        pass
    print(f"  [fast-replay] done: {len(fired)} snipers in {time.time()-t0:.1f}s")
    return fired


if __name__ == "__main__":
    bars = pd.read_parquet(r"C:\coding\ict_crypto_research\notebooks\nb53_outputs\BTCUSDT-1s-bars-2026-09-25.parquet")
    bars_6h = bars.iloc[:6 * 3600].reset_index(drop=True)
    print(f"Testing 6h: {len(bars_6h)} bars")
    mods = _load_live_engine()
    p = mods["optimal_params"]()
    t0 = time.time()
    fired = fast_replay(bars_6h, params=p, warmup_bars=14400, recipe_name="smoke")
    print(f"\n{len(fired)} snipers in {time.time()-t0:.1f}s")
    for f in fired[:10]:
        print(f"  bar_idx={f['bar_idx']} dir={f['direction']:+d} px={f['entry_price_hint']:.2f} "
              f"zone={f['zone_id']} stop={f['stop_usd']:.2f} target={f['target_usd']:.2f}")
