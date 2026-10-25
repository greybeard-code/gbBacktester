"""Deflated Sharpe ratio (Bailey and Lopez de Prado, 2014) for a backtest result, as a
table over the number of tries.

    python3 -m tools.deflated_sharpe --seq 21                      # a ledger line
    python3 -m tools.deflated_sharpe --manifest reports/manifests/<file>.json
    python3 -m tools.deflated_sharpe --sharpe 2.05 --days 425 --skew 0.14 --kurt 14.0 [--trials 30]
    options: --prior-trials N   tries made before the ledger existed (2026-10-18)
             --trial-sharpe-std X  annualized spread of the trial Sharpes (default: luck-only)
             --ledger PATH      (default reports/trials.jsonl)

A plain Sharpe ratio does not know how many variants were tried before this one was
picked. The deflated Sharpe is the probability that the TRUE Sharpe is above what the
best of N luck-only trials would show (their expected maximum, "SR0"). Read it as: near
1.00 survives N tries, about 0.50 is no better than the best of N lucky trials, near 0.00
is what luck alone produces. It is ADVISORY: N cannot be known, so the output is a table
over N, never one number.

Inputs (all kept by the ledger / manifest): the annualized Sharpe, the number of daily
observations T, and the skew and (non-excess) kurtosis of the daily returns. The variance
of the trial Sharpes is by default the luck-only value 1/(T-1) per day, the conservative
choice. If you have a real estimate (the spread of the Sharpes across a sweep of
independent alternatives on the same days), pass it as --trial-sharpe-std (annualized).
The ledger's own Sharpes are NOT used for it: they are mostly neighbouring settings and
walk-forward windows of other lengths, which would understate the spread and make the
result look safer than it is.

Limits to keep in mind: the formula treats the tries as independent, and neighbouring
parameter values are correlated, so the effective N is lower than the count; and the
ledger only starts on 2026-10-18, so older work is invisible unless you pass
--prior-trials. Not a result: nothing is written to the ledger or the manifests.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from statistics import NormalDist

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtester import manifest  # noqa: E402

EULER_GAMMA = 0.5772156649015329
TRADING_DAYS = 252            # metrics.py annualizes the daily Sharpe by sqrt(252)
_Z = NormalDist()


def expected_max_sharpe(n: int, var: float) -> float:
    """SR0: the expected maximum Sharpe (per-period units) of `n` independent
    luck-only trials whose Sharpes have variance `var`. One trial has no selection
    bias, so SR0 = 0 there."""
    if n < 1:
        raise ValueError("n must be at least 1")
    if n == 1:
        return 0.0
    g = EULER_GAMMA
    return math.sqrt(var) * ((1 - g) * _Z.inv_cdf(1 - 1 / n)
                             + g * _Z.inv_cdf(1 - 1 / (n * math.e)))


def deflated_sharpe(sharpe_annual: float, days: int, skew: float, kurt: float,
                    n: int, var: float | None = None) -> tuple[float, float]:
    """(DSR, SR0 annualized). `var` is the per-day variance of the trial Sharpes
    (default: the luck-only 1/(days - 1)). DSR with n = 1 is the probabilistic Sharpe
    ratio, the probability that the true Sharpe is above zero."""
    if days < 3:
        raise ValueError("need at least 3 daily observations")
    sr = sharpe_annual / math.sqrt(TRADING_DAYS)
    v = 1.0 / (days - 1) if var is None else var
    sr0 = expected_max_sharpe(n, v)
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if denom <= 0:
        return float("nan"), sr0 * math.sqrt(TRADING_DAYS)
    z = (sr - sr0) * math.sqrt(days - 1) / math.sqrt(denom)
    return _Z.cdf(z), sr0 * math.sqrt(TRADING_DAYS)


def counts(rows: list[dict], family: str, strategy_sha: str, extra_config: str | None):
    """(distinct configs in this code version, distinct configs across all versions).
    `extra_config` is the target's own config_sha if it is not in the ledger."""
    counted = [r for r in rows if r.get("family") == family
               and r.get("kind") in manifest.COUNTED_KINDS]
    this = [r for r in counted if r.get("strategy_sha") == strategy_sha]
    cfg_this = {r["config_sha"] for r in this} | ({extra_config} if extra_config else set())
    cfg_all = {r["config_sha"] for r in counted} | ({extra_config} if extra_config else set())
    return len(cfg_this), len(cfg_all)


def n_values(n_version: int | None, n_all: int | None, prior: int,
             first_label: str = "ledger, this code version") -> list[tuple[str, int]]:
    """The rows of the table: (label, N), duplicates merged, ascending."""
    rows: list[tuple[str, int]] = [("one try (probabilistic Sharpe)", 1)]
    if n_version:
        rows.append((first_label + (f" + {prior} prior" if prior else ""), n_version + prior))
    if n_all and n_all != n_version:
        rows.append((f"ledger, all code versions" + (f" + {prior} prior" if prior else ""),
                     n_all + prior))
    base = max(n for _, n in rows)
    rows += [("x10", base * 10), ("x100", base * 100)]
    seen: dict[int, str] = {}
    for label, n in rows:
        seen.setdefault(n, label)
    return sorted(((label, n) for n, label in seen.items()), key=lambda x: x[1])


def render(title: str, res: dict, nrows: list[tuple[str, int]],
           var: float | None, notes: list[str]) -> str:
    sharpe, days = res["sharpe"], res["n_days"]
    skew, kurt = res["skew"], res["kurt"]
    spread = ("luck-only variance 1/(T-1)" if var is None else
              f"trial Sharpe std {math.sqrt(var * TRADING_DAYS):.2f} (annualized)")
    lines = [f"Deflated Sharpe: {title}",
             f"  Sharpe {sharpe:.3f} (annualized), {days} days, skew {skew:+.2f}, "
             f"kurtosis {kurt:.1f} (non-excess)",
             f"  variance of the trial Sharpes: {spread}"]
    lines += [f"  note: {n}" for n in notes]
    lines.append("")
    lines.append(f"  {'tries N':>9}  {'luck-only best Sharpe':>22}  {'DSR':>7}   basis")
    for label, n in nrows:
        d, sr0 = deflated_sharpe(sharpe, days, skew, kurt, n, var)
        lines.append(f"  {n:>9,}  {sr0:>22.2f}  {d:>7.3f}   {label}")
    lines += ["",
              "  Read: DSR is the chance the true Sharpe beats the best of N luck-only tries.",
              "  Near 1.00 survives that many tries; about 0.50 is no better than luck. ADVISORY:",
              "  it treats tries as independent (neighbouring settings are not, so the effective",
              "  N is lower), and N is a count, not a fact: add work from before the ledger with",
              "  --prior-trials."]
    return "\n".join(lines)


def _from_row(row: dict) -> dict:
    res = row.get("results") or {}
    return {"sharpe": res.get("sharpe"), "n_days": res.get("n_days"),
            "skew": res.get("skew"), "kurt": res.get("kurt")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--seq", type=int, help="ledger line number")
    src.add_argument("--manifest", help="a run manifest JSON")
    ap.add_argument("--sharpe", type=float, help="annualized Sharpe (manual mode)")
    ap.add_argument("--days", type=int, help="daily observations (manual mode)")
    ap.add_argument("--skew", type=float, help="skew of daily returns (default 0)")
    ap.add_argument("--kurt", type=float, help="non-excess kurtosis of daily returns (default 3)")
    ap.add_argument("--trials", type=int, help="tries so far (manual mode)")
    ap.add_argument("--trial-sharpe-std", type=float,
                    help="annualized std of the trial Sharpes (default: the luck-only value)")
    ap.add_argument("--prior-trials", type=int, default=0,
                    help="tries made before the ledger started (adds to the ledger counts)")
    ap.add_argument("--ledger", help="ledger path (default reports/trials.jsonl)")
    args = ap.parse_args(argv)
    if args.prior_trials < 0:
        ap.error("--prior-trials must be >= 0")
    if args.trial_sharpe_std is not None and args.trial_sharpe_std <= 0:
        ap.error("--trial-sharpe-std must be > 0")
    var = (None if args.trial_sharpe_std is None
           else (args.trial_sharpe_std / math.sqrt(TRADING_DAYS)) ** 2)

    rows = manifest.read_ledger(Path(args.ledger) if args.ledger else None)
    notes: list[str] = []
    family = sha = cfg = None
    if args.seq is not None:
        row = next((r for r in rows if r.get("seq") == args.seq), None)
        if row is None:
            ap.error(f"no ledger line with seq {args.seq}")
        res, title = _from_row(row), f"{row['family']}, ledger seq {row['seq']} ({row['kind']})"
        family, sha, cfg = row["family"], row["strategy_sha"], row["config_sha"]
    elif args.manifest:
        doc = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        res = _from_row(doc)
        title = f"{doc['strategy']['family']}, {Path(args.manifest).name}"
        family, sha, cfg = doc["strategy"]["family"], doc["strategy"]["sha"], doc["config_sha"]
    else:
        if args.sharpe is None or args.days is None:
            ap.error("give --seq, --manifest, or --sharpe and --days")
        res = {"sharpe": args.sharpe, "n_days": args.days, "skew": args.skew, "kurt": args.kurt}
        title = "manual input"
    for k in ("sharpe", "n_days"):
        if res[k] is None:
            ap.error(f"the result has no {k}")
    if res["skew"] is None or res["kurt"] is None:
        res["skew"], res["kurt"] = 0.0, 3.0
        notes.append("skew/kurtosis not recorded: a normal distribution (0, 3) is assumed")

    if family is not None:
        n_ver, n_all = counts(rows, family, sha, cfg)
        nrows = n_values(n_ver, n_all, args.prior_trials)
        first = min((r.get("created_utc", "") for r in rows), default="")
        notes.append(f"ledger: {n_ver} distinct configs of this code version, {n_all} across "
                     f"all versions, ledger starts {first[:10] or 'n/a'}"
                     + ("" if args.prior_trials else "; work before that is not counted "
                        "(--prior-trials)"))
    else:
        base = args.trials + args.prior_trials if args.trials else None
        nrows = n_values(base, None, 0, "tries given (--trials)") if base else [
            ("one try (probabilistic Sharpe)", 1), ("x10", 10), ("x100", 100), ("x1000", 1000)]
    print(render(title, res, nrows, var, notes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
