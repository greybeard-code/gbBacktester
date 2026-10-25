"""Causality check: does a strategy's backtest use data it could not have had?

Reading code for lookahead works until it doesn't: the bug is one index, one
shift, one "today's full-day array" three files away from the signal. These
probes do not read the code. They run the real strategy through the real
engine twice and compare, so the answer comes from the numbers.

Adapted from optd-starter's backtests/probes.py (prefix_invariance and
warmup_sensitivity). Those take `indicator(bars) -> values`; here the unit
under test is the whole STRATEGY inside the engine, because bars are not this
repo's primitive (renko/wave bricks are built from the tick array and carry
[i0, i1) spans into it), and the bugs this repo has shipped lived below the
bar level (brick-anchor resets, crossed quotes).

  truncation   Run the full range and record, for every primary bar, the bar
               itself, the position and every order action the strategy took
               inside on_bar. Then pick cut points (bars where the strategy
               acted, plus random bars), DELETE every tick after each cut,
               rebuild the bars from what is left and run again. Everything
               at or before the cut must be identical. If a bar or a
               decision changes when the future is deleted, the backtest was
               reading the future. Catches: a signal reading the day's final
               price/high/low, a precomputed whole-day array, a bar builder
               that peeks, a higher-timeframe value built from finished data.

  warmup       Run from the first day and again from a later day. Decisions
               on the bars after a warm-up window must agree. If they don't,
               the number depends on how much history was loaded and a live
               chart (which starts somewhere else) will disagree with it.
               Offsets are in DAYS, so a strategy that counts rows (bar.index
               % N) instead of reading the clock fails.

What a clean run means: these checks found nothing, no more. Truncation is a
sampling test (each cut exposes the decisions near it), and a leak that only
fires on rare signals needs a cut near one - raise --cuts. A time-bar cut
leaves the bar after the cut partial; the probe never compares it.

Also compared at every bar: realized P&L and average entry price (so a fill
price or commission that moves with the future is caught even when the order
timing does not).

Runs with the prop-firm tracker OFF unless --prop-threshold is given; a
strategy that sizes from prop_headroom takes a different code path live.

Not covered: orders placed from on_tick / on_fill (only on_bar and
on_secondary_bar are traced; a decision made there shows up in the position
and later actions, but is not compared directly).

Usage (repo root), keep the range short (5-10 days): every cut re-runs the
backtest from the start of the range up to the cut.

    python3 -m tools.causality_check strategies/ema_cross.py \
        --start 2026-06-01 --end 2026-06-05 --cuts 24
"""
from __future__ import annotations

import argparse
import contextlib
import io
import random
import sys
import tempfile
from dataclasses import dataclass, field, fields, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtester import Backtest  # noqa: E402
from backtester.loader import load_strategy_class  # noqa: E402

ET = ZoneInfo("America/New_York")
_FLOAT_TOL = 1e-9
MIN_DECISIONS = 10      # below this a truncation PASS has little to bite on


# ----------------------------------------------------------------------------
# tracing
# ----------------------------------------------------------------------------

@dataclass
class BarRecord:
    index: int
    ts: int
    date: str
    ohlcv: tuple            # (open, high, low, close, volume, buy_vol, sell_vol)
    position: int           # signed position BEFORE on_bar ran
    actions: list = field(default_factory=list)
    # (realized $ net of commission, average entry price) BEFORE on_bar ran.
    # Fills inside a span resolve before on_bar, so this pins every fill price
    # and commission up to the cut: a broker-level leak that moves a fill
    # price without moving its timing shows up here and nowhere else.
    account: tuple = (0.0, 0.0)


class Trace:
    def __init__(self) -> None:
        self.bars: list[BarRecord] = []
        self.by_index: dict[int, BarRecord] = {}
        self.cur: list | None = None     # action sink while a callback runs
        self.pre: list = []              # on_secondary_bar actions, merged into next bar

    def add(self, rec: BarRecord) -> None:
        self.bars.append(rec)
        self.by_index[rec.index] = rec


def _px(p) -> float | None:
    if p is None:
        return None
    p = float(p)
    return None if p != p else round(p, 6)


def _wrap_broker(broker, trace: Trace) -> None:
    """Record order actions made while a traced callback is running."""
    def hook(name, fmt):
        orig = getattr(broker, name)

        def inner(*a, **k):
            if trace.cur is not None:
                trace.cur.append(fmt(*a, **k))
            return orig(*a, **k)
        setattr(broker, name, inner)

    hook("submit", lambda o, bracket=None: (
        "submit", o.side, o.type.value, o.qty, _px(o.price), o.tag, o.is_exit,
        (bracket.stop_ticks, bracket.target_ticks) if bracket else None))
    hook("cancel", lambda o: ("cancel", o.side, o.type.value, _px(o.price), o.tag))
    hook("cancel_all", lambda entries_only=False: ("cancel_all", bool(entries_only)))
    hook("modify", lambda o, price: ("modify", o.side, o.type.value, _px(price)))
    hook("flatten", lambda idx, tag="flatten": ("flatten", tag))


def attach_trace(bt: Backtest, catalog: "CutCatalog") -> Trace:
    """Instrument `bt` (before run()) so every on_bar is recorded."""
    trace = Trace()
    strat = bt.strategy
    _wrap_broker(bt.broker, trace)
    orig_on_bar = strat.on_bar

    def traced_on_bar(bar, bars):
        rec = BarRecord(
            index=bar.index, ts=bar.ts, date=catalog.last_date,
            ohlcv=(bar.open, bar.high, bar.low, bar.close, bar.volume,
                   bar.buy_volume, bar.sell_volume),
            position=int(bt.account.position), actions=list(trace.pre),
            account=(float(bt.account.realized), float(bt.account.avg_price)))
        trace.pre.clear()
        trace.add(rec)
        trace.cur = rec.actions
        try:
            orig_on_bar(bar, bars)
        finally:
            trace.cur = None
    strat.on_bar = traced_on_bar

    from backtester.strategy import Strategy
    if type(strat).on_secondary_bar is not Strategy.on_secondary_bar:
        orig_sec = strat.on_secondary_bar

        def traced_sec(bar, bars, period):
            trace.cur = trace.pre
            try:
                orig_sec(bar, bars, period)
            finally:
                trace.cur = None
        strat.on_secondary_bar = traced_sec
    return trace


# ----------------------------------------------------------------------------
# catalog wrapper: serve the same data, optionally with the future deleted
# ----------------------------------------------------------------------------

def _slice_day(day, n: int):
    """The same DayL1 with only its first n trade events."""
    return replace(day, **{f.name: getattr(day, f.name)[:n]
                           for f in fields(day) if f.name != "date"})


class CutCatalog:
    """Wraps a Catalog. With a cut, `cut_date`'s events after `cut_ts` are
    deleted and later days are dropped; the cut day's bars are rebuilt from the
    truncated ticks in a throwaway cache so the shared cache is never touched.
    Without a cut it only remembers which day file the engine is on."""

    def __init__(self, base, cut_date: str | None = None, cut_ts: int | None = None):
        self.base = base
        self.cut_date = cut_date
        self.cut_ts = cut_ts
        self.last_date = ""
        self._days: dict[str, object] = {}

    def days(self, symbol, start=None, end=None):
        ds = self.base.days(symbol, start, end)
        return [d for d in ds if self.cut_date is None or d <= self.cut_date]

    def _load(self, symbol, date):
        if date in self._days:
            return self._days[date]
        day = self.base.load_day(symbol, date)
        if date == self.cut_date:
            n = int(np.searchsorted(day.ts, self.cut_ts, side="right"))
            day = _slice_day(day, n)
        self._days[date] = day
        return day

    def load_day(self, symbol, date):
        self.last_date = date
        return self._load(symbol, date)

    def load_bars(self, symbol, date, spec, tick_size, day=None):
        return self.load_bars_sequence(symbol, [date], spec, tick_size)[0]

    def load_bars_sequence(self, symbol, dates, spec, tick_size, days=None):
        if self.cut_date is None or self.cut_date not in dates:
            return self.base.load_bars_sequence(symbol, dates, spec, tick_size, days)
        days = [self._load(symbol, d) for d in dates]
        if not hasattr(self.base, "_bars_path"):
            return self.base.load_bars_sequence(symbol, dates, spec, tick_size, days)
        # the bar cache is keyed by date: send the CUT day's bars (built from
        # truncated ticks) to a temp dir so the real cache never sees them
        with tempfile.TemporaryDirectory(prefix="causality_") as tmp:
            orig = self.base._bars_path
            cut = self.cut_date

            def redirected(sym, date, key):
                if date == cut:
                    return Path(tmp) / sym.upper() / key / f"{date}.parquet"
                return orig(sym, date, key)
            self.base._bars_path = redirected
            try:
                return self.base.load_bars_sequence(symbol, dates, spec,
                                                    tick_size, days)
            finally:
                del self.base._bars_path

    def __getattr__(self, name):          # anything else (e.g. data_root)
        return getattr(self.base, name)


def run_traced(factory, start, end, base_catalog=None, cut: tuple | None = None,
               **bt_kwargs) -> Trace:
    """One backtest with tracing. cut = (date, ts_ns) deletes ticks after ts_ns
    on `date` and drops later days; the run ends at that date."""
    bt_kwargs.setdefault("prop", None)
    strat = factory()
    cut_date = cut[0] if cut else None
    bt = Backtest(strat, start=start, end=cut_date or end, progress=False,
                  **bt_kwargs)
    base = base_catalog if base_catalog is not None else bt.catalog
    cat = CutCatalog(base, cut_date, cut[1] if cut else None)
    bt.catalog = cat
    trace = attach_trace(bt, cat)
    # strategies print a summary from on_finish; once per cut that is noise
    with contextlib.redirect_stdout(io.StringIO()):
        bt.run()
    return trace


# ----------------------------------------------------------------------------
# comparison
# ----------------------------------------------------------------------------

@dataclass
class Mismatch:
    cut_index: int
    bar_index: int
    kind: str               # bar | position | account | action | missing
    full: object
    cut: object
    date: str = ""
    ts: int = 0


def _eq(a, b) -> bool:
    if isinstance(a, (tuple, list)) and isinstance(b, (tuple, list)):
        return len(a) == len(b) and all(_eq(x, y) for x, y in zip(a, b))
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) <= _FLOAT_TOL
        except (TypeError, ValueError):
            return False
    return a == b


def compare_prefix(full: Trace, cut: Trace, cut_index: int) -> list[Mismatch]:
    """Bars 0..cut_index must be identical in both traces. Returns every
    divergence in bar order (most useful first: the earliest)."""
    out: list[Mismatch] = []
    for i in range(cut_index + 1):
        f = full.by_index.get(i)
        c = cut.by_index.get(i)
        if f is None:
            break
        if c is None:
            out.append(Mismatch(cut_index, i, "missing", f.ohlcv, None,
                                f.date, f.ts))
            continue
        for kind, a, b in (("bar", (f.ts,) + f.ohlcv, (c.ts,) + c.ohlcv),
                           ("position", f.position, c.position),
                           ("account", f.account, c.account),
                           ("action", f.actions, c.actions)):
            if not _eq(a, b):
                out.append(Mismatch(cut_index, i, kind, a, b, f.date, f.ts))
    return out


def pick_cuts(trace: Trace, n_cuts: int, seed: int = 0) -> list[BarRecord]:
    """Half the cuts on bars where the strategy submitted an order (a leak
    shows up at the decision it changes), half uniformly random (a leak that
    SUPPRESSES a signal shows up nowhere else). The last bar is never cut:
    cutting after it is the full run."""
    bars = trace.bars[:-1]
    if not bars or n_cuts <= 0:
        return []
    decision = [b for b in bars if any(a[0] == "submit" for a in b.actions)]
    k = min(len(decision), (n_cuts + 1) // 2)
    chosen = [decision[int(i * len(decision) / k)] for i in range(k)] if k else []
    taken = {b.index for b in chosen}
    rest = [b for b in bars if b.index not in taken]
    rng = random.Random(seed)
    chosen += rng.sample(rest, min(len(rest), n_cuts - len(chosen)))
    return sorted(chosen, key=lambda b: b.index)


@dataclass
class TruncationReport:
    cuts_run: int
    decision_cuts: int
    bars_compared: int
    mismatches: list[Mismatch]
    deterministic: bool = True
    full_decisions: int = 0          # bars in the full run where it submitted an order

    @property
    def low_power(self) -> bool:
        return self.full_decisions < MIN_DECISIONS

    @property
    def passed(self) -> bool:
        return self.deterministic and not self.mismatches


def truncation_invariance(factory, start, end, n_cuts: int = 24, seed: int = 0,
                          base_catalog=None, progress=None,
                          **bt_kwargs) -> TruncationReport:
    full = run_traced(factory, start, end, base_catalog, **bt_kwargs)
    again = run_traced(factory, start, end, base_catalog, **bt_kwargs)
    if (len(full.bars) != len(again.bars)
            or compare_prefix(full, again, len(full.bars) - 1)):
        # an unseeded random() in the strategy would look like a leak
        return TruncationReport(0, 0, 0, [], deterministic=False)
    cuts = pick_cuts(full, n_cuts, seed)
    mism: list[Mismatch] = []
    compared = 0
    decision = 0
    for n, b in enumerate(cuts, 1):
        if progress:
            progress(f"  cut {n}/{len(cuts)} at bar {b.index} "
                     f"({_et(b.ts)} ET, file {b.date})")
        t = run_traced(factory, start, end, base_catalog,
                       cut=(b.date, b.ts), **bt_kwargs)
        compared += b.index + 1
        decision += any(a[0] == "submit" for a in b.actions)
        mism += compare_prefix(full, t, b.index)
    n_dec = sum(any(a[0] == "submit" for a in b.actions) for b in full.bars)
    return TruncationReport(len(cuts), decision, compared, mism,
                            full_decisions=n_dec)


# ----------------------------------------------------------------------------
# warm-up sensitivity
# ----------------------------------------------------------------------------

def _decisions(trace: Trace, from_ts: int) -> dict[int, list]:
    """bar close ts -> actions with qty dropped (sizing may depend on balance,
    which legitimately depends on where the run started) and the sign of the
    position. Bars sharing a ts are merged in order."""
    out: dict[int, list] = {}
    for b in trace.bars:
        if b.ts < from_ts:
            continue
        acts = [(a[0], a[1], a[2]) + tuple(a[4:]) if a[0] == "submit" else a
                for a in b.actions]
        out.setdefault(b.ts, []).append((int(np.sign(b.position)), acts))
    return out


@dataclass
class WarmupReport:
    offsets: dict[int, dict]          # offset_days -> {mismatches, settled}

    @property
    def passed(self) -> bool:
        return all(v["mismatches"] == 0 for v in self.offsets.values())


def warmup_sensitivity(factory, start, end, offsets_days=(1, 3), warm_days: int = 2,
                       base_catalog=None, **bt_kwargs) -> WarmupReport:
    probe = Backtest(factory(), start=start, end=end, progress=False,
                     **{"prop": None, **bt_kwargs})
    cat0 = base_catalog if base_catalog is not None else probe.catalog
    days = cat0.days(probe.symbol, start, end)
    if len(days) <= max(offsets_days) + warm_days + 1:
        raise ValueError(f"need more than {max(offsets_days) + warm_days + 1} "
                         f"day files; range has {len(days)}")
    base = run_traced(factory, start, end, cat0, **bt_kwargs)
    report: dict[int, dict] = {}
    for off in offsets_days:
        late = run_traced(factory, days[off], end, cat0, **bt_kwargs)
        w_date = days[off + warm_days]
        w_ts = next((b.ts for b in base.bars if b.date >= w_date), None)
        if w_ts is None:
            raise ValueError("warm-up window reaches past the last bar")
        a, b = _decisions(base, w_ts), _decisions(late, w_ts)
        bad = sorted(t for t in set(a) | set(b) if not _eq(a.get(t), b.get(t)))
        # the earlier run's last disagreement anywhere after the later start
        a_all, b_all = _decisions(base, 0), _decisions(late, 0)
        first_late = min((x.ts for x in late.bars), default=0)
        diff_all = sorted(t for t in set(a_all) | set(b_all)
                          if t >= first_late and not _eq(a_all.get(t), b_all.get(t)))
        report[off] = {"mismatches": len(bad), "first_bad_ts": bad[0] if bad else None,
                       "window_from": w_date,
                       "last_disagreement_ts": diff_all[-1] if diff_all else None,
                       "bars_compared": len(a)}
    return WarmupReport(report)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def _et(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1e9, tz=ET).strftime("%Y-%m-%d %H:%M:%S")


def _parse_value(s: str):
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    for cast in (int, float):
        try:
            return cast(s)
        except ValueError:
            pass
    return s


def make_factory(path: str, overrides: dict):
    cls = load_strategy_class(path)

    def factory():
        s = cls()
        for k, v in overrides.items():
            if not hasattr(s, k):
                raise AttributeError(f"{cls.__name__} has no parameter {k!r}")
            setattr(s, k, v)
        return s
    return factory


def format_truncation(rep: TruncationReport, limit: int = 8) -> str:
    if not rep.deterministic:
        return ("truncation: INVALID - two identical full runs gave different "
                "traces. The strategy is not deterministic (unseeded random, "
                "set ordering, wall clock), so a difference after a cut would "
                "prove nothing.")
    lines = [f"truncation: {rep.cuts_run} cuts ({rep.decision_cuts} on decision "
             f"bars), {rep.bars_compared:,} bar comparisons"]
    if rep.passed:
        lines.append("  PASS - deleting the future changed no bar and no decision.")
        if rep.low_power:
            lines.append(
                f"  LOW POWER - the strategy submitted orders on only "
                f"{rep.full_decisions} bar(s) in this range"
                + (" (none at all)" if not rep.full_decisions else "")
                + f"; below {MIN_DECISIONS} a PASS says little. Widen the range "
                  "or loosen the entry filters before reading this as clean.")
        return "\n".join(lines)
    kinds: dict[str, int] = {}
    for m in rep.mismatches:
        kinds[m.kind] = kinds.get(m.kind, 0) + 1
    lines.append("  FAIL - " + ", ".join(f"{v} {k}" for k, v in sorted(kinds.items()))
                 + " mismatches. The result at bar i changed when bars after i "
                   "were deleted.")
    first = {}
    for m in rep.mismatches:           # earliest bar per kind
        if m.kind not in first or m.bar_index < first[m.kind].bar_index:
            first[m.kind] = m
    for m in list(first.values())[:limit]:
        lines.append(f"  [{m.kind}] bar {m.bar_index} ({_et(m.ts)} ET, file "
                     f"{m.date}), cut at bar {m.cut_index}:")
        lines.append(f"      full run : {m.full}")
        lines.append(f"      with cut : {m.cut}")
    return "\n".join(lines)


def format_warmup(rep: WarmupReport, warm_days: int) -> str:
    lines = [f"warmup: decisions compared from {warm_days} day files after each "
             "later start"]
    for off, v in sorted(rep.offsets.items()):
        if v["mismatches"] == 0:
            tail = ""
            if v["last_disagreement_ts"]:
                tail = (f" (they disagreed until {_et(v['last_disagreement_ts'])} "
                        "ET, inside the warm-up window)")
            lines.append(f"  start +{off}d: PASS - {v['bars_compared']:,} bars "
                         f"agree{tail}")
        else:
            lines.append(f"  start +{off}d: FAIL - {v['mismatches']} bars differ "
                         f"after the warm-up window ({v['window_from']}); first at "
                         f"{_et(v['first_bad_ts'])} ET. The result depends on how "
                         "much history was loaded.")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("strategy", help="path to a .py file with a Strategy subclass")
    ap.add_argument("--start", required=True, help="first day, YYYY-MM-DD or YYYYMMDD")
    ap.add_argument("--end", required=True)
    ap.add_argument("--symbol")
    ap.add_argument("--period", help="override bar type (e.g. 1m, r100-4, w120)")
    ap.add_argument("--param", action="append", default=[], metavar="K=V",
                    help="strategy attribute override (repeatable)")
    ap.add_argument("--cuts", type=int, default=24,
                    help="truncation cut points (half on decision bars)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--warmup-offsets", default="1,3", metavar="D,D",
                    help="later start dates, in day files after --start")
    ap.add_argument("--warmup-days", type=int, default=2)
    ap.add_argument("--skip", choices=("truncation", "warmup"), action="append",
                    default=[])
    ap.add_argument("--prop-threshold", type=float, default=0.0,
                    help="run with the prop-firm tracker on (trailing drawdown $); "
                         "needed for strategies that size from prop_headroom / "
                         "size_within_budget. Default off")
    ap.add_argument("--slippage", type=float, default=0.0)
    ap.add_argument("--data-root", default=None)
    a = ap.parse_args(argv)

    overrides = {}
    for kv in a.param:
        k, _, v = kv.partition("=")
        overrides[k] = _parse_value(v)
    factory = make_factory(a.strategy, overrides)
    kw = {"symbol": a.symbol, "period": a.period, "slippage_ticks": a.slippage,
          "data_root": a.data_root}
    kw = {k: v for k, v in kw.items() if v is not None}
    if a.prop_threshold:
        from backtester import PropFirmConfig
        kw["prop"] = PropFirmConfig(threshold=a.prop_threshold)

    print(f"causality check: {Path(a.strategy).name}  {a.start}..{a.end}")
    ok = True
    if "truncation" not in a.skip:
        rep = truncation_invariance(factory, a.start, a.end, a.cuts, a.seed,
                                    progress=lambda s: print(s, file=sys.stderr),
                                    **kw)
        print(format_truncation(rep))
        ok &= rep.passed
    if "warmup" not in a.skip:
        offs = tuple(int(x) for x in a.warmup_offsets.split(",") if x)
        wr = warmup_sensitivity(factory, a.start, a.end, offs, a.warmup_days, **kw)
        print(format_warmup(wr, a.warmup_days))
        ok &= wr.passed
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
