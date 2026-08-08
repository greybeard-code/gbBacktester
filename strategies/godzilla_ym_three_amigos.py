"""GodZillaKilla -- YM three-amigos (2026-08-08). FAILED validation, recorded
negative result. Do not deploy. See CLAUDE.md for the full writeup.

TH+PA+SJ gate (3-of-3, "three amigos" -- see godzilla_evening_confluence.py /
CLAUDE.md for the validated MNQ r70-4 evening champion this mirrors), ported
to YM (full-size E-mini Dow, $5/tick) on ninZaRenko r33-4 per user request.
The MNQ champion's own 20:00-20:45 ET window is a straight loser on YM r33-4
(net -$1,558/19mo, breached). A full-session window sweep + TP/SL grid found
an 11:00-14:00 ET / TP25 / SL150 config (below) that looked strong on the
full history (net $6,844/19mo, Sharpe 1.09, 373 trades) -- but walkforward.py
overturned it: stitched OOS net $421/209 days, Sharpe 0.11, WFE 0.14
("POOR, likely curve-fit"). Kept pinned to that config as the record of what
was tested and rejected, not as something to run live.
"""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "godzilla_killa_base", Path(__file__).with_name("godzilla_killa.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class GodZillaYMThreeAmigos(_mod.GodZillaKilla):
    symbol = "YM"
    period = "r33-4"

    # 3-of-3 gate: TH + PA + SJ only, all three must agree
    set1_required = 3
    use_ko, use_su, use_nc = False, False, False
    use_pa, use_th, use_sj = True, True, True
    confirmation_bars = 1

    # entries-only window -- late-morning/early-afternoon ET, chosen from the
    # full-session hourly sweep (reports/ym_three_amigos_window_sweep.csv)
    tf1_enabled, tf1, tf1_flatten = True, ("11:00", "14:00"), False
    tf2_enabled = False
    tf3_enabled = False
    skip_enabled = False

    # fixed-ticks exits (synthetic single-bracket ATM) -- placeholder values,
    # swept separately
    order_mode = "fixed"
    fixed_qty = 1
    fixed_tp_ticks = 25
    fixed_sl_ticks = 150
