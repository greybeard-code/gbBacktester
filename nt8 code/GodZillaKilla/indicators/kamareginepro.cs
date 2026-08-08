#region Using declarations
using System;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using System.Xml.Serialization;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
using NinjaTrader.Gui.Tools;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.DrawingTools;
using NinjaTrader.NinjaScript.Indicators;
#endregion

// =====================================================================================
//  KamaRegimePro (KAMA-slope regime)
//  Paints the chart background bull/bear based on the sign of the KAMA slope, drops
//  a ★ marker on the bar the regime flips, and shows a live status card in the corner.
//
//  Single trend source: KAMA slope with a sticky flat band (slope between
//  ±FlatThreshold ticks/bar holds the previous direction so noise-flips don't
//  repaint the background).
//
//  Signal output (RegimeSignal): ±1 on regime commit, backed by a Series<double>
//  (NOT AddPlot). Strategies read it via the public RegimeSignal accessor.
//  Using Series<double> instead of a plot keeps the ±1 values completely off
//  the price-panel auto-scale — no more axis squash on high-priced instruments.
// =====================================================================================

namespace NinjaTrader.NinjaScript.Indicators
{
	public enum KamaRegimeCorner { TopLeft, TopRight, BottomLeft, BottomRight }

	public class KamaRegimePro : Indicator
	{
		// ── sub-indicator ────────────────────────────────────────────────
		private KAMA kamaInd;

		// ── signal series (data-only; NOT AddPlot, so no auto-scale interference) ──
		private Series<double> regimeSignalSeries;

		// ── regime state ─────────────────────────────────────────────────
		private int regimeState         = 0;   // committed: +1 bull, -1 bear, 0 pre-init
		private int lastCommittedRegime = 0;   // for pulse-plot detection
		private int pendingSignal       = 0;   // ConfirmBars debounce buffer
		private int pendingCount        = 0;
		private int barsInRegime        = 0;

		// ── pre-computed constants ────────────────────────────────────────
		private int   _warmupBars = 0;
		private Brush _cachedBull;
		private Brush _cachedBear;

		// ── lifecycle guard ──────────────────────────────────────────────
		// Set true on Terminated. Every dispatcher/timer callback checks it and
		// bails, so nothing runs against a torn-down instance (which is what
		// throws "Non-static method requires a target").
		private volatile bool disposed = false;

		// ── readout state (data thread → UI) ─────────────────────────────
		private string rStatus = "WAITING", rLine = "", rSlope = "", rDelta = "", rBars = "";
		private int    rRegime = 0, prevRegime = -99;

		// ── WPF card ──────────────────────────────────────────────────────
		private Grid            chartGrid;
		private Border          card, statusRow;
		private TextBlock       tbTitle, tbSub, tbInstr, tbStatus, tbLine, tbSlope, tbDelta, tbBars;
		private DispatcherTimer flashTimer;
		private bool            injected, absolutePlaced;
		private DateTime        lastUiUpdate   = DateTime.MinValue;
		private DateTime        lastChangeTime = DateTime.MinValue;
		private const int       UiThrottleMs = 150;
		private const double    FlashHz      = 2.5;
		private bool            dragging;
		private Point           dragStart;
		private Thickness       dragOrigMargin;

		private static Brush Frozen(Color c){ var b = new SolidColorBrush(c); b.Freeze(); return b; }
		private static readonly Brush CardBg        = Frozen(Color.FromArgb(235, 20, 22, 28));
		private static readonly Brush TextDim       = Frozen(Color.FromRgb(150, 150, 156));
		private static readonly Brush RowBorder     = Frozen(Color.FromRgb(120, 120, 126));
		private static readonly Brush RowText       = Frozen(Color.FromRgb(214, 214, 218));
		private static readonly Brush RowFill       = Frozen(Color.FromArgb(28, 255, 255, 255));
		private static readonly Brush NeutralAccent = Frozen(Color.FromRgb(120, 120, 126));

		protected override void OnStateChange()
		{
			if (State == State.SetDefaults)
			{
				Name                     = "KamaRegimePro";
				Description              = "KAMA-slope trend regime: color-change background + ★ marker on regime flips + live readout card.";
				Calculate                = Calculate.OnBarClose;
				IsOverlay                = true;
				DisplayInDataBox         = true;
				// MUST be false while hosting a live WPF card + DispatcherTimer.
				// If true, NT can suspend/null the instance while the timer and
				// dispatcher callbacks keep firing → reflection invokes an instance
				// method on a dead target → "Non-static method requires a target".
				IsSuspendedWhileInactive = false;

				// ── KAMA (single trend source) ─────────────────────────────
				// Kaufman-standard KAMA(fast=2, period=10, slow=30). NinjaTrader's
				// KAMA signature is KAMA(fast, period, slow) — the efficiency-ratio
				// period sits in the middle slot, not first.
				KamaFast          = 2;
				KamaPeriod        = 10;
				KamaSlow          = 30;
				// Flat band (ticks/bar): slope inside ±this holds the last direction.
				// 0.5 filters noise-flips without missing real turns on most 5m futures.
				KamaFlatThreshold = 0.5;

				// ── LOGIC ─────────────────────────────────────────────────
				// ConfirmBars = N means a raw signal must persist for N bars before
				// it commits. 1 = commit immediately (no debounce).
				ConfirmBars        = 1;
				// If true the signal series only emits a value on the FIRST bar of a
				// new regime (a clean flip signal for strategies); if false it emits
				// the regime value every bar.
				SignalOnChangeOnly = true;

				// ── VISUAL ────────────────────────────────────────────────
				BullishColor      = Brushes.Turquoise;
				BearishColor      = Brushes.LightCoral;
				BackgroundOpacity = 60;
				ShowReadout       = true;

				// ── ★ marker on the color-change bar ──────────────────────
				ShowStar        = true;
				StarSize        = 18;   // font size of the ★ glyph
				StarOffsetTicks = 2;    // ticks above bar high (bear) or below bar low (bull)

				// ── Card layout ──────────────────────────────────────────
				CardCorner     = KamaRegimeCorner.TopRight;
				CardMarginX    = 12;
				CardMarginY    = 12;
				CardWidth      = 210;
				ShowInstrument = true;
				FlashSeconds   = 4.0;
				StartLabelBars = 3;    // show "…START" for the first N bars, then "…TREND"
				CardFont       = new SimpleFont("Segoe UI", 16) { Bold = true };

				// NB: intentionally NO AddPlot here. Using AddPlot would add ±1
				// values to the price-panel auto-scale and squash the bars.
				// The signal is exposed via a Series<double> (see DataLoaded).
			}
			else if (State == State.DataLoaded)
			{
				// NB: NT's KAMA signature is KAMA(fast, period, slow) — the
				// efficiency-ratio period is the MIDDLE arg. Passing them out of
				// order will land the fast constant in Period, which NT rejects
				// (Period must be ≥ 5).
				disposed = false;

				kamaInd = KAMA(KamaFast, KamaPeriod, KamaSlow);
				_warmupBars = KamaPeriod + KamaSlow + 5;

				// Data-only signal series — invisible on the chart, readable by
				// strategies via the RegimeSignal accessor.
				regimeSignalSeries = new Series<double>(this);

				regimeState = 0; pendingSignal = 0; pendingCount = 0; barsInRegime = 0;
				lastCommittedRegime = 0;
				prevRegime = -99;

				_cachedBull = BuildOpacityBrush(BullishColor, BackgroundOpacity);
				_cachedBear = BuildOpacityBrush(BearishColor, BackgroundOpacity);
			}
			else if (State == State.Historical)
			{
				if (ShowReadout && ChartControl != null)
					ChartControl.Dispatcher.InvokeAsync(() => TryInjectCard());
			}
			else if (State == State.Terminated)
			{
				disposed = true;   // stop all timer/dispatcher callbacks first
				if (ChartControl != null)
					ChartControl.Dispatcher.InvokeAsync(() => TryRemoveCard());
			}
		}

		protected override void OnBarUpdate()
		{
			if (CurrentBar < _warmupBars)
			{
				if (regimeSignalSeries != null) regimeSignalSeries.Reset();
				return;
			}

			// 1. KAMA slope in ticks/bar
			double kamaNow  = kamaInd[0];
			double kamaPrev = kamaInd[1];
			double slopeTk  = (kamaNow - kamaPrev) / TickSize;

			// 2. Raw signal — sticky flat band. Slope above +threshold commits
			//    LONG, below -threshold commits SHORT, in-between holds the last
			//    direction so KAMA hovering near flat doesn't flip repeatedly.
			int rawSignal;
			if (regimeState == 0)
				rawSignal = slopeTk >= 0 ? 1 : -1;   // first commit
			else
			{
				rawSignal = regimeState;
				if      (slopeTk >  KamaFlatThreshold) rawSignal =  1;
				else if (slopeTk < -KamaFlatThreshold) rawSignal = -1;
				// otherwise: hold last direction (sticky)
			}

			// 3. ConfirmBars debounce — a raw signal must persist for N bars
			//    before it commits to regimeState.
			if (rawSignal == pendingSignal) pendingCount++;
			else { pendingSignal = rawSignal; pendingCount = 1; }

			if (rawSignal == regimeState) barsInRegime++;
			else if (pendingCount >= Math.Max(1, ConfirmBars))
			{
				regimeState  = rawSignal;
				barsInRegime = 1;
			}

			// 4. Signal series output (data-only; nothing renders on the price panel).
			//    SignalOnChangeOnly=true → value only on the flip bar, Reset() (gap)
			//    on interior bars.
			bool regimeChanged = regimeState != lastCommittedRegime;
			if (SignalOnChangeOnly)
			{
				if (regimeChanged && regimeState != 0) regimeSignalSeries[0] = regimeState;
				else                                    regimeSignalSeries.Reset();
			}
			else
				regimeSignalSeries[0] = regimeState;

			lastCommittedRegime = regimeState;

			// 5. Background paint
			BackBrush = regimeState ==  1 ? _cachedBull
			          : regimeState == -1 ? _cachedBear
			          : null;

			// 6. ★ marker on the color-change bar (Draw.Text; doesn't affect scale)
			if (ShowStar && regimeChanged && regimeState != 0)
			{
				Brush  starBrush = regimeState == 1 ? BullishColor : BearishColor;
				double y = regimeState == 1
				         ? Low[0]  - StarOffsetTicks * TickSize   // long start: below the bar
				         : High[0] + StarOffsetTicks * TickSize;  // short start: above the bar
				SimpleFont starFont = new SimpleFont("Segoe UI Symbol", Math.Max(6, StarSize)) { Bold = true };
				Draw.Text(this, "star" + CurrentBar, false, "\u2605", 0, y, 0,
				          starBrush, starFont, TextAlignment.Center,
				          Brushes.Transparent, Brushes.Transparent, 0);
			}

			// 7. Readout state — snapshot for the UI thread. rStatus flips between
			//    "…START" (first StartLabelBars bars of a regime) and "…TREND"
			//    (after that) so a fresh flip is visually distinct from a mature one.
			rRegime = regimeState;
			bool freshRegime = barsInRegime <= Math.Max(1, StartLabelBars);
			rStatus = regimeState ==  1 ? (freshRegime ? "BULL START" : "BULL TREND")
			        : regimeState == -1 ? (freshRegime ? "BEAR START" : "BEAR TREND")
			        : "NEUTRAL";
			rLine   = "line "  + FormatPx(kamaNow);
			rSlope  = "slope " + slopeTk.ToString("+0.0;-0.0;0.0") + " t/bar"
			        + (slopeTk > 0 ? " \u2191" : slopeTk < 0 ? " \u2193" : "");
			rDelta  = "close " + FormatPx(Close[0]) + "  \u00B7  "
			        + ((Close[0] - kamaNow) / TickSize).ToString("+0;-0;0") + "t";
			rBars   = "in regime: " + barsInRegime;

			if (regimeState != prevRegime) { lastChangeTime = DateTime.UtcNow; prevRegime = regimeState; }

			if (ShowReadout) UpdateCard(false);
		}

		private string FormatPx(double p)
		{
			if (Instrument != null && Instrument.MasterInstrument != null)
				return Instrument.MasterInstrument.FormatPrice(p);
			return p.ToString("F2");
		}

		private Brush BuildOpacityBrush(Brush source, int opacityPct)
		{
			SolidColorBrush scb = source as SolidColorBrush;
			if (scb == null) return source;
			Color c = scb.Color;
			var b = new SolidColorBrush(Color.FromArgb((byte)(255 * opacityPct / 100), c.R, c.G, c.B));
			b.Freeze();
			return b;
		}

		private static Brush FrozenSolid(Brush src)
		{
			var s = src as SolidColorBrush;
			if (s == null) return NeutralAccent;
			var b = new SolidColorBrush(s.Color); b.Freeze(); return b;
		}

		private static Brush Tint(Brush src, byte alpha)
		{
			var s = src as SolidColorBrush;
			if (s == null) return RowFill;
			var b = new SolidColorBrush(Color.FromArgb(alpha, s.Color.R, s.Color.G, s.Color.B));
			b.Freeze(); return b;
		}

		// =====================================================================
		//  WPF card
		// =====================================================================
		private void TryInjectCard()
		{
			try
			{
				if (injected || ChartControl == null) return;
				chartGrid = ChartControl.Parent as Grid;
				if (chartGrid == null)
				{
					DependencyObject p = ChartControl.Parent;
					while (p != null && !(p is Grid))
						p = LogicalTreeHelper.GetParent(p) ?? VisualTreeHelper.GetParent(p);
					chartGrid = p as Grid;
				}
				if (chartGrid == null) return;

				card = BuildCard();
				ApplyCornerPlacement();
				Grid.SetRowSpan(card,    Math.Max(1, chartGrid.RowDefinitions.Count));
				Grid.SetColumnSpan(card, Math.Max(1, chartGrid.ColumnDefinitions.Count));
				System.Windows.Controls.Panel.SetZIndex(card, 1000);
				chartGrid.Children.Add(card);
				injected = true;

				flashTimer = new DispatcherTimer(TimeSpan.FromMilliseconds(66), DispatcherPriority.Render,
				                                  (s, e) =>
				                                  {
				                                      if (disposed || card == null)
				                                      {
				                                          if (flashTimer != null) { flashTimer.Stop(); flashTimer = null; }
				                                          return;
				                                      }
				                                      UpdateFlash();
				                                  }, ChartControl.Dispatcher);
				flashTimer.Start();
				UpdateCard(true);
			}
			catch (Exception ex) { Print(Name + ": inject error " + ex.Message); }
		}

		private void TryRemoveCard()
		{
			try
			{
				if (flashTimer != null) { flashTimer.Stop(); flashTimer = null; }
				if (card != null && chartGrid != null && chartGrid.Children.Contains(card))
					chartGrid.Children.Remove(card);
				card = null; chartGrid = null; injected = false; absolutePlaced = false;
			}
			catch (Exception ex) { Print(Name + ": remove error " + ex.Message); }
		}

		private Border BuildCard()
		{
			SimpleFont sf = CardFont ?? new SimpleFont("Segoe UI", 16) { Bold = true };
			var fam = sf.Family ?? new FontFamily("Segoe UI");
			var stack = new StackPanel();

			tbTitle = new TextBlock { Text = "KAMA REGIME", Foreground = Brushes.White,
				FontFamily = fam, FontSize = 12, FontWeight = FontWeights.Bold,
				HorizontalAlignment = HorizontalAlignment.Center };
			stack.Children.Add(tbTitle);

			tbSub = new TextBlock { Text = "trend regime detector", Foreground = TextDim,
				FontFamily = fam, FontSize = 10, HorizontalAlignment = HorizontalAlignment.Center,
				Margin = new Thickness(0, 0, 0, 4) };
			stack.Children.Add(tbSub);

			tbInstr = new TextBlock { Text = "Instrument: " + (Instrument != null ? Instrument.MasterInstrument.Name : "\u2014"),
				Foreground = TextDim, FontFamily = fam, FontSize = 11,
				HorizontalAlignment = HorizontalAlignment.Center, Margin = new Thickness(0, 0, 0, 6) };
			tbInstr.Visibility = ShowInstrument ? Visibility.Visible : Visibility.Collapsed;
			stack.Children.Add(tbInstr);

			tbStatus  = MakeRowText("WAITING", fam, (float)sf.Size, FontWeights.Bold);
			statusRow = MakeRow(tbStatus, RowFill, RowBorder);
			stack.Children.Add(statusRow);

			tbLine  = MakeRowText("\u2014", fam, 11f, FontWeights.SemiBold); stack.Children.Add(MakeRow(tbLine,  RowFill, RowBorder));
			tbSlope = MakeRowText("\u2014", fam, 11f, FontWeights.SemiBold); stack.Children.Add(MakeRow(tbSlope, RowFill, RowBorder));
			tbDelta = MakeRowText("\u2014", fam, 11f, FontWeights.SemiBold); stack.Children.Add(MakeRow(tbDelta, RowFill, RowBorder));

			tbBars = MakeRowText("\u2014", fam, 10.5f, FontWeights.Normal);
			tbBars.Foreground = TextDim;
			stack.Children.Add(tbBars);

			var b = new Border
			{
				Width = Math.Max(170, CardWidth), Background = CardBg, BorderBrush = RowBorder,
				BorderThickness = new Thickness(1.5), CornerRadius = new CornerRadius(10),
				Padding = new Thickness(10, 8, 10, 8), SnapsToDevicePixels = true, UseLayoutRounding = true,
				Cursor = Cursors.SizeAll, ToolTip = "Drag to move", Child = stack
			};
			b.MouseLeftButtonDown += OnCardDown;
			b.MouseMove           += OnCardMove;
			b.MouseLeftButtonUp   += OnCardUp;
			return b;
		}

		private static TextBlock MakeRowText(string text, FontFamily fam, float size, FontWeight weight)
		{
			return new TextBlock { Text = text, Foreground = RowText, FontFamily = fam, FontSize = size,
				FontWeight = weight, HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center };
		}

		private static Border MakeRow(TextBlock content, Brush fill, Brush border)
		{
			return new Border { Background = fill, BorderBrush = border, BorderThickness = new Thickness(1.4),
				CornerRadius = new CornerRadius(6), Padding = new Thickness(6, 3, 6, 3),
				Margin = new Thickness(0, 3, 0, 3), Child = content };
		}

		private void ApplyCornerPlacement()
		{
			if (card == null) return;
			bool left = CardCorner == KamaRegimeCorner.TopLeft || CardCorner == KamaRegimeCorner.BottomLeft;
			bool top  = CardCorner == KamaRegimeCorner.TopLeft || CardCorner == KamaRegimeCorner.TopRight;
			card.HorizontalAlignment = left ? HorizontalAlignment.Left : HorizontalAlignment.Right;
			card.VerticalAlignment   = top  ? VerticalAlignment.Top    : VerticalAlignment.Bottom;
			card.Margin = new Thickness(left ? CardMarginX : 0, top ? CardMarginY : 0,
			                            left ? 0 : CardMarginX, top ? 0 : CardMarginY);
			absolutePlaced = false;
		}

		private void EnsureAbsolutePlacement()
		{
			if (absolutePlaced || card == null || chartGrid == null) return;
			double left = card.HorizontalAlignment == HorizontalAlignment.Left
			            ? card.Margin.Left : chartGrid.ActualWidth  - card.ActualWidth  - card.Margin.Right;
			double top  = card.VerticalAlignment == VerticalAlignment.Top
			            ? card.Margin.Top  : chartGrid.ActualHeight - card.ActualHeight - card.Margin.Bottom;
			card.HorizontalAlignment = HorizontalAlignment.Left;
			card.VerticalAlignment   = VerticalAlignment.Top;
			card.Margin = new Thickness(Math.Max(0, left), Math.Max(0, top), 0, 0);
			absolutePlaced = true;
		}

		private void OnCardDown(object s, MouseButtonEventArgs e)
		{
			if (chartGrid == null) return;
			EnsureAbsolutePlacement();
			dragging = true; dragStart = e.GetPosition(chartGrid); dragOrigMargin = card.Margin;
			card.CaptureMouse(); e.Handled = true;
		}

		private void OnCardMove(object s, MouseEventArgs e)
		{
			if (!dragging) return;
			Point p = e.GetPosition(chartGrid);
			double nl = dragOrigMargin.Left + (p.X - dragStart.X);
			double nt = dragOrigMargin.Top  + (p.Y - dragStart.Y);
			double maxL = Math.Max(0, chartGrid.ActualWidth  - card.ActualWidth);
			double maxT = Math.Max(0, chartGrid.ActualHeight - card.ActualHeight);
			card.Margin = new Thickness(Math.Min(Math.Max(0, nl), maxL), Math.Min(Math.Max(0, nt), maxT), 0, 0);
			e.Handled = true;
		}

		private void OnCardUp(object s, MouseButtonEventArgs e)
		{
			if (!dragging) return;
			dragging = false; card.ReleaseMouseCapture(); e.Handled = true;
		}

		private void UpdateCard(bool force)
		{
			if (disposed || !injected || card == null || ChartControl == null) return;
			if (!force && (DateTime.UtcNow - lastUiUpdate).TotalMilliseconds < UiThrottleMs) return;
			lastUiUpdate = DateTime.UtcNow;

			string status = rStatus;
			string line = rLine, slope = rSlope, delta = rDelta, bars = rBars;
			Brush accent = rRegime ==  1 ? FrozenSolid(BullishColor)
			             : rRegime == -1 ? FrozenSolid(BearishColor)
			             : NeutralAccent;

			try
			{
				ChartControl.Dispatcher.InvokeAsync(() =>
				{
					try
					{
						if (disposed || card == null) return;
						tbStatus.Text = status; tbStatus.Foreground = accent;
						statusRow.BorderBrush = accent; statusRow.Background = Tint(accent, 34);
						card.BorderBrush = accent;
						tbLine.Text  = line;
						tbSlope.Text = slope;
						tbDelta.Text = delta;
						tbBars.Text  = bars;
					}
					catch { }
				});
			}
			catch { }
		}

		private void UpdateFlash()
		{
			try
			{
				if (disposed || card == null || FlashSeconds <= 0 || lastChangeTime == DateTime.MinValue) return;
				double t = (DateTime.UtcNow - lastChangeTime).TotalSeconds;
				if (t >= 0 && t < FlashSeconds)
				{
					double pulse = 0.5 + 0.5 * Math.Sin(t * 2.0 * Math.PI * FlashHz);
					double fade  = 1.0 - t / FlashSeconds;
					card.BorderThickness = new Thickness(1.5 + 2.0 * pulse * fade);
					card.Opacity = 0.85 + 0.15 * pulse;
				}
				else if (card.BorderThickness.Left > 1.5)
				{
					card.BorderThickness = new Thickness(1.5);
					card.Opacity = 1.0;
				}
			}
			catch { }
		}

		#region Properties

		// ── Trend (KAMA slope) — plain [Display], not in factory ─────────
		[Range(1, 30)]
		[Display(Name="KAMA fast constant", GroupName="Trend (KAMA)", Order=1)]
		public int KamaFast { get; set; }

		[Range(5, 200)]
		[Display(Name="KAMA period", GroupName="Trend (KAMA)", Order=2)]
		public int KamaPeriod { get; set; }

		[Range(2, 200)]
		[Display(Name="KAMA slow constant", GroupName="Trend (KAMA)", Order=3)]
		public int KamaSlow { get; set; }

		[Range(0.0, 20.0)]
		[Display(Name="Flat band (ticks/bar; hold direction inside)", GroupName="Trend (KAMA)", Order=4)]
		public double KamaFlatThreshold { get; set; }

		// ── Logic ─────────────────────────────────────────────────────
		[Range(1, int.MaxValue)]
		[Display(Name="Confirm Bars", GroupName="Logic", Order=1)]
		public int ConfirmBars { get; set; }

		[Display(Name="Signal On Change Only (emit only on regime flip)", GroupName="Logic", Order=2)]
		public bool SignalOnChangeOnly { get; set; }

		// ── Visual — in factory ────────────────────────────────────────
		[NinjaScriptProperty]
		[Display(Name="Bullish Color", GroupName="Visual", Order=1)]
		public Brush BullishColor { get; set; }

		[NinjaScriptProperty]
		[Display(Name="Bearish Color", GroupName="Visual", Order=2)]
		public Brush BearishColor { get; set; }

		[NinjaScriptProperty] [Range(0, 100)]
		[Display(Name="Background Opacity %", GroupName="Visual", Order=3)]
		public int BackgroundOpacity { get; set; }

		[NinjaScriptProperty]
		[Display(Name="Show Readout Card", GroupName="Visual", Order=4)]
		public bool ShowReadout { get; set; }

		// ── ★ marker on the color-change bar — plain [Display] ────────
		[Display(Name="Show Star On Color Change", GroupName="Visual", Order=5)]
		public bool ShowStar { get; set; }

		[Range(6, 72)]
		[Display(Name="Star Size", GroupName="Visual", Order=6)]
		public int StarSize { get; set; }

		[Range(0, 100)]
		[Display(Name="Star Offset (ticks)", GroupName="Visual", Order=7)]
		public int StarOffsetTicks { get; set; }

		// ── Card layout — plain [Display] ─────────────────────────────
		[Display(Name="Card Corner", GroupName="Readout Card", Order=1)]
		public KamaRegimeCorner CardCorner { get; set; }

		[Range(0, 400)] [Display(Name="Corner Margin X", GroupName="Readout Card", Order=2)]
		public int CardMarginX { get; set; }

		[Range(0, 400)] [Display(Name="Corner Margin Y", GroupName="Readout Card", Order=3)]
		public int CardMarginY { get; set; }

		[Range(170, 400)] [Display(Name="Card Width", GroupName="Readout Card", Order=4)]
		public int CardWidth { get; set; }

		[Display(Name="Show Instrument", GroupName="Readout Card", Order=5)]
		public bool ShowInstrument { get; set; }

		[Range(0, 60)] [Display(Name="Flash Seconds (0 = off)", GroupName="Readout Card", Order=6)]
		public double FlashSeconds { get; set; }

		[Range(1, int.MaxValue)]
		[Display(Name="Start Label Bars (…START → …TREND)", GroupName="Readout Card", Order=7)]
		public int StartLabelBars { get; set; }

		[Display(Name="Card Font", GroupName="Readout Card", Order=8)]
		public SimpleFont CardFont { get; set; }

		#endregion

		#region Accessors
		[Browsable(false)] [XmlIgnore]
		public Series<double> RegimeSignal { get { return regimeSignalSeries; } }

		[Browsable(false)] [XmlIgnore]
		public Series<double> Regime { get { return regimeSignalSeries; } }   // legacy alias
		#endregion
	}
}

#region NinjaScript generated code. Neither change nor remove.

namespace NinjaTrader.NinjaScript.Indicators
{
	public partial class Indicator : NinjaTrader.Gui.NinjaScript.IndicatorRenderBase
	{
		private KamaRegimePro[] cacheKamaRegimePro;
		public KamaRegimePro KamaRegimePro(Brush bullishColor, Brush bearishColor, int backgroundOpacity, bool showReadout)
		{
			return KamaRegimePro(Input, bullishColor, bearishColor, backgroundOpacity, showReadout);
		}

		public KamaRegimePro KamaRegimePro(ISeries<double> input, Brush bullishColor, Brush bearishColor, int backgroundOpacity, bool showReadout)
		{
			if (cacheKamaRegimePro != null)
				for (int idx = 0; idx < cacheKamaRegimePro.Length; idx++)
					if (cacheKamaRegimePro[idx] != null && cacheKamaRegimePro[idx].BullishColor == bullishColor && cacheKamaRegimePro[idx].BearishColor == bearishColor && cacheKamaRegimePro[idx].BackgroundOpacity == backgroundOpacity && cacheKamaRegimePro[idx].ShowReadout == showReadout && cacheKamaRegimePro[idx].EqualsInput(input))
						return cacheKamaRegimePro[idx];
			return CacheIndicator<KamaRegimePro>(new KamaRegimePro(){ BullishColor = bullishColor, BearishColor = bearishColor, BackgroundOpacity = backgroundOpacity, ShowReadout = showReadout }, input, ref cacheKamaRegimePro);
		}
	}
}

namespace NinjaTrader.NinjaScript.MarketAnalyzerColumns
{
	public partial class MarketAnalyzerColumn : MarketAnalyzerColumnBase
	{
		public Indicators.KamaRegimePro KamaRegimePro(Brush bullishColor, Brush bearishColor, int backgroundOpacity, bool showReadout)
		{
			return indicator.KamaRegimePro(Input, bullishColor, bearishColor, backgroundOpacity, showReadout);
		}

		public Indicators.KamaRegimePro KamaRegimePro(ISeries<double> input , Brush bullishColor, Brush bearishColor, int backgroundOpacity, bool showReadout)
		{
			return indicator.KamaRegimePro(input, bullishColor, bearishColor, backgroundOpacity, showReadout);
		}
	}
}

namespace NinjaTrader.NinjaScript.Strategies
{
	public partial class Strategy : NinjaTrader.Gui.NinjaScript.StrategyRenderBase
	{
		public Indicators.KamaRegimePro KamaRegimePro(Brush bullishColor, Brush bearishColor, int backgroundOpacity, bool showReadout)
		{
			return indicator.KamaRegimePro(Input, bullishColor, bearishColor, backgroundOpacity, showReadout);
		}

		public Indicators.KamaRegimePro KamaRegimePro(ISeries<double> input , Brush bullishColor, Brush bearishColor, int backgroundOpacity, bool showReadout)
		{
			return indicator.KamaRegimePro(input, bullishColor, bearishColor, backgroundOpacity, showReadout);
		}
	}
}

#endregion
