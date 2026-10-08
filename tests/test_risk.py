"""Tests for the prop-firm risk module (Apex presets, budget, banking)."""
import numpy as np
import pytest

from backtester import risk
from backtester.account import PropFirmConfig
from backtester.contracts import get_spec


def series(vals):
    v = np.asarray(vals, dtype="float64")
    ts = np.arange(len(v), dtype="int64") * 1_000_000_000
    return v, ts


# ---------------------------------------------------------------- presets --- #
def test_active_is_25k_intraday():
    a = risk.ACTIVE
    assert a.size == 25_000
    assert a.target == 1_500
    assert a.drawdown == 1_000
    assert a.daily_loss_limit is None
    assert a.pass_balance == 26_500
    assert a.start_floor == 24_000
    assert a.pa_lock_balance == 25_100


def test_eval_config_freezes_at_target():
    cfg = risk.ACTIVE.config(phase="eval", platform="rithmic")
    assert cfg.threshold == 1_000
    assert cfg.lock and cfg.lock_buffer == 1_500


def test_tradovate_eval_never_locks():
    cfg = risk.ACTIVE.config(phase="eval", platform="tradovate")
    assert cfg.lock is False
    assert cfg.threshold == 1_000


def test_pa_config_freezes_at_start_plus_100():
    cfg = risk.ACTIVE.config(phase="pa")
    assert cfg.lock and cfg.lock_buffer == 100.0


def test_max_contracts_by_style():
    assert risk.ACTIVE.max_contracts(get_spec("MNQ")) == 40
    assert risk.ACTIVE.max_contracts(get_spec("NQ")) == 4


def test_eod_presets_carry_a_daily_loss_limit():
    a = risk.APEX_EOD[25_000]
    assert a.style == "eod"
    assert a.daily_loss_limit == 500.0


# ------------------------------------------------------------ risk budget --- #
def test_budget_contracts_fit_headroom():
    b = risk.RiskBudget(get_spec("MNQ"), max_loss_frac=0.5,
                        apex_max_contracts=40)
    # headroom 1000 -> budget 500; 25-pt stop -> $50/contract -> 10
    assert b.contracts(1_000, 25) == 10


def test_budget_respects_apex_cap():
    b = risk.RiskBudget(get_spec("MNQ"), max_loss_frac=0.5,
                        apex_max_contracts=4)
    assert b.contracts(1_000_000, 1) == 4


def test_budget_stands_down_when_headroom_tiny():
    b = risk.RiskBudget(get_spec("MNQ"), max_loss_frac=0.5,
                        apex_max_contracts=40)
    # headroom 40 -> budget 20; 25-pt stop -> $50/contract -> 0
    assert b.contracts(40, 25) == 0


def test_budget_stop_points_and_risk_dollars():
    b = risk.RiskBudget(get_spec("MNQ"), max_loss_frac=0.5)
    sp = b.stop_points(1_000, 4)             # 500 / (4*2) = 62.5
    assert sp == pytest.approx(62.5)
    assert b.risk_dollars(4, sp) == pytest.approx(500.0)


# --------------------------------------------------------- profit banking --- #
def test_profit_bank_take_profit_and_arm():
    p = risk.ProfitBankPolicy(target=1_500)
    assert p.take_profit(1_500) and not p.take_profit(1_499)
    assert p.armed(750) and not p.armed(749)


def test_profit_bank_floor_pnl():
    p = risk.ProfitBankPolicy(target=1_500, arm_frac=0.5, giveback=0.4)
    assert p.floor_pnl(700) == float("-inf")      # not armed yet
    assert p.floor_pnl(1_000) == pytest.approx(600.0)


# -------------------------------------------------------- simulate_eval --- #
def test_eval_pass_at_target():
    cfg = risk.ACTIVE.config()
    eq, ts = series([25_000, 26_000, 26_500])
    out = risk.simulate_eval(eq, ts, cfg, 1_500, 25_000)
    assert out.passed and not out.failed
    assert out.index == 2
    assert out.equity == pytest.approx(26_500)


def test_eval_breach_on_touch():
    cfg = risk.ACTIVE.config()
    eq, ts = series([25_000, 24_500, 24_000])
    out = risk.simulate_eval(eq, ts, cfg, 1_500, 25_000)
    assert out.failed and not out.passed
    assert out.index == 2


def test_eval_unrealized_peak_ratchets_floor():
    cfg = risk.ACTIVE.config()
    # peak 25_900 (no pass yet); floor = 24_900; fall to 24_900 -> breach
    eq, ts = series([25_000, 25_900, 24_900])
    out = risk.simulate_eval(eq, ts, cfg, 1_500, 25_000)
    assert out.failed and out.index == 2


def test_eval_lock_freezes_floor():
    eq, ts = series([25_000, 28_000, 26_600])
    locked = PropFirmConfig(threshold=1_000, lock_buffer=1_500, lock=True)
    out = risk.simulate_eval(eq, ts, locked, 5_000, 25_000)
    assert not out.failed and not out.passed     # frozen floor (26_500) held
    unlocked = PropFirmConfig(threshold=1_000, lock=False)
    out2 = risk.simulate_eval(eq, ts, unlocked, 5_000, 25_000)
    assert out2.failed                            # unfrozen floor (27_000) breached


def test_eval_tracks_min_headroom():
    cfg = risk.ACTIVE.config(platform="tradovate")   # trails forever, no lock
    eq, ts = series([25_000, 25_600, 25_100, 26_500])
    out = risk.simulate_eval(eq, ts, cfg, 1_500, 25_000)
    assert out.passed
    assert out.min_headroom == pytest.approx(500.0)   # 25_100 vs floor 24_600
