"""Shared synthetic tick-stream generator for the property tests (plan task 20).

`streams()` is a hypothesis strategy that draws a day of L1 trades: a random walk
on the tick grid, with duplicate timestamps, bursts, multi-second pauses and the
odd 33-minute gap (longer than the renko-family reset gap), random volumes, and
quotes that sit on the trade price or one tick away so both aggressor sides occur.
"""
from __future__ import annotations

import dataclasses
import os

import numpy as np
from hypothesis import HealthCheck
from hypothesis import strategies as st

from backtester.data import DayL1

from conftest import et_ns, make_day_at

TICK = 0.25

# Default: the SAME 60 examples every run (derandomized, no example database), so a
# red build is reproducible and CI is stable - but it also means these tests never
# explore anything new. HYPOTHESIS_EXPLORE=1 switches to fresh random examples (and
# five times as many) to hunt for new cases; a failure it finds prints the example
# to turn into a hand-computed test.
EXPLORE = bool(os.environ.get("HYPOTHESIS_EXPLORE"))
PROFILE = dict(max_examples=300 if EXPLORE else 60, deadline=None,
               derandomize=not EXPLORE, database=None,
               suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])

GAPS_MS = (0, 0, 1, 1, 50, 400, 3000, 90_000, 600_000, 2_000_000)   # 10 min must NOT reset; 33 min must
# the documented renko-family reset gap, written out here ON PURPOSE: importing
# the builder's own constant would let a change to it move the builder and its
# test together
RESET_GAP_NS = 30 * 60 * 1_000_000_000


GAPS_NO_RESET_MS = tuple(g for g in GAPS_MS if g * 1_000_000 <= RESET_GAP_NS)


@st.composite
def streams(draw, min_n=20, max_n=400, reset_gaps=True):
    """reset_gaps=False leaves out the 33-minute pauses, so no renko-family builder
    resets and none of them leaves a hole in its spans (volume then adds up exactly)."""
    n = draw(st.integers(min_n, max_n))
    steps = draw(st.lists(st.integers(-3, 3), min_size=n, max_size=n))
    gaps = draw(st.lists(st.sampled_from(GAPS_MS if reset_gaps else GAPS_NO_RESET_MS),
                         min_size=n, max_size=n))
    vols = draw(st.lists(st.integers(1, 30), min_size=n, max_size=n))
    ask_up = draw(st.lists(st.integers(0, 1), min_size=n, max_size=n))
    bid_dn = draw(st.lists(st.integers(0, 1), min_size=n, max_size=n))
    p = 4000.0 + np.cumsum(np.asarray(steps)) * TICK
    ts = et_ns("20260202", "09:30") + np.cumsum(np.asarray(gaps, dtype="int64")) * 1_000_000
    d = make_day_at(p, ts, "20260202",
                    ask=p + TICK * np.asarray(ask_up), bid=p - TICK * np.asarray(bid_dn))
    return dataclasses.replace(d, volume=np.asarray(vols, dtype="int64"))


def slice_day(d: DayL1, a: int, b: int) -> DayL1:
    return dataclasses.replace(
        d, ts=d.ts[a:b], price=d.price[a:b], volume=d.volume[a:b], ask=d.ask[a:b],
        bid=d.bid[a:b], ask_size=d.ask_size[a:b], bid_size=d.bid_size[a:b],
        aggr=d.aggr[a:b])


def trending_days(tick: float, dates=("20260202", "20260203", "20260204", "20260205"),
                  seed: int = 1, n_day: int = 7000, n_eve: int = 4000):
    """Plain (non-hypothesis) multi-day data for the strategy smoke test: each day
    file has a 09:30 block and an 18:00 block, 0.2-1.5 s between trades, a random
    walk whose drift flips every 300 events (so there are real trends, pauses and
    reversals), everything scaled to `tick`. Gentle +/-1-tick noise would leave
    most strategies without a single decision, which makes a causality check
    say nothing."""
    rng = np.random.default_rng(seed)
    out, price = [], 10000.0
    for date in dates:
        ps, tss = [], []
        for hhmm, n in (("09:30", n_day), ("18:00", n_eve)):
            drift = np.repeat(rng.choice([-1, 0, 0, 1], size=n // 300 + 1), 300)[:n]
            steps = drift + rng.integers(-3, 4, size=n)
            gaps = rng.choice([0, 200, 400, 800, 1500], size=n, p=[.1, .3, .3, .2, .1])
            tss.append(et_ns(date, hhmm) + np.cumsum(gaps.astype("int64")) * 1_000_000)
            p = price + np.cumsum(steps) * tick
            price = float(p[-1])
            ps.append(p)
        out.append(make_day_at(np.concatenate(ps), np.concatenate(tss), date, tick=tick))
    return out
