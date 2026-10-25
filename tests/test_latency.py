"""Order latency (user decision 2026-10-18: build the option, size it from an
estimate, measure tonight).

`latency_ms` delays when an order reaches the exchange: a new order, and the
stop/target legs created at an entry fill, cannot trigger or fill before
send-time + latency. Send time is the bar close for an order placed in on_bar,
the tick for on_tick, and the fill time for on_fill. Cancels, modifications and
the forced flattens are NOT delayed. 0 = off, bit-identical.

Every expected value is worked by hand. MNQ, tick 0.25, $2/point, $0.52 a side,
one event per second, quotes one tick either side of the trade price: ask =
price + .25, bid = price - .25. A 1m bar k holds events 60k..60k+59, so an order
sent in on_bar(0) is sent at event 60's timestamp (the bar boundary).
"""
from __future__ import annotations

import numpy as np
import pytest

from backtester.account import Account, TradeRecorder
from backtester.atm import submit_atm_exits
from backtester.broker import SimBroker
from backtester.engine import Backtest
from backtester.nt8config import AtmBracket, AtmSpec
from backtester.orders import BUY, Order, OrderType
from backtester.strategy import Strategy

from pathlib import Path

from conftest import NS, MultiDayCatalog, et_ns, make_day, make_day_at

REPO = Path(__file__).resolve().parent.parent

TICK = 0.25


def day_at(prices, date="20260202"):
    prices = np.asarray(prices, dtype="float64")
    ts = et_ns(date, "09:30") + np.arange(len(prices), dtype="int64") * NS
    return make_day_at(prices, ts, date)


def run(strat, day, **kw):
    bt = Backtest(strat, progress=False, **{"prop": None, **kw})
    bt.catalog = MultiDayCatalog([day])
    return bt.run()


class Base(Strategy):
    symbol = "MNQ"
    period = "1m"
    session = None
    qty = 1


class MarketOnce(Base):
    def on_bar(self, bar, bars):
        if bar.index == 0:
            self.buy()


class BracketOnce(Base):
    def on_bar(self, bar, bars):
        if bar.index == 0:
            self.buy_bracket(stop_ticks=4)


class LimitOnce(Base):
    limit = 130.0                        # far above the market: marketable at once

    def on_bar(self, bar, bars):
        if bar.index == 0:
            self.buy_limit(self.limit)


class Limit116(LimitOnce):
    limit = 116.0


class ExitFromOnFill(MarketOnce):
    def on_fill(self, fill):
        if fill.side == BUY:
            self.close_position()


# price(e) = 100 + .25 e: every event has its own price, so WHICH event an order
# filled on is visible in the fill price
RAMP = 100.0 + TICK * np.arange(180)


def test_a_market_order_waits_for_the_first_event_at_or_after_send_time_plus_latency():
    day = day_at(RAMP)
    # off: fills on event 60 (price 115.00) at the ask 115.25
    t, = run(MarketOnce(), day).trades
    assert t.entry_ts == int(day.ts[60]) and t.entry_price == 115.25
    # 2,500 ms: sent at ts[60], live at ts[60] + 2.5 s -> the first event at or
    # after that is event 63 (price 115.75), ask 116.00
    t, = run(MarketOnce(), day, latency_ms=2500).trades
    assert t.entry_ts == int(day.ts[63]) and t.entry_price == 116.00
    # exactly 3,000 ms lands ON event 63; one millisecond more misses it and the
    # order waits for event 64 (price 116.00), ask 116.25
    t, = run(MarketOnce(), day, latency_ms=3000).trades
    assert t.entry_ts == int(day.ts[63])
    t, = run(MarketOnce(), day, latency_ms=3001).trades
    assert t.entry_ts == int(day.ts[64]) and t.entry_price == 116.25


def test_a_marketable_limit_is_first_evaluated_when_it_arrives():
    day = day_at(RAMP)
    t, = run(LimitOnce(), day).trades
    assert t.entry_price == 115.25                       # the ask on event 60
    t, = run(LimitOnce(), day, latency_ms=2500).trades
    assert t.entry_ts == int(day.ts[63]) and t.entry_price == 116.00


def test_a_limit_that_was_marketable_when_sent_but_is_not_on_arrival_rests():
    day = day_at(RAMP)
    # a buy limit at 116.00. Sent at event 60 the ask is 115.25 (marketable);
    # off, it fills there at the quote.
    t, = run(Limit116(), day).trades
    assert t.entry_ts == int(day.ts[60]) and t.entry_price == 115.25
    # 3,001 ms: it arrives on event 64, ask 116.25, ABOVE the limit: no longer
    # marketable. The ramp only rises, so no trade prints through 116.00 later
    # and it never fills. (Evaluating the stale send-time quote would hand it a
    # phantom fill at the limit price.)
    assert run(Limit116(), day, latency_ms=3001).trades == []


def _crash():
    # flat 100 through event 63 (so the entry price is 100.25 either way), a dip
    # to 96 on events 64-65, then 92: a 4-tick stop at 99.25 is hit at event 64
    return day_at(np.concatenate([np.full(64, 100.0), np.full(2, 96.0),
                                  np.full(114, 92.0)]))


def test_the_stop_leg_made_at_an_entry_fill_is_also_in_flight():
    day = _crash()
    # off: entry on event 60 at the ask 100.25, stop 99.25 hit on event 64 (price
    # 96.00): fills at the bid 95.75. pnl (95.75 - 100.25) * 2 - 1.04 = -10.04
    t, = run(BracketOnce(), day).trades
    assert t.exit_tag == "stop" and t.exit_ts == int(day.ts[64])
    assert t.exit_price == 95.75 and t.pnl == pytest.approx(-10.04)
    # 2,500 ms: the entry goes live on event 63 (price 100, ask 100.25 again); its
    # stop leg is made at that fill and is live 2.5 s later, from event 66, so
    # the dip on events 64-65 does not stop it: the stop fills on event 66 (price
    # 92.00) at the bid 91.75. pnl (91.75 - 100.25) * 2 - 1.04 = -18.04
    t, = run(BracketOnce(), day, latency_ms=2500).trades
    assert t.entry_ts == int(day.ts[63]) and t.entry_price == 100.25
    assert t.exit_tag == "stop" and t.exit_ts == int(day.ts[66])
    assert t.exit_price == 91.75 and t.pnl == pytest.approx(-18.04)


def test_an_order_sent_from_on_fill_is_timed_from_the_fill_not_the_bar():
    day = day_at(RAMP)
    # off: the entry fills on event 60 and the exit, sent from on_fill, fills on
    # the SAME event at the bid 114.75... (price 115.00 - .25)
    t, = run(ExitFromOnFill(), day).trades
    assert t.entry_price == 115.25 and t.exit_price == 114.75
    # 2,500 ms: entry on event 63; the exit is sent at THAT fill (ts[63]) and is
    # live from event 66 (price 116.50), bid 116.25. Timed from the bar close
    # (ts[60]) it would have gone live on event 63 and filled at 115.50.
    t, = run(ExitFromOnFill(), day, latency_ms=2500).trades
    assert t.entry_ts == int(day.ts[63])
    assert t.exit_ts == int(day.ts[66]) and t.exit_price == 116.25


class MarketOnceTicks(MarketOnce):
    def on_tick(self, ts, price, index):             # forces resolve_span_ticks
        pass


class BracketOnceTicks(BracketOnce):
    def on_tick(self, ts, price, index):
        pass


def test_the_per_tick_resolver_gives_the_same_fills():
    day = day_at(RAMP)
    a, = run(MarketOnce(), day, latency_ms=2500).trades
    b, = run(MarketOnceTicks(), day, latency_ms=2500).trades
    assert (b.entry_ts, b.entry_price) == (a.entry_ts, a.entry_price)
    crash = _crash()
    a, = run(BracketOnce(), crash, latency_ms=2500).trades
    b, = run(BracketOnceTicks(), crash, latency_ms=2500).trades
    assert (b.exit_ts, b.exit_price, b.pnl) == (a.exit_ts, a.exit_price, a.pnl)


class BuyFromOnTick(Base):
    """Sends its market buy from on_tick, when event 10 prints."""

    def on_bar(self, bar, bars):
        pass

    def on_tick(self, ts, price, index):
        if index == 10:
            self.buy()


def test_an_order_sent_from_on_tick_is_timed_from_that_tick():
    day = day_at(RAMP)
    # off: sent after event 10, it fills on the NEXT event, 11 (price 102.75),
    # at the ask 103.00
    t, = run(BuyFromOnTick(), day).trades
    assert t.entry_ts == int(day.ts[11]) and t.entry_price == 103.00
    # 2,500 ms: sent at ts[10], live from ts[12.5] -> event 13 (price 103.25),
    # ask 103.50 - not event 11 (that is what a stale clock would give)
    t, = run(BuyFromOnTick(), day, latency_ms=2500).trades
    assert t.entry_ts == int(day.ts[13]) and t.entry_price == 103.50


def test_a_dynamic_atm_stop_is_in_flight_too(spec):
    # AtmBracket with a breakeven trigger builds a StopPlan: a different code
    # path in the broker. Tick .25, $2/point. Entry at the ask 100.25 (price 100);
    # the 8-tick stop sits at 98.25. Prices: 100 for events 0-3, 97 on 4-5 (a
    # plain stop would fill on event 4), 96 from event 6.
    prices = [100.0] * 4 + [97.0] * 2 + [96.0] * 4
    atm = AtmSpec(name="t", entry_qty=1, brackets=(
        AtmBracket(qty=1, stop_ticks=8, target_ticks=0,
                   be_trigger_ticks=40, be_plus_ticks=2),))

    def go(latency_ms):
        day = make_day(prices)
        broker = SimBroker(spec, Account(spec, 50_000.0), TradeRecorder(spec),
                           None, 0.0, latency_ms=latency_ms)
        broker.begin_day(day)
        broker.now_ts = int(day.ts[0])
        broker.submit(Order(side=BUY, qty=1, type=OrderType.MARKET, tag="entry"))
        broker.resolve_span(0, 4)                         # the entry fills here
        # the exit legs are sent when the entry has filled (broker.now_ts is
        # the fill time; no latency -> filled on event 0, with latency -> event 3)
        entry = broker.fills[0]
        submit_atm_exits(broker, atm, BUY, entry.price)
        broker.resolve_span(4, len(day))
        return broker, day

    off, day = go(0.0)
    assert off.fills[1].order.tag == "atm-stop1"
    assert off.fills[1].ts == int(day.ts[4]) and off.fills[1].price == 96.75
    on, day = go(2500.0)
    assert on.fills[0].ts == int(day.ts[3]) and on.fills[0].price == 100.25
    # sent at ts[3] -> live from event 6 (5.5 s -> the first event >= is 6)
    assert on.fills[1].ts == int(day.ts[6]) and on.fills[1].price == 95.75
    assert on.fills[1].order.price == pytest.approx(98.25)   # effective stop level


def test_latency_comes_from_the_strategy_unless_the_caller_overrides_it():
    day = day_at(RAMP)

    class Slow(MarketOnce):
        latency_ms = 2500.0

    assert run(Slow(), day).trades[0].entry_ts == int(day.ts[63])
    assert run(Slow(), day, latency_ms=0.0).trades[0].entry_ts == int(day.ts[60])
    with pytest.raises(ValueError, match="latency_ms"):
        Backtest(MarketOnce(), progress=False, latency_ms=-1.0)


def test_on_a_time_bar_the_order_is_sent_when_the_next_bars_first_tick_prints():
    # A 1m bar is stamped with its clock boundary (09:31:00), but in a quiet
    # market the next bar's first tick can print seconds later. Here events 0-59
    # run 09:30:00-09:30:59 and the next tick, event 60, prints at 09:31:02, 2 s
    # after the boundary; events then continue one a second. The strategy cannot
    # have acted before 09:31:02, so a 50 ms order is live at 09:31:02.050 and
    # fills on event 61 (09:31:03, price 115.25, ask 115.50), NOT on event 60
    # (stamping the boundary would make it live at 09:31:00.050, before any tick,
    # and the 50 ms would cost nothing).
    prices = 100.0 + TICK * np.arange(180)
    t0 = et_ns("20260202", "09:30")
    ts = t0 + np.arange(180, dtype="int64") * NS
    ts[60:] += 2 * NS
    day = make_day_at(prices, ts, "20260202")
    t, = run(MarketOnce(), day).trades
    assert t.entry_ts == int(day.ts[60]) and t.entry_price == 115.25      # off
    t, = run(MarketOnce(), day, latency_ms=50).trades
    assert t.entry_ts == int(day.ts[61]) and t.entry_price == 115.50
    # a renko-style stamp (bar.ts == the next tick) is untouched: see the gap
    # test above, where the tick is on the boundary and 2.5 s still skips 2 events


def test_engine_settings_can_be_set_by_a_sweep_override_without_a_declaration():
    from backtester.loader import load_strategy
    path = REPO / "strategies" / "ema_cross.py"
    s = load_strategy(path, {"latency_ms": 75.0, "daily_limit_mode": "kill",
                             "daily_loss_limit": 300.0, "daily_profit_target": 900.0})
    bt = Backtest(s, progress=False, prop=None)
    assert (bt.latency_ms, bt.daily_limit_mode) == (75.0, "kill")
    assert (bt.daily_loss_limit, bt.daily_profit_target) == (300.0, 900.0)
    with pytest.raises(AttributeError, match="no parameter"):
        load_strategy(path, {"not_a_setting": 1})
