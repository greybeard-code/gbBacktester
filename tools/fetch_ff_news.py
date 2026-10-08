"""Gather ForexFactory high-impact ("red folder") economic events for the
backtester's historical news filter.

Two data sources, both stdlib-only (no browser, no login, no pandas):

  * feed  — https://nfs.faireconomy.media/ff_calendar_thisweek.json
            ForexFactory's own machine-readable weekly JSON. Current week only.
            Used by default (the weekly-cron path).
  * page  — https://www.forexfactory.com/calendar?month=<mon>.<year>
            The calendar page embeds every event as JS (window.
            calendarComponentStates[1] = {...}); the nested event objects are
            valid JSON, so we bracket-match the `days:` array and parse it.
            Used for arbitrary historical ranges (--start/--end). Needs a
            browser User-Agent (WebFetch/datacenter fetchers get a Cloudflare
            403; plain urllib from a normal IP is served fine).

Every event carries `dateline`, a UTC epoch — timezone-independent, so a
ForexFactory account / timezone setting is irrelevant. We emit both UTC and
US/Eastern (the backtester's user-facing tz) columns.

Usage
-----
    # weekly cron: append the current week's red-folder events
    python tools/fetch_ff_news.py

    # one-time / longer backfill over a date range (inclusive, ET calendar)
    python tools/fetch_ff_news.py --start 2024-12-01 --end 2026-07-31

    # keep medium-impact too, or restrict to certain currencies
    python tools/fetch_ff_news.py --impact high,medium --currency USD,EUR

Output merges into --out (default data/ff_high_impact_news.csv): existing rows
are kept, new ones added, the whole file de-duped on (dateline,currency,event)
and re-sorted by time. So a weekly run accumulates history; a range run
backfills it.
"""
from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
          "jul", "aug", "sep", "oct", "nov", "dec"]

# ForexFactory impact colour -> name. Red folder = High.
_CLASS_TO_IMPACT = {"red": "high", "ora": "medium", "yel": "low", "gra": "holiday"}
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "data" / "ff_high_impact_news.csv"


def _get(url: str, tries: int = 3) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,application/json",
        "Accept-Language": "en-US,en;q=0.9"})
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception as e:                      # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET failed after {tries} tries: {url}\n  {last}")


def _extract_json_array(html: str, key: str) -> str:
    """Return the substring of the `key: [...]` array from the embedded state.

    The top-level state object uses unquoted keys (JS literal), but the nested
    day/event objects are quoted (valid JSON) — so once we isolate the array
    value it parses directly. Bracket-match, honouring string literals.
    """
    marker = "window.calendarComponentStates[1] = "
    i = html.find(marker)
    if i < 0:
        raise ValueError("calendar state not found (page layout changed?)")
    k = html.find(key + ":", i)
    if k < 0:
        raise ValueError(f"{key!r} not found in calendar state")
    start = html.find("[", k)
    depth, in_str, esc = 0, False, ""
    for j in range(start, len(html)):
        c = html[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == in_str:
                in_str = False
        else:
            if c in "\"'":
                in_str = c
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return html[start:j + 1]
    raise ValueError(f"unterminated {key} array")


def _from_page(html: str) -> list[dict]:
    days = json.loads(_extract_json_array(html, "days"))
    out = []
    for d in days:
        for e in d.get("events", []):
            cls = str(e.get("impactClass", "")).rsplit("-", 1)[-1]  # 'red'/'yel'
            out.append({
                "dateline": int(e["dateline"]),
                "currency": e.get("currency", ""),
                "event": e.get("name", ""),
                "impact": _CLASS_TO_IMPACT.get(cls, cls),
            })
    return out


def _from_feed(raw: bytes) -> list[dict]:
    out = []
    for e in json.loads(raw):
        # date is ISO-8601 with an explicit offset, e.g. ...T10:00:00-04:00
        dt = datetime.fromisoformat(e["date"])
        out.append({
            "dateline": int(dt.timestamp()),
            "currency": e.get("country", ""),   # feed calls the currency 'country'
            "event": e.get("title", ""),
            "impact": str(e.get("impact", "")).lower(),
        })
    return out


def _month_iter(start: datetime, end: datetime):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield f"{MONTHS[m - 1]}.{y}"
        m += 1
        if m > 12:
            m, y = 1, y + 1


def gather(start: datetime | None, end: datetime | None, source: str) -> list[dict]:
    if source == "feed":
        # opt-in: ForexFactory's own weekly JSON feed (current week only).
        return _from_feed(_get(FEED_URL))
    # default/auto and page: scrape calendar month pages. With no date range
    # this is the current month (the weekly-cron path) — deterministic and
    # complete, where the feed is only the current week and occasionally flaky.
    if start is None:
        start = datetime.now(ET).replace(day=1)
    if end is None:
        end = datetime.now(ET)
    events: list[dict] = []
    months = list(_month_iter(start, end))
    for n, mo in enumerate(months):
        url = f"https://www.forexfactory.com/calendar?month={mo}"
        html = _get(url).decode("utf-8", "replace")
        got = _from_page(html)
        events.extend(got)
        print(f"  [{n + 1}/{len(months)}] {mo}: {len(got)} events")
        if n + 1 < len(months):
            time.sleep(0.4)                         # be polite
    return events


def _row(e: dict) -> dict:
    u = datetime.fromtimestamp(e["dateline"], timezone.utc)
    l = u.astimezone(ET)
    return {
        "dateline_utc_epoch": e["dateline"],
        "datetime_utc": u.strftime("%Y-%m-%d %H:%M:%S"),
        "datetime_et": l.strftime("%Y-%m-%d %H:%M:%S"),
        "weekday_et": l.strftime("%a"),
        "currency": e["currency"],
        "event": e["event"],
        "impact": e["impact"],
    }


FIELDS = ["dateline_utc_epoch", "datetime_utc", "datetime_et", "weekday_et",
          "currency", "event", "impact"]


def merge_csv(out_path: Path, events: list[dict]) -> tuple[int, int]:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows: dict[tuple, dict] = {}
    if out_path.exists():
        with open(out_path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows[(int(r["dateline_utc_epoch"]), r["currency"], r["event"])] = r
    before = len(rows)
    for e in events:
        r = _row(e)
        rows[(r["dateline_utc_epoch"], r["currency"], r["event"])] = {
            k: str(v) for k, v in r.items()}
    ordered = sorted(rows.values(), key=lambda r: int(r["dateline_utc_epoch"]))
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(ordered)
    return before, len(ordered)


def _parse_date(s: str) -> datetime:
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=ET)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"bad date {s!r} (use YYYY-MM-DD)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=_parse_date, help="first day, YYYY-MM-DD (ET)")
    ap.add_argument("--end", type=_parse_date, help="last day, YYYY-MM-DD (ET)")
    ap.add_argument("--impact", default="high",
                    help="comma list of impacts to keep: high,medium,low,holiday "
                         "(default high = red folder)")
    ap.add_argument("--currency", default=None,
                    help="comma list to keep, e.g. USD,EUR (default: all)")
    ap.add_argument("--source", choices=("auto", "feed", "page"), default="auto",
                    help="auto (feed for current week, page for a range), or force")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"CSV to merge into (default {DEFAULT_OUT})")
    args = ap.parse_args()

    keep_impact = {s.strip().lower() for s in args.impact.split(",") if s.strip()}
    keep_cur = ({s.strip().upper() for s in args.currency.split(",")}
                if args.currency else None)

    mode = ("range " + args.start.strftime("%Y-%m-%d") + ".."
            + (args.end or datetime.now(ET)).strftime("%Y-%m-%d")
            if args.start else
            ("weekly (current week feed)" if args.source == "feed"
             else "weekly (current month)"))
    print(f"Gathering ForexFactory events — {mode}, impact={sorted(keep_impact)}"
          + (f", currency={sorted(keep_cur)}" if keep_cur else ""))

    events = gather(args.start, args.end, args.source)
    # filter impact / currency / date window
    kept = []
    lo = int(args.start.timestamp()) if args.start else None
    hi = int((args.end.replace(hour=23, minute=59, second=59)).timestamp()) \
        if args.end else None
    for e in events:
        if e["impact"] not in keep_impact:
            continue
        if keep_cur and e["currency"].upper() not in keep_cur:
            continue
        if lo is not None and e["dateline"] < lo:
            continue
        if hi is not None and e["dateline"] > hi:
            continue
        kept.append(e)

    before, after = merge_csv(args.out, kept)
    print(f"Kept {len(kept)} events ({len(set((e['dateline'], e['currency'], e['event']) for e in kept))} unique). "
          f"CSV {args.out}: {before} -> {after} rows (+{after - before} new).")


if __name__ == "__main__":
    main()
