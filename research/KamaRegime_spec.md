# KamaRegimePro — Specification and Backtester Port

Status: **ported 2026-08-07**, off by default, and **measured as unhelpful to
GodZillaKilla — do not enable it** (§7.2: it costs money on the validated MNQ
evening champion at every setting tested, and the flip-pulse variant
collapses to the same thing on renko). Kept as a certified NT8 KAMA that
other strategies can use, and as a recorded negative result. Python:
`backtester/indicators.py::KAMA` + `::KamaRegime`, strategy flags on
`strategies/godzilla_killa.py`, tests in `tests/test_kama.py`. The KAMA
recursion is certified bit-identical to NT8's `@KAMA.cs`; the regime state
machine is a faithful reading of the source but has **not** been gated
against an NT8 chart export (§8).

## 0. Provenance and why

`nt8 code/GodZillaKilla/indicators/kamareginepro.cs` — a GreyBeard-community
indicator, supplied alongside a YM 09-26 / ninZaRenko 33-4 chart running
GodZuki v1.4.1 Beta with "Set1 R:3/4: KO, +PA, +TH, +SJ". Motivating quote
(S, Discord): *"th pa sj ym 33/4 with a kama regime is spot on … the kama
regime might be something to look at as a filter"*.

So the ask is specifically the **filter** use: let the KAMA regime decide
which direction GodZillaKilla is allowed to take. That is what is wired up
here; the indicator's chart furniture is not (§4).

## 1. What it is

One trend source, three layers:

```
NT8 KAMA(fast, period, slow)  ->  slope in ticks/bar  ->  sticky flat band
                              ->  confirm-bars debounce  ->  regime +-1
```

| Property | Default | Role |
|---|---|---|
| `KamaFast` | 2 | KAMA fast constant |
| `KamaPeriod` | 10 | efficiency-ratio lookback — NT8's **middle** arg |
| `KamaSlow` | 30 | KAMA slow constant |
| `KamaFlatThreshold` | 0.5 | flat band, **ticks per bar** (§5) |
| `ConfirmBars` | 1 | bars a raw signal must persist before committing |
| `SignalOnChangeOnly` | true | emit only on the flip bar vs. every bar |
| `StartLabelBars` | 3 | "…START" vs "…TREND" card label — cosmetic |

`Calculate = OnBarClose`, so the regime commits on the close of the bar and
never repaints intrabar — it lines up exactly with this engine's `on_bar`.

The signal is a `Series<double>`, deliberately **not** an `AddPlot`, so the
±1 values stay off the price-panel auto-scale. Strategies read it through the
`RegimeSignal` accessor (`Regime` is a legacy alias). That is a NinjaScript
plumbing detail with no analogue here.

## 2. The KAMA underneath

Ported bar-for-bar from `@KAMA.cs` (NinjaTrader 8, 2025 — on disk at
`Documents\NinjaTrader 8\bin\Custom\Indicators\@KAMA.cs`), so this is vendor
behaviour, not a reconstruction of Kaufman's paper:

```
diff[b]  = |Input[b] - Input[b-1]|            (b > 0)
noise[b] = SUM(diff, Period)[b]
signal[b]= |Input[b] - Input[b-Period]|
sc       = (signal/noise * (fastCf - slowCf) + slowCf) ** 2
Value[b] = Value[b-1] + sc * (Input[b] - Value[b-1])
```

with `fastCf = 2/(Fast+1)`, `slowCf = 2/(Slow+1)`. `signal/noise` is
Kaufman's Efficiency Ratio, so `sc` ranges from `slowCf**2` (pure chop,
0.00416 at Slow=30) to `fastCf**2` (pure trend, **0.4444** at Fast=2). The
adaptivity is a 100x swing in the smoothing constant.

Three NT8 details the port reproduces deliberately:

**(a) Warmup is the raw price.** `if (CurrentBar < Period) Value[0] =
Input[0]` — for the first `Period` bars KAMA *is* the close, and the
recursion is seeded off that, not off an SMA. The repo's own `EMA` seeds with
an SMA instead; that difference is exactly why `gbsignals/nt8math.py` exists,
and `KAMA` here follows the NT8 side.

**(b) Bar 0's "diff" is the price, not zero.** `@KAMA.cs:56` reads
`diffSeries[0] = CurrentBar > 0 ? Math.Abs(input0 - Input[1]) : input0`. At
bar 0 that stores e.g. 42000.0 into the noise series. It looks like a bug and
is harmless: the value sits in the rolling window only for bars `0..Period-1`
— every one of which returns from the seeding branch without reading `noise`
— and is subtracted out exactly at bar `Period`, the first bar that publishes
an adaptive value. It is carried here anyway because `SUM` is a **running**
sum (`Value[0] = Input[0] + Value[1] - Input[Period]`, `@SUM.cs:31`), so the
subtraction has to cancel the same double that was added. Verified: shifting
the whole series by +9900 leaves the output shape unchanged
(`test_bar0_diff_is_the_price_and_never_reaches_a_published_value`).

**(c) Zero noise freezes the line.** `if (noise == 0) Value[0] = Value[1]` —
a fully flat window holds the previous value rather than converging to price,
so slope is *exactly* 0.0 there. On renko this happens more than one might
expect, because a bar only prints when price moves.

`Math.Pow(x, 2)` is implemented as `x * x`; both are a single rounding of the
exact square, so this is not a divergence.

**Certification.** `KAMA` was checked against a literal array transcription of
`@KAMA.cs` + `@SUM.cs` over a tick-grid random walk (601 bars), a flat series
that exercises the zero-noise branch, a pure trend, a 2001-bar integer-grid
walk, and non-default `fast=5/period=21/slow=50`: **bit-identical doubles,
zero mismatches on every path.**

## 3. The regime state machine

Per bar, after `CurrentBar >= KamaPeriod + KamaSlow + 5` (45 bars by default;
before that the indicator emits nothing and resets its series):

```
slope = (KAMA[0] - KAMA[1]) / TickSize                    # ticks per bar

raw = sign(slope)                     if regime == 0      # FIRST commit only
raw = +1 if slope >  +FlatThreshold   else
      -1 if slope <  -FlatThreshold   else regime         # sticky otherwise

pendingCount = (raw == pending) ? pendingCount+1 : 1
if raw == regime:                 barsInRegime += 1
elif pendingCount >= ConfirmBars: regime = raw; barsInRegime = 1

flipped = (regime != lastCommitted) and regime != 0
```

Five behaviours worth naming, all reproduced:

1. **The first commit ignores the flat band.** `regimeState == 0` takes
   `slope >= 0 ? 1 : -1` — sign only, ties go bull. A band wide enough that
   nothing ever crosses it therefore does not disable the indicator; it pins
   it to whatever the first post-warmup bar happened to do, forever.
2. **`ConfirmBars` gates the first commit too.** With `ConfirmBars=3` the
   opening regime lands 2 bars later than with `ConfirmBars=1`.
3. **There is no neutral state after warmup.** The band is *sticky*, not a
   dead zone: inside it the previous direction is held. Once committed the
   regime is always ±1. As a filter this means it always permits exactly one
   direction and blocks the other — it is a **directional bias filter, not a
   chop filter**. It can never say "stand aside".
4. **`barsInRegime` undercounts.** It only increments on bars where
   `raw == regime`, so bars spent waiting out a `ConfirmBars` debounce are
   not counted in either regime. Cosmetic (it drives the …START/…TREND
   label), reproduced for faithfulness.
5. **`flipped` is the `RegimeSignal` pulse** — true only on the bar a new
   regime commits, which is also the bar that gets the ★ and the background
   colour change.

## 4. What is not ported

Everything below the state machine in the `.cs` is chart furniture with no
backtest meaning: `BackBrush` painting, the ★ `Draw.Text` marker, the WPF
readout card (build/inject/drag/flash/remove), the `DispatcherTimer`, the
`disposed` lifecycle guard and `IsSuspendedWhileInactive = false`. Those
exist to stop NT8 tearing down a live instance while a UI timer still fires
("Non-static method requires a target"); there is no UI thread here.

`FormatPx`, `BuildOpacityBrush`, `Frozen`/`Tint` and the corner-placement
maths are likewise display-only.

## 5. Calibration — the band is in ticks/bar, and that does not travel

`KamaFlatThreshold` is compared against a **tick-per-bar** slope, so its
meaning depends on both instrument tick size *and bar type*. The source
comment calls 0.5 a filter that "filters noise-flips without missing real
turns on most 5m futures". Measured on YM, 2026-06-01..07-17 (41 day files),
KAMA(2,10,30) slope past warmup:

| bars | count | per day | median \|slope\| | p90 | inside ±0.5 | inside ±2.0 |
|---|---|---|---|---|---|---|
| **r33-4 renko** | 25,988 | 634 | **4.047 t/bar** | 10.169 | **5.4%** | 10.6% |
| 5m time | 9,482 | 231 | 1.146 | 11.096 | 35.8% | 60.4% |
| 1m time | 46,556 | 1,136 | 0.521 | 4.739 | 49.3% | 75.5% |

The 5m column is the calibration the source comment describes — the band
covers about a third of bars, which is what "filter noise-flips" means. **On
r33-4 renko it covers 5.4%, so the band is very nearly inert and the regime
degenerates to `sign(KAMA slope)`.**

That is structural, not a quirk of this sample. On a with-trend renko run
every bar closes exactly `trend` ticks from the previous close, and at ER≈1
the smoothing constant pins to `fastCf**2`, whose steady state advances the
KAMA by exactly the per-bar step. So on r33-4 the trend slope converges to
**4 ticks/bar** — the measured median of 4.047 is that number. To get the
same fraction of bars inside the band on r33-4 as on 5m, the threshold has to
move up by roughly an order of magnitude (±2.0 still only reaches 10.6%).

This is the same shape as the finding recorded for the ER chop filter
(commit `a03ad65`): renko already encodes trend/chop in its geometry, so
filters calibrated on time bars have little to grip. The difference is that
KAMA-slope regime is a *direction* filter, and direction is exactly what
renko does still carry — which is why, unlike the ER filter, this one
measurably moves the numbers (§7).

### 5.1 The trend step is a CLIFF, not a target — measured 2026-08-07

An earlier draft of this section advised sweeping the band "near the
with-trend step size (4 for r33-4), not at 0.5". **That was wrong**, and the
sweep in §7.1 shows why: 4 ticks is the edge of a cliff, and above it the
filter stops working rather than getting sharper.

Instrumenting every attempted entry (`_go`) on YM r33-4, 2025-01-01..06-30:

| band | signals | permitted | blocked | permitted **on the flip bar itself** | median bars-in-regime when permitted |
|---|---|---|---|---|---|
| 3.0 | 876 | 731 | 16.6% | 724 (**99.0%**) | 1 |
| 4.0 | 890 | 652 | 26.7% | 644 (**98.8%**) | 1 |
| 4.5 | 965 | 23 | 97.6% | **0** | 20 |
| 6.0 | 963 | 31 | 96.8% | **0** | 21 |

Two things fall out, and the second one changes how this filter should be
understood:

**(a) The cliff sits exactly at the renko trend step.** In a sustained renko
run the KAMA slope converges to the brick's trend step — 4 ticks on r33-4
(§5). A regime flip therefore needs a slope excursion above the band *in the
new direction*. While the band is below 4 the ordinary turn bars clear it;
at 4.5 they no longer can, and same-bar flips drop to **literally zero out of
965 signals**. The surviving 23–31 entries are mid-regime accidents (median
20 bars into a regime), not filtered signals.

**(b) It is not behaving as a trend-state filter at all.** Below the cliff,
~99% of everything it permits fires on the *exact bar the regime flips*, and
~72% of all GZK signals land on a flip bar — against roughly 5% expected by
chance, given a flip every ~20–22 bars. The GZK confluence bar and the KAMA
flip bar are, on this bar type, largely the same bar. So the gate is really a
**same-bar coincidence detector** — "take this signal only if KAMA turns with
you right now" — which is much closer to using `RegimeSignal` (the flip
pulse) as a *trigger* than using the regime as a *filter*.

That is a property of the setup, not a bug: both the signal and the regime
are computed from the same closed bar, and neither looks ahead. But it means
the band should be kept **strictly below** the bar type's trend step, and
that anyone wanting a genuine trend-state filter here needs a slower regime
source than a KAMA whose slope is pinned to the brick geometry.

## 6. Backtester integration

```python
from backtester import KAMA, KamaRegime

r = KamaRegime(tick_size, fast=2, period=10, slow=30,
               flat_threshold=0.5, confirm_bars=1, warmup_bars=None)
r.update(bar.close)      # once per bar
r.value                  # +1 bull / -1 bear / 0 still in warmup
r.flipped                # True on the bar a regime commits (RegimeSignal)
r.slope_ticks            # NaN during warmup
r.bars_in_regime
```

`warmup_bars=None` reproduces the indicator's `period + slow + 5`.

### GodZillaKilla flags

| attribute | default | meaning |
|---|---|---|
| `kama_filter` | `False` | master switch |
| `kama_fast` / `kama_period` / `kama_slow` | 2 / 10 / 30 | NT8 arg order |
| `kama_flat_threshold` | 0.5 | ticks/bar — **retune per bar type, §5** |
| `kama_confirm_bars` | 1 | 1 = commit immediately |
| `kama_warmup_bars` | 0 | 0 → indicator default (45) |
| `kama_flatten_on_flip` | `False` | force-flat when the regime turns against an open position |

Semantics, matching the news filter's principle:

- **Entries only.** The gate sits at the top of `_go()`, so it covers both the
  flat-entry path and the reversal path. Exits, reversal flattens, window and
  session flattens, ATM protective legs and the daily stand-downs are never
  gated. A reversal signal whose new direction is counter-regime therefore
  still *closes* — it just does not re-enter, leaving the strategy flat.
- **Regime 0 blocks.** During warmup the indicator has no opinion, so no
  entry is allowed rather than guessing a side.
- **The regime updates before every early return** in `on_bar`, so the series
  stays continuous regardless of votes, windows or confirmation deferrals.
- `kama_flatten_on_flip` is the one place the filter touches an existing
  position: on the flip bar only, and only when the new regime opposes the
  position. Exit tag **`kama-flat`**.

One caveat inherent to the engine: bars outside the strategy's `session` are
never delivered to `on_bar`, so the KAMA is built from in-session bars only.
For the Globex session GZK uses (`18:00`–`16:55` ET) that excludes just the
daily maintenance halt, which an NT8 Globex-template chart also excludes. A
strategy with a *narrow* session would feed the KAMA a very different series
than the equivalent NT8 chart — the entry *window* flags are safe here, only
`session` matters.

## 7. Verification

**Unit tests** (`tests/test_kama.py`, 17 cases): NT8 warmup branch, the
hand-computed ER=1 recursion (`v = prev + 4/9*(price-prev)`), the zero-noise
hold, the bar-0 price-as-diff invariance, warmup silence, first commit,
sticky band across a stretch where slope decays through the band and then
sits at exactly 0.0, band-ignored-on-first-commit, `ConfirmBars` debounce
(flips move 6→8 and 25→27), the default 45-bar warmup, and the GZK gate
(counter-regime blocked, warmup blocked, filter-off never gates,
flatten-on-flip opt-in / correct side / correct tag). Full suite: 219 passed.

**Off-by-default is bit-identical.** The same YM config run against
`godzilla_killa.py` at HEAD and against the working tree returns
`net -$4,138.00 / 180 trades / maxDD -$9,123.30`, with the identical
prop-breach timestamp `2026-06-10T13:34:51.884Z`.

**Smoke test — NOT a validation.** YM r33-4, 3-of-4 KO/PA/TH/SJ (the chart's
gate), `confirmation_bars=1`, Globex session, no time windows, fixed
TP60/SL200, 1 contract, 2026-06-01..2026-07-17 (35 trading days):

| run | net | trades | WR | PF | Sharpe | maxDD |
|---|---|---|---|---|---|---|
| filter off | -$4,138.00 | 180 | 68.3% | 0.90 | -1.87 | -$9,123 |
| filter on | -$2,834.80 | 158 | 69.0% | 0.92 | -1.28 | -$8,055 |
| on + flatten-on-flip | +$162.30 | 167 | 46.1% | 1.01 | 0.14 | -$4,194 |

Read this only as *the wiring works and the filter bites in the expected
direction*: it blocks 12% of entries and improves every risk statistic.
**All three runs breach the $2,000 trailing floor**, the base config is a
loser, the exits and the missing time window are placeholders chosen by me
rather than by any study, and 35 days is far too short to conclude anything.
The flatten-on-flip row in particular is a different strategy, not a filtered
one — 90 of its 167 exits are `kama-flat` and it never takes a stop-out, which
is why win rate collapses while average loss falls by two-thirds.

### 7.1 `kama_flat_threshold` sweep — full history, 2026-08-07

Same config, full YM history **2024-12-16..2026-07-31** (534 day files, 420
trading days), $2,000 floor, ranked by Sharpe:

| band | net | trades | WR | PF | Sharpe | maxDD |
|---|---|---|---|---|---|---|
| *off (baseline)* | *-$22,822* | *2025* | *70.2%* | *0.95* | *-0.89* | *-$28,007* |
| 0.5 *(default)* | -$15,169 | 1848 | 70.3% | 0.96 | -0.60 | -$23,527 |
| 1 | -$15,430 | 1787 | 70.3% | 0.96 | -0.63 | -$24,018 |
| **2** | **-$12,106** | 1694 | 70.3% | 0.97 | **-0.51** | -$21,011 |
| 2.5 | -$12,162 | 1691 | 70.3% | 0.97 | -0.51 | -$21,604 |
| 3 | -$15,480 | 1708 | 70.2% | 0.96 | -0.65 | -$25,226 |
| 3.5 | -$18,279 | 1703 | 70.1% | 0.95 | -0.76 | -$27,416 |
| 4 | -$20,393 | 1530 | 69.6% | 0.94 | -0.89 | -$27,706 |
| — | — | — | *cliff* | — | — | — |
| 4.5 | -$7,043 | 64 | 62.5% | 0.62 | -1.25 | -$7,347 |
| 5 | -$7,043 | 64 | 62.5% | 0.62 | -1.25 | -$7,347 |
| 5.5 | -$7,611 | 68 | 61.8% | 0.61 | -1.35 | -$7,914 |
| 6 | -$3,040 | 79 | 68.4% | 0.84 | -0.53 | -$4,310 |
| 6.5 | -$6,055 | 95 | 66.3% | 0.75 | -0.96 | -$6,590 |
| 7 | -$4,252 | 99 | 68.7% | 0.82 | -0.67 | -$5,688 |
| 7.5 | -$3,054 | 103 | 69.9% | 0.87 | -0.47 | -$4,787 |
| 8 | -$1,299 | 161 | 72.0% | 0.96 | -0.16 | -$5,449 |

**Verdict: no threshold rescues this config. Every row loses money and every
row breaches the $2,000 floor.**

The table splits at the cliff (§5.1) and the two halves must not be compared:

- **Usable region (0.5–4).** The filter works as designed here. Best is
  band 2–2.5, which cuts the loss roughly in half (-$22,822 → -$12,106) by
  dropping ~16% of trades, with PF essentially unchanged at 0.96–0.97. That
  is trade *reduction*, not edge — it removes losses and winners in nearly
  equal proportion. Note the profile is not monotone: 2 is better than 3,
  3.5 and 4, which are all worse than the 0.5 default.
- **Post-cliff region (4.5–8).** These rows trade 64–161 times in 420 days
  and their regime gate is effectively broken (zero same-bar flips). Ranking
  by Sharpe puts band 8 on top at -0.16, but that is 161 trades of
  mid-regime residue, and 4.5/5.0 return byte-identical results because no
  decision-relevant slope value separates them. **Treat the whole lower half
  of the table as small-sample noise, not as a result.**

`sweep.py`'s own sensitivity line makes the point: `0.5:-0.60 1:-0.63
2:-0.51 2.5:-0.51 3:-0.65 3.5:-0.76 4:-0.89 4.5:-1.25 5:-1.25 5.5:-1.35
6:-0.53 6.5:-0.96 7:-0.67 7.5:-0.47 [8:-0.16]` — no plateau anywhere, and
the "best" value sits at the edge of the swept range. Raw data
`reports/sweep_Ym33KamaSweep.csv`.

### 7.2 The decisive test — on a config that was already profitable

Everything above was measured on a losing placeholder, so it could only show
"does the filter rescue a bad config" (no). The real question is whether it
improves a good one. Run against the **validated MNQ evening champion**
(`strategies/godzilla_evening_confluence.py` — r70-4, PA+TH+SJ 3-of-3,
20:00–20:45 ET, TP60/SL200, 3 contracts), full history 2024-12-16..2026-07-17,
$2,000 floor. The unfiltered baseline reproduced CLAUDE.md exactly
(net $2,803.20, 140 trades, WR 82.9%, PF 1.39, maxDD -$829.74, headroom
$1,158.92, MC P(breach) 13.3%, P(pass $3k) 56.5%) — which also re-confirms the
KAMA additions leave the champion untouched.

**(A) Regime state as a filter:**

| band | net | trades | WR | PF | Sharpe | maxDD | prop headroom |
|---|---|---|---|---|---|---|---|
| *off (validated)* | *$2,803* | *140* | *82.9%* | *1.39* | *1.29* | *-$830* | *$1,159* |
| 0.5 *(default)* | $2,629 | 138 | 82.6% | 1.36 | 1.20 | -$830 | $1,132 |
| 1 | $1,934 | 130 | 81.5% | 1.27 | 0.88 | -$942 | $1,022 |
| 2 | $1,977 | 126 | 81.7% | 1.28 | 0.92 | -$907 | $1,057 |
| 3 | $1,977 | 126 | 81.7% | 1.28 | 0.92 | -$907 | $1,057 |

**Every setting is worse than off.** At the default band the filter is very
nearly inert — it blocks exactly **2 signals in 19 months** (2025-04-07 and
2026-03-05, verified by diffing the trade logs, so not warmup artifacts), and
both were winners: -$173.76, precisely the shortfall. Wider bands block
10–14 trades and cost ~30% of net. Nothing here is a plateau to stand on.

**(B) The flip pulse as an entry trigger** (require a regime flip in the
signal's direction within `kama_flip_max_age` bars, band 0.5):

| max age | net | trades | note |
|---|---|---|---|
| 0 | $87 | **1** | the config's `confirmation_bars=1` defers the entry off the flip bar |
| 1 | $2,629 | 138 | **byte-identical to the state filter above** |
| 2 | $2,629 | 138 | identical |
| 3 | $2,629 | 138 | identical |

This is §5.1(b) confirmed from the other direction: at any usable recency the
flip requirement **never binds**, because on renko every entry the state
filter permits already sits on a flip bar. The flip pulse and the regime
state are the same signal here, so "trigger" is not a distinct idea from
"filter" on this bar type. At age 0 it is destroyed by the 1-bar confirmation
deferral, which moves the entry off the flip bar entirely — 1 trade in 19
months.

**Verdict: the KAMA regime does not improve GodZillaKilla.** It reduces the
loss on a losing config by trading less (§7.1) and it costs money on the
validated one at every setting tested. `kama_filter` stays **off by default**,
and the code is kept because it is free when off, because it is a certified
NT8 KAMA other strategies can use, and as a recorded negative result. Raw
data `reports/sweep_EvKamaFilter.csv`, `reports/sweep_EvKamaFlip.csv`.

## 8. Known-unverified

1. **No NT8 parity gate.** The KAMA is certified against NT8 source; the
   regime state machine is not certified against a chart. The cheap gate:
   export a YM 33/4 chart with KamaRegimePro on it, and compare the ★ bars
   against `KamaRegime.flipped` — geometry work like `tools/compare_bars.py`,
   but on flip timestamps. Until then the `.cs` reading is the authority.
2. **Which engine set S actually ran.** The quote says "th pa sj", the chart
   card says 3-of-4 including KO. Only the chart config is tested above.
3. ~~`kama_flat_threshold` is unswept.~~ **Swept 2026-08-07 (§7.1)** — no
   value profits on this config; keep the band below the bar type's trend
   step or the gate stops functioning (§5.1). What is still unknown is
   whether the *usable* region helps a config that is profitable to begin
   with; every test so far has been on a losing placeholder.
4. **Not wired into the Terminator line.** `KamaRegime` is exported from
   `backtester`, so any strategy can use it, but only GodZillaKilla has the
   flags.
