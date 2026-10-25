# slippage_study: bid/ask spread by 30-minute window, and what to enter as slippage elsewhere

> **gbBacktester does not need slippage to account for the spread.** Its fill model already
> fills a market order at the opposite quote (buys at the ask, sells at the bid), so the spread is
> paid on every market fill. Do not add this study's numbers to `--slippage` here.
>
> **Other backtesters do.** NinjaTrader 8's Strategy Analyzer and most bar-based tools fill at the
> last or bar price and pay no spread, so their slippage setting has to stand in for it. That is
> what this study is for: it measures the spread from tick data and turns it into slippage
> settings for those tools.

## Summary

Source: 2026 L1 tick data (bid, ask and last events), 14 futures symbols, about 3,500 symbol-days.
For every symbol, day and 30-minute ET window it records the spread in ticks, two ways:

- **trade-weighted**: the spread prevailing at each trade, which is what a market order meets;
- **time-weighted**: the spread held from each quote change to the next.

Exact tick histograms are kept per window, so any percentile can be pooled correctly (by symbol,
window, month, weekday). Averaging per-day percentiles would be wrong.

What it found:

- **Time of day.** The 18:00 ET reopen is the widest window for every symbol except ZN (ZN sits at
  one tick all day). The next widest are 18:30 to 20:00 and 07:00 ET. The tightest are midday to
  mid-afternoon. A flat all-hours value understates the reopen by about 2x.
- **Month.** Spread follows volatility, not the calendar: month by month it tracks the average
  price range at 0.85 to 0.96 correlation on ES, NQ, MNQ, RTY, GC, MGC, SI and CL. Metals show the
  biggest swing: silver's spread in October was about 4.6x tighter than in February.
- **Weekday.** Equities are flat (within a few percent). Metals are wider on Monday and Friday and
  tighter on Wednesday (about 5 to 11%). Energy is widest on Monday and tightest on Friday.
- **So** if slippage should vary, key it to the session and to recent volatility. Calendar month and
  weekday add little.

**Heatmaps:** [results/trends.html](results/trends.html) is a self-contained interactive page
(plotly, works offline): spread by time of day, month and weekday for all 14 symbols, plus
month-by-time and weekday-by-time views with a symbol picker. GitHub shows HTML files as source, so
download it and open it in a browser. Regenerate it with `trends.py` (below).

## Recommended slippage for last-price backtesters (NT8 Strategy Analyzer and similar)

Ticks **per side** (apply to the entry and again to the exit):

- **typical** = ceil(mean spread / 2), the half spread a market order crosses on average;
- **conservative** = p90 of the spread, the "upper end of the typical bid/ask spread" rule.

| Symbol | RTH 09:30-16:00 typical / conservative | Reopen 18:00-20:00 typical / conservative | Overnight 20:00-09:30 typical / conservative | Round trip $, RTH (typical / conservative) | Round trip $, overnight (typical / conservative) |
|---|---|---|---|---|---|
| ES / MES | 1 / 1 | 1 / 2 | 1 / 1 | $25 / $25 (ES); $2.50 / $2.50 (MES) | $25 / $25 (ES); $2.50 / $2.50 (MES) |
| MNQ | 1 / 3 | 2 / 4 | 2 / 3 | $1 / $3 | $2 / $3 |
| NQ (provisional) | 2 / 4 | 3 / 9 | 2 / 6 | $20 / $40 | $20 / $60 |
| YM / MYM | 1 / 3 and 1 / 2 | 2 / 6 and 2 / 7 | 2 / 3 and 2 / 3 | $10 / $30 (YM); $1 / $2 (MYM) | $20 / $30 (YM); $2 / $3 (MYM) |
| RTY / M2K | 1 / 2 | 2 / 5 | 1 / 3 and 1 / 2 | $10 / $20 (RTY); $1 / $2 (M2K) | $10 / $30 (RTY); $1 / $2 (M2K) |
| GC / MGC | 2 / 7 and 2 / 4 | 4 / 12 and 2 / 7 | 3 / 7 and 2 / 5 | $40 / $140 (GC); $4 / $8 (MGC) | $60 / $140 (GC); $4 / $10 (MGC) |
| SI | 3 / 8 | 4 / 13 | 3 / 9 | $150 / $400 | $150 / $450 |
| CL / MCL | 1 / 2 | 2 / 6 | 1 / 2 | $20 / $40 (CL); $2 / $4 (MCL) | $20 / $40 (CL); $2 / $4 (MCL) |
| ZN | 1 / 1 | 1 / 1 | 1 / 1 | $31.25 / $31.25 | $31.25 / $31.25 |

`results/slippage_recommendations_2026.csv` has every symbol for four sessions (RTH, reopen,
overnight 20:00-09:30, all hours), with mean, p75, p90 and p95 spread in ticks. The overnight
session is everything between the reopen and the next morning's cash open, so it is the right row for
a strategy that runs through the Asian and London hours.

How to use it:

- Slippage is one constant per run. Pick the row for the hours the strategy trades. If it trades the
  18:00-20:00 reopen, use that row or exclude those hours.
- In NT8 the slippage setting applies to market and stop-market fills. Limit orders fill at their
  price, and filling them on touch is optimistic: a trade *through* the price is the honest
  condition.
- Slippage is whole ticks, so the tightest products round up (ES's half spread is 0.5 tick but must
  be entered as 1). "Typical" is therefore mildly conservative there.
- Use "typical" as the base case and "conservative" as the stress case, and report both.
- These figures cover the spread only. They do not include latency, order size, or stops running
  through the market in a fast move. A strategy that relies on stops should carry extra margin.
- They are quote-based and have not been compared with actual NT8 fills.

## Caveats

- **NQ-family values are provisional upper bounds.** NQ reads about 1.5x wider than MNQ in the same
  market, which looks too large. A suspected cause is a trade printing between the ask update and
  the bid update of a single price move, which shows a transient wide spread. This has **not** been
  tested. Until it is, prefer MNQ's row for NQ. YM, MYM, RTY and M2K may be affected the same way.
- One year of data (2026). Spreads move with volatility, so re-run on your own period.
- Databento-filled days (ZN has 17, M2K one) and the tail of spliced days carry trades with the
  quote before each, but no standalone quote updates, so they are left out of the time-weighted
  figures. Trade-weighted figures use them.
- The engine attaches a trade's quote the same way (the latest bid and ask event before the trade),
  so the cache, the engine and this study agree. On one sample day for MNQ, ES, ZN and SI the
  study's trade counts, valid counts and summed ticks matched the engine's reduced cache exactly.
- This is descriptive market structure, not a backtest: no manifest or trial-ledger entry.

## Method

- Spread is an integer number of ticks, `rint((ask - bid) / tick)`, valid only if bid < ask. Locked
  and crossed quotes are counted separately and never enter a spread.
- Trade-weighted: the quote prevailing at each Last event is the latest bid and ask event before it
  in row order.
- Time-weighted: each state is held to the next quote event, split at 30-minute edges, and capped at
  300 s so a trading halt or a recorder hole cannot credit a stale quote (`tw_ns_stale` records what
  was dropped; about 6% of quote time, mostly the 17:00-18:00 halt).
- Windows are ET wall-clock 30-minute windows (daylight saving handled). `trading_day` names the CME
  day (18:00 ET and later belong to the next date).
- Pooled means and percentiles leave out day-windows with fewer than 30 valid trades.
- Month and weekday cuts use a window-adjusted index: each day-window divided by that symbol's
  pooled mean for the same ET window, so a month that trades more of the wide windows does not look
  wide.

## Run it

Needs the engine's data setup (`BACKTESTER_DATA_ROOT`, see the main README, "Data"), with
`<root>/2026/<SYM>-2026_L1/2026*.parquet`. From the repo root:

```bash
python slippage_study/spread_study.py run --jobs 1   # resumable: one file per symbol-day under out/days/
python slippage_study/summarize.py                   # out/*.parquet and out/*.csv
python slippage_study/recommend.py                   # out/slippage_recommendations_2026.csv
python slippage_study/trends.py                      # out/trends.html (needs plotly)
python -m pytest tests/test_slippage_study.py -q     # data-free tests
```

**Use one worker, or watch memory.** The heaviest day (MNQ, about 37M events) peaks near 5 GB per
worker. Three workers on a 31 GB machine triggered the kernel out-of-memory killer. On Linux, a cap
such as `systemd-run --user --scope -p MemoryMax=7G python ...` makes an overrun kill only the
job. The full run took about 25 minutes with one worker.

## Files

- `spread_study.py`: measures one raw day into per-window arrays; `run` processes all days.
- `summarize.py`: pools the per-day files into the summary, monthly and coverage tables.
- `recommend.py`: turns the pooled histograms into per-session slippage ticks.
- `trends.py`: the time-of-day, month and weekday heatmaps (`out/trends.html`) and `trend_stats.csv`.
  `results/trends.html` is a copy of that page from the 2026 run.
- `results/`: the pooled outputs of the 2026 run (`slippage_recommendations_2026.csv`,
  `spread_30m_summary_2026.csv`, `coverage_2026.csv`, `trends.html`). The per-day tables are large and are rebuilt
  by the commands above (`out/` is gitignored).
- `../tests/test_slippage_study.py`: hand-computed tests, including DST days, the stale-quote cap,
  chunk invariance and pooled percentiles.
