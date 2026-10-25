"""The engine flattens before a scheduled contract roll (engine.py, rolls.py).

Why: the day files switch contract at a midnight ET inside the Globex session, and
a position carried across it books the calendar spread as P&L (it can inflate a
headline by several percent).

Fixture, hand-computed: MNQ (tick 0.25, $2 a point, $0.52 a side), an overnight
session, two files. File 20260615 runs one event a second from 23:58:00 to 23:59:59
at 100.00 (two one-minute bars); file 20260616 runs 180 events from 00:00:00 at
112.00 (three bars). The contract changes at the midnight between them: a 12-point
jump. Quotes straddle each trade by a tick, so the bid is price - 0.25 and the ask
price + 0.25. A market order sent from bar 0 fills at event 60 (23:59:00).
"""
from __future__ import annotations

import numpy as np
import pytest

from backtester import manifest, metrics, rolls
from backtester.engine import Backtest
from backtester.rolls import RollCalendar
from backtester.strategy import Strategy

from conftest import NS, MultiDayCatalog, et_ns, make_day_at


def file_at(date, hhmm, n, price):
    ts = et_ns(date, hhmm) + np.arange(n, dtype="int64") * NS
    return make_day_at(np.full(n, float(price)), ts, date)


def two_files():
    return [file_at("20260615", "23:58", 120, 100.0),
            file_at("20260616", "00:00", 180, 112.0)]


def calendar(symbol="MNQ", dates=("20260616",)):
    return RollCalendar({"schema": 1, "excluded": {}, "symbols": {symbol: {
        "first": "20260101", "through": "20261231",
        "rolls": [{"date": d, "jump_ticks": 48.0, "jump_pct": 12.0} for d in dates],
        "unexplained": []}}})


class Hold(Strategy):
    symbol = "MNQ"
    period = "1m"
    session = ("18:00", "16:55")           # overnight: the evening file carries into the next
    side = 1

    def on_bar(self, bar, bars):
        if bar.index == 0:
            self.buy() if self.side > 0 else self.sell()


class Short(Hold):
    side = -1


def run(strategy, files=None, cal=calendar(), **kw):
    bt = Backtest(strategy, progress=False, prop=None, roll_calendar=cal, **kw)
    bt.catalog = MultiDayCatalog(files if files is not None else two_files())
    return bt, bt.run()


# ------------------------------------------------------------- the guard --

def test_a_long_held_across_a_roll_is_closed_at_the_last_quote_with_no_jump_pnl():
    # long from event 60 at the ask 100.25; the roll flatten sells at the bid of the
    # last event of the file (119): 99.75. (99.75 - 100.25) * 2 - 2 * 0.52 = -2.04
    bt, res = run(Hold())
    t, = res.trades
    assert (t.exit_tag, t.entry_price, t.exit_price) == ("roll", 100.25, 99.75)
    assert t.pnl == pytest.approx(-2.04)
    assert t.exit_ts == int(two_files()[0].ts[119])
    assert res.rolls == [{"date": "20260616", "position": 1, "flattened": True}]
    assert res.roll_notes == [] and bt.account.position == 0


def test_the_same_run_without_the_guard_books_the_calendar_spread():
    # carried into the 112.00 file and closed at its end (eod) at the bid 111.75:
    # (111.75 - 100.25) * 2 - 1.04 = +21.96. The guard run is -2.04: the 12-point
    # jump is exactly 12 * $2 = $24.00 of the difference
    _, res = run(Hold(), flatten_at_roll=False)
    t, = res.trades
    assert t.exit_tag == "eod" and t.exit_price == 111.75
    assert t.pnl == pytest.approx(21.96)
    assert res.rolls == [] and res.flatten_at_roll is False
    _, guarded = run(Hold())
    assert t.pnl - guarded.trades[0].pnl == pytest.approx(12 * 2.0)


def test_a_short_loses_the_jump_unguarded_and_is_closed_clean_when_guarded():
    # short from event 60 at the bid 99.75. Guarded: buy back at the ask of event
    # 119, 100.25: (99.75 - 100.25) * 2 - 1.04 = -2.04. Unguarded: buy back at the
    # last 112.00 event's ask 112.25: (99.75 - 112.25) * 2 - 1.04 = -26.04
    _, guarded = run(Short())
    _, open_ = run(Short(), flatten_at_roll=False)
    assert guarded.trades[0].pnl == pytest.approx(-2.04)
    assert open_.trades[0].pnl == pytest.approx(-26.04)
    assert guarded.trades[0].exit_tag == "roll"


def test_the_flatten_is_recorded_in_the_equity_series_and_the_daily_pnl():
    bt, res = run(Hold())
    # the evening file's trading day is named by the date it ENDS on, 20260616, but
    # the day is only flushed at the next file's session end: its P&L is the trade's
    assert sum(res.daily_pnl.values()) == pytest.approx(-2.04)
    assert bt.account.balance == pytest.approx(50_000 - 2.04)
    assert res.equity[1] == pytest.approx(50_000 - 2.04)         # the bar the flatten happened on


def test_a_roll_cancels_resting_orders_even_when_the_strategy_is_flat():
    class Rest(Strategy):
        symbol, period, session = "MNQ", "1m", ("18:00", "16:55")

        def on_start(self):
            self.working = []

        def on_bar(self, bar, bars):
            if bar.index == 0:
                self.buy_limit(90.0)                  # far below the market: never fills
            self.working.append(len(self._broker.working))

    s = Rest()
    _, res = run(s)
    # bars: 0 and 1 in the first file, 2..4 in the second. Guarded, the order is
    # gone from the first bar of the second file
    assert s.working == [1, 1, 0, 0, 0]
    assert res.rolls == [{"date": "20260616", "position": 0, "flattened": False}]
    s2 = Rest()
    run(s2, flatten_at_roll=False)
    assert s2.working[:3] == [1, 1, 1]               # carried across the roll


# --------------------------------------------- when it must NOT act --

def test_a_roll_date_that_is_not_between_two_of_the_files_does_nothing():
    for dates in (("20260617",),          # after the second file
                  ("20260615",)):         # on the first file: nothing precedes it
        _, res = run(Hold(), cal=calendar(dates=dates))
        assert res.rolls == []
        assert res.trades[0].exit_tag == "eod" and res.trades[0].pnl == pytest.approx(21.96)


def test_a_symbol_the_calendar_does_not_cover_runs_unguarded_and_says_so():
    _, res = run(Hold(), cal=calendar(symbol="YM"))
    assert res.rolls == [] and res.trades[0].exit_tag == "eod"
    assert len(res.roll_notes) == 1 and "no roll calendar for MNQ" in res.roll_notes[0]


def test_a_missing_calendar_file_is_loud_and_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(rolls, "CALENDAR_PATH", tmp_path / "absent.json")
    bt = Backtest(Hold(), progress=False, prop=None)         # no calendar passed: loads the file
    bt.catalog = MultiDayCatalog(two_files())
    res = bt.run()
    assert res.rolls == [] and res.trades[0].exit_tag == "eod"
    assert len(res.roll_notes) == 1 and "roll_calendar.json is missing" in res.roll_notes[0]


def test_a_run_past_the_end_of_the_calendar_is_flagged():
    short = RollCalendar({"schema": 1, "symbols": {"MNQ": {
        "first": "20260101", "through": "20260615",
        "rolls": [{"date": "20260616"}], "unexplained": []}}})
    _, res = run(Hold(), cal=short)
    assert any("ends 20260615" in n for n in res.roll_notes)
    assert res.rolls and res.rolls[0]["flattened"]            # it still acts on the rolls it has


def test_an_empty_file_between_the_last_old_file_and_the_roll_does_not_hide_it():
    # files 06-15 (100.00), an empty 06-16, 06-17 (112.00); the roll date is 06-17.
    # The roll lies between the last file that RAN (06-15) and the next one that
    # does (06-17), though not between 06-15 and the empty 06-16 file
    import dataclasses
    first = two_files()[0]
    empty = dataclasses.replace(
        first, date="20260616", ts=first.ts[:0], price=first.price[:0],
        volume=first.volume[:0], ask=first.ask[:0], bid=first.bid[:0],
        ask_size=first.ask_size[:0], bid_size=first.bid_size[:0], aggr=first.aggr[:0])
    _, res = run(Hold(), files=[first, empty, file_at("20260617", "00:00", 180, 112.0)],
                 cal=calendar(dates=("20260617",)))
    t, = res.trades
    assert t.exit_tag == "roll" and t.pnl == pytest.approx(-2.04)
    assert res.rolls == [{"date": "20260617", "position": 1, "flattened": True}]


def test_a_flat_strategy_records_the_roll_without_flattening_anything():
    class Idle(Strategy):
        symbol, period, session = "MNQ", "1m", ("18:00", "16:55")

    _, res = run(Idle())
    assert res.trades == []
    assert res.rolls == [{"date": "20260616", "position": 0, "flattened": False}]


# ----------------------------------------------------- the setting --

def test_flatten_at_roll_is_resolved_as_arg_then_strategy_attribute_then_on():
    class Off(Hold):
        flatten_at_roll = False

    cal = calendar()
    assert Backtest(Hold(), roll_calendar=cal).flatten_at_roll is True          # default
    assert Backtest(Off(), roll_calendar=cal).flatten_at_roll is False          # strategy attribute
    assert Backtest(Off(), roll_calendar=cal, flatten_at_roll=True).flatten_at_roll is True
    assert Backtest(Hold(), roll_calendar=cal, flatten_at_roll=False).flatten_at_roll is False


def test_flatten_at_roll_can_be_set_by_override_on_any_strategy():
    from backtester.loader import ENGINE_PARAMS
    assert "flatten_at_roll" in ENGINE_PARAMS


# ---------------------------------------------- what the run reports --

def test_the_console_summary_names_the_rolls_and_warns_when_the_guard_is_off():
    _, on = run(Hold())
    text = metrics.format_console(on, metrics.compute(on))
    assert "Rolls:          1 contract roll(s) in the run; flattened a position at 1: 20260616" in text
    _, off = run(Hold(), flatten_at_roll=False)
    assert "flatten-at-roll is OFF" in metrics.format_console(off, metrics.compute(off))

    class Idle(Strategy):
        symbol, period, session = "MNQ", "1m", ("18:00", "16:55")

    _, idle = run(Idle())                      # a roll crossed, nothing to flatten
    assert "1 contract roll(s) in the run; flattened a position at 0" in \
        metrics.format_console(idle, metrics.compute(idle))


def test_the_manifest_config_carries_the_rolls_the_run_can_see():
    def begin(cal, **kw):
        bt = Backtest(Hold(), roll_calendar=cal, prop=None, **kw)
        bt.catalog = MultiDayCatalog(two_files())             # no data root to fingerprint
        return manifest.begin_run(bt, kind="cli")

    cal = calendar()
    on, off = begin(cal), begin(cal, flatten_at_roll=False)
    assert on.config["roll_flatten"] == cal.window_sha("MNQ", None, None)
    assert "roll_flatten" not in off.config                    # off keeps the sha it always had
    assert on.config_sha != off.config_sha
    moved = begin(calendar(dates=("20260617",)))
    assert moved.config_sha != on.config_sha                   # a different schedule is a different run
    assert begin(calendar(symbol="YM")).config["roll_flatten"] == "no-calendar"


def test_the_manifest_results_record_the_rolls_flattened_and_the_warnings():
    _, on = run(Hold())
    h = manifest.headline(on, metrics.compute(on))
    assert h["rolls"] == {"in_run": 1, "flattened": ["20260616"]}
    assert "roll_notes" not in h and "flatten_at_roll" not in h
    _, off = run(Hold(), flatten_at_roll=False)
    h = manifest.headline(off, metrics.compute(off))
    assert h["flatten_at_roll"] is False and "rolls" not in h
    _, nocal = run(Hold(), cal=calendar(symbol="YM"))
    assert "no roll calendar for MNQ" in manifest.headline(nocal, metrics.compute(nocal))["roll_notes"][0]
