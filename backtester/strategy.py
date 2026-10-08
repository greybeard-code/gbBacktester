"""Strategy base class and bar history container."""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

from .orders import BUY, SELL, BracketSpec, Fill, Order, OrderType
from .risk import ProfitBankPolicy, RiskBudget


@dataclass
class Bar:
    ts: int              # close time, ns UTC
    open: float
    high: float
    low: float
    close: float
    volume: int
    index: int           # global bar index across the run
    buy_volume: int = 0  # aggressor buys (trades at/above the ask)
    sell_volume: int = 0 # aggressor sells (trades at/below the bid)

    @property
    def delta(self) -> int:
        """Order-flow delta: aggressor buy volume minus sell volume."""
        return self.buy_volume - self.sell_volume


class _GrowArray:
    def __init__(self, dtype):
        self._buf = np.empty(4096, dtype=dtype)
        self.n = 0

    def append(self, v):
        if self.n == len(self._buf):
            self._buf = np.concatenate([self._buf, np.empty_like(self._buf)])
        self._buf[self.n] = v
        self.n += 1

    @property
    def values(self) -> np.ndarray:
        return self._buf[:self.n]


class BarHistory:
    """Append-only bar series spanning the whole run (all days)."""

    def __init__(self):
        self.ts = _GrowArray("int64")
        self.open = _GrowArray("float64")
        self.high = _GrowArray("float64")
        self.low = _GrowArray("float64")
        self.close = _GrowArray("float64")
        self.volume = _GrowArray("int64")
        self.delta = _GrowArray("int64")
        self.cum_delta = _GrowArray("int64")   # session-cumulative delta
        self._cum = 0

    def append(self, bar: Bar) -> None:
        self.ts.append(bar.ts)
        self.open.append(bar.open)
        self.high.append(bar.high)
        self.low.append(bar.low)
        self.close.append(bar.close)
        self.volume.append(bar.volume)
        self.delta.append(bar.delta)
        self._cum += bar.delta
        self.cum_delta.append(self._cum)

    def reset_cum_delta(self) -> None:
        """Called by the engine at each session start."""
        self._cum = 0

    def __len__(self) -> int:
        return self.ts.n

    @property
    def closes(self) -> np.ndarray:
        return self.close.values

    @property
    def highs(self) -> np.ndarray:
        return self.high.values

    @property
    def lows(self) -> np.ndarray:
        return self.low.values


class Strategy:
    """Subclass this. Set class attributes, implement on_bar().

    Times are "HH:MM" in US/Eastern (the user's PC/NT8 timezone).
    """

    symbol: str = "MNQ"
    period: str = "1m"                      # e.g. "30s", "1m", "5m", "1h"
    session: tuple[str, str] | None = ("09:30", "16:00")
    flat_at_session_end: bool = True
    qty: int = 1
    # Net-position cap (contracts). None = use the symbol's Apex cap
    # (6 minis / 60 micros); 0 disables the guard entirely.
    max_position: int | None = None
    # Minimum trade duration (seconds), if a prop-firm rule requires one.
    # > 0 makes strategy-initiated exits (close_position / reversals) wait
    # until the position is this old; hard bracket stops and session/DLL
    # flattens are NOT gated. 0 = off (no such rule confirmed for the
    # Intraday Trailing Drawdown accounts this repo currently targets).
    min_hold_s: float = 0.0
    # Extra bar series for multi-timeframe logic, e.g. ["5m", "15m"]. Each is
    # a period string (same grammar as `period`). During on_bar, read the
    # completed secondary bars via self.secondary(period) — only bars that
    # closed at/before the current primary bar are present (no look-ahead).
    secondary_periods: list[str] = []
    # High-impact news filter (ForexFactory "red folder" events). When
    # news_filter is True the engine loads the calendar and NEW entries are
    # blocked within [event - news_pre_min, event + news_post_min]; protective
    # stops/targets and exits are never gated. news_flatten additionally
    # force-flattens an open position on entering a window. news_csv defaults
    # to data/ff_high_impact_news.csv; refresh it with tools/fetch_ff_news.py.
    news_filter: bool = False
    news_pre_min: float = 5.0
    news_post_min: float = 5.0
    news_currencies: tuple[str, ...] = ("USD",)
    news_flatten: bool = False
    news_csv: str | None = None
    # ---- Apex risk helpers (see risk.py) ----
    # Profit target (dollars) for this strategy's account; enables the auto-bank
    # exit in bank_profit(). None = disabled.
    eval_target: float | None = None
    # Fraction of the live Apex headroom a single trade may risk (stop risk).
    risk_max_loss_frac: float = 0.5
    # Profit-bank: arm the peak-protection stop at this fraction of the target,
    # then give back no more than this fraction of the open peak.
    profit_bank_arm_frac: float = 0.5
    profit_bank_giveback: float = 0.4

    def __init__(self):
        self._broker = None      # wired by the engine
        self._account = None
        self._now_ts = 0         # current bar close ts, set by the engine
        self._last_price = 0.0   # last bar close, set by the engine
        self._budget = None      # lazy risk.RiskBudget (see _risk_budget)
        self._secondary = {}     # period -> BarHistory, wired by the engine
        self._news = None        # NewsCalendar | None, wired by the engine

    # ---- lifecycle hooks ----
    def on_start(self) -> None: ...
    def on_bar(self, bar: Bar, bars: BarHistory) -> None: ...
    def on_fill(self, fill: Fill) -> None: ...
    def on_session_end(self, date: str) -> None: ...
    def on_finish(self) -> None: ...
    # Optional: fires when a secondary-series bar completes, just before the
    # primary on_bar that first sees it. Override to update HTF indicators.
    def on_secondary_bar(self, bar: Bar, bars: BarHistory, period: str) -> None: ...
    # Optional: fires per reduced trade event within a bar span. Defining it
    # switches the engine to a slower per-event resolver; orders submitted
    # here fill on later events (no look-ahead). Read-only price at `ts`.
    def on_tick(self, ts: int, price: float, index: int) -> None: ...

    def secondary(self, period: str) -> BarHistory:
        """Completed bars of a declared secondary series (as of now)."""
        return self._secondary[period]

    # ---- state ----
    @property
    def position(self) -> int:
        return self._account.position

    @property
    def flat(self) -> bool:
        return self._account.position == 0

    @property
    def avg_price(self) -> float:
        return self._account.avg_price

    @property
    def balance(self) -> float:
        return self._account.balance

    def position_age_s(self) -> float:
        """Seconds the current open position has been held (inf if flat)."""
        rec = self._broker.recorder
        if rec.open is None or self._account.position == 0:
            return float("inf")
        return (self._now_ts - rec.open.entry_ts) / 1e9

    def hold_ok(self) -> bool:
        """True if the Apex min-hold has elapsed (or is disabled)."""
        return self.min_hold_s <= 0 or self.position_age_s() >= self.min_hold_s

    def news_blocked(self, ts: int | None = None) -> bool:
        """True if now (or `ts`, ns UTC) is inside a high-impact news window.
        Always False unless news_filter is on and the engine loaded a calendar.
        Entry helpers consult this automatically; call it directly for custom
        entry logic or to gate a discretionary decision."""
        if self._news is None:
            return False
        t = self._now_ts if ts is None else ts
        return self._news.blocked(t, self.news_pre_min * 60.0,
                                  self.news_post_min * 60.0)

    # ---- orders ----
    # Entry helpers return None (no order submitted) when the news filter is
    # active and now is inside a high-impact window. Exits are never gated.
    def buy(self, qty: int | None = None, tag: str = "") -> Order | None:
        return self._enter(BUY, qty, tag)

    def sell(self, qty: int | None = None, tag: str = "") -> Order | None:
        return self._enter(SELL, qty, tag)

    def buy_bracket(self, qty: int | None = None, stop_ticks: float | None = None,
                    target_ticks: float | None = None, tag: str = "") -> Order:
        return self._enter(BUY, qty, tag,
                           BracketSpec(stop_ticks, target_ticks))

    def sell_bracket(self, qty: int | None = None, stop_ticks: float | None = None,
                     target_ticks: float | None = None, tag: str = "") -> Order:
        return self._enter(SELL, qty, tag,
                           BracketSpec(stop_ticks, target_ticks))

    def buy_limit(self, price: float, qty: int | None = None, tag: str = "",
                  stop_ticks: float | None = None,
                  target_ticks: float | None = None) -> Order | None:
        if self.news_blocked():
            return None
        o = Order(side=BUY, qty=qty or self.qty, type=OrderType.LIMIT,
                  price=price, tag=tag)
        return self._broker.submit(o, BracketSpec(stop_ticks, target_ticks))

    def sell_limit(self, price: float, qty: int | None = None, tag: str = "",
                   stop_ticks: float | None = None,
                   target_ticks: float | None = None) -> Order | None:
        if self.news_blocked():
            return None
        o = Order(side=SELL, qty=qty or self.qty, type=OrderType.LIMIT,
                  price=price, tag=tag)
        return self._broker.submit(o, BracketSpec(stop_ticks, target_ticks))

    def buy_stop(self, price: float, qty: int | None = None, tag: str = "",
                 stop_ticks: float | None = None,
                 target_ticks: float | None = None) -> Order | None:
        if self.news_blocked():
            return None
        o = Order(side=BUY, qty=qty or self.qty, type=OrderType.STOP,
                  price=price, tag=tag)
        return self._broker.submit(o, BracketSpec(stop_ticks, target_ticks))

    def sell_stop(self, price: float, qty: int | None = None, tag: str = "",
                  stop_ticks: float | None = None,
                  target_ticks: float | None = None) -> Order | None:
        if self.news_blocked():
            return None
        o = Order(side=SELL, qty=qty or self.qty, type=OrderType.STOP,
                  price=price, tag=tag)
        return self._broker.submit(o, BracketSpec(stop_ticks, target_ticks))

    # ---- working-order access / modification ----
    @property
    def working_orders(self) -> list[Order]:
        return list(self._broker.working)

    @property
    def stop_order(self) -> Order | None:
        """First working protective stop (exit-side stop order)."""
        for o in self._broker.working:
            if o.is_exit and o.type is OrderType.STOP:
                return o
        return None

    @property
    def target_order(self) -> Order | None:
        """First working profit target (exit-side limit order)."""
        for o in self._broker.working:
            if o.is_exit and o.type is OrderType.LIMIT:
                return o
        return None

    def move_stop(self, price: float) -> bool:
        """Move all working protective stops to `price` (e.g. trailing)."""
        moved = False
        for o in self._broker.working:
            if o.is_exit and o.type is OrderType.STOP:
                moved = self._broker.modify(o, price) or moved
        return moved

    def move_target(self, price: float) -> bool:
        moved = False
        for o in self._broker.working:
            if o.is_exit and o.type is OrderType.LIMIT:
                moved = self._broker.modify(o, price) or moved
        return moved

    def move_stop_to_breakeven(self, offset_ticks: float = 0.0) -> bool:
        """Stop to entry price +/- offset (offset in the profit direction)."""
        pos = self._account.position
        if pos == 0:
            return False
        tick = self._broker.spec.tick_size
        price = self._account.avg_price + (1 if pos > 0 else -1) * offset_ticks * tick
        return self.move_stop(price)

    def vol_target_contracts(self, daily_atr_points: float,
                             annual_vol_target: float = 0.15,
                             max_contracts: int | None = None) -> int:
        """Carver vol-target size from current balance (see sizing.py).
        Pass a DAILY ATR in points, not an intraday one."""
        from .sizing import carver_contracts
        return carver_contracts(self._account.balance, daily_atr_points,
                                self._broker.spec.point_value,
                                annual_vol_target,
                                max_contracts=max_contracts)

    # ---- Apex risk helpers (see risk.py) ----
    def prop_equity(self, price: float | None = None) -> float:
        """Account equity marked to `price` (default: the last bar close)."""
        p = self._last_price if price is None else float(price)
        return self._account.equity(p)

    def prop_headroom(self, price: float | None = None) -> float:
        """Live buffer above the Apex trailing floor (equity - floor).

        Returns +inf when the run has no prop config (nothing to respect).
        """
        prop = self._broker.prop
        if prop is None:
            return float("inf")
        return self.prop_equity(price) - prop.floor

    def _risk_budget(self) -> RiskBudget:
        if self._budget is None:
            cap = self.max_position or self._broker.spec.apex_max_position
            self._budget = RiskBudget(self._broker.spec,
                                      self.risk_max_loss_frac,
                                      apex_max_contracts=cap)
        return self._budget

    def size_within_budget(self, stop_ticks: float,
                           price: float | None = None) -> int:
        """Largest size whose `stop_ticks` stop fits the Apex headroom budget.

        Sized from the live headroom (equity - floor) so a run of losers cannot
        reach the floor. Returns 0 when even one contract would risk more than
        the budget (stand down). With no prop config it returns the position
        cap instead.
        """
        spec = self._broker.spec
        headroom = self.prop_headroom(price)
        if headroom == float("inf"):
            return int(self.max_position or spec.apex_max_position)
        stop_points = float(stop_ticks) * spec.tick_size
        return self._risk_budget().contracts(headroom, stop_points)

    def bank_profit(self, price: float | None = None,
                    target: float | None = None) -> bool:
        """Protect the Apex unrealized peak. Call from on_bar while in a position.

        * open profit >= target -> close (bank the eval pass);
        * open profit >= arm_frac * target -> ratchet the stop behind the peak
          so no more than `giveback` of the open peak is given back.
        Returns True if it acted. Needs `target` or `self.eval_target`.
        """
        tgt = target if target is not None else self.eval_target
        pos = self._account.position
        rec = self._broker.recorder
        if tgt is None or pos == 0 or rec.open is None:
            return False
        p = self._last_price if price is None else float(price)
        pv = self._broker.spec.point_value
        open_pnl = pos * (p - self._account.avg_price) * pv
        bank = ProfitBankPolicy(tgt, self.profit_bank_arm_frac,
                                self.profit_bank_giveback)
        if bank.take_profit(open_pnl):
            return self.close_position(tag="bank-tp") is not None
        floor_pnl = bank.floor_pnl(rec.open.mfe)
        if floor_pnl == float("-inf"):
            return False
        cur = self.stop_order
        if cur is None:
            return False
        stop_px = self._account.avg_price + floor_pnl / (pos * pv)
        better = stop_px > cur.price if pos > 0 else stop_px < cur.price
        return self.move_stop(stop_px) if better else False

    def close_position(self, tag: str = "exit",
                       force: bool = False) -> Order | None:
        """Flatten with a market order (fills next tick).

        Blocked (returns None) if the Apex min-hold hasn't elapsed, unless
        `force` (risk stand-downs like a daily-loss lock pass force=True).
        """
        pos = self._account.position
        if pos == 0:
            return None
        if not force and not self.hold_ok():
            return None
        o = Order(side=SELL if pos > 0 else BUY, qty=abs(pos),
                  type=OrderType.MARKET, tag=tag, is_exit=True)
        return self._broker.submit(o)

    def cancel_all(self) -> None:
        self._broker.cancel_all()

    def _enter(self, side: int, qty: int | None, tag: str,
               bracket: BracketSpec | None = None) -> Order | None:
        if self.news_blocked():
            return None
        o = Order(side=side, qty=qty or self.qty, type=OrderType.MARKET,
                  tag=tag or ("long" if side == BUY else "short"))
        return self._broker.submit(o, bracket)


@dataclass(frozen=True)
class BarSpec:
    """Parsed bar type.
    kind: 'time' | 'tick' | 'renko' | 'saber' | 'tbars' | 'wave'."""
    kind: str
    seconds: int = 0          # time bars
    ticks: int = 0            # tick-count bars: trades per bar
    brick_ticks: int = 0      # renko: bar body height in ticks
    trend_ticks: int = 0      # renko: with-trend close distance from prev close
    bar_ticks: int = 0        # saber: Bar Size (B), ticks
    offset_ticks: int = 0     # saber: Offset (O), ticks
    filter_s: int = 0         # saber: Time Filter, seconds
    speed_ticks: int = 0      # tbars: "Speed Settings" (N), ticks
    wave_ticks: int = 0       # wave: "Wave Size" (N), ticks

    @property
    def key(self) -> str:
        if self.kind == "time":
            return f"{self.seconds}s"
        if self.kind == "tick":
            return f"{self.ticks}t"
        if self.kind == "saber":
            return f"s{self.bar_ticks}-{self.offset_ticks}-{self.filter_s}"
        if self.kind == "tbars":
            return f"tb{self.speed_ticks}"
        if self.kind == "wave":
            return f"w{self.wave_ticks}"
        return f"r{self.brick_ticks}-{self.trend_ticks}"


def parse_barspec(period: str) -> BarSpec:
    """'30s'/'1m'/'5m'/'1h' time bars; '500t' tick bars; 'r8-4' ninZaRenko
    (brick 8 ticks, trend threshold 4; 'r8' defaults trend to brick/2);
    's64-16'/'s64-16-2' SaberRenko (Bar Size 64, Offset 16, Time Filter
    seconds, default 1); 'tb120' TBars ("Speed Settings" N=120); 'w120' Wave
    Bars ("Wave Size" N=120)."""
    p = period.strip().lower()
    m = re.fullmatch(r"w(\d+)", p)
    if m:
        wave = int(m.group(1))
        if wave < 1:
            raise ValueError(
                f"Wave size ({wave}) must be >= 1")
        return BarSpec("wave", wave_ticks=wave)
    m = re.fullmatch(r"tb(\d+)", p)
    if m:
        speed = int(m.group(1))
        if speed < 2:
            raise ValueError(
                f"TBars speed ({speed}) must be >= 2 — the trend offset is "
                "N//2 ticks, so N=1 gives a zero-tick trend threshold and a "
                "new bar on every uptick; NT8's TBarsNEW pins its own default "
                "to 2 for the same reason")
        return BarSpec("tbars", speed_ticks=speed)
    m = re.fullmatch(r"s(\d+)-(\d+)(?:-(\d+))?", p)
    if m:
        bar = int(m.group(1))
        offset = int(m.group(2))
        filt = int(m.group(3)) if m.group(3) else 1
        if offset > bar:
            raise ValueError(
                f"SaberRenko offset ({offset}) must not exceed bar size "
                f"({bar}) — an offset larger than the bar degenerates "
                "(near-every-tick bars)")
        if bar % offset != 0:
            raise ValueError(
                f"SaberRenko bar size ({bar}) must be a multiple of offset "
                f"({offset}) to keep closes on the renko grid — e.g. "
                "s64-16, s100-25")
        return BarSpec("saber", bar_ticks=bar, offset_ticks=offset,
                       filter_s=max(1, filt))
    m = re.fullmatch(r"r(\d+)(?:-(\d+))?", p)
    if m:
        brick = int(m.group(1))
        trend = int(m.group(2)) if m.group(2) else max(1, brick // 2)
        if trend > brick:
            raise ValueError(
                f"renko trend threshold ({trend}) must not exceed brick "
                f"size ({brick}) — manual best practice is brick a multiple "
                "of trend, e.g. r8-4, r15-5, r20-5")
        return BarSpec("renko", brick_ticks=brick, trend_ticks=trend)
    m = re.fullmatch(r"(\d+)t", p)
    if m:
        return BarSpec("tick", ticks=int(m.group(1)))
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smh])", p)
    if m:
        units = {"s": 1, "m": 60, "h": 3600}
        return BarSpec("time", seconds=int(float(m.group(1)) * units[m.group(2)]))
    if p.isdigit():
        return BarSpec("time", seconds=int(p))
    raise ValueError(
        f"Unrecognized bar period {period!r} "
        "(examples: 30s, 1m, 5m, 500t, r8, r8-4, s64-16, tb120, w120)")


def parse_period(period: str) -> int:
    """Back-compat: seconds of a time-bar spec."""
    spec = parse_barspec(period)
    if spec.kind != "time":
        raise ValueError(f"{period!r} is not a time-bar period")
    return spec.seconds
