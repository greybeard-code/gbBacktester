"""Find exit orders that fire against a trade they were NOT submitted for.

    python3 -m tools.orphan_exits <strategy.py> --start D --end D [--symbol S] [--period P]

Background: before 2026-10-21 the broker did not cancel a position's stop/target legs
when the position was closed some other way (the strategy's own close or reversal),
so a leftover leg could later fire and open a position nobody asked for, or close a
LATER trade at the old leg's level. The broker now cancels such legs (reduce-only),
so this tool should always report zero; it stays as the verifier. It tags every exit
order, when it is submitted, with the trade that is open at that moment, and reports
each exit order that reaches a fill while a different trade (or none) is open:

  A  no trade open: the fill OPENS a position from flat
  B  another, later trade is open: the old leg acts on it

Exit code 1 if any orphan fires.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtester.broker import SimBroker  # noqa: E402
from backtester.engine import Backtest  # noqa: E402
from backtester.loader import load_strategy  # noqa: E402

ET = ZoneInfo("America/New_York")


@contextlib.contextmanager
def watching():
    """Patch SimBroker for the duration; yields the list the hits are appended to."""
    hits: list[dict] = []
    parent: dict[int, object] = {}
    orig_submit, orig_fill = SimBroker.submit, SimBroker._fill

    def submit(self, order, bracket=None):
        r = orig_submit(self, order, bracket)
        if order.is_exit and order.state.value == "working":
            parent[order.id] = self.recorder.open        # the trade it is meant for
        return r

    def fill(self, o, idx):
        if o.is_exit and o.id in parent:
            cur = self.recorder.open
            if parent[o.id] is not cur:
                hits.append({"ts": int(self._day.ts[idx]), "tag": o.tag,
                             "case": "A" if cur is None else "B",
                             "position_before": self.account.position, "harmed": cur})
        return orig_fill(self, o, idx)

    SimBroker.submit, SimBroker._fill = submit, fill
    try:
        yield hits
    finally:
        SimBroker.submit, SimBroker._fill = orig_submit, orig_fill


def find_orphans(bt: Backtest):
    """Run `bt` and return (orphans, result). Each orphan: ts, tag, case A|B,
    position_before, and `trade` - the trade it created (A) or acted on (B)."""
    with watching() as hits:
        res = bt.run()
    for h in hits:
        t = h.pop("harmed")
        if t is None:                                     # case A: the trade it opened
            t = next((x for x in res.trades
                      if x.entry_ts == h["ts"] and x.entry_tag == h["tag"]), None)
        h["trade"] = t
    return hits, res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("strategy")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--symbol")
    ap.add_argument("--period")
    args = ap.parse_args(argv)
    bt = Backtest(load_strategy(args.strategy), start=args.start, end=args.end,
                  symbol=args.symbol, period=args.period, progress=False)
    orphans, res = find_orphans(bt)
    print(f"{res.strategy_name}: {len(res.trades)} trades, "
          f"{len(orphans)} orphaned exit fill(s)")
    for h in orphans:
        t = h["trade"]
        when = dt.datetime.fromtimestamp(h["ts"] / 1e9, ET).strftime("%Y-%m-%d %H:%M:%S ET")
        what = ("opens a position from flat" if h["case"] == "A"
                else "acts on a LATER trade")
        print(f"  {when}  leg '{h['tag']}' {what}; trade pnl "
              f"{t.pnl:,.2f} (exit {t.exit_tag!r})" if t else
              f"  {when}  leg '{h['tag']}' {what}")
    return 1 if orphans else 0


if __name__ == "__main__":
    sys.exit(main())
