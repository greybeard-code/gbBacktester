"""Guards on the shared synthetic streams in conftest.

The engine derives session windows from a day file's DATE STRING
(engine._session_bounds_utc), so a fixture whose timestamps disagree with its
label gets zero bars in any session-bound test and passes or fails for the
wrong reason. These tests pin the fixtures so that can't happen silently.
All expected counts are hand-computed from the stream definitions.
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from backtester.engine import Backtest
from backtester.strategy import Strategy

from conftest import (DAY_NS, NS, MultiDayCatalog, dst_days, et_ns, gap_day,
                      nan_quote_day, overnight_days, shared_ts_day, synth_days)


class CountBars(Strategy):
    symbol = "MNQ"
    period = "1m"
    session = ("09:30", "16:00")
    qty = 1

    def on_start(self):
        self.n = 0

    def on_bar(self, bar, bars):
        self.n += 1


class HoldOvernight(Strategy):
    symbol = "MNQ"
    period = "1m"
    session = ("18:00", "16:55")       # Globex day; wraps past midnight
    qty = 1

    def on_start(self):
        self.n = 0

    def on_bar(self, bar, bars):
        self.n += 1
        if bar.index == 0:
            self.buy()


class BuyFirstBar(Strategy):
    symbol = "MNQ"
    period = "1m"
    session = None
    qty = 1

    def on_bar(self, bar, bars):
        if bar.index == 0:
            self.buy()


def run(strat, days):
    bt = Backtest(strat, prop=None, progress=False)
    bt.catalog = MultiDayCatalog(days)
    return bt.run()


def test_et_ns_is_the_wall_clock_in_both_dst_regimes():
    assert et_ns("20260202", "09:30") % DAY_NS == (14 * 3600 + 30 * 60) * NS  # EST
    jan = et_ns("20260306", "09:30") % DAY_NS
    jun = et_ns("20260609", "09:30") % DAY_NS
    assert jan == (14 * 3600 + 30 * 60) * NS and jun == (13 * 3600 + 30 * 60) * NS


def test_rth_session_sees_every_bar_on_the_labelled_date():
    days = synth_days(5)
    strat = CountBars()
    run(strat, days)
    # 1800 events at 1/s from 09:30:00 = 30 one-minute bars a day, all inside
    # 09:30-16:00 ET
    assert strat.n == 5 * 30


def test_timestamps_that_disagree_with_the_label_get_zero_bars():
    """The trap this fixture replaced: right label, wrong clock (the old
    synth_days sat in October 2025 under February 2026 labels)."""
    shifted = [replace(d, ts=d.ts - 120 * DAY_NS) for d in synth_days(3)]
    strat = CountBars()
    run(strat, shifted)
    assert strat.n == 0


def test_gap_day_has_one_gap_longer_than_the_renko_reset_threshold():
    day = gap_day()
    gaps = np.diff(day.ts)
    assert (gaps > 30 * 60 * NS).sum() == 1
    assert int(gaps.max()) == (45 * 60 + 1) * NS      # 45 min silence + the 1 s step
    strat = CountBars()
    run(strat, [day])
    # 600 events = 10 minute buckets either side; the silent buckets between
    # them hold no trades and emit no bars
    assert strat.n == 20


def test_dst_pair_is_one_utc_hour_apart_and_both_inside_the_session():
    fri, mon = dst_days()
    assert fri.ts[0] % DAY_NS == (14 * 3600 + 30 * 60) * NS     # EST
    assert mon.ts[0] % DAY_NS == (13 * 3600 + 30 * 60) * NS     # EDT
    strat = CountBars()
    run(strat, [fri, mon])
    assert strat.n == 2 * 30


def test_overnight_stream_carries_a_position_across_the_file_boundary():
    a, b = overnight_days()
    assert a.ts[-1] // DAY_NS == (b.ts[0] - NS) // DAY_NS      # ends at 23:59:59 ET
    strat = HoldOvernight()
    res = run(strat, [a, b])
    # file A: 10 bars at 18:01-18:10 and 10 at 23:51-24:00; file B: 10 bars at
    # 00:01-00:10
    assert strat.n == 30
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.entry_ts < int(b.ts[0]) <= t.exit_ts       # opened in A, closed in B
    assert t.exit_tag == "eod"                          # flattened at B's 16:55 flush


def test_nan_quote_fill_falls_back_to_the_trade_price():
    day = nan_quote_day()                  # first 130 events carry no quote
    assert np.isnan(day.ask[:130]).all() and not np.isnan(day.ask[130:]).any()
    assert (day.ask_size[:130] == 0).all() and (day.aggr[:130] == 0).all()
    res = run(BuyFirstBar(), [day])
    # the market buy is submitted on bar 0 (events 0-59) and fills on event 60,
    # which has no quote: it fills at that event's trade price, not an ask
    t = res.trades[0]
    assert t.entry_ts == int(day.ts[60])
    assert t.entry_price == pytest.approx(float(day.price[60]))


def test_shared_timestamp_stream_keeps_every_event_and_volume():
    day = shared_ts_day()                  # 600 events, 3 per second
    assert len(day) == 600 and len(np.unique(day.ts)) == 200
    strat = CountBars()
    run(strat, [day])
    # 200 distinct seconds: 60 + 60 + 60 + 20 -> 4 one-minute bars
    assert strat.n == 4
    from backtester.data import build_time_bars
    bars = build_time_bars(day, 60)
    assert int(bars.volume.sum()) == 600       # same-ms prints are not merged
    assert [int(v) for v in bars.volume] == [180, 180, 180, 60]


def test_catalog_does_not_replace_an_empty_day_with_the_full_one():
    """DayL1 has __len__, so a day truncated to nothing is falsy: `day or full`
    would silently hand back the whole day - the leak causality_check exists
    to catch."""
    from backtester.contracts import get_spec
    from backtester.strategy import parse_barspec
    full = synth_days(1)[0]
    empty = replace(full, **{f: getattr(full, f)[:0] for f in
                             ("ts", "price", "volume", "ask", "bid",
                              "ask_size", "bid_size", "aggr")})
    cat = MultiDayCatalog([full])
    spec, tick = parse_barspec("1m"), get_spec("MNQ").tick_size
    assert len(cat.load_bars("MNQ", full.date, spec, tick)) == 30
    assert len(cat.load_bars("MNQ", full.date, spec, tick, day=empty)) == 0
