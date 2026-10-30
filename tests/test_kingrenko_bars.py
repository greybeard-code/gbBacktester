"""KingRenko$ bars: ninZaRenko's grid with a real open.

The closes, spans and volumes are ninZaRenko's
(same `build_renko_bars` loop); what differs is the open, which is the price of
the first trade in the bar's span, and the high/low that follow from it. So this
file checks the parts that differ and pins the parts that must not.

Every case uses brick 8, trend 4 ticks with tick 0.25:

    brick     = 2.00
    trend     = 1.00   (with-trend close: prior close +/- 1.00)
    reversal  = 3.00   (2B - T: against-trend close: prior close -/+ 3.00)
"""
import numpy as np
import pytest

from backtester.data import (Catalog, DayL1, RENKO_RESET_GAP_NS, build_bars,
                             build_renko_bars, classify_aggressor)
from backtester.strategy import BarSpec, parse_barspec

from conftest import make_day

TICK = 0.25
BRICK, TREND = 8 * TICK, 4 * TICK

# One trade per second. Hand-walked in PATH_NOTES below.
PATH = [100.00, 100.50, 101.00, 101.25,
        101.75, 102.25, 102.75, 99.00, 98.75,
        98.00, 97.75, 95.00, 95.25]

PATH_NOTES = """
idx 0   100.00  seed. Anchor 100.00, no direction yet (T = 1.00 either way).
idx 2   101.00  ON the threshold: strict compare, does NOT complete.
idx 3   101.25  > 101.00 -> bar0 closes at 101.00: O 100.00 (its first trade)
                H 101.00 L 100.00. Tick 3 is the breakout tick: it opens bar1.
idx 5   102.25  > 102.00 -> bar1: O 101.25 H 102.00 L 101.25 C 102.00.
                Bar2 opens on tick 5 (102.25, 0.25 past the close).
                Armed: up > 103.00, down < 99.00 (reversal 3.00).
idx 7   99.00   ON the reversal threshold: does NOT complete.
idx 8   98.75   < 99.00 -> bar2 (a reversal): O 102.25 H 102.75 L 99.00 C 99.00.
                The ninZa builder would give the synthetic open 99.00 + 2.00.
idx 10  97.75   < 98.00 -> bar3: O 98.75 H 98.75 L 98.00 C 98.00.
idx 11  95.00   sweeps two bricks on one trade: bar4 closes 97.00 (O 97.75),
                then bar5 closes 96.00 with an EMPTY span, flat at its level,
                volume 0. Tick 11 opens bar6, still forming when data ends.
"""

EXPECTED = {
    #        open    high    low     close   i0  i1
    "bar0": (100.00, 101.00, 100.00, 101.00, 0, 3),
    "bar1": (101.25, 102.00, 101.25, 102.00, 3, 5),
    "bar2": (102.25, 102.75, 99.00, 99.00, 5, 8),
    "bar3": (98.75, 98.75, 98.00, 98.00, 8, 10),
    "bar4": (97.75, 97.75, 97.00, 97.00, 10, 11),
    "bar5": (96.00, 96.00, 96.00, 96.00, 11, 11),
}


def _day(prices, start_ts=1_000_000_000_000, volumes=None):
    d = make_day(prices, start_ts=start_ts)
    if volumes is not None:
        import dataclasses
        d = dataclasses.replace(d, volume=np.asarray(volumes, dtype="int64"))
    return d


def _build(day, real_open=True, **carry):
    return build_renko_bars(day, BRICK, TREND, real_open=real_open, **carry)


def _rows(b):
    return [(float(b.open[i]), float(b.high[i]), float(b.low[i]),
             float(b.close[i]), int(b.i0[i]), int(b.i1[i]))
            for i in range(len(b))]


def test_hand_computed_path_real_open_and_flat_sweep_brick():
    b = _build(_day(PATH))
    assert _rows(b) == list(EXPECTED.values()), PATH_NOTES
    # the forming brick is carried out, not emitted; its open rides in end_state
    assert b.end_state[5] == 95.00
    assert len(b.end_state) == 6


def test_open_is_the_first_trade_not_the_prior_close_nor_the_synthetic_open():
    b = _build(_day(PATH))
    # not the prior close (the vendor docs' guess): the breakout tick, 1+ tick past it
    assert b.open[1] != b.close[0] and b.open[1] - b.close[0] == 0.25
    # after a reversal the open is on the OLD side of the close (above a down bar)
    assert b.open[2] > b.close[2]
    assert b.open[2] - b.close[1] == 0.25
    # and it is never the ninZa synthetic open (close -/+ brick)
    ninza = _build(_day(PATH), real_open=False)
    assert ninza.open[2] == 99.00 + BRICK and b.open[2] != ninza.open[2]


def test_grid_closes_spans_and_volumes_equal_ninzarenko():
    vols = [3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43]
    d = _day(PATH, volumes=vols)
    king, ninza = _build(d), _build(d, real_open=False)
    for f in ("close", "volume", "buy_volume", "sell_volume", "i0", "i1", "ts_end"):
        assert np.array_equal(getattr(king, f), getattr(ninza, f)), f
    # the breakout tick is in the NEW bar only, and a sweep brick has no volume
    assert king.volume.tolist()[:6] == [sum(vols[0:3]), sum(vols[3:5]),
                                        sum(vols[5:8]), sum(vols[8:10]), vols[10], 0]
    # ninZa's end_state is untouched by the flag: still five elements
    assert len(ninza.end_state) == 5


def test_high_low_are_real_extremes_widened_to_open_and_clamped_close():
    b = _build(_day(PATH))
    for i in range(len(b)):
        seg = np.array(PATH[int(b.i0[i]):int(b.i1[i])]) if b.i1[i] > b.i0[i] else np.array([b.close[i]])
        assert b.high[i] == max(seg.max(), b.open[i], b.close[i])
        assert b.low[i] == min(seg.min(), b.open[i], b.close[i])
    # bar0's high is the clamped close 101.00, a level no trade in its span printed
    assert max(PATH[0:3]) == 101.00 and b.high[0] == 101.00
    # bar4's low is the clamped close 97.00, below every trade in its span
    assert PATH[10] == 97.75 and b.low[4] == 97.00


@pytest.mark.parametrize("m", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])
def test_split_day_carry_equals_a_continuous_build(m):
    whole = _build(_day(PATH))
    first = _build(_day(PATH[:m]))
    second = _build(_day(PATH[m:], start_ts=1_000_000_000_000 + m * 10**9),
                    carry_anchor=first.end_anchor, carry_dir=first.end_dir,
                    carry_state=first.end_state)
    for f in ("open", "high", "low", "close", "volume"):
        joined = np.concatenate([getattr(first, f), getattr(second, f)])
        assert np.array_equal(joined, getattr(whole, f)), (f, m)


def test_a_file_with_no_trades_of_the_forming_brick_keeps_the_carried_open():
    # middle file that never completes a brick must hand the OPEN on, unchanged
    whole = _build(_day(PATH))
    a = _build(_day(PATH[:9]))                                     # bar3 forming, opened on idx 8
    mid = _build(_day(PATH[9:10], start_ts=1_000_000_000_000 + 9 * 10**9),
                 carry_anchor=a.end_anchor, carry_dir=a.end_dir, carry_state=a.end_state)
    assert len(mid) == 0 and mid.end_state[5] == 98.75
    z = _build(_day(PATH[10:], start_ts=1_000_000_000_000 + 10 * 10**9),
               carry_anchor=mid.end_anchor, carry_dir=mid.end_dir, carry_state=mid.end_state)
    assert float(z.open[0]) == float(whole.open[3]) == 98.75


def test_session_gap_closes_a_partial_brick_on_its_real_open():
    # 5 trades, then > 30 minutes of silence, then a fresh session
    prices = [100.00, 100.50, 101.25, 101.50, 101.75, 200.00, 200.50]
    ts = np.array([0, 1, 2, 3, 4, 4 + RENKO_RESET_GAP_NS // 10**9 + 1,
                   4 + RENKO_RESET_GAP_NS // 10**9 + 2], dtype="int64") * 10**9 + 10**12
    p = np.asarray(prices)
    ask, bid = p + TICK, p - TICK
    d = DayL1("20260101", ts, p, np.ones(len(p), dtype="int64"), ask, bid,
              np.ones(len(p), dtype="int64"), np.ones(len(p), dtype="int64"),
              classify_aggressor(p, ask, bid))
    b = _build(d)
    # bar0 closes 101.00 on idx 2 (101.25); the brick forming at the halt is cut at
    # the last trade 101.75 and opens on ITS first trade (101.25), not close - brick
    assert float(b.close[1]) == 101.75 and float(b.open[1]) == 101.25
    assert float(b.high[1]) == 101.75 and float(b.low[1]) == 101.25
    # then the grid re-anchors at the next session's first trade
    assert len(b.end_state) == 6 and b.end_state[5] == 200.00


def test_default_builder_is_still_ninzarenko():
    d = _day(PATH)
    default = build_renko_bars(d, BRICK, TREND)
    assert np.array_equal(default.open, _build(d, real_open=False).open)
    assert len(default.end_state) == 5


def test_parse_barspec_grammar():
    s = parse_barspec("k12-4")
    assert s == BarSpec("kingrenko", brick_ticks=12, trend_ticks=4)
    assert s.key == "k12-4"
    assert parse_barspec("K8").trend_ticks == 4                 # default trend = brick/2
    assert parse_barspec("r12-4").key == "r12-4"                # distinct cache directory
    assert parse_barspec("r12-4").kind == "renko"
    with pytest.raises(ValueError):
        parse_barspec("k4-8")


def test_build_bars_dispatches_kingrenko():
    d = _day(PATH)
    via_spec = build_bars(d, parse_barspec("k8-4"), TICK)
    assert _rows(via_spec) == _rows(_build(d))
    assert _rows(via_spec) != _rows(build_bars(d, parse_barspec("r8-4"), TICK))


def test_sequence_threads_the_open_across_files_and_caches_apart_from_ninza(tmp_path):
    cat = Catalog(data_root=tmp_path, cache_root=tmp_path / "cache")
    k, r = parse_barspec("k8-4"), parse_barspec("r8-4")
    day1 = _day(PATH[:9])
    day2 = _day(PATH[9:], start_ts=2_000_000_000_000)           # no 30-minute gap
    seq = cat.load_bars_sequence("TEST", ["20260101", "20260102"], k, TICK,
                                 days=[day1, day2])
    again = cat.load_bars_sequence("TEST", ["20260101", "20260102"], k, TICK,
                                   days=[day1, day2])           # cache hit
    ninza = cat.load_bars_sequence("TEST", ["20260101", "20260102"], r, TICK,
                                   days=[day1, day2])
    whole = _build(_day(PATH))
    joined = np.concatenate([b.open for b in seq])
    assert np.array_equal(joined, whole.open[:len(joined)])
    assert seq[1].open[0] == 98.75                              # opened in file one
    for a, b in zip(seq, again):
        assert np.array_equal(a.open, b.open) and np.array_equal(a.high, b.high)
    # the two kinds do not share a cache file
    assert not np.array_equal(np.concatenate([b.open for b in ninza]), joined)
