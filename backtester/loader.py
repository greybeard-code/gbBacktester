"""Load a Strategy subclass from a .py file path."""
from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

from .strategy import Strategy


def load_strategy_class(path: str | Path) -> type[Strategy]:
    p = Path(path).resolve()
    spec = importlib.util.spec_from_file_location(p.stem, p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[p.stem] = mod
    spec.loader.exec_module(mod)
    classes = [c for _, c in inspect.getmembers(mod, inspect.isclass)
               if issubclass(c, Strategy) and c is not Strategy
               and c.__module__ == mod.__name__]
    if not classes:
        raise SystemExit(f"No Strategy subclass found in {p}")
    if len(classes) > 1:
        print(f"Multiple strategies in {p.name}; using {classes[0].__name__}")
    return classes[0]


# Settings the ENGINE reads from the strategy with a default (Backtest resolves
# arg > strategy attribute > default), so a sweep / walk-forward / --param can
# set them on any strategy without the class declaring them.
ENGINE_PARAMS = frozenset({"latency_ms", "cancel_latency_ms", "modify_latency_ms",
                           "bracket_latency_ms", "daily_limit_mode",
                           "daily_loss_limit", "daily_profit_target",
                           "flatten_at_roll", "roll_entry_block_minutes"})


def load_strategy(path: str | Path, overrides: dict | None = None) -> Strategy:
    cls = load_strategy_class(path)
    strat = cls()
    for k, v in (overrides or {}).items():
        if not hasattr(strat, k) and k not in ENGINE_PARAMS:
            raise AttributeError(
                f"{cls.__name__} has no parameter {k!r} — check --param names")
        setattr(strat, k, v)
    return strat
