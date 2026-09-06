"""Crossed quotes (bid > ask) in the reduced cache.

A stale book side pairs a fresh quote with an hours-old one, producing an
inverted record the broker would happily fill both ends of. See
`data._invalidate_crossed` for the census this is guarding.
"""
import numpy as np
import pytest

from backtester.data import _invalidate_crossed, classify_aggressor
from backtester.orders import BUY, SELL, Order, OrderType

from conftest import make_day


def _quad(bid, ask):
    return (np.asarray(bid, dtype="float64"), np.asarray(ask, dtype="float64"),
            np.ones(len(bid), dtype="int64"), np.ones(len(ask), dtype="int64"))


def test_crossed_quote_is_invalidated_in_place():
    bid, ask, bs, asz = _quad([105.0, 99.75], [95.0, 100.25])
    assert _invalidate_crossed(bid, ask, bs, asz) == 1
    assert np.isnan(bid[0]) and np.isnan(ask[0])
    assert bs[0] == 0 and asz[0] == 0
    # the healthy row is untouched
    assert bid[1] == 99.75 and ask[1] == 100.25
    assert bs[1] == 1 and asz[1] == 1


def test_clean_and_missing_quotes_are_left_alone():
    # row 0 normal, row 1 locked (bid == ask, legal), row 2 no quote yet
    bid, ask, bs, asz = _quad([99.75, 100.0, np.nan], [100.25, 100.0, np.nan])
    assert _invalidate_crossed(bid, ask, bs, asz) == 0
    assert bid[0] == 99.75 and ask[1] == 100.0
    assert bs.tolist() == [1, 1, 1]


def test_aggressor_stops_mis_tagging_crossed_events():
    # a crossed quote satisfies BOTH price >= ask and price <= bid, so the
    # sell branch wins and every one of them lands as an aggressor sell
    p = np.array([100.0])
    assert classify_aggressor(p, np.array([95.0]), np.array([105.0]))[0] == -1
    bid, ask, bs, asz = _quad([105.0], [95.0])
    _invalidate_crossed(bid, ask, bs, asz)
    assert classify_aggressor(p, ask, bid)[0] == 0


def test_crossed_quote_fabricates_a_risk_free_round_trip(rig):
    """The defect itself, pinned: sell the (high) bid, cover the (low) ask."""
    broker, account, recorder, _ = rig()
    day = make_day([100.0, 100.0], ask=[95.0, 95.0], bid=[105.0, 105.0])
    broker.begin_day(day)
    broker.submit(Order(side=SELL, qty=1, type=OrderType.MARKET))
    broker.resolve_span(0, 1)
    broker.submit(Order(side=BUY, qty=1, type=OrderType.MARKET))
    broker.resolve_span(1, 2)
    assert broker.fills[0].price == pytest.approx(105.0)   # bid
    assert broker.fills[1].price == pytest.approx(95.0)    # ask, 40 ticks below
    # 10.0 points x $2/point = $20 gross, less the $1.00 round turn
    assert recorder.trades[0].pnl == pytest.approx(19.0)


def test_invalidated_quote_fills_at_the_trade_price(rig):
    """With the guard, both legs resolve to the last trade — edge is zero."""
    broker, account, recorder, _ = rig()
    bid, ask, bs, asz = _quad([105.0, 105.0], [95.0, 95.0])
    _invalidate_crossed(bid, ask, bs, asz)
    day = make_day([100.0, 100.0], ask=ask, bid=bid)
    broker.begin_day(day)
    broker.submit(Order(side=SELL, qty=1, type=OrderType.MARKET))
    broker.resolve_span(0, 1)
    broker.submit(Order(side=BUY, qty=1, type=OrderType.MARKET))
    broker.resolve_span(1, 2)
    assert broker.fills[0].price == pytest.approx(100.0)
    assert broker.fills[1].price == pytest.approx(100.0)
    assert recorder.trades[0].pnl == pytest.approx(-1.0)   # commission only


def test_invalidated_quote_keeps_the_trade_for_bars():
    """Price/volume/ts survive — bar geometry and the parity gates anchored at
    the 18:00 reopen must not move."""
    bid, ask, bs, asz = _quad([105.0], [95.0])
    day = make_day([100.0], ask=ask, bid=bid)
    _invalidate_crossed(bid, ask, bs, asz)
    assert day.price[0] == 100.0
    assert day.volume[0] == 1
    assert len(day) == 1
