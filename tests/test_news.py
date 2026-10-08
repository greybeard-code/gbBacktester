"""High-impact news filter: calendar window math + strategy entry gating."""
import csv

import numpy as np

from backtester.account import Account, TradeRecorder
from backtester.broker import SimBroker
from backtester.contracts import ContractSpec
from backtester.news import NewsCalendar
from backtester.strategy import Strategy

from conftest import make_day

NS = 1_000_000_000
# an event at epoch 1_000_000 s (arbitrary), in ns
EVENT_S = 1_000_000
EVENT_NS = EVENT_S * NS


def _cal(*epochs_s):
    return NewsCalendar(np.array(epochs_s, dtype="int64") * NS)


def test_blocked_window_pre_post():
    cal = _cal(EVENT_S)
    pre, post = 5 * 60, 5 * 60         # +/- 5 minutes
    # exactly at the event
    assert cal.blocked(EVENT_NS, pre, post) is True
    # 4 min before / after -> blocked; 6 min -> clear
    assert cal.blocked(EVENT_NS - 4 * 60 * NS, pre, post) is True
    assert cal.blocked(EVENT_NS + 4 * 60 * NS, pre, post) is True
    assert cal.blocked(EVENT_NS - 6 * 60 * NS, pre, post) is False
    assert cal.blocked(EVENT_NS + 6 * 60 * NS, pre, post) is False


def test_blocked_window_edges_inclusive():
    cal = _cal(EVENT_S)
    pre, post = 300, 120              # asymmetric windows
    assert cal.blocked(EVENT_NS - 300 * NS, pre, post) is True    # edge, pre
    assert cal.blocked(EVENT_NS - 301 * NS, pre, post) is False
    assert cal.blocked(EVENT_NS + 120 * NS, pre, post) is True    # edge, post
    assert cal.blocked(EVENT_NS + 121 * NS, pre, post) is False


def test_empty_calendar_never_blocks():
    cal = NewsCalendar(np.array([], dtype="int64"))
    assert cal.blocked(EVENT_NS, 300, 300) is False


def test_nearest_of_many():
    cal = _cal(EVENT_S - 3600, EVENT_S, EVENT_S + 3600)   # 3 events, 1h apart
    # between events, well outside any 5-min window -> clear
    assert cal.blocked(EVENT_NS + 1800 * NS, 300, 300) is False
    # near the third event -> blocked
    assert cal.blocked((EVENT_S + 3600) * NS + 60 * NS, 300, 300) is True


def test_load_filters_currency(tmp_path):
    p = tmp_path / "news.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["dateline_utc_epoch", "datetime_utc", "datetime_et",
                    "weekday_et", "currency", "event", "impact"])
        w.writerow([EVENT_S, "", "", "Wed", "USD", "CPI m/m", "high"])
        w.writerow([EVENT_S + 10, "", "", "Wed", "EUR", "German CPI", "high"])
    assert len(NewsCalendar.load(p)) == 2                    # all
    assert len(NewsCalendar.load(p, currencies=("USD",))) == 1
    assert len(NewsCalendar.load(p, currencies=("usd",))) == 1   # case-insensitive
    assert len(NewsCalendar.load(p, currencies=("JPY",))) == 0


def _strat_with_news(cal):
    spec = ContractSpec("TEST", 0.25, 2.0, 1.00)
    account = Account(spec, 50_000.0)
    broker = SimBroker(spec, account, TradeRecorder(spec))
    s = Strategy()
    s._broker, s._account = broker, account
    s.news_filter = True
    s._news = cal
    day = make_day([100.0, 100.0, 100.0])
    broker.begin_day(day)
    return s, account, broker


def test_entry_blocked_during_window():
    cal = _cal(EVENT_S)
    s, account, broker = _strat_with_news(cal)
    s._now_ts = EVENT_NS                             # inside the window
    assert s.news_blocked() is True
    assert s.buy() is None                           # entry suppressed
    broker.resolve_span(0, 1)
    assert account.position == 0                     # nothing filled


def test_entry_allowed_outside_window():
    cal = _cal(EVENT_S)
    s, account, broker = _strat_with_news(cal)
    s._now_ts = EVENT_NS + 10 * 60 * NS              # 10 min after -> clear
    assert s.news_blocked() is False
    assert s.buy() is not None
    broker.resolve_span(0, 1)
    assert account.position == 1


def test_filter_off_by_default():
    s = Strategy()
    assert s.news_filter is False
    assert s._news is None
    assert s.news_blocked(EVENT_NS) is False         # no calendar -> never blocks
