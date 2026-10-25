"""The opt-in roll blackout (engine setting roll_entry_block_minutes, broker
roll_block_until): for N minutes from the first tick on a new contract, an order that
would OPEN or ADD exposure is refused; reducing orders always go through.

Why it exists: after a roll the bars and indicators
still see the calendar spread as a move, so a channel breakout fires on the jump. Live
trading on the new contract's own history would not see it. The setting takes those
fake signals out so a strategy can be tested for dependence on them. Off by default.

Same fixture as test_roll_flatten: MNQ, an overnight session, file 20260615 (23:58:00-
23:59:59 at 100.00, bars 0 and 1) and file 20260616 (180 events from 00:00:00 at 112.00,
bars 2, 3, 4 ending 00:01:00, 00:02:00, 00:03:00). An order sent from bar k fills at the
first event of the next bar. Quotes a tick either side: ask 112.25, bid 111.75.
"""
from __future__ import annotations

import pytest

from backtester import manifest, metrics
from backtester.engine import Backtest
from backtester.orders import BUY, Order, OrderType
from backtester.strategy import Strategy

from conftest import MultiDayCatalog
from test_roll_flatten import Hold, calendar, file_at, run, two_files


class BuyWhenFlat(Strategy):
    """Tries to buy on every bar of the new file while flat."""
    symbol = "MNQ"
    period = "1m"
    session = ("18:00", "16:55")

    def on_bar(self, bar, bars):
        if bar.index >= 2 and self.flat:
            self.buy()


# ------------------------------------------------------------ the window --

def test_off_by_default_the_first_bar_after_the_roll_can_enter():
    # bar 2 closes 00:01:00 and sends a buy, which fills at event 60 of the new file
    # (ask 112.25); the end-of-day flatten sells at the bid 111.75: (111.75-112.25)*2 - 1.04
    _, res = run(BuyWhenFlat())
    t, = res.trades
    assert t.entry_ts == int(two_files()[1].ts[60]) and t.pnl == pytest.approx(-2.04)
    assert res.roll_entries_refused == 0 and res.roll_entry_block_minutes == 0


def test_a_two_minute_blackout_refuses_the_first_bar_and_lets_the_second_in():
    # the window is [00:00:00, 00:02:00). Bar 2 sends at 00:01:00: refused. Bar 3 sends at
    # 00:02:00, which is not before the end of the window: allowed, and it fills at event 120
    _, res = run(BuyWhenFlat(), roll_entry_block_minutes=2)
    t, = res.trades
    assert t.entry_ts == int(two_files()[1].ts[120]) and t.pnl == pytest.approx(-2.04)
    assert res.roll_entries_refused == 1 and res.roll_entry_block_minutes == 2


def test_the_end_of_the_window_is_exclusive():
    # one minute: the window ends at 00:01:00, exactly when bar 2 sends, so nothing is refused
    _, res = run(BuyWhenFlat(), roll_entry_block_minutes=1)
    assert res.roll_entries_refused == 0
    assert res.trades[0].entry_ts == int(two_files()[1].ts[60])


def test_a_long_blackout_can_swallow_every_entry_of_the_file():
    # three minutes: bars 2 and 3 are refused; bar 4 sends after the last event and never fills
    _, res = run(BuyWhenFlat(), roll_entry_block_minutes=3)
    assert res.trades == [] and res.roll_entries_refused == 2


def test_the_window_starts_at_the_first_tick_on_the_new_contract_not_at_midnight():
    # the new file starts at 00:30:00 (a quiet start). A midnight-based window would have
    # ended at 00:02 and refused nothing; from the first tick it is [00:30, 00:32): bar 2
    # (00:31:00) is refused, bar 3 (00:32:00) is let in
    files = [two_files()[0], file_at("20260616", "00:30", 180, 112.0)]
    _, res = run(BuyWhenFlat(), files=files, roll_entry_block_minutes=2)
    assert res.roll_entries_refused == 1
    assert res.trades[0].entry_ts == int(files[1].ts[120])


# ------------------------------------------------------ what it refuses --

def test_a_strategy_that_submits_orders_directly_is_blocked_too():
    class Direct(BuyWhenFlat):
        def on_bar(self, bar, bars):
            if bar.index == 2:
                self._broker.submit(Order(side=BUY, qty=1, type=OrderType.MARKET, tag="direct"))

    _, res = run(Direct(), roll_entry_block_minutes=2)
    assert res.trades == [] and res.roll_entries_refused == 1
    _, off = run(Direct())
    assert len(off.trades) == 1


def test_a_reducing_order_goes_through_during_the_blackout():
    # no roll flatten, so the long from the first file is carried across (and books the
    # 12-point jump); closing it during the blackout reduces exposure and is allowed:
    # sold at event 60 at the bid 111.75: (111.75 - 100.25) * 2 - 1.04 = +21.96
    class HoldThenClose(Hold):
        def on_bar(self, bar, bars):
            super().on_bar(bar, bars)
            if bar.index == 2:
                self.close_position()

    _, res = run(HoldThenClose(), flatten_at_roll=False, roll_entry_block_minutes=5)
    t, = res.trades
    assert t.exit_tag == "exit" and t.pnl == pytest.approx(21.96)
    assert t.exit_ts == int(two_files()[1].ts[60]) and res.roll_entries_refused == 0


def test_it_works_without_the_flatten_and_still_finds_the_roll():
    _, res = run(BuyWhenFlat(), flatten_at_roll=False, roll_entry_block_minutes=2)
    assert res.roll_entries_refused == 1
    assert res.rolls == [{"date": "20260616", "position": 0, "flattened": False}]


def test_a_symbol_without_a_calendar_has_no_blackout_and_says_so():
    _, res = run(BuyWhenFlat(), cal=calendar(symbol="YM"), roll_entry_block_minutes=2)
    assert res.roll_entries_refused == 0 and len(res.trades) == 1
    assert any("no roll calendar for MNQ" in n for n in res.roll_notes)


# ------------------------------------------------------- the setting --

def test_the_setting_is_resolved_as_arg_then_strategy_attribute_then_off():
    class Blocks(BuyWhenFlat):
        roll_entry_block_minutes = 30

    cal = calendar()
    assert Backtest(BuyWhenFlat(), roll_calendar=cal).roll_entry_block_minutes == 0
    assert Backtest(Blocks(), roll_calendar=cal).roll_entry_block_minutes == 30
    assert Backtest(Blocks(), roll_calendar=cal, roll_entry_block_minutes=0).roll_entry_block_minutes == 0
    assert Backtest(BuyWhenFlat(), roll_calendar=cal, roll_entry_block_minutes=2.5).roll_entry_block_minutes == 2.5
    with pytest.raises(ValueError, match="roll_entry_block_minutes"):
        Backtest(BuyWhenFlat(), roll_calendar=cal, roll_entry_block_minutes=-1)


def test_the_setting_can_be_swept_on_any_strategy():
    from backtester.loader import ENGINE_PARAMS
    assert "roll_entry_block_minutes" in ENGINE_PARAMS


# ---------------------------------------------------- what the run says --

def test_the_console_and_manifest_report_the_blackout_only_when_it_is_on():
    _, on = run(BuyWhenFlat(), roll_entry_block_minutes=2)
    text = metrics.format_console(on, metrics.compute(on))
    assert "Roll blackout:  no new entries for 2 min after each roll; 1 entry order(s) refused" in text
    h = manifest.headline(on, metrics.compute(on))
    assert h["roll_entry_block_minutes"] == 2 and h["roll_entries_refused"] == 1
    _, off = run(BuyWhenFlat())
    assert "Roll blackout" not in metrics.format_console(off, metrics.compute(off))
    h = manifest.headline(off, metrics.compute(off))
    assert "roll_entry_block_minutes" not in h and "roll_entries_refused" not in h


def test_the_manifest_config_changes_with_the_blackout():
    def begin(**kw):
        bt = Backtest(BuyWhenFlat(), roll_calendar=calendar(), prop=None, **kw)
        bt.catalog = MultiDayCatalog(two_files())
        return manifest.begin_run(bt, kind="cli")

    off, on, other = begin(), begin(roll_entry_block_minutes=2), begin(roll_entry_block_minutes=3)
    assert "roll_entry_block_minutes" not in off.config          # off keeps the sha it had
    assert on.config["roll_entry_block_minutes"] == 2
    assert len({off.config_sha, on.config_sha, other.config_sha}) == 3
