"""Tests for the Strategy Apex-risk helpers (size_within_budget, bank_profit)."""
import numpy as np
import pytest

from backtester.account import (Account, PropFirmConfig, PropFirmTracker,
                                TradeRecorder)
from backtester.broker import SimBroker
from backtester.contracts import get_spec
from backtester.orders import BUY, BracketSpec, Order, OrderType
from backtester.strategy import Strategy

from conftest import make_day


def mnq_rig(start=25_000, threshold=1_000, lock=False, prop=True):
    spec = get_spec("MNQ")                 # tick 0.25, point value 2, micro
    account = Account(spec, start)
    recorder = TradeRecorder(spec)
    tracker = PropFirmTracker(PropFirmConfig(threshold=threshold, lock=lock),
                              start) if prop else None
    broker = SimBroker(spec, account, recorder, tracker, slippage_ticks=0.0)
    return broker, account, recorder, tracker


def wired(broker, account, last_price=0.0):
    s = Strategy()
    s._broker = broker
    s._account = account
    s._last_price = last_price
    return s


# --------------------------------------------------------- size_within_budget --- #
def test_headroom_is_equity_minus_floor():
    broker, account, _, _ = mnq_rig()
    s = wired(broker, account)
    assert s.prop_equity(25_000) == pytest.approx(25_000)
    assert s.prop_headroom(25_000) == pytest.approx(1_000)   # floor 24_000


def test_size_within_budget_from_headroom():
    broker, account, _, _ = mnq_rig()
    s = wired(broker, account)
    # headroom 1000 -> budget 500; 25-tick stop = 6.25 pt * $2 = $12.5 -> 40
    assert s.size_within_budget(25, price=25_000) == 40


def test_size_within_budget_shrinks_as_headroom_drops():
    broker, account, _, _ = mnq_rig()
    s = wired(broker, account)
    account.realized = -600.0
    assert s.prop_headroom(25_000) == pytest.approx(400)
    assert s.size_within_budget(25, price=25_000) == 16


def test_size_within_budget_stands_down():
    broker, account, _, _ = mnq_rig()
    s = wired(broker, account)
    account.realized = -980.0                                # headroom 20
    assert s.size_within_budget(25, price=25_000) == 0


def test_size_within_budget_respects_cap():
    broker, account, _, _ = mnq_rig()
    s = wired(broker, account)
    s.max_position = 5
    assert s.size_within_budget(25, price=25_000) == 5


def test_size_within_budget_no_prop_returns_cap():
    broker, account, _, _ = mnq_rig(prop=False)
    s = wired(broker, account)
    assert s.prop_headroom(25_000) == float("inf")
    assert s.size_within_budget(25, price=25_000) == 60     # MNQ apex cap


# --------------------------------------------------------------- bank_profit --- #
def _open_long(broker):
    """Enter 1 MNQ long at ask 100.25 with a 40-tick stop; price runs to 130."""
    day = make_day([100.0, 100.0, 130.0])
    broker.begin_day(day)
    broker.submit(Order(side=BUY, qty=1, type=OrderType.MARKET),
                  BracketSpec(stop_ticks=40))
    broker.resolve_span(0, len(day))


def test_bank_profit_takes_profit_at_target():
    broker, account, recorder, _ = mnq_rig()
    s = wired(broker, account)
    s.eval_target = 50.0
    _open_long(broker)
    assert account.position == 1
    s._last_price = 130.0
    assert s.bank_profit(price=130.0) is True
    exits = [o for o in s.working_orders if o.is_exit and o.tag == "bank-tp"]
    assert len(exits) == 1


def test_bank_profit_ratchets_stop_behind_peak():
    broker, account, recorder, _ = mnq_rig()
    s = wired(broker, account)
    s.eval_target = 100.0                 # arm at 50; do not take profit yet
    _open_long(broker)
    entry = account.avg_price
    peak = recorder.open.mfe              # ≈ (130 - 100.25) * 2 = 59.5
    assert peak == pytest.approx(59.5, abs=0.5)
    s._last_price = 130.0
    assert s.bank_profit(price=130.0) is True
    # stop ratcheted to entry + 0.6 * peak / point_value
    want = entry + (peak * (1 - 0.4)) / (1 * 2)
    assert s.stop_order.price == pytest.approx(want)


def test_bank_profit_no_op_when_not_armed():
    broker, account, _, _ = mnq_rig()
    s = wired(broker, account)
    s.eval_target = 1_000.0               # arm at 500; peak 59.5 < 500
    _open_long(broker)
    base = s.stop_order.price
    s._last_price = 130.0
    assert s.bank_profit(price=130.0) is False
    assert s.stop_order.price == base     # untouched


def test_bank_profit_no_op_when_flat_or_untargeted():
    broker, account, _, _ = mnq_rig()
    s = wired(broker, account)
    s.eval_target = 50.0
    assert s.bank_profit(price=25_000) is False          # flat, no position
    _open_long(broker)
    s.eval_target = None                                  # target disabled
    s._last_price = 130.0
    assert s.bank_profit(price=130.0) is False


def test_example_strategy_loads_and_is_configured():
    from pathlib import Path
    from backtester.loader import load_strategy_class
    path = (Path(__file__).resolve().parent.parent
            / "strategies" / "apex_budget_ema.py")
    s = load_strategy_class(path)()
    assert s.eval_target == 1_500.0
    assert s.max_position == 40
    assert s.risk_max_loss_frac == 0.5
