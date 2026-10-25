"""tools/orphan_exits.py: exit orders that FILL against a trade they were not
submitted for. Hand-computed, MNQ tick 0.25, $2/point, $0.52 a side, one event a
second, quotes a tick either side (ask = price + .25, bid = price - .25).

Since 2026-10-21 the broker cancels a position's legs when the position goes
(the reduce-only rule), so on the real broker the tool reports
nothing. To prove the tool still DETECTS an orphan, the scenarios that used to
create one are re-run with the broker's stale-exit test switched off, which is the
broker as it was before the fix.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import orphan_exits as OX  # noqa: E402

from backtester.broker import SimBroker  # noqa: E402
from backtester.engine import Backtest  # noqa: E402
from backtester.strategy import Strategy  # noqa: E402

from conftest import NS, MultiDayCatalog, et_ns, make_day_at  # noqa: E402


def bt_for(strategy, prices):
    ts = et_ns("20260202", "09:30") + np.arange(len(prices), dtype="int64") * NS
    bt = Backtest(strategy, progress=False, prop=None)
    bt.catalog = MultiDayCatalog([make_day_at(prices, ts, "20260202")])
    return bt


class Scripted(Strategy):
    symbol = "MNQ"
    period = "1m"
    session = None
    qty = 1

    def __init__(self, script):
        self.script = script

    def on_bar(self, bar, bars):
        act = self.script.get(bar.index)
        if act == "bracket":
            self.buy_bracket(stop_ticks=8, target_ticks=400)     # stop 2 points under the fill
        elif act == "buy":
            self.buy()
        elif act == "close":
            self.close_position()


@pytest.fixture
def pre_fix_broker(monkeypatch):
    """The broker before 2026-10-21: it never judges an exit order stale."""
    monkeypatch.setattr(SimBroker, "_stale_exit", lambda self, o: False)


CASE_A = np.concatenate([np.full(130, 100.0), np.full(110, 95.0)])
CASE_B = np.concatenate([np.full(190, 100.0), np.full(110, 95.0)])


# ------------------------------------------------ the tool still sees an orphan --

def test_a_leftover_stop_that_opens_a_position_from_flat_is_case_a(pre_fix_broker):
    # long from event 60 (ask 100.25), stop 98.25; closed by hand: sell fills on event
    # 120 at the bid 99.75 (-2.04). Price falls to 95.00 on event 130: the leftover
    # stop sells at the bid 94.75, opening a short, flattened at the end of the day at
    # the ask 95.25: (94.75 - 95.25) * 2 - 1.04 = -2.04
    orphans, res = OX.find_orphans(bt_for(Scripted({0: "bracket", 1: "close"}), CASE_A))
    o, = orphans
    assert o["case"] == "A" and o["tag"] == "stop" and o["position_before"] == 0
    assert o["trade"].direction == -1 and o["trade"].pnl == pytest.approx(-2.04)
    assert o["trade"].exit_tag == "eod"
    assert len(res.trades) == 2


def test_a_leftover_stop_that_closes_a_LATER_trade_is_case_b(pre_fix_broker):
    # trade A (bar 0 bracket) is closed by hand on bar 1. Trade B is a plain market buy
    # on bar 2, filled on event 180 at the ask 100.25. Price falls to 95.00 on event
    # 190: A's old stop (98.25) sells B's contract at the bid 94.75:
    # (94.75 - 100.25) * 2 - 1.04 = -12.04, tagged 'stop'
    orphans, res = OX.find_orphans(bt_for(Scripted({0: "bracket", 1: "close", 2: "buy"}), CASE_B))
    o, = orphans
    assert o["case"] == "B" and o["position_before"] == 1
    assert o["trade"].direction == 1 and o["trade"].pnl == pytest.approx(-12.04)
    assert o["trade"].exit_tag == "stop"


# ------------------------------------------- and the real broker leaves none --

def test_on_the_real_broker_the_same_scenarios_have_no_orphans():
    # case A: the legs are cancelled when the hand-close flattens (2 of them), so the
    # price drop at event 130 sells nothing: one trade, -2.04
    orphans, res = OX.find_orphans(bt_for(Scripted({0: "bracket", 1: "close"}), CASE_A))
    assert orphans == [] and [round(t.pnl, 2) for t in res.trades] == [-2.04]
    assert res.exits_dropped == 2
    # case B: trade B survives to the end-of-day flatten at the bid 94.75, -12.04
    orphans, res = OX.find_orphans(bt_for(Scripted({0: "bracket", 1: "close", 2: "buy"}), CASE_B))
    assert orphans == []
    assert [(t.exit_tag, round(t.pnl, 2)) for t in res.trades] == [("exit", -2.04), ("eod", -12.04)]


def test_a_clean_strategy_has_no_orphans_and_the_patch_is_removed_afterwards():
    before = (SimBroker.submit, SimBroker._fill)
    # bracket entry, left alone: the stop/target legs act on their own trade
    prices = np.concatenate([np.full(130, 100.0), np.full(110, 95.0)])
    orphans, res = OX.find_orphans(bt_for(Scripted({0: "bracket"}), prices))
    assert orphans == [] and len(res.trades) == 1
    assert res.trades[0].exit_tag == "stop" and res.exits_dropped == 0   # its target went with it (OCO)
    assert (SimBroker.submit, SimBroker._fill) == before          # nothing left patched


def test_the_patch_is_removed_even_if_the_run_blows_up():
    before = (SimBroker.submit, SimBroker._fill)

    class Boom(Scripted):
        def on_bar(self, bar, bars):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        OX.find_orphans(bt_for(Boom({}), np.full(200, 100.0)))
    assert (SimBroker.submit, SimBroker._fill) == before
