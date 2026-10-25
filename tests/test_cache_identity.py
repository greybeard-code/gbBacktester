"""Cache validity against the raw file (plan task 6c, 2026-10-17).

The reduced and bar caches used to be validated only by CACHE_VERSION /
BARS_VERSION, so a raw day that was replaced later (vendor fills, splices,
ingest_true_nrd) kept serving the OLD content: 41 reduced and 929 bar files in
the production cache were older than their raw file. Now every cache file
records the identity of the raw file it was built from (relative path | size |
mtime_ns), is rebuilt on ANY difference, and legacy files without an identity
are rebuilt only if the raw file is NEWER than them. All mtimes are set
explicitly (os.utime); nothing sleeps.
"""
from __future__ import annotations

import os
import shutil

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backtester.data import BARS_VERSION, CACHE_VERSION, RAW_ID_KEY, Catalog
from backtester.strategy import parse_barspec

DATE = "20250115"
NS = 1_000_000_000
T0 = 1_700_000_000 * NS               # arbitrary base mtime (ns)
TICK = 0.25


def write_raw(path, prices, mtime_ns):
    """A raw L1 day of Last trades only, one per second from 10:00 ET."""
    n = len(prices)
    wall = np.datetime64("2025-01-15T10:00:00", "ns") + \
        np.arange(n) * np.timedelta64(1, "s")
    table = pa.table({
        "Timestamp": pa.array(wall, pa.timestamp("ns")),
        "MarketDataType": pa.array(np.full(n, 2, dtype="int8")),
        "Price": pa.array(np.asarray(prices, dtype="float64")),
        "Volume": pa.array(np.ones(n, dtype="int64")),
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    os.utime(path, ns=(mtime_ns, mtime_ns))


def setup(tmp_path, prices, mtime_ns=T0):
    raw = tmp_path / "data" / "MNQ-2025_L1" / f"{DATE}.parquet"
    write_raw(raw, prices, mtime_ns)
    cat = Catalog(data_root=tmp_path / "data", cache_root=tmp_path / "cache")
    return cat, raw


def meta_of(path):
    return pq.read_table(path).schema.metadata or {}


def strip_raw_id(path):
    t = pq.read_table(path)
    md = {k: v for k, v in (t.schema.metadata or {}).items() if k != RAW_ID_KEY}
    pq.write_table(t.replace_schema_metadata(md), path)


def touch(path, ns):
    os.utime(path, ns=(ns, ns))


A = [100.0, 100.25, 100.5]                       # 3 events
B = [100.0 + TICK * i for i in range(130)]       # 130 events: 3 one-minute bars


# ------------------------------------------------------------ reduced cache --

def test_a_rewritten_raw_day_rebuilds_the_reduced_cache(tmp_path):
    cat, raw = setup(tmp_path, A)
    assert len(cat.load_day("MNQ", DATE)) == 3
    rp = cat._reduced_path("MNQ", DATE)
    md = meta_of(rp)
    assert md[b"btcache"] == CACHE_VERSION
    # identity = relative path | size | mtime_ns
    assert md[RAW_ID_KEY] == (f"MNQ-2025_L1/{DATE}.parquet|{raw.stat().st_size}|"
                              f"{T0}").encode()
    write_raw(raw, B, T0 + 10 * NS)               # replaced, same path
    assert len(cat.load_day("MNQ", DATE)) == 130  # same Catalog instance
    assert meta_of(rp)[RAW_ID_KEY].endswith(f"|{T0 + 10 * NS}".encode())


def test_an_unchanged_raw_day_is_not_rebuilt(tmp_path):
    cat, _ = setup(tmp_path, A)
    cat.load_day("MNQ", DATE)
    rp = cat._reduced_path("MNQ", DATE)
    sentinel = T0 + 99 * NS
    touch(rp, sentinel)
    cat.load_day("MNQ", DATE)
    assert rp.stat().st_mtime_ns == sentinel      # a rewrite would have moved it


def test_a_changed_size_alone_rebuilds_even_with_the_same_mtime(tmp_path):
    cat, raw = setup(tmp_path, A)
    cat.load_day("MNQ", DATE)
    write_raw(raw, B, T0)                          # same mtime, different size
    assert len(cat.load_day("MNQ", DATE)) == 130


def test_legacy_cache_older_than_the_raw_file_is_rebuilt(tmp_path):
    cat, raw = setup(tmp_path, A)
    cat.load_day("MNQ", DATE)
    rp = cat._reduced_path("MNQ", DATE)
    strip_raw_id(rp)                               # make it a pre-identity file
    write_raw(raw, B, T0)
    touch(rp, T0 - 100 * NS)                       # cache OLDER than the raw
    assert len(cat.load_day("MNQ", DATE)) == 130
    assert RAW_ID_KEY in meta_of(rp)               # and now it carries an identity


def test_legacy_cache_newer_than_the_raw_file_is_trusted(tmp_path):
    """The documented blind spot, pinned: without an identity, only 'raw newer
    than cache' can be detected."""
    cat, raw = setup(tmp_path, A)
    cat.load_day("MNQ", DATE)
    rp = cat._reduced_path("MNQ", DATE)
    strip_raw_id(rp)
    write_raw(raw, B, T0)                          # different content...
    touch(rp, T0 + 100 * NS)                       # ...but the cache is newer
    assert len(cat.load_day("MNQ", DATE)) == 3
    assert RAW_ID_KEY not in meta_of(rp)           # not rewritten, not stamped


def test_the_chosen_raw_path_is_part_of_the_identity(tmp_path):
    """A date can exist in two contract folders and the year-nested copy wins;
    identical size and mtime, different path, must still rebuild."""
    cat, raw = setup(tmp_path, A)
    cat.load_day("MNQ", DATE)
    rp = cat._reduced_path("MNQ", DATE)
    sentinel = T0 + 50 * NS
    touch(rp, sentinel)
    nested = tmp_path / "data" / "2025" / "MNQ-2025_L1" / f"{DATE}.parquet"
    nested.parent.mkdir(parents=True)
    shutil.copy2(raw, nested)                      # same bytes, size and mtime
    cat.day_files("MNQ", refresh=True)
    assert cat.day_files("MNQ")[DATE] == nested
    cat.load_day("MNQ", DATE)
    assert rp.stat().st_mtime_ns != sentinel       # rebuilt
    assert meta_of(rp)[RAW_ID_KEY].startswith(f"2025/MNQ-2025_L1/{DATE}".encode())


def test_cache_only_use_without_a_raw_file_still_works(tmp_path):
    cat, _ = setup(tmp_path, A)
    cat.load_day("MNQ", DATE)
    gone = Catalog(data_root=tmp_path / "no-such-root", cache_root=tmp_path / "cache")
    assert len(gone.load_day("MNQ", DATE)) == 3    # nothing to compare: trust it


def test_day_files_is_memoised_until_refreshed(tmp_path):
    cat, raw = setup(tmp_path, A)
    first = cat.day_files("MNQ")
    assert cat.day_files("MNQ") is first
    write_raw(raw.parent / "20250116.parquet", A, T0)
    assert "20250116" not in cat.day_files("MNQ")
    assert "20250116" in cat.day_files("MNQ", refresh=True)


# --------------------------------------------------------------- bar caches --

def test_time_bars_go_stale_when_the_raw_day_is_replaced(tmp_path):
    cat, raw = setup(tmp_path, A)
    spec = parse_barspec("1m")
    assert len(cat.load_bars("MNQ", DATE, spec, TICK)) == 1
    bp = cat._bars_path("MNQ", DATE, spec.key)
    assert meta_of(bp)[b"btcache"] == BARS_VERSION and RAW_ID_KEY in meta_of(bp)
    write_raw(raw, B, T0 + 10 * NS)
    # 130 events from 10:00:00 reach 10:02:09: three one-minute bars
    assert len(cat.load_bars("MNQ", DATE, spec, TICK)) == 3


def test_renko_bars_go_stale_when_the_raw_day_is_replaced(tmp_path):
    cat, raw = setup(tmp_path, A)
    spec = parse_barspec("r4-2")                  # 1.00 point bricks
    few = cat.load_bars_sequence("MNQ", [DATE], spec, TICK)[0]
    write_raw(raw, B, T0 + 10 * NS)               # climbs 32.25 points
    many = cat.load_bars_sequence("MNQ", [DATE], spec, TICK)[0]
    fresh_cat = Catalog(data_root=tmp_path / "data", cache_root=tmp_path / "c2")
    expect = fresh_cat.load_bars_sequence("MNQ", [DATE], spec, TICK)[0]
    assert len(many) == len(expect) > len(few)
    assert np.array_equal(many.close, expect.close)


def test_engine_order_never_pairs_stale_bars_with_a_rebuilt_day(tmp_path):
    """The ordering flaw this task's design had to avoid: a bar-cache hit
    returns before load_day runs (the engine loads bars_seq first, then the
    day). If the bars were judged against the REDUCED file they would be
    served stale and their [i0, i1) spans would index a different tick array
    than the one the broker then uses."""
    cat, raw = setup(tmp_path, A)
    spec = parse_barspec("1m")
    cat.load_bars_sequence("MNQ", [DATE], spec, TICK)    # caches bars
    cat.load_day("MNQ", DATE)                            # and the day, from A
    write_raw(raw, B, T0 + 10 * NS)                      # the raw day is replaced
    bars = cat.load_bars_sequence("MNQ", [DATE], spec, TICK)[0]   # engine order:
    day = cat.load_day("MNQ", DATE)                              # bars, then day
    assert len(day) == 130
    assert int(bars.i1[-1]) == len(day)
    assert int(bars.volume.sum()) == int(day.volume.sum())


def test_saber_family_cache_also_checks_the_raw_file(tmp_path):
    cat, raw = setup(tmp_path, A)
    spec = parse_barspec("w4")                     # wave bars: carry-tuple cache
    first = cat.load_bars_sequence("MNQ", [DATE], spec, TICK)[0]
    bp = cat._bars_path("MNQ", DATE, spec.key)
    assert RAW_ID_KEY in meta_of(bp)
    write_raw(raw, B, T0 + 10 * NS)
    second = cat.load_bars_sequence("MNQ", [DATE], spec, TICK)[0]
    assert len(second) > len(first)
