"""HiLoRider — Khahn's Wave-bar Donchian trend-follower, ported for honest fills.

Source: `nt8 code/HiLoRider/HiLoRider v1.0 08-11-26/` (19 .cs files) plus
`Indicators/AlgoTrader/HiLoBands.cs`. Technical overview, the reported-results
analysis, and the staged test plan: `nt8 code/HiLoRider/HiLoRider.md`.
Bar type: `w120` (Wave Bars), ported and parity-gated — see
`nt8 code/HiLoRider/WaveBars/WaveBars.md`.

WHAT THIS IS. Under ~8,800 lines of NT8 infrastructure (HUD, WPF panel, ML
gate, meta-label, GEX, HMM, watchdogs, unmanaged brackets — all off by
default) the strategy is small: on Wave-120 bars, when a Donchian midline
ticks your way and the bar closes your way, enter with the trend; take profit
at a fixed 120 ticks; stop at the opposite Donchian band; trail on recent bar
lows/highs. `Signal2` (midline slope) does nearly all the work, so this
effectively enters on most with-trend bars while flat.

WHY IT IS BEING TESTED. The author reports Sharpe 19.193 / WR 88.5% / PF 7.80
/ MaxDD -$212 on 7,950 MNQ trades, which works out to ~$368k/year on one micro
contract before $8.3k of commission. HiLoRider.md §4.2 derives and MEASURES
the mechanism: a Wave bar's Heikin-Ashi close lags the price actually trading
when the bar completes by `5N/7` ticks — 86 ticks at Wave 120, confirmed at a
median of 88 on both MNQ and MGC, favourable on 99.8% of bars. A backtest that
enters at the bar's close therefore gets ~71% of its 120-tick target for free
on every trade. In THIS engine that is impossible: fills resolve on real ticks
via each bar's `[i0, i1)` span and never touch the HA values, so running the
strategy here produces the honest number mechanically.

PORTED (the live-default config, per State.DataLoaded — note the .cs module
header is stale and documents a retired TBars-era config):

  MNQ: lookback 25, TP 120t, stop-buffer 2t, trail-min 40t, stages 5/6/10
  MGC: lookback 10, TP 120t, stop-buffer 2t, trail-min  4t, stages 3/5/10

NOT PORTED, because all of it ships disabled: ML gate, meta-label, GEX, HMM
regime, SuperTrend, scale-out, DiffCross auto-exit, ADX filter, diff-band /
chop-hold filter, news block, midday block, volatility sizing, daily
loss/profit limits (Realtime-only anyway), drawdown gate. Each is a separate
testable idea; leaving them out keeps the first number interpretable.

KNOWN DIVERGENCES FROM NT8 (all deliberate, none load-bearing):

1. `on_bar` sees the closed bar; NT8 runs `Calculate.OnEachTick`, so its
   entries land intrabar and ours land on the next tick after the close.
2. NT8's historical target check is `High[0] >= targetLevel` against Wave-bar
   high/low — which include the phantom open, a price that never traded. Ours
   is a real limit order resolving on real ticks. This is a correctness
   improvement, and it means our exits will NOT match an NT8 Strategy
   Analyzer run, by design.
3. `EnableCloseConfirmation` is a near-no-op (HiLoBands already requires the
   same Close[0] vs Close[1] test internally); it differs only in realtime,
   where NT8's `Close[0]` is the forming bar. Kept for completeness.
4. `MaxDailyTrades` is inert in the source too — the guard is
   `!OpenForFreeTrade && ...` and `OpenForFreeTrade = true`.
5. Session windows TF1-TF7 collapse to all-hours because TF7 is 00:00-23:59
   and enabled, so `tf_windows = None` reproduces the shipped behaviour.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from backtester.indicators import Highest, Lowest
from backtester.strategy import Strategy

ET = ZoneInfo("America/New_York")


class HiLoRider(Strategy):
    symbol = "MNQ"
    period = "w120"
    # One CME trading day: 18:00 ET -> flat by 16:55, never holding through a
    # halt (CLAUDE.md "CME trading day"). The .cs uses all-hours entries plus
    # IsExitOnSessionCloseStrategy, which this is the faithful analogue of.
    session = ("18:00", "16:55")
    flat_at_session_end = True
    qty = 1

    # ---- HiLoBands (Donchian) --------------------------------------------
    lookback_period = 25          # MNQ live value; MGC ships 10

    # ---- exits -----------------------------------------------------------
    fixed_tp_ticks = 120          # TargetMode=FixedTicks
    stop_buffer_ticks = 2         # StopMode=ChannelBand: band -/+ this
    fixed_sl_ticks = 60           # FALLBACK only, when the band stop is invalid
    max_bars_in_trade = 10        # 0 = off

    # HighLow trail (TrailMode=HighLow), 3 stages on bars-since-entry
    trail_min_ticks = 40          # stage-2 buffer AND the floor from entry
    hl_stage1_lookback = 1
    hl_stage2_trigger_bars = 5
    hl_stage2_lookback = 0
    hl_stage3_trigger_bars = 6
    hl_stage3_trail_ticks = 10
    stop_market_buffer_ticks = 2  # TryMoveStop's keep-inside-market clamp

    # ---- entry -----------------------------------------------------------
    # The shipped live default is a PASSIVE limit `bid -/+ 8 ticks`
    # (useMarketOrder=false), which fills only on a retrace and, because
    # nothing ever cancels it, blocks every later signal while it rests.
    # 0 = market, which is the honest baseline (HiLoRider.md §6 Stage 2).
    entry_limit_offset_ticks = 0
    entry_limit_ttl_bars = 0      # 0 = never cancel, reproducing NT8's GTC
    # v1.2's LiquidityFilterPasses: block a signal when the prevailing spread at
    # the bar close is wider than this (equality passes), or when the quote is
    # unknown (fail closed). 0 = off, which is v1.0 behaviour.
    max_entry_spread_ticks = 0

    # ---- gates -----------------------------------------------------------
    hour_block = (1, 5)           # ET hours blocked INCLUSIVE; None = off
    rth_start = "09:30"
    rth_open_block_min = 15       # block the first N min after the RTH open
    cooldown_bars = 0
    tf_windows = None             # None = all hours (TF7 catch-all is enabled)
    use_signal2 = True            # slot-2 (midline slope) fallback
    # Gap filter: block COUNTER-gap entries for `gap_block_min` after the RTH
    # open when the session gapped at least `gap_filter_pct`. Approximate —
    # NT8 keys off Bars.IsFirstBarOfSession (its own session template), we key
    # off the engine's 18:00 ET Globex session start. Rarely binds.
    gap_filter = True
    gap_filter_pct = 0.50
    gap_block_min = 30

    # ------------------------------------------------------------------
    def on_start(self):
        self.hi = Highest(self.lookback_period)
        self.lo = Lowest(self.lookback_period)
        self.mid = None                 # midline this bar
        self.prev_mid = None
        self.prev_close = None
        self.n_bars = 0                 # NT8's CurrentBar + 1
        self.last_sig_bar = -1
        self.last_exit_bar = -10 ** 9
        self.slot = ""                  # which slot produced the live trade

        # in-trade state (mirrors the .cs fields of the same names)
        self.bars_in_trade = 0
        self.trail_stage = 0
        self.hl_peak = 0.0
        self.filled_price = 0.0
        self.stop_level = 0.0
        self.trade_dir = 0
        self.pending_stop = None        # absolute band stop, applied on fill

        # gap-filter state
        self.session_key = None
        self.prior_session_close = None
        self.session_gap_pct = 0.0
        self.gap_dir = 0

        # diagnostics, reported by on_finish
        self._hist = None
        self.entry_bar = -1
        self.n_signals = 0
        self.n_blocked = {"window": 0, "rth": 0, "hour": 0, "cooldown": 0,
                          "gap": 0, "entry-resting": 0, "entry-ttl": 0,
                          "spread": 0}
        self.bars_in_position = 0
        self.slot_counts = {"crossover": 0, "slope": 0}
        self.entries = 0

    # ---- helpers ---------------------------------------------------------
    @property
    def _tick(self) -> float:
        return self._broker.spec.tick_size

    def _et(self, ts_ns: int) -> datetime:
        return datetime.fromtimestamp(ts_ns / 1e9, ET)

    def _ref_price(self, bar) -> float:
        """Best estimate of the price actually trading when this bar closed.

        NOT `bar.close`: on a Wave bar that is the Heikin-Ashi average, which
        lags the market by ~5N/7 ticks (HiLoRider.md §4.2). A Wave bar always
        completes exactly AT one of its thresholds, and that threshold is one
        of its real extremes — the other extreme is the phantom open, which
        never traded. So the completing price is `high` on an up bar and `low`
        on a down bar, and the HA close direction identifies which (HA closes
        are strongly trending, so this is reliable on this bar type).

        Used only for the entry limit price and TryMoveStop's keep-inside-market
        clamp. The protective stop itself is placed at an absolute band price
        (see `on_fill`), so it does not depend on this estimate.
        """
        if self.prev_close is None:
            return bar.close
        return bar.high if bar.close >= self.prev_close else bar.low

    def _in_tf_window(self, ts_ns) -> bool:
        if not self.tf_windows:
            return True
        t = self._et(ts_ns)
        tod = t.hour * 60 + t.minute
        for w in self.tf_windows:
            sh, sm = map(int, w[0].split(":"))
            eh, em = map(int, w[1].split(":"))
            s, e = sh * 60 + sm, eh * 60 + em
            if s == e:
                continue
            if (s <= e and s <= tod <= e) or (s > e and (tod >= s or tod <= e)):
                return True
        return False

    def _hour_blocked(self, ts_ns) -> bool:
        if not self.hour_block:
            return False
        start, end = self.hour_block
        h = self._et(ts_ns).hour
        if end >= start:
            return start <= h <= end
        return h >= start or h <= end          # overnight wrap

    def _minutes_after_rth(self, ts_ns) -> float:
        """Minutes since today's RTH open (negative before it)."""
        t = self._et(ts_ns)
        sh, sm = map(int, self.rth_start.split(":"))
        return (t.hour * 60 + t.minute + t.second / 60.0) - (sh * 60 + sm)

    def _track_session(self, bar) -> None:
        """Detect a new Globex trading day and capture the gap at its open."""
        t = self._et(bar.ts)
        # trading day rolls at 18:00 ET, so anything from 18:00 belongs to the
        # NEXT calendar day's session
        key = t.date().toordinal() + (1 if t.hour >= 18 else 0)
        if key != self.session_key:
            if self.session_key is not None and self.prev_close is not None:
                prior = self.prev_close
                if prior > 0 and bar.open > 0:
                    self.session_gap_pct = (bar.open - prior) / prior * 100.0
                    self.gap_dir = (1 if self.session_gap_pct > 0
                                    else -1 if self.session_gap_pct < 0 else 0)
            self.session_key = key
            self.prior_session_close = self.prev_close

    def _gap_blocked(self, sig, ts_ns) -> bool:
        if not self.gap_filter or self.gap_block_min <= 0 or self.gap_dir == 0:
            return False
        if abs(self.session_gap_pct) < self.gap_filter_pct:
            return False
        after = self._minutes_after_rth(ts_ns)
        if not (0 <= after <= self.gap_block_min):
            return False
        return (sig == -1 and self.gap_dir == 1) or (sig == 1 and self.gap_dir == -1)

    def _spread_blocked(self, bar) -> bool:
        """v1.2 LiquidityFilterPasses, modelled on the prevailing quote at the
        bar's close (the same quote NT8's GetCurrentBid/Ask would return at that
        instant). Fails CLOSED on an unknown quote, exactly as the .cs does —
        which also means the crossed-quote events NaN'd out by
        `data._invalidate_crossed` block an entry rather than pricing one.
        """
        if self.max_entry_spread_ticks <= 0:
            return False
        day = getattr(self._broker, "_day", None)
        if day is None or len(day) == 0:
            return True
        i = int(day.ts.searchsorted(bar.ts, side="right")) - 1
        if i < 0:
            return True
        bid, ask = float(day.bid[i]), float(day.ask[i])
        if not (bid > 0 and ask > 0) or bid != bid or ask != ask or ask < bid:
            return True
        return (ask - bid) / self._tick - self.max_entry_spread_ticks > 1e-9

    # ---- signal ----------------------------------------------------------
    def _signal(self, bar) -> int:
        """HiLoBands' own two-slot cascade: fresh midline crossover first,
        midline slope as the fallback. One signal per bar (lastHiLoRiderSigBar).
        """
        cb = self.n_bars - 1                       # NT8's 0-based CurrentBar
        if cb < self.lookback_period + 1:
            return 0
        if cb == self.last_sig_bar:
            return 0
        if self.mid is None or self.prev_mid is None or self.prev_close is None:
            return 0

        c0, c1 = bar.close, self.prev_close
        m0, m1 = self.mid, self.prev_mid

        # slot 1 — Signal: a fresh cross of Close through the midline
        sig = 0
        if c0 > m0 and c1 <= m1 and c0 > c1:
            sig, slot = 1, "crossover"
        elif c0 < m0 and c1 >= m1 and c0 < c1:
            sig, slot = -1, "crossover"
        # slot 2 — Signal2: the midline's own slope, checked only if slot 1 was
        # silent. A genuinely looser condition, not a re-check of slot 1.
        elif self.use_signal2 and m0 > m1 and c0 > c1:
            sig, slot = 1, "slope"
        elif self.use_signal2 and m0 < m1 and c0 < c1:
            sig, slot = -1, "slope"

        if sig:
            self.last_sig_bar = cb
            self.slot = slot
        return sig

    # ---- stop placement --------------------------------------------------
    def _band_stop(self, sig, ref_px):
        """StopMode=ChannelBand: the opposite HiLoBands band -/+ buffer, with
        the .cs's validity check and FixedSLTicks fallback."""
        tick = self._tick
        buf = self.stop_buffer_ticks * tick
        raw = (self.lo.value - buf) if sig == 1 else (self.hi.value + buf)
        raw = round(raw / tick) * tick
        if (raw < ref_px) if sig == 1 else (raw > ref_px):
            return raw
        return round((ref_px - sig * self.fixed_sl_ticks * tick) / tick) * tick

    def _try_move_stop(self, candidate, ref_px) -> None:
        """TryMoveStop: clamp to stay `stop_market_buffer_ticks` inside the
        market, then move ONLY if it tightens."""
        if self.position == 0 or self.stop_order is None:
            return
        tick = self._tick
        buf = self.stop_market_buffer_ticks * tick
        is_long = self.trade_dir == 1
        safe = (min(candidate, round((ref_px - buf) / tick) * tick) if is_long
                else max(candidate, round((ref_px + buf) / tick) * tick))
        better = safe > self.stop_level if is_long else safe < self.stop_level
        if not better:
            return
        if self.move_stop(safe):
            self.stop_level = safe

    def _manage_hl_trail(self, bar) -> None:
        """ManageHighLowTrail: 3 stages on bars-since-entry, tighten-only,
        floored at `trail_min_ticks` from entry."""
        tick = self._tick
        if self.n_bars - 1 <= max(self.hl_stage1_lookback, self.hl_stage2_lookback):
            return
        if self.hl_peak == 0:
            self.hl_peak = self.filled_price
        if self.trade_dir == 1:
            self.hl_peak = max(self.hl_peak, bar.high)
        else:
            self.hl_peak = min(self.hl_peak, bar.low)

        if (self.bars_in_trade >= self.hl_stage3_trigger_bars
                and self.trail_stage < 3):
            self.trail_stage = 3
        elif (self.bars_in_trade >= self.hl_stage2_trigger_bars
                and self.trail_stage < 2):
            self.trail_stage = 2
        elif self.trail_stage < 1:
            self.trail_stage = 1

        if self.trail_stage == 3:
            off = self.hl_stage3_trail_ticks * tick
            stop = self.hl_peak - off if self.trade_dir == 1 else self.hl_peak + off
        else:
            if self.trail_stage == 2:
                lb, off = self.hl_stage2_lookback, self.trail_min_ticks * tick
            else:
                lb, off = self.hl_stage1_lookback, self.stop_buffer_ticks * tick
            basis = self._bar_extreme(lb)
            if basis is None:
                return
            stop = basis - off if self.trade_dir == 1 else basis + off
        stop = round(stop / tick) * tick

        # floor: at least trail_min_ticks from ENTRY (a widening clamp)
        if abs(self.filled_price - stop) / tick < self.trail_min_ticks:
            stop = round((self.filled_price
                          - self.trade_dir * self.trail_min_ticks * tick) / tick) * tick
        self._try_move_stop(stop, self._ref_price(bar))

    def _bar_extreme(self, bars_ago: int):
        """Low[bars_ago] for a long / High[bars_ago] for a short, off the
        engine's bar history (index -1 is the bar that just closed)."""
        h = self._hist
        if h is None or len(h) <= bars_ago:
            return None
        arr = h.lows if self.trade_dir == 1 else h.highs
        return float(arr[-1 - bars_ago])

    # ---- hooks -----------------------------------------------------------
    def on_fill(self, fill):
        o = fill.order
        if o.is_exit:
            self.last_exit_bar = self.n_bars - 1
            self.filled_price = 0.0
            self.trade_dir = 0
            self.trail_stage = 0
            self.hl_peak = 0.0
            self.bars_in_trade = 0
            self.stop_level = 0.0
            self.pending_stop = None
            return
        # entry fill: NT8 places the protective stop at an ABSOLUTE price (the
        # channel band), not at a distance from the fill. The bracket child
        # already exists at this point, so correct it here — before any later
        # event in this span is evaluated against it.
        self.filled_price = fill.price
        self.trade_dir = 1 if fill.side > 0 else -1
        self.trail_stage = 0
        self.hl_peak = 0.0
        self.bars_in_trade = 0
        self.entries += 1
        self.slot_counts[self.slot] = self.slot_counts.get(self.slot, 0) + 1
        stop = self.pending_stop
        tick = self._tick
        if stop is None or ((stop >= fill.price) if self.trade_dir == 1
                            else (stop <= fill.price)):
            stop = fill.price - self.trade_dir * self.fixed_sl_ticks * tick
            stop = round(stop / tick) * tick
        self.stop_level = stop
        self.move_stop(stop)
        self.pending_stop = None

    def on_bar(self, bar, bars):
        self._hist = bars
        self.n_bars += 1
        self._track_session(bar)

        # Donchian, inclusive of the current bar (NT8's MAX(High, N)[0])
        self.prev_mid = self.mid
        self.hi.update(bar.high)
        self.lo.update(bar.low)
        cb = self.n_bars - 1
        self.mid = ((self.hi.value + self.lo.value) / 2.0
                    if cb >= self.lookback_period else None)

        try:
            if cb < 20:                            # BarsRequiredToTrade
                return

            if self.position != 0:
                self.bars_in_trade += 1
                if (self.max_bars_in_trade > 0
                        and self.bars_in_trade >= self.max_bars_in_trade):
                    self.close_position(tag="maxbars")
                    return
                self._manage_hl_trail(bar)
                self.bars_in_position += 1
                return

            # a resting entry limit blocks every later signal (NT8 nulls
            # entryOrder only on Filled/Cancelled/Rejected)
            resting = [o for o in self.working_orders if not o.is_exit]
            if resting:
                if (self.entry_limit_ttl_bars > 0
                        and cb - self.entry_bar >= self.entry_limit_ttl_bars):
                    # v1.2's stale-limit-entry cancel. NT8 cancels ASYNC and
                    # `IsEntryStateClear()` still sees the order as active for
                    # the rest of this bar, so this bar cannot re-enter.
                    self.cancel_all()
                    self.n_blocked["entry-ttl"] += 1
                else:
                    self.n_blocked["entry-resting"] += 1
                return

            if not self._in_tf_window(bar.ts):
                self.n_blocked["window"] += 1
                return
            after_rth = self._minutes_after_rth(bar.ts)
            if self.rth_open_block_min > 0 and 0 <= after_rth < self.rth_open_block_min:
                self.n_blocked["rth"] += 1
                return
            if cb - self.last_exit_bar <= self.cooldown_bars:
                self.n_blocked["cooldown"] += 1
                return
            if self._hour_blocked(bar.ts):
                self.n_blocked["hour"] += 1
                return

            sig = self._signal(bar)
            if sig == 0:
                return
            self.n_signals += 1

            if self._gap_blocked(sig, bar.ts):
                self.n_blocked["gap"] += 1
                return
            if self._spread_blocked(bar):
                self.n_blocked["spread"] += 1
                return

            tick = self._tick
            ref = self._ref_price(bar)
            self.pending_stop = self._band_stop(sig, ref)
            # stop_ticks here is only a placeholder that makes the bracket
            # create a stop leg; on_fill immediately re-places it at the
            # absolute band price above. target_ticks=None reproduces v1.2's
            # TargetMode=NoTarget (no profit target at all, trail-only exits).
            kw = dict(stop_ticks=self.fixed_sl_ticks,
                      target_ticks=self.fixed_tp_ticks or None)
            self.entry_bar = cb
            if self.entry_limit_offset_ticks > 0:
                px = ref - sig * self.entry_limit_offset_ticks * tick
                px = round(px / tick) * tick
                if sig == 1:
                    self.buy_limit(px, tag="HLR LE", **kw)
                else:
                    self.sell_limit(px, tag="HLR SE", **kw)
            else:
                if sig == 1:
                    self.buy_bracket(tag="HLR LE", **kw)
                else:
                    self.sell_bracket(tag="HLR SE", **kw)
        finally:
            self.prev_close = bar.close

    def on_finish(self):
        print(f"  HiLoRider: {self.n_signals} signals -> {self.entries} entries "
              f"({self.slot_counts}); {self.bars_in_position} bars in position")
        # every count except 'gap' is BARS gated before the signal was even
        # evaluated (the .cs returns early the same way); 'gap' is signals.
        print(f"  bars gated pre-signal / signals gapped: {self.n_blocked}")
