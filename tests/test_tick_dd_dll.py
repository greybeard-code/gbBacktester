"""Tick-level max drawdown and daily loss limit (plan task 6b, 2026-10-17).

Both used to be evaluated only at bar closes, so a dip through the limit that
recovered inside the bar was invisible. Every expected value below is hand
computed. Contract is MNQ via get_spec: tick 0.25, $2/point, $0.52 per side.
make_day quotes straddle each trade by one tick, one event per second.
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from backtester import metrics
from backtester.account import DrawdownTracker
from backtester.engine import Backtest
from backtester.strategy import Strategy

from conftest import MultiDayCatalog, make_day

DAY_S = 86_400


def _day(prices, n: int, date: str):
    # start on a minute boundary so bar k is exactly events 60k..60k+59
    return replace(make_day(prices, start_ts=(960 + n * DAY_S) * 10**9),
                   date=date)


class BuyWhenFlat(Strategy):
    symbol = "MNQ"
    period = "1m"
    session = None
    qty = 1

    def on_bar(self, bar, bars):
        if self.flat:
            self.buy()


class BracketOnce(Strategy):
    symbol = "MNQ"
    period = "1m"
    session = None
    qty = 1

    def on_bar(self, bar, bars):
        if self.flat and bar.index == 0:
            self.buy_bracket(stop_ticks=20)          # 5-point stop


class BuyWhenFlatTicks(BuyWhenFlat):
    def on_tick(self, ts, price, index):            # forces resolve_span_ticks
        pass


def run(strategy, days, dll=None):
    bt = Backtest(strategy, prop=None, daily_loss_limit=dll,
                  daily_limit_mode="kill", progress=False)
    bt.catalog = MultiDayCatalog(days)
    res = bt.run()
    return res, metrics.compute(res)


# bar 0: flat at 100. Bar 1: the market buy fills on its first event at the
# ask (100.25), price falls a point a second to 95, then recovers to 99 and
# holds there to the bar close. Bar 2 onward: 101.
DIP = ([100.0] * 60
       + [100.0, 99.0, 98.0, 97.0, 96.0, 95.5, 95.0] + [99.0] * 53
       + [101.0] * 120)


# ------------------------------------------------------------- drawdown ---- #

def test_tracker_scalar_and_series_agree():
    a, b = DrawdownTracker(100.0), DrawdownTracker(100.0)
    path = [100.0, 104.0, 101.0, 106.0, 98.0, 99.0]
    for x in path:
        a.update_scalar(x)
    b.update_series(np.array(path))
    assert a.max_dd == b.max_dd == pytest.approx(-8.0)          # 106 -> 98
    assert a.max_dd_pct == pytest.approx(b.max_dd_pct)
    assert a.max_dd_pct == pytest.approx(-8.0 / 106.0 * 100)
    assert a.peak == b.peak == 106.0


def test_max_drawdown_sees_the_dip_inside_the_bar():
    res, st = run(BuyWhenFlat(), [_day(DIP, 0, "20260105")])
    # entry 100.25, commission 0.52 -> balance 49,999.48
    # trough at 95: 49,999.48 + (95 - 100.25) * 2 = 49,988.98 -> -11.02
    assert st["max_drawdown"] == pytest.approx(-11.02)
    assert st["max_drawdown_pct"] == pytest.approx(-11.02 / 50_000 * 100)
    # the bar-close curve only ever sees 99 at the dip bar: -3.02
    assert st["max_drawdown_bar"] == pytest.approx(-3.02)
    # and the bar-close equity curve itself is unchanged (chart input)
    assert res.equity[1] == pytest.approx(49_996.98)


def test_max_drawdown_includes_exit_commission_and_slippage():
    # flat price, so the only loss is the round-trip cost at the eod flatten:
    # buy at ask 100.25, sell at bid 99.75, $1.04 commission
    prices = [100.0] * 180
    res, st = run(BuyWhenFlat(), [_day(prices, 0, "20260105")])
    assert len(res.trades) == 1 and res.trades[0].exit_tag == "eod"
    assert res.trades[0].pnl == pytest.approx(-0.5 * 2 - 1.04)
    # marked on the trade price, equity in position is 49,999.48 + (100 -
    # 100.25) * 2 = 49,998.98; the exit fill at the bid takes it to 49,997.96
    assert st["max_drawdown"] == pytest.approx(-2.04)


def test_drawdown_with_no_trades_is_zero():
    class Idle(BuyWhenFlat):
        def on_bar(self, bar, bars):
            pass
    _, st = run(Idle(), [_day([100.0] * 120, 0, "20260105")])
    assert st["max_drawdown"] == 0.0 and st["max_drawdown_pct"] == 0.0


# ------------------------------------------------------ daily loss limit ---- #

def test_dll_fires_on_an_intra_bar_dip_that_recovers_by_the_close():
    days = [_day(DIP, 0, "20260105"), _day([100.0] * 180, 1, "20260106")]
    res, st = run(BuyWhenFlat(), days, dll=10.0)
    # floor 49,990. Equity at 96 is 49,990.98 (above), at 95.5 it is
    # 49,989.98 (at/below): flatten there at the bid, 95.25
    day1 = [t for t in res.trades if t.entry_ts < days[1].ts[0]]
    assert len(day1) == 1, "stood down: no re-entry for the rest of the day"
    t = day1[0]
    assert t.exit_tag == "dll"
    assert t.entry_price == 100.25 and t.exit_price == 95.25
    assert t.exit_ts == int(days[0].ts[65])                  # the 95.5 event
    assert t.pnl == pytest.approx((95.25 - 100.25) * 2 - 1.04)
    assert res.dll_days == ["20260105"]
    assert res.daily_pnl["20260105"] == pytest.approx(-11.04)
    # the old bar-close check saw 49,996.98 at that bar's close and never fired
    # trading resumes the next day with a fresh floor
    day2 = [t for t in res.trades if t.entry_ts >= days[1].ts[0]]
    assert len(day2) == 1 and day2[0].exit_tag == "eod"
    # drawdown includes the dll exit fill (49,988.96), and day 2's flat round
    # trip (-2.04, as in the commission test above) deepens the same
    # drawdown from the 50,000 peak: -11.04 - 2.04
    assert day2[0].pnl == pytest.approx(-2.04)
    assert st["max_drawdown"] == pytest.approx(-13.08)


def test_dll_crossed_by_a_stop_fill_stands_down_while_flat():
    # stop at 95.25; price gaps 96 -> 94, the stop fills at the bid 93.75
    prices = ([100.0] * 60 + [100.0, 98.0, 96.0, 94.0] + [94.0] * 56
              + [100.0] * 120)
    res, _ = run(BracketOnce(), [_day(prices, 0, "20260105")], dll=10.0)
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.exit_tag == "stop" and t.exit_price == 93.75
    # 49,999.48 + (93.75 - 100.25) * 2 - 0.52 = 49,985.96 <= 49,990
    assert t.pnl == pytest.approx(-13.0 - 1.04)
    assert res.dll_days == ["20260105"]


def test_dll_through_the_on_tick_resolver_matches():
    days = [_day(DIP, 0, "20260105"), _day([100.0] * 180, 1, "20260106")]
    fast, _ = run(BuyWhenFlat(), days, dll=10.0)
    ticks, _ = run(BuyWhenFlatTicks(), days, dll=10.0)
    key = [(t.entry_ts, t.exit_ts, t.exit_price, t.exit_tag, round(t.pnl, 6))
           for t in fast.trades]
    assert key == [(t.entry_ts, t.exit_ts, t.exit_price, t.exit_tag,
                    round(t.pnl, 6)) for t in ticks.trades]
    assert ticks.dll_days == ["20260105"]


def test_dll_off_and_dll_out_of_reach_are_identical():
    days = [_day(DIP, 0, "20260105"), _day([100.0] * 180, 1, "20260106")]
    off, s_off = run(BuyWhenFlat(), days, dll=None)
    far, s_far = run(BuyWhenFlat(), days, dll=1e9)
    assert [(t.entry_ts, t.exit_ts, t.pnl) for t in off.trades] == \
        [(t.entry_ts, t.exit_ts, t.pnl) for t in far.trades]
    assert s_off["max_drawdown"] == s_far["max_drawdown"]
    assert off.dll_days == far.dll_days == []


class ReenterOnStop(BracketOnce):
    """Re-enters from on_fill the instant its stop fills - an order that would
    fill on the SAME event, where _mark(i, i) does nothing. Only the DLL check
    at the end of the broker's _fill can stop it."""

    def on_start(self):
        self.reentries = 0

    def on_fill(self, fill):
        if fill.order.tag == "stop":
            self.reentries += 1
            self.buy()


def test_same_event_reentry_after_a_dll_stop_fill_is_cancelled():
    prices = ([100.0] * 60 + [100.0, 98.0, 96.0, 94.0] + [94.0] * 56
              + [100.0] * 120)
    strat = ReenterOnStop()
    res, _ = run(strat, [_day(prices, 0, "20260105")], dll=10.0)
    assert strat.reentries == 1                 # it did try
    assert len(res.trades) == 1                 # ...and the order never filled
    assert res.trades[0].exit_tag == "stop"
    assert res.dll_days == ["20260105"]
