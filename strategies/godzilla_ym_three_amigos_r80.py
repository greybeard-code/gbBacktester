"""GodZillaKilla -- YM three-amigos on r80-20 ninZaRenko (2026-08-09).

Same 3-of-3 TH+PA+SJ gate as godzilla_ym_three_amigos.py (which failed on
r33-4 in every configuration tried -- see CLAUDE.md), re-tried on a much
coarser brick per user request: all-hours entries (no window restriction),
TP/SL grid TP in {25,50} x SL in {50,100}. Unlike r33-4, TP50/SL100 on
r80-20 came back net positive and survived the $2k floor on the full
history, AND walk-forward converged to the same combo in all 5 windows with
5/5 profitable OOS windows (WFE 1.17, stitched OOS net $4,720/214 days,
Sharpe 2.20) -- see CLAUDE.md for the full writeup and whatever the
follow-up Monte Carlo / sample-size checks conclude.
"""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "godzilla_killa_base", Path(__file__).with_name("godzilla_killa.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class GodZillaYMThreeAmigosR80(_mod.GodZillaKilla):
    symbol = "YM"
    period = "r80-20"

    # 3-of-3 gate: TH + PA + SJ only, all three must agree
    set1_required = 3
    use_ko, use_su, use_nc = False, False, False
    use_pa, use_th, use_sj = True, True, True
    confirmation_bars = 1

    # all-hours: no entry window restriction
    tf1_enabled = False
    tf2_enabled = False
    tf3_enabled = False
    skip_enabled = False

    # fixed-ticks exits (synthetic single-bracket ATM)
    order_mode = "fixed"
    fixed_qty = 1
    fixed_tp_ticks = 50
    fixed_sl_ticks = 100
