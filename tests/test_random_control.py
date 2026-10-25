"""tools/random_control.py: coin-flip direction control + paired noise-floor verdict."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import random_control as RC  # noqa: E402

from backtester import Backtest  # noqa: E402
from backtester.strategy import Strategy  # noqa: E402

from conftest import MultiDayCatalog, synth_days  # noqa: E402


# ------------------------------------------------------------------- scoring

def _days(n=60):
    return [f"2026{(i // 28) + 1:02d}{(i % 28) + 1:02d}" for i in range(n)]


def _daily(values):
    return dict(zip(_days(len(values)), values))


def _controls(mean, sd, k=8, n=60, seed=0):
    rng = np.random.default_rng(seed)
    return [_daily(rng.normal(mean, sd, n)) for _ in range(k)]


def test_clear_edge_over_a_clean_control_passes():
    ctrl = _controls(0.0, 50.0)
    base = _daily(np.random.default_rng(1).normal(40.0, 50.0, 60))
    s = RC.compare_to_control(base, ctrl)
    assert s["verdict"].startswith("PASS"), s["verdict"]
    assert s["t"] > 2 and s["edge_per_day"] > 0
    assert s["days"] == 60 and s["n_controls"] == 8


def test_gap_inside_the_noise_floor_is_inconclusive_not_a_win():
    ctrl = _controls(10.0, 50.0)
    base = _daily(np.random.default_rng(2).normal(12.0, 50.0, 60))
    s = RC.compare_to_control(base, ctrl)
    assert s["verdict"].startswith("INCONCLUSIVE"), s["verdict"]
    assert abs(s["t"]) < 2


def test_control_that_beats_the_strategy_is_a_fail():
    ctrl = _controls(60.0, 20.0)
    base = _daily(np.random.default_rng(3).normal(5.0, 20.0, 60))
    s = RC.compare_to_control(base, ctrl)
    assert s["verdict"].startswith("FAIL"), s["verdict"]
    assert s["t"] < -2 and s["beaten_by"] == 8 and s["rank_p"] == 1.0


def test_beating_the_control_while_losing_money_is_losing():
    ctrl = _controls(-60.0, 10.0)
    base = _daily(np.random.default_rng(4).normal(-5.0, 10.0, 60))
    s = RC.compare_to_control(base, ctrl)
    assert s["verdict"].startswith("LOSING"), s["verdict"]


def test_headline_mostly_surviving_random_direction_is_weak():
    ctrl = _controls(30.0, 10.0)
    base = _daily(np.random.default_rng(5).normal(40.0, 10.0, 60))
    s = RC.compare_to_control(base, ctrl)
    assert s["verdict"].startswith("WEAK"), s["verdict"]
    assert 0.5 <= s["share_kept"] < 1.0


def test_too_few_controls_gives_no_verdict_from_the_noise_floor():
    s = RC.compare_to_control(_daily([1.0] * 10), _controls(0, 1, k=3, n=10))
    assert s["verdict"].startswith("TOO FEW")


def test_explicit_nets_override_daily_sums():
    ctrl = _controls(0.0, 50.0)
    base = _daily(np.random.default_rng(1).normal(40.0, 50.0, 60))
    s = RC.compare_to_control(base, ctrl, base_net=1234.5,
                              control_nets=[1.0] * 8)
    assert s["base_net"] == 1234.5 and s["control_mean_net"] == 1.0


def test_runs_on_different_calendars_are_scored_on_the_overlap():
    base = _daily(np.random.default_rng(1).normal(40.0, 50.0, 60))
    ctrl = [dict(list(d.items())[:50]) for d in _controls(0.0, 50.0)]
    assert RC.compare_to_control(base, ctrl)["days"] == 50
    with pytest.raises(ValueError, match="fewer than 2"):
        RC.compare_to_control({"20260101": 1.0}, _controls(0, 1, k=6, n=1))


# ---------------------------------------------------------------- randomiser

class Base(Strategy):
    symbol = "MNQ"
    period = "1m"
    session = None
    qty = 1


class LongOnly(Base):
    """Always buys when flat, exits on a symmetric bracket."""

    def on_bar(self, bar, bars):
        if self.flat:
            self.buy_bracket(stop_ticks=8, target_ticks=8)


class SarRev(Base):
    """Plain-buy/sell reversal shape: while holding, close then re-enter on
    the same bar (the position is still open when buy() is called)."""

    def on_bar(self, bar, bars):
        if bar.index == 0:
            self.buy()
        elif bar.index % 5 == 0 and self.position != 0:
            self.close_position()
            self.sell() if self.position > 0 else self.buy()


class ScaleIn(Base):
    def on_bar(self, bar, bars):
        if bar.index == 0:
            self.buy()
        elif bar.index == 3:
            self.buy()                         # add while long: not an entry from flat


class LimitEntry(Base):
    def on_bar(self, bar, bars):
        if bar.index == 0:
            self.buy_limit(bar.close - 100.0)  # never fills; bypasses _enter


@pytest.fixture(scope="module")
def cat():
    return MultiDayCatalog(synth_days(n_days=4))


def run(strat_cls, cat, seed=None, p_flip=0.5):
    bt = Backtest(strat_cls(), prop=None, progress=False,
                  start="20260202", end="20260205")
    bt.catalog = cat
    rz = None
    if seed is not None:
        rz = RC.DirectionRandomizer(seed, p_flip).attach(bt.strategy, bt.broker)
    return bt.run(), rz


def key(res):
    return [(t.direction, t.entry_ts, t.exit_ts, round(t.pnl, 6)) for t in res.trades]


def test_zero_flip_probability_is_behaviour_neutral(cat):
    plain, _ = run(LongOnly, cat)
    wrapped, rz = run(LongOnly, cat, seed=7, p_flip=0.0)
    assert key(plain) == key(wrapped) and len(plain.trades) > 20
    # an order placed on a day's last bar is cancelled at the flush, so
    # entries can exceed round trips by a few - never the reverse
    assert rz.flipped == 0 and 0 <= rz.entries - len(plain.trades) <= 4


def test_fair_coin_flips_about_half_and_both_directions_trade(cat):
    plain, _ = run(LongOnly, cat)
    assert {t.direction for t in plain.trades} == {1}
    res, rz = run(LongOnly, cat, seed=3)
    dirs = [t.direction for t in res.trades]
    assert set(dirs) == {1, -1}
    # LongOnly buys every entry, so each short trade is one flip (a flipped
    # order cancelled unfilled at a day flush adds a few more)
    assert dirs.count(-1) > 0
    assert 0 <= rz.flipped - dirs.count(-1) <= rz.entries - len(dirs)
    assert 0.3 < rz.flipped / rz.entries < 0.7
    assert rz.not_flat == 0 and rz.bypassed == 0


def test_same_seed_repeats_and_different_seeds_differ(cat):
    a, _ = run(LongOnly, cat, seed=5)
    b, _ = run(LongOnly, cat, seed=5)
    c, _ = run(LongOnly, cat, seed=6)
    assert key(a) == key(b)
    assert [t.direction for t in a.trades] != [t.direction for t in c.trades]


def test_flip_preserves_bracket_geometry(cat):
    res, _ = run(LongOnly, cat, seed=3)
    pv = res.spec.point_value
    shorts = [t for t in res.trades if t.direction == -1]
    assert shorts
    for t in shorts:
        # an 8-tick (2 pt) bracket either way: exit within one stop/target
        # distance of entry (+ a little for gap fills), never on the wrong side
        # of the geometry
        assert abs(t.exit_price - t.entry_price) <= 2.0 + 0.5 + 1e-9, t


def test_reversal_after_a_pending_close_counts_as_flat_and_is_randomised(cat):
    res, rz = run(SarRev, cat, seed=11, p_flip=1.0)
    assert rz.not_flat == 0                    # close_position() made it effectively flat
    assert rz.flipped == rz.entries > 1
    # with p_flip = 1 every entry is inverted: the first buy() becomes a short
    assert res.trades[0].direction == -1


def test_entries_with_a_position_open_are_counted_not_randomised(cat):
    _, rz = run(ScaleIn, cat, seed=2, p_flip=1.0)
    assert rz.entries == 2 and rz.flipped == 1 and rz.not_flat == 1


def test_limit_entries_are_counted_as_bypassed(cat):
    _, rz = run(LimitEntry, cat, seed=2)
    assert rz.bypassed == 1 and rz.entries == 0 and rz.flipped == 0


# ------------------------------------------------ method-level (clean) control

class GoStrategy(Base):
    """Direction decided by a method, the `_go(direction, bar)` shape:
    the strategy records its own belief in self.dir, and enters on every 10th bar
    while flat. Real signal direction alternates."""

    def on_start(self):
        self.dir = 0
        self.calls = []

    def _go(self, direction, bar):
        self.calls.append(direction)
        self.dir = direction
        (self.buy_bracket if direction > 0 else self.sell_bracket)(
            stop_ticks=8, target_ticks=8)

    def on_bar(self, bar, bars):
        if self.flat and bar.index % 10 == 0:
            self._go(1 if (bar.index // 10) % 2 == 0 else -1, bar)


def run_method(cat, seed, p_flip=0.5):
    import random as _r
    bt = Backtest(GoStrategy(), prop=None, progress=False,
                  start="20260202", end="20260205")
    bt.catalog = cat
    rz = RC.DirectionRandomizer(seed, p_flip).attach_method(bt.strategy, "_go")
    res = bt.run()
    return res, rz, bt.strategy


def test_method_mode_feeds_the_strategy_the_coin_not_the_signal(cat):
    import random as _r
    _, rz, strat = run_method(cat, seed=4)
    rng = _r.Random(4)
    # exactly the stream rng.choice((1, -1)) yields, one draw per call: this is
    # what lets the tool reproduce the 2026-10-16 audit's control
    assert strat.calls == [rng.choice((1, -1)) for _ in strat.calls]
    assert set(strat.calls) == {1, -1} and rz.entries == len(strat.calls)
    assert rz.method == "_go" and rz.not_flat == 0 and rz.bypassed == 0
    assert 0 < rz.flipped < rz.entries


def test_method_mode_strategy_state_follows_the_coin(cat):
    res, _, strat = run_method(cat, seed=4)
    # the strategy's own belief and the broker's position agree on every entry
    # (the property the _enter-level control cannot give)
    assert strat.calls and set(t.direction for t in res.trades) == {1, -1}
    assert [t.direction for t in res.trades][:3] == strat.calls[:3]


def test_method_mode_zero_flip_is_neutral_and_typo_is_rejected(cat):
    plain = Backtest(GoStrategy(), prop=None, progress=False,
                     start="20260202", end="20260205")
    plain.catalog = cat
    res0, rz, _ = run_method(cat, seed=1, p_flip=0.0)
    assert key(plain.run()) == key(res0) and rz.flipped == 0
    bt = Backtest(GoStrategy(), prop=None, progress=False)
    with pytest.raises(AttributeError, match="no method"):
        RC.DirectionRandomizer(1).attach_method(bt.strategy, "_nope")


def test_method_mode_end_to_end_and_report_label(cat, monkeypatch):
    def fake_build(path, overrides, bt_kwargs, prop_threshold):
        bt = Backtest(GoStrategy(), prop=None, progress=False,
                      start="20260202", end="20260205")
        bt.catalog = cat
        return bt
    monkeypatch.setattr(RC, "_build", fake_build)
    payload = {"strategy": "unused", "overrides": {}, "bt_kwargs": {},
               "prop_threshold": None, "direction_method": "_go"}
    res = RC.run_control(payload, seeds=[1, 2, 3, 4, 5], jobs=1)
    assert res["direction_method"] == "_go"
    text = RC.format_report(res)
    assert "_go() called" in text and "LESS of the headline" not in text


# --------------------------------------------------------------- end to end

def test_run_control_end_to_end_on_synthetic_data(cat, monkeypatch):
    def fake_build(path, overrides, bt_kwargs, prop_threshold):
        bt = Backtest(LongOnly(), prop=None, progress=False,
                      start="20260202", end="20260205")
        bt.catalog = cat
        return bt
    monkeypatch.setattr(RC, "_build", fake_build)
    payload = {"strategy": "unused", "overrides": {}, "bt_kwargs": {},
               "prop_threshold": None}
    res = RC.run_control(payload, seeds=[1, 2, 3, 4, 5, 6], jobs=1)
    assert res["base"]["counts"] is None
    assert len(res["controls"]) == 6
    assert all(r["counts"]["flipped"] > 0 for r in res["controls"])
    assert res["score"]["days"] == 4
    text = RC.format_report(res)
    assert "as shipped" in text and "random direction, seed 6" in text
    # the side-flip caveat is printed, and says which way it errs
    assert "flatters the strategy" in text and "confirmed with --direction-method" in text
    assert "paired by day over 4 days" in text
    assert res["score"]["verdict"].split(" ")[0] in {
        "PASS", "WEAK", "FAIL", "LOSING", "INCONCLUSIVE"}


def test_report_warns_when_many_entries_escape_the_randomiser():
    base = {"net": 10.0, "sharpe": 1.0, "trades": 10, "pf": 1.1, "seed": None,
            "daily": _daily([1.0] * 5), "counts": None}
    ctrl = [{"net": 5.0, "sharpe": 0.5, "trades": 10, "pf": 1.0, "seed": s,
             "daily": _daily([float(s)] * 5),
             "counts": {"entries": 100, "flipped": 40, "not_flat": 30, "bypassed": 0}}
            for s in range(6)]
    score = RC.compare_to_control(base["daily"], [c["daily"] for c in ctrl])
    text = RC.format_report({"base": base, "controls": ctrl, "score": score})
    assert "WARNING" in text
