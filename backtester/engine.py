"""Backtest engine: replays days chronologically, drives broker + strategy."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from .account import (Account, DrawdownTracker, PropFirmConfig, PropFirmTracker,
                      Trade, TradeRecorder)
from .broker import SimBroker
from .contracts import ContractSpec, get_spec
from .data import Catalog
from .rolls import RollCalendar
from .strategy import Bar, BarHistory, Strategy, parse_barspec

# All user-facing session times are US/Eastern (user preference — their PC,
# NT8, and trading community all run ET). Internals remain ns UTC.
SESSION_TZ = ZoneInfo("America/New_York")

# Order latency when neither the caller nor the strategy sets one: a new order (and
# the stop/target legs made at an entry fill) reaches the exchange this many ms after
# it is sent. 100 ms is the conservative rung of a live measurement (Apex / Tradovate,
# one weekday in October 2026): half the market-order round trip was ~108 ms at the
# open and ~58 ms in the slow evening hours. latency_ms=0 turns it off, which is the
# OPTIMISTIC case.
DEFAULT_LATENCY_MS = 100.0


@dataclass
class Result:
    symbol: str
    spec: ContractSpec
    start_balance: float
    period: str
    days: list[str]
    trades: list[Trade]
    fills: int
    # sampled at bar closes:
    equity_ts: np.ndarray = field(default_factory=lambda: np.array([], "int64"))
    equity: np.ndarray = field(default_factory=lambda: np.array([]))
    prop_floor: np.ndarray = field(default_factory=lambda: np.array([]))
    daily_pnl: dict[str, float] = field(default_factory=dict)
    prop: PropFirmTracker | None = None
    total_commission: float = 0.0
    runtime_s: float = 0.0
    strategy_name: str = ""
    halted_on: str | None = None
    dll_days: list[str] = field(default_factory=list)
    dpt_days: list[str] = field(default_factory=list)
    # tick-level max drawdown (metrics prefers it over the bar-close curve)
    drawdown: DrawdownTracker | None = None
    # trading-day key -> why that day is not a whole session (data ended or
    # started mid-session). Keys are the CME trading date: the date the day ENDS.
    day_notes: dict[str, str] = field(default_factory=dict)
    # how the daily limits were enforced: "gate" (realized P&L, entries blocked)
    # or "kill" (equity incl. open P&L, flatten + stand down)
    daily_limit_mode: str = "gate"
    # contract rolls the run crossed: {"date", "position", "flattened"} per roll
    # date between two of its files (see rolls.py); `roll_notes` are the warnings
    # (no calendar, calendar too short, data discontinuity on a non-roll date)
    rolls: list[dict] = field(default_factory=list)
    roll_notes: list[str] = field(default_factory=list)
    flatten_at_roll: bool = True
    # opt-in roll blackout: minutes after each roll with no new entries, and how many
    # entry orders it refused
    roll_entry_block_minutes: float = 0.0
    roll_entries_refused: int = 0
    # exit orders (stop/target legs) the broker cancelled because their position
    # was already closed or reversed by something else (reduce-only rule)
    exits_dropped: int = 0
    # the order latency the run used, in ms (0 = off, the optimistic case)
    latency_ms: float = 0.0
    # opt-in execution latencies (ms): a strategy cancel / modify reaching the exchange
    # (0 = instant) and the entry fill -> protective leg live delay (None = latency_ms)
    cancel_latency_ms: float = 0.0
    modify_latency_ms: float = 0.0
    bracket_latency_ms: float | None = None


def _session_bounds_utc(date: str,
                        session: tuple[str, str]) -> tuple[int, int, bool]:
    """UTC ns of the ET session times on the file's calendar date.

    Returns (start_ns, end_ns, wrap). wrap=True means an overnight session
    (start > end, e.g. Globex 18:00 -> 16:55 ET): the trading day that
    *ends* on this date began at `start` on the PREVIOUS date, and a new one
    begins at `start` on this date.
    """
    d = datetime.strptime(date, "%Y%m%d")

    def to_ns(hhmm: str) -> int:
        h, m = map(int, hhmm.split(":"))
        local = datetime(d.year, d.month, d.day, h, m, tzinfo=SESSION_TZ)
        return int(local.timestamp() * 1e9)

    start, end = to_ns(session[0]), to_ns(session[1])
    return start, end, end <= start


def _next_date(date: str) -> str:
    """The next calendar date (YYYYMMDD). A CME trading day is named by the
    date it ENDS on (18:00 ET the evening before -> 17:00 ET), so the evening
    part of a day file belongs to the trading day named by the NEXT date."""
    d = datetime.strptime(date, "%Y%m%d") + timedelta(days=1)
    return d.strftime("%Y%m%d")


def _is_overnight(session: tuple[str, str] | None) -> bool:
    """True for a session that wraps midnight (start >= end, e.g. 18:00-16:55)."""
    return session is not None and session[1] <= session[0]


def _segments(session: tuple[str, str] | None, date: str,
              ts_end: np.ndarray) -> list[tuple[np.ndarray, bool]]:
    """Split one day file's bars into (indices, is_trading_day_end) segments.

    - session None: whole file, day ends with the file.
    - normal session (start < end): one in-window segment, day ends there.
    - overnight session: bars up to `end` finish the trading day that started
      yesterday (day_end=True); bars from `start` on begin the next trading
      day, which carries across the UTC-midnight file boundary
      (day_end=False — no flatten, orders stay working).
    """
    n = len(ts_end)
    if session is None:
        return [(np.arange(n), True)]
    s_ns, e_ns, wrap = _session_bounds_utc(date, session)
    if not wrap:
        idx = np.flatnonzero((ts_end > s_ns) & (ts_end <= e_ns))
        return [(idx, True)] if len(idx) else []
    out: list[tuple[np.ndarray, bool]] = []
    idx1 = np.flatnonzero(ts_end <= e_ns)
    if len(idx1):
        out.append((idx1, True))
    idx2 = np.flatnonzero(ts_end > s_ns)
    if len(idx2):
        out.append((idx2, False))
    return out


class Backtest:
    def __init__(self, strategy: Strategy, start: str | None = None,
                 end: str | None = None, symbol: str | None = None,
                 period: str | None = None, start_balance: float = 50_000.0,
                 prop: PropFirmConfig | None = PropFirmConfig(),
                 slippage_ticks: float = 0.0,
                 daily_loss_limit: float | None = None,
                 daily_profit_target: float | None = None,
                 daily_limit_mode: str | None = None,
                 latency_ms: float | None = None,
                 cancel_latency_ms: float | None = None,
                 modify_latency_ms: float | None = None,
                 bracket_latency_ms: float | None = None,
                 max_position: int | None = None,
                 flatten_at_roll: bool | None = None,
                 roll_entry_block_minutes: float | None = None,
                 roll_calendar: RollCalendar | None = None,
                 data_root=None, cache_root=None,
                 progress: bool = True):
        self.strategy = strategy
        self.symbol = (symbol or strategy.symbol).upper()
        self.barspec = parse_barspec(period or strategy.period)
        self.start, self.end = start, end
        self.start_balance = start_balance
        self.spec = get_spec(self.symbol)
        self.catalog = Catalog(data_root, cache_root)
        self.account = Account(self.spec, start_balance)
        self.recorder = TradeRecorder(self.spec)
        self.prop = PropFirmTracker(prop, start_balance) if prop else None
        # Net-position cap resolution: explicit arg > strategy attr > symbol's
        # Apex default (6 minis / 60 micros). A resolved 0 disables the guard.
        mp = max_position if max_position is not None \
            else getattr(strategy, "max_position", None)
        if mp is None:
            mp = self.spec.apex_max_position
        # order latency in ms: arg > strategy attribute > DEFAULT_LATENCY_MS; an explicit
        # 0 (arg or attribute) turns it off
        if latency_ms is None:
            attr = getattr(strategy, "latency_ms", None)
            latency_ms = DEFAULT_LATENCY_MS if attr is None else float(attr)
        if latency_ms < 0:
            raise ValueError(f"latency_ms must be >= 0, not {latency_ms!r}")
        self.latency_ms = latency_ms
        # Execution latencies, all OFF by default (arg > strategy attribute > default):
        # cancel / modify = one way, half a measured round trip (0 = instant); bracket =
        # fill -> stop live, a full round trip (None = latency_ms, the original model).
        def _ms(arg, name, default):
            v = arg if arg is not None else getattr(strategy, name, None)
            v = default if v is None else float(v)
            if v is not None and v < 0:
                raise ValueError(f"{name} must be >= 0, not {v!r}")
            return v
        self.cancel_latency_ms = _ms(cancel_latency_ms, "cancel_latency_ms", 0.0)
        self.modify_latency_ms = _ms(modify_latency_ms, "modify_latency_ms", 0.0)
        self.bracket_latency_ms = _ms(bracket_latency_ms, "bracket_latency_ms", None)
        self.broker = SimBroker(self.spec, self.account, self.recorder,
                                self.prop, slippage_ticks,
                                on_fill=self._notify_fill,
                                max_position=mp, latency_ms=latency_ms,
                                cancel_latency_ms=self.cancel_latency_ms,
                                modify_latency_ms=self.modify_latency_ms,
                                bracket_latency_ms=self.bracket_latency_ms)
        # daily limits: explicit arg > strategy attribute (mirrors max_position)
        if daily_loss_limit is None:
            daily_loss_limit = getattr(strategy, "daily_loss_limit", None)
        if daily_profit_target is None:
            daily_profit_target = getattr(strategy, "daily_profit_target", None)
        self.daily_loss_limit = daily_loss_limit
        self.daily_profit_target = daily_profit_target
        # How the limits bite (arg > strategy attribute > default "gate"):
        #   "gate": day REALIZED P&L (closed trades) at the limit blocks NEW
        #     entries for the rest of the trading day; exits keep working (the
        #     position is already flat when it trips, so DailyFlatten is moot).
        #     What several NinjaTrader strategies' own daily limits do.
        #   "kill": equity INCLUDING open P&L, tick by tick: flatten there and
        #     refuse every order until the next trading day. What
        #     GodZillaKilla does.
        if daily_limit_mode is None:
            daily_limit_mode = getattr(strategy, "daily_limit_mode", "gate")
        if daily_limit_mode not in ("gate", "kill"):
            raise ValueError(f"daily_limit_mode must be 'gate' or 'kill', "
                             f"not {daily_limit_mode!r}")
        self.daily_limit_mode = daily_limit_mode
        # Flat before every contract roll (arg > strategy attribute > ON): the
        # data switches contract at a midnight ET inside the session and a
        # position carried across it would book the calendar spread as P&L.
        if flatten_at_roll is None:
            flatten_at_roll = getattr(strategy, "flatten_at_roll", True)
        self.flatten_at_roll = bool(flatten_at_roll)
        # Opt-in (arg > strategy attribute > 0 = off): no NEW entries for this many
        # minutes from the first tick on the new contract. After a roll the bars and
        # indicators still see the calendar spread as a move (a channel breakout), which
        # live trading on the new contract's own history would not; this takes the fake
        # signals out so a strategy can be tested for dependence on them.
        if roll_entry_block_minutes is None:
            roll_entry_block_minutes = getattr(strategy, "roll_entry_block_minutes", 0.0) or 0.0
        if roll_entry_block_minutes < 0:
            raise ValueError(f"roll_entry_block_minutes must be >= 0, "
                             f"not {roll_entry_block_minutes!r}")
        self.roll_entry_block_minutes = float(roll_entry_block_minutes)
        self._roll_calendar = roll_calendar
        self._roll_calendar_loaded = roll_calendar is not None
        self.news_flatten = (getattr(strategy, "news_filter", False)
                             and getattr(strategy, "news_flatten", False))
        self.progress = progress
        self._in_bar_cb = False

    @property
    def roll_calendar(self) -> RollCalendar | None:
        """The roll schedule (data/roll_calendar.json unless one was passed in);
        None when the file is missing. Loaded once."""
        if not self._roll_calendar_loaded:
            self._roll_calendar = RollCalendar.load()
            self._roll_calendar_loaded = True
        return self._roll_calendar

    def _notify_fill(self, fill) -> None:
        self.strategy.on_fill(fill)

    def _start_day(self, start_balance: float) -> None:
        """Arm the broker's daily limits for a new trading day."""
        loss, profit = self.daily_loss_limit, self.daily_profit_target
        if self.daily_limit_mode == "kill":
            self.broker.start_trading_day(
                floor=start_balance - loss if loss is not None else None,
                ceiling=start_balance + profit if profit is not None else None)
        else:
            self.broker.start_trading_day(gate_loss=loss, gate_profit=profit)

    @staticmethod
    def _advance_secondary(strat, primary_ts, sec_periods, sec_bars,
                           sec_cursor, sec_hist, sec_index, has_on_sec):
        """Append every secondary bar that closed at/before the current
        primary bar (ts_end <= primary_ts) — no look-ahead — and fire
        on_secondary_bar for each, before the primary on_bar runs."""
        for p in sec_periods:
            sb = sec_bars[p]
            h = sec_hist[p]
            cur = sec_cursor[p]
            n = len(sb)
            while cur < n and int(sb.ts_end[cur]) <= primary_ts:
                sbar = Bar(ts=int(sb.ts_end[cur]),
                           open=float(sb.open[cur]), high=float(sb.high[cur]),
                           low=float(sb.low[cur]), close=float(sb.close[cur]),
                           volume=int(sb.volume[cur]), index=sec_index[p],
                           buy_volume=int(sb.buy_volume[cur]),
                           sell_volume=int(sb.sell_volume[cur]))
                h.append(sbar)
                sec_index[p] += 1
                if has_on_sec:
                    strat.on_secondary_bar(sbar, h, p)
                cur += 1
            sec_cursor[p] = cur

    def run(self) -> Result:
        t_start = time.perf_counter()
        strat = self.strategy
        strat._broker = self.broker
        strat._account = self.account

        # high-impact news filter: load the calendar once; entry helpers on the
        # strategy consult strat._news, and the engine flattens on window entry
        # when news_flatten is set.
        strat._news = None
        if getattr(strat, "news_filter", False):
            from .news import NewsCalendar
            strat._news = NewsCalendar.load(
                getattr(strat, "news_csv", None) or None,
                currencies=getattr(strat, "news_currencies", None))
            if self.progress:
                print(f"  [news] {len(strat._news)} high-impact events "
                      f"({','.join(strat.news_currencies)}), "
                      f"block -{strat.news_pre_min:g}/+{strat.news_post_min:g} min"
                      + (" + flatten" if self.news_flatten else ""))

        # secondary (multi-timeframe) series + optional on_tick. Both stay
        # entirely off the fast path unless a strategy opts in.
        sec_periods = list(getattr(strat, "secondary_periods", []) or [])
        sec_specs = {p: parse_barspec(p) for p in sec_periods}
        sec_hist = {p: BarHistory() for p in sec_periods}
        sec_index = {p: 0 for p in sec_periods}
        strat._secondary = sec_hist
        has_on_sec = type(strat).on_secondary_bar is not Strategy.on_secondary_bar
        has_on_tick = type(strat).on_tick is not Strategy.on_tick

        def _tick_cb(ts, price, idx):
            strat._now_ts = ts
            self.broker.now_ts = ts
            strat.on_tick(ts, price, idx)

        days = self.catalog.days(self.symbol, self.start, self.end)
        if not days:
            raise FileNotFoundError(
                f"No {self.symbol} days in range {self.start}..{self.end}")

        # Sequence-aware: renko brick state carries across day-file
        # boundaries (reset only on a genuine gap), rather than resetting at
        # every calendar-day file start regardless of real market continuity.
        bars_seq = self.catalog.load_bars_sequence(
            self.symbol, days, self.barspec, self.spec.tick_size)
        sec_bars_seq = {p: self.catalog.load_bars_sequence(
                            self.symbol, days, sec_specs[p], self.spec.tick_size)
                        for p in sec_periods}

        hist = BarHistory()
        eq_ts, eq, floor = [], [], []
        daily: dict[str, float] = {}
        dll_days: list[str] = []
        dpt_days: list[str] = []
        bar_index = 0
        halted_on = None
        day_start_balance = self.account.balance
        self._start_day(day_start_balance)
        stood_noted = False        # a daily limit already recorded for this day
        new_trading_day = True
        last_bar_ref = None        # (DayL1, event idx) of last processed bar
        day_notes: dict[str, str] = {}
        first_seg = True           # next processed segment is the run's first
        unflushed_key = None       # evening segment processed, its day not yet flushed
        cur_key = None             # trading date of the last processed segment

        # contract rolls: cancel orders and flatten at the end of the last file
        # before each scheduled roll (a schedule, never the price jump: a jump
        # at the next file's first tick is not known at the end of this one)
        roll_cal = None
        last_file_date = None      # the last non-empty day file this run processed
        roll_notes: list[str] = []
        rolls_hit: list[dict] = []
        if self.flatten_at_roll or self.roll_entry_block_minutes > 0:
            roll_cal = self.roll_calendar
            if roll_cal is None:
                roll_notes.append("data/roll_calendar.json is missing: positions held "
                                  "across a contract roll book the calendar spread as "
                                  "P&L (python3 -m tools.roll_calendar build)")
            else:
                roll_notes += roll_cal.notes(self.symbol, days[0], days[-1])
                if not roll_cal.covers(self.symbol):
                    roll_cal = None
            if self.progress:
                for note in roll_notes:
                    print(f"  [rolls] WARNING: {note}")

        strat.on_start()
        for di, date in enumerate(days):
            day = self.catalog.load_day(self.symbol, date)
            if len(day) == 0:
                continue
            bars = bars_seq[di]
            if len(bars) == 0:
                continue
            sec_bars = {p: sec_bars_seq[p][di] for p in sec_periods}
            sec_cursor = {p: 0 for p in sec_periods}
            # a scheduled contract roll between the last file this run processed
            # and this one (empty or skipped files in between do not hide it):
            # cancel orders and flatten at the last bar processed, which is the
            # last the strategy saw on the old contract
            if (roll_cal is not None and last_file_date is not None
                    and not self.broker.halted):
                roll_date = roll_cal.roll_between(self.symbol, last_file_date, date)
                if roll_date is not None:
                    pos_before = self.account.position
                    flattened = False
                    if self.flatten_at_roll:
                        self.broker.force_cancel_all()
                        flattened = pos_before != 0 and last_bar_ref is not None
                        if flattened:
                            self.broker.begin_day(last_bar_ref[0])
                            self.broker.flatten(last_bar_ref[1], tag="roll")
                            if eq:
                                eq[-1] = self.account.balance
                    if self.roll_entry_block_minutes > 0:
                        # from the first tick on the new contract
                        self.broker.roll_block_until = (
                            int(day.ts[0]) + int(self.roll_entry_block_minutes * 60e9))
                    rolls_hit.append({"date": roll_date, "position": pos_before,
                                      "flattened": flattened})
            last_file_date = date
            self.broker.begin_day(day)

            for seg_idx, day_end in _segments(strat.session, date,
                                              bars.ts_end):
                if self.broker.halted:
                    break
                # The CME trading day this segment belongs to, named by the
                # date it ENDS on: a day-end segment ends today, the evening
                # segment of an overnight session ends tomorrow.
                key = date if day_end else _next_date(date)
                cur_key = key
                if first_seg:
                    first_seg = False
                    if day_end and _is_overnight(strat.session):
                        day_notes[key] = (
                            f"partial first trading day: the run starts at the "
                            f"{date} file, so the {strat.session[0]} ET evening "
                            f"part of the previous calendar day is not loaded "
                            f"(start one file earlier to include it)")
                if new_trading_day and len(seg_idx):
                    hist.reset_cum_delta()
                    for h in sec_hist.values():
                        h.reset_cum_delta()
                    new_trading_day = False
                for j in seg_idx:
                    # once stood down these resolve nothing (no fills, no
                    # marking) but the strategy still gets every bar below
                    if has_on_tick:
                        self.broker.resolve_span_ticks(
                            int(bars.i0[j]), int(bars.i1[j]), _tick_cb)
                    else:
                        self.broker.resolve_span(int(bars.i0[j]),
                                                 int(bars.i1[j]))
                    if self.broker.halted:
                        break
                    if self.broker.limit_hit and not stood_noted:
                        # a daily limit tripped on a tick inside this bar's
                        # span (the broker already acted at that tick)
                        (dpt_days if self.broker.limit_hit == "dpt"
                         else dll_days).append(key)
                        stood_noted = True
                    bar = Bar(ts=int(bars.ts_end[j]),
                              open=float(bars.open[j]),
                              high=float(bars.high[j]),
                              low=float(bars.low[j]),
                              close=float(bars.close[j]),
                              volume=int(bars.volume[j]), index=bar_index,
                              buy_volume=int(bars.buy_volume[j]),
                              sell_volume=int(bars.sell_volume[j]))
                    hist.append(bar)
                    bar_index += 1
                    strat._now_ts = bar.ts
                    # When an order, cancel or modify placed in on_bar is SENT (matters
                    # only with a latency set): the strategy can first have seen the bar close
                    # when the next bar's first tick prints. A tick/renko bar is
                    # stamped with that very tick; a time bar is stamped with the
                    # clock boundary, which on MNQ 1m is a median 68 ms (p90
                    # 738 ms) earlier - stamping the boundary would let a 50 ms
                    # order go live BEFORE that tick and so cost nothing.
                    self.broker.now_ts = bar.ts
                    if (self.broker.latency_ns or self.broker.cancel_ns
                            or self.broker.modify_ns):
                        nxt = int(bars.i1[j])
                        if nxt < len(day.ts):
                            self.broker.now_ts = max(bar.ts, int(day.ts[nxt]))
                    strat._last_price = bar.close
                    # flatten before high-impact news (entries stay gated by
                    # strat.news_blocked in the order helpers)
                    if (self.news_flatten and self.account.position != 0
                            and strat.news_blocked(bar.ts)):
                        self.broker.force_cancel_all()
                        self.broker.flatten(int(bars.i1[j]) - 1, tag="news")
                    if sec_periods:
                        self._advance_secondary(
                            strat, bar.ts, sec_periods, sec_bars,
                            sec_cursor, sec_hist, sec_index, has_on_sec)
                    strat.on_bar(bar, hist)
                    eq_ts.append(bar.ts)
                    eq.append(self.account.equity(bar.close))
                    floor.append(self.prop.floor if self.prop
                                 else float("nan"))
                    last_bar_ref = (day, int(bars.i1[j]) - 1)

                    # the daily limits are enforced inside the broker, tick by
                    # tick; a fill made before on_bar (news flatten) can still
                    # trip one
                    if self.broker.limit_hit and not stood_noted:
                        (dpt_days if self.broker.limit_hit == "dpt"
                         else dll_days).append(key)
                        stood_noted = True

                if not day_end:
                    unflushed_key = key

                if day_end:
                    # trading-day flush: cancel orders, flatten, book P&L
                    self.broker.force_cancel_all()
                    if (strat.flat_at_session_end and not self.broker.halted
                            and len(seg_idx)):
                        last_j = int(seg_idx[-1])
                        self.broker.flatten(int(bars.i1[last_j]) - 1,
                                            tag="eod")
                        if eq:
                            eq[-1] = self.account.equity(
                                float(bars.close[last_j]))
                    if self.broker.limit_hit and not stood_noted:
                        (dpt_days if self.broker.limit_hit == "dpt"
                         else dll_days).append(key)
                    strat.on_session_end(date)
                    daily[date] = self.account.balance - day_start_balance
                    day_start_balance = self.account.balance
                    self._start_day(day_start_balance)
                    stood_noted = False
                    new_trading_day = True
                    unflushed_key = None

            if self.broker.halted:
                halted_on = date
                break
            if self.progress and (di % 20 == 19 or di == len(days) - 1):
                print(f"  ... {date} ({di + 1}/{len(days)} days) "
                      f"balance={self.account.balance:,.2f}")

        # end of data: an overnight session can leave a live position open
        flattened_at_end = False
        if (self.account.position != 0 and not self.broker.halted
                and last_bar_ref is not None):
            d, idx = last_bar_ref
            self.broker.begin_day(d)
            self.broker.flatten(idx, tag="end-of-data")
            flattened_at_end = True
            if eq:
                eq[-1] = self.account.balance

        # The last trading day may never have been flushed: data stopped inside
        # an evening segment (or a position was carried to the end). Its
        # realized P&L is in the trades, so book it on the day it belongs to
        # (the date that day would have ENDED on) and say it is incomplete.
        if (unflushed_key is not None or flattened_at_end) and last_bar_ref:
            tail = self.account.balance - day_start_balance
            tkey = unflushed_key or cur_key
            ld, lidx = last_bar_ref
            last_et = datetime.fromtimestamp(int(ld.ts[lidx]) / 1e9, SESSION_TZ)
            if unflushed_key is None:
                # that day already flushed (an overnight/unflattened position was
                # carried past its session end): the end-of-data flatten adds to it
                day_notes[tkey] = (
                    f"a position carried past the session end was flattened at "
                    f"the end of the data ({last_et:%Y-%m-%d %H:%M} ET); its "
                    f"realized P&L is added to this day")
            else:
                why = ("the run halted on a prop-firm breach"
                       if self.broker.halted else
                       f"data ends {last_et:%Y-%m-%d %H:%M} ET")
                close = f", before the session close ({strat.session[1]} ET)" \
                    if strat.session and not self.broker.halted else ""
                day_notes[tkey] = (
                    f"incomplete trading day: {why}{close}; its realized P&L "
                    f"is booked here"
                    + ("; the open position was flattened at the last bar "
                       "(end-of-data)" if flattened_at_end else ""))
            daily[tkey] = daily.get(tkey, 0.0) + tail
            day_start_balance = self.account.balance

        strat.on_finish()
        return Result(
            symbol=self.symbol, spec=self.spec,
            start_balance=self.start_balance, period=self.barspec.key,
            days=days, trades=self.recorder.trades,
            fills=len(self.broker.fills),
            equity_ts=np.asarray(eq_ts, dtype="int64"),
            equity=np.asarray(eq, dtype="float64"),
            prop_floor=np.asarray(floor, dtype="float64"),
            daily_pnl=daily, prop=self.prop,
            total_commission=self.account.total_commission,
            runtime_s=time.perf_counter() - t_start,
            strategy_name=type(self.strategy).__name__,
            halted_on=halted_on,
            dll_days=dll_days,
            dpt_days=dpt_days,
            day_notes=day_notes,
            daily_limit_mode=self.daily_limit_mode,
            drawdown=self.broker.dd,
            rolls=rolls_hit, roll_notes=roll_notes,
            flatten_at_roll=self.flatten_at_roll,
            roll_entry_block_minutes=self.roll_entry_block_minutes,
            latency_ms=self.latency_ms,
            cancel_latency_ms=self.cancel_latency_ms,
            modify_latency_ms=self.modify_latency_ms,
            bracket_latency_ms=self.bracket_latency_ms,
            roll_entries_refused=self.broker.roll_refused,
            exits_dropped=self.broker.exits_dropped,
        )
