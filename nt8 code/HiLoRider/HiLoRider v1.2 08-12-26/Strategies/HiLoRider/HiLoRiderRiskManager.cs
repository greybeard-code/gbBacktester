// file name = HiLoRiderRiskManager.cs
// HiLoRider — position sizing and session risk gates (Carver volatility-targeting).

#region Using declarations
using System;
using System.ComponentModel.DataAnnotations;
using NinjaTrader.NinjaScript.Strategies;
#endregion

namespace NinjaTrader.NinjaScript.Strategies.HiLoRider
{
    public partial class HiLoRider : Strategy
    {
        private int    _sizedContracts        = 1;
        private double _tradingCapitalCurrent = 0;

        private int    _consecutiveLosses     = 0;
        private bool   _consecutivePaused     = false;
        private int    _recoveryTradesLeft    = 0;

        private double _sessionPnLPeak        = 0;

        internal void InitSessionRiskManager()
        {
            _sessionPnLPeak = 0;
            if (_tradingCapitalCurrent <= 0)
                _tradingCapitalCurrent = TradingCapital;
            _sizedContracts = ComputeCarverContracts();
        }

        internal void ClearConsecutivePause()
        {
            if (!_consecutivePaused) return;
            _consecutivePaused  = false;
            _consecutiveLosses  = 0;
            _recoveryTradesLeft = 2;
            Print("[HiLoRider RiskMgr] Consecutive pause cleared — 2 half-size recovery trades.");
        }

        internal int GetSizedContracts()
        {
            if (!EnableVolatilitySizing)
            {
                int c = Contracts;
                if (_econReduceSize) c = Math.Max(MinContracts, c / 2);
                return c;
            }
            int qty = (_recoveryTradesLeft > 0) ? Math.Max(1, _sizedContracts / 2) : _sizedContracts;
            if (_econReduceSize) qty = Math.Max(MinContracts, qty / 2);
            return Math.Max(MinContracts, Math.Min(qty, MaxContracts));
        }

        private int ComputeCarverContracts()
        {
            if (!EnableVolatilitySizing) return Contracts;
            if (cachedATR <= 0) return Contracts;

            double dailyCashVolTarget  = _tradingCapitalCurrent * AnnualVolTargetPct / 16.0;
            double instrVolDollars     = cachedATR / TickSize * TickSize * ContractPointValue;
            if (instrVolDollars <= 0)   return Contracts;

            int base_ = (int)Math.Round(dailyCashVolTarget / instrVolDollars);
            return Math.Max(MinContracts, Math.Min(base_, MaxContracts));
        }

        internal void OnTradeClosedRiskUpdate(double profitCurrency)
        {
            if (EnableRollingCapital)
            {
                if (profitCurrency > 0)
                    _tradingCapitalCurrent += profitCurrency * RatchetFraction;
                else
                    _tradingCapitalCurrent += profitCurrency;
            }

            if (profitCurrency < 0)
            {
                _consecutiveLosses++;
                if (MaxConsecutiveLosses > 0 && _consecutiveLosses >= MaxConsecutiveLosses)
                {
                    _consecutivePaused    = true;
                    _autoPaused           = true;
                    _autoPausedReason     = "CONSEC LOSSES";
                    Print($"[HiLoRider RiskMgr] {MaxConsecutiveLosses} consecutive losses — strategy paused until next session.");
                }
            }
            else
            {
                _consecutiveLosses = 0;
                if (_recoveryTradesLeft > 0) _recoveryTradesLeft--;
            }

            if (EnableDrawdownGate && DrawdownFromPeakPct > 0)
            {
                double sessionPnL = GetDailyLimitTotalPnl();
                if (sessionPnL > _sessionPnLPeak) _sessionPnLPeak = sessionPnL;
                if (_sessionPnLPeak > 0 && sessionPnL < _sessionPnLPeak * (1.0 - DrawdownFromPeakPct / 100.0))
                {
                    _autoPaused       = true;
                    _autoPausedReason = "DRAWDOWN";
                    Print($"[HiLoRider RiskMgr] Drawdown gate hit — paused.");
                }
            }
        }

        // ── Properties ────────────────────────────────────────────────────────
        [NinjaScriptProperty]
        [Display(Name = "Enable Volatility Sizing", Order = 1, GroupName = "11. Position Sizing")]
        public bool EnableVolatilitySizing { get; set; }

        [NinjaScriptProperty][Range(1000, double.MaxValue)]
        [Display(Name = "Trading Capital ($)", Order = 2, GroupName = "11. Position Sizing")]
        public double TradingCapital { get; set; }

        [NinjaScriptProperty][Range(0.01, 1.0)]
        [Display(Name = "Annual Vol Target %", Order = 3, GroupName = "11. Position Sizing")]
        public double AnnualVolTargetPct { get; set; }

        [NinjaScriptProperty][Range(0.1, 100.0)]
        [Display(Name = "Contract Point Value ($)", Order = 4, GroupName = "11. Position Sizing",
            Description = "MNQ=$2/pt, NQ=$20/pt, ES=$50/pt, MES=$5/pt")]
        public double ContractPointValue { get; set; }

        [NinjaScriptProperty][Range(1, 100)]
        [Display(Name = "Min Contracts", Order = 5, GroupName = "11. Position Sizing")]
        public int MinContracts { get; set; }

        [NinjaScriptProperty][Range(1, 100)]
        [Display(Name = "Max Contracts", Order = 6, GroupName = "11. Position Sizing")]
        public int MaxContracts { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Enable Rolling Capital", Order = 7, GroupName = "11. Position Sizing")]
        public bool EnableRollingCapital { get; set; }

        [NinjaScriptProperty][Range(0.0, 1.0)]
        [Display(Name = "Ratchet Fraction", Order = 8, GroupName = "11. Position Sizing")]
        public double RatchetFraction { get; set; }

        [NinjaScriptProperty][Range(0, 20)]
        [Display(Name = "Max Consecutive Losses", Order = 9, GroupName = "11. Position Sizing")]
        public int MaxConsecutiveLosses { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Enable Drawdown Gate", Order = 10, GroupName = "11. Position Sizing")]
        public bool EnableDrawdownGate { get; set; }

        [NinjaScriptProperty][Range(0.1, 50.0)]
        [Display(Name = "Drawdown From Peak %", Order = 11, GroupName = "11. Position Sizing")]
        public double DrawdownFromPeakPct { get; set; }
    }
}
