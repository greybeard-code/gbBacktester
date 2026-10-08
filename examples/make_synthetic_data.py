"""Generate fake L1 tick data in the layout the backtester reads.

Lets you run the whole pipeline (bars, fills, prop-firm tracker, Monte Carlo,
tearsheet) without owning any market data:

    python examples/make_synthetic_data.py --out demo_data
    export BACKTESTER_DATA_ROOT=demo_data BACKTESTER_CACHE=demo_cache
    python cli.py strategies/ema_cross.py --start 2026-06-01 --end 2026-06-30

The prices are a random walk, so there is no edge: results are meaningless.
This only shows that the machinery runs and what its output looks like.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

ET = ZoneInfo("America/New_York")
MDT_ASK, MDT_BID, MDT_LAST = 0, 1, 2


def make_day(day: date, rng: np.random.Generator, tick: float, base: float,
             trades_per_day: int) -> pa.Table:
    """One RTH session (09:30-16:00 ET) of trades with a 1-tick-wide book."""
    open_ns = int(datetime(day.year, day.month, day.day, 9, 30, tzinfo=ET)
                  .timestamp() * 1e9)
    close_ns = open_ns + int(6.5 * 3600 * 1e9)
    ts = np.sort(rng.integers(open_ns, close_ns, trades_per_day)).astype("int64")

    # Mid price in ticks: a random walk where ~30% of trades move it one tick.
    steps = rng.choice([-1, 0, 1], trades_per_day, p=[0.15, 0.70, 0.15])
    bid_ticks = np.round(base / tick) + np.cumsum(steps)
    bid = bid_ticks * tick
    ask = bid + tick
    at_ask = rng.random(trades_per_day) < 0.5
    price = np.where(at_ask, ask, bid)
    size = rng.integers(1, 6, trades_per_day).astype("int64")

    # Each trade is preceded by its prevailing ask and bid quote.
    n = trades_per_day
    out_ts = np.repeat(ts, 3)
    mdt = np.tile(np.array([MDT_ASK, MDT_BID, MDT_LAST], dtype="int8"), n)
    out_price = np.empty(3 * n)
    out_price[0::3], out_price[1::3], out_price[2::3] = ask, bid, price
    out_vol = np.empty(3 * n, dtype="int64")
    out_vol[0::3] = rng.integers(1, 40, n)
    out_vol[1::3] = rng.integers(1, 40, n)
    out_vol[2::3] = size
    return pa.table(
        {"Timestamp": out_ts, "MarketDataType": mdt,
         "Price": out_price, "Volume": out_vol},
        metadata={b"replay_importer.timestamps": b"UTC"})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True, help="data root to create")
    ap.add_argument("--symbol", default="MNQ")
    ap.add_argument("--start", default="2026-06-01", help="first day (YYYY-MM-DD)")
    ap.add_argument("--days", type=int, default=22, help="weekdays to generate")
    ap.add_argument("--tick", type=float, default=0.25)
    ap.add_argument("--base", type=float, default=20000.0, help="starting price")
    ap.add_argument("--trades-per-day", type=int, default=30000)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    day = date.fromisoformat(args.start)
    base, made = args.base, 0
    while made < args.days:
        if day.weekday() < 5:
            folder = (Path(args.out) / str(day.year)
                      / f"{args.symbol.upper()}-{day.year}_L1")
            folder.mkdir(parents=True, exist_ok=True)
            pq.write_table(
                make_day(day, rng, args.tick, base, args.trades_per_day),
                folder / f"{day:%Y%m%d}.parquet")
            made += 1
        day += timedelta(days=1)
    print(f"wrote {made} days of synthetic {args.symbol.upper()} to {args.out}")


if __name__ == "__main__":
    main()
