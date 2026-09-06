#!/usr/bin/env python3
"""
EconomicCalendar.py — Daily economic calendar reader for NinjaTrader bots.

Fetches today's high-impact USD events from ForexFactory, scores a
directional bias for MNQ/NQ futures, and writes a JSON session-context
file that the C# bots read at session start (State.DataLoaded).

Output:
    ~/Documents/NinjaTrader 8/EconomicCalendar/calendar_YYYY-MM-DD.json
    ~/Documents/NinjaTrader 8/EconomicCalendar/calendar_latest.json

Usage:
    python EconomicCalendar.py                     # today
    python EconomicCalendar.py --date 2026-06-16   # specific date
    python EconomicCalendar.py --verbose            # debug output
    python EconomicCalendar.py --out C:/path/cal.json

Requirements:
    pip install requests beautifulsoup4

Scheduling (run before market open, e.g. 08:00 ET):
    schtasks /create /tn "EconCalendar" /tr "python C:\\...\\EconomicCalendar.py"
             /sc daily /st 08:00
"""

import os
import sys
import json
import re
import argparse
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

# Force UTF-8 output so Unicode characters render on all Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: Missing dependencies.  Run:  pip install requests beautifulsoup4")
    sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════════════

BASE_DIR = Path(os.path.expanduser("~")) / "Documents" / "NinjaTrader 8"
OUT_DIR  = BASE_DIR / "EconomicCalendar"

NEWS_BLOCK_BEFORE = 5   # minutes to pause BEFORE a high-impact event
NEWS_BLOCK_AFTER  = 20  # minutes to resume AFTER a high-impact event

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ══════════════════════════════════════════════════════════════════════════════
# Event catalog
# Each entry: event_name_substring → (equity_bias_when_hot, is_inverse_metric)
#
# equity_bias_when_hot:
#   +1  = actual > forecast → bullish MNQ (strong growth, jobs, confidence)
#   -1  = actual > forecast → bearish MNQ (hot inflation, high unemployment)
#    0  = ambiguous (Fed language-dependent; no score assigned)
#
# is_inverse_metric:
#   True  = a LOWER actual is the "good" outcome (claims, unemployment rate)
#           so actual > forecast → bearish despite the +1/-1 setting above
# ══════════════════════════════════════════════════════════════════════════════

EVENT_CATALOG: dict[str, tuple[int, bool]] = {
    # ── Labor ─────────────────────────────────────────────────────────────────
    "Non-Farm Payrolls":            (+1, False),
    "Nonfarm Payrolls":             (+1, False),
    "ADP Non-Farm":                 (+1, False),
    "ADP Employment":               (+1, False),
    "JOLTS Job Openings":           (+1, False),
    "Employment Change":            (+1, False),
    "Average Hourly Earnings":      (-1, False),  # wage pressure = inflation risk
    "Unemployment Rate":            (-1, True),   # lower is better
    "Initial Jobless Claims":       (-1, True),
    "Continuing Jobless Claims":    (-1, True),

    # ── Inflation  (hot = hawkish Fed = bearish equities) ─────────────────────
    "CPI":                          (-1, False),
    "Consumer Price Index":         (-1, False),
    "Core CPI":                     (-1, False),
    "PPI":                          (-1, False),
    "Producer Price Index":         (-1, False),
    "Core PPI":                     (-1, False),
    "PCE Price Index":              (-1, False),
    "Core PCE":                     (-1, False),

    # ── Growth / Activity ─────────────────────────────────────────────────────
    "GDP":                          (+1, False),
    "Retail Sales":                 (+1, False),
    "Core Retail Sales":            (+1, False),
    "Industrial Production":        (+1, False),
    "Capacity Utilization":         (+1, False),
    "Durable Goods":                (+1, False),
    "Factory Orders":               (+1, False),
    "Trade Balance":                (+1, False),

    # ── Consumer confidence ───────────────────────────────────────────────────
    "Consumer Confidence":          (+1, False),
    "Consumer Sentiment":           (+1, False),
    "Michigan Sentiment":           (+1, False),

    # ── Business / PMI ────────────────────────────────────────────────────────
    "ISM Manufacturing":            (+1, False),
    "ISM Non-Manufacturing":        (+1, False),
    "ISM Services":                 (+1, False),
    "Flash Manufacturing PMI":      (+1, False),
    "Flash Services PMI":           (+1, False),
    "Empire State Manufacturing":   (+1, False),
    "Philly Fed Manufacturing":     (+1, False),
    "Philly Fed":                   (+1, False),
    "Chicago PMI":                  (+1, False),

    # ── Housing ───────────────────────────────────────────────────────────────
    "New Home Sales":               (+1, False),
    "Existing Home Sales":          (+1, False),
    "Building Permits":             (+1, False),
    "Housing Starts":               (+1, False),
    "Pending Home Sales":           (+1, False),

    # ── Fed / FOMC  (bias = 0; direction depends on hawk/dove language) ───────
    "FOMC Statement":               (0,  False),
    "FOMC Minutes":                 (0,  False),
    "Fed Funds Rate":               (0,  False),
    "Federal Reserve":              (0,  False),
    "Fed Chair":                    (0,  False),
    "Powell":                       (0,  False),
    "Yellen":                       (0,  False),
}

IMPACT_WEIGHTS = {"HIGH": 1.0, "MEDIUM": 0.4, "LOW": 0.1}


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _parse_number(s: str) -> Optional[float]:
    """Parse '0.4%', '-245K', '1.23M' → float, or None if unparseable."""
    if not s or s.strip() in ("", "—", "-", "N/A", "..."):
        return None
    s = s.strip().replace(",", "").replace("%", "")
    suffix_map = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
    suffix = s[-1].upper() if s[-1:].upper() in suffix_map else None
    if suffix:
        s = s[:-1]
    try:
        v = float(s)
        return v * suffix_map[suffix] if suffix else v
    except ValueError:
        return None


def _hhmm(total_minutes: int) -> str:
    total_minutes = max(0, min(total_minutes, 1439))
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def _normalize_time(raw: str) -> Optional[str]:
    """'8:30am' / '8:30 AM' / '08:30' → '08:30', or None."""
    raw = raw.strip().replace("\xa0", "").lower()
    for fmt in ("%I:%M%p", "%I:%M %p", "%H:%M"):
        try:
            return datetime.strptime(raw, fmt).strftime("%H:%M")
        except ValueError:
            pass
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Data sources  (JSON API → HTML scraper → empty fallback)
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_json_api(target_date: date, verbose: bool) -> list[dict]:
    """
    ForexFactory exposes a JSON endpoint for the current week.
    Fast and no HTML parsing — try this first.
    """
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    if verbose:
        print(f"  [JSON API] {url}")
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    raw = resp.json()

    events = []
    for item in raw:
        country = item.get("country", "").upper()
        if country != "USD":
            continue

        # Parse ISO timestamp (FF feed gives ET-offset times for USD events,
        # e.g. "2026-06-16T08:30:00-04:00"). strftime gives the ET wall-clock time.
        dt_str = item.get("date", "")
        try:
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            item_date = dt.date()
        except (ValueError, AttributeError):
            continue

        if item_date != target_date:
            continue

        # Normalize time to HH:MM ET (dt already carries the ET UTC offset)
        try:
            time_et = dt.strftime("%H:%M")
        except Exception:
            time_et = ""

        impact_raw = item.get("impact", "").lower()
        if "high" in impact_raw:
            impact = "HIGH"
        elif "medium" in impact_raw or "moderate" in impact_raw:
            impact = "MEDIUM"
        else:
            impact = "LOW"

        events.append({
            "time_et":  time_et,
            "currency": "USD",
            "impact":   impact,
            "name":     item.get("title", ""),
            "actual":   item.get("actual",   ""),
            "forecast": item.get("forecast", ""),
            "previous": item.get("previous", ""),
        })

    return events


def _fetch_html_scraper(target_date: date, verbose: bool) -> list[dict]:
    """Fallback: scrape ForexFactory calendar HTML page."""
    day_param = target_date.strftime("%b%d.%Y").lower()
    url = f"https://www.forexfactory.com/calendar?day={day_param}"
    if verbose:
        print(f"  [HTML scraper] {url}")

    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Try known row selectors across FF versions
    rows = []
    for sel in ["tr.calendar__row", "tr.calendar_row", "tr[data-eventid]"]:
        rows = soup.select(sel)
        if rows:
            if verbose:
                print(f"  Matched selector '{sel}' ({len(rows)} rows)")
            break

    if not rows:
        raise RuntimeError("HTML structure not recognized — FF may have changed its layout")

    events = []
    current_time_raw = ""

    for row in rows:
        # Time cell (FF re-uses previous time when blank)
        for cls in [r"calendar__time", r"calendar_time", r"time"]:
            tc = row.find("td", class_=re.compile(cls))
            if tc:
                t = tc.get_text(strip=True)
                if t and t.lower() not in ("all day", "tentative"):
                    current_time_raw = t
                break

        # Currency filter
        currency = ""
        for cls in [r"calendar__currency", r"currency"]:
            cc = row.find("td", class_=re.compile(cls))
            if cc:
                currency = cc.get_text(strip=True)
                break
        if currency != "USD":
            continue

        # Impact
        impact = "LOW"
        for cls in [r"calendar__impact", r"calendar_impact", r"impact"]:
            ic = row.find("td", class_=re.compile(cls))
            if ic:
                s = str(ic).lower()
                if "high" in s or "red" in s:
                    impact = "HIGH"
                elif "medium" in s or "orange" in s:
                    impact = "MEDIUM"
                break

        # Event name
        name = ""
        for cls in [r"calendar__event", r"calendar_event", r"event"]:
            nc = row.find("td", class_=re.compile(cls))
            if nc:
                name = nc.get_text(strip=True)
                break
        if not name:
            continue

        def _cell(pat: str) -> str:
            c = row.find("td", class_=re.compile(pat))
            return c.get_text(strip=True) if c else ""

        events.append({
            "time_et":  _normalize_time(current_time_raw) or current_time_raw,
            "currency": "USD",
            "impact":   impact,
            "name":     name,
            "actual":   _cell(r"actual"),
            "forecast": _cell(r"forecast"),
            "previous": _cell(r"previous"),
        })

    return events


def fetch_events(target_date: date, verbose: bool = False) -> tuple[list[dict], str, Optional[str]]:
    """
    Try JSON API → HTML scraper → empty.
    Returns (events, source_name, error_message_or_None).
    """
    for fetch_fn, label in [(_fetch_json_api, "forexfactory_json"),
                             (_fetch_html_scraper, "forexfactory_html")]:
        try:
            events = fetch_fn(target_date, verbose)
            return events, label, None   # success even if empty (weekend = no events)
        except Exception as e:
            if verbose:
                print(f"  {label} failed: {e}")

    return [], "none", "All data sources failed — using NEUTRAL bias"


# ══════════════════════════════════════════════════════════════════════════════
# Scoring
# ══════════════════════════════════════════════════════════════════════════════

def score_event(ev: dict) -> tuple[float, str]:
    """Return (bias_contribution, notes) for one event."""
    name     = ev.get("name", "")
    actual_s = ev.get("actual", "")
    fore_s   = ev.get("forecast", "")
    impact   = ev.get("impact", "LOW")
    weight   = IMPACT_WEIGHTS.get(impact, 0.1)

    # Catalog lookup (substring, case-insensitive)
    direction, inverse = 0, False
    name_up = name.upper()
    for key, (d, inv) in EVENT_CATALOG.items():
        if key.upper() in name_up:
            direction, inverse = d, inv
            break

    if direction == 0:
        return 0.0, "Ambiguous or unrecognized — no score assigned"

    actual   = _parse_number(actual_s)
    forecast = _parse_number(fore_s)

    if actual is None:
        return 0.0, "Pending — no actual data released yet"
    if forecast is None:
        return 0.0, "No consensus forecast — cannot score vs. expectations"

    # For inverse metrics (claims, unemployment): lower actual = better
    beat = (actual < forecast) if inverse else (actual > forecast)
    contrib = direction * weight if beat else -direction * weight
    verb    = "beat" if beat else "missed"
    label   = "BULLISH" if contrib > 0 else "BEARISH"
    return contrib, f"{name} {verb} expectations → {label} for MNQ (weight {weight:.0%})"


# ══════════════════════════════════════════════════════════════════════════════
# Block windows
# ══════════════════════════════════════════════════════════════════════════════

def compute_block_windows(events: list[dict]) -> list[dict]:
    """Return trading pause windows around HIGH-impact events."""
    windows = []
    seen: set[str] = set()
    for ev in events:
        if ev["impact"] != "HIGH":
            continue
        t = ev.get("time_et", "")
        if not t or ":" not in t or t in seen:
            continue
        seen.add(t)
        try:
            h, m = map(int, t.split(":"))
            total = h * 60 + m
            windows.append({
                "event":           ev["name"],
                "event_time_et":   t,
                "block_start_et":  _hhmm(total - NEWS_BLOCK_BEFORE),
                "block_end_et":    _hhmm(total + NEWS_BLOCK_AFTER),
            })
        except ValueError:
            pass
    return windows


# ══════════════════════════════════════════════════════════════════════════════
# Directive + labels
# ══════════════════════════════════════════════════════════════════════════════

def _bias_label(score: float) -> str:
    if score >  0.50: return "BULLISH"
    if score >  0.20: return "LEAN_BULLISH"
    if score < -0.50: return "BEARISH"
    if score < -0.20: return "LEAN_BEARISH"
    return "NEUTRAL"

def _confidence(high_count: int) -> str:
    if high_count == 0: return "LOW"
    if high_count <= 2: return "MEDIUM"
    return "HIGH"

def build_directive(bias_score: float, high_count: int) -> dict:
    abs_b = abs(bias_score)
    return {
        "allow_longs":   bias_score > -0.60,
        "allow_shorts":  bias_score < +0.60,
        "reduce_size":   abs_b > 0.70 or high_count >= 3,
        "caution_mode":  high_count >= 2 or abs_b > 0.40,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Build and save
# ══════════════════════════════════════════════════════════════════════════════

def build_calendar(target_date: date, verbose: bool = False) -> dict:
    print(f"Fetching economic calendar for {target_date} …")
    raw_events, source, error = fetch_events(target_date, verbose)

    if error:
        print(f"  WARNING: {error}")
    else:
        print(f"  Source: {source}  |  {len(raw_events)} USD events found")

    # Score events
    scored = []
    total_bias = 0.0
    high_count = 0

    for ev in raw_events:
        contrib, notes = score_event(ev)
        ev["bias_contribution"] = round(contrib, 3)
        ev["notes"]             = notes
        total_bias             += contrib
        if ev["impact"] == "HIGH":
            high_count += 1
        scored.append(ev)

    bias_score    = round(max(-1.0, min(1.0, total_bias)), 3)
    block_windows = compute_block_windows(scored)
    directive     = build_directive(bias_score, high_count)

    # Recommendation text
    high_events = [e for e in scored if e["impact"] == "HIGH"]
    if high_events:
        names = ", ".join(e["name"] for e in high_events[:3])
        prefix = "CAUTION" if high_count >= 2 else "NOTE"
        recommendation = (
            f"{prefix}: {high_count} high-impact USD event(s). "
            f"Key: {names}. Bias: {_bias_label(bias_score)}."
        )
    elif error:
        recommendation = "Calendar unavailable — assume NEUTRAL bias, trade with caution."
    else:
        recommendation = "No high-impact USD events — standard session rules apply."

    return {
        "date":               target_date.isoformat(),
        "generated_utc":      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source":             source,
        "fetch_error":        error,
        "bias":               _bias_label(bias_score),
        "bias_score":         bias_score,       # -1.0 bearish → +1.0 bullish
        "confidence":         _confidence(high_count),
        "high_impact_count":  high_count,
        "block_windows":      block_windows,    # list of {event, event_time_et, block_start_et, block_end_et}
        "directive":          directive,        # {allow_longs, allow_shorts, reduce_size, caution_mode}
        "events":             scored,
        "recommendation":     recommendation,
    }


def save_calendar(data: dict, out_path: Optional[Path] = None) -> Path:
    if out_path is None:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUT_DIR / f"calendar_{data['date']}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(data, indent=2)
    out_path.write_text(payload, encoding="utf-8")

    # Stable symlink/copy for bots that always read the same path
    latest = out_path.parent / "calendar_latest.json"
    latest.write_text(payload, encoding="utf-8")

    return out_path


# ══════════════════════════════════════════════════════════════════════════════
# Console summary
# ══════════════════════════════════════════════════════════════════════════════

def print_summary(data: dict) -> None:
    bar = "─" * 60
    print(f"\n{bar}")
    print(f"  ECONOMIC CALENDAR  ·  {data['date']}")
    print(bar)
    print(f"  Bias        : {data['bias']:16s}  score = {data['bias_score']:+.3f}")
    print(f"  Confidence  : {data['confidence']}")
    print(f"  High-impact : {data['high_impact_count']} event(s)")

    if data["block_windows"]:
        print(f"\n  NEWS BLOCK WINDOWS (ET):")
        for w in data["block_windows"]:
            print(f"    {w['block_start_et']} – {w['block_end_et']}  ⇐  {w['event']}")

    high_evs = [e for e in data["events"] if e["impact"] == "HIGH"]
    if high_evs:
        print(f"\n  HIGH-IMPACT EVENTS:")
        for e in high_evs:
            arrow = "▲" if e["bias_contribution"] > 0 else \
                    "▼" if e["bias_contribution"] < 0 else "–"
            act_line = (
                f"  Act: {e['actual'] or '?':8s}"
                f"  Fcst: {e['forecast'] or '?':8s}"
                f"  Prev: {e['previous'] or '?'}"
            ) if (e["actual"] or e["forecast"]) else "  (pending)"
            print(f"    {e['time_et']:6s} {arrow}  {e['name']}")
            print(f"         {act_line}")
            print(f"         {e['notes']}")

    d = data["directive"]
    print(f"\n  BOT DIRECTIVE:")
    print(f"    allow_longs   = {str(d['allow_longs']):<5}  |  "
          f"allow_shorts = {d['allow_shorts']}")
    print(f"    reduce_size   = {str(d['reduce_size']):<5}  |  "
          f"caution_mode = {d['caution_mode']}")
    print(f"\n  {data['recommendation']}")
    print(f"{bar}\n")


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch economic calendar and write bot directive JSON"
    )
    parser.add_argument("--date",    default=None,
                        help="Target date YYYY-MM-DD (default: today)")
    parser.add_argument("--out",     default=None,
                        help="Override output JSON file path")
    parser.add_argument("--verbose", action="store_true",
                        help="Print debug info (URLs, row counts, etc.)")
    args = parser.parse_args()

    if args.date:
        try:
            target_date = date.fromisoformat(args.date)
        except ValueError:
            print(f"ERROR: invalid date '{args.date}' — use YYYY-MM-DD")
            sys.exit(1)
    else:
        target_date = date.today()

    out_path = Path(args.out) if args.out else None
    data     = build_calendar(target_date, verbose=args.verbose)
    saved    = save_calendar(data, out_path)

    print_summary(data)
    print(f"Saved  → {saved}")
    print(f"Latest → {saved.parent / 'calendar_latest.json'}")


if __name__ == "__main__":
    main()
