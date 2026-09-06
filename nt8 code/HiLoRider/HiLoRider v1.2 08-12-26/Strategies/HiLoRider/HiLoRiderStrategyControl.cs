// file name = HiLoRiderStrategyControl.cs
// HiLoRider — Strategy ON/OFF + direction toggles.

#region Using declarations
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using NinjaTrader.NinjaScript.Strategies;
#endregion

namespace NinjaTrader.NinjaScript.Strategies.HiLoRider
{
    public partial class HiLoRider : Strategy
    {
        // Automated entries are OFF by default. The full strategy panel can
        // disable/re-enable them without touching the NinjaTrader activation
        // state or any protection logic.
        internal bool strategyEnabled    = false;
        internal bool _strategyWasEnabledBeforeManualTrade = false;
        internal bool longEnabled        = true;
        internal bool shortEnabled       = true;
        internal void SetStrategyEnabled(bool value)
        {
            strategyEnabled = value;
            if (value) _strategyLockedByManualTrade = false;
            ChartControl?.Dispatcher.InvokeAsync(() => UpdateStrategyControlButtons());
        }

        internal void SetLongEnabled(bool value)
        {
            longEnabled = value;
            ChartControl?.Dispatcher.InvokeAsync(() => UpdateStrategyControlButtons());
        }

        internal void SetShortEnabled(bool value)
        {
            shortEnabled = value;
            ChartControl?.Dispatcher.InvokeAsync(() => UpdateStrategyControlButtons());
        }

        internal void UpdateStrategyControlButtons()
        {
            if (_btnStrategy != null && strategyEnabled)
            {
                _btnStrategy.Content         = "STRATEGY  ON";
                _btnStrategy.Background      = UiBrush(Color.FromArgb(60, 0, 212, 160));
                _btnStrategy.Foreground      = UiBrush(C_GREEN);
                _btnStrategy.BorderBrush     = UiBrush(C_GREEN);
                _btnStrategy.BorderThickness = new Thickness(2);
            }
            else if (_btnStrategy != null)
            {
                _btnStrategy.Content         = "STRATEGY  OFF";
                _btnStrategy.Background      = UiBrush(C_DIM);
                _btnStrategy.Foreground      = UiBrush(C_MUTED);
                _btnStrategy.BorderBrush     = UiBrush(C_BORDER);
                _btnStrategy.BorderThickness = new Thickness(1);
            }

            if (_btnLong == null || _btnShort == null) return;

            if (longEnabled)
            {
                _btnLong.Content         = "LONG  ▲";
                _btnLong.Background      = UiBrush(Color.FromArgb(60, 59, 130, 246));
                _btnLong.Foreground      = UiBrush(C_BLUE);
                _btnLong.BorderBrush     = UiBrush(C_BLUE);
                _btnLong.BorderThickness = new Thickness(1);
            }
            else
            {
                _btnLong.Content         = "LONG  OFF";
                _btnLong.Background      = UiBrush(C_DIM);
                _btnLong.Foreground      = UiBrush(C_MUTED);
                _btnLong.BorderBrush     = UiBrush(C_BORDER);
                _btnLong.BorderThickness = new Thickness(1);
            }

            if (shortEnabled)
            {
                _btnShort.Content         = "SHORT  ▼";
                _btnShort.Background      = UiBrush(Color.FromArgb(60, 255, 77, 106));
                _btnShort.Foreground      = UiBrush(C_RED);
                _btnShort.BorderBrush     = UiBrush(C_RED);
                _btnShort.BorderThickness = new Thickness(1);
            }
            else
            {
                _btnShort.Content         = "SHORT  OFF";
                _btnShort.Background      = UiBrush(C_DIM);
                _btnShort.Foreground      = UiBrush(C_MUTED);
                _btnShort.BorderBrush     = UiBrush(C_BORDER);
                _btnShort.BorderThickness = new Thickness(1);
            }

        }
    }
}
