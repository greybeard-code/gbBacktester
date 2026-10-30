"""GreyKo spellings of the two renko bar types: `gk` = Traded = `k`, `gr` = Full Brick = `r`.

GreyKo (NT8 bar type id 91003) has one grid and two OHLC modes. The Python builders already
exist as `k` (KingRenko$, real open) and `r` (ninZaRenko, synthetic open); `gk` and `gr` are
the same builders under the GreyKo names, kept apart from `k` / `r` in the key (so in the
cache folder, the manifest and the report). `k` and `r` stay: they are the vendor-parity bars.

Reuses the hand-walked path of test_kingrenko_bars.py (brick 8, trend 4 at tick 0.25).
"""
import numpy as np
import pytest

from backtester.data import Catalog, build_bars
from backtester.strategy import BarSpec, parse_barspec

from test_kingrenko_bars import PATH, TICK, _day, _rows


def test_gk_is_the_kingrenko_kind_and_gr_the_renko_kind():
    gk, gr = parse_barspec("gk12-4"), parse_barspec("gr12-4")
    assert (gk.kind, gk.brick_ticks, gk.trend_ticks, gk.greyko) == ("kingrenko", 12, 4, True)
    assert (gr.kind, gr.brick_ticks, gr.trend_ticks, gr.greyko) == ("renko", 12, 4, True)


def test_keys_keep_the_greyko_name_and_the_vendor_keys_are_unchanged():
    assert parse_barspec("gk12-4").key == "gk12-4"
    assert parse_barspec("gr12-4").key == "gr12-4"
    assert parse_barspec("k12-4").key == "k12-4"
    assert parse_barspec("r12-4").key == "r12-4"
    assert not parse_barspec("k12-4").greyko and not parse_barspec("r12-4").greyko


def test_trend_defaults_to_half_the_brick_like_k_and_r():
    assert parse_barspec("gk12").trend_ticks == 6
    assert parse_barspec("gr13").trend_ticks == 6          # 13 // 2
    assert parse_barspec("gk1").trend_ticks == 1           # never below one tick


def test_trend_above_brick_is_rejected_for_the_new_names_too():
    for p in ("gk4-8", "gr4-8"):
        with pytest.raises(ValueError, match="must not exceed"):
            parse_barspec(p)


@pytest.mark.parametrize("p", ["g12-4", "gg12-4", "gk", "gr-4", "gx12-4", "gt12-4"])
def test_other_g_spellings_are_still_rejected(p):
    with pytest.raises(ValueError, match="Unrecognized bar period"):
        parse_barspec(p)


def test_gk_builds_the_k_bars_and_gr_builds_the_r_bars():
    d = _day(PATH)
    gk, k = build_bars(d, parse_barspec("gk8-4"), TICK), build_bars(d, parse_barspec("k8-4"), TICK)
    gr, r = build_bars(d, parse_barspec("gr8-4"), TICK), build_bars(d, parse_barspec("r8-4"), TICK)
    assert _rows(gk) == _rows(k) and len(gk) > 0
    assert _rows(gr) == _rows(r) and len(gr) > 0
    # and the two GreyKo modes differ from each other (Traded vs Full Brick OHLC)
    assert _rows(gk) != _rows(gr)
    # hand-walked (PATH_NOTES in test_kingrenko_bars.py): the first bar opens on the anchor in
    # both modes; bar 1 opens on its first trade (101.25) in Traded and on close - B =
    # 102.00 - 2.00 = 100.00 in Full Brick
    assert float(gk.open[0]) == 100.00 and float(gr.open[0]) == 100.00
    assert float(gk.open[1]) == 101.25 and float(gr.open[1]) == 100.00


def test_the_names_have_their_own_cache_folder(tmp_path):
    cat = Catalog(data_root=tmp_path, cache_root=tmp_path / "cache")
    paths = {s: cat._bars_path("TEST", "20260101", parse_barspec(s).key)
             for s in ("k8-4", "gk8-4", "r8-4", "gr8-4")}
    assert len(set(paths.values())) == 4
    assert paths["gk8-4"].parent.name == "gk8-4"
    day = _day(PATH)
    a = cat.load_bars_sequence("TEST", ["20260101"], parse_barspec("gk8-4"), TICK, days=[day])
    b = cat.load_bars_sequence("TEST", ["20260101"], parse_barspec("gk8-4"), TICK, days=[day])  # hit
    k = cat.load_bars_sequence("TEST", ["20260101"], parse_barspec("k8-4"), TICK, days=[day])
    assert paths["gk8-4"].exists()
    for x, y, z in zip(a, b, k):
        assert _rows(x) == _rows(y) == _rows(z)


def test_barspec_is_hashable_and_greyko_names_compare_unequal_to_vendor_ones():
    gk, k = parse_barspec("gk12-4"), parse_barspec("k12-4")
    assert gk != k and hash(gk) != hash(k)
    assert gk == BarSpec("kingrenko", brick_ticks=12, trend_ticks=4, greyko=True)
