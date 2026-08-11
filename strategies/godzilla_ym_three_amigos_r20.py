"""GodZillaKilla -- YM three-amigos on r20-5 ninZaRenko (2026-08-09).

Same 3-of-3 TH+PA+SJ gate, all-hours (no window), as
godzilla_ym_three_amigos_r80.py -- r80-20 prints too slowly to watch live,
so this quarters the brick (same 4:1 brick:trend ratio) per user request.
TP/SL swept separately; see CLAUDE.md for the result.
"""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "godzilla_killa_base", Path(__file__).with_name("godzilla_killa.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class GodZillaYMThreeAmigosR20(_mod.GodZillaKilla):
    symbol = "YM"
    period = "r20-5"

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

    # fixed-ticks exits (synthetic single-bracket ATM) -- placeholder,
    # swept separately
    order_mode = "fixed"
    fixed_qty = 1
    fixed_tp_ticks = 50
    fixed_sl_ticks = 100
