"""Engine paths beyond the daily loss limit: overnight carry, session-end
flatten, out-of-session bars, end-of-data flatten, news flatten, the daily
profit target and the prop-firm halt.

Every day is built with explicit prices at ET wall-clock times (conftest
make_day_at + et_ns), so session windows really apply, and every expected
value is worked by hand. Contract: MNQ via get_spec, tick 0.25, $2/point,
$0.52 per side. make_day_at quotes straddle each trade by one tick (ask = price
+ 0.25, bid = price - 0.25), one event per second. A 1m bar k holds events
60k..60k+59 when the day starts on a minute; an order submitted in on_bar(k)
fills on the FIRST event of bar k+1, at the ask (buy) or bid (sell).
"""
from __future__ import annotations

import numpy as np
import pytest

from backtester.account import PropFirmConfig
from backtester.engine import Backtest
from backtester.orders import OrderState
from backtester.strategy import Strategy

from conftest import NS, MultiDayCatalog, et_ns, make_day_at

TICK = 0.25
PV = 2.0                      # MNQ $/point
COMM = 0.52                   # per side
RT = 2 * COMM


def day_at(date, hhmm, prices):
    prices = np.asarray(prices, dtype="float64")
    ts = et_ns(date, hhmm) + np.arange(len(prices), dtype="int64") * NS
    return make_day_at(prices, ts, date)


def ramp(n, start=100.0, step=TICK, per=60):
    """price rises `step` every `per` events (one minute by default)"""
    return start + step * (np.arange(n) // per)


class Probe(Strategy):
    """Records what the engine hands it; enters via subclass hooks."""
    symbol = "MNQ"
    period = "1m"
    session = None
    qty = 1

    def on_start(self):
        self.indices = []
        self.positions = []

    def on_bar(self, bar, bars):
        self.indices.append(bar.index)
        self.positions.append(self.position)
        self.act(bar)

    def act(self, bar):
        pass


class HoldFirst(Probe):
    def act(self, bar):
        if bar.index == 0:
            self.buy()


class BuyWhenFlat(Probe):
    def act(self, bar):
        if self.flat:
            self.buy()


def run(strat, days, **kw):
    bt = Backtest(strat, progress=False, **{"prop": None, **kw})
    bt.catalog = MultiDayCatalog(days)
    return bt.run()


# ------------------------------------------------------------ session flush --

def test_session_end_flatten_and_out_of_session_bars_are_skipped():
    # 30 minutes of data from 09:30, but the session ends 09:45. Prices ramp
    # 100 -> 103.5 over the first 15 minutes, then crash to 50: if the engine
    # looked past 09:45 the exit would show it.
    prices = np.concatenate([ramp(900), np.full(900, 50.0)])
    day = day_at("20260202", "09:30", prices)

    class S(HoldFirst):
        session = ("09:30", "09:45")

    s = S()
    res = run(s, [day])
    # bars with ts_end 09:31..09:45 are in-session: 15 of the day's 30, and
    # they are the only ones that get a bar.index
    assert s.indices == list(range(15))
    assert len(res.equity) == 15
    t, = res.trades
    # buy submitted on bar 0 fills on event 60 (price 100.25) at ask 100.50
    assert t.entry_ts == int(day.ts[60]) and t.entry_price == 100.50
    # flatten at the last event of the last in-session bar: event 899, price
    # 100 + .25 * 14 = 103.50, at the bid 103.25 (tag eod)
    assert t.exit_tag == "eod" and t.exit_ts == int(day.ts[899])
    assert t.exit_price == 103.25
    assert t.pnl == pytest.approx((103.25 - 100.50) * PV - RT)         # 4.46
    assert res.daily_pnl == {"20260202": pytest.approx(4.46)}
    assert res.day_notes == {}             # a whole session needs no note


# --------------------------------------------------------------- overnight --

def _overnight(prices_a, prices_b):
    a = np.asarray(prices_a, dtype="float64")
    ts_a = np.concatenate([et_ns("20260202", "18:00") + np.arange(600) * NS,
                           et_ns("20260202", "23:50") + np.arange(600) * NS])
    return (make_day_at(a, ts_a, "20260202"),
            day_at("20260203", "00:00", prices_b))


class Globex(HoldFirst):
    session = ("18:00", "16:55")


def test_overnight_position_carries_across_the_file_boundary_to_the_eod_flush():
    # file A holds 18:00-18:10 and 23:50-23:59:59 ET, file B 00:00-00:10 ET:
    # one trading day straight through midnight, flattened at B's flush
    a, b = _overnight(np.full(1200, 100.0), ramp(600))
    s = Globex()
    res = run(s, [a, b])
    assert s.indices == list(range(30))       # 10 + 10 + 10 bars, no reset at midnight
    assert s.positions[1:] == [1] * 29        # still long on every bar, B's too
    t, = res.trades
    assert t.entry_ts == int(a.ts[60]) and t.entry_price == 100.25   # ask of 100.00
    # B's last bar (events 540-599): price 100 + .25 * 9 = 102.25, bid 102.00
    assert t.exit_ts == int(b.ts[599]) and t.exit_tag == "eod"
    assert t.exit_price == 102.00
    assert t.pnl == pytest.approx((102.00 - 100.25) * PV - RT)         # 2.46
    # file A is not a trading-day end (bars after 18:00 begin the NEXT day);
    # P&L is booked once, when the day ends in file B
    assert res.daily_pnl == {"20260203": pytest.approx(2.46)}
    assert res.day_notes == {}             # starts in the evening, ends at the flush


def test_end_of_data_flatten_when_an_overnight_session_never_closes():
    # only file A: the session is still open when the data stops, so the
    # engine flattens at the last event it saw (tag end-of-data)
    prices = np.concatenate([ramp(600), 102.25 - TICK * (np.arange(600) // 60)])
    a, _ = _overnight(prices, np.zeros(1))
    res = run(Globex(), [a])
    t, = res.trades
    # buy fills on event 60: price 100.25, ask 100.50. Last event 1199: second
    # half price 102.25 - .25 * 9 = 100.00, bid 99.75
    assert t.entry_price == 100.50
    assert t.exit_tag == "end-of-data" and t.exit_ts == int(a.ts[1199])
    assert t.exit_price == 99.75
    assert t.pnl == pytest.approx((99.75 - 100.50) * PV - RT)         # -2.54
    assert res.equity[-1] == pytest.approx(50_000 + t.pnl)
    # The evening of 20260202 belongs to the trading day that ENDS on 20260203
    # (CME: 18:00 ET -> 17:00 ET, named by its end date). That day never closed,
    # so its realized P&L is booked there anyway and the day is flagged (it used
    # to be missing from daily_pnl, so Sharpe and the daily stats never saw it).
    assert res.daily_pnl == {"20260203": pytest.approx(-2.54)}
    assert sum(res.daily_pnl.values()) == pytest.approx(sum(x.pnl for x in res.trades))
    note = res.day_notes["20260203"]
    assert "incomplete trading day" in note and "2026-02-02 23:59 ET" in note
    assert "before the session close (16:55 ET)" in note and "end-of-data" in note
    assert list(res.day_notes) == ["20260203"]


# ---------------------------------------- stand-down: bars yes, orders no --

class RecordingBuyer(BuyWhenFlat):
    """BuyWhenFlat that records, at every bar, how many orders are already
    working and the state of the order each buy() hands back at once."""

    def on_start(self):
        super().on_start()
        self.working_at_bar = []
        self.states = []

    def act(self, bar):
        self.working_at_bar.append(len(self._broker.working))
        if self.flat:
            self.states.append(self.buy().state)


class GlobexRecordingBuyer(RecordingBuyer):
    session = ("18:00", "16:55")


def run_bt(strat, days, **kw):
    bt = Backtest(strat, progress=False, **{"prop": None, **kw})
    bt.catalog = MultiDayCatalog(days)
    return bt, bt.run()


def test_after_the_daily_loss_limit_on_bar_continues_but_orders_are_refused():
    # bar 0 (events 0-59) flat at 100 -> buy; it fills on event 60 at the ask
    # 100.25 (price 100), balance 50,000 - .52 = 49,999.48, DLL floor 49,990.
    # Price drops to 95.50 at event 80: equity 49,999.48 + (95.50 - 100.25) * 2
    # = 49,989.98 <= 49,990, so the broker flattens at that event's bid 95.25:
    # realized (95.25 - 100.25) * 2 = -10.00, trade pnl -10.00 - 1.04 = -11.04.
    # Price is back at 100 from event 120, where a re-entry would have won.
    prices = np.concatenate([np.full(80, 100.0), np.full(40, 95.5),
                             np.full(180, 100.0)])
    day = day_at("20260202", "09:30", prices)
    s = RecordingBuyer()
    bt, res = run_bt(s, [day], daily_loss_limit=10.0,
                     daily_limit_mode="kill")
    t, = res.trades
    assert t.exit_tag == "dll" and t.exit_ts == int(day.ts[80])
    assert t.exit_price == 95.25
    assert t.pnl == pytest.approx(-11.04)
    # the strategy is handed all 5 bars (it used to lose bars 1-4, indicators
    # and all), flat from bar 1, and tries to buy on each of them...
    assert s.indices == [0, 1, 2, 3, 4]
    assert s.positions == [0, 0, 0, 0, 0]
    # ...bar 0's order is accepted, every later one is refused outright: handed
    # back already CANCELLED, never queued
    W, C = OrderState.WORKING, OrderState.CANCELLED
    assert s.states == [W, C, C, C, C]
    assert s.working_at_bar == [0, 0, 0, 0, 0]
    assert bt.broker.working == []
    assert res.dll_days == ["20260202"]
    assert len(res.equity) == 5
    assert list(res.equity[1:]) == [pytest.approx(49_988.96)] * 4   # flat all day
    assert res.daily_pnl == {"20260202": pytest.approx(-11.04)}


def test_stand_down_spans_the_file_boundary_and_names_the_trading_day():
    # Overnight session, one trading day: A holds 18:00-18:10 and 23:50-23:59,
    # B holds 00:00-00:10 (20260203). Same numbers as above: buy on bar 0, fills
    # at event 60 (ask 100.25), price 95.50 from event 100: equity 49,989.98 <=
    # 49,990 -> flatten at bid 95.25, pnl -11.04. 30 one-minute bars in all.
    a_prices = np.concatenate([np.full(100, 100.0), np.full(1100, 95.5)])
    a, b = _overnight(a_prices, np.full(600, 100.0))
    s = GlobexRecordingBuyer()
    _, res = run_bt(s, [a, b], daily_loss_limit=10.0,
                    daily_limit_mode="kill")
    t, = res.trades
    assert t.exit_tag == "dll" and t.exit_ts == int(a.ts[100])
    assert t.pnl == pytest.approx(-11.04)
    # the strategy sees every bar, including the 20 after the limit tripped, in
    # BOTH files, and none of its re-entries is accepted
    assert s.indices == list(range(30))
    W, C = OrderState.WORKING, OrderState.CANCELLED
    assert s.states == [W] + [C] * 29
    assert s.working_at_bar == [0] * 30
    # the trip happened on the evening of 20260202, which is part of the
    # trading day that ENDS on 20260203: that is the date it is filed under
    assert res.dll_days == ["20260203"]
    assert res.daily_pnl == {"20260203": pytest.approx(-11.04)}
    assert res.day_notes == {}
    assert len(res.equity) == 30


def test_a_closed_trade_in_an_unfinished_evening_is_booked_with_a_note():
    # Data stops inside the evening of 20260202. The trade is opened and closed
    # before then, so nothing is open at the end - the old engine still dropped
    # it from daily_pnl because the trading day never flushed.
    # ramp: price 100 + .25 per minute. Buy on bar 0 -> event 60, price 100.25,
    # ask 100.50. Sell submitted on bar 3 -> fills on event 240, price 101.00,
    # bid 100.75. pnl = (100.75 - 100.50) * 2 - 1.04 = -0.54.
    class InOut(Probe):
        session = ("18:00", "16:55")

        def act(self, bar):
            if bar.index == 0:
                self.buy()
            elif bar.index == 3:
                self.close_position()

    a, _ = _overnight(ramp(1200), np.zeros(1))
    res = run(InOut(), [a])
    t, = res.trades
    assert t.exit_tag != "end-of-data" and t.pnl == pytest.approx(-0.54)
    assert res.daily_pnl == {"20260203": pytest.approx(-0.54)}
    note = res.day_notes["20260203"]
    assert "incomplete trading day" in note and "flattened" not in note


def test_a_run_that_starts_mid_session_flags_its_first_day():
    # Only B (00:00-00:10 on 20260203): the run starts in the middle of the
    # trading day that began at 18:00 the evening before, which is not loaded.
    b = day_at("20260203", "00:00", ramp(600))
    res = run(Globex(), [b])
    note = res.day_notes["20260203"]
    assert "partial first trading day" in note and "18:00 ET" in note
    assert list(res.day_notes) == ["20260203"]


def test_a_prop_halt_inside_an_evening_is_booked_and_not_called_end_of_data():
    # Same numbers as the prop-halt test below: buy fills on event 60 (ask
    # 100.25), price 95.50 from event 80 -> equity 49,989.98 <= floor 49,990,
    # flatten at bid 95.25, pnl -11.04. It happens in the EVENING of 20260202,
    # so the unfinished day is the one that ends on 20260203.
    prices = np.concatenate([np.full(80, 100.0), np.full(1120, 95.5)])
    a, _ = _overnight(prices, np.zeros(1))
    res = run(Globex(), [a], prop=PropFirmConfig(threshold=10.0, halt_on_breach=True))
    t, = res.trades
    assert t.exit_tag == "prop-halt" and t.pnl == pytest.approx(-11.04)
    assert res.halted_on == "20260202"
    assert res.daily_pnl == {"20260203": pytest.approx(-11.04)}
    note = res.day_notes["20260203"]
    assert "halted on a prop-firm breach" in note and "data ends" not in note
    assert "before the session close" not in note


def test_a_position_carried_past_the_session_end_adds_to_a_day_that_did_flush():
    # flat_at_session_end=False: the 09:45 flush books the day with the position
    # still open (only the entry commission is realized: -0.52), and the
    # end-of-data flatten happens afterwards at the last in-session event 899
    # (price 100 + .25 * 14 = 103.50, bid 103.25). Entry on event 60: ask 100.50.
    # pnl = (103.25 - 100.50) * 2 - 1.04 = 4.46, so 4.98 is added to the day.
    class Carry(HoldFirst):
        session = ("09:30", "09:45")
        flat_at_session_end = False

    day = day_at("20260202", "09:30", ramp(1800))
    res = run(Carry(), [day])
    t, = res.trades
    assert t.exit_tag == "end-of-data" and t.exit_ts == int(day.ts[899])
    assert t.pnl == pytest.approx(4.46)
    assert res.daily_pnl == {"20260202": pytest.approx(4.46)}      # -0.52 + 4.98
    note = res.day_notes["20260202"]
    assert "carried past the session end" in note
    assert "incomplete trading day" not in note


# -------------------------------------------------------------------- news --

def _news_csv(tmp_path, event_ns):
    p = tmp_path / "news.csv"
    p.write_text("dateline_utc_epoch,datetime_utc,datetime_et,weekday_et,"
                 "currency,event,impact\n"
                 f"{event_ns // NS},x,x,Mon,USD,Test,high\n", encoding="utf-8")
    return str(p)


class NewsBuyer(BuyWhenFlat):
    news_filter = True
    news_flatten = True
    news_pre_min = 5.0
    news_post_min = 5.0


def test_news_flatten_then_entries_stay_blocked_until_the_window_closes(tmp_path):
    # event 09:40 ET, +/-5 min -> blocked for bar closes 09:35:00..09:45:00
    # inclusive = bars 4..14. Price rises .25 a minute throughout.
    day = day_at("20260202", "09:30", ramp(1800))
    s = NewsBuyer()
    s.news_csv = _news_csv(tmp_path, et_ns("20260202", "09:40"))
    res = run(s, [day])
    first, second = res.trades
    # trade 1: bought on bar 0, fills on event 60 (price 100.25) at ask 100.50.
    # Bar 4 (ts_end 09:35:00) is the first blocked bar: flatten at its last
    # event 299, price 101.00, bid 100.75 (tag news)
    assert first.entry_price == 100.50
    assert first.exit_tag == "news" and first.exit_ts == int(day.ts[299])
    assert first.exit_price == 100.75
    assert first.pnl == pytest.approx((100.75 - 100.50) * PV - RT)    # -0.54
    # bars 4..14 are blocked, so buy() returns None there; bar 15 (09:46:00) is
    # the first free bar, its order fills on event 960 (price 104.00, ask 104.25)
    assert second.entry_ts == int(day.ts[960]) and second.entry_price == 104.25
    # then eod at event 1799: price 107.25, bid 107.00
    assert second.exit_tag == "eod" and second.exit_price == 107.00
    assert second.pnl == pytest.approx((107.00 - 104.25) * PV - RT)   # 4.46
    # and nothing entered inside the window
    assert not any(first.exit_ts < t.entry_ts < int(day.ts[900]) for t in res.trades)


def test_news_flatten_that_itself_crosses_the_dll_stands_the_day_down(tmp_path):
    """The one path where the daily loss limit is crossed by a fill that
    happens AFTER the broker's resolve_span: the engine's news flatten. Marked
    equity stays just above the floor, the bid fill takes it through, and the
    engine must notice the broker stood down (this was the untested check after
    on_bar)."""
    # The event is at 10:05 ET, so the FIRST blocked bar (ts_end 10:00:00) is
    # also the day's LAST bar: no later bar exists to notice the stand-down at
    # its top-of-loop check, so only the check after on_bar can record it.
    # Price 100 until the last event 1799, which is 95.75.
    # Entry: ask 100.25, balance 49,999.48.
    # Marked at 95.75: 49,999.48 + (95.75 - 100.25) * 2 = 49,990.48, above the
    # DLL floor 50,000 - 10 = 49,990. Flatten at bid 95.50: realized
    # (95.50 - 100.25) * 2 = -9.50, balance 49,989.46 <= 49,990.
    prices = np.full(1800, 100.0)
    prices[1799] = 95.75
    day = day_at("20260202", "09:30", prices)
    s = NewsBuyer()
    s.news_csv = _news_csv(tmp_path, et_ns("20260202", "10:05"))
    res = run(s, [day], daily_loss_limit=10.0, daily_limit_mode="kill")
    t, = res.trades
    assert t.exit_tag == "news" and t.exit_ts == int(day.ts[1799])
    assert t.exit_price == 95.50
    assert t.pnl == pytest.approx(-9.50 - RT)                          # -10.54
    assert res.dll_days == ["20260202"]
    assert len(res.equity) == 30           # no extra point from a skipped bar
    assert res.equity[-1] == pytest.approx(50_000 - 10.54)
    assert res.daily_pnl == {"20260202": pytest.approx(-10.54)}


# ------------------------------------------------------ daily profit target --

def test_daily_profit_target_kill_switch_flattens_on_the_first_tick_and_stands_down():
    # kill mode: equity INCLUDING open P&L, checked on every tick.
    # day 1: flat 100 for bar 0, then +.25 every second from event 60 (price
    # 100.00 on event 60, 100 + .25 * (e - 60) after), then flat at 129.75.
    # Entry on event 60: price 100.00, ask 100.25, balance 49,999.48. Ceiling =
    # 50,000 + 20. Equity at event e = 49,999.48 + (price - 100.25) * 2: event
    # 102 (110.50) is 50,019.98, still short; event 103 (110.75) is 50,020.48 ->
    # flatten there at the bid 110.50 (tag dpt). Realized (110.50 - 100.25) * 2 =
    # 20.50, trade pnl 20.50 - 1.04 = 19.46 (the old bar-close check waited for
    # event 119 and booked 27.46).
    p1 = np.concatenate([np.full(60, 100.0), 100.0 + TICK * np.arange(120),
                         np.full(120, 129.75)])
    d1 = day_at("20260202", "09:30", p1)
    d2 = day_at("20260203", "09:30", np.full(180, 100.0))
    s = BuyWhenFlat()
    res = run(s, [d1, d2], daily_profit_target=20.0,
              daily_limit_mode="kill")
    first, second = res.trades            # one trade a day: no re-entry on day 1
    assert first.exit_tag == "dpt" and first.exit_ts == int(d1.ts[103])
    assert first.exit_price == 110.50
    assert first.pnl == pytest.approx((110.50 - 100.25) * PV - RT)    # 19.46
    assert res.dpt_days == ["20260202"] and res.dll_days == []
    # day 1 has 5 bars (300 events) and day 2 has 3: the strategy is still
    # handed every bar after the stand-down (bars 2-4 of day 1), it just cannot
    # trade them - day 1 still has exactly one trade (its buys are refused)
    assert s.indices == [0, 1, 2, 3, 4, 5, 6, 7]
    # day 2 trades normally: buy on bar 0, fill on event 60 at ask 100.25,
    # eod flatten at bid 99.75
    assert second.entry_ts == int(d2.ts[60])
    assert second.pnl == pytest.approx((99.75 - 100.25) * PV - RT)    # -2.04
    assert res.daily_pnl["20260202"] == pytest.approx(19.46)


# --------------------------------------------------------------- prop halt --

def test_prop_firm_halt_flattens_at_the_breach_event_and_stops_the_run():
    # $10 trailing floor on a $50,000 account: floor 49,990. Entry on event 60
    # (price 100.00, ask 100.25). Price drops to 95.50 on event 80: equity
    # 49,999.48 + (95.50 - 100.25) * 2 = 49,989.98 <= 49,990 -> breach there,
    # flatten at bid 95.25 (tag prop-halt). Days 2 and 3 never run.
    p1 = np.concatenate([np.full(80, 100.0), np.full(100, 95.5)])
    d1 = day_at("20260202", "09:30", p1)
    d2 = day_at("20260203", "09:30", np.full(180, 100.0))
    d3 = day_at("20260204", "09:30", np.full(180, 100.0))
    s = BuyWhenFlat()
    res = run(s, [d1, d2, d3],
              prop=PropFirmConfig(threshold=10.0, halt_on_breach=True))
    t, = res.trades
    assert t.exit_tag == "prop-halt" and t.exit_ts == int(d1.ts[80])
    assert t.exit_price == 95.25
    assert t.pnl == pytest.approx((95.25 - 100.25) * PV - RT)         # -11.04
    assert res.halted_on == "20260202"
    assert res.prop.breached and res.prop.breach_ts == int(d1.ts[80])
    assert list(res.daily_pnl) == ["20260202"]       # later days never booked
    assert s.indices == [0]    # bar 1 (the one containing the breach) never ran
