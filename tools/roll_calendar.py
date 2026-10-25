"""Build and check data/roll_calendar.json, the schedule of contract rolls the
engine flattens before (backtester/rolls.py explains why a schedule and not the
price jump).

    python3 -m tools.roll_calendar build [--symbols MNQ,YM,...] [--jobs 4]
    python3 -m tools.roll_calendar check [--symbols ...] [--jobs 4]
    python3 -m tools.roll_calendar show  [--symbols ...]

build   scans every day file of each symbol (cached reductions, a few minutes in
        all), measures the jump at each midnight-ET boundary, keeps the ones
        inside the windows the roll rules predict as ROLLS and lists big jumps
        outside every window as UNEXPLAINED (data defects), then writes the file.
        Run it after each weekly data run: the engine warns when a run goes past
        the calendar's `through` date.
check   rescans and compares with the file; exit 1 if anything differs.
show    prints the calendar.

Reads the data in place; writes only data/roll_calendar.json. The file holds roll
DATES and jump sizes, not prices of the raw repo: still keep it out of the public
repo like the rest of the private data notes.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtester import rolls  # noqa: E402
from backtester.contracts import get_spec  # noqa: E402
from backtester.data import Catalog  # noqa: E402

EXCLUDED = {"CL": "monthly contract, roll rule not modelled; unmaintained since 2026-05",
            "MCL": "monthly contract, roll rule not modelled; unmaintained since 2026-05"}
METHOD = ("jump = first trade after midnight ET minus last before, for files that "
          "meet at midnight (both within 10 min); a jump of at least "
          f"{rolls.FLOOR_PCT}% and {rolls.FLOOR_TICKS} ticks within "
          f"{rolls.WINDOW_DAYS} days of a rule-predicted roll date is a roll; one of "
          f"{rolls.UNEXPLAINED_PCT}% or more outside every window is unexplained")


def scan_symbol(symbol: str, data_root=None) -> dict:
    """The calendar entry for one symbol, from its day files."""
    cat = Catalog(data_root)
    dates = list(cat.day_files(symbol))
    tick = get_spec(symbol).tick_size
    found: list[rolls.Boundary] = []
    prev = None
    for d in dates:
        day = cat.load_day(symbol, d)
        if prev is not None:
            b = rolls.measure(prev, day, tick)
            if b is not None:
                found.append(b)
        prev = day if len(day) else None
    y0, y1 = int(dates[0][:4]), int(dates[-1][:4])
    kept, unexplained = rolls.classify(symbol, found, y0, y1)
    return {"first": dates[0], "through": dates[-1],
            "rolls": [rolls.entry(b) for b in kept],
            "unexplained": [rolls.entry(b) for b in unexplained]}


def build(symbols: list[str], jobs: int, data_root=None) -> dict:
    if jobs <= 1:
        out = {s: scan_symbol(s, data_root) for s in symbols}
    else:
        with cf.ProcessPoolExecutor(max_workers=jobs) as ex:
            futs = {s: ex.submit(scan_symbol, s, data_root) for s in symbols}
            out = {s: futs[s].result() for s in symbols}
    return {"schema": rolls.SCHEMA, "method": METHOD,
            "excluded": {k: v for k, v in EXCLUDED.items()},
            "symbols": dict(sorted(out.items()))}


def _strip(cal: dict) -> dict:
    return {k: v for k, v in cal.items() if k != "generated_utc"}


def _symbols(arg: str | None) -> list[str]:
    return [s.strip().upper() for s in arg.split(",")] if arg else list(rolls.SYMBOLS)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=("build", "check", "show"))
    ap.add_argument("--symbols", help="comma list (default: every rule-covered symbol)")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--out", help="calendar path (default data/roll_calendar.json)")
    ap.add_argument("--data-root")
    args = ap.parse_args(argv)
    path = Path(args.out) if args.out else rolls.CALENDAR_PATH

    if args.command == "show":
        cal = json.loads(path.read_text(encoding="utf-8"))
        for s in _symbols(args.symbols):
            v = cal["symbols"].get(s)
            if v is None:
                print(f"{s}: not in the calendar")
                continue
            print(f"{s}: files {v['first']}..{v['through']}, {len(v['rolls'])} rolls, "
                  f"{len(v['unexplained'])} unexplained")
            for r in v["rolls"]:
                print(f"    roll        {r['date']}  {r['jump_ticks']:+9.1f} ticks  "
                      f"{r['jump_pct']:+6.2f}%")
            for r in v["unexplained"]:
                print(f"    UNEXPLAINED {r['date']}  {r['jump_ticks']:+9.1f} ticks  "
                      f"{r['jump_pct']:+6.2f}%")
        return 0

    fresh = build(_symbols(args.symbols), args.jobs, args.data_root)
    if args.command == "build":
        if args.symbols and path.is_file():          # a partial build keeps the others
            old = json.loads(path.read_text(encoding="utf-8"))
            fresh["symbols"] = dict(sorted({**old["symbols"], **fresh["symbols"]}.items()))
        fresh["generated_utc"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(fresh, indent=1) + "\n", encoding="utf-8")
        n = sum(len(v["rolls"]) for v in fresh["symbols"].values())
        u = sum(len(v["unexplained"]) for v in fresh["symbols"].values())
        print(f"wrote {path}: {len(fresh['symbols'])} symbols, {n} rolls, {u} unexplained")
        return 0

    # check
    old = _strip(json.loads(path.read_text(encoding="utf-8")))
    bad = 0
    for s, v in fresh["symbols"].items():
        o = old["symbols"].get(s)
        if o != v:
            bad += 1
            print(f"DIFFERS {s}: file has {None if o is None else (len(o['rolls']), len(o['unexplained']), o['through'])}, "
                  f"data gives {(len(v['rolls']), len(v['unexplained']), v['through'])}")
    print("roll calendar matches the data" if not bad else
          f"{bad} symbol(s) differ: python3 -m tools.roll_calendar build")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
