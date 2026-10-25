"""Renko brick-state carry across day-file boundaries.

Day files are ET calendar days, but an overnight session keeps trading
through midnight ET with no real gap there. build_renko_bars alone resets
its anchor at the start of every call; Catalog.load_bars_sequence must
carry the still-forming brick's anchor/direction across days UNLESS the
actual tick gap between them is a genuine reset (> RENKO_RESET_GAP_NS).
"""
import numpy as np
import pytest

from backtester.data import (
    Catalog, RENKO_RESET_GAP_NS, build_renko_bars,
)
from backtester.strategy import BarSpec

from conftest import make_day


# ---------------- build_renko_bars carry-in/out ----------------------------

def test_default_carry_matches_fresh_start():
    """No carry args -> identical to today's fresh-start behavior."""
    prices = [100 + 0.25 * i for i in range(10)]
    day = make_day(prices)
    bars = build_renko_bars(day, brick=1.0, trend=0.5)
    assert list(bars.close) == [100.5, 101.0, 101.5, 102.0]
    assert bars.end_anchor == pytest.approx(102.0)   # last emitted close
    assert bars.end_dir == 1


def test_carry_in_continues_uptrend_without_resetting():
    """Continuing an uptrend: no 'first bar of session' (body=T) bar prints;
    the next bar is a normal with-trend close from the carried anchor."""
    day = make_day([100.75, 101.25])     # strictly clears each +T threshold
    bars = build_renko_bars(day, brick=1.0, trend=0.5,
                            carry_anchor=100.0, carry_dir=1)
    assert list(bars.close) == [100.5, 101.0]
    assert bars.open[0] == pytest.approx(99.5)       # B-T below carried anchor
    assert bars.end_anchor == pytest.approx(101.0)
    assert bars.end_dir == 1


def test_carry_in_can_reverse_immediately():
    day = make_day([98.0])               # breaks the 2B-T=1.5 reversal down
    bars = build_renko_bars(day, brick=1.0, trend=0.5,
                            carry_anchor=100.0, carry_dir=1)
    assert list(bars.close) == [98.5]
    assert bars.end_dir == -1


def test_carry_in_no_signal_yet_returns_unclosed_state():
    """A day that never breaks a threshold still reports the (unclosed)
    carried state back out unchanged, for the next day to pick up."""
    day = make_day([100.1, 100.2])       # never reaches +/- T = 0.5
    bars = build_renko_bars(day, brick=1.0, trend=0.5,
                            carry_anchor=100.0, carry_dir=1)
    assert len(bars) == 0
    assert bars.end_anchor == pytest.approx(100.0)
    assert bars.end_dir == 1


# ---------------- Catalog.load_bars_sequence --------------------------------

class _Recorder:
    """Wraps Catalog to log every build_renko_bars call's carry-in args."""


def test_sequence_carries_across_continuous_days(tmp_path):
    cat = Catalog(data_root=tmp_path, cache_root=tmp_path / "cache")
    spec = BarSpec("renko", brick_ticks=4, trend_ticks=2)   # brick=1.0, trend=0.5 @ tick .25
    tick = 0.25

    day1 = make_day([100 + 0.25 * i for i in range(10)],
                    start_ts=1_000_000_000_000)             # closes at 102.0, dir +1
    # day2 starts immediately (no real gap) and continues the uptrend
    day2 = make_day([102.75, 103.25], start_ts=2_000_000_000_000)

    bars_list = cat.load_bars_sequence("TEST", ["20260101", "20260102"], spec,
                                       tick, days=[day1, day2])
    b1, b2 = bars_list
    assert list(b1.close) == [100.5, 101.0, 101.5, 102.0]
    # day2 continues from 102.0 with NO fresh-start (no T-only body=trend bar)
    assert list(b2.close) == [102.5, 103.0]
    assert b2.open[0] == pytest.approx(101.5)        # B-T below carried close


def test_sequence_resets_on_genuine_gap(tmp_path):
    cat = Catalog(data_root=tmp_path, cache_root=tmp_path / "cache")
    spec = BarSpec("renko", brick_ticks=4, trend_ticks=2)
    tick = 0.25

    day1 = make_day([100 + 0.25 * i for i in range(10)], start_ts=1_000_000_000_000)
    # day2 starts long after RENKO_RESET_GAP_NS -> must reset fresh (body=T)
    gap_start = int(day1.ts[-1]) + RENKO_RESET_GAP_NS + 1_000_000_000
    day2 = make_day([300.0, 300.75], start_ts=gap_start)

    b1, b2 = cat.load_bars_sequence("TEST", ["20260101", "20260102"], spec,
                                    tick, days=[day1, day2])
    assert list(b1.close) == [100.5, 101.0, 101.5, 102.0]
    # fresh reset: first bar closes exactly T from the new anchor (300.0)
    assert list(b2.close) == [300.5]
    assert b2.open[0] == pytest.approx(300.0)


def test_sequence_cache_hit_matches_uncached(tmp_path):
    """A second call with the identical day sequence must hit the cache and
    return numerically identical bars (round-trip through parquet)."""
    cat = Catalog(data_root=tmp_path, cache_root=tmp_path / "cache")
    spec = BarSpec("renko", brick_ticks=4, trend_ticks=2)
    tick = 0.25
    day1 = make_day([100 + 0.25 * i for i in range(10)], start_ts=1_000_000_000_000)
    day2 = make_day([102.75, 103.25], start_ts=2_000_000_000_000)

    first = cat.load_bars_sequence("TEST", ["20260101", "20260102"], spec,
                                   tick, days=[day1, day2])
    second = cat.load_bars_sequence("TEST", ["20260101", "20260102"], spec,
                                    tick, days=[day1, day2])
    for a, b in zip(first, second):
        assert np.array_equal(a.close, b.close)
        assert np.array_equal(a.open, b.open)


def test_sequence_empty_day_carries_through(tmp_path):
    """A zero-tick day (e.g. a holiday) is skipped without resetting the
    carried state for the day after it."""
    cat = Catalog(data_root=tmp_path, cache_root=tmp_path / "cache")
    spec = BarSpec("renko", brick_ticks=4, trend_ticks=2)
    tick = 0.25
    day1 = make_day([100 + 0.25 * i for i in range(10)], start_ts=1_000_000_000_000)
    from backtester.data import DayL1
    empty = DayL1("20260102", *([np.array([], dtype=t) for t in
                                 ("int64", "float64", "int64", "float64",
                                  "float64", "int64", "int64", "int8")]))
    # starts 5s after day1's last tick (1_009_000_000_000) — well within the
    # 30-min reset threshold, so the empty day in between must not matter
    day3 = make_day([102.75, 103.25], start_ts=1_014_000_000_000)

    bars_list = cat.load_bars_sequence(
        "TEST", ["20260101", "20260102", "20260103"], spec, tick,
        days=[day1, empty, day3])
    b1, b2, b3 = bars_list
    assert len(b2) == 0
    assert list(b3.close) == [102.5, 103.0]     # carried through the empty day
    assert b3.open[0] == pytest.approx(101.5)


def test_non_renko_spec_delegates_to_load_bars(tmp_path, monkeypatch):
    cat = Catalog(data_root=tmp_path, cache_root=tmp_path / "cache")
    spec = BarSpec("time", seconds=60)
    day1 = make_day([100.0, 100.25], start_ts=1_000_000_000_000)
    calls = []
    orig = cat.load_bars
    def spy(symbol, date, spec, tick_size, day=None):
        calls.append(date)
        return orig(symbol, date, spec, tick_size, day)
    monkeypatch.setattr(cat, "load_bars", spy)
    cat.load_bars_sequence("TEST", ["20260101"], spec, 0.25, days=[day1])
    assert calls == ["20260101"]


# ---------------- the forming brick's volume and range cross the boundary ----

def _vol_day(ticks, vols, start_ts):
    """prices 4000 + tick * 0.25, one unit-volume-by-default event a second"""
    import dataclasses
    d = make_day([4000.0 + 0.25 * t for t in ticks], start_ts=start_ts)
    return dataclasses.replace(d, volume=np.asarray(vols, dtype="int64"))


SPEC84 = BarSpec("renko", brick_ticks=8, trend_ticks=4)        # brick 2.00, trend 1.00


def test_sequence_straddling_brick_keeps_its_volume_and_range(tmp_path):
    # the hand case from test_bar_invariants: bar 1 = events 5..8 = volume 4,
    # low 3999.50 (the dip is in the FIRST file), high 4002.00
    day1 = _vol_day([0, 1, 2, 3, 4, 5, -2], [1] * 7, 1_000_000_000_000)
    day2 = _vol_day([6, 8, 9], [1] * 3, 1_007_000_000_000)          # no gap between them
    cat = Catalog(data_root=tmp_path, cache_root=tmp_path / "cache")
    b1, b2 = cat.load_bars_sequence("TEST", ["20260101", "20260102"], SPEC84, 0.25,
                                    days=[day1, day2])
    assert len(b1) == 1 and len(b2) == 1
    assert b2.volume[0] == 4 and b2.low[0] == 3999.5 and b2.high[0] == 4002.0
    assert (b2.open[0], b2.close[0]) == (4000.0, 4002.0)


def test_a_cache_hit_returns_the_same_bars_and_the_stored_carry_for_the_next_day(tmp_path, monkeypatch):
    import backtester.data as D
    day1 = _vol_day([0, 1, 2, 3, 4, 5, -2], [1] * 7, 1_000_000_000_000)
    day2 = _vol_day([6, 8, 9], [1] * 3, 1_007_000_000_000)
    day3 = _vol_day([10, 12, 13], [1] * 3, 1_010_000_000_000)      # bar 2 straddles 2 -> 3
    cat = Catalog(data_root=tmp_path, cache_root=tmp_path / "cache")
    args = ("TEST", ["20260101", "20260102", "20260103"], SPEC84, 0.25)
    first = cat.load_bars_sequence(*args, days=[day1, day2, day3])
    # the second pass must be served from the parquet cache: building is an error
    monkeypatch.setattr(D, "build_renko_bars", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("rebuilt instead of reading the cache")))
    second = cat.load_bars_sequence(*args, days=[day1, day2, day3])
    for a, b in zip(first, second):
        for f in ("ts_end", "open", "high", "low", "close", "volume", "buy_volume", "sell_volume"):
            assert np.array_equal(getattr(a, f), getattr(b, f)), f
    assert [len(b) for b in second] == [1, 1, 1]
    # day 2's end_state came out of the metadata of a CACHE HIT and fed day 3
    assert second[2].volume[0] == first[2].volume[0]


def test_a_day_cached_under_another_carry_in_state_is_rebuilt(tmp_path):
    day2 = _vol_day([6, 8, 9], [1] * 3, 1_007_000_000_000)
    cat = Catalog(data_root=tmp_path, cache_root=tmp_path / "cache")
    carry_a, carry_b = (2, 0, 0, 4001.25, 3999.5), (10, 0, 0, 4001.25, 3995.0)
    args = ("TEST", "20260102", SPEC84, 0.25, day2, 4001.0, 1)
    va = cat._load_renko_cached(*args, carry_a)[0].volume[0]
    vb = cat._load_renko_cached(*args, carry_b)[0]
    assert va == 4 and vb.volume[0] == 12 and vb.low[0] == 3995.0     # not the cached A
    again = cat._load_renko_cached(*args, carry_a)[0]
    assert again.volume[0] == 4 and again.low[0] == 3999.5            # and back again


def test_a_genuine_gap_between_files_drops_the_carried_state(tmp_path):
    # file 1 leaves a forming brick with 6 volume. File 2 starts 31 minutes later:
    # a fresh start, so its first bar has NOTHING of file 1 in it
    day1 = _vol_day([0, 1], [3, 3], 1_000_000_000_000)
    gap_start = int(day1.ts[-1]) + RENKO_RESET_GAP_NS + 1_000_000_000
    day2 = _vol_day([0, 5], [7, 11], gap_start)                    # fresh anchor 4000.00
    cat = Catalog(data_root=tmp_path, cache_root=tmp_path / "cache")
    b1, b2 = cat.load_bars_sequence("TEST", ["20260101", "20260102"], SPEC84, 0.25,
                                    days=[day1, day2])
    assert len(b1) == 0 and len(b2) == 1
    assert b2.volume[0] == 7                     # not 13
    assert b2.low[0] == 4000.0 and b2.high[0] == 4001.0
