"""backtester/manifest.py: run manifests and the trial ledger (plan tasks 8-10).

The autouse fixture in conftest points the ledger and manifest folder at a tmp
dir for every test, so nothing here can touch reports/trials.jsonl. The
end-to-end tests also redirect the DEFAULT data and cache roots to tmp: the
synthetic day is dated 2025-01-15, which exists in the real repo, and a stray
write would overwrite a real cache file.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent

import backtester.data as D  # noqa: E402
from backtester import manifest as M  # noqa: E402
from backtester import metrics, sweep as sweep_mod, walkforward as wf  # noqa: E402
from backtester.account import PropFirmConfig  # noqa: E402
from backtester.engine import Backtest  # noqa: E402
from backtester.loader import load_strategy, load_strategy_class  # noqa: E402

from conftest import write_synthetic_raw  # noqa: E402

SRC = textwrap.dedent('''
    from backtester import Strategy

    class ManifestProbe(Strategy):
        symbol = "MNQ"
        period = "30s"
        session = None
        qty = 1
        stop = 8

        @property
        def twice(self):
            return self.qty * 2

        def helper(self):
            return 1

        def on_start(self):
            self.n = 0                  # runtime state, NOT configuration
            self.scratch = [1, 2, 3]

        def on_bar(self, bar, bars):
            self.n += 1
            if self.flat and bar.index % 7 == 0:
                self.buy_bracket(stop_ticks=self.stop, target_ticks=8)
''')


@pytest.fixture
def world(tmp_path, monkeypatch):
    data, cache = tmp_path / "data", tmp_path / "cache"
    for i, day in enumerate(("2025-01-15", "2025-01-16", "2025-01-17", "2025-01-21",
                             "2025-01-22", "2025-01-23")):
        write_synthetic_raw(data / "MNQ-2025_L1" / f"{day.replace('-', '')}.parquet",
                            seed=i + 1, n_trades=1500, gap_after=700, day=day)
    monkeypatch.setattr(D, "DEFAULT_DATA_ROOT", data)
    monkeypatch.setattr(D, "DEFAULT_CACHE_ROOT", cache)
    strat = tmp_path / "probe_strategy.py"
    strat.write_text(SRC, encoding="utf-8")
    return SimpleNamespace(tmp=tmp_path, data=data, cache=cache, strat=strat)


def make_bt(world, end="20250115", **kw):
    s = load_strategy(world.strat, kw.pop("overrides", None))
    kw.setdefault("prop", PropFirmConfig(threshold=2000.0))
    return Backtest(s, start="20250115", end=end, progress=False, **kw)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ----------------------------------------------------------- the params snapshot

def test_params_are_configuration_only_and_taken_before_the_run(world):
    bt = make_bt(world, overrides={"stop": 12})
    before = M.strategy_params(bt.strategy)
    assert before["stop"] == 12 and before["qty"] == 1 and before["period"] == "30s"
    assert "twice" not in before and "helper" not in before        # property / method
    assert "n" not in before and "scratch" not in before           # no runtime state yet
    assert not any(k.startswith("_") for k in before)
    bt.run()
    after = M.strategy_params(bt.strategy)
    assert "n" in after and "scratch" in after       # what a post-run snapshot would leak
    # and a property that needs the broker must not be touched before it is wired
    assert M.strategy_params(load_strategy(world.strat)) == \
        M.strategy_params(load_strategy(world.strat))


def test_config_sha_is_stable_and_changes_with_each_selection_choice(world):
    base = M.begin_run(make_bt(world), kind="cli")
    assert M.begin_run(make_bt(world), kind="cli").config_sha == base.config_sha
    assert M.begin_run(make_bt(world), kind="sweep").config_sha == base.config_sha   # kind is not config
    for label, bt in {
        "param": make_bt(world, overrides={"stop": 9}),
        "slippage": make_bt(world, slippage_ticks=1.0),
        "prop floor": make_bt(world, prop=PropFirmConfig(threshold=1500.0)),
        "no prop": make_bt(world, prop=None),
        "bar spec": make_bt(world, period="1m"),
        "balance": make_bt(world, start_balance=25_000.0),
        "dll": make_bt(world, daily_loss_limit=300.0),
    }.items():
        assert M.begin_run(bt, kind="cli").config_sha != base.config_sha, label
    bt = make_bt(world)
    bt.end = "20250116"
    assert M.begin_run(bt, kind="cli").config_sha != base.config_sha      # window
    world.strat.write_text(SRC.replace("stop = 8", "stop = 8  # edited"), encoding="utf-8")
    assert M.begin_run(make_bt(world), kind="cli").config_sha != base.config_sha  # source


def test_window_spelling_does_not_make_a_new_configuration(world):
    a, b = make_bt(world), make_bt(world)
    a.start, a.end = "2025-01-15", "2025-01-16"
    b.start, b.end = "20250115", "20250116"
    assert M.begin_run(a, kind="cli").config_sha == M.begin_run(b, kind="cli").config_sha


def test_the_raw_data_is_not_part_of_the_configuration(world):
    a = M.begin_run(make_bt(world), kind="cli")
    D_raw = world.data / "MNQ-2025_L1" / "20250115.parquet"
    write_synthetic_raw(D_raw, seed=99, n_trades=1500, gap_after=700)
    b = M.begin_run(make_bt(world), kind="cli")
    assert b.inputs["raw_sha"] != a.inputs["raw_sha"]           # the data moved...
    assert b.config_sha == a.config_sha                         # ...the config did not


# ---------------------------------------------------------- strategy fingerprint

def test_fingerprint_hashes_the_class_that_ran_without_re_executing_it(tmp_path):
    mod = tmp_path / "fp_count_mod.py"
    mod.write_text("import builtins\nbuiltins._fp_exec = getattr(builtins, '_fp_exec', 0) + 1\n"
                   "from backtester import Strategy\nclass S(Strategy):\n    pass\n",
                   encoding="utf-8")
    import builtins
    builtins._fp_exec = 0
    try:
        cls = load_strategy_class(mod)
        assert builtins._fp_exec == 1
        a = M.strategy_fingerprint(cls)
        assert M.strategy_fingerprint(cls) == a
        assert builtins._fp_exec == 1                    # hashing never re-imports
    finally:
        del builtins._fp_exec
        sys.modules.pop("fp_count_mod", None)


def test_fingerprint_follows_a_project_folder_outside_the_repo(tmp_path):
    """Strategies kept in a project folder outside the repo load their base from a
    sibling file with importlib (never registered in sys.modules); their base
    must still be part of the fingerprint."""
    proj = tmp_path / "NinjaProject"
    proj.mkdir()
    base = proj / "sibling_base_gold.py"
    base.write_text("from backtester import Strategy\nclass Base(Strategy):\n    stop = 8\n",
                    encoding="utf-8")
    (proj / "child_gold.py").write_text(textwrap.dedent('''
        import importlib.util
        from pathlib import Path
        _s = importlib.util.spec_from_file_location(
            "sibling_base_gold_loaded", Path(__file__).with_name("sibling_base_gold.py"))
        _mod = importlib.util.module_from_spec(_s)
        _s.loader.exec_module(_mod)

        class Child(_mod.Base):
            qty = 1
    '''), encoding="utf-8")
    try:
        cls = load_strategy_class(proj / "child_gold.py")
        a = M.strategy_fingerprint(cls)
        base.write_text(base.read_text(encoding="utf-8").replace("8", "9"), encoding="utf-8")
        cls = load_strategy_class(proj / "child_gold.py")
        assert M.strategy_fingerprint(cls) != a
    finally:
        sys.modules.pop("child_gold", None)


def test_fingerprint_finds_a_base_class_that_is_never_named_in_the_module(tmp_path):
    """class C(__import__("base").Base): no global refers to the base module, so
    only the class's own MRO can find it."""
    proj = tmp_path / "Proj2"
    proj.mkdir()
    base = proj / "base_dyn_gold.py"
    base.write_text("from backtester import Strategy\nclass BaseDyn(Strategy):\n    stop = 8\n",
                    encoding="utf-8")
    (proj / "child_dyn_gold.py").write_text(
        "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).parent))\n"
        "class ChildDyn(__import__('base_dyn_gold').BaseDyn):\n    qty = 1\n",
        encoding="utf-8")
    try:
        cls = load_strategy_class(proj / "child_dyn_gold.py")
        a = M.strategy_fingerprint(cls)
        base.write_text(base.read_text(encoding="utf-8").replace("8", "9"), encoding="utf-8")
        assert M.strategy_fingerprint(cls) != a
    finally:
        for m in ("child_dyn_gold", "base_dyn_gold"):
            sys.modules.pop(m, None)
        sys.path[:] = [p for p in sys.path if p != str(proj)]


def test_the_ledger_and_manifest_folder_are_isolated_per_test(tmp_path):
    assert os.environ["BACKTESTER_LEDGER"].startswith(str(tmp_path))
    assert os.environ["BACKTESTER_MANIFEST_DIR"].startswith(str(tmp_path))
    assert M.ledger_path() != REPO / "reports" / "trials.jsonl"


# ----------------------------------------------------------------------- ledger

def _rec(kind="cli", family="S:s.py", cfg="c1", sha="x"):
    return M.RunRecord(kind=kind, strategy="S", family=family, strategy_file="",
                       strategy_sha=sha, params={}, config={}, config_sha=cfg,
                       inputs={}, created_utc="t")


def _log(rec, ledger):
    holder = {}

    def make(lines):
        info = {**M._numbering(rec, lines), **M._versions(rec, lines)}
        holder.update(info)
        return {**info, "kind": rec.kind, "family": rec.family,
                "config_sha": rec.config_sha, "strategy_sha": rec.strategy_sha}
    M.append_ledger(make, ledger)
    return holder


def _core(h):
    return {k: h[k] for k in ("seq", "trial", "distinct_configs")}


def test_numbering_counts_trials_and_distinct_configs_per_family(tmp_path):
    led = tmp_path / "l.jsonl"
    assert _core(_log(_rec(cfg="a"), led)) == {"seq": 1, "trial": 1, "distinct_configs": 1}
    assert _core(_log(_rec(cfg="a"), led)) == {"seq": 2, "trial": 2, "distinct_configs": 1}   # verbatim re-run
    assert _core(_log(_rec(cfg="b"), led)) == {"seq": 3, "trial": 3, "distinct_configs": 2}
    assert _log(_rec(kind="sweep", cfg="c"), led)["trial"] == 4          # sweeps count
    assert _log(_rec(kind="walkforward", cfg="d"), led)["trial"] == 5    # so do walk-forwards
    other = _log(_rec(family="T:t.py", cfg="a"), led)
    assert (other["trial"], other["distinct_configs"]) == (1, 1)         # separate family
    ctl = _log(_rec(kind="random_control", cfg="seed7"), led)
    assert ctl["trial"] is None and ctl["seq"] == 7                      # logged, not counted
    assert ctl["distinct_configs"] == 4                                  # and does not inflate N
    after = _log(_rec(cfg="e"), led)
    assert (after["trial"], after["distinct_configs"]) == (6, 5)


def test_a_code_change_starts_a_new_trial_count_but_the_family_total_continues(tmp_path):
    led = tmp_path / "l.jsonl"
    _log(_rec(cfg="a", sha="v1"), led)
    _log(_rec(cfg="b", sha="v1"), led)
    # the strategy's code changed: the same file is now a new testing regime, so
    # the trial count and the distinct-config count start again...
    third = _log(_rec(cfg="c", sha="v2"), led)
    assert (third["trial"], third["distinct_configs"]) == (1, 1)
    # ...while the number of tries across ALL versions keeps running (2 + this)
    assert (third["code_version"], third["code_versions"], third["family_trials"]) == (2, 2, 3)
    # runs of the new version keep counting inside it
    fourth = _log(_rec(cfg="d", sha="v2"), led)
    assert (fourth["trial"], fourth["distinct_configs"], fourth["family_trials"]) == (2, 2, 4)
    # a control is logged but is not a try; it reports the version it ran on
    ctl = _log(_rec(kind="random_control", cfg="s1", sha="v1"), led)
    assert ctl["trial"] is None and ctl["family_trials"] is None
    assert (ctl["code_version"], ctl["code_versions"]) == (1, 2)
    # going back to the old code is its first version again, trial 3 within it
    back = _log(_rec(cfg="e", sha="v1"), led)
    assert (back["trial"], back["code_version"], back["family_trials"]) == (3, 1, 5)
    # the ledger line carries the version fields
    assert M.read_ledger(led)[2]["code_version"] == 2


def test_the_trial_text_names_the_code_version_only_once_there_are_several():
    one = _rec(cfg="a", sha="v1")
    info = {"trial": 2, "distinct_configs": 2, "code_version": 1,
            "code_versions": 1, "family_trials": 2}
    assert M.trial_text(info, one) == "trial #2 of S:s.py (2 distinct configs)"
    info = {"trial": 1, "distinct_configs": 1, "code_version": 2,
            "code_versions": 2, "family_trials": 7}
    assert M.trial_text(info, one) == ("trial #1 of S:s.py (1 distinct configs)"
                                       " - code version 2 of 2; 7 tries across all versions")


def test_a_corrupt_ledger_line_is_skipped_not_fatal(tmp_path):
    led = tmp_path / "l.jsonl"
    _log(_rec(cfg="a"), led)
    with open(led, "a", encoding="utf-8") as fh:
        fh.write("{ not json\n")
    info = _log(_rec(cfg="b"), led)
    assert info["trial"] == 2 and info["seq"] == 2


def test_concurrent_writers_get_unique_gap_free_numbers(tmp_path):
    led = tmp_path / "ledger" / "trials.jsonl"
    code = textwrap.dedent(f'''
        import os, sys
        sys.path.insert(0, {str(REPO)!r})
        from backtester import manifest as M
        rec = M.RunRecord(kind="sweep", strategy="S", family="S:s.py", strategy_file="",
                          strategy_sha="x", params={{}}, config={{}}, config_sha="",
                          inputs={{}}, created_utc="t")
        for i in range(25):
            rec.config_sha = f"{{os.getpid()}}-{{i}}"
            M.append_ledger(lambda lines: {{**M._numbering(rec, lines), "kind": rec.kind,
                            "family": rec.family, "config_sha": rec.config_sha,
                            "strategy_sha": rec.strategy_sha}})
    ''')
    env = {**os.environ, "BACKTESTER_LEDGER": str(led)}
    procs = [subprocess.Popen([sys.executable, "-c", code], env=env) for _ in range(8)]
    assert all(p.wait(timeout=120) == 0 for p in procs)
    lines = M.read_ledger(led)
    assert len(lines) == 200
    assert [l["seq"] for l in lines] == list(range(1, 201))              # written in order
    assert [l["trial"] for l in lines] == list(range(1, 201))
    assert [l["distinct_configs"] for l in lines] == list(range(1, 201))


def test_a_failing_ledger_warns_loudly_and_does_not_kill_the_run(world, capsys, tmp_path):
    bt = make_bt(world)
    rec = M.begin_run(bt, kind="cli")
    res = bt.run()
    bad = tmp_path / "is_a_directory"
    bad.mkdir()
    out = M.finish_run(rec, res, metrics.compute(res), ledger=bad)
    assert out is None
    assert "WARNING: could not write the run manifest/ledger line" in capsys.readouterr().err


# ----------------------------------------------------------------- the default latency

@pytest.mark.real_default_latency
def test_the_default_latency_is_in_the_config_and_off_keeps_the_old_sha(world):
    default = M.begin_run(make_bt(world), kind="cli")
    explicit = M.begin_run(make_bt(world, latency_ms=100.0), kind="cli")
    off = M.begin_run(make_bt(world, latency_ms=0.0), kind="cli")
    assert default.config["latency_ms"] == 100.0
    assert default.config_sha == explicit.config_sha        # a default is the same config as 100
    assert "latency_ms" not in off.config                   # off = the sha it always had
    assert off.config_sha != default.config_sha


@pytest.mark.real_default_latency
def test_cli_runs_at_100_ms_by_default_and_latency_zero_turns_it_off(world, capsys):
    import cli
    argv = [str(world.strat), "--start", "20250115", "--end", "20250115",
            "--mc", "0", "--out", str(world.tmp / "out" / "Probe.html")]
    cli.main(argv)
    assert "Order latency:  100 ms" in capsys.readouterr().out
    cli.main(argv + ["--latency-ms", "0"])
    assert "Order latency:  OFF (0 ms)" in capsys.readouterr().out


# ----------------------------------------------------------------- execution latencies

def test_execution_latencies_are_in_the_config_only_when_set(world):
    base = M.begin_run(make_bt(world), kind="cli")
    for k in ("cancel_latency_ms", "modify_latency_ms", "bracket_latency_ms"):
        assert k not in base.config
    # explicit zeros are the off state: the sha it always had
    zeros = M.begin_run(make_bt(world, cancel_latency_ms=0, modify_latency_ms=0), kind="cli")
    assert zeros.config_sha == base.config_sha
    on = M.begin_run(make_bt(world, cancel_latency_ms=65, modify_latency_ms=25,
                             bracket_latency_ms=182), kind="cli")
    assert (on.config["cancel_latency_ms"], on.config["modify_latency_ms"],
            on.config["bracket_latency_ms"]) == (65.0, 25.0, 182.0)
    # each one alone is a different config; a bracket latency of 0 is SET (not None)
    shas = {M.begin_run(make_bt(world, **{k: v}), kind="cli").config_sha
            for k, v in (("cancel_latency_ms", 65), ("modify_latency_ms", 25),
                         ("bracket_latency_ms", 182), ("bracket_latency_ms", 0))}
    assert len(shas) == 4 and base.config_sha not in shas and on.config_sha not in shas


def test_cli_takes_the_three_execution_latency_flags(world, capsys):
    import cli
    argv = [str(world.strat), "--start", "20250115", "--end", "20250115",
            "--mc", "0", "--out", str(world.tmp / "out" / "Probe.html")]
    cli.main(argv)
    assert "Exec latency" not in capsys.readouterr().out
    cli.main(argv + ["--cancel-latency-ms", "65", "--modify-latency-ms", "25",
                     "--bracket-latency-ms", "182"])
    assert "Exec latency:   cancel 65 ms, modify 25 ms, bracket 182 ms" in capsys.readouterr().out


# ----------------------------------------------------------------- CLI end to end

def test_cli_run_writes_a_manifest_and_a_ledger_line(world, capsys, monkeypatch):
    import cli
    out_html = world.tmp / "out" / "Probe.html"
    argv = [str(world.strat), "--start", "20250115", "--end", "20250115",
            "--mc", "0", "--out", str(out_html)]
    cli.main(argv)
    text = capsys.readouterr().out
    assert "trial #1 of ManifestProbe:probe_strategy.py (1 distinct configs)" in text
    # the CITABLE manifest is the unique one in the manifest folder, and the
    # ledger line points at it; the file next to the tearsheet is a COPY
    uniq = Path(os.environ["BACKTESTER_MANIFEST_DIR"])
    files = sorted(uniq.glob("*.json"))
    assert len(files) == 1 and files[0].name.endswith("_ManifestProbe_MNQ_1.json")
    man_path = files[0]
    assert f"Manifest:       {man_path}" in text and "[seq 1]" in text
    copy = out_html.with_name("Probe_manifest.json")
    assert copy.read_text(encoding="utf-8") == man_path.read_text(encoding="utf-8")
    assert M.read_ledger()[0]["manifest"] == str(man_path)
    m = read(man_path)

    # an independent run of the same thing must agree with the manifest
    bt = make_bt(world)
    res = bt.run()
    st = metrics.compute(res)
    assert m["manifest_version"] == M.MANIFEST_VERSION
    assert m["kind"] == "cli" and m["trial"]["trial"] == 1 and m["trial"]["seq"] == 1
    assert m["engine"]["cache_version"] == D.CACHE_VERSION.decode()
    assert m["engine"]["bars_version"] == D.BARS_VERSION.decode()
    assert "L1 trade events" in m["engine"]["fills"]
    assert m["data"] == M.raw_fingerprint(bt.catalog, "MNQ", "20250115", "20250115")
    assert m["data"]["n_days"] == 1 and len(m["data"]["raw_sha"]) == 64
    assert m["strategy"]["sha"] == M.strategy_fingerprint(type(bt.strategy))
    assert m["strategy"]["params"]["stop"] == 8 and "n" not in m["strategy"]["params"]
    assert m["config"]["symbol"] == "MNQ" and m["config"]["bar_spec"] == "30s"
    assert m["config"]["prop"]["threshold"] == 2000.0
    assert m["config"]["slippage_ticks"] == 0.0
    assert m["results"]["net_pnl"] == round(st["net_pnl"], 2)
    assert m["results"]["total_trades"] == st["total_trades"] > 0
    assert m["results"]["trades_sha"] == M.trades_sha(res.trades)
    assert m["git_sha"] and "dirty" in m
    assert Path(m["ledger"]) == M.ledger_path()

    # re-run: trial 2 of 1 distinct config; a changed slippage: trial 3 of 2
    cli.main(argv)
    assert "trial #2 of ManifestProbe:probe_strategy.py (1 distinct configs)" in capsys.readouterr().out
    # the second run did NOT overwrite the first run's cited manifest
    assert len(sorted(uniq.glob("*.json"))) == 2 and man_path.exists()
    assert read(man_path)["trial"]["trial"] == 1
    cli.main(argv + ["--slippage", "1"])
    assert "trial #3 of ManifestProbe:probe_strategy.py (2 distinct configs)" in capsys.readouterr().out
    led = M.read_ledger()
    assert [l["seq"] for l in led] == [1, 2, 3]
    assert led[0]["config_sha"] == led[1]["config_sha"] != led[2]["config_sha"]


def test_cli_daily_limit_mode_is_part_of_the_configuration_only_when_a_limit_is_set(
        world, capsys):
    import cli
    base = [str(world.strat), "--start", "20250115", "--end", "20250115",
            "--mc", "0", "--no-report"]
    cli.main(base)                                         # no limit
    cli.main(base + ["--daily-loss-limit", "5"])           # default mode: gate
    cli.main(base + ["--daily-loss-limit", "5", "--daily-limit-mode", "kill",
                     "--daily-profit-target", "9"])
    capsys.readouterr()
    cfgs = [l for l in M.read_ledger()]
    uniq = sorted(Path(os.environ["BACKTESTER_MANIFEST_DIR"]).glob("*.json"))
    docs = [read(p) for p in uniq]
    assert "daily_limit_mode" not in docs[0]["config"]      # runs without a limit keep their sha
    assert docs[1]["config"]["daily_limit_mode"] == "gate"
    assert docs[2]["config"]["daily_limit_mode"] == "kill"
    assert docs[2]["config"]["daily_profit_target"] == 9.0
    # the same $5 means a different thing in the two modes: three distinct configs
    assert len({c["config_sha"] for c in cfgs}) == 3


def test_cli_without_a_report_still_writes_the_manifest_to_the_manifest_dir(world, capsys):
    import cli
    cli.main([str(world.strat), "--start", "20250115", "--end", "20250115",
              "--mc", "0", "--no-report"])
    files = list(Path(os.environ["BACKTESTER_MANIFEST_DIR"]).glob("*.json"))
    assert len(files) == 1 and files[0].name.endswith("_ManifestProbe_MNQ_1.json")
    assert "Manifest:" in capsys.readouterr().out


# ------------------------------------------------------------ sweep / walk-forward

class _Serial:
    """In-process stand-in for ProcessPoolExecutor so the test's env is used
    (a forkserver would hand workers a stale environment)."""
    def __init__(self, max_workers=None): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def map(self, fn, it): return [fn(x) for x in it]


def test_sweep_logs_every_combo_and_writes_one_study_manifest(world, monkeypatch):
    monkeypatch.setattr(sweep_mod, "ProcessPoolExecutor", _Serial)
    mp = world.tmp / "sweep_manifest.json"
    rows = sweep_mod.run_sweep(
        str(world.strat), {"stop": [6, 8], "qty": [1, 2]},
        start="20250115", end="20250116", manifest_path=mp)
    assert [r["trial"] for r in rows] == [1, 2, 3, 4]
    led = M.read_ledger()
    assert [l["kind"] for l in led] == ["sweep"] * 4
    assert len({l["config_sha"] for l in led}) == 4
    m = read(mp)
    assert m["kind"] == "sweep" and len(m["rows"]) == 4
    assert m["data"]["n_days"] == 2 and len(m["data"]["raw_sha"]) == 64
    assert m["spec"]["grid"] == {"stop": [6, 8], "qty": [1, 2]}
    assert [r["trial"] for r in m["rows"]] == [1, 2, 3, 4]


def test_walkforward_logs_in_sample_combos_and_oos_runs_as_trials(world, monkeypatch):
    monkeypatch.setattr(sweep_mod, "ProcessPoolExecutor", _Serial)
    mp = world.tmp / "wf_manifest.json"
    windows = wf.run_walkforward(
        str(world.strat), {"stop": [6, 8]}, n_windows=2, ratio=1, min_trades=1,
        data_root=world.data, cache_root=world.cache, manifest_path=mp)
    assert len(windows) == 2
    led = M.read_ledger()
    # per window: 2 in-sample combos + 1 out-of-sample run = 3 counted trials
    assert [l["kind"] for l in led] == ["walkforward"] * 6
    assert [l["trial"] for l in led] == [1, 2, 3, 4, 5, 6]
    assert [w.oos_trial for w in windows] == [3, 6]
    m = read(mp)
    assert m["kind"] == "walkforward" and [r["oos_trial"] for r in m["rows"]] == [3, 6]


def test_random_control_runs_are_logged_but_do_not_count_as_trials(world, monkeypatch):
    sys.path.insert(0, str(REPO / "tools"))
    import random_control as RC
    monkeypatch.setattr(RC, "_build", lambda path, ov, kw, prop: make_bt(world, end="20250117"))
    payload = {"strategy": "unused", "overrides": {}, "bt_kwargs": {},
               "prop_threshold": 2000.0, "direction_method": None}
    # one real trial first
    bt = make_bt(world)
    rec = M.begin_run(bt, kind="cli")
    res = bt.run()
    M.finish_run(rec, res, metrics.compute(res), write_manifest=False)
    out = RC.run_control(payload, seeds=[1, 2, 3, 4, 5], jobs=1)
    led = M.read_ledger()
    ctl = [l for l in led if l["kind"] == "random_control"]
    assert len(ctl) == 6                                   # base + 5 seeds, all logged
    assert all(l["trial"] is None for l in ctl)
    assert {l["extra"]["mode"] for l in ctl} == {"side"}
    assert sorted(l["extra"]["seed"] for l in ctl if l["extra"]["seed"] is not None) == [1, 2, 3, 4, 5]
    nxt = M.begin_run(make_bt(world), kind="cli")
    assert M._numbering(nxt, led)["trial"] == 2            # the controls did not inflate N
    assert out["base"]["inputs"]["raw_sha"] and out["base"]["trades_sha"]


# ------------------------------------------------------------------ dirty flag --

def _git(repo, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def test_dirty_flag_covers_the_root_clis_and_new_engine_files_but_not_wip_strategies(tmp_path):
    repo = tmp_path / "repo"
    for d in ("backtester", "tools", "strategies"):
        (repo / d).mkdir(parents=True)
        (repo / d / "m.py").write_text("x = 1\n", encoding="utf-8")
    for f in ("cli.py", "sweep.py", "walkforward.py", "sweep_confluence.py", "README.md"):
        (repo / f).write_text("x = 1\n", encoding="utf-8")
    _git(repo, "init", "-q"); _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "c")
    assert M.git_state(repo)["dirty"] is False
    assert len(M.git_state(repo)["git_sha"]) == 40
    (repo / "README.md").write_text("changed\n", encoding="utf-8")               # irrelevant
    assert M.git_state(repo)["dirty"] is False
    (repo / "strategies" / "wip.py").write_text("y = 2\n", encoding="utf-8")      # untracked WIP strategy
    assert M.git_state(repo)["dirty"] is False
    for f in ("cli.py", "sweep.py", "walkforward.py", "sweep_confluence.py",
              "backtester/m.py", "tools/m.py", "strategies/m.py"):
        p = repo / f
        old = p.read_text(encoding="utf-8")
        p.write_text(old + "# edit\n", encoding="utf-8")
        assert M.git_state(repo)["dirty"] is True, f
        p.write_text(old, encoding="utf-8")
    (repo / "backtester" / "new_module.py").write_text("z = 3\n", encoding="utf-8")  # NEW engine file
    assert M.git_state(repo)["dirty"] is True
    assert M.git_state(tmp_path / "not-a-repo")["dirty"] is None


# ----------------------------------------------- ad-hoc scripts and the confluence sweep

def test_the_two_line_pattern_for_a_script_that_calls_backtest_directly(world):
    """The pattern for a script that calls Backtest.run() itself: a number from it
    is only reportable if the script brackets the run like this."""
    bt = make_bt(world)
    rec = M.begin_run(bt, kind="cli")                     # line 1: before the run
    res = bt.run()
    m = M.finish_run(rec, res, metrics.compute(res))      # line 2: after it
    assert m["trial"] == 1 and Path(m["path"]).exists()
    assert M.read_ledger()[0]["manifest"] == m["path"]


def test_begin_run_never_raises_and_finish_run_accepts_its_none():
    class Broken:                                         # not a Backtest at all
        pass
    assert M.begin_run(Broken(), kind="cli") is None
    assert M.finish_run(None, object(), {}) is None


def test_headline_carries_day_notes_only_when_a_day_is_incomplete():
    import numpy as np
    from backtester.account import Trade
    t = Trade(direction=1, qty=1, entry_ts=0, exit_ts=60 * 10**9, pnl=25.0)
    res = SimpleNamespace(
        trades=[t], daily_pnl={"20260105": 25.0}, start_balance=10_000.0,
        equity=np.asarray([], dtype=float), total_commission=0.0, drawdown=None,
        prop=None, strategy_name="S", symbol="MNQ", period="1m",
        days=["20260105"], runtime_s=1.0, dll_days=[], halted_on=None)
    assert "day_notes" not in M.headline(res, metrics.compute(res))
    res.day_notes = {"20260106": "incomplete trading day: x"}
    assert M.headline(res, metrics.compute(res))["day_notes"] == \
        {"20260106": "incomplete trading day: x"}
