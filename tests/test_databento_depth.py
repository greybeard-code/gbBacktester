"""tools/databento_depth.py: MBP-10 -> NT8-style depth events. No network, no real data."""
from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import databento_depth as D  # noqa: E402

ASK, BID = D.ASK, D.BID
ADD, UPD, REM = D.ADD, D.UPDATE, D.REMOVE


def ladder(prices, size=10):
    return [(p, size + i) for i, p in enumerate(prices)]


# ---- diff conventions copied from recorded NT8 data ---------------------------------

def test_initial_book_is_ten_adds_in_order():
    ops = D.diff_side([], ladder(range(100, 110)), ASK)
    assert [(o, p) for o, p, _, _ in ops] == [(ADD, i) for i in range(10)]


def test_size_change_is_one_update():
    old = ladder(range(100, 110))
    new = list(old); new[3] = (103, 99)
    assert D.diff_side(old, new, ASK) == [(UPD, 3, 103, 99)]


def test_new_best_ask_is_add0_then_overflow_remove_at_10():
    old = ladder(range(100, 110))
    new = ladder([99] + list(range(100, 109)))
    ops = D.diff_side(old, new, ASK)
    assert ops[0][:3] == (ADD, 0, 99)
    assert ops[1][:3] == (REM, 10, 109)              # the level that fell off the window
    assert ops[1][3] == old[-1][1]                   # overflow keeps the dropped level's size
    assert len(ops) == 2 or all(o[0] == UPD for o in ops[2:])


def test_new_best_bid_is_add0_then_remove_at_10():
    old = ladder(range(110, 100, -1))
    new = ladder([111] + list(range(110, 101, -1)))
    ops = D.diff_side(old, new, BID)
    assert (ops[0][0], ops[0][1]) == (ADD, 0) and (ops[1][0], ops[1][1]) == (REM, 10)


def test_vanished_best_is_rem0_with_size_zero_then_add9():
    old = ladder(range(100, 110))
    new = ladder(range(101, 111))                    # 100 cancelled, 110 enters at the back
    ops = D.diff_side(old, new, ASK)
    assert ops[0] == (REM, 0, 100, 0)                # a cancel carries Volume 0
    assert ops[1][:3] == (ADD, 9, 110)
    assert all(o[0] == UPD for o in ops[2:])


def test_missing_levels_when_book_is_short():
    old = ladder(range(100, 104))
    new = ladder(range(100, 106))
    ops = D.diff_side(old, new, ASK)
    assert [(o[0], o[1]) for o in ops] == [(ADD, 4), (ADD, 5)]


def test_cleared_book_removes_everything():
    old = ladder(range(100, 105))
    ops = D.diff_side(old, [], ASK)
    assert [o[0] for o in ops] == [REM] * 5
    b = D.Book()
    b.s[ASK] = [list(x) for x in old]
    for o in ops:
        b.apply(ASK, o[0], o[1], o[2], o[3])
    assert b.s[ASK] == []


def test_several_levels_moving_at_once_never_goes_past_position_10():
    old = ladder(range(100, 110))
    new = ladder([96, 97, 98, 99] + list(range(100, 106)))      # four new bests push four levels out
    ops = D.diff_side(old, new, ASK)
    assert max(o[1] for o in ops) == 10
    assert [(o[0], o[1]) for o in ops[:8]] == [(ADD, 0), (REM, 10), (ADD, 1), (REM, 10),
                                              (ADD, 2), (REM, 10), (ADD, 3), (REM, 10)]
    book = D.Book(); book.s[ASK] = [list(x) for x in old]
    for op, pos, p, s in ops:
        book.apply(ASK, op, pos, p, s)
    assert [tuple(x) for x in book.s[ASK]] == new


def test_diff_always_reaches_the_target_book():
    rng = np.random.default_rng(1)
    for _ in range(3000):
        a = sorted(rng.choice(60, size=int(rng.integers(0, 11)), replace=False).tolist())
        b = sorted(rng.choice(60, size=int(rng.integers(0, 11)), replace=False).tolist())
        for side, key in ((ASK, lambda x: x), (BID, lambda x: -x)):
            old = [(p, int(rng.integers(1, 50))) for p in sorted(a, key=key)]
            new = [(p, int(rng.integers(1, 50))) for p in sorted(b, key=key)]
            book = D.Book(); book.s[side] = [list(x) for x in old]
            for op, pos, p, s in D.diff_side(old, new, side):
                book.apply(side, op, pos, p, s)
            assert [tuple(x) for x in book.s[side][:10]] == new
            assert max([o[1] for o in D.diff_side(old, new, side)] or [0]) <= 10


# ---- reading --------------------------------------------------------------------------

def test_selftest_whole_pipeline():
    assert D.selftest(4000)


def test_record_layout_size():
    assert D.MBP10.itemsize == 368


def test_wrong_record_type_is_an_error_not_a_skip():
    data, _ = D._synthetic_stream(50)
    off = 16 + 4 * 0                                 # header is 8 + 16 bytes of metadata
    bad = bytearray(data)
    bad[24 + 3 * 368 + 1] = 0x01                     # rtype of record 3 -> MBP-1
    with pytest.raises(D.DbnError, match="MBP-10"):
        list(D.iter_records(io.BytesIO(bytes(bad))))


def test_not_dbn_and_truncated_streams_are_rejected():
    with pytest.raises(D.DbnError, match="not a DBN"):
        list(D.iter_records(io.BytesIO(b"hello world, definitely not")))
    data, _ = D._synthetic_stream(20)
    with pytest.raises(D.DbnError, match="trailing"):
        list(D.iter_records(io.BytesIO(data[:-100])))


def test_chunk_boundaries_do_not_change_the_result():
    data, _ = D._synthetic_stream(3000, seed=11)
    outs = []
    for chunk in (7, 1000, 400_000):
        c = D.Converter()
        evs = [c.convert(a) for a in D.iter_records(io.BytesIO(data), chunk)]
        outs.append(np.concatenate([e["price_int"] for e in evs]).tolist())
    assert outs[0] == outs[1] == outs[2]


def test_timestamps_never_go_backwards():
    data, _ = D._synthetic_stream(500)
    arr = np.frombuffer(data[24:], dtype=D.MBP10).copy()
    arr["ts_recv"][100] -= np.uint64(10**9)          # one record stamped in the past
    c = D.Converter()
    ev = c.convert(arr)
    assert (np.diff(ev["ts"]) >= 0).all()
    assert c.ts_regressions == 1


def test_initial_book_suppresses_duplicate_adds_at_a_seam():
    data, snaps = D._synthetic_stream(200)
    arr = np.frombuffer(data[24:], dtype=D.MBP10)
    book = {ASK: [(int(round(p / D.PX_SCALE)), s) for p, s in snaps[0][0]],
            BID: [(int(round(p / D.PX_SCALE)), s) for p, s in snaps[0][1]]}
    ev = D.Converter(book).convert(arr[:1])          # record 0 equals the seeded book
    assert len(ev["ts"]) == 0


# ---- parquet output -------------------------------------------------------------------

def test_events_parquet_matches_the_repo_l2_schema(tmp_path):
    data, _ = D._synthetic_stream(300)
    out = tmp_path / "x.parquet"
    with D._writer(out, {"databento.symbol": "ESM6"}) as w:
        D.convert_stream(io.BytesIO(data), w)
    t = pq.read_table(out)
    assert t.schema.names == ["Timestamp", "MarketDataType", "Operation", "Position",
                              "MarketMaker", "Price", "Volume"]
    assert str(t.schema.field("Timestamp").type) == "timestamp[ns, tz=UTC]"
    assert t.column("MarketMaker").null_count == t.num_rows
    md = pq.read_schema(out).metadata
    assert md[b"replay_importer.source_name"] == D.SOURCE_NAME.encode()   # Repair/Audit leave it alone
    assert md[b"replay_importer.timestamps"] == b"UTC"
    assert set(np.unique(t.column("Position").to_numpy())) <= set(range(0, 11))


# ---- targets, cost, fetch (network mocked) ---------------------------------------------

def make_repo(tmp_path):
    root = tmp_path / "Parquet" / "2026"
    for sym in ("ES", "SI"):
        (root / f"{sym}-2026_L1").mkdir(parents=True)
        (root / f"{sym}-2026_L2").mkdir(parents=True)

    def l1(sym, day, source=None, splice=False):
        md = {b"replay_importer.timestamps": b"UTC"}
        if source:
            md[b"replay_importer.source_name"] = source.encode()
        if splice:
            md[b"splice.after_ns"] = b"1"
        t = pa.table({"Timestamp": pa.array([1], pa.int64()), "MarketDataType": pa.array([2], pa.int8()),
                      "Price": [1.0], "Volume": [1]}).replace_schema_metadata(md)
        pq.write_table(t, root / f"{sym}-2026_L1" / f"{day}.parquet")

    def l2(sym, day, rows):
        t = pa.table({"Timestamp": pa.array([1] * rows, pa.int64())})
        pq.write_table(t, root / f"{sym}-2026_L2" / f"{day}.parquet")

    l1("ES", "20260105"); l2("ES", "20260105", 5)                  # healthy
    l1("ES", "20260106"); l2("ES", "20260106", 0)                  # empty depth
    l1("ES", "20260107")                                           # no L2 file
    l1("ES", "20260108", splice=True); l2("ES", "20260108", 3)     # spliced
    l1("ES", "20260110"); l2("ES", "20260110", 0)                  # a Saturday: never a target
    l1("SI", "20260109", source="databento GLBX.MDP3 tbbo")        # Databento whole day
    (root / "CL-2026_L1").mkdir(); (root / "CL-2026_L2").mkdir()
    l1("CL", "20260106"); l2("CL", "20260106", 0)                  # oil: never a target by default
    return tmp_path / "Parquet"


def test_scan_targets_classifies_and_skips_saturdays(tmp_path):
    root = make_repo(tmp_path)
    got = {(t["symbol"], t["day"]): t["kind"] for t in
           D.scan_targets(root, 2026, set(D.KINDS) | {"databento"})}
    assert got == {("ES", "20260106"): "empty", ("ES", "20260107"): "nofile",
                   ("ES", "20260108"): "spliced", ("SI", "20260109"): "databento"}
    assert not any(x["symbol"] == "CL" for x in D.scan_targets(root, 2026, set(D.KINDS) | {"databento"}))
    assert [x["symbol"] for x in D.scan_targets(root, 2026, {"empty"}, ["CL"])] == ["CL"]   # explicit still works
    only = D.scan_targets(root, 2026, {"empty"}, ["ES"])
    assert [t["day"] for t in only] == ["20260106"]


def test_request_params_use_the_roll_rule_contract():
    p = D.request_params("SI", "20250305")           # May 2025 is the SI front month then
    assert p["symbols"] == "SIK5" and p["schema"] == "mbp-10" and p["dataset"] == "GLBX.MDP3"
    assert p["start"].startswith("2025-03-05T05:00") and p["end"].startswith("2025-03-06T05:00")


def test_cost_command_sums_and_never_downloads(tmp_path, monkeypatch, capsys):
    root = make_repo(tmp_path)
    calls = []

    def fake_api(method, endpoint, params, key, stream=False):
        calls.append((method, endpoint))
        assert method == "GET" and not stream
        return {"metadata.get_cost": b"2.50", "metadata.get_record_count": b"1000",
                "metadata.get_billable_size": b"368000"}[endpoint]
    monkeypatch.setattr(D, "_api", fake_api)
    monkeypatch.setenv("DATABENTO_API_KEY", "db-test")
    args = type("A", (), dict(parquet_root=str(root), year=2026, kinds="empty,nofile,databento",
                              symbols=None, days=None, since="", until="99999999", sample=None, out=None))()
    assert D.cmd_cost(args) == 0
    out = capsys.readouterr().out
    assert "TOTAL $7.50 for 3 symbol-days" in out          # "spliced" is not in --kinds
    assert "db-test" not in out                      # the key is never printed
    assert all(m == "GET" and e.startswith("metadata.") for m, e in calls)


def test_fetch_dry_run_then_write_backs_up_the_old_file(tmp_path, monkeypatch):
    root = make_repo(tmp_path)
    data, _ = D._synthetic_stream(400)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "ES_20260106.mbp10.dbn.zst").write_bytes(pa.compress(data, codec="zstd", asbytes=True))
    monkeypatch.setattr(D, "_api", lambda m, e, p, k, stream=False: b"1.25")
    monkeypatch.setenv("DATABENTO_API_KEY", "db-test")
    hold = tmp_path / "hold"
    base = dict(parquet_root=str(root), cache_dir=str(cache), symbol="ES", day="20260106",
                write=False, overwrite=True, max_cost=None, splice=False, hold_root=str(hold))
    target = root / "2026" / "ES-2026_L2" / "20260106.parquet"
    assert pq.ParquetFile(target).metadata.num_rows == 0
    D.cmd_fetch(type("A", (), base)())               # dry run: converts, writes nothing
    assert pq.ParquetFile(target).metadata.num_rows == 0 and not hold.exists()
    D.cmd_fetch(type("A", (), dict(base, write=True))())
    assert pq.ParquetFile(target).metadata.num_rows > 0
    saved = list(hold.rglob("20260106.parquet"))
    assert len(saved) == 1 and pq.ParquetFile(saved[0]).metadata.num_rows == 0


def test_fetch_refuses_to_overwrite_real_depth_without_the_flag(tmp_path, monkeypatch, capsys):
    root = make_repo(tmp_path)
    monkeypatch.setattr(D, "_api", lambda *a, **k: pytest.fail("must not call the API"))
    monkeypatch.setenv("DATABENTO_API_KEY", "db-test")
    args = type("A", (), dict(parquet_root=str(root), cache_dir=str(tmp_path / "c"), symbol="ES",
                              day="20260105", write=True, overwrite=False, max_cost=None, splice=False,
                              hold_root=str(tmp_path / "h")))()
    D.cmd_fetch(args)
    assert "already has" in capsys.readouterr().out


# ---- validation against NT8 depth ---------------------------------------------------------

def test_validate_events_identical_streams_match_exactly():
    data, _ = D._synthetic_stream(3000, seed=5)
    stats, ev = D.convert_to_memory(io.BytesIO(data))
    lo, hi = int(ev["ts"][100]), int(ev["ts"][-100])
    res = D.validate_events(ev, ev, lo, hi, step_s=0.5, tol_ms=1.0)
    assert res["samples"] > 5
    assert all(v == 1.0 for v in res["match_same_instant"].values())


def test_validate_events_detects_a_different_book():
    data, _ = D._synthetic_stream(3000, seed=5)
    _, ev = D.convert_to_memory(io.BytesIO(data))
    other = {k: v.copy() for k, v in ev.items()}
    other["size"] = other["size"] + 1                # every level off by one contract
    res = D.validate_events(ev, other, int(ev["ts"][100]), int(ev["ts"][-100]), step_s=0.5, tol_ms=1.0)
    assert res["match_same_instant"][10] < 0.2


# ---- spliced days: keep the NT8 prefix, continue from Databento -----------------------------

def test_splice_fill_continues_the_book_without_a_break(tmp_path):
    data, snaps = D._synthetic_stream(2000, seed=21)
    arr = np.frombuffer(data[24:], dtype=D.MBP10)
    cut = 800
    # "NT8 depth": what the recorder wrote for the first `cut` records, then it stopped
    prefix = tmp_path / "prefix.parquet"
    nt8_meta = {b"replay_importer.timestamps": b"UTC", b"nrd2parquet.source_name": b"20260106.nrd"}
    with pq.ParquetWriter(str(prefix), D.L2_SCHEMA.with_metadata(nt8_meta), compression="zstd") as w:
        D.convert_stream(io.BytesIO(data[:24 + cut * 368]), w)
    # Databento's stream for the rest of the day, starting a few records BEFORE the seam
    tail = data[:24] + arr[cut - 5:].tobytes()
    out = tmp_path / "out.parquet"
    stats = D.splice_fill(prefix, io.BytesIO(tail), out, {"databento.symbol": "ESH6"})
    t = pq.read_table(out)
    assert t.num_rows == stats["existing_rows"] + stats["events"]
    ts = t.column("Timestamp").cast("int64").to_numpy()
    assert (np.diff(ts) >= 0).all()                                   # one monotonic stream
    book = D.Book()
    for i in range(t.num_rows):
        book.apply(int(t.column("MarketDataType")[i].as_py()), int(t.column("Operation")[i].as_py()),
                   int(t.column("Position")[i].as_py()), float(t.column("Price")[i].as_py()),
                   int(t.column("Volume")[i].as_py()))
    got = tuple(tuple((p, s) for p, s in book.s[sd][:10]) for sd in (D.ASK, D.BID))
    assert got == snaps[-1]                                           # ends on the true final book
    md = pq.read_schema(out).metadata
    assert md[b"nrd2parquet.source_name"] == b"20260106.nrd"          # NT8 provenance kept
    assert b"replay_importer.source_name" not in md or b"databento" not in md[b"replay_importer.source_name"]
    assert md[b"databento.symbol"] == b"ESH6"


def test_fetch_splice_requires_existing_depth(tmp_path, monkeypatch, capsys):
    root = make_repo(tmp_path)
    monkeypatch.setattr(D, "_api", lambda *a, **k: pytest.fail("must not call the API"))
    monkeypatch.setenv("DATABENTO_API_KEY", "db-test")
    args = type("A", (), dict(parquet_root=str(root), cache_dir=str(tmp_path / "c"), symbol="ES",
                              day="20260106", write=True, overwrite=False, max_cost=None, splice=True,
                              hold_root=str(tmp_path / "h")))()
    D.cmd_fetch(args)                                    # 20260106 has a 0-row L2 file
    assert "--splice needs" in capsys.readouterr().out


# ---- batch mode: fetch-all ---------------------------------------------------------------

def _batch_args(root, tmp_path, **kw):
    base = dict(parquet_root=str(root), cache_dir=str(tmp_path / "cache"), year=2026,
                kinds="empty,nofile,databento,spliced", symbols=None, since="", until="99999999",
                budget=None, max_day_cost=None, write=False, log=str(tmp_path / "fill_log.csv"),
                hold_root=str(tmp_path / "hold"))
    base.update(kw)
    return type("A", (), base)()


def _fake_api_factory(cost="2.00"):
    calls = []

    def fake(method, endpoint, params, key, stream=False):
        calls.append((method, endpoint))
        if endpoint == "timeseries.get_range":
            pytest.fail("a dry run must never download")
        return {"metadata.get_cost": cost.encode(), "metadata.get_record_count": b"1",
                "metadata.get_billable_size": b"1"}[endpoint]
    return fake, calls


def test_fetch_all_dry_run_prices_only_and_never_downloads(tmp_path, monkeypatch, capsys):
    root = make_repo(tmp_path)
    fake, calls = _fake_api_factory()
    monkeypatch.setattr(D, "_api", fake)
    monkeypatch.setenv("DATABENTO_API_KEY", "db-test")
    assert D.cmd_fetch_all(_batch_args(root, tmp_path)) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "4 symbol-days" in out
    assert all(e.startswith("metadata.") for _, e in calls)


def test_fetch_all_write_fills_days_and_is_resumable(tmp_path, monkeypatch, capsys):
    root = make_repo(tmp_path)
    data, _ = D._synthetic_stream(300)
    payload = pa.compress(data, codec="zstd", asbytes=True)

    def fake(method, endpoint, params, key, stream=False):
        if endpoint == "timeseries.get_range":
            class R(io.BytesIO):
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return R(payload)
        return {"metadata.get_cost": b"1.50", "metadata.get_record_count": b"300",
                "metadata.get_billable_size": b"1"}[endpoint]
    monkeypatch.setattr(D, "_api", fake)
    monkeypatch.setenv("DATABENTO_API_KEY", "db-test")
    # the spliced ES day has partial depth (3 rows of a dummy table); give it a real NT8 prefix instead
    args = _batch_args(root, tmp_path, write=True, kinds="empty,nofile,databento")
    assert D.cmd_fetch_all(args) == 0
    filled = [t for t in D.scan_targets(root, 2026, set(D.KINDS))]
    assert filled == [{"symbol": "ES", "day": "20260108", "kind": "spliced"}]      # only the untouched kind remains
    for sym, day in (("ES", "20260106"), ("ES", "20260107"), ("SI", "20260109")):
        assert pq.ParquetFile(root / "2026" / f"{sym}-2026_L2" / f"{day}.parquet").metadata.num_rows > 0
    log = list(csv.DictReader(open(tmp_path / "fill_log.csv")))
    assert [r["status"] for r in log] == ["wrote"] * 3 and all(r["charged"] == "True" for r in log)
    capsys.readouterr()
    D.cmd_fetch_all(_batch_args(root, tmp_path, write=True, kinds="empty,nofile,databento"))   # second run
    assert "0 symbol-days with missing depth" in capsys.readouterr().out


def test_fetch_all_budget_stops_before_overspending(tmp_path, monkeypatch, capsys):
    root = make_repo(tmp_path)
    data, _ = D._synthetic_stream(100)
    payload = pa.compress(data, codec="zstd", asbytes=True)

    def fake(method, endpoint, params, key, stream=False):
        if endpoint == "timeseries.get_range":
            class R(io.BytesIO):
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return R(payload)
        return {"metadata.get_cost": b"4.00", "metadata.get_record_count": b"1", "metadata.get_billable_size": b"1"}[endpoint]
    monkeypatch.setattr(D, "_api", fake)
    monkeypatch.setenv("DATABENTO_API_KEY", "db-test")
    rc = D.cmd_fetch_all(_batch_args(root, tmp_path, write=True, budget=9.0, kinds="empty,nofile,databento"))
    out = capsys.readouterr().out
    assert rc == 2 and "STOPPED" in out and "spent $8.00" in out        # two days ($8); a third would pass $9
    assert sum(1 for t in D.scan_targets(root, 2026, {"empty", "nofile", "databento"})) == 1


def test_fetch_all_one_failing_day_does_not_stop_the_run(tmp_path, monkeypatch, capsys):
    root = make_repo(tmp_path)
    data, _ = D._synthetic_stream(100)
    payload = pa.compress(data, codec="zstd", asbytes=True)

    def fake(method, endpoint, params, key, stream=False):
        if endpoint == "timeseries.get_range":
            if params["symbols"].startswith("SI"):
                raise RuntimeError("Databento boom")
            class R(io.BytesIO):
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return R(payload)
        return {"metadata.get_cost": b"1.00", "metadata.get_record_count": b"1", "metadata.get_billable_size": b"1"}[endpoint]
    monkeypatch.setattr(D, "_api", fake)
    monkeypatch.setenv("DATABENTO_API_KEY", "db-test")
    D.cmd_fetch_all(_batch_args(root, tmp_path, write=True, kinds="empty,nofile,databento"))
    log = list(csv.DictReader(open(tmp_path / "fill_log.csv")))
    assert sorted(r["status"][:5] for r in log) == ["ERROR", "wrote", "wrote"]


def test_fetch_all_budget_also_guards_splice_days(tmp_path, monkeypatch, capsys):
    root = make_repo(tmp_path)
    data, _ = D._synthetic_stream(200)
    nt8_meta = {b"replay_importer.timestamps": b"UTC", b"nrd2parquet.source_name": b"20260108.nrd"}
    prefix = root / "2026" / "ES-2026_L2" / "20260108.parquet"          # a real NT8-style partial day
    with pq.ParquetWriter(str(prefix), D.L2_SCHEMA.with_metadata(nt8_meta), compression="zstd") as w:
        D.convert_stream(io.BytesIO(data[:24 + 100 * 368]), w)

    def fake(method, endpoint, params, key, stream=False):
        if endpoint == "timeseries.get_range":
            pytest.fail("over budget: must not download")
        return {"metadata.get_cost": b"5.00", "metadata.get_record_count": b"1", "metadata.get_billable_size": b"1"}[endpoint]
    monkeypatch.setattr(D, "_api", fake)
    monkeypatch.setenv("DATABENTO_API_KEY", "db-test")
    rc = D.cmd_fetch_all(_batch_args(root, tmp_path, write=True, budget=4.0, kinds="spliced"))
    assert rc == 2 and "ES 20260108" in capsys.readouterr().out
