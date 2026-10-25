"""Stitching walk-forward out-of-sample days.

An OOS window cut in mid-week splits one CME trading day (18:00 ET -> 17:00 ET,
named by the date it ENDS on): the earlier window books the evening part under
the NEXT date, the later window books the morning part under that same date.
Stitching with dict.update() kept only the later half and silently dropped the
other. Every figure is worked by hand: MNQ, tick 0.25, $2/point, $0.52 a side.
"""
from __future__ import annotations

import numpy as np
import pytest

from backtester.engine import Backtest
from backtester.strategy import Strategy
from backtester.walkforward import Window, stitch_daily, summarize

from conftest import NS, MultiDayCatalog, et_ns, make_day_at


def test_stitch_sums_days_that_two_windows_share():
    w1 = Window(1, [], [], oos_daily={"20260203": -1.0, "20260204": 2.0})
    w2 = Window(2, [], [], oos_daily={"20260204": 3.0, "20260205": 4.0})
    assert stitch_daily([w1, w2]) == {"20260203": -1.0, "20260204": 5.0,
                                      "20260205": 4.0}
    assert stitch_daily([]) == {}


def test_the_printed_summary_uses_the_summed_days():
    def win(i, daily):
        return Window(i, [], [], is_metric=1.0, oos_metric=1.0,
                      oos_stats={"net_pnl": sum(daily.values()), "total_trades": 1},
                      oos_daily=daily)
    text = summarize([win(1, {"20260203": 100.0, "20260204": 50.0}),
                      win(2, {"20260204": 25.0, "20260205": 10.0})],
                     "sharpe", 50_000.0)
    # 100 + (50 + 25) + 10 = 185 over 3 trading days (update() would say 135)
    assert "Stitched OOS: net $185 over 3 days" in text


def _file(date, *blocks):
    """One day file from (HH:MM, prices) blocks, one event per second."""
    ts = np.concatenate([et_ns(date, hhmm) + np.arange(len(p), dtype="int64") * NS
                         for hhmm, p in blocks])
    prices = np.concatenate([np.asarray(p, dtype="float64") for _, p in blocks])
    return make_day_at(prices, ts, date)


class InOut(Strategy):
    """Buys on every 10th bar and closes on the 4th bar after: flat again well
    before each 10-bar block ends, so nothing is open at a window boundary."""
    symbol = "MNQ"
    period = "1m"
    session = ("18:00", "16:55")
    qty = 1

    def on_bar(self, bar, bars):
        if bar.index % 10 == 0 and self.flat:
            self.buy()
        elif bar.index % 10 == 3:
            self.close_position()


def _run(days):
    bt = Backtest(InOut(), prop=None, progress=False)
    bt.catalog = MultiDayCatalog(days)
    return bt.run()


def test_windows_cut_mid_week_split_a_trading_day_and_must_be_summed():
    # price rises .25 a minute inside every 10-minute block. In each block the
    # buy (bar 0) fills on event 60: price 100.25, ask 100.50; the close (bar 3)
    # fills on event 240: price 101.00, bid 100.75 -> (100.75 - 100.50) * 2 -
    # 1.04 = -0.54 per trade, one trade per block.
    ramp = 100.0 + 0.25 * (np.arange(600) // 60)
    a = _file("20260202", ("18:00", ramp))                       # evening only
    b = _file("20260203", ("00:00", ramp), ("18:00", ramp))      # morning + evening
    c = _file("20260204", ("00:00", ramp))                       # morning only
    whole = _run([a, b, c])
    w1, w2 = _run([a, b]), _run([c])           # cut between B and C, mid-week

    assert [round(t.pnl, 2) for t in whole.trades] == [-0.54] * 4
    # trading day 20260203 = A's evening + B's morning; 20260204 = B's evening
    # + C's morning
    assert whole.daily_pnl == {"20260203": pytest.approx(-1.08),
                               "20260204": pytest.approx(-1.08)}
    # the cut splits 20260204: window 1 books B's evening, window 2 books C's
    # morning, both under the SAME key, each flagged
    assert w1.daily_pnl == {"20260203": pytest.approx(-1.08),
                            "20260204": pytest.approx(-0.54)}
    assert w2.daily_pnl == {"20260204": pytest.approx(-0.54)}
    assert "incomplete trading day" in w1.day_notes["20260204"]
    assert "partial first trading day" in w2.day_notes["20260204"]

    stitched = stitch_daily([Window(1, [], [], oos_daily=w1.daily_pnl),
                             Window(2, [], [], oos_daily=w2.daily_pnl)])
    assert stitched == whole.daily_pnl                       # no half lost
    trades_total = sum(t.pnl for r in (w1, w2) for t in r.trades)
    assert sum(stitched.values()) == pytest.approx(trades_total)    # -2.16
    # what dict.update() used to give: the evening half silently gone
    lossy = {**w1.daily_pnl, **w2.daily_pnl}
    assert sum(lossy.values()) == pytest.approx(-1.62) != pytest.approx(trades_total)
