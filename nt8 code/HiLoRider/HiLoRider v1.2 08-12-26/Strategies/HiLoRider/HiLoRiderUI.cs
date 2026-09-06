// file name = HiLoRiderUI.cs
// HiLoRider — WPF ChartTrader panel.
// Layout (top-down):
//   Always visible: Header, Regime Badge, Strategy Control, Manual Trading,
//                   Stop Management
//   Collapsible:    Entry Window, Stop Mode, Target Mode,
//                   Risk Parameters, Trail Mode, Scale-Out, Auto Exit, ML
//   Always visible: Trade Report + Backtest Report buttons

#region Using declarations
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using NinjaTrader.Cbi;
using NinjaTrader.Gui.Chart;
using NinjaTrader.NinjaScript.Strategies;
#endregion

namespace NinjaTrader.NinjaScript.Strategies.HiLoRider
{
    public partial class HiLoRider : Strategy
    {
        // ── Chart references ──────────────────────────────────────────────────
        private Chart        _ctChart;
        private Grid         _ctTraderGrid;
        private ScrollViewer _ctScrollViewer;
        private RowDefinition _ctPanelRow;
        private bool         _ctPanelActive = false;
        private StackPanel   hudStack;
        private StackPanel   _strategyPanelBody;
        private bool         uiPanelActive = false;
        private DispatcherTimer _livePanelRefreshTimer;
        private volatile bool _livePanelRefreshDirty;
        private static readonly TimeSpan LivePanelRefreshInterval = TimeSpan.FromMilliseconds(250);
        private static readonly object UiBrushCacheLock = new object();
        private static readonly Dictionary<Color, SolidColorBrush> UiBrushCache =
            new Dictionary<Color, SolidColorBrush>();

        private static SolidColorBrush UiBrush(Color color)
        {
            lock (UiBrushCacheLock)
            {
                SolidColorBrush brush;
                if (UiBrushCache.TryGetValue(color, out brush)) return brush;
                brush = new SolidColorBrush(color);
                brush.Freeze();
                UiBrushCache[color] = brush;
                return brush;
            }
        }

        // ── Always-visible controls ───────────────────────────────────────────
        private Button    _btnStrategy, _btnLong, _btnShort;
        private Button    btnBuy, btnSell;
        private Button    btnAdd1, btnClose1, btnClose;
        private Button    btnBE, btnMoveTS, btnMoveTS50;
        private Button    btnOrderTypeMkt, btnOrderTypeLmt;
        private TextBox   tbContracts;
        private TextBox   tbLimitOffset;

        // ── Entry confirm dialog ──────────────────────────────────────────────
        private Border          entryWarningPanel;
        private TextBlock       entryWarningLine1, entryWarningLine2;
        private Button          entryConfirmBtn;
        private enum            PendingEntryDir { None, Long, Short }
        private PendingEntryDir pendingEntryDir = PendingEntryDir.None;
        private DispatcherTimer entryWarningTimer;

        // ── Regime badge ──────────────────────────────────────────────────────
        private Border    regimeBadgeBorder;
        private TextBlock regimeBadgeValue, regimeBadgeSub;
        private TextBlock regimeBadgeBar1, regimeBadgeBar0, regimeBadgeLongest;

        // ── Live gauge TextBlocks ─────────────────────────────────────────────
        private TextBlock liveBuyTb, liveSellTb;

        // ── SESSION P&L card TextBlocks ───────────────────────────────────────
        private TextBlock livePnlNavTb, livePnlRealizedTb, livePnlUnrealizedTb;
        private TextBlock livePnlTotalTb, livePnlRecordTb;

        // ── Stop mode section ─────────────────────────────────────────────────
        private RadioButton rbStopReversal, rbStopFixed, rbStopATR, rbStopHL;
        private UIElement   _stopReversalRow, _stopFixedRow, _stopATRRow, _stopHLRow;
        private TextBox     tbStopBuf, tbFixedSL, tbSLATR, tbSwingBuf;

        // ── Target mode section ───────────────────────────────────────────────
        private RadioButton rbTargetNo, rbTargetFixed, rbTargetATR, rbTargetRR;
        private UIElement   _targetFixedRow, _targetATRRow, _targetRRRow;
        private TextBlock   _targetNote;
        private TextBox     tbTP, tbTPATR, tbRRTarget;

        // ── Trail mode section ────────────────────────────────────────────────
        private RadioButton rbTrailHL, rbTrailStaged, rbTrailATR, rbTrailFST, rbTrailFixed, rbTrailMidline;
        private UIElement   _trailHL1Row, _trailHL2Row, _trailMinTicksRow, _trailHL2TrigRow, _trailHL3TrigRow, _trailHL3TrailRow;
        private UIElement   _trailSt1Row, _trailSt2PctRow, _trailSt2Row, _trailSt3PctRow, _trailSt3Row;
        private UIElement   _trailA1Row, _trailA2Row, _trailA3Row, _trailA4Row, _trailA4PctRow;
        private UIElement   _fst1TrigRow, _fst1OffRow, _fst2TrigRow, _fst2LockRow;
        private UIElement   _fst3TrigRow, _fst3LockRow, _fst4TrigRow, _fst4TrailRow;
        private UIElement   _trailMidBarsRow, _trailMidOffRow;
        private TextBlock   _trailNote;
        private TextBox     tbHLSt1Lb, tbHLSt2Lb, tbTrailMin, tbHLSt2Trig, tbHLSt3Trig, tbHLSt3Trail;
        private TextBox     tbSt1Act, tbSt2Pct, tbSt2Trail, tbSt3Pct, tbSt3Trail;
        private TextBox     tbATR1, tbATR2, tbATR3, tbATR4, tbATR4Pct;
        private TextBox     tbFst1Trig, tbFst1Off, tbFst2Trig, tbFst2Lock;
        private TextBox     tbFst3Trig, tbFst3Lock, tbFst4Trig, tbFst4Trail;
        private TextBox     tbTrailMidBars, tbTrailMidOff;

        // ── Scale-out section ─────────────────────────────────────────────────
        private Button _btnSOOff;
        private Button _btnSOBars;
        private Button _btnSOTicks;
        private Border _soBarsBorder;
        private Border _soTicksBorder;

        // ── Entry filter checkboxes ───────────────────────────────────────────

        // ── Auto exit radio ───────────────────────────────────────────────────
        private RadioButton rbAEDiffCross, rbAEDisabled;

        // ── ML section ────────────────────────────────────────────────────────
        private Button    btnMLToggle, btnMLTrainer;
        private TextBlock liveMLStatusTb, liveMLProbTb;

        // ── Dashboard buttons ─────────────────────────────────────────────────
        private Button btnTradeReport, btnBacktestReport;
        private Button btnTradeLogToggle;
        private Button btnEconCal;

        // ── Collapsible wrappers ──────────────────────────────────────────────
        private Border _entryWrapper, _filtersWrapper, _stopModeWrapper;
        private Border _targetModeWrapper, _riskWrapper, _trailWrapper;
        private Border _autoExWrapper, _mlWrapper;
        private Border _sizeWrapper;

        // ── Risk spinners ─────────────────────────────────────────────────────
        private TextBox tbBETrig, tbBEOff, tbLossLim, tbProfLim;
        private Button btnEnableLossLimit, btnEnableProfitLimit;

        // ── Misc ──────────────────────────────────────────────────────────────
        internal bool useMarketOrder         = false;  // default Limit (matches TrendMaster, Jul 2026)
        private  bool uiBusy                 = false;
        private  bool _strategyLockedByManualTrade = false;

        // ── Colour palette ────────────────────────────────────────────────────
        private static readonly Color C_BG     = Color.FromRgb(13,  15,  20);
        private static readonly Color C_CARD   = Color.FromRgb(19,  22,  30);
        private static readonly Color C_BORDER = Color.FromRgb(30,  35,  48);
        private static readonly Color C_DIM    = Color.FromRgb(40,  45,  58);
        private static readonly Color C_MUTED  = Color.FromRgb(90,  96, 112);
        private static readonly Color C_TEXT   = Color.FromRgb(232, 234, 240);
        private static readonly Color C_GREEN  = Color.FromRgb(0,   212, 160);
        private static readonly Color C_RED    = Color.FromRgb(255,  77, 106);
        private static readonly Color C_AMBER  = Color.FromRgb(245, 158,  11);
        private static readonly Color C_BLUE   = Color.FromRgb(59,  130, 246);
        private static readonly Color C_CYAN   = Color.FromRgb(40,  210, 230);
        private static readonly Color C_PURPLE = Color.FromRgb(167, 105, 255);

        // =====================================================================
        protected void CreateWPFControls()
        {
            try
            {
                if (uiPanelActive) return;
                _ctChart = Window.GetWindow(ChartControl.Parent) as Chart;
                if (_ctChart == null) return;
                ChartTrader ct = _ctChart.FindFirst("ChartWindowChartTraderControl") as ChartTrader;
                if (ct?.Content == null) return;
                _ctTraderGrid = ct.Content as Grid;
                if (_ctTraderGrid == null) return;

                hudStack = new StackPanel
                {
                    Orientation = Orientation.Vertical,
                    Background  = new SolidColorBrush(C_BG),
                    MinWidth    = 290,
                };

                BuildHeader();

                // Keep the lightweight title bar alive in Fast Mode while
                // collapsing all original strategy controls below it.
                StackPanel panelRoot = hudStack;
                _strategyPanelBody = new StackPanel
                {
                    Orientation = Orientation.Vertical,
                    Background  = new SolidColorBrush(C_BG),
                };
                hudStack = _strategyPanelBody;

                BuildDivider("SIGNAL STATE");
                BuildRegimeBadge();
                BuildDivider("SESSION P&L");
                BuildSessionPnLCard();
                BuildDivider("STRATEGY CONTROL");
                BuildStrategyControlSection();
                BuildDivider("MANUAL TRADING");
                BuildManualTradingSection();
                BuildDivider("STOP MANAGEMENT");
                BuildStopManagementSection();

                BuildCollapsibleDivider("ENTRY WINDOW",      out _entryWrapper,      startExpanded: false);
                BuildEntryWindowSection();
                BuildCollapsibleDivider("ENTRY FILTERS",     out _filtersWrapper,    startExpanded: false);
                BuildEntryFiltersSection();
                BuildCollapsibleDivider("STOP MODE",         out _stopModeWrapper,   startExpanded: false);
                BuildStopModeSection();
                BuildCollapsibleDivider("TARGET MODE",       out _targetModeWrapper, startExpanded: false);
                BuildTargetModeSection();
                BuildCollapsibleDivider("RISK PARAMETERS",   out _riskWrapper,       startExpanded: false);
                BuildRiskSection();
                BuildCollapsibleDivider("TRAIL MODE",        out _trailWrapper,      startExpanded: false);
                BuildTrailModeSection();
                BuildDivider("SCALE-OUT");
                BuildScaleOutSection();
                BuildCollapsibleDivider("AUTO EXIT MODE",    out _autoExWrapper,     startExpanded: false);
                BuildAutoExitSection();
                BuildCollapsibleDivider("MACHINE LEARNING",  out _mlWrapper,         startExpanded: true);
                BuildMLSection();
                BuildCollapsibleDivider("POSITION SIZE",     out _sizeWrapper,       startExpanded: false);
                BuildPositionSizeSection();

                BuildDivider("DASHBOARD");
                BuildDashboardSection();

                hudStack = panelRoot;
                hudStack.Children.Add(_strategyPanelBody);

                _ctScrollViewer = new ScrollViewer
                {
                    Content                       = hudStack,
                    VerticalScrollBarVisibility   = ScrollBarVisibility.Visible,
                    HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
                    MaxHeight                     = 600,
                    MinHeight                     = 0,
                    PanningMode                   = PanningMode.VerticalOnly,
                    IsManipulationEnabled         = true,
                    Background                    = new SolidColorBrush(C_BG),
                };
                _ctScrollViewer.PreviewMouseWheel += OnPanelPreviewMouseWheel;

                InsertPanel();
                _livePanelRefreshTimer = new DispatcherTimer
                {
                    Interval = LivePanelRefreshInterval
                };
                _livePanelRefreshTimer.Tick += OnLivePanelRefreshTimerTick;
                _livePanelRefreshDirty = true;
                _ctChart.MainTabControl.SelectionChanged += OnCTTabChanged;
                uiPanelActive = true;
                ApplyPanelVisibilityAndMode();
            }
            catch (Exception ex) { Print($"[HiLoRiderUI] CreateWPFControls: {ex.Message}"); }
        }

        protected void DisposeWPFControls()
        {
            try
            {
                entryWarningTimer?.Stop();
                if (_livePanelRefreshTimer != null)
                {
                    _livePanelRefreshTimer.Stop();
                    _livePanelRefreshTimer.Tick -= OnLivePanelRefreshTimerTick;
                    _livePanelRefreshTimer = null;
                }
                _livePanelRefreshDirty = false;
                if (_ctChart != null) _ctChart.MainTabControl.SelectionChanged -= OnCTTabChanged;
                if (_ctScrollViewer != null)
                {
                    _ctScrollViewer.PreviewMouseWheel -= OnPanelPreviewMouseWheel;
                    _ctScrollViewer.Content = null;
                }
                RemovePanel();
                _ctScrollViewer = null; _ctTraderGrid = null; _ctChart = null;
                _strategyPanelBody = null;
                _btnPlaybackFastMode = null;
                _btnShowTextHud = null;
                hudStack = null;
                uiPanelActive = false;
            }
            catch { }
        }

        // ── Header ────────────────────────────────────────────────────────────
        private void BuildHeader()
        {
            var g = new Grid { Margin = new Thickness(10, 10, 10, 8) };
            g.ColumnDefinitions.Add(new ColumnDefinition());
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var left = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center };
            var dot  = new System.Windows.Shapes.Ellipse
            {
                Width = 8, Height = 8, Fill = new SolidColorBrush(C_RED),
                VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 7, 0),
            };
            left.Children.Add(dot);
            left.Children.Add(Tx("HI LO RIDER", 14, C_TEXT, bold: true));
            left.Children.Add(Tx("  v1.2 / safety 3.3.15", 10, C_MUTED));
            var chip = new Border
            {
                Background = new SolidColorBrush(C_DIM), CornerRadius = new CornerRadius(4),
                Padding = new Thickness(6, 3, 6, 3), Margin = new Thickness(0, 0, 6, 0),
                Child = Tx("MNQ 120T", 10, C_MUTED),
            };

            _btnPlaybackFastMode = new Button
            {
                FontSize        = 10,
                FontWeight      = FontWeights.Bold,
                MinWidth        = 58,
                Padding         = new Thickness(7, 3, 7, 3),
                BorderThickness = new Thickness(1),
                Cursor          = Cursors.Hand,
            };
            _btnPlaybackFastMode.Click += (s, e) =>
                SetPlaybackFastModeFromUi(!PlaybackFastMode);
            RefreshPlaybackFastModeButtonStyle();

            Grid.SetColumn(left, 0);
            Grid.SetColumn(chip, 1);
            Grid.SetColumn(_btnPlaybackFastMode, 2);
            g.Children.Add(left);
            g.Children.Add(chip);
            g.Children.Add(_btnPlaybackFastMode);
            hudStack.Children.Add(g);
            hudStack.Children.Add(HRule());
        }

        // ── Regime badge ──────────────────────────────────────────────────────
        private void BuildRegimeBadge()
        {
            regimeBadgeBorder = new Border
            {
                Background = new SolidColorBrush(C_CARD), BorderBrush = new SolidColorBrush(C_RED),
                BorderThickness = new Thickness(3, 1, 1, 1), CornerRadius = new CornerRadius(6),
                Padding = new Thickness(10, 8, 10, 8), Margin = new Thickness(6, 2, 6, 2),
            };
            var sp = new StackPanel();
            sp.Children.Add(Tx("SIGNAL STATE", 8, C_MUTED));
            regimeBadgeValue   = Tx("DETECTING...", 15, C_RED, bold: true);
            regimeBadgeSub     = Tx("Initializing...", 9, C_MUTED);
            regimeBadgeBar1    = Tx("Bar[1]: ---", 9, C_MUTED);
            regimeBadgeBar0    = Tx("Bar[0]: ---", 9, C_MUTED);
            regimeBadgeLongest = Tx("Longest: ---", 9, C_MUTED);
            sp.Children.Add(regimeBadgeValue);
            sp.Children.Add(regimeBadgeSub);
            sp.Children.Add(regimeBadgeBar1);
            sp.Children.Add(regimeBadgeBar0);
            sp.Children.Add(regimeBadgeLongest);
            // GEX regime readout — merged into this card to save panel space
            AddGEXRows(sp);
            regimeBadgeBorder.Child = sp;
            hudStack.Children.Add(regimeBadgeBorder);

            // BUY / SELL readiness chips
            var readyCard = Card();
            readyCard.Margin = new Thickness(6, 2, 6, 2);
            var rg = new Grid();
            rg.ColumnDefinitions.Add(new ColumnDefinition());
            rg.ColumnDefinitions.Add(new ColumnDefinition());
            var buySp  = ReadyChipCell("BUY",  out liveBuyTb);
            var sellSp = ReadyChipCell("SELL", out liveSellTb);
            Grid.SetColumn(buySp, 0); Grid.SetColumn(sellSp, 1);
            rg.Children.Add(buySp); rg.Children.Add(sellSp);
            readyCard.Child = rg;
            hudStack.Children.Add(readyCard);
        }

        // ── Strategy control ──────────────────────────────────────────────────
        private void BuildStrategyControlSection()
        {
            _btnStrategy = new Button
            {
                FontSize = 13, FontWeight = FontWeights.Bold, Height = 36,
                Margin = new Thickness(0, 0, 2, 0), BorderThickness = new Thickness(2), Cursor = Cursors.Hand,
            };
            _btnStrategy.Click += (s, e) => SetStrategyEnabled(!strategyEnabled);

            _btnShowTextHud = new Button
            {
                FontSize        = 13,
                FontWeight      = FontWeights.Bold,
                Height          = 36,
                Margin          = new Thickness(2, 0, 0, 0),
                BorderThickness = new Thickness(1),
                Cursor          = Cursors.Hand,
            };
            _btnShowTextHud.Click += (s, e) =>
            {
                try
                {
                    if (PlaybackFastMode) return;
                    if (_textHudWindow == null)
                    {
                        EnableTextHud = true;
                        BuildTextHudWindow();
                    }
                    else
                    {
                        EnableTextHud = false;
                        TearDownTextHudWindow(removeButton: false);
                    }
                }
                catch (Exception ex)
                {
                    try { Print($"[HiLoRider] HUD toggle click: {ex.Message}"); } catch { }
                }
            };

            var strategyRow = new Grid { Margin = new Thickness(6, 4, 6, 3) };
            strategyRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            strategyRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            Grid.SetColumn(_btnStrategy, 0);
            Grid.SetColumn(_btnShowTextHud, 1);
            strategyRow.Children.Add(_btnStrategy);
            strategyRow.Children.Add(_btnShowTextHud);
            hudStack.Children.Add(strategyRow);

            var dirRow = new Grid { Margin = new Thickness(6, 0, 6, 4) };
            dirRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            dirRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            _btnLong = new Button { Height = 32, FontSize = 12, FontWeight = FontWeights.Bold,
                Margin = new Thickness(0, 0, 2, 0), Cursor = Cursors.Hand };
            _btnShort = new Button { Height = 32, FontSize = 12, FontWeight = FontWeights.Bold,
                Margin = new Thickness(2, 0, 0, 0), Cursor = Cursors.Hand };
            _btnLong.Click  += (s, e) => SetLongEnabled(!longEnabled);
            _btnShort.Click += (s, e) => SetShortEnabled(!shortEnabled);
            Grid.SetColumn(_btnLong, 0); Grid.SetColumn(_btnShort, 1);
            dirRow.Children.Add(_btnLong); dirRow.Children.Add(_btnShort);
            hudStack.Children.Add(dirRow);

            RefreshTextHudToggleButtonStyle();
            UpdateStrategyControlButtons();
        }

        // ── Manual trading ────────────────────────────────────────────────────
        private void BuildManualTradingSection()
        {
            // MKT / LMT toggle
            var otRow = new Grid { Margin = new Thickness(6, 6, 6, 2) };
            otRow.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            otRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            otRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            otRow.Children.Add(new TextBlock { Text = "ENTRY TYPE", Foreground = new SolidColorBrush(C_MUTED),
                FontSize = 10, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 8, 0) });
            btnOrderTypeMkt = new Button { Content = "MKT", Height = 28, FontSize = 11,
                FontWeight = FontWeights.Bold, Margin = new Thickness(0, 0, 2, 0), Cursor = Cursors.Hand,
                Background = new SolidColorBrush(Color.FromArgb(80, 245, 158, 11)),
                Foreground = new SolidColorBrush(C_AMBER),
                BorderBrush = new SolidColorBrush(C_AMBER), BorderThickness = new Thickness(2) };
            btnOrderTypeLmt = new Button { Content = "LMT", Height = 28, FontSize = 11,
                FontWeight = FontWeights.Bold, Margin = new Thickness(2, 0, 0, 0), Cursor = Cursors.Hand,
                Background = new SolidColorBrush(C_DIM), Foreground = new SolidColorBrush(C_MUTED),
                BorderBrush = new SolidColorBrush(C_BORDER), BorderThickness = new Thickness(1) };
            btnOrderTypeMkt.Click += (s, e) => { useMarketOrder = true;  RefreshOrderTypeButtons(); RefreshBuySellLabels(); };
            btnOrderTypeLmt.Click += (s, e) => { useMarketOrder = false; RefreshOrderTypeButtons(); RefreshBuySellLabels(); };
            Grid.SetColumn(btnOrderTypeMkt, 1); Grid.SetColumn(btnOrderTypeLmt, 2);
            otRow.Children.Add(btnOrderTypeMkt); otRow.Children.Add(btnOrderTypeLmt);
            hudStack.Children.Add(otRow);

            // Contracts spinner
            hudStack.Children.Add(FullWidthSpinner("Contracts", Contracts.ToString(), out tbContracts, v => { Contracts = v; }));

            hudStack.Children.Add(FullWidthSpinner("Limit Offset Ticks", LimitOffsetTicks.ToString(), out tbLimitOffset, v => { LimitOffsetTicks = v; }));

            // BUY / SELL
            var bsRow = new Grid { Margin = new Thickness(6, 4, 6, 3) };
            bsRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            bsRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            btnBuy = new Button { Content = "▲  BUY MKT", FontSize = 14, FontWeight = FontWeights.Bold,
                Padding = new Thickness(0, 10, 0, 10), Margin = new Thickness(0, 0, 3, 0),
                Background = new SolidColorBrush(Color.FromArgb(70, 0, 212, 160)),
                Foreground = new SolidColorBrush(C_GREEN), BorderBrush = new SolidColorBrush(C_GREEN),
                BorderThickness = new Thickness(2), Cursor = Cursors.Hand };
            btnSell = new Button { Content = "▼  SELL MKT", FontSize = 14, FontWeight = FontWeights.Bold,
                Padding = new Thickness(0, 10, 0, 10), Margin = new Thickness(3, 0, 0, 0),
                Background = new SolidColorBrush(Color.FromArgb(70, 255, 77, 106)),
                Foreground = new SolidColorBrush(C_RED), BorderBrush = new SolidColorBrush(C_RED),
                BorderThickness = new Thickness(2), Cursor = Cursors.Hand };
            Grid.SetColumn(btnBuy, 0); Grid.SetColumn(btnSell, 1);
            bsRow.Children.Add(btnBuy); bsRow.Children.Add(btnSell);
            hudStack.Children.Add(bsRow);

            // Sync ENTRY TYPE buttons + BUY/SELL labels to the actual default
            // (useMarketOrder) -- without this, the buttons were built with a
            // hardcoded "MKT is active" look regardless of the real default.
            RefreshOrderTypeButtons();
            RefreshBuySellLabels();

            // Entry warning panel
            entryWarningPanel = new Border
            {
                Background = new SolidColorBrush(Color.FromRgb(55, 15, 15)),
                BorderBrush = new SolidColorBrush(C_RED), BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6), Margin = new Thickness(6, 2, 6, 4),
                Visibility = Visibility.Collapsed,
            };
            var warnSp = new StackPanel { Margin = new Thickness(10, 8, 10, 8) };
            entryWarningLine1 = new TextBlock { Text = "⚠  ENTRY WARNING",
                Foreground = new SolidColorBrush(C_RED), FontWeight = FontWeights.Bold,
                FontSize = 11, TextAlignment = TextAlignment.Center, Margin = new Thickness(0, 0, 0, 4) };
            entryWarningLine2 = new TextBlock { Text = "",
                Foreground = new SolidColorBrush(C_AMBER), FontSize = 10,
                TextWrapping = TextWrapping.Wrap, TextAlignment = TextAlignment.Center,
                Margin = new Thickness(0, 0, 0, 8) };
            entryConfirmBtn = new Button { Content = "CONFIRM  →  EXECUTE ANYWAY", Height = 30, FontSize = 11,
                FontWeight = FontWeights.Bold, Background = new SolidColorBrush(Color.FromRgb(100, 25, 25)),
                Foreground = Brushes.White, BorderThickness = new Thickness(0), Cursor = Cursors.Hand };
            entryConfirmBtn.Click += OnEntryConfirmClicked;
            warnSp.Children.Add(entryWarningLine1);
            warnSp.Children.Add(entryWarningLine2);
            warnSp.Children.Add(entryConfirmBtn);
            entryWarningPanel.Child = warnSp;
            hudStack.Children.Add(entryWarningPanel);

            entryWarningTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(10) };
            entryWarningTimer.Tick += (s, e) => DismissEntryWarning();

            btnBuy.Click  += (s, e) => ChartControl.Dispatcher.InvokeAsync(() => HandleBuyClick());
            btnSell.Click += (s, e) => ChartControl.Dispatcher.InvokeAsync(() => HandleSellClick());

            // Add 1 / Close 1 / Close All
            var acRow = new Grid { Margin = new Thickness(6, 0, 6, 4) };
            acRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            acRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            acRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            btnAdd1   = FlatBtn("Add 1",     Color.FromRgb(0,  95, 155));
            btnClose1 = FlatBtn("Close 1",   Color.FromRgb(155, 75,  0));
            btnClose  = FlatBtn("Flatten", Color.FromRgb(60,  60, 60));
            btnAdd1.Click   += (s, e) => ChartControl.Dispatcher.InvokeAsync(() => DoAddOneContract());
            btnClose1.Click += (s, e) => ChartControl.Dispatcher.InvokeAsync(() => DoCloseOneContract());
            btnClose.Click  += (s, e) => ChartControl.Dispatcher.InvokeAsync(() => DoManualClose());
            Grid.SetColumn(btnAdd1, 0); Grid.SetColumn(btnClose1, 1); Grid.SetColumn(btnClose, 2);
            acRow.Children.Add(btnAdd1); acRow.Children.Add(btnClose1); acRow.Children.Add(btnClose);
            hudStack.Children.Add(acRow);
        }

        // ── Stop management ───────────────────────────────────────────────────
        private void BuildStopManagementSection()
        {
            var smRow = new Grid { Margin = new Thickness(6, 4, 6, 3) };
            smRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            smRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            smRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            btnBE       = FlatBtn("Move to BE",     Color.FromRgb(175, 115,  0));
            btnMoveTS50 = FlatBtn("Move Stop 50%",  Color.FromRgb(55, 55, 130));
            btnMoveTS   = FlatBtn("Tighten Stop",   Color.FromRgb(35,   75, 155));
            btnBE.Click       += (s, e) => ChartControl.Dispatcher.InvokeAsync(() => DoBreakeven());
            btnMoveTS50.Click += (s, e) => ChartControl.Dispatcher.InvokeAsync(() => DoMoveTS50Pct());
            btnMoveTS.Click   += (s, e) => ChartControl.Dispatcher.InvokeAsync(() => DoMoveTrailstop());
            Grid.SetColumn(btnBE, 0); Grid.SetColumn(btnMoveTS50, 1); Grid.SetColumn(btnMoveTS, 2);
            smRow.Children.Add(btnBE); smRow.Children.Add(btnMoveTS50); smRow.Children.Add(btnMoveTS);
            hudStack.Children.Add(smRow);
        }

        // ── Entry window (collapsible) ────────────────────────────────────────
        private void BuildEntryWindowSection()
        {
            var card = Card();
            var sp   = new StackPanel();
            sp.Children.Add(FullWidthSpinner("Window Start (HHMM)", EntryWindowStart.ToString(), out TextBox _, v => EntryWindowStart = v));
            sp.Children.Add(FullWidthSpinner("RTH End (HHMM)",      RTHWindowEnd.ToString(),     out TextBox _, v => RTHWindowEnd     = v));
            sp.Children.Add(FullWidthSpinner("Cooldown Bars",        CooldownBars.ToString(),     out TextBox _, v => CooldownBars      = v));
            sp.Children.Add(FullWidthSpinner("Max Daily Trades",     MaxDailyTrades.ToString(),   out TextBox _, v => MaxDailyTrades    = v));
            card.Child = sp;
            AddToCollapsible(_entryWrapper, card);
        }


        // ── Position Size section ─────────────────────────────────────────────
        private void BuildPositionSizeSection()
        {
            var card = Card();
            var sp   = new StackPanel();
            TextBox _sz, _mbTb;
            sp.Children.Add(FullWidthSpinner("Contracts",         Contracts.ToString(),       out _sz,   v => Contracts      = v));
            sp.Children.Add(FullWidthSpinner("Max Bars In Trade", MaxBarsInTrade.ToString(),  out _mbTb, v => MaxBarsInTrade  = v));
            card.Child = sp;
            AddToCollapsible(_sizeWrapper, card);
        }

        // ── Entry filters (collapsible) ───────────────────────────────────────
        private void BuildEntryFiltersSection()
        {
            var card = Card();
            var sp   = new StackPanel();

            // Diff band filter (Aug 2026 — was Properties-dialog-only, never on this panel)
            var cbDiffBand = DarkCheckBox("Diff Band Filter", EnableDiffBandFilter);
            cbDiffBand.Checked   += (s, e) => { EnableDiffBandFilter = true; };
            cbDiffBand.Unchecked += (s, e) => { EnableDiffBandFilter = false; };
            sp.Children.Add(cbDiffBand);
            TextBox tbDiffMin, tbDiffMax, tbBarsToHold;
            sp.Children.Add(FullWidthSpinner("Diff Min %",  DiffMinPercent.ToString(), out tbDiffMin, v => DiffMinPercent = v));
            sp.Children.Add(FullWidthSpinner("Diff Max %",  DiffMaxPercent.ToString(), out tbDiffMax, v => DiffMaxPercent = v));
            sp.Children.Add(FullWidthSpinner("Bars To Hold (chop wait)", BarsToHold.ToString(), out tbBarsToHold, v => BarsToHold = v));

            sp.Children.Add(new Border { Height = 6 });

            // ADX minimum
            var cbADX = DarkCheckBox("ADX ≥ min", EnableADXFilter);
            cbADX.Checked   += (s, e) => { EnableADXFilter = true; };
            cbADX.Unchecked += (s, e) => { EnableADXFilter = false; };
            sp.Children.Add(cbADX);
            TextBox tbAdxMin;
            sp.Children.Add(FullWidthSpinnerD("ADX Minimum", ADXMinimum, out tbAdxMin, v => ADXMinimum = v));

            sp.Children.Add(new Border { Height = 6 });

            // Hour block
            var cbHour = DarkCheckBox("Block bad hours (ET)", EnableHourBlock);
            cbHour.Checked   += (s, e) => { EnableHourBlock = true; };
            cbHour.Unchecked += (s, e) => { EnableHourBlock = false; };
            sp.Children.Add(cbHour);
            TextBox tbHrStart, tbHrEnd;
            sp.Children.Add(FullWidthSpinner("Block Start (ET hr 0-23)", HourBlockStart.ToString(), out tbHrStart, v => HourBlockStart = v));
            sp.Children.Add(FullWidthSpinner("Block End   (ET hr 0-23)", HourBlockEnd.ToString(),   out tbHrEnd,   v => HourBlockEnd   = v));

            card.Child = sp;
            AddToCollapsible(_filtersWrapper, card);
        }

        // ── Stop mode (collapsible) ───────────────────────────────────────────
        private void BuildStopModeSection()
        {
            var card = Card();
            var sp   = new StackPanel();
            sp.Children.Add(Tx("STOP MODE", 9, C_MUTED));

            var row1 = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 4, 0, 2) };
            var row2 = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 0, 0, 8) };
            rbStopReversal = DarkRadio("Channel Band", StopMode == RPStopMode.ChannelBand);
            rbStopFixed    = DarkRadio("Fixed Tick",   StopMode == RPStopMode.FixedTick);
            rbStopATR      = DarkRadio("ATR",          StopMode == RPStopMode.ATR);
            rbStopHL       = DarkRadio("High/Low",     StopMode == RPStopMode.HighLow);
            rbStopReversal.GroupName = rbStopFixed.GroupName =
                rbStopATR.GroupName  = rbStopHL.GroupName = "RPStopMode";
            rbStopReversal.Checked += (s, e) => { if (!uiBusy) { StopMode = RPStopMode.ChannelBand; RefreshStopModeParams(); } };
            rbStopFixed.Checked    += (s, e) => { if (!uiBusy) { StopMode = RPStopMode.FixedTick;   RefreshStopModeParams(); } };
            rbStopATR.Checked      += (s, e) => { if (!uiBusy) { StopMode = RPStopMode.ATR;         RefreshStopModeParams(); } };
            rbStopHL.Checked       += (s, e) => { if (!uiBusy) { StopMode = RPStopMode.HighLow;     RefreshStopModeParams(); } };
            row1.Children.Add(rbStopReversal); row1.Children.Add(rbStopFixed);
            row2.Children.Add(rbStopATR);      row2.Children.Add(rbStopHL);
            sp.Children.Add(row1); sp.Children.Add(row2);

            _stopReversalRow = FullWidthSpinner("Stop Buffer Ticks",    StopBufferTicks.ToString(),          out tbStopBuf,  v => StopBufferTicks      = v);
            _stopFixedRow    = FullWidthSpinner("Fixed SL Ticks",       FixedSLTicks.ToString(),             out tbFixedSL,  v => FixedSLTicks          = v);
            _stopATRRow      = FullWidthSpinnerD("SL ATR Multiplier",   SLATRMultiplier,                     out tbSLATR,    v => SLATRMultiplier       = v);
            _stopHLRow       = FullWidthSpinner("Stop Buffer Ticks",   StopBufferTicks.ToString(),          out tbSwingBuf, v => StopBufferTicks        = v);
            sp.Children.Add(_stopReversalRow);
            sp.Children.Add(_stopFixedRow);
            sp.Children.Add(_stopATRRow);
            sp.Children.Add(_stopHLRow);

            card.Child = sp;
            AddToCollapsible(_stopModeWrapper, card);
            RefreshStopModeParams();
        }

        private void RefreshStopModeParams()
        {
            if (_stopReversalRow == null) return;
            _stopReversalRow.Visibility = StopMode == RPStopMode.ChannelBand ? Visibility.Visible : Visibility.Collapsed;
            _stopFixedRow.Visibility    = StopMode == RPStopMode.FixedTick   ? Visibility.Visible : Visibility.Collapsed;
            _stopATRRow.Visibility      = StopMode == RPStopMode.ATR         ? Visibility.Visible : Visibility.Collapsed;
            _stopHLRow.Visibility       = StopMode == RPStopMode.HighLow     ? Visibility.Visible : Visibility.Collapsed;
        }

        // ── Target mode (collapsible) ─────────────────────────────────────────
        private void BuildTargetModeSection()
        {
            var card = Card();
            var sp   = new StackPanel();
            sp.Children.Add(Tx("TARGET MODE", 9, C_MUTED));

            var row1 = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 4, 0, 2) };
            var row2 = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 0, 0, 8) };
            rbTargetNo    = DarkRadio("No Target",   TargetMode == RPTargetMode.NoTarget);
            rbTargetFixed = DarkRadio("Fixed Ticks", TargetMode == RPTargetMode.FixedTicks);
            rbTargetATR   = DarkRadio("ATR",         TargetMode == RPTargetMode.ATR);
            rbTargetRR    = DarkRadio("Risk:Reward", TargetMode == RPTargetMode.RiskReward);
            rbTargetNo.GroupName = rbTargetFixed.GroupName =
                rbTargetATR.GroupName = rbTargetRR.GroupName = "RPTargetMode";
            rbTargetNo.Checked    += (s, e) => { if (!uiBusy) { TargetMode = RPTargetMode.NoTarget;    TrailMode = RPTrailMode.HighLow; RefreshTargetModeParams(); RefreshTrailModeSection(); } };
            rbTargetFixed.Checked += (s, e) => { if (!uiBusy) { TargetMode = RPTargetMode.FixedTicks;  RefreshTargetModeParams(); } };
            rbTargetATR.Checked   += (s, e) => { if (!uiBusy) { TargetMode = RPTargetMode.ATR;         RefreshTargetModeParams(); } };
            rbTargetRR.Checked    += (s, e) => { if (!uiBusy) { TargetMode = RPTargetMode.RiskReward;  RefreshTargetModeParams(); } };
            row1.Children.Add(rbTargetNo); row1.Children.Add(rbTargetFixed);
            row2.Children.Add(rbTargetATR); row2.Children.Add(rbTargetRR);
            sp.Children.Add(row1); sp.Children.Add(row2);

            _targetFixedRow = FullWidthSpinner("Fixed TP Ticks",  FixedTPTicks.ToString(), out tbTP,       v => FixedTPTicks     = v);
            _targetATRRow   = FullWidthSpinnerD("TP ATR Mult",    TPATRMultiplier,         out tbTPATR,    v => TPATRMultiplier  = v);
            _targetRRRow    = FullWidthSpinnerD("Risk:Reward",    RiskRewardRatio,         out tbRRTarget, v => RiskRewardRatio  = v);
            _targetNote     = Tx("", 9, C_MUTED);
            sp.Children.Add(_targetFixedRow);
            sp.Children.Add(_targetATRRow);
            sp.Children.Add(_targetRRRow);
            sp.Children.Add(_targetNote);

            card.Child = sp;
            AddToCollapsible(_targetModeWrapper, card);
            RefreshTargetModeParams();
        }

        private void RefreshTargetModeParams()
        {
            if (_targetFixedRow == null) return;
            _targetFixedRow.Visibility = TargetMode == RPTargetMode.FixedTicks  ? Visibility.Visible : Visibility.Collapsed;
            _targetATRRow.Visibility   = TargetMode == RPTargetMode.ATR         ? Visibility.Visible : Visibility.Collapsed;
            _targetRRRow.Visibility    = TargetMode == RPTargetMode.RiskReward  ? Visibility.Visible : Visibility.Collapsed;
            string note = TargetMode == RPTargetMode.NoTarget
                ? "No profit target — stop-only bracket riding the High/Low trail." : "";
            _targetNote.Text       = note;
            _targetNote.Visibility = string.IsNullOrEmpty(note) ? Visibility.Collapsed : Visibility.Visible;
        }

        // ── Risk parameters (collapsible) ─────────────────────────────────────
        private void BuildRiskSection()
        {
            var card = Card();
            var sp   = new StackPanel();
            sp.Children.Add(FullWidthSpinner("BE Trigger Ticks",    BE_TriggerTicks.ToString(),        out tbBETrig,  v => BE_TriggerTicks   = v));
            sp.Children.Add(FullWidthSpinner("BE Offset Ticks",     BE_OffsetTicks.ToString(),         out tbBEOff,   v => BE_OffsetTicks    = v));
            sp.Children.Add(BuildLimitToggleRow("Daily Loss Limit $",   () => EnableDailyLossLimit,   (int)DailyLossLimit,
                out btnEnableLossLimit, out tbLossLim, v => EnableDailyLossLimit   = v, v => DailyLossLimit   = v));
            sp.Children.Add(BuildLimitToggleRow("Daily Profit Limit $", () => EnableDailyProfitLimit, (int)DailyProfitLimit,
                out btnEnableProfitLimit, out tbProfLim, v => EnableDailyProfitLimit = v, v => DailyProfitLimit = v));
            card.Child = sp;
            AddToCollapsible(_riskWrapper, card);
        }

        // ── Trail mode (collapsible) ──────────────────────────────────────────
        private void BuildTrailModeSection()
        {
            var card = Card();
            var sp   = new StackPanel();
            sp.Children.Add(Tx("TRAIL MODE", 9, C_MUTED));

            var row1 = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 4, 0, 2) };
            var row2 = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 0, 0, 8) };
            rbTrailHL      = DarkRadio("High/Low",      TrailMode == RPTrailMode.HighLow);
            rbTrailStaged  = DarkRadio("Staged",        TrailMode == RPTrailMode.StagedTrail);
            rbTrailATR     = DarkRadio("ATR",           TrailMode == RPTrailMode.ATR);
            rbTrailFST     = DarkRadio("FST+Trail",     TrailMode == RPTrailMode.FixedStopTrail);
            rbTrailFixed   = DarkRadio("FixedStop",     TrailMode == RPTrailMode.FixedStop);
            rbTrailMidline = DarkRadio("Midline Offset", TrailMode == RPTrailMode.MidlineOffset);
            rbTrailHL.GroupName = rbTrailStaged.GroupName = rbTrailATR.GroupName =
                rbTrailFST.GroupName = rbTrailFixed.GroupName = rbTrailMidline.GroupName = "RPTrailMode";
            rbTrailHL.Checked     += (s, e) => { if (!uiBusy) { TrailMode = RPTrailMode.HighLow;        RefreshTrailModeParams(); } };
            rbTrailStaged.Checked += (s, e) => { if (!uiBusy) { TrailMode = RPTrailMode.StagedTrail;    RefreshTrailModeParams(); } };
            rbTrailATR.Checked    += (s, e) => { if (!uiBusy) { TrailMode = RPTrailMode.ATR;            RefreshTrailModeParams(); } };
            rbTrailFST.Checked    += (s, e) => { if (!uiBusy) { TrailMode = RPTrailMode.FixedStopTrail; RefreshTrailModeParams(); } };
            rbTrailFixed.Checked  += (s, e) => { if (!uiBusy) { TrailMode = RPTrailMode.FixedStop;      RefreshTrailModeParams(); } };
            rbTrailMidline.Checked += (s, e) => { if (!uiBusy) { TrailMode = RPTrailMode.MidlineOffset; RefreshTrailModeParams(); } };
            row1.Children.Add(rbTrailHL);    row1.Children.Add(rbTrailStaged); row1.Children.Add(rbTrailATR);
            row2.Children.Add(rbTrailFST);   row2.Children.Add(rbTrailFixed);  row2.Children.Add(rbTrailMidline);
            sp.Children.Add(row1); sp.Children.Add(row2);

            // HighLow params
            _trailHL1Row    = FullWidthSpinner("HL Stage1 Lookback",    HLStage1LookbackBars.ToString(),   out tbHLSt1Lb,   v => HLStage1LookbackBars  = v);
            _trailHL2Row    = FullWidthSpinner("HL Stage2 Lookback",    HLStage2LookbackBars.ToString(),   out tbHLSt2Lb,   v => HLStage2LookbackBars  = v);
            _trailMinTicksRow = FullWidthSpinner("Trail Min Ticks",     TrailMinTicks.ToString(),          out tbTrailMin,  v => TrailMinTicks         = v);
            _trailHL2TrigRow= FullWidthSpinner("HL Stage2 Trigger Bars", HLStage2TriggerBars.ToString(),   out tbHLSt2Trig, v => HLStage2TriggerBars  = v);
            _trailHL3TrigRow  = FullWidthSpinner("HL Stage3 Trigger Bars", HLStage3TriggerBars.ToString(),   out tbHLSt3Trig,  v => HLStage3TriggerBars  = v);
            _trailHL3TrailRow = FullWidthSpinner("HL Stage3 Trail Ticks", HLStage3TrailTicks.ToString(),     out tbHLSt3Trail, v => HLStage3TrailTicks    = v);
            // Staged params
            _trailSt1Row    = FullWidthSpinner("Stage1 Activation Ticks",Stage1ActivationTicks.ToString(), out tbSt1Act,   v => Stage1ActivationTicks  = v);
            _trailSt2PctRow = FullWidthSpinner("Stage2 Trigger %",       Stage2TriggerPct.ToString(),      out tbSt2Pct,   v => Stage2TriggerPct       = v);
            _trailSt2Row    = FullWidthSpinner("Stage2 Trail Ticks",     Stage2TrailTicks.ToString(),      out tbSt2Trail, v => Stage2TrailTicks        = v);
            _trailSt3PctRow = FullWidthSpinner("Stage3 Trigger %",       Stage3TriggerPct.ToString(),      out tbSt3Pct,   v => Stage3TriggerPct       = v);
            _trailSt3Row    = FullWidthSpinner("Stage3 Trail Ticks",     Stage3TrailTicks.ToString(),      out tbSt3Trail, v => Stage3TrailTicks        = v);
            // ATR params
            _trailA1Row     = FullWidthSpinnerD("ATR Stage1 Mult", ATRTrailStage1Mult, out tbATR1, v => ATRTrailStage1Mult = v);
            _trailA2Row     = FullWidthSpinnerD("ATR Stage2 Mult", ATRTrailStage2Mult, out tbATR2, v => ATRTrailStage2Mult = v);
            _trailA3Row     = FullWidthSpinnerD("ATR Stage3 Mult", ATRTrailStage3Mult, out tbATR3, v => ATRTrailStage3Mult = v);
            _trailA4Row     = FullWidthSpinnerD("ATR Stage4 Mult", ATRTrailStage4Mult, out tbATR4, v => ATRTrailStage4Mult = v);
            _trailA4PctRow  = FullWidthSpinner("ATR Stage4 Trig %", ATRTrailStage4TriggerPct.ToString(), out tbATR4Pct, v => ATRTrailStage4TriggerPct = v);
            // FST params
            _fst1TrigRow = FullWidthSpinner("FST Stage1 Trig %",    FST_Stage1TriggerPct.ToString(),  out tbFst1Trig,  v => FST_Stage1TriggerPct  = v);
            _fst1OffRow  = FullWidthSpinner("FST Stage1 BE Offset",  FST_Stage1OffsetTicks.ToString(), out tbFst1Off,   v => FST_Stage1OffsetTicks = v);
            _fst2TrigRow = FullWidthSpinner("FST Stage2 Trig %",    FST_Stage2TriggerPct.ToString(),  out tbFst2Trig,  v => FST_Stage2TriggerPct  = v);
            _fst2LockRow = FullWidthSpinner("FST Stage2 Lock %",    FST_Stage2LockPct.ToString(),     out tbFst2Lock,  v => FST_Stage2LockPct     = v);
            _fst3TrigRow = FullWidthSpinner("FST Stage3 Trig %",    FST_Stage3TriggerPct.ToString(),  out tbFst3Trig,  v => FST_Stage3TriggerPct  = v);
            _fst3LockRow = FullWidthSpinner("FST Stage3 Lock %",    FST_Stage3LockPct.ToString(),     out tbFst3Lock,  v => FST_Stage3LockPct     = v);
            _fst4TrigRow = FullWidthSpinner("FST Stage4 Trig %",    FST_Stage4TriggerPct.ToString(),  out tbFst4Trig,  v => FST_Stage4TriggerPct  = v);
            _fst4TrailRow= FullWidthSpinner("FST Stage4 Trail Ticks",FST_Stage4TrailTicks.ToString(), out tbFst4Trail, v => FST_Stage4TrailTicks  = v);
            // Midline Offset params (this bot's own genesis-validated trail)
            _trailMidBarsRow = FullWidthSpinner("Trail Bars Before Trail", TrailBarsBeforeTrail.ToString(), out tbTrailMidBars, v => TrailBarsBeforeTrail = v);
            _trailMidOffRow  = FullWidthSpinner("Trail Offset Ticks",      TrailOffsetTicks.ToString(),     out tbTrailMidOff,  v => TrailOffsetTicks     = v);
            _trailNote   = Tx("", 9, C_MUTED);

            foreach (var row in new[] {
                _trailHL1Row, _trailHL2Row, _trailMinTicksRow, _trailHL2TrigRow, _trailHL3TrigRow, _trailHL3TrailRow,
                _trailSt1Row, _trailSt2PctRow, _trailSt2Row, _trailSt3PctRow, _trailSt3Row,
                _trailA1Row, _trailA2Row, _trailA3Row, _trailA4Row, _trailA4PctRow,
                _fst1TrigRow, _fst1OffRow, _fst2TrigRow, _fst2LockRow,
                _fst3TrigRow, _fst3LockRow, _fst4TrigRow, _fst4TrailRow,
                _trailMidBarsRow, _trailMidOffRow,
            }) sp.Children.Add(row);
            sp.Children.Add(_trailNote);

            card.Child = sp;
            AddToCollapsible(_trailWrapper, card);
            RefreshTrailModeParams();
        }

        private void RefreshTrailModeSection()
        {
            if (rbTrailHL == null) return;
            uiBusy = true;
            rbTrailHL.IsChecked      = TrailMode == RPTrailMode.HighLow;
            rbTrailStaged.IsChecked  = TrailMode == RPTrailMode.StagedTrail;
            rbTrailATR.IsChecked     = TrailMode == RPTrailMode.ATR;
            rbTrailFST.IsChecked     = TrailMode == RPTrailMode.FixedStopTrail;
            rbTrailFixed.IsChecked   = TrailMode == RPTrailMode.FixedStop;
            rbTrailMidline.IsChecked = TrailMode == RPTrailMode.MidlineOffset;
            uiBusy = false;
            RefreshTrailModeParams();
        }

        private void RefreshTrailModeParams()
        {
            if (_trailHL1Row == null) return;
            var hl     = TrailMode == RPTrailMode.HighLow        ? Visibility.Visible : Visibility.Collapsed;
            var staged = TrailMode == RPTrailMode.StagedTrail     ? Visibility.Visible : Visibility.Collapsed;
            var atr    = TrailMode == RPTrailMode.ATR             ? Visibility.Visible : Visibility.Collapsed;
            var fst    = TrailMode == RPTrailMode.FixedStopTrail  ? Visibility.Visible : Visibility.Collapsed;
            var mid    = TrailMode == RPTrailMode.MidlineOffset   ? Visibility.Visible : Visibility.Collapsed;

            _trailHL1Row.Visibility = _trailHL2Row.Visibility =
                _trailMinTicksRow.Visibility =
                _trailHL2TrigRow.Visibility = _trailHL3TrigRow.Visibility = _trailHL3TrailRow.Visibility = hl;
            _trailSt1Row.Visibility = _trailSt2PctRow.Visibility = _trailSt2Row.Visibility =
                _trailSt3PctRow.Visibility = _trailSt3Row.Visibility = staged;
            _trailA1Row.Visibility  = _trailA2Row.Visibility  = _trailA3Row.Visibility  =
                _trailA4Row.Visibility = _trailA4PctRow.Visibility = atr;
            _fst1TrigRow.Visibility = _fst1OffRow.Visibility  = _fst2TrigRow.Visibility =
                _fst2LockRow.Visibility = _fst3TrigRow.Visibility = _fst3LockRow.Visibility =
                _fst4TrigRow.Visibility = _fst4TrailRow.Visibility = fst;
            _trailMidBarsRow.Visibility = _trailMidOffRow.Visibility = mid;

            string note = TrailMode == RPTrailMode.FixedStop ? "Fixed stop — no trailing after breakeven." : "";
            _trailNote.Text       = note;
            _trailNote.Visibility = string.IsNullOrEmpty(note) ? Visibility.Collapsed : Visibility.Visible;
        }

        // ── Scale-out (always-visible toggle + collapsing param panel) ──────────
        private void BuildScaleOutSection()
        {
            var card = Card();
            var sp   = new StackPanel();
            var hdrRow = new Grid { Margin = new Thickness(0, 0, 0, 4) };
            hdrRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            hdrRow.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var lbl = Tx("SCALE-OUT", 9, C_MUTED);
            lbl.VerticalAlignment = VerticalAlignment.Center;
            Grid.SetColumn(lbl, 0); hdrRow.Children.Add(lbl);
            var btnSeg = new StackPanel { Orientation = Orientation.Horizontal };
            _btnSOOff   = MkSOMode("OFF");
            _btnSOBars  = MkSOMode("BARS");
            _btnSOTicks = MkSOMode("TICKS");
            btnSeg.Children.Add(_btnSOOff);
            btnSeg.Children.Add(_btnSOBars);
            btnSeg.Children.Add(_btnSOTicks);
            Grid.SetColumn(btnSeg, 1); hdrRow.Children.Add(btnSeg);
            sp.Children.Add(hdrRow);
            _soBarsBorder = new Border { Margin = new Thickness(0, 2, 0, 0) };
            var barsSp = new StackPanel();
            barsSp.Children.Add(Tx("Stage 1", 9, C_CYAN, bold: true));
            barsSp.Children.Add(FullWidthSpinner("Bars in trade", ScaleOut1Bars.ToString(), out TextBox tbSOB1,  v => ScaleOut1Bars = v));
            barsSp.Children.Add(FullWidthSpinner("Qty",           ScaleOut1Qty.ToString(),  out TextBox tbSOBQ1, v => ScaleOut1Qty  = v));
            barsSp.Children.Add(HRule(new Thickness(0, 6, 0, 6)));
            barsSp.Children.Add(Tx("Stage 2  (fires after Stage 1)", 9, C_AMBER, bold: true));
            barsSp.Children.Add(FullWidthSpinner("Bars in trade", ScaleOut2Bars.ToString(), out TextBox tbSOB2,  v => ScaleOut2Bars = v));
            barsSp.Children.Add(FullWidthSpinner("Qty",           ScaleOut2Qty.ToString(),  out TextBox tbSOBQ2, v => ScaleOut2Qty  = v));
            _soBarsBorder.Child = barsSp;
            sp.Children.Add(_soBarsBorder);
            _soTicksBorder = new Border { Margin = new Thickness(0, 2, 0, 0) };
            var ticksSp = new StackPanel();
            ticksSp.Children.Add(Tx("Level 1", 9, C_CYAN, bold: true));
            ticksSp.Children.Add(FullWidthSpinner("SO1 Ticks", ScaleOutTicks1.ToString(), out TextBox tbSOT1,  v => ScaleOutTicks1 = v));
            ticksSp.Children.Add(FullWidthSpinner("Qty",       ScaleOut1Qty.ToString(),   out TextBox tbSOTQ1, v => ScaleOut1Qty   = v));
            ticksSp.Children.Add(HRule(new Thickness(0, 6, 0, 6)));
            ticksSp.Children.Add(Tx("Level 2  (fires after Level 1)", 9, C_AMBER, bold: true));
            ticksSp.Children.Add(FullWidthSpinner("SO2 Ticks", ScaleOutTicks2.ToString(), out TextBox tbSOT2,  v => ScaleOutTicks2 = v));
            ticksSp.Children.Add(FullWidthSpinner("Qty",       ScaleOut2Qty.ToString(),   out TextBox tbSOTQ2, v => ScaleOut2Qty   = v));
            _soTicksBorder.Child = ticksSp;
            sp.Children.Add(_soTicksBorder);
            card.Child = sp;
            hudStack.Children.Add(card);
            bool startTicks = ScaleOutMode == ScaleOutMode.Ticks;
            bool startBars  = !startTicks && ScaleOutMode == ScaleOutMode.Bars;
            ApplySOMode(startTicks ? 2 : (startBars ? 1 : 0));
            _btnSOOff.Click   += (s, e) => { ScaleOutMode = ScaleOutMode.Disabled; ApplySOMode(0); };
            _btnSOBars.Click  += (s, e) => { ScaleOutMode = ScaleOutMode.Bars;     ApplySOMode(1); };
            _btnSOTicks.Click += (s, e) => { ScaleOutMode = ScaleOutMode.Ticks;    ApplySOMode(2); };
        }

        private Button MkSOMode(string label) => new Button
        {
            Content = label, Height = 22, FontSize = 9, Padding = new Thickness(6, 0, 6, 0),
            Background      = new SolidColorBrush(Color.FromArgb(20, 90, 96, 112)),
            Foreground      = new SolidColorBrush(C_MUTED),
            BorderBrush     = new SolidColorBrush(C_BORDER),
            BorderThickness = new Thickness(1), Cursor = Cursors.Hand, Margin = new Thickness(1, 0, 0, 0),
        };

        private void ApplySOMode(int mode)
        {
            if (_btnSOOff   != null) { bool a = mode == 0; _btnSOOff.Background   = new SolidColorBrush(a ? Color.FromArgb(50, 90, 96, 112) : Color.FromArgb(20, 90, 96, 112)); _btnSOOff.Foreground   = new SolidColorBrush(a ? C_TEXT : C_MUTED); _btnSOOff.BorderBrush   = new SolidColorBrush(a ? C_MUTED : C_BORDER); }
            if (_btnSOBars  != null) { bool a = mode == 1; _btnSOBars.Background  = new SolidColorBrush(a ? Color.FromArgb(60, 40, 210, 230) : Color.FromArgb(20, 90, 96, 112)); _btnSOBars.Foreground  = new SolidColorBrush(a ? C_CYAN  : C_MUTED); _btnSOBars.BorderBrush  = new SolidColorBrush(a ? C_CYAN  : C_BORDER); }
            if (_btnSOTicks != null) { bool a = mode == 2; _btnSOTicks.Background = new SolidColorBrush(a ? Color.FromArgb(60, 40, 210, 230) : Color.FromArgb(20, 90, 96, 112)); _btnSOTicks.Foreground = new SolidColorBrush(a ? C_CYAN  : C_MUTED); _btnSOTicks.BorderBrush = new SolidColorBrush(a ? C_CYAN  : C_BORDER); }
            if (_soBarsBorder  != null) _soBarsBorder.Visibility  = mode == 1 ? Visibility.Visible : Visibility.Collapsed;
            if (_soTicksBorder != null) _soTicksBorder.Visibility = mode == 2 ? Visibility.Visible : Visibility.Collapsed;
        }

        // ── Auto exit (collapsible) ───────────────────────────────────────────
        private void BuildAutoExitSection()
        {
            var card = Card();
            var sp   = new StackPanel();
            sp.Children.Add(Tx("EXIT TRIGGER", 9, C_MUTED));
            sp.Children.Add(Tx("DiffCross — exit when PriceMomentum Diff turns against trade.", 9, C_CYAN));
            sp.Children.Add(Tx("Disabled  — no auto-exit (manual close only).", 9, C_MUTED));
            sp.Children.Add(HRule(new Thickness(0, 4, 0, 6)));

            var aeRow = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 2, 0, 4) };
            rbAEDiffCross  = DarkRadio("DiffCross",  AutoExitMode == HiLoRiderAutoExit.DiffCross);
            rbAEDisabled   = DarkRadio("Disabled",   AutoExitMode == HiLoRiderAutoExit.Disabled);
            rbAEDiffCross.GroupName = rbAEDisabled.GroupName = "HiLoRiderAutoExit";
            rbAEDiffCross.Checked  += (s, e) => AutoExitMode = HiLoRiderAutoExit.DiffCross;
            rbAEDisabled.Checked   += (s, e) => AutoExitMode = HiLoRiderAutoExit.Disabled;
            aeRow.Children.Add(rbAEDiffCross);
            aeRow.Children.Add(rbAEDisabled);
            sp.Children.Add(aeRow);
            card.Child = sp;
            AddToCollapsible(_autoExWrapper, card);
        }

        // ── Machine learning (collapsible, starts expanded) ───────────────────
        private void BuildMLSection()
        {
            var card = Card();
            var sp   = new StackPanel();
            var mlBtnRow = new Grid { Margin = new Thickness(0, 0, 0, 6) };
            mlBtnRow.ColumnDefinitions.Add(new ColumnDefinition());
            mlBtnRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(4) });
            mlBtnRow.ColumnDefinitions.Add(new ColumnDefinition());
            btnMLTrainer = new Button
            {
                Content = "ML Trainer", FontSize = 12, FontWeight = FontWeights.Bold, Height = 32,
                Cursor = Cursors.Hand,
                Background  = new SolidColorBrush(Color.FromRgb(0, 40, 80)),
                Foreground  = new SolidColorBrush(Color.FromRgb(80, 160, 255)),
                BorderBrush = new SolidColorBrush(Color.FromRgb(80, 160, 255)),
            };
            btnMLTrainer.Click += (s, e) => DoRunMLTrainer("HiLoRider_trades.jsonl", "HiLoRiderLogs", "HiLoRiderTrainer.py", "HiLoRider_ML_Report.html", btnMLTrainer);
            btnMLToggle = new Button
            {
                Content = EnableML ? "ML FILTER  ON" : "ML FILTER  OFF",
                FontSize = 12, FontWeight = FontWeights.Bold, Height = 32,
                Cursor = Cursors.Hand,
                Background  = new SolidColorBrush(EnableML ? Color.FromRgb(0, 60, 40) : Color.FromRgb(50, 20, 20)),
                Foreground  = new SolidColorBrush(EnableML ? C_GREEN : C_MUTED),
                BorderBrush = new SolidColorBrush(EnableML ? C_GREEN : C_MUTED),
            };
            btnMLToggle.Click += (s, e) => { EnableML = !EnableML; RefreshMLSection(); };
            Grid.SetColumn(btnMLTrainer, 0); Grid.SetColumn(btnMLToggle, 2);
            mlBtnRow.Children.Add(btnMLTrainer); mlBtnRow.Children.Add(btnMLToggle);
            sp.Children.Add(mlBtnRow);

            var statusRow = new Grid { Margin = new Thickness(0, 0, 0, 2) };
            statusRow.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            statusRow.ColumnDefinitions.Add(new ColumnDefinition());
            var lbl = Tx("Status:  ", 10, C_MUTED);
            Grid.SetColumn(lbl, 0);
            liveMLStatusTb = Tx("Disabled", 10, C_MUTED);
            Grid.SetColumn(liveMLStatusTb, 1);
            statusRow.Children.Add(lbl); statusRow.Children.Add(liveMLStatusTb);
            sp.Children.Add(statusRow);

            liveMLProbTb = Tx("---", 11, C_MUTED, bold: true);
            sp.Children.Add(liveMLProbTb);
            sp.Children.Add(Tx($"Threshold {MLThreshold:F2}  ·  Window {MLWindowSize}  ·  LR {MLLearningRate:F3}", 9, C_MUTED));

            card.Child = sp;
            AddToCollapsible(_mlWrapper, card);
        }

        private void RefreshMLSection()
        {
            if (btnMLToggle == null) return;
            bool on = EnableML;
            btnMLToggle.Content     = on ? "ML FILTER  ON" : "ML FILTER  OFF";
            btnMLToggle.Background  = UiBrush(on ? Color.FromRgb(0, 60, 40) : Color.FromRgb(50, 20, 20));
            btnMLToggle.Foreground  = UiBrush(on ? C_GREEN : C_MUTED);
            btnMLToggle.BorderBrush = UiBrush(on ? C_GREEN : C_MUTED);
            if (!on) { SetTx(liveMLStatusTb, "Disabled", C_MUTED); return; }
            if (_mlSampleCount < MLMinSamples)
                SetTx(liveMLStatusTb, $"Warming up  ({_mlSampleCount}/{MLMinSamples})", C_AMBER);
            else if (_mlLastBlocked)
                SetTx(liveMLStatusTb, $"BLOCKED  p={_mlLastScore:F3}", C_RED);
            else
                SetTx(liveMLStatusTb, $"PASS  p={_mlLastScore:F3}", C_GREEN);
            SetTx(liveMLProbTb,
                  _mlSampleCount >= MLMinSamples ? $"{_mlLastScore:F3}" : "---",
                  _mlLastScore >= MLThreshold ? C_GREEN : C_RED);
        }

        // ── Dashboard ─────────────────────────────────────────────────────────
        private void BuildDashboardSection()
        {
            btnTradeReport = new Button
            {
                Content = "Trade Report", Padding = new Thickness(0, 8, 0, 8),
                FontSize = 13, FontWeight = FontWeights.SemiBold,
                Background = new SolidColorBrush(Color.FromRgb(35, 40, 80)),
                Foreground = Brushes.White, BorderBrush = new SolidColorBrush(C_BLUE),
                BorderThickness = new Thickness(1), Margin = new Thickness(6, 4, 3, 10),
                Cursor = Cursors.Hand,
                ToolTip = "Run HiLoRiderDashboardGenerator.py on the live log and open in browser",
            };
            btnTradeReport.Click += (s, e) =>
            {
                try { DoGenerateReport(btnTradeReport, "Trade Report",
                    "HiLoRider_trades.jsonl", "HiLoRider_Dashboard.html", "HiLoRider — Trade Report",
                    "Complete at least one live or Market Replay trade with logging enabled."); }
                catch (Exception ex) { Print($"[HiLoRiderUI] Trade Report: {ex.Message}"); }
            };

            btnBacktestReport = new Button
            {
                Content = "Backtest Report", Padding = new Thickness(0, 8, 0, 8),
                FontSize = 13, FontWeight = FontWeights.SemiBold,
                Background = new SolidColorBrush(Color.FromRgb(35, 40, 80)),
                Foreground = Brushes.White, BorderBrush = new SolidColorBrush(C_BLUE),
                BorderThickness = new Thickness(1), Margin = new Thickness(3, 4, 6, 10),
                Cursor = Cursors.Hand,
                ToolTip = "Run HiLoRiderDashboardGenerator.py on the backtest log and open in browser",
            };
            btnBacktestReport.Click += (s, e) =>
            {
                try { DoGenerateReport(btnBacktestReport, "Backtest Report",
                    "HiLoRider_trades_backtest.jsonl", "HiLoRider_Backtest_Dashboard.html", "HiLoRider — Backtest Report",
                    "Run a Strategy Analyzer backtest with logging enabled first."); }
                catch (Exception ex) { Print($"[HiLoRiderUI] Backtest Report: {ex.Message}"); }
            };

            var reportRow = new Grid();
            reportRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            reportRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            Grid.SetColumn(btnTradeReport,    0);
            Grid.SetColumn(btnBacktestReport, 1);
            reportRow.Children.Add(btnTradeReport);
            reportRow.Children.Add(btnBacktestReport);
            hudStack.Children.Add(reportRow);

            // ── Trade Log ON/OFF (Jul 2026) ─────────────────────────────
            // EnableTradeLogging has no UI visibility anywhere else -- see
            // CLAUDE.md "Reverter -- Live Trades Not Logging" for the full root-
            // cause writeup (confirmed via NT8 execution log: real position closes
            // with zero corresponding JSONL/CSV rows, zero errors, because a saved
            // chart/workspace instance can carry a stale EnableTradeLogging=false
            // forward across sessions with nothing in the UI showing it). This
            // button makes the current state visible and fixable without the
            // Properties dialog.
            btnTradeLogToggle = new Button
            {
                Content = EnableTradeLogging ? "TRADE LOG  ON" : "TRADE LOG  OFF",
                FontSize = 12, FontWeight = FontWeights.Bold, Height = 28,
                Cursor = Cursors.Hand,
                Background  = new SolidColorBrush(EnableTradeLogging ? Color.FromRgb(0, 60, 40) : Color.FromRgb(50, 20, 20)),
                Foreground  = new SolidColorBrush(EnableTradeLogging ? C_GREEN : C_RED),
                BorderBrush = new SolidColorBrush(EnableTradeLogging ? C_GREEN : C_RED),
                BorderThickness = new Thickness(1), Margin = new Thickness(6, 0, 6, 10),
                ToolTip = "Toggle live/replay JSONL+CSV trade logging. If this shows OFF and you " +
                          "didn't turn it off yourself, a saved chart instance likely carried a " +
                          "stale value forward -- click to turn it back on.",
            };
            btnTradeLogToggle.Click += (s, e) =>
            {
                EnableTradeLogging = !EnableTradeLogging;
                if (EnableTradeLogging && _jsonLogger == null) InitializeTradeLoggers();
                RefreshTradeLogButton();
            };
            hudStack.Children.Add(btnTradeLogToggle);

            btnEconCal = new Button
            {
                Content = "Econ Calendar", Padding = new Thickness(0, 8, 0, 8),
                FontSize = 13, FontWeight = FontWeights.SemiBold,
                Background = new SolidColorBrush(Color.FromRgb(45, 35, 10)),
                Foreground = new SolidColorBrush(C_AMBER),
                BorderBrush = new SolidColorBrush(C_AMBER),
                BorderThickness = new Thickness(1), Margin = new Thickness(6, 4, 6, 6),
                Cursor = Cursors.Hand,
                ToolTip = "Fetch today's economic events (EconomicCalendar.py) and refresh the regime badge bias.",
            };
            btnEconCal.Click += (s, e) => { try { DoRunEconCal(); } catch (Exception ex) { Print($"[UI] EconCal: {ex.Message}"); } };
            var econGexRow = new Grid();
            econGexRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            econGexRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            Grid.SetColumn(btnEconCal, 0);
            Grid.SetColumn(_btnRefreshGEX, 1);
            econGexRow.Children.Add(btnEconCal);
            econGexRow.Children.Add(_btnRefreshGEX);
            hudStack.Children.Add(econGexRow);
        }

        private void RefreshTradeLogButton()
        {
            if (btnTradeLogToggle == null) return;
            bool on = EnableTradeLogging;
            btnTradeLogToggle.Content     = on ? "TRADE LOG  ON" : "TRADE LOG  OFF";
            btnTradeLogToggle.Background  = new SolidColorBrush(on ? Color.FromRgb(0, 60, 40) : Color.FromRgb(50, 20, 20));
            btnTradeLogToggle.Foreground  = new SolidColorBrush(on ? C_GREEN : C_RED);
            btnTradeLogToggle.BorderBrush = new SolidColorBrush(on ? C_GREEN : C_RED);
        }

        private void DoRunEconCal()
        {
            string myDocs = Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments);
            string script = Path.Combine(myDocs, "NinjaTrader 8", "EconomicCalendar.py");
            if (!File.Exists(script))
            { MessageBox.Show("EconomicCalendar.py not found.\nExpected:\n" + script, "Econ Calendar", MessageBoxButton.OK, MessageBoxImage.Warning); return; }
            string python = ResolvePython();
            btnEconCal.Content = "Fetching…"; btnEconCal.IsEnabled = false;
            System.Threading.ThreadPool.QueueUserWorkItem(_ =>
            {
                string err = ""; bool ok = false;
                string expected = Path.Combine(myDocs, "NinjaTrader 8", "EconomicCalendar", "calendar_latest.json");
                var run = KhanShared.KhanPythonToolRunner.Run(
                    python, script, "", Path.GetDirectoryName(script), expected, 30000,
                    _runId, StrategyVersion, FeatureVersion, GetConfigHash(), false, false);
                err = run.BestError;
                ok = run.Success;
                ChartControl.Dispatcher.InvokeAsync(() =>
                {
                    btnEconCal.Content = "Econ Calendar"; btnEconCal.IsEnabled = true;
                    if (ok) { LoadEconomicCalendar(); UpdateLivePanel(); }
                    else MessageBox.Show("EconomicCalendar.py failed.\n" + err.Trim(), "Econ Calendar", MessageBoxButton.OK, MessageBoxImage.Warning);
                });
            });
        }

        // ── SESSION P&L card ──────────────────────────────────────────────────
        private void BuildSessionPnLCard()
        {
            var card = new Border
            {
                Background      = new SolidColorBrush(C_CARD),
                BorderBrush     = new SolidColorBrush(C_BORDER),
                BorderThickness = new Thickness(1),
                CornerRadius    = new CornerRadius(5),
                Padding         = new Thickness(10, 7, 10, 7),
                Margin          = new Thickness(6, 2, 6, 2),
            };
            var sp = new StackPanel();

            // Account NAV
            var navRow = new Grid();
            navRow.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            navRow.ColumnDefinitions.Add(new ColumnDefinition());
            var navLbl = Tx("ACCOUNT NAV", 8, C_MUTED);
            livePnlNavTb = Tx("---", 11, C_TEXT);
            livePnlNavTb.HorizontalAlignment = HorizontalAlignment.Right;
            Grid.SetColumn(navLbl, 0); Grid.SetColumn(livePnlNavTb, 1);
            navRow.Children.Add(navLbl); navRow.Children.Add(livePnlNavTb);
            sp.Children.Add(navRow);

            // Realized / Unrealized two-column
            var rlzGrid = new Grid { Margin = new Thickness(0, 4, 0, 0) };
            rlzGrid.ColumnDefinitions.Add(new ColumnDefinition());
            rlzGrid.ColumnDefinitions.Add(new ColumnDefinition());
            var rlzSp  = new StackPanel();
            rlzSp.Children.Add(Tx("REALIZED", 8, C_MUTED));
            livePnlRealizedTb = Tx("---", 11, C_MUTED, bold: true);
            rlzSp.Children.Add(livePnlRealizedTb);
            var unrSp  = new StackPanel();
            unrSp.Children.Add(Tx("UNREALIZED", 8, C_MUTED));
            livePnlUnrealizedTb = Tx("---", 11, C_MUTED, bold: true);
            unrSp.Children.Add(livePnlUnrealizedTb);
            Grid.SetColumn(rlzSp, 0); Grid.SetColumn(unrSp, 1);
            rlzGrid.Children.Add(rlzSp); rlzGrid.Children.Add(unrSp);
            sp.Children.Add(rlzGrid);

            // Total P&L (prominent)
            var totSp = new StackPanel { Margin = new Thickness(0, 4, 0, 0) };
            totSp.Children.Add(Tx("TOTAL P&L", 8, C_MUTED));
            livePnlTotalTb = Tx("---", 15, C_MUTED, bold: true);
            totSp.Children.Add(livePnlTotalTb);
            sp.Children.Add(totSp);

            // Divider + record
            sp.Children.Add(new Border
            {
                BorderBrush     = new SolidColorBrush(C_DIM),
                BorderThickness = new Thickness(0, 1, 0, 0),
                Margin          = new Thickness(0, 5, 0, 4),
            });
            livePnlRecordTb = Tx("0W / 0L", 9, C_MUTED);
            sp.Children.Add(livePnlRecordTb);

            card.Child = sp;
            hudStack.Children.Add(card);
        }

        // ── UpdateLivePanel ────────────────────────────────────────────────────
        internal void UpdateLivePanel()
        {
            if (!uiPanelActive || ChartControl == null) return;
            _livePanelRefreshDirty = true;
        }

        private void OnLivePanelRefreshTimerTick(object sender, EventArgs e)
        {
            if (!_livePanelRefreshDirty || !uiPanelActive) return;
            _livePanelRefreshDirty = false;
            if (_ctScrollViewer != null && _ctScrollViewer.Visibility != Visibility.Visible) return;
            try { RefreshLiveCard();              } catch { }
            try { UpdateStrategyControlButtons(); } catch { }
            try { RefreshMLSection();             } catch { }
            try { RefreshGEXCard();               } catch { }
        }

        private void RefreshLiveCard()
        {
            if (regimeBadgeValue == null) return;

            // Regime badge
            Color regColor = currentRegime == 2 ? (tradeDir == 1 ? C_GREEN : C_RED)
                           : currentRegime == 1 ? C_AMBER
                           : currentRegime == 4 ? C_CYAN
                           : currentRegime == 3 ? (
                               currentRegimeLabel.StartsWith("LOSS")     ? C_RED   :
                               currentRegimeLabel.StartsWith("DRAWDOWN") ? C_RED   :
                               currentRegimeLabel.StartsWith("PROFIT")   ? C_GREEN :
                               currentRegimeLabel.StartsWith("COOLDOWN") ? C_AMBER :
                               currentRegimeLabel.StartsWith("CONSEC")   ? C_AMBER :
                               C_MUTED)
                           : currentRegime == 0 ? (
                               currentRegimeLabel.Contains("TREND ▲") ? C_GREEN :
                               currentRegimeLabel.Contains("TREND ▼") ? C_RED   :
                               C_MUTED)
                           : C_MUTED;
            regimeBadgeValue.FontSize = 15;
            SetTx(regimeBadgeValue, currentRegimeLabel, regColor);
            regimeBadgeBorder.BorderBrush = UiBrush(regColor);
            if (Position.MarketPosition != MarketPosition.Flat)
            {
                double _unreal = GetDailyLimitUnrealizedPnl();
                double _real   = GetDailyLimitRealizedPnl();
                double _total  = _real + _unreal;
                string _arrow  = Position.MarketPosition == MarketPosition.Long ? "  ▲" : "  ▼";
                Color  _c      = _unreal >= 0 ? C_GREEN : C_RED;
                regimeBadgeValue.FontSize = 20;
                SetTx(regimeBadgeValue, (_unreal >= 0 ? "+" : "") + _unreal.ToString("C0") + _arrow, _c);
                regimeBadgeBorder.BorderBrush = UiBrush(_c);
            }

            // Bar range gauges
            double b1 = bar1RangeTicks;
            double b0 = bar0RangeTicks;
            if (regimeBadgeSub != null)
            {
                string aeMode = AutoExitMode == HiLoRiderAutoExit.DiffCross ? "AutoExit: DiffCross" : "AutoExit: Disabled";
                SetTx(regimeBadgeSub, aeMode + GetEconSubSuffix() + GetChopSubSuffix() + GetConnectionSubSuffix(), C_MUTED);
            }
            bool nearMid = hiloInd != null && hiloInd.MiddleLine.IsValidDataPoint(0)
                && Math.Abs(Close[0] - hiloInd.MiddleLine[0]) / TickSize <= 5;
            SetTx(regimeBadgeBar1,    $"Bar[1]: {b1:F0}t", nearMid ? C_AMBER : C_MUTED);
            SetTx(regimeBadgeBar0,    $"Bar[0]: {b0:F0}t", C_MUTED);
            SetTx(regimeBadgeLongest, "", C_MUTED);

            // Auto exit radio sync
            if (rbAEDiffCross != null)
            {
                uiBusy = true;
                rbAEDiffCross.IsChecked  = AutoExitMode == HiLoRiderAutoExit.DiffCross;
                rbAEDisabled.IsChecked   = AutoExitMode == HiLoRiderAutoExit.Disabled;
                uiBusy = false;
            }

            // BUY/SELL readiness
            bool inPos  = Position.MarketPosition != MarketPosition.Flat;
            string buyW = GetLongEntryWarning();
            string selW = GetShortEntryWarning();
            SetTx(liveBuyTb,  buyW  == null ? "READY" : "BLOCKED", buyW  == null ? C_GREEN : C_AMBER);
            SetTx(liveSellTb, selW  == null ? "READY" : "BLOCKED", selW  == null ? C_GREEN : C_AMBER);

            if (btnBuy  != null) { btnBuy.BorderBrush  = UiBrush(inPos ? C_DIM : buyW  != null ? C_AMBER : C_GREEN); btnBuy.BorderThickness  = new Thickness(!inPos ? 2 : 1); btnBuy.Opacity  = inPos ? 0.35 : 1.0; }
            if (btnSell != null) { btnSell.BorderBrush = UiBrush(inPos ? C_DIM : selW  != null ? C_AMBER : C_GREEN); btnSell.BorderThickness = new Thickness(!inPos ? 2 : 1); btnSell.Opacity = inPos ? 0.35 : 1.0; }
            double stopOp = inPos ? 1.0 : 0.35;
            if (btnBE      != null) btnBE.Opacity      = stopOp;
            if (btnMoveTS  != null) btnMoveTS.Opacity  = stopOp;
            if (btnMoveTS50!= null) btnMoveTS50.Opacity= stopOp;
            if (btnAdd1    != null) btnAdd1.Opacity    = inPos ? 1.0 : 0.35;
            if (btnClose1  != null) btnClose1.Opacity  = inPos ? 1.0 : 0.35;
            if (btnClose   != null) btnClose.Opacity   = inPos ? 1.0 : 0.35;

            if (btnEnableLossLimit   != null) ApplyLimitBtnStyle(btnEnableLossLimit,   EnableDailyLossLimit);
            if (btnEnableProfitLimit != null) ApplyLimitBtnStyle(btnEnableProfitLimit, EnableDailyProfitLimit);

            // SESSION P&L card
            if (livePnlNavTb != null)
            {
                double nav = 0;
                try { nav = Account?.Get(AccountItem.NetLiquidation, Currency.UsDollar) ?? 0; } catch { }
                double realized   = GetDailyLimitRealizedPnl();
                double unrealized = GetDailyLimitUnrealizedPnl();
                double totalPnL   = realized + unrealized;
                int    trades     = hudWins + hudLosses;
                double winRate    = trades > 0 ? (double)hudWins / trades : 0;
                bool   lossHit    = EnableDailyLossLimit   && DailyLossLimit   > 0 && totalPnL <= -DailyLossLimit;
                bool   profitHit  = EnableDailyProfitLimit && DailyProfitLimit > 0 && totalPnL >=  DailyProfitLimit;

                SetTx(livePnlNavTb,        nav.ToString("C"),       nav       >= 0 ? C_TEXT  : C_RED);
                SetTx(livePnlRealizedTb,   realized.ToString("C"),  realized  >= 0 ? C_GREEN : C_RED);
                SetTx(livePnlUnrealizedTb, unrealized.ToString("C"),unrealized>= 0 ? C_GREEN : C_RED);

                Color  totColor = lossHit ? C_RED : profitHit ? C_GREEN : totalPnL >= 0 ? C_GREEN : C_RED;
                string totStr   = totalPnL.ToString("C")
                    + (lossHit ? "  ⛔ LOSS" : profitHit ? "  ⛔ PROFIT" : "");
                SetTx(livePnlTotalTb, totStr, totColor);

                string record = $"{hudWins}W / {hudLosses}L";
                if (trades > 0) record += $"  ({winRate:P0})";
                SetTx(livePnlRecordTb, record, C_MUTED);
            }
        }

        // ── Entry warnings ────────────────────────────────────────────────────
        private string GetLongEntryWarning()
        {
            double pnl = GetDailyLimitTotalPnl();
            if (EnableDailyLossLimit   && DailyLossLimit   > 0 && pnl <= -DailyLossLimit)   return $"LOSS LIMIT HIT: ${Math.Abs(pnl):F0}";
            if (EnableDailyProfitLimit && DailyProfitLimit > 0 && pnl >= DailyProfitLimit)  return $"PROFIT LIMIT HIT: ${pnl:F0}";
            return null;
        }

        private string GetShortEntryWarning()
        {
            double pnl = GetDailyLimitTotalPnl();
            if (EnableDailyLossLimit   && DailyLossLimit   > 0 && pnl <= -DailyLossLimit)   return $"LOSS LIMIT HIT: ${Math.Abs(pnl):F0}";
            if (EnableDailyProfitLimit && DailyProfitLimit > 0 && pnl >= DailyProfitLimit)  return $"PROFIT LIMIT HIT: ${pnl:F0}";
            return null;
        }

        private void HandleBuyClick()
        {
            if (!IsEntryStateClear()) return;
            string reason = GetLongEntryWarning();
            if (reason == null) { DismissEntryWarning(); QueueStrategyAction(() => { DoManualBuy(); LockStrategyForTrade(); }); return; }
            pendingEntryDir          = PendingEntryDir.Long;
            entryWarningLine2.Text   = reason;
            entryConfirmBtn.Content  = "CONFIRM BUY  →  EXECUTE ANYWAY";
            entryConfirmBtn.Background = new SolidColorBrush(Color.FromRgb(0, 100, 30));
            entryWarningPanel.Visibility = Visibility.Visible;
            entryWarningTimer.Stop(); entryWarningTimer.Start();
        }

        private void HandleSellClick()
        {
            if (!IsEntryStateClear()) return;
            string reason = GetShortEntryWarning();
            if (reason == null) { DismissEntryWarning(); QueueStrategyAction(() => { DoManualSell(); LockStrategyForTrade(); }); return; }
            pendingEntryDir          = PendingEntryDir.Short;
            entryWarningLine2.Text   = reason;
            entryConfirmBtn.Content  = "CONFIRM SELL  →  EXECUTE ANYWAY";
            entryConfirmBtn.Background = new SolidColorBrush(Color.FromRgb(130, 0, 0));
            entryWarningPanel.Visibility = Visibility.Visible;
            entryWarningTimer.Stop(); entryWarningTimer.Start();
        }

        private void OnEntryConfirmClicked(object sender, RoutedEventArgs e)
        {
            var dir = pendingEntryDir;
            DismissEntryWarning();
            if (dir == PendingEntryDir.Long)  QueueStrategyAction(() => { DoManualBuy(); LockStrategyForTrade(); });
            if (dir == PendingEntryDir.Short) QueueStrategyAction(() => { DoManualSell(); LockStrategyForTrade(); });
        }

        private void DismissEntryWarning()
        {
            entryWarningTimer?.Stop();
            pendingEntryDir = PendingEntryDir.None;
            if (entryWarningPanel != null) entryWarningPanel.Visibility = Visibility.Collapsed;
        }

        private void QueueStrategyAction(Action action)
        {
            try
            {
                TriggerCustomEvent(_ =>
                {
                    try { action(); }
                    catch (Exception ex) { Print("[HiLoRiderUI] Order action: " + ex.Message); }
                }, null);
            }
            catch (Exception ex) { Print("[HiLoRiderUI] Unable to queue strategy action: " + ex.Message); }
        }
        // ── Manual trade execution ────────────────────────────────────────────
        private void LockStrategyForTrade()
        {
            _strategyWasEnabledBeforeManualTrade = strategyEnabled;
            _strategyLockedByManualTrade = true;
            if (strategyEnabled) SetStrategyEnabled(false);
            else ChartControl?.Dispatcher.InvokeAsync(() => UpdateStrategyControlButtons());
}

        private void DoManualBuy()
        {
            if (!IsEntryStateClear()) return;
            _isManualTrade = true;
            tradeDir = 1;
            double entryPx = useMarketOrder ? GetCurrentAsk(0) : GetCurrentBid(0) - LimitOffsetTicks * TickSize;
            stopLevel      = GetHiLoRiderStopPrice(1, entryPx);
            targetLevel    = TargetMode == RPTargetMode.NoTarget ? 0 : CalcTargetLevel(1, entryPx);
            targetTicksHeld= targetLevel > 0 ? Math.Max(1.0, Math.Abs(targetLevel - entryPx) / TickSize) : FixedTPTicks;
            bePriceTrigger = entryPx + BE_TriggerTicks * TickSize;
            SnapshotEntryIndicators("Manual");
            ocoId = State == State.Historical
                ? DateTime.Now.ToString("HHmmssff") + CurrentBar + "M"
                : GetAtmStrategyUniqueId();
            entryOrder = useMarketOrder
                ? SubmitOrderUnmanaged(0, OrderAction.Buy, OrderType.Market, Contracts, 0, 0, ocoId, "HLR LE")
                : SubmitOrderUnmanaged(0, OrderAction.Buy, OrderType.Limit,  Contracts, entryPx, 0, ocoId, "HLR LE");
        }

        private void DoManualSell()
        {
            if (!IsEntryStateClear()) return;
            _isManualTrade = true;
            tradeDir = -1;
            double entryPx = useMarketOrder ? GetCurrentBid(0) : GetCurrentAsk(0) + LimitOffsetTicks * TickSize;
            stopLevel      = GetHiLoRiderStopPrice(-1, entryPx);
            targetLevel    = TargetMode == RPTargetMode.NoTarget ? 0 : CalcTargetLevel(-1, entryPx);
            targetTicksHeld= targetLevel > 0 ? Math.Max(1.0, Math.Abs(targetLevel - entryPx) / TickSize) : FixedTPTicks;
            bePriceTrigger = entryPx - BE_TriggerTicks * TickSize;
            SnapshotEntryIndicators("Manual");
            ocoId = State == State.Historical
                ? DateTime.Now.ToString("HHmmssff") + CurrentBar + "M"
                : GetAtmStrategyUniqueId();
            entryOrder = useMarketOrder
                ? SubmitOrderUnmanaged(0, OrderAction.SellShort, OrderType.Market, Contracts, 0, 0, ocoId, "HLR SE")
                : SubmitOrderUnmanaged(0, OrderAction.SellShort, OrderType.Limit,  Contracts, entryPx, 0, ocoId, "HLR SE");
        }

        private void DoAddOneContract()
        {
            if (Position.MarketPosition == MarketPosition.Flat) return;
            if (stopOrder == null) return;
            bool isLong = Position.MarketPosition == MarketPosition.Long;
            if (isLong) SubmitOrderUnmanaged(0, OrderAction.Buy,       OrderType.Market, 1, 0, 0, "", "HLR Add1 L");
            else        SubmitOrderUnmanaged(0, OrderAction.SellShort, OrderType.Market, 1, 0, 0, "", "HLR Add1 S");
            // Bracket resize happens in OnOrderUpdate once the add order's fill is
            // CONFIRMED (see the "HLR Add1" handler in HiLoRider.cs) -- previously resized
            // here synchronously, assuming the add would fill, which could size the
            // bracket for more contracts than the position actually held if the add
            // order was rejected or didn't fill as submitted (fleet-wide order-safety
            // audit, Jul 2026).
        }

        private void DoCloseOneContract()
        {
            if (Position.MarketPosition == MarketPosition.Flat) return;
            if (Position.Quantity <= 1) { DoManualClose(); return; }
            SubmitPartialExit("HLR Close1", 1);
}

        private void DoManualClose()
        {
            if (Position.MarketPosition == MarketPosition.Flat) return;
            SubmitFullExit("HLR Close");
}

        private void DoBreakeven()
        {
            if (Position.MarketPosition == MarketPosition.Flat || stopOrder == null || filledPrice == 0) return;
            double beStop = Instrument.MasterInstrument.RoundToTickSize(
                filledPrice + (tradeDir == 1 ? 1 : -1) * BE_OffsetTicks * TickSize);
            bool alreadyPast = tradeDir == 1 ? stopLevel >= beStop : stopLevel <= beStop;
            if (alreadyPast) { beRealized = true; return; }
            RequestStopChange(beStop, false);
            beRealized = true;
}

        private void DoMoveTrailstop()
        {
            if (Position.MarketPosition == MarketPosition.Flat || stopOrder == null) return;
            double mktPx = tradeDir == 1 ? GetCurrentBid() : GetCurrentAsk();
            if (mktPx <= 0) mktPx = Close[0];
            if (filledPrice > 0 && (tradeDir == 1 ? mktPx <= filledPrice : mktPx >= filledPrice)) return;
            double newStop = tradeDir == 1
                ? Instrument.MasterInstrument.RoundToTickSize(Low[0] - 4 * TickSize)
                : Instrument.MasterInstrument.RoundToTickSize(High[0] + 4 * TickSize);
            RequestStopChange(newStop, false);
}

        private void DoMoveTS50Pct()
        {
            if (Position.MarketPosition == MarketPosition.Flat || stopOrder == null) return;
            double currentPx = tradeDir == 1 ? GetCurrentBid() : GetCurrentAsk();
            double midStop   = Instrument.MasterInstrument.RoundToTickSize((stopLevel + currentPx) / 2.0);
            double buffer    = StopMarketBufferTicks * TickSize;
            bool better = tradeDir == 1
                ? midStop > stopLevel && midStop < GetCurrentBid() - buffer
                : midStop < stopLevel && midStop > GetCurrentAsk() + buffer;
            if (!better) return;
            RequestStopChange(midStop, false);
        }

        // FileShare.ReadWrite is required: the running strategy's own trade logger
        // holds the log open for writing, so File.ReadLines/ReadAllLines throw
        // "being used by another process".
        internal static string[] ReadAllLinesShared(string path)
        {
            using (var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr = new StreamReader(fs))
                return sr.ReadToEnd().Split(new[] { "\r\n", "\n" }, StringSplitOptions.None);
        }

        private void DoRunMLTrainer(string jsonlName, string logFolderName, string trainerName, string reportName, Button trainerBtn)
        {
            try
            {
                string myDocs = Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments);
                string logDir = !string.IsNullOrWhiteSpace(TradeLogPath)
                    ? TradeLogPath : Path.Combine(myDocs, "NinjaTrader 8", logFolderName);
                logDir = Path.Combine(logDir, string.IsNullOrWhiteSpace(_runId) ? "UNASSIGNED_RUN" : _runId);
                string jsonlPath = null;
                foreach (string d in new[] { logDir })
                { if (string.IsNullOrEmpty(d)) continue; string c = Path.Combine(d, jsonlName); if (File.Exists(c)) { jsonlPath = c; logDir = d; break; } }
                const int MIN_TRADES = 50;
                int tradeCount = 0;
                if (jsonlPath != null) foreach (string line in ReadAllLinesShared(jsonlPath)) if (!string.IsNullOrWhiteSpace(line)) tradeCount++;
                if (tradeCount == 0)
                {
                    MessageBox.Show($"No completed automatic trades are available for the active run yet.\n\nRunId: {_runId}\nLog: {jsonlPath ?? Path.Combine(logDir, jsonlName)}",
                        "ML Trainer — No Active-Run Trades", MessageBoxButton.OK, MessageBoxImage.Information); return;
                }
                if (tradeCount < MIN_TRADES)
                {
                    MessageBox.Show($"The active run does not yet have enough completed automatic trades.\n\nRunId: {_runId}\nFound: {tradeCount}  Required: {MIN_TRADES}\n(50-99=low, 100-199=medium, 200+=high confidence)\n\nLog: {jsonlPath ?? Path.Combine(logDir, jsonlName)}",
                        "ML Trainer — Not Enough Data", MessageBoxButton.OK, MessageBoxImage.Information); return;
                }
                string trainerScript = null;
                foreach (string d in new[] { logDir, Path.Combine(myDocs, "NinjaTrader 8") })
                { if (string.IsNullOrEmpty(d)) continue; string c = Path.Combine(d, trainerName); if (File.Exists(c)) { trainerScript = c; break; } }
                if (trainerScript == null)
                { MessageBox.Show($"{trainerName} not found.\nExpected in: {Path.Combine(myDocs, "NinjaTrader 8")}", "ML Trainer", MessageBoxButton.OK, MessageBoxImage.Warning); return; }
                string python = ResolvePython(); string htmlPath = Path.Combine(logDir, reportName);
                trainerBtn.Content = "Training…"; trainerBtn.IsEnabled = false;
                string cJ = jsonlPath, cS = trainerScript, cH = htmlPath, cL = logDir;
                System.Threading.ThreadPool.QueueUserWorkItem(_ =>
                {
                    string stderr = ""; bool ok = false;
                    var run = KhanShared.KhanPythonToolRunner.Run(
                        python, cS, $"--log \"{cJ}\" --out \"{cH}\"", cL, cH, 120000,
                        _runId, StrategyVersion, FeatureVersion, GetConfigHash());
                    stderr = run.BestError;
                    ok = run.Success;
                    ChartControl.Dispatcher.InvokeAsync(() =>
                    {
                        trainerBtn.Content = "ML Trainer"; trainerBtn.IsEnabled = true;
                        if (ok) OpenInBrowser(cH);
                        else MessageBox.Show($"Trainer failed.\n{stderr.Trim()}", "ML Trainer", MessageBoxButton.OK, MessageBoxImage.Warning);
                    });
                });
            }
            catch (Exception ex) { MessageBox.Show($"ML Trainer error: {ex.Message}", Name, MessageBoxButton.OK, MessageBoxImage.Error); }
        }

        // ── Report generation ─────────────────────────────────────────────────
        private void DoGenerateReport(Button btn, string btnLabel, string jsonlName,
            string outHtmlName, string title, string missingHint)
        {
            try
            {
                string myDocs  = Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments);
                string logDir  = !string.IsNullOrWhiteSpace(TradeLogPath)
                    ? TradeLogPath
                    : Path.Combine(myDocs, "NinjaTrader 8", "HiLoRiderLogs");
                logDir = Path.Combine(logDir, string.IsNullOrWhiteSpace(_runId) ? "UNASSIGNED_RUN" : _runId);

                string[] logCandidates = { logDir };
                string jsonlFile = null;
                foreach (string d in logCandidates)
                {
                    if (string.IsNullOrEmpty(d)) continue;
                    string c = Path.Combine(d, jsonlName);
                    if (File.Exists(c)) { jsonlFile = c; logDir = d; break; }
                }
                if (jsonlFile == null)
                {
                    MessageBox.Show($"Trade log not found:\n{Path.Combine(logDir, jsonlName)}\n\n{missingHint}",
                        title, MessageBoxButton.OK, MessageBoxImage.Warning);
                    return;
                }

                // Guard: file exists but may contain zero trade records (e.g. just
                // a BOM, written when the JSON logger was first instantiated but no
                // trade has closed yet) -- launching the Python generator on an
                // empty log throws a raw traceback ("No valid JSON records found")
                // that surfaced to the user unhelpfully as a crash dialog (Jul 2026).
                // Read with FileShare.ReadWrite and a short retry (Jul 2026 fix) --
                // the live/backtest trade logger holds this file open with its own
                // StreamWriter, and a bare File.ReadLines() can hit a transient share
                // violation while a trade is mid-write. The old code silently
                // swallowed that exception and treated it identically to "genuinely
                // empty," so a fully-populated file could still report "no trades
                // logged" if the read raced the writer -- confirmed on ThreeAmigos
                // (see CLAUDE.md), rolled out fleet-wide here.
                int  liveLineCount = 0;
                bool readOk        = false;
                for (int attempt = 0; attempt < 3 && !readOk; attempt++)
                {
                    try
                    {
                        liveLineCount = 0;
                        foreach (string ln in ReadAllLinesShared(jsonlFile))
                            if (!string.IsNullOrWhiteSpace(ln.TrimStart('\uFEFF')))
                                liveLineCount++;
                        readOk = true;
                    }
                    catch { if (attempt < 2) System.Threading.Thread.Sleep(100); }
                }
                if (readOk && liveLineCount == 0)
                {
                    MessageBox.Show($"No trades logged yet in:\n{jsonlFile}\n\n{missingHint}",
                        title, MessageBoxButton.OK, MessageBoxImage.Information);
                    return;
                }
                // readOk == false after 3 attempts -- fall through and let the Python
                // script make its own attempt rather than showing a misleading "no
                // trades" message for what may just be a momentarily-locked file.

                string scriptPath = null;
                if (!string.IsNullOrWhiteSpace(DashboardScriptPath) && File.Exists(DashboardScriptPath))
                    scriptPath = DashboardScriptPath;
                if (scriptPath == null)
                {
                    foreach (string d in new[] { logDir, Path.Combine(myDocs, "NinjaTrader 8") })
                    {
                        if (string.IsNullOrEmpty(d)) continue;
                        string c = Path.Combine(d, "HiLoRiderDashboardGenerator.py");
                        if (File.Exists(c)) { scriptPath = c; break; }
                    }
                }
                if (scriptPath == null)
                {
                    MessageBox.Show(
                        "HiLoRiderDashboardGenerator.py not found.\n\n" +
                        "Copy the script to one of these folders:\n\n" +
                        $"  {logDir}\n  {Path.Combine(myDocs, "NinjaTrader 8")}",
                        title, MessageBoxButton.OK, MessageBoxImage.Warning);
                    return;
                }

                string pythonExe = ResolvePython();
                string outFile   = Path.Combine(logDir, outHtmlName);

                btn.IsEnabled = false;
                btn.Content   = "Generating…";

                System.Threading.ThreadPool.QueueUserWorkItem(_ =>
                {
                    string stdout = "", stderr = ""; int exitCode = -1;
                    bool   htmlOk = false; string launchErr = null;
                    try
                    {
                        var run = KhanShared.KhanPythonToolRunner.Run(
                            pythonExe, scriptPath, $"\"{jsonlFile}\" --out \"{outFile}\"",
                            logDir, outFile, 60000, _runId, StrategyVersion,
                            FeatureVersion, GetConfigHash());
                        stdout = run.StandardOutput;
                        stderr = run.StandardError;
                        exitCode = run.ExitCode;
                        launchErr = string.IsNullOrWhiteSpace(run.Error) ? null : run.Error;
                        htmlOk = run.Success;
                    }
                    catch (Exception ex) { launchErr = ex.Message; }

                    ChartControl.Dispatcher.InvokeAsync(() =>
                    {
                        btn.IsEnabled = true;
                        btn.Content   = btnLabel;
                        if (launchErr != null)
                        { MessageBox.Show($"Error launching generator:\n\n{launchErr}", title, MessageBoxButton.OK, MessageBoxImage.Error); return; }
                        if (htmlOk)
                            OpenInBrowser(outFile);
                        else
                        {
                            string detail = !string.IsNullOrWhiteSpace(stderr) ? stderr
                                          : !string.IsNullOrWhiteSpace(stdout) ? stdout
                                          : $"Python exited with code {exitCode}";
                            MessageBox.Show($"HiLoRiderDashboardGenerator.py failed:\n\n{detail.Trim()}",
                                title, MessageBoxButton.OK, MessageBoxImage.Error);
                        }
                    });
                });
            }
            catch (Exception ex)
            {
                MessageBox.Show($"Error generating {btnLabel}:\n\n{ex.Message}", "HiLoRider", MessageBoxButton.OK, MessageBoxImage.Error);
            }
        }

        private void OpenInBrowser(string filePath)
        {
            string fileUrl = new Uri(filePath).AbsoluteUri;
            // Try Chrome first
            foreach (string chrome in new[] {
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),      @"Google\Chrome\Application\chrome.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86),   @"Google\Chrome\Application\chrome.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), @"Google\Chrome\Application\chrome.exe"),
            })
            {
                if (!File.Exists(chrome)) continue;
                try { Process.Start(new ProcessStartInfo(chrome, $"\"{fileUrl}\"") { UseShellExecute = false }); return; }
                catch { }
            }
            // Default browser fallback
            try { Process.Start(new ProcessStartInfo(filePath) { UseShellExecute = true }); }
            catch (Exception ex) { MessageBox.Show($"Could not open report:\n{ex.Message}", "HiLoRider", MessageBoxButton.OK, MessageBoxImage.Error); }
        }

        private string ResolvePython()
        {
            string user  = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            var candidates = new System.Collections.Generic.List<string>();
            foreach (string root in new[] {
                Path.Combine(user,  "anaconda3"), Path.Combine(user,  "miniconda3"),
                Path.Combine(user,  "Anaconda3"), Path.Combine(user,  "Miniconda3"),
                Path.Combine(local, "anaconda3"), Path.Combine(local, "miniconda3"),
                @"C:\ProgramData\anaconda3",      @"C:\ProgramData\Anaconda3",
            }) candidates.Add(Path.Combine(root, "python.exe"));
            foreach (string pfRoot in new[] {
                Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
                Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86),
            }) {
                string pyRoot = Path.Combine(pfRoot, "Python");
                if (!Directory.Exists(pyRoot)) continue;
                foreach (string d in Directory.GetDirectories(pyRoot, "Python3*"))
                    candidates.Add(Path.Combine(d, "python.exe"));
            }
            foreach (string p in candidates) if (File.Exists(p)) return p;
            return string.IsNullOrWhiteSpace(PythonExePath) ? "python" : PythonExePath;
        }

        // ── Helper factories ──────────────────────────────────────────────────
        private TextBlock Tx(string text, double size, Color col, bool bold = false)
            => new TextBlock { Text = text, FontSize = size, Foreground = UiBrush(col),
                               FontWeight = bold ? FontWeights.Bold : FontWeights.Normal };

        private void SetTx(TextBlock tb, string text, Color col)
        { if (tb == null) return; tb.Text = text; tb.Foreground = UiBrush(col); }

        private Border Card() => new Border
        {
            Background = new SolidColorBrush(C_CARD), BorderBrush = new SolidColorBrush(C_BORDER),
            BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(5),
            Padding = new Thickness(10, 8, 10, 8), Margin = new Thickness(6, 2, 6, 2),
        };

        private FrameworkElement HRule(Thickness? margin = null)
            => new Border { BorderBrush = new SolidColorBrush(C_BORDER), BorderThickness = new Thickness(0, 1, 0, 0),
                            Margin = margin ?? new Thickness(6, 2, 6, 2) };

        private void BuildDivider(string label)
        {
            var sp = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(10, 6, 10, 3) };
            sp.Children.Add(new Border { BorderBrush = new SolidColorBrush(C_BORDER), BorderThickness = new Thickness(0, 1, 0, 0),
                Width = 8, Height = 1, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 6, 0) });
            sp.Children.Add(Tx(label, 9, C_MUTED));
            sp.Children.Add(new Border { BorderBrush = new SolidColorBrush(C_BORDER), BorderThickness = new Thickness(0, 1, 0, 0),
                Height = 1, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(6, 0, 0, 0),
                HorizontalAlignment = HorizontalAlignment.Stretch });
            hudStack.Children.Add(sp);
        }

        private void BuildCollapsibleDivider(string label, out Border wrapper, bool startExpanded)
        {
            wrapper = new Border { Visibility = startExpanded ? Visibility.Visible : Visibility.Collapsed };
            var wrapCapture = wrapper;
            var btn = new Button
            {
                Content = (startExpanded ? "▾ " : "▸ ") + label,
                FontSize = 9, FontWeight = FontWeights.SemiBold,
                Foreground = new SolidColorBrush(C_MUTED), Background = Brushes.Transparent,
                BorderThickness = new Thickness(0), Margin = new Thickness(10, 4, 10, 2),
                HorizontalAlignment = HorizontalAlignment.Left, Cursor = Cursors.Hand,
            };
            bool expanded = startExpanded;
            btn.Click += (s, e) =>
            {
                expanded = !expanded;
                btn.Content = (expanded ? "▾ " : "▸ ") + label;
                wrapCapture.Visibility = expanded ? Visibility.Visible : Visibility.Collapsed;
            };
            hudStack.Children.Add(btn);
            hudStack.Children.Add(wrapper);
        }

        private void AddToCollapsible(Border wrapper, UIElement content)
        {
            if (wrapper == null) { hudStack.Children.Add(content); return; }
            var sp = wrapper.Child as StackPanel;
            if (sp == null) { sp = new StackPanel(); wrapper.Child = sp; }
            sp.Children.Add(content);
        }

        private RadioButton DarkRadio(string label, bool isChecked)
            => new RadioButton { Content = label, IsChecked = isChecked,
                Foreground = new SolidColorBrush(C_TEXT), Background = Brushes.Transparent,
                Margin = new Thickness(0, 0, 12, 0), FontSize = 11 };

        private CheckBox DarkCheckBox(string label, bool isChecked)
            => new CheckBox { Content = label, IsChecked = isChecked,
                Foreground = new SolidColorBrush(C_TEXT), Background = Brushes.Transparent,
                Margin = new Thickness(0, 3, 0, 3), FontSize = 11, Cursor = Cursors.Hand };

        private Button FlatBtn(string label, Color accent)
            => new Button { Content = label, Height = 30, FontSize = 11,
                Background = new SolidColorBrush(Color.FromArgb(60, accent.R, accent.G, accent.B)),
                Foreground = Brushes.White, BorderBrush = new SolidColorBrush(accent),
                BorderThickness = new Thickness(1), Margin = new Thickness(0, 0, 0, 2), Cursor = Cursors.Hand };

        private UIElement TwoCol(UIElement left, UIElement right)
        {
            var g = new Grid { Margin = new Thickness(0, 4, 0, 0) };
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            Grid.SetColumn(left, 0); Grid.SetColumn(right, 1);
            g.Children.Add(left); g.Children.Add(right);
            return g;
        }

        private UIElement FullWidthSpinner(string label, string value, out TextBox tb, Action<int> setter)
        {
            var row = new Grid { Margin = new Thickness(3) };
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            var lbl = new TextBlock { Text = label, Foreground = new SolidColorBrush(C_MUTED),
                FontSize = 10, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 8, 0) };
            Grid.SetColumn(lbl, 0); row.Children.Add(lbl);
            var sp = new StackPanel { HorizontalAlignment = HorizontalAlignment.Right };
            sp.Children.Add(MakeSpinnerGrid(value, out tb, setter));
            Grid.SetColumn(sp, 1); row.Children.Add(sp);
            return row;
        }

        private Grid MakeSpinnerGrid(string value, out TextBox tb, Action<int> setter)
        {
            var downTb = new TextBlock { Text = "▼", FontSize = 13, FontWeight = FontWeights.Bold,
                Foreground = new SolidColorBrush(C_RED),
                HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center };
            var down = new Border { Width = 26, Height = 28, Cursor = Cursors.Hand,
                Background = new SolidColorBrush(Color.FromRgb(30, 22, 28)),
                BorderBrush = new SolidColorBrush(C_BORDER), BorderThickness = new Thickness(1),
                Child = downTb };

            tb = new TextBox { Text = value, Width = 52, Height = 28, TextAlignment = TextAlignment.Center,
                VerticalContentAlignment = VerticalAlignment.Center,
                Background = new SolidColorBrush(C_DIM), Foreground = new SolidColorBrush(C_TEXT),
                FontWeight = FontWeights.Bold, FontSize = 12,
                BorderBrush = new SolidColorBrush(C_BORDER), BorderThickness = new Thickness(1) };

            var upTb = new TextBlock { Text = "▲", FontSize = 13, FontWeight = FontWeights.Bold,
                Foreground = new SolidColorBrush(C_GREEN),
                HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center };
            var up = new Border { Width = 26, Height = 28, Cursor = Cursors.Hand,
                Background = new SolidColorBrush(Color.FromRgb(15, 30, 26)),
                BorderBrush = new SolidColorBrush(C_BORDER), BorderThickness = new Thickness(1),
                Child = upTb };

            var g = new Grid();
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(26) });
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(52) });
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(26) });
            Grid.SetColumn(down, 0); g.Children.Add(down);
            Grid.SetColumn(tb,   1); g.Children.Add(tb);
            Grid.SetColumn(up,   2); g.Children.Add(up);

            var cap = tb;
            cap.MouseLeftButtonDown += (s, e) => { Keyboard.Focus(cap); cap.SelectAll(); e.Handled = true; };
            KeyEventHandler ntKG = null;
            ntKG = (ks, ke) => {
                if (!cap.IsKeyboardFocused) return;
                var k = ke.Key;
                if (k == Key.Back) {
                    int si = cap.SelectionStart, sl = cap.SelectionLength;
                    if (sl > 0) { cap.Text = cap.Text.Remove(si, sl); cap.CaretIndex = si; }
                    else if (si > 0) { cap.Text = cap.Text.Remove(si - 1, 1); cap.CaretIndex = si - 1; }
                    ke.Handled = true; return;
                }
                string d = null;
                if (k >= Key.D0 && k <= Key.D9) d = ((int)(k - Key.D0)).ToString();
                else if (k >= Key.NumPad0 && k <= Key.NumPad9) d = ((int)(k - Key.NumPad0)).ToString();
                if (d == null) return;
                int csi = cap.SelectionStart, csl = cap.SelectionLength;
                string t = csl > 0 ? cap.Text.Remove(csi, csl) : cap.Text;
                int ci  = csl > 0 ? csi : cap.CaretIndex;
                cap.Text = t.Insert(ci, d); cap.CaretIndex = ci + 1; ke.Handled = true;
            };
            cap.GotKeyboardFocus  += (s, e) => { cap.SelectAll(); Window.GetWindow(cap)?.AddHandler(UIElement.PreviewKeyDownEvent, (Delegate)ntKG); };
            cap.LostKeyboardFocus += (s, e) => Window.GetWindow(cap)?.RemoveHandler(UIElement.PreviewKeyDownEvent, (Delegate)ntKG);
            down.MouseLeftButtonDown += (s, e) => { if (int.TryParse(cap.Text, out int v) && v > 1) { v--; cap.Text = v.ToString(); setter(v); } };
            up.MouseLeftButtonDown   += (s, e) => { if (int.TryParse(cap.Text, out int v)) { v++; cap.Text = v.ToString(); setter(v); } };
            cap.LostFocus        += (s, e) => { if (int.TryParse(cap.Text, out int v) && v > 0) setter(v); else cap.Text = "1"; };
            cap.PreviewTextInput += (s, e) => { e.Handled = !char.IsDigit(e.Text, 0); };
            return g;
        }

        private UIElement FullWidthSpinnerD(string label, double value, out TextBox tb, Action<double> setter)
        {
            var row = new Grid { Margin = new Thickness(3) };
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            var lbl = new TextBlock { Text = label, Foreground = new SolidColorBrush(C_MUTED),
                FontSize = 10, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 8, 0) };
            Grid.SetColumn(lbl, 0); row.Children.Add(lbl);
            var sp = new StackPanel { HorizontalAlignment = HorizontalAlignment.Right };
            sp.Children.Add(MakeSpinnerGridD(value, out tb, setter));
            Grid.SetColumn(sp, 1); row.Children.Add(sp);
            return row;
        }

        private Grid MakeSpinnerGridD(double value, out TextBox tb, Action<double> setter)
        {
            const double step = 0.1, min = 0.1;
            var ci = System.Globalization.CultureInfo.InvariantCulture;
            string Fmt(double d) => d.ToString("0.0#", ci);
            var downTb = new TextBlock { Text = "▼", FontSize = 13, FontWeight = FontWeights.Bold,
                Foreground = new SolidColorBrush(C_RED),
                HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center };
            var down = new Border { Width = 26, Height = 28, Cursor = Cursors.Hand,
                Background = new SolidColorBrush(Color.FromRgb(30, 22, 28)),
                BorderBrush = new SolidColorBrush(C_BORDER), BorderThickness = new Thickness(1), Child = downTb };
            tb = new TextBox { Text = Fmt(value), Width = 52, Height = 28, TextAlignment = TextAlignment.Center,
                VerticalContentAlignment = VerticalAlignment.Center,
                Background = new SolidColorBrush(C_DIM), Foreground = new SolidColorBrush(C_TEXT),
                FontWeight = FontWeights.Bold, FontSize = 12,
                BorderBrush = new SolidColorBrush(C_BORDER), BorderThickness = new Thickness(1) };
            var upTb = new TextBlock { Text = "▲", FontSize = 13, FontWeight = FontWeights.Bold,
                Foreground = new SolidColorBrush(C_GREEN),
                HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center };
            var up = new Border { Width = 26, Height = 28, Cursor = Cursors.Hand,
                Background = new SolidColorBrush(Color.FromRgb(15, 30, 26)),
                BorderBrush = new SolidColorBrush(C_BORDER), BorderThickness = new Thickness(1), Child = upTb };
            var g = new Grid();
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(26) });
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(52) });
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(26) });
            Grid.SetColumn(down, 0); g.Children.Add(down);
            Grid.SetColumn(tb,   1); g.Children.Add(tb);
            Grid.SetColumn(up,   2); g.Children.Add(up);
            var cap = tb;
            cap.MouseLeftButtonDown += (s, e) => { Keyboard.Focus(cap); cap.SelectAll(); e.Handled = true; };
            KeyEventHandler ntKG = null;
            ntKG = (ks, ke) => {
                if (!cap.IsKeyboardFocused) return;
                var k = ke.Key;
                if (k == Key.Back) {
                    int si = cap.SelectionStart, sl = cap.SelectionLength;
                    if (sl > 0) { cap.Text = cap.Text.Remove(si, sl); cap.CaretIndex = si; }
                    else if (si > 0) { cap.Text = cap.Text.Remove(si - 1, 1); cap.CaretIndex = si - 1; }
                    ke.Handled = true; return;
                }
                if (k == Key.OemPeriod || k == Key.Decimal) {
                    if (!cap.Text.Contains(".")) {
                        int si2 = cap.SelectionStart, sl2 = cap.SelectionLength;
                        string t2 = sl2 > 0 ? cap.Text.Remove(si2, sl2) : cap.Text;
                        int ci2 = sl2 > 0 ? si2 : cap.CaretIndex;
                        cap.Text = t2.Insert(ci2, "."); cap.CaretIndex = ci2 + 1;
                    }
                    ke.Handled = true; return;
                }
                string d = null;
                if (k >= Key.D0 && k <= Key.D9) d = ((int)(k - Key.D0)).ToString();
                else if (k >= Key.NumPad0 && k <= Key.NumPad9) d = ((int)(k - Key.NumPad0)).ToString();
                if (d == null) return;
                int csi = cap.SelectionStart, csl = cap.SelectionLength;
                string t = csl > 0 ? cap.Text.Remove(csi, csl) : cap.Text;
                int cii = csl > 0 ? csi : cap.CaretIndex;
                cap.Text = t.Insert(cii, d); cap.CaretIndex = cii + 1; ke.Handled = true;
            };
            cap.GotKeyboardFocus  += (s, e) => { cap.SelectAll(); Window.GetWindow(cap)?.AddHandler(UIElement.PreviewKeyDownEvent, (Delegate)ntKG); };
            cap.LostKeyboardFocus += (s, e) => Window.GetWindow(cap)?.RemoveHandler(UIElement.PreviewKeyDownEvent, (Delegate)ntKG);
            down.MouseLeftButtonDown += (s, e) => { if (double.TryParse(cap.Text, System.Globalization.NumberStyles.Any, ci, out double v) && v - step >= min) { v = Math.Round(v - step, 2); cap.Text = Fmt(v); setter(v); } };
            up.MouseLeftButtonDown   += (s, e) => { if (double.TryParse(cap.Text, System.Globalization.NumberStyles.Any, ci, out double v)) { v = Math.Round(v + step, 2); cap.Text = Fmt(v); setter(v); } };
            cap.LostFocus += (s, e) => { if (double.TryParse(cap.Text, System.Globalization.NumberStyles.Any, ci, out double v) && v >= min) { cap.Text = Fmt(Math.Round(v, 2)); setter(Math.Round(v, 2)); } else cap.Text = Fmt(min); };
            cap.PreviewTextInput += (s, e) => { e.Handled = !(char.IsDigit(e.Text, 0) || (e.Text == "." && !cap.Text.Contains("."))); };
            return g;
        }

        private UIElement SOSpinnerRow(string label, string init, out TextBox tb, Action<int> setter)
        {
            var sp = new StackPanel { Margin = new Thickness(0, 2, 4, 0) };
            sp.Children.Add(new TextBlock { Text = label, FontSize = 9,
                Foreground = new SolidColorBrush(C_MUTED), Margin = new Thickness(0, 0, 0, 3) });
            sp.Children.Add(MakeSpinnerGrid(init, out tb, setter));
            return sp;
        }

        private StackPanel ReadyChipCell(string label, out TextBlock valueTb)
        {
            var sp = new StackPanel { Margin = new Thickness(4) };
            sp.Children.Add(Tx(label, 8, C_MUTED));
            valueTb = Tx("---", 11, C_MUTED, bold: true);
            sp.Children.Add(valueTb);
            return sp;
        }

        private void RefreshOrderTypeButtons()
        {
            if (btnOrderTypeMkt == null || btnOrderTypeLmt == null) return;
            btnOrderTypeMkt.Background      = new SolidColorBrush(useMarketOrder  ? Color.FromArgb(80, 245, 158, 11) : C_DIM);
            btnOrderTypeMkt.Foreground      = new SolidColorBrush(useMarketOrder  ? C_AMBER  : C_MUTED);
            btnOrderTypeMkt.BorderBrush     = new SolidColorBrush(useMarketOrder  ? C_AMBER  : C_BORDER);
            btnOrderTypeMkt.BorderThickness = new Thickness(useMarketOrder  ? 2 : 1);
            btnOrderTypeLmt.Background      = new SolidColorBrush(!useMarketOrder ? Color.FromArgb(80, 59, 130, 246) : C_DIM);
            btnOrderTypeLmt.Foreground      = new SolidColorBrush(!useMarketOrder ? C_BLUE   : C_MUTED);
            btnOrderTypeLmt.BorderBrush     = new SolidColorBrush(!useMarketOrder ? C_BLUE   : C_BORDER);
            btnOrderTypeLmt.BorderThickness = new Thickness(!useMarketOrder ? 2 : 1);
        }

        private void RefreshBuySellLabels()
        {
            string tag = useMarketOrder ? "MKT" : "LMT";
            if (btnBuy  != null) btnBuy.Content  = $"▲  BUY {tag}";
            if (btnSell != null) btnSell.Content = $"▼  SELL {tag}";
        }

        // ── Daily limit toggle helpers ────────────────────────────────────────
        private void ApplyLimitBtnStyle(Button btn, bool en)
        {
            btn.Content         = en ? "ON" : "OFF";
            btn.Background      = UiBrush(en ? Color.FromArgb(70, 0, 212, 160) : Color.FromArgb(40, 80, 80, 80));
            btn.Foreground      = UiBrush(en ? C_GREEN : C_MUTED);
            btn.BorderBrush     = UiBrush(en ? C_GREEN : C_BORDER);
            btn.BorderThickness = new Thickness(1);
        }

        private UIElement BuildLimitToggleRow(string label, Func<bool> getter, int initAmt,
            out Button btnToggle, out TextBox tbAmt, Action<bool> enableSetter, Action<int> amtSetter)
        {
            var row = new Grid { Margin = new Thickness(3, 2, 3, 2) };
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

            var leftSp = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center };
            leftSp.Children.Add(new TextBlock { Text = label, FontSize = 10,
                Foreground = new SolidColorBrush(C_TEXT), VerticalAlignment = VerticalAlignment.Center,
                Margin = new Thickness(0, 0, 8, 0) });
            var btn = new Button { FontSize = 10, FontWeight = FontWeights.Bold,
                Height = 26, Padding = new Thickness(8, 0, 8, 0), VerticalAlignment = VerticalAlignment.Center };
            ApplyLimitBtnStyle(btn, getter());
            btn.Click += (s, e) => { bool nv = !getter(); enableSetter(nv); ApplyLimitBtnStyle(btn, nv); };
            btnToggle = btn;
            leftSp.Children.Add(btn);
            Grid.SetColumn(leftSp, 0); row.Children.Add(leftSp);

            var rSp = new StackPanel { HorizontalAlignment = HorizontalAlignment.Right };
            rSp.Children.Add(MakeSpinnerGrid(initAmt.ToString(), out tbAmt, amtSetter));
            Grid.SetColumn(rSp, 1); row.Children.Add(rSp);
            return row;
        }

        // ── Panel insertion helpers ───────────────────────────────────────────
        private void InsertPanel()
        {
            if (_ctPanelActive || _ctTraderGrid == null || _ctScrollViewer == null) return;
            if (_ctTraderGrid.Children.Contains(_ctScrollViewer)) return;
            _ctPanelRow = new RowDefinition
            {
                Height = new GridLength(0),
                MinHeight = 0,
                MaxHeight = 600,
            };
            _ctTraderGrid.RowDefinitions.Add(_ctPanelRow);
            Grid.SetRow(_ctScrollViewer, _ctTraderGrid.RowDefinitions.Count - 1);
            if (_ctTraderGrid.ColumnDefinitions.Count > 0)
                Grid.SetColumnSpan(_ctScrollViewer, _ctTraderGrid.ColumnDefinitions.Count);
            _ctTraderGrid.Children.Add(_ctScrollViewer);
            _ctPanelActive = true;
        }

        private void RemovePanel()
        {
            if (!_ctPanelActive || _ctTraderGrid == null) return;
            if (_ctScrollViewer != null && _ctTraderGrid.Children.Contains(_ctScrollViewer))
            {
                int removedRow = _ctPanelRow == null
                    ? Grid.GetRow(_ctScrollViewer)
                    : _ctTraderGrid.RowDefinitions.IndexOf(_ctPanelRow);
                _ctTraderGrid.Children.Remove(_ctScrollViewer);
                if (_ctPanelRow != null)
                    _ctTraderGrid.RowDefinitions.Remove(_ctPanelRow);

                if (removedRow >= 0)
                {
                    foreach (UIElement child in _ctTraderGrid.Children)
                    {
                        int row = Grid.GetRow(child);
                        if (row > removedRow)
                            Grid.SetRow(child, row - 1);
                    }
                }
            }
            _ctPanelRow = null;
            _ctPanelActive = false;
        }

        private void OnPanelPreviewMouseWheel(object sender, MouseWheelEventArgs e)
        {
            if (_ctScrollViewer == null || e.Delta == 0) return;
            double current = _ctScrollViewer.VerticalOffset;
            double target = Math.Max(0, Math.Min(
                _ctScrollViewer.ScrollableHeight, current - e.Delta));
            if (Math.Abs(target - current) < 0.5) return;
            _ctScrollViewer.ScrollToVerticalOffset(target);
            e.Handled = true;
        }

        private void OnCTTabChanged(object sender, SelectionChangedEventArgs e)
        {
            ApplyPanelVisibilityAndMode();
        }

        // Chart Trader's grid is shared by every tab. Each strategy owns one
        // row, so inactive tabs must collapse both content and RowDefinition.
        private void ApplyPanelVisibilityAndMode()
        {
            if (_ctScrollViewer == null) return;

            bool selected = IsCTTabSelected();
            bool compact  = PlaybackFastMode;

            _ctScrollViewer.Visibility = selected ? Visibility.Visible : Visibility.Collapsed;
            _ctScrollViewer.VerticalScrollBarVisibility = compact
                ? ScrollBarVisibility.Hidden
                : ScrollBarVisibility.Visible;
            _ctScrollViewer.MaxHeight = compact ? 80 : 600;

            if (_strategyPanelBody != null)
                _strategyPanelBody.Visibility = compact
                    ? Visibility.Collapsed
                    : Visibility.Visible;

            if (_ctPanelRow != null)
            {
                if (!selected)
                {
                    _ctPanelRow.MinHeight = 0;
                    _ctPanelRow.MaxHeight = 0;
                    _ctPanelRow.Height = new GridLength(0);
                }
                else if (compact)
                {
                    _ctPanelRow.MinHeight = 0;
                    _ctPanelRow.MaxHeight = 80;
                    _ctPanelRow.Height = GridLength.Auto;
                }
                else
                {
                    _ctPanelRow.MaxHeight = 600;
                    _ctPanelRow.MinHeight = 160;
                    _ctPanelRow.Height = new GridLength(1, GridUnitType.Star);
                }
            }

            bool refreshPanel = selected && !compact;
            if (_livePanelRefreshTimer != null)
            {
                if (refreshPanel && !_livePanelRefreshTimer.IsEnabled)
                    _livePanelRefreshTimer.Start();
                else if (!refreshPanel && _livePanelRefreshTimer.IsEnabled)
                    _livePanelRefreshTimer.Stop();
            }

            if (refreshPanel)
                _livePanelRefreshDirty = true;
        }

        private void ApplyPlaybackFastModePanelState()
        {
            ApplyPanelVisibilityAndMode();
        }

        private bool IsCTTabSelected()
        {
            if (_ctChart == null) return false;
            try
            {
                foreach (System.Windows.Controls.TabItem tab in _ctChart.MainTabControl.Items)
                    if ((tab.Content as NinjaTrader.Gui.Chart.ChartTab)?.ChartControl == ChartControl
                        && tab == _ctChart.MainTabControl.SelectedItem)
                        return true;
            }
            catch { return true; }
            return false;
        }
    }
}
