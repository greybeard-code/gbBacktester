"""Per-symbol, per-session slippage recommendations for backtesters that fill at last/bar prices
(NT8 Strategy Analyzer and similar), from the pooled 2026 spread histograms.

Such a backtester pays no spread, so slippage must stand in for it. Per side (entry, exit), in ticks:
  typical       ceil(mean spread / 2): the average half spread a market order crosses
  conservative  p90 of the spread: "the upper end of the typical bid/ask spread"
Both are trade-weighted over trades that had a valid quote (bid < ask).
Sessions are ET windows: RTH 09:30-16:00, reopen 18:00-20:00, overnight 20:00-09:30, all.
Writes out/slippage_recommendations_2026.csv and prints the table.
"""
from __future__ import annotations

import csv
import math
import sys
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).parent))
import spread_study as ss  # noqa: E402
import summarize as sm  # noqa: E402

OUT = ss.OUT
SESSIONS = {
    "RTH 09:30-16:00": set(range(19, 32)),
    "reopen 18:00-20:00": {36, 37, 38, 39},
    "overnight 20:00-09:30": set(range(40, 48)) | set(range(0, 19)),
    "all hours": set(range(48)),
}
SYMS = ["ES", "MES", "NQ", "MNQ", "YM", "MYM", "RTY", "M2K", "GC", "MGC", "SI", "CL", "MCL", "ZN"]


def main():
    t = pq.read_table(OUT / "spread_30m_by_day_2026.parquet",
                      columns=["symbol", "window_idx", "n_valid", "tr_sum_ticks", "tr_hist"])
    sym = t.column("symbol").to_numpy(zero_copy_only=False)
    win = t.column("window_idx").to_numpy(zero_copy_only=False)
    nv = t.column("n_valid").to_numpy(zero_copy_only=False)
    sm_ = t.column("tr_sum_ticks").to_numpy(zero_copy_only=False)
    hist = np.array(t.column("tr_hist").to_pylist(), dtype=np.int64)
    keep = nv >= ss.MIN_TRADES_POOL
    rows = []
    for s in SYMS:
        tv = ss.TICK[s] * ss.POINT_VALUE[s]
        for name, wins in SESSIONS.items():
            sel = keep & (sym == s) & np.isin(win, list(wins))
            n = int(nv[sel].sum())
            if n == 0:
                continue
            h = hist[sel].sum(axis=0)
            mean = sm_[sel].sum() / n
            p75, p90, p95 = (sm.pct_from_hist(h, q) for q in (0.75, 0.9, 0.95))
            typ = max(1, math.ceil(mean / 2))
            cons = int(p90)
            rows.append({"symbol": s, "session": name, "trades": n, "mean_spread_ticks": round(mean, 2),
                         "p75_ticks": int(p75), "p90_ticks": cons, "p95_ticks": int(p95),
                         "typical_slip_ticks_per_side": typ, "conservative_slip_ticks_per_side": cons,
                         "tick_value_usd": tv,
                         "typical_round_trip_usd": round(2 * typ * tv, 2),
                         "conservative_round_trip_usd": round(2 * cons * tv, 2)})
    with open(OUT / "slippage_recommendations_2026.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"{'sym':4} {'session':22} {'mean':>5} {'p75':>3} {'p90':>3} {'p95':>3} | typ cons | $RT typ  $RT cons")
    for r in rows:
        print(f"{r['symbol']:4} {r['session']:22} {r['mean_spread_ticks']:5.2f} {r['p75_ticks']:3d} {r['p90_ticks']:3d} "
              f"{r['p95_ticks']:3d} | {r['typical_slip_ticks_per_side']:3d} {r['conservative_slip_ticks_per_side']:4d} | "
              f"{r['typical_round_trip_usd']:8.2f} {r['conservative_round_trip_usd']:9.2f}")


if __name__ == "__main__":
    main()
