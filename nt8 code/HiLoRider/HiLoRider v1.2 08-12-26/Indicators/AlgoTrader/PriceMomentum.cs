// file name = PriceMomentum.cs
#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Text;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Input;
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

namespace NinjaTrader.NinjaScript.Indicators.AlgoTrader
{
    public class PriceMomentum : Indicator
    {
        private Series<double> rawDiffSeries;
        private Series<double> absDiffSeries;
        private MAX            _maxAbsDiff;
        private double         _currentDynamicMax;
        private double _prevTrendLevel;
        private bool   _hasPrevTrendLevel;

        private void NormalizeThresholdRelationship()
        {
            double requestedMin = DiffMinPercent;
            double requestedMax = DiffMaxPercent;
            double normalizedMin = double.IsNaN(requestedMin) || double.IsInfinity(requestedMin)
                ? 20.0 : Math.Max(1.0, Math.Min(100.0, requestedMin));
            double normalizedMax = double.IsNaN(requestedMax) || double.IsInfinity(requestedMax)
                ? 80.0 : Math.Max(1.0, Math.Min(100.0, requestedMax));
            if (normalizedMin > normalizedMax)
            {
                double swap = normalizedMin; normalizedMin = normalizedMax; normalizedMax = swap;
            }
            else if (Math.Abs(normalizedMin - normalizedMax) <= 1e-9)
            {
                if (normalizedMin < 100.0) normalizedMax = normalizedMin + 1.0;
                else { normalizedMin = 99.0; normalizedMax = 100.0; }
            }
            DiffMinPercent = normalizedMin;
            DiffMaxPercent = normalizedMax;
            if (double.IsNaN(requestedMin) || double.IsInfinity(requestedMin)
                || double.IsNaN(requestedMax) || double.IsInfinity(requestedMax)
                || Math.Abs(requestedMin - normalizedMin) > 1e-9
                || Math.Abs(requestedMax - normalizedMax) > 1e-9)
                Print($"[Price Momentum] Thresholds normalized from {requestedMin}/{requestedMax} "
                    + $"to {normalizedMin}/{normalizedMax}; Min must be below Max.");
        }

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description                                 = @"Price Momentum Oscillator. Adaptive Exhaustion lines based on the highest absolute Diff over a lookback period.";
                Name                                        = "Price Momentum";
                Calculate                                   = Calculate.OnBarClose;
                IsOverlay                                   = false;
                DrawOnPricePanel                            = true;

                // --- Thresholds ---
                DiffMaxLookback     = 20;
                DiffMinPercent      = 20;
                DiffMaxPercent      = 80;

                // --- Price-panel trend line ---
                ShowPriceTrendLine  = false;
                OffsetTicks         = 40;
                TrendLineWidth      = 2;
                
                // --- Plots ---
                AddPlot(new Stroke(Brushes.Cyan, 6), PlotStyle.Bar, "DiffHistogram");
                
                AddLine(new Stroke(Brushes.Gray, DashStyleHelper.Solid, 2), 0, "ZeroLine");
                AddLine(new Stroke(Brushes.Lime, DashStyleHelper.Dash, 2),  1.5, "DiffMinPos");
                AddLine(new Stroke(Brushes.Lime, DashStyleHelper.Dash, 2), -1.5, "DiffMinNeg");
                AddLine(new Stroke(Brushes.Red, DashStyleHelper.Dash, 2),  3.7, "DiffMaxPos");
                AddLine(new Stroke(Brushes.Red, DashStyleHelper.Dash, 2), -3.7, "DiffMaxNeg");
            }
            else if (State == State.Configure)
            {
                NormalizeThresholdRelationship();
            }
            else if (State == State.DataLoaded)
            {
                rawDiffSeries      = new Series<double>(this);
                absDiffSeries      = new Series<double>(this);
                _maxAbsDiff        = MAX(absDiffSeries, DiffMaxLookback);
                _currentDynamicMax = 0;
                _hasPrevTrendLevel = false;
            }
        }

        protected override void OnBarUpdate()
        {
            if (CurrentBar < 2)
            {
                rawDiffSeries[0] = 0;
                absDiffSeries[0] = 0;
                _currentDynamicMax = 0;
                return;
            }

            double rawDiff = Close[0] - Close[1];
            rawDiffSeries[0] = rawDiff;

            const double tmoAlpha = 0.33;
            double previousDiff  = DiffHistogram.IsValidDataPoint(1) ? DiffHistogram[1] : 0;
            double smoothedDiff  = (rawDiff * tmoAlpha) + (previousDiff * (1.0 - tmoAlpha));

            DiffHistogram[0] = smoothedDiff;
            absDiffSeries[0] = Math.Abs(smoothedDiff);

            // Reading [0] drives the cached child indicator's update. Checking
            // IsValidDataPoint first can leave a lazily evaluated MAX uninitialized.
            _currentDynamicMax = _maxAbsDiff != null ? _maxAbsDiff[0] : 0;
            double currentDynamicMax = _currentDynamicMax;
            double visualExhaustion = currentDynamicMax * (DiffMaxPercent / 100.0);
			double visualDiffMin = currentDynamicMax * (DiffMinPercent / 100.0);

            Lines[1].Value =  visualDiffMin;
            Lines[2].Value = -visualDiffMin;
            Lines[3].Value =  visualExhaustion;
            Lines[4].Value = -visualExhaustion;

            bool isUp      = smoothedDiff >= 0;
            bool isGrowing = Math.Abs(smoothedDiff) > Math.Abs(previousDiff);

            if (isUp)
                PlotBrushes[0][0] = isGrowing ? Brushes.Lime      : Brushes.DarkGreen;
            else
                PlotBrushes[0][0] = isGrowing ? Brushes.Red       : Brushes.Maroon;

            if (ShowPriceTrendLine)
            {
                double offset = OffsetTicks * TickSize;
                double level  = isUp ? Low[0] - offset : High[0] + offset;
                Brush  brush  = isUp ? Brushes.Lime : Brushes.Red;

                if (_hasPrevTrendLevel)
                {
                    Draw.Line(this, "PMTrend" + CurrentBar, false, 1, _prevTrendLevel, 0, level,
                        brush, DashStyleHelper.Solid, TrendLineWidth);
                }

                _prevTrendLevel    = level;
                _hasPrevTrendLevel = true;
            }
        }

        #region Properties
        [Range(1, 10000)]
        [NinjaScriptProperty]
        [Display(Name="Diff Max Lookback", Order=1, GroupName="Thresholds")]
        public int DiffMaxLookback { get; set; }

        [Range(0, 100)]
        [NinjaScriptProperty]
        [Display(Name="Diff Min Threshold", Order=2, GroupName="Thresholds")]
        public double DiffMinPercent { get; set; }

        [Range(0, 100)]
        [NinjaScriptProperty]
        [Display(Name="Exhaustion Line %", Order=3, GroupName="Thresholds")]
        public double DiffMaxPercent { get; set; }

        // Not [NinjaScriptProperty] on purpose: keeping these out of PriceMomentum's
        // generated constructor signature so the ~14 bots calling PriceMomentum(lookback, min, max)
        // don't all need updating. Defaults are set in SetDefaults; edit in code if needed.
        [Browsable(false)] [XmlIgnore]
        public bool ShowPriceTrendLine { get; set; }

        [Browsable(false)] [XmlIgnore]
        public int OffsetTicks { get; set; }

        [Browsable(false)] [XmlIgnore]
        public int TrendLineWidth { get; set; }

        [Browsable(false)] [XmlIgnore]
        public Series<double> DiffHistogram => Values[0];

        [Browsable(false)] [XmlIgnore] 
        public double CurrentDiff => IsValidDataPoint(0) ? DiffHistogram[0] : 0;

        // COMPATIBILITY PROPERTY: Fixes CS1061
        // Allows strategies using .DiffMax to automatically use the new Adaptive Max
        [Browsable(false)] [XmlIgnore] 
        public double DiffMax => CurrentDynamicMax;

        [Browsable(false)] [XmlIgnore] 
        public double CurrentDynamicMax => IsValidDataPoint(0) ? _currentDynamicMax : 0;

        // Returns the adaptive maximum ending at a specific completed signal bar.
        // Strategies use this instead of CurrentDynamicMax when their decision is
        // based on bar [1], keeping the threshold and oscillator snapshot aligned.
        public double GetDynamicMax(int barsAgo)
        {
            if (absDiffSeries == null || barsAgo < 0 || CurrentBar < barsAgo)
                return 0;
            double max = 0;
            int last = Math.Min(CurrentBar, barsAgo + DiffMaxLookback - 1);
            for (int i = barsAgo; i <= last; i++)
                if (absDiffSeries.IsValidDataPoint(i))
                    max = Math.Max(max, absDiffSeries[i]);
            return max;
        }

        // Single source of truth for entry thresholds — same values drawn on chart as visual lines.
        // MomentumBot reads these directly so threshold math is never duplicated in the strategy.
        [Browsable(false)] [XmlIgnore]
        public double CurrentDiffMin => CurrentDynamicMax > 0
            ? CurrentDynamicMax * (DiffMinPercent / 100.0)
            : 0;

        [Browsable(false)] [XmlIgnore]
        public double CurrentDiffMax => CurrentDynamicMax > 0
            ? CurrentDynamicMax * (DiffMaxPercent / 100.0)
            : 0;

        #endregion
    }
}

#region NinjaScript generated code. Neither change nor remove.

namespace NinjaTrader.NinjaScript.Indicators
{
	public partial class Indicator : NinjaTrader.Gui.NinjaScript.IndicatorRenderBase
	{
		private AlgoTrader.PriceMomentum[] cachePriceMomentum;
		public AlgoTrader.PriceMomentum PriceMomentum(int diffMaxLookback, double diffMinPercent, double diffMaxPercent)
		{
			return PriceMomentum(Input, diffMaxLookback, diffMinPercent, diffMaxPercent);
		}

		public AlgoTrader.PriceMomentum PriceMomentum(ISeries<double> input, int diffMaxLookback, double diffMinPercent, double diffMaxPercent)
		{
			if (cachePriceMomentum != null)
				for (int idx = 0; idx < cachePriceMomentum.Length; idx++)
					if (cachePriceMomentum[idx] != null && cachePriceMomentum[idx].DiffMaxLookback == diffMaxLookback && cachePriceMomentum[idx].DiffMinPercent == diffMinPercent && cachePriceMomentum[idx].DiffMaxPercent == diffMaxPercent && cachePriceMomentum[idx].EqualsInput(input))
						return cachePriceMomentum[idx];
			return CacheIndicator<AlgoTrader.PriceMomentum>(new AlgoTrader.PriceMomentum(){ DiffMaxLookback = diffMaxLookback, DiffMinPercent = diffMinPercent, DiffMaxPercent = diffMaxPercent }, input, ref cachePriceMomentum);
		}
	}
}

namespace NinjaTrader.NinjaScript.MarketAnalyzerColumns
{
	public partial class MarketAnalyzerColumn : MarketAnalyzerColumnBase
	{
		public Indicators.AlgoTrader.PriceMomentum PriceMomentum(int diffMaxLookback, double diffMinPercent, double diffMaxPercent)
		{
			return indicator.PriceMomentum(Input, diffMaxLookback, diffMinPercent, diffMaxPercent);
		}

		public Indicators.AlgoTrader.PriceMomentum PriceMomentum(ISeries<double> input , int diffMaxLookback, double diffMinPercent, double diffMaxPercent)
		{
			return indicator.PriceMomentum(input, diffMaxLookback, diffMinPercent, diffMaxPercent);
		}
	}
}

namespace NinjaTrader.NinjaScript.Strategies
{
	public partial class Strategy : NinjaTrader.Gui.NinjaScript.StrategyRenderBase
	{
		public Indicators.AlgoTrader.PriceMomentum PriceMomentum(int diffMaxLookback, double diffMinPercent, double diffMaxPercent)
		{
			return indicator.PriceMomentum(Input, diffMaxLookback, diffMinPercent, diffMaxPercent);
		}

		public Indicators.AlgoTrader.PriceMomentum PriceMomentum(ISeries<double> input , int diffMaxLookback, double diffMinPercent, double diffMaxPercent)
		{
			return indicator.PriceMomentum(input, diffMaxLookback, diffMinPercent, diffMaxPercent);
		}
	}
}

#endregion
