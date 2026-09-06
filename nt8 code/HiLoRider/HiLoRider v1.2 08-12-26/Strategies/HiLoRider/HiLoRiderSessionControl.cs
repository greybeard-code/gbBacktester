// file name = HiLoRiderSessionControl.cs
// HiLoRider — 4-window time control (TF1–TF4).

#region Using declarations
using System;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Xml.Serialization;
using NinjaTrader.Gui;
using NinjaTrader.NinjaScript.Strategies;
#endregion

namespace NinjaTrader.NinjaScript.Strategies.HiLoRider
{
    public partial class HiLoRider : Strategy
    {
        private bool _enableTF2 = false;
        private bool _enableTF3 = false;
        private bool _enableTF4 = false;
        private bool _enableTF5 = false;
        private bool _enableTF6 = false;
        private bool _enableTF7 = false;

        internal bool IsInSessionWindow()
        {
            TimeSpan now = TimeZoneInfo.ConvertTimeFromUtc(
                Time[0].ToUniversalTime(), EasternTZ).TimeOfDay;
            if (IsTimeInWindow(now, TF1Start.TimeOfDay, TF1End.TimeOfDay)) return true;
            if (_enableTF2 && IsTimeInWindow(now, TF2Start.TimeOfDay, TF2End.TimeOfDay)) return true;
            if (_enableTF3 && IsTimeInWindow(now, TF3Start.TimeOfDay, TF3End.TimeOfDay)) return true;
            if (_enableTF4 && IsTimeInWindow(now, TF4Start.TimeOfDay, TF4End.TimeOfDay)) return true;
            if (_enableTF5 && IsTimeInWindow(now, TF5Start.TimeOfDay, TF5End.TimeOfDay)) return true;
            if (_enableTF6 && IsTimeInWindow(now, TF6Start.TimeOfDay, TF6End.TimeOfDay)) return true;
            if (_enableTF7 && IsTimeInWindow(now, TF7Start.TimeOfDay, TF7End.TimeOfDay)) return true;
            return false;
        }

        private static bool IsTimeInWindow(TimeSpan now, TimeSpan start, TimeSpan end)
        {
            if (start == end) return false;
            if (start > end) return now >= start || now <= end;  // overnight wrap
            return now >= start && now <= end;
        }

        // ── Properties ────────────────────────────────────────────────────────
        [NinjaScriptProperty]
        [PropertyEditor("NinjaTrader.Gui.Tools.TimeEditorKey")]
        [Display(Name = "TF1 Start  (RTH Morning)", Order = 1, GroupName = "12. Session & Time Control",
            Description = "Start of Time Frame 1 — always active. Default 09:30 ET.")]
        public DateTime TF1Start { get; set; }

        [NinjaScriptProperty]
        [PropertyEditor("NinjaTrader.Gui.Tools.TimeEditorKey")]
        [Display(Name = "TF1 End    (RTH Morning)", Order = 2, GroupName = "12. Session & Time Control",
            Description = "End of Time Frame 1. Default 16:00 ET.")]
        public DateTime TF1End { get; set; }

        [NinjaScriptProperty]
        [RefreshProperties(RefreshProperties.All)]
        [Display(Name = "Enable TF2 (Midday / PM)", Order = 3, GroupName = "12. Session & Time Control",
            Description = "Enable Time Frame 2 — Midday / PM session.")]
        public bool EnableTF2
        {
            get => _enableTF2;
            set => _enableTF2 = value;
        }

        [NinjaScriptProperty]
        [PropertyEditor("NinjaTrader.Gui.Tools.TimeEditorKey")]
        [Display(Name = "TF2 Start  (Midday / PM)", Order = 4, GroupName = "12. Session & Time Control")]
        public DateTime TF2Start { get; set; }

        [NinjaScriptProperty]
        [PropertyEditor("NinjaTrader.Gui.Tools.TimeEditorKey")]
        [Display(Name = "TF2 End    (Midday / PM)", Order = 5, GroupName = "12. Session & Time Control")]
        public DateTime TF2End { get; set; }

        [NinjaScriptProperty]
        [RefreshProperties(RefreshProperties.All)]
        [Display(Name = "Enable TF3 (Overnight / Pre-NY)", Order = 6, GroupName = "12. Session & Time Control",
            Description = "Enable Time Frame 3 — Overnight / Pre-NY. Wraps midnight if Start > End.")]
        public bool EnableTF3
        {
            get => _enableTF3;
            set => _enableTF3 = value;
        }

        [NinjaScriptProperty]
        [PropertyEditor("NinjaTrader.Gui.Tools.TimeEditorKey")]
        [Display(Name = "TF3 Start  (Overnight / Pre-NY)", Order = 7, GroupName = "12. Session & Time Control")]
        public DateTime TF3Start { get; set; }

        [NinjaScriptProperty]
        [PropertyEditor("NinjaTrader.Gui.Tools.TimeEditorKey")]
        [Display(Name = "TF3 End    (Overnight / Pre-NY)", Order = 8, GroupName = "12. Session & Time Control")]
        public DateTime TF3End { get; set; }

        [NinjaScriptProperty]
        [RefreshProperties(RefreshProperties.All)]
        [Display(Name = "Enable TF4 (All Day)", Order = 9, GroupName = "12. Session & Time Control",
            Description = "Enable Time Frame 4 — master-open override. 00:00–23:59 opens every bar.")]
        public bool EnableTF4
        {
            get => _enableTF4;
            set => _enableTF4 = value;
        }

        [NinjaScriptProperty]
        [PropertyEditor("NinjaTrader.Gui.Tools.TimeEditorKey")]
        [Display(Name = "TF4 Start  (All Day)", Order = 10, GroupName = "12. Session & Time Control")]
        public DateTime TF4Start { get; set; }

        [NinjaScriptProperty]
        [PropertyEditor("NinjaTrader.Gui.Tools.TimeEditorKey")]
        [Display(Name = "TF4 End    (All Day)", Order = 11, GroupName = "12. Session & Time Control")]
        public DateTime TF4End { get; set; }

        [NinjaScriptProperty]
        [RefreshProperties(RefreshProperties.All)]
        [Display(Name = "Enable TF5 (Midday I)", Order = 12, GroupName = "12. Session & Time Control",
            Description = "Enable Time Frame 5 — Midday I (optimized 11:00–13:00 ET, OOS Sharpe 1.59).")]
        public bool EnableTF5
        {
            get => _enableTF5;
            set => _enableTF5 = value;
        }

        [NinjaScriptProperty]
        [PropertyEditor("NinjaTrader.Gui.Tools.TimeEditorKey")]
        [Display(Name = "TF5 Start  (Midday I)", Order = 13, GroupName = "12. Session & Time Control")]
        public DateTime TF5Start { get; set; }

        [NinjaScriptProperty]
        [PropertyEditor("NinjaTrader.Gui.Tools.TimeEditorKey")]
        [Display(Name = "TF5 End    (Midday I)", Order = 14, GroupName = "12. Session & Time Control")]
        public DateTime TF5End { get; set; }

        [NinjaScriptProperty]
        [RefreshProperties(RefreshProperties.All)]
        [Display(Name = "Enable TF6 (Early Globex)", Order = 15, GroupName = "12. Session & Time Control",
            Description = "Enable Time Frame 6 — Early Globex (optimized 03:00–05:00 ET, OOS Sharpe 2.59).")]
        public bool EnableTF6
        {
            get => _enableTF6;
            set => _enableTF6 = value;
        }

        [NinjaScriptProperty]
        [PropertyEditor("NinjaTrader.Gui.Tools.TimeEditorKey")]
        [Display(Name = "TF6 Start  (Early Globex)", Order = 16, GroupName = "12. Session & Time Control")]
        public DateTime TF6Start { get; set; }

        [NinjaScriptProperty]
        [PropertyEditor("NinjaTrader.Gui.Tools.TimeEditorKey")]
        [Display(Name = "TF6 End    (Early Globex)", Order = 17, GroupName = "12. Session & Time Control")]
        public DateTime TF6End { get; set; }

        [NinjaScriptProperty]
        [RefreshProperties(RefreshProperties.All)]
        [Display(Name = "Enable TF7 (Midday II)", Order = 18, GroupName = "12. Session & Time Control",
            Description = "Enable Time Frame 7 — Midday II (optimized 13:00–15:00 ET, OOS Sharpe 1.85).")]
        public bool EnableTF7
        {
            get => _enableTF7;
            set => _enableTF7 = value;
        }

        [NinjaScriptProperty]
        [PropertyEditor("NinjaTrader.Gui.Tools.TimeEditorKey")]
        [Display(Name = "TF7 Start  (Midday II)", Order = 19, GroupName = "12. Session & Time Control")]
        public DateTime TF7Start { get; set; }

        [NinjaScriptProperty]
        [PropertyEditor("NinjaTrader.Gui.Tools.TimeEditorKey")]
        [Display(Name = "TF7 End    (Midday II)", Order = 20, GroupName = "12. Session & Time Control")]
        public DateTime TF7End { get; set; }
    }
}
