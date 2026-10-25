"""Exit orders only reduce a position (broker._fill, _drop_stale_exits).

Order.is_exit says an exit "may flatten, never reverse"; since 2026-10-21 the broker
enforces it, as NT8 does for a position's managed stops and targets and Nautilus does
for reduce-only orders:

  * an exit order that triggers when it cannot reduce (flat, or on the position's own
    side) is cancelled, not filled;
  * an exit fill is clamped to the open position;
  * a fill that closes the position, or turns it, cancels the exit orders that can no
    longer reduce it, and keeps those that still can (legs submitted for the new side).

Hand-computed on MNQ (tick 0.25, $2/point, $0.52 a side), one event a second from
09:30, one-minute bars of 60 events, quotes a tick either side of the trade price
(ask = price + .25, bid = price - .25). An order sent from bar k fills at event
60 * (k + 1). All scenarios end flat through the end-of-day flatten.
"""
from __future__ import annotations

import numpy as np
import pytest

from backtester import manifest, metrics
from backtester.engine import Backtest
from backtester.orders import SELL, BUY, Order, OrderType
from backtester.strategy import Strategy

from conftest import NS, MultiDayCatalog, et_ns, make_day_at


def prices_of(*runs):
    return np.concatenate([np.full(n, float(p)) for n, p in runs])


class Script(Strategy):
    symbol = "MNQ"
    period = "1m"
    session = None
    qty = 1

    def __init__(self, script):
        self.script = script

    def on_bar(self, bar, bars):
        for act in self.script.get(bar.index, ()):
            act(self)


def exit_order(side, kind, price, qty=1, tag="prot"):
    return Order(side=side, qty=qty, type=kind, price=price, tag=tag, is_exit=True)


def run(script, prices):
    ts = et_ns("20260202", "09:30") + np.arange(len(prices), dtype="int64") * NS
    bt = Backtest(Script(script), progress=False, prop=None)
    bt.catalog = MultiDayCatalog([make_day_at(prices, ts, "20260202")])
    return bt, bt.run()


def pnls(res):
    return [round(t.pnl, 2) for t in res.trades]


# the long every scenario starts from: fills at event 60 (ask 100.25), stop 98.25, far target
def long_bracket(qty=1):
    return lambda s: s.buy_bracket(qty, stop_ticks=8, target_ticks=400)


# ------------------------------------------------ legs follow their position --

def test_a_reversal_drops_the_old_positions_legs():
    # bar 1: sell 2 fills at event 120 at the bid 99.75: closes the long (-1.00 -1.04
    # = -2.04) and opens a short 1 at 99.75. The long's SELL stop (98.25) and SELL
    # target now sit on the short's own side, so both are dropped. The price falls to
    # 95.00 at event 130 and nothing sells; the short is bought back at the end of the
    # day at the ask 95.25: (99.75 - 95.25) * 2 - 1.04 = +7.96
    bt, res = run({0: [long_bracket()], 1: [lambda s: s.sell(2)]},
                  prices_of((130, 100), (110, 95)))
    assert pnls(res) == [-2.04, 7.96]
    assert len(bt.broker.fills) == 3 and res.exits_dropped == 2
    assert bt.account.position == 0


def test_a_leg_for_the_new_side_survives_the_reversal():
    # the same reversal, with a BUY stop at 103.00 submitted beside it (protecting the
    # short). It is on the short's reducing side, so it stays. The price goes to 104 at
    # event 130, the stop fills at the ask 104.25: (99.75 - 104.25) * 2 - 1.04 = -10.04.
    # Had it been dropped with the old long's legs, the short would ride to 101 and be
    # bought back at 101.25 for -4.04
    def protect(s):
        s.sell(2)
        s._broker.submit(exit_order(BUY, OrderType.STOP, 103.0))

    bt, res = run({0: [long_bracket()], 1: [protect]},
                  prices_of((130, 100), (20, 104), (90, 101)))
    assert pnls(res) == [-2.04, -10.04]
    assert res.trades[1].exit_tag == "prot" and res.exits_dropped == 2


def test_a_hand_close_drops_the_legs_so_they_cannot_close_a_later_trade():
    # trade A is closed by close_position on bar 1 (event 120, bid 99.75: -2.04); its
    # legs are dropped. Trade B is a plain buy sent from bar 2 (fills event 180 at the
    # ask 100.25). The price falls to 95 at event 190: A's old stop at 98.25 used to sell
    # B's contract at 94.75; now B rides to the end of the day, sold at the bid 94.75
    # anyway, but tagged 'eod', and A's legs are gone
    bt, res = run({0: [long_bracket()], 1: [lambda s: s.close_position()],
                   2: [lambda s: s.buy()]}, prices_of((190, 100), (110, 95)))
    assert [(t.exit_tag, round(t.pnl, 2)) for t in res.trades] == [("exit", -2.04), ("eod", -12.04)]
    assert res.exits_dropped == 2


def test_a_scale_in_keeps_the_legs_that_still_reduce():
    # long 1 with a bracket; a second buy fills at event 120 (ask 100.25), position 2.
    # A buy that ADDS is not a closing fill: the SELL legs still reduce a long and stay.
    # At event 130 the 1-contract stop sells 1 at the bid 94.75 (position 1, so the
    # target is still useful and stays); the other contract goes at the end of the day,
    # also at 94.75. Nothing is dropped (the day-end flush cancels the target)
    bt, res = run({0: [long_bracket()], 1: [lambda s: s.buy()]}, prices_of((130, 100), (110, 95)))
    t, = res.trades
    assert t.exit_tag == "stop" and res.exits_dropped == 0
    assert [(f.side, f.qty, f.price) for f in bt.broker.fills] == [
        (BUY, 1, 100.25), (BUY, 1, 100.25), (SELL, 1, 94.75), (SELL, 1, 94.75)]


# ----------------------------------------------------------- the fill clamp --

def test_an_exit_larger_than_the_position_is_clamped_to_it():
    # long 2 from event 60 (ask 100.25) with a 2-contract stop at 98.25; the strategy
    # sells 1 by hand at event 120 (bid 99.75), position 1. At event 130 the stop
    # triggers for 2 but only 1 is open: it sells 1 at 94.75, not 2. Exits avg 97.25:
    # (97.25 - 100.25) * 2 contracts * $2 = -12.00, commission 4 sides * 0.52 = 2.08
    bt, res = run({0: [long_bracket(2)], 1: [lambda s: s.sell(1)]},
                  prices_of((130, 100), (110, 95)))
    t, = res.trades
    assert t.pnl == pytest.approx(-14.08)
    assert t.exit_tag == "short"          # the first exit was the hand sell; the stop was the second
    assert bt.account.position == 0 and len(bt.broker.fills) == 3
    assert [f.qty for f in bt.broker.fills] == [2, 1, 1]
    assert res.exits_dropped == 0        # the target went with the stop (OCO), not as a leftover


# --------------------------------------------- exits that cannot reduce --

def test_an_exit_order_that_triggers_while_flat_is_cancelled_not_filled():
    # a SELL stop at 98.00 is sent while flat. The price falls to 95.00: it used to open
    # a short at the bid 94.75. Now it is dropped, and the run has no trade at all
    bt, res = run({0: [lambda s: s._broker.submit(exit_order(SELL, OrderType.STOP, 98.0))]},
                  prices_of((130, 100), (110, 95)))
    assert res.trades == [] and bt.broker.fills == [] and res.exits_dropped == 1


def test_an_exit_on_the_side_the_position_opens_on_is_dropped_when_it_opens():
    # a BUY stop marked as an exit at 103.00 is sent while flat, with a BUY entry (long
    # from event 60, ask 100.25). Once long, a BUY exit could only ADD, so the entry's
    # fill drops it. The price reaches 104 at event 130 and nothing happens; the long
    # is sold at the end of the day at the bid 103.75: (103.75 - 100.25) * 2 - 1.04 = 5.96
    def long_with_wrong_stop(s):
        s._broker.submit(exit_order(BUY, OrderType.STOP, 103.0, tag="stray"))
        s.buy()

    bt, res = run({0: [long_with_wrong_stop]}, prices_of((130, 100), (110, 104)))
    assert pnls(res) == [5.96] and res.exits_dropped == 1
    assert len(bt.broker.fills) == 2


def test_an_exit_sent_before_its_entry_fills_is_kept_for_that_entry():
    # a short is entered at event 60 (bid 99.75) and a BUY stop at 103.00 is sent with
    # it, while still flat. Entering from flat drops nothing, so the stop protects the
    # short: price 104 at event 130, fills at the ask 104.25: (99.75 - 104.25) * 2 - 1.04
    def short_with_stop(s):
        s.sell()
        s._broker.submit(exit_order(BUY, OrderType.STOP, 103.0))

    bt, res = run({0: [short_with_stop]}, prices_of((130, 100), (110, 104)))
    t, = res.trades
    assert t.exit_tag == "prot" and t.pnl == pytest.approx(-10.04)
    assert res.exits_dropped == 0


def test_a_close_order_for_a_position_that_is_already_gone_does_nothing():
    # bar 0 enters with a stop at 98.25; bar 1 sends a closing SELL, but the stop has
    # already taken the position out (price 95 from event 75, stop sells at the bid
    # 94.75). The closing order, due at event 120, finds the position flat. It used to
    # sell 1 more and open a short; now it is cancelled
    bt, res = run({0: [long_bracket()], 1: [lambda s: s._broker.submit(
        exit_order(SELL, OrderType.MARKET, 0.0, tag="late-close"))]},
        prices_of((75, 100), (165, 95)))
    t, = res.trades
    assert t.exit_tag == "stop"
    assert bt.account.position == 0 and len(bt.broker.fills) == 2


# -------------------------------------------------------- what the run says --

def test_the_run_reports_how_many_legs_were_dropped():
    bt, res = run({0: [long_bracket()], 1: [lambda s: s.sell(2)]}, prices_of((130, 100), (110, 95)))
    text = metrics.format_console(res, metrics.compute(res))
    assert "Exit legs:      2 stop/target order(s) cancelled" in text
    assert manifest.headline(res, metrics.compute(res))["exits_dropped"] == 2
    _, clean = run({0: [long_bracket()]}, prices_of((130, 100), (110, 95)))
    assert clean.exits_dropped == 0                    # the stop filled and took its target (OCO)
    _, none = run({0: [lambda s: s.buy()]}, prices_of((130, 100), (110, 95)))
    assert none.exits_dropped == 0
    assert "Exit legs" not in metrics.format_console(none, metrics.compute(none))
    assert "exits_dropped" not in manifest.headline(none, metrics.compute(none))
