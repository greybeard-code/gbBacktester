"""backtester/indicators.py and gbsignals/nt8math.py, every expected value worked
by hand (plan task 19). Only KAMA/KamaRegime had direct tests before (test_kama).

Two different families live here on purpose: indicators.py warms EMA up with an
SMA seed and reports NaN until ready; nt8math.py reproduces NT8's EMA (seeded
with the first value) and SMA (partial window from bar 0), which the GodZillaKilla
signal ports need for bar-exact parity. The tests pin BOTH so neither drifts into
the other.
"""
from __future__ import annotations

import math

import pytest

from backtester.gbsignals.nt8math import (APPROX_EPS, Nt8Ema, Nt8Sma,
                                          approx_compare, round_to_tick)
from backtester.indicators import (ATR, EMA, RSI, SMA, Bollinger,
                                   EfficiencyRatio, Highest, Lowest)


def feed(ind, xs):
    return [ind.update(x) for x in xs]


def isnan(x):
    return math.isnan(x)


# ------------------------------------------------------------------- EMA ----

def test_ema_is_nan_until_the_period_then_seeds_with_the_sma():
    # period 3 -> k = 2 / (3 + 1) = 0.5. Seed (3rd bar) = (2 + 4 + 6) / 3 = 4;
    # then 4 + .5 * (8 - 4) = 6 and 6 + .5 * (10 - 6) = 8
    e = EMA(3)
    out = feed(e, [2, 4, 6, 8, 10])
    assert isnan(out[0]) and isnan(out[1])
    assert out[2:] == pytest.approx([4.0, 6.0, 8.0])
    assert e.ready and e.value == pytest.approx(8.0)


def test_ema_ready_flips_exactly_on_the_period_th_bar():
    e = EMA(3)
    flags = []
    for x in (1, 1, 1, 1):
        e.update(x)
        flags.append(e.ready)
    assert flags == [False, False, True, True]


def test_ema_of_a_constant_is_that_constant():
    e = EMA(5)
    feed(e, [7.5] * 20)
    assert e.value == pytest.approx(7.5)


# ------------------------------------------------------------------- SMA ----

def test_sma_is_nan_until_full_then_a_sliding_mean():
    s = SMA(3)
    out = feed(s, [1, 2, 3, 4, 5, 6])
    assert isnan(out[0]) and isnan(out[1])
    assert out[2:] == pytest.approx([2.0, 3.0, 4.0, 5.0])      # 6/3, 9/3, 12/3, 15/3


def test_sma_running_sum_does_not_drift_over_many_bars():
    s = SMA(10)
    feed(s, [0.1 * (i % 7) for i in range(10_000)])
    window = [0.1 * (i % 7) for i in range(9_990, 10_000)]
    assert s.value == pytest.approx(sum(window) / 10, abs=1e-9)


# ------------------------------------------------------------- Bollinger ----

def test_bollinger_uses_the_population_standard_deviation():
    # closes 1,2,3: mean 2, population variance (1 + 0 + 1) / 3 = 2/3 -> sd
    # 0.8164965809; bands 2 +/- 2 * sd = 3.6329931619 and 0.3670068381. (The
    # SAMPLE sd would be 1.0 and the bands 4.0 / 0.0.)
    b = Bollinger(3, 2.0)
    out = feed(b, [1, 2, 3])
    assert out[2] == pytest.approx(2.0)
    assert b.middle == pytest.approx(2.0)
    assert b.upper == pytest.approx(2.0 + 2 * math.sqrt(2 / 3))
    assert b.lower == pytest.approx(2.0 - 2 * math.sqrt(2 / 3))
    assert b.upper == pytest.approx(3.6329931619)


def test_bollinger_slides_and_is_nan_before_the_window_fills():
    b = Bollinger(3, 2.0)
    out = feed(b, [1, 2, 3, 4])
    assert isnan(out[0]) and isnan(out[1]) and not isnan(out[2])
    # window [2,3,4]: same sd, mean 3
    assert b.middle == pytest.approx(3.0)
    assert b.upper - b.middle == pytest.approx(2 * math.sqrt(2 / 3))
    assert b.middle - b.lower == pytest.approx(2 * math.sqrt(2 / 3))


def test_bollinger_collapses_to_the_mean_on_a_flat_window():
    b = Bollinger(4, 2.0)
    feed(b, [5, 5, 5, 5])
    assert b.upper == b.middle == b.lower == 5.0


def test_bollinger_multiplier_scales_the_band():
    wide, narrow = Bollinger(3, 3.0), Bollinger(3, 1.0)
    feed(wide, [1, 2, 3])
    feed(narrow, [1, 2, 3])
    assert wide.upper - wide.middle == pytest.approx(3 * (narrow.upper - narrow.middle))


# ------------------------------------------------------------------- ATR ----

def test_atr_wilder_smoothing_with_a_gap_hand_computed():
    # (high, low, close) per bar, period 3
    #  1: TR = 10 - 8 = 2 (no previous close)
    #  2: prev close 9:  max(11-9, |11-9|, |9-9|)   = 2
    #  3: prev close 10: max(13-10, |13-10|, |10-10|) = 3 -> seed = (2+2+3)/3 = 7/3
    #  4: prev close 12: max(12-9, 0, |9-12|) = 3 -> 7/3 + (3 - 7/3)/3 = 23/9
    #  5: gap up, prev close 11: max(20-18, |20-11|, |18-11|) = 9
    #     -> 23/9 + (9 - 23/9)/3 = 127/27
    a = ATR(3)
    bars = [(10, 8, 9), (11, 9, 10), (13, 10, 12), (12, 9, 11), (20, 18, 19)]
    out = [a.update(*b) for b in bars]
    assert isnan(out[0]) and isnan(out[1])
    assert out[2] == pytest.approx(7 / 3)
    assert out[3] == pytest.approx(23 / 9)
    assert out[4] == pytest.approx(127 / 27)
    assert a.ready


def test_atr_true_range_includes_the_gap_not_just_the_bar_range():
    a = ATR(1)                    # period 1: ATR == the latest true range
    a.update(10, 9, 9.5)
    # a bar of range 0.5 that gapped 5 above the prior close: TR = 5.5, not 0.5
    assert a.update(15, 14.5, 15) == pytest.approx(5.5)


# ------------------------------------------------------------------- RSI ----

def test_rsi_wilder_hand_computed():
    # closes 10,11,12,11,12,11, period 3. Changes +1,+1,-1 seed the averages:
    # avg gain (1+1+0)/3 = 2/3, avg loss (0+0+1)/3 = 1/3 -> RS 2 -> RSI 66.667
    # then +1: gain 2/3 + (1-2/3)/3 = 7/9, loss 1/3 + (0-1/3)/3 = 2/9
    #         -> RS 3.5 -> RSI 100 - 100/4.5 = 77.778
    # then -1: gain 7/9 - 7/27 = 14/27, loss 2/9 + (1-2/9)/3 = 13/27
    #         -> RSI = 100 * 14 / 27 = 51.852
    r = RSI(3)
    out = feed(r, [10, 11, 12, 11, 12, 11])
    assert all(isnan(x) for x in out[:3])                 # first close + 2 seed changes
    assert out[3] == pytest.approx(200 / 3)
    assert out[4] == pytest.approx(100 - 100 / 4.5)
    assert out[5] == pytest.approx(1400 / 27)


def test_rsi_all_gains_is_100():
    r = RSI(3)
    feed(r, [1, 2, 3, 4, 5])
    assert r.value == 100.0


def test_rsi_all_losses_is_zero():
    r = RSI(3)
    feed(r, [5, 4, 3, 2, 1])
    assert r.value == pytest.approx(0.0)


def test_rsi_of_a_flat_series_is_100_because_there_is_no_loss():
    # Pinned current behavior: zero average loss is reported as 100, so a market
    # that has not moved reads "overbought". Not claimed to match NT8.
    r = RSI(3)
    feed(r, [10, 10, 10, 10, 10])
    assert r.value == 100.0


def test_rsi_is_nan_until_period_plus_one_closes():
    r = RSI(3)
    ready = []
    for x in (1, 2, 1, 2, 1):
        r.update(x)
        ready.append(r.ready)
    assert ready == [False, False, False, True, True]


# ------------------------------------------------------ EfficiencyRatio ----

def test_efficiency_ratio_hand_computed():
    # period 3 needs 4 closes. 10,12,11,15: net |15-10| = 5; noise 2 + 1 + 4 = 7
    er = EfficiencyRatio(3)
    out = feed(er, [10, 12, 11, 15])
    assert all(isnan(x) for x in out[:3])
    assert out[3] == pytest.approx(5 / 7)


def test_efficiency_ratio_extremes():
    trend, chop, flat = EfficiencyRatio(3), EfficiencyRatio(3), EfficiencyRatio(3)
    assert feed(trend, [1, 2, 3, 4])[-1] == pytest.approx(1.0)       # net 3 / noise 3
    assert feed(chop, [10, 11, 10, 11])[-1] == pytest.approx(1 / 3)  # net 1 / noise 3
    assert feed(flat, [5, 5, 5, 5])[-1] == 0.0       # no movement is 0, not NaN or a crash
    assert flat.ready


def test_efficiency_ratio_slides_its_window():
    er = EfficiencyRatio(2)
    feed(er, [0, 10, 20, 21])           # last 3 closes 10, 20, 21: net 11 / noise 11
    assert er.value == pytest.approx(1.0)
    er.update(20)                       # 20, 21, 20: net 0
    assert er.value == pytest.approx(0.0)


# ------------------------------------------------------ Highest / Lowest ----

def test_highest_and_lowest_hand_computed_over_a_sliding_window():
    h, lo = Highest(3), Lowest(3)
    xs = [3, 1, 2, 5, 4]
    hs, ls = feed(h, xs), feed(lo, xs)
    # windows: [3] [3,1] [3,1,2] [1,2,5] [2,5,4]
    assert hs == [3, 3, 3, 5, 5]
    assert ls == [3, 1, 1, 1, 2]
    assert h.ready and lo.ready


def test_highest_and_lowest_report_a_partial_window_value_before_ready():
    # Unlike EMA/SMA/ATR/RSI (NaN until ready), these return the extreme of what
    # they have seen. Pinned so a caller that skips the .ready check is a known
    # risk, not a surprise.
    h = Highest(5)
    h.update(7)
    assert h.value == 7 and not h.ready


# ================================================================ nt8math ====

def test_approx_compare_is_zero_within_the_epsilon_else_the_sign():
    assert APPROX_EPS == 1e-10
    assert approx_compare(1.0, 1.0) == 0
    assert approx_compare(0.0, 9.9e-11) == 0        # inside the epsilon
    assert approx_compare(0.0, 1e-10) == -1         # exactly epsilon is NOT inside
    assert approx_compare(1.0, 2.0) == -1
    assert approx_compare(2.0, 1.0) == 1
    assert approx_compare(1.0 + 5e-11, 1.0) == 0    # float noise is equal


def test_round_to_tick_rounds_to_the_nearest_tick_and_halves_up():
    # tick 0.25: 100.12 is closer to 100.00, 100.13 to 100.25, and the exact half
    # 100.125 (= 400.5 ticks, exactly representable) goes up
    assert round_to_tick(100.12, 0.25) == 100.00
    assert round_to_tick(100.13, 0.25) == 100.25
    assert round_to_tick(100.125, 0.25) == 100.25
    assert round_to_tick(100.25, 0.25) == 100.25
    assert round_to_tick(7.3, 0.0) == 7.3           # no tick size: unchanged
    assert round_to_tick(7.3, -1.0) == 7.3


@pytest.mark.xfail(strict=True, reason=(
    "docstring says round-half-AWAY-from-zero, but floor(x/tick + 0.5) rounds "
    "negative ties toward +inf (-100.125 -> -100.00, not -100.25). Only matters "
    "for negative prices (spreads); futures prices the gb signals see are "
    "positive. Left unfixed pending a decision; strict so a fix shows up."))
def test_round_to_tick_negative_half_goes_away_from_zero():
    assert round_to_tick(-100.125, 0.25) == -100.25


def test_nt8_ema_is_seeded_with_the_first_value():
    # k = 2/(3+1) = 0.5: 10; 10 + .5 * (20 - 10) = 15; 15 + .5 * (40 - 15) = 27.5
    e = Nt8Ema(3)
    assert feed(e, [10, 20, 40]) == pytest.approx([10.0, 15.0, 27.5])


def test_nt8_sma_averages_the_partial_window_from_bar_zero():
    s = Nt8Sma(3)
    # 3 / 1, (3 + 6) / 2, (3 + 6 + 9) / 3, then the window slides to [6, 9, 12]
    assert feed(s, [3, 6, 9, 12]) == pytest.approx([3.0, 4.5, 6.0, 9.0])


def test_the_two_families_differ_exactly_where_the_docstring_says():
    xs = [10, 20, 30]
    nt8_e, ind_e = Nt8Ema(3), EMA(3)
    nt8_s, ind_s = Nt8Sma(3), SMA(3)
    a, b = feed(nt8_e, xs), feed(ind_e, xs)
    c, d = feed(nt8_s, xs), feed(ind_s, xs)
    assert a[0] == 10.0 and isnan(b[0])             # NT8 has a value on bar 0
    assert c[0] == 10.0 and isnan(d[0])
    assert c[2] == pytest.approx(d[2]) == pytest.approx(20.0)   # they agree once full
