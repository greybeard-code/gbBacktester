// file name = HiLoRider.cs
// HiLoRider — v1.2 (Aug 2026 performance/UI release)
//
// MNQ + MGC strategy built around HiLoBands (bin\Custom\Indicators\
// AlgoTrader\HiLoBands.cs) -- a single-period Donchian-style channel
// (Upper=MAX(High,N), Lower=MIN(Low,N), Middle=avg) that now also emits its
// own entry signal (+1/-1 crossover of Close vs the midline) and draws
// arrows at the exact signal bar. Built on the Reaper v1.4 codebase (full
// family architecture reused: panel, HUD, ML, logger, GEX, watchdogs,
// unmanaged bracket) -- cloned from LiquidRider's own architecture, the
// closest existing precedent for "reference a real external indicator
// instance directly, read its own Signal series" (see hiloInd below).
//
// SIGNAL LOGIC (Aug 2026, 2-slot priority cascade, delegated entirely to
// hiloInd's own Signal/Signal2 outputs -- not a re-implemented copy of the
// channel/crossover math inline, matching the "real indicator instance"
// pattern this fleet already uses for LinRegRider's linreg1 and this bot's
// own former UptrickLiquidReversalBands reference):
//   Slot 1 (priority): hiloInd.Signal[0]  -- fresh crossover of Close vs the
//     channel midline, confirmed by Close[0] vs Close[1] (Close[1]<=midline[1]
//     -> Close[0]>midline[0] AND Close[0]>Close[1] for longs, mirror shorts).
//   Slot 2 (fallback, only checked when Slot 1 stays silent): hiloInd.
//     Signal2[0]  -- the midline's OWN slope, confirmed the same way
//     (midline[0]>midline[1] AND Close[0]>Close[1] for longs, mirror shorts).
//     Does not require a fresh crossover -- a genuinely different, looser
//     condition than Slot 1, not a re-check of it.
//   Direction is WITH the signal (trend-following), same category as
//   LinRegRider/TrendMaster/LiquidRider in this fleet. Whichever slot fires
//   is tagged into entrySignalSource ("HiLoRider-Crossover"/"HiLoRider-Slope").
//
// COMPILE-ORDER DEPENDENCY: this strategy calls the auto-generated
// HiLoBands(...) accessor method, which NT8 only generates once
// HiLoBands.cs has compiled successfully at least once. HiLoBands.cs must
// compile BEFORE HiLoRider.cs will.
//
// GENESIS BACKTEST -- MGC first (HiLoRider_MGC_Backtest.py, Aug 2026), MNQ
// added later the same month (HiLoRider_MNQ_Backtest.py) using the identical
// sequential-sweep methodology (period -> confirmatory cross-check -> target
// -> stop buffer -> trail, plus a negative-TrailOffsetTicks edge-artifact
// re-check on each instrument independently, not assumed to transfer).
//
// CURRENT VALIDATED BAR BASIS (Aug 2026): WaveBars Value=120. DataLoaded applies
// the current instrument profiles only when UseInstrumentProfileDefaults=true:
//   MNQ: Lookback=25, StopBuffer=2, Target=120, TrailMin=40,
//        HLStage2/3=5/6 bars, Stage3Trail=10.
//   MGC: Lookback=10, StopBuffer=2, Target=120, TrailMin=4,
//        HLStage2/3=3/5 bars, Stage3Trail=10.
// The older TBars/pseudo-TBars configurations are retained in research history,
// but are not the deployed defaults and must not be presented as such here.
//
// ADX FILTER: OFF by default (EnableADXFilter=false) -- untested for this
//   signal on either instrument. EnableDiffBandFilter also off, same reason.
//
// STOP / TARGET / TRAIL: StopMode=ChannelBand (hiloInd's own opposite band
//   -/+ StopBufferTicks) + TargetMode=FixedTicks + TrailMode=MidlineOffset
//   (see genesis backtests above) -- both instruments independently
//   validated, values applied via the per-instrument override in
//   State.DataLoaded (MNQ=25/2/120, MGC=10/2/120). Any OTHER
//   instrument runs entirely unvalidated on the SetDefaults()
//   values, with a loud Print() warning at DataLoaded.
//
// AUTO EXIT (HiLoRiderAutoExit enum): Disabled by default -- not part of
//   the validated backtest. Available to enable/test later.
//
// FILE STRUCTURE:
//   HiLoRider.cs                   — core strategy  ← this file
//   HiLoRiderSessionState.cs       — session state, MFE/MAE, OnPositionUpdate
//   HiLoRiderHUD.cs                — text HUD overlay
//   HiLoRiderUI.cs                 — WPF ChartTrader panel
//   HiLoRiderStrategyControl.cs    — strategy ON/OFF + direction toggles
//   HiLoRiderScaleOut.cs           — scale-out logic
//   HiLoRiderFixedStopTrail.cs     — FixedStopTrail 4-stage ladder
//   HiLoRiderStopModes.cs          — CalcStopLevel / CalcTargetLevel
//   HiLoRiderRiskManager.cs        — position sizing
//   HiLoRiderSessionControl.cs     — 4-window time control
//   HiLoRiderEntryFilters.cs       — news, midday, gap filters
//   HiLoRiderML.cs                 — online logistic regression ML gate
//   HiLoRiderMetaLabel.cs          — offline RF meta-label gate (stub)
//   HiLoRiderRegime.cs             — regime detection
//   HiLoRiderTradeLogger.cs        — JSONL / CSV trade logger

#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Windows.Media;
using System.Xml.Serialization;
using NinjaTrader.Cbi;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.DrawingTools;
using NinjaTrader.NinjaScript.Indicators;
using PMIndicator  = NinjaTrader.NinjaScript.Indicators.AlgoTrader.PriceMomentum;
using DRIndicator  = NinjaTrader.NinjaScript.Indicators.AlgoTrader.DailyRange;
using HiLoIndicator = NinjaTrader.NinjaScript.Indicators.AlgoTrader.HiLoBands;
#endregion

namespace NinjaTrader.NinjaScript.Strategies.HiLoRider
{
    // ── Enums ─────────────────────────────────────────────────────────────────
    public enum RPStopMode    { ChannelBand, FixedTick, ATR, HighLow }   // ChannelBand repurposed for this bot -- see GetHiLoRiderStopPrice()
    public enum RPTargetMode  { NoTarget, FixedTicks, ATR, RiskReward }
    public enum RPTrailMode   { HighLow, StagedTrail, ATR, FixedStopTrail, FixedStop, MidlineOffset }
    public enum HiLoRiderAutoExit { DiffCross, Disabled }

    public partial class HiLoRider : Strategy
    {
        // ── PriceMomentum indicator (used for DiffCross auto-exit + ML) ───────
        private PMIndicator pm1;

        // ── ATR / ADX indicators ──────────────────────────────────────────────
        internal ATR atr1;
        private  ADX adx1;

        // ── DailyRange indicator ───────────────────────────────────────────────
        private DRIndicator dr1;

        // ── ET timezone ───────────────────────────────────────────────────────
        private static readonly TimeZoneInfo EasternTZ =
            TimeZoneInfo.FindSystemTimeZoneById("Eastern Standard Time");
        private long _cachedEtSourceTicks = long.MinValue;
        private DateTimeKind _cachedEtSourceKind = DateTimeKind.Unspecified;
        private int _cachedEtTimeValue = 0;

        private int ToTimeET(DateTime t)
        {
            if (t.Ticks == _cachedEtSourceTicks && t.Kind == _cachedEtSourceKind)
                return _cachedEtTimeValue;
            DateTime et = TimeZoneInfo.ConvertTimeFromUtc(t.ToUniversalTime(), EasternTZ);
            _cachedEtSourceTicks = t.Ticks;
            _cachedEtSourceKind = t.Kind;
            _cachedEtTimeValue = et.Hour * 10000 + et.Minute * 100 + et.Second;
            return _cachedEtTimeValue;
        }

        // For HUD: current and previous bar ranges in ticks
        internal double bar1RangeTicks  = 0;   // last closed bar
        internal double bar0RangeTicks  = 0;   // current forming bar

        // ── Regime state ───────────────────────────────────────────────────────
        internal int    currentRegime      = 0;
        internal string currentRegimeLabel = "READY";

        // ── SuperTrendRegime indicator (informational, Jul 2026) ────────────
        // Regime detection + badge display only -- NOT wired as an entry filter
        // here (that role is validated only for Reaper and PivotMaster; see
        // CLAUDE.md "SuperTrendRegime" section for the per-bot backtest results).
        private SuperTrendRegime superTrend1;

        // ── PriceMomentum cache (for DiffCross exit + entry filter + ML) ────────
        internal double cachedDiff           = 0;
        internal double cachedAdaptiveFloor  = 2.0;
        internal double cachedAdaptiveExhaust= 14.0;

        // ── HiLoBands indicator (channel + entry signal + trail basis) ─────────
        // A real, separately-compiled AlgoTrader indicator (not a re-implementation
        // of its channel/crossover math inline) -- same "reference the real
        // indicator" pattern as LinRegRider's own linreg1/NT8-stock-LinReg usage
        // and this file's own superTrend1/dr1 instances. Depends on HiLoBands.cs
        // compiling successfully FIRST -- see the module-header note. Entry
        // signal (hiloInd.Signal), stop (hiloInd.UpperBand/LowerBand), and trail
        // (hiloInd.MiddleLine) all read from this one instance.
        private HiLoIndicator hiloInd;

        // ── Session state ──────────────────────────────────────────────────────
        private bool   beRealized       = false;
        private int    stagedTrailStage = 0;
        private double _hlPeakPrice     = 0;
        internal int   tradeDir         = 0;
        internal double filledPrice     = 0;
        private double bePriceTrigger   = 0;
        internal double stopLevel       = 0;
        internal double targetLevel     = 0;
        private double targetTicksHeld  = 0;
        private bool   autoExitPending  = false;

        // ── Daily trade counter ────────────────────────────────────────────────
        private int  _tradesThisSession = 0;

        // ── Manual trade flag — suppresses DiffCross auto-exit ────────────────
        internal bool _isManualTrade             = false;
        private  bool _diffConfirmedSinceEntry   = false;

        // ── Auto-pause (limits/risk) — keeps STRATEGY button green ────────────
        internal bool   _autoPaused       = false;
        internal string _autoPausedReason = "";

        // ── Entry dedup guard ──────────────────────────────────────────────────
        private int lastHiLoRiderSigBar = -1;

        // ── Cascade slot tracking (Aug 2026) — which of the two HiLoBands
        // signals produced the current entry, threaded into entrySignalSource
        // via SnapshotEntryIndicators() below.
        private string _lastHiLoRiderSlot = "HiLoRider";

        // ── Chop-hold cache (Aug 2026, ported from Reaper) ─────────────────────
        // When a real signal fires but IsChoppy() is true (|diff| below the
        // DiffMinPercent floor) and BarsToHold > 0, the signal is cached here
        // and re-checked for up to BarsToHold bars for diff to clear the floor
        // IN THE SIGNAL'S OWN DIRECTION. _chopPendingDir=0 means no pending signal.
        private int _chopPendingDir = 0;
        private int _chopPendingBar = 0;

        // ── Last exit bar (for cooldown) ───────────────────────────────────────
        private int lastExitBar = -999;

        // ── Order references (unmanaged bracket) ──────────────────────────────
        private Order  entryOrder    = null;

        // Aug 2026: bar the current resting LIMIT entry order was submitted on --
        // used to cancel it if it's still working (unfilled) one full bar later.
        // Only meaningful for limit entries (useMarketOrder == false); market
        // orders fill immediately and never reach the check that reads this.
        private int _entryOrderSubmitBar = -1;

        // ── Connection status (Aug 2026, NinjaScript Unmanaged Approach doc audit) ─────
        private bool _orderConnectionLost = false;
        private bool _priceConnectionLost = false;
        private bool _connectionPauseOwned = false;
        private Order  stopOrder     = null;
        private Order  targetOrder   = null;
        private string ocoId         = "";
        private string bracketOcoId  = "";

        // ── OI-5 deferred bracket ─────────────────────────────────────────────
        private bool _pendingBracketPlacement = false;
        private bool _pendingBracketIsLong    = false;
        private int  _pendingBracketQty       = 0;

        // ── Bug 8 synthetic-stop watchdog ─────────────────────────────────────
        private bool _emergencyStopSubmitted = false;

        public override string DisplayName => string.IsNullOrWhiteSpace(InstanceTag)
            ? Name : Name + " [" + InstanceTag.Trim() + "]";

        private int RthStartHHMM => RTHStartHour * 100 + RTHStartMinute;

        private static bool IsInEntryWindow(int timeEtScaled, int winStartScaled, int winEndScaled)
        {
            if (winStartScaled == winEndScaled) return false;
            if (winStartScaled > winEndScaled)
                return timeEtScaled >= winStartScaled || timeEtScaled <= winEndScaled;
            return timeEtScaled >= winStartScaled && timeEtScaled <= winEndScaled;
        }

        // ── Entry snapshot for logger ──────────────────────────────────────────
        internal double   entryAdx               = 0;
        internal double   entryDiff              = 0;
        internal double   entryBar1RangeTicks    = 0;
        internal double   entryBar0RangeTicks    = 0;
        internal int      entryBarsInTrade       = 0;
        internal DateTime entryTimeSnapshot      = DateTime.MinValue;
        internal string   entrySignalSource      = "---";

        // ── UpdateBarRangeCache (HUD display only) ────────────────────────────
        private void UpdateBarRangeCache()
        {
            if (CurrentBar < 2) return;
            int bo = State == State.Realtime ? 1 : 0;
            bar1RangeTicks = (High[bo + 1] - Low[bo + 1]) / TickSize;
            bar0RangeTicks = (High[bo] - Low[bo]) / TickSize;
        }

        // ── GetHiLoRiderSignal ───────────────────────────────────────────────────
        // Aug 2026: 2-slot priority cascade, both delegated entirely to hiloInd's
        // own Signal/Signal2 outputs -- HiLoBands.cs itself computes both on its
        // own closed bar (Calculate.OnBarClose, native NT8 -- no custom CalcMode/
        // veloBo-style indexing branch needed, unlike Ovelez's own
        // OliverVelez20SMA_v2 dependency; a Calculate.OnBarClose indicator's
        // Values[] already reflect the last CLOSED bar throughout the strategy's
        // own still-forming bar, in both Historical and Realtime).
        //   Slot 1 (Signal,  priority): fresh crossover of Close vs the midline,
        //     confirmed by Close[0] vs Close[1].
        //     Long:  hiloInd.Signal[0] == +1     Short: hiloInd.Signal[0] == -1
        //   Slot 2 (Signal2, fallback, only checked when Slot 1 stays silent):
        //     the midline's OWN slope, confirmed the same way -- does not require
        //     a fresh crossover, a genuinely different (looser) condition, not a
        //     re-check of Slot 1.
        //     Long:  hiloInd.Signal2[0] == +1    Short: hiloInd.Signal2[0] == -1
        // Whichever slot fires sets _lastHiLoRiderSlot, threaded into
        // entrySignalSource via SnapshotEntryIndicators() at the entry call site.
        private int GetHiLoRiderSignal()
        {
            if (CurrentBar < LookbackPeriod + 1)      return 0;
            if (CurrentBar == lastHiLoRiderSigBar)    return 0;
            if (hiloInd == null)                      return 0;

            if (hiloInd.Signal.IsValidDataPoint(0))
            {
                double sig1 = hiloInd.Signal[0];
                if (sig1 > 0)
                {
                    lastHiLoRiderSigBar = CurrentBar;
                    _lastHiLoRiderSlot  = "HiLoRider-Crossover";
                    return 1;
                }
                if (sig1 < 0)
                {
                    lastHiLoRiderSigBar = CurrentBar;
                    _lastHiLoRiderSlot  = "HiLoRider-Crossover";
                    return -1;
                }
            }

            if (hiloInd.Signal2.IsValidDataPoint(0))
            {
                double sig2 = hiloInd.Signal2[0];
                if (sig2 > 0)
                {
                    lastHiLoRiderSigBar = CurrentBar;
                    _lastHiLoRiderSlot  = "HiLoRider-Slope";
                    return 1;
                }
                if (sig2 < 0)
                {
                    lastHiLoRiderSigBar = CurrentBar;
                    _lastHiLoRiderSlot  = "HiLoRider-Slope";
                    return -1;
                }
            }

            return 0;
        }

        // ── GetHiLoRiderStopPrice ─────────────────────────────────────────────────
        // Default live path: StopMode=ChannelBand -- opposite channel band (the
        // same band the entry signal itself crossed relative to) -/+
        // StopBufferTicks, matching HiLoRider_MGC_Backtest.py exactly. ATR/HighLow
        // modes still delegate to CalcStopLevel() (HiLoRiderStopModes.cs, inherited
        // fleet architecture, untested for this signal) if the user switches away
        // from the validated default. pivotBarsAgo is accepted for interface
        // parity with the rest of the fleet's chop-hold mechanism but unused here
        // -- the channel band is read same-bar, not from a historical pivot bar.
        private double GetHiLoRiderStopPrice(int sig, double entryPx, int pivotBarsAgo = 1)
        {
            if (StopMode == RPStopMode.FixedTick)
            {
                double fix = entryPx + (sig == 1 ? -1 : 1) * FixedSLTicks * TickSize;
                return Instrument.MasterInstrument.RoundToTickSize(fix);
            }
            if (StopMode == RPStopMode.ATR)
                return CalcStopLevel(sig, entryPx);   // from HiLoRiderStopModes.cs
            if (StopMode == RPStopMode.HighLow)
                return CalcStopLevel(sig, entryPx);

            // Default (StopMode=ChannelBand, repurposed for this bot): opposite
            // HiLoBands channel band -/+ buffer -- NOT the old Low[pivotBarsAgo]/
            // High[pivotBarsAgo] swing-bar logic other fleet bots use for this
            // same enum value. Panel label reads "Channel Band" for this bot.
            if (hiloInd != null && hiloInd.UpperBand.IsValidDataPoint(0) && hiloInd.LowerBand.IsValidDataPoint(0))
            {
                double buf = StopBufferTicks * TickSize;
                double raw = (sig == 1)
                    ? hiloInd.LowerBand[0] - buf
                    : hiloInd.UpperBand[0] + buf;
                raw = Instrument.MasterInstrument.RoundToTickSize(raw);

                bool valid = (sig == 1) ? raw < entryPx : raw > entryPx;
                if (valid) return raw;
            }

            double fallback2 = entryPx + (sig == 1 ? -1 : 1) * FixedSLTicks * TickSize;
            return Instrument.MasterInstrument.RoundToTickSize(fallback2);
        }

        // ── CacheIndicatorValues (called IsFirstTickOfBar) ─────────────────────
        private void CacheIndicatorValues()
        {
            UpdateBarRangeCache();
            int bo = State == State.Realtime ? 1 : 0;

            pm1?.Update();
            atr1?.Update();
            adx1?.Update();
            hiloInd?.Update();

            if (pm1 != null && pm1.IsValidDataPoint(bo))
            {
                cachedDiff = pm1.DiffHistogram != null
                           ? pm1.DiffHistogram[bo]
                           : 0;

                double dynMax = pm1.GetDynamicMax(bo);
                if (dynMax > 0)
                {
                    cachedAdaptiveFloor   = dynMax * (DiffMinPercent / 100.0);
                    cachedAdaptiveExhaust = dynMax * (DiffMaxPercent / 100.0);
                }
            }
        }

        // ── SnapshotEntryIndicators ────────────────────────────────────────────
        private void SnapshotEntryIndicators(string sigSource = "HiLoRider", int signalBarsAgo = 1)
        {
            entryAdx            = (adx1 != null && adx1.IsValidDataPoint(signalBarsAgo)) ? adx1[signalBarsAgo] : 0;
            entryDiff           = cachedDiff;
            entryBar1RangeTicks = CurrentBar >= signalBarsAgo + 1
                                ? (High[signalBarsAgo + 1] - Low[signalBarsAgo + 1]) / TickSize : 0;
            entryBar0RangeTicks = CurrentBar >= signalBarsAgo
                                ? (High[signalBarsAgo] - Low[signalBarsAgo]) / TickSize : 0;
            entryTimeSnapshot   = Time[signalBarsAgo];
            entryBarsInTrade    = 0;
            entrySignalSource   = sigSource;
            CacheMlEntryFeatureSnapshot(tradeDir, sigSource);
        }

        // ── CheckDiffCrossAutoExit ─────────────────────────────────────────────
        private bool CheckDiffCrossAutoExit(int posDir)
        {
            if (AutoExitMode != HiLoRiderAutoExit.DiffCross) return false;
            if (pm1 == null || !pm1.IsValidDataPoint(0)) return false;
            if (!_diffConfirmedSinceEntry) return false;
            return posDir == 1 ? cachedDiff < 0 : cachedDiff > 0;
        }

        // ── OnStateChange ──────────────────────────────────────────────────────
        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "HiLoRider v1.2 (Aug 2026) — completed-bar HiLoBands crossover/slope " +
                              "cascade for WaveBars Value=120. Validated profiles are applied in " +
                              "State.DataLoaded when UseInstrumentProfileDefaults is enabled: " +
                              "MNQ Lookback=25, StopBuffer=2, Target=120, TrailMin=40, stages 5/6/10; " +
                              "MGC Lookback=10, StopBuffer=2, Target=120, TrailMin=4, stages 3/5/10. " +
                              "Momentum and ADX entry gates ship disabled. Other instruments retain " +
                              "unvalidated SetDefaults values and emit a warning.";
                Name        = "HiLoRider";

                Calculate                    = Calculate.OnEachTick;
                IsExitOnSessionCloseStrategy = true;
                ExitOnSessionCloseSeconds    = 30;
                IsFillLimitOnTouch           = false;
                IsUnmanaged                  = true;
                IsAdoptAccountPositionAware  = true;
                MaximumBarsLookBack          = NinjaTrader.NinjaScript.MaximumBarsLookBack.Infinite;
                BarsRequiredToTrade          = 20;
                OrderFillResolution          = OrderFillResolution.Standard;
                Slippage                     = 0;
                StartBehavior                = StartBehavior.WaitUntilFlat;
                TimeInForce                  = TimeInForce.Gtc;
                TraceOrders                  = false;
                // RealtimeErrorHandling.IgnoreAllErrors (documented Aug 2026 -- was
                // previously set with no explanatory comment anywhere in source, flagged
                // during a fleet-wide audit against NinjaTrader's own "Unmanaged Approach"
                // documentation). Overrides NT8's own default safety net
                // (RealtimeErrorHandling.StopCancelClose auto-stops the strategy, cancels
                // remaining orders, and closes the position on ANY broker order rejection).
                // Deliberate: this bot's own OnOrderUpdate has substantial custom rejection
                // handling of its own (entry-order partial-fill/reject recovery, the Bug
                // 8b/8c synthetic-stop watchdog, Bug 10 zombie-stop sibling-order cleanup,
                // the Finding 5 stuck-autoExitPending fix + its DiffCross/ribbon-cross
                // extension) that NT8's own blunt auto-stop-cancel-close would otherwise
                // fight or override. NOT guaranteed exhaustive -- the Finding 5 DiffX gap
                // was a real, live hole in this exact coverage until it was found and fixed
                // (Aug 2026). See CLAUDE.md "Fleet-Wide Naked-Position Fix" / "Other audit
                // findings from the same pass" for the full reasoning and evidence.
                RealtimeErrorHandling        = RealtimeErrorHandling.IgnoreAllErrors;
                // Aug 2026: HUD disabled by default fleet-wide -- see the
                // EnableTextHud property's own comment for the full performance reasoning.
                EnableTextHud = false;
                StopTargetHandling           = StopTargetHandling.PerEntryExecution;

                // ── Entry windows ─────────────────────────────────────────────
                EntryWindowStart = 1800;
                RTHStartHour     = 9;
                RTHStartMinute   = 30;
                RTHWindowEnd     = 1130;
                RTHOpenBlockMins = 15;  // Aldridge HFT: block first 15 min of RTH

                // ── Order ─────────────────────────────────────────────────────
                Contracts        = 1;
                LimitOffsetTicks = 12;   // Original HiLoRider calibrated default; unchanged by the safety port.
                MaxEntrySpreadTicks = 3; // exact bid/ask gate; equality passes; invalid quotes fail closed
                MaxDailyTrades   = 20;
                OpenForFreeTrade = true;

                // ── Daily limits ──────────────────────────────────────────────
                EnableDailyLossLimit   = false;
                DailyLossLimit         = 1000.0;
                EnableDailyProfitLimit = false;
                DailyProfitLimit       = 1500.0;

                // ── Risk ──────────────────────────────────────────────────────
                // FixedSLTicks here is a FALLBACK ONLY -- used by GetHiLoRiderStopPrice()
                // if the ChannelBand stop ever comes out invalid (e.g. indicator not warmed
                // up yet). Not the primary stop distance under the validated default.
                FixedSLTicks         = 60;
                // TrailMinTicks IS genuinely live -- TrailMode below is HighLow (the fleet
                // bar-count 3-stage trail), NOT MidlineOffset; this is the Stage-2 trail
                // buffer + the floor-clamp minimum distance from entry (ManageHighLowTrail()).
                TrailMinTicks        = 4;
                // FixedTPTicks: Aug 2026, HiLoRider_MGC_Wave120_Backtest.py -- real Wave-120
                // MGC data (12,132 bars, first-ever real Wave-bar export in this project),
                // Period=10 fixed, sequential TP->StopBuffer->TrailMin->HLStage2/3->
                // HLStage3Trail sweep with a WR>90% degenerate-artifact filter (a target
                // tighter than typical single-bar noise looked spectacular but was a near-
                // guaranteed-win illusion, rejected -- same class of artifact already
                // documented elsewhere in this fleet, e.g. RangeRider's own target sweep).
                // Winner: N=4,476 Sharpe=18.218 WR=87.1% PF=7.24 CI-lo=17.357 perm_p=0.003 PASS.
                TargetMode      = RPTargetMode.NoTarget;
                FixedTPTicks    = 120;
                MaxBarsInTrade  = 14;     // confirmed dead parameter in this sweep (5-50 all identical) -- trades resolve via stop/target first

                // StopBufferTicks IS the primary stop-distance driver under StopMode=ChannelBand
                // (opposite channel band -/+ this many ticks) AND the Stage-1 HighLow trail
                // buffer (dual-purposed in ManageHighLowTrail()). Re-swept Aug 2026 on real
                // Wave-120 MGC data at Period=10/TP=120: 2t won cleanly (same monotonic-
                // decline-as-buffer-widens shape as the original genesis sweep).
                StopBufferTicks      = 2;
                StopMarketBufferTicks = 2;

                // ── Breakeven ─────────────────────────────────────────────────
                BE_TriggerTicks = 9999;  // BE off — matches "no trail, pure fixed SL/TP" backtest
                BE_OffsetTicks  = 10;

                // ── Cooldown ──────────────────────────────────────────────────
                CooldownBars   = 0;

                // ── PriceMomentum (entry filter / ML) ─────────────────────────
                // Untested for this signal -- HiLoRider_MGC_Backtest.py did not sweep the
                // diff-band filter at all, unlike LiquidRider's own (different-signal)
                // validated 70/95 value this bot was cloned from. Ships OFF, same caveat
                // pattern used fleet-wide for an unswept filter (e.g. VWAPFader/LinRegRider's
                // own genesis defaults). pm1/cachedDiff still computed for ML features.
                EnableDiffBandFilter = false;
                BarsToHold     = 3;      // fleet-standard value -- inert while the filter above is off
                ShowPM         = false;
                DiffLookback   = 20;
                DiffMinPercent = 40;     // fleet-standard value -- inert while the filter above is off
                DiffMaxPercent = 100;

                // ── HiLoRider trend engine ───────────────────────────────────────
                // DiffCross auto-exit disabled: the validating backtest only modeled
                // fixed SL/TP/trail exits — enabling DiffCross here would add live
                // behavior beyond what was actually backtested.
                AutoExitMode    = HiLoRiderAutoExit.Disabled;

                // ADX filter: OFF by default. Untested for this signal (genesis
                // backtest, HiLoRider_MGC_Backtest.py, did not sweep ADX).
                EnableADXFilter   = false;
                ADXMinimum        = 0.0;
                ADXMaximum        = 100.0;

                // ── HiLoBands channel engine ──────────────────────────────────
                // Passed straight through to HiLoBands. DataLoaded applies the current
                // Wave-120 profile when instrument defaults are enabled (MNQ=25, MGC=10).
                LookbackPeriod       = 10;
                HiLoWidth            = 2;
                ShowHiLoBands        = true;

                // ── Stop & target modes ───────────────────────────────────────
                // The ChannelBand enum value is intentionally routed to the HiLo channel-
                // band stop by GetHiLoRiderStopPrice(). The current deployed trail is the
                // fleet HighLow three-stage mode below, not the older MidlineOffset study.
                StopMode        = RPStopMode.ChannelBand;   // see GetHiLoRiderStopPrice()
                MinStopTicks    = 5;
                HLInitialStopLookbackBars = 2;   // unused under ChannelBand/MidlineOffset -- HighLow-mode-only property, fleet-standard value kept for interface parity
                ATRPeriod       = 14;
                SLATRMultiplier = 1.25;   // unused — ATR stop mode not selected
                TPATRMultiplier = 3.5;    // unused — ATR target mode not selected
                RiskRewardRatio = 3.00;   // unused — RiskReward target mode not selected
                ATRSLMinTicks   = 10;     // unused — ATR stop mode not selected

                // ── Trail ──────────────────────────────────────────────────────
                // TrailMode is genuinely HighLow (the fleet bar-count 3-stage trail,
                // ManageHighLowTrail() below) -- NOT MidlineOffset. TrailBarsBeforeTrail/
                // TrailOffsetTicks (HiLoRiderEntryFilters.cs) only drive
                // ManageMidlineOffsetTrail(), which is never called while TrailMode=HighLow --
                // both are genuinely INERT here, kept only for interface parity / in case
                // TrailMode is ever switched back.
                TrailMode            = RPTrailMode.MidlineOffset;
                TrailBarsBeforeTrail = 1;    // inert under TrailMode=HighLow (MidlineOffset-only property)
                TrailOffsetTicks     = 0;    // inert under TrailMode=HighLow (MidlineOffset-only property)
                HLStage1LookbackBars = 1;
                // HLStage2/3TriggerBars, HLStage3TrailTicks: Aug 2026,
                // HiLoRider_MGC_Wave120_Backtest.py -- independently re-swept for this bot's
                // own signal on real Wave-120 MGC data at Period=10/TP=120/StopBuf=2t (the
                // earlier "ported fleet-wide, NOT independently backtested" flag is now
                // resolved). All three showed only minor sensitivity once TP=120 resolves
                // most trades before Stage 2/3 can bind -- winner reported anyway, not a
                // fragile pick (full battery: N=4,476 Sharpe=18.218 CI-lo=17.357 PASS).
                HLStage2TriggerBars  = 3;
                HLStage2LookbackBars = 0;
                HLStage3TriggerBars  = 5;
                HLStage3TrailTicks   = 10;

                // ── ATR trail multipliers ─────────────────────────────────────
                ATRTrailStage1Mult       = 1.50;
                ATRTrailStage2Mult       = 1.25;
                ATRTrailStage3Mult       = 1.0;
                ATRTrailStage4Mult       = 0.75;
                ATRTrailStage4TriggerPct = 40;

                // ── Staged trail ──────────────────────────────────────────────
                Stage1ActivationTicks = 180;
                Stage1TrailTicks      = 170;
                Stage2TriggerPct      = 50;
                Stage2TrailTicks      = 150;
                Stage3TriggerPct      = 75;
                Stage3TrailTicks      = 120;

                // ── FixedStop+Trail ───────────────────────────────────────────
                FST_Stage1TriggerPct  = 25;
                FST_Stage1OffsetTicks = 170;
                FST_Stage2TriggerPct  = 50;
                FST_Stage2LockPct     = 25;
                FST_Stage3TriggerPct  = 75;
                FST_Stage3LockPct     = 60;
                FST_Stage4TriggerPct  = 85;
                FST_Stage4TrailTicks  = 80;

                // ── Scale-out ─────────────────────────────────────────────────
                ScaleOutMode   = ScaleOutMode.Disabled;
                ScaleOut1Qty   = 1;
                ScaleOut2Qty   = 1;
                ScaleOut1Bars  = 7;
                ScaleOut2Bars  = 14;
                ScaleOutTicks1 = 180;
                ScaleOutTicks2 = 360;

                // ── DailyRange indicator ───────────────────────────────────────
                ShowDR                        = false;
                DR_RangeLookbackDays          = 5;
                DR_UseMedian                  = true;
                DR_RevAtrMultiplier           = 8.0;
                DR_TrendDisplacementAtrFilter = 0.0;

                // ── Position sizing & risk ────────────────────────────────────
                EnableVolatilitySizing = false;
                TradingCapital         = 25000.0;
                AnnualVolTargetPct     = 0.15;
                ContractPointValue     = 2.0;
                MinContracts           = 1;
                MaxContracts           = 10;
                EnableRollingCapital   = false;
                RatchetFraction        = 0.50;
                MaxConsecutiveLosses   = 0;   // disabled -- explicit user instruction, Jul 2026
                EnableDrawdownGate     = false;
                DrawdownFromPeakPct    = 5.0;

                // ── Meta-label ────────────────────────────────────────────────
                EnableMetaLabel = false;
                MetaThreshold   = 0.55;
                MetaModelPath   = @"C:\HiLoRider\models\meta_model.json";

                // ── Machine learning ──────────────────────────────────────────
                EnableML         = false;
                MLThreshold      = 0.52;
                MLMinSamples     = 10;
                MLWindowSize     = 60;
                MLLearningRate   = 0.08;
                MLRegularisation = 0.001;

                // ── Entry filters ─────────────────────────────────────────────
                EnableMidDayBlock = false;  // not independently tested for this signal — off, matching the all-hours backtest
                MidDayBlockStart  = 1130;
                MidDayBlockEnd    = 1330;
                EnableGapFilter   = true;
                GapFilterPct      = 0.50;
                GapBlockMinutes   = 30;
                EnableNewsBlock   = false;
                NewsBlockMins     = 5;
                NewsTime1         = 830;
                NewsTime2         = 1400;
                NewsTime3         = 1000;
                NewsTime4         = 0;
                // Validated Aug 2026 (HiLoRider_DiffFilter_Backtest.py, DiffMinPercent=10 —
                // see the PriceMomentum SetDefaults comment above for the full result).
                EnableHourBlock   = true;
                HourBlockStart    = 1;
                HourBlockEnd      = 5;
                // Jul 2026, explicit user instruction: require Close[0] vs Close[1] to
                // confirm the signal's direction before entering.
                EnableCloseConfirmation = true;
                // ── HMM regime (off by default) ──────────────────────────────
                EnableHMM         = false;
                HMMWindowBars     = 240;
                HMMUpdateBars     = 240;
                RegimeBullSizePct = 100;
                RegimeBearSizePct = 60;
                RegimeBearNoLongs = false;

                // ── Trade logging ─────────────────────────────────────────────
                EnableTradeLogging  = true;
                InstanceTag = "";
                EnableChartUI = true;
                PlaybackFastMode = false;
                DailyLimitScope = HiLoRiderDailyLimitScopeType.PerAccount;
                EnableHistoricalTradeLogging = true;
                UseInstrumentProfileDefaults = true;
                TradeLogPath        = "";
                DashboardScriptPath = "";
                PythonExePath       = "python";

                // ── GEX Regime ────────────────────────────────────────────────
                EnableGEXFilter        = false;
                GEXStalenessHours      = 4.0;
                GEXCallWallGateEnabled = true;
                GEXPutWallGateEnabled  = true;

                // ── Session & Time Control ────────────────────────────────────
                // Session-window restriction was NOT tested in the validating
                // backtest (ran all-hours) -- TF7 (00:00-23:59 catch-all) already
                // covers all hours regardless of the other toggles below. A future
                // window sweep (like the ones already run for Reaper/MomoMaster)
                // could improve on this; flagged as an open item, not blocking.
                EnableTF2 = true;
                EnableTF3 = true;
                EnableTF4 = true;
                EnableTF5 = true;
                EnableTF6 = true;
                EnableTF7 = true;
                TF1Start  = DateTime.Parse("08:00", System.Globalization.CultureInfo.InvariantCulture);
                TF1End    = DateTime.Parse("10:00", System.Globalization.CultureInfo.InvariantCulture);
                TF2Start  = DateTime.Parse("11:00", System.Globalization.CultureInfo.InvariantCulture);
                TF2End    = DateTime.Parse("13:00", System.Globalization.CultureInfo.InvariantCulture);
                TF3Start  = DateTime.Parse("13:00", System.Globalization.CultureInfo.InvariantCulture);
                TF3End    = DateTime.Parse("15:00", System.Globalization.CultureInfo.InvariantCulture);
                TF4Start  = DateTime.Parse("19:00", System.Globalization.CultureInfo.InvariantCulture);
                TF4End    = DateTime.Parse("21:00", System.Globalization.CultureInfo.InvariantCulture);
                TF5Start  = DateTime.Parse("23:00", System.Globalization.CultureInfo.InvariantCulture);
                TF5End    = DateTime.Parse("02:00", System.Globalization.CultureInfo.InvariantCulture);
                TF6Start  = DateTime.Parse("03:00", System.Globalization.CultureInfo.InvariantCulture);
                TF6End    = DateTime.Parse("05:00", System.Globalization.CultureInfo.InvariantCulture);
                TF7Start  = DateTime.Parse("00:00", System.Globalization.CultureInfo.InvariantCulture);
                TF7End    = DateTime.Parse("23:59", System.Globalization.CultureInfo.InvariantCulture);

            }
            else if (State == State.Configure)
            {
                // Infinite is deliberate. Several strategy/indicator paths use
                // IsValidDataPoint(), which NT8 rejects with TwoHundredFiftySix.
                MaximumBarsLookBack = NinjaTrader.NinjaScript.MaximumBarsLookBack.Infinite;
            }
            else if (State == State.DataLoaded)
            {
                // Retain the new strategy's validated Wave-120 profiles. The switch
                // prevents DataLoaded from overwriting values during controlled optimizer
                // or manual parameter sweeps.
                string instr = Instrument?.MasterInstrument?.Name ?? "";
                if (UseInstrumentProfileDefaults
                    && instr.IndexOf("MNQ", StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    LookbackPeriod       = 5;
                    StopBufferTicks      = 2;
                    FixedTPTicks         = 120;
                    TrailBarsBeforeTrail = 1;
                    TrailOffsetTicks     = 0;
                    TrailMinTicks        = 40;
                    HLStage2TriggerBars  = 5;
                    HLStage3TriggerBars  = 6;
                    HLStage3TrailTicks   = 10;
                    Print("[HiLoRider] MNQ validated Wave-120 profile applied -- Lookback=25, " +
                          "StopBuffer=2, Target=120, TrailMin=40, HLStage2=5, HLStage3=6, HLStage3Trail=10");
                }
                else if (UseInstrumentProfileDefaults
                    && instr.IndexOf("MGC", StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    LookbackPeriod       = 8;
                    StopBufferTicks      = 2;
                    FixedTPTicks         = 120;
                    TrailBarsBeforeTrail = 1;
                    TrailOffsetTicks     = 0;
                    TrailMinTicks        = 4;
                    HLStage2TriggerBars  = 3;
                    HLStage3TriggerBars  = 5;
                    HLStage3TrailTicks   = 10;
                    Print("[HiLoRider] MGC validated Wave-120 profile applied -- Lookback=10, " +
                          "StopBuffer=2, Target=120, TrailMin=4, HLStage2=3, HLStage3=5, HLStage3Trail=10");
                }
                else if (UseInstrumentProfileDefaults)
                {
                    Print($"[HiLoRider] WARNING: instrument '{instr}' is neither MNQ nor MGC -- " +
                          "no validated profile exists; SetDefaults values are retained.");
                }

                // ── PriceMomentum — always init (used for DiffCross exit + ML) ─
                pm1 = (PMIndicator)this.PriceMomentum(DiffLookback, DiffMinPercent, DiffMaxPercent);
                if (ShowPM && !PlaybackFastMode) AddChartIndicator(pm1);

                // ── ATR / ADX ─────────────────────────────────────────────────
                atr1 = ATR(BarsArray[0], ATRPeriod);
                adx1 = ADX(BarsArray[0], 14);

                // ── DailyRange indicator ───────────────────────────────────────
                dr1 = (DRIndicator)this.DailyRange(
                    DR_RangeLookbackDays,
                    DR_UseMedian,
                    5,    // MinDisplacementTicks
                    100, 1.0, true,
                    true, true, 60, 40); // preserve v1.1 previous-close and late-day-zone behavior
                if (ShowDR && !PlaybackFastMode) AddChartIndicator(dr1);

                // ── SuperTrendRegime (informational + regime badge) ─────────────
                superTrend1 = this.SuperTrendRegime(false, false, false, 630, RegimeTimeFrame.Day);
                if (ShowSuperTrend && !PlaybackFastMode) AddChartIndicator(superTrend1);

                // ── HiLoBands (entry signal + stop basis + trail basis) ──────────
                // Argument order matches HiLoBands.cs's own [NinjaScriptProperty]
                // declaration order: LookbackPeriod, Width.
                hiloInd = this.HiLoBands(LookbackPeriod, HiLoWidth, !PlaybackFastMode);
                hiloInd.SetPlaybackFastMode(PlaybackFastMode, false);
                if (ShowHiLoBands) AddChartIndicator(hiloInd);

                // ── Meta-label ─────────────────────────────────────────────────
                LoadMetaModel();

                // ── Trade loggers — init here so both Historical and Realtime ──
                // have open streams.  TryLogTrade routes by State internally.
                InitializeExecutionSafety();
                InitializeTradeLoggers();

                LoadEconomicCalendar();
                LoadGEXData();
            }
            else if (State == State.Realtime)
            {
                BeginOperationalRealtime();
                HandleRealtimeSafetyTransition();
                TryBootstrapMlFromJsonl();

                try { _initialAllTradesCount = SystemPerformance?.AllTrades?.Count ?? 0; }
                catch { _initialAllTradesCount = 0; }
                // Aug 2026: NT8 always runs State.Historical first when a strategy is
                // enabled/added to a chart (independent of Market Replay), replaying the
                // chart's own signal/entry logic against the loaded historical bars. If a
                // signal fires on one of the LAST few historical bars, the resulting
                // simulated "trade" has no chance to close before historical data runs out
                // -- leaving entryOrder/stopOrder/targetOrder/filledPrice/tradeDir etc.
                // (plain strategy-owned fields, never auto-reset by NT8 at this transition)
                // stuck holding that unfinished trade's stale values. The entry-evaluation
                // gate further down ("if (entryOrder != null) return; if (filledPrice > 0)
                // return;") then silently blocks every new signal from here on, even though
                // the REAL account is genuinely flat -- matching the live-reported symptom
                // exactly (a real position never existed; the user had to manually flatten
                // before the bot would trade again). Fixed and validated first on DaMaster.
                //
                // Gated on Position.MarketPosition == Flat so this can NEVER clear tracking
                // for a REAL, currently-open position (the one scenario where staying
                // "stuck" is the correct, safety-preserving behavior, e.g. StartBehavior=
                // WaitUntilFlat protecting against stacking a new entry on an existing one).
                if (Position.MarketPosition == MarketPosition.Flat)
                {
                    entryOrder               = null;
                    _entryOrderSubmitBar     = -1;
                    stopOrder                = null;
                    targetOrder              = null;
                    filledPrice              = 0;
                    tradeDir                 = 0;
                    stopLevel                = 0;
                    targetLevel              = 0;
                    autoExitPending          = false;
                    _emergencyStopSubmitted  = false;
                    _pendingBracketPlacement = false;
                    beRealized               = false;
                    _diffConfirmedSinceEntry = false;
                    _hlPeakPrice             = 0;
                    entryBarsInTrade         = 0;
                }

                if (ChartControl != null)
                {
                    if (EnableChartUI)
                        ChartControl.Dispatcher.InvokeAsync(CreateWPFControls);
                    if (EnableTextHud && !PlaybackFastMode)
                        ChartControl.Dispatcher.InvokeAsync(BuildTextHudWindow);
                }
            }
            else if (State == State.Terminated)
            {
                DisposeTradeLoggers();
                if (ChartControl != null)
                {
                    ChartControl.Dispatcher.InvokeAsync(DisposeWPFControls);
                    ChartControl.Dispatcher.InvokeAsync(() => TearDownTextHudWindow());
                }
            }
        }

        // ── OnBarUpdate ───────────────────────────────────────────────────────
        protected override void OnBarUpdate()
        {
            if (BarsInProgress != 0) return;
            AdvanceExecutionSafety(Time[0]);
            AdvanceOperationalParity(Time[0]);

            // Safety 3.3.15: a max-speed same-timestamp entry/exit can deliver
            // its execution and flat-position callbacks out of order. Recheck on
            // the next completed-bar boundary, with the first bar of each session
            // acting as a hard fallback. Legitimate working entries and connection
            // loss gates remain fail-closed inside the reconciliation method.
            if (IsFirstTickOfBar && Position.MarketPosition == MarketPosition.Flat)
                ReconcileSettledFlatState(Bars.IsFirstBarOfSession
                    ? "session-start fallback" : "completed-bar fallback");

            TryRefreshEconCalendar();

            // SafetyTransfer: deferred protection and position/bracket reconciliation.
            if (_pendingBracketPlacement
                && Position.MarketPosition != MarketPosition.Flat
                && filledPrice > 0 && stopLevel > 0 && stopOrder == null)
                PlaceBracketOrders(_pendingBracketIsLong, _pendingBracketQty);
            if (stopOrder != null || Position.MarketPosition == MarketPosition.Flat)
                _pendingBracketPlacement = false;
            if (Position.MarketPosition != MarketPosition.Flat
                && stopOrder != null
                && GetOrderRemainingQuantity(stopOrder) != Position.Quantity)
                QueueProtectionReconciliation("position/bracket quantity drift");
            // ── Bug 8c: synthetic-stop watchdog ───────────────────────────────
            if (Position.MarketPosition != MarketPosition.Flat
                && filledPrice > 0
                && stopOrder == null
                && stopLevel > 0
                && !_emergencyStopSubmitted)
            {
                bool isLong  = (tradeDir ==  1);
                bool isShort = (tradeDir == -1);
                bool stopBreached = (isLong  && Low[0]  <= stopLevel)
                                 || (isShort && High[0] >= stopLevel);
                if (stopBreached)
                {
                    _emergencyStopSubmitted = true;
                    QueueProtectionFailureExit("synthetic stop breached without an active protective stop");
}
            }

            // ── Stale limit entry order (Aug 2026, explicit user instruction) ──
            // A LIMIT entry order that's still working (unfilled) a full bar after
            // it was submitted gets cancelled -- the setup it was chasing is stale
            // by then. Market entries never reach this (_entryOrderSubmitBar is only
            // set for limit orders -- see the submission block). Checked once per
            // bar (IsFirstTickOfBar) so a limit order gets its full first bar to
            // fill before being judged idle.
            if (entryOrder != null && !useMarketOrder && IsFirstTickOfBar
                && _entryOrderSubmitBar >= 0 && CurrentBar > _entryOrderSubmitBar)
            {
                try
                {
                    Print($"[HiLoRider] Limit entry idle for 1 bar (submitted bar {_entryOrderSubmitBar}, " +
                          $"now bar {CurrentBar}) -- cancelling.");
                    CancelOrder(entryOrder);
                }
                catch (Exception ex)
                {
                    Print($"[HiLoRider] CancelOrder (stale limit entry) failed: {ex.Message}");
                }
                // Cleared immediately so this doesn't re-fire every tick while the
                // cancel confirmation is in flight; OnOrderUpdate's own handling
                // (HandleSafetyOrderUpdate) nulls entryOrder out once it's confirmed.
                _entryOrderSubmitBar = -1;
            }

            // ── New session: reset daily state ────────────────────────────────
            if (Bars.IsFirstBarOfSession
                && Position.MarketPosition == MarketPosition.Flat
                && !_exitPending)
            {
                _tradesThisSession       = 0;
                _pendingBracketPlacement = false;
                _pendingBracketIsLong    = false;
                _pendingBracketQty       = 0;
                _emergencyStopSubmitted  = false;
                beRealized               = false;
                stagedTrailStage         = 0;
                _hlPeakPrice             = 0;
                tradeDir                 = 0;
                filledPrice              = 0;
                _diffConfirmedSinceEntry = false;
                bePriceTrigger           = 0;
                stopLevel                = 0;
                targetLevel              = 0;
                targetTicksHeld          = 0;
                autoExitPending          = false;
                lastExitBar              = -999;
                lastHiLoRiderSigBar         = -1;
                _chopPendingDir          = 0;
                _chopPendingBar          = 0;
                _autoPaused              = false;
                _autoPausedReason        = "";
                cachedDiff               = 0;
                cachedAdaptiveFloor      = 2.0;
                cachedAdaptiveExhaust    = 14.0;
                cachedATRForTrail        = 0;
                bar1RangeTicks  = 0;
                bar0RangeTicks  = 0;
                ResetSessionMfeMae();
                CaptureSessionBaseline();
                ClearConsecutivePause();
                // SafetyTransfer: operator enable state persists across session resets.
                InitSessionRiskManager();
                ResetEntryFilterState();
                CaptureGapAtSessionOpen();
                ResetHMMSessionState();
                LoadMetaModel();
            }

            // ── Warmup guard ───────────────────────────────────────────────────
            if (CurrentBar < 20) return;

            // ── Cache bar-close indicator values ──────────────────────────────
            if (IsFirstTickOfBar)
            {
                CacheIndicatorValues();
                CacheATRValue();
            }

            // Entries are completed-bar decisions. Fast Mode can skip redundant
            // later ticks only while flat; open-position management remains tick-by-tick.
            if (PlaybackFastMode
                && Position.MarketPosition == MarketPosition.Flat
                && !IsFirstTickOfBar)
                return;

            UpdateGapIfNeeded();
            UpdateHMM();
            if (!PlaybackFastMode)
                DetectHiLoRiderRegime();

            if (IsFirstTickOfBar && Position.MarketPosition != MarketPosition.Flat)
                entryBarsInTrade++;

            if (IsFirstTickOfBar && MaxBarsInTrade > 0 && entryBarsInTrade >= MaxBarsInTrade && !autoExitPending
                && Position.MarketPosition != MarketPosition.Flat)
            {
                SubmitFullExit("MaxBars Exit");
                DrawHUD(); UpdateLivePanel(); return;
}

            // ── Current bar range for HUD ─────────────────────────────────────
            bar0RangeTicks = (High[0] - Low[0]) / TickSize;

            // ── Manage open position ──────────────────────────────────────────
            if (Position.MarketPosition != MarketPosition.Flat)
            {
                bool isLongPos  = Position.MarketPosition == MarketPosition.Long;
                int  posDir     = isLongPos ? 1 : -1;

                // Enforce live daily limits on every position-management tick.
                string openLimitReason;
                if (!_autoPaused && TryGetDailyLimitBreach(out openLimitReason))
                {
                    _autoPaused = true;
                    _autoPausedReason = openLimitReason.IndexOf("PROFIT", StringComparison.Ordinal) >= 0
                        ? "PROFIT LIMIT" : "LOSS LIMIT";
                    currentRegimeLabel = openLimitReason;
                    SubmitFullExit("HLR LimitExit");
                    DrawHUD(); UpdateLivePanel(); return;
                }

                // ── Manual target check (historical only — avoids slow Limit order engine) ──
                if (State == State.Historical && targetLevel > 0 && !autoExitPending)
                {
                    bool targetHit = (isLongPos  && High[0] >= targetLevel)
                                  || (!isLongPos && Low[0]  <= targetLevel);
                    if (targetHit)
                    {
                        SubmitFullExit("HLR Target");
                        DrawHUD(); UpdateLivePanel(); return;
}
                }

                // Track Diff confirmation for long/short DiffCross exit guard
                if (!_diffConfirmedSinceEntry)
                {
                    if (posDir ==  1 && cachedDiff > 0) _diffConfirmedSinceEntry = true;
                    if (posDir == -1 && cachedDiff < 0) _diffConfirmedSinceEntry = true;
                }

                // ── Auto-exit checks ──────────────────────────────────────────
                if (!autoExitPending)
                {
                    bool doExit = false;

                    if (AutoExitMode == HiLoRiderAutoExit.DiffCross && !_isManualTrade)
                        doExit = CheckDiffCrossAutoExit(posDir);

                    if (doExit)
                    {
                        string exitLabel = isLongPos ? "HLR DiffX LX" : "HLR DiffX SX";
                        SubmitFullExit(exitLabel);
                        DrawHUD();
                        UpdateLivePanel();
                        return;
}
                }

                ManageTrail(Close[0]);
                CheckNewScaleOut();
                DrawHUD();
                UpdateLivePanel();
                return;
            }

            DrawHUD();
            UpdateLivePanel();

            // Completed-bar strategies have no new entry work on later flat ticks.
            if (!IsFirstTickOfBar) return;

            if (!IsEntryStateClear()) return;

            try
            {
                if (!IsInSessionWindow()) return;

                int timeET = ToTimeET(Time[0]);

                if (!OpenForFreeTrade && MaxDailyTrades > 0 && _tradesThisSession >= MaxDailyTrades) return;

                if (RTHOpenBlockMins > 0)
                {
                    int rthStart = RthStartHHMM * 100;
                    if (timeET >= rthStart)
                    {
                        DateTime etNow   = TimeZoneInfo.ConvertTimeFromUtc(Time[0].ToUniversalTime(), EasternTZ);
                        DateTime rthOpen = new DateTime(etNow.Year, etNow.Month, etNow.Day,
                                                        RTHStartHour, RTHStartMinute, 0);
                        if (etNow < rthOpen.AddMinutes(RTHOpenBlockMins)) return;
                    }
                }

                // Scoped daily limits: isolated per strategy instance, coordinated
                // per account, or disabled. Playback retains the established bypass.
                string dailyLimitReason;
                if (TryGetDailyLimitBreach(out dailyLimitReason))
                {
                    double sessionPnL = GetDailyLimitTotalPnl();
                    Print($"[HiLoRider] {dailyLimitReason}: P&L={sessionPnL:C0} Scope={DailyLimitScope}");
                    _autoPaused = true;
                    _autoPausedReason = dailyLimitReason.IndexOf("PROFIT", StringComparison.Ordinal) >= 0
                        ? "PROFIT LIMIT" : "LOSS LIMIT";
                    return;
                }

                if (!strategyEnabled) return;
                if (_autoPaused)      return;
                if ((CurrentBar - lastExitBar) <= CooldownBars) return;

                // Direction-agnostic entry filters
                if (!CheckMidDayBlock())    return;
                if (!CheckNewsBlock())      return;
                if (!CheckADXFilter())      return;
                if (!CheckHourBlock())      return;

                // ── Get signal (chop-hold aware, Aug 2026, ported from Reaper) ──
                // CheckDiffBandFilter() used to run BEFORE the signal was even
                // evaluated -- meaning a real midline crossover that fired during
                // chop was never even detected. That's still exactly what happens
                // at BarsToHold=0 (the default, unchanged live behavior). BarsToHold>0
                // adds a genuinely new behavior: instead of rejecting, the signal
                // is cached and re-checked each bar for up to BarsToHold bars for
                // diff to clear the floor IN THE SIGNAL'S OWN DIRECTION. The
                // ceiling/exhaustion half of the diff band filter is untouched --
                // still an immediate reject, no hold applied.
                int sig          = 0;
                int pivotBarsAgo = State == State.Realtime ? 1 : 0;

                if (_chopPendingDir != 0)
                {
                    int elapsed = CurrentBar - _chopPendingBar;
                    if (elapsed > BarsToHold)
                    {
                        _chopPendingDir = 0;   // expired, discard
                    }
                    else if (DiffConfirmsChopHold(_chopPendingDir))
                    {
                        sig             = _chopPendingDir;
                        pivotBarsAgo    = elapsed + (State == State.Realtime ? 1 : 0);
                        _chopPendingDir = 0;             // consume
                    }
                    else
                    {
                        return;   // still holding -- diff hasn't confirmed yet, don't evaluate a fresh signal this bar
                    }
                }

                if (sig == 0)
                {
                    if (EnableDiffBandFilter && cachedAdaptiveExhaust > 0
                        && Math.Abs(cachedDiff) >= cachedAdaptiveExhaust)
                        return;   // overextended -- always an immediate reject, no hold

                    int freshSig = GetHiLoRiderSignal();
                    if (freshSig == 0) return;

                    if (EnableDiffBandFilter && IsChoppy())
                    {
                        if (BarsToHold <= 0) return;   // no hold configured -- reject immediately (today's default)
                        _chopPendingDir = freshSig;
                        _chopPendingBar = CurrentBar;
                        return;   // don't fire yet -- wait for confirmation
                    }

                    sig = freshSig;
                }

                if (!CheckCloseConfirmation(sig)) return;

                // Direction gates (panel buttons)
                if (sig ==  1 && !longEnabled)  return;
                if (sig == -1 && !shortEnabled) return;
                if (sig ==  1 && !_econAllowLongs)  return;
                if (sig == -1 && !_econAllowShorts) return;

                if (!CheckGapFilter(sig))        return;
                if (!CheckGEXFilter(sig))        return;
                if (!CheckHMMDirectionGate(sig)) return;

                // ML gate
                if (!MLFilterPasses(sig, "HiLoRider")) return;

                // Meta-label gate
                if (!MetaLabelPasses(sig)) return;

                string liquidityBlockReason;
                bool liquidityPass = LiquidityFilterPasses(out liquidityBlockReason);
                LogLiquiditySignalCandidate(sig, liquidityPass, liquidityBlockReason);
                if (!liquidityPass) return;
                // ── Build stop and target ─────────────────────────────────────
                tradeDir = sig;

                double limitPx = (sig == 1)
                    ? GetCurrentBid(0) - LimitOffsetTicks * TickSize
                    : GetCurrentAsk(0) + LimitOffsetTicks * TickSize;

                bool noTarget = TargetMode == RPTargetMode.NoTarget;

                // NoTarget always rides HighLow trail
                if (noTarget && TrailMode != RPTrailMode.HighLow)
                    TrailMode = RPTrailMode.HighLow;

                targetLevel = noTarget ? 0 : CalcTargetLevel(sig, limitPx);
                targetTicksHeld = targetLevel > 0
                    ? Math.Max(1.0, Math.Abs(targetLevel - limitPx) / TickSize)
                    : FixedTPTicks;

                stopLevel = GetHiLoRiderStopPrice(sig, limitPx, pivotBarsAgo);

                {
                    bool stopOk = (sig == 1) ? stopLevel < limitPx : stopLevel > limitPx;
                    if (!stopOk)
                    {
                        double slFallback = FixedSLTicks * TickSize;
                        stopLevel = (sig == 1) ? limitPx - slFallback : limitPx + slFallback;
                        stopLevel = Instrument.MasterInstrument.RoundToTickSize(stopLevel);
                        Print($"[HiLoRider] Stop sanity fallback");
                    }
                }

                SnapshotEntryIndicators(_lastHiLoRiderSlot, pivotBarsAgo);

                int orderQty = GetSizedContracts();
                if (orderQty <= 0) return;

                ocoId = (State == State.Historical)
                    ? DateTime.Now.ToString("HHmmssff") + CurrentBar
                    : GetAtmStrategyUniqueId();

                try
                {
                    if (sig == 1)
                    {
                        entryOrder = useMarketOrder
                            ? SubmitOrderUnmanaged(0, OrderAction.Buy,
                                  OrderType.Market, orderQty, 0, 0, ocoId, "HLR LE")
                            : SubmitOrderUnmanaged(0, OrderAction.Buy,
                                  OrderType.Limit, orderQty, limitPx, 0, ocoId, "HLR LE");
                    }
                    else
                    {
                        entryOrder = useMarketOrder
                            ? SubmitOrderUnmanaged(0, OrderAction.SellShort,
                                  OrderType.Market, orderQty, 0, 0, ocoId, "HLR SE")
                            : SubmitOrderUnmanaged(0, OrderAction.SellShort,
                                  OrderType.Limit, orderQty, limitPx, 0, ocoId, "HLR SE");
                    }
                    // Aug 2026: only limit entries can "sit idly" -- market fills
                    // essentially immediately, so there's nothing to time out.
                    _entryOrderSubmitBar = (!useMarketOrder && entryOrder != null) ? CurrentBar : -1;
                }
                catch (Exception submitEx)
                {
                    entryOrder            = null;
                    _entryOrderSubmitBar  = -1;
                    Print($"[HiLoRider] Submit failed bar {CurrentBar}: {submitEx.Message}");
                }
            }
            catch (Exception ex)
            {
                Print($"[HiLoRider] OnBarUpdate exception bar {CurrentBar}: {ex.Message}");
            }
        }

        // ── ManageTrail ────────────────────────────────────────────────────────
        private void ManageTrail(double currentPrice)
        {
            if (stopOrder  == null) return;
            if (filledPrice == 0)   return;

            // Breakeven
            if (!beRealized)
            {
                bool beHit = (tradeDir == 1)
                    ? currentPrice >= bePriceTrigger
                    : currentPrice <= bePriceTrigger;

                if (beHit)
                {
                    double beStop = Instrument.MasterInstrument.RoundToTickSize(
                        filledPrice + (tradeDir == 1 ? 1 : -1) * BE_OffsetTicks * TickSize);
                    bool beBetter = (tradeDir == 1) ? beStop > stopLevel : beStop < stopLevel;
                    if (beBetter) { TryMoveStop(beStop); beRealized = true; }
                }
            }

            if      (TrailMode == RPTrailMode.HighLow)        ManageHighLowTrail();
            else if (TrailMode == RPTrailMode.MidlineOffset)   ManageMidlineOffsetTrail();
            else if (TrailMode == RPTrailMode.FixedStopTrail)  CheckFixedStopTrail();
            else if (TrailMode == RPTrailMode.ATR)             ManageATRTrail(currentPrice);
            else if (TrailMode == RPTrailMode.FixedStop)       { /* no trailing after BE */ }
            else                                               ManageStagedTicksTrail(currentPrice);
        }

        // ── ManageMidlineOffsetTrail ─────────────────────────────────────────────
        // Genesis-validated (HiLoRider_MGC_Backtest.py, Aug 2026): once
        // entryBarsInTrade >= TrailBarsBeforeTrail, the stop starts tracking the
        // HiLoBands channel midline -/+ TrailOffsetTicks every bar (tighten-only,
        // same universal invariant as every other trail mode in this fleet).
        // TrailOffsetTicks is intentionally clamped to >=0 in its own [Range] --
        // a negative value was confirmed to be an unbounded runaway artifact
        // (the stop crosses past the midline into the losing side, converging on
        // a near-immediate guaranteed-small-win exit, not a real edge). See the
        // module header for the full evidence.
        private void ManageMidlineOffsetTrail()
        {
            if (hiloInd == null) return;
            if (entryBarsInTrade < TrailBarsBeforeTrail) return;
            if (!hiloInd.MiddleLine.IsValidDataPoint(0)) return;

            double mid = hiloInd.MiddleLine[0];
            double offset = TrailOffsetTicks * TickSize;
            double candidate = (tradeDir == 1)
                ? Instrument.MasterInstrument.RoundToTickSize(mid - offset)
                : Instrument.MasterInstrument.RoundToTickSize(mid + offset);

            bool better = (tradeDir == 1) ? candidate > stopLevel : candidate < stopLevel;
            if (better) TryMoveStop(candidate);
        }

        private void ManageStagedTicksTrail(double currentPrice)
        {
            double profitTicks = (tradeDir == 1)
                ? (currentPrice - filledPrice) / TickSize
                : (filledPrice  - currentPrice) / TickSize;

            double stage2Trigger = (targetTicksHeld > 0)
                ? targetTicksHeld * Stage2TriggerPct / 100.0 : double.MaxValue;
            double stage3Trigger = (targetTicksHeld > 0)
                ? targetTicksHeld * Stage3TriggerPct / 100.0 : double.MaxValue;

            if      (profitTicks >= stage3Trigger && stagedTrailStage < 3) stagedTrailStage = 3;
            else if (profitTicks >= stage2Trigger && stagedTrailStage < 2) stagedTrailStage = 2;
            else if (profitTicks >= Stage1ActivationTicks && stagedTrailStage < 1) stagedTrailStage = 1;

            if (stagedTrailStage == 0) return;

            int ticks = stagedTrailStage == 1 ? Stage1TrailTicks
                      : stagedTrailStage == 2 ? Stage2TrailTicks
                      :                         Stage3TrailTicks;

            double trailStop = (tradeDir == 1)
                ? currentPrice - ticks * TickSize
                : currentPrice + ticks * TickSize;
            TryMoveStop(Instrument.MasterInstrument.RoundToTickSize(trailStop));
        }

        private void ManageHighLowTrail()
        {
            int maxLookback = Math.Max(HLStage1LookbackBars, HLStage2LookbackBars);
            if (CurrentBar <= maxLookback) return;

            if (_hlPeakPrice == 0) _hlPeakPrice = filledPrice;
            if (tradeDir ==  1 && High[0] > _hlPeakPrice) _hlPeakPrice = High[0];
            if (tradeDir == -1 && Low[0]  < _hlPeakPrice) _hlPeakPrice = Low[0];

            // Jul 2026 rebuild: Stage 2/3 triggers switched from profit-ticks to bar-count
            // since entry (entryBarsInTrade, the existing per-bar-in-trade counter).
            // HLStage2TriggerTicks/HLStage3TriggerTicks -> HLStage2TriggerBars(=3)/
            // HLStage3TriggerBars(=11). This also absorbs the old separate "N-bars-after-
            // entry" tightening rule (2 bars fleet-wide, 3 for LinRegRider/TrendMaster) --
            // Stage 2 now IS that tightening rule, formalized as a real stage instead of a
            // bolt-on override layered on top of the profit-based staging.
            if      (entryBarsInTrade >= HLStage3TriggerBars && stagedTrailStage < 3) stagedTrailStage = 3;
            else if (entryBarsInTrade >= HLStage2TriggerBars && stagedTrailStage < 2) stagedTrailStage = 2;
            else if (stagedTrailStage < 1)                                            stagedTrailStage = 1;

            double hlStop;
            if (stagedTrailStage == 3)
            {
                // Unchanged: rolling peak since entry +/- HLStage3TrailTicks (default 40).
                hlStop = (tradeDir == 1)
                    ? Instrument.MasterInstrument.RoundToTickSize(_hlPeakPrice - HLStage3TrailTicks * TickSize)
                    : Instrument.MasterInstrument.RoundToTickSize(_hlPeakPrice + HLStage3TrailTicks * TickSize);
            }
            else if (stagedTrailStage == 2)
            {
                // Stage 2 buffer is TrailMinTicks, not StopBufferTicks -- explicit Jul 2026
                // instruction: "when the trade is [HLStage2TriggerBars] bars after entry
                // move stop to Low[0]/High[0] +/- TrailMinTicks."
                int lb = HLStage2LookbackBars;
                if (lb > CurrentBar) return;
                double hlBuffer = TrailMinTicks * TickSize;
                hlStop = (tradeDir == 1)
                    ? Instrument.MasterInstrument.RoundToTickSize(Low[lb]  - hlBuffer)
                    : Instrument.MasterInstrument.RoundToTickSize(High[lb] + hlBuffer);
            }
            else
            {
                int lb = HLStage1LookbackBars;
                if (lb > CurrentBar) return;
                double hlBuffer = StopBufferTicks * TickSize;
                hlStop = (tradeDir == 1)
                    ? Instrument.MasterInstrument.RoundToTickSize(Low[lb]  - hlBuffer)
                    : Instrument.MasterInstrument.RoundToTickSize(High[lb] + hlBuffer);
            }
            // Floor: HighLow stop must be at least TrailMinTicks from entry. Independent
            // safety clamp, unchanged from the earlier decoupled-floor fix (see CLAUDE.md
            // "TrailMinTicks -- decoupled trail floor fix" for the full history) -- kept
            // even though Stage 2 above now also uses TrailMinTicks as its own buffer,
            // since this one is relative to entry price, not to Low[0]/High[0].
            double distTicks = Math.Abs(filledPrice - hlStop) / TickSize;
            if (distTicks < TrailMinTicks)
                hlStop = Instrument.MasterInstrument.RoundToTickSize(
                    filledPrice + (tradeDir == 1 ? -1 : 1) * TrailMinTicks * TickSize);

            TryMoveStop(hlStop);
        }

        private void ManageATRTrail(double currentPrice)
        {
            if (cachedATRForTrail <= 0) return;
            if (!beRealized)            return;

            double profitTicks = (tradeDir == 1)
                ? (currentPrice - filledPrice) / TickSize
                : (filledPrice  - currentPrice) / TickSize;

            double stage2Trigger = targetTicksHeld > 0
                ? targetTicksHeld * Stage2TriggerPct / 100.0 : double.MaxValue;
            double stage3Trigger = targetTicksHeld > 0
                ? targetTicksHeld * Stage3TriggerPct / 100.0 : double.MaxValue;
            double stage4Trigger = targetTicksHeld > 0
                ? targetTicksHeld * ATRTrailStage4TriggerPct / 100.0 : double.MaxValue;

            if      (profitTicks >= stage4Trigger         && stagedTrailStage < 4) stagedTrailStage = 4;
            else if (profitTicks >= stage3Trigger         && stagedTrailStage < 3) stagedTrailStage = 3;
            else if (profitTicks >= stage2Trigger         && stagedTrailStage < 2) stagedTrailStage = 2;
            else if (profitTicks >= Stage1ActivationTicks && stagedTrailStage < 1) stagedTrailStage = 1;

            if (stagedTrailStage == 0) return;

            double mult = stagedTrailStage == 1 ? ATRTrailStage1Mult
                        : stagedTrailStage == 2 ? ATRTrailStage2Mult
                        : stagedTrailStage == 3 ? ATRTrailStage3Mult
                        :                         ATRTrailStage4Mult;

            double trailStop = (tradeDir == 1)
                ? currentPrice - mult * cachedATRForTrail
                : currentPrice + mult * cachedATRForTrail;
            TryMoveStop(Instrument.MasterInstrument.RoundToTickSize(trailStop));
        }

        private void TryMoveStop(double newStop)
        {
            if (stopOrder == null || Position.MarketPosition == MarketPosition.Flat) return;
            double buffer = StopMarketBufferTicks * TickSize;
            bool isLong = tradeDir == 1;
            double marketRef = isLong ? GetCurrentBid() : GetCurrentAsk();
            if (marketRef <= 0) marketRef = Close[0];
            double safeCandidate = isLong
                ? Math.Min(newStop, Instrument.MasterInstrument.RoundToTickSize(marketRef - buffer))
                : Math.Max(newStop, Instrument.MasterInstrument.RoundToTickSize(marketRef + buffer));
            RequestStopChange(safeCandidate, true);
}

        // ── OnMarketData ──────────────────────────────────────────────────────
        protected override void OnMarketData(MarketDataEventArgs e)
        {
            TrackLiquidityMarketData(e);
            if (e.MarketDataType != MarketDataType.Last) return;
            if (CurrentBar < 20) return;

            // Update current bar range for HUD
            bar0RangeTicks = (High[0] - Low[0]) / TickSize;

            if (Position.MarketPosition == MarketPosition.Flat)
            {
                if (_emergencyStopSubmitted) _emergencyStopSubmitted = false;
                return;
            }
            if (filledPrice == 0) return;

            if (stopOrder == null)
            {
                if (!_emergencyStopSubmitted && stopLevel > 0)
                {
                    bool isLong  = (tradeDir ==  1);
                    bool isShort = (tradeDir == -1);
                    bool priceHitStop = (isLong  && e.Price <= stopLevel)
                                     || (isShort && e.Price >= stopLevel);
                    if (priceHitStop)
                    {
                        _emergencyStopSubmitted = true;
                        QueueProtectionFailureExit("synthetic stop crossed without a working protective stop");
}
                }
                return;
            }

            ManageTrail(e.Price);
            CheckNewScaleOut();
            if (TrailMode == RPTrailMode.FixedStopTrail)
                CheckFixedStopTrail();
        }

        // ── OnOrderUpdate ─────────────────────────────────────────────────────
        protected override void OnOrderUpdate(Order order,
            double limitPrice, double stopPrice,
            int quantity, int filled,
            double averageFillPrice,
            OrderState orderState,
            DateTime time, ErrorCode error, string comment)
        {
            HandleSafetyOrderUpdate(order, limitPrice, stopPrice, quantity, filled,
                averageFillPrice, orderState, time, error, comment);
}
        // OnConnectionStatusUpdate() (Aug 2026, from the NinjaScript "Unmanaged Approach"
        // doc audit -- validated on Reaper first, compiled clean, before this rollout).
        // Pauses new entries on loss, retains resting protection, and surfaces a
        // "CONN LOST" flag on the regime badge. Once both order and price connections
        // are restored, it clears only a pause owned by this connection handler and
        // immediately queues protection reconciliation for any open position. It does
        // not cancel protection or flatten merely because a connection event occurred.
        protected override void OnConnectionStatusUpdate(ConnectionStatusEventArgs connectionStatusUpdate)
        {
            _orderConnectionLost = connectionStatusUpdate.Status == ConnectionStatus.ConnectionLost
                                || connectionStatusUpdate.Status == ConnectionStatus.Disconnected;
            _priceConnectionLost = connectionStatusUpdate.PriceStatus == ConnectionStatus.ConnectionLost
                                || connectionStatusUpdate.PriceStatus == ConnectionStatus.Disconnected;

            if (_orderConnectionLost || _priceConnectionLost)
            {
                if (!_autoPaused)
                {
                    _autoPaused = true;
                    _autoPausedReason = "CONNECTION LOST";
                    _connectionPauseOwned = true;
                }
                Print("[HiLoRider] Connection lost; entries paused and resting protection retained.");
            }
            else if (connectionStatusUpdate.Status == ConnectionStatus.Connected
                  && connectionStatusUpdate.PriceStatus == ConnectionStatus.Connected)
            {
                if (_connectionPauseOwned)
                {
                    _autoPaused = false;
                    _autoPausedReason = "";
                    _connectionPauseOwned = false;
                }
                if (Position.MarketPosition != MarketPosition.Flat)
                    QueueProtectionReconciliation("connection restored");
                Print("[HiLoRider] Connections restored; protection reconciliation queued.");
            }
        }

        // Regime-badge sub-line suffix, same priority-suffix pattern as GetChopSubSuffix()/
        // GetEconSubSuffix() -- appended alongside them in the UI file, not a replacement
        // for any existing badge state.
        internal string GetConnectionSubSuffix()
        {
            if (_orderConnectionLost)  return "  ·  ORDER CONN LOST";
            if (_priceConnectionLost)  return "  ·  PRICE CONN LOST";
            return "";
        }


        // ── PlaceBracketOrders ────────────────────────────────────────────────
        private void PlaceBracketOrders(bool isLong, int qty)
        {
            ReconcileProtection(isLong, qty);
}

        // ── DetectHiLoRiderRegime ────────────────────────────────────────────────
        // ARMED fires when Close[0] is within a few ticks of the HiLoBands
        // midline -- a genuine crossover is close to happening on the next bar,
        // not a 3-bar-pivot pre-confirmation state (this signal has no such
        // intermediate stage of its own; a crossover either fires on close or
        // it doesn't).
        internal void DetectHiLoRiderRegime()
        {
            if (Position.MarketPosition != MarketPosition.Flat)
            {
                currentRegime      = 2;
                currentRegimeLabel = tradeDir == 1 ? "IN TRADE  ▲" : "IN TRADE  ▼";
                return;
            }

            if (!strategyEnabled)
            {
                currentRegime      = 3;
                currentRegimeLabel = "STRATEGY  OFF";
                return;
            }

            if (_autoPaused)
            {
                currentRegime      = 3;
                currentRegimeLabel = _autoPausedReason.Length > 0 ? _autoPausedReason : "PAUSED";
                return;
            }

            if (!IsInSessionWindow())
            {
                currentRegime      = 3;
                currentRegimeLabel = "OUTSIDE WINDOW";
                return;
            }

            if (CooldownBars > 0 && (CurrentBar - lastExitBar) < CooldownBars)
            {
                int rem = CooldownBars - (CurrentBar - lastExitBar);
                currentRegime      = 3;
                currentRegimeLabel = $"COOLDOWN  ({rem})";
                return;
            }

            if (hiloInd != null && hiloInd.MiddleLine.IsValidDataPoint(0))
            {
                double dist = Math.Abs(Close[0] - hiloInd.MiddleLine[0]) / TickSize;
                if (dist <= 5)
                {
                    currentRegime      = 1;
                    currentRegimeLabel = Close[0] >= hiloInd.MiddleLine[0] ? "ARMED  ▲ NEAR MID" : "ARMED  ▼ NEAR MID";
                    return;
                }
            }

            currentRegime      = 0;
            currentRegimeLabel = "READY";
            if (superTrend1 != null && superTrend1.IsValidDataPoint(0))
            {
                double superLine = superTrend1.SuperLine[0];
                if (Close[0] > superLine)      currentRegimeLabel = "READY  ·  TREND ▲";
                else if (Close[0] < superLine) currentRegimeLabel = "READY  ·  TREND ▼";
            }
        }

        // ═════════════════════════════════════════════════════════════════════
        // PROPERTIES
        // ═════════════════════════════════════════════════════════════════════
        #region Properties

        // ── 01. Order ─────────────────────────────────────────────────────────
        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "Contracts", Order = 1, GroupName = "01. Order")]
        public int Contracts { get; set; }

        [NinjaScriptProperty]
        [Range(0, 100)]
        [Display(Name = "Limit Offset Ticks", Order = 2, GroupName = "01. Order",
            Description = "Ticks behind bid (long) or ask (short) for limit entry. 0 = at market.")]
        public int LimitOffsetTicks { get; set; }

        [NinjaScriptProperty]
        [Range(0, 100)]
        [Display(Name = "Max Entry Spread Ticks", Order = 3, GroupName = "01. Order",
            Description = "Blocks entries when current ask-minus-bid exceeds this value. Equality passes. 0 disables. Invalid, zero, inverted, stale, or non-finite quotes fail closed when enabled.")]
        public int MaxEntrySpreadTicks { get; set; }
        [NinjaScriptProperty]
        [Display(Name = "Open For Free Trade", Order = 3, GroupName = "01. Order")]
        public bool OpenForFreeTrade { get; set; }

        [NinjaScriptProperty]
        [Range(0, 50)]
        [Display(Name = "Max Daily Trades (0 = unlimited)", Order = 4, GroupName = "01. Order")]
        public int MaxDailyTrades { get; set; }

        // ── 02. Stop ──────────────────────────────────────────────────────────
        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "Fixed SL Ticks", Order = 1, GroupName = "02. Stop",
            Description = "Fixed fallback stop distance in ticks.  Also used for FixedTick stop mode.")]
        public int FixedSLTicks { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "Trail Min Ticks", Order = 2, GroupName = "02. Stop",
            Description = "HighLow trail's minimum stop distance from entry -- decoupled from " +
                "FixedSLTicks (Jul 2026 fix; see ManageHighLowTrail floor comment). Validated 20t " +
                "via TrailFloorDecouple_Backtest.py.")]
        public int TrailMinTicks { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "Min Stop Ticks", Order = 1, GroupName = "02. Stop",
            Description = "HighLow stop: minimum swing-stop distance (ticks). If the swing stop is closer than this, fall back to the fixed stop.")]
        public int MinStopTicks { get; set; }

        [NinjaScriptProperty]
        [Range(1, 5)]
        [Display(Name = "HL Initial Stop Lookback Bars", Order = 13, GroupName = "02. Stop",
            Description = "GetSwingBasedStop's basis bar: Low[N] (long) / High[N] (short), replacing " +
                "the old min-of-Low[1]/Low[0] basis. Ported from RangeRider's own genesis backtest " +
                "(Lookback=2 won a joint sweep vs TrailMinTicks, Aug 2026) -- NOT independently " +
                "validated for this bot's own signal.")]
        public int HLInitialStopLookbackBars { get; set; }

        [NinjaScriptProperty]
        [Range(0, 200)]
        [Display(Name = "Reversal Bar Stop Buffer Ticks", Order = 2, GroupName = "02. Stop",
            Description = "Buffer added beyond Low[1] (long) or High[1] (short) for the reversal-bar stop.")]
        public int StopBufferTicks { get; set; }

        [NinjaScriptProperty]
        [Range(0, 50)]
        [Display(Name = "Stop Market Buffer Ticks", Order = 3, GroupName = "02. Stop")]
        public int StopMarketBufferTicks { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "Fixed TP Ticks", Order = 4, GroupName = "02. Stop")]
        public int FixedTPTicks { get; set; }

        [NinjaScriptProperty]
        [Range(0, 99999)]
        [Display(Name = "BE Trigger Ticks", Order = 5, GroupName = "02. Stop")]
        public int BE_TriggerTicks { get; set; }

        [NinjaScriptProperty]
        [Range(0, 100)]
        [Display(Name = "BE Offset Ticks", Order = 6, GroupName = "02. Stop")]
        public int BE_OffsetTicks { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Enable Daily Loss Limit", Order = 7, GroupName = "02. Stop")]
        public bool EnableDailyLossLimit { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name = "Daily Loss Limit ($)", Order = 8, GroupName = "02. Stop")]
        public double DailyLossLimit { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Enable Daily Profit Limit", Order = 9, GroupName = "02. Stop")]
        public bool EnableDailyProfitLimit { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name = "Daily Profit Limit ($)", Order = 10, GroupName = "02. Stop")]
        public double DailyProfitLimit { get; set; }

        [NinjaScriptProperty]
        [Range(0, 50)]
        [Display(Name = "Cooldown Bars After Exit", Order = 11, GroupName = "02. Stop")]
        public int CooldownBars { get; set; }

        [NinjaScriptProperty]
        [Range(0, 200)]
        [Display(Name = "Max Bars In Trade (0 = off)", Order = 12, GroupName = "02. Stop")]
        public int MaxBarsInTrade { get; set; }

        // ── 03. Trail ─────────────────────────────────────────────────────────
        [NinjaScriptProperty]
        [Display(Name = "Trail Mode", Order = 1, GroupName = "03. Trail")]
        public RPTrailMode TrailMode { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "Stage 1 Activation Ticks", Order = 2, GroupName = "03. Trail")]
        public int Stage1ActivationTicks { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "Stage 1 Trail Ticks", Order = 3, GroupName = "03. Trail")]
        public int Stage1TrailTicks { get; set; }

        [NinjaScriptProperty][Range(1, 99)]
        [Display(Name = "Stage 2 Trigger (%)", Order = 4, GroupName = "03. Trail")]
        public int Stage2TriggerPct { get; set; }

        [NinjaScriptProperty][Range(1, int.MaxValue)]
        [Display(Name = "Stage 2 Trail Ticks", Order = 5, GroupName = "03. Trail")]
        public int Stage2TrailTicks { get; set; }

        [NinjaScriptProperty][Range(1, 99)]
        [Display(Name = "Stage 3 Trigger (%)", Order = 6, GroupName = "03. Trail")]
        public int Stage3TriggerPct { get; set; }

        [NinjaScriptProperty][Range(1, int.MaxValue)]
        [Display(Name = "Stage 3 Trail Ticks", Order = 7, GroupName = "03. Trail")]
        public int Stage3TrailTicks { get; set; }

        [NinjaScriptProperty][Range(0, 20)]
        [Display(Name = "HL Stage 1 Lookback Bars", Order = 8, GroupName = "03. Trail")]
        public int HLStage1LookbackBars { get; set; }

        [NinjaScriptProperty][Range(0, 20)]
        [Display(Name = "HL Stage 2 Lookback Bars", Order = 9, GroupName = "03. Trail")]
        public int HLStage2LookbackBars { get; set; }

        [NinjaScriptProperty][Range(1, 500)]
        [Display(Name = "HL Stage 2 Trigger Bars", Order = 11, GroupName = "03. Trail",
            Description = "Bars since entry before Stage 2 activates (was ticks-of-profit " +
                          "-- Jul 2026 bar-count trail rebuild). Default 3.")]
        public int HLStage2TriggerBars { get; set; }

        [NinjaScriptProperty][Range(1, 500)]
        [Display(Name = "HL Stage 3 Trigger Bars", Order = 12, GroupName = "03. Trail",
            Description = "Bars since entry before Stage 3 activates (was ticks-of-profit " +
                          "-- Jul 2026 bar-count trail rebuild). Default 11.")]
        public int HLStage3TriggerBars { get; set; }

        [NinjaScriptProperty]
        [Range(0, 500)]
        [Display(Name = "HL Stage 3 Trail Ticks", Order = 13, GroupName = "03. Trail",
            Description = "Stage 3: trail this many ticks below the rolling peak high (long) " +
                          "or above the rolling peak low (short). Default 40.")]
        public int HLStage3TrailTicks { get; set; }

        [NinjaScriptProperty][Range(0.1, 5.0)]
        [Display(Name = "ATR Trail Stage 1 Mult", Order = 14, GroupName = "03. Trail")]
        public double ATRTrailStage1Mult { get; set; }

        [NinjaScriptProperty][Range(0.1, 5.0)]
        [Display(Name = "ATR Trail Stage 2 Mult", Order = 15, GroupName = "03. Trail")]
        public double ATRTrailStage2Mult { get; set; }

        [NinjaScriptProperty][Range(0.1, 5.0)]
        [Display(Name = "ATR Trail Stage 3 Mult", Order = 16, GroupName = "03. Trail")]
        public double ATRTrailStage3Mult { get; set; }

        [NinjaScriptProperty][Range(0.1, 5.0)]
        [Display(Name = "ATR Trail Stage 4 Mult", Order = 17, GroupName = "03. Trail")]
        public double ATRTrailStage4Mult { get; set; }

        [NinjaScriptProperty][Range(1, 99)]
        [Display(Name = "ATR Trail Stage 4 Trigger (%)", Order = 18, GroupName = "03. Trail")]
        public int ATRTrailStage4TriggerPct { get; set; }

        // ── 03b. FixedStop+Trail ──────────────────────────────────────────────
        [NinjaScriptProperty][Range(1, 99)]
        [Display(Name = "FST Stage 1 Trigger (%)", Order = 1, GroupName = "03b. FixedStop+Trail")]
        public int FST_Stage1TriggerPct { get; set; }

        [NinjaScriptProperty][Range(0, int.MaxValue)]
        [Display(Name = "FST Stage 1 BE Offset Ticks", Order = 2, GroupName = "03b. FixedStop+Trail")]
        public int FST_Stage1OffsetTicks { get; set; }

        [NinjaScriptProperty][Range(1, 99)]
        [Display(Name = "FST Stage 2 Trigger (%)", Order = 3, GroupName = "03b. FixedStop+Trail")]
        public int FST_Stage2TriggerPct { get; set; }

        [NinjaScriptProperty][Range(1, 99)]
        [Display(Name = "FST Stage 2 Lock (% of target)", Order = 4, GroupName = "03b. FixedStop+Trail")]
        public int FST_Stage2LockPct { get; set; }

        [NinjaScriptProperty][Range(1, 99)]
        [Display(Name = "FST Stage 3 Trigger (%)", Order = 5, GroupName = "03b. FixedStop+Trail")]
        public int FST_Stage3TriggerPct { get; set; }

        [NinjaScriptProperty][Range(1, 99)]
        [Display(Name = "FST Stage 3 Lock (% of target)", Order = 6, GroupName = "03b. FixedStop+Trail")]
        public int FST_Stage3LockPct { get; set; }

        [NinjaScriptProperty][Range(1, 99)]
        [Display(Name = "FST Stage 4 Trigger (%)", Order = 7, GroupName = "03b. FixedStop+Trail")]
        public int FST_Stage4TriggerPct { get; set; }

        [NinjaScriptProperty][Range(1, int.MaxValue)]
        [Display(Name = "FST Stage 4 Trail Ticks", Order = 8, GroupName = "03b. FixedStop+Trail")]
        public int FST_Stage4TrailTicks { get; set; }

        // ── 04. Auto Exit ─────────────────────────────────────────────────────
        [NinjaScriptProperty]
        [Display(Name = "Auto Exit Mode", Order = 1, GroupName = "04. Auto Exit",
            Description = "DiffCross — exit when PriceMomentum diff turns against the trade.  " +
                          "Disabled — no auto-exit.")]
        public HiLoRiderAutoExit AutoExitMode { get; set; }

        // ── 05. Stop & Target Modes ───────────────────────────────────────────
        [NinjaScriptProperty]
        [Display(Name = "Stop Mode", Order = 1, GroupName = "05. Stop & Target Modes")]
        public RPStopMode StopMode { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Target Mode", Order = 2, GroupName = "05. Stop & Target Modes")]
        public RPTargetMode TargetMode { get; set; }

        [NinjaScriptProperty][Range(2, 100)]
        [Display(Name = "ATR Period", Order = 3, GroupName = "05. Stop & Target Modes")]
        public int ATRPeriod { get; set; }

        [NinjaScriptProperty][Range(0.25, 5.0)]
        [Display(Name = "SL ATR Multiplier", Order = 4, GroupName = "05. Stop & Target Modes")]
        public double SLATRMultiplier { get; set; }

        [NinjaScriptProperty][Range(0.25, 10.0)]
        [Display(Name = "TP ATR Multiplier", Order = 5, GroupName = "05. Stop & Target Modes")]
        public double TPATRMultiplier { get; set; }

        [NinjaScriptProperty][Range(0.5, 10.0)]
        [Display(Name = "Risk:Reward Ratio", Order = 6, GroupName = "05. Stop & Target Modes")]
        public double RiskRewardRatio { get; set; }

        [NinjaScriptProperty][Range(10, 500)]
        [Display(Name = "ATR SL Min Ticks", Order = 7, GroupName = "05. Stop & Target Modes")]
        public int ATRSLMinTicks { get; set; }

        // ── 06. PriceMomentum (DiffCross / ML) ────────────────────────────────
        [NinjaScriptProperty]
        [Display(Name = "Show PriceMomentum on Chart", Order = 1, GroupName = "06. PriceMomentum")]
        public bool ShowPM { get; set; }

        [NinjaScriptProperty][Range(2, 200)]
        [Display(Name = "Diff Lookback", Order = 2, GroupName = "06. PriceMomentum",
            Description = "PriceMomentum lookback period. Default 20.")]
        public int DiffLookback { get; set; }

        [NinjaScriptProperty][Range(1, 99)]
        [Display(Name = "Diff Min %", Order = 3, GroupName = "06. PriceMomentum",
            Description = "PriceMomentum lower band percentile. Default 30.")]
        public int DiffMinPercent { get; set; }

        [NinjaScriptProperty][Range(1, 100)]
        [Display(Name = "Diff Max %", Order = 4, GroupName = "06. PriceMomentum",
            Description = "PriceMomentum upper band percentile. Default 45.")]
        public int DiffMaxPercent { get; set; }

        // ── 06c. HiLoBands ───────────────────────────────────────────────────
        [NinjaScriptProperty]
        [Display(Name = "Show HiLoBands on Chart", Order = 1, GroupName = "06c. HiLoBands",
            Description = "Adds the real HiLoBands indicator instance (hiloInd) to the chart -- the "
                        + "actual channel/midline the entry signal, stop, and trail all react to. "
                        + "Default true.")]
        public bool ShowHiLoBands { get; set; }

        // ── 06b. Daily Range ──────────────────────────────────────────────────
        [NinjaScriptProperty]
        [Display(Name = "Show Daily Range on Chart", Order = 1, GroupName = "06b. Daily Range",
            Description = "Overlay the DailyRange indicator (session open, HOD/LOD, median-range targets, ATR levels).")]
        public bool ShowDR { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Show SuperTrendRegime on Chart", Order = 1, GroupName = "06d. SuperTrendRegime",
            Description = "Informational trend line + regime badge display. Not wired as an entry filter here.")]
        public bool ShowSuperTrend { get; set; }

        [NinjaScriptProperty]
        [Range(1, 60)]
        [Display(Name = "Range Lookback Days", Order = 2, GroupName = "06b. Daily Range",
            Description = "Number of prior RTH sessions used to compute the median (or minimum) daily range.")]
        public int DR_RangeLookbackDays { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Use Median Range", Order = 3, GroupName = "06b. Daily Range",
            Description = "True = median of prior sessions; False = minimum. Median is more robust to outlier days.")]
        public bool DR_UseMedian { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, 20.0)]
        [Display(Name = "Rev ATR Multiplier", Order = 4, GroupName = "06b. Daily Range",
            Description = "Developing range must exceed this × ATR before a reversal label fires.")]
        public double DR_RevAtrMultiplier { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, 5.0)]
        [Display(Name = "Trend Displacement Filter (ATR)", Order = 5, GroupName = "06b. Daily Range",
            Description = "Suppress the first trend label when price is already this many ATRs from the session open. 0 = disabled.")]
        public double DR_TrendDisplacementAtrFilter { get; set; }

        // ── 07. Trade Logging ─────────────────────────────────────────────────
        [NinjaScriptProperty]
        [Display(Name = "Enable Trade Logging", Order = 1, GroupName = "08. Trade Logging")]
        public bool EnableTradeLogging { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Log Folder Path", Order = 2, GroupName = "08. Trade Logging")]
        public string TradeLogPath { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Dashboard Script Path", Order = 3, GroupName = "08. Trade Logging")]
        public string DashboardScriptPath { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Python Executable", Order = 4, GroupName = "08. Trade Logging")]
        public string PythonExePath { get; set; }

        // ── Entry Window ──────────────────────────────────────────────────────
        [NinjaScriptProperty][Range(0, 2400)]
        [Display(Name = "Entry Window Start (HHMM)", Order = 1, GroupName = "10. Entry Window")]
        public int EntryWindowStart { get; set; }

        [NinjaScriptProperty][Range(0, 23)]
        [Display(Name = "RTH Start Hour", Order = 2, GroupName = "10. Entry Window")]
        public int RTHStartHour { get; set; }

        [NinjaScriptProperty][Range(0, 59)]
        [Display(Name = "RTH Start Minute", Order = 3, GroupName = "10. Entry Window")]
        public int RTHStartMinute { get; set; }

        [NinjaScriptProperty][Range(0, 2400)]
        [Display(Name = "RTH Window End (HHMM)", Order = 4, GroupName = "10. Entry Window")]
        public int RTHWindowEnd { get; set; }

        [NinjaScriptProperty][Range(0, 120)]
        [Display(Name = "RTH Open Block Mins", Order = 5, GroupName = "10. Entry Window")]
        public int RTHOpenBlockMins { get; set; }

        #endregion
    }
}