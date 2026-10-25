"""tools/roll_calendar.py: scan the day files, keep the rolls, report the defects,
write and check data/roll_calendar.json.

Hand-computed on synthetic MNQ files (tick 0.25), no data root: the tool's Catalog is
replaced by one that serves in-memory days. Dates are the real ones the scan found:
the June 2025 roll lands on Monday 2025-06-16 and 2025-03-27 is the one-day
wrong-contract file.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from tools import roll_calendar as RC

from conftest import NS, et_ns, make_day_at


def one_event(date, hhmm, seconds, price):
    return make_day_at(np.array([float(price)]),
                       np.array([et_ns(date, hhmm, seconds)], dtype="int64"), date)


def evening(date, price):            # a file that ends at 23:59:55
    return one_event(date, "23:59", 55, price)


def morning(date, price):            # a file that starts at 00:00:05
    return one_event(date, "00:00", 5, price)


class StubCatalog:
    FILES = {}

    def __init__(self, data_root=None):
        pass

    def day_files(self, symbol):
        return {d: None for d in sorted(self.FILES)}

    def load_day(self, symbol, date):
        return self.FILES[date]


@pytest.fixture
def stub(monkeypatch):
    # Sun 06-15 evening 20000.00, Mon 06-16 first trade 20200.00: +800 ticks = 1.0 %
    # (the predicted roll). 03-26 ends at 20134.25, 03-27 starts at 20342.00 (+831
    # ticks, 10 days after the predicted 03-17 roll: unexplained). A quiet night:
    # 06-16 ends 20200.00, 06-17 starts 20200.25 (+1 tick).
    files = {
        "20250326": evening("20250326", 20134.25),
        "20250327": morning("20250327", 20342.00),
        "20250615": evening("20250615", 20000.00),
        "20250616": morning("20250616", 20200.00),
    }
    # a second sequence in the same month so 06-16's end meets 06-17's start
    files["20250616"] = make_day_at(
        np.array([20200.00, 20200.00]),
        np.array([et_ns("20250616", "00:00", 5), et_ns("20250616", "23:59", 55)],
                 dtype="int64"), "20250616")
    files["20250617"] = morning("20250617", 20200.25)
    StubCatalog.FILES = files
    monkeypatch.setattr(RC, "Catalog", StubCatalog)
    return files


def test_scan_keeps_the_rule_predicted_roll_and_reports_the_defect_day(stub):
    v = RC.scan_symbol("MNQ")
    assert (v["first"], v["through"]) == ("20250326", "20250617")
    # 800 ticks * 0.25 = 200.00 points on 20000.00 = 1.0 %
    assert v["rolls"] == [{"date": "20250616", "jump_ticks": 800.0, "jump_pct": 1.0}]
    # 20342.00 - 20134.25 = 207.75 points = 831 ticks = 1.0318 % of 20134.25; 03-27 is
    # 10 days after the predicted 03-17: outside the +-7 day window
    assert v["unexplained"] == [{"date": "20250327", "jump_ticks": 831.0, "jump_pct": 1.0318}]


def test_build_writes_the_file_and_check_agrees_with_it(stub, tmp_path, capsys):
    out = tmp_path / "cal.json"
    assert RC.main(["build", "--symbols", "MNQ", "--jobs", "1", "--out", str(out)]) == 0
    cal = json.loads(out.read_text(encoding="utf-8"))
    assert cal["schema"] == 1 and "generated_utc" in cal
    assert cal["symbols"]["MNQ"]["rolls"][0]["date"] == "20250616"
    assert cal["excluded"].keys() == {"CL", "MCL"}
    capsys.readouterr()
    assert RC.main(["check", "--symbols", "MNQ", "--jobs", "1", "--out", str(out)]) == 0
    assert "matches the data" in capsys.readouterr().out


def test_check_fails_when_the_data_moved_and_names_the_symbol(stub, tmp_path, capsys):
    out = tmp_path / "cal.json"
    RC.main(["build", "--symbols", "MNQ", "--jobs", "1", "--out", str(out)])
    # the data changes: the Monday file now opens at the old level (no jump any more)
    stub["20250616"] = make_day_at(
        np.array([20000.25, 20200.00]),
        np.array([et_ns("20250616", "00:00", 5), et_ns("20250616", "23:59", 55)],
                 dtype="int64"), "20250616")
    capsys.readouterr()
    assert RC.main(["check", "--symbols", "MNQ", "--jobs", "1", "--out", str(out)]) == 1
    text = capsys.readouterr().out
    assert "DIFFERS MNQ" in text and "tools.roll_calendar build" in text


def test_a_partial_build_keeps_the_symbols_it_did_not_scan(stub, tmp_path):
    out = tmp_path / "cal.json"
    out.write_text(json.dumps({"schema": 1, "excluded": {}, "symbols": {"YM": {
        "first": "20190101", "through": "20261016",
        "rolls": [{"date": "20250616", "jump_ticks": 314.0, "jump_pct": 0.74}],
        "unexplained": []}}}), encoding="utf-8")
    RC.main(["build", "--symbols", "MNQ", "--jobs", "1", "--out", str(out)])
    cal = json.loads(out.read_text(encoding="utf-8"))
    assert set(cal["symbols"]) == {"MNQ", "YM"}
    assert cal["symbols"]["YM"]["rolls"][0]["jump_ticks"] == 314.0       # untouched


def test_show_prints_rolls_and_unexplained_days(stub, tmp_path, capsys):
    out = tmp_path / "cal.json"
    RC.main(["build", "--symbols", "MNQ", "--jobs", "1", "--out", str(out)])
    capsys.readouterr()
    assert RC.main(["show", "--symbols", "MNQ,ZN", "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "MNQ: files 20250326..20250617, 1 rolls, 1 unexplained" in text
    assert "roll        20250616     +800.0 ticks   +1.00%" in text
    assert "UNEXPLAINED 20250327     +831.0 ticks   +1.03%" in text
    assert "ZN: not in the calendar" in text


def test_the_default_symbol_list_is_every_rule_covered_symbol():
    assert RC._symbols(None) == list(RC.rolls.SYMBOLS)
    assert RC._symbols("mnq, ym") == ["MNQ", "YM"]
