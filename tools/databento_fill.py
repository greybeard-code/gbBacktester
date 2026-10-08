"""Fill missing L1 Parquet days from Databento when no .nrd exists.

The NRD->Parquet converter can only produce days we actually recorded. A
handful of 2025 days were never captured (failed replay downloads). This
fetches those days from Databento's GLBX.MDP3 dataset and writes them in the
repo's existing L1 Parquet layout, so data.py picks them up with no changes.

Schema is TBBO ("BBO on trade"): every trade plus the top-of-book immediately
before it. That is exactly the information data._reduce_raw keeps -- it
discards standalone quote updates and retains only the quote prevailing at
each trade -- so TBBO is lossless for this engine and far cheaper than mbp-1.

Uses the plain HTTP API via stdlib urllib rather than the `databento` package,
which depends on pandas (this project deliberately avoids a pandas dependency).

    set DATABENTO_API_KEY=db-...
    python tools/databento_fill.py --cost            # price it, download nothing
    python tools/databento_fill.py --fetch           # download and write
    python tools/databento_fill.py --cost --include-cl

Days whose recording EXISTS but is unusable (malformed / fails the decoder's
integrity check) are not gaps to the scan above; name them explicitly:

    python tools/databento_fill.py --cost --days 20260811 --symbols ES,MES

A recording that simply STOPS early (e.g. 2026-08-10 ends 21:54 ET) can keep
its NT8 data and get only the missing tail from Databento: --splice-tail reads
the existing L1 file, fetches from its last event to ET midnight, and appends
the events after that timestamp. The original is copied to
<repo>/hold/parquet-replaced-<date>/ first; the result keeps its NT8 metadata
and gains splice.* keys recording where the Databento tail starts.

    python tools/databento_fill.py --cost --splice-tail --days 20260810 --symbols MGC

A recording with HOLES in the middle of the day (the recorder dropped a symbol
for 10-30 min) keeps its NT8 data and gets only the holes from Databento:
--splice-windows reads a CSV of (symbol, date, start_et, end_et) windows, finds
the NT8 trades bounding each hole in the existing file, fetches TBBO for just
that span, and inserts the events lying more than SPLICE_GUARD_NS inside it
(so a boundary trade can't be duplicated across sources). NT8 L1 rows inside
the hole are dropped; L2 is untouched. A window whose Databento prices don't
join NT8's on both sides is refused (wrong contract). Windows done are listed
in the splice.windows key, so re-running skips them.

    python tools/databento_fill.py --cost --splice-windows holes.csv --symbols ES

Most such "holes" are recorder STALLS: NT8 kept the trades and wrote them late,
in a burst right after the gap, so the prices are all there but the timing is
squeezed. --splice-windows refuses those. Add --retime-stalls to fix them:
the NT8 L1 rows from the last pre-stall trade to the end of the burst (gap +
burst) are replaced by Databento's events over that span, so each trade is
kept once at its real time. Allowed only when the seams join and NT8's volume
over the span is 80-110% of Databento's (the burst really is the backlog).
"""
from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

API = "https://hist.databento.com/v0"
DATASET = "GLBX.MDP3"
SCHEMA = "tbbo"

# Defaults; override with --parquet-root / --continuous-root.
NRD_ROOT = Path(r"M:\NinjaTrader_DataRepo\RawData\Continuous")
PARQUET_ROOT = Path(r"M:\NinjaTrader_DataRepo\RawData\Parquet")

EASTERN = ZoneInfo("America/New_York")

MDT_ASK, MDT_BID, MDT_LAST = 0, 1, 2

QUARTERLY = {"ES", "MES", "NQ", "MNQ", "YM", "MYM", "RTY", "M2K"}
METALS = {"GC", "MGC"}
MAINTAINED = QUARTERLY | METALS
MONTH_CODE = "FGHJKMNQUVXZ"          # Jan..Dec

# Days on which the market itself was shut -- no vendor has data for these, so
# they are never fetch candidates even though the audit reports them missing.
MARKET_CLOSED = {"20250418"}          # Good Friday 2025

# Explicit front-month overrides for days where the roll rule below diverges
# from where the volume actually was. Empty since 2026-09-26: the gold rule now
# rolls 3 business days before the 1st of the expiry month (liquidity leaves
# before first notice day), which covers the MGC 2025-05-28..30 days that
# needed overrides under the old 1st-of-month rule (MGCM5 had collapsed to
# $0.0002 of data on 2025-05-30 while MGCQ5 billed $0.2175).
CONTRACT_OVERRIDE: dict[tuple[str, str], str] = {}


# --------------------------------------------------------------------------
# contract roll -- mirrors Get-RollDate in Build-ContinuousContracts.ps1 (and
# Get-ActiveContractForDate in Audit-ContinuousContracts.ps1) so the fetched
# day comes from the same contract the rest of the continuous series uses.
# Gold lists Feb/Apr/Jun/Aug/Dec only in the continuous series (no October).
# --------------------------------------------------------------------------

def third_friday(year: int, month: int) -> date:
    d = date(year, month, 1)
    while d.weekday() != 4:
        d += timedelta(days=1)
    return d + timedelta(days=14)


def nth_business_day_before(d: date, n: int) -> date:
    cur, count = d - timedelta(days=1), 0
    while True:
        if cur.weekday() < 5:
            count += 1
            if count >= n:
                return cur
        cur -= timedelta(days=1)


def active_contract(symbol: str, d: date) -> tuple[int, int]:
    """-> (expiry_year, expiry_month) of the front contract covering `d`."""
    if symbol in QUARTERLY:
        months, rule = (3, 6, 9, 12), "quarterly"
    elif symbol in METALS:
        months, rule = (2, 4, 6, 8, 12), "gold"
    else:                                   # CL/MCL and anything else
        months, rule = tuple(range(1, 13)), "crudeoil"

    cands = []
    for y in range(d.year - 2, d.year + 2):
        for m in months:
            if rule == "quarterly":
                roll = third_friday(y, m) - timedelta(days=4)
            elif rule == "crudeoil":
                pm, py = (12, y - 1) if m == 1 else (m - 1, y)
                roll = nth_business_day_before(date(py, pm, 25), 6)
            elif rule == "gold":
                roll = nth_business_day_before(date(y, m, 1), 3)
            else:
                roll = date(y, m, 1)
            cands.append((y, m, roll))
    cands.sort(key=lambda c: c[0] * 12 + c[1])

    prev = date.min
    for y, m, roll in cands:
        if prev <= d < roll:
            return y, m
        prev = roll
    return cands[-1][0], cands[-1][1]


def raw_symbol(symbol: str, d: date) -> str:
    """CME/Databento raw symbol, e.g. YM 2025-05-28 -> YMM5."""
    override = CONTRACT_OVERRIDE.get((symbol, d.strftime("%Y%m%d")))
    if override:
        return override
    y, m = active_contract(symbol, d)
    return f"{symbol}{MONTH_CODE[m - 1]}{y % 10}"


def neighbour_symbols(symbol: str, d: date) -> list[str]:
    """The roll-rule contract's previous and next contracts in its cycle. On a
    roll-edge day the recording may hold either, so a splice that doesn't join
    up with the rule's contract tries these."""
    months = ((3, 6, 9, 12) if symbol in QUARTERLY else
              (2, 4, 6, 8, 12) if symbol in METALS else tuple(range(1, 13)))
    y, m = active_contract(symbol, d)
    i = months.index(m)
    prev = (y, months[i - 1]) if i > 0 else (y - 1, months[-1])
    nxt = (y, months[i + 1]) if i + 1 < len(months) else (y + 1, months[0])
    return [f"{symbol}{MONTH_CODE[mm - 1]}{yy % 10}" for yy, mm in (prev, nxt)]


# --------------------------------------------------------------------------
# gap detection
# --------------------------------------------------------------------------

def year_range(symbol: str, year: int) -> tuple[date, date]:
    if symbol in QUARTERLY:
        return (third_friday(year - 1, 12) - timedelta(days=4),
                third_friday(year, 12) - timedelta(days=5))
    return date(year, 1, 1), date(year, 12, 31)


def find_gaps(year: int, symbols: list[str] | None) -> dict[str, list[str]]:
    """Absent weekdays per symbol in the NRD continuous archive.

    Only *absent* files count. Small-but-present files are the thin holiday
    evening sessions (Christmas, New Year) which are complete as recorded --
    treating them as gaps would refetch data we already have.
    """
    folder_root = NRD_ROOT / str(year)
    out: dict[str, list[str]] = {}
    for p in sorted(folder_root.iterdir()):
        if not p.is_dir():
            continue
        sym = p.name.split()[0]
        if symbols and sym not in symbols:
            continue
        present = {f.stem for f in p.glob("*.nrd")
                   if len(f.stem) == 8 and f.stem.isdigit()}
        start, end = year_range(sym, year)
        gaps = []
        d = start
        while d <= end:
            k = d.strftime("%Y%m%d")
            if d.weekday() < 5 and k not in present and k not in MARKET_CLOSED:
                gaps.append(k)
            d += timedelta(days=1)
        if gaps:
            out[sym] = gaps
    return out


# --------------------------------------------------------------------------
# Databento HTTP
# --------------------------------------------------------------------------

def _auth_header(key: str) -> str:
    return "Basic " + base64.b64encode(f"{key}:".encode()).decode()


def _request(method: str, endpoint: str, key: str, params: dict) -> bytes:
    body = urllib.parse.urlencode(params).encode()
    if method == "GET":
        req = urllib.request.Request(f"{API}/{endpoint}?{body.decode()}")
    else:
        req = urllib.request.Request(f"{API}/{endpoint}", data=body)
    req.add_header("Authorization", _auth_header(key))
    # Databento's gateway returns the odd 502/503/504 under load; retry those
    # (and network timeouts) with backoff. 4xx means a bad request: fail now.
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == 4:
                raise
            why = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == 4:
                raise
            why = str(e)
        wait = 10 * 2 ** attempt
        print(f"    (Databento {endpoint}: {why}; retrying in {wait}s)", flush=True)
        time.sleep(wait)


def day_window(day: str, after_ns: int | None = None) -> tuple[str, str]:
    """ET calendar day -> [start, end) as UTC ISO strings.

    Repo day files are ET calendar days, so the fetch window
    must be the ET midnight-to-midnight span expressed in UTC. With after_ns
    the window starts at that instant instead (floored to the second; the
    caller filters to events strictly after it).
    """
    d = datetime.strptime(day, "%Y%m%d").replace(tzinfo=EASTERN)
    lo = d.astimezone(ZoneInfo("UTC"))
    if after_ns is not None:
        lo = datetime.fromtimestamp(after_ns // 1_000_000_000, ZoneInfo("UTC"))
    hi = (d + timedelta(days=1)).astimezone(ZoneInfo("UTC"))
    return lo.strftime("%Y-%m-%dT%H:%M:%S"), hi.strftime("%Y-%m-%dT%H:%M:%S")


def span_window(lo_ns: int, hi_ns: int) -> tuple[str, str]:
    """[lo_ns, hi_ns] -> UTC ISO strings, widened to whole seconds."""
    utc = ZoneInfo("UTC")
    lo = datetime.fromtimestamp(lo_ns // 1_000_000_000, utc)
    hi = datetime.fromtimestamp(-(-hi_ns // 1_000_000_000), utc)
    return lo.strftime("%Y-%m-%dT%H:%M:%S"), hi.strftime("%Y-%m-%dT%H:%M:%S")


def get_cost(key: str, symbol: str, day: str, after_ns: int | None = None,
             span: tuple[str, str] | None = None) -> float:
    lo, hi = span or day_window(day, after_ns)
    raw = _request("GET", "metadata.get_cost", key, {
        "dataset": DATASET, "symbols": raw_symbol(
            symbol, datetime.strptime(day, "%Y%m%d").date()),
        "schema": SCHEMA, "start": lo, "end": hi,
        "stype_in": "raw_symbol",
    })
    return float(json.loads(raw))


def fetch_tbbo(key: str, symbol: str, day: str,
               after_ns: int | None = None,
               span: tuple[str, str] | None = None,
               raw: str | None = None) -> list[dict]:
    lo, hi = span or day_window(day, after_ns)
    raw = _request("POST", "timeseries.get_range", key, {
        "dataset": DATASET, "symbols": raw or raw_symbol(
            symbol, datetime.strptime(day, "%Y%m%d").date()),
        "schema": SCHEMA, "start": lo, "end": hi,
        "stype_in": "raw_symbol",
        "encoding": "csv",
        "pretty_px": "true",     # decimal prices, not 1e-9 fixed point
        "pretty_ts": "false",    # keep raw int64 ns -- what we store
        "map_symbols": "false",
    })
    return list(csv.DictReader(io.StringIO(raw.decode())))


# --------------------------------------------------------------------------
# TBBO -> NT8-style L1 Parquet
# --------------------------------------------------------------------------

def to_l1_table(rows: list[dict]) -> pa.Table:
    """Expand each TBBO record into Ask, Bid, Last events.

    data._reduce_raw takes, for each trade, the most recent preceding Ask/Bid
    by array position -- so the two quote events must precede their trade.
    """
    ts_out, mdt_out, px_out, vol_out = [], [], [], []
    for r in rows:
        ts = int(r["ts_recv"])
        for px_key, sz_key, mdt in (("ask_px_00", "ask_sz_00", MDT_ASK),
                                    ("bid_px_00", "bid_sz_00", MDT_BID)):
            px = r.get(px_key, "")
            if px in ("", "nan"):
                continue
            px = float(px)
            if not np.isfinite(px) or abs(px) > 1e15:   # unset-book sentinel
                continue
            ts_out.append(ts)
            mdt_out.append(mdt)
            px_out.append(px)
            vol_out.append(int(r.get(sz_key) or 0))
        ts_out.append(ts)
        mdt_out.append(MDT_LAST)
        px_out.append(float(r["price"]))
        vol_out.append(int(r["size"]))

    table = pa.table({
        "Timestamp": pa.array(np.asarray(ts_out, dtype="int64")
                              .view("datetime64[ns]"),
                              type=pa.timestamp("ns", tz="UTC")),
        "MarketDataType": pa.array(np.asarray(mdt_out, dtype="int8")),
        "Price": pa.array(np.asarray(px_out, dtype="float64")),
        "Volume": pa.array(np.asarray(vol_out, dtype="int64")),
    })
    # Tagged UTC so data._reduce_raw skips the legacy ET->UTC correction.
    return table.replace_schema_metadata({
        b"replay_importer.timestamps": b"UTC",
        b"replay_importer.source_tz": b"UTC",
        b"replay_importer.version": b"2",
        b"replay_importer.source_name": b"databento GLBX.MDP3 tbbo",
    })


def season_year(symbol: str, day: str) -> int:
    """Roll-season year folder (matches nrd_to_parquet.season_year): quarterly
    index futures move to next year's folder on the Monday before December's
    3rd Friday; everything else uses the calendar year."""
    d = datetime.strptime(day, "%Y%m%d").date()
    if symbol in QUARTERLY and d >= third_friday(d.year, 12) - timedelta(days=4):
        return d.year + 1
    return d.year


def out_path(symbol: str, day: str) -> Path:
    y = season_year(symbol, day)
    return PARQUET_ROOT / str(y) / f"{symbol}-{y}_L1" / f"{day}.parquet"


def splice_tail(existing: pa.Table, rows: list[dict], after_ns: int) -> tuple[pa.Table, int]:
    """Append the TBBO events strictly after `after_ns` to an existing NT8 L1
    table. Returns (new table, number of trades appended). Existing metadata is
    kept (NT8 provenance) and splice.* keys are added."""
    tail_rows = [r for r in rows if int(r["ts_recv"]) > after_ns]
    if not tail_rows:
        return existing, 0
    tail = to_l1_table(tail_rows).select(existing.column_names).cast(existing.schema)
    merged = pa.concat_tables([existing.replace_schema_metadata(None),
                               tail.replace_schema_metadata(None)])
    meta = dict(existing.schema.metadata or {})
    meta.update({
        b"splice.source": b"databento GLBX.MDP3 tbbo",
        b"splice.after_ns": str(after_ns).encode(),
        b"splice.trades": str(len(tail_rows)).encode(),
    })
    return merged.replace_schema_metadata(meta), len(tail_rows)


def backup(path: Path) -> Path:
    hold = PARQUET_ROOT.parent.parent / "hold" / f"parquet-replaced-{date.today():%Y%m%d}"
    dest = hold / path.parent.name / path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = 1
    while dest.exists():
        dest = dest.with_name(f"{path.stem}.{n}{path.suffix}")
        n += 1
    shutil.copy2(path, dest)
    return dest


def run_splice(args, key: str, gaps: dict[str, list[str]]) -> int:
    grand, written = 0.0, 0
    for sym in sorted(gaps):
        for day in gaps[sym]:
            dst = out_path(sym, day)
            if not dst.exists():
                print(f"  {sym} {day}  no existing file to splice into; use a full fill")
                continue
            existing = pq.read_table(dst)
            if (existing.schema.metadata or {}).get(b"splice.source") and not args.overwrite:
                print(f"  {sym} {day}  already spliced, skipping")
                continue
            ts = existing["Timestamp"].cast(pa.int64()).to_numpy()
            after_ns = int(ts.max())
            end = datetime.strptime(day, "%Y%m%d").replace(tzinfo=EASTERN) + timedelta(days=1)
            after_et = datetime.fromtimestamp(after_ns / 1e9, EASTERN)
            if (end - after_et).total_seconds() < 120:
                print(f"  {sym} {day}  already runs to {after_et:%H:%M:%S} ET; nothing to fill")
                continue
            d = datetime.strptime(day, "%Y%m%d").date()
            if args.cost:
                c = get_cost(key, sym, day, after_ns)
                grand += c
                print(f"  {sym:<4} {day}  {raw_symbol(sym, d):<6} tail from {after_et:%H:%M:%S} ET  ${c:>8.4f}")
                continue
            rows = fetch_tbbo(key, sym, day, after_ns)
            merged, n = splice_tail(existing, rows, after_ns)
            if not n:
                print(f"  {sym} {day}  no trades after {after_et:%H:%M:%S} ET")
                continue
            bak = backup(dst)
            tmp = dst.with_suffix(f".{os.getpid()}.tmp")
            pq.write_table(merged, tmp, compression="zstd")
            os.replace(tmp, dst)
            written += 1
            print(f"  {sym} {day}  +{n:,} trades after {after_et:%H:%M:%S} ET "
                  f"({existing.num_rows:,} -> {merged.num_rows:,} L1 events); original -> {bak}")
    if args.cost:
        print(f"TOTAL ESTIMATE: ${grand:.2f}  (schema={SCHEMA}). No data downloaded.")
    else:
        print(f"\nSpliced {written} day files.")
    return 0


SPLICE_GUARD_NS = 1_000_000_000      # insert only events > 1 s inside a hole
SEAM_MAX_FRAC = 0.005                # Databento must join NT8 within 0.5% at both ends
BURST_RATIO = 5                      # trades/s in the 15 s after a hole vs the 5 min before
RETIME_VOL_RANGE = (0.80, 1.10)      # NT8 / Databento volume over a stall's gap + burst


def read_windows(path: str, syms: list[str] | None, days: list[str] | None) -> dict:
    """CSV with symbol,date,start_et,end_et (e.g. real_holes.csv) ->
    {(symbol, date): [(start_et, end_et), ...]}."""
    out: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for r in csv.DictReader(open(path, newline="")):
        s, d = r["symbol"].upper(), r["date"]
        if (syms and s not in syms) or (days and d not in days):
            continue
        out.setdefault((s, d), []).append((r["start_et"], r["end_et"]))
    return out


def _et_ns(day: str, hms: str) -> int:
    t = datetime.strptime(f"{day} {hms}", "%Y%m%d %H:%M:%S").replace(tzinfo=EASTERN)
    return int(t.timestamp()) * 1_000_000_000


def hole_bounds(ts: np.ndarray, trade_ts: np.ndarray, day: str,
                start_et: str, end_et: str) -> tuple[int, int] | None:
    """Exact NT8 trades bounding a hole listed at second resolution: the last
    trade before start_et + 1 s and the first trade at or after end_et."""
    lo, hi = _et_ns(day, start_et) + 1_000_000_000, _et_ns(day, end_et)
    i = np.searchsorted(trade_ts, lo) - 1
    j = np.searchsorted(trade_ts, hi)
    if i < 0 or j >= len(trade_ts) or trade_ts[j] - trade_ts[i] < 3 * SPLICE_GUARD_NS:
        return None
    return int(trade_ts[i]), int(trade_ts[j])


def splice_window(existing: pa.Table, rows: list[dict], a_ns: int, b_ns: int,
                  px_a: float, px_b: float) -> tuple[pa.Table, int, str]:
    """Replace the L1 rows strictly inside (a_ns, b_ns) with the TBBO events
    more than SPLICE_GUARD_NS inside it. Returns (table, trades inserted, note);
    note is non-empty when the window was refused."""
    # A recorder STALL looks like a hole but isn't one: NT8 keeps the trades and
    # writes them late, in a burst right after the gap (ES 2022-06-13: 11 min of
    # trades stamped into 10 s, 44x the normal rate). Filling it would count those
    # trades twice, so refuse when the burst is there.
    ts = existing["Timestamp"].cast(pa.int64()).to_numpy()
    tr_ts = ts[existing["MarketDataType"].to_numpy() == MDT_LAST]
    before = np.count_nonzero((tr_ts >= a_ns - 300 * 10**9) & (tr_ts <= a_ns)) / 300
    after = np.count_nonzero((tr_ts >= b_ns) & (tr_ts < b_ns + 15 * 10**9)) / 15
    if after > BURST_RATIO * max(before, 1.0):
        return existing, 0, (f"REFUSED: backlog burst after the hole ({after:.0f} trades/s vs "
                             f"{before:.0f} before) -- a recorder stall, not lost data")
    ins = sorted((r for r in rows if a_ns + SPLICE_GUARD_NS < int(r["ts_recv"]) < b_ns - SPLICE_GUARD_NS),
                 key=lambda r: int(r["ts_recv"]))
    if not ins:
        return existing, 0, "no Databento trades inside the hole"
    first, last = float(ins[0]["price"]), float(ins[-1]["price"])
    seam = max(abs(first - px_a) / px_a, abs(last - px_b) / px_b)
    if seam > SEAM_MAX_FRAC:
        return existing, 0, (f"REFUSED: seam {first} vs NT8 {px_a} / {last} vs NT8 {px_b} "
                             f"({100 * seam:.2f}%) -- wrong contract?")
    ts = existing["Timestamp"].cast(pa.int64()).to_numpy()
    lo = int(np.searchsorted(ts, a_ns, side="right"))
    hi = int(np.searchsorted(ts, b_ns, side="left"))
    mid = to_l1_table(ins).select(existing.column_names).cast(existing.schema)
    merged = pa.concat_tables([existing.slice(0, lo).replace_schema_metadata(None),
                               mid.replace_schema_metadata(None),
                               existing.slice(hi).replace_schema_metadata(None)])
    return merged.replace_schema_metadata(existing.schema.metadata), len(ins), ""


def burst_end(trade_ts: np.ndarray, b_ns: int) -> int | None:
    """End of the backlog burst after a stall: the first NT8 trade at the start of a
    1 s bucket (from b_ns) holding no more than 3x the caught-up rate, measured
    20-80 s after the gap. The rate before the gap is no reference: across the
    09:30 open it is the pre-market rate."""
    caught_up = max(np.count_nonzero((trade_ts >= b_ns + 20 * 10**9)
                                     & (trade_ts < b_ns + 80 * 10**9)) / 60, 1.0)
    for k in range(20):
        t = b_ns + k * 10**9
        if np.count_nonzero((trade_ts >= t) & (trade_ts < t + 10**9)) <= 3 * caught_up:
            i = int(np.searchsorted(trade_ts, t))
            return int(trade_ts[i]) if i < len(trade_ts) else None
    return None


def retime_stall(existing: pa.Table, rows: list[dict], a_ns: int, c_ns: int,
                 px_a: float, px_c: float) -> tuple[pa.Table, int, str]:
    """Replace the L1 rows strictly inside (a_ns, c_ns) -- a stall's gap plus its
    backlog burst -- with the TBBO events more than SPLICE_GUARD_NS inside it.
    Returns (table, trades inserted, note); note is non-empty when refused."""
    span_rows = [r for r in rows if a_ns < int(r["ts_recv"]) < c_ns]
    ins = sorted((r for r in span_rows if a_ns + SPLICE_GUARD_NS < int(r["ts_recv"]) < c_ns - SPLICE_GUARD_NS),
                 key=lambda r: int(r["ts_recv"]))
    if not ins:
        return existing, 0, "no Databento trades in the stall span"
    first, last = float(ins[0]["price"]), float(ins[-1]["price"])
    seam = max(abs(first - px_a) / px_a, abs(last - px_c) / px_c)
    if seam > SEAM_MAX_FRAC:
        return existing, 0, (f"REFUSED: seam {first} vs NT8 {px_a} / {last} vs NT8 {px_c} "
                             f"({100 * seam:.2f}%) -- wrong contract?")
    ts = existing["Timestamp"].cast(pa.int64()).to_numpy()
    mdt = existing["MarketDataType"].to_numpy()
    vol = existing["Volume"].to_numpy()
    nt8_vol = int(vol[(mdt == MDT_LAST) & (ts > a_ns) & (ts < c_ns)].sum())
    db_vol = sum(int(r["size"]) for r in span_rows)
    ratio = nt8_vol / max(db_vol, 1)
    if not RETIME_VOL_RANGE[0] <= ratio <= RETIME_VOL_RANGE[1]:
        return existing, 0, (f"REFUSED: NT8 volume over the stall is {ratio:.2f}x Databento's "
                             f"({nt8_vol:,} vs {db_vol:,}) -- not a clean backlog")
    lo = int(np.searchsorted(ts, a_ns, side="right"))
    hi = int(np.searchsorted(ts, c_ns, side="left"))
    mid = to_l1_table(ins).select(existing.column_names).cast(existing.schema)
    merged = pa.concat_tables([existing.slice(0, lo).replace_schema_metadata(None),
                               mid.replace_schema_metadata(None),
                               existing.slice(hi).replace_schema_metadata(None)])
    return merged.replace_schema_metadata(existing.schema.metadata), len(ins), ""


def run_splice_windows(args, key: str, windows: dict) -> int:
    grand, files, trades, refused, failed = 0.0, 0, 0, 0, 0
    for (sym, day) in sorted(windows):
        dst = out_path(sym, day)
        d = datetime.strptime(day, "%Y%m%d").date()
        if not dst.exists():
            print(f"  {sym} {day}  no existing file; skipping")
            continue
        # cost mode only needs the bounds: read just those columns over the share
        table = (pq.read_table(dst, columns=["Timestamp", "MarketDataType", "Price"])
                 if args.cost else pq.read_table(dst))
        meta = dict(table.schema.metadata or {})
        if b"databento" in meta.get(b"replay_importer.source_name", b""):
            print(f"  {sym} {day}  whole day is already Databento; skipping")
            continue
        ts = table["Timestamp"].cast(pa.int64()).to_numpy()
        if not (np.diff(ts) >= 0).all():
            print(f"  {sym} {day}  timestamps not sorted; skipping")
            continue
        done = json.loads(meta.get(b"splice.windows", b"[]"))
        done_spans = {(w[0], w[1]) for w in done}
        orig_rows, changed = table.num_rows, False
        for start_et, end_et in windows[(sym, day)]:
            mdt = table["MarketDataType"].to_numpy()
            ts = table["Timestamp"].cast(pa.int64()).to_numpy()
            px = table["Price"].to_numpy()
            tr = np.flatnonzero(mdt == MDT_LAST)
            b = hole_bounds(ts, ts[tr], day, start_et, end_et)
            if b is None:
                print(f"  {sym} {day} {start_et}-{end_et}  hole not found in the file; skipping")
                continue
            a_ns, b_ns = b
            if (a_ns, b_ns) in done_spans:
                continue
            span = span_window(a_ns, b_ns)
            # a recorder stall needs no download: check for its backlog burst first
            tt = ts[tr]
            before = np.count_nonzero((tt >= a_ns - 300 * 10**9) & (tt <= a_ns)) / 300
            after = np.count_nonzero((tt >= b_ns) & (tt < b_ns + 15 * 10**9)) / 15
            stall = after > BURST_RATIO * max(before, 1.0)
            retime = getattr(args, "retime_stalls", False)
            if stall and not retime:
                print(f"  {sym} {day} {start_et}-{end_et}  REFUSED: backlog burst after the hole "
                      f"({after:.0f} trades/s vs {before:.0f} before) -- a recorder stall, not lost data")
                continue
            if not stall and retime:
                continue                      # a real hole: plain --splice-windows handles it
            if stall:
                c_ns = burst_end(tt, b_ns)
                if c_ns is None:
                    print(f"  {sym} {day} {start_et}-{end_et}  REFUSED: no end to the backlog burst found")
                    continue
                if (a_ns, c_ns) in done_spans:
                    continue
                b_ns, span = c_ns, span_window(a_ns, c_ns)
            if args.cost:
                c = get_cost(key, sym, day, span=span)
                grand += c
                print(f"  {sym:<4} {day} {start_et}-{end_et}  {raw_symbol(sym, d):<6} ${c:>8.4f}")
                continue
            try:
                rows = fetch_tbbo(key, sym, day, span=span)
            except (urllib.error.URLError, TimeoutError) as e:
                failed += 1
                print(f"  {sym} {day} {start_et}-{end_et}  FETCH FAILED after retries: {e}; skipped")
                continue
            px_a = float(px[tr[np.searchsorted(ts[tr], a_ns)]])
            px_b = float(px[tr[np.searchsorted(ts[tr], b_ns)]])
            fill = retime_stall if stall else splice_window
            new, n, note = fill(table, rows, a_ns, b_ns, px_a, px_b)
            used = raw_symbol(sym, d)
            if note.startswith("REFUSED: seam"):
                for alt in neighbour_symbols(sym, d):
                    try:
                        alt_rows = fetch_tbbo(key, sym, day, span=span, raw=alt)
                    except (urllib.error.URLError, TimeoutError):
                        continue
                    alt_new, alt_n, alt_note = fill(table, alt_rows, a_ns, b_ns, px_a, px_b)
                    if not alt_note:
                        new, n, note, used = alt_new, alt_n, "", alt
                        break
            table = new
            if note:
                refused += note.startswith("REFUSED")
                print(f"  {sym} {day} {start_et}-{end_et}  {note}")
                continue
            done.append([a_ns, b_ns, n] + (["retime"] if stall else [])); done_spans.add((a_ns, b_ns))
            trades += n; changed = True
            print(f"  {sym} {day} {start_et}-{end_et}  {'re-timed, ' if stall else ''}+{n:,} trades"
                  f"{'' if used == raw_symbol(sym, d) else f' (from {used}, not the roll rule contract)'}")
        if changed:
            meta.update({b"splice.source": b"databento GLBX.MDP3 tbbo",
                         b"splice.windows": json.dumps(done).encode()})
            table = table.replace_schema_metadata(meta)
            bak = backup(dst)
            tmp = dst.with_suffix(f".{os.getpid()}.tmp")
            pq.write_table(table, tmp, compression="zstd")
            os.replace(tmp, dst)
            files += 1
            print(f"  {sym} {day}  {orig_rows:,} -> {table.num_rows:,} L1 events; original -> {bak}")
    if args.cost:
        print(f"TOTAL ESTIMATE: ${grand:.2f}  (schema={SCHEMA}). No data downloaded.")
    else:
        print(f"\nSpliced {trades:,} trades into {files} day files; {refused} windows refused; "
              f"{failed} fetches failed (re-run to retry them).")
    return 0


# --------------------------------------------------------------------------

def main() -> int:
    global PARQUET_ROOT, NRD_ROOT
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--symbols", help="comma list; default = maintained symbols")
    ap.add_argument("--include-cl", action="store_true",
                    help="also fill CL/MCL (not in the maintained set)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--cost", action="store_true", help="price it, download nothing")
    mode.add_argument("--fetch", action="store_true", help="download and write Parquet")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--days", help="comma list of YYYYMMDD to fill regardless of the "
                                   "gap scan (e.g. days whose recording is malformed)")
    ap.add_argument("--splice-tail", action="store_true",
                    help="append only the missing tail of an existing file (needs --days)")
    ap.add_argument("--splice-windows", metavar="CSV",
                    help="fill mid-day holes listed in CSV (symbol,date,start_et,end_et)")
    ap.add_argument("--retime-stalls", action="store_true",
                    help="with --splice-windows: re-time recorder stalls instead of refusing them")
    ap.add_argument("--parquet-root", help=f"default {PARQUET_ROOT}")
    ap.add_argument("--continuous-root", help=f"default {NRD_ROOT}")
    args = ap.parse_args()

    if args.parquet_root:
        PARQUET_ROOT = Path(args.parquet_root)
    if args.continuous_root:
        NRD_ROOT = Path(args.continuous_root)

    key = os.environ.get("DATABENTO_API_KEY")
    if not key:
        print("DATABENTO_API_KEY is not set.", file=sys.stderr)
        return 2

    syms = ([s.strip().upper() for s in args.symbols.split(",")]
            if args.symbols else None)
    if args.splice_windows:
        days = [d.strip() for d in args.days.split(",")] if args.days else None
        return run_splice_windows(args, key, read_windows(args.splice_windows, syms, days))
    if args.days:
        days = [d.strip() for d in args.days.split(",")]
        gaps = {s: list(days) for s in (syms or sorted(MAINTAINED))}
    else:
        gaps = find_gaps(args.year, syms)
    if not syms:
        allow = MAINTAINED | ({"CL", "MCL"} if args.include_cl else set())
        gaps = {s: v for s, v in gaps.items() if s in allow}

    if not gaps:
        print("No fillable gaps found.")
        return 0

    if args.splice_tail:
        if not args.days:
            print("--splice-tail needs --days", file=sys.stderr)
            return 2
        return run_splice(args, key, gaps)

    total_days = sum(len(v) for v in gaps.values())
    print(f"Fillable gaps: {total_days} day-fetches across {len(gaps)} symbols\n")

    if args.cost:
        grand = 0.0
        for sym in sorted(gaps):
            sub = 0.0
            for day in gaps[sym]:
                d = datetime.strptime(day, "%Y%m%d").date()
                c = get_cost(key, sym, day)
                sub += c
                print(f"  {sym:<4} {day}  {raw_symbol(sym, d):<6} ${c:>8.4f}")
            grand += sub
            print(f"  {sym:<4} subtotal{'':<15}${sub:>8.4f}\n")
        print(f"TOTAL ESTIMATE: ${grand:.2f}  ({total_days} day-fetches, "
              f"schema={SCHEMA})")
        print("No data downloaded. Re-run with --fetch to download.")
        return 0

    written = 0
    for sym in sorted(gaps):
        for day in gaps[sym]:
            dst = out_path(sym, day)
            if dst.exists() and not args.overwrite:
                print(f"  {sym} {day}  exists, skipping")
                continue
            rows = fetch_tbbo(key, sym, day)
            if not rows:
                print(f"  {sym} {day}  NO DATA returned (market closed?)")
                continue
            table = to_l1_table(rows)
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_suffix(f".{os.getpid()}.tmp")
            pq.write_table(table, tmp, compression="zstd")
            os.replace(tmp, dst)
            written += 1
            print(f"  {sym} {day}  {len(rows):>8,} trades -> "
                  f"{table.num_rows:>9,} L1 events  {dst}")
    print(f"\nWrote {written} day files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
