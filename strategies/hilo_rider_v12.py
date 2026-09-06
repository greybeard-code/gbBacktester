"""HiLoRider v1.2 (Khahn, 2026-08-12) — MGC profile, honest-fill re-test.

Source: `nt8 code/HiLoRider/HiLoRider v1.2 08-12-26/`. The v1.0 port, its
mechanism analysis and the staged test plan are in `strategies/hilo_rider.py`
and `nt8 code/HiLoRider/HiLoRider.md`; this subclass only carries the deltas.

The signal is UNCHANGED. `Indicators/AlgoTrader/HiLoBands.cs` differs from v1.0
only in chart rendering (a `ShowSignalArrows` flag and the Fast-Mode plumbing
around it); the midline/crossover/slope math is byte-identical, as is
`HiLoRiderStopModes.cs`. Most of the ~2,000 changed lines are execution-safety
and operations work (`HiLoRiderExecutionSafety.cs`,
`HiLoRiderOperationalParity.cs`, deferred-flat reconciliation, a shared
`SubmitFullExit`), which cannot move a backtest.

WHAT ACTUALLY CHANGES THE TRADES, and it is not small:

1. **TargetMode = NoTarget** (was FixedTicks/120). `HiLoRider.cs:1230` also
   force-sets `TrailMode = HighLow` whenever NoTarget is selected, so the
   SetDefaults switch to MidlineOffset never takes effect. Exits are now stop,
   trail and MaxBarsInTrade only. This deletes the exact mechanism
   HiLoRider.md §4.2 blamed for the author's reported numbers: there is no
   120-tick target left for the Heikin-Ashi close lag (5N/7 ticks, ~86 at
   N=120) to hand over for free. It does NOT make the lag harmless — the trail
   basis and the entry reference are still HA-lagged — but v1.2 is a genuinely
   different exit architecture, not a re-skin, so it earns its own number.
2. **MaxBarsInTrade 10 -> 14.** Dead in v1.0 (the target resolved first); with
   no target it is now a live, frequently-binding exit.
3. **A 1-bar TTL on the resting limit entry** (`_entryOrderSubmitBar`). In v1.0
   nothing ever cancelled it, so one unfilled limit blocked every later signal
   forever — the reason `entry_limit_offset_ticks = 0` (market) was the only
   honest v1.0 baseline. With the TTL the shipped passive limit is finally
   testable, so both are run here.
4. **LimitOffsetTicks 8 -> 12** (further from the bid, fills less often).
5. **MaxEntrySpreadTicks = 3**, a new liquidity gate that fails CLOSED on an
   invalid/unknown quote.
6. `pivotBarsAgo`/indicator reads become bar 0 in Historical (1 only in
   Realtime), i.e. decisions on the completed bar — which is what this port
   already did.

DEFECT FOUND IN v1.2, and the reason `lookback_period` is 8 here. The MGC block
of `State.DataLoaded` sets `LookbackPeriod = 8`, while its own `Print()` on the
next line, the property description, the class Description and the module
header all say **10**. MNQ has the same defect and it is wider: the code sets
**5** where every comment says **25**. Whatever was validated, 8 (and 5) is
what actually loads on a chart, so 8 is the default here and 10 is run as a
sensitivity check. Worth reporting upstream.

ALSO NOTE, operationally: `strategyEnabled` now defaults to **false**
(HiLoRiderStrategyControl.cs) — automated entries are off until the panel
enables them, so a Strategy Analyzer run of v1.2 as shipped takes zero trades.
Modelled here as enabled, which is the only interesting case.

Everything listed as NOT PORTED in `hilo_rider.py` still ships disabled in
v1.2 and is still not ported — including the econ calendar, which is
structurally realtime-only (`HiLoRider.md` §8.6). Its *idea* is testable here
though, via this repo's own historical news calendar, and on MGC it helps:
`news_filter=True, news_pre_min=5, news_post_min=20` (Khahn's own window) takes
w120 from +$23,117 to +$26,537 at Sharpe 2.12 -> 2.46 and P(breach) 51.9% ->
45.1%. Left OFF here so this class reproduces the shipped config; turn it on
deliberately, and include it in any walk-forward rather than bolting it on.
"""
from __future__ import annotations

from strategies.hilo_rider import HiLoRider


class HiLoRiderV12(HiLoRider):
    symbol = "MGC"
    period = "w120"

    # ---- v1.2 MGC instrument profile (State.DataLoaded) -------------------
    lookback_period = 8           # SOURCE CODE value; the comments claim 10
    stop_buffer_ticks = 2
    trail_min_ticks = 4
    hl_stage2_trigger_bars = 3
    hl_stage3_trigger_bars = 5
    hl_stage3_trail_ticks = 10

    # ---- v1.2 exit architecture -------------------------------------------
    fixed_tp_ticks = 0            # TargetMode=NoTarget -> no target order
    max_bars_in_trade = 14

    # ---- v1.2 entry --------------------------------------------------------
    entry_limit_offset_ticks = 0  # market baseline; 12 = the shipped limit
    entry_limit_ttl_bars = 1
    max_entry_spread_ticks = 3

    # The MNQ profile, for reference (source-code values, comments claim 25):
    #   lookback_period 5, stop_buffer_ticks 2, trail_min_ticks 40,
    #   hl_stage2_trigger_bars 5, hl_stage3_trigger_bars 6,
    #   hl_stage3_trail_ticks 10
