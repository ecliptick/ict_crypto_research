"""Pull 2026-09-24 aggTrades (the day before today) from Binance Futures.

The live engine has a 4h rolling buffer of 1s bars. At 00:47 UTC today
its buffer covers 20:47 yesterday → 00:47 today. We need yesterday's
evening aggTrades so we can run a backtest with the same warmup.
"""
from __future__ import annotations
import json
import sys
import urllib.request
import urllib.error
import time
from pathlib import Path

sys.path.insert(0, ".")

OUT_DIR = Path("notebooks/nb53_outputs")
OUT_DIR.mkdir(parents=True, exist_ok=True)
JSONL_PATH = OUT_DIR / "yesterday_aggtrades.jsonl"
PARQUET_PATH = OUT_DIR / "yesterday_aggtrades.parquet"

# ymd range
import datetime as dt
START_MS = int(dt.datetime(2026, 9, 24, 20, 47, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
END_MS = int(dt.datetime(2026, 9, 25, 8, 30, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)


def fetch_page(start_ms: int, end_ms: int, from_id: int | None) -> list[dict]:
    url = "https://fapi.binance.com/fapi/v1/aggTrades"
    params = []
    if from_id is not None:
        params.append(f"fromId={from_id}")
    else:
        params.append(f"startTime={start_ms}")
    params.append(f"endTime={end_ms}")
    params.append("limit=1000")
    full_url = url + "?" + "&".join(params)
    req = urllib.request.Request(full_url, headers={"User-Agent": "ict-crypto/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    print(f"Pulling aggTrades {START_MS} (UTC 2026-09-24 20:47:00) "
          f"→ {END_MS} (UTC 2026-09-25 08:30:00)")
    all_trades: list[dict] = []
    from_id: int | None = 3462032841  # approx agg_id at 2026-09-24 ~20:47 — we need to discover
    last_id = from_id
    n_calls = 0
    t0 = time.time()
    while True:
        try:
            trades = fetch_page(START_MS, END_MS, from_id=last_id)
        except urllib.error.HTTPError as e:
            print(f"HTTP {e.code} fromId={last_id}: {e.reason}")
            time.sleep(2)
            continue
        n_calls += 1
        if not trades:
            break
        last_id = trades[-1]["a"] + 1  # next agg_id to query
        all_trades.extend(trades)
        if len(trades) < 1000:
            break
        # Stop when we pass END_MS
        ts_last = trades[-1]["T"]
        if ts_last >= END_MS:
            break
        if n_calls % 25 == 0:
            print(f"  {n_calls} calls, {len(all_trades)} trades, "
                  f"last_id={last_id}, last_ts={ts_last}")
        time.sleep(0.05)

    print(f"Done — {len(all_trades)} trades in {n_calls} calls, "
          f"{time.time()-t0:.1f}s")
    if not all_trades:
        return
    first_ts = dt.datetime.fromtimestamp(all_trades[0]["T"]/1000, tz=dt.timezone.utc)
    last_ts = dt.datetime.fromtimestamp(all_trades[-1]["T"]/1000, tz=dt.timezone.utc)
    print(f"Range: {first_ts} → {last_ts}")

    # Write JSONL
    with open(JSONL_PATH, "w") as f:
        for t in all_trades:
            f.write(json.dumps(t) + "\n")
    print(f"→ {JSONL_PATH}")

    # Write parquet
    import pandas as pd
    df = pd.DataFrame(all_trades)
    # aggTrades schema: a (agg_trade_id), p (price), q (qty), f, l, T (ts_ms), m (is_buyer_maker)
    df = df.rename(columns={"a": "agg_trade_id", "p": "price",
                            "q": "quantity", "T": "ts_ms",
                            "m": "is_buyer_maker"})
    df["ts"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    df = df[["agg_trade_id", "price", "quantity", "ts", "is_buyer_maker"]]
    df.to_parquet(PARQUET_PATH)
    print(f"→ {PARQUET_PATH}")


if __name__ == "__main__":
    main()
