"""Terminator_V2 -- MES config, ninZaRenko brick calibrated from MNQ r100-4.

Champion SAR signal (ATR 28 x 3.25, TerminatorRec structure: session
18:00-16:55 ET, entries 15:30-16:55 + 18:00-22:55, 100-tick hard stop),
same as strategies/terminator_rec.py, with only symbol + brick changed.

Brick calibration (2026-08-07): avg ATR(20) on 5-minute bars over the last
90 sessions, in ticks, scaled proportionally to MNQ's (keeping the champion's
25:1 brick:trend ratio). MNQ avg ATR 103.6 ticks (r100-4 is right at that);
MES avg ATR 15.5 ticks -> ratio 0.15 -> r15-1. (MYM ratio 0.19 -> r19-1, MGC
ratio 0.47 -> r47-2 -- both FAILED, see below. All ratios came out smaller
than MNQ's brick, matching the "half or smaller" rule of thumb.) Re-derive
via the same method (backtester/indicators.ATR over Catalog 5m bars) if MES
volatility regime shifts materially.

*** FAILED VALIDATION (2026-08-07) — DO NOT DEPLOY. ***
Full-sample in-sample number looked fine (2024-12-16..2026-07-31, $2,000 Apex
floor): net $4,666, 1995 trades, WR 41.3%, PF 1.11, Sharpe 1.13, maxDD
-$2,330, survives with $1,476 headroom, MC(2000) P(breach) = 26.6% -- already
8x the MNQ champion's 3.3% breach risk on the same methodology, but still net
positive. It was the only one of {MES r15-1, MYM r19-1, MGC r47-2} that
survived in-sample at all.

Two independent out-of-sample checks then both failed it:

1. July-only slices (single-month, low trade count, but a real recency
   check): July 2025 net -$215 (35 trades, Sharpe -1.22); July 2026 net
   -$1,199 (105 trades, Sharpe -7.37). Negative both months -- the full-
   sample edge did not show up in either individual July.

2. walkforward.py (5 windows, IS:OOS 5:1, grid atr_mult={3.0,3.25,3.5} x
   atr_period={24,28,32} x sl_ticks={15,25,50,100}, same session/entry-window
   structure as terminator_rec.py): stitched OOS net **-$799** over 209 days,
   OOS Sharpe **-0.54**, walk-forward efficiency **-0.09 (POOR, Davey:
   discard)**, only 3/5 windows OOS-profitable, and NO parameter
   convergence -- best sl_ticks per window bounced 100/50/100/15/15 and
   atr_mult 3.0-3.5 with no plateau (log: reports/terminator_mes_walkforward.log).
   This is the same failure shape as terminator_mcl: IS Sharpe 2.26/2.00/1.71
   in windows 1-3 collapsed to OOS Sharpe 2.47/1.23/-3.30 -- no stable optimum,
   classic curve-fit.

MYM (r19-1) and MGC (r47-2) FAILED outright on the original in-sample test,
no walk-forward needed to reject them:
  MYM: net -$4,404, Sharpe -1.75, maxDD -$5,190, BREACHED, MC P(breach) 98.7%.
  MGC: net -$10,809, Sharpe -0.87, maxDD -$15,209, BREACHED, MC P(breach) 99.6%.
Full data: reports/terminator_renko_othersymbols.csv,
reports/terminator_mes_walkforward.log.

Conclusion: the champion Terminator SAR structure (ATR trail, r100-4-style
renko, this session/entry-window shape) does not transfer to MES/MYM/MGC via
simple ATR-ratio brick recalibration. Kept only so the negative result is
recorded, same as terminator_mcl.py. Re-evaluate only with a genuinely
different approach (e.g. re-tuning entry windows/session per instrument the
way MCL's oil-specific window was, not just rescaling the brick), not by
re-running this same grid.
"""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "terminator_v2_base", Path(__file__).with_name("terminator_v2.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TerminatorMES(_mod.TerminatorV2):
    symbol = "MES"
    period = "r15-1"
    session = ("18:00", "16:55")
    flat_at_session_end = True
    qty = 1

    entry_window = ("15:30", "16:55")
    entry_window2 = ("18:00", "22:55")

    atr_period = 28
    atr_mult = 3.25
    sl_ticks = 100
