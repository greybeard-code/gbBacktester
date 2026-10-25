# data/ — reference datasets

## ff_high_impact_news.csv — ForexFactory high-impact ("red folder") events

Historical economic-calendar events for the backtester's news filter (e.g.
skipping GZK/Terminator entries around high-impact releases). Produced by
`tools/fetch_ff_news.py`.

**Source:** ForexFactory calendar (`forexfactory.com/calendar`), impact =
**High** = the red-folder icon (`icon--ff-impact-red`). All currencies are
captured; filter to `currency == "USD"` for MNQ/ES index work (~567 of the
rows).

**Schema** (one row per event):

| column | meaning |
|---|---|
| `dateline_utc_epoch` | event time as a UTC epoch (ForexFactory's own `dateline` — timezone-independent, the authoritative field) |
| `datetime_utc` | same instant, `YYYY-MM-DD HH:MM:SS` UTC |
| `datetime_et` | same instant in **US/Eastern** (the backtester's user-facing tz) |
| `weekday_et` | day of week in ET |
| `currency` | affected currency (`USD`, `EUR`, …) |
| `event` | event name (e.g. `Non-Farm Employment Change`, `FOMC Statement`) |
| `impact` | `high` (red folder). `medium`/`low`/`holiday` appear only if gathered with `--impact` |

Times reconcile to known releases: NFP & CPI 08:30 ET, FOMC decision 14:00 ET,
ADP 08:15 ET, ISM 10:00 ET. Rows are de-duped on `(dateline,currency,event)`
and sorted by time. Initial backfill covered 2024-12 → 2026-07 (1,375 events,
matching an independent browser extraction exactly).

### Refreshing

```bash
# weekly (cron): append the current month's red-folder events (idempotent)
.venv/Scripts/python tools/fetch_ff_news.py

# backfill / longer range (inclusive, ET calendar days)
.venv/Scripts/python tools/fetch_ff_news.py --start 2024-12-01 --end 2026-07-31

# also keep medium impact, or restrict currencies
.venv/Scripts/python tools/fetch_ff_news.py --impact high,medium --currency USD,EUR
```

The default (no dates) scrapes the **current month** — deterministic and
complete. `--source feed` uses ForexFactory's lightweight weekly JSON
(`nfs.faireconomy.media/ff_calendar_thisweek.json`, current week only; it can
rate-limit with `429`, hence page is the default). No login or account is
needed — `dateline` is a UTC epoch, so a ForexFactory timezone setting is
irrelevant. The scraper sends a normal browser User-Agent (datacenter
fetchers get a Cloudflare 403; plain `urllib` from a normal IP is served).

**Note:** ForexFactory's terms discourage automated collection. This pulls a
small volume (≈1 request/month) for personal backtesting research only; keep
the cadence light and don't redistribute the data.

> This file is not shipped with the repo. Generate it yourself with
> `python tools/fetch_ff_news.py --start YYYY-MM-DD --end YYYY-MM-DD` and use it
> under ForexFactory's terms.

## roll_calendar.json — contract-roll schedule (built per installation)

The dates the raw day files switch from one futures contract to the next, per
symbol (ES MES NQ MNQ YM MYM RTY M2K GC MGC SI), with the measured jump in ticks
and percent. The engine cancels orders and flattens before each one
(`backtester/rolls.py`, setting `flatten_at_roll`), because the contract changes
at a midnight ET inside the session and a position carried across it would book
the calendar spread as P&L. The file also lists `unexplained` midnight
discontinuities outside any roll window (data defects such as a one-day
wrong-contract file), which the engine warns about and does not flatten on; CL
and MCL are excluded.

Built from your data, not typed in, and not shipped (it depends on your files):
`python3 -m tools.roll_calendar build` (a few minutes), verified with
`... check`, shown with `... show`. Run `build` after each data update; a run
that goes past a symbol's `through` date gets a warning. `.gitignore` excludes
the file.
