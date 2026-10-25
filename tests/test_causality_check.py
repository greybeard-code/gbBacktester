"""tools/causality_check.py: delete-the-future and start-later probes, on synthetic multi-day data."""
from __future__ import annotations

import random
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import causality_check as CC  # noqa: E402

from backtester.strategy import Strategy  # noqa: E402

from conftest import MultiDayCatalog, synth_days  # noqa: E402

class Base(Strategy):
    symbol = "MNQ"
    period = "1m"
    session = None
    qty = 1


class CleanSma(Base):
    """Finite-window signal: close vs the mean of the 5 closes before it."""

    def on_bar(self, bar, bars):
        if len(bars) < 7 or not self.flat:
            return
        ref = float(np.mean(bars.closes[-6:-1]))
        if bar.close > ref:
            self.buy_bracket(stop_ticks=6, target_ticks=6)
        elif bar.close < ref:
            self.sell_bracket(stop_ticks=6, target_ticks=6)


class PeeksDayClose(Base):
    """The classic leak: 'is the day going to close above here?' - reads the
    last tick of the whole day file, which does not exist yet at bar close."""

    def on_bar(self, bar, bars):
        if not self.flat:
            return
        day_last = float(self._broker._day.price[-1])
        if day_last > bar.close:
            self.buy_bracket(stop_ticks=6, target_ticks=6)
        elif day_last < bar.close:
            self.sell_bracket(stop_ticks=6, target_ticks=6)


class CountsRows(Base):
    """Signal keyed on the row count since the run started, not the clock."""

    def on_bar(self, bar, bars):
        if self.flat and bar.index % 7 == 3:
            self.buy_bracket(stop_ticks=6, target_ticks=6)


class Unseeded(Base):
    def on_bar(self, bar, bars):
        if self.flat and random.random() < 0.3:
            self.buy_bracket(stop_ticks=6, target_ticks=6)


@pytest.fixture(scope="module")
def cat():
    return MultiDayCatalog(synth_days())


def run(factory, cat, **kw):
    return CC.truncation_invariance(factory, "20260202", "20260206",
                                    base_catalog=cat, **kw)


# ---------------------------------------------------------------- truncation

def test_clean_strategy_passes_truncation(cat):
    rep = run(CleanSma, cat, n_cuts=10, seed=3)
    assert rep.deterministic
    assert rep.full_decisions >= CC.MIN_DECISIONS and not rep.low_power
    assert "LOW POWER" not in CC.format_truncation(rep)
    assert rep.cuts_run == 10
    assert rep.decision_cuts > 0              # the sampler found real decisions
    assert rep.bars_compared > 500
    assert rep.mismatches == []
    assert rep.passed


def test_strategy_reading_the_day_close_fails_truncation(cat):
    rep = run(PeeksDayClose, cat, n_cuts=10, seed=3)
    assert not rep.passed
    kinds = {m.kind for m in rep.mismatches}
    assert "action" in kinds
    # the bars themselves are fine - it is the DECISION that read the future
    assert "bar" not in kinds
    m = min((m for m in rep.mismatches if m.kind == "action"),
            key=lambda m: m.bar_index)
    assert m.full != m.cut
    text = CC.format_truncation(rep)
    assert "FAIL" in text and "[action]" in text


def test_nondeterministic_strategy_is_reported_invalid_not_leaky(cat):
    random.seed(1234)                          # varies call to call afterwards
    rep = run(Unseeded, cat, n_cuts=4, seed=0)
    assert not rep.deterministic and not rep.passed
    assert rep.mismatches == []
    assert "INVALID" in CC.format_truncation(rep)


def test_cut_catalog_deletes_the_future_and_keeps_the_past(cat):
    days = synth_days()
    cut_date = "20260204"
    cut_ts = int(days[2].ts[600])
    c = CC.CutCatalog(cat, cut_date, cut_ts)
    assert c.days("MNQ") == ["20260202", "20260203", "20260204"]
    assert len(c.load_day("MNQ", "20260202")) == len(days[0])
    cut_day = c.load_day("MNQ", cut_date)
    assert len(cut_day) == 601 and int(cut_day.ts[-1]) == cut_ts
    assert c.last_date == cut_date
    # the source catalog is untouched
    assert len(cat.load_day("MNQ", cut_date)) == len(days[2])


def test_cut_catalog_without_a_cut_only_tracks_the_file(cat):
    c = CC.CutCatalog(cat)
    assert c.days("MNQ", "20260203", "20260204") == ["20260203", "20260204"]
    c.load_day("MNQ", "20260203")
    assert c.last_date == "20260203"


def test_trace_records_bars_positions_and_order_actions(cat):
    t = CC.run_traced(CleanSma, "20260202", "20260202", cat, prop=None)
    assert [b.index for b in t.bars] == list(range(len(t.bars)))
    subs = [a for b in t.bars for a in b.actions if a[0] == "submit"]
    assert subs and all(a[7] == (6, 6) for a in subs)     # bracket geometry kept
    assert all(a[2] == "market" for a in subs)
    assert any(b.position != 0 for b in t.bars)


def test_pick_cuts_samples_decisions_and_never_the_last_bar():
    t = CC.Trace()
    for i in range(100):
        acts = [("submit", 1, "market", 1, None, "", False, None)] if i % 10 == 0 else []
        t.add(CC.BarRecord(i, i, "d", (1, 1, 1, 1, 1, 0, 0), 0, acts))
    cuts = CC.pick_cuts(t, 8, seed=5)
    idx = [c.index for c in cuts]
    assert idx == sorted(idx) and len(set(idx)) == len(idx) == 8
    assert 99 not in idx
    assert sum(1 for c in cuts if c.actions) >= 4          # half on decisions
    assert CC.pick_cuts(t, 0) == []
    assert CC.pick_cuts(CC.Trace(), 5) == []


def _trace(rows):
    t = CC.Trace()
    for i, (ohlcv, pos, acts) in enumerate(rows):
        t.add(CC.BarRecord(i, 1000 + i, "d", ohlcv, pos, acts))
    return t


def test_compare_prefix_classifies_what_changed():
    a = (1.0, 2.0, 0.5, 1.5, 10, 4, 6)
    sub = ("submit", 1, "market", 1, None, "", False, None)
    full = _trace([(a, 0, []), (a, 0, [sub]), (a, 1, []), (a, 1, [])])
    same = _trace([(a, 0, []), (a, 0, [sub]), (a, 1, []), (a, 1, [])])
    assert CC.compare_prefix(full, same, 3) == []

    bar = _trace([(a, 0, []), ((1.0, 2.0, 0.5, 1.75, 10, 4, 6), 0, [sub]),
                  (a, 1, []), (a, 1, [])])
    assert [m.kind for m in CC.compare_prefix(full, bar, 3)] == ["bar"]

    act = _trace([(a, 0, []), (a, 0, []), (a, 1, []), (a, 1, [])])
    assert [m.kind for m in CC.compare_prefix(full, act, 3)] == ["action"]

    pos = _trace([(a, 0, []), (a, 0, [sub]), (a, 0, []), (a, 1, [])])
    assert [m.kind for m in CC.compare_prefix(full, pos, 3)] == ["position"]

    short = _trace([(a, 0, []), (a, 0, [sub])])
    assert [m.kind for m in CC.compare_prefix(full, short, 3)] == ["missing", "missing"]

    # bars after the cut are never compared
    later = _trace([(a, 0, []), (a, 0, [sub]), (a, 1, []), (a, 9, [sub])])
    assert CC.compare_prefix(full, later, 2) == []


def test_fill_price_or_commission_drift_is_caught_even_when_timing_is_identical():
    a = (1.0, 2.0, 0.5, 1.5, 10, 4, 6)

    def tr(account):
        t = CC.Trace()
        for i in range(3):
            t.add(CC.BarRecord(i, 1000 + i, "d", a, 1, [], account[i]))
        return t
    base = tr([(0.0, 0.0), (0.0, 100.25), (-1.04, 100.25)])
    assert CC.compare_prefix(base, tr([(0.0, 0.0), (0.0, 100.25), (-1.04, 100.25)]), 2) == []
    moved = CC.compare_prefix(base, tr([(0.0, 0.0), (0.0, 100.50), (-1.04, 100.50)]), 2)
    assert [m.kind for m in moved] == ["account", "account"]


def test_trace_records_realized_and_avg_price(cat):
    t = CC.run_traced(CleanSma, "20260202", "20260202", cat, prop=None)
    assert any(b.account[1] != 0.0 for b in t.bars)        # a position was open
    assert any(b.account[0] != 0.0 for b in t.bars)        # and something closed


def test_float_noise_below_tolerance_is_not_a_leak():
    a = (1.0, 2.0, 0.5, 1.5, 10, 4, 6)
    b = (1.0, 2.0, 0.5, 1.5 + 1e-12, 10, 4, 6)
    assert CC.compare_prefix(_trace([(a, 0, [])]), _trace([(b, 0, [])]), 0) == []


# -------------------------------------------------------------------- warm-up

def test_finite_window_strategy_is_warmup_insensitive(cat):
    rep = CC.warmup_sensitivity(CleanSma, "20260202", "20260206", (1, 2),
                                warm_days=1, base_catalog=cat)
    assert rep.passed
    # it DID disagree early on (shorter history = fewer signals) - that part
    # is inside the warm-up window and must not count
    assert any(v["last_disagreement_ts"] for v in rep.offsets.values())
    assert "PASS" in CC.format_warmup(rep, 1)


def test_row_counting_strategy_fails_warmup(cat):
    rep = CC.warmup_sensitivity(CountsRows, "20260202", "20260206", (1,),
                                warm_days=1, base_catalog=cat)
    assert not rep.passed
    assert rep.offsets[1]["mismatches"] > 0
    assert "FAIL" in CC.format_warmup(rep, 1)


def test_warmup_needs_enough_days(cat):
    with pytest.raises(ValueError, match="more than"):
        CC.warmup_sensitivity(CleanSma, "20260202", "20260203", (3,), 2,
                              base_catalog=cat)


def test_make_factory_applies_overrides_and_rejects_typos():
    f = CC.make_factory(str(Path(__file__).resolve().parent.parent
                            / "strategies" / "ema_cross.py"), {"fast_period": 5})
    assert f().fast_period == 5
    assert f() is not f()
    bad = CC.make_factory(str(Path(__file__).resolve().parent.parent
                              / "strategies" / "ema_cross.py"), {"nope": 1})
    with pytest.raises(AttributeError, match="nope"):
        bad()


# ------------------------------------------------- multi-timeframe (secondary)

FIVE_MIN_NS = 300 * 1_000_000_000


class MtfClean(Base):
    """Reads only COMPLETED 5m bars (what the engine hands out)."""
    secondary_periods = ["5m"]

    def on_bar(self, bar, bars):
        sec = self.secondary("5m")
        if len(sec) < 3 or not self.flat:
            return
        if sec.closes[-1] > sec.closes[-2] and bar.close >= sec.closes[-1]:
            self.buy_bracket(stop_ticks=6, target_ticks=6)
        elif sec.closes[-1] < sec.closes[-2] and bar.close <= sec.closes[-1]:
            self.sell_bracket(stop_ticks=6, target_ticks=6)


class MtfForming(Base):
    """optd's test 4: a 1m signal reading the 5m candle that is still forming.
    The 'forming close' is the last tick before the next 5m edge - already
    printed in the day file, not yet printed at bar close."""
    secondary_periods = ["5m"]

    def on_bar(self, bar, bars):
        sec = self.secondary("5m")
        if len(sec) < 3 or not self.flat:
            return
        day = self._broker._day
        edge = -(-bar.ts // FIVE_MIN_NS) * FIVE_MIN_NS          # ceil to the 5m edge
        k = int(np.searchsorted(day.ts, edge, side="left")) - 1
        forming_close = float(day.price[k])
        if forming_close > sec.closes[-1]:
            self.buy_bracket(stop_ticks=6, target_ticks=6)
        elif forming_close < sec.closes[-1]:
            self.sell_bracket(stop_ticks=6, target_ticks=6)


class MtfSecondaryOrders(Base):
    """Submits from on_secondary_bar, which the engine fires just BEFORE the
    primary on_bar of the bar that closes the 5m candle."""
    secondary_periods = ["5m"]

    def on_secondary_bar(self, bar, bars, period):
        if self.flat and len(bars) >= 2:
            self.buy_bracket(stop_ticks=6, target_ticks=6)


def test_clean_secondary_read_passes(cat):
    rep = run(MtfClean, cat, n_cuts=12, seed=2)
    assert rep.passed and rep.decision_cuts > 0, CC.format_truncation(rep)


def test_reading_the_forming_higher_timeframe_candle_is_caught(cat):
    rep = run(MtfForming, cat, n_cuts=12, seed=2)
    assert not rep.passed
    assert {m.kind for m in rep.mismatches} >= {"action"}
    assert "bar" not in {m.kind for m in rep.mismatches}      # decisions, not bars


def test_orders_from_on_secondary_bar_attach_to_the_next_primary_bar(cat):
    t = CC.run_traced(MtfSecondaryOrders, "20260202", "20260202", cat, prop=None)
    acting = [b for b in t.bars if any(a[0] == "submit" for a in b.actions)]
    assert acting, "the secondary callback never submitted"
    # a 5m candle closes only on a multiple of five minutes
    assert all(b.ts % FIVE_MIN_NS == 0 for b in acting)
    assert t.cur is None and t.pre == []                      # nothing left dangling
    rep = run(MtfSecondaryOrders, cat, n_cuts=8, seed=1)
    assert rep.passed, CC.format_truncation(rep)


class NeverTrades(Base):
    def on_bar(self, bar, bars):
        pass


def test_a_pass_with_no_decisions_is_flagged_low_power(cat):
    rep = run(NeverTrades, cat, n_cuts=6, seed=0)
    assert rep.passed and rep.full_decisions == 0 and rep.low_power
    text = CC.format_truncation(rep)
    assert "PASS" in text and "LOW POWER" in text and "none at all" in text
