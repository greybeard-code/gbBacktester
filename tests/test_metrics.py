"""metrics.compute / format_console, on hand-built Result stand-ins.

Every Sharpe, drawdown, Calmar and profit-factor verdict in this repo comes
from metrics.compute, and it had no direct test. Expected values are worked by
hand in the comments (252 trading days; returns are daily P&L / start balance).
"""
from __future__ import annotations

import math
import re
from types import SimpleNamespace

import numpy as np
import pytest

from backtester import metrics
from backtester.account import PropFirmConfig, PropFirmTracker, Trade

NS = 1_000_000_000
SQRT252 = math.sqrt(252)


def trade(pnl, dur_s=60.0, t0=0):
    return Trade(direction=1, qty=1, entry_ts=t0, exit_ts=t0 + int(dur_s * NS),
                 pnl=pnl)


def result(trades=(), daily=None, equity=None, balance=10_000.0,
           commission=0.0, drawdown=None, prop=None, **kw):
    return SimpleNamespace(
        trades=list(trades), daily_pnl=dict(daily or {}), start_balance=balance,
        equity=np.asarray(equity if equity is not None else [], dtype=float),
        total_commission=commission, drawdown=drawdown, prop=prop,
        strategy_name="S", symbol="MNQ", period="1m", days=["20260105"],
        runtime_s=1.0, dll_days=[], halted_on=None, **kw)


def days(*vals):
    return {f"202601{i + 1:02d}": float(v) for i, v in enumerate(vals)}


# ----------------------------------------------------------- sharpe & friends

def test_sharpe_sortino_calmar_hand_computed():
    # daily P&L 400, -100, 200, -300 on 10,000 -> returns .04 -.01 .02 -.03
    # mean .005; deviations .035 -.015 .015 -.035, sum of squares .0029, ddof=1
    # sd = sqrt(.0029 / 3) = .0310913 -> Sharpe = .005 / .0310913 * sqrt(252)
    # downside returns -.01, -.03: mean -.02, sd = sqrt(.0002 / 1) = .0141421
    # Sortino = .005 / .0141421 * sqrt(252) = sqrt(31.5)
    # Calmar = (.005 * 252) / (500 / 10,000) = 1.26 / .05 = 25.2 with a -$500 DD
    r = result(daily=days(400, -100, 200, -300),
               drawdown=SimpleNamespace(max_dd=-500.0, max_dd_pct=-4.9))
    st = metrics.compute(r)
    assert st["sharpe"] == pytest.approx(2.5528888, rel=1e-6)
    assert st["sortino"] == pytest.approx(math.sqrt(31.5), rel=1e-9)
    assert st["calmar"] == pytest.approx(25.2, rel=1e-9)


def test_sharpe_two_days_is_sqrt_504():
    # returns .01, .03: mean .02, sd = sqrt(2 * .01^2 / 1) = .01 * sqrt(2)
    # Sharpe = .02 / (.01 * sqrt(2)) * sqrt(252) = sqrt(2) * sqrt(252) = sqrt(504)
    st = metrics.compute(result(daily=days(100, 300)))
    assert st["sharpe"] == pytest.approx(math.sqrt(504), rel=1e-9)
    assert math.isnan(st["sortino"])               # no losing day at all


def test_flat_days_are_part_of_the_sharpe_denominator():
    # 100, 0, -50, 0, 200 -> returns .01 0 -.005 0 .02; mean .005
    # deviations .005 -.005 -.01 -.005 .015, sum of squares 4e-4, ddof=1
    # sd = sqrt(4e-4 / 4) = .01 -> Sharpe = .005 / .01 * sqrt(252) = sqrt(252)/2
    st = metrics.compute(result(daily=days(100, 0, -50, 0, 200)))
    assert st["sharpe"] == pytest.approx(SQRT252 / 2, rel=1e-9)
    assert st["trading_days"] == 5
    assert (st["winning_days"], st["losing_days"]) == (2, 1)   # flat days: neither
    assert (st["best_day"], st["worst_day"]) == (200.0, -50.0)
    assert st["avg_day"] == pytest.approx(50.0)
    # dropping the flat days would have given a different (inflated) answer
    no_flat = metrics.compute(result(daily=days(100, -50, 200)))
    assert no_flat["sharpe"] != pytest.approx(st["sharpe"], rel=1e-3)


def test_ratios_are_nan_when_they_cannot_be_defined():
    one = metrics.compute(result(daily=days(100)))
    assert math.isnan(one["sharpe"]) and math.isnan(one["sortino"])
    assert math.isnan(one["calmar"])
    constant = metrics.compute(result(daily=days(100, 100, 100)))
    assert math.isnan(constant["sharpe"])           # sd == 0
    one_loss = metrics.compute(result(daily=days(100, -100, 50)))
    assert not math.isnan(one_loss["sharpe"])
    assert math.isnan(one_loss["sortino"])          # a single downside day has no sd
    none = metrics.compute(result())
    assert math.isnan(none["sharpe"]) and none["trading_days"] == 0


def test_calmar_needs_a_drawdown():
    # no drawdown (max_dd == 0) -> calmar stays nan rather than dividing by zero
    st = metrics.compute(result(daily=days(100, 300),
                                drawdown=SimpleNamespace(max_dd=0.0, max_dd_pct=0.0)))
    assert math.isnan(st["calmar"])


# --------------------------------------------------------------- drawdown ---

def test_tick_drawdown_is_the_headline_and_bar_close_is_kept_alongside():
    # bar-close equity 10,000 10,200 9,800 10,100: peak 10,200, trough 9,800
    # -> -400, and -400 / 10,200 = -3.9216 %
    r = result(equity=[10_000, 10_200, 9_800, 10_100],
               drawdown=SimpleNamespace(max_dd=-650.0, max_dd_pct=-6.4))
    st = metrics.compute(r)
    assert st["max_drawdown"] == -650.0 and st["max_drawdown_pct"] == -6.4
    assert st["max_drawdown_bar"] == pytest.approx(-400.0)
    assert st["max_drawdown_bar_pct"] == pytest.approx(-400 / 10_200 * 100)


def test_without_a_tick_tracker_the_bar_close_curve_is_the_headline():
    st = metrics.compute(result(equity=[10_000, 10_200, 9_800, 10_100]))
    assert st["max_drawdown"] == pytest.approx(-400.0)
    assert st["max_drawdown_pct"] == pytest.approx(-400 / 10_200 * 100)


def test_drawdown_percent_when_the_peak_is_not_positive():
    # equity -100 -300 -200: peak stays -100, dd = 0 -200 -100 -> -200 dollars,
    # but a percent of a non-positive peak is meaningless -> 0.0, not an error
    st = metrics.compute(result(equity=[-100, -300, -200]))
    assert st["max_drawdown"] == pytest.approx(-200.0)
    assert st["max_drawdown_pct"] == 0.0


def test_drawdown_percent_is_of_the_running_peak():
    # 100 -> 50 -> 200: worst is 100 -> 50 = -50 dollars = -50 %
    st = metrics.compute(result(equity=[100, 50, 200]))
    assert st["max_drawdown"] == pytest.approx(-50.0)
    assert st["max_drawdown_pct"] == pytest.approx(-50.0)


def test_no_equity_no_drawdown():
    st = metrics.compute(result())
    assert st["max_drawdown"] == 0.0 and st["max_drawdown_pct"] == 0.0


# ---------------------------------------------------------- trade statistics

def test_trade_statistics_hand_computed():
    # pnl 100 -50 30 -20: wins 130, losses 70 -> PF 130 / 70 = 1.857143
    # net 60, gross = net + commission 8 = 68, win rate 2/4 = 50 %
    # avg trade 15, avg win 65, avg loss -35, largest win 100, loss -50
    r = result(trades=[trade(100), trade(-50), trade(30), trade(-20)],
               commission=8.0)
    st = metrics.compute(r)
    assert st["net_pnl"] == pytest.approx(60.0)
    assert st["gross_pnl"] == pytest.approx(68.0)
    assert st["commission"] == 8.0 and st["total_trades"] == 4
    assert st["profit_factor"] == pytest.approx(130 / 70)
    assert st["win_rate"] == pytest.approx(50.0)
    assert st["avg_trade"] == st["expectancy"] == pytest.approx(15.0)
    assert st["avg_win"] == pytest.approx(65.0)
    assert st["avg_loss"] == pytest.approx(-35.0)
    assert (st["largest_win"], st["largest_loss"]) == (100.0, -50.0)


def test_profit_factor_edge_cases():
    assert metrics.compute(result(trades=[trade(10), trade(20)]))["profit_factor"] == math.inf
    assert metrics.compute(result(trades=[trade(-10), trade(-5)]))["profit_factor"] == 0.0
    assert math.isnan(metrics.compute(result(trades=[trade(0), trade(0)]))["profit_factor"])
    assert math.isnan(metrics.compute(result())["profit_factor"])


def test_break_even_trades_are_not_wins():
    st = metrics.compute(result(trades=[trade(0), trade(0), trade(10), trade(-10)]))
    assert st["win_rate"] == pytest.approx(25.0)          # only the +10
    assert st["avg_win"] == 10.0 and st["avg_loss"] == -10.0


def test_no_trades():
    st = metrics.compute(result())
    assert st["net_pnl"] == 0.0 and st["total_trades"] == 0
    for k in ("win_rate", "avg_trade", "avg_win", "avg_loss", "largest_win",
              "largest_loss", "expectancy", "median_duration_s"):
        assert math.isnan(st[k]), k
    assert st["sub10s_trades"] == 0 and st["sub10s_pnl"] == 0.0


def test_all_winners_has_no_average_loss():
    st = metrics.compute(result(trades=[trade(10), trade(30)]))
    assert math.isnan(st["avg_loss"]) and st["avg_win"] == 20.0


# ----------------------------------------------------------------- durations

def test_sub_10_second_flag_is_strictly_below_ten_seconds():
    # durations 5, 30, 9.9, 120, 10.0 s; pnl 10 -20 5 40 7
    # sub-10s = the 5 s and 9.9 s trades (10.0 s is NOT sub-10): 2 of 5 = 40 %,
    # their pnl 10 + 5 = 15; median of 5, 9.9, 10, 30, 120 = 10.0; min 5
    r = result(trades=[trade(10, 5), trade(-20, 30), trade(5, 9.9),
                       trade(40, 120), trade(7, 10.0)])
    st = metrics.compute(r)
    assert st["sub10s_trades"] == 2
    assert st["sub10s_pct"] == pytest.approx(40.0)
    assert st["sub10s_pnl"] == pytest.approx(15.0)
    assert st["median_duration_s"] == pytest.approx(10.0)
    assert st["min_duration_s"] == pytest.approx(5.0)


# --------------------------------------------------------------- consistency

def test_consistency_is_best_day_over_total_trade_profit():
    # best day 300 over trade net 350 = 85.714 %. The daily P&L sums to 300,
    # not 350 (an open-trade carry), so a version that divided by the daily sum
    # would give 100 % and be caught
    r = result(trades=[trade(200), trade(150)], daily=days(100, 300, -100))
    assert metrics.compute(r)["consistency_pct"] == pytest.approx(300 / 350 * 100)


def test_consistency_is_nan_without_a_profit():
    r = result(trades=[trade(-100), trade(20)], daily=days(100, -50))
    assert math.isnan(metrics.compute(r)["consistency_pct"])


# ---------------------------------------------------------------------- prop

def test_prop_fields_pass_through_and_infinite_headroom_is_nan():
    tr = PropFirmTracker(PropFirmConfig(threshold=2000.0), 50_000.0)
    # never fed an equity value: min_headroom is still +inf
    st = metrics.compute(result(prop=tr))
    assert st["prop_breached"] is False and math.isnan(st["prop_min_headroom"])
    assert st["prop_threshold"] == 2000.0
    # equity 50,000 -> 51,000 -> 49,500: peak 51,000, floor 49,000 (trail),
    # lock disabled by default buffer 100: floor = min(49,000, 50,100) = 49,000;
    # lowest headroom is 49,500 - 49,000 = 500 at the last value
    tr.update_series(np.array([50_000.0, 51_000.0, 49_500.0]),
                     np.array([1, 2, 3]))
    st = metrics.compute(result(prop=tr))
    assert st["prop_min_headroom"] == pytest.approx(500.0)
    assert st["prop_breached"] is False and st["prop_breach_ts"] is None
    tr.update_series(np.array([48_900.0]), np.array([4]))      # through the floor
    st = metrics.compute(result(prop=tr))
    assert st["prop_breached"] is True and st["prop_breach_ts"] == 4


def test_no_prop_no_prop_fields():
    assert "prop_breached" not in metrics.compute(result())


# ----------------------------------------------------------------- console

def test_console_is_nan_safe_and_omits_duration_without_trades():
    r = result(daily=days(100, 300))
    text = metrics.format_console(r, metrics.compute(r))
    assert "Net P&L:        $0.00" in text
    # undefined dollar amounts AND ratios print n/a, never "$nan" or a bare "nan"
    assert "avg win n/a" in text and "$nan" not in text
    assert "win rate n/a" in text and "profit factor n/a" in text
    assert "Sortino: n/a" in text and "Calmar: n/a" in text
    assert not re.search(r"nan", text, re.I)
    assert "Duration:" not in text


def test_console_prints_defined_ratios_as_numbers():
    # one +$25 winner, one +$25 winning day: win rate 100.0%, nothing lost so the
    # profit factor is infinite (printed as inf, not n/a); one day -> Sharpe n/a
    r = result(trades=[trade(25, 5)], daily=days(25))
    text = metrics.format_console(r, metrics.compute(r))
    assert "win rate 100.0%" in text
    assert "profit factor inf" in text
    assert "Sharpe:         n/a" in text
    # two trades, +30 / -10: win rate 50.0%, profit factor 30 / 10 = 3.00
    r2 = result(trades=[trade(30), trade(-10)], daily=days(30, -10))
    t2 = metrics.format_console(r2, metrics.compute(r2))
    assert "win rate 50.0%" in t2 and "profit factor 3.00" in t2
    assert "Sharpe:         n/a" not in t2


def test_console_lists_day_notes_and_omits_them_when_there_are_none():
    r = result(trades=[trade(25, 5)], daily=days(25))
    assert "Note " not in metrics.format_console(r, metrics.compute(r))
    r.day_notes = {"20260204": "incomplete trading day: b", "20260203": "partial: a"}
    text = metrics.format_console(r, metrics.compute(r))
    # one line per note, in date order
    assert text.index("Note 20260203:  partial: a") < \
        text.index("Note 20260204:  incomplete trading day: b")


def test_console_reports_dll_halt_and_prop_status():
    tr = PropFirmTracker(PropFirmConfig(threshold=2000.0), 50_000.0)
    tr.update_series(np.array([50_000.0, 47_000.0]), np.array([1, 2]))
    r = result(trades=[trade(25, 5)], daily=days(25), prop=tr)
    r.dll_days = ["20260105", "20260106"]
    r.halted_on = "20260106"
    text = metrics.format_console(r, metrics.compute(r))
    assert "BREACHED trailing threshold" in text
    assert "Daily loss limit hit on 2 day(s): 20260105, 20260106" in text
    assert "HALTED on 20260106" in text
    assert "<10s: 1 trades" in text
