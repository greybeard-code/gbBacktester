// file name = HiLoRiderHUD.cs
// HiLoRider — floating text HUD window (dark terminal style, MomoMaster-derived).
//
// Sections:
//   HEADER          — instrument, bar type, chart number, ET time
//   SESSION P&L     — realized, unrealized, W/L record, MFE/MAE, daily limits
//   HILOBANDS CHANNEL — upper/mid/lower band, last signal, signal state
//   AUTO EXIT       — current mode, threshold, status
//   ACTIVE TRADE    — direction, entry, stop, P&L, MFE/MAE, trail stage, breakeven
//   NEXT TRADE SETUP — signal status, stop mode, BE, cooldown
//   TRAIL CONFIG    — current mode + stage parameters
//   MACHINE LEARNING — ML status, features

using System;
using System.Collections.Generic;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Media;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
using NinjaTrader.NinjaScript;

namespace NinjaTrader.NinjaScript.Strategies.HiLoRider
{
    public partial class HiLoRider : Strategy
    {
        // ── Window state ──────────────────────────────────────────────────────
        private Window    _textHudWindow;
        private TextBlock _textHudBlock;
        private bool      _textHudBuilt;
        private volatile bool _textHudRefreshPending = false;
        private DateTime _nextTextHudRefreshUtc = DateTime.MinValue;
        private static readonly TimeSpan TextHudRefreshInterval = TimeSpan.FromSeconds(1);

        private double      _textHudLeft   = double.NaN;
        private double      _textHudTop    = double.NaN;
        private double      _textHudWidth  = 440;
        private double      _textHudHeight = 920;
        private WindowState _textHudState  = WindowState.Normal;

        private Button _btnShowTextHud;
        private Button _btnPlaybackFastMode;
        private System.ComponentModel.CancelEventHandler _textHudClosingHandler;

        // ── Enable/disable toggle (Aug 2026) ────────────────────────────────────
        // Was unconditionally built+shown for every strategy instance, then
        // dispatched a ~250-Run WPF TextBlock rebuild to the UI thread on every
        // realtime bar/tick (DispatchTextHudRefresh() below) -- even while
        // minimized (no WindowState check there at all). Real, continuous cost
        // across a fleet routinely running many bot instances at once. Default
        // OFF; toggle live via the TEXT HUD ON/OFF panel button -- no restart
        // needed either direction. Fully-qualified attributes -- this file may
        // not import System.ComponentModel.DataAnnotations.
        [NinjaTrader.NinjaScript.NinjaScriptProperty]
        [System.ComponentModel.DataAnnotations.Display(Name = "Enable Text HUD", Order = 1,
            GroupName = "16. Text HUD",
            Description = "Build/show the floating Text HUD window. OFF by default -- it was a " +
                          "real, continuous performance cost across a multi-bot fleet. Toggle " +
                          "live via the TEXT HUD ON/OFF panel button.")]
        public bool EnableTextHud { get; set; }

        private string _cachedTextHudBarType  = null;
        private string _cachedTextHudChartNum = null;

        // ── Colour palette ────────────────────────────────────────────────────
        private static Color THC_PANEL  => Color.FromRgb(13,  15,  20);
        private static Color THC_CARD   => Color.FromRgb(19,  22,  30);
        private static Color THC_BORDER => Color.FromRgb(30,  35,  48);
        private static Color THC_GREEN  => Color.FromRgb(0,   212, 160);
        private static Color THC_RED    => Color.FromRgb(255,  77, 106);
        private static Color THC_AMBER  => Color.FromRgb(245, 158,  11);
        private static Color THC_BLUE   => Color.FromRgb(59,  130, 246);
        private static Color THC_CYAN   => Color.FromRgb(40,  210, 230);
        private static Color THC_PURPLE => Color.FromRgb(167, 105, 255);
        private static Color THC_TEXT   => Color.FromRgb(232, 234, 240);
        private static Color THC_MUTED  => Color.FromRgb(90,   96, 112);
        private static Color THC_DIM    => Color.FromRgb(40,   45,  58);

        // ─────────────────────────────────────────────────────────────────────
        protected void BuildTextHudWindow()
        {
            try
            {
            if (PlaybackFastMode || _textHudBuilt) return;

            _textHudBlock = new TextBlock
            {
                FontFamily   = new FontFamily("Consolas, Courier New"),
                FontSize     = 13,
                Foreground   = UiBrush(THC_TEXT),
                Background   = UiBrush(THC_PANEL),
                TextWrapping = TextWrapping.NoWrap,
                Padding      = new Thickness(12, 10, 12, 10),
            };
            _textHudBlock.Inlines.Add(new Run("Initializing…") { Foreground = UiBrush(THC_MUTED) });

            var scroll = new ScrollViewer
            {
                Content                       = _textHudBlock,
                VerticalScrollBarVisibility   = ScrollBarVisibility.Auto,
                HorizontalScrollBarVisibility = ScrollBarVisibility.Auto,
                Background                    = UiBrush(THC_PANEL),
            };

            _textHudWindow = new Window
            {
                Title         = "HI LO RIDER  |  Text HUD",
                Content       = scroll,
                Background    = UiBrush(THC_PANEL),
                WindowStyle   = WindowStyle.ToolWindow,
                ResizeMode    = ResizeMode.CanResize,
                SizeToContent = SizeToContent.Manual,
                Width         = _textHudWidth,
                Height        = _textHudHeight,
                MinWidth      = 340,
                MinHeight     = 200,
                ShowInTaskbar = true,
                Topmost       = false,
            };

            if (!double.IsNaN(_textHudLeft) && !double.IsNaN(_textHudTop))
            {
                _textHudWindow.WindowStartupLocation = WindowStartupLocation.Manual;
                _textHudWindow.Left = _textHudLeft;
                _textHudWindow.Top  = _textHudTop;
            }
            else
            {
                _textHudWindow.WindowStartupLocation = WindowStartupLocation.CenterScreen;
            }
            _textHudWindow.WindowState = _textHudState;
            _textHudClosingHandler = (s, e) =>
            {
                e.Cancel = true;
                SaveTextHudGeometry();
                var window = s as Window;
                if (window != null) window.WindowState = WindowState.Minimized;
            };
            _textHudWindow.Closing     += _textHudClosingHandler;
            _textHudWindow.Deactivated += (s, e) => { SaveTextHudGeometry(); _textHudWindow.WindowState = WindowState.Minimized; };
            _textHudWindow.LocationChanged += (s, e) => SaveTextHudGeometry();
            _textHudWindow.SizeChanged     += (s, e) => SaveTextHudGeometry();
            _textHudWindow.StateChanged    += (s, e) => SaveTextHudGeometry();
            _textHudWindow.Show();

            _textHudBuilt = true;
            RefreshTextHudToggleButtonStyle();
            }
            catch (Exception ex)
            {
                try { Print($"[HiLoRider] BuildTextHudWindow: {ex.Message}"); } catch { }
            }
        }

        // The toggle belongs to the original strategy panel, so closing the
        // floating window never removes or rebuilds Chart Trader rows.
        protected void TearDownTextHudWindow(bool removeButton = true)
        {
            try
            {
                var window = _textHudWindow;
                if (window != null)
                {
                    SaveTextHudGeometry();
                    if (_textHudClosingHandler != null)
                        window.Closing -= _textHudClosingHandler;
                    _textHudClosingHandler = null;
                    window.Close();
                    _textHudWindow = null;
                }
                _textHudBuilt = false;
                RefreshTextHudToggleButtonStyle();
            }
            catch (Exception ex)
            {
                _textHudBuilt = false;
                _textHudRefreshPending = false;
                try { Print($"[HiLoRider] TearDownTextHudWindow: {ex.Message}"); } catch { }
            }
        }

        private void SetPlaybackFastModeFromUi(bool enabled)
        {
            try
            {
            if (PlaybackFastMode == enabled) return;

            PlaybackFastMode = enabled;

            // Indicator drawing objects belong to the NinjaScript thread.
            TriggerCustomEvent(o =>
            {
                try { hiloInd?.SetPlaybackFastMode(enabled, enabled); }
                catch (Exception ex) { Print($"[HiLoRider] Fast Mode visual switch: {ex.Message}"); }
            }, null);

            if (enabled)
            {
                EnableTextHud = false;
                TearDownTextHudWindow(removeButton: false);
            }

            if (EnableChartUI)
            {
                if (!uiPanelActive)
                    CreateWPFControls();
                ApplyPlaybackFastModePanelState();
            }

            RefreshPlaybackFastModeButtonStyle();
            RefreshTextHudToggleButtonStyle();
            Print($"[HiLoRider] Playback Fast Mode {(enabled ? "ON" : "OFF")} (live switch; strategy remains enabled).");
            }
            catch (Exception ex)
            {
                try { Print($"[HiLoRider] Fast Mode toggle: {ex.Message}"); } catch { }
            }
        }

        private void RefreshPlaybackFastModeButtonStyle()
        {
            if (_btnPlaybackFastMode == null) return;
            bool on = PlaybackFastMode;
            _btnPlaybackFastMode.Content     = on ? "FAST MODE ON" : "FAST MODE OFF";
            _btnPlaybackFastMode.Background  = UiBrush(on ? Color.FromRgb(20, 70, 45) : Color.FromRgb(45, 45, 50));
            _btnPlaybackFastMode.Foreground  = UiBrush(on ? THC_GREEN : THC_TEXT);
            _btnPlaybackFastMode.BorderBrush = UiBrush(on ? THC_GREEN : THC_MUTED);
            _btnPlaybackFastMode.ToolTip = on
                ? "Playback Fast Mode is ON. Click to restore the full strategy panel and visuals without disabling the strategy."
                : "Click to speed up Playback without disabling the strategy. Collapses controls, suppresses strategy visuals, and skips redundant flat intrabar work.";
        }

        private void RefreshTextHudToggleButtonStyle()
        {
            if (_btnShowTextHud == null) return;
            bool on = _textHudWindow != null;
            _btnShowTextHud.IsEnabled       = !PlaybackFastMode;
            _btnShowTextHud.Content         = on ? "HUD ON" : "HUD OFF";
            _btnShowTextHud.Background      = UiBrush(on ? Color.FromArgb(40, 40, 210, 230) : Color.FromRgb(50, 20, 20));
            _btnShowTextHud.Foreground      = UiBrush(on ? THC_CYAN : THC_RED);
            _btnShowTextHud.BorderBrush     = UiBrush(on ? THC_CYAN : THC_RED);
            _btnShowTextHud.ToolTip         = on
                ? "Text HUD is ON. Click to disable it (closes the window -- it costs a WPF " +
                  "refresh dispatch on every realtime bar/tick while enabled)."
                : "Text HUD is OFF (default -- was a real, continuous performance cost across " +
                  "a multi-bot fleet). Click to enable and open it.";
        }

        private void SaveTextHudGeometry()
        {
            if (_textHudWindow == null) return;
            _textHudState = _textHudWindow.WindowState;
            if (_textHudWindow.WindowState == WindowState.Normal)
            {
                _textHudLeft   = _textHudWindow.Left;
                _textHudTop    = _textHudWindow.Top;
                _textHudWidth  = _textHudWindow.Width;
                _textHudHeight = _textHudWindow.Height;
            }
        }

        protected void DispatchTextHudRefresh()
        {
            try
            {
                var window = _textHudWindow;
                if (!_textHudBuilt || window == null || State != State.Realtime) return;
                DateTime nowUtc = DateTime.UtcNow;
                if (nowUtc < _nextTextHudRefreshUtc || _textHudRefreshPending) return;
                var dispatcher = ChartControl?.Dispatcher;
                if (dispatcher == null) return;
                _nextTextHudRefreshUtc = nowUtc + TextHudRefreshInterval;
                _textHudRefreshPending = true;
                dispatcher.BeginInvoke(
                    System.Windows.Threading.DispatcherPriority.Background,
                    new Action(() =>
                    {
                        try
                        {
                            var current = _textHudWindow;
                            if (!_textHudBuilt || current == null
                                || !ReferenceEquals(current, window)
                                || !window.IsVisible
                                || window.WindowState == WindowState.Minimized)
                                return;
                            RefreshTextHudWindow();
                        }
                        catch (Exception ex)
                        {
                            try { Print($"[HiLoRider] RefreshTextHudWindow: {ex.Message}"); } catch { }
                        }
                        finally { _textHudRefreshPending = false; }
                    }));
            }
            catch (Exception ex)
            {
                _textHudRefreshPending = false;
                try { Print($"[HiLoRider] DispatchTextHudRefresh: {ex.Message}"); } catch { }
            }
        }

        // ─────────────────────────────────────────────────────────────────────
        private void RefreshTextHudWindow()
        {
            if (_textHudBlock == null) return;
            var runs = new List<Inline>(250);

            void Add(string text, Color color, bool bold = false)
            {
                var r = new Run(text) { Foreground = UiBrush(color) };
                if (bold) r.FontWeight = FontWeights.Bold;
                runs.Add(r);
            }
            void NL() => runs.Add(new LineBreak());
            void AddLn(string text, Color color, bool bold = false) { Add(text, color, bold); NL(); }
            void KV(string key, string val, Color valColor) { Add(key, THC_MUTED); AddLn(val, valColor); }
            void Div() => AddLn(new string('─', 40), THC_DIM);
            void SectionHeader(string title) { Div(); Add("── ", THC_DIM); Add(title, THC_MUTED, bold: true); NL(); }

            try
            {
                // ── HEADER ────────────────────────────────────────────────────
                string account  = Account?.Name ?? "Backtest";
                string instr    = Instrument?.FullName ?? "---";
                string barType  = GetTextHudBarTypeDisplay();
                string chartNum = GetTextHudChartNumber();
                string timeET   = GetCurrentETTimeString();

                Add("HI LO RIDER", THC_RED, bold: true);
                Add("  |  ", THC_DIM);
                AddLn(account, THC_TEXT, bold: true);
                Add(instr, THC_CYAN);
                Add("  |  ", THC_DIM);
                Add(barType, THC_MUTED);
                if (!string.IsNullOrEmpty(chartNum))
                { Add("  [", THC_DIM); Add(chartNum, THC_MUTED); Add("]", THC_DIM); }
                Add("  |  ", THC_DIM);
                Add(timeET, THC_TEXT);
                AddLn(" ET", THC_MUTED);

                // ── SESSION P&L ───────────────────────────────────────────────
                double realized   = GetDailyLimitRealizedPnl();
                double unrealized = GetDailyLimitUnrealizedPnl();
                double totalPnL   = realized + unrealized;
                double nav        = 0;
                try { nav = Account?.Get(AccountItem.NetLiquidation, Currency.UsDollar) ?? 0; } catch { }
                int    totalTrades = hudWins + hudLosses;
                double winRate     = totalTrades > 0 ? (double)hudWins / totalTrades : 0;
                bool   lossHit     = EnableDailyLossLimit && DailyLossLimit > 0 && totalPnL <= -DailyLossLimit;
                bool   profitHit   = EnableDailyProfitLimit && DailyProfitLimit > 0 && totalPnL >= DailyProfitLimit;
                int coolRemain     = (CooldownBars > 0 && lastExitBar >= 0)
                                   ? System.Math.Max(0, CooldownBars - (CurrentBar - lastExitBar)) : 0;

                SectionHeader("SESSION P&L");
                KV("Account NAV:   ", nav.ToString("C"), nav >= 0 ? THC_GREEN : THC_RED);
                KV("Realized:      ", realized.ToString("C"), realized >= 0 ? THC_GREEN : THC_RED);
                KV("Unrealized:    ", unrealized.ToString("C"), unrealized >= 0 ? THC_GREEN : THC_RED);
                Add("Total:         ", THC_MUTED);
                Add(totalPnL.ToString("C"), totalPnL >= 0 ? THC_GREEN : THC_RED, bold: true);
                if (lossHit)   Add("  ⛔ LOSS LIMIT",   THC_RED,   bold: true);
                if (profitHit) Add("  ⛔ PROFIT LIMIT", THC_GREEN, bold: true);
                NL();
                if (EnableDailyLossLimit && DailyLossLimit > 0)
                {
                    double rem = System.Math.Max(0, DailyLossLimit + System.Math.Min(0, totalPnL));
                    KV("Loss Limit:    ",
                        lossHit ? $"${DailyLossLimit:F0}  HIT" : $"${DailyLossLimit:F0}  (${rem:F0} left)",
                        lossHit ? THC_RED : THC_MUTED);
                }
                if (EnableDailyProfitLimit && DailyProfitLimit > 0)
                {
                    double rem = System.Math.Max(0, DailyProfitLimit - System.Math.Max(0, totalPnL));
                    KV("Profit Limit:  ",
                        profitHit ? $"${DailyProfitLimit:F0}  HIT" : $"${DailyProfitLimit:F0}  (${rem:F0} left)",
                        profitHit ? THC_GREEN : THC_MUTED);
                }
                Add("Record:        ", THC_MUTED);
                Add($"{hudWins}W", THC_GREEN, bold: true);
                Add(" / ", THC_DIM);
                Add($"{hudLosses}L", THC_RED, bold: true);
                Add($"  ({totalTrades} trades)", THC_MUTED);
                if (totalTrades > 0) Add($"  {winRate:P0}", THC_MUTED);
                NL();
                KV("Sess Max MFE:  ", sessionMaxMfeTicks > 0 ? $"{sessionMaxMfeTicks:F0}t" : "---", THC_GREEN);
                KV("Sess Max MAE:  ", sessionMaxMaeTicks > 0 ? $"{sessionMaxMaeTicks:F0}t" : "---", THC_RED);

                // ── HILOBANDS CHANNEL ────────────────────────────────────────────
                SectionHeader("HILOBANDS CHANNEL");
                if (hiloInd != null && hiloInd.MiddleLine.IsValidDataPoint(0))
                {
                    double upperNow = hiloInd.UpperBand[0];
                    double lowerNow = hiloInd.LowerBand[0];
                    double midNow   = hiloInd.MiddleLine[0];
                    double sigNow   = hiloInd.Signal.IsValidDataPoint(0)  ? hiloInd.Signal[0]  : 0;
                    double sig2Now  = hiloInd.Signal2.IsValidDataPoint(0) ? hiloInd.Signal2[0] : 0;

                    KV("Upper Band:    ", $"{upperNow:F2}", THC_RED);
                    KV("Midline:       ", $"{midNow:F2}", THC_TEXT);
                    KV("Lower Band:    ", $"{lowerNow:F2}", THC_GREEN);

                    Add("Slot1 Cross:   ", THC_MUTED);
                    if (sigNow > 0)      AddLn("▲ LONG cross",  THC_GREEN, bold: true);
                    else if (sigNow < 0) AddLn("▼ SHORT cross", THC_RED,   bold: true);
                    else                 AddLn("none this bar", THC_MUTED);

                    Add("Slot2 Slope:   ", THC_MUTED);
                    if (sig2Now > 0)      AddLn("▲ LONG slope",  THC_CYAN,   bold: true);
                    else if (sig2Now < 0) AddLn("▼ SHORT slope", THC_AMBER, bold: true);
                    else                  AddLn("none this bar", THC_MUTED);
                }
                else
                {
                    KV("Status:        ", $"Warming up  (need {LookbackPeriod} bars)", THC_MUTED);
                }

                // Signal state
                Add("Signal State:  ", THC_MUTED);
                Color regimeColor = currentRegime == 2 ? (tradeDir == 1 ? THC_GREEN : THC_RED)
                                  : currentRegime == 1 ? THC_AMBER
                                  : THC_MUTED;
                AddLn(currentRegimeLabel, regimeColor, bold: true);

                // ── AUTO EXIT ─────────────────────────────────────────────────
                SectionHeader("AUTO EXIT");
                string aeMode  = AutoExitMode == HiLoRiderAutoExit.DiffCross ? "DiffCross" : "Disabled";
                Color  aeColor = AutoExitMode == HiLoRiderAutoExit.Disabled  ? THC_MUTED  : THC_CYAN;
                KV("Mode:          ", aeMode, aeColor);
                if (AutoExitMode == HiLoRiderAutoExit.DiffCross)
                {
                    KV("Diff now:      ", cachedDiff.ToString("+0.00;-0.00"), cachedDiff >= 0 ? THC_GREEN : THC_RED);
                }

                // ── ACTIVE TRADE / NEXT SETUP ─────────────────────────────────
                bool isLongPos = Position.MarketPosition == MarketPosition.Long;
                bool inPos     = Position.MarketPosition != MarketPosition.Flat;

                if (inPos && filledPrice > 0)
                {
                    Color  dirColor = isLongPos ? THC_GREEN : THC_RED;
                    string dir      = isLongPos ? "LONG  ▲" : "SHORT ▼";
                    double profitT  = isLongPos
                        ? (Close[0] - filledPrice) / TickSize
                        : (filledPrice - Close[0]) / TickSize;
                    double toStop   = isLongPos
                        ? (Close[0] - stopLevel) / TickSize
                        : (stopLevel - Close[0]) / TickSize;
                    double toTarget = targetLevel > 0
                        ? System.Math.Max(0, isLongPos
                            ? (targetLevel - Close[0]) / TickSize
                            : (Close[0] - targetLevel) / TickSize) : 0;
                    double mfeTrade = isLongPos
                        ? System.Math.Max(0, (hudHighSinceEntry - filledPrice) / TickSize)
                        : System.Math.Max(0, (filledPrice - hudLowSinceEntry)  / TickSize);
                    double maeTrade = isLongPos
                        ? System.Math.Max(0, (filledPrice - hudLowSinceEntry)  / TickSize)
                        : System.Math.Max(0, (hudHighSinceEntry - filledPrice) / TickSize);
                    double unreal   = GetCurrentUnrealized();

                    // Channel-band stop reference (StopMode=ChannelBand, repurposed)
                    double channelStop = (hiloInd != null && hiloInd.UpperBand.IsValidDataPoint(0) && hiloInd.LowerBand.IsValidDataPoint(0))
                        ? (isLongPos ? hiloInd.LowerBand[0] : hiloInd.UpperBand[0])
                        : 0;

                    string stageLabel = TrailMode == RPTrailMode.MidlineOffset
                        ? (entryBarsInTrade < TrailBarsBeforeTrail
                            ? $"Fixed stop  (trail engages bar {TrailBarsBeforeTrail}+)"
                            : $"Midline ±{TrailOffsetTicks}t  (bar {entryBarsInTrade})")
                        : stagedTrailStage == 0 ? "None (initial stop)" :
                        TrailMode == RPTrailMode.FixedStop      ? "BE locked" :
                        TrailMode == RPTrailMode.HighLow
                            ? (stagedTrailStage == 1 ? $"1 – Low/High[{HLStage1LookbackBars}]"
                             : stagedTrailStage == 2 ? $"2 – Low/High[{HLStage2LookbackBars}]"
                             :                         $"3 – Peak ±{HLStage3TrailTicks}t")
                            : (stagedTrailStage == 1 ? $"1 – Wide ({Stage1TrailTicks}t)"
                             : stagedTrailStage == 2 ? $"2 – Medium ({Stage2TrailTicks}t)"
                             :                         $"3 – Tight ({Stage3TrailTicks}t)");

                    SectionHeader("ACTIVE TRADE");
                    Add("Direction:     ", THC_MUTED); AddLn(dir, dirColor, bold: true);
                    KV("Qty:           ", $"{Position.Quantity} ct", THC_TEXT);
                    KV("Entry:         ", $"{filledPrice:F2}", THC_TEXT);
                    if (channelStop > 0)
                        KV("Channel Band:  ", $"stop anchor = {channelStop:F2}", THC_AMBER);
                    Add("Stop:          ", THC_MUTED);
                    Add($"{stopLevel:F2}", THC_RED);
                    AddLn($"  ({toStop:F0}t away)", THC_MUTED);
                    Add("Target:        ", THC_MUTED);
                    if (targetLevel > 0)
                    { Add($"{targetLevel:F2}", THC_GREEN); AddLn($"  ({toTarget:F0}t away)", THC_MUTED); }
                    else AddLn("None  (trail-only)", THC_MUTED);
                    Add("Current P&L:   ", THC_MUTED);
                    Add($"{profitT:+0.#;-0.#}t", profitT >= 0 ? THC_GREEN : THC_RED, bold: true);
                    AddLn($"  ({unreal:C})", profitT >= 0 ? THC_GREEN : THC_RED);
                    KV("MFE:           ", $"{mfeTrade:F0}t", THC_GREEN);
                    KV("MAE:           ", $"{maeTrade:F0}t", THC_RED);
                    KV("Trail Stage:   ", stageLabel, THC_CYAN);
                    Add("Breakeven:     ", THC_MUTED);
                    if (beRealized)
                    { Add("✓ REALIZED", THC_GREEN, bold: true); AddLn($"  stop @ {stopLevel:F2}", THC_MUTED); }
                    else
                    {
                        double beRem = System.Math.Max(0, BE_TriggerTicks - profitT);
                        Add("Pending  ", THC_AMBER);
                        AddLn($"({BE_TriggerTicks}t trigger, {beRem:F0}t remaining)", THC_MUTED);
                    }
                }
                else
                {
                    // ── Next setup ────────────────────────────────────────────
                    bool   budgetUsed = MaxDailyTrades > 0 && _tradesThisSession >= MaxDailyTrades;
                    bool   inWindow   = IsInSessionWindow();
                    string sigStatus;
                    Color  sigColor;
                    if (lossHit)
                    { sigStatus = "Daily loss limit hit"; sigColor = THC_RED; }
                    else if (budgetUsed)
                    { sigStatus = $"Daily limit reached ({_tradesThisSession}/{MaxDailyTrades})"; sigColor = THC_MUTED; }
                    else if (!inWindow)
                    { sigStatus = "Outside entry window"; sigColor = THC_MUTED; }
                    else if (coolRemain > 0)
                    { sigStatus = $"Cooldown  ({coolRemain} bars remaining)"; sigColor = THC_AMBER; }
                    else if (currentRegime == 1)
                    { sigStatus = currentRegimeLabel + "  — awaiting entry bar"; sigColor = THC_AMBER; }
                    else
                    { sigStatus = "READY — watching for crossover or midline slope"; sigColor = THC_GREEN; }

                    string stopModeLabel = StopMode == RPStopMode.ChannelBand ? $"Channel Band ({StopBufferTicks}t buf)"
                        : StopMode == RPStopMode.FixedTick ? $"Fixed ({FixedSLTicks}t)"
                        : StopMode == RPStopMode.ATR       ? $"ATR ({SLATRMultiplier}×)"
                        : $"High/Low ({StopBufferTicks}t buf)";

                    SectionHeader("NEXT TRADE SETUP");
                    Add("Status:        ", THC_MUTED); AddLn(sigStatus, sigColor, bold: true);
                    KV("Signal:        ", $"HiLoBands({LookbackPeriod}) crossover -> slope cascade", THC_TEXT);
                    KV("Stop Mode:     ", stopModeLabel, THC_TEXT);
                    KV("Target Mode:   ", TargetMode == RPTargetMode.NoTarget ? "No Target (trail only)" : $"Fixed ({FixedTPTicks}t)", THC_TEXT);
                    KV("BE Trigger:    ", $"{BE_TriggerTicks}t  (+{BE_OffsetTicks}t offset)", THC_AMBER);
                    KV("Cooldown:      ", $"{CooldownBars} bars  (rem: {coolRemain})", THC_MUTED);
                    KV("Auto Exit:     ", aeMode, aeColor);
                    KV("Trades today:  ", $"{_tradesThisSession}/{(MaxDailyTrades > 0 ? MaxDailyTrades.ToString() : "∞")}", THC_MUTED);
                }

                // ── TRAIL CONFIG ──────────────────────────────────────────────
                SectionHeader("TRAIL CONFIG");
                string trailLabel = TrailMode == RPTrailMode.StagedTrail   ? "Staged Ticks"
                                  : TrailMode == RPTrailMode.HighLow        ? "High/Low Bars"
                                  : TrailMode == RPTrailMode.MidlineOffset  ? "Midline Offset"
                                  : TrailMode == RPTrailMode.FixedStopTrail ? "Fixed Stop Trail"
                                  : TrailMode == RPTrailMode.FixedStop       ? "Fixed Stop (no trail)"
                                  :                                            "ATR Trail";
                Color trailColor  = TrailMode == RPTrailMode.StagedTrail   ? THC_CYAN
                                  : TrailMode == RPTrailMode.MidlineOffset  ? THC_GREEN
                                  : TrailMode == RPTrailMode.ATR            ? THC_PURPLE
                                  : TrailMode == RPTrailMode.FixedStop      ? THC_MUTED
                                  : THC_AMBER;
                KV("Mode:          ", trailLabel, trailColor);
                if (TrailMode == RPTrailMode.HighLow)
                {
                    KV("Stage 1:       ", $"Immediate  |  Low/High[{HLStage1LookbackBars}]", THC_TEXT);
                    KV("Stage 2:       ", $"bar {HLStage2TriggerBars}+  |  Low/High[{HLStage2LookbackBars}] ±TrailMin", THC_TEXT);
                    KV("Stage 3:       ", $"bar {HLStage3TriggerBars}+  |  Peak ±{HLStage3TrailTicks}t", THC_TEXT);
                }
                else if (TrailMode == RPTrailMode.MidlineOffset)
                {
                    KV("Before bar:    ", $"Fixed stop  (bars 0-{TrailBarsBeforeTrail - 1})", THC_TEXT);
                    KV("From bar:      ", $"{TrailBarsBeforeTrail}+  |  midline ±{TrailOffsetTicks}t (tighten-only)", THC_TEXT);
                }
                else if (TrailMode == RPTrailMode.StagedTrail)
                {
                    KV("Stage 1:       ", $"After BE  |  trail {Stage1TrailTicks}t  (act {Stage1ActivationTicks}t)", THC_TEXT);
                    KV("Stage 2:       ", $"{Stage2TriggerPct}% of target  |  trail {Stage2TrailTicks}t", THC_TEXT);
                    KV("Stage 3:       ", $"{Stage3TriggerPct}% of target  |  trail {Stage3TrailTicks}t", THC_TEXT);
                }
                else if (TrailMode == RPTrailMode.ATR)
                {
                    KV("Stage 1:       ", $"After BE  |  {ATRTrailStage1Mult:F2}× ATR  (act {Stage1ActivationTicks}t)", THC_TEXT);
                    KV("Stage 2:       ", $"{Stage2TriggerPct}%  |  {ATRTrailStage2Mult:F2}× ATR", THC_TEXT);
                    KV("Stage 3:       ", $"{Stage3TriggerPct}%  |  {ATRTrailStage3Mult:F2}× ATR", THC_TEXT);
                    KV("Stage 4:       ", $"{ATRTrailStage4TriggerPct}%  |  {ATRTrailStage4Mult:F2}× ATR", THC_TEXT);
                    KV("ATR now:       ", cachedATRForTrail > 0 ? $"{cachedATRForTrail / TickSize:F1}t" : "warming up",
                        cachedATRForTrail > 0 ? THC_TEXT : THC_MUTED);
                }
                KV("Current Stage: ", stagedTrailStage == 0 ? "None" : stagedTrailStage.ToString(),
                    stagedTrailStage == 0 ? THC_MUTED : THC_CYAN);

                // ── MACHINE LEARNING ──────────────────────────────────────────
                SectionHeader("MACHINE LEARNING");
                string mlStatus = GetMLStatusString();
                double mlProb   = GetMLWinProbability();
                Color  mlColor  = !EnableML ? THC_MUTED
                                : _mlSampleCount < MLMinSamples ? THC_AMBER
                                : _mlLastBlocked ? THC_RED : THC_GREEN;
                Add("Status:        ", THC_MUTED); AddLn(mlStatus, mlColor, bold: true);
                if (EnableML && mlProb >= 0)
                {
                    KV("Threshold:     ", $"{MLThreshold:F2}", THC_MUTED);
                    Add("Last p(win):   ", THC_MUTED);
                    AddLn($"{mlProb:F3}", mlProb >= MLThreshold ? THC_GREEN : THC_RED, bold: true);
                }
            }
            catch (Exception ex)
            {
                runs.Clear();
                runs.Add(new Run($"HUD error: {ex.Message}") { Foreground = UiBrush(THC_RED) });
            }

            _textHudBlock.Inlines.Clear();
            foreach (var r in runs) _textHudBlock.Inlines.Add(r);
        }

        // ── Bar type / chart number helpers (cached) ──────────────────────────
        private string GetTextHudBarTypeDisplay()
        {
            if (_cachedTextHudBarType != null) return _cachedTextHudBarType;
            try
            {
                string ds = $"{Bars?.BarsType?.Name ?? "---"} {Bars?.BarsPeriod?.Value}";
                return _cachedTextHudBarType = ds;
            }
            catch { return _cachedTextHudBarType = "---"; }
        }

        private string GetTextHudChartNumber()
        {
            if (_cachedTextHudChartNum != null) return _cachedTextHudChartNum;
            try
            {
                if (ChartControl == null) return _cachedTextHudChartNum = "";
                var win = Window.GetWindow(ChartControl.Parent);
                if (win == null) return _cachedTextHudChartNum = "";
                string title = win.Title ?? "";
                var m = System.Text.RegularExpressions.Regex.Match(title, @"^(Chart\s*\d+)",
                    System.Text.RegularExpressions.RegexOptions.IgnoreCase);
                return _cachedTextHudChartNum = m.Success ? m.Value : "";
            }
            catch { return _cachedTextHudChartNum = ""; }
        }
    }
}
