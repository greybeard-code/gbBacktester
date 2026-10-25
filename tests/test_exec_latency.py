"""Execution latency beyond the order latency: strategy cancels, strategy modifies, and
the delay between an entry fill and its protective stop/target going live (user
decision 2026-10-24, from the live measurement of 2026-10-22).

All three are OPT-IN (default: cancels and modifies instant, bracket = the order
latency, the engine's original behavior):

- `cancel_latency_ms`: a strategy's cancel reaches the exchange this long after it is
  sent; until then the order is live and can fill. One way = half a measured round trip.
- `modify_latency_ms`: same for a price change; the OLD price stays live until then.
- `bracket_latency_ms`: a stop/target leg made at an entry fill (bracket child or ATM leg)
  goes live this long after the FILL. A full round trip: the fill notice has to reach the
  strategy before the stop can be sent.

A request takes effect from the first event whose timestamp is at or after its arrival
(an arrival exactly ON an event's timestamp beats that event, the same inclusive rule an
order's own arrival follows), never before the order it names has arrived, and the
engine's own cancels (session end, roll flatten, kill switch, gate, OCO sibling) are never
delayed.

MNQ, tick 0.25, $2/point, $0.52 a side, one event per second from 09:30:00, quotes one tick
either side of the trade price (ask = price + .25, bid = price - .25). A 1m bar k holds
events 60k..60k+59: an order or request sent in on_bar(k) is sent at event 60(k+1)'s
timestamp. Every expected value is worked by hand.
"""
from __future__ import annotations

import numpy as np
import pytest

from backtester import metrics
from backtester.account import Account, TradeRecorder
from backtester.atm import submit_atm_exits
from backtester.broker import SimBroker
from backtester.engine import Backtest
from backtester.loader import ENGINE_PARAMS
from backtester.nt8config import AtmBracket, AtmSpec
from backtester.orders import BUY, Order, OrderType
from backtester.strategy import Strategy

from conftest import NS, MultiDayCatalog, et_ns, make_day, make_day_at


def day_at(prices, date="20260202"):
    prices = np.asarray(prices, dtype="float64")
    ts = et_ns(date, "09:30") + np.arange(len(prices), dtype="int64") * NS
    return make_day_at(prices, ts, date)


def run(strat, day, **kw):
    bt = Backtest(strat, progress=False, **{"prop": None, **kw})
    bt.catalog = MultiDayCatalog([day])
    res = bt.run()
    return bt, res


class Base(Strategy):
    symbol = "MNQ"
    period = "1m"
    session = None
    qty = 1


# ------------------------------------------------------------------ cancel

class RestThenCancel(Base):
    """A resting buy limit at 99.00 from event 60, cancelled from on_bar(1), event 120."""

    def on_bar(self, bar, bars):
        if bar.index == 0:
            self.buy_limit(99.0)
        elif bar.index == 1:
            self.cancel_all()


class RestThenCancelTicks(RestThenCancel):
    def on_tick(self, ts, price, index):         # forces the per-tick resolver
        pass


# flat 100.00 through event 120; from event 121 the market trades at 98.50, THROUGH the
# 99.00 limit: a resting order fills on event 121 at the quote (ask 98.75, marketable)
DIP_AT_121 = np.concatenate([np.full(121, 100.0), np.full(279, 98.5)])


@pytest.mark.parametrize("cls", [RestThenCancel, RestThenCancelTicks])
def test_a_cancel_leaves_the_order_live_until_it_reaches_the_exchange(cls):
    day = day_at(DIP_AT_121)
    # instant: gone at event 120, the dip finds nothing
    assert run(cls(), day)[1].trades == []
    # 500 ms: lands between events 120 and 121; 1,000 ms: lands exactly ON event 121's
    # timestamp, which it beats. Either way the order is gone before the dip
    assert run(cls(), day, cancel_latency_ms=500)[1].trades == []
    assert run(cls(), day, cancel_latency_ms=1000)[1].trades == []
    # 1,001 ms: lands before event 122, so the order is still live ON event 121 and
    # fills there at the ask 98.75
    t, = run(cls(), day, cancel_latency_ms=1001)[1].trades
    assert t.entry_ts == int(day.ts[121]) and t.entry_price == 98.75


# ------------------------------------------------------------------ modify

class RestThenModify(Base):
    """A resting buy limit at 99.00 from event 60, moved to 98.00 from on_bar(1)."""

    def on_bar(self, bar, bars):
        if bar.index == 0:
            self.order = self.buy_limit(99.0)
        elif bar.index == 1:
            assert self._broker.modify(self.order, 98.0)


class RestThenModifyTicks(RestThenModify):
    def on_tick(self, ts, price, index):
        pass


# 100.00 through event 120; 98.50 on events 121-124 (through 99.00, not through 98.00);
# 97.50 from event 125 (through 98.00)
STEP_DOWN = np.concatenate([np.full(121, 100.0), np.full(4, 98.5), np.full(275, 97.5)])


@pytest.mark.parametrize("cls", [RestThenModify, RestThenModifyTicks])
def test_a_modify_leaves_the_old_price_live_until_it_reaches_the_exchange(cls):
    day = day_at(STEP_DOWN)
    # instant, 500 ms, and exactly ON event 121: the limit is already 98.00 when the market
    # trades 98.50, which does not go through it; it fills on event 125 at the ask 97.75
    for ms in (0, 500, 1000):
        t, = run(cls(), day, modify_latency_ms=ms)[1].trades
        assert t.entry_ts == int(day.ts[125]) and t.entry_price == 97.75, ms
    # 1,001 ms: the change lands before event 122, so 99.00 is still the live price on
    # event 121 and the order fills there at the ask 98.75
    strat = cls()
    t, = run(strat, day, modify_latency_ms=1001)[1].trades
    assert t.entry_ts == int(day.ts[121]) and t.entry_price == 98.75
    # ... and the change that lands afterwards finds a filled order and leaves it alone
    assert strat.order.price == 99.0


def test_the_live_price_stays_the_old_one_until_the_modify_arrives():
    # flat 100.00 all day: the 99.00 limit rests, so its price can be watched
    day = day_at(np.full(200, 100.0))
    bt = Backtest(RestThenModify(), progress=False, prop=None, modify_latency_ms=5000.0)
    broker = bt.broker
    broker.begin_day(day)
    broker.now_ts = int(day.ts[60])
    o = broker.submit(Order(side=BUY, qty=1, type=OrderType.LIMIT, price=99.0))
    broker.now_ts = int(day.ts[120])
    assert broker.modify(o, 98.0) is True            # accepted, on its way
    assert o.price == 99.0 and len(broker._requests) == 1
    broker.resolve_span(120, 125)                    # events 120-124: it has not landed (125)
    assert o.price == 99.0 and len(broker._requests) == 1
    broker.resolve_span(125, 130)                    # it lands on event 125
    assert o.price == 98.0 and broker._requests == []


# ------------------------------------------------------------------ bracket

class BracketOnce(Base):
    def on_bar(self, bar, bars):
        if bar.index == 0:
            self.buy_bracket(stop_ticks=4)


class BracketOnceTicks(BracketOnce):
    def on_tick(self, ts, price, index):
        pass


def _crash():
    # flat 100 through event 63 (entry ask 100.25), a dip to 96 on events 64-65, then 92:
    # a 4-tick stop at 99.25 is hit at event 64 if it is live then
    return day_at(np.concatenate([np.full(64, 100.0), np.full(2, 96.0), np.full(114, 92.0)]))


@pytest.mark.parametrize("cls", [BracketOnce, BracketOnceTicks])
def test_the_stop_goes_live_bracket_latency_after_the_fill(cls):
    day = _crash()
    # default: entry on event 60 at the ask 100.25, stop live at once, hit on event 64 (price 96)
    # and filled at the bid 95.75: pnl (95.75 - 100.25) * 2 - 1.04 = -10.04
    t, = run(cls(), day)[1].trades
    assert t.exit_tag == "stop" and t.exit_ts == int(day.ts[64])
    assert t.exit_price == 95.75 and t.pnl == pytest.approx(-10.04)
    # 6,000 ms after the fill (event 60): live from event 66 (price 92), so the dip on 64-65
    # does not stop it; filled at the bid 91.75: pnl (91.75 - 100.25) * 2 - 1.04 = -18.04
    t, = run(cls(), day, bracket_latency_ms=6000)[1].trades
    assert t.entry_ts == int(day.ts[60])                       # the ENTRY was not delayed
    assert t.exit_ts == int(day.ts[66]) and t.exit_price == 91.75
    assert t.pnl == pytest.approx(-18.04)


class TargetOnce(Base):
    def on_bar(self, bar, bars):
        if bar.index == 0:
            self.buy_bracket(target_ticks=4)


def test_the_target_leg_goes_live_bracket_latency_after_the_fill_too():
    # entry on event 60 at the ask 100.25; the 4-tick target sits at 101.25. Price 102 on
    # event 62 only (through the target), 100 otherwise.
    day = day_at(np.concatenate([np.full(62, 100.0), np.full(1, 102.0), np.full(237, 100.0)]))
    # live at once: filled on event 62 at the bid 101.75
    t, = run(TargetOnce(), day)[1].trades
    assert t.exit_tag == "target" and t.exit_ts == int(day.ts[62]) and t.exit_price == 101.75
    # 6,000 ms: the leg is not live until event 66, when the price is back at 100: it never
    # fills, and the trade is closed by the end of the data instead
    t, = run(TargetOnce(), day, bracket_latency_ms=6000)[1].trades
    assert t.exit_tag != "target" and t.exit_ts > int(day.ts[66])


def test_bracket_latency_is_independent_of_the_order_latency():
    day = _crash()
    # the original model: the leg follows the order latency. 2,500 ms: entry on event 63,
    # stop live from event 66 (exit 91.75, pnl -18.04)
    t, = run(BracketOnce(), day, latency_ms=2500)[1].trades
    assert t.entry_ts == int(day.ts[63]) and t.exit_ts == int(day.ts[66])
    # an explicit bracket latency of 0 makes the leg live AT the fill (event 63), so the
    # dip on event 64 stops it at the bid 95.75: pnl (95.75 - 100.25) * 2 - 1.04 = -10.04
    t, = run(BracketOnce(), day, latency_ms=2500, bracket_latency_ms=0)[1].trades
    assert t.entry_ts == int(day.ts[63])
    assert t.exit_ts == int(day.ts[64]) and t.exit_price == 95.75
    assert t.pnl == pytest.approx(-10.04)


def test_an_atm_leg_is_a_bracket_leg_too(spec):
    # the dynamic-stop case from test_latency: entry ask 100.25 on event 0, 8-tick stop at
    # 98.25, prices 100 on events 0-3, 97 on 4-5 (a live stop fills on event 4), 96 from 6
    prices = [100.0] * 4 + [97.0] * 2 + [96.0] * 4
    atm = AtmSpec(name="t", entry_qty=1, brackets=(
        AtmBracket(qty=1, stop_ticks=8, target_ticks=0,
                   be_trigger_ticks=40, be_plus_ticks=2),))

    def go(**kw):
        day = make_day(prices)
        broker = SimBroker(spec, Account(spec, 50_000.0), TradeRecorder(spec), None, 0.0, **kw)
        broker.begin_day(day)
        broker.now_ts = int(day.ts[0])
        broker.submit(Order(side=BUY, qty=1, type=OrderType.MARKET, tag="entry"))
        broker.resolve_span(0, 4)
        submit_atm_exits(broker, atm, BUY, broker.fills[0].price)
        broker.resolve_span(4, len(day))
        return broker, day

    off, day = go()
    assert off.fills[1].ts == int(day.ts[4]) and off.fills[1].price == 96.75
    # 5,500 ms after the fill on event 0: the legs are live from event 6 (price 96), bid 95.75
    on, day = go(bracket_latency_ms=5500.0)
    assert on.fills[0].ts == int(day.ts[0])                    # the entry is not delayed
    assert on.fills[1].ts == int(day.ts[6]) and on.fills[1].price == 95.75


# ------------------------------------------------------------------ send time on a time bar

@pytest.mark.parametrize("cls,kw", [(RestThenCancel, {"cancel_latency_ms": 1000}),
                                    (RestThenModify, {"modify_latency_ms": 1000})])
def test_a_cancel_or_modify_is_sent_when_the_next_bars_first_tick_prints(cls, kw):
    # Order latency 0, only the request is delayed. A 1m bar is stamped with its clock
    # boundary (09:32:00 for bar 1) but its successor's first tick, event 120, prints at
    # 09:32:02. The strategy cannot have acted before 09:32:02, so a 1,000 ms request is
    # sent at 09:32:02 and lands at 09:32:03: the 99.00 limit is still live (at 99.00) on
    # event 120, which trades at 98.50, and fills there at the ask 98.75. Stamping the
    # boundary instead would land the request at 09:32:01, before that tick.
    prices = np.concatenate([np.full(120, 100.0), np.full(280, 98.5)])
    ts = et_ns("20260202", "09:30") + np.arange(400, dtype="int64") * NS
    ts[120:] += 2 * NS
    day = make_day_at(prices, ts, "20260202")
    t, = run(cls(), day, latency_ms=0, **kw)[1].trades
    assert t.entry_ts == int(day.ts[120]) and t.entry_price == 98.75
    # nothing delayed: the request is instant at the bar close and the order never fills
    zero = {k: 0 for k in kw}
    assert run(cls(), day, latency_ms=0, **zero)[1].trades == []


# ------------------------------------------------------------------ what is never delayed

class RestOnly(Base):
    def on_bar(self, bar, bars):
        if bar.index == 0:
            self.buy_limit(99.0)


def test_the_engine_cancels_at_the_end_of_the_run_are_instant():
    day = day_at(np.full(300, 100.0))                          # the 99.00 limit never fills
    # the strategy never cancels, so only the engine's own end-of-day cancel can remove the
    # resting order; it must not be queued behind a 5 s cancel latency and leave it working
    bt, res = run(RestOnly(), day, cancel_latency_ms=5000.0)
    assert res.trades == []
    assert bt.broker.working == [] and bt.broker._requests == []


class BracketBothLegs(Base):
    def on_bar(self, bar, bars):
        if bar.index == 0:
            self.buy_bracket(stop_ticks=4, target_ticks=4)


def test_the_oco_sibling_is_cancelled_at_once_even_with_a_cancel_latency():
    # entry on event 60 at the ask 100.25; stop 99.25, target 101.25. Price 102 on event 62
    # trades through the target (filled at the bid 101.75), then crashes to 90.
    day = day_at(np.concatenate([np.full(62, 100.0), np.full(1, 102.0), np.full(137, 90.0)]))
    _, res = run(BracketBothLegs(), day, cancel_latency_ms=5000.0)
    t, = res.trades
    assert t.exit_tag == "target" and t.exit_ts == int(day.ts[62]) and t.exit_price == 101.75
    # the stop was cancelled by the OCO rule at the target fill; had that cancel been
    # delayed the reduce-only rule would have had to drop it (counted here)
    assert res.exits_dropped == 0


class ReverseWithOldStop(Base):
    """Long with a 4-tick stop; at bar 1's close cancel everything and close at market (the
    stop-and-reverse pattern)."""

    def on_bar(self, bar, bars):
        if bar.index == 0:
            self.buy_bracket(stop_ticks=4)
        elif bar.index == 1:
            self.cancel_all()
            self.close_position(tag="reverse")


def test_a_stop_left_live_by_a_slow_cancel_can_be_hit_before_the_exit_lands():
    # order latency 3,000 ms: entry on event 63 at the ask 100.25 (stop at 99.25, live from
    # event 66). At event 120 the strategy cancels and sends the close; both land on event
    # 123. Price: 100 through event 121, 96 on events 122-124, then 92.
    day = day_at(np.concatenate([np.full(122, 100.0), np.full(3, 96.0), np.full(275, 92.0)]))
    # cancel instant: the stop is gone at event 120; the close fills on event 123 at the
    # bid 95.75
    t, = run(ReverseWithOldStop(), day, latency_ms=3000)[1].trades
    assert t.exit_tag == "reverse" and t.exit_ts == int(day.ts[123]) and t.exit_price == 95.75
    # cancel 3,000 ms: it has not landed when event 122 trades through the stop, so the
    # stop fills there (bid 95.75) and the close, landing on flat, is dropped by the
    # reduce-only rule
    bt, res = run(ReverseWithOldStop(), day, latency_ms=3000, cancel_latency_ms=3000)
    t, = res.trades
    assert t.exit_tag == "stop" and t.exit_ts == int(day.ts[122]) and t.exit_price == 95.75
    assert res.exits_dropped == 1


# ------------------------------------------------------------------ a request cannot overtake its order

class CancelRightAfterSubmit(Base):
    """The limit goes out from on_bar(0) (event 60); on_tick sends the cancel after event 61."""

    def on_bar(self, bar, bars):
        if bar.index == 0:
            self.buy_limit(99.0)

    def on_tick(self, ts, price, index):
        if index == 61:
            self.cancel_all()


def test_a_cancel_cannot_arrive_before_the_order_it_names():
    # price 100 through event 64, 98.50 from event 65. Order latency 5,000 ms: the limit
    # arrives at event 65's timestamp. The cancel is sent at event 61 and would land at
    # 61.1 s on its own, but the order is not there yet: it can only land after the order
    # does. So the order is live on event 65, and fills there at the ask 98.75.
    day = day_at(np.concatenate([np.full(65, 100.0), np.full(335, 98.5)]))
    t, = run(CancelRightAfterSubmit(), day, latency_ms=5000, cancel_latency_ms=100)[1].trades
    assert t.entry_ts == int(day.ts[65]) and t.entry_price == 98.75
    # with nothing delayed the cancel at event 61 beats the order and nothing fills
    assert run(CancelRightAfterSubmit(), day)[1].trades == []


# ------------------------------------------------------------------ the settings themselves

class Plain(Base):
    def on_bar(self, bar, bars):
        pass


def test_the_defaults_are_off_and_change_nothing():
    bt = Backtest(Plain(), progress=False, prop=None)
    assert bt.cancel_latency_ms == 0.0 and bt.modify_latency_ms == 0.0
    assert bt.bracket_latency_ms is None
    assert bt.broker.cancel_ns == 0 and bt.broker.modify_ns == 0 and bt.broker.bracket_ns is None
    day = _crash()
    a = run(BracketOnce(), day)[1].trades
    b = run(BracketOnce(), day, cancel_latency_ms=0, modify_latency_ms=0)[1].trades
    assert [(t.entry_ts, t.exit_ts, t.pnl) for t in a] == [(t.entry_ts, t.exit_ts, t.pnl) for t in b]
    # an explicit bracket latency equal to the order latency is the same as none
    c = run(BracketOnce(), day, latency_ms=2500)[1].trades
    d = run(BracketOnce(), day, latency_ms=2500, bracket_latency_ms=2500)[1].trades
    assert [(t.entry_ts, t.exit_ts, t.pnl) for t in c] == [(t.entry_ts, t.exit_ts, t.pnl) for t in d]


def test_argument_beats_strategy_attribute_beats_default():
    class Attrs(Plain):
        cancel_latency_ms = 65
        modify_latency_ms = 25
        bracket_latency_ms = 0                   # 0 is a value (live at the fill), not "unset"

    bt = Backtest(Attrs(), progress=False, prop=None)
    assert (bt.cancel_latency_ms, bt.modify_latency_ms, bt.bracket_latency_ms) == (65.0, 25.0, 0.0)
    bt = Backtest(Attrs(), progress=False, prop=None, cancel_latency_ms=40,
                  modify_latency_ms=0, bracket_latency_ms=182)
    assert (bt.cancel_latency_ms, bt.modify_latency_ms, bt.bracket_latency_ms) == (40.0, 0.0, 182.0)
    assert bt.broker.cancel_ns == 40_000_000 and bt.broker.bracket_ns == 182_000_000


@pytest.mark.parametrize("name", ["cancel_latency_ms", "modify_latency_ms", "bracket_latency_ms"])
def test_a_negative_latency_is_refused(name):
    with pytest.raises(ValueError, match=name):
        Backtest(Plain(), progress=False, prop=None, **{name: -1.0})


def test_the_settings_can_be_swept_or_set_with_param_on_any_strategy():
    assert {"cancel_latency_ms", "modify_latency_ms", "bracket_latency_ms"} <= ENGINE_PARAMS


def test_the_console_summary_names_them_only_when_they_are_set():
    day = _crash()
    off = run(BracketOnce(), day)[1]
    assert "Exec latency" not in metrics.format_console(off, metrics.compute(off))
    on = run(BracketOnce(), day, cancel_latency_ms=65, modify_latency_ms=25, bracket_latency_ms=182)[1]
    text = metrics.format_console(on, metrics.compute(on))
    assert "Exec latency:   cancel 65 ms, modify 25 ms, bracket 182 ms" in text
    only = run(BracketOnce(), day, cancel_latency_ms=65)[1]
    assert "cancel 65 ms, modify 0 ms, bracket = order latency" in \
        metrics.format_console(only, metrics.compute(only))
    assert (on.cancel_latency_ms, on.modify_latency_ms, on.bracket_latency_ms) == (65.0, 25.0, 182.0)


def test_the_cli_help_documents_the_three_flags():
    import io
    from contextlib import redirect_stdout

    import cli
    buf = io.StringIO()
    with pytest.raises(SystemExit), redirect_stdout(buf):
        cli.main(["--help"])
    text = " ".join(buf.getvalue().split())
    for flag in ("--cancel-latency-ms", "--modify-latency-ms", "--bracket-latency-ms"):
        assert flag in text, flag
    assert "full round trip" in text
