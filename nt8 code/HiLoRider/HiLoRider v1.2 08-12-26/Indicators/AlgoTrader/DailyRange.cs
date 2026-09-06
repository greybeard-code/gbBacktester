// DailyRange.cs
// Indicator for the "Daily Range" concept.
//
// CONCEPT:
//   Dual-anchor session logic (mirrors SessionTrendLine):
//     • Globex open (18:00 ET) is the active anchor from 18:00 until 9:29 ET.
//     • NY open (9:30 ET) becomes the active anchor from 9:30 to 16:00 ET.
//       At 9:30 the session high/low resets so targets reflect only the RTH move.
//   Calculate the rolling N-day median (or minimum) of RTH-only daily high-low
//   ranges (9:30–16:00 ET) so targets always reflect typical RTH range.
//   Draw target lines above and below the active session open at that distance.
//
// VISUAL OUTPUT (all on the price panel as horizontal rays):
//   Plot 0  — Active session open  (light blue dashed, width 2)
//             Globex open 18:00–9:29 ET; NY open 9:30–16:00 ET
//   Plot 1  — Upper target   = LOD + MedianRange  (cyan solid)
//   Plot 2  — Lower target   = HOD − MedianRange  (cyan solid)
//   Plot 3  — High of Day    = sessionHigh  (yellow solid, width 2)
//   Plot 4  — Low of Day     = sessionLow   (yellow solid, width 2)
//   Plots 5–12  — ATR rainbow levels above session open
//   Plots 13–20 — ATR rainbow levels below session open
//   Plot 21 — Previous session's Close  (silver dashed, width 2)
//   Plot 22 — Late-day zone High  (goldenrod solid, width 1)
//   Plot 23 — Late-day zone Low   (goldenrod solid, width 1)
//
// LATE-DAY ZONE (Aug 2026 — "Fabulous Four" reference, Oliver Velez-style):
//   The high/low of the previous session's trailing LateDayLookbackMinutes
//   (default 60, tunable 15–180) of activity, captured the instant the new
//   session begins and held constant for that whole session — same
//   "extend into the future" behavior as SessionOpen/HOD/LOD above. Drawn
//   as two HLine plots (22/23) PLUS a light vertical-gradient shaded fill
//   between them (OnRender, SharpDX LinearGradientBrush) so the zone reads
//   as a genuine support/resistance block, not just two lines. Tracked via
//   a time-based rolling window (not session-gated), so it always reflects
//   "the last N minutes of whatever just traded" regardless of which
//   session anchor (Globex/NY) was active at the time.
//
// STATUS LABEL (bottom-left):
//   Shows MedianRange, MinRange, and developing range.

#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Linq;
using System.Windows.Media;
using System.Xml.Serialization;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.DrawingTools;
using NinjaTrader.Gui.Tools;
using SharpDX;
// NOTE: do NOT import SharpDX.Direct2D1 globally -- it contains types
// (SolidColorBrush, PathGeometry, etc.) that collide with System.Windows.Media.
// Fully-qualify SharpDX.Direct2D1.* at use sites instead (see TrendArchitect.cs).
#endregion

namespace NinjaTrader.NinjaScript.Indicators.AlgoTrader
{
    public class DailyRange : Indicator
    {
        // ── Indicator instances ──────────────────────────────────────────────
        private ATR _atr;

        // ── Session-level state ──────────────────────────────────────────────
        // sessionOpen  : active anchor — Globex open 18:00–9:29 ET, NY open 9:30–16:00 ET
        // globexOpen   : captured at 18:00 ET; becomes sessionOpen until NY open
        // nyOpen       : captured at 9:30 ET; becomes sessionOpen for RTH window
        private double sessionOpen = 0;
        private double globexOpen  = 0;
        private double nyOpen      = 0;
        private double sessionHigh = double.MinValue;
        private double sessionLow  = double.MaxValue;
        private bool   _nyOpenSeen = false;

        // ── Legacy bot-facing state (kept for strategy compatibility) ─────────
        private bool tradeTakenToday = false;
        private bool windowClosed    = false;
        private int  signalDirection = 0;

        // ── Rolling range history ────────────────────────────────────────────
        private readonly List<double> dailyRangeTicks = new List<double>();

        // ── Previous-session close + late-day zone state (Aug 2026) ──────────
        private double prevSessionClose  = 0;
        private double prevLateHigh      = 0;
        private double prevLateLow       = 0;
        private int    sessionStartBarIndex = 0;

        // Trailing-minutes window, time-based (NOT session-gated) -- rolls
        // naturally across the session boundary so it always reflects "the
        // last N minutes of whatever just traded," matching the video spec
        // ("go back about 45 minutes to an hour... not much more than that").
        private class LateBar { public DateTime Time; public double High; public double Low; }
        private readonly List<LateBar> lateWindowBars = new List<LateBar>();

        // ── ET timezone ──────────────────────────────────────────────────────
        private static readonly TimeZoneInfo EasternTZ =
            TimeZoneInfo.FindSystemTimeZoneById("Eastern Standard Time");

        private int ToTimeET(DateTime barTime)
        {
            DateTime et = TimeZoneInfo.ConvertTimeFromUtc(barTime.ToUniversalTime(), EasternTZ);
            return et.Hour * 10000 + et.Minute * 100 + et.Second;
        }

        // ── Computed range levels ────────────────────────────────────────────
        private double medianRangeTicks = 0;
        private double minRangeTicks    = 0;

        // ── Plot index constants ──────────────────────────────────────────────
        private const int PLOT_OPEN      = 0;
        private const int PLOT_UP_TARGET = 1;
        private const int PLOT_DN_TARGET = 2;
        private const int PLOT_HOD       = 3;
        private const int PLOT_LOD       = 4;
        private const int PLOT_ATR_ABOVE = 5;
        private const int PLOT_ATR_BELOW = 13;
        // Appended after the existing 21 plots (0–20) -- keeps the ATR rainbow
        // constants above untouched and avoids reordering the generated
        // constructor signature's own parameter list.
        private const int PLOT_PREV_CLOSE = 21;
        private const int PLOT_LATE_HIGH  = 22;
        private const int PLOT_LATE_LOW   = 23;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description       = "Daily Range: draws median/min daily-range target levels, ATR rainbow lines, " +
                                     "the previous session's close, and a gradient-shaded late-day zone.";
                Name              = "Daily Range";
                Calculate         = Calculate.OnBarClose;
                IsOverlay         = true;
                DrawOnPricePanel  = true;
                DisplayInDataBox  = false;
                PaintPriceMarkers = false;

                // ── Parameters ──────────────────────────────────────────────
                RangeLookbackDays    = 14;
                UseMedian            = true;
                MinDisplacementTicks = 5;
                AtrPeriod            = 100;
                AtrMultiplier        = 1.0;
                ShowAtrLines         = true;

                // ── Prev close + late-day zone (Aug 2026) ──────────────────────
                ShowPrevClose          = true;
                ShowLateDayZone        = true;
                LateDayLookbackMinutes = 60;
                // Aug 2026: default bumped 14->40. At 14% the fill was nearly
                // invisible against a black chart background and got lost
                // among this file's own other lines/labels -- see the
                // "more visible" follow-up section below for the full fix.
                LateDayZoneOpacity     = 40;

                // ── Plots ────────────────────────────────────────────────────
                AddPlot(new Stroke(Brushes.LightSkyBlue, DashStyleHelper.Dash, 2),  PlotStyle.HLine, "SessionOpen");
                AddPlot(new Stroke(Brushes.Cyan,         DashStyleHelper.Solid, 2), PlotStyle.HLine, "UpperTarget");
                AddPlot(new Stroke(Brushes.Cyan,         DashStyleHelper.Solid, 2), PlotStyle.HLine, "LowerTarget");
                AddPlot(new Stroke(Brushes.Yellow,       DashStyleHelper.Solid, 2), PlotStyle.HLine, "HighOfDay");
                AddPlot(new Stroke(Brushes.Yellow,       DashStyleHelper.Solid, 2), PlotStyle.HLine, "LowOfDay");
                // ATR rainbow above (plots 5–12)
                AddPlot(new Stroke(Brushes.Yellow,      DashStyleHelper.Dash, 1), PlotStyle.HLine, "AtrAbove1");
                AddPlot(new Stroke(Brushes.LimeGreen,   DashStyleHelper.Dash, 1), PlotStyle.HLine, "AtrAbove2");
                AddPlot(new Stroke(Brushes.DeepSkyBlue, DashStyleHelper.Dash, 1), PlotStyle.HLine, "AtrAbove3");
                AddPlot(new Stroke(Brushes.LightBlue,   DashStyleHelper.Dash, 1), PlotStyle.HLine, "AtrAbove4");
                AddPlot(new Stroke(Brushes.Violet,      DashStyleHelper.Dash, 1), PlotStyle.HLine, "AtrAbove5");
                AddPlot(new Stroke(Brushes.Magenta,     DashStyleHelper.Dash, 1), PlotStyle.HLine, "AtrAbove6");
                AddPlot(new Stroke(Brushes.Orange,      DashStyleHelper.Dash, 1), PlotStyle.HLine, "AtrAbove7");
                AddPlot(new Stroke(Brushes.Red,         DashStyleHelper.Dash, 1), PlotStyle.HLine, "AtrAbove8");
                // ATR rainbow below (plots 13–20)
                AddPlot(new Stroke(Brushes.Yellow,      DashStyleHelper.Dash, 1), PlotStyle.HLine, "AtrBelow1");
                AddPlot(new Stroke(Brushes.LimeGreen,   DashStyleHelper.Dash, 1), PlotStyle.HLine, "AtrBelow2");
                AddPlot(new Stroke(Brushes.DeepSkyBlue, DashStyleHelper.Dash, 1), PlotStyle.HLine, "AtrBelow3");
                AddPlot(new Stroke(Brushes.LightBlue,   DashStyleHelper.Dash, 1), PlotStyle.HLine, "AtrBelow4");
                AddPlot(new Stroke(Brushes.Violet,      DashStyleHelper.Dash, 1), PlotStyle.HLine, "AtrBelow5");
                AddPlot(new Stroke(Brushes.Magenta,     DashStyleHelper.Dash, 1), PlotStyle.HLine, "AtrBelow6");
                AddPlot(new Stroke(Brushes.Orange,      DashStyleHelper.Dash, 1), PlotStyle.HLine, "AtrBelow7");
                AddPlot(new Stroke(Brushes.Red,         DashStyleHelper.Dash, 1), PlotStyle.HLine, "AtrBelow8");

                // Prev close + late-day zone boundary lines (Aug 2026).
                // LateDayHigh/Low switched Goldenrod(width1) -> White(width2):
                // Goldenrod at width 1 was nearly indistinguishable from the
                // solid Yellow HOD/LOD lines and the ATR rainbow's own
                // Orange/Magenta lines already crowding this same price
                // region -- White has zero collision with this file's
                // existing palette and reads as a crisp, bold boundary.
                AddPlot(new Stroke(Brushes.Silver, DashStyleHelper.Dash,  2), PlotStyle.HLine, "PrevClose");
                AddPlot(new Stroke(Brushes.White,  DashStyleHelper.Solid, 2), PlotStyle.HLine, "LateDayHigh");
                AddPlot(new Stroke(Brushes.White,  DashStyleHelper.Solid, 2), PlotStyle.HLine, "LateDayLow");
            }
            else if (State == State.DataLoaded)
            {
                _atr        = ATR(AtrPeriod);
                _nyOpenSeen = false;
            }
        }

        protected override void OnBarUpdate()
        {
            if (BarsInProgress != 0) return;

            // ── 1. Detect new session (Globex open at 18:00 ET) ──────────────
            if (Bars.IsFirstBarOfSession)
            {
                RecordCompletedSessionRange();

                // ── Capture yesterday's close + late-day zone from the just-
                //    completed session, BEFORE today's first bar is added to
                //    the rolling window below (so the window still only holds
                //    the tail of the completed session at this point). ───────
                if (CurrentBar >= 1)
                    prevSessionClose = Close[1];

                if (lateWindowBars.Count > 0)
                {
                    double hi = double.MinValue, lo = double.MaxValue;
                    for (int i = 0; i < lateWindowBars.Count; i++)
                    {
                        if (lateWindowBars[i].High > hi) hi = lateWindowBars[i].High;
                        if (lateWindowBars[i].Low  < lo) lo = lateWindowBars[i].Low;
                    }
                    prevLateHigh = hi;
                    prevLateLow  = lo;
                }

                sessionStartBarIndex = CurrentBar;

                sessionOpen     = 0;
                globexOpen      = 0;
                nyOpen          = 0;
                sessionHigh     = double.MinValue;
                sessionLow      = double.MaxValue;
                tradeTakenToday = false;
                windowClosed    = false;
                signalDirection = 0;
                _nyOpenSeen     = false;

                globexOpen  = Open[0];
                sessionOpen = Open[0];
                sessionHigh = High[0];
                sessionLow  = Low[0];
            }

            // ── Maintain the trailing-minutes window for the late-day zone ────
            // Time-based, unconditional every bar (not session-gated).
            lateWindowBars.Add(new LateBar { Time = Time[0], High = High[0], Low = Low[0] });
            DateTime lateCutoff = Time[0].AddMinutes(-LateDayLookbackMinutes);
            while (lateWindowBars.Count > 0 && lateWindowBars[0].Time < lateCutoff)
                lateWindowBars.RemoveAt(0);

            int timeET = ToTimeET(Time[0]);

            // ── 2. Switch to NY open anchor at 9:30 ET ────────────────────────
            if (!_nyOpenSeen && timeET >= 093000 && timeET < 160000)
            {
                _nyOpenSeen = true;
                nyOpen      = Open[0];

                sessionOpen = Open[0];
                sessionHigh = High[0];
                sessionLow  = Low[0];
            }

            // ── 3. Track session high/low within the active window ────────────
            if (sessionOpen > 0)
            {
                bool inWindow = _nyOpenSeen
                    ? (timeET >= 093000 && timeET < 160000)
                    : (timeET >= 180000 || timeET < 093000);

                if (inWindow)
                {
                    if (High[0] > sessionHigh) sessionHigh = High[0];
                    if (Low[0]  < sessionLow)  sessionLow  = Low[0];
                }
            }

            if (sessionOpen == 0) return;

            // ── 4. Compute rolling range statistics ───────────────────────────
            if (dailyRangeTicks.Count >= 1)
            {
                var window = dailyRangeTicks
                    .Skip(Math.Max(0, dailyRangeTicks.Count - RangeLookbackDays))
                    .ToList();

                window.Sort();
                minRangeTicks = window[0];

                int mid = window.Count / 2;
                medianRangeTicks = (window.Count % 2 == 0)
                    ? (window[mid - 1] + window[mid]) / 2.0
                    : window[mid];
            }

            double targetTicks = UseMedian ? medianRangeTicks : minRangeTicks;

            if (targetTicks <= 0)
            {
                Values[PLOT_OPEN][0]       = sessionOpen;
                Values[PLOT_PREV_CLOSE][0] = ShowPrevClose && prevSessionClose > 0 ? prevSessionClose : double.NaN;
                Values[PLOT_LATE_HIGH][0]  = ShowLateDayZone && prevLateHigh > 0 ? prevLateHigh : double.NaN;
                Values[PLOT_LATE_LOW][0]   = ShowLateDayZone && prevLateLow > 0 && prevLateLow < double.MaxValue ? prevLateLow : double.NaN;
                return;
            }

            double targetPrice = targetTicks * TickSize;

            // ── 5. Draw price panel plots ─────────────────────────────────────
            Values[PLOT_OPEN][0]      = sessionOpen;
            Values[PLOT_UP_TARGET][0] = sessionLow  + targetPrice;
            Values[PLOT_DN_TARGET][0] = sessionHigh - targetPrice;
            Values[PLOT_HOD][0]       = sessionHigh > double.MinValue ? sessionHigh : sessionOpen;
            Values[PLOT_LOD][0]       = sessionLow  < double.MaxValue ? sessionLow  : sessionOpen;

            // ── 5a. Prev close + late-day zone lines ──────────────────────────
            Values[PLOT_PREV_CLOSE][0] = ShowPrevClose && prevSessionClose > 0 ? prevSessionClose : double.NaN;
            Values[PLOT_LATE_HIGH][0]  = ShowLateDayZone && prevLateHigh > 0 ? prevLateHigh : double.NaN;
            Values[PLOT_LATE_LOW][0]   = ShowLateDayZone && prevLateLow > 0 && prevLateLow < double.MaxValue ? prevLateLow : double.NaN;

            // ── 5b. ATR rainbow lines ─────────────────────────────────────────
            if (_atr != null && _atr.IsValidDataPoint(0) && sessionOpen > 0)
            {
                double step = _atr[0] * AtrMultiplier;
                if (step > 0)
                {
                    int linesNeeded = 4;
                    if (medianRangeTicks > 0)
                    {
                        double medianPrice = medianRangeTicks * TickSize;
                        linesNeeded = Math.Max(4, (int)Math.Ceiling(medianPrice / step) + 1);
                    }
                    int lineCount = Math.Min(linesNeeded, 8);

                    for (int i = 0; i < 8; i++)
                    {
                        double offset = step * (i + 1);
                        if (ShowAtrLines && i < lineCount)
                        {
                            Values[PLOT_ATR_ABOVE + i][0] = sessionOpen + offset;
                            Values[PLOT_ATR_BELOW + i][0] = sessionOpen - offset;
                        }
                        else
                        {
                            Values[PLOT_ATR_ABOVE + i][0] = double.NaN;
                            Values[PLOT_ATR_BELOW + i][0] = double.NaN;
                        }
                    }
                }
            }

            // ── 6. Status label ───────────────────────────────────────────────
            double devRangeTicks = (sessionHigh > double.MinValue && sessionLow < double.MaxValue)
                ? (sessionHigh - sessionLow) / TickSize
                : 0;

            string sessionLabel = _nyOpenSeen ? "NY Open" : "GB Open";
            string statusText   = $"[{sessionLabel}]  Median: {medianRangeTicks:F0}t  |  Min: {minRangeTicks:F0}t  |  Range: {devRangeTicks:F0}t";

            Draw.TextFixed(this, "DROStatus", statusText, TextPosition.BottomLeft, Brushes.White,
                new SimpleFont("Arial", 11), Brushes.Transparent, Brushes.Transparent, 0);
        }

        // ─────────────────────────────────────────────────────────────────────
        // RecordCompletedSessionRange
        // ─────────────────────────────────────────────────────────────────────
        private void RecordCompletedSessionRange()
        {
            if (sessionHigh > double.MinValue
                && sessionLow  < double.MaxValue
                && sessionHigh >= sessionLow
                && nyOpen > 0)
            {
                double rangeTicks = (sessionHigh - sessionLow) / TickSize;
                if (rangeTicks > 0 && rangeTicks < 5000)
                    dailyRangeTicks.Add(rangeTicks);
            }
        }

        // ── Public methods for strategy compatibility ─────────────────────────
        public void MarkTradeTaken()
        {
            tradeTakenToday = true;
            signalDirection = 0;
        }

        public void ResetSession()
        {
            sessionOpen     = Open[0];
            globexOpen      = Open[0];
            nyOpen          = 0;
            sessionHigh     = High[0];
            sessionLow      = Low[0];
            tradeTakenToday = false;
            windowClosed    = false;
            signalDirection = 0;
            _nyOpenSeen     = false;
        }

        // ─────────────────────────────────────────────────────────────────────
        // OnRender — light vertical-gradient shaded fill for the late-day zone.
        // The boundary LINES (Plots 22/23) already render for free via the
        // ordinary HLine plot mechanism, same as SessionOpen/HOD/LOD above --
        // this only paints the translucent fill between them. Current session
        // only (matches this file's own "active session" design philosophy,
        // same as SessionOpen's own header-comment framing) -- not a replay
        // of every past session's own zone.
        // ─────────────────────────────────────────────────────────────────────
        protected override void OnRender(ChartControl chartControl, ChartScale chartScale)
        {
            base.OnRender(chartControl, chartScale);

            if (!ShowLateDayZone) return;
            if (prevLateHigh <= 0 || prevLateLow <= 0 || prevLateLow >= double.MaxValue) return;
            if (prevLateHigh <= prevLateLow) return;
            if (ChartBars == null || RenderTarget == null) return;

            int fromIdx = Math.Max(sessionStartBarIndex, ChartBars.FromIndex);
            if (fromIdx > ChartBars.ToIndex) return; // zone hasn't started within the visible range

            float xLeft  = chartControl.GetXByBarIndex(ChartBars, fromIdx);
            float xRight = (float)(ChartPanel.X + ChartPanel.W);
            if (xRight <= xLeft) return;

            float yTop = chartScale.GetYByValue(prevLateHigh);
            float yBot = chartScale.GetYByValue(prevLateLow);
            if (yBot <= yTop) return;

            var rect = new SharpDX.RectangleF(xLeft, yTop, xRight - xLeft, yBot - yTop);

            // Light vertical gradient: brighter near the zone's own edges (the
            // two levels that actually act as support/resistance), softer
            // through the middle -- reads as a genuine "glow zone," not a flat
            // translucent block. Warm gold, driven by LateDayZoneOpacity
            // (default 40%). Aug 2026: raised the clamp ceiling to match the
            // widened [Range(5,90)] property, and softened the middle-fade
            // multiplier 0.35->0.6 -- at the old, much lower default opacity
            // the 0.35 multiplier faded the zone's middle to near-nothing,
            // compounding the low-opacity visibility problem this whole
            // change fixes (see "more visible" follow-up in CLAUDE.md).
            float edgeA = (float)Math.Max(0.02, Math.Min(0.90, LateDayZoneOpacity / 100.0));
            float midA  = edgeA * 0.6f;

            var stops = new SharpDX.Direct2D1.GradientStop[3];
            stops[0].Position = 0.0f; stops[0].Color = new SharpDX.Color4(1.0f, 0.82f, 0.30f, edgeA);
            stops[1].Position = 0.5f; stops[1].Color = new SharpDX.Color4(1.0f, 0.82f, 0.30f, midA);
            stops[2].Position = 1.0f; stops[2].Color = new SharpDX.Color4(1.0f, 0.82f, 0.30f, edgeA);

            using (var stopCollection = new SharpDX.Direct2D1.GradientStopCollection(RenderTarget, stops))
            using (var gradientBrush = new SharpDX.Direct2D1.LinearGradientBrush(
                       RenderTarget,
                       new SharpDX.Direct2D1.LinearGradientBrushProperties
                       {
                           StartPoint = new SharpDX.Vector2(0, yTop),
                           EndPoint   = new SharpDX.Vector2(0, yBot)
                       },
                       stopCollection))
            {
                RenderTarget.FillRectangle(rect, gradientBrush);
            }
        }

        // ── Public accessors ──────────────────────────────────────────────────
        public double SessionOpen930      => nyOpen;
        public double SessionOpenGlobex   => globexOpen;
        public double MedianRangeTicks    => medianRangeTicks;
        public double MinRangeTicks       => minRangeTicks;
        public double TargetRangeTicks    => UseMedian ? medianRangeTicks : minRangeTicks;
        public int    SignalDirection     => signalDirection;
        public bool   TradeTakenToday    => tradeTakenToday;
        public bool   WindowClosed       => windowClosed;
        public double DevelopingHighTicks => sessionOpen > 0 ? (sessionHigh - sessionOpen) / TickSize : 0;
        public double DevelopingLowTicks  => sessionOpen > 0 ? (sessionOpen - sessionLow)  / TickSize : 0;

        public double DailyRangeTicks =>
            sessionOpen > 0 && sessionHigh > double.MinValue && sessionLow < double.MaxValue
                ? (sessionHigh - sessionLow) / TickSize
                : 0;

        #region Properties

        [NinjaScriptProperty]
        [Range(2, 252)]
        [Display(Name = "Range Lookback Days", Description = "Number of past sessions to include in range calculation.", Order = 1, GroupName = "Parameters")]
        public int RangeLookbackDays { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Use Median (unchecked = Minimum)", Description = "Target = Median range if checked, Minimum range if unchecked.", Order = 2, GroupName = "Parameters")]
        public bool UseMedian { get; set; }

        [NinjaScriptProperty]
        [Range(1, 100)]
        [Display(Name = "Min Displacement (ticks)", Description = "Minimum ticks price must be above/below open to generate a signal.", Order = 3, GroupName = "Parameters")]
        public int MinDisplacementTicks { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "ATR Period", Description = "Lookback period for the ATR used to space the rainbow level lines.", Order = 4, GroupName = "Parameters")]
        public int AtrPeriod { get; set; }

        [NinjaScriptProperty]
        [Range(0.1, double.MaxValue)]
        [Display(Name = "ATR Multiplier", Description = "Step size between each rainbow level = ATR × this multiplier.", Order = 5, GroupName = "Parameters")]
        public double AtrMultiplier { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Show ATR Lines", Description = "Show or hide the ATR rainbow level lines above and below the session open.", Order = 6, GroupName = "Parameters")]
        public bool ShowAtrLines { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Show Prev Close", Description = "Draw a horizontal line at the previous session's closing price.", Order = 7, GroupName = "Parameters")]
        public bool ShowPrevClose { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Show Late-Day Zone", Description = "Shade a zone spanning the high/low of the previous session's last N minutes of activity (video default: last 45-60 minutes).", Order = 8, GroupName = "Parameters")]
        public bool ShowLateDayZone { get; set; }

        [NinjaScriptProperty]
        [Range(15, 180)]
        [Display(Name = "Late-Day Lookback (minutes)", Description = "Trailing minutes of the previous session used to build the late-day zone.", Order = 9, GroupName = "Parameters")]
        public int LateDayLookbackMinutes { get; set; }

        [NinjaScriptProperty]
        [Range(5, 90)]
        [Display(Name = "Late-Day Zone Opacity (%)", Description = "Opacity at the zone's own top/bottom edges for the gradient fill; the middle fades to roughly 60% of this. Default 40 -- raise if the zone is still hard to see on your chart's background/color scheme.", Order = 10, GroupName = "Parameters")]
        public int LateDayZoneOpacity { get; set; }

        // ── Plot accessors ────────────────────────────────────────────────────
        [Browsable(false)] [XmlIgnore]
        public Series<double> SessionOpenPlot => Values[PLOT_OPEN];

        [Browsable(false)] [XmlIgnore]
        public Series<double> UpperTarget     => Values[PLOT_UP_TARGET];

        [Browsable(false)] [XmlIgnore]
        public Series<double> LowerTarget     => Values[PLOT_DN_TARGET];

        [Browsable(false)] [XmlIgnore]
        public Series<double> HighOfDayPlot   => Values[PLOT_HOD];

        [Browsable(false)] [XmlIgnore]
        public Series<double> LowOfDayPlot    => Values[PLOT_LOD];

        [Browsable(false)] [XmlIgnore]
        public Series<double> PrevClosePlot   => Values[PLOT_PREV_CLOSE];

        [Browsable(false)] [XmlIgnore]
        public Series<double> LateDayHighPlot => Values[PLOT_LATE_HIGH];

        [Browsable(false)] [XmlIgnore]
        public Series<double> LateDayLowPlot  => Values[PLOT_LATE_LOW];

        #endregion
    }
}

#region NinjaScript generated code. Neither change nor remove.

namespace NinjaTrader.NinjaScript.Indicators
{
	public partial class Indicator : NinjaTrader.Gui.NinjaScript.IndicatorRenderBase
	{
		private AlgoTrader.DailyRange[] cacheDailyRange;
		public AlgoTrader.DailyRange DailyRange(int rangeLookbackDays, bool useMedian, int minDisplacementTicks, int atrPeriod, double atrMultiplier, bool showAtrLines, bool showPrevClose, bool showLateDayZone, int lateDayLookbackMinutes, int lateDayZoneOpacity)
		{
			return DailyRange(Input, rangeLookbackDays, useMedian, minDisplacementTicks, atrPeriod, atrMultiplier, showAtrLines, showPrevClose, showLateDayZone, lateDayLookbackMinutes, lateDayZoneOpacity);
		}

		public AlgoTrader.DailyRange DailyRange(ISeries<double> input, int rangeLookbackDays, bool useMedian, int minDisplacementTicks, int atrPeriod, double atrMultiplier, bool showAtrLines, bool showPrevClose, bool showLateDayZone, int lateDayLookbackMinutes, int lateDayZoneOpacity)
		{
			if (cacheDailyRange != null)
				for (int idx = 0; idx < cacheDailyRange.Length; idx++)
					if (cacheDailyRange[idx] != null && cacheDailyRange[idx].RangeLookbackDays == rangeLookbackDays && cacheDailyRange[idx].UseMedian == useMedian && cacheDailyRange[idx].MinDisplacementTicks == minDisplacementTicks && cacheDailyRange[idx].AtrPeriod == atrPeriod && cacheDailyRange[idx].AtrMultiplier == atrMultiplier && cacheDailyRange[idx].ShowAtrLines == showAtrLines && cacheDailyRange[idx].ShowPrevClose == showPrevClose && cacheDailyRange[idx].ShowLateDayZone == showLateDayZone && cacheDailyRange[idx].LateDayLookbackMinutes == lateDayLookbackMinutes && cacheDailyRange[idx].LateDayZoneOpacity == lateDayZoneOpacity && cacheDailyRange[idx].EqualsInput(input))
						return cacheDailyRange[idx];
			return CacheIndicator<AlgoTrader.DailyRange>(new AlgoTrader.DailyRange(){ RangeLookbackDays = rangeLookbackDays, UseMedian = useMedian, MinDisplacementTicks = minDisplacementTicks, AtrPeriod = atrPeriod, AtrMultiplier = atrMultiplier, ShowAtrLines = showAtrLines, ShowPrevClose = showPrevClose, ShowLateDayZone = showLateDayZone, LateDayLookbackMinutes = lateDayLookbackMinutes, LateDayZoneOpacity = lateDayZoneOpacity }, input, ref cacheDailyRange);
		}
	}
}

namespace NinjaTrader.NinjaScript.MarketAnalyzerColumns
{
	public partial class MarketAnalyzerColumn : MarketAnalyzerColumnBase
	{
		public Indicators.AlgoTrader.DailyRange DailyRange(int rangeLookbackDays, bool useMedian, int minDisplacementTicks, int atrPeriod, double atrMultiplier, bool showAtrLines, bool showPrevClose, bool showLateDayZone, int lateDayLookbackMinutes, int lateDayZoneOpacity)
		{
			return indicator.DailyRange(Input, rangeLookbackDays, useMedian, minDisplacementTicks, atrPeriod, atrMultiplier, showAtrLines, showPrevClose, showLateDayZone, lateDayLookbackMinutes, lateDayZoneOpacity);
		}

		public Indicators.AlgoTrader.DailyRange DailyRange(ISeries<double> input , int rangeLookbackDays, bool useMedian, int minDisplacementTicks, int atrPeriod, double atrMultiplier, bool showAtrLines, bool showPrevClose, bool showLateDayZone, int lateDayLookbackMinutes, int lateDayZoneOpacity)
		{
			return indicator.DailyRange(input, rangeLookbackDays, useMedian, minDisplacementTicks, atrPeriod, atrMultiplier, showAtrLines, showPrevClose, showLateDayZone, lateDayLookbackMinutes, lateDayZoneOpacity);
		}
	}
}

namespace NinjaTrader.NinjaScript.Strategies
{
	public partial class Strategy : NinjaTrader.Gui.NinjaScript.StrategyRenderBase
	{
		public Indicators.AlgoTrader.DailyRange DailyRange(int rangeLookbackDays, bool useMedian, int minDisplacementTicks, int atrPeriod, double atrMultiplier, bool showAtrLines, bool showPrevClose, bool showLateDayZone, int lateDayLookbackMinutes, int lateDayZoneOpacity)
		{
			return indicator.DailyRange(Input, rangeLookbackDays, useMedian, minDisplacementTicks, atrPeriod, atrMultiplier, showAtrLines, showPrevClose, showLateDayZone, lateDayLookbackMinutes, lateDayZoneOpacity);
		}

		public Indicators.AlgoTrader.DailyRange DailyRange(ISeries<double> input , int rangeLookbackDays, bool useMedian, int minDisplacementTicks, int atrPeriod, double atrMultiplier, bool showAtrLines, bool showPrevClose, bool showLateDayZone, int lateDayLookbackMinutes, int lateDayZoneOpacity)
		{
			return indicator.DailyRange(input, rangeLookbackDays, useMedian, minDisplacementTicks, atrPeriod, atrMultiplier, showAtrLines, showPrevClose, showLateDayZone, lateDayLookbackMinutes, lateDayZoneOpacity);
		}
	}
}

#endregion
