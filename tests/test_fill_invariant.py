"""Fill-price invariant: the engine fills on TICKS, never on bars.

Every fill price must trace to the tick at its fill event: that event's trade
price, its prevailing ask or bid (+/- slippage), or the order's own stop/limit
price. Never a bar's open/high/low/close unless it happens to equal one of
those. This is what actually tests "ticks, not OHLC" inside the engine
(plan task 8a): a fuzzer drives market, limit, stop, bracket, stop-move and
reversal orders through the real Catalog + Backtest for EVERY bar type, on a
synthetic raw file with real bid/ask rows, and each fill is checked against the
raw tick at its timestamp.

The checker is proven to bite: a broker patched to fill at a later tick (the
same species of error as filling at a bar's close) is caught.
"""
from __future__ import annotations

import math
import random

import numpy as np
import pytest

from backtester.broker import SimBroker
from backtester.data import Catalog
from backtester.engine import Backtest
from backtester.orders import BUY, OrderType
from backtester.strategy import Strategy

from conftest import write_synthetic_raw as write_raw

DATE = "20250115"
TICK = 0.25

BAR_TYPES = ["30s", "25t", "r4-2", "s8-2", "tb8", "w8"]


# ------------------------------------------------------------------ raw data ---

# ------------------------------------------------------------------- fuzzer ----

class Fuzz(Strategy):
    symbol = "MNQ"
    period = "30s"
    session = None
    qty = 1
    seed = 0

    def on_start(self):
        self.rng = random.Random(self.seed)

    def on_bar(self, bar, bars):
        r, t = self.rng, TICK
        if r.random() < 0.25:
            self._broker.cancel_all(entries_only=True)
        if self.flat:
            x = r.random()
            st, tg = r.randint(6, 20), r.randint(6, 20)
            if x < 0.25:
                (self.buy_bracket if r.random() < .5 else self.sell_bracket)(
                    stop_ticks=st, target_ticks=tg)
            elif x < 0.50:
                k = r.randint(-3, 6)            # negative = marketable at once
                self.buy_limit(bar.close - k * t, stop_ticks=st, target_ticks=tg)
            elif x < 0.65:
                k = r.randint(-3, 6)
                self.sell_limit(bar.close + k * t, stop_ticks=st, target_ticks=tg)
            elif x < 0.80:
                k = r.randint(-2, 6)            # <= 0 triggers at once
                self.buy_stop(bar.close + k * t, stop_ticks=st, target_ticks=tg)
            elif x < 0.90:
                k = r.randint(-2, 6)
                self.sell_stop(bar.close - k * t, stop_ticks=st, target_ticks=tg)
        else:
            x = r.random()
            if x < 0.15:
                self.move_stop_to_breakeven(r.randint(0, 3))
            elif x < 0.25:
                self.close_position()
            elif x < 0.35:
                (self.sell if self.position > 0 else self.buy)(qty=2)   # reverse
            elif x < 0.45:
                (self.buy if self.position > 0 else self.sell)(qty=1)   # scale in


def run_fuzz(tmp_path, period, seed, slip):
    raw = tmp_path / "data" / "MNQ-2025_L1" / f"{DATE}.parquet"
    if not raw.exists():
        write_raw(raw)
    strat = Fuzz()
    strat.seed = seed
    bt = Backtest(strat, period=period, start=DATE, end=DATE, prop=None,
                  slippage_ticks=slip, progress=False,
                  data_root=tmp_path / "data", cache_root=tmp_path / "cache")
    bt.run()
    return bt, bt.catalog.load_day("MNQ", DATE)


# ------------------------------------------------------------------- checker ---

def fill_violations(fills, day, slip):
    """Fills whose price is not derivable from the tick(s) at their timestamp.
    Returns [(fill, [candidate prices])]."""
    s = slip * TICK
    bad = []
    for f in fills:
        lo = int(np.searchsorted(day.ts, f.ts, side="left"))
        hi = int(np.searchsorted(day.ts, f.ts, side="right"))
        cands = []
        for i in range(lo, hi):                      # events sharing the stamp
            p, ask, bid = float(day.price[i]), float(day.ask[i]), float(day.bid[i])
            if f.side == BUY:
                cands += [p + s]                     # no quote yet: trade price + slip
                if not math.isnan(ask):
                    cands += [ask + s, ask]          # market/stop; marketable limit
            else:
                cands += [p - s]
                if not math.isnan(bid):
                    cands += [bid - s, bid]
        if f.order.type is not OrderType.MARKET:     # passive limit / stop level
            cands.append(float(f.order.price))
        if not any(abs(f.price - c) < 1e-9 for c in cands):
            bad.append((f, cands))
    return bad


# --------------------------------------------------------------------- tests ---

@pytest.mark.parametrize("slip", [0.0, 1.0])
@pytest.mark.parametrize("period", BAR_TYPES)
def test_every_fill_traces_to_a_tick(tmp_path, period, slip):
    seeds = (1, 2, 3)
    n_fills = 0
    for seed in seeds:
        bt, day = run_fuzz(tmp_path, period, seed, slip)
        bad = fill_violations(bt.broker.fills, day, slip)
        assert not bad, (period, seed, slip, bad[0])
        n_fills += len(bt.broker.fills)
    # not vacuous: the fuzzer really trades on every bar type
    assert n_fills >= 150, (period, slip, n_fills)


def test_the_fuzzer_exercises_every_fill_path(tmp_path):
    """Market, marketable limit, passive limit, stop and bracket-child fills
    all occur; otherwise the invariant above would only be testing one."""
    seen = {"market": 0, "limit_quote": 0, "limit_own": 0, "stop": 0,
            "bracket_child": 0, "exit_market": 0}
    for period in ("30s", "r4-2"):
        for seed in (1, 2, 3):
            bt, day = run_fuzz(tmp_path, period, seed, 0.0)
            for f in bt.broker.fills:
                o = f.order
                i = int(np.searchsorted(day.ts, f.ts, side="left"))
                if o.type is OrderType.MARKET:
                    seen["exit_market" if o.is_exit else "market"] += 1
                elif o.type is OrderType.LIMIT:
                    q = day.ask[i] if f.side == BUY else day.bid[i]
                    seen["limit_quote" if abs(f.price - q) < 1e-9 else "limit_own"] += 1
                else:
                    seen["stop"] += 1
                seen["bracket_child"] += o.tag in ("stop", "target")
    assert all(v >= 5 for v in seen.values()), seen


def test_the_checker_rejects_a_fill_at_a_later_ticks_price(tmp_path):
    """Prove the invariant can fail: make the broker fill every market/stop
    order at the trade price 30 events LATER (the same species of error as
    filling at a bar's close, which no single tick can justify)."""
    def later_tick(self, o, idx):
        d = self._day
        return float(d.price[min(idx + 30, len(d.price) - 1)])
    mp = pytest.MonkeyPatch()
    mp.setattr(SimBroker, "_fill_price", later_tick)
    try:
        bt, day = run_fuzz(tmp_path, "30s", 1, 0.0)
        bad = fill_violations(bt.broker.fills, day, 0.0)
    finally:
        mp.undo()
    assert len(bad) > 20


def test_the_checker_accepts_a_correct_fill_and_rejects_a_bar_ohlc_price(tmp_path):
    from types import SimpleNamespace
    raw = tmp_path / "data" / "MNQ-2025_L1" / f"{DATE}.parquet"
    write_raw(raw)
    day = Catalog(tmp_path / "data", tmp_path / "cache").load_day("MNQ", DATE)
    i = 500
    order = SimpleNamespace(type=OrderType.MARKET, price=float("nan"))
    good = SimpleNamespace(ts=int(day.ts[i]), side=BUY, price=float(day.ask[i]),
                           order=order)
    assert fill_violations([good], day, 0.0) == []
    # a price 7 ticks away from anything at that tick, like a bar's high would be
    off = SimpleNamespace(ts=int(day.ts[i]), side=BUY,
                          price=float(day.ask[i]) + 7 * TICK, order=order)
    assert len(fill_violations([off], day, 0.0)) == 1
    # slippage is respected: ask + 1 tick passes only when slip says so
    slipped = SimpleNamespace(ts=int(day.ts[i]), side=BUY,
                              price=float(day.ask[i]) + TICK, order=order)
    assert len(fill_violations([slipped], day, 0.0)) == 1
    assert fill_violations([slipped], day, 1.0) == []
