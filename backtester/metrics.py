"""Performance metrics from a backtest Result."""
from __future__ import annotations

import numpy as np

TRADING_DAYS = 252


def compute(result) -> dict:
    trades = result.trades
    pnl = np.array([t.pnl for t in trades]) if trades else np.array([])
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]

    daily = np.array(list(result.daily_pnl.values()))
    daily_ret = daily / result.start_balance if len(daily) else np.array([])

    sharpe = sortino = float("nan")
    if len(daily_ret) > 1 and daily_ret.std(ddof=1) > 0:
        sharpe = float(daily_ret.mean() / daily_ret.std(ddof=1)
                       * np.sqrt(TRADING_DAYS))
    if len(daily_ret) > 1:
        downside = daily_ret[daily_ret < 0]
        if len(downside) > 1 and downside.std(ddof=1) > 0:
            sortino = float(daily_ret.mean() / downside.std(ddof=1)
                            * np.sqrt(TRADING_DAYS))

    # max drawdown on the bar-close equity curve (kept for comparison; it
    # misses any dip that recovers inside a bar)
    max_dd_bar = dd_pct_bar = 0.0
    if len(result.equity):
        peak = np.maximum.accumulate(result.equity)
        dd = result.equity - peak
        max_dd_bar = float(dd.min())
        troughs = peak > 0
        dd_pct_bar = (float((dd[troughs] / peak[troughs]).min() * 100)
                      if troughs.any() else 0.0)
    # the headline max drawdown is tick-level when the run tracked it
    tracker = getattr(result, "drawdown", None)
    if tracker is not None:
        max_dd, dd_pct = float(tracker.max_dd), float(tracker.max_dd_pct)
    else:
        max_dd, dd_pct = max_dd_bar, dd_pct_bar

    total_pnl = float(pnl.sum()) if len(pnl) else 0.0
    gross_win = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(-losses.sum()) if len(losses) else 0.0

    # Calmar: annualized return / max drawdown (both as fractions of start
    # balance). Chan (Machine Trading): preferred metric for directional
    # strategies; target > 1, quality > 2.
    calmar = float("nan")
    if len(daily_ret) > 1 and max_dd < 0:
        ann_ret = float(daily_ret.mean()) * TRADING_DAYS
        calmar = ann_ret / (abs(max_dd) / result.start_balance)

    stats = {
        "net_pnl": total_pnl,
        "gross_pnl": total_pnl + result.total_commission,
        "total_trades": len(trades),
        "win_rate": float(len(wins) / len(pnl) * 100) if len(pnl) else float("nan"),
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else float("inf") if gross_win > 0 else float("nan"),
        "avg_trade": float(pnl.mean()) if len(pnl) else float("nan"),
        "avg_win": float(wins.mean()) if len(wins) else float("nan"),
        "avg_loss": float(losses.mean()) if len(losses) else float("nan"),
        "largest_win": float(pnl.max()) if len(pnl) else float("nan"),
        "largest_loss": float(pnl.min()) if len(pnl) else float("nan"),
        "expectancy": float(pnl.mean()) if len(pnl) else float("nan"),
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "max_drawdown": max_dd,
        "max_drawdown_pct": dd_pct,
        "max_drawdown_bar": max_dd_bar,
        "max_drawdown_bar_pct": dd_pct_bar,
        "commission": result.total_commission,
        "trading_days": len(result.daily_pnl),
        "winning_days": int((daily > 0).sum()) if len(daily) else 0,
        "losing_days": int((daily < 0).sum()) if len(daily) else 0,
        "best_day": float(daily.max()) if len(daily) else float("nan"),
        "worst_day": float(daily.min()) if len(daily) else float("nan"),
        "avg_day": float(daily.mean()) if len(daily) else float("nan"),
    }

    # trade durations — fast-hit visibility. No confirmed prop-firm rule
    # ties to a specific duration (the earlier "30s minimum hold" figure
    # doesn't appear in the Intraday Trailing Drawdown PA/Eval rules, the
    # account type actually used here); this is now just a
    # diagnostic flag so a new strategy's reliance on very-fast hits is
    # visible at a glance.
    if trades:
        dur_s = np.array([(t.exit_ts - t.entry_ts) / 1e9 for t in trades])
        sub10 = dur_s < 10.0
        stats["median_duration_s"] = float(np.median(dur_s))
        stats["min_duration_s"] = float(dur_s.min())
        stats["sub10s_trades"] = int(sub10.sum())
        stats["sub10s_pct"] = float(sub10.mean() * 100)
        stats["sub10s_pnl"] = float(pnl[sub10].sum())
    else:
        stats["median_duration_s"] = float("nan")
        stats["min_duration_s"] = float("nan")
        stats["sub10s_trades"] = 0
        stats["sub10s_pct"] = float("nan")
        stats["sub10s_pnl"] = 0.0

    # prop-firm consistency: largest profitable day as % of total profit
    if len(daily) and total_pnl > 0:
        stats["consistency_pct"] = float(daily.max() / total_pnl * 100)
    else:
        stats["consistency_pct"] = float("nan")

    if result.prop is not None:
        a = result.prop
        stats["prop_breached"] = a.breached
        stats["prop_breach_ts"] = a.breach_ts
        stats["prop_min_headroom"] = (
            float(a.min_headroom) if np.isfinite(a.min_headroom) else float("nan"))
        stats["prop_threshold"] = a.cfg.threshold
    return stats


def format_console(result, stats: dict) -> str:
    def d(x):
        return f"${x:,.2f}" if x == x else "n/a"   # NaN-safe

    def r(x, spec=".2f", suffix=""):
        # ratios are NaN when undefined (no trades, no losing day, one day...)
        return f"{x:{spec}}{suffix}" if x == x else "n/a"

    lines = [
        "",
        f"=== {result.strategy_name} | {result.symbol} {result.period} bars | "
        f"{result.days[0]}..{result.days[-1]} ({stats['trading_days']} days) ===",
        f"Net P&L:        {d(stats['net_pnl'])}   (gross {d(stats['gross_pnl'])}, "
        f"commission {d(stats['commission'])})",
        f"Trades:         {stats['total_trades']}   win rate {r(stats['win_rate'], '.1f', '%')}   "
        f"profit factor {r(stats['profit_factor'])}",
        f"Avg trade:      {d(stats['avg_trade'])}   avg win {d(stats['avg_win'])}   "
        f"avg loss {d(stats['avg_loss'])}",
        f"Sharpe:         {r(stats['sharpe'])}   Sortino: {r(stats['sortino'])}   "
        f"Calmar: {r(stats['calmar'])}",
        f"Max drawdown:   {d(stats['max_drawdown'])} ({stats['max_drawdown_pct']:.2f}%)",
        f"Days:           +{stats['winning_days']} / -{stats['losing_days']}   "
        f"best {d(stats['best_day'])}   worst {d(stats['worst_day'])}",
        f"Consistency:    largest day = {stats['consistency_pct']:.1f}% of profit"
        if stats["consistency_pct"] == stats["consistency_pct"] else
        "Consistency:    n/a",
    ]
    if stats.get("total_trades"):
        lines.append(
            f"Duration:       median {stats['median_duration_s']:.0f}s   "
            f"min {stats['min_duration_s']:.0f}s   "
            f"<10s: {stats['sub10s_trades']} trades "
            f"({stats['sub10s_pct']:.1f}%, {d(stats['sub10s_pnl'])})")
    if "prop_breached" in stats:
        if stats["prop_breached"]:
            ts = np.datetime64(stats["prop_breach_ts"], "ns")
            lines.append(f"PROP:           *** BREACHED trailing threshold at {ts} UTC ***")
        else:
            lines.append(
                f"PROP:           survived; min headroom to threshold "
                f"{d(stats['prop_min_headroom'])} (threshold {d(stats['prop_threshold'])})")
    mode = getattr(result, "daily_limit_mode", None)
    how = {"gate": " [entry gate: realized P&L]",
           "kill": " [kill switch: equity incl. open P&L]"}.get(mode, "")
    if result.dll_days:
        lines.append(f"Daily loss limit hit on {len(result.dll_days)} day(s): "
                     + ", ".join(result.dll_days[:5])
                     + ("..." if len(result.dll_days) > 5 else "") + how)
    dpt = getattr(result, "dpt_days", None)
    if dpt:
        lines.append(f"Daily profit target hit on {len(dpt)} day(s): "
                     + ", ".join(dpt[:5]) + ("..." if len(dpt) > 5 else "") + how)
    for day, note in sorted((getattr(result, "day_notes", None) or {}).items()):
        lines.append(f"Note {day}:  {note}")
    rolls = getattr(result, "rolls", None)
    if rolls:
        flat = [r["date"] for r in rolls if r["flattened"]]
        lines.append(f"Rolls:          {len(rolls)} contract roll(s) in the run; flattened "
                     f"a position at {len(flat)}"
                     + (": " + ", ".join(flat[:6]) + ("..." if len(flat) > 6 else "")
                        if flat else ""))
    if getattr(result, "roll_entry_block_minutes", 0):
        lines.append(f"Roll blackout:  no new entries for {result.roll_entry_block_minutes:g} min "
                     f"after each roll; {result.roll_entries_refused} entry order(s) refused")
    lat = getattr(result, "latency_ms", None)
    if lat is not None:
        lines.append(f"Order latency:  {lat:g} ms" if lat > 0 else
                     "Order latency:  OFF (0 ms): the optimistic case, fills as if instant")
    cx, mod, br = (getattr(result, "cancel_latency_ms", 0.0), getattr(result, "modify_latency_ms", 0.0),
                   getattr(result, "bracket_latency_ms", None))
    if cx or mod or br is not None:
        lines.append(f"Exec latency:   cancel {cx:g} ms, modify {mod:g} ms, bracket "
                     + ("= order latency" if br is None else f"{br:g} ms")
                     + " (strategy cancels/modifies and fill -> stop live)")
    if getattr(result, "exits_dropped", 0):
        lines.append(f"Exit legs:      {result.exits_dropped} stop/target order(s) cancelled "
                     f"because their position was already closed or reversed")
    for note in getattr(result, "roll_notes", None) or []:
        lines.append(f"Roll warning:   {note}")
    if getattr(result, "flatten_at_roll", True) is False:
        lines.append("Rolls:          flatten-at-roll is OFF: a position held across a "
                     "contract roll books the calendar spread as P&L")
    if result.halted_on:
        lines.append(f"HALTED on {result.halted_on} (prop breach, halt_on_breach=True)")
    lines.append(f"Runtime:        {result.runtime_s:.1f}s")
    return "\n".join(lines)
