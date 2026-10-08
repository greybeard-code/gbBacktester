"""BandFade — Bollinger-band mean reversion for SIDEWAYS regimes.

Designed as a COMPLEMENT to the Terminator SAR champion, not a replacement:
Terminator is a trend-following stop-and-reverse that structurally gives back
money in chop, so this trades the regime Terminator sits out.

**Why time bars, not renko** (measured 2026-08-06, MNQ July 2026): ER(30) over
r100-4 ninZaRenko closes has median 1.000 and only 9.7% of bars below 0.2 —
renko only prints when price MOVES, so sideways action emits no bricks and a
pure with-trend run is ER=1.0 by definition. Renko is already a chop filter by
construction, which makes it the WRONG bar type for a strategy whose whole
premise is trading chop. The same ER on 1m time bars has median 0.168 (58%
below 0.2) — the regime is actually visible there. Hence a time period here.

Signal (all thresholds sweepable, nothing hand-picked yet):
  - Bollinger(bb_period, bb_mult) on closes. Long when close pierces the LOWER
    band, short when it pierces the UPPER band — fading the excursion.
  - RSI(rsi_period) confirmation: long needs RSI <= rsi_os, short RSI >= rsi_ob.
    Filters "band pierce that is really a breakout" from "band pierce that is
    exhaustion".
  - Regime gate, the INVERSE of the Terminator chop filter: only enter while
    ER(er_period) <= er_max, i.e. only when price is actually chopping. A
    band fade in a strong trend is how mean reversion strategies die.
  - Exit: hard bracket (ATR-scaled stop / target), plus an optional
    take-profit when price reverts through the middle band (exit_on_mid),
    which is the actual mean-reversion thesis completing.

Prop-firm posture matches the rest of the repo: full Globex trading day
session with ONE flatten before the 17:00 ET halt, and min_hold_s left at 0
here (set it to 30 to price in the Apex minimum-hold rule — see CLAUDE.md).

**FAILED VALIDATION 2026-08-06 — recorded negative result, do not deploy.**
Defaults over 510 days (2024-12-16..2026-07-31, MNQ 5m): net **-$6,037**,
PF 0.89, Sharpe -1.12, maxDD -$7,697, BREACHED the $2k floor, MC P(breach)
99.2% / P(profit) 5%. A 28-combo in-sample sweep over exit_on_mid /
target_atr / er_max / bb_mult found NO viable region: the best combo
(exit_on_mid=False, target_atr=4.0, er_max=0.20, bb_mult=2.0) is net +$848,
Sharpe 0.24, PF 1.05 — breakeven-with-noise, still negative prop headroom,
and best-of-28 on in-sample data is selection bias, not an edge. Every
sensitivity axis flagged FRAGILE. It was not walk-forwarded; there was
nothing worth walking forward.

Two things here ARE worth keeping:
  1. `exit_on_mid=True` is genuinely harmful (-$1,547 vs +$848 at
     target_atr=4.0) — reverting to the mid-band caps winners at roughly
     half the band while stops still pay full freight, so the payoff
     asymmetry inverts against a ~50% win rate. If mean reversion is
     retried here, do NOT exit at the mean.
  2. The tighter the chop gate, the better it does (er_max 0.20 >> 0.35 >>
     0.50 at every other setting) — consistent with the premise being
     right (fade only in chop) even though this implementation of it
     loses. A future attempt should gate HARDER, not looser, and probably
     needs a real edge beyond band+RSI (e.g. the order-flow absorption
     idea in the roadmap) rather than another parameter hunt.
"""
from backtester import ATR, Bollinger, EfficiencyRatio, RSI, Strategy


class BandFade(Strategy):
    symbol = "MNQ"
    period = "5m"                      # time bars: chop must be VISIBLE (see docstring)
    session = ("18:00", "16:55")       # one Globex trading day, flat before the halt
    flat_at_session_end = True
    qty = 1

    bb_period = 20
    bb_mult = 2.0
    rsi_period = 14
    rsi_os = 30.0                      # long confirmation ceiling
    rsi_ob = 70.0                      # short confirmation floor
    er_period = 30
    er_max = 0.35                      # only trade while ER <= this (chop regime)

    atr_period = 14
    stop_atr = 2.0                     # hard stop = N x ATR at entry
    target_atr = 2.0                   # hard target = N x ATR at entry
    exit_on_mid = True                 # also take profit on reversion to mid-band
    cooldown_bars = 3                  # bars to wait after an entry before re-entering

    def on_start(self):
        self.bb = Bollinger(self.bb_period, self.bb_mult)
        self.rsi = RSI(self.rsi_period)
        self.er = EfficiencyRatio(self.er_period)
        self.atr = ATR(self.atr_period)
        self.last_entry_bar = -1

    def _atr_ticks(self, mult):
        tick = self._broker.spec.tick_size
        return max(1, round(mult * self.atr.value / tick))

    def on_bar(self, bar, bars):
        c = bar.close
        self.bb.update(c)
        self.rsi.update(c)
        self.er.update(c)
        self.atr.update(bar.high, bar.low, c)

        if not (self.bb.ready and self.rsi.ready
                and self.er.ready and self.atr.ready):
            return

        # --- manage an open position: mean reversion completed at the mid-band ---
        if self.position != 0:
            if self.exit_on_mid and self.hold_ok():
                reverted = (self.position > 0 and c >= self.bb.middle) or \
                           (self.position < 0 and c <= self.bb.middle)
                if reverted:
                    self.cancel_all()
                    self.close_position(tag="mid")
            return                     # never stack entries on an open position

        # --- entries: chop regime only, band pierce + RSI exhaustion ---
        if self.er.value > self.er_max:
            return                     # trending — a fade here is how this dies
        if (self.cooldown_bars > 0 and self.last_entry_bar >= 0
                and bar.index - self.last_entry_bar < self.cooldown_bars):
            return

        kw = {"stop_ticks": self._atr_ticks(self.stop_atr),
              "target_ticks": self._atr_ticks(self.target_atr)}
        if c <= self.bb.lower and self.rsi.value <= self.rsi_os:
            self.buy_bracket(tag="fade-long", **kw)
            self.last_entry_bar = bar.index
        elif c >= self.bb.upper and self.rsi.value >= self.rsi_ob:
            self.sell_bracket(tag="fade-short", **kw)
            self.last_entry_bar = bar.index
