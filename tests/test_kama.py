"""NT8 KAMA port, the KamaRegimePro state machine, and the GZK regime gate.

Expected KAMA values are hand-computed from @KAMA.cs's recursion (see
research/KamaRegime_spec.md §2), not captured from the implementation.
"""
import importlib.util
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from backtester.indicators import KAMA, KamaRegime
from backtester.orders import BUY, SELL

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "godzilla_killa", ROOT / "strategies" / "godzilla_killa.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
GodZillaKilla = _mod.GodZillaKilla


# ---------------- KAMA ------------------------------------------------------

def test_period_below_nt8_minimum_rejected():
    with pytest.raises(ValueError, match="period"):
        KAMA(2, 4, 30)


def test_warmup_value_is_the_input_price():
    """@KAMA.cs: `if (CurrentBar < Period) Value[0] = Input[0]`."""
    k = KAMA(2, 5, 30)
    prices = [100.0, 103.0, 99.0, 104.0, 101.0]
    for p in prices:
        assert k.update(p) == p
        assert not k.ready               # still in the seeding branch
    k.update(106.0)                      # bar 5 == Period -> adaptive
    assert k.ready


def test_pure_trend_hand_computed():
    """Perfectly efficient trend: signal == noise, so ER == 1 and the
    smoothing constant collapses to fastCf**2 = (2/3)**2 = 4/9."""
    k = KAMA(2, 5, 30)
    for p in (100.0, 101.0, 102.0, 103.0, 104.0):
        k.update(p)                      # warmup -> value == price == 104
    # bar 5: signal = |105-100| = 5, noise = 5 x |1| = 5  ->  sc = 2/3
    v5 = 104.0 + (4 / 9) * (105.0 - 104.0)
    assert k.update(105.0) == pytest.approx(v5, abs=1e-12)
    # bar 6: the bar-0 diff (which NT8 seeds with the raw PRICE) has already
    # left the window; signal = |105-101| = 4, noise = 4 -> ER still 1
    v6 = v5 + (4 / 9) * (105.0 - v5)
    assert k.update(105.0) == pytest.approx(v6, abs=1e-12)


def test_zero_noise_holds_previous_value():
    """`if (noise == 0) Value[0] = Value[1]` — and the held value is NOT the
    price, so the branch is doing real work."""
    k = KAMA(2, 5, 30)
    for p in (100.0, 101.0, 102.0, 103.0, 104.0):
        k.update(p)
    v = 104.0
    for signal in (5, 4, 3, 2, 1):       # noise shrinks as the trend ages out
        v = v + (4 / 9) * (105.0 - v)    # ER == 1 on every one of these bars
        assert k.update(105.0) == pytest.approx(v, abs=1e-12)
    frozen = k.update(105.0)             # window now all zeros -> noise == 0
    assert frozen == pytest.approx(v, abs=1e-12)
    assert frozen != 105.0


def test_bar0_diff_is_the_price_and_never_reaches_a_published_value():
    """NT8 stores the raw price as bar 0's |diff| (@KAMA.cs:56). It is
    subtracted out of the running noise sum exactly at bar `Period`, the
    first bar that publishes an adaptive value — so a 100x larger price
    level must not change the shape of the output."""
    def run(offset):
        k = KAMA(2, 5, 30)
        out = [k.update(offset + p) for p in (0, 1, 2, 3, 4, 5, 4, 6, 5, 7)]
        return [v - offset for v in out]
    assert run(100.0) == pytest.approx(run(10000.0), abs=1e-9)


# ---------------- KamaRegime state machine ----------------------------------
#
# Fixture: 15 bars up 10/bar, 10 bars dead flat, 10 bars down 10/bar. With
# period=5 / warmup_bars=6 / tick_size=1.0 the slope (ticks/bar) runs
#   bar 6  +6.914 ... bar 14 +9.972   (trend)
#   bar 15  +5.54 ... bar 18 +0.95    (decaying catch-up)
#   bar 19-24  0.0                    (KAMA frozen: noise == 0)
#   bar 25  -3.917 ...                (down-trend)

UP = [100.0 + 10 * i for i in range(15)]
FLAT = [UP[-1]] * 10
DOWN = [FLAT[-1] - 10 * (i + 1) for i in range(10)]
SERIES = UP + FLAT + DOWN


def _drive(prices, **kw):
    kw.setdefault("period", 5)
    kw.setdefault("warmup_bars", 6)
    kw.setdefault("flat_threshold", 2.0)
    r = KamaRegime(1.0, **kw)
    rows = []
    for p in prices:
        r.update(p)
        rows.append((r.value, r.flipped, r.slope_ticks, r.bars_in_regime))
    return r, rows


def test_warmup_is_silent():
    r, rows = _drive(SERIES)
    for value, flipped, slope, _ in rows[:6]:
        assert value == 0 and not flipped and math.isnan(slope)
    assert not KamaRegime(1.0, period=5, warmup_bars=6).ready


def test_first_commit_then_flip():
    r, rows = _drive(SERIES)
    assert rows[6][0] == 1 and rows[6][1] is True      # first commit, bull
    assert rows[6][2] == pytest.approx(6.914, abs=0.001)
    assert rows[24][0] == 1 and rows[24][1] is False   # still bull
    assert rows[25][0] == -1 and rows[25][1] is True   # flip to bear
    assert rows[25][3] == 1                            # bars_in_regime resets
    assert [i for i, r_ in enumerate(rows) if r_[1]] == [6, 25]


def test_flat_band_is_sticky():
    """Slope decays through the +-2 band and sits at exactly 0.0 for six
    bars; the regime must hold bull rather than drift to neutral."""
    r, rows = _drive(SERIES)
    assert rows[17][2] == pytest.approx(1.71, abs=0.001)   # inside the band
    assert all(rows[i][2] == 0.0 for i in range(19, 25))   # KAMA frozen
    assert all(rows[i][0] == 1 for i in range(17, 25))
    assert rows[24][3] == 19                               # kept counting


def test_first_commit_ignores_the_flat_band():
    """regimeState == 0 -> sign only, no band (kamareginepro.cs:205). With a
    band so wide nothing can ever cross it, the regime commits once and then
    holds for the whole series, including the down-trend."""
    r, rows = _drive(SERIES, flat_threshold=100.0)
    assert rows[6][0] == 1 and rows[6][1] is True
    assert rows[-1][0] == 1 and rows[-1][2] < -9        # slope deeply negative
    assert sum(1 for r_ in rows if r_[1]) == 1


def test_confirm_bars_debounce():
    """ConfirmBars = N means the raw signal must persist N bars — including
    for the very first commit."""
    _, one = _drive(SERIES, confirm_bars=1)
    _, three = _drive(SERIES, confirm_bars=3)
    assert [i for i, r_ in enumerate(one) if r_[1]] == [6, 25]
    assert [i for i, r_ in enumerate(three) if r_[1]] == [8, 27]


def test_default_warmup_matches_the_indicator():
    r = KamaRegime(0.25, period=10, slow=30)
    assert r.warmup_bars == 45              # KamaPeriod + KamaSlow + 5


# ---------------- GodZillaKilla regime gate ---------------------------------

class _Broker:
    def __init__(self):
        self.spec = SimpleNamespace(tick_size=1.0)
        self.orders = []
        self.cancels = 0

    def submit(self, order, bracket=None):
        self.orders.append(order)
        return order

    def cancel_all(self):
        self.cancels += 1


def _gated(position=0, **kw):
    s = GodZillaKilla()
    for k, v in kw.items():
        setattr(s, k, v)
    s._broker = _Broker()
    s._account = SimpleNamespace(position=position)
    s._atm = SimpleNamespace(entry_qty=2, name="stub")
    s._pending_dir = 0
    s._kama = KamaRegime(1.0, period=5, warmup_bars=6, flat_threshold=2.0)
    return s


def _warm(s, bars):
    for p in bars:
        s._kama.update(p)
    return s


def test_entry_blocked_against_the_regime():
    s = _warm(_gated(kama_filter=True), SERIES[:7])   # bull committed at bar 6
    assert s._kama.value == 1
    s._go(-1)                                          # short into a bull regime
    assert s._broker.orders == []
    s._go(1)
    assert len(s._broker.orders) == 1
    assert s._broker.orders[0].side == BUY and s._broker.orders[0].qty == 2


def test_entry_blocked_during_warmup():
    """Regime 0 has no opinion yet — block rather than guess."""
    s = _warm(_gated(kama_filter=True), SERIES[:3])
    assert s._kama.value == 0
    s._go(1)
    s._go(-1)
    assert s._broker.orders == []


def test_filter_off_never_gates():
    s = _gated(kama_filter=False)
    s._kama = None                    # what on_start builds when the flag is off
    s._go(-1)
    assert len(s._broker.orders) == 1


def test_flatten_on_flip_is_opt_in_and_exit_only():
    # long position, regime about to flip bearish on bar 25
    s = _warm(_gated(position=3, kama_filter=True, kama_flatten_on_flip=True),
              SERIES[:25])
    assert s._kama.value == 1
    s._kama.update(SERIES[25])
    assert s._kama.flipped and s._kama.value == -1
    s._check_kama_flatten()
    assert s._broker.cancels == 1
    assert len(s._broker.orders) == 1
    o = s._broker.orders[0]
    assert o.side == SELL and o.qty == 3 and o.is_exit and o.tag == "kama-flat"

    # same flip, flag off -> position untouched
    s2 = _warm(_gated(position=3, kama_filter=True), SERIES[:26])
    s2._check_kama_flatten()
    assert s2._broker.orders == [] and s2._broker.cancels == 0


def test_flatten_on_flip_ignores_a_flip_in_our_favour():
    s = _warm(_gated(position=-3, kama_filter=True, kama_flatten_on_flip=True),
              SERIES[:26])                     # short, regime just flipped bear
    assert s._kama.flipped and s._kama.value == -1
    s._check_kama_flatten()
    assert s._broker.orders == []


def test_defaults_leave_the_filter_off():
    s = GodZillaKilla()
    assert s.kama_filter is False and s.kama_flatten_on_flip is False
    assert (s.kama_fast, s.kama_period, s.kama_slow) == (2, 10, 30)
    assert s.kama_flat_threshold == 0.5 and s.kama_confirm_bars == 1
