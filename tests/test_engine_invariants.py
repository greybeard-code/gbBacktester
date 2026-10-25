"""Engine invariants under random play (plan task 20, part 2).

hypothesis draws a tick stream (synth_stream.streams) and a RANDOM SCRIPT of
orders (markets, brackets, resting limits, closes, sizes big enough to hit the
position cap), runs it through the real engine on time, tick and renko bars, with
and without a daily limit (gate / kill), slippage and order latency, and checks
the identities that must hold whatever the strategy does:

- the final balance is the start balance plus the sum of the trade P&Ls, and the
  daily P&L adds up to the same number (the engine ends flat);
- commission is the per-side rate times the contracts filled, three ways;
- no position ever exceeds the cap, and the running position ends flat;
- every trade has a sane clock, MAE <= 0 <= MFE, prices on the tick grid;
- with latency, no order fills before it reached the exchange.

The plan also asked for "MAE <= pnl <= MFE". That is NOT an invariant here and is
deliberately not asserted: a stop or target fills on the TRIGGER event (at the
quote), and MAE/MFE mark only the events before it, so a gap through a target
makes pnl exceed MFE and a gap through a stop makes pnl fall below MAE. Two
hand-computed cases below pin that, so nobody "fixes" the test into a wrong one.
"""
from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from backtester.engine import Backtest
from backtester.strategy import Strategy

from conftest import NS, MultiDayCatalog, et_ns, make_day_at
from synth_stream import EXPLORE, PROFILE, TICK, streams

SLOW = dict(PROFILE, max_examples=min(PROFILE['max_examples'], 40) if not EXPLORE else 150)

ACTIONS = st.one_of(
    st.just(("noop",)),
    st.tuples(st.sampled_from(("buy", "sell")), st.integers(1, 80)),   # 80 > the 60-micro cap
    st.tuples(st.sampled_from(("buy_b", "sell_b")), st.integers(1, 5),
              st.integers(4, 40), st.integers(4, 40)),
    st.tuples(st.sampled_from(("buy_l", "sell_l")), st.integers(1, 5), st.integers(1, 20)),
    st.just(("close",)),
)


class Scripted(Strategy):
    symbol = "MNQ"
    session = None
    qty = 1

    def __init__(self, script, period):
        self.script = script
        self.period = period

    def on_bar(self, bar, bars):
        act = self.script[bar.index % len(self.script)]
        k = act[0]
        if k == "buy":
            self.buy(act[1])
        elif k == "sell":
            self.sell(act[1])
        elif k == "buy_b":
            self.buy_bracket(act[1], stop_ticks=act[2], target_ticks=act[3])
        elif k == "sell_b":
            self.sell_bracket(act[1], stop_ticks=act[2], target_ticks=act[3])
        elif k == "buy_l":
            self.buy_limit(bar.close - act[2] * TICK, act[1])
        elif k == "sell_l":
            self.sell_limit(bar.close + act[2] * TICK, act[1])
        elif k == "close":
            self.close_position()


def run(d, script, period, **kw):
    bt = Backtest(Scripted(script, period), progress=False, prop=None, **kw)
    bt.catalog = MultiDayCatalog([d])
    return bt, bt.run()


def check_run(bt, res, latency_ms=0.0, check_cap=True):
    acct, spec = bt.account, bt.spec
    pnl = sum(t.pnl for t in res.trades)
    assert acct.position == 0, "the engine must end flat"
    assert acct.balance == pytest.approx(bt.start_balance + pnl, abs=1e-6)
    assert sum(res.daily_pnl.values()) == pytest.approx(pnl, abs=1e-6)
    # commission: per side per contract, three independent ways
    filled = sum(f.qty for f in bt.broker.fills)
    assert res.total_commission == pytest.approx(spec.commission_side * filled, abs=1e-6)
    assert res.total_commission == pytest.approx(sum(f.commission for f in bt.broker.fills), abs=1e-6)
    assert sum(t.commission for t in res.trades) == pytest.approx(res.total_commission, abs=1e-6)
    # position: back to flat, never above the cap, and every exit fill only reduces
    cap, pos = bt.broker.max_position, 0
    for f in bt.broker.fills:
        assert f.qty > 0
        new = pos + f.side * f.qty
        if f.order.is_exit:
            # an exit order only REDUCES: it never opens, adds to or reverses a position
            assert abs(new) < abs(pos) and (new == 0 or (new > 0) == (pos > 0)), \
                f"exit fill {f.order.tag!r} took the position {pos} -> {new}"
        pos = new
        if check_cap:
            assert abs(pos) <= cap, f"position {pos} exceeds the cap {cap}"
    assert pos == 0
    # the trade list is the round trips: one per return to flat, and a reversal
    # (long straight to short) closes one trade and opens the next
    trips, pos = 0, 0
    for f in bt.broker.fills:
        new = pos + f.side * f.qty
        if pos != 0 and (new == 0 or (new > 0) != (pos > 0)):
            trips += 1
        pos = new
    assert trips == len(res.trades), (trips, len(res.trades))
    for f in bt.broker.fills:
        # a trade's entry/exit price is a quantity-weighted AVERAGE of its fills
        # and so is off the grid after a scale-in; each FILL is on it
        assert (f.price / TICK) == pytest.approx(round(f.price / TICK), abs=1e-6)
    for t in res.trades:
        assert t.entry_ts <= t.exit_ts
        assert t.mae <= 0.0 <= t.mfe
        assert t.qty > 0
    if latency_ms:
        for f in bt.broker.fills:
            a = f.order.active_ts
            assert a is None or f.ts >= a, "an order filled before it reached the exchange"
    # the equity curve ends where the balance does
    if len(res.equity):
        assert res.equity[-1] == pytest.approx(acct.balance, abs=1e-6)


@settings(**SLOW)
@given(d=streams(min_n=60, max_n=300),
       script=st.lists(ACTIONS, min_size=3, max_size=12),
       period=st.sampled_from(("5s", "7t", "r6-3")),
       slippage=st.sampled_from((0, 1, 2)))
def test_accounting_identities_hold_for_any_strategy(d, script, period, slippage):
    bt, res = run(d, script, period, slippage_ticks=slippage)
    check_run(bt, res, check_cap=True)


@settings(**SLOW)
@given(d=streams(min_n=60, max_n=300),
       script=st.lists(ACTIONS, min_size=3, max_size=12),
       period=st.sampled_from(("5s", "7t")),
       mode=st.sampled_from(("gate", "kill")),
       limit=st.integers(5, 400),
       target=st.one_of(st.none(), st.integers(5, 400)))
def test_accounting_identities_hold_under_daily_limits(d, script, period, mode, limit, target):
    bt, res = run(d, script, period, daily_limit_mode=mode, daily_loss_limit=float(limit),
                  daily_profit_target=None if target is None else float(target))
    check_run(bt, res, check_cap=True)
    assert res.daily_limit_mode == mode


@settings(**SLOW)
@given(d=streams(min_n=60, max_n=300),
       script=st.lists(ACTIONS, min_size=3, max_size=12),
       period=st.sampled_from(("5s", "7t", "r6-3")),
       latency=st.sampled_from((1.0, 50.0, 750.0, 5000.0)))
def test_accounting_identities_and_arrival_times_hold_with_latency(d, script, period, latency):
    bt, res = run(d, script, period, latency_ms=latency)
    check_run(bt, res, latency_ms=latency, check_cap=True)


@settings(**SLOW)
@given(d=streams(min_n=60, max_n=300), script=st.lists(ACTIONS, min_size=3, max_size=12))
def test_explicit_zero_latency_is_bit_identical_to_the_unit_test_default_of_zero(d, script):
    # conftest zeroes the engine default (100 ms) for unit tests; the real default is
    # pinned in tests/test_default_latency.py. This guards "off" = the instant-fill path.
    _, off = run(d, script, "5s")
    _, zero = run(d, script, "5s", latency_ms=0.0)
    assert [(t.entry_ts, t.exit_ts, t.pnl) for t in off.trades] == \
           [(t.entry_ts, t.exit_ts, t.pnl) for t in zero.trades]


@settings(**SLOW)
@given(d=streams(min_n=60, max_n=300),
       script=st.lists(st.one_of(st.just(("noop",)),
                                 st.tuples(st.sampled_from(("buy", "sell")), st.integers(1, 80))),
                       min_size=3, max_size=12),
       period=st.sampled_from(("5s", "7t", "r6-3")))
def test_the_position_cap_holds_for_plain_market_play(d, script, period):
    bt, res = run(d, script, period)
    check_run(bt, res, check_cap=True)


# FIXED 2026-10-21 (exit legs only reduce): this was a strict xfail.
# Order.is_exit says exit orders "may flatten, never reverse", but the broker did not
# enforce it and the position-cap clamp skips them, so a bracket leg left working by
# the strategy's own close_position or reversal later OPENED a naked position
# (positions went 61 and -70 against a cap of 60 in generated runs). The broker now
# cancels such legs (the reduce-only rule NT8 applies to a position's stops/targets).
def test_a_leftover_bracket_leg_must_not_open_a_new_position():
    # long 1 from event 60 (ask 100.25) with a 2-point stop (98.25) and a far
    # target; the strategy closes it with its own close_position on bar 1 (fills event 120, bid
    # 99.75, pnl (99.75 - 100.25) * 2 - 1.04 = -2.04). The position is gone, so the
    # broker cancels both legs; at event 130 the price drops to 95.00 through where the
    # stop was and nothing happens (it used to sell 1 more at 94.75: a short nobody
    # asked for, closed by the end-of-day flatten).
    prices = np.concatenate([np.full(130, 100.0), np.full(110, 95.0)])
    # (the script cycles over the bars: 0 bracket, 1 close, 2 idle; the 4th bar's
    # bracket is sent after the last event and never fills)
    bt, res = run(_hand_day(prices), [("buy_b", 1, 8, 400), ("close",), ("noop",)], "1m")
    assert [round(t.pnl, 2) for t in res.trades] == [-2.04]
    assert sum(f.side * f.qty for f in bt.broker.fills) == 0
    assert len(bt.broker.fills) == 2
    assert res.exits_dropped == 2                    # the stop and the target


# ------------------------------- why "MAE <= pnl <= MFE" is not an invariant ---

def _hand_day(prices):
    ts = et_ns("20260202", "09:30") + np.arange(len(prices), dtype="int64") * NS
    return make_day_at(prices, ts, "20260202")


def test_a_gap_through_a_target_makes_pnl_exceed_mfe():
    # long from event 60 (ask 100.25); 100.00 until a jump to 105.00 on event 90,
    # through the 4-point target (104.25): fills at the BID 104.75. MFE marks only
    # events 60..89 (price 100: -$0.50), so it is 0; pnl = (104.75 - 100.25) * 2 - 1.04
    prices = np.concatenate([np.full(90, 100.0), np.full(90, 105.0)])

    bt, res = run(_hand_day(prices), [("buy_b", 1, 40, 16), ("noop",)], "1m")
    t = res.trades[0]
    assert t.exit_price == 104.75 and t.pnl == pytest.approx(7.96)
    assert t.mfe == 0.0 and t.pnl > t.mfe


def test_a_gap_through_a_stop_makes_pnl_fall_below_mae():
    # long from event 60 (ask 100.25), 4-tick stop at 99.25; price 100 then a drop
    # to 90.00 on event 90 (the trigger event): fills at the bid 89.75.
    # MAE marks events 60..89 only (price 100: -$0.50): pnl = (89.75 - 100.25) * 2 - 1.04
    prices = np.concatenate([np.full(90, 100.0), np.full(90, 90.0)])
    bt, res = run(_hand_day(prices), [("buy_b", 1, 4, 400), ("noop",)], "1m")
    t = res.trades[0]
    assert t.exit_price == 89.75 and t.pnl == pytest.approx(-22.04)
    assert t.mae == pytest.approx(-0.5) and t.pnl < t.mae


# ---------------------------------------------------------- real data (opt-in) ---

@pytest.mark.data
def test_accounting_identities_hold_on_real_days():
    from backtester.data import Catalog
    from backtester.loader import load_strategy
    from pathlib import Path
    cat = Catalog()
    try:
        days = cat.days("MNQ", "20260504", "20260506")
    except FileNotFoundError as e:
        pytest.skip(f"no tick data: {str(e).splitlines()[0]}")
    if not days:
        pytest.skip("no MNQ day files in 20260504..20260506")
    repo = Path(__file__).resolve().parent.parent
    for mode in (None, "gate", "kill"):
        strat = load_strategy(repo / "strategies" / "ema_cross.py")
        kw = {} if mode is None else dict(daily_limit_mode=mode, daily_loss_limit=150.0)
        bt = Backtest(strat, start="20260504", end="20260506", progress=False, prop=None, **kw)
        res = bt.run()
        check_run(bt, res)
        assert len(res.trades) > 0
