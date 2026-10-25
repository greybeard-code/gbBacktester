"""Daily limits, the two mechanisms (user decision 2026-10-18).

GATE (default): day REALIZED P&L - trades closed today - at the limit blocks NEW
entries for the rest of the trading day; exits keep working. What several NinjaTrader
strategies' own daily limits do.
KILL SWITCH: equity INCLUDING open P&L, tick by tick: flatten there and refuse
every order. What GodZillaKilla does.

Every expected value is worked by hand. Contract: MNQ via get_spec, tick 0.25,
$2/point, $0.52 per side. make_day_at quotes straddle each trade by one tick
(ask = price + .25, bid = price - .25), one event per second, so a 1m bar k is
events 60k..60k+59 and an order submitted in on_bar(k) fills on event 60(k+1).
"""
from __future__ import annotations

import numpy as np
import pytest

from backtester.engine import Backtest
from backtester.orders import OrderState
from backtester.strategy import Strategy

from conftest import NS, MultiDayCatalog, et_ns, make_day_at

W, C = OrderState.WORKING, OrderState.CANCELLED


def day_at(date, prices):
    prices = np.asarray(prices, dtype="float64")
    ts = et_ns(date, "09:30") + np.arange(len(prices), dtype="int64") * NS
    return make_day_at(prices, ts, date)


def run_bt(strat, days, **kw):
    bt = Backtest(strat, progress=False, **{"prop": None, **kw})
    bt.catalog = MultiDayCatalog(days)
    return bt, bt.run()


class BracketBuyer(Strategy):
    """Buys a bracket whenever flat; records what the gate does to it."""
    symbol = "MNQ"
    period = "1m"
    session = None
    qty = 1
    stop_ticks = 20
    target_ticks = None

    def on_start(self):
        self.indices, self.positions = [], []
        self.states, self.blocked, self.stood = [], [], []

    def on_bar(self, bar, bars):
        self.indices.append(bar.index)
        self.positions.append(self.position)
        self.blocked.append(self._broker.entries_blocked)
        self.stood.append(self._broker.stood_down)
        if self.flat:
            o = self.buy_bracket(stop_ticks=self.stop_ticks,
                                 target_ticks=self.target_ticks)
            self.states.append(o.state)


# ------------------------------------------------------------------- gate --

def test_a_realized_loss_blocks_new_entries_but_does_not_stand_the_broker_down():
    # day 1: price 100 until event 69, then 95.00 to event 119, then 100.
    # bar 0: buy bracket (5-point stop). Fills on event 60, ask 100.25; stop
    # 95.25. Event 70 (price 95.00) is the first at/through it: the stop fills
    # at the bid 94.75 (never better than the stop). Realized (94.75 - 100.25)
    # * 2 = -11.00, trade pnl -11.00 - 1.04 = -12.04 <= -10: the gate trips ON
    # THAT FILL. Every later buy is refused. Day 2 is untouched (the gate
    # re-arms at the trading-day flush): one normal trade, eod flatten at the
    # bid 99.75, (99.75 - 100.25) * 2 - 1.04 = -2.04.
    p1 = np.concatenate([np.full(70, 100.0), np.full(50, 95.0), np.full(180, 100.0)])
    d1 = day_at("20260202", p1)
    d2 = day_at("20260203", np.full(180, 100.0))
    s = BracketBuyer()
    bt, res = run_bt(s, [d1, d2], daily_loss_limit=10.0)
    first, second = res.trades
    assert first.exit_tag == "stop" and first.exit_ts == int(d1.ts[70])
    assert first.exit_price == 94.75
    assert first.pnl == pytest.approx(-12.04)
    assert second.pnl == pytest.approx(-2.04)
    # day 1: 5 bars. The entry on bar 0 is accepted; the four later buys are
    # refused outright (handed back already CANCELLED). Day 2 bar 0 is accepted.
    assert s.states == [W, C, C, C, C, W]
    assert s.blocked == [False, True, True, True, True, False, False, False]
    assert not any(s.stood)                  # the gate never stands the broker down
    assert s.indices == list(range(8))       # and the strategy keeps its bars
    assert res.dll_days == ["20260202"] and res.dpt_days == []
    assert res.daily_limit_mode == "gate"
    assert res.daily_pnl == {"20260202": pytest.approx(-12.04),
                             "20260203": pytest.approx(-2.04)}
    assert bt.broker.working == []


def _underwater_then_target():
    # price 100 to event 69, 92.00 on events 70-89, then 105.00. The bracket has
    # a 10-point stop (90.25) it never reaches and a 4-point target (104.25).
    prices = np.concatenate([np.full(70, 100.0), np.full(20, 92.0),
                             np.full(90, 105.0)])
    return day_at("20260202", prices)


class UnderwaterBuyer(BracketBuyer):
    stop_ticks = 40
    target_ticks = 16


def test_the_gate_lets_an_underwater_trade_run_where_the_kill_switch_cuts_it():
    day = _underwater_then_target()
    # GATE: realized P&L is 0 until the trade closes, so the -$16.50 floating
    # loss at 92.00 triggers nothing. Event 90 (price 105) is through the
    # target: bid 104.75 is better than the 104.25 limit, so it fills at the
    # quote. pnl (104.75 - 100.25) * 2 - 1.04 = 7.96.
    _, gate = run_bt(UnderwaterBuyer(), [day], daily_loss_limit=10.0)
    t, again = gate.trades
    assert t.exit_tag == "target" and t.exit_price == 104.75
    assert t.pnl == pytest.approx(7.96)
    # the day is still open for business: it buys again on bar 1 (event 120,
    # price 105, ask 105.25) and is flattened at eod (bid 104.75): -2.04
    assert again.pnl == pytest.approx(-2.04)
    assert gate.dll_days == []
    # KILL SWITCH, same ticks: balance 49,999.48, floor 49,990. Equity at event
    # 70 (price 92.00) = 49,999.48 + (92 - 100.25) * 2 = 49,982.98 <= floor
    # (event 69 at 100.00 is 49,998.98, above it), so it flattens there at the
    # bid 91.75: (91.75 - 100.25) * 2 - 1.04 = -18.04.
    _, kill = run_bt(UnderwaterBuyer(), [day], daily_loss_limit=10.0,
                     daily_limit_mode="kill")
    t, = kill.trades
    assert t.exit_tag == "dll" and t.exit_ts == int(day.ts[70])
    assert t.exit_price == 91.75
    assert t.pnl == pytest.approx(-18.04)
    assert kill.dll_days == ["20260202"] and kill.daily_limit_mode == "kill"


class Reverser(Strategy):
    """Long on bar 0, then a single qty-2 market sell on bar 1 (a reversal)."""
    symbol = "MNQ"
    period = "1m"
    session = None
    qty = 1

    def on_start(self):
        self.positions = []

    def on_bar(self, bar, bars):
        self.positions.append(self.position)
        if bar.index == 0:
            self.buy()
        elif bar.index == 1:
            self.sell(qty=2, tag="rev")


def test_a_reversal_that_trips_the_gate_is_cut_at_flat():
    # long 1 from event 60 (ask 100.25). The qty-2 sell is submitted on bar 1 and
    # fills on event 120, price 94.00, bid 93.75: it closes the long first,
    # realized (93.75 - 100.25) * 2 = -13.00, trade pnl -14.04 <= -10 -> the
    # gate trips, and the SHORT leg of the reversal is never opened.
    prices = np.concatenate([np.full(120, 100.0), np.full(180, 94.0)])
    day = day_at("20260202", prices)
    s = Reverser()
    _, res = run_bt(s, [day], daily_loss_limit=10.0)
    t, = res.trades
    assert t.exit_price == 93.75 and t.pnl == pytest.approx(-14.04)
    assert s.positions == [0, 1, 0, 0, 0]
    assert res.fills == 2                          # buy 1, sell 1 (not 2)
    # control: with no limit the same sell goes short and is flattened at eod
    s2 = Reverser()
    _, free = run_bt(s2, [day])
    assert s2.positions == [0, 1, -1, -1, -1]
    assert len(free.trades) == 2


class PendingEntry(Strategy):
    """Bar 0: a market bracket with a 4-point target AND a resting buy limit
    well below the market."""
    symbol = "MNQ"
    period = "1m"
    session = None
    qty = 1

    def on_start(self):
        self.positions, self.limit_states = [], []
        self.lim = None

    def on_bar(self, bar, bars):
        self.positions.append(self.position)
        if bar.index == 0:
            self.buy_bracket(stop_ticks=40, target_ticks=16)
            self.lim = self.buy_limit(90.0, tag="dip")
        if self.lim is not None:
            self.limit_states.append(self.lim.state)


def test_a_profit_gate_cancels_pending_entries_and_refuses_new_ones():
    # entry on event 60 (ask 100.25); event 90 (price 105) is through the 104.25
    # target: bid 104.75, pnl +7.96 >= the $5 profit gate -> blocked. The
    # resting buy limit at 90.00 is cancelled at that moment; the dip to 89.00
    # on events 120-179 would otherwise have filled it (marketable at the ask).
    prices = np.concatenate([np.full(90, 100.0), np.full(30, 105.0), np.full(60, 89.0)])
    day = day_at("20260202", prices)
    s = PendingEntry()
    _, res = run_bt(s, [day], daily_profit_target=5.0)
    t, = res.trades
    assert t.exit_tag == "target" and t.pnl == pytest.approx(7.96)
    assert s.positions == [0, 0, 0]                   # never filled the dip
    assert s.limit_states == [W, C, C]                # cancelled by the trip
    assert res.dpt_days == ["20260202"] and res.dll_days == []
    # control: no profit gate -> the limit rests, then the dip fills it
    s2 = PendingEntry()
    _, free = run_bt(s2, [day])
    assert s2.positions == [0, 0, 1]
    assert len(free.trades) == 2                      # the dip trade, flat at eod


def test_a_limit_out_of_reach_changes_nothing():
    day = _underwater_then_target()
    _, off = run_bt(UnderwaterBuyer(), [day])
    for mode in ("gate", "kill"):
        _, far = run_bt(UnderwaterBuyer(), [day], daily_loss_limit=1e9,
                        daily_profit_target=1e9, daily_limit_mode=mode)
        assert [t.pnl for t in far.trades] == [t.pnl for t in off.trades]
        assert far.daily_pnl == off.daily_pnl
        assert far.dll_days == far.dpt_days == []


# ------------------------------------------------------------ kill switch --

def test_a_zero_profit_target_in_kill_mode_stands_down_before_any_trade():
    # ceiling = start balance + 0, so the very first (flat) mark is already at
    # it: nothing may trade all day
    day = day_at("20260202", np.full(180, 100.0))
    s = BracketBuyer()
    _, res = run_bt(s, [day], daily_profit_target=0.0, daily_limit_mode="kill")
    assert res.trades == [] and res.dpt_days == ["20260202"]
    assert s.states == [C, C, C]                      # every buy refused
    assert s.stood == [True, True, True]


class ReenterOnTarget(BracketBuyer):
    """Re-enters from on_fill the instant its target fills: an order that would
    fill on the SAME event, which _mark(i, i) never sees."""
    stop_ticks = 40
    target_ticks = 16

    def on_fill(self, fill):
        if fill.order.tag == "target":
            self.reentries = getattr(self, "reentries", 0) + 1
            self.buy()


def test_same_event_reentry_after_a_profit_target_fill_is_cancelled_in_kill_mode():
    # entry on event 60 (ask 100.25); price jumps to 105 on event 90, through the
    # 104.25 target: bid 104.75, balance 49,999.48 + 9.00 - 0.52 = 50,007.96, past
    # the 50,005 ceiling BY THE FILL ITSELF (the marks before it stayed under:
    # 49,998.98). Only the check at the end of the fill can stop the market buy
    # the strategy placed from on_fill.
    prices = np.concatenate([np.full(90, 100.0), np.full(90, 105.0)])
    s = ReenterOnTarget()
    _, res = run_bt(s, [day_at("20260202", prices)], daily_profit_target=5.0,
                    daily_limit_mode="kill")
    assert s.reentries == 1                  # it did try
    t, = res.trades                          # ...and the order never filled
    assert t.exit_tag == "target" and t.pnl == pytest.approx(7.96)
    assert res.dpt_days == ["20260202"]


class BracketBuyerTicks(BracketBuyer):
    def on_tick(self, ts, price, index):       # forces resolve_span_ticks
        pass


def test_the_gate_behaves_the_same_on_the_per_tick_resolver():
    p1 = np.concatenate([np.full(70, 100.0), np.full(50, 95.0), np.full(180, 100.0)])
    days = [day_at("20260202", p1), day_at("20260203", np.full(180, 100.0))]
    fast_s, tick_s = BracketBuyer(), BracketBuyerTicks()
    _, fast = run_bt(fast_s, days, daily_loss_limit=10.0)
    _, ticks = run_bt(tick_s, days, daily_loss_limit=10.0)
    assert [t.pnl for t in ticks.trades] == [t.pnl for t in fast.trades]
    assert tick_s.states == fast_s.states == [W, C, C, C, C, W]
    assert ticks.dll_days == fast.dll_days == ["20260202"]


# ----------------------------------------------------------------- config --

def test_mode_comes_from_the_strategy_unless_the_caller_overrides_it():
    day = day_at("20260202", np.full(180, 100.0))

    class Kills(BracketBuyer):
        daily_limit_mode = "kill"

    assert run_bt(Kills(), [day], daily_loss_limit=5.0)[0].daily_limit_mode == "kill"
    assert run_bt(Kills(), [day], daily_loss_limit=5.0,
                  daily_limit_mode="gate")[0].daily_limit_mode == "gate"
    assert run_bt(BracketBuyer(), [day])[0].daily_limit_mode == "gate"   # default
    with pytest.raises(ValueError, match="gate' or 'kill"):
        Backtest(BracketBuyer(), progress=False, daily_limit_mode="soft")
