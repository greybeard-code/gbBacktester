"""Run a backtest from the command line.

Usage:
    python cli.py strategies/ema_cross.py --start 2026-06-01 --end 2026-06-17
    python cli.py strategies/ema_cross.py --symbol MNQ --period 1m --out report.html
"""
from __future__ import annotations

import argparse
from pathlib import Path

from backtester import PropFirmConfig, Backtest
from backtester import manifest, metrics, montecarlo, report
from backtester.loader import load_strategy


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Tick-level futures backtester")
    ap.add_argument("strategy", help="path to a .py file with a Strategy subclass")
    ap.add_argument("--symbol", help="override strategy symbol (e.g. MNQ)")
    ap.add_argument("--period", help="override bar type: time (30s, 1m, 5m), "
                                     "tick (500t), ninZaRenko (r8, r8-4), "
                                     "SaberRenko (s64-16), TBars (tb120)")
    ap.add_argument("--start", help="first day, YYYY-MM-DD or YYYYMMDD")
    ap.add_argument("--end", help="last day, YYYY-MM-DD or YYYYMMDD")
    ap.add_argument("--balance", type=float, default=50_000.0,
                    help="starting balance (default 50000)")
    ap.add_argument("--prop-threshold", "--apex-threshold", dest="prop_threshold",
                    type=float, default=2000.0,
                    help="prop-firm trailing threshold in $, Apex-style "
                         "(default 2000; 0 disables)")
    ap.add_argument("--prop-halt", "--apex-halt", dest="prop_halt",
                    action="store_true",
                    help="stop the backtest when the threshold is breached")
    ap.add_argument("--slippage", type=float, default=0.0,
                    help="extra slippage in ticks on market/stop fills")
    ap.add_argument("--daily-loss-limit", type=float, default=None, metavar="$",
                    help="daily loss limit in $ (see --daily-limit-mode)")
    ap.add_argument("--daily-profit-target", type=float, default=None, metavar="$",
                    help="daily profit target in $ (see --daily-limit-mode)")
    ap.add_argument("--daily-limit-mode", choices=("gate", "kill"), default=None,
                    help="how the daily limits bite. gate (default): REALIZED "
                         "P&L of closed trades blocks new entries for the rest "
                         "of the trading day, exits keep working (how most NT8 "
                         "strategies' own daily limits behave). kill: equity INCLUDING "
                         "open P&L, tick by tick, flattens and refuses every "
                         "order (GodZillaKilla). Default: the strategy's own "
                         "daily_limit_mode, else gate")
    ap.add_argument("--latency-ms", type=float, default=None, metavar="MS",
                    help="order latency: a new order (and the stop/target legs "
                         "made at an entry fill) reaches the exchange this long "
                         "after it is sent. A strategy's cancels and modifies "
                         "are instant unless --cancel-latency-ms / --modify-latency-ms "
                         "are set; forced flattens are never delayed. Default: the strategy's "
                         "latency_ms, else 100 (live-measured, 2026-10-22); "
                         "0 turns it off, the optimistic case")
    ap.add_argument("--cancel-latency-ms", type=float, default=None, metavar="MS",
                    help="a strategy's cancel reaches the exchange this long after it is "
                         "sent (the order can still fill meanwhile). One way = half a "
                         "measured round trip (one weekday, Apex / Tradovate: ~40 evening, ~65 open). "
                         "Default: the strategy's cancel_latency_ms, else 0 (instant)")
    ap.add_argument("--modify-latency-ms", type=float, default=None, metavar="MS",
                    help="a strategy's modify (move_stop, move_target) reaches the exchange "
                         "this long after it is sent (the old price stays live meanwhile). "
                         "Measured one way: ~22 evening, ~25 open. Default: the strategy's "
                         "modify_latency_ms, else 0 (instant)")
    ap.add_argument("--bracket-latency-ms", type=float, default=None, metavar="MS",
                    help="a protective stop/target made at an entry fill (a bracket or ATM "
                         "leg) goes live this long after the FILL: a full round trip, since "
                         "the fill notice must reach the strategy first (measured ~105 "
                         "evening, ~182 open). Default: the strategy's bracket_latency_ms, "
                         "else the order latency")
    ap.add_argument("--no-roll-flatten", action="store_true",
                    help="do NOT flatten before contract rolls (the default does: "
                         "the day files switch contract at a midnight inside the "
                         "session, and a position carried across it books the "
                         "calendar spread as P&L). Use to reproduce numbers from "
                         "before 2026-10-19")
    ap.add_argument("--roll-block-minutes", type=float, default=None, metavar="MIN",
                    help="no NEW entries for this many minutes from the first tick on "
                         "each new contract (after a roll the bars and indicators still "
                         "see the calendar spread as a move). Default: the strategy's "
                         "roll_entry_block_minutes, else 0 (off). A sensitivity check: "
                         "re-run with 120 and see whether the result depends on the "
                         "post-roll signals")
    ap.add_argument("--news-filter", action="store_true",
                    help="block entries around high-impact (FF red folder) news")
    ap.add_argument("--news-pre", type=float, default=None, metavar="MIN",
                    help="minutes before an event to stop entering (default 5)")
    ap.add_argument("--news-post", type=float, default=None, metavar="MIN",
                    help="minutes after an event to resume entering (default 5)")
    ap.add_argument("--news-currencies", default=None, metavar="USD,EUR",
                    help="event currencies to honor (default USD)")
    ap.add_argument("--news-flatten", action="store_true",
                    help="also flatten an open position entering a news window")
    ap.add_argument("--news-csv", default=None,
                    help="override the news calendar CSV path")
    ap.add_argument("--mc", type=int, default=2000, metavar="N",
                    help="Monte Carlo simulations (default 2000; 0 disables)")
    ap.add_argument("--mc-target", type=float, default=None, metavar="$",
                    help="eval profit target for P(pass before breach), "
                         "e.g. 3000 for a 50K Apex eval")
    ap.add_argument("--out", default=None,
                    help="tearsheet path (default reports/<strategy>_<symbol>.html)")
    ap.add_argument("--no-report", action="store_true", help="console output only")
    ap.add_argument("--data-root", default=None)
    args = ap.parse_args(argv)

    strat = load_strategy(args.strategy)
    if args.news_filter:
        strat.news_filter = True
        if args.news_pre is not None:
            strat.news_pre_min = args.news_pre
        if args.news_post is not None:
            strat.news_post_min = args.news_post
        if args.news_currencies:
            strat.news_currencies = tuple(c.strip().upper()
                                          for c in args.news_currencies.split(","))
        if args.news_flatten:
            strat.news_flatten = True
        if args.news_csv:
            strat.news_csv = args.news_csv
    prop = None
    if args.prop_threshold > 0:
        prop = PropFirmConfig(threshold=args.prop_threshold,
                              halt_on_breach=args.prop_halt)

    bt = Backtest(strat, start=args.start, end=args.end, symbol=args.symbol,
                  period=args.period, start_balance=args.balance, prop=prop,
                  slippage_ticks=args.slippage,
                  daily_loss_limit=args.daily_loss_limit,
                  daily_profit_target=args.daily_profit_target,
                  daily_limit_mode=args.daily_limit_mode,
                  latency_ms=args.latency_ms,
                  cancel_latency_ms=args.cancel_latency_ms,
                  modify_latency_ms=args.modify_latency_ms,
                  bracket_latency_ms=args.bracket_latency_ms,
                  flatten_at_roll=False if args.no_roll_flatten else None,
                  roll_entry_block_minutes=args.roll_block_minutes,
                  data_root=args.data_root)
    print(f"Running {type(strat).__name__} on {bt.symbol}, "
          f"{bt.barspec.key} bars ...")
    rec = manifest.begin_run(bt, kind="cli")     # config BEFORE the run
    result = bt.run()
    stats = metrics.compute(result)
    print(metrics.format_console(result, stats))

    mc = None
    if args.mc > 0 and len(result.trades) >= 2:
        mc = montecarlo.run(
            result.trades, start_balance=args.balance, n_sims=args.mc,
            prop_threshold=args.prop_threshold if args.prop_threshold > 0 else None,
            profit_target=args.mc_target)
        print()
        print(montecarlo.format_console(mc))

    out = Path(args.out or (Path("reports")
                            / f"{result.strategy_name}_{result.symbol}.html"))
    # the manifest goes next to the tearsheet when there is one, else in
    # the manifest folder (manifest.manifest_dir()): a number with no manifest did not
    # come from the engine
    m = manifest.finish_run(
        rec, result, stats,
        manifest_path=(out.with_name(out.stem + "_manifest.json")
                       if (args.out or not args.no_report) else None))
    if m:
        print(f"\nManifest:       {Path(m['path']).resolve()}")
        if m["copy"]:
            print(f"                (copy next to the tearsheet: {Path(m['copy']).resolve()})")
        print(f"Ledger:         {m['text']}  [seq {m['seq']}]")

    if not args.no_report:
        path = report.render(result, stats, out, mc=mc)
        csv_path = report.write_trades_csv(
            result, out.with_name(out.stem + "_trades.csv"))
        print(f"\nTearsheet: {path.resolve()}")
        print(f"Trade log: {csv_path.resolve()}")


if __name__ == "__main__":
    main()
