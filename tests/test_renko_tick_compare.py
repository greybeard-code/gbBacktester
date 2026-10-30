"""Renko thresholds are compared in whole ticks, not in float prices.

On a 0.1-tick instrument `anchor + 4 * 0.1` is not the float of the level a trade
printed at: 15.7 + 0.4 = 16.099999999999998 but a trade AT 16.1 is 16.1, so the old
float compare called a touch (which must not complete a brick) a breakout about one
time in five. 0.25 and 1.0 are exact in binary and never showed it.

Every case is brick 8 / trend 4 ticks at tick 0.1, anchored where the float compare is
known to disagree with the tick compare (A = 157 -> 15.7).
"""
import numpy as np
import pytest

from backtester.data import Catalog, build_bars, build_renko_bars
from backtester.strategy import parse_barspec

from conftest import make_day

TICK = 0.1
A = 157


def px(t):
    """The price a parquet file holds for tick number t (the nearest decimal)."""
    return round(t * TICK, 1)


# Hand-walked, in ticks relative to A. Volume 1 per trade.
#   idx0   0  seed, anchor 0
#   idx1  +4  ON the threshold (0 + T): strict compare, does NOT complete
#   idx2  +2
#   idx3  +5  > +4 -> bar0 closes at +4; idx3 is the breakout tick and opens bar1
#   idx4  +8  ON the next threshold (+4 + T): does NOT complete
#   idx5  +9  > +8 -> bar1 closes at +8; idx5 opens bar2
#   idx6  -4  ON the reversal threshold (+8 - R, R = 2B - T = 12): does NOT complete
#   idx7  -5  < -4 -> bar2 closes at -4 (a reversal); idx7 opens bar3, still forming
REL = [0, 4, 2, 5, 8, 9, -4, -5]
#          open  high  low  close  i0  i1  volume
EXPECTED = [(0, 4, 0, 4, 0, 3, 3),
            (5, 8, 5, 8, 3, 5, 2),
            (9, 9, -4, -4, 5, 7, 2)]


def _day():
    return make_day([px(A + r) for r in REL], tick=TICK)


def _rows(b):
    return [(round(b.open[i] / TICK), round(b.high[i] / TICK), round(b.low[i] / TICK),
             round(b.close[i] / TICK), int(b.i0[i]), int(b.i1[i]), int(b.volume[i]))
            for i in range(len(b))]


def _expected():
    return [(A + o, A + h, A + l, A + c, i0, i1, v) for o, h, l, c, i0, i1, v in EXPECTED]


def test_the_path_really_sits_on_a_float_disagreement():
    # the premise: at A the float level is NOT the float of the printed price
    assert px(A + 4) > px(A) + 4 * TICK


def test_touches_do_not_complete_bricks_when_compared_in_ticks():
    b = build_renko_bars(_day(), 8 * TICK, 4 * TICK, real_open=True, tick=TICK)
    assert _rows(b) == _expected()


def test_the_float_compare_without_tick_gets_this_path_wrong():
    # documents WHY the tick argument exists: this is the old behaviour
    b = build_renko_bars(_day(), 8 * TICK, 4 * TICK, real_open=True)
    assert _rows(b) != _expected()


def test_closes_are_the_decimal_prices_not_float_products():
    b = build_renko_bars(_day(), 8 * TICK, 4 * TICK, tick=TICK)
    assert [float(c) for c in b.close] == [px(A + 4), px(A + 8), px(A - 4)]
    assert b.end_anchor == px(A - 4) and b.end_dir == -1      # 15.3, not 15.299999999999999


def test_kingrenko_open_and_forming_state_in_ticks():
    b = build_renko_bars(_day(), 8 * TICK, 4 * TICK, real_open=True, tick=TICK)
    assert [round(o / TICK) for o in b.open] == [A, A + 5, A + 9]
    assert b.end_state[5] == px(A - 5)


@pytest.mark.parametrize("m", range(1, 8))
def test_split_day_carry_equals_a_continuous_build(m):
    whole = build_renko_bars(_day(), 8 * TICK, 4 * TICK, tick=TICK)
    first = build_renko_bars(make_day([px(A + r) for r in REL[:m]], tick=TICK),
                             8 * TICK, 4 * TICK, tick=TICK)
    second = build_renko_bars(
        make_day([px(A + r) for r in REL[m:]], tick=TICK,
                 start_ts=1_000_000_000_000 + m * 10**9),
        8 * TICK, 4 * TICK, carry_anchor=first.end_anchor, carry_dir=first.end_dir,
        carry_state=first.end_state, tick=TICK)
    for f in ("open", "high", "low", "close", "volume"):
        joined = np.concatenate([getattr(first, f), getattr(second, f)])
        assert np.array_equal(joined, getattr(whole, f)), (f, m)


@pytest.mark.parametrize("spec", ["r8-4", "k8-4"])
def test_build_bars_passes_the_tick(spec):
    d = _day()
    via = build_bars(d, parse_barspec(spec), TICK)
    direct = build_renko_bars(d, 8 * TICK, 4 * TICK, real_open=spec[0] == "k", tick=TICK)
    assert _rows(via) == _rows(direct)
    assert [r[3] for r in _rows(via)] == [A + 4, A + 8, A - 4]


def test_catalog_sequence_passes_the_tick(tmp_path):
    cat = Catalog(data_root=tmp_path, cache_root=tmp_path / "cache")
    d = _day()
    seq = cat.load_bars_sequence("TEST", ["20260101"], parse_barspec("r8-4"), TICK, days=[d])
    assert [round(float(c) / TICK) for c in seq[0].close] == [A + 4, A + 8, A - 4]
    # the spans tell a touch that completed a brick (old float compare) from one that did not
    assert seq[0].i1.tolist() == [3, 5, 7] and seq[0].volume.tolist() == [3, 2, 2]
