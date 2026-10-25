"""Simulated broker: order matching against the trade-event stream.

Fill model (tick-level L1):
- MARKET: fills on the next trade event, at the prevailing ask (buy) / bid
  (sell) plus configured slippage ticks.
- LIMIT: fills when a trade prints *through* the limit (strictly better than
  the limit price) — touch alone never fills, approximating queue risk. If the
  order is already marketable when first evaluated (ask <= buy limit), it fills
  immediately at the quote.
- STOP: triggers when a trade prints at/through the stop, then fills like a
  market order at the prevailing quote (plus slippage) on that event.

Orders are resolved chronologically within each bar span, so an entry fill
activates its bracket children and those children can fill later in the same
bar, or even on the same event.

Execution latency (all opt-in except latency_ms): a new order reaches the exchange
latency_ms after it is sent; a strategy's cancel and modify reach it cancel_ms /
modify_ms after they are sent (until then the order is live as it was); the
protective legs made at an entry fill go live bracket_ms after the FILL. Each
one-way figure is half a measured round trip (the price is set when the broker
acts, halfway through the round trip); bracket is a full round trip because the
fill notice must reach the strategy before the stop can be sent. Engine-forced
cancels (session end, kill switch, roll, gate, OCO sibling) are never delayed.
"""
from __future__ import annotations

from typing import Callable

import numpy as np

from .account import Account, DrawdownTracker, PropFirmTracker, TradeRecorder
from .contracts import ContractSpec
from .data import DayL1
from .orders import BUY, SELL, BracketSpec, Fill, Order, OrderState, OrderType


class SimBroker:
    def __init__(self, spec: ContractSpec, account: Account,
                 recorder: TradeRecorder, prop: PropFirmTracker | None = None,
                 slippage_ticks: float = 0.0,
                 on_fill: Callable[[Fill], None] | None = None,
                 max_position: int | None = None,
                 latency_ms: float = 0.0,
                 cancel_latency_ms: float = 0.0,
                 modify_latency_ms: float = 0.0,
                 bracket_latency_ms: float | None = None):
        self.spec = spec
        self.account = account
        self.recorder = recorder
        self.prop = prop
        self.slippage = slippage_ticks * spec.tick_size
        self.on_fill = on_fill
        # Apex net-position cap (contracts). None/0 = off. A fill is clamped so
        # |net position| can never exceed it (models the 6-mini/60-micro rule).
        self.max_position = max_position or None
        self._cap_warned = False
        # Order latency (ms). A new order, and the stop/target legs created at an
        # entry fill, reaches the exchange latency_ms after the moment it was
        # sent (self.now_ts: the bar close for an order placed in on_bar, the
        # tick for on_tick, the fill for on_fill) and cannot trigger or fill on
        # an earlier event. A strategy's cancels and modifications are delayed only
        # when cancel_latency_ms / modify_latency_ms are set (below). NEVER delayed:
        # the engine's own flattens and cancels (session end, kill switch, news, prop
        # halt, roll, end of data). 0 = off, bit-identical to the engine without it.
        self.latency_ns = int(round(latency_ms * 1e6))
        # Strategy cancels / modifies reach the exchange this long after they are sent
        # (0 = instant, the optimistic case) and the protective legs made at an entry
        # fill go live this long after the fill (None = the same as latency_ms, the
        # engine's original behavior). Requests wait in self._requests.
        self.cancel_ns = int(round(cancel_latency_ms * 1e6))
        self.modify_ns = int(round(modify_latency_ms * 1e6))
        self.bracket_ns = (None if bracket_latency_ms is None
                           else int(round(bracket_latency_ms * 1e6)))
        self._requests: list[tuple[int, int, str, Order, float | None]] = []
        self._request_seq = 0
        self.now_ts = 0
        self.working: list[Order] = []
        # exit orders cancelled because their position was already gone (see
        # _drop_stale_exits); reported on the Result so a strategy that leaves legs
        # behind can see how often
        self.exits_dropped = 0
        self.fills: list[Fill] = []
        self._brackets: dict[int, BracketSpec] = {}   # entry order id -> spec
        self._day: DayL1 | None = None
        self.halted = False
        # tick-level max drawdown (see DrawdownTracker)
        self.dd = DrawdownTracker(account.start_balance)
        # Daily limits. TWO mechanisms, chosen by the engine per trading day:
        #
        # KILL SWITCH (daily_limit_mode "kill"): enforced tick by tick on
        # EQUITY, open P&L included. dll_floor / dpt_ceiling are that trading
        # day's start balance -/+ the limit. The first event whose equity is at
        # or beyond one flattens at that event's quote and stands the broker
        # down - no fills, no marking, every order refused - until the engine
        # starts the next trading day.
        #
        # GATE (daily_limit_mode "gate", the default): day REALIZED P&L, the sum
        # of trades closed today, at or beyond gate_loss / gate_profit blocks NEW
        # ENTRIES for the rest of the trading day. Exits, protective stops and
        # targets keep working. It can only change when a trade closes, so it
        # is exact at tick level, and the position is already flat when it
        # trips (a reversal fill is cut at flat): there is never anything left
        # to flatten, which is why a separate daily-flatten setting is moot here.
        self.dll_floor: float | None = None
        self.dpt_ceiling: float | None = None
        self.stood_down = False
        self.dll_ts: int | None = None
        self.gate_loss: float | None = None
        self.gate_profit: float | None = None
        self.entries_blocked = False
        # Roll blackout (opt-in, engine setting roll_entry_block_minutes): until this
        # time (ns) an order that would OPEN or ADD exposure is refused. Set by the
        # engine at the first tick on a new contract. 0 = never.
        self.roll_block_until = 0
        self.roll_refused = 0            # entry orders refused by the blackout
        self.day_realized = 0.0          # P&L of trades closed this trading day
        self._n_booked = 0               # recorder trades already counted
        self.limit_hit: str | None = None    # "dll" / "dpt": first limit today

    def start_trading_day(self, floor: float | None = None,
                          ceiling: float | None = None,
                          gate_loss: float | None = None,
                          gate_profit: float | None = None) -> None:
        """Called by the engine at each trading-day start (None = limit off)."""
        self.dll_floor, self.dpt_ceiling = floor, ceiling
        self.gate_loss, self.gate_profit = gate_loss, gate_profit
        self.stood_down = False
        self.entries_blocked = False
        self.dll_ts = None
        self.limit_hit = None
        self.day_realized = 0.0
        self._n_booked = len(self.recorder.trades)

    def stand_down(self, idx: int, tag: str = "dll") -> None:
        """A daily limit (loss limit `dll`, profit target `dpt`) hit at event
        `idx`: cancel everything, flatten there, and refuse fills, marking and
        NEW ORDERS for the rest of the trading day. The strategy keeps getting
        its bars (the engine still calls on_bar) but every order it submits is
        rejected - see submit()."""
        self.stood_down = True          # before flatten: its fill re-checks
        self.limit_hit = self.limit_hit or tag
        self.dll_ts = int(self._day.ts[idx])
        self.force_cancel_all()
        self.flatten(idx, tag=tag)
        self.force_cancel_all()         # anything on_fill submitted

    def _stand_down(self, idx: int, tag: str = "dll") -> None:
        self.stand_down(idx, tag)

    # ---------------- realized-P&L gate ----------------

    def _book_closed(self, idx: int) -> None:
        """Add trades closed since the last call to the day's realized P&L and
        trip the gate if that crosses a limit. Only a closing fill changes it."""
        tr = self.recorder.trades
        while self._n_booked < len(tr):
            self.day_realized += tr[self._n_booked].pnl
            self._n_booked += 1
        if self.entries_blocked or self.stood_down:
            return
        kind = None
        if self.gate_loss is not None and self.day_realized <= -self.gate_loss:
            kind = "dll"
        elif self.gate_profit is not None and self.day_realized >= self.gate_profit:
            kind = "dpt"
        if kind is None:
            return
        self.entries_blocked = True
        self.limit_hit = self.limit_hit or kind
        self.dll_ts = int(self._day.ts[idx])
        pos = self.account.position
        for o in list(self.working):            # cancel what would open or add
            if pos == 0 or o.side * pos > 0:
                self._cancel_now(o)

    def _refuses(self, order: Order) -> bool:
        """True if the daily-limit gate refuses this order: it would open or add
        exposure."""
        pos = self.account.position
        return self.entries_blocked and (pos == 0 or order.side * pos > 0)

    def _roll_blocks(self, order: Order) -> bool:
        """True during a roll blackout for an order that would open or add exposure
        (reducing orders always go through)."""
        pos = self.account.position
        return self.now_ts < self.roll_block_until and (pos == 0 or order.side * pos > 0)

    # ---------------- order entry ----------------

    def submit(self, order: Order, bracket: BracketSpec | None = None) -> Order:
        if not self.stood_down and self._roll_blocks(order):
            self.roll_refused += 1
            order.state = OrderState.CANCELLED
            return order
        if self.stood_down or self._refuses(order):
            # stood down (kill switch), or the gate refuses a new entry: the
            # order is refused, not queued. The caller still gets an Order back
            # (already CANCELLED), so a strategy that keeps the handle never
            # sees None.
            order.state = OrderState.CANCELLED
            return order
        if order.active_ts is None:
            if order.bracket_leg and self.bracket_ns is not None:
                order.active_ts = self.now_ts + self.bracket_ns
            elif self.latency_ns:
                order.active_ts = self.now_ts + self.latency_ns
        self.working.append(order)
        if bracket and (bracket.stop_ticks or bracket.target_ticks):
            self._brackets[order.id] = bracket
        return order

    def cancel(self, order: Order) -> None:
        """A STRATEGY cancel. Instant unless cancel_latency_ms is set; then the order
        stays live (and can fill) until the request reaches the exchange."""
        if self.cancel_ns > 0:
            if order.state is OrderState.WORKING and order in self.working:
                self._queue_request(order, "cancel", None, self.cancel_ns)
            return
        self._cancel_now(order)

    def cancel_all(self, entries_only: bool = False) -> None:
        """A STRATEGY cancel of every working order (see cancel)."""
        for o in list(self.working):
            if entries_only and o.is_exit:
                continue
            self.cancel(o)

    def force_cancel_all(self, entries_only: bool = False) -> None:
        """An ENGINE cancel (session end, roll flatten, news, kill switch): instant,
        never delayed, and it drops the requests still on their way."""
        for o in list(self.working):
            if entries_only and o.is_exit:
                continue
            self._cancel_now(o)
        self._requests = [r for r in self._requests
                          if r[3].state is OrderState.WORKING and r[3] in self.working]

    def _cancel_now(self, order: Order) -> None:
        if order.state is OrderState.WORKING and order in self.working:
            order.state = OrderState.CANCELLED
            self.working.remove(order)

    def modify(self, order: Order, price: float) -> bool:
        """Change a working limit/stop price. Takes effect from the next event
        the order is evaluated against (no retroactive fills); with modify_latency_ms
        set, the old price stays live until the request reaches the exchange (and
        order.price shows the live price until then)."""
        if order.state is not OrderState.WORKING or order.type is OrderType.MARKET:
            return False
        if self.modify_ns > 0:
            self._queue_request(order, "modify", float(price), self.modify_ns)
            return True
        order.price = float(price)
        return True

    # ---------------- strategy requests on their way to the exchange ----------------

    def _queue_request(self, order: Order, kind: str, price: float | None,
                       ns: int) -> None:
        """A cancel/modify sent now arrives `ns` later, and never before the order it
        names has arrived (one connection, in order): at least 1 ns after active_ts."""
        at = self.now_ts + ns
        if order.active_ts is not None:
            at = max(at, order.active_ts + 1)
        self._request_seq += 1
        self._requests.append((at, self._request_seq, kind, order, price))

    def _next_request_index(self, i: int, i1: int) -> int | None:
        """Index in [i, i1) of the first event at or after the earliest queued
        request's arrival time, or None."""
        if not self._requests:
            return None
        t = min(r[0] for r in self._requests)
        k = max(i, int(np.searchsorted(self._day.ts, t, side="left")))
        return k if k < i1 else None

    def _apply_requests_at(self, k: int) -> None:
        """Apply, in arrival order, every queued request that has arrived by event
        k's timestamp. A request whose order is gone (filled, cancelled) is dropped."""
        t = int(self._day.ts[k])
        due = sorted((r for r in self._requests if r[0] <= t), key=lambda r: (r[0], r[1]))
        self._requests = [r for r in self._requests if r[0] > t]
        for _, _, kind, o, price in due:
            if o.state is not OrderState.WORKING or o not in self.working:
                continue
            if kind == "cancel":
                self._cancel_now(o)
            elif o.type is not OrderType.MARKET:
                o.price = price

    # ---------------- day lifecycle ----------------

    def begin_day(self, day: DayL1) -> None:
        self._day = day

    def flatten(self, idx: int, tag: str = "flatten") -> None:
        """Close any open position at the quotes of event `idx`."""
        pos = self.account.position
        if pos == 0:
            return
        side = SELL if pos > 0 else BUY
        o = Order(side=side, qty=abs(pos), type=OrderType.MARKET,
                  tag=tag, is_exit=True)
        self._fill(o, idx)

    # ---------------- span resolution ----------------

    def resolve_span(self, i0: int, i1: int) -> None:
        """Resolve fills and mark equity over trade events [i0, i1)."""
        if self.halted or self.stood_down or self._day is None or i0 >= i1:
            return
        i = i0
        guard = 0
        while i < i1 and self.working:
            k_req = self._next_request_index(i, i1)   # None unless a cancel/modify is due
            best_k, best_o = None, None
            for o in self.working:
                k = self._trigger_index(o, i, i1)
                if k is not None and (best_k is None or k < best_k):
                    best_k, best_o = k, o
            if k_req is not None and (best_k is None or k_req <= best_k):
                # a strategy cancel/modify reaches the exchange before this fill (or
                # before the next trigger): apply it, then look again from there
                self._mark(i, k_req)
                if self.halted or self.stood_down:
                    return
                self._apply_requests_at(k_req)
                i = k_req
                guard += 1
                if guard > 10000:
                    raise RuntimeError("resolve_span: runaway request loop")
                continue
            if best_o is None:
                break
            self._mark(i, best_k)
            if self.halted or self.stood_down:
                return
            self._fill(best_o, best_k)
            if self.stood_down:
                return
            i = best_k
            guard += 1
            if guard > 10000:
                raise RuntimeError("resolve_span: runaway fill loop")
        self._mark(i, i1)

    def resolve_span_ticks(self, i0: int, i1: int, on_tick) -> None:
        """resolve_span variant that calls on_tick(ts, price, idx) after each
        trade event, so orders a strategy submits inside on_tick fill on LATER
        events (no look-ahead). Fills/marks are identical to resolve_span when
        on_tick submits nothing — only finer-grained. Per-event Python, so
        slower; the engine uses it only when a Strategy defines on_tick.
        """
        if self.halted or self.stood_down or self._day is None or i0 >= i1:
            return
        d = self._day
        i = i0
        for e in range(i0, i1):
            if self._requests and min(r[0] for r in self._requests) <= int(d.ts[e]):
                self._apply_requests_at(e)         # a cancel/modify has arrived by e
            # fill every working order that triggers at or before event e
            while self.working:
                best_k, best_o = None, None
                for o in self.working:
                    k = self._trigger_index(o, i, e + 1)
                    if k is not None and (best_k is None or k < best_k):
                        best_k, best_o = k, o
                if best_o is None:
                    break
                self._mark(i, best_k)
                if self.halted or self.stood_down:
                    return
                self._fill(best_o, best_k)
                if self.stood_down:
                    return
                i = best_k
            self._mark(i, e + 1)
            if self.halted or self.stood_down:
                return
            i = e + 1
            on_tick(int(d.ts[e]), float(d.price[e]), e)

    def _trigger_index(self, o: Order, i: int, i1: int) -> int | None:
        d = self._day
        p = d.price
        lo = i                      # first event this order may act on
        if o.active_ts is not None:
            lo = max(i, int(np.searchsorted(d.ts, o.active_ts, side="left")))
            if lo >= i1:
                return None         # still on its way to the exchange
        if o.type is OrderType.MARKET:
            return lo
        if o.type is OrderType.LIMIT:
            quote = d.ask[lo] if o.side == BUY else d.bid[lo]
            if not np.isnan(quote) and (
                    quote <= o.price if o.side == BUY else quote >= o.price):
                return lo  # marketable at first evaluation
            seg = p[lo:i1]
            hit = seg < o.price if o.side == BUY else seg > o.price
            k = int(np.argmax(hit))
            return lo + k if hit[k if k < len(hit) else 0] else None
        elif o.plan is not None:  # dynamic stop (auto-BE / trailing)
            seg = p[i:i1]       # the whole uncommitted window: levels() needs it
            levels = o.plan.levels(seg)
            hit = seg >= levels if o.side == BUY else seg <= levels
            if lo > i:
                hit[:lo - i] = False
            k = int(np.argmax(hit))
            if not hit[k if k < len(hit) else 0]:
                return None
            o.price = float(levels[k])   # effective stop at the trigger
            return i + k
        else:  # STOP
            seg = p[lo:i1]
            hit = seg >= o.price if o.side == BUY else seg <= o.price
            k = int(np.argmax(hit))
            return lo + k if hit[k if k < len(hit) else 0] else None

    def _fill_price(self, o: Order, idx: int) -> float:
        d = self._day
        if o.type is OrderType.LIMIT:
            quote = d.ask[idx] if o.side == BUY else d.bid[idx]
            if not np.isnan(quote) and (
                    quote <= o.price if o.side == BUY else quote >= o.price):
                return float(quote)     # marketable limit: fill at the quote
            return float(o.price)
        # market / triggered stop: prevailing quote +/- slippage
        quote = d.ask[idx] if o.side == BUY else d.bid[idx]
        base = float(quote) if not np.isnan(quote) else float(d.price[idx])
        px = base + o.side * self.slippage
        if o.type is OrderType.STOP:
            # never fill a stop better than its trigger price
            px = max(px, o.price) if o.side == BUY else min(px, o.price)
        return px

    # ---------------- exit orders only reduce ----------------

    def _stale_exit(self, o: Order) -> bool:
        """True if exit order `o` cannot reduce the position now: it is flat, or
        the order is on the position's own side (it would add)."""
        pos = self.account.position
        return pos == 0 or o.side * pos > 0

    def _drop(self, o: Order) -> None:
        """Cancel a working exit order that has nothing left to exit."""
        if o.state is OrderState.WORKING and o in self.working:
            self._cancel_now(o)
            self.exits_dropped += 1

    def _drop_stale_exits(self) -> None:
        """Cancel every working exit order that could no longer REDUCE the
        position: all of them when it is flat, those on the position's own side
        when it has reversed. This is the reduce-only rule (NT8 cancels a
        position's managed stops and targets when it closes; Nautilus does the same
        for reduce-only orders). Without it a bracket leg left behind by the
        strategy's own reversal or close would later OPEN a position, or close a
        later trade at the old leg's level.
        Exit orders submitted while the position is on the right side stay."""
        for o in list(self.working):
            if o.is_exit and self._stale_exit(o):
                self._drop(o)

    def _fill(self, o: Order, idx: int) -> None:
        if o.is_exit:
            if self._stale_exit(o):
                self._drop(o)           # nothing to reduce (flat, or it would add)
                return
            pos = self.account.position
            if pos != 0 and o.qty > abs(pos):
                o.qty = abs(pos)        # an exit never reverses
        d = self._day
        px = self._fill_price(o, idx)
        ts = int(d.ts[idx])
        self.now_ts = ts            # orders sent from here on (children, on_fill)
        o.state = OrderState.FILLED
        o.fill_price, o.fill_ts = px, ts
        if o in self.working:
            self.working.remove(o)

        # Apex max-position guard: clamp this fill so the resulting net
        # position never exceeds the cap. Correct for both adds and reversals
        # (a reversal only clamps if the far side would overshoot the cap).
        pos_before = self.account.position
        after = pos_before + o.side * o.qty
        if (self.max_position is not None and not o.is_exit
                and abs(after) > self.max_position):
            o.qty = max(0, o.qty - (abs(after) - self.max_position))
            if not self._cap_warned:
                print(f"  [broker] max-position cap {self.max_position} hit — "
                      f"clamping entry to keep |position| <= cap")
                self._cap_warned = True
            if o.qty == 0:
                o.state = OrderState.CANCELLED
                return
            after = pos_before + o.side * o.qty

        # split fills that reverse through flat so trade recording stays clean
        parts = [(o.qty, pos_before)]
        if pos_before != 0 and after != 0 and (after > 0) != (pos_before > 0):
            parts = [(abs(pos_before), pos_before), (abs(after), 0)]
        done = 0
        for qty, pb in parts:
            if self.entries_blocked and abs(pb + o.side * qty) > abs(pb):
                break        # the gate: nothing that opens or adds exposure
            self.account.apply_fill(o.side, qty, px)
            self.recorder.on_fill(self.account, ts, o.side, qty, px,
                                  o.tag, pb)
            done += qty
            self._book_closed(idx)       # may trip the gate; a later part obeys
        if done == 0:
            o.state = OrderState.CANCELLED
            return
        o.qty = done                     # a reversal cut at flat fills only its close
        # equity right after the fill, at the fill price: commission and
        # slippage are in it, which no trade-price mark would show
        eq_after = self.account.equity(px)
        self.dd.update_scalar(eq_after)

        fill = Fill(order=o, ts=ts, price=px, qty=o.qty, side=o.side,
                    commission=self.spec.commission_side * o.qty)
        self.fills.append(fill)

        # OCO sibling cancellation
        if o.oco_id is not None:
            for sib in list(self.working):
                if sib.id == o.oco_id:
                    self._cancel_now(sib)

        # a fill that took the position away (or turned it) leaves the legs it did
        # not cancel itself without a position to protect: drop them, before this
        # fill's own children exist. (After the OCO step, so a stop's own target is
        # not counted: exits_dropped is the legs a strategy LEFT behind.)
        self._drop_stale_exits()

        # bracket children activation
        br = self._brackets.pop(o.id, None)
        if br is not None and self.account.position != 0:
            exit_side = -o.side
            stop_o = target_o = None
            if br.stop_ticks:
                stop_px = px - o.side * br.stop_ticks * self.spec.tick_size
                stop_o = Order(side=exit_side, qty=o.qty, type=OrderType.STOP,
                               price=stop_px, tag="stop", is_exit=True,
                               bracket_leg=True)
            if br.target_ticks:
                tgt_px = px + o.side * br.target_ticks * self.spec.tick_size
                target_o = Order(side=exit_side, qty=o.qty,
                                 type=OrderType.LIMIT, price=tgt_px,
                                 tag="target", is_exit=True, bracket_leg=True)
            if stop_o and target_o:
                stop_o.oco_id, target_o.oco_id = target_o.id, stop_o.id
            for child in (stop_o, target_o):
                if child is not None:
                    self.submit(child)

        if self.on_fill:
            self.on_fill(fill)

        # a fill can cross a kill-switch limit by itself (a stop-loss fill, or
        # slippage/commission on any fill)
        if not self.stood_down:
            if self.dll_floor is not None and eq_after <= self.dll_floor:
                self._stand_down(idx, "dll")
            elif self.dpt_ceiling is not None and eq_after >= self.dpt_ceiling:
                self._stand_down(idx, "dpt")

    # ---------------- equity marking ----------------

    def _mark(self, i: int, k: int) -> None:
        """Mark equity/prop/drawdown/trade-extremes over events [i, k) with
        constant position. Enforces the daily loss limit on every event."""
        if i >= k or self._day is None:
            return
        d = self._day
        # commit this consumed segment into dynamic-stop high-water marks
        # (evaluation windows always extend past the last committed event,
        # so levels() sees every event exactly once at trigger time)
        for o in self.working:
            if o.plan is not None:
                o.plan.advance(d.price[i:k])
        pos = self.account.position
        if pos == 0:
            bal = self.account.balance
            self.dd.update_scalar(bal)
            if self.prop is not None:
                breach = self.prop.update_scalar(bal, int(d.ts[i]))
                if breach and self.prop.cfg.halt_on_breach:
                    self.halted = True
            if not self.stood_down:
                tag = ("dll" if self.dll_floor is not None and bal <= self.dll_floor
                       else "dpt" if self.dpt_ceiling is not None
                       and bal >= self.dpt_ceiling else None)
                if tag:
                    self.stood_down = True   # flat: nothing to flatten
                    self.limit_hit = self.limit_hit or tag
                    self.dll_ts = int(d.ts[i])
                    self.force_cancel_all()
            return
        seg = d.price[i:k]
        unreal = pos * (seg - self.account.avg_price) * self.spec.point_value
        eq = self.account.balance + unreal
        # kill switch: the first event at/below the floor (or at/above the
        # ceiling) is the last one experienced in position - the segment ends
        # there
        cut = cut_tag = None
        if not self.stood_down:
            firsts = []
            if self.dll_floor is not None:
                hit = eq <= self.dll_floor
                if hit.any():
                    firsts.append((int(np.argmax(hit)), "dll"))
            if self.dpt_ceiling is not None:
                hit = eq >= self.dpt_ceiling
                if hit.any():
                    firsts.append((int(np.argmax(hit)), "dpt"))
            if firsts:
                k, cut_tag = min(firsts)
                cut = k + 1
                unreal, eq = unreal[:cut], eq[:cut]
        self.recorder.mark(float(unreal.min()), float(unreal.max()))
        self.dd.update_series(eq)
        if self.prop is not None:
            breach = self.prop.update_series(eq, d.ts[i:i + len(eq)])
            if breach and self.prop.cfg.halt_on_breach:
                # flatten at the breach event; engine sees halted and stops
                self.halted = True
                floor = self.prop.floor
                hit = eq <= floor
                idx = i + (int(np.argmax(hit)) if hit.any() else len(eq) - 1)
                self.flatten(idx, tag="prop-halt")
                return
        if cut is not None:
            self._stand_down(i + cut - 1, cut_tag)
