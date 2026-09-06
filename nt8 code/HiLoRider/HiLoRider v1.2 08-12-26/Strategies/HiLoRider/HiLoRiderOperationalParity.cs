// Fleet-wide operational parity controls transferred from GoldDigger v3.3.13.
#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel.DataAnnotations;
using System.Globalization;
using NinjaTrader.Cbi;
using NinjaTrader.NinjaScript;
#endregion

namespace NinjaTrader.NinjaScript.Strategies.HiLoRider
{
    public enum HiLoRiderDailyLimitScopeType { PerStrategy, PerAccount, Disabled }

    public partial class HiLoRider : Strategy
    {
        private sealed class SharedAccountDailyLimitState
        {
            public DateTime SessionKey;
            public double RealizedBaseline;
            public volatile bool Breached;
            public string BreachReason = "";
            public double BreachPnl;
        }

        private static readonly object AccountDailyLimitLock = new object();
        private static readonly Dictionary<string, SharedAccountDailyLimitState>
            AccountDailyLimitStates = new Dictionary<string, SharedAccountDailyLimitState>();

        private SharedAccountDailyLimitState _sharedAccountDailyLimitState;
        private DateTime _dailyLimitSessionKey = DateTime.MinValue;
        private double _strategySessionRealized = 0;
        private DateTime _lastOperationalSessionKey = DateTime.MinValue;
        internal string _effectiveInstanceTag = "";

        internal void InitializeOperationalParity()
        {
            string requestedTag = (InstanceTag ?? "").Trim();
            if (requestedTag.Length > 40) requestedTag = requestedTag.Substring(0, 40);
            string instrumentTag = Instrument?.MasterInstrument?.Name ?? "Instrument";
            string accountTag = Account?.Name ?? "Account";
            string runSuffix = string.IsNullOrEmpty(_runId) ? "run" :
                _runId.Substring(Math.Max(0, _runId.Length - Math.Min(6, _runId.Length)));
            _effectiveInstanceTag = string.IsNullOrEmpty(requestedTag)
                ? instrumentTag + ":" + accountTag + ":" + runSuffix
                : requestedTag;
            _lastOperationalSessionKey = DateTime.MinValue;
        }

        internal void BeginOperationalRealtime()
        {
            DateTime key = GetOperationalSessionKey(DateTime.Now);
            _lastOperationalSessionKey = key;
            ResetOperationalDailyLimitSession(key);
        }

        internal void AdvanceOperationalParity(DateTime barTime)
        {
            DateTime key = GetOperationalSessionKey(barTime);
            if (key == _lastOperationalSessionKey) return;
            _lastOperationalSessionKey = key;
            if (Position.MarketPosition == MarketPosition.Flat && !_exitPending)
            {
                _tradesThisSession = 0;
                _autoPaused = false;
                _autoPausedReason = "";
            }
            ResetOperationalDailyLimitSession(key);
        }

        private DateTime GetOperationalSessionKey(DateTime sourceTime)
        {
            DateTime et = TimeZoneInfo.ConvertTimeFromUtc(sourceTime.ToUniversalTime(), EasternTZ);
            return et.Hour >= 18 ? et.Date : et.Date.AddDays(-1);
        }

        private bool IsOperationalPlayback()
        {
            try { return Account?.Connection == Connection.PlaybackConnection; }
            catch { return false; }
        }

        private string GetAccountDailyLimitKey(DateTime sessionKey)
        {
            string accountName = Account?.Name ?? "";
            int accountIdentity = Account == null ? 0 : Account.GetHashCode();
            return accountName + "#" + accountIdentity.ToString("X8", CultureInfo.InvariantCulture)
                + "|" + sessionKey.ToString("yyyyMMdd", CultureInfo.InvariantCulture);
        }

        private void ResetOperationalDailyLimitSession(DateTime sessionKey)
        {
            _dailyLimitSessionKey = sessionKey;
            _strategySessionRealized = 0;
            _sharedAccountDailyLimitState = null;
            sessionRealizedBaseline = GetCurrentRealized();

            if (DailyLimitScope != HiLoRiderDailyLimitScopeType.PerAccount || State != State.Realtime
                || IsOperationalPlayback()) return;

            string key = GetAccountDailyLimitKey(sessionKey);
            lock (AccountDailyLimitLock)
            {
                var staleKeys = new List<string>();
                DateTime oldestRetained = sessionKey.AddDays(-7);
                foreach (var pair in AccountDailyLimitStates)
                    if (pair.Value.SessionKey < oldestRetained) staleKeys.Add(pair.Key);
                foreach (string staleKey in staleKeys) AccountDailyLimitStates.Remove(staleKey);

                SharedAccountDailyLimitState state;
                if (!AccountDailyLimitStates.TryGetValue(key, out state))
                {
                    state = new SharedAccountDailyLimitState
                    {
                        SessionKey = sessionKey,
                        RealizedBaseline = sessionRealizedBaseline
                    };
                    AccountDailyLimitStates[key] = state;
                }
                _sharedAccountDailyLimitState = state;
                sessionRealizedBaseline = state.RealizedBaseline;
            }
        }

        internal void RecordStrategyRealizedExecution(double exitPrice, int quantity)
        {
            if (quantity <= 0 || tradeDir == 0 || filledPrice <= 0
                || Instrument?.MasterInstrument == null) return;
            double pointValue = Instrument.MasterInstrument.PointValue;
            if (pointValue <= 0 || double.IsNaN(pointValue) || double.IsInfinity(pointValue)) return;
            _strategySessionRealized +=
                (exitPrice - filledPrice) * tradeDir * quantity * pointValue;
        }

        internal double GetDailyLimitRealizedPnl()
        {
            if (DailyLimitScope == HiLoRiderDailyLimitScopeType.PerAccount && State == State.Realtime
                && !IsOperationalPlayback())
            {
                double baseline = _sharedAccountDailyLimitState?.RealizedBaseline
                    ?? sessionRealizedBaseline;
                return GetCurrentRealized() - baseline;
            }
            return _strategySessionRealized;
        }

        internal double GetDailyLimitUnrealizedPnl()
        {
            if (DailyLimitScope == HiLoRiderDailyLimitScopeType.PerAccount && State == State.Realtime
                && !IsOperationalPlayback())
            {
                try { return Account?.Get(AccountItem.UnrealizedProfitLoss, Currency.UsDollar) ?? 0; }
                catch { return 0; }
            }
            return GetCurrentUnrealized();
        }

        internal double GetDailyLimitTotalPnl()
        {
            return GetDailyLimitRealizedPnl() + GetDailyLimitUnrealizedPnl();
        }

        internal bool TryGetDailyLimitBreach(out string reason)
        {
            reason = "";
            if (State != State.Realtime || IsOperationalPlayback()
                || DailyLimitScope == HiLoRiderDailyLimitScopeType.Disabled) return false;

            SharedAccountDailyLimitState shared = _sharedAccountDailyLimitState;
            if (DailyLimitScope == HiLoRiderDailyLimitScopeType.PerAccount && shared != null && shared.Breached)
            {
                reason = shared.BreachReason;
                return !string.IsNullOrEmpty(reason);
            }
            if (!EnableDailyLossLimit && !EnableDailyProfitLimit) return false;

            double pnl = GetDailyLimitTotalPnl();
            if (EnableDailyLossLimit && DailyLossLimit > 0 && pnl <= -Math.Abs(DailyLossLimit))
                reason = "DAILY LOSS LIMIT HIT";
            else if (EnableDailyProfitLimit && DailyProfitLimit > 0 && pnl >= Math.Abs(DailyProfitLimit))
                reason = "DAILY PROFIT LIMIT HIT";
            else return false;

            if (DailyLimitScope == HiLoRiderDailyLimitScopeType.PerAccount && shared != null)
            {
                lock (AccountDailyLimitLock)
                {
                    if (!shared.Breached)
                    {
                        shared.BreachReason = reason;
                        shared.BreachPnl = pnl;
                        shared.Breached = true;
                        Print("[HiLoRider] Account daily-limit coordinator set " + reason
                            + " account=" + (Account?.Name ?? "")
                            + " session=" + _dailyLimitSessionKey.ToString("yyyy-MM-dd")
                            + " pnl=" + pnl.ToString("F2", CultureInfo.InvariantCulture));
                    }
                    else reason = shared.BreachReason;
                }
            }
            return true;
        }

        [NinjaScriptProperty]
        [Display(Name = "Instance Tag", Description = "Optional label added to telemetry and the strategy display name for concurrent instances.", GroupName = "00. Operations", Order = 1)]
        public string InstanceTag { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Enable Chart UI / HUD", Description = "Disable the ChartTrader panel and floating HUD for lower UI overhead on secondary instances.", GroupName = "00. Operations", Order = 2)]
        public bool EnableChartUI { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Playback Fast Mode", Description = "For high-speed Playback: suppress chart drawings and the custom panel; skip non-first flat-bar ticks while preserving tick-by-tick open-position management and trade logging. Toggle live with the Chart Trader FM button.", GroupName = "00. Operations", Order = 3)]
        public bool PlaybackFastMode { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Daily Limit Scope", Description = "PerStrategy isolates this instance; PerAccount coordinates concurrent HiLoRider instances; Disabled bypasses daily limits.", GroupName = "00. Operations", Order = 4)]
        public HiLoRiderDailyLimitScopeType DailyLimitScope { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Log Historical Trades", Description = "Disable historical JSONL/CSV logging for faster Strategy Analyzer sweeps; realtime logging is unaffected.", GroupName = "00. Operations", Order = 5)]
        public bool EnableHistoricalTradeLogging { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Use Instrument Profile Defaults", Description = "Preserves the validated MGC/MNQ profile. Disable for controlled optimizer or manual parameter sweeps so entered values are retained.", GroupName = "00. Operations", Order = 6)]
        public bool UseInstrumentProfileDefaults { get; set; }
    }
}
