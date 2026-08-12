# HiLoRider — technical overview and backtester test plan

Status: **ported and tested 2026-08-11 — FAILED validation, do not deploy.**
Python: `strategies/hilo_rider.py`. Bar type `w120`, parity-gated
([`WaveBars/WaveBars.md`](WaveBars/WaveBars.md)). Source read:
`HiLoRider v1.0 08-11-26/` (19 `.cs` files, 8,800 lines) + `HiLoBands.cs`.
Author: Khahn. Recommended bar type: **Wave 120**.

**Bottom line (§7).** On real-tick fills the reported Sharpe 19.193 / WR 88.5% /
PF 7.80 / MaxDD −$212 becomes **Sharpe 1.16 / WR 65.1% / PF 1.04 / MaxDD
−$5,666**, and it **breaches the $2,000 prop floor three weeks into the sample**
(MC P(breach) 70.4%). The mechanism is identified and measured in §4.2: a Wave
bar's Heikin-Ashi close sits ~86 ticks behind the tradable price, so a
close-filled backtest gets ~71% of its 120-tick target free on every trade.
Separately, §7.2 found a **repo-wide data defect** (crossed bid/ask at the Globex
reopen) worth 60% of even the honest +$13,486 — so Stages 3-5 are on hold until
that is fixed.

§1–§6 are the pre-port analysis, kept because §4.2's prediction was
subsequently confirmed by measurement; §7 is the result.

---

## 1. What the strategy actually is

Stripped of the ~8,000 lines of infrastructure (HUD, WPF panel, ML gate,
meta-label stub, GEX, HMM regime, econ calendar, watchdogs, unmanaged bracket
plumbing — **all of it off by default**), HiLoRider is small:

> On Wave-120 bars, when a Donchian midline ticks in your favour and the bar
> closed in your favour, enter with the trend. Take profit at a fixed 120 ticks.
> Stop at the opposite Donchian band. Trail on recent bar lows/highs.

That is it. Everything else is either disabled or cosmetic.

### 1.1 The indicator — `HiLoBands.cs`

A single-period Donchian channel, 40 lines of real logic:

```
upper = MAX(High, LookbackPeriod)[0]      # inclusive of the current bar
lower = MIN(Low,  LookbackPeriod)[0]
mid   = (upper + lower) / 2
```

It emits **two** signal series, and the distinction matters:

| | condition (long; shorts mirror) |
|---|---|
| `Signal` (slot 1) | `Close[0] > mid[0]` **and** `Close[1] <= mid[1]` **and** `Close[0] > Close[1]` — a *fresh crossover* |
| `Signal2` (slot 2) | `mid[0] > mid[1]` **and** `Close[0] > Close[1]` — the midline's *own slope* |

Warmup: values start at `CurrentBar >= LookbackPeriod`, signals at
`LookbackPeriod + 1`.

**`Signal2` is doing essentially all the work.** It requires only that the
Donchian midline moved up and the bar closed up. On Heikin-Ashi Wave bars — where
the close is a 4-way average and therefore heavily autocorrelated — `Close[0] >
Close[1]` is true on most with-trend bars, and a rising midline is true whenever
the channel is drifting. `Signal` (a genuine crossover) is rare by construction;
`Signal2` fires constantly. With 7,950 reported MNQ trades over 29,868 bars
(≈1 trade per 3.8 bars), the observed frequency confirms slot 2 dominates.

So the honest one-line description of the entry is: **"while flat, enter with the
trend on nearly every with-trend bar."** It is not a selective signal.

### 1.2 The cascade

`GetHiLoRiderSignal()` (`HiLoRider.cs:291`) checks `Signal` first, falls back to
`Signal2`, tags which fired into `entrySignalSource`, and dedups to **one signal
per bar** via `lastHiLoRiderSigBar`.

---

## 2. Live configuration as shipped

`SetDefaults()` carries the MGC values; `State.DataLoaded` overrides per
instrument (`HiLoRider.cs:730-800`). **The module header comment block is stale**
— it documents the older TBars-era config (MNQ 60/4/200/5/0, MGC 80/4/150/5/0)
that the code no longer uses. Trust the code.

| | MNQ | MGC |
|---|---|---|
| `LookbackPeriod` | **25** | **10** |
| `FixedTPTicks` | 120 | 120 |
| `StopBufferTicks` | 2 | 2 |
| `TrailMinTicks` | **40** | **4** |
| `HLStage2TriggerBars` | 5 | 3 |
| `HLStage3TriggerBars` | 6 | 5 |
| `HLStage3TrailTicks` | 10 | 10 |

Shared: `StopMode = ReversalBar` (**repurposed** — it means "opposite channel
band", see §3.1), `TargetMode = FixedTicks`, `TrailMode = HighLow`,
`MaxBarsInTrade = 10`, `FixedSLTicks = 60` (fallback only), `MinStopTicks = 5`,
`CooldownBars = 0`, `Contracts = 1`, `BE_TriggerTicks = 9999` (breakeven off).

**Active gates:** `EnableHourBlock` (blocks ET hours 01:00–05:59),
`RTHOpenBlockMins = 15` (blocks the first 15 min after 09:30 ET),
`EnableGapFilter` (blocks counter-gap entries for 30 min after 09:30 when the
session gapped ≥ 0.50%), `EnableCloseConfirmation`, and the TF1–TF7 session
windows — but **TF7 is 00:00–23:59 and enabled**, so session windows are
effectively wide open. The strategy runs all hours except 01:00–05:59 ET.

**`MaxDailyTrades = 20` is inert**, because the guard is
`if (!OpenForFreeTrade && MaxDailyTrades > 0 && ...)` and `OpenForFreeTrade =
true`. There is no daily trade cap in force.

**`EnableCloseConfirmation` is very nearly a no-op** — both `Signal` and
`Signal2` already require `Close[0]` vs `Close[1]` internally. It differs only in
realtime, where `Calculate.OnEachTick` makes the strategy's `Close[0]` the
*forming* bar while `hiloInd` (an `OnBarClose` indicator) reports the last
*closed* bar. In the Strategy Analyzer it is redundant.

**Off by default, and therefore out of scope for a v1 port:** ML gate,
meta-label, GEX, HMM regime, SuperTrend (informational only), scale-out,
`DiffCross` auto-exit, ADX filter, diff-band/chop-hold filter (so `BarsToHold = 3`
is inert), news block, midday block, volatility sizing, daily loss/profit limits
(which are Realtime-only anyway), drawdown gate.

---

## 3. Exits

### 3.1 Stop — opposite channel band

`GetHiLoRiderStopPrice()` (`HiLoRider.cs:343`): `hiloInd.LowerBand[0] −
StopBufferTicks` for a long, `UpperBand[0] +` for a short, rounded to tick,
validity-checked (must sit on the correct side of entry), else fall back to
`FixedSLTicks = 60` ticks.

This stop is **wide**. On Wave-120 bars each bar's high/low range is ~180 ticks
(WaveBars.md §4), so a 25-bar Donchian channel on MNQ spans many hundreds of
ticks. Against a 120-tick target the nominal risk/reward is deeply negative —
which is why the trail below, not the stop, is what actually closes losers.

### 3.2 Target — fixed 120 ticks, measured from the *limit price*

`CalcTargetLevel()` computes from `limitPx`, **not** from the eventual fill
(`HiLoRider.cs:1259`). If the entry fills anywhere other than `limitPx`, the
realised target distance differs from 120 ticks. Faithful to port, worth a
comment.

In historical mode the target is checked manually
(`HiLoRider.cs:1062-1076`) as `High[0] >= targetLevel` for a long — i.e. against
**Wave-bar high/low, which include the phantom open**, a price that never traded.
For a long, the phantom open sits *below* the close, so this particular check is
conservative for targets and aggressive for stops. Not the source of the problem
in §4, but it does mean NT8's own historical run is not tick-accurate either.

### 3.3 Trail — `HighLow`, three stages, tighten-only

`ManageHighLowTrail()`, staged on `entryBarsInTrade`:

| stage | trigger (MNQ / MGC) | stop |
|---|---|---|
| 1 | immediately | `Low[1] − StopBufferTicks(2)` |
| 2 | ≥ 5 / ≥ 3 bars | `Low[0] − TrailMinTicks(40 / 4)` |
| 3 | ≥ 6 / ≥ 5 bars | `peak_since_entry − HLStage3TrailTicks(10)` |

Plus a floor: the stop must be at least `TrailMinTicks` from entry (a *widening*
clamp), and `TryMoveStop` only ever tightens.

Two notes for the port. `Low[0]` at stage 2 is the **forming** bar's low under
`Calculate.OnEachTick`; our engine's `on_bar` sees the just-closed bar, a
documented one-bar divergence. And these lows are *Wave-bar* lows including the
phantom open, so the trail anchors on prices that never printed — in our engine
that simply places a real stop order further away than the NT8 chart implies.
Faithful, but it changes the trail's character versus a raw-price bar type.

### 3.4 `MaxBarsInTrade = 10`

Market exit after 10 bars in trade. The source calls this a "confirmed dead
parameter (5-50 all identical)" because the target resolves first. **Under
honest fills that is unlikely to remain true** — if the target stops being nearly
free (§4), this becomes the dominant exit. Port it and watch it.

### 3.5 Entry order type — the live default is a *passive* limit

`useMarketOrder` defaults to **false** (`HiLoRiderUI.cs:125`), so the live entry is

```
long:  limit at GetCurrentBid(0) - LimitOffsetTicks(8) * TickSize
short: limit at GetCurrentAsk(0) + 8 ticks
```

A buy limit **8 ticks below the bid** on a trend-following signal: it fills only
if price retraces 8 ticks. And nothing cancels it — `entryOrder` is nulled only
on Filled / Cancelled / Rejected (`HiLoRider.cs:1640, 1672`), and the entry gate
is `if (entryOrder != null) return;`, so an unfilled GTC limit **blocks every
subsequent signal** until the session-close sweep clears it. That is a material
behaviour, not a detail, and it must be modelled to test the strategy as it
actually ships.

---

## 4. The reported results, and why they cannot be right

### 4.1 The claims

| | MNQ | MGC |
|---|---|---|
| bars | 29,868 (Aug 2025–Aug 2026) | 12,132 (Aug 2025–Aug 2026) |
| trades | 7,950 | 4,476 |
| Sharpe | 19.193 | 18.218 |
| win rate | 88.5% | 87.1% |
| profit factor | 7.80 | 7.24 |
| max drawdown | **−$212** | **−$433** |
| bootstrap CI-lo | 18.417 | 17.357 |
| permutation p | 0.003 | 0.003 |

Take the MNQ row at face value. PF 7.80 at WR 88.5% implies avg win ≈ avg loss
(`(0.885·W)/(0.115·L) = 7.80` → `W/L ≈ 1.01`). At a 120-tick target on MNQ
(120 × $0.50 = $60), 7,950 trades would net roughly
`7950 × (0.885×60 − 0.115×59) ≈ **$368,000** on one micro contract in one year`
— with a maximum drawdown of $212, and before the $8,268 of round-turn
commission those trades cost. Those figures are not optimistic; they are not
physically available in the MNQ micro market.

The permutation test and bootstrap CI do not help. Both resample the *same*
trade-P&L series. If every trade carries a constant structural subsidy, both
tests will confirm with high confidence that the subsidy is real — which it is.
They validate internal consistency, not the fill model.

Note also the **cross-instrument similarity**: MNQ and MGC have very different
volatility, tick value and lookback (25 vs 10), yet returned WR 88.5% / 87.1% and
PF 7.80 / 7.24. Two independent edges do not land that close. A shared structural
artifact does.

The validating scripts (`HiLoRider_MNQ_Wave120_Backtest.py`,
`HiLoRider_MGC_Wave120_Backtest.py`) are **not available** — not in the folder,
and not held on this side either (confirmed 2026-08-11). The fill model therefore
cannot be audited at source, and probably never will be. What follows is a
**hypothesis with arithmetic**, not a reading of their code — and §6 Stage 2b is
designed to confirm or kill it empirically without needing them.

### 4.2 The mechanism: the Heikin-Ashi close lags the market by 5N/7 ticks

**MEASURED 2026-08-11 on real repo data — no longer a hypothesis.** The port
landed (§5.0), so the Stage 2b measurement was run immediately; the numbers are
at the end of this section.

A backtest driven by a Wave-bar CSV has one obvious entry price to reach for: the
signal bar's close. On Wave bars that close is **Heikin-Ashi**, and it is not a
price anyone can trade at. How far off is it? In a steady with-trend up run, let
`x = edge − ha_close` (how far the HA close sits below the threshold the bar
actually completed at), with trend offset `T` and open offset `O`. The bar was
**born** at the previous threshold `edge − T`, and its phantom open is measured
back from *that*, not from where it completes:

```
fake_open     = (edge - T) - O
ha_close_prev = (edge - T) - x
ha_open       = (fake_open + ha_close_prev)/2 = edge - T - (O + x)/2

high = edge          <- max_exc replaces the running high with the threshold
low  = fake_open = edge - T - O
close_raw = edge     <- clamped: the breaking tick is strictly beyond it

ha_close = (ha_open + high + low + close_raw)/4
         = edge - [2T + (O + x)/2 + O]/4

fixed point:  3.5x = 2T + 1.5O
with O = N and T = N/2:   3.5x = 2.5N   ->   x = 5N/7
```

**The Heikin-Ashi close lags the real breakout price by `5N/7` ticks** — about
0.71 N. At Wave 120 that is **86 ticks**.

> **Correction.** An earlier revision of this section derived `x = N/2` (60
> ticks) by placing the phantom open at `edge − O` instead of `edge − T − O`.
> That understated the subsidy. The corrected figure is larger, so the
> conclusion below is stronger, not weaker.

Verified two ways. A clean synthetic with-trend run (each bar completed by a
single tick one past its threshold, no dips, so `run_lo` stays at the phantom
open) reproduces the closed form to the tick grid:

| N | 8 | 20 | 40 | 120 | 200 |
|---|---|---|---|---|---|
| measured `x` (ticks) | 6.00 | 14.00 | 28.00 | **86.00** | 143.00 |
| `5N/7` | 5.71 | 14.29 | 28.57 | **85.71** | 142.86 |

And on **real MNQ and MGC ticks** (July 2026, 27 sessions, `w120`), measuring
`price of the tick that actually closed the bar − the bar's reported HA close`,
signed in the direction of the bar:

| | bars | median | mean | subsidy is favourable on |
|---|---|---|---|---|
| MNQ w120 | 4,566 | **88 ticks** | 91.6 | **99.8%** of bars |
| MGC w120 | 532 | **88 ticks** | **85.7** | 98.3% of bars |

MGC's mean of 85.7 is `5 × 120 / 7 = 85.71` to two decimals. The medians are
identical across two instruments with completely different volatility and tick
value, and the sign is favourable on essentially every bar — which is the
signature of a structural constant, not a market effect.

So a backtest that buys at the HA close of an up bar buys **~86 ticks below
where the market actually is**, unconditionally, on every trade, always in the
trade's favour. The target is 120 ticks. **Roughly 71% of it is free before the
trade starts** — and the remaining ~34 ticks is well under one with-trend bar
(60 ticks), which is the cheap direction by construction.

That single fact produces a high-80s win rate, a PF near avg-win/avg-loss ≈ 1,
and a drawdown that only appears when the trend genuinely reverses — i.e. every
line of the table in §4.1, from an entry-price fiction rather than an edge.

It is also scale-invariant, which explains the cross-instrument similarity: the
subsidy is `5N/7` ticks and the target is `N` ticks on **both** instruments, so
both land at the same 71% ratio (MNQ $43 free of a $60 target; MGC $86 free of a
$120 target).

### 4.3 Why this repo can settle it

This is the exact failure class the backtester exists to catch, and the CLAUDE.md
note on the Drew/GZK thread is the precedent: *"NT8 Strategy Analyzer on Renko
bars, whose synthetic-brick fills are the fantasy-fill artifact this repo exists
to avoid."* Same shape, different bar type.

Here, fills resolve on **real ticks** via each bar's `[i0, i1)` span regardless of
what the bar's OHLC says (WaveBars.md §4.1). The HA close is never a fillable
price in this engine. So porting HiLoRider produces the honest number
mechanically — no argument required.

And §6 Stage 2b measures the subsidy directly, with no engine changes: for every
signal bar, record `ha_close − last traded price in that bar's span`. If the
median is ≈ 60 ticks at `w120`, §4.2 is confirmed outright.

### 4.4 The two shipped Python tools are live-log tools, not the backtest

`HiLoRiderTrainer.py` (569 lines) and `HiLoRiderDashboardGenerator.py` (981 lines)
are the only Python in the folder, and **neither can corroborate the §4.1
figures**. Both read `HiLoRider_trades.jsonl` — written at runtime by
`HiLoRiderTradeLogger.cs` — and both explicitly *drop* backtest rows:

```python
df = df[~df['Account'].str.strip().str.lower().isin(
    ['backtest', 'historical', 'optimizer'])]
```

They are live-account reporting tools. Their default paths
(`C:\Users\Administrator\Documents\NinjaTrader 8\HiLoRiderLogs\`) point at
Khahn's VPS, so a **live** log does exist somewhere — see §6.1.

Two incidental confirmations of things already flagged above. The staleness in §2
is fleet-wide, not a one-file slip: both docstrings still describe the *retired*
genesis config ("LookbackPeriod=80 ... FixedTicks(150t) ... TrailMode=
MidlineOffset"), and the dashboard's own header hardcodes *"MNQ Futures · TBars
120 · Reversal-Bar Momentum"* — wrong bar type, wrong instrument for a gold bot,
wrong signal family. **Only the code is authoritative in this project.** And the
trainer confirms the ML layer is inert by construction: 5 of its 8 features are
dead (f0/f2/f3 hardcoded `0.5`, f7 an admitted mis-reconstruction of a
per-session counter from a lifetime one), which is one more reason §5.2 leaves it
out.

### 4.5 A second, independent inflation mechanism: closed-trade-only drawdown

The dashboard computes max drawdown like this (`compute_all`):

```python
df_s['CumDollars'] = df_s['ProfitCurrency'].cumsum()
peak_arr = np.maximum.accumulate(cum_arr)
max_dd   = float((cum_arr - peak_arr).min())
```

That is the drawdown of the **closed-trade** equity curve. It never looks at
intratrade excursion — even though `MaeTicks` is in the log and the dashboard
plots it two panels away. `calmar` then divides a *calendar*-annualised P&L
(`total_pnl × 365 / span_days`) by that number, so a small denominator inflates it
by construction.

If the missing backtest scripts share this convention — same author, same fleet,
same log schema — then **"MaxDD −$212" is not the same quantity this repo
reports**, before any argument about fills. `PropFirmTracker` trails the
*intratrade* equity peak and a breach is an equity **touch**; `TradeRecorder`
records MAE/MFE in dollars. With a stop parked at the opposite Donchian band on
bars whose high/low range is ~180 ticks, intratrade MAE is exactly where the risk
lives — so our drawdown figure will be far larger than theirs *for structural
reasons alone*, independent of §4.2. Expect that gap and do not treat it as a
port bug.

I can only demonstrate this convention in the dashboard, not in the backtest. But
it is a second mechanism pointing the same way, and it is cheap to check: Stage 2
reports both closed-trade DD and intratrade-touch DD side by side, so the
contribution of each mechanism becomes separable.

A smaller accounting note for any future comparison: `IsWin = ProfitTicks > 0`
and `IsLoss = ProfitTicks < 0`, so scratches at exactly 0 fall into neither
bucket — their win rate divides by all trades while their profit factor divides
by losses only.

### 4.6 The live/backtest disagreement, independently

Even setting §4.2 aside, the shipped bot cannot reproduce its own backtest,
because live entries go in at `bid − 8 ticks` (§3.5) — a *real, quoted* price —
while the backtest used a synthetic one ~60 ticks better, and assumed it always
filled. Those are two different strategies. This is worth telling Khahn
regardless of how the port comes out.

---

## 5. Port design (`strategies/hilo_rider.py`)

### 5.1 In scope for v1

- **`HiLoBands`** — Donchian via `indicators.Highest` / `indicators.Lowest`
  (already inclusive-of-current, matching NT8's `MAX(High, N)[0]`), plus `mid`
  and both signal series with NT8's warmup boundaries (`N`, then `N + 1`).
- **Cascade** — `Signal` priority, `Signal2` fallback, one signal per bar, source
  tag on the trade record so slot 1 vs slot 2 attribution is measurable.
- **Gates** — hour block 01:00–05:59 ET, RTH-open 15 min block, gap filter, close
  confirmation, TF windows (default all-hours), `CooldownBars`. All ET, per repo
  convention.
- **Entry** — `entry_limit_offset_ticks` (0 = market). Default **8**, matching the
  shipped bot, plus `entry_limit_ttl_bars` (default 0 = never cancel) so the
  block-everything behaviour of §3.5 is reproduced rather than quietly fixed.
- **Stop** — opposite band ∓ `StopBufferTicks`, validity check, `FixedSLTicks`
  fallback.
- **Target** — `FixedTPTicks` from the *limit price* (§3.2).
- **Trail** — the three `HighLow` stages, the `TrailMinTicks` floor, tighten-only.
- **`MaxBarsInTrade`** market exit.
- **Session** — the Globex trading day `("18:00", "16:55")` ET with
  `flat_at_session_end`, so a run is one compliant CME trading day (CLAUDE.md
  "CME trading day"). `min_hold_s = 0` and `news_filter` off, per repo default.

### 5.2 Out of scope for v1

Everything in §2's "off by default" list. Each is a separate, individually
testable idea; none is part of what Khahn validated. Not porting them keeps the
first number interpretable.

### 5.3 Known divergences to record in the docstring

1. `on_bar` sees the closed bar; NT8 evaluates on each tick, so entries land one
   bar later than NT8's and stage-2 `Low[0]` means the just-closed bar.
2. NT8's historical target check reads Wave-bar `High[0]`/`Low[0]` (which include
   the phantom open); ours is a real stop/limit resolving on real ticks. **This is
   a correctness improvement, not a parity loss** — but it means our exits will
   not match an NT8 Strategy Analyzer run, by design.
3. `RealtimeErrorHandling.IgnoreAllErrors`, the unmanaged-bracket watchdogs, and
   the OCO plumbing have no analogue and no bearing on a backtest.

---

## 6. Test plan

Staged, each stage gated on the previous one. Stop at the first stage that fails
and record it as a negative result, per this repo's practice.

**Stage 0 — Wave Bars lands and passes its parity gate.** ✅ **DONE 2026-08-11.**
Port: `w120`, `backtester/data.py::build_wave_bars`, `tests/test_wave_bars.py`.
Parity gate run against a real MNQ Wave-120 export: timing 99.8%, **high/low
100.0%** (low exact on every matched bar), close 91.9%, full OHLC 79.4%, 99.9% of
bars within ±1 tick on every field. Geometry and timing **certified**; the only
residual is ±1-tick Heikin-Ashi rounding, which cannot reach the fill model.
**MGC gated too** (344 bars, 07-05 → 07-27 ET): high/low **341/341 = 100.0%**
and **every field within ±1 tick on 341/341** — not one bar off by more than a
tick, cleaner than MNQ. Full write-up in `WaveBars/WaveBars.md` §7 and §7.1.
**Stages 2 and 4 are both unblocked.**

One data limit to carry into Stage 4: the repo's recorded MGC contract expires
after 2026-07-28 (154k trades that day, then 4,181 → 155 → ~20/day), so **MGC
strategy runs are only meaningful up to ~2026-07-28**, and a full-history MGC
backtest will silently trade near-empty days after it. Check trade counts before
trusting any MGC result past that date; certifying/extending needs contract
MGC 08-26 recorded (`research/TBars_spec.md` §9).

**Stage 1 — signal parity (cheap, optional).**
`HiLoBands` is 40 lines of Donchian, so this is low-risk. Run it only if Stage 2
produces a surprise: export `Signal`/`Signal2` per bar from NT8 (pattern:
`tools/gbSignalExporter.cs`) and match bar-for-bar via
`tools/compare_signals.py`. This separates "did I port the indicator" from "is
the edge real".

**Stage 2 — the honest baseline.** ✅ **RUN 2026-08-11.** `strategies/hilo_rider.py`,
MNQ `w120`, `lookback_period=25`, TP 120, StopBuffer 2, TrailMin 40, stages
5/6/10, **market entry**, 2024-12-16 → 2026-08-07 (501 days), 1 contract,
$2,000 prop floor. Full result and interpretation in **§7**.

> **Prediction, recorded so it can be wrong — and it was wrong.** I predicted "a
> large loser". It came back **net +$13,486, Sharpe 1.16**. Wrong in direction;
> right in substance (no usable edge, floor breached in week three). What the
> subsidy inflated was the win rate and profit factor, not the sign. See §7.4.

**Stage 2b — measure the subsidy directly.** ✅ **DONE 2026-08-11, confirmed.**
Ran as soon as the bar type landed, since it needs bars but no strategy: for
every `w120` bar, `price of the tick that closed it − its reported HA close`,
signed with the bar. Median **88 ticks** on both MNQ (n=4,566) and MGC (n=532),
favourable on 99.8% / 98.3% of bars, MGC mean 85.7 = `5N/7` exactly. Full
numbers and the closed form in §4.2. This is the artifact to hand Khahn — it
explains the Sharpe-19 result without needing his scripts.
Re-run per-signal-bar during Stage 2 to confirm the subsidy on the *entered*
subset matches the all-bars distribution (no reason to expect otherwise, but it
is one line of extra evidence).

**Stage 3 — the shipped entry model.**
Stage 2 rerun with the `bid − 8` limit and no TTL. Report the **fill rate** and
how much of the signal stream the resting-order block swallows. Expect far fewer
trades; direction of the P&L effect is genuinely unknown (a filled pullback entry
is 8 ticks better, but the block is arbitrary).

**Stage 4 — MGC.** Only if Stage 2 or 3 is not a decisive loser.
`LookbackPeriod=10`, TP 120, TrailMin 4, stages 3/5/10, MGC history 2025-01-01 →
2026-08-07. Note MGC commission is $1.34 round turn.

**Stage 5 — validation, only on a positive Stage 2/3/4.**
In this order, and each is a stop-gate:
1. Monte Carlo — P(breach $2k), P(pass a $3k eval before breach).
2. Parameter sweep over `LookbackPeriod` × TP × `StopBufferTicks` × `TrailMinTicks`,
   **cross-checking `prop_min_headroom` on the whole grid, not just the winner**
   (the r80-20 lesson: Sharpe-ranked selection walks into floor breaches the
   metric cannot see).
3. `walkforward.py`, 5 windows. Anything below WFE ≈ 1.0 or without parameter
   convergence gets written up as a negative result and stops here — same bar
   that `terminator_mcl`, `terminator_mes` and the YM r33-4 Three Amigos failed.

**Stage 6 — write-up.** Result goes in this file either way. A negative is worth
recording: it is a clean, quantified demonstration of the HA-close fill artifact
on a new bar type, which is reusable knowledge.

### 6.1 What I need from you before starting

1. ~~**A real NT8 Wave-120 chart export.**~~ ✅ Both received and gated
   2026-08-11 (`WaveBars/bars_MNQ_Wave_120.csv`,
   `WaveBars/bars_MGC_Wave_120.csv`). Nothing further needed here.
2. **Confirmation of the instrument to lead with.** MNQ has the longer usable
   history (2024-12-16 → 2026-08-07) and cheaper commission; MGC is what Khahn
   appears to trade but its recorded contract dies after 2026-07-28, so an MGC
   run cannot use the full window without new data. Default, and my
   recommendation: **lead with MNQ**.
3. **Khahn's live `HiLoRider_trades.jsonl`**, if he has one with real fills
   (default path `C:\Users\Administrator\Documents\NinjaTrader 8\HiLoRiderLogs\`).
   This is now the better ask than the backtest scripts, which are gone: a live
   log is *real broker fills*, so comparing its realised ticks/trade and win rate
   against the §4.1 claims settles the fill-model question empirically and from
   his own account. If live expectancy is a fraction of the backtest's, that is
   the finding, and no port is needed to establish it.
   Caveats to apply when reading it: it will be thin (this is an Aug 2026 bot),
   `EnableTradeLogging` must have been on, and the log records `ProfitTicks` per
   round trip — so recompute drawdown intratrade rather than reusing the
   dashboard's closed-trade figure (§4.5).

---

## 7. Stage 2 result — MNQ w120, honest fills, run 2026-08-11

`strategies/hilo_rider.py`, MNQ `w120`, lookback 25, TP 120t, stop = opposite
Donchian band − 2t, HighLow trail (min 40t, stages 5/6/10), MaxBarsInTrade 10,
market entry, session `("18:00","16:55")` ET, 1 contract, $2,000 prop floor,
2024-12-16 → 2026-08-07 (501 days).

| | reported (§4.1) | **measured here** |
|---|---|---|
| trades | 7,950 | **9,199** |
| win rate | 88.5% | **65.1%** |
| profit factor | 7.80 | **1.04** |
| Sharpe | 19.193 | **1.16** |
| max drawdown | −$212 | **−$5,665.58 (−11.04%)** |
| net | (implied ~$368k) | **+$13,486.04** |
| prop floor | — | **BREACHED 2025-01-07**, MC P(breach) **70.4%** |

Gross $23,053, commission $9,567 on 9,199 round turns. 9,361 signals → 9,193
entries; slot attribution **2,001 crossover / 7,192 slope**, confirming §1.1 —
`Signal2` does ~78% of the work. Avg win $59.68 (= the 120-tick target ×
$0.50), avg loss $107.15. The edge is arithmetically thin and fully explained:
`0.651 × 59.68 − 0.349 × 107.15 = +$1.46/trade`, matching the reported avg
trade of $1.47.

Trade count within 16% of the author's 7,950 is a useful cross-check that the
signal port is faithful.

### 7.1 The claimed edge is a fill artifact, confirmed

Sharpe 19.193 → **1.16**. PF 7.80 → **1.04**. WR 88.5% → **65.1%**. Max
drawdown −$212 → **−$5,666, twenty-seven times worse**. Nothing about the
strategy changed between those two columns except that fills resolve on real
ticks here. §4.2's measured ~86-tick-per-trade Heikin-Ashi subsidy accounts for
the gap: it converts a 65%-win coin-flip into an 88.5%-win machine and hides the
drawdown entirely.

### 7.2 A data defect makes even +$13,486 an overstatement

**60% of the net profit comes from 80 trades (0.9%) that fill against a crossed
quote** — an event whose recorded `bid > ask`:

| | trades | net | win rate |
|---|---|---|---|
| touching a crossed quote | **80** | **+$8,066.30** | 82.5% |
| clean | 9,119 | **+$5,419.74** | 65.0% |

Worked example, 2025-02-12 13:30:14.312 UTC — one event, `price 21650.00,
bid 21779.75, ask 21564.50` (bid **861 ticks above** the ask). The strategy
shorted at the inverted bid and its target bought back at the inverted ask, both
off that single record: +$429.46 in zero seconds with MAE and MFE both 0.00.

Repo-wide census, MNQ 2024-12-16 → 2026-08-07: **702 crossed-quote events on
450 of 541 days**, median cross 478 ticks, max 1,802. 467 of them sit at
22:00/23:00 UTC (**18:00/19:00 ET — the Globex reopen**), all stamped
`22:00:00.1xx`: `_reduce_raw` carries the prevailing bid/ask across the
17:00–18:00 halt without invalidating it, so the first print after reopen pairs
a stale quote with a fresh one. This is **not specific to HiLoRider** — any
strategy that can trade in the first moments after the reopen is exposed. See
the CLAUDE.md "Conventions & gotchas" entry.

So the contamination-free estimate is **≈ +$5,420 over 19 months on 9,119
trades = +$0.59/trade**, i.e. ~1 tick of gross edge per trade after commission.
(First-order: excising trades from a log is not the same as re-running without
them, but the direction is unambiguous.)

### 7.3 And what survives is concentrated in very fast trades

| | trades | net |
|---|---|---|
| held < 10 s | 689 | **+$28,304.94** |
| held ≥ 10 s | 8,510 | **−$14,818.90** |

The sub-10-second book is worth more than the entire gross profit; everything
held longer loses. Some of that is the crossed-quote trades above, but not all —
Wave-120 bars complete on a 60-tick move, so in a fast trend three bars and a
120-tick target can resolve inside a few seconds, and those are genuine fills.
Either way the profile is the opposite of robust, and it is exactly what the
repo's `sub10s_*` diagnostic exists to surface.

### 7.4 Verdict

**HiLoRider does not have a deployable edge, and must not be run on a prop
account.** It breached the $2,000 trailing floor on **2025-01-07**, about three
weeks into the sample, with a 70.4% Monte Carlo breach probability. Strip the
crossed-quote contamination and the remaining edge is ~1 tick per trade before
it; strip the sub-10-second trades and it is decisively negative.

The strategy is not *garbage* — a 65% win rate on a with-trend Donchian signal
is a real, if tiny, effect, and gross P&L is positive. It is simply nowhere near
what was reported, and the 9,567 dollars of commission on 9,199 round turns is
of the same order as the entire gross edge. Trading it at 1 MNQ contract has
roughly the expectancy of paying the exchange for the privilege.

Stages 3–5 are **not worth running until the crossed-quote data defect is
fixed**, because every number they produce would carry the same 60%
contamination. Sequence from here:

1. Fix the reopen quote-carry defect in `_reduce_raw` (CACHE_VERSION bump,
   ~1 GB/symbol rebuild, and **every existing champion result needs
   re-validating** — that is the user's call, not a side effect to slip in).
2. Re-run Stage 2 clean to get the real headline.
3. Only then Stage 3 (the shipped `bid − 8` passive limit and its
   blocks-everything behaviour) and Stage 4 (MGC, capped at 2026-07-28 by the
   contract expiry noted in Stage 0).

Raw data: `reports/HiLoRider_MNQ_trades.csv`, `reports/HiLoRider_MNQ.html`
(both gitignored; re-run
`cli.py strategies/hilo_rider.py --start 2024-12-16 --end 2026-08-07`).
