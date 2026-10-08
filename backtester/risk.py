"""Prop-firm risk module — Apex account presets, risk budget, profit banking.

Sits on top of ``account.PropFirmTracker`` (the live trailing floor) and adds
the pieces a bot needs to *obey* the Apex rules rather than only measure them:

  * ``ApexAccount`` — account presets. The active account is a **25K Intraday**
    evaluation (target $1,500, trailing drawdown $1,000, no daily loss limit,
    4 NQ / 40 MNQ). ``ApexAccount.config()`` builds the matching
    ``PropFirmConfig`` so the trailing floor and the eval race use the real
    rules (per apextraderfunding.com, Oct 2026).

  * ``RiskBudget`` — Apex trails the live *unrealized* peak, so the binding
    number is the run's headroom (``equity - floor``). The budget converts that
    headroom into a max-size / max-stop-distance / max-risk-per-trade limit so
    a run of losers cannot reach the floor (Davey BWATS position sizing).

  * ``ProfitBankPolicy`` — because the Apex intraday floor ratchets up with
    unrealized profit and never recedes, an open winner that reverses eats the
    buffer. The policy decides when to bank the position (take profit at the
    eval target) and when to lock it (ratchet a stop behind the unrealized
    high-water mark).

  * ``simulate_eval`` — run a bar-close equity path through the Apex rules and
    report pass / breach / unresolved, plus where it resolved.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .account import PropFirmConfig
from .contracts import ContractSpec


@dataclass(frozen=True)
class ApexAccount:
    """One Apex Trader Funding account preset."""

    name: str
    size: float
    target: float
    drawdown: float
    max_minis: int
    max_micros: int
    daily_loss_limit: float | None
    style: str = "intraday"            # "intraday" | "eod"

    @property
    def pass_balance(self) -> float:
        return self.size + self.target

    @property
    def start_floor(self) -> float:
        return self.size - self.drawdown

    @property
    def pa_lock_balance(self) -> float:
        return self.size + 100.0

    def max_contracts(self, spec: ContractSpec) -> int:
        return self.max_micros if spec.is_micro else self.max_minis

    def config(self, phase: str = "eval",
               platform: str = "rithmic") -> PropFirmConfig:
        """Build the ``PropFirmConfig`` matching this account's rules.

        phase="eval", platform in {"rithmic","wealthcharts"}: the floor freezes
        at the profit-target balance; ``platform="tradovate"`` never freezes.
        phase="pa" (funded): the floor freezes at ``start + $100``.
        """
        if phase == "pa":
            return PropFirmConfig(threshold=self.drawdown,
                                  lock_buffer=100.0, lock=True)
        if platform == "tradovate":
            return PropFirmConfig(threshold=self.drawdown, lock=False)
        return PropFirmConfig(threshold=self.drawdown,
                              lock_buffer=self.target, lock=True)


# 2026 Apex tiers (profit target = 6% of size). Intraday: no daily loss limit,
# trailing drawdown enforced live incl. unrealized. EOD: trailing only at the
# close, plus a daily loss limit. Third-party pages also list 75K/250K/300K.
APEX_INTRADAY: dict[float, ApexAccount] = {
    25_000: ApexAccount("25K Intraday", 25_000, 1_500, 1_000, 4, 40, None),
    50_000: ApexAccount("50K Intraday", 50_000, 3_000, 2_000, 6, 60, None),
    100_000: ApexAccount("100K Intraday", 100_000, 6_000, 3_000, 10, 100, None),
    150_000: ApexAccount("150K Intraday", 150_000, 9_000, 4_500, 14, 140, None),
}
APEX_EOD: dict[float, ApexAccount] = {
    25_000: ApexAccount("25K EOD", 25_000, 1_500, 1_000, 4, 40, 500.0, "eod"),
    50_000: ApexAccount("50K EOD", 50_000, 3_000, 2_000, 6, 60, 1_000.0, "eod"),
    100_000: ApexAccount("100K EOD", 100_000, 6_000, 3_000, 8, 80, 1_500.0, "eod"),
    150_000: ApexAccount("150K EOD", 150_000, 9_000, 4_000, 12, 120, 2_000.0, "eod"),
}

# The account currently in use (a few 25K Intraday evaluations).
ACTIVE = APEX_INTRADAY[25_000]

# --------------------------------------------------------------------------- #
# Risk budget
# --------------------------------------------------------------------------- #
@dataclass
class RiskBudget:
    """Turn Apex headroom into a position / stop limit.

    ``equity - floor`` is the live headroom. A single trade's worst case
    (``contracts * stop_points * point_value``) is capped at ``max_loss_frac``
    of that headroom, so a run of losers cannot reach the floor.
    """

    spec: ContractSpec
    max_loss_frac: float = 0.5
    apex_max_contracts: int | None = None      # None -> spec.apex_max_position

    def _cap(self) -> int:
        if self.apex_max_contracts is not None:
            return self.apex_max_contracts
        return self.spec.apex_max_position

    def loss_budget(self, headroom: float) -> float:
        """Dollars that a single trade may risk from the current headroom."""
        return max(0.0, float(headroom)) * self.max_loss_frac

    def contracts(self, headroom: float, stop_points: float) -> int:
        """Largest size whose stop risk fits the budget (0 = stand down)."""
        per = float(stop_points) * self.spec.point_value
        if per <= 0:
            return 0
        n = int(self.loss_budget(headroom) // per)
        return max(0, min(n, self._cap()))

    def stop_points(self, headroom: float, contracts: int) -> float:
        """Widest stop (points) that keeps ``contracts`` inside the budget."""
        if contracts <= 0:
            return 0.0
        return self.loss_budget(headroom) / (contracts * self.spec.point_value)

    def risk_dollars(self, contracts: int, stop_points: float) -> float:
        return contracts * float(stop_points) * self.spec.point_value


# --------------------------------------------------------------------------- #
# Profit banking
# --------------------------------------------------------------------------- #
@dataclass
class ProfitBankPolicy:
    """Protect the Apex unrealized peak (which trails the floor up, never down).

    ``take_profit``: close once open profit reaches ``target`` (bank the pass).
    ``floor_pnl``: once open profit reaches ``arm_frac * target``, hold no
    lower than ``peak * (1 - giveback)`` (ratchet a stop behind the high-water).
    """

    target: float
    arm_frac: float = 0.5
    giveback: float = 0.4

    def take_profit(self, open_pnl: float) -> bool:
        return float(open_pnl) >= self.target

    def armed(self, open_pnl: float) -> bool:
        return float(open_pnl) >= self.arm_frac * self.target

    def floor_pnl(self, peak_pnl: float) -> float:
        """Minimum open P&L to hold (``-inf`` until armed)."""
        if not self.armed(peak_pnl):
            return float("-inf")
        return float(peak_pnl) * (1.0 - self.giveback)


# --------------------------------------------------------------------------- #
# Evaluation simulation
# --------------------------------------------------------------------------- #
@dataclass
class EvalOutcome:
    passed: bool
    failed: bool
    index: int | None = None
    ts: int | None = None
    equity: float | None = None
    min_headroom: float = float("inf")


def simulate_eval(equity: np.ndarray, ts: np.ndarray, cfg: PropFirmConfig,
                  target: float, start_balance: float) -> EvalOutcome:
    """Race the profit target against the trailing floor on an equity path.

    ``equity``/``ts`` are a chronological bar-close segment. Pass when equity
    reaches ``start_balance + target``; fail when it touches the trailing floor
    (peak - threshold, frozen at ``start + lock_buffer`` when ``cfg.lock``).
    A same-bar tie counts as a breach (conservative, matches montecarlo).
    """
    equity = np.asarray(equity, dtype="float64")
    ts = np.asarray(ts, dtype="int64")
    if len(equity) == 0:
        return EvalOutcome(passed=False, failed=False)

    peak = np.maximum.accumulate(np.maximum(equity, start_balance))
    floor = peak - cfg.threshold
    if cfg.lock:
        floor = np.minimum(floor, start_balance + cfg.lock_buffer)
    headroom = equity - floor
    min_headroom = float(headroom.min())

    hit = equity >= start_balance + target
    breach = headroom <= 0
    pass_i = int(np.argmax(hit)) if hit.any() else None
    breach_i = int(np.argmax(breach)) if breach.any() else None

    if breach_i is not None and (pass_i is None or breach_i <= pass_i):
        return EvalOutcome(passed=False, failed=True, index=breach_i,
                           ts=int(ts[breach_i]), equity=float(equity[breach_i]),
                           min_headroom=min_headroom)
    if pass_i is not None:
        return EvalOutcome(passed=True, failed=False, index=pass_i,
                           ts=int(ts[pass_i]), equity=float(equity[pass_i]),
                           min_headroom=min_headroom)
    return EvalOutcome(passed=False, failed=False, min_headroom=min_headroom)
