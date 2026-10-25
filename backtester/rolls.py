"""Contract rolls: where the day files switch from one contract to the next.

Each raw day file holds ONE contract, unadjusted (the weekly pipeline picks a
contract per ET calendar day by its roll rules). When two files meet at midnight
ET inside a continuous Globex session and the contract changes between them, the
price jumps by the calendar spread. A position held across that midnight books the
spread as P&L and the bar builders see it as a real move. In the data this engine was
built on, index rolls land at Monday 00:00 ET and gold and silver on a weekday
midnight.

The engine therefore flattens before a roll, and it must do so from a SCHEDULE: a
roll date is public in advance, a price jump at the next file's first tick is not,
so deciding from the jump would be look-ahead. This module holds the schedule
(data/roll_calendar.json, written by tools/roll_calendar.py) and the pure functions
the tool uses to build it:

  measure()   the jump at a midnight boundary between two day files
  classify()  keep the jumps that fall inside the window the documented roll rules
              predict (+-7 days) as ROLLS; a big jump outside any window is
              UNEXPLAINED (a data defect, e.g. a one-day wrong-contract file) and is
              reported, never flattened on.

Rules the windows come from (the data pipeline's roll rules): index
futures roll on the Monday before the 3rd Friday of Mar/Jun/Sep/Dec; gold (GC, MGC)
and silver (SI) on the 3rd business day before the 1st of the expiry month (gold
Feb/Apr/Jun/Aug/Oct/Dec, silver Mar/May/Jul/Sep/Dec). CL and MCL are not covered.
"""
from __future__ import annotations

import bisect
import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
CALENDAR_PATH = Path(__file__).resolve().parent.parent / "data" / "roll_calendar.json"
SCHEMA = 1

WINDOW_DAYS = 7               # a jump this close to a predicted roll date is a roll
CONTIGUOUS_S = 600            # files "meet at midnight": last/first trade within 10 min
FLOOR_PCT = 0.05              # a roll must jump at least this % of price ...
FLOOR_TICKS = 8               # ... and this many ticks (ordinary midnight moves: p95 ~5)
UNEXPLAINED_PCT = 0.25        # a bigger jump outside every window is reported

INDEX = ("ES", "MES", "NQ", "MNQ", "YM", "MYM", "RTY", "M2K")
EXPIRY_MONTHS = {**{s: (3, 6, 9, 12) for s in INDEX},
                 "GC": (2, 4, 6, 8, 10, 12), "MGC": (2, 4, 6, 8, 10, 12),
                 "SI": (3, 5, 7, 9, 12)}
SYMBOLS = tuple(EXPIRY_MONTHS)


def third_friday(year: int, month: int) -> dt.date:
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(4 - d.weekday()) % 7)        # first Friday
    return d + dt.timedelta(days=14)


def business_days_before(d: dt.date, n: int) -> dt.date:
    """The n-th weekday before `d` (weekends only; exchange holidays ignored)."""
    while n:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def expected_dates(symbol: str, first_year: int, last_year: int) -> list[dt.date]:
    """Roll dates the pipeline's rules predict (the first file on the new contract)."""
    months = EXPIRY_MONTHS.get(symbol.upper())
    if months is None:
        raise KeyError(f"no roll rule for {symbol}")
    out = []
    for y in range(first_year, last_year + 1):
        for m in months:
            if symbol.upper() in INDEX:
                out.append(third_friday(y, m) - dt.timedelta(days=4))   # the Monday
            else:
                out.append(business_days_before(dt.date(y, m, 1), 3))
    return out


@dataclass(frozen=True)
class Boundary:
    date: str            # YYYYMMDD of the file AFTER midnight (the new contract's first day)
    jump_ticks: float    # first trade price after midnight minus last before, in ticks
    jump_pct: float      # the same, % of the earlier price
    gap_s: float         # seconds between the two trades
    old_px: float
    new_px: float


def _midnight_ns(date: str) -> int:
    d = dt.datetime.strptime(date, "%Y%m%d")
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=ET).timestamp()) * 10**9


def measure(prev, nxt, tick: float) -> Boundary | None:
    """The jump between two DayL1 files, or None unless they meet at midnight ET:
    consecutive calendar dates, the last trade of `prev` within 10 min before
    midnight and the first of `nxt` within 10 min after. Anything else is a real
    halt or weekend (the engine already flattens at a session end there)."""
    if len(prev) == 0 or len(nxt) == 0:
        return None
    d0 = dt.datetime.strptime(prev.date, "%Y%m%d").date()
    d1 = dt.datetime.strptime(nxt.date, "%Y%m%d").date()
    if (d1 - d0).days != 1:
        return None
    mid = _midnight_ns(nxt.date)
    last_ts, first_ts = int(prev.ts[-1]), int(nxt.ts[0])
    lim = CONTIGUOUS_S * 10**9
    if not (0 <= mid - last_ts <= lim and 0 <= first_ts - mid <= lim):
        return None
    old_px, new_px = float(prev.price[-1]), float(nxt.price[0])
    return Boundary(date=nxt.date, jump_ticks=(new_px - old_px) / tick,
                    jump_pct=(new_px - old_px) / old_px * 100.0,
                    gap_s=(first_ts - last_ts) / 1e9, old_px=old_px, new_px=new_px)


def classify(symbol: str, boundaries: list[Boundary], first_year: int,
             last_year: int) -> tuple[list[Boundary], list[Boundary]]:
    """(rolls, unexplained). A boundary inside a predicted window that jumps at
    least FLOOR_PCT and FLOOR_TICKS is a roll (several in one window are all kept:
    each is a real discontinuity). A boundary outside every window that jumps
    UNEXPLAINED_PCT or more is unexplained. Everything else is ordinary."""
    expected = expected_dates(symbol, first_year - 1, last_year + 1)
    rolls, unexplained = [], []
    for b in boundaries:
        d = dt.datetime.strptime(b.date, "%Y%m%d").date()
        near = any(abs((d - e).days) <= WINDOW_DAYS for e in expected)
        if near:
            if abs(b.jump_pct) >= FLOOR_PCT and abs(b.jump_ticks) >= FLOOR_TICKS:
                rolls.append(b)
        elif abs(b.jump_pct) >= UNEXPLAINED_PCT:
            unexplained.append(b)
    return rolls, unexplained


def entry(b: Boundary) -> dict:
    return {"date": b.date, "jump_ticks": round(b.jump_ticks, 2),
            "jump_pct": round(b.jump_pct, 4)}


class RollCalendar:
    """data/roll_calendar.json: per symbol, the dates the contract changes."""

    def __init__(self, data: dict):
        if data.get("schema") != SCHEMA:
            raise ValueError(f"roll calendar schema {data.get('schema')!r}, "
                             f"expected {SCHEMA}")
        self.data = data
        self._syms = {}
        for sym, v in data.get("symbols", {}).items():
            dates = [r["date"] for r in v["rolls"]]
            if dates != sorted(set(dates)):
                raise ValueError(f"{sym}: roll dates must be unique and ascending")
            for key in ("first", "through"):
                if not (isinstance(v.get(key), str) and len(v[key]) == 8):
                    raise ValueError(f"{sym}: {key!r} must be a YYYYMMDD string")
            self._syms[sym.upper()] = (dates, v)

    @classmethod
    def load(cls, path: Path | str | None = None) -> "RollCalendar | None":
        """None when the file does not exist (the engine then warns)."""
        p = Path(path) if path else CALENDAR_PATH
        if not p.is_file():
            return None
        return cls(json.loads(p.read_text(encoding="utf-8")))

    def covers(self, symbol: str) -> bool:
        return symbol.upper() in self._syms

    def roll_dates(self, symbol: str) -> list[str]:
        return list(self._syms[symbol.upper()][0])

    def roll_between(self, symbol: str, cur_date: str, next_date: str) -> str | None:
        """The roll date B with cur_date < B <= next_date: the contract changes
        between the file dated `cur_date` and the next file in the run."""
        dates = self._syms[symbol.upper()][0]
        i = bisect.bisect_right(dates, cur_date)
        if i < len(dates) and dates[i] <= next_date:
            return dates[i]
        return None

    def window_sha(self, symbol: str, start: str | None, end: str | None) -> str:
        """Identity of the rolls a run over [start, end] can see: the sha of the
        roll dates inside it. Extending the calendar, or editing the unexplained
        list, does not change it; adding a roll inside the window does."""
        s = (start or "00000000").replace("-", "")
        e = (end or "99999999").replace("-", "")
        dates = [d for d in self.roll_dates(symbol) if s <= d <= e]
        return hashlib.sha256(json.dumps(dates).encode()).hexdigest()[:16]

    def notes(self, symbol: str, first_day: str, last_day: str) -> list[str]:
        """Things a reader of a run over these days must know."""
        sym = symbol.upper()
        if sym not in self._syms:
            why = self.data.get("excluded", {}).get(sym, "not in the calendar")
            return [f"no roll calendar for {sym} ({why}): a position held across a "
                    f"contract roll books the calendar spread as P&L"]
        _, v = self._syms[sym]
        out = []
        if last_day > v["through"]:
            out.append(f"the roll calendar for {sym} ends {v['through']} but the run "
                       f"reaches {last_day}: rolls after that are not flattened "
                       f"(python3 -m tools.roll_calendar build)")
        if first_day < v["first"]:
            out.append(f"the roll calendar for {sym} starts {v['first']} but the run "
                       f"starts {first_day}: earlier rolls are not flattened")
        for u in v.get("unexplained", []):
            if first_day <= u["date"] <= last_day:
                out.append(f"{sym} {u['date']}: unscheduled price discontinuity of "
                           f"{u['jump_ticks']:+.0f} ticks ({u['jump_pct']:+.2f}%) at "
                           f"midnight, not a roll date; looks like a data defect "
                           f"(a one-day wrong-contract file). Not flattened: a "
                           f"position open across it carries phantom P&L")
        return out
