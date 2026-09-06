// file name = SuperTrendRegime.cs
// Namespace   : NinjaTrader.NinjaScript.Indicators.AlgoTrader
//
// Port of a user-supplied ThinkScript study, Jul 2026. See CLAUDE.md for the
// full port notes (dead-code findings in the original, design decisions made
// while porting).
//
// ── WHAT THIS INDICATOR DOES ────────────────────────────────────────────────
//
//  Intraday trend-regime classifier, active only on the current/most-recent
//  session. Builds a composite trendline ("SuperLine") = average of:
//    • EMA(200)
//    • EMA(72)
//    • a volume-weighted average of the running session VWAP, itself
//      re-anchored over a selectable outer period (Day / Week / Month)
//
//  Classifies each bar into one of 4 regimes by comparing Close to SuperLine
//  (bullish/bearish) and Close to today's session-high/low midpoint
//  (strong/weak):
//    STRONG BULLISH — Close > SuperLine AND Close > session midpoint
//    WEAK BULLISH   — Close > SuperLine AND Close <= session midpoint
//    STRONG BEARISH — Close < SuperLine AND Close < session midpoint
//    WEAK BEARISH   — Close < SuperLine AND Close >= session midpoint
//
//  Displayed as a fixed on-chart text label, and optionally a colored
//  "ribbon" strip of square markers pinned near the chart's lowest price.
//
// ── PORTED FROM THINKSCRIPT — DEAD CODE FOUND, COMPLETED RATHER THAN COPIED ─
//
//  The source ThinkScript declared its bearish ribbon squares and crossover
//  arrows with `def` (not `plot`) and had their styling calls commented out
//  -- in ThinkScript, `def` never renders regardless of styling, so both
//  features were fully inert despite `showArrows`/`showRibbon` existing as
//  toggles that imply they were meant to work. This port makes them real,
//  working plots gated by those same toggles rather than reproducing the
//  dead code as-is. Also dropped: `range`, a fast/slow EMA(72)/EMA(89) pair,
//  and a volume*vwap^2 accumulator -- all computed in the source but never
//  referenced by anything, pure dead weight.
//
// ── STRATEGY USAGE ──────────────────────────────────────────────────────────
//
//   var str = SuperTrendRegime(true, false, false, 930, RegimeTimeFrame.Day);
//   AddChartIndicator(str);
//   double superLine = str.SuperLine[0];
//   int    regime    = str.Regime[0];   // +2 strong bull / +1 weak bull / -1 weak bear / -2 strong bear / 0 n/a (not today)
//
// ────────────────────────────────────────────────────────────────────────────

#region Using declarations
using System;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Windows.Media;
using System.Xml.Serialization;
using NinjaTrader.Cbi;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
using NinjaTrader.Gui.SuperDom;
using NinjaTrader.Gui.Tools;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
using NinjaTrader.Core.FloatingPoint;
using NinjaTrader.NinjaScript.DrawingTools;
using NinjaTrader.NinjaScript.Indicators;
#endregion

namespace NinjaTrader.NinjaScript.Indicators
{
    // Both the enum and the class live directly in the flat Indicators namespace,
    // NOT nested under .AlgoTrader -- matching TrendArchitect.cs, the only other
    // file in this codebase with custom enums on a [NinjaScriptProperty]. NT8's
    // generated wrapper code proved sensitive to a nested-namespace class paired
    // with a custom enum (two different regenerations each broke a different
    // qualified reference); this flat structure is the one pattern confirmed to
    // already compile cleanly elsewhere in this project.
    public enum RegimeTimeFrame { Day, Week, Month }

    public class SuperTrendRegime : Indicator
    {
        // ── Plot indices ─────────────────────────────────────────────────────
        private const int PLOT_SUPERLINE   = 0;
        private const int PLOT_REGIME      = 1; // hidden state series for strategies
        private const int PLOT_BULL_WEAK   = 2;
        private const int PLOT_BULL_STRONG = 3;
        private const int PLOT_BEAR_WEAK   = 4;
        private const int PLOT_BEAR_STRONG = 5;
        private const int PLOT_ARROW_UP    = 6;
        private const int PLOT_ARROW_DN    = 7;

        private EMA ema200, ema72;

        // Outer-period (Day/Week/Month) VWAP-of-VWAP accumulators
        private double _volumeSum, _volumeVwapSum;
        private object _prevPeriodKey;
        private DateTime _firstBarSunday = DateTime.MinValue;

        // Running session VWAP (resets every session, independent of the
        // outer Day/Week/Month anchor above -- mirrors ThinkScript's `vwap`
        // built-in, which is always session-scoped regardless of chart TF)
        private double _sessionCumPV, _sessionCumVol;

        // Today's running session high/low
        private double _todayHigh, _todayLow;
        private bool _wasToday;

        private TimeSpan _arrowsFromTimeSpan;

        [NinjaScriptProperty]
        [Display(Name = "Show Labels", GroupName = "01. Parameters", Order = 0)]
        public bool ShowLabels { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Show Arrows", GroupName = "01. Parameters", Order = 1)]
        public bool ShowArrows { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Show Ribbon", GroupName = "01. Parameters", Order = 2)]
        public bool ShowRibbon { get; set; }

        [NinjaScriptProperty]
        [Range(0, 2359)]
        [Display(Name = "Arrows From Time (HHmm)", GroupName = "01. Parameters", Order = 3,
            Description = "Arrows only fire at/after this time of day, e.g. 630 = 06:30")]
        public int ArrowsFromTime { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Time Frame", GroupName = "01. Parameters", Order = 4,
            Description = "Outer anchor period for the VWAP-of-VWAP component of SuperLine")]
        public RegimeTimeFrame TimeFrame { get; set; }

        [Browsable(false)]
        [XmlIgnore]
        public Series<double> SuperLine => Values[PLOT_SUPERLINE];

        [Browsable(false)]
        [XmlIgnore]
        public Series<double> Regime => Values[PLOT_REGIME];

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Port of a ThinkScript intraday trend-regime study -- composite " +
                    "EMA(200)/EMA(72)/anchored-VWAP trendline classified into 4 strength states.";
                Name                    = "Super Trend Regime";
                IsSuspendedWhileInactive = true;
                IsOverlay                = true;

                ShowLabels     = true;
                ShowArrows     = false;
                ShowRibbon     = false;
                ArrowsFromTime = 630;
                TimeFrame      = RegimeTimeFrame.Day;

                AddPlot(new Stroke(Brushes.DodgerBlue, 2), PlotStyle.Line, "SuperLine");
                AddPlot(Brushes.Transparent, "Regime");

                AddPlot(new Stroke(Brushes.LightGreen, 5), PlotStyle.Square, "BullWeak");
                AddPlot(new Stroke(Brushes.Green,      5), PlotStyle.Square, "BullStrong");
                AddPlot(new Stroke(Brushes.LightPink,  5), PlotStyle.Square, "BearWeak");
                AddPlot(new Stroke(Brushes.Red,        5), PlotStyle.Square, "BearStrong");

                AddPlot(new Stroke(Brushes.Green, 3), PlotStyle.TriangleUp,   "ArrowUp");
                AddPlot(new Stroke(Brushes.Red,   3), PlotStyle.TriangleDown, "ArrowDown");
            }
            else if (State == State.DataLoaded)
            {
                ema200 = EMA(200);
                ema72  = EMA(72);
                _arrowsFromTimeSpan = new TimeSpan(ArrowsFromTime / 100, ArrowsFromTime % 100, 0);
            }
        }

        protected override void OnBarUpdate()
        {
            if (CurrentBar < 200) return;

            // ── "today" = this bar belongs to the most recent session currently
            // loaded -- matches ThinkScript's getDay() >= getLastDay() (a bar's
            // date can never exceed the chart's last date, only equal or trail it)
            bool today = Time[0].Date == Bars.GetTime(Bars.Count - 1).Date;

            // ── Running session VWAP (always session-scoped, like ThinkScript's
            // `vwap` built-in) -- resets on the first bar of every session
            if (Bars.IsFirstBarOfSession)
            {
                _sessionCumPV  = 0;
                _sessionCumVol = 0;
            }
            double typicalPx = (High[0] + Low[0] + Close[0]) / 3.0;
            _sessionCumVol += Volume[0];
            _sessionCumPV  += typicalPx * Volume[0];
            double sessionVwap = _sessionCumVol > 0 ? _sessionCumPV / _sessionCumVol : Close[0];

            // ── Outer Day/Week/Month anchor -- volume-weighted average of the
            // session VWAP itself, re-anchored each time the period rolls over
            object periodKey = ComputePeriodKey(Time[0].Date);
            bool periodRolled = _prevPeriodKey == null || !periodKey.Equals(_prevPeriodKey);
            if (periodRolled)
            {
                _volumeSum     = Volume[0];
                _volumeVwapSum = Volume[0] * sessionVwap;
            }
            else
            {
                _volumeSum     += Volume[0];
                _volumeVwapSum += Volume[0] * sessionVwap;
            }
            _prevPeriodKey = periodKey;
            double anchoredVwap = _volumeSum > 0 ? _volumeVwapSum / _volumeSum : sessionVwap;

            double superLine = (ema200[0] + ema72[0] + anchoredVwap) / 3.0;
            Values[PLOT_SUPERLINE][0] = superLine;

            // ── Today's running session high/low ────────────────────────────
            if (today && !_wasToday)
            {
                _todayHigh = High[0];
                _todayLow  = Low[0];
            }
            else if (today)
            {
                _todayHigh = Math.Max(_todayHigh, High[0]);
                _todayLow  = Math.Min(_todayLow,  Low[0]);
            }
            _wasToday = today;

            double sessionMid   = (_todayHigh + _todayLow) / 2.0;
            bool aboveSessionMid = Close[0] > sessionMid;
            bool belowSessionMid = Close[0] < sessionMid;

            bool bullish = Close[0] > superLine;
            bool bearish = Close[0] < superLine;

            // Regime[0]: +2 strong bull, +1 weak bull, -1 weak bear, -2 strong bear, 0 n/a
            int regime = 0;
            if (today && bullish)
                regime = aboveSessionMid ? 2 : 1;
            else if (today && bearish)
                regime = belowSessionMid ? -2 : -1;
            Values[PLOT_REGIME][0] = regime;

            // ── Ribbon (pinned near the chart's lowest-so-far price, matching
            // ThinkScript's lowestAll(lo) - 5*TickSize) ─────────────────────
            double ribbonY = MIN(Low, CurrentBar)[0] - 5 * TickSize;
            bool showRibbonToday = today && ShowRibbon;
            Values[PLOT_BULL_WEAK][0]   = (showRibbonToday && regime == 1)  ? ribbonY : double.NaN;
            Values[PLOT_BULL_STRONG][0] = (showRibbonToday && regime == 2)  ? ribbonY : double.NaN;
            Values[PLOT_BEAR_WEAK][0]   = (showRibbonToday && regime == -1) ? ribbonY : double.NaN;
            Values[PLOT_BEAR_STRONG][0] = (showRibbonToday && regime == -2) ? ribbonY : double.NaN;

            // ── Crossover arrows (Close crossing SuperLine), gated by ShowArrows
            // and time-of-day, matching ThinkScript's secondsFromTime(from) >= 0
            bool afterArrowTime = Time[0].TimeOfDay >= _arrowsFromTimeSpan;
            bool crossedUp   = ShowArrows && afterArrowTime
                && Close[0] > superLine && Close[1] <= Values[PLOT_SUPERLINE][1];
            bool crossedDown = ShowArrows && afterArrowTime
                && Close[0] < superLine && Close[1] >= Values[PLOT_SUPERLINE][1];
            Values[PLOT_ARROW_UP][0]   = crossedUp   ? Low[0]  : double.NaN;
            Values[PLOT_ARROW_DN][0]   = crossedDown ? High[0] : double.NaN;

            // ── Label (top-right, one line reflecting whichever regime is active) ──
            if (ShowLabels && today && regime != 0)
            {
                string text = regime == 2  ? "STRONG BULLISH"
                            : regime == 1  ? "WEAK BULLISH"
                            : regime == -2 ? "STRONG BEARISH"
                            :                "WEAK BEARISH";
                Brush color = regime == 2  ? Brushes.Green
                            : regime == 1  ? Brushes.LightGreen
                            : regime == -2 ? Brushes.Red
                            :                Brushes.LightPink;
                Draw.TextFixed(this, "SuperTrendRegimeLabel", text, TextPosition.TopRight,
                    color, new SimpleFont("Arial", 12), Brushes.Transparent, Brushes.Transparent, 0);
            }
            else if (!ShowLabels || regime == 0)
            {
                RemoveDrawObject("SuperTrendRegimeLabel");
            }
        }

        private object ComputePeriodKey(DateTime date)
        {
            switch (TimeFrame)
            {
                case RegimeTimeFrame.Month:
                    return date.Year * 100 + date.Month;
                case RegimeTimeFrame.Week:
                    if (_firstBarSunday == DateTime.MinValue)
                        _firstBarSunday = date.AddDays(-(int)date.DayOfWeek);
                    return (int)Math.Floor((date - _firstBarSunday).TotalDays / 7.0);
                default: // Day
                    return date;
            }
        }
    }
}

#region NinjaScript generated code. Neither change nor remove.

namespace NinjaTrader.NinjaScript.Indicators
{
	public partial class Indicator : NinjaTrader.Gui.NinjaScript.IndicatorRenderBase
	{
		private SuperTrendRegime[] cacheSuperTrendRegime;
		public SuperTrendRegime SuperTrendRegime(bool showLabels, bool showArrows, bool showRibbon, int arrowsFromTime, RegimeTimeFrame timeFrame)
		{
			return SuperTrendRegime(Input, showLabels, showArrows, showRibbon, arrowsFromTime, timeFrame);
		}

		public SuperTrendRegime SuperTrendRegime(ISeries<double> input, bool showLabels, bool showArrows, bool showRibbon, int arrowsFromTime, RegimeTimeFrame timeFrame)
		{
			if (cacheSuperTrendRegime != null)
				for (int idx = 0; idx < cacheSuperTrendRegime.Length; idx++)
					if (cacheSuperTrendRegime[idx] != null && cacheSuperTrendRegime[idx].ShowLabels == showLabels && cacheSuperTrendRegime[idx].ShowArrows == showArrows && cacheSuperTrendRegime[idx].ShowRibbon == showRibbon && cacheSuperTrendRegime[idx].ArrowsFromTime == arrowsFromTime && cacheSuperTrendRegime[idx].TimeFrame == timeFrame && cacheSuperTrendRegime[idx].EqualsInput(input))
						return cacheSuperTrendRegime[idx];
			return CacheIndicator<SuperTrendRegime>(new SuperTrendRegime(){ ShowLabels = showLabels, ShowArrows = showArrows, ShowRibbon = showRibbon, ArrowsFromTime = arrowsFromTime, TimeFrame = timeFrame }, input, ref cacheSuperTrendRegime);
		}
	}
}

namespace NinjaTrader.NinjaScript.MarketAnalyzerColumns
{
	public partial class MarketAnalyzerColumn : MarketAnalyzerColumnBase
	{
		public Indicators.SuperTrendRegime SuperTrendRegime(bool showLabels, bool showArrows, bool showRibbon, int arrowsFromTime, RegimeTimeFrame timeFrame)
		{
			return indicator.SuperTrendRegime(Input, showLabels, showArrows, showRibbon, arrowsFromTime, timeFrame);
		}

		public Indicators.SuperTrendRegime SuperTrendRegime(ISeries<double> input , bool showLabels, bool showArrows, bool showRibbon, int arrowsFromTime, RegimeTimeFrame timeFrame)
		{
			return indicator.SuperTrendRegime(input, showLabels, showArrows, showRibbon, arrowsFromTime, timeFrame);
		}
	}
}

namespace NinjaTrader.NinjaScript.Strategies
{
	public partial class Strategy : NinjaTrader.Gui.NinjaScript.StrategyRenderBase
	{
		public Indicators.SuperTrendRegime SuperTrendRegime(bool showLabels, bool showArrows, bool showRibbon, int arrowsFromTime, RegimeTimeFrame timeFrame)
		{
			return indicator.SuperTrendRegime(Input, showLabels, showArrows, showRibbon, arrowsFromTime, timeFrame);
		}

		public Indicators.SuperTrendRegime SuperTrendRegime(ISeries<double> input , bool showLabels, bool showArrows, bool showRibbon, int arrowsFromTime, RegimeTimeFrame timeFrame)
		{
			return indicator.SuperTrendRegime(input, showLabels, showArrows, showRibbon, arrowsFromTime, timeFrame);
		}
	}
}

#endregion
