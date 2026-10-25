"""tools/deflated_sharpe.py: the deflated Sharpe ratio table.

The numbers are hand-computed from the Bailey and Lopez de Prado (2014) formula
    DSR = Phi( (SR - SR0) * sqrt(T - 1) / sqrt(1 - skew*SR + (kurt - 1)/4 * SR^2) )
    SR0 = sqrt(V) * ((1 - g) * Phi^-1(1 - 1/N) + g * Phi^-1(1 - 1/(N*e)))     g = 0.5772157
with SR the PER-DAY Sharpe (the tool takes the annualized one and divides by sqrt(252)),
T the number of days, kurt the NON-excess kurtosis, V the variance of the trial Sharpes
(luck-only: 1/(T-1)). The running example: SR = 0.1 a day, T = 101, normal returns.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import deflated_sharpe as DS  # noqa: E402

SR_DAY = 0.1
ANN = SR_DAY * math.sqrt(252)            # 1.5874508 annualized


# ------------------------------------------------------------ the maths --

def test_expected_max_sharpe_of_one_try_is_zero_and_grows_with_n():
    assert DS.expected_max_sharpe(1, 0.01) == 0.0
    # N = 2: Phi^-1(1/2) = 0 and Phi^-1(1 - 1/(2e)) = Phi^-1(0.81606) = 0.90045, so the
    # factor is g * 0.90045 = 0.51975
    assert DS.expected_max_sharpe(2, 1e-4) == pytest.approx(0.01 * 0.51975, rel=1e-3)
    # N = 100: the factor is about 2.53 (the expected maximum of 100 normals is 2.51)
    assert DS.expected_max_sharpe(100, 1.0) == pytest.approx(2.5306, abs=2e-3)
    vals = [DS.expected_max_sharpe(n, 1.0) for n in (2, 5, 30, 100, 1000)]
    assert vals == sorted(vals) and len(set(vals)) == 5
    # SR0 scales with the standard deviation of the trial Sharpes
    assert DS.expected_max_sharpe(30, 4.0) == pytest.approx(2 * DS.expected_max_sharpe(30, 1.0))
    with pytest.raises(ValueError):
        DS.expected_max_sharpe(0, 1.0)


def test_one_try_normal_returns_is_the_probabilistic_sharpe_ratio():
    # SR = 0.1, T = 101: den = sqrt(1 + (3-1)/4 * 0.01) = 1.0024969, z = 1 / 1.0024969 =
    # 0.99751, Phi(0.99751) = 0.84074
    d, sr0 = DS.deflated_sharpe(ANN, 101, 0.0, 3.0, 1)
    assert d == pytest.approx(0.84074, abs=5e-5) and sr0 == 0.0


def test_negative_skew_lowers_it_and_positive_skew_raises_it():
    # skew -1: den = sqrt(1 + 0.1 + 0.005) = 1.05119, z = 0.95130, Phi = 0.82927
    assert DS.deflated_sharpe(ANN, 101, -1.0, 3.0, 1)[0] == pytest.approx(0.82927, abs=5e-4)
    # skew +1: den = sqrt(1 - 0.1 + 0.005) = 0.95131, z = 1.05119, Phi = 0.85341
    assert DS.deflated_sharpe(ANN, 101, 1.0, 3.0, 1)[0] == pytest.approx(0.85341, abs=5e-4)


def test_fat_tails_lower_it():
    # kurt 9: den = sqrt(1 + (9-1)/4 * 0.01) = sqrt(1.02) = 1.00995, z = 0.99010, Phi = 0.83896
    assert DS.deflated_sharpe(ANN, 101, 0.0, 9.0, 1)[0] == pytest.approx(0.83896, abs=5e-4)


def test_two_tries_use_the_luck_only_variance_by_default():
    # T = 101 so V = 1/100; SR0 = 0.1 * 0.51975 = 0.051975 a day = 0.8250 annualized;
    # z = (0.1 - 0.051975) * 10 / 1.0024969 = 0.47904, Phi = 0.68402
    d, sr0 = DS.deflated_sharpe(ANN, 101, 0.0, 3.0, 2)
    assert d == pytest.approx(0.68402, abs=5e-4) and sr0 == pytest.approx(0.8250, abs=2e-3)


def test_it_falls_as_the_tries_rise_and_a_smaller_spread_helps():
    ds = [DS.deflated_sharpe(ANN, 101, 0.0, 3.0, n)[0] for n in (1, 2, 10, 100, 1000)]
    assert ds == sorted(ds, reverse=True)
    wide = DS.deflated_sharpe(ANN, 101, 0.0, 3.0, 100)[0]
    narrow = DS.deflated_sharpe(ANN, 101, 0.0, 3.0, 100, var=1e-4)[0]     # std 0.01 a day
    assert narrow > wide


def test_more_days_make_the_same_sharpe_more_convincing():
    assert DS.deflated_sharpe(ANN, 400, 0.0, 3.0, 30)[0] > DS.deflated_sharpe(ANN, 101, 0.0, 3.0, 30)[0]


def test_an_impossible_variance_term_gives_nan_and_too_few_days_is_refused():
    d, _ = DS.deflated_sharpe(ANN, 101, 50.0, 3.0, 1)       # 1 - 5 + 0.005 < 0
    assert math.isnan(d)
    with pytest.raises(ValueError):
        DS.deflated_sharpe(ANN, 2, 0.0, 3.0, 1)


# ------------------------------------------------------ counting the tries --

def row(seq, cfg, kind="cli", sha="v1", family="F", sharpe=1.0, days=100):
    return {"seq": seq, "family": family, "kind": kind, "strategy_sha": sha, "config_sha": cfg,
            "created_utc": f"2026-10-{seq:02d}T00:00:00+00:00",
            "results": {"sharpe": sharpe, "n_days": days, "skew": 0.0, "kurt": 3.0}}


LEDGER = [row(1, "A"), row(2, "B", kind="sweep"), row(3, "A"),                   # A twice, B once
          row(4, "C", sha="v2"), row(5, "D", kind="random_control"),                # other version; a control
          row(6, "E", family="OTHER"), row(7, "F", kind="walkforward")]


def test_counts_distinct_configs_per_code_version_and_across_versions():
    assert DS.counts(LEDGER, "F", "v1", None) == (3, 4)     # v1: A, B, F; all: A, B, F, C
    assert DS.counts(LEDGER, "F", "v2", None) == (1, 4)
    assert DS.counts(LEDGER, "F", "v1", "NEW") == (4, 5)    # a result not in the ledger yet counts
    assert DS.counts(LEDGER, "F", "v1", "A") == (3, 4)      # ... once
    assert DS.counts(LEDGER, "OTHER", "v1", None) == (1, 1)      # another family is counted apart
    assert DS.counts(LEDGER, "OTHER", "v9", None) == (0, 1)      # a code version with no tries yet


def test_the_table_rows_merge_duplicates_and_add_prior_work():
    assert DS.n_values(3, 4, 0) == [
        ("one try (probabilistic Sharpe)", 1), ("ledger, this code version", 3),
        ("ledger, all code versions", 4), ("x10", 40), ("x100", 400)]
    assert [n for _, n in DS.n_values(3, 3, 0)] == [1, 3, 30, 300]       # no second ledger row
    rows = DS.n_values(3, 4, 40)
    assert [n for _, n in rows] == [1, 43, 44, 440, 4400]
    assert rows[1][0] == "ledger, this code version + 40 prior"
    assert [n for _, n in DS.n_values(1, 1, 0)] == [1, 10, 100]          # a duplicate of 1 is merged


# --------------------------------------------------------- the command line --

def ledger_file(tmp_path, rows):
    p = tmp_path / "trials.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return str(p)


def cell(text, n):
    """The DSR printed on the table row for N = n."""
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0] == f"{n:,}":
            return float(parts[2])
    raise AssertionError(f"no row for N={n}:\n{text}")


def test_a_ledger_line_gives_the_table_with_the_ledger_counts(tmp_path, capsys):
    rows = [row(1, "A", sharpe=ANN, days=101), row(2, "B", sharpe=1.2, days=101),
            row(3, "C", sharpe=1.1, days=101)]
    assert DS.main(["--seq", "1", "--ledger", ledger_file(tmp_path, rows)]) == 0
    out = capsys.readouterr().out
    assert "ledger seq 1 (cli)" in out
    assert "Sharpe 1.587 (annualized), 101 days, skew +0.00, kurtosis 3.0" in out
    assert "3 distinct configs of this code version, 3 across all versions" in out
    assert "ledger starts 2026-10-01" in out
    assert cell(out, 1) == pytest.approx(0.841, abs=1e-3)           # the probabilistic Sharpe
    d3 = DS.deflated_sharpe(ANN, 101, 0.0, 3.0, 3)[0]
    assert cell(out, 3) == pytest.approx(d3, abs=1e-3)
    assert cell(out, 30) < cell(out, 3) < cell(out, 1)


def test_prior_trials_move_the_ledger_rows(tmp_path, capsys):
    rows = [row(1, "A", sharpe=ANN, days=101)]
    DS.main(["--seq", "1", "--prior-trials", "40", "--ledger", ledger_file(tmp_path, rows)])
    out = capsys.readouterr().out
    assert "ledger, this code version + 40 prior" in out
    assert cell(out, 41) == pytest.approx(DS.deflated_sharpe(ANN, 101, 0.0, 3.0, 41)[0], abs=1e-3)
    assert "work before that is not counted" not in out


def test_manual_input_needs_no_ledger_and_takes_the_tries_given(tmp_path, capsys):
    DS.main(["--sharpe", str(ANN), "--days", "101", "--trials", "2",
             "--ledger", str(tmp_path / "none.jsonl")])
    out = capsys.readouterr().out
    assert "manual input" in out and "tries given (--trials)" in out
    assert cell(out, 2) == pytest.approx(0.684, abs=1e-3)
    DS.main(["--sharpe", str(ANN), "--days", "101", "--ledger", str(tmp_path / "none.jsonl")])
    out = capsys.readouterr().out
    assert [cell(out, n) for n in (1, 10, 100)] == sorted([cell(out, n) for n in (1, 10, 100)],
                                                          reverse=True)


def test_a_manifest_adds_its_own_config_to_the_count(tmp_path, capsys):
    doc = {"config_sha": "NEW", "strategy": {"family": "F", "sha": "v1"},
           "results": {"sharpe": ANN, "n_days": 101, "skew": 0.0, "kurt": 3.0}}
    mp = tmp_path / "m.json"
    mp.write_text(json.dumps(doc), encoding="utf-8")
    DS.main(["--manifest", str(mp), "--ledger", ledger_file(tmp_path, LEDGER)])
    out = capsys.readouterr().out
    assert "4 distinct configs of this code version, 5 across all versions" in out


def test_missing_skew_and_kurtosis_fall_back_to_a_normal_with_a_note(tmp_path, capsys):
    r = row(1, "A", sharpe=ANN, days=101)
    r["results"]["skew"] = r["results"]["kurt"] = None
    DS.main(["--seq", "1", "--ledger", ledger_file(tmp_path, [r])])
    out = capsys.readouterr().out
    assert "a normal distribution (0, 3) is assumed" in out
    assert cell(out, 1) == pytest.approx(0.841, abs=1e-3)


def test_trial_sharpe_std_replaces_the_luck_only_variance(tmp_path, capsys):
    led = ledger_file(tmp_path, [row(1, "A", sharpe=ANN, days=101)])
    DS.main(["--seq", "1", "--trial-sharpe-std", "0.5", "--ledger", led])
    out = capsys.readouterr().out
    assert "trial Sharpe std 0.50 (annualized)" in out
    narrow = DS.deflated_sharpe(ANN, 101, 0.0, 3.0, 10, var=(0.5 / math.sqrt(252)) ** 2)[0]
    assert cell(out, 10) == pytest.approx(narrow, abs=1e-3)
    # the luck-only value passed explicitly changes nothing: std = sqrt(252 / (T - 1))
    DS.main(["--seq", "1", "--ledger", led])
    default = capsys.readouterr().out
    DS.main(["--seq", "1", "--trial-sharpe-std", str(math.sqrt(252 / 100)), "--ledger", led])
    same = capsys.readouterr().out
    assert cell(same, 10) == cell(default, 10)


@pytest.mark.parametrize("argv, msg", [
    ([], "give --seq"),
    (["--seq", "9"], "no ledger line with seq 9"),
    (["--sharpe", "1", "--days", "100", "--prior-trials", "-1"], "prior-trials"),
    (["--sharpe", "1", "--days", "100", "--trial-sharpe-std", "0"], "trial-sharpe-std"),
])
def test_bad_input_is_refused_with_a_reason(tmp_path, capsys, argv, msg):
    with pytest.raises(SystemExit) as e:
        DS.main(argv + ["--ledger", ledger_file(tmp_path, [row(1, "A")])])
    assert e.value.code == 2 and msg in capsys.readouterr().err
