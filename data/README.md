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
