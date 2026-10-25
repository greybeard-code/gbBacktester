"""report.py's trades CSV, tools/compare_nt8.py, the CLI end to end, and the
walk-forward verdict (plan task 23).

The trades CSV is a CONTRACT: report.write_trades_csv writes it, compare_nt8.py
reads it, and users diff it against NT8 Strategy Analyzer exports. Every value
is worked by hand. MNQ tick 0.25.
"""
from __future__ import annotations

import csv
import statistics
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import compare_nt8 as CN  # noqa: E402

import backtester.data as D  # noqa: E402
from backtester import report  # noqa: E402
from backtester.account import Trade  # noqa: E402
from backtester.walkforward import Window, summarize  # noqa: E402

from conftest import NS, write_synthetic_raw  # noqa: E402

HEADER = ["direction", "qty", "entry_time_utc", "exit_time_utc", "entry_price",
          "exit_price", "pnl", "commission", "mae", "mfe", "entry_tag", "exit_tag"]


def ns(iso: str) -> int:
    return int(np.datetime64(iso, "ns").astype("int64"))


def trades():
    return [
        Trade(direction=1, qty=2, entry_ts=ns("2026-02-02T14:31:00"),
              exit_ts=ns("2026-02-02T14:36:30.5"), entry_price=20000.25,
              exit_price=20010.0, pnl=38.0, commission=2.08, mae=-12.5, mfe=41.0,
              entry_tag="long", exit_tag="target"),
        Trade(direction=-1, qty=1, entry_ts=ns("2026-02-02T15:00:00"),
              exit_ts=ns("2026-02-02T15:02:00"), entry_price=19990.0,
              exit_price=19995.5, pnl=-12.04, commission=1.04, mae=-12.0, mfe=0.0,
              entry_tag="short", exit_tag="stop"),
    ]


# --------------------------------------------------------- trades CSV schema --

def test_trades_csv_has_the_documented_columns_and_formats(tmp_path):
    p = report.write_trades_csv(SimpleNamespace(trades=trades()), tmp_path / "sub" / "t.csv")
    assert p.exists()                                      # creates the folder
    rows = list(csv.reader(open(p, encoding="utf-8")))
    assert rows[0] == HEADER
    assert rows[1] == ["long", "2", "2026-02-02T14:31:00.000000", "2026-02-02T14:36:30.500000",
                       "20000.2500", "20010.0000", "38.00", "2.08", "-12.50", "41.00",
                       "long", "target"]
    assert rows[2][:4] == ["short", "1", "2026-02-02T15:00:00.000000", "2026-02-02T15:02:00.000000"]
    assert rows[2][4:] == ["19990.0000", "19995.5000", "-12.04", "1.04", "-12.00", "0.00",
                           "short", "stop"]
    assert len(rows) == 3


def test_an_empty_run_still_writes_the_header(tmp_path):
    p = report.write_trades_csv(SimpleNamespace(trades=[]), tmp_path / "t.csv")
    assert list(csv.reader(open(p, encoding="utf-8"))) == [HEADER]


def test_compare_nt8_reads_back_what_report_writes(tmp_path):
    p = report.write_trades_csv(SimpleNamespace(trades=trades()), tmp_path / "t.csv")
    back = CN.load_ours(str(p))
    assert [b["dir"] for b in back] == [1, -1]
    assert [b["qty"] for b in back] == [2, 1]
    assert [b["entry_p"] for b in back] == [20000.25, 19990.0]
    assert [b["exit_p"] for b in back] == [20010.0, 19995.5]
    assert back[0]["entry_t"].isoformat() == "2026-02-02T14:31:00+00:00"
    assert back[0]["exit_t"].isoformat() == "2026-02-02T14:36:30.500000+00:00"


# ------------------------------------------------------ compare_nt8 end to end --

NT8_COLS = "Trade #,Market pos.,Qty,Entry price,Exit price,Entry time,Exit time"


def test_compare_nt8_matches_trades_and_reports_tick_deltas(tmp_path, capsys, monkeypatch):
    ours = report.write_trades_csv(SimpleNamespace(trades=trades() + [
        Trade(direction=1, qty=1, entry_ts=ns("2026-02-02T20:00:00"),
              exit_ts=ns("2026-02-02T20:01:00"), entry_price=20100.0,
              exit_price=20101.0, pnl=1.0, commission=1.04, entry_tag="long",
              exit_tag="x")]), tmp_path / "ours.csv")
    # NT8 exports Chicago wall clock (CST = UTC-6 in February), "$" and "," in prices.
    # trade 1: 30 s later, entry 2 ticks... ours 20000.25 vs 20000.50 = -1 tick,
    #          exit 20010.00 vs 20009.75 = +1 tick
    # trade 2: identical prices, same second
    # ours' 3rd trade (20:00 UTC) has no NT8 partner; NT8 has an unmatched L at 11:00 CST
    nt8 = tmp_path / "nt8.csv"
    nt8.write_text(NT8_COLS + "\n"
                   '1,Long,2,"$20,000.50","$20,009.75",2/2/2026 8:31:30 AM,2/2/2026 8:37:00 AM\n'
                   '2,Short,1,"$19,990.00","$19,995.50",2/2/2026 9:00:00 AM,2/2/2026 9:02:00 AM\n'
                   '3,Long,1,"$20,500.00","$20,501.00",2/2/2026 11:00:00 AM,2/2/2026 11:01:00 AM\n',
                   encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["compare_nt8", str(ours), str(nt8)])
    CN.main()
    out = capsys.readouterr().out
    assert "ours: 3 trades   nt8: 3 trades" in out
    assert "matched 2 | only-ours 1 | only-nt8 1" in out
    assert "entry delta ticks: mean -0.50  median -0.50  max|.| 1.0" in out
    assert "exit  delta ticks: mean +0.50  median +0.50  max|.| 1.0" in out
    assert "only-ours: L 1 entry 2026-02-02 20:00:00+00:00 @ 20100.0" in out
    assert "<-- check" not in out                 # nothing beyond 2 ticks


def test_compare_nt8_flags_a_big_delta_and_respects_direction(tmp_path, capsys, monkeypatch):
    ours = report.write_trades_csv(SimpleNamespace(trades=[trades()[0]]), tmp_path / "o.csv")
    nt8 = tmp_path / "n.csv"
    # the NT8 trade is SHORT at the same time: direction differs, so no match
    nt8.write_text(NT8_COLS + "\n"
                   '1,Short,2,20000.25,20010.00,2/2/2026 8:31:00 AM,2/2/2026 8:36:00 AM\n',
                   encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["compare_nt8", str(ours), str(nt8)])
    CN.main()
    assert "matched 0 | only-ours 1 | only-nt8 1" in capsys.readouterr().out
    # a matching long whose entry is 3 ticks off is flagged
    nt8.write_text(NT8_COLS + "\n"
                   '1,Long,2,20001.00,20010.00,2/2/2026 8:31:00 AM,2/2/2026 8:36:00 AM\n',
                   encoding="utf-8")
    CN.main()
    out = capsys.readouterr().out
    assert "-3.0" in out and "<-- check" in out          # (20000.25 - 20001.00) / 0.25


def test_compare_nt8_tolerance_is_inclusive_to_the_second(tmp_path, capsys, monkeypatch):
    ours = report.write_trades_csv(SimpleNamespace(trades=[trades()[0]]), tmp_path / "o.csv")
    nt8 = tmp_path / "n.csv"
    monkeypatch.setattr(sys, "argv", ["compare_nt8", str(ours), str(nt8)])
    # ours enters 08:31:00 CST; NT8 at 08:32:30 is exactly 90 s away (the default tolerance)
    nt8.write_text(NT8_COLS + "\n"
                   '1,Long,2,20000.25,20010.00,2/2/2026 8:32:30 AM,2/2/2026 8:36:00 AM\n',
                   encoding="utf-8")
    CN.main()
    assert "matched 1 | only-ours 0 | only-nt8 0" in capsys.readouterr().out
    # one second further and it is a different trade
    nt8.write_text(NT8_COLS + "\n"
                   '1,Long,2,20000.25,20010.00,2/2/2026 8:32:31 AM,2/2/2026 8:36:00 AM\n',
                   encoding="utf-8")
    CN.main()
    assert "matched 0 | only-ours 1 | only-nt8 1" in capsys.readouterr().out


# ------------------------------------------------------- the CLI, end to end --

@pytest.fixture
def world(tmp_path, monkeypatch):
    data, cache = tmp_path / "data", tmp_path / "cache"
    for i, day in enumerate(("2025-01-15", "2025-01-16", "2025-01-17")):
        write_synthetic_raw(data / "MNQ-2025_L1" / f"{day.replace('-', '')}.parquet",
                            seed=i + 1, n_trades=1500, gap_after=700, day=day)
    monkeypatch.setattr(D, "DEFAULT_DATA_ROOT", data)
    monkeypatch.setattr(D, "DEFAULT_CACHE_ROOT", cache)
    return tmp_path


def test_cli_end_to_end_writes_tearsheet_trade_log_manifest_and_console(world, capsys):
    import cli
    out_html = world / "out" / "EmaCross.html"
    cli.main([str(REPO / "strategies" / "ema_cross.py"), "--start", "20250115",
              "--end", "20250117", "--mc", "0", "--out", str(out_html)])
    text = capsys.readouterr().out
    for needle in ("Running EmaCross on MNQ", "Net P&L:", "Trades:", "Sharpe:",
                   "Max drawdown:", "PROP:", "Manifest:", "Ledger:", "Tearsheet:",
                   "Trade log:"):
        assert needle in text, needle
    assert "nan" not in text.lower().replace("finance", "")      # ratios print n/a, not nan
    # the tearsheet: a self-contained page that carries the strategy's name
    page = out_html.read_text(encoding="utf-8")
    assert "<html" in page.lower() and "EmaCross" in page and "plotly" in page.lower()
    # the trade log: the documented header, and one row per trade the console counted
    log = list(csv.reader(open(out_html.with_name("EmaCross_trades.csv"), encoding="utf-8")))
    assert log[0] == HEADER
    n_trades = int(text.split("Trades:")[1].split()[0])
    assert len(log) - 1 == n_trades > 0
    assert (out_html.with_name("EmaCross_manifest.json")).is_file()
    # ...and compare_nt8 can read the log the CLI wrote
    assert len(CN.load_ours(str(out_html.with_name("EmaCross_trades.csv")))) == n_trades


# -------------------------------------------- walk-forward verdict and stitching --

def win(i, is_m, oos_m, net, daily):
    return Window(i, [], [], is_metric=is_m, oos_metric=oos_m,
                  oos_stats={"net_pnl": net, "total_trades": 5}, oos_daily=daily,
                  best_params={"p": i})


def test_wfe_is_ok_at_exactly_half_and_the_stitched_sharpe_is_computed_independently():
    ws = [win(1, 2.0, 1.0, 500.0, {"20260105": 500.0}),
          win(2, 1.0, 0.0, -200.0, {"20260106": -200.0}),
          win(3, 3.0, 2.0, 300.0, {"20260107": 300.0})]
    text = summarize(ws, "sharpe", 50_000.0)
    # mean OOS 1.0 / mean IS 2.0 = 0.50 -> the verdict threshold is >= 0.5
    assert "Walk-forward efficiency: 0.50 (OK" in text
    assert "Profitable OOS windows: 2/3" in text
    # stitched: 500 - 200 + 300 = $600 over 3 days. Sharpe from first principles:
    # returns .010, -.004, .006 -> mean .004, sample sd sqrt(1.04e-4 / 2) -> * sqrt(252)
    r = [500 / 50_000, -200 / 50_000, 300 / 50_000]
    sharpe = statistics.mean(r) / statistics.stdev(r) * 252 ** 0.5
    assert f"Stitched OOS: net $600 over 3 days, Sharpe {sharpe:.2f}" in text
    assert f"{sharpe:.2f}" == "8.81"


def test_wfe_below_half_is_called_poor_and_a_nonpositive_in_sample_has_no_wfe():
    poor = summarize([win(1, 2.0, 0.4, 10.0, {"a": 10.0}), win(2, 2.0, 0.0, -5.0, {"b": -5.0}),
                      win(3, 2.0, 0.2, 1.0, {"c": 1.0})], "sharpe", 50_000.0)
    assert "Walk-forward efficiency: 0.10 (POOR" in poor and "discard" in poor
    assert "Profitable OOS windows: 2/3" in poor        # a window that nets exactly 0 or less is not one
    flat = summarize([win(1, 1.0, 1.0, 0.0, {"a": 0.0}), win(2, 1.0, 1.0, 5.0, {"b": 5.0}),
                      win(3, 1.0, 1.0, -3.0, {"c": -3.0})], "sharpe", 50_000.0)
    assert "Profitable OOS windows: 1/3" in flat
    none = summarize([win(1, -1.0, 0.5, 10.0, {"a": 10.0}), win(2, 0.0, 0.5, 5.0, {"b": 5.0})],
                     "sharpe", 50_000.0)
    assert "Walk-forward efficiency" not in none         # nothing to divide by


def test_a_single_stitched_day_has_no_sharpe_and_says_n_a():
    text = summarize([win(1, 1.0, 1.0, 100.0, {"20260105": 100.0})], "sharpe", 50_000.0)
    assert "Sharpe n/a" in text and "nan" not in text.lower()
