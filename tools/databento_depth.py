#!/usr/bin/env python3
"""databento_depth.py -- fill missing NT8-style L2 (depth) Parquet from Databento MBP-10.

Why: about 13% of the L2 day files in the repo are empty (the recorder held only
trades + best bid/ask that day), spliced days lose depth after the splice, and
Databento-filled days have no L2 at all (TBBO is top of book). Databento's MBP-10
schema is the same market's 10-level book, so it can fill them.

What it does
------------
Each MBP-10 record carries the FULL 10-level book after one exchange event.
This tool diffs consecutive snapshots into NinjaTrader-style depth events, the
form the repo's `<SYM>-<YEAR>_L2` Parquet uses:

    Timestamp (ns, UTC) | MarketDataType 0 ask / 1 bid | Operation 0 add 1 update
    2 remove | Position (0 = best) | MarketMaker (null) | Price | Volume

Conventions copied from real NT8 data (see tests): the book starts with an `add`
for each of its 10 levels per side; a new best price is `add@0` then `rem@10`;
a vanished best is `rem@0` then `add@9`; a size change is `update@pos`. Replaying
the events with insert/delete/modify-at-position semantics reproduces the book.

Modes (all but `fetch --write` are read-only):
    cost      price the days that need depth (metadata.get_cost -- free to call)
    fetch     download MBP-10, convert, optionally replace the L2 file (--write)
    convert   convert a saved .dbn/.dbn.zst file offline, no network, no key
    validate  compare a converted day with the NT8 depth already in the repo
    selftest  synthetic round trip, needs nothing

Needs only numpy + pyarrow (the bridge venv has both). The API key is read from
DATABENTO_API_KEY and never printed. Raw downloads are cached (--cache-dir) so
re-running a conversion never bills twice.

    python databento_depth.py selftest
    python databento_depth.py cost --year 2026 --parquet-root M:\\RawData\\Parquet --sample 6
    python databento_depth.py validate --symbol ES --day 20260930 --window 14:00-14:10
    python databento_depth.py fetch --symbol ES --day 20260429 --write
"""
from __future__ import annotations

import argparse
import base64
import collections
import csv
import hashlib
import io
import json
import os
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import databento_fill as DF          # roll rules, ET day windows  # noqa: E402

API = "https://hist.databento.com/v0"
DATASET = "GLBX.MDP3"
SCHEMA = "mbp-10"
SOURCE_NAME = "databento GLBX.MDP3 mbp-10"
CONVERTER_VERSION = "1"

UNDEF_PRICE = np.iinfo(np.int64).max        # DBN: price not set (missing level)
PX_SCALE = 1e-9                              # DBN fixed-point prices
RTYPE_MBP10 = 0x0A
DEPTH = 10                                   # book levels per side
ASK, BID = 0, 1                              # NT8 MarketDataType
ADD, UPDATE, REMOVE = 0, 1, 2                # NT8 Operation

_LEVEL = np.dtype([("bid_px", "<i8"), ("ask_px", "<i8"), ("bid_sz", "<u4"),
                   ("ask_sz", "<u4"), ("bid_ct", "<u4"), ("ask_ct", "<u4")])
MBP10 = np.dtype([("length", "u1"), ("rtype", "u1"), ("publisher_id", "<u2"),
                  ("instrument_id", "<u4"), ("ts_event", "<u8"), ("price", "<i8"),
                  ("size", "<u4"), ("action", "u1"), ("side", "u1"), ("flags", "u1"),
                  ("depth", "u1"), ("ts_recv", "<u8"), ("ts_in_delta", "<i4"),
                  ("sequence", "<u4"), ("levels", _LEVEL, (DEPTH,))])
assert MBP10.itemsize == 368, MBP10.itemsize

L2_SCHEMA = pa.schema([
    ("Timestamp", pa.timestamp("ns", tz="UTC")), ("MarketDataType", pa.int8()),
    ("Operation", pa.int8()), ("Position", pa.int32()), ("MarketMaker", pa.string()),
    ("Price", pa.float64()), ("Volume", pa.int64())])

ET = ZoneInfo("America/New_York")


class DbnError(ValueError):
    """The byte stream is not the MBP-10 DBN layout this module understands."""


# ---------------------------------------------------------------------------
# DBN reading
# ---------------------------------------------------------------------------

def open_stream(src, compressed: bool | None = None):
    """-> readable binary stream of DBN bytes from a path or a file-like object.

    Databento sends the whole DBN stream zstd-framed when asked for
    compression=zstd; pyarrow ships a zstd codec, so no extra package is needed.
    A path ending in .zst, or a stream that starts with the zstd magic, is
    decompressed on the fly.
    """
    raw = open(src, "rb") if isinstance(src, (str, os.PathLike)) else src
    if compressed is None:
        name = str(src).lower() if isinstance(src, (str, os.PathLike)) else ""
        compressed = name.endswith(".zst")
    if not compressed:
        return raw
    return pa.CompressedInputStream(pa.PythonFile(raw, mode="r"), "zstd")


def _read_exact(stream, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        part = stream.read(n - len(buf))
        if not part:
            break
        buf += part
    return bytes(buf)


def read_dbn_header(stream) -> dict:
    """Consume the DBN metadata block; -> {'version': int}."""
    head = _read_exact(stream, 8)
    if len(head) < 8 or head[:3] != b"DBN":
        raise DbnError(f"not a DBN stream (starts {head[:8]!r})")
    version = head[3]
    meta_len = int.from_bytes(head[4:8], "little")
    meta = _read_exact(stream, meta_len)
    if len(meta) != meta_len:
        raise DbnError("truncated DBN metadata")
    return {"version": version, "metadata_len": meta_len}


def iter_records(stream, chunk_records: int = 400_000):
    """Yield numpy arrays of MBP10 records, `chunk_records` at a time.

    Every record is checked (length 92 words = 368 bytes, rtype 0x0A). Anything
    else raises DbnError rather than being skipped, so a changed layout can never
    quietly produce a plausible but wrong book.
    """
    read_dbn_header(stream)
    size = MBP10.itemsize
    want = chunk_records * size
    leftover = b""
    while True:
        data = leftover + _read_exact(stream, want - len(leftover))
        n = len(data) // size
        leftover = data[n * size:]
        if n:
            arr = np.frombuffer(data, dtype=MBP10, count=n)
            odd = (arr["length"] != size // 4) | (arr["rtype"] != RTYPE_MBP10)
            if odd.any():
                i = int(np.nonzero(odd)[0][0])
                raise DbnError(f"record {i} of the chunk is rtype {int(arr['rtype'][i]):#x}, "
                               f"{int(arr['length'][i])} words; expected MBP-10 "
                               f"({RTYPE_MBP10:#x}, {size // 4}). Layout changed?")
            yield arr
        if len(data) < want:                 # short read = end of stream
            if leftover:
                raise DbnError(f"{len(leftover)} trailing bytes do not make a record")
            return


# ---------------------------------------------------------------------------
# snapshot -> NT8 depth events
# ---------------------------------------------------------------------------

def _side_arrays(arr: np.ndarray, side: int):
    """-> (px int64 (n,10), sz int64 (n,10)) with missing levels normalised."""
    lv = arr["levels"]
    px = lv["ask_px" if side == ASK else "bid_px"].astype(np.int64)
    sz = lv["ask_sz" if side == ASK else "bid_sz"].astype(np.int64)
    missing = (px == UNDEF_PRICE) | (sz == 0)
    return np.where(missing, UNDEF_PRICE, px), np.where(missing, 0, sz)


def diff_side(old: list, new: list, side: int) -> list:
    """NT8-style depth ops turning book `old` into `new`.

    old/new: lists of (price_int, size) best-first, at most 10 long. Returns
    [(op, pos, price_int, size), ...] in emission order. Same conventions as the
    recorded data: removals first (highest position first), then inserts in
    ascending position each followed at once by the overflow `rem@10` when the
    book would exceed ten levels, then size updates. No position is ever above 10. That yields `add@0, rem@10` for a new best price and
    `rem@0, add@9` for a vanished one.
    """
    if old == new:
        return []
    ops: list = []
    cur = [list(x) for x in old]
    new_px = {p for p, _ in new}
    worst = new[-1][0] if len(new) == DEPTH else None

    def pushed_out(p: int) -> bool:          # fell off the 10-level window, not cancelled
        if worst is None:
            return False
        return p > worst if side == ASK else p < worst

    for i in range(len(cur) - 1, -1, -1):
        p = cur[i][0]
        if p not in new_px and not pushed_out(p):
            ops.append((REMOVE, i, p, 0))          # NT8 writes Volume 0 on a remove
            del cur[i]
    for j, (p, s) in enumerate(new):
        if j < len(cur) and cur[j][0] == p:
            continue
        cur.insert(j, [p, s])
        ops.append((ADD, j, p, s))
        if len(cur) > DEPTH:
            # Trim at once, as the recorder does: the tail (index 10) is always a level pushed
            # out of the window, and Position never exceeds 10 however many levels move.
            ops.append((REMOVE, DEPTH, cur[DEPTH][0], cur[DEPTH][1]))
            cur.pop()
    while len(cur) > len(new):
        i = len(cur) - 1
        ops.append((REMOVE, i, cur[i][0], cur[i][1]))   # overflow keeps the dropped level's size, as NT8 does
        cur.pop()
    for j, (p, s) in enumerate(new):
        if cur[j][1] != s:
            ops.append((UPDATE, j, p, s))
            cur[j][1] = s
    return ops


class Converter:
    """Streaming MBP-10 -> NT8 depth events. Holds the running book between chunks."""

    def __init__(self, initial_book: dict | None = None):
        # book[side] = (px int64 (10,), sz int64 (10,)), normalised like _side_arrays
        empty = (np.full(DEPTH, UNDEF_PRICE, np.int64), np.zeros(DEPTH, np.int64))
        self.state = {ASK: empty, BID: empty}
        if initial_book:
            for side in (ASK, BID):
                levels = initial_book.get(side, [])[:DEPTH]
                px = np.full(DEPTH, UNDEF_PRICE, np.int64)
                sz = np.zeros(DEPTH, np.int64)
                for i, (p, s) in enumerate(levels):
                    px[i], sz[i] = p, s
                self.state[side] = (px, sz)
        self.last_ts = 0
        self.ts_regressions = 0
        self.records = 0

    def convert(self, arr: np.ndarray) -> dict:
        """-> dict of equal-length numpy columns for this chunk's events."""
        n = len(arr)
        ts = arr["ts_recv"].astype(np.int64)
        if n:
            self.ts_regressions += int((np.diff(np.concatenate([[self.last_ts], ts])) < 0).sum()
                                       if self.last_ts else (np.diff(ts) < 0).sum())
        ts = np.maximum.accumulate(np.maximum(ts, self.last_ts))
        parts = []
        for side in (ASK, BID):
            px, sz = _side_arrays(arr, side)
            prev_px, prev_sz = self.state[side]
            pxa = np.vstack([prev_px[None, :], px])
            sza = np.vstack([prev_sz[None, :], sz])
            same = (pxa[1:] == pxa[:-1]).all(axis=1)
            chg = sza[1:] != sza[:-1]
            r, c = np.nonzero(chg & same[:, None])           # size-only changes: vectorised
            parts.append((r, np.full(len(r), side, np.int8), c.astype(np.int64),
                          np.full(len(r), UPDATE, np.int8), c.astype(np.int32),
                          px[r, c], sz[r, c]))
            rows = np.nonzero(~same)[0]                       # structural changes: python
            if len(rows):
                olds_px, olds_sz = pxa[:-1][rows].tolist(), sza[:-1][rows].tolist()
                news_px, news_sz = px[rows].tolist(), sz[rows].tolist()
                rr, kk, oo, pp, qq, ss = [], [], [], [], [], []
                for q, row in enumerate(rows.tolist()):
                    old = [(a, b) for a, b in zip(olds_px[q], olds_sz[q]) if a != UNDEF_PRICE]
                    new = [(a, b) for a, b in zip(news_px[q], news_sz[q]) if a != UNDEF_PRICE]
                    for k, (op, pos, p, s) in enumerate(diff_side(old, new, side)):
                        rr.append(row); kk.append(k); oo.append(op); pp.append(pos)
                        qq.append(p); ss.append(s)
                parts.append((np.asarray(rr, np.int64), np.full(len(rr), side, np.int8),
                              np.asarray(kk, np.int64), np.asarray(oo, np.int8),
                              np.asarray(pp, np.int32), np.asarray(qq, np.int64),
                              np.asarray(ss, np.int64)))
            self.state[side] = (px[-1].copy(), sz[-1].copy()) if n else (prev_px, prev_sz)
        row = np.concatenate([p[0] for p in parts]); side_c = np.concatenate([p[1] for p in parts])
        k = np.concatenate([p[2] for p in parts]); op = np.concatenate([p[3] for p in parts])
        pos = np.concatenate([p[4] for p in parts]); pxi = np.concatenate([p[5] for p in parts])
        szc = np.concatenate([p[6] for p in parts])
        order = np.lexsort((k, side_c, row))                  # by record, ask before bid, emission order
        row, side_c, op, pos, pxi, szc = (a[order] for a in (row, side_c, op, pos, pxi, szc))
        rec = self.records + row                      # global record index of each event
        self.records += n
        if n:
            self.last_ts = int(ts[-1])
        return {"ts": ts[row], "side": side_c, "op": op, "pos": pos,
                "price": pxi.astype(np.float64) * PX_SCALE, "size": szc,
                "price_int": pxi, "rec": rec}


def events_to_table(ev: dict, meta: dict | None = None) -> pa.Table:
    n = len(ev["ts"])
    px = np.round(ev["price"], 9)
    table = pa.table({
        "Timestamp": pa.array(ev["ts"].view("datetime64[ns]"), type=pa.timestamp("ns", tz="UTC")),
        "MarketDataType": pa.array(ev["side"].astype(np.int8)),
        "Operation": pa.array(ev["op"].astype(np.int8)),
        "Position": pa.array(ev["pos"].astype(np.int32)),
        "MarketMaker": pa.nulls(n, pa.string()),
        "Price": pa.array(px),
        "Volume": pa.array(ev["size"].astype(np.int64)),
    }, schema=L2_SCHEMA)
    return table


def schema_metadata_bytes(extra: dict, keep_source: bool) -> dict:
    """Metadata for a day that keeps its NT8 provenance (spliced): no databento source_name."""
    base = {"databento.schema": SCHEMA, "databento.dataset": DATASET,
            "databento.depth_converter": CONVERTER_VERSION}
    base.update(extra)
    return {k.encode(): str(v).encode() for k, v in base.items()}


def schema_metadata(extra: dict) -> dict:
    base = {"replay_importer.timestamps": "UTC",
            "replay_importer.source_name": SOURCE_NAME,
            "databento.schema": SCHEMA, "databento.dataset": DATASET,
            "databento.depth_converter": CONVERTER_VERSION}
    base.update(extra)
    return {k.encode(): str(v).encode() for k, v in base.items()}

def book_at_end(table: pa.Table) -> tuple[dict, int]:
    """Replay an NT8 L2 table -> ({side: [(price_int, size)...]}, last_ts_ns)."""
    ts = table.column("Timestamp").cast("int64").to_numpy()
    sd = table.column("MarketDataType").to_numpy(); op = table.column("Operation").to_numpy()
    ps = table.column("Position").to_numpy(); px = table.column("Price").to_numpy()
    sz = table.column("Volume").to_numpy()
    b = Book()
    for i in range(len(ts)):
        b.apply(int(sd[i]), int(op[i]), int(ps[i]), float(px[i]), int(sz[i]))
    book = {s_: [(int(round(p / PX_SCALE)), int(v)) for p, v in b.s[s_][:DEPTH]] for s_ in (ASK, BID)}
    return book, int(ts[-1]) if len(ts) else 0


def splice_fill(existing: Path, stream, out: Path, extra_meta: dict) -> dict:
    """Keep the NT8 depth already in `existing`, continue it from Databento after its last event.

    The converter starts from the NT8 book at the seam, so the first Databento record emits
    the small correction between the two feeds and the result replays as one stream.
    """
    old = pq.read_table(existing)
    book, last_ts = book_at_end(old)
    md = dict(old.schema.metadata or {})
    md.update(schema_metadata_bytes(extra_meta, keep_source=True))
    with pq.ParquetWriter(str(out), L2_SCHEMA.with_metadata(md), compression="zstd") as w:
        w.write_table(old.cast(L2_SCHEMA))
        stats = convert_stream(stream, w, initial_book=book, min_ts=last_ts + 1)
    stats["seam_ns"] = last_ts
    stats["existing_rows"] = old.num_rows
    return stats


def convert_stream(stream, writer=None, initial_book=None, min_ts: int = 0,
                   chunk_records: int = 400_000) -> dict:
    """Convert a whole DBN stream; write chunks to `writer` (pq.ParquetWriter) if given.

    min_ts drops events stamped before it (for a spliced day that already has
    depth up to there). Returns stats incl. the final book.
    """
    conv = Converter(initial_book)
    rows = 0
    first_ts = last_ts = None
    t0 = time.time()
    for arr in iter_records(stream, chunk_records):
        if min_ts:
            # Records before the seam are dropped WITHOUT touching the running book: it was
            # seeded with the NT8 book at the seam, and the first kept record then emits
            # the correction from that book to Databento's.
            arr = arr[arr["ts_recv"] >= np.uint64(min_ts)]
            if not len(arr):
                continue
        ev = conv.convert(arr)
        if len(ev["ts"]):
            rows += len(ev["ts"])
            first_ts = int(ev["ts"][0]) if first_ts is None else first_ts
            last_ts = int(ev["ts"][-1])
            if writer is not None:
                writer.write_table(events_to_table(ev))
    return {"records": conv.records, "events": rows, "first_ts": first_ts, "last_ts": last_ts,
            "ts_regressions": conv.ts_regressions, "seconds": round(time.time() - t0, 1),
            "book": {s: _book_list(conv.state[s]) for s in (ASK, BID)}}


def _book_list(state) -> list:
    px, sz = state
    return [(int(p), int(s)) for p, s in zip(px, sz) if p != UNDEF_PRICE]


# ---------------------------------------------------------------------------
# reference book replay (tests and validation)
# ---------------------------------------------------------------------------

class Book:
    """NT8 depth book. Same insert/delete/modify-at-position semantics as the data."""

    def __init__(self):
        self.s = {ASK: [], BID: []}

    def apply(self, side: int, op: int, pos: int, price: float, size: int) -> None:
        b = self.s[side]
        if op == ADD:
            b.insert(min(pos, len(b)), [price, size])
        elif op == UPDATE:
            if pos < len(b):
                b[pos] = [price, size]
        elif pos < len(b):
            del b[pos]

    def snapshot(self) -> tuple:
        return tuple(tuple(map(tuple, self.s[sd][:DEPTH])) for sd in (ASK, BID))


# ---------------------------------------------------------------------------
# Databento HTTP (stdlib)
# ---------------------------------------------------------------------------

def _key() -> str:
    key = os.environ.get("DATABENTO_API_KEY", "")
    if not key:
        sys.exit("Set DATABENTO_API_KEY in this session first (it is never printed).")
    return key


def _auth(key: str) -> str:
    return "Basic " + base64.b64encode(f"{key}:".encode()).decode()


def _api(method: str, endpoint: str, params: dict, key: str, stream: bool = False):
    body = urllib.parse.urlencode(params).encode()
    req = (urllib.request.Request(f"{API}/{endpoint}?{body.decode()}") if method == "GET"
           else urllib.request.Request(f"{API}/{endpoint}", data=body))
    req.add_header("Authorization", _auth(key))
    for attempt in range(5):
        try:
            resp = urllib.request.urlopen(req, timeout=900)
            return resp if stream else resp.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()[:300]
            except Exception:
                pass
            if e.code < 500 or attempt == 4:
                raise RuntimeError(f"Databento {endpoint}: HTTP {e.code} {detail}") from None
            why = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == 4:
                raise
            why = str(e)
        wait = 10 * 2 ** attempt
        print(f"    (Databento {endpoint}: {why}; retrying in {wait}s)", flush=True)
        time.sleep(wait)


def request_params(symbol: str, day: str, span: tuple[str, str] | None = None) -> dict:
    lo, hi = span or DF.day_window(day)
    return {"dataset": DATASET,
            "symbols": DF.raw_symbol(symbol, datetime.strptime(day, "%Y%m%d").date()),
            "schema": SCHEMA, "start": lo, "end": hi, "stype_in": "raw_symbol"}


def get_cost(key: str, symbol: str, day: str, span=None) -> dict:
    """metadata.get_cost (+ record count and billable bytes). All three are free calls."""
    params = request_params(symbol, day, span)
    out = {"symbol": symbol, "day": day, "raw": params["symbols"],
           "cost": float(json.loads(_api("GET", "metadata.get_cost", params, key)))}
    for name, field in (("metadata.get_record_count", "records"),
                        ("metadata.get_billable_size", "bytes")):
        try:
            out[field] = int(json.loads(_api("GET", name, params, key)))
        except Exception as e:        # informational only
            out[field] = None
            out.setdefault("notes", []).append(f"{field}: {e}")
    return out


def download(key: str, symbol: str, day: str, dest: Path, span=None) -> Path:
    """Stream the raw zstd DBN to `dest` (cached: an existing file is reused, free)."""
    if dest.exists() and dest.stat().st_size > 0:
        print(f"    cache hit {dest.name} ({dest.stat().st_size / 1e6:.0f} MB) -- no download, no charge")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    params = dict(request_params(symbol, day, span), encoding="dbn", compression="zstd")
    tmp = dest.with_suffix(dest.suffix + ".part")
    with _api("POST", "timeseries.get_range", params, key, stream=True) as resp, open(tmp, "wb") as f:
        while True:
            block = resp.read(8 << 20)
            if not block:
                break
            f.write(block)
    os.replace(tmp, dest)
    return dest


# ---------------------------------------------------------------------------
# targets: which symbol-days need depth
# ---------------------------------------------------------------------------

KINDS = ("empty", "databento", "spliced", "nofile")


def scan_targets(parquet_root: Path, year: int, kinds: set, symbols: list | None = None,
                 since: str = "", until: str = "99999999", weekdays_only: bool = False) -> list:
    """Symbol-days of `year` whose L2 is empty / absent / partial. Saturdays are never targets.

    Only the maintained symbols unless `symbols` says otherwise (CL/MCL are not wanted).
    """
    out = []
    wanted = set(symbols) if symbols else set(DF.MAINTAINED)
    for l1dir in sorted((parquet_root / str(year)).glob("*-*_L1")):
        sym = l1dir.name.split("-")[0]
        if sym not in wanted:
            continue
        for f in sorted(l1dir.glob("*.parquet")):
            day = f.stem
            if not (since <= day <= until) or len(day) != 8:
                continue
            wd = datetime.strptime(day, "%Y%m%d").weekday()
            if wd == 5 or (weekdays_only and wd == 6):
                continue
            md = pq.read_schema(f).metadata or {}
            src = md.get(b"replay_importer.source_name", b"").decode()
            l2 = l1dir.parent / f"{sym}-{year}_L2" / f"{day}.parquet"
            n2 = pq.ParquetFile(l2).metadata.num_rows if l2.exists() else None
            if l2.exists() and b"databento.depth_converter" in (pq.read_schema(l2).metadata or {}):
                continue                                  # already filled by this tool: resumable
            if "databento" in src:
                if n2:                                    # a Databento L1 day that already has depth
                    continue
                kind = "databento"
            elif b"splice.windows" in md or b"splice.after_ns" in md:
                kind = "spliced"
            elif n2 is None:
                kind = "nofile"
            elif n2 == 0:
                kind = "empty"
            else:
                continue
            if kind in kinds:
                out.append({"symbol": sym, "day": day, "kind": kind})
    return out


# ---------------------------------------------------------------------------
# validation against the NT8 depth in the repo
# ---------------------------------------------------------------------------

def _hms_ns(day: str, hms: str) -> int:
    d = datetime.strptime(day + hms, "%Y%m%d%H:%M").replace(tzinfo=ET)
    return int(d.timestamp()) * 1_000_000_000


def validate_events(nt8: dict, db: dict, lo_ns: int, hi_ns: int, step_s: float = 5.0,
                    tol_ms: float = 150.0, levels=(1, 3, 5, 10)) -> dict:
    """Compare NT8 depth with converted Databento depth over [lo_ns, hi_ns].

    NT8 events are replayed from the start of the day (a book can only be rebuilt
    from the beginning); the converted Databento events start with its own full
    snapshot. At each sample time the NT8 book is compared with every Databento
    book state within +-tol_ms, because the two feeds are stamped by different
    clocks. Reports the share of samples where the top-k levels (price AND size)
    match exactly, per k, both within the tolerance and at the same instant. Because
    thin books (silver: 1-5 contracts a level) make a +-1 size difference look like a
    different book, it also reports prices-only matching and the distribution of the
    size difference at levels whose price agrees.
    """
    tol = int(tol_ms * 1e6)
    samples = np.arange(lo_ns, hi_ns, int(step_s * 1e9)).tolist()
    n_ts, d_ts = nt8["ts"], db["ts"]
    n_n, n_d = len(n_ts), len(d_ts)
    nb, dbk = Book(), Book()
    ni = di = 0
    hist = collections.deque()              # (ts, snapshot) for Databento events in the window
    base = dbk.snapshot()                   # Databento book just before hist[0]
    within = {k: 0 for k in levels}
    same = {k: 0 for k in levels}
    price_within = {k: 0 for k in levels}
    size_diff = collections.Counter()       # |NT8 size - Databento size| at levels whose price agrees
    total = 0

    def top(snap, k):
        return tuple(x[:k] for x in snap)

    def prices(snap, k):
        return tuple(tuple(p for p, _ in x[:k]) for x in snap)

    for t in samples:
        while ni < n_n and n_ts[ni] <= t:
            nb.apply(int(nt8["side"][ni]), int(nt8["op"][ni]), int(nt8["pos"][ni]),
                     round(float(nt8["price"][ni]), 9), int(nt8["size"][ni]))
            ni += 1
        while di < n_d and d_ts[di] <= t + tol:
            dbk.apply(int(db["side"][di]), int(db["op"][di]), int(db["pos"][di]),
                      round(float(db["price"][di]), 9), int(db["size"][di]))
            hist.append((int(d_ts[di]), dbk.snapshot()))
            di += 1
        while hist and hist[0][0] < t - tol:
            base = hist.popleft()[1]
        ns = nb.snapshot()
        if not ns[0] and not ns[1]:
            continue
        cands = [base] + [h[1] for h in hist]
        at_t = base
        for ts_h, snap in hist:
            if ts_h <= t:
                at_t = snap
            else:
                break
        total += 1
        for k in levels:
            if any(top(c, k) == top(ns, k) for c in cands):
                within[k] += 1
            if top(at_t, k) == top(ns, k):
                same[k] += 1
            if any(prices(c, k) == prices(ns, k) for c in cands):
                price_within[k] += 1
        for side_n, side_d in zip(ns, at_t):
            for (pn, sn), (pd, sd) in zip(side_n, side_d):
                if pn == pd:
                    d = abs(sn - sd)
                    size_diff["0" if d == 0 else "1" if d == 1 else "2-3" if d <= 3 else "4+"] += 1
    pct = lambda d: {k: (round(v / total, 4) if total else None) for k, v in d.items()}
    n_lv = sum(size_diff.values())
    out_sizes = {k: round(v / n_lv, 4) for k, v in sorted(size_diff.items())} if n_lv else {}
    return {"samples": total, "tolerance_ms": tol_ms,
            "match_within_tolerance": pct(within), "match_same_instant": pct(same),
            "prices_only_within_tolerance": pct(price_within),
            "size_difference_where_price_agrees": out_sizes}


def load_nt8_events(path: Path) -> dict:
    t = pq.read_table(path)
    return {"ts": t.column("Timestamp").cast("int64").to_numpy(),
            "side": t.column("MarketDataType").to_numpy(),
            "op": t.column("Operation").to_numpy(),
            "pos": t.column("Position").to_numpy(),
            "price": t.column("Price").to_numpy(),
            "size": t.column("Volume").to_numpy()}


def convert_to_memory(stream, **kw) -> tuple[dict, dict]:
    """Run the converter, collecting events in memory (validation of a window or small day)."""
    class _Collect:
        def __init__(self):
            self.t = []
        def write_table(self, table):
            self.t.append(table)
    col = _Collect()
    stats = convert_stream(stream, col, **kw)
    if not col.t:
        return stats, {k: np.array([]) for k in ("ts", "side", "op", "pos", "price", "size")}
    tbl = pa.concat_tables(col.t)
    return stats, {"ts": tbl.column("Timestamp").cast("int64").to_numpy(),
                   "side": tbl.column("MarketDataType").to_numpy(),
                   "op": tbl.column("Operation").to_numpy(),
                   "pos": tbl.column("Position").to_numpy(),
                   "price": tbl.column("Price").to_numpy(),
                   "size": tbl.column("Volume").to_numpy()}


# ---------------------------------------------------------------------------
# selftest (no network, no data)
# ---------------------------------------------------------------------------

def _synthetic_stream(n: int, seed: int = 7, tick: float = 0.25) -> tuple[bytes, list]:
    """Random-walk order book -> MBP-10 records as DBN bytes, plus the true 10-level snapshots.

    The book is kept deeper than 10 levels so levels enter and leave the visible
    window, and it never crosses. Integer tick prices.
    """
    rng = np.random.default_rng(seed)
    asks = {1000 + i: int(rng.integers(5, 90)) for i in range(1, 16)}
    bids = {1000 - i: int(rng.integers(5, 90)) for i in range(0, 15)}
    recs = np.zeros(n, dtype=MBP10)
    snaps = []
    ts = 1_700_000_000_000_000_000
    for i in range(n):
        ts += int(rng.integers(1_000, 5_000_000))
        side_ask = rng.random() < 0.5
        book = asks if side_ask else bids
        r = rng.random()
        if r < 0.50:                                           # size change
            p = list(book)[int(rng.integers(min(len(book), 14)))]
            book[p] = max(1, book[p] + int(rng.integers(-4, 9)))
        elif r < 0.68:                                         # new level: at/near the touch or deeper
            if side_ask:
                p = (min(asks) - int(rng.integers(1, 3))) if rng.random() < 0.4 else (min(asks) + int(rng.integers(1, 15)))
                ok = p > max(bids)
            else:
                p = (max(bids) + int(rng.integers(1, 3))) if rng.random() < 0.4 else (max(bids) - int(rng.integers(1, 15)))
                ok = p < min(asks)
            if ok and p not in book:
                book[p] = int(rng.integers(5, 90))
        elif r < 0.88 and len(book) > 12:                      # cancel a level, often the best
            p = (min(book) if side_ask else max(book)) if rng.random() < 0.5 else list(book)[int(rng.integers(len(book)))]
            del book[p]
        # else: a trade -- the book does not change
        while len(asks) < 14:
            asks[max(asks) + 1] = int(rng.integers(5, 90))
        while len(bids) < 14:
            bids[min(bids) - 1] = int(rng.integers(5, 90))
        a = sorted(asks.items())[:DEPTH]
        b = sorted(bids.items(), reverse=True)[:DEPTH]
        rec = recs[i]
        rec["length"], rec["rtype"], rec["ts_recv"], rec["ts_event"] = 92, RTYPE_MBP10, ts, ts - 1000
        lv = rec["levels"]
        lv["ask_px"] = UNDEF_PRICE
        lv["bid_px"] = UNDEF_PRICE
        for j, (p, s_) in enumerate(a):
            lv["ask_px"][j], lv["ask_sz"][j] = int(round(p * tick / PX_SCALE)), s_
        for j, (p, s_) in enumerate(b):
            lv["bid_px"][j], lv["bid_sz"][j] = int(round(p * tick / PX_SCALE)), s_
        snaps.append((tuple((round(p * tick, 9), s_) for p, s_ in a),
                      tuple((round(p * tick, 9), s_) for p, s_ in b)))
    head = b"DBN" + bytes([2]) + (16).to_bytes(4, "little") + b"\0" * 16
    return head + recs.tobytes(), snaps


def selftest(n: int = 20000) -> bool:
    """Convert a synthetic stream, replay the events, and compare with the true book after EVERY record."""
    data, snaps = _synthetic_stream(n)
    ok = True
    for compressed in (False, True):
        raw = data
        if compressed:
            raw = pa.compress(data, codec="zstd", asbytes=True)
        stream = open_stream(io.BytesIO(raw), compressed=compressed)
        conv = Converter()
        book = Book()
        bad = events = rec_seen = 0
        for arr in iter_records(stream, chunk_records=1500):      # small chunks: exercises the carry-over
            ev = conv.convert(arr)
            events += len(ev["ts"])
            order = ev["rec"].tolist()
            q = 0
            for r in range(rec_seen, rec_seen + len(arr)):
                while q < len(order) and order[q] == r:
                    book.apply(int(ev["side"][q]), int(ev["op"][q]), int(ev["pos"][q]),
                               round(float(ev["price"][q]), 9), int(ev["size"][q]))
                    q += 1
                got = tuple(tuple((p, sz) for p, sz in book.s[sd][:DEPTH]) for sd in (ASK, BID))
                if got != snaps[r]:
                    bad += 1
            rec_seen += len(arr)
        print(f"selftest zstd={compressed}: {rec_seen} records -> {events} events, "
              f"{bad} records whose replayed book differs, ts_regressions={conv.ts_regressions}")
        ok = ok and bad == 0 and rec_seen == n
    return ok


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def default_parquet_root() -> Path:
    return Path(os.environ.get("BACKTESTER_DATA_ROOT") or r"M:\RawData\Parquet")


def l2_path(root: Path, sym: str, day: str) -> Path:
    season = DF.season_year(sym, day)
    return root / str(season) / f"{sym}-{season}_L2" / f"{day}.parquet"


def cmd_cost(a) -> int:
    root = Path(a.parquet_root)
    kinds = set(a.kinds.split(","))
    syms = a.symbols.split(",") if a.symbols else None
    if a.days:
        targets = [{"symbol": s, "day": d, "kind": "manual"}
                   for s in (syms or ["ES"]) for d in a.days.split(",")]
    else:
        targets = scan_targets(root, a.year, kinds, syms, a.since, a.until)
    if a.sample and len(targets) > a.sample:
        step = len(targets) / a.sample
        targets = [targets[int(i * step)] for i in range(a.sample)]
    print(f"{len(targets)} symbol-days to price (metadata calls are free)")
    key = _key()
    rows, total = [], 0.0
    for t in targets:
        try:
            c = get_cost(key, t["symbol"], t["day"])
        except Exception as e:
            print(f"  {t['symbol']} {t['day']}: {e}")
            continue
        c["kind"] = t["kind"]
        rows.append(c)
        total += c["cost"]
        mb = f"{c['bytes'] / 1e6:,.1f} MB" if c.get("bytes") else "?"
        rec = f"{c['records']:,}" if c.get("records") else "?"
        print(f"  {t['symbol']:4} {t['day']} {c['raw']:6} {t['kind']:10} ${c['cost']:9.2f}  {rec:>12} records  {mb:>10}")
    print(f"TOTAL ${total:,.2f} for {len(rows)} symbol-days" + (" (a sample; extrapolate)" if a.sample else ""))
    if rows and not a.sample:
        for label, key in (("symbol", lambda r: r["symbol"]), ("kind", lambda r: r["kind"]),
                           ("month", lambda r: r["day"][:6])):
            agg = collections.defaultdict(lambda: [0, 0.0])
            for r in rows:
                agg[key(r)][0] += 1
                agg[key(r)][1] += r["cost"]
            print(f"  by {label}: " + "  ".join(f"{k} {n}d ${c:,.0f}" for k, (n, c) in sorted(agg.items())))
        top = sorted(rows, key=lambda r: -r["cost"])[:5]
        print("  five dearest days: " + "  ".join(f"{r['symbol']} {r['day']} ${r['cost']:.2f}" for r in top))
    if a.out and rows:
        with open(a.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["symbol", "day", "kind", "raw", "cost", "records", "bytes"],
                               extrasaction="ignore")
            w.writeheader(); w.writerows(rows)
        print("wrote", a.out)
    return 0


def _writer(path: Path, meta: dict) -> pq.ParquetWriter:
    return pq.ParquetWriter(str(path), L2_SCHEMA.with_metadata(schema_metadata(meta)), compression="zstd")


def cmd_convert(a) -> int:
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    with _writer(tmp, {"databento.source_file": Path(a.from_file).name}) as w:
        stats = convert_stream(open_stream(a.from_file), w)
    os.replace(tmp, out)
    print(json.dumps({k: v for k, v in stats.items() if k != "book"}, indent=1))
    return 0


def cmd_validate(a) -> int:
    root = Path(a.parquet_root)
    nt8_path = l2_path(root, a.symbol, a.day)
    nt8 = load_nt8_events(nt8_path)
    if len(nt8["ts"]) == 0:
        sys.exit(f"{nt8_path} has no NT8 depth -- pick a day that has some to compare against")
    lo_s, hi_s = a.window.split("-")
    lo, hi = _hms_ns(a.day, lo_s), _hms_ns(a.day, hi_s)
    span = (datetime.fromtimestamp((lo - 60 * 10**9) // 10**9, ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S"),
            datetime.fromtimestamp(-(-(hi + 10**9) // 10**9), ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S"))
    if a.from_file:
        src = Path(a.from_file)
    else:
        key = _key()
        print("cost of this request: $%.2f" % get_cost(key, a.symbol, a.day, span)["cost"])
        if not a.yes:
            sys.exit("re-run with --yes to download it")
        src = download(key, a.symbol, a.day, Path(a.cache_dir) /
                       f"{a.symbol}_{a.day}_{lo_s.replace(':', '')}-{hi_s.replace(':', '')}.mbp10.dbn.zst", span)
    stats, db = convert_to_memory(open_stream(src))
    print({k: v for k, v in stats.items() if k != "book"})
    print(json.dumps(validate_events(nt8, db, lo, hi, a.step, a.tol_ms), indent=1))
    return 0


def _commit(write: bool, hold_root: str, tmp: Path, dest: Path, stats: dict) -> str:
    """Dry run unless `write`; on write the old file goes to hold/ first. -> status word."""
    if stats["events"] == 0:
        tmp.unlink(missing_ok=True)
        return "no depth events returned (market closed?)"
    if not write:
        tmp.unlink(missing_ok=True)
        return "dry run: converted OK, nothing written"
    if dest.exists():
        hold = Path(hold_root) / f"l2-replaced-{date.today():%Y%m%d}" / dest.parent.name
        hold.mkdir(parents=True, exist_ok=True)
        target = hold / dest.name
        if target.exists():                      # never overwrite an earlier backup of this day
            target = hold / f"{dest.stem}.{int(time.time())}{dest.suffix}"
        os.replace(dest, target)
    os.replace(tmp, dest)
    return "wrote"


def fill_day(key: str, root: Path, sym: str, day: str, *, splice: bool = False, overwrite: bool = False,
             write: bool = False, cache_dir: str = "dbn_cache", hold_root: str = "hold",
             max_cost: float | None = None, price_only: bool = False) -> dict:
    """Price, download, convert and (with write) replace ONE symbol-day's L2. Returns a status row."""
    t0 = time.time()
    row = {"symbol": sym, "day": day, "mode": "splice" if splice else "full", "status": "", "cost": None,
           "records": None, "events": None, "charged": False, "secs": 0}
    dest = l2_path(root, sym, day)
    have = pq.ParquetFile(dest).metadata.num_rows if dest.exists() else None
    if splice and not have:
        row["status"] = "skipped: --splice needs an L2 file with some depth already"
        return row
    if not splice and have and not overwrite:
        row["status"] = f"skipped: L2 already has {have:,} rows (--overwrite, or --splice for a partial day)"
        return row
    raw = DF.raw_symbol(sym, datetime.strptime(day, "%Y%m%d").date())
    span, tag, seam = None, "", 0
    if splice:
        _, seam = book_at_end(pq.read_table(dest))
        lo_str, _ = DF.span_window(seam, seam)
        span = (lo_str, DF.day_window(day)[1])
        tag = f"_from{seam}"
    cost = get_cost(key, sym, day, span)
    row.update(cost=cost["cost"], records=cost.get("records"), contract=raw)
    if max_cost is not None and cost["cost"] > max_cost:
        row["status"] = f"skipped: ${cost['cost']:.2f} is over --max-cost {max_cost}"
        return row
    if price_only:
        row["status"] = "priced"
        return row
    cache = Path(cache_dir) / f"{sym}_{day}{tag}.mbp10.dbn.zst"
    row["charged"] = not (cache.exists() and cache.stat().st_size > 0)
    src = download(key, sym, day, cache, span)
    tmp = dest.with_suffix(".tmp")
    dest.parent.mkdir(parents=True, exist_ok=True)
    meta = {"databento.symbol": cost["raw"], "databento.day": day}
    if splice:
        stats = splice_fill(dest, open_stream(src), tmp, dict(meta, **{"databento.spliced_from_ns": seam}))
    else:
        with _writer(tmp, meta) as w:
            stats = convert_stream(open_stream(src), w)
    row.update(events=stats["events"], records=stats["records"])
    row["status"] = _commit(write, hold_root, tmp, dest, stats)
    row["secs"] = round(time.time() - t0)
    return row


def cmd_fetch(a) -> int:
    root = Path(a.parquet_root)
    key = _key()
    for sym in a.symbol.split(","):
        for day in a.day.split(","):
            print(f"{sym} {day}:")
            row = fill_day(key, root, sym, day, splice=a.splice, overwrite=a.overwrite, write=a.write,
                           cache_dir=a.cache_dir, hold_root=a.hold_root, max_cost=a.max_cost)
            if row["cost"] is not None:
                print(f"    cost ${row['cost']:.2f}, {row['records'] or '?'} records")
            if row["events"] is not None:
                print(f"    {row['records']:,} records -> {row['events']:,} events")
            print(f"    {row['status']}")
    return 0


LOG_FIELDS = ["when", "symbol", "day", "kind", "mode", "status", "cost", "charged", "records", "events", "secs"]


def cmd_fetch_all(a) -> int:
    """Walk every symbol-day with missing depth, cheapest-risk first (oldest first), within a budget."""
    root = Path(a.parquet_root)
    kinds = set(a.kinds.split(","))
    syms = a.symbols.split(",") if a.symbols else None
    targets = scan_targets(root, a.year, kinds, syms, a.since, a.until)
    targets.sort(key=lambda t: (t["day"], t["symbol"]))
    print(f"{len(targets)} symbol-days with missing depth; budget "
          f"{'$%.2f' % a.budget if a.budget is not None else 'none'}; "
          f"{'WRITING' if a.write else 'DRY RUN -- pricing only, nothing downloaded or billed'}")
    key = _key()
    log = Path(a.log)
    new_log = not log.exists()
    spent, done, stopped = 0.0, 0, None
    with open(log, "a", newline="") as lf:
        w = csv.DictWriter(lf, fieldnames=LOG_FIELDS, extrasaction="ignore")
        if new_log:
            w.writeheader()
        for t in targets:
            sym, day, kind = t["symbol"], t["day"], t["kind"]
            l2f = l2_path(root, sym, day)
            splice = kind == "spliced" and l2f.exists() and pq.ParquetFile(l2f).metadata.num_rows > 0
            try:
                common = dict(splice=splice, overwrite=True, write=a.write, cache_dir=a.cache_dir,
                              hold_root=a.hold_root, max_cost=a.max_day_cost)
                quote = fill_day(key, root, sym, day, price_only=True, **common)   # splice days quote the tail only
                priced = quote["cost"]
                if (priced is not None and a.budget is not None and a.write
                        and not quote["status"].startswith("skipped") and spent + priced > a.budget):
                    stopped = f"budget ${a.budget:.2f} would be exceeded by {sym} {day} (${priced:.2f})"
                    break
                row = fill_day(key, root, sym, day, price_only=False, **common) if a.write \
                    and not quote["status"].startswith("skipped") else quote
            except Exception as e:                       # one bad day must not stop the run
                row = {"symbol": sym, "day": day, "mode": "?", "status": f"ERROR: {e}"[:200]}
            row.update(when=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), kind=kind)
            if row.get("charged") and row.get("cost"):
                spent += row["cost"]
            w.writerow(row); lf.flush()
            done += 1
            c = f"${row['cost']:.2f}" if row.get("cost") is not None else "   -  "
            print(f"  [{done}/{len(targets)}] {sym:4} {day} {kind:9} {c:>7}  {row['status']}", flush=True)
    print(f"{done} processed; spent ${spent:.2f}" + (f"; STOPPED: {stopped}" if stopped else ""))
    print(f"log: {log}")
    return 0 if not stopped else 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    root_default = str(default_parquet_root())

    def common(p):
        p.add_argument("--parquet-root", default=root_default)
        p.add_argument("--cache-dir", default="dbn_cache")

    p = sub.add_parser("selftest"); p.set_defaults(fn=lambda a: 0 if selftest() else 1)
    p = sub.add_parser("cost"); common(p)
    p.add_argument("--year", type=int, default=2026)
    p.add_argument("--kinds", default="empty,databento,nofile,spliced",
                   help="spliced days are priced for the whole day: an upper bound (fetch --splice only buys the tail)")
    p.add_argument("--symbols"); p.add_argument("--days"); p.add_argument("--since", default=""); p.add_argument("--until", default="99999999")
    p.add_argument("--sample", type=int, help="price an evenly spaced sample of N, then extrapolate")
    p.add_argument("--out"); p.set_defaults(fn=cmd_cost)
    p = sub.add_parser("convert")
    p.add_argument("--from-file", required=True); p.add_argument("--out", required=True); p.set_defaults(fn=cmd_convert)
    p = sub.add_parser("validate"); common(p)
    p.add_argument("--symbol", required=True); p.add_argument("--day", required=True)
    p.add_argument("--window", required=True, help="ET, HH:MM-HH:MM")
    p.add_argument("--from-file"); p.add_argument("--yes", action="store_true")
    p.add_argument("--step", type=float, default=5.0); p.add_argument("--tol-ms", type=float, default=150.0)
    p.set_defaults(fn=cmd_validate)
    p = sub.add_parser("fetch"); common(p)
    p.add_argument("--symbol", required=True); p.add_argument("--day", required=True)
    p.add_argument("--write", action="store_true"); p.add_argument("--overwrite", action="store_true")
    p.add_argument("--splice", action="store_true",
                   help="day whose NT8 depth stops partway: keep it, continue from Databento after its last event")
    p.add_argument("--max-cost", type=float)
    p.add_argument("--hold-root", default=str(Path(root_default).parent.parent / "hold"))
    p.set_defaults(fn=cmd_fetch)
    p = sub.add_parser("fetch-all", help="walk every symbol-day with missing depth, within a budget"); common(p)
    p.add_argument("--year", type=int, default=2026)
    p.add_argument("--kinds", default="empty,databento,nofile,spliced")
    p.add_argument("--symbols"); p.add_argument("--since", default=""); p.add_argument("--until", default="99999999")
    p.add_argument("--budget", type=float, help="stop before total spend would exceed this many dollars (with --write)")
    p.add_argument("--max-day-cost", type=float, help="skip any single day dearer than this")
    p.add_argument("--write", action="store_true", help="download, convert and replace; without it only prices")
    p.add_argument("--log", default="fill_log.csv")
    p.add_argument("--hold-root", default=str(Path(root_default).parent.parent / "hold"))
    p.set_defaults(fn=cmd_fetch_all)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
