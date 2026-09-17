"""God Trades — Dewber's real live/funded gbZeus NT8 template, not the deck.

Source: NT8 strategy templates pulled from the shared repo
(`templates/Strategy/GreyBeard.gbZeus/Dewber_NQ.xml`, 2026-08-26 snapshot).
This is a DIFFERENT config from `GodTradesZeus` (which mirrors the deck's
generic "NQ 1000-tick, 10:15-15:00 ET" methodology) — Dewber's saved template
shows he actually runs the indicator on a **1-minute chart**, in **two**
session windows, with a hard daily loss stop. Kept as a separate strategy
(not a GodTradesZeus subclass) so neither config's results get muddied.

Diffed against `Dewber_NQ_1m_Eval.xml` (same file otherwise): only
DailyLoss/DailyProfit differ (700/off here vs. 2000/1500 on the eval
variant) — this file uses the live/funded numbers.

CAVEAT: the NT8 template stores session start/end as bare HH:MM with no
timezone tag. Assumed US/Eastern per this repo's ET-everywhere convention
(and because Dewber trades US index futures), but that is NOT verified
against Dewber directly — confirm before treating results here as anything
more than "what the template says if its times are ET."
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("god_trades", _here / "god_trades.py")
_mod = importlib.util.module_from_spec(_spec)
sys.modules["god_trades"] = _mod
_spec.loader.exec_module(_mod)
GodTrades = _mod.GodTrades


class GodTradesZeusDewber(GodTrades):
    period = "1m"
    qty = 1

    # v16.6 indicator: BG + FC only, no OBR (matches GodTradesZeus)
    enable_bg = True
    enable_fc = True
    enable_obr = False

    # ExitModeInput=BandTarget in the template -> methodology-faithful exits
    exit_mode = "band"
    track_band_target = True
    stop_offset_ticks = 0
    max_stop_ticks = 0  # MaxStopTicks=0 in the template (off)

    # two session windows, both FlattenAtWindowEnd=true; times as stored in
    # the template (see CAVEAT above re: timezone)
    entry_window = [("09:00", "10:00"), ("13:00", "16:00")]
    flatten_at_window_end = True

    # SpiderwebSuppress=true / 100 ticks / 5 lines
    spiderweb_suppress = True
    spiderweb_distance_ticks = 100
    spiderweb_line_count = 5

    # DailyLoss=700, UseDailyProfit=false -> no daily profit target
    daily_loss_limit = 700
    daily_profit_target = None
