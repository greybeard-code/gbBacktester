"""Engine branches the coverage run (plan task 24) showed no test reached.

Each is a real behavior, not a defensive line: a prop-firm breach caused by a gap
fill while flat, an empty range, a day with nothing to trade, and the news filter
gating EVERY entry helper (limits and stops too) while leaving exits alone.
Hand-computed: MNQ tick 0.25, $2/point, $0.52 a side, one event per second, quotes
a tick either side of the trade price.
"""
from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from backtester.account import PropFirmConfig
from backtester.engine import Backtest
from backtester.strategy import Strategy

from conftest import NS, MultiDayCatalog, et_ns, make_day_at


def day_at(date, prices, hhmm="09:30"):
    prices = np.asarray(prices, dtype="float64")
    ts = et_ns(date, hhmm) + np.arange(len(prices), dtype="int64") * NS
    return make_day_at(prices, ts, date)


class Base(Strategy):
    symbol = "MNQ"
    period = "1m"
    session = None
    qty = 1


def run(strat, days, **kw):
    bt = Backtest(strat, progress=False, **{"prop": None, **kw})
    bt.catalog = MultiDayCatalog(days)
    return bt, bt.run()


# --------------------------------------------- prop breach by a gap fill, flat --

class BracketOnce(Base):
    def on_bar(self, bar, bars):
        if bar.index == 0:
            self.buy_bracket(stop_ticks=4)


def test_a_stop_filled_through_the_floor_halts_the_run_while_flat():
    # Floor 50,000 - 10 = 49,990. Long from event 60 at the ask 100.25 (balance
    # 49,999.48), stop 99.25. Price 100 until event 89, then 90.00: the stop fills
    # on event 90 at the bid 89.75, realized (89.75 - 100.25) * 2 = -21.00, balance
    # 49,999.48 - 21.00 - 0.52 = 49,977.96 <= 49,990. The marks before the fill
    # (price 100: 49,998.98) never touched the floor, so it is the FLAT mark right
    # after the fill that sees the breach and halts, at the fill's own timestamp.
    prices = np.concatenate([np.full(90, 100.0), np.full(90, 90.0)])
    d1 = day_at("20260202", prices)
    d2 = day_at("20260203", np.full(180, 100.0))
    bt, res = run(BracketOnce(), [d1, d2],
                  prop=PropFirmConfig(threshold=10.0, halt_on_breach=True))
    t, = res.trades
    assert t.exit_tag == "stop" and t.exit_price == 89.75
    assert t.pnl == pytest.approx(-22.04)
    assert res.halted_on == "20260202" and res.prop.breached
    assert res.prop.breach_ts == int(d1.ts[90])
    assert list(res.daily_pnl) == ["20260202"]            # day 2 never ran


# ---------------------------------------------------- nothing to run / nothing to trade --

def test_a_range_with_no_days_says_so():
    bt = Backtest(Base(), progress=False, prop=None, start="20260301", end="20260305")
    bt.catalog = MultiDayCatalog([day_at("20260202", np.full(120, 100.0))])
    with pytest.raises(FileNotFoundError, match="No MNQ days in range 20260301..20260305"):
        bt.run()


class BuyOnBarZero(Base):
    def on_start(self):
        self.seen = []

    def on_bar(self, bar, bars):
        self.seen.append(bar.index)
        if bar.index == 0 and self.flat:
            self.buy()


class BuyEveryThirdBar(BuyOnBarZero):
    """A day here has 3 one-minute bars, and bar.index runs on across days, so
    index % 3 == 0 is the first bar of each day."""

    def on_bar(self, bar, bars):
        self.seen.append(bar.index)
        if bar.index % 3 == 0 and self.flat:
            self.buy()


def test_an_empty_day_file_is_skipped_and_the_next_day_trades_normally():
    d1 = day_at("20260202", np.full(180, 100.0))
    empty = dataclasses.replace(
        d1, date="20260203", ts=d1.ts[:0], price=d1.price[:0], volume=d1.volume[:0],
        ask=d1.ask[:0], bid=d1.bid[:0], ask_size=d1.ask_size[:0],
        bid_size=d1.bid_size[:0], aggr=d1.aggr[:0])
    d3 = day_at("20260204", np.full(180, 100.0))
    s = BuyEveryThirdBar()
    _, res = run(s, [d1, empty, d3])
    assert len(res.trades) == 2                          # one trade on each real day
    assert s.seen == [0, 1, 2, 3, 4, 5]                  # the empty day contributes no bars
    assert set(res.daily_pnl) == {"20260202", "20260204"}


class RenkoBuyer(BuyOnBarZero):
    period = "r100-4"


def test_a_day_too_quiet_to_complete_one_renko_brick_is_skipped():
    # r100-4 needs a 25-point move; 120 flat events build no bar at all, so the
    # strategy is never called and nothing trades, while the next day (a 40-point
    # ramp, four events per point) does build bars
    quiet = day_at("20260202", np.full(120, 100.0))
    ramp = day_at("20260203", 100.0 + 0.25 * np.arange(400))
    s = RenkoBuyer()
    _, res = run(s, [quiet, ramp])
    assert s.seen and s.seen[0] == 0                     # first bar index is the ramp's
    assert "20260202" not in res.daily_pnl


# ----------------------------- the news filter gates every entry helper, not exits --

def news_csv(tmp_path, event_ns):
    p = tmp_path / "news.csv"
    p.write_text("dateline_utc_epoch,datetime_utc,datetime_et,weekday_et,"
                 "currency,event,impact\n"
                 f"{event_ns // NS},x,x,Mon,USD,Test,high\n", encoding="utf-8")
    return str(p)


class EveryHelper(Base):
    news_filter = True
    news_pre_min = 5.0
    news_post_min = 5.0

    def on_start(self):
        self.out = {}

    def on_bar(self, bar, bars):
        if bar.index == 0:
            self.buy()                                    # outside the window: fills
        elif bar.index in (1, 6):                         # 1 = free, 6 = inside
            px = bar.close
            self.out[bar.index] = {
                "buy": self.buy(), "sell": self.sell(),
                "buy_bracket": self.buy_bracket(stop_ticks=8),
                "sell_bracket": self.sell_bracket(stop_ticks=8),
                "buy_limit": self.buy_limit(px - 1.0),
                "sell_limit": self.sell_limit(px + 1.0),
                "buy_stop": self.buy_stop(px + 1.0),
                "sell_stop": self.sell_stop(px - 1.0),
            }
            if bar.index == 6:
                self.out["close"] = self.close_position()


def test_news_window_blocks_every_entry_helper_but_never_the_exit(tmp_path):
    # event 09:40 ET +/- 5 min: bars closing 09:35:00..09:45:00 are blocked =
    # bar indexes 4..14 (bar k closes at 09:31 + k). Bar 1 is free, bar 6 is inside.
    day = day_at("20260202", 100.0 + 0.25 * (np.arange(1800) // 60))
    s = EveryHelper()
    s.news_csv = news_csv(tmp_path, et_ns("20260202", "09:40"))
    run(s, [day])
    free, blocked = s.out[1], s.out[6]
    assert all(v is not None for v in free.values()), free
    assert all(v is None for v in blocked.values()), blocked
    assert s.out["close"] is not None                    # the exit is never gated
