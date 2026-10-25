"""Trend analysis of the 30-minute spread table: time of day, month, weekday.

Reads out/spread_30m_by_day_2026.parquet. Writes out/trends.html (self-contained plotly page,
derived from the tick data, so check its licence before sharing) and out/trend_stats.csv. Prints the numbers behind the page.

Measure: trade-weighted mean spread in ticks of each day-window (windows with < 30 valid trades
are dropped). Month and weekday cuts use a WINDOW-ADJUSTED index: each day-window's spread
divided by that symbol's pooled mean for the same ET window, averaged with trade-count weights.
A month that merely trades more of the wide 18:00 window therefore does not look wide.
"""
from __future__ import annotations

import csv
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import plotly.graph_objects as go
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).parent))
import spread_study as ss  # noqa: E402

OUT = ss.OUT
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]
CME_ORDER = list(range(36, 48)) + list(range(0, 36))          # 18:00 ET ... 17:30 ET
SYM_ORDER = ["ES", "MES", "NQ", "MNQ", "YM", "MYM", "RTY", "M2K", "GC", "MGC", "SI", "CL", "MCL", "ZN"]


def label(w: int) -> str:
    return f"{w // 2:02d}:{30 * (w % 2):02d}"


def load():
    t = pq.read_table(OUT / "spread_30m_by_day_2026.parquet",
                      columns=["symbol", "date", "trading_day", "window_idx", "n_valid",
                               "tr_sum_ticks", "range_ticks", "n_trades", "roll_day"])
    d = {n: t.column(n).to_numpy(zero_copy_only=False) for n in t.column_names}
    keep = d["n_valid"] >= ss.MIN_TRADES_POOL
    d = {k: v[keep] for k, v in d.items()}
    d["mean"] = d["tr_sum_ticks"] / d["n_valid"]
    d["month"] = np.array([int(s[4:6]) for s in d["date"]])
    d["wday"] = np.array([dt.datetime.strptime(s, "%Y%m%d").weekday() for s in d["trading_day"]])
    return d


def wmean(values, weights, codes, n):
    num = np.bincount(codes, weights=values * weights, minlength=n)
    den = np.bincount(codes, weights=weights, minlength=n)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 0, num / den, np.nan)


def ranks(x):
    r = np.empty(len(x))
    r[np.argsort(x, kind="stable")] = np.arange(len(x))
    return r


def corr(a, b):
    ok = np.isfinite(a) & np.isfinite(b)
    return float(np.corrcoef(a[ok], b[ok])[0, 1]) if ok.sum() > 2 else float("nan")


def adjusted(d, sel):
    """Window-adjusted spread and range indices for one symbol's rows."""
    w = d["window_idx"][sel]
    wt = d["n_valid"][sel].astype(float)
    pooled = wmean(d["mean"][sel], wt, w, 48)                  # symbol-window pooled mean spread
    rng_pooled = wmean(d["range_ticks"][sel].astype(float), wt, w, 48)
    return d["mean"][sel] / pooled[w], d["range_ticks"][sel] / np.where(rng_pooled[w] > 0, rng_pooled[w], np.nan), wt


def heat(z_ratio, x, y, text=None, hover=None, title="", height=None):
    z = np.log2(np.clip(z_ratio, 0.25, 4))
    fig = go.Figure(go.Heatmap(
        z=z, x=x, y=y, zmin=-2, zmax=2, zmid=0, colorscale="RdBu_r", text=text,
        texttemplate="%{text}" if text is not None else None, customdata=hover,
        hovertemplate="%{y} %{x}<br>%{customdata}<extra></extra>" if hover is not None else None,
        colorbar=dict(title="vs own mean", tickvals=[-2, -1, 0, 1, 2], ticktext=["x0.25", "x0.5", "x1", "x2", "x4"])))
    fig.update_layout(title=title, template="plotly_white", height=height or 120 + 26 * len(y),
                      margin=dict(l=60, r=20, t=60, b=60), xaxis=dict(type="category", tickangle=-60),
                      yaxis=dict(autorange="reversed", type="category"))
    return fig


def main():
    d = load()
    syms = [s for s in SYM_ORDER if s in set(d["symbol"].tolist())]
    stats = []
    figs = []

    # ---- A. time of day: symbol x ET window, ratio to the symbol's own pooled mean
    wins_present = [w for w in CME_ORDER if (d["window_idx"] == w).any()]
    z = np.full((len(syms), len(wins_present)), np.nan)
    hov = np.empty(z.shape, dtype=object)
    overall = {}
    for i, s in enumerate(syms):
        sel = np.flatnonzero(d["symbol"] == s)
        overall[s] = d["tr_sum_ticks"][sel].sum() / d["n_valid"][sel].sum()
        m = wmean(d["mean"][sel], d["n_valid"][sel].astype(float), d["window_idx"][sel], 48)
        tv = ss.TICK[s] * ss.POINT_VALUE[s]
        for j, w in enumerate(wins_present):
            z[i, j] = m[w] / overall[s]
            hov[i, j] = f"{m[w]:.2f} ticks (${m[w] * tv:.2f}) = x{m[w] / overall[s]:.2f} of {s} mean {overall[s]:.2f}"
    figs.append(("Time of day (ET, CME day order from the 18:00 reopen)",
                 "Colour = spread vs that symbol's own all-year mean. Red = wider than usual.",
                 heat(z, [label(w) for w in wins_present], syms, hover=hov, title="Mean spread by 30-minute ET window")))

    # ---- B/C. month and weekday (window-adjusted), plus volatility link
    zb = np.full((len(syms), 10), np.nan)
    zc = np.full((len(syms), 5), np.nan)
    vb = np.full((len(syms), 10), np.nan)
    tb = np.empty(zb.shape, dtype=object)
    tc = np.empty(zc.shape, dtype=object)
    for i, s in enumerate(syms):
        sel = np.flatnonzero(d["symbol"] == s)
        adj, radj, wt = adjusted(d, sel)
        mo = d["month"][sel] - 1
        wd = d["wday"][sel]
        okm = np.isfinite(adj)
        mi = wmean(np.nan_to_num(adj), wt * okm, mo, 12)
        vi = wmean(np.nan_to_num(radj, nan=0.0), wt * np.isfinite(radj), mo, 12)
        wi = wmean(np.nan_to_num(adj), wt * okm, np.minimum(wd, 6), 7)
        for j in range(10):
            zb[i, j], vb[i, j] = mi[j], vi[j]
            tb[i, j] = "" if np.isnan(mi[j]) else f"{mi[j]:.2f}"
        for j in range(5):
            zc[i, j] = wi[j]
            tc[i, j] = "" if np.isnan(wi[j]) else f"{wi[j]:.2f}"
        # statistics
        mm = np.arange(10)
        ok = np.isfinite(mi[:10])
        lr = np.log(adj[np.isfinite(adj) & np.isfinite(radj) & (radj > 0)])
        lv = np.log(radj[np.isfinite(adj) & np.isfinite(radj) & (radj > 0)])
        # within-window share of variance explained by time of day
        w = d["window_idx"][sel]
        ssw = np.nansum((d["mean"][sel] - wmean(d["mean"][sel], np.ones(len(sel)), w, 48)[w]) ** 2)
        sst = np.nansum((d["mean"][sel] - np.nanmean(d["mean"][sel])) ** 2)
        stats.append({
            "symbol": s, "overall_mean_ticks": round(overall[s], 3),
            "tod_r2": round(1 - ssw / sst, 3),
            "month_rank_corr": round(corr(ranks(mi[:10][ok]), ranks(mm[ok].astype(float))), 2),
            "month_index_first": round(mi[0], 3), "month_index_last": round(mi[9], 3),
            "month_index_min": round(np.nanmin(mi[:10]), 3), "month_index_max": round(np.nanmax(mi[:10]), 3),
            "month_vs_vol_corr": round(corr(mi[:10], vi[:10]), 2),
            "daywin_spread_vs_range_corr_log": round(corr(lr, lv), 2),
            "weekday_index_spread": round(float(np.nanmax(wi[:5]) - np.nanmin(wi[:5])), 3),
        })
    figs.append(("Month (window-adjusted)",
                 "Each cell: spread index vs the same ET windows over the whole year (1.00 = normal). Removes the effect of trading more of the wide windows.",
                 heat(zb, MONTHS[:10], syms, text=tb, title="Spread index by month")))
    figs.append(("Weekday of CME trading day (window-adjusted)",
                 "Monday's trading day starts with the Sunday 18:00 ET reopen.",
                 heat(zc, WEEKDAYS, syms, text=tc, title="Spread index by weekday")))

    # ---- D. per-symbol month x window and weekday x window, with a symbol dropdown
    for kind in ("month", "weekday"):
        fig = go.Figure()
        ncol = 10 if kind == "month" else 5
        xs = MONTHS[:10] if kind == "month" else WEEKDAYS
        for k, s in enumerate(syms):
            sel = np.flatnonzero(d["symbol"] == s)
            col = (d["month"][sel] - 1) if kind == "month" else np.minimum(d["wday"][sel], 4)
            w = d["window_idx"][sel]
            zz = np.full((len(wins_present), ncol), np.nan)
            m = wmean(d["mean"][sel], d["n_valid"][sel].astype(float), w, 48)
            for r, win in enumerate(wins_present):
                msk = w == win
                cm = wmean(d["mean"][sel][msk], d["n_valid"][sel][msk].astype(float), col[msk], ncol)
                zz[r, :] = cm[:ncol] / m[win]
            zlog = np.log2(np.clip(zz, 0.25, 4))
            fig.add_trace(go.Heatmap(z=zlog, x=xs, y=[label(w) for w in wins_present], zmin=-2, zmax=2,
                                     zmid=0, colorscale="RdBu_r", visible=(k == 0), showscale=(k == 0),
                                     customdata=zz, hovertemplate="%{y} %{x}<br>x%{customdata:.2f} of this window's mean<extra></extra>",
                                     colorbar=dict(title="vs window mean", tickvals=[-2, -1, 0, 1, 2],
                                                   ticktext=["x0.25", "x0.5", "x1", "x2", "x4"])))
        buttons = [dict(label=s, method="update",
                        args=[{"visible": [j == k for j in range(len(syms))]}]) for k, s in enumerate(syms)]
        fig.update_layout(updatemenus=[dict(buttons=buttons, x=0.0, y=1.15, xanchor="left")],
                          template="plotly_white", height=900, margin=dict(l=60, r=20, t=90, b=40),
                          title=f"{kind.capitalize()} x ET window, ratio to the same window's all-year mean (pick a symbol)",
                          yaxis=dict(autorange="reversed", type="category"), xaxis=dict(type="category"))
        figs.append((f"{kind.capitalize()} by time of day", "Pick a symbol. Each cell is compared with the same ET window averaged over the year.", fig))

    # ---- write page + stats
    parts = ['<!doctype html><html><head><meta charset="utf-8"><title>Spread trends 2026</title>'
             '<style>body{font:15px system-ui;margin:24px;max-width:1500px}h2{margin:36px 0 4px}p{color:#555;margin:2px 0 10px}</style>'
             '</head><body><h1>Bid/ask spread trends, 2026 (trade-weighted, ticks)</h1>'
             '<p>Derived from tick data you may not be allowed to redistribute: check before sharing this page.</p>']
    for i, (h, note, fig) in enumerate(figs):
        parts.append(f"<h2>{h}</h2><p>{note}</p>")
        parts.append(fig.to_html(full_html=False, include_plotlyjs="inline" if i == 0 else False))
    parts.append("</body></html>")
    (OUT / "trends.html").write_text("\n".join(parts))
    with open(OUT / "trend_stats.csv", "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(stats[0]))
        wr.writeheader()
        wr.writerows(stats)
    print(f"wrote {OUT / 'trends.html'} and trend_stats.csv")
    for r in stats:
        print(r)
    print("\nmonth index (window-adjusted), Jan..Oct")
    for i, s in enumerate(syms):
        print(f"{s:4}", " ".join(f"{v:5.2f}" for v in zb[i]))
    print("\nweekday index Mon..Fri")
    for i, s in enumerate(syms):
        print(f"{s:4}", " ".join(f"{v:5.2f}" for v in zc[i]))
    print("\nvolatility (range) index by month, Jan..Oct")
    for i, s in enumerate(syms):
        print(f"{s:4}", " ".join(f"{v:5.2f}" for v in vb[i]))


if __name__ == "__main__":
    main()
