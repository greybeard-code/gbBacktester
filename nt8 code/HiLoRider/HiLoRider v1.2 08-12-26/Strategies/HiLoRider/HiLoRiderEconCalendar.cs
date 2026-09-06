// HiLoRiderEconCalendar.cs — reads calendar_latest.json for daily bias and news-block windows.
// LoadEconomicCalendar() is called in State.DataLoaded.
// IsInCalendarNewsBlock() is called inside CheckNewsBlock().

using System;
using System.Collections.Generic;
using System.IO;
using System.Text.RegularExpressions;
using NinjaTrader.NinjaScript.Strategies;

namespace NinjaTrader.NinjaScript.Strategies.HiLoRider
{
    public partial class HiLoRider : Strategy
    {
        // ── Economic calendar state ────────────────────────────────────────────
        private double   _econBiasScore      = 0.0;
        private bool     _econAllowLongs     = true;
        private bool     _econAllowShorts    = true;
        private bool     _econCautionMode    = false;
        private bool     _econReduceSize     = false;
        private DateTime _econLastLoadedDate = DateTime.MinValue;
        private readonly List<(TimeSpan start, TimeSpan end)> _econBlockWindows
            = new List<(TimeSpan, TimeSpan)>();

        private static readonly TimeZoneInfo _econTZ =
            TimeZoneInfo.FindSystemTimeZoneById("Eastern Standard Time");

        internal void LoadEconomicCalendar()
        {
            ResetEconomicCalendarState();
            _econLastLoadedDate = DateTime.Today;
            string calFile = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
                "NinjaTrader 8", "EconomicCalendar", "calendar_latest.json");

            if (!File.Exists(calFile))
            {
                Print("[EconCal] calendar_latest.json not found — NEUTRAL defaults");
                return;
            }
            try
            {
                string json = File.ReadAllText(calFile, System.Text.Encoding.UTF8);

                string dateStr = EconExtractString(json, "date");
                if (!DateTime.TryParseExact(dateStr, "yyyy-MM-dd",
                    System.Globalization.CultureInfo.InvariantCulture,
                    System.Globalization.DateTimeStyles.None, out DateTime calDate) ||
                    calDate.Date != DateTime.Today)
                {
                    Print($"[EconCal] Calendar date '{dateStr}' is not today — NEUTRAL defaults");
                    return;
                }

                _econBiasScore   = EconExtractDouble(json, "bias_score");
                _econAllowLongs  = EconExtractBool(json, "allow_longs", true);
                _econAllowShorts = EconExtractBool(json, "allow_shorts", true);
                _econCautionMode = EconExtractBool(json, "caution_mode", false);
                _econReduceSize  = EconExtractBool(json, "reduce_size", false);

                _econBlockWindows.Clear();
                var matches = Regex.Matches(json,
                    @"""block_start_et""\s*:\s*""(\d{2}:\d{2})""\s*,\s*""block_end_et""\s*:\s*""(\d{2}:\d{2})""");
                foreach (Match m in matches)
                    if (TimeSpan.TryParse(m.Groups[1].Value, out TimeSpan s) &&
                        TimeSpan.TryParse(m.Groups[2].Value, out TimeSpan e))
                        _econBlockWindows.Add((s, e));

                Print($"[EconCal] bias={_econBiasScore:+0.00} longs={_econAllowLongs} " +
                      $"shorts={_econAllowShorts} blocks={_econBlockWindows.Count} caution={_econCautionMode}");
            }
            catch (Exception ex)
            {
                ResetEconomicCalendarState();
                Print($"[EconCal] Parse error — NEUTRAL defaults: {ex.Message}");
            }
        }

        private void ResetEconomicCalendarState()
        {
            _econBiasScore   = 0.0;
            _econAllowLongs  = true;
            _econAllowShorts = true;
            _econCautionMode = false;
            _econReduceSize  = false;
            _econBlockWindows.Clear();
        }

        internal bool IsInCalendarNewsBlock()
        {
            if (_econBlockWindows.Count == 0) return false;
            TimeSpan now = TimeZoneInfo.ConvertTimeFromUtc(Time[0].ToUniversalTime(), _econTZ).TimeOfDay;
            foreach (var (start, end) in _econBlockWindows)
                if (now >= start && now <= end) return true;
            return false;
        }

        internal void TryRefreshEconCalendar()
        {
            if (!IsFirstTickOfBar) return;
            if (DateTime.Today == _econLastLoadedDate) return;
            TimeSpan etNow = TimeZoneInfo.ConvertTimeFromUtc(Time[0].ToUniversalTime(), _econTZ).TimeOfDay;
            if (etNow < new TimeSpan(8, 5, 0)) return;
            LoadEconomicCalendar();
        }

        internal string GetEconSubSuffix()
        {
            TimeSpan now = TimeZoneInfo.ConvertTimeFromUtc(DateTime.UtcNow, _econTZ).TimeOfDay;
            foreach (var (start, end) in _econBlockWindows)
                if (now >= start && now <= end) return "  ·  NEWS BLOCK";
            if (_econBiasScore >  0.20) return $"  ·  Econ BULLISH {_econBiasScore:+0.00}";
            if (_econBiasScore < -0.20) return $"  ·  Econ BEARISH {_econBiasScore:+0.00}";
            if (_econCautionMode)       return  "  ·  Econ CAUTION";
            return "";
        }

        private static double EconExtractDouble(string json, string key)
        {
            var m = Regex.Match(json, $@"""{key}""\s*:\s*(-?[\d.]+)");
            return m.Success && double.TryParse(m.Groups[1].Value,
                System.Globalization.NumberStyles.Any,
                System.Globalization.CultureInfo.InvariantCulture, out double v) ? v : 0.0;
        }
        private static bool EconExtractBool(string json, string key, bool defaultValue)
        {
            var m = Regex.Match(json, $@"""{key}""\s*:\s*(true|false)");
            return m.Success ? m.Groups[1].Value == "true" : defaultValue;
        }
        private static string EconExtractString(string json, string key)
        {
            var m = Regex.Match(json, $@"""{key}""\s*:\s*""([^""]+)""");
            return m.Success ? m.Groups[1].Value : string.Empty;
        }
    }
}
