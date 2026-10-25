"""Random-direction control: how much of the P&L is the signal, how much is structure?

A strategy is paid for two different things: knowing WHICH WAY to trade, and
trading at all inside a particular structure (entry timing, bracket geometry,
session window, costs, the market's own drift). Keep everything and replace
only the direction of each new entry with a coin flip. If the strategy keeps
most of its P&L under random direction, the P&L is the structure. Whatever
sits above the coin-flip distribution is what the signal is answerable for.

This is optd-starter's drift control (backtests/drift.py) rebuilt for a
tick-level engine. optd's control holds the same bars and exits at the close
with a market order, so against a strategy that exits on a resting bracket the
exit-mechanism gap alone can read as edge. Here the exit mechanism is held
FIXED - the strategy's own brackets, stops and session flatten all still run -
and only the direction changes. What carries over from optd is the scoring:
the gap is judged against its own noise floor, and a gap you cannot separate
from noise is INCONCLUSIVE, not a win. That one rule disqualifies more
strategies than the controls do.

Scoring pairs the two legs by DAY (both runs cover the same calendar, so the
market's variance cancels): d[day] = strategy P&L - mean over seeds of control
P&L, t = mean(d) / (sd(d) / sqrt(days)). Also reported: where the strategy
sits against the spread of control NETS (z, and how many controls matched or
beat it), and the share of the headline that survives randomised direction.

Two controls - they answer different questions, pick on purpose.

  --direction-method NAME   (the clean one, needs the strategy's cooperation)
      Name the strategy method that takes the trade direction as its first
      argument (a `_go(direction, bar)` entry method). Each call gets
      random.choice((1, -1)) instead of the signal's direction, so the
      strategy's own state follows the coin: its stops, reversals and exits
      run for the randomised position exactly as designed. This is "random
      direction, everything else the strategy does unchanged" and is the
      control the 2026-10-16 bias check used. It reproduced that audit's
      figures when this tool was built (see below).

  default (no method named)   (works on any strategy, LENIENT to the strategy)
      Every entry goes through Strategy._enter (buy, sell, buy_bracket,
      sell_bracket). When the strategy is flat, or has just issued a close that
      will flatten it (the SAR "close then re-enter" shape), the side is
      replaced by a fair coin; bracket ticks are relative to the fill, so
      geometry is preserved. The strategy still believes its OWN signal, so
      anything it times off that signal (a signal-flip exit, a reversal) hits
      the coin-flipped position at the TRUE signal's moment: a flipped trade
      that exits on a signal flip is the original trade mirrored. That makes
      this control keep LESS of the headline than the method control (on a
      stop-and-reverse strategy, 27% against 46%), so it will flatter a strategy's
      t-statistic. Read it ONE way only: a FAIL or INCONCLUSIVE from this control
      is decisive (the strategy could not even beat the lenient bar); a PASS
      is not, and must be confirmed with --direction-method.
      Reported honestly:
        - entries placed while a position is open (scale-ins, reversals by
          plain buy/sell) are NOT randomised and are counted;
        - buy_limit / sell_limit / buy_stop / sell_stop and hand-built Orders
          bypass _enter. Their price encodes a direction, so they cannot be
          flipped meaningfully and are counted as bypassed.
      If either count is large the control is not a clean test of direction
      for that strategy.

Usage (repo root):

    python3 -m tools.random_control strategies/terminator_rec.py \
        --start 20241216 --end 20260703 --seeds 8 --jobs 4 \
        --prop-threshold 2000
"""
from __future__ import annotations

import argparse
import random
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtester import Backtest, PropFirmConfig, manifest, metrics  # noqa: E402
from backtester.loader import load_strategy_class  # noqa: E402
from backtester.orders import OrderState, OrderType  # noqa: E402

MIN_CONTROLS = 5


# ----------------------------------------------------------------------------
# the control
# ----------------------------------------------------------------------------

class DirectionRandomizer:
    """Replace the side of each new entry with a coin flip (probability
    `p_flip` of reversing it - 0.5 makes the direction independent of the
    signal, 0 is a no-op used to prove the wrapper is behaviour-neutral)."""

    def __init__(self, seed: int, p_flip: float = 0.5):
        self.rng = random.Random(seed)
        self.p_flip = p_flip
        self.entries = 0            # entries seen by _enter
        self.flipped = 0
        self.not_flat = 0           # entries while a position stays open
        self.bypassed = 0           # non-exit orders that skipped _enter
        self.method = None          # name of the wrapped direction method, if any
        self._in_enter = False

    def attach_method(self, strat, name: str) -> "DirectionRandomizer":
        """Randomise the first positional argument (the direction) of
        strat.<name>(direction, ...). The strategy's own state then follows the
        coin. Uses rng.choice((1, -1)) per call, i.e. direction independent of
        the signal; p_flip == 0 leaves the method untouched."""
        if not callable(getattr(strat, name, None)):
            raise AttributeError(f"{type(strat).__name__} has no method {name!r}")
        orig = getattr(strat, name)
        self.method = name

        def wrapped(direction, *a, **k):
            self.entries += 1
            d = self.rng.choice((1, -1)) if self.p_flip else direction
            if d != direction:
                self.flipped += 1
            return orig(d, *a, **k)
        setattr(strat, name, wrapped)
        return self

    def attach(self, strat, broker) -> "DirectionRandomizer":
        orig_enter = strat._enter
        orig_submit = broker.submit

        def effectively_flat() -> bool:
            pos = strat._account.position
            if pos == 0:
                return True
            cover = sum(o.qty for o in broker.working
                        if o.is_exit and o.type is OrderType.MARKET
                        and o.state is OrderState.WORKING
                        and o.side == (-1 if pos > 0 else 1))
            return cover >= abs(pos)

        def enter(side, qty, tag, bracket=None):
            self.entries += 1
            flip = False
            if effectively_flat():
                # one draw per flat entry, so the random stream does not
                # depend on whether the news filter then blocks the order
                flip = self.rng.random() < self.p_flip
            else:
                self.not_flat += 1
            self._in_enter = True
            try:
                order = orig_enter(-side if flip else side, qty, tag, bracket)
            finally:
                self._in_enter = False
            if flip and order is not None:
                self.flipped += 1
            return order

        def submit(order, bracket=None):
            if not order.is_exit and not self._in_enter:
                self.bypassed += 1
            return orig_submit(order, bracket)

        strat._enter = enter
        broker.submit = submit
        return self

    def counts(self) -> dict:
        return {"entries": self.entries, "flipped": self.flipped,
                "not_flat": self.not_flat, "bypassed": self.bypassed}


# ----------------------------------------------------------------------------
# scoring
# ----------------------------------------------------------------------------

def compare_to_control(base_daily: dict, control_dailies: list[dict],
                       base_net: float | None = None,
                       control_nets: list[float] | None = None) -> dict:
    """Score a strategy against its own random-direction controls.

    `*_daily` map day -> P&L ($). Nets default to the daily sums; pass the
    metrics' net_pnl to keep the headline identical to the run's own report.
    """
    k = len(control_dailies)
    if base_net is None:
        base_net = float(sum(base_daily.values()))
    if control_nets is None:
        control_nets = [float(sum(d.values())) for d in control_dailies]
    cn = np.asarray(control_nets, dtype=float)
    out = {"n_controls": k, "base_net": float(base_net),
           "control_mean_net": float(cn.mean()) if k else 0.0,
           "control_sd_net": float(cn.std(ddof=1)) if k > 1 else 0.0}
    if k < MIN_CONTROLS:
        out.update(verdict=f"TOO FEW CONTROLS - {k} seeds; the noise floor needs "
                           f"at least {MIN_CONTROLS}.",
                   t=0.0, edge_per_day=0.0, edge_se=0.0, days=0, z=0.0,
                   beaten_by=0, rank_p=1.0, share_kept=float("nan"))
        return out

    days = sorted(set(base_daily).intersection(*[set(d) for d in control_dailies]))
    if len(days) < 2:
        raise ValueError("base and control runs share fewer than 2 trading days")
    b = np.array([base_daily[d] for d in days], dtype=float)
    c = np.mean([[d[x] for x in days] for d in control_dailies], axis=0)
    diff = b - c
    edge = float(diff.mean())
    se = float(diff.std(ddof=1) / np.sqrt(len(diff)))
    t = edge / se if se > 0 else 0.0

    sd = out["control_sd_net"]
    z = (base_net - out["control_mean_net"]) / sd if sd > 0 else 0.0
    beaten_by = int((cn >= base_net).sum())
    share = (out["control_mean_net"] / base_net) if base_net > 0 else float("nan")
    out.update(t=t, edge_per_day=edge, edge_se=se, days=len(days), z=z,
               beaten_by=beaten_by, rank_p=(1 + beaten_by) / (1 + k),
               share_kept=share)

    # Order matters (as in optd): an edge you cannot separate from noise is
    # not an edge yet, so that check comes first.
    if abs(t) < 2.0:
        v = (f"INCONCLUSIVE - the gap is {edge:+,.2f}/day but the noise floor is "
             f"+/-{se:,.2f} (t={t:.2f}). You cannot tell this apart from a coin "
             "flip. More days, or no claim.")
    elif edge <= 0:
        v = (f"FAIL - random direction over the same entries, exits and costs "
             f"does {-edge:,.2f}/day better (t={t:.2f}). The signal is "
             "decoration.")
    elif base_net <= 0:
        v = ("LOSING - it beats its own control and still loses money. There "
             "is nothing here to own.")
    elif out["control_mean_net"] > 0 and share >= 0.5:
        v = (f"WEAK - {share:.0%} of the headline survives random direction "
             f"(t={t:.2f}). Most of what it makes is the structure it trades "
             "inside, not the signal. Size it accordingly.")
    else:
        v = (f"PASS - the signal beats its own coin-flip twin (t={t:.2f}); "
             f"{share:.0%} of the headline survives random direction.")
    out["verdict"] = v
    return out


# ----------------------------------------------------------------------------
# running
# ----------------------------------------------------------------------------

def _build(path: str, overrides: dict, bt_kwargs: dict, prop_threshold):
    cls = load_strategy_class(path)
    strat = cls()
    for k, v in overrides.items():
        if not hasattr(strat, k):
            raise AttributeError(f"{cls.__name__} has no parameter {k!r}")
        setattr(strat, k, v)
    prop = PropFirmConfig(threshold=prop_threshold) if prop_threshold else None
    return Backtest(strat, prop=prop, progress=False, **bt_kwargs)


def run_one(payload: dict) -> dict:
    """seed None = as shipped. Module-level so a process pool can pickle it."""
    bt = _build(payload["strategy"], payload["overrides"], payload["bt_kwargs"],
                payload["prop_threshold"])
    rz = None
    if payload["seed"] is not None:
        rz = DirectionRandomizer(payload["seed"], payload.get("p_flip", 0.5))
        if payload.get("direction_method"):
            rz.attach_method(bt.strategy, payload["direction_method"])
        else:
            rz.attach(bt.strategy, bt.broker)
    # a control is LOGGED (ledger kind "random_control") but is not a try at a
    # strategy variant, so it does not count toward the trial number
    rec = manifest.begin_run(bt, kind="random_control",
                             extra={"seed": payload["seed"],
                                    "mode": payload.get("direction_method") or "side"})
    res = bt.run()
    m = metrics.compute(res)
    mm = manifest.finish_run(rec, res, m, write_manifest=False)
    return {"seed": payload["seed"], "net": m["net_pnl"], "sharpe": m["sharpe"],
            "trades": m["total_trades"], "pf": m["profit_factor"],
            "daily": res.daily_pnl, "counts": rz.counts() if rz else None,
            "inputs": rec.inputs if rec else {}, "seq": mm["seq"] if mm else None,
            "trades_sha": manifest.trades_sha(res.trades)}


def run_control(payload: dict, seeds: list[int], jobs: int = 1,
                progress=None) -> dict:
    jobs_list = [dict(payload, seed=None)] + [dict(payload, seed=s) for s in seeds]
    if jobs > 1:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            rows = list(ex.map(run_one, jobs_list))
    else:
        rows = []
        for j in jobs_list:
            rows.append(run_one(j))
            if progress:
                progress(rows[-1])
    base, ctrl = rows[0], rows[1:]
    score = compare_to_control(base["daily"], [r["daily"] for r in ctrl],
                               base["net"], [r["net"] for r in ctrl])
    return {"base": base, "controls": ctrl, "score": score,
            "direction_method": payload.get("direction_method")}


def format_report(res: dict) -> str:
    b, ctrl, s = res["base"], res["controls"], res["score"]
    lines = [f"{'as shipped':<26} net ${b['net']:>11,.2f}  trades {b['trades']:>5}  "
             f"PF {b['pf']:.2f}  Sharpe {b['sharpe']:>5.2f}"]
    for r in ctrl:
        lines.append(f"{'random direction, seed ' + str(r['seed']):<26} "
                     f"net ${r['net']:>11,.2f}  trades {r['trades']:>5}  "
                     f"PF {r['pf']:.2f}  Sharpe {r['sharpe']:>5.2f}")
    lines.append("")
    lines.append(f"control nets: mean ${s['control_mean_net']:,.2f}, "
                 f"sd ${s['control_sd_net']:,.2f} over {s['n_controls']} seeds")
    if s["days"]:
        lines.append(f"as shipped sits {s['z']:+.1f} sd from the control mean; "
                     f"{s['beaten_by']}/{s['n_controls']} controls matched or "
                     f"beat it (rank p = {s['rank_p']:.3f})")
        lines.append(f"paired by day over {s['days']} days: signal adds "
                     f"{s['edge_per_day']:+,.2f}/day +/- {s['edge_se']:,.2f} "
                     f"(t = {s['t']:.2f})")
        if s["share_kept"] == s["share_kept"]:
            lines.append(f"{s['share_kept']:.0%} of the headline survives random "
                         "direction")
    cs = [r["counts"] for r in ctrl if r["counts"]]
    if cs and res.get("direction_method"):
        tot = {k: sum(c[k] for c in cs) for k in cs[0]}
        lines.append(f"randomiser: {res['direction_method']}() called {tot['entries']:,} "
                     f"times across seeds, direction changed on {tot['flipped']:,}")
    elif cs:
        tot = {k: sum(c[k] for c in cs) for k in cs[0]}
        lines.append("mode: side flip at Strategy._enter - the strategy still times "
                     "its exits off its true signal, so the control keeps LESS of "
                     "the headline than --direction-method and flatters the "
                     "strategy. A FAIL/INCONCLUSIVE here stands; a PASS must be "
                     "confirmed with --direction-method (see module docstring)")
        lines.append(f"randomiser: {tot['flipped']:,} of {tot['entries']:,} entries "
                     f"flipped across seeds; {tot['not_flat']:,} entries placed "
                     f"with a position open (not randomised); {tot['bypassed']:,} "
                     "limit/stop/direct orders bypassed it")
        if tot["entries"] and (tot["not_flat"] + tot["bypassed"]) > 0.05 * tot["entries"]:
            lines.append("  WARNING: >5% of entries were not randomised; the control "
                         "is not a clean test of direction for this strategy.")
    lines.append("")
    lines.append(s["verdict"])
    return "\n".join(lines)


def _parse_value(s: str):
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    for cast in (int, float):
        try:
            return cast(s)
        except ValueError:
            pass
    return s


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("strategy")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--symbol")
    ap.add_argument("--period")
    ap.add_argument("--param", action="append", default=[], metavar="K=V")
    ap.add_argument("--direction-method", metavar="NAME",
                    help="strategy method whose first argument is the trade "
                         "direction (e.g. _go): randomise that instead of the "
                         "entry side. The clean control; see the docstring")
    ap.add_argument("--seeds", type=int, default=8,
                    help=f"control runs (>= {MIN_CONTROLS} for a verdict)")
    ap.add_argument("--seed0", type=int, default=1, help="first control seed")
    ap.add_argument("--jobs", type=int, default=1, help="parallel runs")
    ap.add_argument("--prop-threshold", type=float, default=0.0,
                    help="prop-firm trailing drawdown; 0 = off")
    ap.add_argument("--slippage", type=float, default=0.0)
    ap.add_argument("--balance", type=float, default=50_000.0)
    ap.add_argument("--data-root")
    a = ap.parse_args(argv)

    bt_kwargs = {"start": a.start, "end": a.end, "symbol": a.symbol,
                 "period": a.period, "slippage_ticks": a.slippage,
                 "start_balance": a.balance, "data_root": a.data_root}
    payload = {"strategy": str(Path(a.strategy).resolve()),
               "overrides": {k: _parse_value(v) for k, _, v in
                             (kv.partition("=") for kv in a.param)},
               "bt_kwargs": {k: v for k, v in bt_kwargs.items() if v is not None},
               "prop_threshold": a.prop_threshold or None,
               "direction_method": a.direction_method}
    seeds = list(range(a.seed0, a.seed0 + a.seeds))
    print(f"random-direction control: {Path(a.strategy).name}, {len(seeds)} seeds",
          file=sys.stderr)
    res = run_control(payload, seeds, a.jobs,
                      progress=lambda r: print(
                          f"  done {'base' if r['seed'] is None else 'seed ' + str(r['seed'])}"
                          f": net ${r['net']:,.2f}", file=sys.stderr))
    print(format_report(res))
    out = manifest.write_study_manifest(
        None, "random_control",
        {k: payload[k] for k in ("strategy", "overrides", "bt_kwargs",
                                 "prop_threshold", "direction_method")
         if k in payload} | {"seeds": seeds},
        res["base"]["inputs"],
        [{"seed": r["seed"], "net_pnl": r["net"], "trades": r["trades"],
          "sharpe": r["sharpe"], "ledger_seq": r["seq"], "trades_sha": r["trades_sha"]}
         for r in [res["base"], *res["controls"]]])
    if out:
        print(f"Manifest: {out.resolve()}")
    # scriptable like causality_check: 1 = the signal did not beat its control
    word = res["score"]["verdict"].split(" ")[0]
    return 1 if word in ("FAIL", "LOSING") else (2 if word == "TOO" else 0)


if __name__ == "__main__":
    sys.exit(main())
