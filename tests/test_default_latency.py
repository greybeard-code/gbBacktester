"""The engine's DEFAULT order latency is 100 ms (user decision 2026-10-23, from the live
live measurement of 2026-10-22 (Apex / Tradovate): half the market-order round trip was
~108 ms at the open and ~58 ms in the slow evening hours).

Resolution order: explicit argument > the strategy's `latency_ms` attribute > 100.
An explicit 0 (argument or attribute) turns it off, the optimistic case.

MNQ, tick 0.25, one event per second from 09:30:00 ET, flat price. A 1m bar k holds
events 60k..60k+59, so an order sent in on_bar(0) is sent at event 60's timestamp, and
with 100 ms of latency it cannot fill before event 61 (the first event at or after
60.100 s). Every expected value is worked by hand.
"""
from __future__ import annotations

import numpy as np
import pytest

from backtester import engine, metrics
from backtester.engine import DEFAULT_LATENCY_MS, Backtest
from backtester.strategy import Strategy

from conftest import NS, MultiDayCatalog, et_ns, make_day_at

pytestmark = pytest.mark.real_default_latency


def day_at(n=400, price=100.0, date="20260202"):
    prices = np.full(n, price, dtype="float64")
    ts = et_ns(date, "09:30") + np.arange(n, dtype="int64") * NS
    return make_day_at(prices, ts, date)


class Base(Strategy):
    symbol = "MNQ"
    period = "1m"
    session = None
    qty = 1


class MarketOnce(Base):
    def on_bar(self, bar, bars):
        if bar.index == 0:
            self.buy()


class AttrOff(MarketOnce):
    latency_ms = 0


class Attr40(MarketOnce):
    latency_ms = 40


def make(strat, **kw):
    return Backtest(strat, progress=False, **{"prop": None, **kw})


def run(strat, day, **kw):
    bt = make(strat, **kw)
    bt.catalog = MultiDayCatalog([day])
    return bt.run()


# ------------------------------------------------------------------ the value

def test_the_default_constant_is_one_hundred_milliseconds():
    assert DEFAULT_LATENCY_MS == 100.0


def test_a_backtest_with_no_latency_setting_uses_the_default():
    bt = make(MarketOnce())
    assert bt.latency_ms == 100.0
    assert bt.broker.latency_ns == 100_000_000


def test_the_default_is_read_when_the_backtest_is_built_not_frozen_at_import(monkeypatch):
    monkeypatch.setattr(engine, "DEFAULT_LATENCY_MS", 37.0)
    assert make(MarketOnce()).latency_ms == 37.0


def test_an_explicit_zero_turns_it_off_by_argument_and_by_strategy_attribute():
    assert make(MarketOnce(), latency_ms=0.0).latency_ms == 0.0
    assert make(AttrOff()).latency_ms == 0.0           # 0 is a value, not "unset"


def test_argument_beats_attribute_beats_default():
    assert make(Attr40()).latency_ms == 40.0
    assert make(Attr40(), latency_ms=250.0).latency_ms == 250.0
    assert make(Attr40(), latency_ms=0.0).latency_ms == 0.0
    assert make(MarketOnce(), latency_ms=50.0).latency_ms == 50.0


def test_a_negative_latency_is_still_refused():
    with pytest.raises(ValueError):
        make(MarketOnce(), latency_ms=-1.0)


# ------------------------------------------------------------------ behavior

def test_a_default_run_fills_one_event_later_than_an_instant_run():
    day = day_at()
    default = run(MarketOnce(), day)
    off = run(MarketOnce(), day, latency_ms=0.0)
    assert off.trades[0].entry_ts == int(day.ts[60])          # sent at event 60, instant
    assert default.trades[0].entry_ts == int(day.ts[61])      # 60.000 s + 100 ms -> event 61
    assert default.latency_ms == 100.0 and off.latency_ms == 0.0


def test_a_default_run_is_bit_identical_to_an_explicit_100():
    day = day_at()
    a = run(MarketOnce(), day)
    b = run(MarketOnce(), day, latency_ms=100.0)
    assert [(t.entry_ts, t.exit_ts, t.pnl) for t in a.trades] == \
           [(t.entry_ts, t.exit_ts, t.pnl) for t in b.trades]
    assert a.latency_ms == b.latency_ms == 100.0


def test_slippage_still_applies_to_a_fill_that_waited_out_the_latency():
    # flat price 100.00, quotes one tick either side: ask 100.25. A market buy fills at the
    # ask plus the slippage ticks (0.25 each), on whichever event the latency lets it fill.
    day = day_at()
    t, = run(MarketOnce(), day).trades                                  # default 100 ms, no slippage
    assert t.entry_ts == int(day.ts[61]) and t.entry_price == 100.25
    t, = run(MarketOnce(), day, slippage_ticks=2.0).trades              # + 2 ticks = + 0.50
    assert t.entry_ts == int(day.ts[61]) and t.entry_price == 100.75
    t, = run(MarketOnce(), day, slippage_ticks=2.0, latency_ms=0.0).trades
    assert t.entry_ts == int(day.ts[60]) and t.entry_price == 100.75


# ------------------------------------------------------------------ what the user sees

def test_the_console_summary_states_the_latency_and_flags_the_off_case():
    day = day_at()
    on = metrics.format_console(*(lambda r: (r, metrics.compute(r)))(run(MarketOnce(), day)))
    off = metrics.format_console(*(lambda r: (r, metrics.compute(r)))(
        run(MarketOnce(), day, latency_ms=0.0)))
    assert "Order latency:  100 ms" in on
    assert "Order latency:  OFF (0 ms)" in off and "optimistic" in off
    assert "OFF" not in on and "100 ms" not in off


def test_the_cli_help_says_what_the_default_is():
    import cli
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with pytest.raises(SystemExit), redirect_stdout(buf):
        cli.main(["--help"])
    text = " ".join(buf.getvalue().split())
    assert "else 100" in text and "0 turns it off" in text
