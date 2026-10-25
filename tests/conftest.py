import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from backtester.account import Account, PropFirmConfig, PropFirmTracker, TradeRecorder
from backtester.broker import SimBroker
from backtester.contracts import ContractSpec
from backtester.data import DayL1


@pytest.fixture(autouse=True)
def _isolate_ledger_and_manifests(tmp_path, monkeypatch):
    """No test may ever write to the real trial ledger (manifest.ledger_path()) or
    manifest folder: that ledger is the only record of how many variants were
    tried."""
    monkeypatch.setenv("BACKTESTER_LEDGER", str(tmp_path / "_ledger" / "trials.jsonl"))
    monkeypatch.setenv("BACKTESTER_MANIFEST_DIR", str(tmp_path / "_manifests"))


@pytest.fixture(autouse=True)
def _unit_tests_run_at_zero_latency(request, monkeypatch):
    """The engine's default order latency is 100 ms (engine.DEFAULT_LATENCY_MS, measured
    live in October 2026; tests/test_default_latency.py pins it). The unit tests here
    check engine mechanics against hand-computed INSTANT fills, so each runs with the
    default zeroed, which isolates what it checks. Tests that need the real default say
    so with @pytest.mark.real_default_latency; the real-data tests
    (@pytest.mark.data) always run at the real default."""
    if request.node.get_closest_marker("data") or \
            request.node.get_closest_marker("real_default_latency"):
        return
    from backtester import engine
    monkeypatch.setattr(engine, "DEFAULT_LATENCY_MS", 0.0)


@pytest.fixture
def spec():
    # simple math: tick 0.25, point value 2 (MNQ-like), $1.00 round turn
    return ContractSpec("TEST", 0.25, 2.0, 1.00)


ET = ZoneInfo("America/New_York")
NS = 1_000_000_000
DAY_NS = 86_400 * NS


def et_ns(date: str, hhmm: str = "00:00", seconds: int = 0) -> int:
    """ns UTC of a US/Eastern wall-clock time on a YYYYMMDD date. The engine
    derives session windows from the file's DATE STRING, so a fixture's
    timestamps must be built from the date it is labelled with."""
    d = datetime.strptime(date, "%Y%m%d")
    h, m = map(int, hhmm.split(":"))
    return int(datetime(d.year, d.month, d.day, h, m, seconds,
                        tzinfo=ET).timestamp()) * NS


def make_day_at(prices, ts, date, tick=0.25, ask=None, bid=None):
    """Synthetic DayL1 at explicit timestamps. Bid/ask straddle each trade by
    1 tick (aggressor 0) unless explicit ask/bid arrays are given."""
    from backtester.data import DayL1, classify_aggressor

    p = np.asarray(prices, dtype="float64")
    n = len(p)
    ask = np.asarray(ask, dtype="float64") if ask is not None else p + tick
    bid = np.asarray(bid, dtype="float64") if bid is not None else p - tick
    return DayL1(
        date=date,
        ts=np.asarray(ts, dtype="int64"),
        price=p,
        volume=np.ones(n, dtype="int64"),
        ask=ask,
        bid=bid,
        ask_size=np.ones(n, dtype="int64"),
        bid_size=np.ones(n, dtype="int64"),
        aggr=classify_aggressor(p, ask, bid),
    )


def make_day(prices, tick=0.25, start_ts=1_000_000_000_000,
             ask=None, bid=None):
    """Synthetic DayL1: one event per second from start_ts (default t = 1000 s,
    which is NOT on a minute boundary: bar 0 of a 1m series holds only 20
    events), labelled 20260101. Fine for tests with session=None; use
    synth_days / make_day_at when the date or the clock matters."""
    n = len(np.asarray(prices))
    return make_day_at(prices, start_ts + np.arange(n, dtype="int64") * NS,
                       "20260101", tick, ask, bid)


def _walk(rng, n, start=100.0):
    steps = rng.choice([-0.25, 0.0, 0.25], size=n, p=[0.4, 0.2, 0.4])
    return start + np.cumsum(steps)


def synth_days(n_days=5, n_events=1800, seed=11, first_date="20260202",
               start="09:30"):
    """n_days consecutive calendar days of a 1 event/s random walk on the
    0.25 tick grid, one file each, starting at `start` ET ON THE LABELLED
    DATE (so session=("09:30","16:00") sees them). Default 1800 events =
    30 one-minute bars per day, minute and 5-minute aligned."""
    rng = np.random.default_rng(seed)
    d0 = datetime.strptime(first_date, "%Y%m%d")
    out = []
    for i in range(n_days):
        date = (d0 + timedelta(days=i)).strftime("%Y%m%d")
        ts = et_ns(date, start) + np.arange(n_events, dtype="int64") * NS
        out.append(make_day_at(_walk(rng, n_events), ts, date))
    return out


def gap_day(date="20260203", n_each=600, gap_min=45, seed=3):
    """One file: n_each events from 09:30 ET, a gap_min-minute silence (more
    than the 30-minute renko/saber reset threshold), then n_each more."""
    rng = np.random.default_rng(seed)
    t0 = et_ns(date, "09:30")
    ts = np.concatenate([
        t0 + np.arange(n_each, dtype="int64") * NS,
        t0 + (n_each + gap_min * 60 + np.arange(n_each, dtype="int64")) * NS])
    return make_day_at(_walk(rng, 2 * n_each), ts, date)


def dst_days(seed=5):
    """Fri 2026-03-06 (EST) and Mon 2026-03-09 (EDT; US DST began Sun
    03-08), each 09:30 ET + 30 minutes: the same ET clock, one UTC hour
    apart."""
    return (synth_days(1, first_date="20260306", seed=seed)
            + synth_days(1, first_date="20260309", seed=seed + 1))


def overnight_days(seed=7):
    """A Globex-style overnight session split across two ET-calendar-day
    files. File A (20260202): 18:00-18:10 and 23:50-23:59:59 ET. File B
    (20260203): 00:00-00:10 ET. Trading straight through midnight."""
    rng = np.random.default_rng(seed)
    px = _walk(rng, 1800)
    sec = np.arange(600, dtype="int64") * NS
    a_ts = np.concatenate([et_ns("20260202", "18:00") + sec,
                           et_ns("20260202", "23:50") + sec])
    return [make_day_at(px[:1200], a_ts, "20260202"),
            make_day_at(px[1200:], et_ns("20260203", "00:00") + sec, "20260203")]


def nan_quote_day(date="20260202", n=300, nan_first=130, seed=9):
    """The first nan_first events carry no quote (NaN bid/ask, size 0,
    aggressor 0) - the state before the first quote of a file."""
    day = synth_days(1, n_events=n, first_date=date, seed=seed)[0]
    for arr in (day.ask, day.bid):
        arr[:nan_first] = np.nan
    day.ask_size[:nan_first] = 0
    day.bid_size[:nan_first] = 0
    day.aggr[:nan_first] = 0
    return day


def shared_ts_day(date="20260202", n=600, per=3, seed=13):
    """`per` consecutive events share each timestamp (same-ms prints)."""
    rng = np.random.default_rng(seed)
    ts = et_ns(date, "09:30") + (np.arange(n, dtype="int64") // per) * NS
    return make_day_at(_walk(rng, n), ts, date)


def write_synthetic_raw(path, seed=1, n_trades=4000, gap_after=2000, day="2025-01-15"):
    """A raw L1 Parquet day with real bid/ask rows: per second an ask row, a bid
    row, then a Last row (file order is what `prevailing()` uses). The trade
    walks +/- up to 3 ticks, the spread is 1-2 ticks each side, and a 40-minute
    silence after `gap_after` trades splits the stream (renko-family reset).
    Timestamps are tz-naive ET wall clock, like the legacy raw repo."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    tick = 0.25
    rng = np.random.default_rng(seed)
    px = 100.0 + np.cumsum(rng.integers(-3, 4, size=n_trades) * tick)
    a_ticks = rng.integers(1, 3, size=n_trades)
    b_ticks = rng.integers(1, 3, size=n_trades)
    sec = np.arange(n_trades, dtype="int64")
    sec[gap_after:] += 2400
    wall = (np.datetime64(f"{day}T10:00:00", "ns") + sec * np.timedelta64(1, "s"))
    price = np.empty(3 * n_trades)
    price[0::3] = px + a_ticks * tick
    price[1::3] = px - b_ticks * tick
    price[2::3] = px
    table = pa.table({
        "Timestamp": pa.array(np.repeat(wall, 3), pa.timestamp("ns")),
        "MarketDataType": pa.array(np.tile(np.array([0, 1, 2], dtype="int8"), n_trades)),
        "Price": pa.array(price),
        "Volume": pa.array(np.ones(3 * n_trades, dtype="int64")),
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)


class MultiDayCatalog:
    """Duck-typed Catalog over in-memory days. Builds whatever bar kind the spec
    names (time, tick, renko, saber, tbars, wave), each day on its own with no
    carry across days (fine for one day or for time/tick bars). Honours
    start/end in days() and a `days=` override in load_bars_sequence, which is
    how tools.causality_check feeds it truncated days."""

    def __init__(self, days):
        self._d = {d.date: d for d in days}

    def days(self, symbol, start=None, end=None):
        s = start.replace("-", "") if start else "00000000"
        e = end.replace("-", "") if end else "99999999"
        return [d for d in sorted(self._d) if s <= d <= e]

    def load_day(self, symbol, date):
        return self._d[date]

    def load_bars(self, symbol, date, spec, tick_size, day=None):
        from backtester.data import build_bars
        # `is not None`, not truthiness: DayL1 defines __len__, so an empty
        # (e.g. truncated-to-nothing) day is falsy and must NOT fall back to
        # the full day
        return build_bars(day if day is not None else self._d[date], spec,
                          tick_size)

    def load_bars_sequence(self, symbol, dates, spec, tick_size, days=None):
        from backtester.data import build_bars
        days = days if days is not None else [self._d[d] for d in dates]
        return [build_bars(d, spec, tick_size) for d in days]


@pytest.fixture
def rig(spec):
    """(broker, account, recorder, apex_factory) wired together."""
    def build(start_balance=50_000.0, apex_cfg=None, slippage=0.0):
        account = Account(spec, start_balance)
        recorder = TradeRecorder(spec)
        prop = PropFirmTracker(apex_cfg, start_balance) if apex_cfg else None
        broker = SimBroker(spec, account, recorder, prop, slippage)
        return broker, account, recorder, prop
    return build
