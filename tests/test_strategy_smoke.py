"""Smoke test for every strategy tracked in git (plan task 22).

For each `strategies/*.py` that git tracks: it loads, it runs on multi-day
synthetic ticks (trends, pauses, a day block and an evening block, scaled to the
strategy's own tick size), the run is deterministic, and it passes the truncation
probe (deleting every tick after a cut point must not change anything the
strategy did before it - no look-ahead). The manifest layer must also be able to
fingerprint it, since the run manifests and the trial ledger depend on that.

LIMIT, stated plainly: a strategy that makes no decisions on this data proves
loading, running and determinism only. The tool calls that LOW POWER and so does
this test: it warns for each such strategy, and a separate test fails if the
number of full-power strategies falls (so the data cannot quietly degrade).
Real-data causality for the ones that matter is `tools.causality_check`.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import causality_check as CC  # noqa: E402

from backtester import manifest as M  # noqa: E402
from backtester.contracts import get_spec  # noqa: E402
from backtester.loader import load_strategy_class  # noqa: E402

from conftest import MultiDayCatalog  # noqa: E402
from synth_stream import trending_days  # noqa: E402


def tracked_strategies() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "strategies/*.py"], cwd=REPO,
                             capture_output=True, text=True, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        out = []
    return sorted(out) or sorted(f"strategies/{p.name}" for p in (REPO / "strategies").glob("*.py"))


FILES = tracked_strategies()

# Classes that are frameworks, not strategies: they refuse to run bare and are
# exercised through their subclasses and their own tests. The expected error is
# asserted so a change in behavior shows up.
NEEDS_CONFIG = {
    "strategies/godzilla_killa.py": "order_mode='atm' needs atm_template",
}

POWER = {}          # file -> full-run decision count, filled by the runs below
FULL_POWER = 10     # tools.causality_check.MIN_DECISIONS


def run_probe(path: str):
    cls = load_strategy_class(REPO / path)
    tick = get_spec(cls.symbol).tick_size
    cat = MultiDayCatalog(trending_days(tick))
    return cls, CC.truncation_invariance(lambda: cls(), "20260202", "20260205",
                                         n_cuts=4, seed=0, base_catalog=cat)


def test_the_strategy_list_is_not_empty_and_has_the_shipped_examples():
    names = {Path(f).name for f in FILES}
    assert len(FILES) >= 3
    assert {"ema_cross.py", "band_fade.py", "mtf_example.py"} <= names


@pytest.mark.parametrize("path", FILES)
def test_strategy_loads_runs_is_deterministic_and_has_no_lookahead(path):
    if path in NEEDS_CONFIG:
        with pytest.raises(ValueError, match=re.escape(NEEDS_CONFIG[path])):
            run_probe(path)
        return
    cls, rep = run_probe(path)
    POWER[path] = rep.full_decisions
    assert rep.deterministic, f"{path}: two identical runs differ (unseeded randomness?)"
    assert not rep.mismatches, (
        f"{path}: look-ahead? {len(rep.mismatches)} bars changed when the future "
        f"was deleted, first: {rep.mismatches[0]}")
    if rep.low_power:
        warnings.warn(
            f"LOW POWER: {path} submitted orders on only {rep.full_decisions} bars of "
            f"synthetic data, so the truncation probe says little for it "
            f"(run tools.causality_check on real ticks)", UserWarning, stacklevel=1)


@pytest.mark.parametrize("path", FILES)
def test_strategy_can_be_fingerprinted_for_the_manifests_and_the_ledger(path):
    cls = load_strategy_class(REPO / path)
    assert re.fullmatch(r"[0-9a-f]{64}", M.strategy_fingerprint(cls))
    params = M.strategy_params(cls())
    json.dumps(params, default=str)               # must be serialisable into a manifest
    assert params.get("symbol") and params.get("period")


def test_enough_strategies_run_at_full_power_for_the_probe_to_mean_something():
    # runs after the parametrized tests in file order; a lone `-k` run may not
    # have filled POWER, so only judge when most strategies reported
    if len(POWER) < len(FILES) - len(NEEDS_CONFIG):
        pytest.skip("run the whole file: the power census needs every strategy's result")
    full = sorted(Path(p).name for p, n in POWER.items() if n >= FULL_POWER)
    # the public repo ships four example strategies; two of them decide often enough here
    assert len(full) >= 2, f"only {len(full)} strategies have >= {FULL_POWER} decisions: {full}"
