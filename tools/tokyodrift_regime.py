"""Regime diagnostics for strategies/gb_tokyo_drift.py.

Runs the strategy unfiltered, joins every Trade back to the regime features
recorded on its signal (via the "#<fire_idx>" suffix on entry_tag), and
tabulates P&L by bucket. HYPOTHESIS GENERATION ONLY — every bucket here is
read in-sample; a filter suggested by this table still has to earn its keep in
walk-forward (walkforward.py) before it means anything.

Each row also shows the 2025 / 2026 split (does the bucket's edge hold in both
halves, or is it one year?) and net with its best 5 trades removed (is it a
handful of outliers?).

Usage (repo root): python3 -m tools.tokyodrift_regime [buffer R]
"""
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from backtester import Backtest
from backtester.loader import load_strategy

ET = ZoneInfo("America/New_York")


def _row(label, trades):
    if not trades:
        return f"  {label:<26}{0:>5}"
    pnl = [t.pnl for t in trades]
    wins = sum(p for p in pnl if p > 0)
    loss = -sum(p for p in pnl if p < 0)
    pf = wins / loss if loss else float("inf")
    y25 = sum(t.pnl for t in trades if _year(t) == 2025)
    y26 = sum(t.pnl for t in trades if _year(t) == 2026)
    ex5 = sum(sorted(pnl)[:-5]) if len(pnl) > 5 else float("nan")
    wr = 100.0 * sum(1 for p in pnl if p > 0) / len(pnl)
    return (f"  {label:<26}{len(pnl):>5}{sum(pnl):>9,.0f}{pf:>6.2f}{wr:>6.1f}"
            f"{y25:>9,.0f}{y26:>9,.0f}{ex5:>10,.0f}")


def _year(t):
    return datetime.fromtimestamp(t.entry_ts / 1e9, ET).year


def main():
    ov = {}
    if len(sys.argv) == 3:
        ov = {"stop_buffer_ticks": int(sys.argv[1]), "target_r_multiple": float(sys.argv[2])}
    strat = load_strategy("strategies/gb_tokyo_drift.py", ov)
    res = Backtest(strat, start="2025-01-01", end="2026-08-07", prop=None,
                   progress=False).run()
    rows = []
    for t in res.trades:
        if "#" not in t.entry_tag:
            continue
        f = strat.fires[int(t.entry_tag.split("#")[1])]
        rows.append((t, f))
    print(f"gb_tokyo_drift {ov or 'shipped defaults'}: {len(rows)} trades joined "
          f"of {len(res.trades)}")
    hdr = (f"  {'bucket':<26}{'n':>5}{'net':>9}{'pf':>6}{'win%':>6}"
           f"{'2025':>9}{'2026':>9}{'ex-top5':>10}")

    def table(title, keyfn, order=None):
        groups = {}
        for t, f in rows:
            groups.setdefault(keyfn(t, f), []).append(t)
        print(f"\n{title}\n{hdr}")
        for k in (order or sorted(groups, key=str)):
            if k in groups:
                print(_row(str(k), groups[k]))

    def align(v, d):
        return "n/a (warmup)" if v != v else ("with" if v == d else "against")

    def bucket(v, edges, labels):
        if v != v:
            return "n/a (warmup)"
        for e, lab in zip(edges, labels):
            if v < e:
                return lab
        return labels[-1]

    table("ALL", lambda t, f: "all")
    table("Direction", lambda t, f: "long" if t.direction > 0 else "short")
    table("Level", lambda t, f: f["name"])
    table("Level x direction", lambda t, f: f"{f['name']} {'L' if t.direction > 0 else 'S'}")
    table("Daily trend (close vs SMA20 of session closes)",
          lambda t, f: align(f["dtrend"], t.direction))
    table("Daily trend x direction",
          lambda t, f: f"{align(f['dtrend'], t.direction)} {'L' if t.direction > 0 else 'S'}")
    table("5m EMA200 side", lambda t, f: align(f["ema_side"], t.direction))
    table("Overnight drift (close vs session open, signed by trade dir)",
          lambda t, f: "n/a" if f["night_move"] != f["night_move"] else
          ("with drift" if f["night_move"] * t.direction > 0 else "against drift"))
    table("Vol ratio (10 vs 40 session ranges)",
          lambda t, f: bucket(f["vol_ratio"], [0.8, 1.0, 1.2], ["<0.8", "0.8-1.0", "1.0-1.2", ">=1.2"]),
          ["<0.8", "0.8-1.0", "1.0-1.2", ">=1.2", "n/a (warmup)"])
    table("Daily efficiency ratio (20 sessions)",
          lambda t, f: bucket(f["er"], [0.15, 0.30], ["<0.15", "0.15-0.30", ">=0.30"]),
          ["<0.15", "0.15-0.30", ">=0.30", "n/a (warmup)"])
    table("5m ATR as % of price",
          lambda t, f: bucket(f["atr_pct"], [0.08, 0.12, 0.18], ["<0.08", "0.08-0.12", "0.12-0.18", ">=0.18"]),
          ["<0.08", "0.08-0.12", "0.12-0.18", ">=0.18"])


if __name__ == "__main__":
    main()
