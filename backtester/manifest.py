"""Run provenance: a manifest per run and a ledger of trials.

A number in a write-up that cites no manifest did not come from this engine.
Every run started from the CLI, a sweep, a walk-forward or a control tool
writes (1) a JSON manifest - git SHA and dirty flag, cache/bar versions, the
raw-data fingerprint, the strategy and its parameters, the bar spec, window,
costs, prop floor, the trial number, the headline results and a hash of the
whole trade list - and (2) one line in an append-only ledger
(trials.jsonl) so the number of variants tried is always known. That count is
what any later deflated Sharpe has to be judged against. Both live in ONE folder
per user, outside any checkout (data_home(): ~/.local/share/gbbacktester), so a clone
or a worktree shares the count instead of starting at trial 1. The ledger is the only
record of how many variants were tried, so keep a copy of that folder somewhere safe.

What counts as a trial: only runs that select among variants - `cli`, `sweep`
and `walkforward` (a walk-forward's in-sample grid AND its out-of-sample run).
Controls (random-direction seeds) are logged but are not tries at a strategy
variant. "Trial N" is the N-th counted run of a strategy FAMILY (class name +
file; every instrument, window and parameter set counts, which is the
conservative reading); "K distinct configs" is how many different
configurations those N runs were (a verbatim re-run does not inflate it).

Hooks are explicit calls from the entry points, never inside Backtest.run: a
hook there would make every unit test and every causality-check cut write to
the real ledger. Failure to write a manifest or ledger line prints a loud
warning and never kills a long run.

The fingerprint helpers (raw data, strategy, trades) live here so every tool that
needs one computes it the same way, byte for byte.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import os
import subprocess
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from .data import BARS_VERSION, CACHE_VERSION

REPO = Path(__file__).resolve().parent.parent
MANIFEST_VERSION = 1
COUNTED_KINDS = ("cli", "sweep", "walkforward")


# ----------------------------------------------------------------------------
# hashing and fingerprints (do not change the formats: stored manifests and
# ledger lines are compared against them)
# ----------------------------------------------------------------------------

def _sha(text: str | bytes) -> str:
    if isinstance(text, str):
        text = text.encode()
    return hashlib.sha256(text).hexdigest()


def kv_sha(meta: dict | None) -> str:
    items = sorted((bytes(k), bytes(v)) for k, v in (meta or {}).items())
    return _sha(b"\n".join(k + b"=" + v for k, v in items))


def fingerprint_line(date: str, rel_path: str, size: int, num_rows: int,
                     kv: str) -> str:
    return f"{date}|{rel_path}|{size}|{num_rows}|{kv}"


def raw_fingerprint(catalog, symbol: str, start: str | None,
                    end: str | None) -> dict:
    """Fingerprint of the raw day files the engine would read for this window.
    The day list comes from Catalog.days, the same call the engine makes."""
    days = catalog.days(symbol, start, end)
    files = catalog.day_files(symbol)
    lines = []
    for d in days:
        p = files[d]
        pf = pq.ParquetFile(p)
        rel = p.relative_to(catalog.data_root).as_posix()
        lines.append(fingerprint_line(d, rel, p.stat().st_size, pf.metadata.num_rows,
                                      kv_sha(pf.schema_arrow.metadata)))
    return {"raw_sha": _sha("\n".join(lines)), "n_days": len(days),
            "first_day": days[0] if days else None,
            "last_day": days[-1] if days else None}


def strategy_fingerprint(cls, extra_dirs=(), repo: Path | None = None) -> str:
    """sha256 of the strategy file plus every module it depends on.

    `cls` is the Strategy class that actually RAN (not re-imported from a path:
    that would re-execute the module and could hash a file edited mid-sweep).
    Dependencies are found by walking the module graph from the strategy itself
    (module globals that are modules, the modules of the classes/functions it
    references, base classes), NOT by scanning sys.modules: that would hash
    whatever else the process happened to import. Several strategies load their
    base with importlib and never register it in sys.modules (`_mod =
    module_from_spec(...)`); the `_mod` global finds it. A module counts as a
    dependency if it lives under the repo's strategies/ folder, under the
    strategy file's own folder (a strategy kept in a project folder outside the
    repo uses the same sibling-loading pattern), or under `extra_dirs`."""
    repo = Path(repo) if repo else REPO
    root_mod = sys.modules.get(cls.__module__)
    root = Path(root_mod.__file__).resolve()
    allowed = [(repo / "strategies").resolve(), root.parent,
               *[Path(d).resolve() for d in extra_dirs]]
    seen: dict[Path, types.ModuleType] = {}

    def visit(mod):
        f = getattr(mod, "__file__", None)
        if not f:
            return
        fp = Path(f).resolve()
        if fp in seen or (fp != root and not any(d in fp.parents for d in allowed)):
            return
        seen[fp] = mod
        for v in list(vars(mod).values()):
            m = v if isinstance(v, types.ModuleType) else \
                sys.modules.get(getattr(v, "__module__", None) or "")
            if m is not None and m is not mod:
                visit(m)
        for base in getattr(cls, "__mro__", ()) if fp == root else ():
            bm = sys.modules.get(base.__module__)
            if bm is not None and bm is not mod:
                visit(bm)

    visit(root_mod)
    parts = [f"{f.relative_to(repo).as_posix() if repo in f.parents else f.name}"
             f"|{_sha(f.read_bytes())}" for f in sorted(seen)]
    return _sha("\n".join(parts))


def trades_sha(trades) -> str:
    """Deterministic hash of the whole trade list (strings, never raw floats)."""
    lines = [f"{t.entry_ts}|{t.exit_ts}|{t.direction}|{t.qty}|"
             f"{t.entry_price:.6f}|{t.exit_price:.6f}|{t.exit_tag}|{t.pnl:.2f}"
             for t in trades]
    return _sha("\n".join(lines))


# what can change a run's numbers: engine, tools, strategies, and the root CLIs
_TRACKED_PATHS = ("backtester", "tools", "strategies", "cli.py", "sweep.py",
                  "walkforward.py", "sweep_confluence.py")
# NEW files only matter here when they are engine/tool code (an untracked
# work-in-progress strategy does not affect a run that does not use it, and the
# strategy that did run is covered by strategy_sha)
_UNTRACKED_PATHS = ("backtester", "tools")


def git_state(repo: Path | None = None) -> dict:
    repo = Path(repo) if repo else REPO

    def run(*args):
        try:
            return subprocess.check_output(["git", *args], cwd=repo, text=True,
                                           stderr=subprocess.DEVNULL).strip()
        except Exception:
            return "unknown"

    sha = run("rev-parse", "HEAD")
    modified = run("status", "--porcelain", "--untracked-files=no",
                   "--", *_TRACKED_PATHS)
    new = run("ls-files", "--others", "--exclude-standard", "--", *_UNTRACKED_PATHS)
    if "unknown" in (modified, new):
        return {"git_sha": sha, "dirty": None}
    return {"git_sha": sha, "dirty": bool(modified or new)}


# ----------------------------------------------------------------------------
# configuration snapshot (taken BEFORE the run)
# ----------------------------------------------------------------------------

def strategy_params(strategy) -> dict:
    """Public, non-callable, JSON-able configuration of a strategy: class
    attributes along the MRO plus instance overrides. Read from __dict__s, never
    through getattr, so properties (flat, position, avg_price ...) are not
    triggered before the broker is wired. Must run BEFORE bt.run(): on_start
    creates instance state (indicators, RNGs) that is not configuration."""
    cls = type(strategy)
    names = set(vars(strategy))
    for k in cls.__mro__:
        if k is not object:
            names.update(vars(k))
    out = {}
    for name in sorted(names):
        if name.startswith("_"):
            continue
        if name in vars(strategy):
            v = vars(strategy)[name]
        else:
            holder = next(k for k in cls.__mro__ if name in vars(k))
            v = vars(holder)[name]
            if isinstance(v, (property, classmethod, staticmethod, types.FunctionType)):
                continue
        if callable(v):
            continue
        if isinstance(v, (int, float, str, bool, type(None), tuple, list, dict,
                          set, frozenset)):
            out[name] = v
    return out


def _json_default(o):
    if isinstance(o, (set, frozenset)):
        return sorted(o, key=str)
    return str(o)


def _sanitize(o):
    """Strict JSON: NaN/inf -> None, recursively."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _sanitize(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_sanitize(v) for v in o]
    return o


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=_json_default)


def family_of(strategy) -> str:
    mod = sys.modules.get(type(strategy).__module__)
    fn = Path(getattr(mod, "__file__", "") or "").name
    return f"{type(strategy).__name__}:{fn}"


@dataclass
class RunRecord:
    kind: str
    strategy: str
    family: str
    strategy_file: str
    strategy_sha: str
    params: dict
    config: dict
    config_sha: str
    inputs: dict
    created_utc: str
    extra: dict = field(default_factory=dict)


def begin_run(bt, *, kind: str, inputs: dict | None = None,
              extra: dict | None = None) -> RunRecord | None:
    """Capture configuration and inputs. Call BEFORE bt.run().

    `inputs` may be passed in already computed (a sweep fingerprints the raw
    data once in the parent, not once per worker). Returns None, after a loud
    warning, if anything goes wrong: bookkeeping must never kill a long run
    (finish_run(None, ...) is a no-op)."""
    try:
        return _begin_run(bt, kind=kind, inputs=inputs, extra=extra)
    except Exception as e:
        print(f"WARNING: could not capture the run configuration for the manifest: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return None


def _begin_run(bt, *, kind: str, inputs: dict | None,
               extra: dict | None) -> RunRecord:
    strat = bt.strategy
    mod = sys.modules.get(type(strat).__module__)
    sfile = str(Path(getattr(mod, "__file__", "") or "").resolve())
    params = strategy_params(strat)
    prop = None
    if bt.prop is not None:
        c = bt.prop.cfg
        prop = {"threshold": c.threshold, "lock_buffer": c.lock_buffer,
                "lock": c.lock, "halt_on_breach": c.halt_on_breach}
    config = {
        "symbol": bt.symbol, "bar_spec": bt.barspec.key,
        "session": list(strat.session) if strat.session else None,
        # 2026-06-01 and 20260601 are the same window: one config, not two
        "start": bt.start.replace("-", "") if bt.start else None,
        "end": bt.end.replace("-", "") if bt.end else None,
        "start_balance": bt.start_balance,
        "slippage_ticks": round(bt.broker.slippage / bt.spec.tick_size, 9),
        "prop": prop, "daily_loss_limit": bt.daily_loss_limit,
        "daily_profit_target": bt.daily_profit_target,
        "max_position": bt.broker.max_position,
    }
    # the SAME dollar limit means different things in the two modes, so the mode
    # is part of the configuration - but only when a limit is set, so runs
    # without one keep their config_sha
    if bt.daily_loss_limit is not None or bt.daily_profit_target is not None:
        config["daily_limit_mode"] = bt.daily_limit_mode
    if bt.latency_ms:
        config["latency_ms"] = bt.latency_ms          # off = the sha it always had
    # execution latencies: only when set, so runs without them keep their config_sha
    if bt.cancel_latency_ms:
        config["cancel_latency_ms"] = bt.cancel_latency_ms
    if bt.modify_latency_ms:
        config["modify_latency_ms"] = bt.modify_latency_ms
    if bt.bracket_latency_ms is not None:
        config["bracket_latency_ms"] = bt.bracket_latency_ms
    # the roll schedule a run can see (off = the sha it always had): the dates
    # inside its window, so extending the calendar elsewhere changes nothing
    if bt.roll_entry_block_minutes > 0:
        config["roll_entry_block_minutes"] = bt.roll_entry_block_minutes   # off = the sha it had
    if bt.flatten_at_roll or bt.roll_entry_block_minutes > 0:
        cal = bt.roll_calendar
        config["roll_flatten"] = (cal.window_sha(bt.symbol, bt.start, bt.end)
                                  if cal is not None and cal.covers(bt.symbol)
                                  else "no-calendar")
    ssha = strategy_fingerprint(type(strat))
    if inputs is None:
        try:
            inputs = raw_fingerprint(bt.catalog, bt.symbol, bt.start, bt.end)
        except Exception as e:                       # stub catalog, no data root
            inputs = {"raw_sha": None, "n_days": None, "first_day": None,
                      "last_day": None, "note": f"unavailable: {e}"[:200]}
    components = {"strategy_sha": ssha, "params": params, "config": config}
    return RunRecord(
        kind=kind, strategy=type(strat).__name__, family=family_of(strat),
        strategy_file=sfile, strategy_sha=ssha, params=params, config=config,
        config_sha=_sha(canonical(components)), inputs=dict(inputs),
        created_utc=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        extra=dict(extra or {}))


# ----------------------------------------------------------------------------
# results
# ----------------------------------------------------------------------------

def _num(x):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else x


def _moments(daily: list[float], balance: float) -> tuple:
    """(skew, non-excess kurtosis) of daily returns, for a later deflated Sharpe."""
    x = np.asarray(daily, dtype=float) / balance
    if len(x) < 4 or x.std() == 0:
        return None, None
    z = (x - x.mean()) / x.std()
    return float((z ** 3).mean()), float((z ** 4).mean())


def headline(result, stats) -> dict:
    skew, kurt = _moments(list(result.daily_pnl.values()), result.start_balance)
    notes = dict(getattr(result, "day_notes", None) or {})
    rolls = list(getattr(result, "rolls", None) or [])
    roll_notes = list(getattr(result, "roll_notes", None) or [])
    return {
        "net_pnl": round(stats["net_pnl"], 2),
        "total_trades": int(stats["total_trades"]),
        "sharpe": _num(round(stats["sharpe"], 6)),
        "max_drawdown": round(stats["max_drawdown"], 2),
        "prop_min_headroom": _num(round(stats["prop_min_headroom"], 2))
        if "prop_min_headroom" in stats else None,
        "profit_factor": _num(round(stats["profit_factor"], 6)),
        "n_days": int(stats["trading_days"]),
        "skew": skew, "kurt": kurt,
        "trades_sha": trades_sha(result.trades),
        # only when a day is not a whole session (data started/ended mid-session)
        **({"day_notes": notes} if notes else {}),
        # contract rolls the run crossed, and what it knew was wrong with them
        **({"rolls": {"in_run": len(rolls),
                      "flattened": [r["date"] for r in rolls if r["flattened"]]}}
           if rolls else {}),
        **({"roll_notes": roll_notes} if roll_notes else {}),
        **({"exits_dropped": int(result.exits_dropped)}
           if getattr(result, "exits_dropped", 0) else {}),
        **({"roll_entry_block_minutes": result.roll_entry_block_minutes,
            "roll_entries_refused": int(result.roll_entries_refused)}
           if getattr(result, "roll_entry_block_minutes", 0) else {}),
        **({"flatten_at_roll": False}
           if getattr(result, "flatten_at_roll", True) is False else {}),
    }


# ----------------------------------------------------------------------------
# ledger
# ----------------------------------------------------------------------------

def data_home() -> Path:
    """The folder the ledger and the manifests live in by default: one per user,
    outside any checkout (XDG_DATA_HOME, else ~/.local/share; %LOCALAPPDATA% on
    Windows). A default INSIDE the repo meant every clone and worktree began an
    empty ledger, which silently restarted the trial count."""
    base = os.environ.get("XDG_DATA_HOME")
    if not base and os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
    return Path(base or Path.home() / ".local" / "share") / "gbbacktester"


def ledger_path() -> Path:
    return Path(os.environ.get("BACKTESTER_LEDGER") or data_home() / "trials.jsonl")


def manifest_dir() -> Path:
    return Path(os.environ.get("BACKTESTER_MANIFEST_DIR") or data_home() / "manifests")


def _lock(fh):
    try:
        import fcntl
        fcntl.flock(fh, fcntl.LOCK_EX)
    except ImportError:          # Windows: single-writer assumption
        pass


def append_ledger(make_entry, path: Path | None = None) -> dict:
    """Atomically assign the next sequence number / trial number and append.
    `make_entry(existing_lines) -> dict` runs under an exclusive file lock, so
    concurrent sweep workers get unique, gap-free numbers."""
    path = Path(path) if path else ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as fh:
        _lock(fh)
        fh.seek(0)
        lines = []
        for raw in fh:
            raw = raw.strip()
            if raw:
                try:
                    lines.append(json.loads(raw))
                except ValueError:
                    pass
        entry = make_entry(lines)
        fh.seek(0, 2)
        fh.write(json.dumps(entry, sort_keys=True, default=_json_default) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return entry


def read_ledger(path: Path | None = None) -> list[dict]:
    """The ledger lines. A line that does not parse (a write cut short) is skipped
    with a warning, exactly as append_ledger skips it, so the two never disagree about
    what is in the ledger."""
    path = Path(path) if path else ledger_path()
    if not path.exists():
        return []
    out, bad = [], 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                bad += 1
    if bad:
        print(f"WARNING: {bad} unreadable line(s) in {path} skipped",
              file=sys.stderr)
    return out


def _numbering(rec: RunRecord, lines: list[dict]) -> dict:
    """seq / trial / distinct_configs, counted WITHIN one code version.

    A testing regime (user definition, 2026-10-18) is iterations on TP, SL,
    allowed times and days and any setting inside the strategy, over any symbol
    or bar type; a CODE change starts a new regime. The code version is
    `strategy_sha` (a hash of the strategy module and the modules it imports)."""
    counted = rec.kind in COUNTED_KINDS
    ver = [l for l in lines if l.get("family") == rec.family
           and l.get("kind") in COUNTED_KINDS
           and l.get("strategy_sha") == rec.strategy_sha]
    cfgs = {l["config_sha"] for l in ver} | ({rec.config_sha} if counted else set())
    return {"seq": len(lines) + 1,
            "trial": len(ver) + 1 if counted else None,
            "distinct_configs": len(cfgs)}


def _versions(rec: RunRecord, lines: list[dict]) -> dict:
    """Which code version of this strategy file this run is, and the total tries
    across ALL versions (the conservative count a deflated Sharpe may prefer).
    A code edit that merely changed a default constant also shows up as a new
    version, because the hash cannot tell a setting from logic: judge by whether
    the earlier tests had to be redone."""
    counted = rec.kind in COUNTED_KINDS
    fam = [l for l in lines if l.get("family") == rec.family
           and l.get("kind") in COUNTED_KINDS]
    shas: list = []
    for l in fam:
        if l.get("strategy_sha") not in shas:
            shas.append(l.get("strategy_sha"))
    if rec.strategy_sha not in shas:
        shas.append(rec.strategy_sha)
    return {"code_version": shas.index(rec.strategy_sha) + 1,
            "code_versions": len(shas),
            "family_trials": len(fam) + 1 if counted else None}


def trial_text(info: dict, rec: RunRecord) -> str:
    if info.get("trial") is None:
        return (f"logged as '{rec.kind}' (a control, not counted as a trial); "
                f"{info['distinct_configs']} distinct configs of {rec.family} tried")
    text = (f"trial #{info['trial']} of {rec.family} "
            f"({info['distinct_configs']} distinct configs)")
    if info.get("code_versions", 1) > 1:
        text += (f" - code version {info['code_version']} of "
                 f"{info['code_versions']}; {info['family_trials']} tries "
                 f"across all versions")
    return text


# ----------------------------------------------------------------------------
# finishing a run
# ----------------------------------------------------------------------------

def _stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def finish_run(rec: RunRecord | None, result, stats, *, manifest_path=None,
               write_manifest: bool = True, ledger: Path | None = None) -> dict | None:
    """Append the ledger line and (optionally) write the manifest JSON.

    The manifest is ALWAYS written to a unique file,
    manifest_dir()/<UTC stamp>_<strategy>_<symbol>_<seq>.json, and the ledger
    line records that path, so a cited manifest can never be overwritten by
    the next run and a ledger row leads to its manifest. `manifest_path`
    (e.g. next to the tearsheet) gets an extra convenience COPY.

    Returns {"trial", "seq", "distinct_configs", "path", "copy", "text"} or None
    if anything failed (after a loud warning: a two-hour sweep must not die over
    its bookkeeping)."""
    if rec is None:
        return None
    try:
        res = headline(result, stats)
        git = git_state()
        holder: dict = {}

        def make(lines):
            info = {**_numbering(rec, lines), **_versions(rec, lines)}
            holder.update(info)
            holder["unique"] = (
                str(manifest_dir() / f"{_stamp()}_{rec.strategy}_"
                                     f"{rec.config['symbol']}_{info['seq']}.json")
                if write_manifest else None)
            return {**info, "manifest": holder["unique"],
                    "created_utc": rec.created_utc, "kind": rec.kind,
                    "family": rec.family, "strategy": rec.strategy,
                    "strategy_sha": rec.strategy_sha, "config_sha": rec.config_sha,
                    "components": {"params": rec.params, "config": rec.config},
                    "results": res, **git, "extra": rec.extra}
        append_ledger(make, ledger)
        info = {k: holder[k] for k in ("seq", "trial", "distinct_configs",
                                        "code_version", "code_versions",
                                        "family_trials")}
        path = copy = None
        if write_manifest:
            path = Path(holder["unique"])
            doc = {
                "manifest_version": MANIFEST_VERSION, "created_utc": rec.created_utc,
                "kind": rec.kind, "trial": info, **git,
                "engine": {"cache_version": CACHE_VERSION.decode(),
                           "bars_version": BARS_VERSION.decode(),
                           "fills": "L1 trade events (broker.resolve_span)"},
                "data": rec.inputs,
                "strategy": {"name": rec.strategy, "family": rec.family,
                             "file": rec.strategy_file, "sha": rec.strategy_sha,
                             "params": rec.params},
                "config": rec.config, "config_sha": rec.config_sha,
                "results": res, "extra": rec.extra,
                "ledger": str(ledger or ledger_path()),
            }
            text = json.dumps(_sanitize(doc), indent=2, sort_keys=True,
                              default=_json_default) + "\n"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            if manifest_path:
                copy = Path(manifest_path)
                copy.parent.mkdir(parents=True, exist_ok=True)
                copy.write_text(text, encoding="utf-8")
        return {**info, "path": str(path) if path else None,
                "copy": str(copy) if copy else None,
                "text": trial_text(info, rec)}
    except Exception as e:                                   # never kill the run
        print(f"WARNING: could not write the run manifest/ledger line: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return None


def write_study_manifest(path, kind: str, spec: dict, inputs: dict,
                         rows: list[dict]) -> Path | None:
    """One manifest per sweep / walk-forward invocation: the grid, the data it
    ran on, and per-row results with their ledger trial numbers."""
    try:
        path = Path(path) if path else manifest_dir() / f"{_stamp()}_{kind}.json"
        doc = {"manifest_version": MANIFEST_VERSION,
               "created_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
               "kind": kind, **git_state(),
               "engine": {"cache_version": CACHE_VERSION.decode(),
                          "bars_version": BARS_VERSION.decode(),
                          "fills": "L1 trade events (broker.resolve_span)"},
               "data": inputs, "spec": spec, "rows": rows,
               "ledger": str(ledger_path())}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_sanitize(doc), indent=2, sort_keys=True,
                                   default=_json_default) + "\n", encoding="utf-8")
        return path
    except Exception as e:
        print(f"WARNING: could not write the {kind} manifest: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return None
