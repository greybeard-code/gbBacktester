#region Using declarations
using System;
using NinjaTrader.Data;
using NinjaTrader.Gui.Chart;
#endregion

//-----------------------------------------------------------------------------------
//  Wave Bars
//  Open-source bars type — by FlowMatriX
//-----------------------------------------------------------------------------------
//  Directional bars: a bar keeps forming while price stays inside its band and
//  closes when the band breaks. The band is asymmetric, so counter-trend noise
//  is absorbed instead of printing a new bar. New bars open behind the breakout
//  price (phantom open) and closes are Heikin-Ashi smoothed, producing long,
//  overlapping "waves" in trending markets.
//
//  Single input — Wave Size (Ticks), stored in BarsPeriod.Value:
//      trend side of the band ....... size / 2 ticks
//      reversal side of the band .... size * 2 ticks
//      phantom open distance ........ size     ticks
//-----------------------------------------------------------------------------------

namespace NinjaTrader.NinjaScript.BarsTypes
{
	public class WaveBarsType : BarsType
	{
		private double	barMax;
		private double	barMin;
		private int		barDirection;
		private double	openOffset;
		private double	trendOffset;
		private double	reversalOffset;
		private bool	isStateSeeded;

		protected override void OnStateChange()
		{
			if (State == State.SetDefaults)
			{
				Description	= "Directional wave bars with asymmetric trend/reversal thresholds and Heikin-Ashi smoothed closes.";
				Name		= "Wave";
				BarsPeriod	= new BarsPeriod
				{
					BarsPeriodType		= (BarsPeriodType)77077,
					BarsPeriodTypeName	= "Wave",
					Value				= 25
				};
				BuiltFrom			= BarsPeriodType.Tick;
				DefaultChartStyle	= ChartStyleType.CandleStick;
				DaysToLoad			= 5;
				IsIntraday			= true;
				IsTimeBased			= false;
			}
			else if (State == State.Configure)
			{
				Properties.Remove(Properties.Find("BaseBarsPeriodType",		    true));
				Properties.Remove(Properties.Find("BaseBarsPeriodValue",		true));
				Properties.Remove(Properties.Find("PointAndFigurePriceType",	true));
				Properties.Remove(Properties.Find("ReversalType",				true));
				Properties.Remove(Properties.Find("Value2",						true));
				SetPropertyName("Value", "Wave Size");

				Name = string.Format("Wave {0}", BarsPeriod.Value);
			}
		}

		public override void ApplyDefaultBasePeriodValue(BarsPeriod period)
		{
		}

		public override void ApplyDefaultValue(BarsPeriod period)
		{
			period.Value = 25;
		}

		public override string ChartLabel(DateTime dateTime)
		{
			return dateTime.ToString("T", Core.Globals.GeneralOptions.CurrentCulture);
		}

		public override int GetInitialLookBackDays(BarsPeriod barsPeriod, TradingHours tradingHours, int barsBack)
		{
			return 3;
		}

		public override double GetPercentComplete(Bars bars, DateTime now)
		{
			if (bars == null || bars.Count == 0 || trendOffset <= 0 || bars.LastPrice == 0)
				return 0.0;

			double anchor	= barDirection < 0 ? barMin + trendOffset : barMax - trendOffset;
			double up		= barMax - anchor > 0 ? (bars.LastPrice - anchor) / (barMax - anchor) : 0.0;
			double down		= anchor - barMin > 0 ? (anchor - bars.LastPrice) / (anchor - barMin) : 0.0;
			return Math.Min(1.0, Math.Max(0.0, Math.Max(up, down)));
		}

		protected override void OnDataPoint(Bars bars, double open, double high, double low, double close, DateTime time, long volume, bool isBar, double bid, double ask)
		{
			if (SessionIterator == null)
				SessionIterator = new SessionIterator(bars);

			bool isNewSession = SessionIterator.IsNewSession(time, isBar);
			if (isNewSession)
				SessionIterator.GetNextSession(time, isBar);

			if (bars.Count == 0 || (bars.IsResetOnNewTradingDay && isNewSession))
			{
				SeedOffsets(bars);
				barMax = open + trendOffset;
				barMin = open - trendOffset;
				AddBar(bars, open, high, low, GetHeikinAshiClose(open, high, low, close), time, volume);
				isStateSeeded = true;
			}
			else
			{
				if (!isStateSeeded)
				{
					SeedOffsets(bars);
					RecoverBand(bars);
					isStateSeeded = true;
				}

				bool maxExceeded = bars.Instrument.MasterInstrument.Compare(close, barMax) > 0;
				bool minExceeded = bars.Instrument.MasterInstrument.Compare(close, barMin) < 0;

				if (!maxExceeded && !minExceeded)
				{
					int		idx		= bars.Count - 1;
					double	newHigh	= Math.Max(close, bars.GetHigh(idx));
					double	newLow	= Math.Min(close, bars.GetLow(idx));
					UpdateBar(bars, newHigh, newLow, GetHeikinAshiClose(bars.GetOpen(idx), newHigh, newLow, close), time, volume);
				}
				else
				{
					double edge		= maxExceeded ? Math.Min(close, barMax) : Math.Max(close, barMin);
					barDirection	= maxExceeded ? 1 : -1;
					double fakeOpen	= edge - openOffset * barDirection;
					int		idx			= bars.Count - 1;
					double	closingHigh	= maxExceeded ? edge : bars.GetHigh(idx);
					double	closingLow	= minExceeded ? edge : bars.GetLow(idx);
					
					UpdateBar(bars, closingHigh, closingLow, GetHeikinAshiClose(bars.GetOpen(idx), closingHigh, closingLow, edge), time, 0);
					barMax = edge + (barDirection > 0 ? trendOffset : reversalOffset);
					barMin = edge - (barDirection > 0 ? reversalOffset : trendOffset);
					
					double	haOpen	= GetHeikinAshiOpen(fakeOpen, bars.GetClose(idx));
					double	haHigh	= maxExceeded ? edge : fakeOpen;
					double	haLow	= minExceeded ? edge : fakeOpen;
					
					AddBar(bars, haOpen, haHigh, haLow, GetHeikinAshiClose(haOpen, haHigh, haLow, edge), time, volume);
				}
			}

			bars.LastPrice = close;
		}

		private void SeedOffsets(Bars bars)
		{
			double tickSize	= bars.Instrument.MasterInstrument.TickSize;
			int size		= Math.Max(1, bars.BarsPeriod.Value);
			trendOffset		= Math.Max(1, size / 2) * tickSize;
			reversalOffset	= size * 2 * tickSize;
			openOffset		= size * tickSize;
		}

		private void RecoverBand(Bars bars)
		{
			int idx = bars.Count - 1;
			int likelyDirection = bars.Instrument.MasterInstrument.Compare(bars.GetClose(idx), bars.GetOpen(idx)) >= 0 ? 1 : -1;

			if (bars.Count < 2 || (!TrySetBand(bars, idx, likelyDirection) && !TrySetBand(bars, idx, -likelyDirection)))
			{
				barMax = bars.GetOpen(idx) + trendOffset;
				barMin = bars.GetOpen(idx) - trendOffset;
			}
		}

		private bool TrySetBand(Bars bars, int idx, int direction)
		{
			double	fakeOpen	= 2.0 * bars.GetOpen(idx) - bars.GetClose(idx - 1);
			double	edge		= fakeOpen + openOffset * direction;
			double	max			= edge + (direction > 0 ? trendOffset : reversalOffset);
			double	min			= edge - (direction > 0 ? reversalOffset : trendOffset);
			var		instrument	= bars.Instrument.MasterInstrument;
			bool bornAtEdge = direction > 0
				? instrument.Compare(bars.GetHigh(idx), edge) >= 0
				: instrument.Compare(bars.GetLow(idx),  edge) <= 0;
			if (!bornAtEdge || instrument.Compare(bars.GetHigh(idx), max) > 0 || instrument.Compare(bars.GetLow(idx), min) < 0)
				return false;

			barDirection	= direction;
			barMax			= max;
			barMin			= min;
			return true;
		}

		private double GetHeikinAshiOpen(double open, double close)
		{
			return (open + close) * 0.5;
		}

		private double GetHeikinAshiClose(double open, double high, double low, double close)
		{
			return (open + high + low + close) * 0.25;
		}
	}
}
