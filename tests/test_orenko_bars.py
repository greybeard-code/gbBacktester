"""Orenko bars: geometry, overlap, volume identity, gap re-seed, cross-day carry.

Orenko is the TBars loop with the three
distances taken directly and no Heikin-Ashi smoothing, so this file checks the
parts that differ (raw OHLC, the open offset, a bar abandoned at a gap) and
the contract it shares (strict breakout, breakout tick in the NEW bar).

Every case uses open=2, trend=4, reversal=8 ticks with tick 0.25:

    open offset  = 2 * 0.25 = 0.50   (next bar opens this far back inside)
    trend        = 4 * 0.25 = 1.00   (with-trend continuation)
    reversal     = 8 * 0.25 = 2.00   (against-trend)
"""
import numpy as np
import pytest

from backtester.data import (Catalog, DayL1, RENKO_RESET_GAP_NS, build_bars,
                             build_orenko_bars, classify_aggressor)
from backtester.strategy import BarSpec, parse_barspec

TICK = 0.25
OPEN_T, TREND_T, REV_T = 2, 4, 8

# One tick per second. Hand-walked in PATH_NOTES below.
PATH = [100.00, 100.25, 100.50, 100.75, 101.00, 101.25,
        100.75, 100.25, 99.25, 99.00, 98.75,
        98.50, 98.00, 97.75]

PATH_NOTES = """
idx 0   100.00  seed. Band collapsed on 100.00 (no direction yet).
idx 1   100.25  > 100.00 -> bar0 completes at its threshold 100.00 (a doji).
                Bar1 opens: O = 100.00 - 0.50 = 99.50, armed 98.00 .. 101.00.
idx 4   101.00  ON the threshold: strict compare, does NOT complete.
idx 5   101.25  > 101.00 -> bar1: O 99.50 H 101.00 L 99.50 C 101.00.
                Bar2 opens at 100.50, armed 99.00 .. 102.00.
idx 9   99.00   ON the lower threshold: does NOT complete.
idx 10  98.75   < 99.00 -> bar2 (a reversal, 2.00 from 101.00):
                O 100.50 H 101.00 L 99.00 C 99.00.
                Bar3 opens at 99.50, armed 98.00 .. 101.00.
idx 12  98.00   ON the threshold: does NOT complete.
idx 13  97.75   < 98.00 -> bar3: O 99.50 H 99.50 L 98.00 C 98.00.
                Bar4 opens at 98.50 and is still forming when data ends.
"""

EXPECTED = {
    #        open    high    low     close   i0  i1
    "bar0": (100.00, 100.00, 100.00, 100.00, 0, 1),
    "bar1": (99.50, 101.00, 99.50, 101.00, 1, 5),
    "bar2": (100.50, 101.00, 99.00, 99.00, 5, 10),
    "bar3": (99.50, 99.50, 98.00, 98.00, 10, 13),
}


def _day(prices, ts_s=None, volumes=None):
    p = np.asarray(prices, dtype="float64")
    ts = (np.arange(len(p)) if ts_s is None else np.asarray(ts_s)) * 10**9
    v = (np.asarray(volumes, dtype="int64") if volumes is not None
         else np.ones(len(p), dtype="int64"))
    ask, bid = p + TICK, p - TICK
    return DayL1("20260101", ts.astype("int64"), p, v, ask, bid,
                 np.ones(len(p), dtype="int64"), np.ones(len(p), dtype="int64"),
                 classify_aggressor(p, ask, bid))


def _build(day, open_t=OPEN_T, trend_t=TREND_T, rev_t=REV_T, carry=None):
    return build_orenko_bars(day, open_t, trend_t, rev_t, TICK, carry=carry)


def _rows(b):
    return [(float(b.open[i]), float(b.high[i]), float(b.low[i]),
             float(b.close[i]), int(b.i0[i]), int(b.i1[i]))
            for i in range(len(b))]


def test_hand_computed_trend_reversal_and_overlap():
    b = _build(_day(PATH))
    assert _rows(b) == list(EXPECTED.values()), PATH_NOTES
    # the forming bar is carried out, not emitted
    assert b.end_state[0] == 98.50                      # its raw synthetic open
    # open[i] != close[i-1]: consecutive bars overlap by the open offset
    for i in range(1, len(b)):
        assert abs(b.open[i] - b.close[i - 1]) in (0.0, OPEN_T * TICK)
    assert b.open[1] == b.close[0] - OPEN_T * TICK       # after an up bar: below
    assert b.open[3] == b.close[2] + OPEN_T * TICK       # after a down bar: above


def test_zero_open_offset_opens_on_the_prior_close():
    b = _build(_day(PATH), open_t=0)
    assert len(b) >= 3
    for i in range(1, len(b)):
        assert b.open[i] == b.close[i - 1]


def test_completed_bars_close_on_their_extreme():
    b = _build(_day(PATH))
    up = b.close > b.open
    assert (b.close[up] == b.high[up]).all()
    down = b.close < b.open
    assert (b.close[down] == b.low[down]).all()


def test_breakout_tick_belongs_to_the_new_bar_only():
    vols = [3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47]
    d = _day(PATH, volumes=vols)
    b = _build(d)
    for i, (_, _, _, _, i0, i1) in enumerate(EXPECTED.values()):
        assert b.volume[i] == sum(vols[i0:i1])
    # exact identity: completed bars + the forming bar = everything traded
    assert int(b.volume.sum()) + b.end_state[6] == sum(vols)


@pytest.mark.parametrize("m", [1, 2, 4, 5, 7, 9, 10, 12, 13])
def test_split_day_carry_equals_a_continuous_build(m):
    whole = _build(_day(PATH))
    first = _build(_day(PATH[:m]))
    second = _build(_day(PATH[m:], ts_s=np.arange(m, len(PATH))),
                    carry=first.end_state)
    for f in ("open", "high", "low", "close", "volume"):
        joined = np.concatenate([getattr(first, f), getattr(second, f)])
        assert np.array_equal(joined, getattr(whole, f)), f
    assert second.end_state == whole.end_state


def test_gap_reseed_keeps_the_direction_and_arms_an_inverted_band():
    # The NT8 export shows the vendor carries direction through a re-seed.
    # bar3 was a down bar (dir = -1), so after the gap the seed at 200.00 arms
    # max = 200.00 - 1.00 = 199.00 and min = 201.00 (inverted). The first tick,
    # 200.25, satisfies BOTH tests; maxExceeded wins, so the bar completes UP at
    # 199.00 (the band's max), 1.00 BELOW its own open, and NT8 keeps the open
    # inside the range: H = 200.00 (the open), L = C = 199.00.
    ts = list(range(14)) + [3614, 3615, 3616]
    d = _day(PATH + [200.00, 200.25, 200.50], ts_s=ts)
    b = _build(d)
    rows = _rows(b)
    assert rows[:4] == list(EXPECTED.values())
    # bar4: opened at 98.50, abandoned by the gap before any updating tick, so
    # it keeps the 98.00 it was opened with (the raw tick, 97.75, is outside it)
    assert rows[4] == (98.50, 98.50, 98.00, 98.00, 13, 14)
    assert rows[5] == (200.00, 200.00, 199.00, 199.00, 14, 15)
    # successor: dir is now +1, opens 0.50 back from 199.00, armed 197.00..200.00;
    # 200.50 breaks 200.00 (the open sits inside its range: L = 198.50)
    assert rows[6] == (198.50, 200.00, 198.50, 200.00, 15, 16)
    assert len(rows) == 7
    assert (b.high >= np.maximum(b.open, b.close)).all()
    assert (b.low <= np.minimum(b.open, b.close)).all()


def test_inverted_band_tick_beyond_both_thresholds_keeps_the_close_in_range():
    # After the same down bar and gap, the band is max 199.00 / min 201.00. A
    # first tick at 202.00 is above BOTH, so only maxExceeded fires: the close
    # clamps to 199.00 while the running low is still the 200.00 open. The
    # close is folded into the range so the bar is drawable: L = C = 199.00.
    ts = list(range(14)) + [3614, 3615]
    b = _build(_day(PATH + [200.00, 202.00], ts_s=ts))
    assert _rows(b)[5] == (200.00, 200.00, 199.00, 199.00, 14, 15)
    assert (b.low <= np.minimum(b.open, b.close)).all()
    assert (b.high >= np.maximum(b.open, b.close)).all()


def test_inverted_band_successor_abandoned_by_a_gap_keeps_its_open_in_range():
    # Same re-seed as above, but a second gap hits bar6 (opened at 198.50, below
    # the 199.00 both-exceeded close) before any updating tick. Its low must
    # still include the 198.50 open, otherwise the emitted bar has open < low.
    ts = list(range(14)) + [3614, 3615, 9000]
    d = _day(PATH + [200.00, 200.25, 300.00], ts_s=ts)
    b = _build(d)
    rows = _rows(b)
    assert rows[5] == (200.00, 200.00, 199.00, 199.00, 14, 15)
    assert rows[6] == (198.50, 199.00, 198.50, 199.00, 15, 16)
    assert len(rows) == 7
    assert (b.low <= np.minimum(b.open, b.close)).all()


def test_reseed_carry_in_keeps_direction_like_an_in_day_gap():
    # Same bars as the in-day gap case, but the second session is a separate
    # build handed ("reseed", -1), the direction bar3 ended on.
    b = _build(_day([200.00, 200.25, 200.50]), carry=("reseed", -1))
    assert _rows(b) == [(200.00, 200.00, 199.00, 199.00, 0, 1),
                        (198.50, 200.00, 198.50, 200.00, 1, 2)]
    # no direction (carry=None) is a collapsed doji instead
    assert _rows(_build(_day([200.00, 200.25, 200.50])))[0] == (
        200.00, 200.00, 200.00, 200.00, 0, 1)


def _two_files(tmp_path, gap_s):
    cat = Catalog(data_root=tmp_path, cache_root=tmp_path / "cache")
    day1 = _day(PATH)
    t0 = int(day1.ts[-1]) + int(gap_s * 1e9)
    day2 = _day([200.00, 200.25, 200.50],
                ts_s=np.array([t0, t0 + 10**9, t0 + 2 * 10**9]) // 10**9)
    return cat, day1, day2


def test_loader_keeps_direction_across_a_day_file_gap(tmp_path):
    # A Sunday reopen: the day file changes AND a genuine gap passes. NT8 keeps
    # Friday's direction, so day2's first bar is the carried-direction bar, not
    # a collapsed doji (TBars/Wave still drop it; see the loader docstring).
    cat, day1, day2 = _two_files(tmp_path, RENKO_RESET_GAP_NS / 1e9 + 60)
    b1, b2 = cat.load_bars_sequence("TEST", ["20260101", "20260102"],
                                    parse_barspec("o4-2-8"), TICK,
                                    days=[day1, day2])
    assert _rows(b1) == list(EXPECTED.values())
    assert _rows(b2) == [(200.00, 200.00, 199.00, 199.00, 0, 1),
                         (198.50, 200.00, 198.50, 200.00, 1, 2)]


def test_loader_continues_the_forming_bar_without_a_gap(tmp_path):
    # No gap: day2 CONTINUES day1's forming bar. It opened at 98.50 after a down
    # close at 98.00 (so L = 98.00) and is armed 97.00..100.00 (reversal up =
    # 2.00); 200.00 completes it at the 100.00 edge. Its opening tick is on the
    # previous day, so the span is empty: (0, 0).
    cat, day1, day2 = _two_files(tmp_path, 5)
    _, b2 = cat.load_bars_sequence("TEST", ["20260101", "20260102"],
                                   parse_barspec("o4-2-8"), TICK,
                                   days=[day1, day2])
    assert _rows(b2)[0] == (98.50, 100.00, 98.00, 100.00, 0, 0)


def test_gap_after_updating_ticks_closes_on_the_last_trade():
    # bar1 is mid-flight (99.50 open, 100.25 and 100.50 inside its band) when
    # the gap hits: it closes on 100.50, its last updating tick.
    d = _day([100.00, 100.25, 100.50, 100.25, 150.00],
             ts_s=[0, 1, 2, 3, 7200])
    rows = _rows(_build(d))
    assert rows[1] == (99.50, 100.50, 99.50, 100.25, 1, 4)


def test_orenko_does_not_use_heikin_ashi():
    # A Heikin-Ashi close would be (O+H+L+C)/4, never exactly the high.
    b = _build(_day(PATH))
    assert b.close[1] == b.high[1] == 101.00


def test_parse_barspec_grammar():
    # NT8 chart label order: Trend / Open Offset / Reversal
    s = parse_barspec("o4-12-28")
    assert s == BarSpec("orenko", orenko_trend=4, orenko_open=12, orenko_rev=28)
    assert s.key == "o4-12-28"
    assert parse_barspec("O1-0-1").orenko_open == 0
    for bad in ("o4-12", "o0-12-28", "o4-12-0", "o-1-2-3"):
        with pytest.raises(ValueError):
            parse_barspec(bad)


def test_build_bars_dispatches_orenko():
    d = _day(PATH)
    via_spec = build_bars(d, parse_barspec("o4-2-8"), TICK)
    assert _rows(via_spec) == _rows(_build(d))
