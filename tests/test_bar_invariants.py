"""Property tests for every bar builder (plan task 20, part 1).

hypothesis draws synthetic tick streams (random walks on the tick grid with
duplicate timestamps, sub-millisecond bursts, multi-second pauses and the odd
30+ minute gap that resets renko-family bars) and every builder must satisfy the
invariants below. The SAME checks run on real days under `@pytest.mark.data`.

The invariants were found by measuring, not assumed (the plan says to check the
spec before asserting). What does NOT hold is as informative as what does:
- renko spans are not contiguous: a forming brick abandoned at a 30-minute gap
  leaves a hole, and a bar cut by a gap has a body shorter than the brick;
- renko/TBars/Wave H/L may lie outside the traded range (synthetic open, Heikin-
  Ashi), so "OHLC inside the span's prices" is asserted for time/tick bars only;
- a bar that straddles a day-file boundary must come out the same whether the
  stream is built whole or as two files with the carry passed across (the
  acceptance test for every carrying bar type; renko and Saber used to lose part
  of such a bar until BARS_VERSION 10).
"""
from __future__ import annotations

import dataclasses

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from backtester.data import (Catalog, DayL1, build_bars,
                             build_renko_bars, build_saber_bars,
                             build_orenko_bars, build_tbar_bars, build_wave_bars)
from backtester.strategy import parse_barspec

from conftest import et_ns, make_day_at
from synth_stream import (GAPS_MS, PROFILE, RESET_GAP_NS, TICK, slice_day,  # noqa: F401
                          streams)

SPECS = ("5s", "60s", "7t", "r8-4", "r10-3", "r20", "s16-4", "tb40", "tb120", "w40", "w120", "o2-6-12", "o4-0-8")
TICK_GRID_KINDS = ("renko", "saber", "tbars", "wave", "orenko")
CARRY_STATE_KINDS = ("saber", "tbars", "wave", "orenko")       # end_state[-3:] = forming volumes


def span_sums(d: DayL1, b):
    vol = np.array([d.volume[x:y].sum() for x, y in zip(b.i0, b.i1)], dtype="int64")
    buy = np.array([np.where(d.aggr[x:y] == 1, d.volume[x:y], 0).sum()
                    for x, y in zip(b.i0, b.i1)], dtype="int64")
    sell = np.array([np.where(d.aggr[x:y] == -1, d.volume[x:y], 0).sum()
                     for x, y in zip(b.i0, b.i1)], dtype="int64")
    return vol, buy, sell


def gap_between(d: DayL1, j: int) -> bool:
    """True if a 30+ minute trade gap sits between events j-1 and j."""
    return 0 < j < len(d) and int(d.ts[j] - d.ts[j - 1]) > RESET_GAP_NS


# ---------------------------------------------------------------- the checks --

def check_bars(d: DayL1, spec_s: str) -> None:
    spec = parse_barspec(spec_s)
    b = build_bars(d, spec, TICK)
    n, m = len(d), len(b)
    if m == 0:
        return                                    # nothing completed: nothing to check
    arrays = (b.ts_end, b.open, b.high, b.low, b.close, b.volume, b.buy_volume,
              b.sell_volume, b.i0, b.i1)
    assert all(len(a) == m for a in arrays)
    # spans: inside the day, ordered, and never overlapping (an order must not
    # be able to fill in two bars)
    assert (b.i0 >= 0).all() and (b.i0 <= b.i1).all() and (b.i1 <= n).all()
    assert (np.diff(b.i0) >= 0).all() and (b.i0[1:] >= b.i1[:-1]).all()
    assert (np.diff(b.ts_end) >= 0).all()
    # every bar's volume is exactly its span's volume, split by aggressor
    vol, buy, sell = span_sums(d, b)
    assert (b.volume == vol).all() and (b.buy_volume == buy).all()
    assert (b.sell_volume == sell).all()
    assert (b.high >= b.low).all()
    eps = 1e-9
    assert ((b.open <= b.high + eps) & (b.open >= b.low - eps)
            & (b.close <= b.high + eps) & (b.close >= b.low - eps)).all()

    if spec.kind in ("time", "tick"):
        assert int(b.i0[0]) == 0 and (b.i0[1:] == b.i1[:-1]).all() and int(b.i1[-1]) == n
        for k in range(m):
            seg = d.price[int(b.i0[k]):int(b.i1[k])]
            assert (b.open[k], b.close[k]) == (seg[0], seg[-1])
            assert (b.high[k], b.low[k]) == (seg.max(), seg.min())
        if spec.kind == "time":
            period = spec.seconds * 1_000_000_000
            for k in range(m):
                t = d.ts[int(b.i0[k]):int(b.i1[k])]
                assert (t < b.ts_end[k]).all() and (t >= b.ts_end[k] - period).all()
        else:
            sizes = (b.i1 - b.i0)
            assert (sizes[:-1] == spec.ticks).all() and 0 < sizes[-1] <= spec.ticks

    if spec.kind in TICK_GRID_KINDS:
        # a 30-minute trade gap RESETS these builders: no bar may span one
        for k in range(m):
            assert not any(gap_between(d, j) for j in range(int(b.i0[k]) + 1, int(b.i1[k]))), (
                f"{spec.kind} bar {k} spans a 30-minute gap")
        for arr in (b.open, b.high, b.low, b.close):
            q = arr / TICK
            assert np.allclose(q, np.round(q), atol=1e-6)

    if spec.kind in CARRY_STATE_KINDS:
        assert int(b.i0[0]) == 0 and (b.i0[1:] == b.i1[:-1]).all()
        # the bar still forming at the end carries its volume out: nothing is lost
        fv, fb, fs = b.end_state[-3:]
        assert int(b.volume.sum()) + int(fv) == int(d.volume.sum())
        assert int(fv) == int(d.volume[int(b.i1[-1]):].sum())
        assert int(b.buy_volume.sum()) + int(fb) == int(np.where(d.aggr == 1, d.volume, 0).sum())
        assert int(b.sell_volume.sum()) + int(fs) == int(np.where(d.aggr == -1, d.volume, 0).sum())
    if spec.kind == "saber":
        # Saber H/L are real traded extremes: inside the day's range
        assert b.high.max() <= d.price.max() + eps and b.low.min() >= d.price.min() - eps

    if spec.kind == "renko":
        brick = spec.brick_ticks
        for k in range(1, m):
            if b.i0[k] > b.i1[k - 1]:
                # a hole in the spans = an abandoned forming brick = a reset gap
                lo, hi = int(b.i1[k - 1]), int(b.i0[k])
                assert any(gap_between(d, j) for j in range(max(lo, 1), hi + 1)), (
                    "renko spans have a hole with no 30-minute gap in it")
        if int(b.i0[0]) > 0:
            assert any(gap_between(d, j) for j in range(1, int(b.i0[0]) + 1))
        body = np.abs(b.close - b.open) / TICK
        assert (body <= brick + 1e-6).all()
        for k in np.flatnonzero(body == 0):
            # a doji is only ever a forming brick cut off by a reset gap whose
            # last price equals its open
            assert gap_between(d, int(b.i1[k])), f"zero-body renko bar {k} not cut by a gap"
        assert ((b.high >= np.maximum(b.open, b.close) - eps)
                & (b.low <= np.minimum(b.open, b.close) + eps)).all()
        for k in range(1, m):
            i0, i1 = int(b.i0[k]), int(b.i1[k])
            interior = (i0 == b.i1[k - 1] and not gap_between(d, i0) and not gap_between(d, i1))
            if interior:
                assert abs(body[k] - brick) < 1e-6, (
                    f"interior renko bar {k} has body {body[k]} ticks, brick is {brick}")


@pytest.mark.parametrize("spec_s", SPECS)
@settings(**PROFILE)
@given(d=streams())
def test_bar_invariants_hold_on_random_tick_streams(spec_s, d):
    check_bars(d, spec_s)


# ------------------------------------------------- building in two halves ----
# Day files are ET calendar days and an overnight session runs straight through
# midnight, so the engine builds each file separately and threads a carry across
# the boundary (Catalog.load_bars_sequence). If the carry is right, the two
# halves joined must equal one build over the whole stream.

FIELDS = ("ts_end", "open", "high", "low", "close", "volume", "buy_volume", "sell_volume")


def build_chain(spec_s, parts):
    """Build consecutive files the way Catalog.load_bars_sequence does: each file
    gets the previous file's carry (no reset gap between them in these tests)."""
    out, prev = [], None
    for part in parts:
        b = _build_next(spec_s, part, prev)
        out.append(b)
        prev = b
    return out


def _build_next(spec_s, d, prev):
    sp = parse_barspec(spec_s)
    if sp.kind == "renko":
        b, t = sp.brick_ticks * TICK, sp.trend_ticks * TICK
        if prev is None:
            return build_renko_bars(d, b, t)
        return build_renko_bars(d, b, t, carry_anchor=prev.end_anchor,
                                carry_dir=prev.end_dir, carry_state=prev.end_state)
    carry = None if prev is None else prev.end_state
    if sp.kind == "saber":
        return build_saber_bars(d, sp.bar_ticks, sp.offset_ticks, TICK,
                                sp.filter_s * 1_000_000_000, carry=carry)
    if sp.kind == "tbars":
        return build_tbar_bars(d, sp.speed_ticks, TICK, carry=carry)
    if sp.kind == "orenko":
        return build_orenko_bars(d, sp.orenko_open, sp.orenko_trend,
                                 sp.orenko_rev, TICK, carry=carry)
    return build_wave_bars(d, sp.wave_ticks, TICK, carry=carry)


def build_in_two(spec_s, d1, d2):
    b1, b2 = build_chain(spec_s, [d1, d2])
    return b1, b2


def split_diff(spec_s, d, m):
    d1, d2 = slice_day(d, 0, m), slice_day(d, m, len(d))
    whole = build_bars(d, parse_barspec(spec_s), TICK)
    b1, b2 = build_in_two(spec_s, d1, d2)
    bad = []
    for f in FIELDS:
        joined = np.concatenate([getattr(b1, f), getattr(b2, f)])
        w = getattr(whole, f)
        if len(joined) != len(w) or not np.allclose(joined, w):
            bad.append(f)
    return bad


@st.composite
def split_streams(draw, reset_gaps=True):
    d = draw(streams(min_n=40, max_n=300, reset_gaps=reset_gaps))
    m = draw(st.integers(5, len(d) - 5))
    # a real 30-minute gap at the split is where the loader drops the carry
    # on purpose, so it is not a split the carry has to survive
    assume_ok = not gap_between(d, m)
    return d, m, assume_ok


def run_split(spec_s, fields_that_must_match):
    @settings(**PROFILE)
    @given(case=split_streams())
    def go(case):
        d, m, ok = case
        if not ok:
            return
        bad = set(split_diff(spec_s, d, m))
        assert not (bad & set(fields_that_must_match)), (spec_s, m, sorted(bad))
    go()


@st.composite
def three_way_streams(draw):
    d = draw(streams(min_n=60, max_n=300))
    m1 = draw(st.integers(5, len(d) - 30))
    m2 = draw(st.integers(m1 + 5, len(d) - 5))
    return d, m1, m2, not (gap_between(d, m1) or gap_between(d, m2))


@pytest.mark.parametrize("spec_s", ("r8-4", "r10-3", "s16-4", "tb40", "w40", "o2-6-12"))
@settings(**PROFILE)
@given(case=three_way_streams())
def test_a_middle_file_inside_one_bar_hands_the_carry_on_intact(spec_s, case):
    # three files: the middle one can sit entirely inside a single forming bar,
    # so the carried state is NEVER consumed there - it must be extended with the
    # middle file's events and passed on, not dropped or doubled
    d, m1, m2, ok = case
    if not ok:
        return
    whole = build_bars(d, parse_barspec(spec_s), TICK)
    parts = build_chain(spec_s, [slice_day(d, 0, m1), slice_day(d, m1, m2),
                                 slice_day(d, m2, len(d))])
    for f in FIELDS:
        joined = np.concatenate([getattr(b, f) for b in parts])
        w = getattr(whole, f)
        assert len(joined) == len(w) and np.allclose(joined, w), (spec_s, f, m1, m2)


@pytest.mark.parametrize("spec_s", ("r8-4", "r10-3", "s16-4", "tb40", "tb120", "w40", "w120", "o2-6-12"))
def test_building_in_two_halves_equals_one_build_for_every_carrying_bar_type(spec_s):
    """The acceptance test for the carry: every field of every bar, including the
    one that straddles the file boundary and including a brick abandoned at a
    30-minute gap (the random streams contain both)."""
    run_split(spec_s, FIELDS)


@pytest.mark.parametrize("spec_s", ("r8-4", "r10-3", "s16-4", "tb40", "w40", "o2-6-12"))
@settings(**PROFILE)
@given(case=split_streams(reset_gaps=False))
def test_no_volume_is_lost_across_the_file_boundary(spec_s, case):
    # No reset gap, so nothing is abandoned: everything the bars did not use is
    # the forming bar, carried out in end_state and counted in the second file's
    # bars or in ITS end_state. (renko end_state = (vol, buy, sell, hi, lo);
    # saber/tbars/wave end with (vol, buy, sell).)
    d, m, ok = case
    if not ok:
        return
    d1, d2 = slice_day(d, 0, m), slice_day(d, m, len(d))
    b1, b2 = build_in_two(spec_s, d1, d2)
    fv, fb = ((b2.end_state[0], b2.end_state[1]) if spec_s.startswith("r")
              else (b2.end_state[-3], b2.end_state[-2]))
    assert int(b1.volume.sum()) + int(b2.volume.sum()) + int(fv) == int(d.volume.sum())
    assert (int(b1.buy_volume.sum()) + int(b2.buy_volume.sum()) + int(fb)
            == int(np.where(d.aggr == 1, d.volume, 0).sum()))


def _stream(ticks, vols, gaps_s=None, date="20260202"):
    """Hand-built day: price 4000 + tick * 0.25, one event a second unless gaps_s
    (seconds before each event) says otherwise."""
    p = 4000.0 + np.asarray(ticks, dtype="float64") * TICK
    gaps = np.ones(len(p)) if gaps_s is None else np.asarray(gaps_s, dtype="float64")
    ts = et_ns(date, "09:30") + (np.cumsum(gaps) * 1_000_000_000).astype("int64")
    d = make_day_at(p, ts, date)
    return dataclasses.replace(d, volume=np.asarray(vols, dtype="int64"))


def _saber_split(ticks, m):
    """Saber s16-4 (trigger 4.00 points, offset 1.00, 1 s filter) over prices
    4000 + ticks/4, one event a second, built whole and as two files split before
    event m."""
    d = _stream(ticks, [1] * len(ticks))
    whole = build_bars(d, parse_barspec("s16-4"), TICK)
    b1, b2 = build_in_two("s16-4", slice_day(d, 0, m), slice_day(d, m, len(d)))
    return whole, b1, b2


def test_saber_straddling_bar_keeps_its_pre_boundary_low():
    # open 4000.00; the first file dips to 3999.00 (-4 ticks) and comes back; the
    # second file breaks out at 4004.00 (>= the 4000 + 4.00 trigger). One build
    # over everything: bar 0 spans all 6 events, high 4004.00, LOW 3999.00. (This
    # was a real defect until BARS_VERSION 10: the second file reported 4000.00.)
    whole, b1, b2 = _saber_split([0, -4, 0, 4, 8, 16], m=3)
    assert len(b1) == 0 and len(b2) == 1 and len(whole) == 1
    assert whole.low[0] == 3999.00 and whole.high[0] == 4004.00
    assert b2.high[0] == 4004.00
    assert b2.low[0] == whole.low[0]


def test_saber_straddling_bar_keeps_its_pre_boundary_high():
    # mirror: the first file spikes to 4001.00 (+4 ticks); the second file breaks
    # DOWN at 3996.00. Whole bar: HIGH 4001.00, low 3996.00
    whole, b1, b2 = _saber_split([0, 4, 0, -4, -8, -16], m=3)
    assert len(b1) == 0 and len(b2) == 1 and len(whole) == 1
    assert whole.high[0] == 4001.00 and whole.low[0] == 3996.00
    assert b2.high[0] == whole.high[0]


def test_renko_straddling_brick_keeps_its_pre_boundary_volume_and_low():
    # r8-4: brick 2.00, trend 1.00, one unit-volume event a second. Ticks from 4000:
    #   idx 0..4  4000.00 .. 4001.00   (never above the 4001.00 trigger)
    #   idx 5     4001.25  > 4001.00: completes bar 0 (close clamped to 4001.00,
    #                      span [0,5)) and OPENS bar 1
    #   idx 6     3999.50  a dip below bar 1's synthetic open
    #   idx 7     4001.50, idx 8 4002.00
    #   idx 9     4002.25  > 4002.00: completes bar 1, span [5,9), close 4002.00
    # Bar 1 = open 4002.00 - 2.00 = 4000.00, high 4002.00, LOW 3999.50 (the dip),
    # volume 4. Split before idx 7: the dip and idx 5 are in the first file. (A
    # real gap until BARS_VERSION 10: the second file reported low 4000.00, volume 2.)
    d = _stream([0, 1, 2, 3, 4, 5, -2, 6, 8, 9], [1] * 10)
    whole = build_bars(d, parse_barspec("r8-4"), TICK)
    assert len(whole) == 2
    assert (whole.i0[1], whole.i1[1]) == (5, 9)
    assert (whole.open[1], whole.high[1], whole.low[1], whole.close[1]) == (4000.0, 4002.0, 3999.5, 4002.0)
    assert whole.volume[1] == 4
    b1, b2 = build_in_two("r8-4", slice_day(d, 0, 7), slice_day(d, 7, len(d)))
    assert len(b1) == 1 and len(b2) == 1               # bar 0 in file 1, bar 1 in file 2
    assert (b2.open[0], b2.close[0]) == (whole.open[1], whole.close[1])
    assert b2.low[0] == whole.low[1] == 3999.5
    assert b2.high[0] == whole.high[1]
    assert b2.volume[0] == whole.volume[1] == 4
    # what the first file carries out: events 5 and 6 (vol 2), range 3999.50..4001.25
    assert b1.end_state == (2, 0, 0, 4001.25, 3999.5)


def test_a_reset_gap_abandons_the_carried_brick_with_its_volume():
    # r8-4 (trend 1.00). File 1: two events (vol 3 each), no bar yet. File 2:
    # 4000.50 (vol 5), then a 40-minute pause, then 4000.00 (vol 7) and 4001.25
    # (vol 11). The pause hits a forming brick that has no trend yet, so that brick
    # is ABANDONED (no bar, a hole in the spans) - and the 6 + 5 carried and local
    # volume with it. The first bar is the post-pause brick: span [the 4000.00
    # event], volume 7, NOT 13 (7 plus the carried 6), high/low from that event.
    d = _stream([0, 1, 2, 0, 5], [3, 3, 5, 7, 11], gaps_s=[1, 1, 1, 40 * 60, 1])
    whole = build_bars(d, parse_barspec("r8-4"), TICK)
    assert len(whole) == 1
    assert (whole.i0[0], whole.i1[0]) == (3, 4) and whole.volume[0] == 7
    assert whole.close[0] == 4001.0 and whole.high[0] == 4001.0
    b1, b2 = build_in_two("r8-4", slice_day(d, 0, 2), slice_day(d, 2, 5))
    assert len(b1) == 0 and b1.end_state[0] == 6
    assert len(b2) == 1
    assert b2.volume[0] == 7                            # the carried 6 died with the brick
    assert (b2.open[0], b2.high[0], b2.low[0], b2.close[0]) == tuple(
        float(x) for x in (whole.open[0], whole.high[0], whole.low[0], whole.close[0]))


def test_the_carried_state_is_used_once_and_only_by_the_first_bar():
    # r8-4: a long rising run so file 2 emits several bars; only the FIRST carries
    # the pre-boundary volume. ticks 0..16 one a second, vol 2 each; split after 3.
    d = _stream(list(range(0, 17)), [2] * 17)
    whole = build_bars(d, parse_barspec("r8-4"), TICK)
    b1, b2 = build_in_two("r8-4", slice_day(d, 0, 3), slice_day(d, 3, len(d)))
    assert list(np.concatenate([b1.volume, b2.volume])) == list(whole.volume)
    assert list(whole.volume) == [10, 8, 8] and len(b2) == 3


# ---------------------------------------------------------- the same, on real data

@pytest.fixture(scope="module")
def real_days():
    cat = Catalog()
    try:
        days = [d for d in cat.days("MNQ", "20260504", "20260507")][:3]
    except FileNotFoundError as e:
        pytest.skip(f"no tick data: {str(e).splitlines()[0]}")
    if not days:
        pytest.skip("no tick data: no MNQ day files in 20260504..20260507")
    return [cat.load_day("MNQ", d) for d in days]


@pytest.mark.data
@pytest.mark.parametrize("spec_s", ("60s", "500t", "r100-4", "r60-3", "s64-16", "tb120", "w120", "o4-12-28"))
def test_bar_invariants_hold_on_real_days(spec_s, real_days):
    for d in real_days:
        check_bars(d, spec_s)
