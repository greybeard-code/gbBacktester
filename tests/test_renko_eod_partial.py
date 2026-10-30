"""The forming renko brick is closed as a partial at the end of a day file that ends at a gap.

NT8 prints a last, partial bar at the weekend halt (Fri 16:59:59 in the ninZaRenko
64-16 export); until BARS_VERSION 12 the backtester dropped it, one bar a week. The
rule is stateless: a file that ends more than the 30-minute reset gap before the end
of its own Eastern calendar day ends at a gap, whatever the next file holds. It is
opt-in (`close_at_eod`) because it assumes whole-day files; the Catalog sets it.

Every expectation is hand-walked at tick 0.25, brick 8 / trend 4 ticks, A = 400
(price 100.0), ticks relative to A: [0, +5, +7, +3].
  0   seed, anchor 0
  +5  > +4 (0 + T): bar0 closes UP at +4, body T = 1.00, open = the anchor;
      +5 is the breakout tick and opens bar1 (up trend, anchor +4)
  +7, +3  inside bar1's levels (up +8, down +4 - 12 = -8): nothing completes
  bar1, closed as a partial at the last trade (+3):
      synthetic open = anchor - (B - T) = +4 - 4 = 0, so it is an UP bar of body 3
      -> O 0  H +7  L 0  C +3 ; real open (KingRenko$): O +5  H +7  L +3  C +3
"""
import numpy as np
import pytest

from backtester.data import Catalog, build_bars, build_renko_bars
from backtester.strategy import parse_barspec

from conftest import et_ns, make_day_at
from test_cache_identity import DATE as CAT_DATE, setup as cat_setup

TICK = 0.25
A = 400
REL = [0, 5, 7, 3]
NS = 1_000_000_000
FRIDAY = "20260508"


def px(r):
    return (A + r) * TICK


def day(last_hhmm="16:59", last_sec=59, date=FRIDAY):
    t_end = et_ns(date, last_hhmm, last_sec)
    ts = t_end - (len(REL) - 1 - np.arange(len(REL), dtype="int64")) * NS
    return make_day_at([px(r) for r in REL], ts, date)


def rows(b):
    return [(round(b.open[i] / TICK) - A, round(b.high[i] / TICK) - A,
             round(b.low[i] / TICK) - A, round(b.close[i] / TICK) - A,
             int(b.i0[i]), int(b.i1[i]), int(b.volume[i])) for i in range(len(b))]


def build(d, **kw):
    return build_renko_bars(d, 8 * TICK, 4 * TICK, tick=TICK, **kw)


def test_friday_file_gets_its_closing_partial():
    b = build(day(), close_at_eod=True)
    assert rows(b) == [(0, 4, 0, 4, 0, 1, 1),
                       (0, 7, 0, 3, 1, 4, 3)]
    assert int(b.ts_end[-1]) == int(day().ts[-1])      # stamped at the last trade
    assert b.end_dir == 0                              # nothing left forming to carry
    assert b.end_state[:3] == (0, 0, 0)


def test_the_real_open_variant_opens_on_the_first_trade_of_its_span():
    b = build(day(), close_at_eod=True, real_open=True)
    assert rows(b)[-1] == (5, 7, 3, 3, 1, 4, 3)


def test_off_by_default_the_brick_stays_forming():
    b = build(day())
    assert rows(b) == [(0, 4, 0, 4, 0, 1, 1)]
    assert b.end_dir == 1 and b.end_state[0] == 3      # its 3 lots are carried out


def test_the_cutoff_is_30_minutes_before_midnight_inclusive():
    # last trade at exactly 23:30:00 is a gap to any next trade (> 30 min)
    assert len(build(day("23:30", 0), close_at_eod=True)) == 2
    # one second later the next file could continue without a reset: carry decides
    assert len(build(day("23:30", 1), close_at_eod=True)) == 1


def test_a_file_stamped_outside_its_own_day_is_left_alone():
    d = day()
    d.ts = d.ts + 400 * 86_400 * NS        # a year later than the label
    assert len(build(d, close_at_eod=True)) == 1


def test_no_forming_brick_means_nothing_to_close():
    d = make_day_at([px(0), px(1)], [et_ns(FRIDAY, "16:59", 58), et_ns(FRIDAY, "16:59", 59)],
                    FRIDAY)
    assert len(build(d, close_at_eod=True)) == 0       # no bar had completed: direction 0


def test_build_bars_passes_the_flag_for_both_renko_kinds():
    for spec, real in (("r8-4", False), ("k8-4", True)):
        b = build_bars(day(), parse_barspec(spec), TICK, close_at_eod=True)
        assert rows(b) == rows(build(day(), close_at_eod=True, real_open=real))
        assert len(build_bars(day(), parse_barspec(spec), TICK)) == 1


@pytest.mark.parametrize("spec", ["r8-4", "k8-4"])
def test_the_catalog_closes_the_brick_on_both_load_paths(tmp_path, spec):
    # a raw day of 1 trade/s from 10:00 ET ends hours before midnight
    prices = [px(r) for r in REL]
    cat, _ = cat_setup(tmp_path, prices)
    single = cat.load_bars("MNQ", CAT_DATE, parse_barspec(spec), TICK)
    seq = Catalog(data_root=tmp_path / "data", cache_root=tmp_path / "cache2") \
        .load_bars_sequence("MNQ", [CAT_DATE], parse_barspec(spec), TICK)[0]
    for b in (single, seq):
        assert len(b) == 2 and int(b.i1[-1]) == len(REL)
        assert round(b.close[-1] / TICK) - A == 3
