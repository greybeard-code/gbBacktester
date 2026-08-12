"""Wave Bars geometry, seed behaviour, volume identity, and cross-day carry.

Port spec: `nt8 code/HiLoRider/WaveBars/WaveBars.md`. Wave Bars is the TBars
algorithm with three differences, so this file concentrates on those (the
shared hot loop is exercised by test_tbars.py, and `build_tbar_bars` was
verified bit-identical across the `_build_tbar_family_core` extraction):

    1. volume is NOT double-counted -> an exact sum identity (§5.1)
    2. the seed band is symmetric +/-trend, no direction carry -> no doji
       stub, no inverted band, and a cheap first reversal after a reset (§5.2)
    3. the trend offset is clamped to >= 1 tick -> `w1` is legal (§2)

All hand-computed cases use wave_ticks=4 with tick 0.25, so the three derived
distances are the same small round numbers test_tbars.py uses:

    trend offset  = max(1, 4 // 2) * 0.25 = 0.50   (with-trend continuation)
    reversal      =         (4 * 2) * 0.25 = 2.00   (against-trend)
    open offset   =          4      * 0.25 = 1.00   (phantom open, back from close)
"""
import numpy as np
import pytest

from backtester.data import (
    DayL1, RENKO_RESET_GAP_NS, build_tbar_bars, build_wave_bars,
    classify_aggressor,
)
from backtester.strategy import BarSpec, parse_barspec

from conftest import make_day

N, TICK = 4, 0.25
TREND, REV, OPEN_OFF = 0.50, 2.00, 1.00


def _day(ts_s, prices, volumes=None, tick=TICK):
    """DayL1 with explicit timestamps — make_day is 1 tick/second, too coarse
    for the session-gap cases below."""
    ts = (np.asarray(ts_s, dtype="float64") * 1e9).astype("int64")
    p = np.asarray(prices, dtype="float64")
    v = (np.asarray(volumes, dtype="int64") if volumes is not None
         else np.ones(len(p), dtype="int64"))
    ask, bid = p + tick, p - tick
    return DayL1("20260101", ts, p, v, ask, bid,
                 np.ones(len(p), dtype="int64"), np.ones(len(p), dtype="int64"),
                 classify_aggressor(p, ask, bid))


def _ha(o, h, l, c):
    """HA close, tick-rounded the way NT8 stores it (banker's — the rounding
    lives INSIDE the state loop, see WaveBars.md §4.2)."""
    return float(np.round((o + h + l + c) * 0.25 / TICK) * TICK)


def _walk(seed, n_ticks, tick=TICK, gaps=0):
    rng = np.random.default_rng(seed)
    px, prices = 100.0, []
    for _ in range(n_ticks):
        px += rng.choice([-2, -1, -1, 0, 1, 1, 2]) * tick
        prices.append(round(px, 6))
    step = np.full(n_ticks, 1, dtype="int64")
    if gaps:
        for g in rng.choice(np.arange(1, n_ticks), size=gaps, replace=False):
            step[g] = RENKO_RESET_GAP_NS // 1_000_000_000 + 60
    return _day(np.cumsum(step), prices,
                rng.integers(1, 20, size=n_ticks).astype("int64"), tick)


# ---------------- the seed: symmetric band, no doji stub -------------------

def test_fresh_seed_opens_a_real_band_not_a_collapsed_one():
    # TBars seeds bar_max == bar_min == open (bar_dir is 0), so the next
    # differing tick completes a zero-range doji. Wave seeds +/-trend, so the
    # same ticks leave the bar still forming.
    day = make_day([100.0, 100.25, 100.5])
    bars = build_wave_bars(day, N, TICK)
    assert len(bars) == 0                       # nothing broke 100.50 / 99.50
    _, bar_max, bar_min = bars.end_state[:3]
    assert bar_max == pytest.approx(100.0 + TREND)
    assert bar_min == pytest.approx(100.0 - TREND)

    assert len(build_tbar_bars(day, N, TICK)) == 1   # TBars: the doji stub


def test_first_bar_is_a_real_bar_with_hand_computed_ohlc():
    # Seed at 100.0 -> band [99.50, 100.50]. Tick 100.75 breaks up (strict),
    # clamping the close to bar_max exactly.
    #   run_hi/run_lo over the updating ticks (index 1 only) = 100.25 / 100.0
    #   high = c1 = 100.50 (max_exc replaces), low = run_lo = 100.0
    #   close = (100.0 + 100.50 + 100.0 + 100.50)/4 = 100.25
    bars = build_wave_bars(make_day([100.0, 100.25, 100.75]), N, TICK)
    assert len(bars) == 1
    assert bars.open[0] == pytest.approx(100.0)
    assert bars.high[0] == pytest.approx(100.5)
    assert bars.low[0] == pytest.approx(100.0)
    assert bars.close[0] == pytest.approx(_ha(100.0, 100.5, 100.0, 100.5))
    assert bars.close[0] == pytest.approx(100.25)
    # ...and it is NOT the doji TBars emits from the same ticks
    assert bars.high[0] != bars.low[0]

    # successor state: phantom open 1.00 back from the break, HA open is the
    # midpoint of that and the close just stored -> (99.50 + 100.25)/2 = 99.875
    # = 399.5 ticks, an exact midpoint, and NT8 rounds half-to-EVEN -> 100.00
    bar_open, bar_max, bar_min, bar_dir = bars.end_state[:4]
    assert bar_open == pytest.approx(100.0)
    assert bar_dir == 1
    assert bar_max == pytest.approx(100.5 + TREND)   # 101.00, with-trend
    assert bar_min == pytest.approx(100.5 - REV)     # 98.50, against


def test_breakout_is_strict_a_tick_on_the_threshold_does_not_complete():
    assert len(build_wave_bars(make_day([100.0, 100.5]), N, TICK)) == 0
    assert len(build_wave_bars(make_day([100.0, 100.75]), N, TICK)) == 1
    assert len(build_wave_bars(make_day([100.0, 99.5]), N, TICK)) == 0
    assert len(build_wave_bars(make_day([100.0, 99.25]), N, TICK)) == 1


# ---------------- the 4x asymmetry, and its one-bar suspension -------------

def test_reversal_costs_four_times_a_continuation_mid_run():
    # After the first (up) bar: bar_max 101.00, bar_min 98.50 from c1 100.50.
    # 99.50 is a full 1.00 against the trend and still does not complete.
    assert len(build_wave_bars(
        make_day([100.0, 100.25, 100.75, 99.5]), N, TICK)) == 1
    bars = build_wave_bars(make_day([100.0, 100.25, 100.75, 98.25]), N, TICK)
    assert len(bars) == 2
    assert bars.low[1] == pytest.approx(98.5)        # clamped to bar_min
    assert (REV / TREND) == 4.0


def test_first_bar_after_a_seed_can_reverse_at_trend_distance():
    # The symmetric seed suspends the asymmetry for exactly one bar: a DOWN
    # break needs only `trend` (0.50), not `reversal` (2.00). Faithful — the
    # C# seeds `open +/- trendOffset` with no direction (WaveBars.md §5.2).
    bars = build_wave_bars(make_day([100.0, 99.25]), N, TICK)
    assert len(bars) == 1
    assert bars.open[0] == pytest.approx(100.0)
    assert bars.high[0] == pytest.approx(100.0)      # run_hi, no updating ticks
    assert bars.low[0] == pytest.approx(99.5)        # clamped to bar_min = trend
    assert bars.close[0] == pytest.approx(_ha(100.0, 100.0, 99.5, 99.5))
    assert bars.close[0] == pytest.approx(99.75)
    assert bars.end_state[3] == -1
    # the same distance mid-run does nothing (see the test above)


def test_reversal_is_cheap_again_immediately_after_a_gap_reseed():
    gap = RENKO_RESET_GAP_NS // 1_000_000_000 + 60
    # up run, halt, then a small down move that only clears `trend`
    ts = [0, 1, 2, 2 + gap, 3 + gap]
    prices = [100.0, 100.25, 100.75, 105.0, 104.25]
    bars = build_wave_bars(_day(ts, prices), N, TICK)
    # bar 0 breaks up, bar 1 is the forming bar abandoned at the halt, bar 2 is
    # the post-reset one: seeded at 105.0, broken DOWN at 104.50 — only `trend`
    # away, even though the run into the halt was up.
    assert len(bars) == 3
    assert bars.open[2] == pytest.approx(105.0)
    assert bars.low[2] == pytest.approx(105.0 - TREND)
    assert bars.close[2] == pytest.approx(_ha(105.0, 105.0, 104.5, 104.5))


# ---------------- no inverted band, ever ----------------------------------

def _assert_ohlc_sane(bars, label):
    for i in range(len(bars)):
        o, h, l, c = bars.open[i], bars.high[i], bars.low[i], bars.close[i]
        assert l <= h, f"{label} bar {i}: low {l} > high {h}"
        assert l - 1e-9 <= o <= h + 1e-9, f"{label} bar {i}: open {o} outside [{l},{h}]"
        assert l - 1e-9 <= c <= h + 1e-9, f"{label} bar {i}: close {c} outside [{l},{h}]"


def test_gap_reseed_after_a_down_run_cannot_invert_the_band():
    # This is exactly the case that breaks TBars: the prior session ends DOWN,
    # the DLL seeds bar_max = open - trend < bar_min = open + trend, both tests
    # pass at once, and it emits a bar whose open sits ABOVE its own high.
    # Wave never consults bar_dir at the seed, so it is unreachable here.
    gap = RENKO_RESET_GAP_NS // 1_000_000_000 + 60
    ts = [0, 1, 2, 3, 3 + gap, 4 + gap, 5 + gap]
    prices = [100.0, 100.25, 97.5, 97.0, 98.0, 98.0, 98.75]
    bars = build_wave_bars(_day(ts, prices), N, TICK)
    _assert_ohlc_sane(bars, "wave gap reseed")

    faithful = build_tbar_bars(_day(ts, prices), N, TICK,
                               reset_carries_dir=True)
    assert any(faithful.open[i] > faithful.high[i] + 1e-9
               for i in range(len(faithful))), \
        "the TBars bug this variant is free of should still reproduce"


def test_band_stays_ordered_and_ohlc_sane_across_random_walks_with_gaps():
    for seed in range(12):
        for tick, n in ((0.25, 4), (0.10, 37), (0.25, 120)):
            bars = build_wave_bars(_walk(seed, 3000, tick, gaps=4), n, tick)
            assert len(bars) > 3
            _assert_ohlc_sane(bars, f"walk seed={seed} N={n}")
            _, bar_max, bar_min = bars.end_state[:3]
            assert bar_max > bar_min, f"inverted band: seed={seed} N={n}"


def test_ha_output_always_lands_on_the_tick_grid():
    for tick, n in ((0.25, 4), (0.10, 37)):
        bars = build_wave_bars(_walk(3, 4000, tick, gaps=2), n, tick)
        for arr in (bars.open, bars.high, bars.low, bars.close):
            off = np.abs(arr / tick - np.round(arr / tick))
            assert off.max() < 1e-6, "a bar price left the tick grid"


# ---------------- volume: an exact identity, unlike TBars -----------------

def test_spans_are_contiguous_and_never_overlap():
    day = make_day([100.0, 100.75, 101.25, 101.75, 99.0, 98.0, 97.0, 96.0])
    bars = build_wave_bars(day, N, TICK)
    assert len(bars) >= 3
    assert list(bars.i1[:-1]) == list(bars.i0[1:])
    assert bars.i0[0] == 0


def test_bar_volume_sums_to_traded_volume_exactly():
    # Wave passes 0 to UpdateBar on the completing tick and the real volume to
    # AddBar, so the breakout tick lands in the NEW bar only — this repo's own
    # [i0, i1) convention. Nothing is lost or double-counted; the only shortfall
    # is the bar still forming when the data ends. TBars cannot make this
    # assertion (NT8 counts the breakout tick twice, TBars_spec.md §8.1).
    for seed in (0, 1, 2, 5):
        day = _walk(seed, 2000, gaps=2)
        bars = build_wave_bars(day, N, TICK)
        assert len(bars) > 3
        forming = int(bars.i1[-1])
        assert (bars.volume.sum() + day.volume[forming:].sum()
                == day.volume.sum())
        assert (bars.buy_volume.sum() + bars.sell_volume.sum()
                <= bars.volume.sum())
        for i in range(len(bars)):
            lo, hi = int(bars.i0[i]), int(bars.i1[i])
            assert bars.volume[i] == day.volume[lo:hi].sum()


# ---------------- the >= 1 tick trend clamp -------------------------------

def test_trend_offset_is_clamped_to_at_least_one_tick():
    # `max(1, N // 2)`: N=1..3 all collapse to a 1-tick trend offset, so w1 is
    # legal and non-degenerate. TBars has no clamp, which is why tb1 is
    # rejected outright (a zero-tick threshold = a bar per uptick).
    for n in (1, 2, 3):
        bars = build_wave_bars(make_day([100.0, 100.0]), n, TICK)
        bar_open, bar_max, bar_min = bars.end_state[:3]
        assert bar_max - bar_open == pytest.approx(TICK), f"N={n}"
        assert bar_open - bar_min == pytest.approx(TICK), f"N={n}"
    # and N=4 is the first value where the trend offset actually grows
    bars = build_wave_bars(make_day([100.0, 100.0]), 4, TICK)
    assert bars.end_state[1] - bars.end_state[0] == pytest.approx(2 * TICK)


# ---------------- cross-day carry ------------------------------------------

def test_carry_across_a_day_boundary_matches_one_continuous_build():
    # Day FILES are ET calendar days; an overnight session runs through
    # midnight ET with no real gap, so splitting there must be invisible.
    prices = [100.0, 100.25, 100.75, 101.25, 101.0, 99.0, 98.5, 97.0,
              96.5, 96.0, 97.5, 98.5, 99.5, 100.5, 101.5]
    ts = list(range(len(prices)))
    whole = build_wave_bars(_day(ts, prices), N, TICK)

    cut = 6
    a = build_wave_bars(_day(ts[:cut], prices[:cut]), N, TICK)
    b = build_wave_bars(_day(ts[cut:], prices[cut:]), N, TICK,
                        carry=a.end_state)

    assert len(a) + len(b) == len(whole)
    for col in ("open", "high", "low", "close"):
        joined = list(getattr(a, col)) + list(getattr(b, col))
        assert joined == pytest.approx(list(getattr(whole, col))), col


def test_carry_preserves_volume_across_a_day_boundary():
    # A bar still forming at the end of a day file must report ALL its volume
    # on the day it completes, not just the post-boundary part.
    prices = [100.0, 100.25, 100.75, 101.25, 101.0, 99.0, 98.5, 97.0,
              96.5, 96.0, 97.5, 98.5, 99.5, 100.5, 101.5]
    vols = [3, 7, 2, 9, 4, 6, 1, 8, 5, 2, 7, 3, 9, 4, 6]
    ts = list(range(len(prices)))
    whole = build_wave_bars(_day(ts, prices, vols), N, TICK)

    cut = 6
    a = build_wave_bars(_day(ts[:cut], prices[:cut], vols[:cut]), N, TICK)
    b = build_wave_bars(_day(ts[cut:], prices[cut:], vols[cut:]), N, TICK,
                        carry=a.end_state)

    assert list(a.volume) + list(b.volume) == list(whole.volume)
    assert list(a.buy_volume) + list(b.buy_volume) == list(whole.buy_volume)
    assert list(a.sell_volume) + list(b.sell_volume) == list(whole.sell_volume)


def test_a_split_random_walk_reproduces_the_continuous_build():
    for seed in (11, 12, 13):
        day = _walk(seed, 2500, gaps=1)
        whole = build_wave_bars(day, N, TICK)
        cut = 1200
        head = DayL1(day.date, day.ts[:cut], day.price[:cut], day.volume[:cut],
                     day.ask[:cut], day.bid[:cut], day.ask_size[:cut],
                     day.bid_size[:cut], day.aggr[:cut])
        tail = DayL1(day.date, day.ts[cut:], day.price[cut:], day.volume[cut:],
                     day.ask[cut:], day.bid[cut:], day.ask_size[cut:],
                     day.bid_size[cut:], day.aggr[cut:])
        a = build_wave_bars(head, N, TICK)
        b = build_wave_bars(tail, N, TICK, carry=a.end_state)
        assert len(a) + len(b) == len(whole), f"seed={seed}"
        for col in ("open", "high", "low", "close", "volume"):
            joined = list(getattr(a, col)) + list(getattr(b, col))
            assert joined == pytest.approx(list(getattr(whole, col))), col


def test_carried_bar_may_complete_on_the_very_first_tick_of_the_next_day():
    # The carried bar was opened on the PRIOR day, so index 0 here is an
    # ordinary updating tick and must stay eligible to complete it at once
    # (contrast the fresh seed, whose own opening tick cannot complete it).
    carry = (99.5, 100.5, 98.0, 1, 100.0, 99.0, 0, 0, 0)   # bar_max 100.5
    bars = build_wave_bars(_day([0, 1], [100.75, 101.0]), N, TICK, carry=carry)
    assert len(bars) >= 1
    assert bars.i0[0] == 0 and bars.i1[0] == 0     # completed before any tick
    assert bars.high[0] == pytest.approx(100.5)


def test_session_gap_closes_the_forming_bar_as_a_partial():
    # The C# does not close the forming bar at a reset — it just AddBars a
    # fresh one, so the forming bar is completed by abandonment and keeps the
    # HA close it last held.
    gap = RENKO_RESET_GAP_NS // 1_000_000_000 + 60
    ts = [0, 1, 2, 2 + gap, 3 + gap, 4 + gap]
    prices = [100.0, 100.25, 100.4, 105.0, 105.25, 105.75]
    bars = build_wave_bars(_day(ts, prices), N, TICK)
    assert len(bars) >= 1
    assert bars.i1[0] == 3                         # stops at the last pre-gap tick
    assert bars.ts_end[0] == 2 * 1_000_000_000
    _assert_ohlc_sane(bars, "partial at halt")


# ---------------- BarSpec / dispatch wiring --------------------------------

def test_barspec_key_and_parse_roundtrip():
    spec = parse_barspec("w120")
    assert spec.kind == "wave" and spec.wave_ticks == 120
    assert spec.key == "w120"
    assert BarSpec("wave", wave_ticks=8).key == "w8"
    # w1 is legal (trend clamps to 1 tick) where tb1 is not
    assert parse_barspec("w1").wave_ticks == 1
    with pytest.raises(ValueError):
        parse_barspec("tb1")
    # and the grammar does not shadow the neighbouring ones
    assert parse_barspec("120t").kind == "tick"
    assert parse_barspec("tb120").kind == "tbars"


def test_build_bars_dispatches_on_kind():
    from backtester.data import build_bars
    day = make_day([100.0, 100.25, 100.75, 101.25])
    viaspec = build_bars(day, parse_barspec("w4"), TICK)
    direct = build_wave_bars(day, 4, TICK)
    assert list(viaspec.close) == pytest.approx(list(direct.close))
    # and w4 is genuinely a different series from tb4 on the same ticks
    assert list(build_bars(day, parse_barspec("tb4"), TICK).close) != \
        pytest.approx(list(direct.close))
