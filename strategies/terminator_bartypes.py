"""Terminator_V2 -- standard bar types (time/tick), replacing native ninZaRenko
r100-4. Tests whether the champion SAR signal (ATR 28 x 3.25, TerminatorRec
config: session 18:00-16:55 ET, entries 15:30-16:55 + 18:00-22:55, 100-tick
hard stop) survives on ordinary bars instead of renko, on MNQ/MES/MYM.

*** FAILED VALIDATION (2026-08-07) — DO NOT DEPLOY. ***
Swept 1m/3m/5m/10m time bars and 500t/1000t tick bars x {MNQ, MES, MYM},
full history 2024-12-16..2026-07-31, $2,000 Apex floor, everything else held
identical to strategies/terminator_rec.py (only --period/--symbol changed).
Full results: reports/terminator_bartype_sweep.csv.

  symbol period   net_pnl  trades  win%   PF  sharpe   maxDD    prop
  MNQ    1m        1,719    3259  33.3  1.02   0.29   -5,899  BREACHED
  MNQ    3m        4,003     871  30.1  1.15   0.94   -1,796  survives ($92)
  MNQ    5m        6,844     462  30.5  1.46   1.65   -1,151  survives ($947)
  MNQ    10m         754     227  29.1  1.10   0.30   -1,638  survives ($573)
  MNQ    500t     -6,570    5588  33.3  0.95  -0.92  -11,760  BREACHED
  MNQ    1000t     5,969    2701  32.8  1.08   0.97   -5,328  BREACHED
  MES    1m       -8,626    3348  34.2  0.82  -2.54   -8,792  BREACHED
  MES    3m         -277     855  41.2  0.99  -0.10   -2,452  BREACHED
  MES    5m         -954     463  42.5  0.93  -0.41   -2,039  BREACHED
  MES    10m         -492     233  38.2  0.94  -0.21   -1,911  survives ($45)
  MES    500t     -2,445    2618  37.7  0.95  -0.70   -2,666  BREACHED
  MES    1000t      -421    1318  39.4  0.99  -0.12   -2,518  BREACHED
  MYM    1m      -11,878    4199  29.6  0.66  -6.14  -12,019  BREACHED
  MYM    3m       -2,138     854  38.8  0.82  -1.39   -2,247  BREACHED
  MYM    5m       -1,179     394  35.8  0.84  -0.77   -1,516  survives ($479)
  MYM    10m        -143     161  36.6  0.96  -0.14     -945  survives ($1,053)
  MYM    500t         581     362  37.8  1.07   0.33   -1,142  survives ($845)
  MYM    1000t        11     180  38.9  1.00   0.01   -1,163  survives ($803)

MES: every bar type loses money -- not viable at all on this symbol/config.
MYM: only marginal, thin-sample (161-394 trade) results scrape by; none
worth deploying. MNQ 5m is the best of the whole matrix (Sharpe 1.65, PF
1.46) but still far below the r100-4 champion (Sharpe 3.90, net $22,409,
990 trades) -- and it hasn't been through walk-forward, so even that
number is unvalidated, not just weaker.

Conclusion: the renko structure is load-bearing for this signal, not
incidental. Matches [[renko-is-already-a-chop-filter]] -- on time/tick bars
the ATR-trail SAR has no chop filter and gets whipsawed; ninZaRenko's
equal-step-size geometry is what lets the trail line and the ER-style
trend/chop separation work. Kept only so the negative result is recorded;
re-evaluate only if the signal itself changes (e.g. an explicit chop filter
on time bars), not by re-sweeping more bar-type/symbol combos of the same
SAR logic.

Encodes the single best-of-sweep candidate (MNQ 5m) below for reference --
this config must NOT be deployed as-is.
"""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "terminator_v2_base", Path(__file__).with_name("terminator_v2.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TerminatorBarTypes(_mod.TerminatorV2):
    symbol = "MNQ"
    period = "5m"
    session = ("18:00", "16:55")
    flat_at_session_end = True
    qty = 1

    entry_window = ("15:30", "16:55")
    entry_window2 = ("18:00", "22:55")

    atr_period = 28
    atr_mult = 3.25
    sl_ticks = 100
