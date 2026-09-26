"""gbTokyoDrift — NY volume-profile levels + pre-market range + FVG continuation.

Port of `~/dev/NinjaScript/fitAsianMom/ADITRADYFITMOM/gbTokyoDrift.cs` (GreyBeard,
v1.0.0-Beta). Built 2026-09-25 because NT8's Strategy Analyzer isn't trusted for
fill accuracy here (bar-based/synthetic fills); this repo resolves every fill on
real recorded ticks instead — same rationale as every other NT8 port in this repo.

**PARITY STATUS (2026-09-25): 14/20 exact signal-level matches, mechanism
VALIDATED, one isolated residual not yet root-caused.** See
`tools/compare_tokyodrift.py`. Ground truth is the .cs's own `DiagLog` output
(`tokyodrift-fvg-diag.log`, MGC 5m, 2026-06-14..2026-09-21, captured via a
temporary diagnostic build of gbTokyoDrift.cs — see that strategy's Obsidian
testing log, 2026-09-25 entry). Our local MGC cache only reaches 2026-08-07, so
the checkable overlap is 2026-06-14..2026-08-07 (20 of the .cs's 25 fires); the
gate is signal-level agreement (entry bar + direction + level name), NOT P&L,
which legitimately differs (real tick fills here vs. SA's bar fills there — the
entire point of this port existing).

Result: **all 8 Range-High/Range-Low fires matched exactly**, with near-identical
`gapAtr` on every one (e.g. 0.51 vs 0.46, 0.97 vs 0.99 — rounding-scale, not
structural). This validates the core mechanism: `InWin`, bar-open reconstruction,
the 3-bar gap-detection geometry, and the touch/lag/cooldown/fill/confirm state
machine all check out. The 6 misses and 6 extras are **isolated entirely to NY
POC/VAH/VAL touches** — a borderline touch-tolerance call (tolerance is a tight
`0.10 * ATR`) tipping differently on specific nights, most likely from ordinary
NT8-vs-Python bar-construction differences propagating into the volume-profile
bins/POC-VAH-VAL values (this repo documents exactly this class of residual at
length elsewhere — e.g. TBars' ±1-tick HA-rounding propagation, ninZaRenko's
96-100% rather than 100% — and treats it as expected, not blocking). NOT yet
root-caused to a specific line; the next debugging step, if pursued, is
comparing this port's raw 5m OHLC against a `gbBarExporter` NT8 chart export for
the same MGC window (the established parity tool for exactly this question),
which would show directly whether the bars themselves differ before suspecting
the volume-profile math built on top of them.

**DROPPED** (live-trading-only, no backtest meaning — same rationale as
`gb_pullback_5bar.py`'s docstring): `ShowFvgBoxes`/`Draw.*`, the alert/`Fire` path,
the on-chart dashboard and its manual AUTO/LONG/SHORT/REV/BE/nudge controls,
`MaxDaysToKeep` draw-tag housekeeping, the public `Signal` series/plot,
`TouchAfterStart`/`AlertTouchTicks`/intrabar touch alerts (no meaning against
bar-close-resolved fills). `EngineTolMode` is ported ATR-only — the .cs's
`Ticks`/`None` modes are unused at gbTokyoDrift's shipped defaults and not needed
for the parity check.

**SESSION**: uses this engine's native overnight-session support,
`session=("18:00","16:55")` — the actual CME trading day (see CLAUDE.md's "CME
trading day" note) — deliberately NOT `session=None`. `session=None` treats each
ET-calendar-day *file* as one segment (`day_end=True`, no carry across the
UTC-midnight file boundary), which would split every overnight setup in this
strategy's 18:00-02:00 ET window in half at midnight — the exact class of bug the
.cs's own `|| newDay` fix (2026-09-22, see its changelog) exists to prevent,
reintroduced from the engine side. Bars 16:55-18:00 ET (the daily maintenance
halt) are skipped, which costs nothing real: no trades print there anyway, and
the NY profile window (9:30-16:00 ET) is untouched by the exclusion.

**BAR-TIME RECONSTRUCTION** (the single highest-fidelity-risk spot in this port):
the .cs computes every session test off the bar's OPEN time
(`Time[barsAgo].Subtract(barSpan)` for time bars), never its close time, because
NT8's `Time[]` is close-stamped. `Bar.ts` here is *also* close time, so every
window test below goes through `_open_dt(bar)`, never `bar.ts` directly. A bar
attributed to the wrong window here would silently misattribute the touch/gap
detection windows below it.

Window-boundary arithmetic (night-window end, range-window end, the
trading-begins line) is done on **naive** local-clock datetimes — an ET-aware
reading is taken then immediately stripped of tzinfo, and all further
add-minutes/date arithmetic happens naive-to-naive — matching the .cs's own
naive `DateTime` math exactly (NT8 hands NinjaScript already-localized wall-clock
stamps with no timezone object at all). This reproduces the .cs's own
DST-transition-night simplification (not perfectly "correct", but not this
port's bug to fix silently) rather than diverging from it.

Gap-native stop uses the same estimate-then-snap pattern as
`gb_pullback_5bar.py`: `buy_bracket`/`sell_bracket` take tick offsets from the
eventual fill, but our stop is an ABSOLUTE price anchored to the FVG's far edge
— so an estimated `stop_ticks` (from `bar.close`, so no bar is ever naked) goes
into the bracket at submission, then `on_fill` snaps it to the exact price via
`move_stop`. The target is a tick count (`target_r_multiple` * the *estimated*
risk), which needs no such correction — the .cs computes its own target off the
same kind of reference-price estimate (`Close[0]`), not the eventual fill, so
this is matched behavior, not an approximation this port introduces alone.

**REGIME FILTERS (research, 2026-09-25 — NOT in the .cs; all default OFF, so
baseline and parity are unchanged: $2,248.52 / 322 trades, 14/20).** Features
are recorded on every `fires[]` row (and trades carry `#<fire_idx>` in their
entry tag) for `tools/tokyodrift_regime.py`. Findings, MGC 2025-01..2026-08:
  * Daily trend alignment (the original "counter-trend signals fail"
    hypothesis): REFUTED — at 8/1.5, with-trend trades do worse than against.
  * Overnight-drift alignment: looks strong at 4/2.0, but the "against" side
    turns profitable at 8/1.5 — a split that flips with the stop buffer isn't a
    regime effect. Not carried forward.
  * Vol expansion (`vol_min_ratio`): the only feature positive in both years,
    at both param sets, ex-top-5. In-sample, fixed >=1.0 at 8/1.5 gives PF
    1.98 / 125 trades and turns 2026Q3 from -$400 to +$49. But it FAILS
    walk-forward when swept: stitched OOS $1,345 vs $2,682 unfiltered (5x5),
    $2,175 vs $2,757 (8x3), and the optimizer picked "off" for the recent
    losing window in both layouts. Treat >=1.0 as an unvalidated, pre-registered
    hypothesis for data after 2026-08-07 — not as a validated filter.
`regime_seed` warms session history from the bar cache so each walk-forward
OOS segment (a fresh Backtest) isn't blocked by a 40-session warmup; only
sessions that ended before the run's first bar are read (checked: live
session ranges match the table exactly).

**DATA**: every result above predates a repo fix. MGC days around the Aug 2026
roll (2026-07-29..08-18) had been converted from the expiring 08-26 contract,
so they held almost no trades. That window is the end of the "recent losing"
stretch, so re-run after the repo and `.cache/` are rebuilt for those dates
(see CLAUDE.md "Data").
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from backtester import ATR, EMA, Strategy
from backtester.strategy import parse_barspec

ET = ZoneInfo("America/New_York")


_SESSION_TABLE: dict = {}


def _session_table(symbol: str, period_s: int):
    """[(session_start_naive_et, session_close, session_range)] per CME session
    (18:00 ET -> 16:55 ET), built once per process from the on-disk bar cache.

    Exists ONLY to warm the regime filters: walk-forward runs every OOS segment
    as a fresh Backtest, so a 40-session lookback would otherwise block most of
    each 48-52-day OOS window. The strategy only ever reads sessions that ENDED
    before its first bar (see _seed_regime), so this is history, not lookahead.
    """
    key = (symbol, period_s)
    if key in _SESSION_TABLE:
        return _SESSION_TABLE[key]
    import pyarrow.parquet as pq
    from backtester.data import DEFAULT_CACHE_ROOT
    sess: dict = {}
    for p in sorted((DEFAULT_CACHE_ROOT / "bars" / symbol / f"{period_s}s").glob("*.parquet")):
        t = pq.read_table(p, columns=["ts_end", "high", "low", "close"]).to_pydict()
        for ts, hi, lo, cl in zip(t["ts_end"], t["high"], t["low"], t["close"]):
            o = datetime.fromtimestamp(ts / 1e9, ET).replace(tzinfo=None)
            o -= timedelta(seconds=period_s)
            tod = o.hour * 60 + o.minute
            if 16 * 60 + 55 <= tod < 18 * 60:
                continue   # daily halt — the engine's session skips these too
            start = datetime(o.year, o.month, o.day, 18, 0)
            if tod < 18 * 60:
                start -= timedelta(days=1)
            s = sess.get(start)
            if s is None:
                sess[start] = [hi, lo, cl]
            else:
                s[0], s[1], s[2] = max(s[0], hi), min(s[1], lo), cl
    rows = [(k, v[2], v[0] - v[1]) for k, v in sorted(sess.items())]
    _SESSION_TABLE[key] = rows
    return rows


def _hm_to_min(s: str) -> int:
    h, m = map(int, s.split(":"))
    return h * 60 + m


def _in_win(tod_min: float, start_min: int, end_min: int) -> bool:
    """Session-window test with midnight wrap-around. Mirrors gbTokyoDrift.cs InWin()."""
    if start_min == end_min:
        return True
    if start_min < end_min:
        return start_min <= tod_min < end_min
    return tod_min >= start_min or tod_min < end_min


class _Touch:
    __slots__ = ("name", "price", "bar")

    def __init__(self, name, price, bar):
        self.name, self.price, self.bar = name, price, bar


class _Setup:
    __slots__ = ("name", "dir", "gtop", "gbot", "confp", "touch_price",
                 "fvg_bar", "fill_bar", "st", "gap_atr", "disp_atr", "lag")

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class GbTokyoDrift(Strategy):
    symbol = "MGC"
    period = "5m"
    session = ("18:00", "16:55")   # CME trading day; see module docstring
    flat_at_session_end = False    # matches .cs IsExitOnSessionCloseStrategy=False
    qty = 1

    # ── 1. Sessions (ET "HH:MM") — match gbTokyoDrift.cs SetDefaults exactly ──
    ny_start, ny_end = "09:30", "16:00"
    night_start, night_end = "18:00", "02:00"
    range_start, range_end = "18:00", "20:00"
    trade_line_time = "20:00"

    # ── 2/3/4. Volume profile / levels ──
    value_area_pct = 0.70
    use_poc, use_vah, use_val = True, True, True
    use_range_high, use_range_low = True, True

    # ── 7. FVG engine ──
    fvg_on_va = True
    fvg_on_range = True
    fvg_after_start = False
    atr_length = 14
    engine_tol_atr = 0.10          # ATR-mode only (see docstring)
    touch_cooldown = 10
    min_lag = 1
    max_lag = 4
    min_gap_atr = 0.10
    min_disp_atr = 0.60
    need_reject = True
    fill_mode = "far"              # "near" | "mid" | "far" — .cs FillMode
    max_fill_bars = 30
    confirm_mode = "gap"           # "gap" (CloseBackThroughGap) | "extreme" (CloseBeyondFvgBarExtreme)
    max_confirm_bars = 15
    invalidation_buf = 0.15
    one_signal_per_day = False

    # ── New vs. the indicator: gap-native trade management ──
    stop_buffer_ticks = 4
    target_r_multiple = 2.0
    take_longs = True
    take_shorts = True
    bars_required_to_trade = 20    # ATR(14) warmup + headroom; .cs gates on CurrentBar>=3 + atr readiness

    # ── Regime filters (research, 2026-09-25 — NOT in the .cs). All default
    # OFF so the baseline/parity behavior above is unchanged. They gate ENTRIES
    # only: the FVG engine's touch/setup state runs identically either way.
    # Every input is from COMPLETED sessions or the current closed bar — no
    # lookahead. Filters fail CLOSED (no trade) until their inputs are warm.
    trend_filter = "none"          # "none" | "daily" (session close vs SMA of
                                   # session closes) | "ema" (5m close vs EMA)
    trend_len = 20                 # sessions for "daily", bars for "ema"
    vol_min_ratio = 0.0            # >0: require mean(last vol_fast session ranges) >=
                                   # this x mean(last vol_slow) — vol EXPANSION. 0 = off
    vol_fast, vol_slow = 10, 40    # sessions
    er_min = 0.0                   # min daily efficiency ratio (trend strength, direction-free). 0 = off
    er_len = 20                    # sessions
    drift_filter = False           # only trade WITH the current session's move so far
                                   # (bar close vs the 18:00 session open, signed by trade dir)
    regime_ema_len = 200           # 5m EMA for the "ema" trend filter + ema_side diagnostic
    regime_seed = True             # warm session history from the bar cache (see _session_table)

    # ---- lifecycle ----
    def on_start(self):
        self.atr = ATR(self.atr_length)
        self._tick = self._broker.spec.tick_size
        self._period_s = parse_barspec(self.period).seconds

        self.vol_at_price: dict[float, float] = {}
        self.ny_high = self.ny_low = None
        self.ny_active = False
        self.pending = False
        self.ny_date = None      # naive ET datetime
        self.p_date = None
        self.p_poc = self.p_vah = self.p_val = float("nan")
        self.night_end_ts = None  # naive ET datetime

        self.a_poc = self.a_vah = self.a_val = float("nan")
        self.va_end_ts = None

        self.r_hi = self.r_lo = float("nan")
        self.r_start_ts = self.r_end_ts = None
        self.r_done = False

        self.trade_start_ts = None

        self.last_touch_bar: dict[str, int] = {}
        self.last_sig_day: dict[str, int] = {}
        self.touches: list[_Touch] = []
        self.setups: list[_Setup] = []
        self.day_count = 0

        self.prev_in_night = False
        self.prev_in_range = False
        self.prev_bar_open = None

        self._pending_stop_px = None

        # Regime state: one row per completed CME session (18:00-16:55 ET)
        self.sess_open = self.sess_hi = self.sess_lo = self.sess_close = None
        self.sess_closes: list[float] = []
        self.sess_ranges: list[float] = []
        self.ema = EMA(self.regime_ema_len)
        self._seeded = not self.regime_seed
        self._first_sess_start = None

        # Diagnostics for the parity check (tools/compare_tokyodrift.py reads
        # this list directly when the strategy is driven from a script rather
        # than the CLI). Never affects trading behavior.
        self.fires: list[dict] = []

    def on_session_end(self, date):
        self.day_count += 1
        if self.sess_close is not None:
            full = None
            if self._first_sess_start is not None:
                # The run's first session is usually partial (a Backtest starts at
                # the 00:00 ET file boundary, six hours into the session). It has
                # now ended, so its full-session row from the cache is history.
                full = next((r for r in _session_table(self.symbol, self._period_s)
                             if r[0] == self._first_sess_start), None)
                self._first_sess_start = None
            if full is not None:
                self.sess_closes.append(full[1])
                self.sess_ranges.append(full[2])
            else:
                self.sess_closes.append(self.sess_close)
                self.sess_ranges.append(self.sess_hi - self.sess_lo)
        self.sess_open = self.sess_hi = self.sess_lo = self.sess_close = None

    def _seed_regime(self, t: datetime):
        """Prime session history with sessions that STARTED before the current
        one (so they have all ended — no lookahead)."""
        start = datetime(t.year, t.month, t.day, 18, 0)
        if t.hour * 60 + t.minute < 18 * 60:
            start -= timedelta(days=1)
        prior = [r for r in _session_table(self.symbol, self._period_s) if r[0] < start]
        keep = max(self.vol_slow, self.trend_len, self.er_len + 1) + 5
        for _, close, rng in prior[-keep:]:
            self.sess_closes.append(close)
            self.sess_ranges.append(rng)
        self._first_sess_start = start

    # ---- regime features (completed sessions + current closed bar only) ----
    def _regime(self, bar, atrv) -> dict:
        c, r = self.sess_closes, self.sess_ranges
        f = {"ema_side": float("nan"), "dtrend": float("nan"), "er": float("nan"),
             "vol_ratio": float("nan"), "atr_pct": atrv / bar.close * 100.0,
             "night_move": float("nan")}
        if self.ema.ready:
            f["ema_side"] = 1.0 if bar.close > self.ema.value else -1.0
        n = self.trend_len
        if len(c) >= n:
            f["dtrend"] = 1.0 if c[-1] > sum(c[-n:]) / n else -1.0
        if len(c) >= self.er_len + 1:
            w = c[-(self.er_len + 1):]
            noise = sum(abs(w[i] - w[i - 1]) for i in range(1, len(w)))
            f["er"] = abs(w[-1] - w[0]) / noise if noise > 0 else 0.0
        if len(r) >= self.vol_slow:
            slow = sum(r[-self.vol_slow:]) / self.vol_slow
            fast = sum(r[-self.vol_fast:]) / self.vol_fast
            f["vol_ratio"] = fast / slow if slow > 0 else float("nan")
        if self.sess_open is not None and atrv > 0:
            f["night_move"] = (bar.close - self.sess_open) / atrv
        return f

    def _regime_ok(self, direction, f) -> bool:
        if self.trend_filter == "daily":
            if f["dtrend"] != f["dtrend"] or f["dtrend"] != direction:
                return False
        elif self.trend_filter == "ema":
            if f["ema_side"] != f["ema_side"] or f["ema_side"] != direction:
                return False
        if self.vol_min_ratio > 0:
            if f["vol_ratio"] != f["vol_ratio"] or f["vol_ratio"] < self.vol_min_ratio:
                return False
        if self.drift_filter:
            if f["night_move"] != f["night_move"] or f["night_move"] * direction <= 0:
                return False
        if self.er_min > 0:
            if f["er"] != f["er"] or f["er"] < self.er_min:
                return False
        return True

    # ---- time helpers ----
    def _open_dt(self, bar) -> datetime:
        """Naive ET local datetime of this bar's OPEN time (see module docstring)."""
        close_dt = datetime.fromtimestamp(bar.ts / 1e9, ET).replace(tzinfo=None)
        return close_dt - timedelta(seconds=self._period_s)

    @staticmethod
    def _next_occurrence(after: datetime, tod_min: int) -> datetime:
        d = datetime(after.year, after.month, after.day) + timedelta(minutes=tod_min)
        if d <= after:
            d += timedelta(days=1)
        return d

    # ---- main per-bar logic (mirrors ProcessClosedBar) ----
    def on_bar(self, bar, bars):
        t = self._open_dt(bar)
        tod = t.hour * 60 + t.minute + t.second / 60.0

        ny_s, ny_e = _hm_to_min(self.ny_start), _hm_to_min(self.ny_end)
        night_s, night_e = _hm_to_min(self.night_start), _hm_to_min(self.night_end)
        rng_s, rng_e = _hm_to_min(self.range_start), _hm_to_min(self.range_end)

        in_ny = _in_win(tod, ny_s, ny_e)
        in_night = _in_win(tod, night_s, night_e)
        in_range = _in_win(tod, rng_s, rng_e)

        # 1) NY session finished -> compute POC/VAH/VAL. Finalize purely on
        #    leaving the window (+ a 25h staleness fallback), NEVER on a bare
        #    calendar-date change — that was gbTokyoDrift.cs's 2026-09-22 bug
        #    fix (the `|| newDay` split) and this port must not reintroduce it.
        if self.ny_active and (not in_ny or (self.ny_date and
                                              (t - self.ny_date) > timedelta(hours=25))):
            vp = self._find_poc()
            vh, vl = self._value_area(vp)
            self.p_poc, self.p_vah, self.p_val = vp, vh, vl
            self.p_date = self.ny_date
            self.pending = vp == vp   # not NaN
            self.ny_active = False

        # 2) Collect bars during the NY session
        if in_ny and not self.ny_active:
            self.vol_at_price.clear()
            self.ny_high, self.ny_low = bar.high, bar.low
            self.ny_date = t
            self.ny_active = True
        if in_ny:
            if bar.high > self.ny_high:
                self.ny_high = bar.high
            if bar.low < self.ny_low:
                self.ny_low = bar.low
            bin_size = self._tick * 2
            typical = (bar.high + bar.low + bar.close) / 3.0
            b = round(typical / bin_size) * bin_size
            self.vol_at_price[b] = self.vol_at_price.get(b, 0.0) + bar.volume

        # 3) Overnight window opens -> the latest pending session becomes live
        if in_night and not self.prev_in_night:
            s_min, e_min = night_s, night_e
            dur = (e_min - s_min + 1440) if (e_min - s_min) <= 0 else (e_min - s_min)
            start_ts = datetime(t.year, t.month, t.day) + timedelta(minutes=s_min)
            if start_ts > t:
                start_ts -= timedelta(days=1)
            self.night_end_ts = start_ts + timedelta(minutes=dur)

            if self.pending:
                self.a_poc = self.p_poc if self.use_poc else float("nan")
                self.a_vah = self.p_vah if self.use_vah else float("nan")
                self.a_val = self.p_val if self.use_val else float("nan")
                self.va_end_ts = self.night_end_ts
                self.pending = False

        # 4) Pre-market range high/low, extended to end of the overnight window
        if in_range and not self.prev_in_range:
            self.r_hi, self.r_lo = bar.high, bar.low
            self.r_start_ts = t
            self.r_end_ts = (self.night_end_ts if (self.night_end_ts and self.night_end_ts > t)
                              else self._next_occurrence(t, night_e))
            self.r_done = False
        elif in_range:
            self.r_hi = max(self.r_hi, bar.high)
            self.r_lo = min(self.r_lo, bar.low)
        if not in_range and self.prev_in_range:
            self.r_done = True

        # 5) "Trading begins" line
        h, m = map(int, self.trade_line_time.split(":"))
        v_ts = datetime(t.year, t.month, t.day, h, m)
        if self.prev_bar_open is not None and t >= v_ts and self.prev_bar_open < v_ts:
            self.trade_start_ts = v_ts

        va_live = self.va_end_ts is not None and t < self.va_end_ts
        rng_live = self.r_done and self.r_end_ts is not None and t < self.r_end_ts
        trade_live = (self.trade_start_ts is not None and self.night_end_ts is not None
                      and self.trade_start_ts <= t < self.night_end_ts)

        # Regime bookkeeping for the current (incomplete) session
        if not self._seeded:
            self._seed_regime(t)
            self._seeded = True
        if self.sess_open is None:
            self.sess_open, self.sess_hi, self.sess_lo = bar.open, bar.high, bar.low
        self.sess_hi = max(self.sess_hi, bar.high)
        self.sess_lo = min(self.sess_lo, bar.low)
        self.sess_close = bar.close
        self.ema.update(bar.close)

        # 6) FVG continuation engine
        atrv = self.atr.update(bar.high, bar.low, bar.close)
        if self.atr.ready and bar.index >= 2:
            sig_dir, sig_gtop, sig_gbot, sig_name = self._run_fvg_engine(
                bar, bars, atrv, va_live, rng_live, trade_live)
            if sig_dir != 0:
                feats = self._regime(bar, atrv)
                self.fires.append({
                    "entry_time": bar.ts, "dir": sig_dir, "name": sig_name,
                    "gtop": sig_gtop, "gbot": sig_gbot, "atr": atrv, **feats,
                })
                if self._regime_ok(sig_dir, feats):
                    self._try_enter(sig_dir, sig_gtop, sig_gbot, bar,
                                    fire_idx=len(self.fires) - 1)

        self.prev_in_night = in_night
        self.prev_in_range = in_range
        self.prev_bar_open = t

    def on_fill(self, fill):
        if not fill.order.is_exit and self._pending_stop_px is not None:
            self.move_stop(self._pending_stop_px)
            self._pending_stop_px = None

    # ---- volume profile (mirrors FindPOC / ComputeValueArea) ----
    def _find_poc(self) -> float:
        vap = self.vol_at_price
        if not vap:
            return float("nan")
        max_vol = max(vap.values())
        mid = (self.ny_high + self.ny_low) / 2.0
        poc, best_dist = float("nan"), float("inf")
        for price, vol in vap.items():
            if vol != max_vol:
                continue
            dist = abs(price - mid)
            if dist < best_dist:
                best_dist, poc = dist, price
        return poc

    def _value_area(self, poc: float) -> tuple[float, float]:
        vap = self.vol_at_price
        if not vap or poc != poc:  # NaN check
            return poc, poc
        bins = sorted(vap.keys())
        try:
            poc_idx = bins.index(poc)
        except ValueError:
            return poc, poc
        total = sum(vap.values())
        target = total * self.value_area_pct
        lo_idx = hi_idx = poc_idx
        acc = vap[bins[poc_idx]]
        while acc < target and (lo_idx > 0 or hi_idx < len(bins) - 1):
            below = vap[bins[lo_idx - 1]] if lo_idx > 0 else -1
            above = vap[bins[hi_idx + 1]] if hi_idx < len(bins) - 1 else -1
            if above >= below:
                hi_idx += 1
                acc += above
            else:
                lo_idx -= 1
                acc += below
        return bins[hi_idx], bins[lo_idx]   # (vah, val)

    # ---- FVG engine (mirrors RunFvgEngine) ----
    def _run_fvg_engine(self, bar, bars, atrv, va_live, rng_live, trade_live):
        sig_dir, sig_gtop, sig_gbot, sig_name = 0, 0.0, 0.0, ""
        bi = bar.index
        arm_ok = (not self.fvg_after_start) or trade_live

        lvl_names: list[str] = []
        lvl_prices: list[float] = []
        if self.fvg_on_va and va_live:
            for nm, px in (("NY POC", self.a_poc), ("NY VAH", self.a_vah), ("NY VAL", self.a_val)):
                if px == px:  # not NaN
                    lvl_names.append(nm)
                    lvl_prices.append(px)
        if self.fvg_on_range and rng_live and self.use_range_high and self.r_hi == self.r_hi:
            lvl_names.append("Range High")
            lvl_prices.append(self.r_hi)
        if self.fvg_on_range and rng_live and self.use_range_low and self.r_lo == self.r_lo:
            lvl_names.append("Range Low")
            lvl_prices.append(self.r_lo)

        # ── STAGE 1: register touches, then prune expired ones ──
        ftol = atrv * self.engine_tol_atr
        if arm_ok:
            for nm, px in zip(lvl_names, lvl_prices):
                if bar.low - ftol <= px <= bar.high + ftol:
                    prev = self.last_touch_bar.get(nm, -999_999)
                    if bi - prev >= self.touch_cooldown:
                        self.last_touch_bar[nm] = bi
                        self.touches.append(_Touch(nm, px, bi))
        self.touches = [tc for tc in self.touches if bi - tc.bar <= self.max_lag]

        # ── STAGE 2: FVG on the 3-bar window ending on this bar ──
        # bar = "this" (C# [1]); bars[-2] = C# [2]; bars[-3] = C# [3]
        o2, c2 = bars.open.values[bi - 1], bars.close.values[bi - 1]
        h3, l3 = bars.high.values[bi - 2], bars.low.values[bi - 2]
        disp = abs(c2 - o2)
        bull = (bar.low > h3 and (bar.low - h3) >= atrv * self.min_gap_atr
                and disp >= atrv * self.min_disp_atr and c2 > o2)
        bear = (bar.high < l3 and (l3 - bar.high) >= atrv * self.min_gap_atr
                and disp >= atrv * self.min_disp_atr and c2 < o2)

        if (bull or bear) and self.touches:
            direction = 1 if bull else -1
            gbot = h3 if bull else bar.high
            gtop = bar.low if bull else l3

            best = None
            for tc in reversed(self.touches):
                lag = bi - tc.bar
                ok_lag = self.min_lag <= lag <= self.max_lag
                ok_rej = (not self.need_reject) or (
                    bar.close > tc.price if direction == 1 else bar.close < tc.price)
                if ok_lag and ok_rej:
                    best = tc
                    break

            if best is not None:
                cp = (gtop if direction == 1 else gbot) if self.confirm_mode == "gap" \
                    else (bar.high if direction == 1 else bar.low)
                su = _Setup(name=best.name, dir=direction, gtop=gtop, gbot=gbot,
                            confp=cp, touch_price=best.price, fvg_bar=bi, fill_bar=0,
                            st=0, gap_atr=(gtop - gbot) / atrv, disp_atr=disp / atrv,
                            lag=bi - best.bar)
                self.setups.append(su)
                self.touches.remove(best)

        # ── STAGES 3 & 4: manage open setups ──
        inv_b = atrv * self.invalidation_buf
        for su in list(self.setups):
            mid = (su.gtop + su.gbot) / 2.0
            dead = False

            if self.fill_mode == "far":
                fill_p = su.gbot if su.dir == 1 else su.gtop
            elif self.fill_mode == "mid":
                fill_p = mid
            else:
                fill_p = su.gtop if su.dir == 1 else su.gbot

            blown = (bar.close < su.gbot - inv_b) if su.dir == 1 else (bar.close > su.gtop + inv_b)

            if su.st == 0:
                filled = (bar.low <= fill_p) if su.dir == 1 else (bar.high >= fill_p)
                if filled:
                    su.st, su.fill_bar = 1, bi
                elif blown or (bi - su.fvg_bar > self.max_fill_bars):
                    dead = True

            if su.st == 1 and not dead:
                conf = (bar.close > su.confp) if su.dir == 1 else (bar.close < su.confp)
                last_day = self.last_sig_day.get(su.name)
                ok = (not self.one_signal_per_day) or last_day is None or last_day != self.day_count

                if conf and ok:
                    dead = True
                    self.last_sig_day[su.name] = self.day_count
                    sig_dir, sig_gtop, sig_gbot, sig_name = su.dir, su.gtop, su.gbot, su.name
                elif conf and not ok:
                    dead = True
                elif blown or (bi - su.fill_bar > self.max_confirm_bars):
                    dead = True

            if dead:
                self.setups.remove(su)

        return sig_dir, sig_gtop, sig_gbot, sig_name

    # ---- trading (new vs. the indicator) ----
    def _try_enter(self, direction, gtop, gbot, bar, fire_idx=None):
        if not self.flat:
            return
        if direction > 0 and not self.take_longs:
            return
        if direction < 0 and not self.take_shorts:
            return

        tick = self._tick
        buffer = self.stop_buffer_ticks * tick
        ref = bar.close
        if direction > 0:
            stop_px = gbot - buffer
            risk = ref - stop_px
        else:
            stop_px = gtop + buffer
            risk = stop_px - ref
        if risk <= 0:
            return   # malformed gap safety net; never submit a non-positive-risk bracket

        est_stop_ticks = max(1, round(risk / tick))
        target_ticks = max(1, round(risk * self.target_r_multiple / tick))
        tag = "td-long" if direction > 0 else "td-short"
        if fire_idx is not None:
            tag += f"#{fire_idx}"   # joins a Trade back to its fires[] row (regime diagnostics)

        if direction > 0:
            order = self.buy_bracket(stop_ticks=est_stop_ticks, target_ticks=target_ticks, tag=tag)
        else:
            order = self.sell_bracket(stop_ticks=est_stop_ticks, target_ticks=target_ticks, tag=tag)
        if order is not None:
            self._pending_stop_px = stop_px
