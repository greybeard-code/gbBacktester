"""Incremental (streaming) indicators, updated once per bar — NT8-style.

Each indicator exposes .update(...) returning the new value, .value for the
latest value, and .ready once enough bars have been seen.
"""
from __future__ import annotations

from collections import deque

import math


class EMA:
    def __init__(self, period: int):
        self.period = period
        self.k = 2.0 / (period + 1)
        self.value = math.nan
        self._seed: list[float] = []

    @property
    def ready(self) -> bool:
        return not math.isnan(self.value)

    def update(self, price: float) -> float:
        if math.isnan(self.value):
            self._seed.append(price)
            if len(self._seed) >= self.period:
                self.value = sum(self._seed) / len(self._seed)
                self._seed.clear()
        else:
            self.value += self.k * (price - self.value)
        return self.value


class SMA:
    def __init__(self, period: int):
        self.period = period
        self._win: deque[float] = deque(maxlen=period)
        self._sum = 0.0
        self.value = math.nan

    @property
    def ready(self) -> bool:
        return len(self._win) == self.period

    def update(self, price: float) -> float:
        if len(self._win) == self.period:
            self._sum -= self._win[0]
        self._win.append(price)
        self._sum += price
        if self.ready:
            self.value = self._sum / self.period
        return self.value


class Bollinger:
    """SMA +/- mult * population std-dev (NT8 Bollinger). update(close)
    once per bar; read .middle/.upper/.lower."""

    def __init__(self, period: int, mult: float = 2.0):
        self.period = period
        self.mult = mult
        self._win: deque[float] = deque(maxlen=period)
        self.middle = math.nan
        self.upper = math.nan
        self.lower = math.nan

    @property
    def ready(self) -> bool:
        return len(self._win) == self.period

    def update(self, price: float) -> float:
        self._win.append(price)
        if self.ready:
            m = self._sum() / self.period
            var = sum((p - m) ** 2 for p in self._win) / self.period
            sd = math.sqrt(var)
            self.middle = m
            self.upper = m + self.mult * sd
            self.lower = m - self.mult * sd
        return self.middle

    def _sum(self) -> float:
        return sum(self._win)


class ATR:
    """Wilder's ATR. update(high, low, close) once per bar."""

    def __init__(self, period: int):
        self.period = period
        self.value = math.nan
        self._prev_close = math.nan
        self._seed: list[float] = []

    @property
    def ready(self) -> bool:
        return not math.isnan(self.value)

    def update(self, high: float, low: float, close: float) -> float:
        if math.isnan(self._prev_close):
            tr = high - low
        else:
            tr = max(high - low, abs(high - self._prev_close),
                     abs(low - self._prev_close))
        self._prev_close = close
        if math.isnan(self.value):
            self._seed.append(tr)
            if len(self._seed) >= self.period:
                self.value = sum(self._seed) / len(self._seed)
                self._seed.clear()
        else:
            self.value += (tr - self.value) / self.period
        return self.value


class RSI:
    """Wilder's RSI. update(close) once per bar."""

    def __init__(self, period: int):
        self.period = period
        self.value = math.nan
        self._prev = math.nan
        self._avg_gain = math.nan
        self._avg_loss = math.nan
        self._seed_g: list[float] = []
        self._seed_l: list[float] = []

    @property
    def ready(self) -> bool:
        return not math.isnan(self.value)

    def update(self, close: float) -> float:
        if math.isnan(self._prev):
            self._prev = close
            return self.value
        chg = close - self._prev
        self._prev = close
        gain, loss = max(chg, 0.0), max(-chg, 0.0)
        if math.isnan(self._avg_gain):
            self._seed_g.append(gain)
            self._seed_l.append(loss)
            if len(self._seed_g) >= self.period:
                self._avg_gain = sum(self._seed_g) / self.period
                self._avg_loss = sum(self._seed_l) / self.period
                self._seed_g.clear()
                self._seed_l.clear()
            else:
                return self.value
        else:
            self._avg_gain += (gain - self._avg_gain) / self.period
            self._avg_loss += (loss - self._avg_loss) / self.period
        if self._avg_loss == 0:
            self.value = 100.0
        else:
            self.value = 100.0 - 100.0 / (1.0 + self._avg_gain / self._avg_loss)
        return self.value


class EfficiencyRatio:
    """Kaufman's Efficiency Ratio: |net change| / sum of |bar changes|.

    0 = pure chop, 1 = perfect trend. TSM guidance: < 0.12 suppress entries
    (chop), > 0.4 trending (tighten trails). update(close) once per bar.
    """

    def __init__(self, period: int = 20):
        self.period = period
        self._win: deque[float] = deque(maxlen=period + 1)
        self.value = math.nan

    @property
    def ready(self) -> bool:
        return len(self._win) == self.period + 1

    def update(self, close: float) -> float:
        self._win.append(close)
        if self.ready:
            w = list(self._win)
            noise = sum(abs(w[i] - w[i - 1]) for i in range(1, len(w)))
            self.value = abs(w[-1] - w[0]) / noise if noise > 0 else 0.0
        return self.value


class KAMA:
    """Kaufman's Adaptive Moving Average, ported bar-for-bar from NT8's
    @KAMA.cs (NinjaTrader 8, 2025) — the indicator KamaRegimePro consumes.

    Signature order follows NT8's: KAMA(fast, period, slow), the
    efficiency-ratio period in the MIDDLE slot. Two NT8 warmup quirks are
    reproduced deliberately (see research/KamaRegime_spec.md §2):

      * for the first `period` bars the value IS the input price — the
        adaptive recursion only starts at bar `period`, seeded off that;
      * NT8 stores the raw PRICE (not 0) as bar 0's |diff| (@KAMA.cs:56).
        That value leaves the rolling noise window exactly at bar `period`,
        one bar before it could ever be read, so it never reaches a
        published value — but it is carried here anyway, because the noise
        term is a running sum and the subtraction has to cancel the same
        double NT8 added.

    update(close) once per bar; `ready` once the adaptive recursion is live.
    """

    def __init__(self, fast: int = 2, period: int = 10, slow: int = 30):
        if period < 5:
            raise ValueError(f"KAMA period must be >= 5 (NT8 range), got {period}")
        self.fast = fast
        self.period = period
        self.slow = slow
        self._fast_cf = 2.0 / (fast + 1)
        self._slow_cf = 2.0 / (slow + 1)
        self.value = math.nan
        self._bar = -1                                   # NT8 CurrentBar
        self._in: deque[float] = deque(maxlen=period + 1)   # Input[0..period]
        self._diffs: deque[float] = deque(maxlen=period + 1)
        self._noise = 0.0                                # NT8 SUM(diffSeries, period)
        self._prev_in = math.nan

    @property
    def ready(self) -> bool:
        """True once past NT8's `CurrentBar < Period` seeding branch."""
        return self._bar >= self.period

    def update(self, price: float) -> float:
        self._bar += 1
        diff = abs(price - self._prev_in) if self._bar > 0 else price
        self._prev_in = price
        self._in.append(price)
        self._diffs.append(diff)
        # NT8 SUM: Value[0] = Input[0] + Value[1] - Input[Period]. Same
        # running-sum form and evaluation order, so the doubles match.
        drop = self._diffs[0] if self._bar >= self.period else 0.0
        self._noise = (diff + self._noise) - drop

        if self._bar < self.period:
            self.value = price
            return self.value
        if self._noise == 0:
            return self.value                            # Value[0] = Value[1]
        signal = abs(price - self._in[0])
        # ((signal/noise) * (fastCf - slowCf) + slowCf) ** 2 — C# precedence;
        # x*x is Math.Pow(x, 2) to the same single rounding.
        sc = signal / self._noise * (self._fast_cf - self._slow_cf) + self._slow_cf
        prev = self.value
        self.value = prev + sc * sc * (price - prev)
        return self.value


class KamaRegime:
    """The tradable half of KamaRegimePro (nt8 code/GodZillaKilla/indicators/
    kamareginepro.cs): a bull/bear trend regime from the sign of the KAMA
    slope, with a sticky flat band and a confirm-bars debounce.

    Slope is measured in TICKS PER BAR, so `flat_threshold` is only
    comparable across instruments after dividing by tick size — and it is
    calibrated for time bars. On renko/TBars closes every with-trend bar
    moves a fixed number of ticks, so the band rarely binds; see
    research/KamaRegime_spec.md §5 before using it on those.

    The background paint, the ★ flip marker and the WPF readout card are
    chart cosmetics and are not ported. update(close) once per bar, then read:

      value           +1 bull / -1 bear / 0 pre-init (still in warmup)
      flipped         True on the bar a new regime commits (RegimeSignal)
      slope_ticks     KAMA[0] - KAMA[1], in ticks (NaN during warmup)
      bars_in_regime  bars the committed regime has held
    """

    def __init__(self, tick_size: float, fast: int = 2, period: int = 10,
                 slow: int = 30, flat_threshold: float = 0.5,
                 confirm_bars: int = 1, warmup_bars: int | None = None):
        self.tick_size = tick_size
        self.flat_threshold = flat_threshold
        self.confirm_bars = confirm_bars
        # KamaRegimePro: _warmupBars = KamaPeriod + KamaSlow + 5
        self.warmup_bars = period + slow + 5 if warmup_bars is None else warmup_bars
        self.kama = KAMA(fast, period, slow)
        self.value = 0
        self.flipped = False
        self.slope_ticks = math.nan
        self.bars_in_regime = 0
        self._bar = -1
        self._pending = 0
        self._pending_n = 0
        self._last_committed = 0

    @property
    def ready(self) -> bool:
        return self.value != 0

    def update(self, close: float) -> int:
        self._bar += 1
        prev_kama = self.kama.value
        # the sub-indicator updates on EVERY bar, warmup included — the
        # regime block below is what NT8 skips (kamareginepro.cs:189)
        kama = self.kama.update(close)
        self.flipped = False
        if self._bar < self.warmup_bars:
            self.slope_ticks = math.nan
            return self.value
        self.slope_ticks = (kama - prev_kama) / self.tick_size

        # sticky flat band: outside +-threshold commits a direction, inside
        # holds the last one. The FIRST commit has no band — sign only.
        if self.value == 0:
            raw = 1 if self.slope_ticks >= 0 else -1
        else:
            raw = self.value
            if self.slope_ticks > self.flat_threshold:
                raw = 1
            elif self.slope_ticks < -self.flat_threshold:
                raw = -1

        if raw == self._pending:
            self._pending_n += 1
        else:
            self._pending, self._pending_n = raw, 1

        if raw == self.value:
            self.bars_in_regime += 1
        elif self._pending_n >= max(1, self.confirm_bars):
            self.value = raw
            self.bars_in_regime = 1

        self.flipped = self.value != self._last_committed and self.value != 0
        self._last_committed = self.value
        return self.value


class Highest:
    def __init__(self, period: int):
        self.period = period
        self._win: deque[float] = deque(maxlen=period)
        self.value = math.nan

    @property
    def ready(self) -> bool:
        return len(self._win) == self.period

    def update(self, price: float) -> float:
        self._win.append(price)
        self.value = max(self._win)
        return self.value


class Lowest:
    def __init__(self, period: int):
        self.period = period
        self._win: deque[float] = deque(maxlen=period)
        self.value = math.nan

    @property
    def ready(self) -> bool:
        return len(self._win) == self.period

    def update(self, price: float) -> float:
        self._win.append(price)
        self.value = min(self._win)
        return self.value
