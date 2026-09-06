#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Linq;
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
#endregion

//This namespace holds Indicators in this folder and is required. Do not change it. 
namespace NinjaTrader.NinjaScript.Indicators.AlgoTrader
{
    public class HiLoBands : Indicator
    {
        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = @"Draw highest high and lowest low within the last x number of bars. Signal[0] = +1 when Close crosses above the middle line AND Close[0]>Close[1] (arrow up), -1 when Close crosses below it AND Close[0]<Close[1] (arrow down). Signal2[0] = +1 when the middle line itself is rising AND Close[0]>Close[1] (arrow up), -1 when falling AND Close[0]<Close[1] (arrow down).";
                Name = "HiLoBands";
                Calculate = Calculate.OnBarClose;
                IsOverlay = true;
                DisplayInDataBox = true;
                DrawOnPricePanel = true;
                DrawHorizontalGridLines = true;
                DrawVerticalGridLines = true;
                PaintPriceMarkers = true;
                ScaleJustification = NinjaTrader.Gui.Chart.ScaleJustification.Right;
                IsSuspendedWhileInactive = true;

                LookbackPeriod = 20; // Standalone indicator default; HiLoRider supplies its Wave-120 instrument profile.
                Width = 2;
                ShowSignalArrows = true;

                // Add plots for the highest high and lowest low
				AddPlot(Brushes.Lime, "Highest High");
				AddPlot(Brushes.Red, "Lowest Low");
                AddPlot(Brushes.LightGray, "Middle Line");
                AddPlot(Brushes.Transparent, "Signal");    // Plot 3: +1/-1/0, not drawn as a line -- read via Signal[] or the arrows
                AddPlot(Brushes.Transparent, "Signal2");   // Plot 4: +1/-1/0, midline-slope entry logic -- read via Signal2[] or the arrows
            }
            else if (State == State.Configure)
            {
                // Set the stroke thickness for the plots
				Plots[0].Width = Width; // Set thickness for Highest High plot
                Plots[1].Width = Width; // Set thickness for Lowest Low plot
				Plots[2].Width = Width; // Set thickness for Middle Line plot
            }
        }

        protected override void OnBarUpdate()
        {
            // Ensure we have enough bars to calculate
            if (CurrentBar < LookbackPeriod)
                return;

            // Calculate the highest high and lowest low for the last x bars
            // highestHigh/lowestLow (fast lookback) are no longer plotted directly (the
            // Cyan/Magenta lines were removed) but are kept as local values -- Middle Line
            // still needs them for its own midpoint calc.
            double highestHigh = MAX(High, LookbackPeriod)[0];
            double lowestLow = MIN(Low, LookbackPeriod)[0];
			double midline = (highestHigh + lowestLow) / 2;

            // Assign the values to the plots
			Values[0][0] = highestHigh; // Assign to the first plot (Highest High)
            Values[1][0] = lowestLow;   // Assign to the second plot (Lowest Low)
			Values[2][0] = midline;   // Assign to the third plot (Middle Line)

            // Entry signal 1: fresh crossover of Close vs the middle line, confirmed by
            // Close[0] vs Close[1] (an up-close for longs, a down-close for shorts).
            // +1 = Close crosses above midline (long), -1 = crosses below (short), 0 = neither.
            Values[3][0] = 0;
            // Entry signal 2: the middle line's OWN slope, confirmed the same way. Does not
            // require a fresh crossover -- fires whenever the midline is rising/falling AND
            // Close itself is also moving in that direction. A different (looser, more
            // frequent) condition than Signal 1, not a variant of it.
            Values[4][0] = 0;
            if (CurrentBar >= LookbackPeriod + 1)
            {
                double prevMid = Values[2][1];

                if (Close[0] > midline && Close[1] <= prevMid && Close[0] > Close[1])
                {
                    Values[3][0] = 1;
                    if (ShowSignalArrows)
                        Draw.ArrowUp(this, "HLBUp" + CurrentBar, false, 0, Low[0] - 4 * TickSize, Brushes.Lime);
                }
                else if (Close[0] < midline && Close[1] >= prevMid && Close[0] < Close[1])
                {
                    Values[3][0] = -1;
                    if (ShowSignalArrows)
                        Draw.ArrowDown(this, "HLBDown" + CurrentBar, false, 0, High[0] + 4 * TickSize, Brushes.Red);
                }

                if (midline > prevMid && Close[0] > Close[1])
                {
                    Values[4][0] = 1;
                    if (ShowSignalArrows)
                        Draw.ArrowUp(this, "HLBUp2" + CurrentBar, false, 0, Low[0] - 8 * TickSize, Brushes.Cyan);
                }
                else if (midline < prevMid && Close[0] < Close[1])
                {
                    Values[4][0] = -1;
                    if (ShowSignalArrows)
                        Draw.ArrowDown(this, "HLBDown2" + CurrentBar, false, 0, High[0] + 8 * TickSize, Brushes.Orange);
                }
            }
        }

        // Signal and band series continue calculating; only chart rendering is
        // suppressed when Fast Mode is active.
        public void SetPlaybackFastMode(bool enabled, bool clearExistingArrows)
        {
            ShowSignalArrows = !enabled;

            if (Plots != null && Plots.Length >= 5)
            {
                Plots[0].Brush = enabled ? Brushes.Transparent : Brushes.Lime;
                Plots[1].Brush = enabled ? Brushes.Transparent : Brushes.Red;
                Plots[2].Brush = enabled ? Brushes.Transparent : Brushes.LightGray;
                Plots[3].Brush = Brushes.Transparent;
                Plots[4].Brush = Brushes.Transparent;
            }

            if (enabled && clearExistingArrows)
                RemoveDrawObjects();
        }

        #region Properties
		
//        [NinjaScriptProperty]
//        [Display(Name = "Highest High", Order = 1, GroupName = "Parameters")]
//        public Series<double> Highest High { get; set; }

//        [NinjaScriptProperty]
//        [Display(Name = "Lowest Low", Order = 2, GroupName = "Parameters")]
//        public Series<double> LowestLow { get; set; }

        [Range(1, int.MaxValue), NinjaScriptProperty]
        [Display(Name = "Lookback Period", Description = "Number of bars to look back", Order = 1, GroupName = "Parameters")]
        public int LookbackPeriod { get; set; }
		
        [Range(1, 20), NinjaScriptProperty]
        [Display(Name = "Line Width", Order = 3, GroupName = "Parameters")]
        public int Width { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Show Signal Arrows", Description = "Draw the HiLoBands signal arrows. Disable for high-speed Playback runs; signal values are still calculated.", Order = 4, GroupName = "Parameters")]
        public bool ShowSignalArrows { get; set; }

        [Browsable(false)]
        [XmlIgnore]
        public Series<double> Signal
        {
            get { return Values[3]; }
        }

        [Browsable(false)]
        [XmlIgnore]
        public Series<double> Signal2
        {
            get { return Values[4]; }
        }

        [Browsable(false)]
        [XmlIgnore]
        public Series<double> UpperBand
        {
            get { return Values[0]; }
        }

        [Browsable(false)]
        [XmlIgnore]
        public Series<double> LowerBand
        {
            get { return Values[1]; }
        }

        [Browsable(false)]
        [XmlIgnore]
        public Series<double> MiddleLine
        {
            get { return Values[2]; }
        }
		
        #endregion
    }
}

#region NinjaScript generated code. Neither change nor remove.

namespace NinjaTrader.NinjaScript.Indicators
{
	public partial class Indicator : NinjaTrader.Gui.NinjaScript.IndicatorRenderBase
	{
		private AlgoTrader.HiLoBands[] cacheHiLoBands;
		public AlgoTrader.HiLoBands HiLoBands(int lookbackPeriod, int width, bool showSignalArrows)
		{
			return HiLoBands(Input, lookbackPeriod, width, showSignalArrows);
		}

		public AlgoTrader.HiLoBands HiLoBands(ISeries<double> input, int lookbackPeriod, int width, bool showSignalArrows)
		{
			if (cacheHiLoBands != null)
				for (int idx = 0; idx < cacheHiLoBands.Length; idx++)
					if (cacheHiLoBands[idx] != null && cacheHiLoBands[idx].LookbackPeriod == lookbackPeriod && cacheHiLoBands[idx].Width == width && cacheHiLoBands[idx].ShowSignalArrows == showSignalArrows && cacheHiLoBands[idx].EqualsInput(input))
						return cacheHiLoBands[idx];
			return CacheIndicator<AlgoTrader.HiLoBands>(new AlgoTrader.HiLoBands(){ LookbackPeriod = lookbackPeriod, Width = width, ShowSignalArrows = showSignalArrows }, input, ref cacheHiLoBands);
		}
	}
}

namespace NinjaTrader.NinjaScript.MarketAnalyzerColumns
{
	public partial class MarketAnalyzerColumn : MarketAnalyzerColumnBase
	{
		public Indicators.AlgoTrader.HiLoBands HiLoBands(int lookbackPeriod, int width, bool showSignalArrows)
		{
			return indicator.HiLoBands(Input, lookbackPeriod, width, showSignalArrows);
		}

		public Indicators.AlgoTrader.HiLoBands HiLoBands(ISeries<double> input , int lookbackPeriod, int width, bool showSignalArrows)
		{
			return indicator.HiLoBands(input, lookbackPeriod, width, showSignalArrows);
		}
	}
}

namespace NinjaTrader.NinjaScript.Strategies
{
	public partial class Strategy : NinjaTrader.Gui.NinjaScript.StrategyRenderBase
	{
		public Indicators.AlgoTrader.HiLoBands HiLoBands(int lookbackPeriod, int width, bool showSignalArrows)
		{
			return indicator.HiLoBands(Input, lookbackPeriod, width, showSignalArrows);
		}

		public Indicators.AlgoTrader.HiLoBands HiLoBands(ISeries<double> input , int lookbackPeriod, int width, bool showSignalArrows)
		{
			return indicator.HiLoBands(input, lookbackPeriod, width, showSignalArrows);
		}
	}
}

#endregion
