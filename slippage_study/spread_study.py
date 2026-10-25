"""Bid/ask spread by 30-minute window, per symbol and day, from the raw L1 Parquet.

Two measures per (symbol, ET calendar day, 30-minute ET window):

  trade-weighted  the spread prevailing at each Last event (what a market order meets).
                  Same attach rule as the engine's reduced cache: the latest ask / bid
                  event BEFORE the trade in row order.
  time-weighted   the spread held from each quote change to the next, split at window
                  boundaries, each interval capped at MAX_STALE_S so a halt or a recorder
                  hole cannot credit a stale quote.

Spreads are integer ticks (rint((ask-bid)/tick)), so a window carries EXACT count vectors
(`tr_hist`, `tw_hist`: index k = spread of k ticks, last bin = overflow) and any pooled
percentile (by symbol, window, month, weekday) can be computed from them. Averaging per-day
percentiles would be wrong.

The study treats a quote as valid only if bid < ask (spread >= 1 tick); locked and crossed
quotes are counted separately. The engine invalidates only bid > ask; `engine_rule=True`
reproduces that for the cross-check.

Read-only: opens the raw files in place, writes only under out/.

  python spread_study.py run [--symbols MNQ,ES] [--start 20260101] [--end 20261231] [--jobs 4]
  python spread_study.py consolidate
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

ET = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
_root = os.environ.get("BACKTESTER_DATA_ROOT")
DEFAULT_ROOT = Path(_root).expanduser() if _root else None     # same variable the engine reads
REPO = HERE.parent                       # the gbBacktester checkout this folder sits in

MDT_ASK, MDT_BID, MDT_LAST = 0, 1, 2
W_NS = 30 * 60 * 1_000_000_000          # window length
NB = 65                                  # histogram bins: 0..63 ticks, 64 = overflow (>=64)
OVERFLOW = NB - 1
MAX_STALE_S = 300                        # a quote state counts at most this long without a new event
MIN_TRADES_POOL = 30                     # windows thinner than this are left out of pooled means
PROCESSING_VERSION = 1
TW_CHUNK = 4_000_000                     # rows per time-weighted chunk (memory bound)

# tick sizes mirror backtester/contracts.py (checked by a test, not imported: no dependency on the engine)
TICK = {"ES": .25, "NQ": .25, "YM": 1.0, "RTY": .1, "CL": .01, "GC": .1, "SI": .005,
        "ZN": 0.015625, "MES": .25, "MNQ": .25, "MYM": 1.0, "M2K": .1, "MCL": .01, "MGC": .1}
POINT_VALUE = {"ES": 50, "NQ": 20, "YM": 5, "RTY": 50, "CL": 1000, "GC": 100, "SI": 5000,
               "ZN": 1000, "MES": 5, "MNQ": 2, "MYM": .5, "M2K": 5, "MCL": 100, "MGC": 10}


# --------------------------------------------------------------------------- windows

def day_bins(date: str) -> tuple[int, int, np.ndarray, np.ndarray]:
    """UTC-aligned 30-minute bins covering the ET calendar day `date` (YYYYMMDD).

    Returns (first_bin, n_bins, label_idx, trading_day_shift): `label_idx[i]` is the ET
    wall-clock window (0..47) of bin i; `shift[i]` is 1 when that window starts at or
    after 18:00 ET (the CME day it belongs to is named by the NEXT date). ET offsets are
    whole multiples of 30 minutes, so UTC bins coincide with ET windows; on a DST day the
    wall-clock labels skip (spring) or repeat (fall), and repeated labels simply pool.
    """
    d = dt.date(int(date[:4]), int(date[4:6]), int(date[6:]))
    start = dt.datetime(d.year, d.month, d.day, tzinfo=ET)
    end = dt.datetime.combine(d + dt.timedelta(days=1), dt.time(), tzinfo=ET)
    s_ns = int(start.timestamp()) * 1_000_000_000
    e_ns = int(end.timestamp()) * 1_000_000_000
    first = s_ns // W_NS
    n = (e_ns - s_ns) // W_NS
    labels = np.empty(n, dtype=np.int64)
    for i in range(n):
        t = dt.datetime.fromtimestamp((first + i) * W_NS / 1e9, tz=ET)
        labels[i] = (t.hour * 60 + t.minute) // 30
    return first, n, labels, (labels >= 36).astype(np.int64)


# --------------------------------------------------------------------------- core

def spread_ticks(ask: np.ndarray, bid: np.ndarray, tick: float) -> np.ndarray:
    """Integer spread in ticks; NaN quote -> NO_QUOTE. Crossed gives <0, locked 0."""
    out = np.full(ask.shape, NO_QUOTE, dtype=np.int64)
    ok = np.isfinite(ask) & np.isfinite(bid)
    out[ok] = np.rint((ask[ok] - bid[ok]) / tick).astype(np.int64)
    return out


NO_QUOTE = -(10 ** 9)


def _ffill_values(is_kind: np.ndarray, price: np.ndarray) -> np.ndarray:
    """Forward-fill `price` taken at positions where is_kind; NaN before the first."""
    idx = np.where(is_kind, np.arange(len(price)), -1)
    np.maximum.accumulate(idx, out=idx)
    out = price[np.maximum(idx, 0)]
    out[idx < 0] = np.nan
    return out


def measure_day(ts: np.ndarray, mdt: np.ndarray, price: np.ndarray, vol: np.ndarray,
                date: str, tick: float, *, tw_valid_from_ns: int | None = None,
                tw_enabled: bool = True, engine_rule: bool = False,
                max_stale_s: float = MAX_STALE_S) -> dict:
    """Aggregate one day's raw events into per-window arrays (see module doc).

    ts int64 ns UTC, row order = file order (non-decreasing). Only MarketDataType
    0 (ask), 1 (bid), 2 (last) are used. `tw_valid_from_ns`: time-weighted values are
    only trusted before this instant (spliced Databento tail has no quote updates).
    """
    first, nbin, label, shift = day_bins(date)
    keep = mdt <= MDT_LAST
    if not keep.all():                       # types 3..8 exist (count equals Last for 5): ignore
        ts, mdt, price, vol = ts[keep], mdt[keep], price[keep], vol[keep]

    n = len(ts)
    res = {k: np.zeros(nbin, dtype=np.int64) for k in (
        "n_trades", "volume", "n_valid", "n_noquote", "n_crossed", "n_locked", "tr_sum_ticks",
        "tr_max", "n_quotes", "tw_ns", "tw_sum_tick_ns", "tw_max", "tw_ns_locked_crossed",
        "tw_ns_stale", "range_ticks")}
    tr_hist = np.zeros((nbin, NB), dtype=np.int64)
    tw_hist = np.zeros((nbin, NB), dtype=np.int64)
    res["bins_first"] = first
    if n == 0:
        res.update(tr_hist=tr_hist, tw_hist=tw_hist, label=label, shift=shift)
        return res

    # ---- trades: prevailing quote = latest bid / ask event before the Last in row order
    row = np.arange(n)
    last_idx = np.flatnonzero(mdt == MDT_LAST)
    ask_idx = np.flatnonzero(mdt == MDT_ASK)
    bid_idx = np.flatnonzero(mdt == MDT_BID)
    ask_p = np.full(len(last_idx), np.nan)
    bid_p = np.full(len(last_idx), np.nan)
    pa_ = np.searchsorted(ask_idx, last_idx, side="right") - 1
    pb_ = np.searchsorted(bid_idx, last_idx, side="right") - 1
    ask_p[pa_ >= 0] = price[ask_idx][pa_[pa_ >= 0]]
    bid_p[pb_ >= 0] = price[bid_idx][pb_[pb_ >= 0]]
    t_ts, t_px, t_vol = ts[last_idx], price[last_idx], vol[last_idx]
    t_bin = np.clip(t_ts // W_NS - first, 0, nbin - 1)
    if engine_rule:                          # the engine NaNs bid > ask only
        crossed_e = bid_p > ask_p
        ask_p[crossed_e] = np.nan
        bid_p[crossed_e] = np.nan
    sp = spread_ticks(ask_p, bid_p, tick)
    res["n_trades"] = np.bincount(t_bin, minlength=nbin)
    res["volume"] = np.bincount(t_bin, weights=t_vol, minlength=nbin).astype(np.int64)
    valid = sp >= 1
    res["n_noquote"] = np.bincount(t_bin[sp == NO_QUOTE], minlength=nbin)
    res["n_crossed"] = np.bincount(t_bin[(sp < 0) & (sp != NO_QUOTE)], minlength=nbin)
    res["n_locked"] = np.bincount(t_bin[sp == 0], minlength=nbin)
    res["n_valid"] = np.bincount(t_bin[valid], minlength=nbin)
    res["tr_sum_ticks"] = np.bincount(t_bin[valid], weights=sp[valid], minlength=nbin).astype(np.int64)
    key = t_bin[valid] * NB + np.minimum(sp[valid], OVERFLOW)
    tr_hist = np.bincount(key, minlength=nbin * NB).reshape(nbin, NB).astype(np.int64)
    mx = np.zeros(nbin, dtype=np.int64)
    np.maximum.at(mx, t_bin[valid], sp[valid])
    res["tr_max"] = mx
    # high-low range of the window's trade prices, in ticks (separates wide spreads from volatility)
    if len(t_px):
        hi = np.full(nbin, -np.inf)
        lo = np.full(nbin, np.inf)
        np.maximum.at(hi, t_bin, t_px)
        np.minimum.at(lo, t_bin, t_px)
        has = np.isfinite(hi)
        res["range_ticks"][has] = np.rint((hi[has] - lo[has]) / tick).astype(np.int64)

    # ---- time-weighted: spread state after each quote event, held to the next event.
    # Processed in row chunks (a busy day is ~32M quote events) with the previous chunk's last
    # event carried over, so memory stays bounded.
    if tw_enabled:
        cap = int(max_stale_s * 1e9)
        end_of_data = (first + nbin) * W_NS

        def add(a_ts, nxt_ts, qsp):
            gap = nxt_ts - a_ts
            dur = np.minimum(gap, cap)
            stale = gap - dur
            b_ts = a_ts + dur
            if tw_valid_from_ns is not None:   # spliced tail: no standalone quotes after this
                b_ts = np.minimum(b_ts, tw_valid_from_ns)
                dur = np.maximum(b_ts - a_ts, 0)
                stale = np.zeros_like(stale)
            ba = a_ts // W_NS
            bb = np.maximum(b_ts - 1, a_ts) // W_NS
            cross = bb > ba
            # first piece: [a, min(b, boundary)) in bin ba; second piece in bin bb
            piece1 = np.where(cross, (ba + 1) * W_NS - a_ts, dur)
            piece2 = np.where(cross, b_ts - (ba + 1) * W_NS, 0)
            bin1 = np.clip(ba - first, 0, nbin - 1)
            bin2 = np.clip(bb - first, 0, nbin - 1)
            res["tw_ns_stale"] += np.rint(np.bincount(bin1, weights=stale, minlength=nbin)).astype(np.int64)
            ok = qsp >= 1
            bad = (qsp < 1) & (qsp != NO_QUOTE)
            k = np.minimum(qsp[ok], OVERFLOW)
            for bins_, piece in ((bin1, piece1), (bin2, piece2)):
                tw_hist[:] += np.rint(np.bincount(bins_[ok] * NB + k, weights=piece[ok],
                                                  minlength=nbin * NB)).astype(np.int64).reshape(nbin, NB)
                res["tw_ns_locked_crossed"] += np.rint(np.bincount(
                    bins_[bad], weights=piece[bad], minlength=nbin)).astype(np.int64)
                res["tw_sum_tick_ns"] += np.rint(np.bincount(
                    bins_[ok], weights=piece[ok] * qsp[ok], minlength=nbin)).astype(np.int64)
                sel = ok & (piece > 0)
                m2 = np.zeros(nbin, dtype=np.int64)
                np.maximum.at(m2, bins_[sel], qsp[sel])
                res["tw_max"] = np.maximum(res["tw_max"], m2)

        ask_c = bid_c = np.nan
        pend = None                           # (ts, spread) of the previous chunk's last event
        for r0 in range(0, n, TW_CHUNK):
            r1 = min(n, r0 + TW_CHUNK)
            sel = np.flatnonzero(mdt[r0:r1] != MDT_LAST)
            if not len(sel):
                continue
            cts = ts[r0:r1][sel]
            is_ask = mdt[r0:r1][sel] == MDT_ASK
            cp = price[r0:r1][sel]
            res["n_quotes"] += np.bincount(np.clip(cts // W_NS - first, 0, nbin - 1), minlength=nbin)
            kind = np.concatenate(([True, False], is_ask))     # virtual ask / bid carry the state in
            pp = np.concatenate(([ask_c, bid_c], cp))
            ask_ff = _ffill_values(kind, pp)[2:]
            bid_ff = _ffill_values(~kind, pp)[2:]
            qsp = spread_ticks(ask_ff, bid_ff, tick)
            if engine_rule:
                qsp[qsp < 0] = NO_QUOTE
            ask_c, bid_c = ask_ff[-1], bid_ff[-1]
            a_ts = cts if pend is None else np.concatenate(([pend[0]], cts))
            a_sp = qsp if pend is None else np.concatenate(([pend[1]], qsp))
            if len(a_ts) > 1:
                add(a_ts[:-1], a_ts[1:], a_sp[:-1])
            pend = (a_ts[-1], a_sp[-1])
        if pend is not None:
            add(np.array([pend[0]]), np.array([min(end_of_data, pend[0] + cap)]), np.array([pend[1]]))
        res["tw_ns"] = tw_hist.sum(axis=1)
    res.update(tr_hist=tr_hist, tw_hist=tw_hist, label=label, shift=shift)
    return res


# --------------------------------------------------------------------------- IO

def read_day(path: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, dict]:
    t = pq.read_table(path, columns=["Timestamp", "MarketDataType", "Price", "Volume"])
    meta = {k.decode(): v.decode() for k, v in (t.schema.metadata or {}).items()}
    if meta.get("replay_importer.timestamps") != "UTC":
        raise ValueError("timestamps not tagged UTC (legacy ET-wall-clock file)")
    ts = t.column("Timestamp").to_numpy(zero_copy_only=False).astype("datetime64[ns]").astype("int64")
    mdt = t.column("MarketDataType").to_numpy()
    price = t.column("Price").to_numpy()
    vol = t.column("Volume").to_numpy()
    return ts, mdt, price, vol, meta


def classify_source(meta: dict) -> tuple[bool, int | None]:
    """(whole day is Databento TBBO, splice instant ns or None)."""
    src = meta.get("replay_importer.source_name", meta.get("nrd2parquet.source_name", ""))
    whole = "databento" in src.lower()
    after = meta.get("splice.after_ns")
    return whole, (int(after) if after else None)


def roll_dates(symbol: str) -> set[str]:
    try:
        cal = json.loads((REPO / "data" / "roll_calendar.json").read_text())
        return {r["date"] for r in cal["symbols"][symbol]["rolls"]}
    except Exception:
        return set()


SCHEMA_COLS = ["symbol", "date", "trading_day", "window_et", "window_idx", "dst_repeat"]


def day_rows(symbol: str, date: str, res: dict, flags: dict) -> list[dict]:
    """Collapse UTC bins into ET wall-clock windows (pools the repeated hour on a fall-back day)."""
    d = dt.date(int(date[:4]), int(date[4:6]), int(date[6:]))
    rows = []
    labels = res["label"]
    scalar = ("n_trades", "volume", "n_valid", "n_noquote", "n_crossed", "n_locked", "tr_sum_ticks",
              "n_quotes", "tw_ns", "tw_sum_tick_ns", "tw_ns_locked_crossed", "tw_ns_stale")
    for lab in sorted(set(labels.tolist())):
        sel = np.flatnonzero(labels == lab)
        if res["n_trades"][sel].sum() == 0 and res["n_quotes"][sel].sum() == 0:
            continue
        row = {"symbol": symbol, "date": date,
               "trading_day": (d + dt.timedelta(days=int(res["shift"][sel[0]]))).strftime("%Y%m%d"),
               "window_et": f"{lab // 2:02d}:{30 * (lab % 2):02d}", "window_idx": int(lab),
               "dst_repeat": len(sel) > 1}
        for k in scalar:
            row[k] = int(res[k][sel].sum())
        row["tr_max"] = int(res["tr_max"][sel].max())
        row["tw_max"] = int(res["tw_max"][sel].max())
        row["range_ticks"] = int(res["range_ticks"][sel].max())
        row["tr_hist"] = res["tr_hist"][sel].sum(axis=0).tolist()
        row["tw_hist"] = res["tw_hist"][sel].sum(axis=0).tolist()
        row.update(flags)
        rows.append(row)
    return rows


def process_one(symbol: str, path: str, outdir: str) -> tuple[str, str, float]:
    date = Path(path).stem
    out = Path(outdir) / symbol / f"{date}.parquet"
    if out.exists():
        return symbol, date, 0.0
    t0 = time.time()
    ts, mdt, price, vol, meta = read_day(path)
    whole_db, splice_ns = classify_source(meta)
    # a partial-day splice invalidates the time-weighted value only for time at/after splice
    res = measure_day(ts, mdt, price, vol, date, TICK[symbol],
                      tw_enabled=not whole_db, tw_valid_from_ns=None if whole_db else splice_ns)
    flags = {"databento_day": whole_db, "spliced": splice_ns is not None,
             "roll_day": date in roll_dates(symbol),
             "src_name": meta.get("replay_importer.source_name", meta.get("nrd2parquet.source_name", "")),
             "src_size": int(meta.get("replay_importer.source_size", meta.get("nrd2parquet.source_size", 0)) or 0),
             "src_mtime_ns": int(meta.get("replay_importer.source_mtime_ns", meta.get("nrd2parquet.source_mtime_ns", 0)) or 0)}
    rows = day_rows(symbol, date, res, flags)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    pq.write_table(rows_to_table(rows), tmp)
    tmp.rename(out)
    return symbol, date, time.time() - t0


def rows_to_table(rows: list[dict]) -> pa.Table:
    if not rows:
        return pa.table({c: [] for c in SCHEMA_COLS})
    cols = {}
    for k in rows[0]:
        vals = [r[k] for r in rows]
        cols[k] = pa.array(vals, type=pa.list_(pa.int64())) if k.endswith("_hist") else pa.array(vals)
    return pa.table(cols)


def _worker(args):
    try:
        return process_one(*args)
    except Exception as e:                    # one bad day must not stop the run
        symbol, path, _ = args
        return symbol, Path(path).stem, f"ERROR {type(e).__name__}: {e}"


def list_files(root: Path, symbols: list[str] | None, start: str, end: str):
    out = []
    for d in sorted(glob.glob(str(root / "2026" / "*-2026_L1"))):
        sym = Path(d).name.split("-")[0]
        if symbols and sym not in symbols:
            continue
        for f in sorted(glob.glob(d + "/2026*.parquet")):
            if start <= Path(f).stem <= end:
                out.append((sym, f, str(OUT / "days")))
    return out


def cmd_run(a):
    if DEFAULT_ROOT is None:
        sys.exit("Set BACKTESTER_DATA_ROOT to the Parquet root (see the main README, Data).")
    files = list_files(DEFAULT_ROOT, a.symbols.split(",") if a.symbols else None, a.start, a.end)
    print(f"{len(files)} symbol-days, {a.jobs} jobs", flush=True)
    OUT.mkdir(exist_ok=True)
    (OUT / "run_meta.json").write_text(json.dumps({
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "processing_version": PROCESSING_VERSION, "max_stale_s": MAX_STALE_S,
        "data_root": str(DEFAULT_ROOT), "repo_git": os.popen(
            f"git -C {REPO} rev-parse --short HEAD").read().strip(),
        "started": dt.datetime.now().isoformat(timespec="seconds"),
        "args": {k: v for k, v in vars(a).items() if k != "fn"}}, indent=1))
    done = errs = 0
    t0 = time.time()
    with ProcessPoolExecutor(a.jobs) as ex:
        futs = [ex.submit(_worker, f) for f in files]
        for fu in as_completed(futs):
            sym, date, secs = fu.result()
            done += 1
            if isinstance(secs, str):
                errs += 1
                print(f"{sym} {date} {secs}", flush=True)
            if done % 50 == 0 or done == len(files):
                el = time.time() - t0
                print(f"{done}/{len(files)}  errors={errs}  elapsed={el:.0f}s  eta={(len(files)-done)*el/done:.0f}s", flush=True)


def cmd_consolidate(_):
    parts = sorted((OUT / "days").glob("*/*.parquet"))
    tbl = pa.concat_tables([pq.read_table(p) for p in parts])
    pq.write_table(tbl, OUT / "spread_30m_by_day_2026.parquet")
    print(f"{len(parts)} day files, {tbl.num_rows} rows")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--symbols", default="")
    r.add_argument("--start", default="20260101")
    r.add_argument("--end", default="20261231")
    r.add_argument("--jobs", type=int, default=4)
    r.set_defaults(fn=cmd_run)
    c = sub.add_parser("consolidate")
    c.set_defaults(fn=cmd_consolidate)
    args = ap.parse_args()
    args.fn(args)
