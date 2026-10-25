"""The roll schedule (backtester/rolls.py): the rules that predict roll dates, the
jump measurement at a midnight boundary, the classification into rolls and
unexplained discontinuities, and the calendar file the engine reads.

Hand-computed throughout. The dates are real: June 2025 had its 3rd Friday on the
20th, so the index roll (Monday before) is 2025-06-16, which is also where the
data's MNQ price jumps 868 ticks.
"""
from __future__ import annotations

import datetime as dt
import json

import numpy as np
import pytest

from backtester import rolls
from backtester.rolls import Boundary, RollCalendar

from conftest import NS, et_ns, make_day_at


def d(y, m, day):
    return dt.date(y, m, day)


# ------------------------------------------------------------ the rules --

def test_third_friday_and_the_index_roll_monday():
    # 2025-06-01 is a Sunday: Fridays are the 6th, 13th, 20th -> 3rd is the 20th
    assert rolls.third_friday(2025, 6) == d(2025, 6, 20)
    # 2025-03-01 is a Saturday: Fridays 7, 14, 21 -> 21
    assert rolls.third_friday(2025, 3) == d(2025, 3, 21)
    # 2025-12-01 is a Monday: Fridays 5, 12, 19 -> 19
    assert rolls.third_friday(2025, 12) == d(2025, 12, 19)
    # the roll is the Monday before: Friday - 4 days
    assert rolls.expected_dates("MNQ", 2025, 2025) == [
        d(2025, 3, 17), d(2025, 6, 16), d(2025, 9, 15), d(2025, 12, 15)]


def test_business_days_before_skips_weekends():
    # 2025-02-01 is a Saturday: Fri Jan 31 is 1, Thu Jan 30 is 2, Wed Jan 29 is 3
    assert rolls.business_days_before(d(2025, 2, 1), 3) == d(2025, 1, 29)
    # 2025-04-01 is a Tuesday: Mon Mar 31 is 1, Fri Mar 28 is 2, Thu Mar 27 is 3
    assert rolls.business_days_before(d(2025, 4, 1), 3) == d(2025, 3, 27)
    assert rolls.business_days_before(d(2025, 4, 1), 0) == d(2025, 4, 1)


def test_metal_rolls_use_each_metals_expiry_months():
    gold = rolls.expected_dates("MGC", 2025, 2025)
    assert gold[:3] == [d(2025, 1, 29), d(2025, 3, 27), d(2025, 5, 28)]   # Feb/Apr/Jun expiry
    assert len(gold) == 6
    # silver: Mar/May/Jul/Sep/Dec; Mar 1 2025 is a Saturday -> Fri Feb 28, Thu 27, Wed 26
    si = rolls.expected_dates("SI", 2025, 2025)
    assert si[0] == d(2025, 2, 26) and len(si) == 5
    assert rolls.expected_dates("mnq", 2025, 2025) == rolls.expected_dates("MNQ", 2025, 2025)


def test_a_symbol_without_a_rule_is_refused_not_guessed():
    with pytest.raises(KeyError, match="CL"):
        rolls.expected_dates("CL", 2025, 2025)


# ------------------------------------------------------- the measurement --

def day(date, hhmm, seconds, price, n=3):
    ts = et_ns(date, hhmm, seconds) + np.arange(n, dtype="int64") * NS
    return make_day_at(np.full(n, float(price)), ts, date)


def test_measure_reports_the_jump_in_ticks_and_percent():
    prev = day("20260615", "23:59", 50, 100.0)               # last trade 23:59:52
    nxt = day("20260616", "00:00", 3, 112.0)                 # first trade 00:00:03
    b = rolls.measure(prev, nxt, 0.25)
    # (112 - 100) / 0.25 = 48 ticks; 12 / 100 = 12 %; 23:59:52 -> 00:00:03 = 11 s
    assert b == Boundary(date="20260616", jump_ticks=48.0, jump_pct=12.0,
                         gap_s=11.0, old_px=100.0, new_px=112.0)


def test_measure_is_negative_for_a_drop():
    b = rolls.measure(day("20260615", "23:59", 50, 100.0),
                      day("20260616", "00:00", 3, 96.0), 0.25)
    assert b.jump_ticks == -16.0 and b.jump_pct == -4.0


def test_measure_only_counts_files_that_meet_at_midnight():
    prev = day("20260615", "23:59", 50, 100.0)               # last trade 23:59:52
    # not the next calendar day
    assert rolls.measure(prev, day("20260617", "00:00", 3, 112.0), 0.25) is None
    # first trade 10 min 8 s after midnight: a halt, not a continuous session
    assert rolls.measure(prev, day("20260616", "00:10", 8, 112.0), 0.25) is None
    # the last trade (23:49:54) 10 min 6 s before midnight
    assert rolls.measure(day("20260615", "23:49", 52, 100.0),
                         day("20260616", "00:00", 3, 112.0), 0.25) is None
    # exactly 10 min on each side still counts (the limit is inclusive)
    prev10 = day("20260615", "23:50", 0, 100.0, n=1)         # 23:50:00 exactly
    nxt10 = day("20260616", "00:10", 0, 112.0, n=1)          # 00:10:00 exactly
    assert rolls.measure(prev10, nxt10, 0.25) is not None
    # an empty file has no price
    empty = make_day_at(np.array([]), np.array([], dtype="int64"), "20260616")
    assert rolls.measure(prev, empty, 0.25) is None


def test_measure_works_across_the_dst_change():
    # clocks go forward on 2026-03-08; midnight ET of 03-08 is still EST (UTC-5)
    prev = day("20260307", "23:59", 50, 100.0)
    nxt = day("20260308", "00:00", 3, 101.0)
    assert rolls.measure(prev, nxt, 0.25).gap_s == 11.0


# ---------------------------------------------------- the classification --

def B(date, ticks, price=20000.0, tick=0.25):
    pct = ticks * tick / price * 100
    return Boundary(date=date, jump_ticks=float(ticks), jump_pct=pct, gap_s=2.0,
                    old_px=price, new_px=price + ticks * tick)


def test_a_big_jump_inside_a_window_is_a_roll_and_a_quiet_one_is_not():
    rs, un = rolls.classify("MNQ", [
        B("20250616", 868),          # the predicted roll day: 1.09 %
        B("20250617", 5),            # next night, ordinary: not a roll
        B("20250620", 900),          # 4 days after the prediction, still in the window
    ], 2025, 2025)
    assert [r.date for r in rs] == ["20250616", "20250620"]      # several per window are kept
    assert un == []


def test_the_window_is_seven_days_either_side():
    rs, un = rolls.classify("MNQ", [B("20250609", 900), B("20250623", 900),     # -7 and +7 days
                                    B("20250608", 900), B("20250624", 900)],    # -8 and +8
                            2025, 2025)
    assert [r.date for r in rs] == ["20250609", "20250623"]
    assert [u.date for u in un] == ["20250608", "20250624"]       # outside, and 0.9 % >= 0.25 %


def test_a_big_jump_outside_every_window_is_unexplained_not_a_roll():
    # MNQ 2025-03-27: 10 days after the predicted 03-17 roll, 1.03 % -> a defect day
    rs, un = rolls.classify("MNQ", [B("20250327", 831), B("20250328", -767)], 2025, 2025)
    assert rs == []
    assert [u.date for u in un] == ["20250327", "20250328"]


def test_a_moderate_jump_outside_a_window_is_ordinary():
    # 0.20 % at a random midnight: a news gap, below the 0.25 % that gets reported
    rs, un = rolls.classify("MNQ", [B("20250805", 160)], 2025, 2025)     # 40 pts = 0.2 %
    assert rs == [] and un == []


def test_the_percent_floor_decides_a_roll_on_a_high_priced_contract():
    # MNQ at 20000: 0.05 % is 10 points = 40 ticks. 39 ticks = 0.04875 %, 41 = 0.05125 %
    rs, _ = rolls.classify("MNQ", [B("20250616", 39), B("20250617", 41)], 2025, 2025)
    assert [r.date for r in rs] == ["20250617"]


def test_the_tick_floor_decides_a_roll_on_a_cheap_tick_contract():
    # SI at 30.00, tick 0.005: 7 ticks = 0.117 % clears the percent floor but not the 8-tick one
    rs, _ = rolls.classify("SI", [B("20250226", 7, 30.0, 0.005),
                                  B("20250227", 8, 30.0, 0.005)], 2025, 2025)
    assert [r.date for r in rs] == ["20250227"]


# ------------------------------------------------------ the calendar file --

def cal(**over):
    sym = {"first": "20190101", "through": "20261016",
           "rolls": [{"date": "20250616", "jump_ticks": 868.0, "jump_pct": 1.0},
                     {"date": "20250915", "jump_ticks": 950.0, "jump_pct": 0.98}],
           "unexplained": [{"date": "20250327", "jump_ticks": 831.0, "jump_pct": 1.03}]}
    sym.update(over)
    return RollCalendar({"schema": 1, "excluded": {"CL": "monthly contract"},
                         "symbols": {"MNQ": sym}})


def test_roll_between_finds_the_roll_that_falls_between_two_files():
    c = cal()
    assert c.roll_between("MNQ", "20250615", "20250616") == "20250616"   # Sunday -> Monday
    assert c.roll_between("MNQ", "20250613", "20250616") == "20250616"   # no Sunday file
    assert c.roll_between("mnq", "20250615", "20250616") == "20250616"
    assert c.roll_between("MNQ", "20250616", "20250617") is None          # already past
    assert c.roll_between("MNQ", "20250614", "20250615") is None          # not yet
    assert c.roll_between("MNQ", "20250915", "20250916") is None
    # two rolls between two distant files: the first one, once
    assert c.roll_between("MNQ", "20250601", "20251001") == "20250616"


def test_window_sha_depends_on_the_rolls_in_the_window_only():
    base = cal().window_sha("MNQ", "20250601", "20250630")
    # extending the calendar, or editing what it reports as unexplained, changes nothing
    assert cal(through="20271231").window_sha("MNQ", "20250601", "20250630") == base
    assert cal(unexplained=[]).window_sha("MNQ", "20250601", "20250630") == base
    # a roll outside the window changes nothing; a different roll date inside does
    assert cal().window_sha("MNQ", "20250601", "20250630") == \
        cal().window_sha("MNQ", "2025-06-01", "2025-06-30")
    moved = cal(rolls=[{"date": "20250617", "jump_ticks": 868.0, "jump_pct": 1.0},
                       {"date": "20250915", "jump_ticks": 950.0, "jump_pct": 0.98}])
    assert moved.window_sha("MNQ", "20250601", "20250630") != base
    # the second roll (09-15) is outside June: moving it leaves June's identity alone
    later = cal(rolls=[{"date": "20250616", "jump_ticks": 868.0, "jump_pct": 1.0},
                       {"date": "20250916", "jump_ticks": 950.0, "jump_pct": 0.98}])
    assert later.window_sha("MNQ", "20250601", "20250630") == base
    # no bounds: the whole history
    assert cal().window_sha("MNQ", None, None) != base


def test_notes_say_what_a_reader_of_the_run_must_know():
    c = cal()
    assert c.notes("MNQ", "20250601", "20250630") == []          # inside coverage, no defect day
    late = c.notes("MNQ", "20250601", "20270101")
    assert len(late) == 1 and "ends 20261016" in late[0] and "20270101" in late[0]
    # the hint must name a subcommand that exists (it said `update`, which does not)
    assert "tools.roll_calendar build" in late[0] and "roll_calendar update" not in late[0]
    early = c.notes("MNQ", "20180101", "20250301")
    assert len(early) == 1 and "starts 20190101" in early[0]
    defect = c.notes("MNQ", "20250320", "20250401")
    assert len(defect) == 1 and "20250327" in defect[0] and "+831 ticks" in defect[0]
    assert c.notes("MNQ", "20250401", "20250630") == []          # the defect day is outside
    cl = c.notes("CL", "20250101", "20250301")
    assert len(cl) == 1 and "no roll calendar for CL" in cl[0] and "monthly contract" in cl[0]
    assert "not in the calendar" in c.notes("ZN", "20250101", "20250301")[0]


@pytest.mark.parametrize("data, msg", [
    ({"schema": 2, "symbols": {}}, "schema"),
    ({"schema": 1, "symbols": {"MNQ": {"first": "20190101", "through": "20261016",
                                       "rolls": [{"date": "20250915"}, {"date": "20250616"}]}}},
     "ascending"),
    ({"schema": 1, "symbols": {"MNQ": {"first": "20190101", "through": "20261016",
                                       "rolls": [{"date": "20250616"}, {"date": "20250616"}]}}},
     "unique"),
    ({"schema": 1, "symbols": {"MNQ": {"first": "2019", "through": "20261016", "rolls": []}}},
     "first"),
    ({"schema": 1, "symbols": {"MNQ": {"first": "20190101", "rolls": []}}}, "through"),
])
def test_a_malformed_calendar_is_refused(data, msg):
    with pytest.raises(ValueError, match=msg):
        RollCalendar(data)


def test_load_returns_none_for_a_missing_file_and_round_trips_a_real_one(tmp_path):
    assert RollCalendar.load(tmp_path / "nope.json") is None
    p = tmp_path / "c.json"
    p.write_text(json.dumps(cal().data), encoding="utf-8")
    c = RollCalendar.load(p)
    assert c.covers("MNQ") and not c.covers("CL")
    assert c.roll_dates("MNQ") == ["20250616", "20250915"]


# ------------------------------------------- the shipped data/roll_calendar.json --

@pytest.mark.skipif(not rolls.CALENDAR_PATH.is_file(),
                    reason="data/roll_calendar.json is built per installation: "
                           "python3 -m tools.roll_calendar build")
def test_the_shipped_calendar_is_valid_and_has_the_hand_verified_rolls():
    c = RollCalendar.load()
    assert c is not None, "data/roll_calendar.json is missing: python3 -m tools.roll_calendar build"
    assert set(rolls.SYMBOLS) <= set(c.data["symbols"])
    mnq = c.roll_dates("MNQ")
    for r in ("20250314", "20250616", "20250915", "20251215", "20260615"):
        assert r in mnq
    assert "20250327" not in mnq                    # the one-day wrong-contract file
    assert {"20250616", "20250915", "20251215", "20260615"} <= set(c.roll_dates("YM"))
    assert {"20250129", "20250327", "20250528", "20250729"} <= set(c.roll_dates("MGC"))
    unexplained = [u["date"] for u in c.data["symbols"]["MNQ"]["unexplained"]]
    assert unexplained == ["20250327", "20250328"]
    for sym in ("CL", "MCL"):
        assert sym in c.data["excluded"] and not c.covers(sym)
