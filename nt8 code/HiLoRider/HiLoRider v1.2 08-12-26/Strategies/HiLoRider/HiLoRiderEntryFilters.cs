// file name = HiLoRiderEntryFilters.cs
// HiLoRider — entry filters: midday block, gap, news block, ER.

#region Using declarations
using System;
using System.ComponentModel.DataAnnotations;
using NinjaTrader.NinjaScript.Strategies;
#endregion

namespace NinjaTrader.NinjaScript.Strategies.HiLoRider
{
    public partial class HiLoRider : Strategy
    {
        // ── Gap filter state ───────────────────────────────────────────────────
        private double _sessionGapPct     = 0;
        private int    _gapDirection      = 0;    // +1 gap up, -1 gap down, 0 none
        private double _priorSessionClose = 0;
        private bool   _gapCaptured       = false;

        internal void ResetEntryFilterState()
        {
            _sessionGapPct     = 0;
            _gapDirection      = 0;
            _priorSessionClose = 0;
            _gapCaptured       = false;
        }

        internal void CaptureGapAtSessionOpen()
        {
            if (CurrentBar < 2) return;
            try
            {
                // Prior close is Close[1] at IsFirstBarOfSession
                _priorSessionClose = Close[1];
                _gapCaptured       = false;
            }
            catch { }
        }

        internal void UpdateGapIfNeeded()
        {
            if (_gapCaptured || !EnableGapFilter) return;
            if (_priorSessionClose <= 0 || CurrentBar < 1) return;
            if (!Bars.IsFirstBarOfSession) return;

            double open = Open[0];
            if (open > 0 && _priorSessionClose > 0)
            {
                _sessionGapPct = (open - _priorSessionClose) / _priorSessionClose * 100.0;
                _gapDirection  = _sessionGapPct > 0 ? 1 : (_sessionGapPct < 0 ? -1 : 0);
                _gapCaptured   = true;
            }
        }

        internal bool CheckGapFilter(int sig)
        {
            if (!EnableGapFilter || GapBlockMinutes <= 0 || _gapDirection == 0) return true;
            if (Math.Abs(_sessionGapPct) < GapFilterPct) return true;
            if (sig == 0) return true;   // direction-agnostic call → pass through

            // Block counter-gap entries in the gap block window
            DateTime etNow   = TimeZoneInfo.ConvertTimeFromUtc(Time[0].ToUniversalTime(), EasternTZ);
            DateTime rthOpen = new DateTime(etNow.Year, etNow.Month, etNow.Day, RTHStartHour, RTHStartMinute, 0);
            if (etNow > rthOpen.AddMinutes(GapBlockMinutes)) return true;

            bool counterGap = (sig == -1 && _gapDirection == 1) || (sig == 1 && _gapDirection == -1);
            return !counterGap;
        }

        internal bool CheckMidDayBlock()
        {
            if (!EnableMidDayBlock) return true;
            int timeET = ToTimeET(Time[0]);
            int s = MidDayBlockStart * 100;
            int e = MidDayBlockEnd   * 100;
            if (s > e) return !(timeET >= s || timeET <= e);
            return !(timeET >= s && timeET <= e);
        }

        internal bool CheckNewsBlock()
        {
            if (IsInCalendarNewsBlock()) return false;
            if (!EnableNewsBlock || NewsBlockMins <= 0) return true;
            int timeET = ToTimeET(Time[0]);
            foreach (int nt in new[] { NewsTime1, NewsTime2, NewsTime3, NewsTime4 })
            {
                if (nt <= 0) continue;
                int scaled = nt * 100;
                int low    = scaled - NewsBlockMins * 100;
                int high   = scaled + NewsBlockMins * 100;
                if (timeET >= low && timeET <= high) return false;
            }
            return true;
        }

        // ── Properties ────────────────────────────────────────────────────────
        [NinjaScriptProperty]
        [Display(Name = "Enable MidDay Block", Order = 1, GroupName = "13. Entry Filters")]
        public bool EnableMidDayBlock { get; set; }

        [NinjaScriptProperty][Range(0, 2400)]
        [Display(Name = "MidDay Block Start (HHMM)", Order = 2, GroupName = "13. Entry Filters")]
        public int MidDayBlockStart { get; set; }

        [NinjaScriptProperty][Range(0, 2400)]
        [Display(Name = "MidDay Block End (HHMM)", Order = 3, GroupName = "13. Entry Filters")]
        public int MidDayBlockEnd { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Enable Gap Filter", Order = 4, GroupName = "13. Entry Filters")]
        public bool EnableGapFilter { get; set; }

        [NinjaScriptProperty][Range(0.01, 5.0)]
        [Display(Name = "Gap Filter % (min gap size)", Order = 5, GroupName = "13. Entry Filters")]
        public double GapFilterPct { get; set; }

        [NinjaScriptProperty][Range(0, 120)]
        [Display(Name = "Gap Block Minutes", Order = 6, GroupName = "13. Entry Filters")]
        public int GapBlockMinutes { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Enable News Block", Order = 7, GroupName = "13. Entry Filters")]
        public bool EnableNewsBlock { get; set; }

        [NinjaScriptProperty][Range(0, 30)]
        [Display(Name = "News Block ± Mins", Order = 8, GroupName = "13. Entry Filters")]
        public int NewsBlockMins { get; set; }

        [NinjaScriptProperty][Range(0, 2400)]
        [Display(Name = "News Time 1 (HHMM, 0=off)", Order = 9, GroupName = "13. Entry Filters")]
        public int NewsTime1 { get; set; }

        [NinjaScriptProperty][Range(0, 2400)]
        [Display(Name = "News Time 2 (HHMM, 0=off)", Order = 10, GroupName = "13. Entry Filters")]
        public int NewsTime2 { get; set; }

        [NinjaScriptProperty][Range(0, 2400)]
        [Display(Name = "News Time 3 (HHMM, 0=off)", Order = 11, GroupName = "13. Entry Filters")]
        public int NewsTime3 { get; set; }

        [NinjaScriptProperty][Range(0, 2400)]
        [Display(Name = "News Time 4 (HHMM, 0=off)", Order = 12, GroupName = "13. Entry Filters")]
        public int NewsTime4 { get; set; }

        // ── Diff band filter (magnitude-based) ─────────────────────────────────
        // Blocks entry when |diff| < floor (chop) or |diff| >= ceiling (overextended).
        // Direction of diff is ignored — the HiLoBands midline crossover determines
        // long/short. Floor = DiffMinPercent% of rolling dynamic max; ceiling =
        // DiffMaxPercent%. Off by default -- see EnableDiffBandFilter's own comment.
        internal bool CheckDiffBandFilter()
        {
            if (!EnableDiffBandFilter) return true;
            if (cachedAdaptiveFloor <= 0) return true;   // indicator not warmed up
            double absDiff = Math.Abs(cachedDiff);
            if (absDiff < cachedAdaptiveFloor)   return false;  // below floor
            if (absDiff >= cachedAdaptiveExhaust) return false;  // above ceiling
            return true;
        }

        [NinjaScriptProperty]
        [Display(Name = "Enable Diff Band Filter", Order = 13, GroupName = "13. Entry Filters",
            Description = "Block entry when |diff| is outside [DiffMin%, DiffMax%] of rolling max. "
                        + "Magnitude-only — diff sign is irrelevant for reversal entries. "
                        + "Optimized Jun 2026: DiffMin=10%, DiffMax=50%, ADX 30/65.")]
        public bool EnableDiffBandFilter { get; set; }

        // ── IsChoppy / chop-hold (Aug 2026, ported from Reaper) ────────────────
        // IsChoppy() is the same "below the floor" half of CheckDiffBandFilter's
        // own check, exposed on its own: |diff| < DiffMinPercent's derived floor.
        // Used by the chop-hold mechanism in HiLoRider.cs's main entry flow, and
        // by GetChopSubSuffix() below for the regime badge. Does NOT check the
        // ceiling/exhaustion side -- that stays CheckDiffBandFilter's own
        // instant-reject-only responsibility.
        internal bool IsChoppy()
        {
            if (cachedAdaptiveFloor <= 0) return false;   // indicator not warmed up
            return Math.Abs(cachedDiff) < cachedAdaptiveFloor;
        }

        // Used only to resolve a held chop-pending signal: requires diff to have
        // cleared the floor IN THE CACHED SIGNAL'S OWN DIRECTION -- a stricter,
        // SIGNED check than CheckDiffBandFilter's own magnitude-only design.
        internal bool DiffConfirmsChopHold(int dir)
        {
            if (cachedAdaptiveFloor <= 0) return false;
            if (Math.Abs(cachedDiff) < cachedAdaptiveFloor) return false;   // still choppy
            return dir == 1 ? cachedDiff > 0 : cachedDiff < 0;
        }

        // Regime-badge sub-line suffix, same priority-suffix pattern as
        // GetEconSubSuffix() (HiLoRiderEconCalendar.cs) -- appended alongside it
        // in HiLoRiderUI.cs, not a replacement for any existing badge state.
        internal string GetChopSubSuffix()
        {
            if (EnableDiffBandFilter && IsChoppy()) return "  ·  CHOPPY";
            return "";
        }

        [NinjaScriptProperty][Range(0, 10)]
        [Display(Name = "Bars To Hold (chop wait)", Order = 14, GroupName = "13. Entry Filters",
            Description = "When a signal fires but |diff| is below the floor (choppy), wait up to this "
                        + "many bars for diff to clear the floor IN THE SIGNAL'S OWN DIRECTION before "
                        + "firing, instead of rejecting immediately. 0 = reject immediately (matches "
                        + "pre-Aug-2026 behavior exactly). Default 3 (Aug 2026, fleet-wide rollout) -- "
                        + "a real live-behavior change, applied per explicit user instruction, not "
                        + "independently backtested.")]
        public int BarsToHold { get; set; }

        // ── ADX minimum ───────────────────────────────────────────────────────
        internal bool CheckADXFilter()
        {
            if (!EnableADXFilter) return true;
            if (adx1 == null || !adx1.IsValidDataPoint(0)) return true;
            double adx = adx1[0];
            if (adx < ADXMinimum) return false;
            if (adx > ADXMaximum) return false;
            return true;
        }

        [NinjaScriptProperty]
        [Display(Name = "Enable ADX Filter", Order = 16, GroupName = "13. Entry Filters")]
        public bool EnableADXFilter { get; set; }

        [NinjaScriptProperty][Range(0.0, 100.0)]
        [Display(Name = "ADX Minimum", Order = 17, GroupName = "13. Entry Filters",
            Description = "Block entry when ADX < this value. Default 0 (no floor — ceiling-only gate).")]
        public double ADXMinimum { get; set; }

        [NinjaScriptProperty][Range(0.0, 100.0)]
        [Display(Name = "ADX Maximum", Order = 18, GroupName = "13. Entry Filters",
            Description = "Block entry when ADX > this value. Untested for this signal -- " +
                          "EnableADXFilter=false by default, both bounds inert (0/100). Toggleable if a future pass validates a gate.")]
        public double ADXMaximum { get; set; }

        // ── HiLoBands tunables (passed to the hiloInd indicator instance in
        //    State.DataLoaded) ──────────────────────────────────────────────
        [NinjaScriptProperty][Range(1, int.MaxValue)]
        [Display(Name = "HiLoBands Lookback Period", Order = 19, GroupName = "13. Entry Filters",
            Description = "Donchian-style channel period used by the crossover/slope entry and " +
                          "opposite-band stop. Current Wave-120 instrument defaults: MNQ=25 and " +
                          "MGC=10. Disable Use Instrument Profile Defaults for controlled tests.")]
        public int LookbackPeriod { get; set; }

        [NinjaScriptProperty][Range(1, int.MaxValue)]
        [Display(Name = "HiLoBands Line Width", Order = 20, GroupName = "13. Entry Filters",
            Description = "Cosmetic chart line thickness for the HiLoBands indicator instance. Default 2.")]
        public int HiLoWidth { get; set; }

        [NinjaScriptProperty][Range(0, int.MaxValue)]
        [Display(Name = "Trail Bars Before Trail", Order = 21, GroupName = "13. Entry Filters",
            Description = "TrailMode=MidlineOffset only: bars since entry before the stop starts " +
                          "tracking the channel midline +/- TrailOffsetTicks (tighten-only). Inert " +
                          "under the deployed HighLow trail mode.")]
        public int TrailBarsBeforeTrail { get; set; }

        [NinjaScriptProperty][Range(0, int.MaxValue)]
        [Display(Name = "Trail Offset Ticks", Order = 22, GroupName = "13. Entry Filters",
            Description = "TrailMode=MidlineOffset only: stop = midline -/+ this many ticks once " +
                          "TrailBarsBeforeTrail has elapsed. Inert under the deployed HighLow trail " +
                          "mode; values below zero remain prohibited.")]
        public int TrailOffsetTicks { get; set; }

        // ── Globex bad-hour block ─────────────────────────────────────────────
        internal bool CheckHourBlock()
        {
            if (!EnableHourBlock) return true;
            int etH = TimeZoneInfo.ConvertTimeFromUtc(Time[0].ToUniversalTime(), EasternTZ).Hour;
            if (HourBlockEnd >= HourBlockStart)
                return etH < HourBlockStart || etH > HourBlockEnd;
            // overnight wrap (e.g. 22:xx – 05:xx)
            return etH > HourBlockEnd && etH < HourBlockStart;
        }

        [NinjaScriptProperty]
        [Display(Name = "Enable Hour Block", Order = 21, GroupName = "13. Entry Filters")]
        public bool EnableHourBlock { get; set; }

        [NinjaScriptProperty][Range(0, 23)]
        [Display(Name = "Block Start Hour (ET, 0-23)", Order = 22, GroupName = "13. Entry Filters",
            Description = "Block entries at or after this ET hour. Default 1 (1am).")]
        public int HourBlockStart { get; set; }

        [NinjaScriptProperty][Range(0, 23)]
        [Display(Name = "Block End Hour (ET, 0-23)", Order = 23, GroupName = "13. Entry Filters",
            Description = "Block entries through and including this ET hour. Default 5 (5am).")]
        public int HourBlockEnd { get; set; }

        // ── Close confirmation (Jul 2026) ──────────────────
        // Extra post-signal gate: require the current bar to have actually
        // moved in the signal's direction before entering -- Close[0] > Close[1]
        // for longs, Close[0] < Close[1] for shorts.
        internal bool CheckCloseConfirmation(int sig)
        {
            if (!EnableCloseConfirmation) return true;
            if (CurrentBar < 1) return true;
            if (sig ==  1 && Close[0] <= Close[1]) return false;
            if (sig == -1 && Close[0] >= Close[1]) return false;
            return true;
        }

        [NinjaScriptProperty]
        [Display(Name = "Enable Close Confirmation", Order = 24, GroupName = "13. Entry Filters",
            Description = "Require Close[0] > Close[1] for longs, Close[0] < Close[1] for shorts, " +
                          "on top of the entry signal.")]
        public bool EnableCloseConfirmation { get; set; }

    }
}
