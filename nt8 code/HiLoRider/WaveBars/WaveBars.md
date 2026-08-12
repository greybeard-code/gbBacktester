# Wave Bars — technical notes and backtester port plan

Status: **ported 2026-08-11**, grammar `w<N>`. **Parity gate RUN 2026-08-11**
against a real MNQ Wave-120 chart export (§7): bar timing and **geometry
certified** — high/low 100.0%, low exact on every single bar — with the only
residual being ±1-tick Heikin-Ashi rounding propagation (99.9% of bars are
within ±1 tick on every field). Python: `backtester/data.py::build_wave_bars`
(sharing `_build_tbar_family_core` with `build_tbar_bars`), tests in
`tests/test_wave_bars.py`. Source read: `BarsTypes/WaveBarsType.cs` (206 lines,
complete, plus `Info.xml` → NT8 8.1.7.0). Khahn's recommended setting is
**Wave 120**.

Headline finding, and the reason this port is cheap: **Wave Bars is the TBars
algorithm.** The two share the same one-parameter derivation, the same strict
breakout test, the same threshold clamp, the same phantom open, the same
Heikin-Ashi output transform, and the same reset trigger. This repo already has
a certified TBars port (`backtester/data.py::build_tbar_bars`, grammar `tb<N>`,
spec `research/TBars_spec.md`, parity gate run 2026-08-04). Wave Bars differs in
**four** behavioural places — and in three of the four, Wave Bars is the *better*
of the two, because it fixes bugs the TBars port had to work around.

---

## 1. Provenance

The file header says *"Open-source bars type — by FlowMatriX"*. It is readable,
idiomatic NinjaScript with real identifier names, so unlike ninZaRenko, TBars,
and SaberRenko this is **actual source, not a decompilation** — the repo's
no-decompiled-source policy does not bite here and the file can be read
directly rather than characterised behaviourally.

Whether FlowMatriX wrote this independently or reimplemented TBars is not
determinable from the file and is not a question this port needs answered. What
matters is the measurable fact: the math is TBars' math, so the existing,
gate-tested port is reusable almost wholesale.

| | TBarsNEW (ported) | **Wave Bars** |
|---|---|---|
| `BarsPeriodType` id | 98765 | **77077** |
| parameter lives in | `BaseBarsPeriodValue` | **`BarsPeriod.Value`** |
| grid label | "Speed Settings" | **"Wave Size"** |
| `Value` / `Value2` | derived (`N/2`, `N*2`), removed from grid | **`Value` *is* N; `Value2` removed** |
| default N | 2 (via `ApplyDefaultBasePeriodValue`) | **25** (via `ApplyDefaultValue`) |
| `DaysToLoad` | 5 | 5 |
| `BuiltFrom` | Tick | Tick |
| per-tick `Print()` | yes | none |
| `try/catch` swallowing `OnDataPoint` | yes (corrupt state continues silently) | **none** |
| `GetPercentComplete` | — | implemented |

---

## 2. Parameters

**One** knob, `BarsPeriod.Value` = N ("Wave Size", ticks). `SeedOffsets` derives
everything:

```
trendOffset    = max(1, N / 2) * tickSize      <- INTEGER division, clamped >= 1 tick
reversalOffset = N * 2         * tickSize
openOffset     = N             * tickSize
```

A reversal costs **4x** a with-trend continuation. That asymmetry is the whole
point of the bar type: continuation is cheap, changing your mind is expensive.
All three distances are whole tick counts, so every threshold sits on the tick
grid.

Note the `max(1, ...)`: TBars has no such clamp, so `tb1` gives a zero-tick trend
threshold (a bar per uptick) and `parse_barspec` rejects it. `w1` is legal —
trend 1 / reversal 2 / open 1 — merely useless.

**Wave 120 in instrument terms:**

| | ticks | MNQ (0.25 / $0.50) | MGC (0.10 / $1.00) |
|---|---|---|---|
| trend (continuation) | 60 | 15.00 pts = $30.00 | 6.00 = $6.00 |
| reversal | 240 | 60.00 pts = $120.00 | 24.00 = $24.00 |
| phantom open offset | 120 | 30.00 pts = $60.00 | 12.00 = $12.00 |

MGC Wave 120 is numerically identical to the `research/TBars_spec.md` §1
reference config (trend $6 / reversal $24 / open offset $12) — the same chart,
under a different bar type's name.

---

## 3. The per-tick rule

State: `(barMax, barMin, barDirection)` plus the forming bar's stored OHLC.

For each incoming trade at price `p`:

- `maxExceeded = Compare(p, barMax) > 0`, `minExceeded = Compare(p, barMin) < 0`
  — **strict**. A tick landing exactly *on* a threshold does not complete the bar.
- **Neither** → extend the forming bar: `high = max(p, high)`,
  `low = min(p, low)`, and *rewrite* the close as
  `ha_close(storedOpen, high, low, p)`.
- **Either** → complete and open a successor (§4).

Because `p` is strictly beyond the threshold, the `Math.Min(p, barMax)` /
`Math.Max(p, barMin)` clamp always collapses to the threshold itself.
**Completing closes are therefore always exactly on a threshold**, hence always
on the tick grid.

## 4. Completion geometry

With `edge` = the crossed threshold and `d = +1` (up) / `-1` (down):

```
fakeOpen = edge - openOffset * d          <- phantom open, N ticks BEHIND the break

closing bar:  high  = edge if maxExceeded else running high
              low   = edge if minExceeded else running low
              close = ha_close(storedOpen, high, low, edge)
              volume added = 0                      <- see §5.1

next bar:     open   = (fakeOpen + closing bar's stored close) / 2   <- HA open
              high   = edge     if up else fakeOpen
              low    = fakeOpen if up else edge
              close  = ha_close(open, high, low, edge)
              barMax = edge + (trend    if d > 0 else reversal)
              barMin = edge - (reversal if d > 0 else trend)
              volume added = the breakout tick's volume
```

Each new bar therefore *starts* already spanning `openOffset` ticks. That is
where the "waves" in the name come from: in a with-trend up run, bar *i+1* spans
`[edge_i - N, edge_i + N/2]` — **180 ticks of high/low range at Wave 120 while
advancing only 60 ticks net**, so consecutive bars overlap by 3:1 and the chart
reads as long overlapping swells rather than a staircase.

### 4.1 The output is Heikin-Ashi, and that is faithful

```
ha_close(o,h,l,c) = (o + h + l + c) / 4
ha_open(fo, prevClose) = (fo + prevClose) / 2
```

High and low are the **real** extremes (unioned with the phantom open). Breakout
detection runs on **raw** tick prices; only the emitted OHLC is transformed. A
strategy reading `bars.close` sees a synthetic value — but it sees exactly what
the same strategy sees in NT8, which is the point.

**Fills are unaffected in this repo**: the engine resolves orders on real ticks
via each bar's `[i0, i1)` span, so the HA smoothing never reaches the fill model.
That property is load-bearing for the HiLoRider evaluation — see
`../HiLoRider.md` §4.

### 4.2 Tick rounding is inside the loop

Wave Bars never calls `Math.Round` itself, but it reads its own state back out
of NT8 (`bars.GetOpen`, `GetClose`, `GetHigh`, `GetLow`), and **NT8 stores every
bar price on the instrument's tick grid, rounded half-to-even** (.NET
`Math.Round`'s default `MidpointRounding.ToEven`). So the rounding is inside the
state loop exactly as it is for TBars: the next bar's HA open reads a *rounded*
close, and each tick's HA close reads a *rounded* open.

This was worth ~79 points of OHLC parity for TBars (`research/TBars_spec.md`
§3.2: 0.1% without it, 79.3% with; half-up 71.6%, half-down 72.7%). The port must
reuse the same `rnd()` helper. Only the two HA averages need it — high, low, the
clamped threshold and the phantom open are already on the grid.

### 4.3 `GetPercentComplete`

Cosmetic (chart progress indicator), reads `bars.LastPrice` against the
with-trend edge. No effect on bar geometry; not ported.

---

## 5. The four behavioural differences from TBars

### 5.1 Volume is NOT double-counted — Wave Bars already matches this repo

TBars calls `UpdateBar(..., volume)` for the completing tick **and**
`AddBar(..., volume)` for the bar it opens, so that tick's volume lands in
*both* bars and NT8's bar volumes exceed traded volume. The 2026-08-04 volume
gate confirmed it measurably: 97.8% of bars satisfied
`nt8_vol == our_vol + volume[breakout tick]`, and NT8's bar volumes summed
**2,292 contracts more** than were actually traded (`TBars_spec.md` §8.1).

Wave Bars passes **`0`** to `UpdateBar` on the completing tick
(`WaveBarsType.cs:141`) and the real volume to `AddBar`
(`WaveBarsType.cs:149`). The breakout tick's volume goes to the **new bar only**
— which is precisely the non-overlapping `[i0, i1)` convention this repo already
enforces for renko/saber/tbars.

**Consequence:** `research/TBars_spec.md` §4's "deliberate divergence #1"
disappears. On Wave Bars the port is not diverging from the platform at all, and
the volume gate becomes a clean equality test rather than a
plus-the-breakout-tick test. Expect `sum(bar volume) == traded volume` minus only
the bar still forming when data ends (TBars residual: −0.086%).

### 5.2 The seed band is symmetric — the inverted-band bug does not exist

TBars re-seeds with `bar_max/bar_min = open ± trend * dir`, carrying
`barDirection` across the reset. With `dir = -1` that is **inverted**
(`bar_max < bar_min`), both tests pass at once, `maxExceeded` wins, and NT8 emits
a bar whose open sits above its own high. Hand-verified at N=4:
`O=98.00 H=97.50 L=97.50 C=97.50`. This is why `build_tbar_bars` carries a
`reset_carries_dir=False` default that deliberately refuses parity, and why
`NinjaScript/gbTBars/` exists at all — fixing it on the NT8 side dropped
geometry mismatches from 24 to 4 (`TBars_spec.md` §5.2, §8.2).

Wave Bars seeds:

```csharp
barMax = open + trendOffset;
barMin = open - trendOffset;      // WaveBarsType.cs:108-109
```

No direction, symmetric, always `barMax > barMin`. **The bug is fixed at
source.** There is no `reset_carries_dir` analogue to implement and no malformed
bar can ever reach an indicator.

Two real geometry consequences follow, both worth stating because they are not
just bug-absence:

1. **No seed doji stub.** TBars' seed collapses `bar_max == bar_min == open`
   (`dir` is 0 at a true first bar), so the next differing tick immediately
   completes a zero-range bar — one stub per fresh start and per reset, ~9% of
   all bars on MGC at 1.8 gaps/day (`TBars_spec.md` §5.3). Wave Bars' seed band
   is `2 * trend` wide, so the first bar after a reset is a **real bar**.
2. **A reversal immediately after a reset is 4x cheap.** The symmetric seed means
   the first bar after a reset can complete at `trend` distance *in either
   direction*, where mid-run a counter-trend completion needs `reversal = 4 x
   trend`. This is a genuine asymmetry-suspension for exactly one bar per reset,
   and it is faithful — not something to "fix".

### 5.3 `RecoverBand()` — state recovery with no TBars equivalent

TBars has no notion of resuming: if the BarsType instance's in-memory band is
empty but the `Bars` series already holds bars, TBars just carries on with
`barMax = barMin = 0` semantics. Wave Bars guards this with an `isStateSeeded`
flag and reverse-engineers the band from the last stored bar
(`WaveBarsType.cs:165-194`):

```
fakeOpen = 2 * GetOpen(idx) - GetClose(idx-1)      // inverts the HA-open formula
edge     = fakeOpen + openOffset * direction        // recovers the birth threshold
max/min  = edge +/- (trend | reversal) per direction
```

It validates the candidate two ways — the bar must have been *born at that edge*
(`high >= edge` for up / `low <= edge` for down) and must not already violate the
candidate band — tries the likely direction first (`close >= open` → up), then
the opposite, and on failure falls back to a symmetric `±trend` band around the
last bar's **open**.

**When it runs:** only when NT8 attaches the bar type to a series that already
has bars — a reload from NT8's bar cache, re-adding the study, a data-series
rebuild. **It never runs in this port**, which always builds forward from raw
ticks. It is documented here for two reasons: it is the mechanism that makes a
live NT8 Wave chart survive a reload without re-anchoring (a real robustness
advantage over TBars), and its fallback anchors on an *HA* open, which is a
synthetic value and generally off the tick grid — so a recovered band can be
very slightly off-grid on the NT8 side. Not portable, not worth chasing.

### 5.4 Reset trigger — identical to TBars, and the same caveat applies

```csharp
if (bars.Count == 0 || (bars.IsResetOnNewTradingDay && isNewSession))
```

`Bars.IsResetOnNewTradingDay` forwards to the **Data Series "Break at EOD"**
toggle, which **defaults to ON** (user-confirmed 2026-08-04). So the reset branch
*is* live on a stock chart, and its reset *point* is the trading-hours template
boundary. The forming bar is **not closed** at a reset — Wave Bars just `AddBar`s
a fresh one, so the forming bar is completed by abandonment, keeping the HA close
it last held.

The port will reset on a genuine trade gap `> RENKO_RESET_GAP_NS` (30 min),
identical to renko/saber/tbars — which for this repo's data is the real CME halt.
The reset-*point* difference (template boundary vs. trade gap) is the residual
that `TBars_spec.md` §8.2 established is **largely benign once neither side emits
a malformed bar** — and per §5.2 above, Wave Bars never does. That is the main
reason to expect better parity than TBars achieved.

Day files are ET calendar days but an overnight session runs straight through
midnight ET with no real gap, so `Catalog.load_bars_sequence` must thread carry
state across file boundaries — the same hazard, and the same fix, as the
2026-07-11 renko day-boundary bug.

---

## 6. Port plan

### 6.1 Shape of the change

The hot loop is ~100 lines and is **certified** for TBars by a parity gate. It
must not be perturbed. So: extract the existing loop from `build_tbar_bars` into
a private `_build_tbar_family_core(...)` taking
`(trend_off, rev_off, open_off, seed_symmetric, reset_carries_dir)`, and have two
thin public builders call it:

- `build_tbar_bars(...)` — unchanged signature and behaviour
  (`seed_symmetric=False`)
- `build_wave_bars(day, wave_ticks, tick_size, carry=None)` — new
  (`seed_symmetric=True`, no `reset_carries_dir` parameter at all)

The refactor is guarded by a golden-output test asserting `build_tbar_bars` is
byte-identical before and after (bit-identical re-runs are load-bearing in this
repo).

The carry tuple shape is already exactly right:
`(bar_open, bar_max, bar_min, bar_dir, run_hi, run_lo, volume, buy_volume,
sell_volume)`.

### 6.2 Plumbing checklist

1. `strategy.py::BarSpec` — add `kind="wave"`, field `wave_ticks`, `key` →
   `f"w{wave_ticks}"`.
2. `strategy.py::parse_barspec` — grammar **`w<N>`** (e.g. `w120`). No collision
   with `tb\d+` / `s\d+-\d+` / `r\d+` / `\d+t` / `\d+[smh]`. Accept `N >= 1`
   (the `max(1, N//2)` clamp makes `w1` legal, unlike `tb1`); update the
   error-message example list.
3. `data.py::build_bars` — dispatch `spec.kind == "wave"`.
4. `data.py::Catalog.load_bars_sequence` — add `"wave"` to the tuple-carry kinds
   alongside `saber` / `tbars`.
5. **`BARS_VERSION` does not need a bump.** `w120` is a new cache key and cannot
   collide with a cached `tb120`; the `end_state` tuple shape is unchanged. This
   keeps ~1 GB/symbol of existing cache valid.
6. `nt8config.py` — map `BarsPeriodTypeSerialize == 77077` → `w{Value}`. Note it
   reads **`Value`**, not `BaseBarsPeriodValue`, and there is **no `Value2`** to
   cross-check against, so TBars' `Value == N//2 and Value2 == N*2` assertion has
   no analogue here.
7. `tools/compare_bars.py` — no change expected; it takes `--period` generically.
8. Docstring cross-references to this file and `research/TBars_spec.md`.
9. `CLAUDE.md` — a Wave Bars entry in the architecture list once the gate has run.

Estimated size: ~120 lines net of new/refactored engine code (most of it the
extraction), plus tests.

### 6.3 Tests — `tests/test_wave_bars.py`

Hand-computed fills and hand-computed geometry, matching the style already used
in `tests/test_tbars.py`:

- derived offsets from N, including the `max(1, N//2)` clamp at N = 1, 2, 3
- **strict** breakout: a tick exactly on a threshold does not complete the bar
- completing close clamps exactly to the threshold (stays on the tick grid)
- HA open / HA close formulas, and **half-to-even rounding inside the loop**
- 4x reversal asymmetry over a hand-computed up-up-down sequence
- **no seed doji** — assert the first bar after a fresh start / reset is a real
  bar with a `2 * trend` band (the explicit contrast with `tb`)
- **no inverted band, ever** — after a reset following a *down* run, assert
  `bar_max > bar_min` and that no emitted bar has `open`/`close` outside
  `[low, high]`. This is the TBars §5.2 bug and it must not reproduce.
- **reversal-after-reset costs `trend`, not `reversal`** (§5.2 consequence 2)
- spans `[i0, i1)` contiguous and non-overlapping; breakout tick belongs to the
  **new** bar
- **volume is an exact identity**: `sum(bar volume) == traded volume` minus only
  the still-forming bar (this is the §5.1 improvement, and it is a strictly
  stronger assertion than TBars can make)
- carry across a simulated day-file split reproduces the continuous build
  bit-for-bit
- `parse_barspec("w120")`

### 6.4 Parity gate — needed from Khahn/the user before any strategy work

Export a real NT8 **Wave 120** chart with `tools/gbBarExporter.cs` and run:

```bash
.venv\Scripts\python tools\compare_bars.py "<export>.csv" --symbol MNQ --period w120 --tolerance-s 10
```

Requirements for the export to be usable:

- MNQ (and separately MGC) — the two instruments HiLoRider claims validation on
- **record whether "Break at EOD" is ON or OFF** on the data series; it gates the
  reset branch entirely
- a window with clean tick data, ≥ 2000 bars, contract stated (MNQ 09-26 style)

**Prediction, stated so the gate can falsify it.** TBars landed at timing 99.1%,
high/low 98.7%, close 91.5%, full OHLC 79.3%, with two characterised residuals:
(a) ±1-tick propagation on the two HA averages, and (b) 29 session-boundary bars
caused by the inverted re-seed. Wave Bars removes (b) at source (§5.2), so full
OHLC should land **~85–90%** with high/low **~99.5%+** and the only remaining
residual being (a). If full OHLC comes back near 79% *with* mismatches clustered
at session boundaries, then either the export had Break at EOD ON with a
template boundary far from any trade gap, or an assumption in §5.2 is wrong —
investigate before trusting the bar type.

### 6.5 What the port measured on landing (2026-08-11)

Smoke run over 27 real sessions (2026-07-01 → 07-31), `w120` vs `tb120`, through
`Catalog.load_bars_sequence` so the cache and cross-day carry are exercised:

| | days | bars | bars/day | zero-range bars | invalid OHLC |
|---|---|---|---|---|---|
| MNQ `w120` | 27 | 4,566 | 169.1 | **1 (0.02%)** | 0 |
| MNQ `tb120` | 27 | 4,569 | 169.2 | 24 (0.5%) | 0 |
| MGC `w120` | 27 | 532 | 19.7 | **2 (0.4%)** | 0 |
| MGC `tb120` | 27 | 546 | 20.2 | 36 (**6.6%**) | 0 |

**§5.2 confirmed on real data.** The seed doji stubs are gone: MGC drops from
6.6% of all bars to 0.4%, MNQ from 0.5% to 0.02%, and the handful left are
genuine zero-range bars rather than seed artifacts. Zero invalid-OHLC bars, as
§5.2 predicts by construction.

**The bars/day question is answered, and it was not the stubs.** MGC `w120` runs
**19.7 bars/day**, squarely in line with `TBars_spec.md` §1's ≈21 and *nothing
like* the ≈48/day implied by HiLoRider's own "12,132 MGC bars, Aug 2025–Aug
2026" note. MNQ runs 169/day vs the ≈119/day implied by their "29,868 bars".
Both of their figures are off, in opposite directions, so whatever produced
their bar files was not this bar type at this setting on this data. Worth
knowing before comparing any strategy result against theirs.

**A pre-existing family-wide gap, surfaced by the volume check (NOT introduced
here).** Bar volumes sum to 99.42% of traded volume on MNQ. The missing 374,313
contracts are **exactly** the bar still forming at the end of a day file that is
followed by a genuine gap — verified to the contract. The cause: at an
*intra-file* gap the builder emits the abandoned forming bar, but when
`_load_sequence_carry` drops the carry at an *inter-file* gap, the previous
file's forming bar is discarded instead of emitted.

**Frequency: once per WEEKEND, not once per day** (corrected 2026-08-11 — an
earlier revision of this section said ≈1 bar/day, which was wrong by 5x). Day
files are ET calendar days, so on a weekday the 17:00–18:00 halt sits *inside*
the file: it is an intra-file gap and the abandoned bar IS emitted. Confirmed on
the data — weekday files show `max intra-day gap = 60.0 min`. A Friday file
instead just *ends* at 16:59 ET (`max intra-day gap = 0.1 min`), so its forming
bar is carried out, and then the 2,940-minute weekend gap drops it. The parity
gate saw exactly this: **4 of its 5 unmatched bars are 16:59:59 bars on the
three Fridays in the window plus the end of data** (§7).

`tb120` reports the identical figure to four decimal places (99.4234%), and
`s64-16` has the same shape (0.037%), so this is shared TBars-family behaviour
that predates this change — the `build_tbar_bars` golden check (§6.6) proves the
refactor altered nothing. **Not fixed here**, because fixing it would add a bar
to `build_tbar_bars` output and invalidate the certified 2026-08-04 TBars parity
numbers and every existing saber/tbars run. Flagging it as the user's call; at
~1 bar per weekend it costs ~0.2% of bars, and NT8 (Break at EOD ON) *does* show
that bar.

### 6.6 Verification done

- **`build_tbar_bars` is bit-identical across the `_build_tbar_family_core`
  extraction** — the pre-refactor `data.py` was pulled straight out of git and
  compared array-by-array (all 10 columns plus `end_state`) on **840 randomised
  cases**: 40 seeds × {N=4, 5, 120, 2, 37} × {tick 0.25, 0.10} × {0, 3 gaps} ×
  both `reset_carries_dir` modes, plus 40 carried-in split-day cases. Zero
  differences. This was the stated precondition for touching the certified loop.
- `tests/test_wave_bars.py` — 19 tests covering all three §5 divergences,
  hand-computed geometry, the strict breakout, the tick grid, span contiguity,
  the exact volume identity, the `>= 1` tick clamp, cross-day carry vs a
  continuous build, and the grammar. Full suite: **238 passed**.

### 6.7 Sequencing

Bars first, strategy second. Both are now clear: the port is in and the parity
gate has run (§7). Stage 2b (the Heikin-Ashi entry-subsidy measurement) needed
only bars, so it is also done — see `../HiLoRider.md` §4.2. `../HiLoRider.md` §6
Stage 2 is unblocked.

## 7. Parity gate — run 2026-08-11, MNQ Wave 120

Export: `bars_MNQ_Wave_120.csv` (this folder), an MNQ / Wave / 120 chart,
2026-07-19 18:00 → 2026-08-11 18:04 ET, 3,059 bars. Clipped to
**2026-08-07 17:00 ET** (2,902 bars) because the repo's MNQ parquet ends with the
`20260807` file — the 157 dropped bars are past our data, not a mismatch.

```bash
python tools\compare_bars.py "<clipped>.csv" --symbol MNQ --period w120 --tolerance-s 10
```

| metric | **Wave 120** | TBars 120 (2026-08-04) |
|---|---|---|
| matched by close time | **2897 / 2902 (99.8%)** | 99.1% |
| identical **high/low** | **2896 / 2897 (100.0%)** | 98.7% |
| identical open | 2492 / 2897 (86.0%) | ~86.8% |
| identical close | 2663 / 2897 (91.9%) | 91.5% |
| identical full OHLC | 2300 / 2897 (79.4%) | 79.3% |
| every field within ±1 tick | **2894 / 2897 (99.9%)** | — |

**What this certifies.** The geometry is right, and more cleanly than TBars ever
managed: **low is exact on literally every matched bar (2897/2897)** and high on
all but one. Thresholds, the strict breakout, the exact-threshold clamp, the
phantom-open offset, the 4x asymmetry, and bar timing all reproduce.

Bar 1 was verified by hand against the export, to the cent — and it is the
**§5.2 fix visible in NT8's own output**:

```
seed 28747.5, symmetric band [28732.5, 28762.5]   (open +/- trend = 60 ticks)
NT8 high  = 28762.5  == the upper threshold exactly            <- clamped
ha_close  = (28747.5 + 28762.5 + 28744.75 + 28762.5)/4 = 28754.3125
          -> half-to-even on the 0.25 grid = 28754.25 == NT8 close
```

TBars' export began with a **seed doji at the same 28747.5**; Wave's first bar is
a real bar. Same instrument, same window, same seed price, different seed
geometry — exactly as §5.2 predicts.

**Residual (b) is eliminated.** TBars had 24–29 genuinely-wrong bars clustered at
the 18:00 ET reopen (the inverted re-seed). Here the reopen is no longer special
at all: OHLC-mismatch rate **19.8% inside the 18:00–18:15 window vs 20.6%
everywhere else**, and **zero reopen bars have a high/low mismatch**. The reset
*point* difference (trading-hours template vs. this port's >30 min trade gap)
is confirmed benign once neither side emits a malformed bar, as
`research/TBars_spec.md` §8.2 concluded.

**What remains is only residual (a).** ±1 tick on the two Heikin-Ashi averages:
open off by ±1 on 13.8% of bars (−1: 224, +1: 178), close on 8.0% (−1: 110,
+1: 122), near-symmetric in both directions — error *propagation*, not a wrong
tie-break. Essentially unchanged from TBars (open 13.2%, close 7.5%), as expected
since none of the three Wave differences touches the HA math.

**A prediction I got wrong.** §6.4 predicted full OHLC would land at **85–90%**;
it came in at **79.4%**. The reasoning error: residual (b) was only ~1.3% of
TBars' bars, so removing it could only ever lift the total by about a point —
the dominant term is residual (a) at ~14% + ~8%, which the fix does not address.
A realistic ceiling was ~80.6%. The *headline* number is therefore flat versus
TBars while the *geometry* number went 98.7% → 100.0%, which is the upgrade that
actually matters: TBars' 79.3% contained wrong bars, Wave's 79.4% contains only
rounding.

**The 8 imperfect bars, accounted for.**

- **5 unmatched.** Four are `16:59:59` bars — 2026-07-24, 07-31 and 08-07 (all
  **Fridays**) plus the end of data: the once-per-weekend forming-bar drop
  documented in §6.5, showing up exactly where that analysis says it should.
  The fifth is 07-29, whose file reports a 50.7-minute halt gap instead of the
  usual 60.0, shifting the boundary bar — a data-quality artifact on that date.
- **3 bars off by more than ±1 tick** (0.10%), all on **2026-07-31 between
  16:02 and 16:53 ET** (dH +115, dC +27, dO −6/+14). Localised to one afternoon
  on the same date that also produced two of the unmatched bars, so this is tick
  data, not geometry. Compare `TBars_spec.md` §8.2, where the 07-24 tick data had
  to be repaired before the vendor gate would run clean; 07-31 looks like the
  same class of problem and is worth a re-export if tighter parity is ever
  wanted.

**Judgement.** Geometry and timing certified for `w120`; safe to build strategy
work on. Not byte-perfect, and the reason is understood and bounded: ±1-tick HA
rounding propagation on ~14% of opens and ~8% of closes. Fills in this engine
resolve on real ticks via the `[i0, i1)` spans and never touch the HA values, so
this residual cannot reach the fill model — it affects only what an indicator
reading `bars.close` sees, and it affects NT8's own chart identically half the
time.

### 7.1 Second instrument — MGC Wave 120, run 2026-08-11

Export: `bars_MGC_Wave_120.csv` (this folder), 2026-07-05 18:00 → 2026-08-07
13:00 ET, 589 bars. **Usable window is 2026-07-05 18:00 → 2026-07-27 17:00 ET
(344 bars)**, because the repo's recorded MGC contract expires out from under the
comparison — exactly the hazard `research/TBars_spec.md` §9 flagged. Trade counts
per day collapse: ~155–260k through 07-28, then **4,181 on 07-30, 155 on 07-31,
and ~20–100/day after**. Independently, the export's price level jumps **+59.0 at
07-27 18:22 on 352 contracts** and its 07-28 session trades 4082–4107 while our
07-28 data trades 4009–4056 — i.e. from 07-27 evening the two are on different
contract months.

**The clip is not a favourable-window choice, and the numbers prove it.** Re-run
at successively later cuts and the *absolute count of correct bars never moves*:

| cut (ET) | NT8 bars | matched | high/low exact | full OHLC exact |
|---|---|---|---|---|
| **07-27 17:00** | 344 | 341 | **341** | **267** |
| 07-28 17:00 | 359 | 348 | 341 | 267 |
| 07-30 17:00 | 432 | 370 | 341 | 267 |
| none (08-07) | 589 | 371 | 341 | 267 |

Every bar past 07-27 17:00 is a mismatch; only the denominator grows. So the
boundary is where the data stops overlapping, not where the score peaks.

| metric | **MGC Wave 120** | MNQ Wave 120 |
|---|---|---|
| matched by close time | **341 / 344 (99.1%)** | 2897 / 2902 (99.8%) |
| identical **high/low** | **341 / 341 (100.0%)** | 2896 / 2897 (100.0%) |
| identical open | 286 / 341 (83.9%) | 2492 / 2897 (86.0%) |
| identical close | 316 / 341 (92.7%) | 2663 / 2897 (91.9%) |
| identical full OHLC | 267 / 341 (78.3%) | 2300 / 2897 (79.4%) |
| every field within ±1 tick | **341 / 341 (100.0%)** | 2894 / 2897 (99.9%) |

**MGC is the cleaner of the two gates.** High/low is exact on *every* matched
bar with no exceptions, and **not one bar is off by more than a single tick** —
where MNQ had 3, all attributable to the 2026-07-31 tick-data anomaly. The
delta distribution contains literally nothing but 0 and ±1: open −1 on 8.8% /
+1 on 7.3%, close −1 on 2.9% / +1 on 4.4%, near-symmetric. That is residual (a)
and nothing else.

**Residual (b) confirmed dead on a second instrument.** In the 18:00–18:15 ET
reopen window, 3 of 25 bars mismatch (12.0%) versus 22.5% everywhere else — the
reopen is *better* than average here — and **zero reopen bars have a high/low
mismatch**.

**The weekend forming-bar drop, confirmed again and exactly.** All 3 unmatched
bars are `16:59:5x` bars on **2026-07-10, 07-17 and 07-24 — the three Fridays in
the window**, and nothing else. That is §6.5's once-per-weekend mechanism
reproducing on a second instrument with no false positives.

Bars/day over the certified window: **344 bars / 16 sessions = 21.5/day**, which
matches `research/TBars_spec.md` §1's ≈21 for MGC Speed 120 closely, and confirms
§6.5's finding that HiLoRider's own ≈48/day figure is not this bar type at this
setting.

**To certify MGC beyond 07-27** the repo needs a later MGC contract recorded (per
`TBars_spec.md` §9, contract **MGC 08-26** rather than the 12-26 on the Khahn
workspace), or a re-export restricted to the contract we hold. Not blocking:
344 bars with 100% geometry is sufficient to trust `w120` on MGC.
