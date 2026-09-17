"""gbPullback5Bar — 5-bar reversal/pullback continuation.

Port of `~/dev/NinjaScript/gpPullBack/gbPullback5Bar.cs` (GreyBeard,
v1.0.0-Beta; "original implementation, not derived from any third-party
source" per its own header).

**FIRST POSITIVE, WALK-FORWARD-CONFIRMED RESULT, 2026-09-15 — promising but
NOT yet live-ready** (needs NT8 chart parity + paper trading before real
size). Dre traded this live on ninZaRenko r64-16, RTH 09:30-16:00 ET
(corrected from an earlier wrong guess of SaberRenko 64/16, which came from
a mismatched leftover XML — see below). At Dre's own r64-16, MNQ, $2k floor,
full history 2024-12-16..2026-08-07 (541 days): net **-$1,347**, Sharpe
-0.32, BREACHED the floor 2025-03-04 (MC P(breach) 88.1%) — a loser, but
much closer to breakeven than the first (wrong-bar-type) guess. User asked
to iterate over bar type/size, stop/target, session window, and instrument —
full logs/CSVs in `reports/gb_pullback_5bar_sweeps/`. Summary, in order:

1. **Bar size** (ninZaRenko, Dre's 4:1 trend:brick ratio held fixed, swept
   32-8 through 256-64): strictly monotonic improvement from r32-8 (net
   -$28,231, Sharpe -3.94) up through a clean 6-point PLATEAU at r88-22
   through r136-34 (all net positive, Sharpe 0.5-1.7, not a single spike),
   then noisy/thinning-sample bouncing beyond r144 (few hundred trades,
   high variance). **r112-28** is the plateau's standout: net $4,845,
   Sharpe 1.66, PF 1.11, 2,632 trades, prop headroom +$1,156 (best of the
   whole grid at the time).
2. **Bar type**, size-matched to r112-28 (workers capped low — a 9-way,
   full-history, all-different-bar-type sweep OOM'd this box once):
   SaberRenko s112-28 barely positive (Sharpe 0.07), TBars tb56 and Wave
   w112 both solidly positive (0.73, 1.13) but below renko; **time and tick
   bars are catastrophic and monotonically worse with higher frequency**
   (5m Sharpe -0.47 down to 500t Sharpe -13.40) — confirms this repo's
   general finding that renko-family bars are an implicit chop filter this
   signal depends on. ninZaRenko stays the pick.
3. **Stop/target grid** at r112-28 (stop_offset_ticks x profit_target_ticks,
   30 combos): stop_offset has a genuine plateau (1/2/4/8 all Sharpe
   ~1.9-2.0 by Sharpe-ranking at target=160; Dre's own live value of 2 sits
   right on it). profit_target looked monotonically better up to 160
   (Sharpe 1.99, headroom +$892) — but extending further (200-400) mostly
   BREACHES despite fine Sharpe (the same Sharpe-vs-headroom trap flagged
   elsewhere in CLAUDE.md), recovering only at target=600 on a thin,
   low-win-rate sample. Picking by Sharpe alone would have shipped a
   breaching config.
4. **Walk-forward** (5 windows, ratio 5, grid profit_target_ticks in
   {50,80,120,160,200} x stop_offset_ticks in {1,2,4}, r112-28 fixed):
   **converged on {target=50, stop=1} in all 5 IS windows** (real
   parameter convergence, not noise) and went 4/5 profitable OOS. Stitched
   OOS net $1,837/216 days, Sharpe 1.13, **WFE 0.85 ("OK — edge survives
   OOS")**. This is the config actually validated OOS — not the target=160
   full-history-Sharpe pick above, which was never checked out of sample
   and is exactly the single-shot-sweep trap this repo's own methodology
   warns against (see the YM Three Amigos section of CLAUDE.md). Handily,
   {target=50, stop=1} was ALSO the best full-history prop-headroom point
   in the whole stop/target grid (+$1,163) — the walk-forward pick and the
   floor-safety pick are the same config, not competing.
   **Shipped defaults below are r112-28 / stop_offset_ticks=1 /
   profit_target_ticks=50 / Dre's own 09:30-16:00 ET session.**
   Full-history headline at these defaults: net **$4,988.72**, 2,632
   trades, WR 79.9%, PF 1.11, Sharpe 1.71, Sortino 2.16, Calmar 2.91,
   maxDD -$1,016 (-1.82%), **survived the $2k floor with $1,162.92
   headroom**, MC (2000 sims) P(breach)=11.2%, P(profit)=98%. Caveat:
   20.3% of trades close in <10s, worth +$11,041 (a real contribution, not
   an artifact — the crossed-quote fantasy-fill bug is already fixed
   repo-wide — but expected given renko's threshold-triggered bars putting
   price close to target the instant a new bar forms; worth keeping an eye
   on).
5. **Session window** (7 windows tried at r112-28/stop1/target160 — note:
   ticks=160 here, run before the walk-forward stage picked target=50, so
   these numbers aren't at the final defaults): EVERY window tested was
   net positive and floor-safe, including the weakest (midday 11:30-13:30,
   Sharpe 0.18, headroom only +$100). Dre's actual window (09:30-16:00,
   Sharpe 1.99) is mid-pack; the .cs's own original default (09:30-16:45,
   Sharpe 2.14) and a morning-only window (09:30-11:30, Sharpe 2.20, best
   of the seven) both edge it out but are UNVALIDATED (no walk-forward run
   on session choice) — plausible next step if pursued further.
6. **Other instruments**, first at r112-28 unscaled: MNQ dominant (Sharpe
   1.99, 2,632 trades); MYM/MES/YM only produced 192/30/189 trades over
   541 days — too thin to judge, and a sign the tick brick wasn't
   volatility-calibrated. Re-tried dollar-scaled to MNQ's brick/target
   ($56/$80, e.g. MES r45-11 target64, MGC r56-14 target80, YM r11-3
   target16): **all three failed outright** — MES net -$1,423 (breach),
   MGC barely +$599 but still breaches (-$390 headroom), YM catastrophic
   -$145,001 (a too-small brick for full-size Dow massively overtrades,
   the same time/tick-bar chop-overtrading pattern as point 2). **This
   edge looks MNQ-specific**, not a generic bar-type effect that transfers
   with simple recalibration.

**Bottom line: MNQ / r112-28 / stop1 / target50 / RTH 09:30-16:00 ET is the
first genuinely promising, walk-forward-checked config for this strategy —
but it has NOT had an NT8 chart-parity check on this specific renko size,
nor a live/paper-trading trial, and the session-window improvements above
are still unvalidated upside. Treat as a strong candidate, not a champion.**

**OPEN BUG, found 2026-09-16, NOT YET ROOT-CAUSED: our ninZaRenko builder
diverges from a real NT8 Market Replay at r112-28 partway through a
multi-day run.** User ran gbPullback5Bar live in NT8 Market Replay
(ninZaRenko Brick=112/Reversal=28, confirmed by the user — NOT a guess),
MNQ 09-26, 2026-08-31..2026-09-11, exported the executions grid (42 round
trips). Reconstructed round trips confirm `profit_target_ticks=50` exactly
(35/36 profit-target exits at precisely 50 ticks) and that the live .cs
does NOT flatten at session end (one trade rode 15:55->17:56 ET past the
16:00 close) — so `flat_at_session_end=True` here is a confirmed,
deliberate divergence from live, not just a documented one.
Comparing our own r112-28 backtest (same window, `flat_at_session_end`
overridden False to match) against the real trades: **the first 11 real
trades match PERFECTLY** (entry price/time near-exact, 8/31 through
2026-09-02 15:49 ET) **then EVERY subsequent real trade (9/3 through 9/11,
31 trades) misses entirely.** This is not gradual drift or generic renko
size-sensitivity (an earlier note here wrongly concluded r111-28 was the
"real" live config at 95% match — WRONG, retracted: that was a
coincidental compensating path through the state machine, not evidence of
an off-by-one in the brick/trend-to-price conversion, which was checked
and looks correct: `spec.brick_ticks * tick_size` / `spec.trend_ticks *
tick_size` in `Catalog._bars_for_day`, feeding `build_renko_bars`
straightforwardly, and MNQ's tick_size=0.25 is exactly representable in
float64 so no accumulation drift is expected there either). This is a
**single cascading divergence point**: renko is a state machine (each
bar's anchor depends on the previous bar's close), so ONE bar forming
differently from NT8 near the 9/2evening->9/3 boundary throws off every
subsequent bar for the rest of the run, with no in-session mechanism to
resync. Ordinary daily resets on either side of it (8/31->9/1, 9/1->9/2)
matched fine, so the general gap-reset logic in `build_renko_bars` isn't
obviously broken either — the divergence looks localized to something
specific about that one boundary.
**Root cause NOT YET FOUND.** This needs a genuine NT8 ninZaRenko bar/
chart export (Brick=112, Reversal=28, MNQ, spanning at least
2026-09-02 12:00 ET through 2026-09-03 10:00 ET) diffed bar-by-bar against
`build_renko_bars` output — the same methodology already used to validate
the five ninZaRenko settings in CLAUDE.md's ninZaRenko section (10/3, 36/2,
40/10, 64/16, 100/4), none of which include 112/28. Until that's done,
**do not trust this backtester's ninZaRenko output at r112-28 (or nearby
untested sizes) as faithful to real NT8 bars** — the bar-size sweep and
walk-forward above are still internally-consistent Python-vs-Python
results, but their absolute numbers may not transfer to live trading if
this bug is present at other sizes too, which is unknown.

Signal (bar-direction state machine, no indicators): track the direction
(sign of close-open) of the last nonzero-direction bar. When a bar's
direction flips against that ("the pullback bar"), arm a pending trend in
the PRIOR direction and remember the pullback bar's open/high/low. Within
`pullback_bar_limit` bars (counting the pullback bar as bar 1 — so
increment-then-compare, matching the .cs's exact ordering: the pullback bar
itself never confirms, bars 2-5 get the confirmation check, bar 6 cancels),
if a later bar's close crosses back through the pullback bar's OPEN in the
armed direction, enter with the trend. Stop is an ABSOLUTE price beyond the
pullback bar's own high/low by `stop_offset_ticks` (not a tick offset from
the entry fill); target is a fixed `profit_target_ticks`.

The .cs has no bar type baked in (any chart bar series). No NT8 export/
template exists FOR THIS STRATEGY to derive one from: the only file found
alongside it, `V4 _ Built in Pullback 5Bar_NQ _SaberRenko64_16.xml`, is a
mismatched leftover — its `<StrategyType>` is
`NinjaTrader.NinjaScript.Strategies.TradeSaberPredator.PredatorXOrderEntryLT`,
an unrelated third-party strategy, not gbPullback5Bar; its filename's
"SaberRenko" was a red herring (user-confirmed 2026-09-15: Dre actually
traded ninZaRenko r64-16, not SaberRenko). `symbol` deviates from Dre's
full-size NQ to MNQ per CLAUDE.md (a full-size NQ contract alone overshoots
the $2k prop floor) — re-tested at full size in the instrument sweep below.

`PullbackBarLimit=5` and `TakeLongs/TakeShorts=true` are straight from
`SetDefaults` and unchanged; `StopOffsetTicks`/`ProfitTargetTicks`/session
below are the walk-forward-picked values from the sweep above, not the
.cs's raw SetDefaults (which were 2/50/09:30-16:45 — coincidentally
StopOffsetTicks=2 sits on the same plateau as the picked 1, so that one
barely moved). `flat_at_session_end=True` is a deliberate divergence —
the .cs has `IsExitOnSessionCloseStrategy=false` (no auto-flatten) and
relies on a human closing the dashboard's manual FLATTEN button; this repo
has no such button, so flatten-at-session-end is the safe substitute.
DailyProfitTarget/DailyMaxLoss (both 0=off by default) map to the engine's
own `daily_loss_limit`/`daily_profit_target` (also off by default) rather
than a hand-rolled copy — note the semantics differ slightly: the .cs only
blocks NEW entries once hit, this engine flattens and stands down for the
rest of the day. Moot at the shipped (off) defaults.

Dropped entirely (live-trading-only, no backtest meaning): the on-chart
dashboard, manual REV/BE/FLATTEN/nudge buttons, `_autoEnabled` /
`_longEnabled` / `_shortEnabled` toggles.

Stop-price mechanics: since the stop is anchored to the pullback bar's
high/low rather than the entry fill price, `buy_bracket`/`sell_bracket`
(tick-offsets-from-fill only) can't place it directly. Entry uses a bracket
with an ESTIMATED stop_ticks (computed off `bar.close`, so the bracket
child is submitted synchronously — no bar is ever naked) plus the exact
`target_ticks`; `on_fill` (which fires synchronously right after the
broker creates those bracket children, still inside the same span) then
snaps the stop to the exact absolute price via `move_stop`. `broker.modify`
applies from the next evaluated event, so the correction is live for the
rest of the entry bar.
"""
from backtester import Strategy


class GbPullback5Bar(Strategy):
    symbol = "MNQ"
    period = "r112-28"                 # ninZaRenko brick 112 / trend 28 — see sweep below (Dre traded r64-16 live)
    session = ("09:30", "16:00")       # RTH 9:30am-4pm ET — Dre's actual live config
    flat_at_session_end = True         # deliberate divergence — see docstring
    qty = 1

    pullback_bar_limit = 5
    stop_offset_ticks = 1               # walk-forward-picked, see sweep below (Dre traded 2 live; plateau covers both)
    profit_target_ticks = 50
    take_longs = True
    take_shorts = True
    bars_required_to_trade = 5         # NT8 CurrentBar < BarsRequiredToTrade

    daily_loss_limit = None            # None = off; maps to DailyMaxLoss=0
    daily_profit_target = None         # None = off; maps to DailyProfitTarget=0

    def on_start(self):
        self.pending_trend = 0         # +1 (armed long) / -1 (armed short) / 0
        self.bars_since_pullback = 0
        self.pullback_open = 0.0
        self.pullback_high = 0.0
        self.pullback_low = 0.0
        self.last_bar_dir = 0
        self._pending_stop_px = None   # exact stop price awaiting on_fill snap

    def on_session_end(self, date):
        # mirrors NT8's Bars.IsFirstBarOfSession reset (see .cs OnBarUpdate)
        self.pending_trend = 0
        self.last_bar_dir = 0
        self._pending_stop_px = None

    def on_bar(self, bar, bars):
        if bar.index < self.bars_required_to_trade:
            return

        current_dir = (bar.close > bar.open) - (bar.close < bar.open)

        # 1) age / confirm an already-armed pullback using the bar that just closed
        if self.pending_trend != 0:
            self.bars_since_pullback += 1
            if self.bars_since_pullback > self.pullback_bar_limit:
                self.pending_trend = 0
            elif self.flat:
                confirmed_long = (self.pending_trend > 0
                                   and bar.close > self.pullback_open)
                confirmed_short = (self.pending_trend < 0
                                    and bar.close < self.pullback_open)
                if confirmed_long and self.take_longs:
                    self._enter_pullback(1, bar)
                    self.pending_trend = 0
                elif confirmed_short and self.take_shorts:
                    self._enter_pullback(-1, bar)
                    self.pending_trend = 0

        # 2) look for a brand-new pullback bar: direction flips against the
        #    previously established nonzero bar direction
        if (self.pending_trend == 0 and current_dir != 0
                and self.last_bar_dir != 0 and current_dir != self.last_bar_dir):
            self.pending_trend = self.last_bar_dir
            self.pullback_open = bar.open
            self.pullback_high = bar.high
            self.pullback_low = bar.low
            self.bars_since_pullback = 1

        if current_dir != 0:
            self.last_bar_dir = current_dir

    def _enter_pullback(self, direction, bar):
        tick = self._broker.spec.tick_size
        if direction > 0:
            stop_px = self.pullback_low - self.stop_offset_ticks * tick
        else:
            stop_px = self.pullback_high + self.stop_offset_ticks * tick
        est_stop_ticks = max(1, round(abs(bar.close - stop_px) / tick))
        tag = "pullback-long" if direction > 0 else "pullback-short"

        if direction > 0:
            order = self.buy_bracket(stop_ticks=est_stop_ticks,
                                     target_ticks=self.profit_target_ticks,
                                     tag=tag)
        else:
            order = self.sell_bracket(stop_ticks=est_stop_ticks,
                                      target_ticks=self.profit_target_ticks,
                                      tag=tag)
        if order is not None:
            self._pending_stop_px = stop_px

    def on_fill(self, fill):
        if not fill.order.is_exit and self._pending_stop_px is not None:
            self.move_stop(self._pending_stop_px)
            self._pending_stop_px = None
