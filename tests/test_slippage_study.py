"""Tests for slippage_study/: synthetic streams with hand-computed spreads, window bucketing
(incl. DST), the stale-quote cap, chunk invariance and pooled percentiles. Data-free."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "slippage_study"))
import spread_study as ss  # noqa: E402
import summarize as sm  # noqa: E402

S = 1_000_000_000
DATE = "20260115"                      # EST: ET midnight = 05:00 UTC
MID = int(np.datetime64("2026-01-15T05:00:00").astype("datetime64[ns]").astype("int64"))
TICK = 0.25


def ev(rows):
    """rows: (seconds after ET midnight, type, price, volume) -> arrays."""
    rows = sorted(rows, key=lambda r: r[0])
    ts = np.array([MID + int(r[0] * S) for r in rows], dtype=np.int64)
    return (ts, np.array([r[1] for r in rows], dtype=np.int8),
            np.array([r[2] for r in rows], dtype=float), np.array([r[3] for r in rows], dtype=np.int64))


def win(res, hh, mm):
    """Row index of the UTC bin holding ET hh:mm (EST day)."""
    return (hh * 60 + mm) // 30


T = 10 * 3600                          # 10:00:00 ET


def test_trade_weighted_hist_and_mean():
    # 10:00:00 bid 100.00 ask 100.25 (1 tick); trade; 10:00:10 ask -> 100.75 (3 ticks); two trades
    r = ev([(T, 1, 100.00, 5), (T, 0, 100.25, 5), (T + 1, 2, 100.25, 2),
            (T + 10, 0, 100.75, 5), (T + 11, 2, 100.75, 1), (T + 12, 2, 100.00, 4)])
    res = ss.measure_day(*r, DATE, TICK)
    w = win(res, 10, 0)
    assert res["n_trades"][w] == 3 and res["volume"][w] == 7
    assert res["n_valid"][w] == 3
    assert res["tr_hist"][w][1] == 1 and res["tr_hist"][w][3] == 2 and res["tr_hist"][w].sum() == 3
    assert res["tr_sum_ticks"][w] == 1 + 3 + 3 and res["tr_max"][w] == 3
    assert res["range_ticks"][w] == 3        # trades 100.25 .. 100.75 .. 100.00 -> range 0.75 = 3 ticks


def test_prevailing_quote_is_strictly_before_the_trade_in_row_order():
    # the ask moves to 100.75 in the SAME timestamp but AFTER the trade row: the trade sees 1 tick
    ts = np.array([MID + T * S] * 4, dtype=np.int64)
    mdt = np.array([1, 0, 2, 0], dtype=np.int8)
    px = np.array([100.0, 100.25, 100.25, 100.75])
    res = ss.measure_day(ts, mdt, px, np.array([1, 1, 1, 1]), DATE, TICK)
    assert res["tr_hist"][win(res, 10, 0)][1] == 1


def test_invalid_trades_are_split_out():
    r = ev([(T, 2, 100.0, 1),                              # no quote yet
            (T + 1, 1, 100.00, 1), (T + 1, 0, 100.00, 1),  # locked
            (T + 2, 2, 100.0, 1),
            (T + 3, 0, 99.75, 1),                          # crossed (ask 99.75 < bid 100)
            (T + 4, 2, 100.0, 1),
            (T + 5, 0, 100.25, 1), (T + 6, 2, 100.0, 1)])  # 1 tick, valid
    res = ss.measure_day(*r, DATE, TICK)
    w = win(res, 10, 0)
    assert (res["n_trades"][w], res["n_noquote"][w], res["n_locked"][w],
            res["n_crossed"][w], res["n_valid"][w]) == (4, 1, 1, 1, 1)
    # engine rule only blanks crossed: locked is then NOT a valid-spread >=1 either
    eng = ss.measure_day(*r, DATE, TICK, engine_rule=True)
    assert eng["n_crossed"][w] == 0 and eng["n_noquote"][w] == 2


def test_ignores_other_market_data_types():
    # a type-5 row (as many as Last in real files) must not disturb the prevailing quote
    r = ev([(T, 1, 100.00, 5), (T, 0, 100.25, 5), (T + 1, 5, 999.0, 1), (T + 1, 2, 100.25, 1)])
    res = ss.measure_day(*r, DATE, TICK)
    assert res["tr_hist"][win(res, 10, 0)][1] == 1
    assert res["tw_hist"][win(res, 10, 0)][1] == 300 * S and res["tw_ns_locked_crossed"][win(res, 10, 0)] == 0


def test_time_weighted_exact_and_split_at_boundary():
    # 1 tick from 10:29:50 to 10:30:10 -> 10 s in the 10:00 window, 10 s in 10:30; then 3 ticks for 20 s
    t0 = T + 29 * 60 + 50
    r = ev([(t0, 1, 100.00, 1), (t0, 0, 100.25, 1),      # 1 tick
            (t0 + 20, 0, 100.75, 1),                      # 3 ticks from 10:30:10
            (t0 + 40, 1, 100.50, 1)])                     # bid up: 1 tick from 10:30:30 (cap -> 300 s)
    res = ss.measure_day(*r, DATE, TICK)
    a, b = win(res, 10, 0), win(res, 10, 30)
    assert res["tw_hist"][a][1] == 10 * S and res["tw_hist"][a].sum() == 10 * S
    assert res["tw_hist"][b][1] == 10 * S + 300 * S      # 10 s + capped final state
    assert res["tw_hist"][b][3] == 20 * S
    assert res["tw_sum_tick_ns"][b] == 1 * (310 * S) + 3 * (20 * S)
    assert res["tw_ns"][b] == 330 * S


def test_cap_limits_stale_state():
    r = ev([(T, 1, 100.00, 1), (T, 0, 100.25, 1), (T + 3000, 2, 100.25, 1)])  # trade 50 min later
    res = ss.measure_day(*r, DATE, TICK)
    w = win(res, 10, 0)
    assert res["tw_hist"][w][1] == 300 * S               # not 50 minutes
    assert res["tw_ns"][w] == 300 * S and res["tw_ns_stale"][w] == 0


def test_stale_time_is_recorded():
    r = ev([(T, 1, 100.00, 1), (T, 0, 100.25, 1), (T + 600, 0, 100.50, 1)])
    res = ss.measure_day(*r, DATE, TICK)
    w = win(res, 10, 0)
    assert res["tw_ns_stale"][w] == 300 * S              # 600 s gap - 300 s cap


def test_no_time_before_both_sides_quoted():
    r = ev([(T, 0, 100.25, 1), (T + 20, 1, 100.00, 1), (T + 30, 0, 100.25, 1)])
    res = ss.measure_day(*r, DATE, TICK)
    w = win(res, 10, 0)
    # one-sided for 20 s: not counted; from T+20, 1 tick for 10 s + capped 300 s
    assert res["tw_hist"][w][1] == 10 * S + 300 * S


def test_locked_and_crossed_time_kept_apart():
    r = ev([(T, 1, 100.00, 1), (T, 0, 100.00, 1), (T + 5, 0, 100.25, 1)])  # locked 5 s then 1 tick
    res = ss.measure_day(*r, DATE, TICK)
    w = win(res, 10, 0)
    assert res["tw_ns_locked_crossed"][w] == 5 * S and res["tw_hist"][w][1] == 300 * S


def test_splice_and_databento_flags_blank_time_weighting():
    r = ev([(T, 1, 100.00, 1), (T, 0, 100.25, 1), (T + 100, 2, 100.25, 1)])
    cut = MID + (T + 40) * S
    res = ss.measure_day(*r, DATE, TICK, tw_valid_from_ns=cut)
    assert res["tw_hist"][win(res, 10, 0)][1] == 40 * S
    assert res["tr_hist"][win(res, 10, 0)][1] == 1       # trades stay valid
    off = ss.measure_day(*r, DATE, TICK, tw_enabled=False)
    assert off["tw_hist"].sum() == 0 and off["tr_hist"].sum() == 1


def test_overflow_bin():
    r = ev([(T, 1, 100.00, 1), (T, 0, 100.00 + 70 * TICK, 1), (T + 1, 2, 100.0, 1)])
    res = ss.measure_day(*r, DATE, TICK)
    w = win(res, 10, 0)
    assert res["tr_hist"][w][ss.OVERFLOW] == 1 and res["tr_max"][w] == 70


def test_window_labels_and_trading_day_with_dst():
    first, n, lab, shift = ss.day_bins("20260115")
    assert n == 48 and lab.tolist() == list(range(48))
    assert shift.tolist() == [0] * 36 + [1] * 12           # 18:00 ET and later belongs to next date
    _, n, lab, _ = ss.day_bins("20260308")                  # spring forward: 02:00-03:00 absent
    assert n == 46 and 4 not in lab and 5 not in lab and lab[4] == 6
    _, n, lab, _ = ss.day_bins("20261101")                  # fall back: 01:00-02:00 occurs twice
    assert n == 50 and (lab == 2).sum() == 2 and (lab == 3).sum() == 2


def test_dst_day_event_lands_in_right_wall_clock_window():
    # 2026-03-08: after 02:00 the offset is EDT (UTC-4): 12:00 UTC = 08:00 ET
    first, n, lab, _ = ss.day_bins("20260308")
    ts = np.array([int(np.datetime64("2026-03-08T12:00:00").astype("datetime64[ns]").astype("int64"))])
    b = ts[0] // ss.W_NS - first
    assert lab[b] == 16


def test_rows_pool_repeated_hour_and_report_trading_day():
    r = ev([(T, 1, 100.00, 1), (T, 0, 100.25, 1), (19 * 3600, 1, 100.0, 1), (19 * 3600, 0, 100.25, 1),
            (19 * 3600 + 1, 2, 100.25, 3)])
    res = ss.measure_day(*r, DATE, TICK)
    rows = ss.day_rows("MNQ", DATE, res, {})
    by = {x["window_et"]: x for x in rows}
    assert by["19:00"]["trading_day"] == "20260116" and by["10:00"]["trading_day"] == "20260115"
    assert by["19:00"]["n_trades"] == 1 and by["19:00"]["tr_hist"][1] == 1


def test_tick_table_matches_the_engine():
    from backtester.contracts import SPECS
    for sym, tick in ss.TICK.items():
        s = SPECS[sym]
        assert s.tick_size == tick and s.point_value == ss.POINT_VALUE[sym], sym


def test_chunking_does_not_change_results(monkeypatch):
    rng = np.random.default_rng(7)
    rows = []
    bid, ask = 100.0, 100.25
    for i in range(400):
        t = T + 1700 + i * 0.9 + rng.random() * 0.5            # crosses the 10:30 boundary
        kind = int(rng.integers(0, 3))
        if kind == 0:
            ask = bid + 0.25 * int(rng.integers(0, 6)) + 0.25
        elif kind == 1:
            bid = ask - 0.25 * int(rng.integers(1, 5))
        rows.append((t, kind, ask if kind == 0 else (bid if kind == 1 else ask), 1))
    r = ev(rows)
    monkeypatch.setattr(ss, "TW_CHUNK", 10**9)
    ref = ss.measure_day(*r, DATE, TICK)
    for chunk in (1, 2, 7, 64):
        monkeypatch.setattr(ss, "TW_CHUNK", chunk)
        got = ss.measure_day(*r, DATE, TICK)
        for k in ("tw_hist", "tr_hist", "tw_sum_tick_ns", "tw_ns", "tw_ns_stale", "n_quotes",
                  "tw_ns_locked_crossed", "tw_max"):
            assert np.array_equal(ref[k], got[k]), (chunk, k)
    assert ref["tw_hist"].sum() > 0



def hist(**k):
    h = np.zeros(65, dtype=np.int64)
    for tick, n in k.items():
        h[int(tick[1:])] = n
    return h


def test_percentiles_from_pooled_histogram():
    h = hist(t1=90, t2=6, t5=4)                    # 100 trades
    assert sm.pct_from_hist(h, 0.5) == 1 and sm.pct_from_hist(h, 0.9) == 1
    assert sm.pct_from_hist(h, 0.95) == 2 and sm.pct_from_hist(h, 0.96) == 2
    assert sm.pct_from_hist(h, 0.97) == 5
    assert np.isnan(sm.pct_from_hist(hist(), 0.5))


def test_pooling_differs_from_averaging_per_day_percentiles():
    # day A: 100 trades all 1 tick; day B: 1 trade at 10 ticks. Pooled p95 = 1; mean of day p95s = 5.5
    a, b = hist(t1=100), hist(t10=1)
    pooled = sm.pct_from_hist(a + b, 0.95)
    avg = (sm.pct_from_hist(a, 0.95) + sm.pct_from_hist(b, 0.95)) / 2
    assert pooled == 1 and avg == 5.5
