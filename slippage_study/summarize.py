"""Pool the per-day window table into the summaries used for trend work.

Reads out/days/*/*.parquet (written by spread_study.py run). Writes into out/:
  spread_30m_by_day_2026.parquet   the full long table (symbol x day x 30-min ET window)
  spread_30m_summary_2026.csv      symbol x ET window, pooled over all days
  spread_30m_by_month_2026.csv     symbol x month x ET window (trend cuts)
  coverage_2026.csv                days / data-quality per symbol

Every percentile comes from POOLED exact tick histograms, never from averaged per-day values.
Trade-weighted columns use trades with a valid quote (bid < ask); time-weighted columns use
quote time with a valid quote, only on days/windows with standalone quote updates.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).parent))
import spread_study as ss  # noqa: E402

OUT = ss.OUT
PCTS = (0.5, 0.75, 0.9, 0.95)


def pct_from_hist(h: np.ndarray, q: float) -> float:
    """Smallest spread (ticks) with cumulative share >= q; NaN for an empty histogram."""
    tot = h.sum()
    if tot == 0:
        return float("nan")
    return float(np.searchsorted(np.cumsum(h), q * tot, side="left"))


def load() -> dict[str, np.ndarray]:
    parts = sorted((OUT / "days").glob("*/*.parquet"))
    tables = [pq.read_table(p) for p in parts]
    empty = [p for p, t in zip(parts, tables) if t.num_rows == 0]
    if empty:                       # a day file with no events at all (holiday stub)
        print(f"{len(empty)} empty day files skipped, e.g. {empty[0].parent.name}/{empty[0].stem}")
    tbl = pa.concat_tables([t for t in tables if t.num_rows])
    pq.write_table(tbl, OUT / "spread_30m_by_day_2026.parquet")
    d = {}
    for name in tbl.column_names:
        col = tbl.column(name)
        if name.endswith("_hist"):
            d[name] = np.array(col.to_pylist(), dtype=np.int64)
        else:
            d[name] = col.to_numpy(zero_copy_only=False)
    print(f"{len(parts)} day files, {tbl.num_rows} rows")
    return d


def pooled_row(d, sel: np.ndarray, sym: str) -> dict:
    tick = ss.TICK[sym]
    tv = tick * ss.POINT_VALUE[sym]            # dollars per tick per contract
    # drop thin windows (per day-window) from the pooled means/percentiles
    sel = sel[d["n_valid"][sel] >= ss.MIN_TRADES_POOL]
    n_valid = d["n_valid"][sel].sum()
    trh = d["tr_hist"][sel].sum(axis=0)
    # time-weighted: only rows that actually carry quote time
    tsel = sel[d["tw_ns"][sel] > 0]
    twh = d["tw_hist"][tsel].sum(axis=0)
    tw_ns = d["tw_ns"][tsel].sum()
    row = {"n_day_windows": len(sel), "n_days_tw": len(tsel), "n_trades_valid": int(n_valid),
           "trades_per_window": round(n_valid / max(1, len(sel)), 1),
           "volume_per_window": round(d["volume"][sel].sum() / max(1, len(sel)), 1),
           "range_ticks_mean": round(d["range_ticks"][sel].mean(), 2) if len(sel) else float("nan")}
    mean_tr = d["tr_sum_ticks"][sel].sum() / n_valid if n_valid else float("nan")
    mean_tw = d["tw_sum_tick_ns"][tsel].sum() / tw_ns if tw_ns else float("nan")
    row["tr_mean_ticks"] = round(mean_tr, 4)
    for q in PCTS:
        row[f"tr_p{int(q * 100)}_ticks"] = pct_from_hist(trh, q)
    row["tr_max_ticks"] = int(d["tr_max"][sel].max()) if len(sel) else 0
    row["tr_pct_1tick"] = round(100 * trh[1] / n_valid, 2) if n_valid else float("nan")
    row["tw_mean_ticks"] = round(mean_tw, 4)
    for q in PCTS:
        row[f"tw_p{int(q * 100)}_ticks"] = pct_from_hist(twh, q)
    row["tw_pct_1tick"] = round(100 * twh[1] / tw_ns, 2) if tw_ns else float("nan")
    row["tr_mean_usd"] = round(mean_tr * tv, 3)          # full spread, dollars per contract
    row["tr_p90_usd"] = round(row["tr_p90_ticks"] * tv, 3) if n_valid else float("nan")
    row["tr_half_spread_usd"] = round(mean_tr * tv / 2, 3)   # cost per side of crossing the touch
    row["pct_locked_crossed_noquote"] = round(
        100 * (d["n_locked"][sel].sum() + d["n_crossed"][sel].sum() + d["n_noquote"][sel].sum())
        / max(1, d["n_trades"][sel].sum()), 4)
    return row


def write_csv(path: Path, rows: list[dict]):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {path} ({len(rows)} rows)")


def main():
    d = load()
    syms = sorted(set(d["symbol"].tolist()))
    month = np.array([s[:6] for s in d["date"]])
    widx = d["window_idx"]
    wet = d["window_et"]
    summary, bymonth = [], []
    for sym in syms:
        s_sel = np.flatnonzero(d["symbol"] == sym)
        for w in range(48):
            sel = s_sel[widx[s_sel] == w]
            if len(sel) == 0:
                continue
            r = pooled_row(d, sel, sym)
            if r["n_day_windows"] == 0:
                continue
            summary.append({"symbol": sym, "window_et": wet[sel[0]], **r})
            for m in sorted(set(month[sel].tolist())):
                msel = sel[month[sel] == m]
                rm = pooled_row(d, msel, sym)
                if rm["n_day_windows"]:
                    bymonth.append({"symbol": sym, "month": m, "window_et": wet[sel[0]], **rm})
    write_csv(OUT / "spread_30m_summary_2026.csv", summary)
    write_csv(OUT / "spread_30m_by_month_2026.csv", bymonth)

    cov = []
    for sym in syms:
        s = np.flatnonzero(d["symbol"] == sym)
        dates = sorted(set(d["date"][s].tolist()))
        day_flag = lambda col: sorted({d["date"][i] for i in s if d[col][i]})   # noqa: E731
        thin = int((d["n_valid"][s] < ss.MIN_TRADES_POOL).sum())
        tw_ns = d["tw_ns"][s].sum()
        cov.append({"symbol": sym, "first_day": dates[0], "last_day": dates[-1], "days": len(dates),
                    "databento_days": len(day_flag("databento_day")),
                    "spliced_days": len(day_flag("spliced")),
                    "roll_days": len(day_flag("roll_day")),
                    "day_windows": len(s), "thin_day_windows_lt30": thin,
                    "trades": int(d["n_trades"][s].sum()),
                    "pct_trades_no_quote": round(100 * d["n_noquote"][s].sum() / max(1, d["n_trades"][s].sum()), 4),
                    "pct_trades_locked": round(100 * d["n_locked"][s].sum() / max(1, d["n_trades"][s].sum()), 4),
                    "pct_trades_crossed": round(100 * d["n_crossed"][s].sum() / max(1, d["n_trades"][s].sum()), 4),
                    "tw_hours": round(tw_ns / 3.6e12, 1),
                    "tw_pct_stale_dropped": round(100 * d["tw_ns_stale"][s].sum() / max(1, tw_ns + d["tw_ns_stale"][s].sum()), 3),
                    "tw_pct_locked_crossed": round(100 * d["tw_ns_locked_crossed"][s].sum() / max(1, tw_ns + d["tw_ns_locked_crossed"][s].sum()), 3)})
    write_csv(OUT / "coverage_2026.csv", cov)


if __name__ == "__main__":
    main()
