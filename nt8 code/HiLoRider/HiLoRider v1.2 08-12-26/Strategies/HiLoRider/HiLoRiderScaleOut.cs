// file name = HiLoRiderScaleOut.cs
// HiLoRider — Scale-Out logic (ScaleOutMode: Disabled / Bars / Ticks).
// Compiled alongside HiLoRider.cs as a partial class.

#region Using declarations
using System;
using System.ComponentModel.DataAnnotations;
using NinjaTrader.Cbi;
using NinjaTrader.NinjaScript.Strategies;
#endregion

namespace NinjaTrader.NinjaScript.Strategies.HiLoRider
{
    public enum ScaleOutMode { Disabled, Bars, Ticks }

    public partial class HiLoRider : Strategy
    {
        // ── Scale-out state ───────────────────────────────────────────────────
        private bool _newSO1Fired   = false;
        private bool _newSO2Fired   = false;
        private int  _newSOEntryBar = -1;

        // ── CheckNewScaleOut  —  ScaleOutMode (Bars / Ticks) system ──────────
        internal void CheckNewScaleOut()
        {
            if (ScaleOutMode == ScaleOutMode.Disabled) return;
            if (Position.Quantity <= 1) return;
            if (filledPrice == 0) return;
            if (tradeDir == 0) return;

            bool isLong = tradeDir == 1;
            bool stage1Hit, stage2Hit;

            if (ScaleOutMode == ScaleOutMode.Bars)
            {
                int barsHeld = _newSOEntryBar >= 0 ? CurrentBar - _newSOEntryBar : 0;
                stage1Hit = barsHeld >= ScaleOut1Bars;
                stage2Hit = barsHeld >= ScaleOut2Bars;
            }
            else // Ticks
            {
                double profitTicks = isLong
                    ? (Close[0] - filledPrice) / TickSize
                    : (filledPrice - Close[0])  / TickSize;
                stage1Hit = profitTicks >= ScaleOutTicks1;
                stage2Hit = profitTicks >= ScaleOutTicks2;
            }

            if (!_newSO1Fired && stage1Hit)
            {
                int qty = Math.Min(ScaleOut1Qty, Position.Quantity - 1);
                if (qty > 0)
                {
                    try
                    {
                        _newSO1Fired = SubmitPartialExit("HLR NSO1", qty);
                    }
                    catch (Exception ex) { Print($"[HiLoRider] NewScaleOut1 error: {ex.Message}"); }
                }
            }

            if (_newSO1Fired && !_newSO2Fired && stage2Hit)
            {
                int qty = Math.Min(ScaleOut2Qty, Position.Quantity - 1);
                if (qty > 0)
                {
                    try
                    {
                        _newSO2Fired = SubmitPartialExit("HLR NSO2", qty);
                    }
                    catch (Exception ex) { Print($"[HiLoRider] NewScaleOut2 error: {ex.Message}"); }
                }
            }
        }

        internal void ResetScaleOutState()
        {
            _newSO1Fired   = false;
            _newSO2Fired   = false;
            _newSOEntryBar = -1;
        }

        // ── Properties ────────────────────────────────────────────────────────
        [NinjaScriptProperty][Range(1, 10)]
        [Display(Name = "Scale-Out 1 Qty", Order = 1, GroupName = "04b. Scale-Out")]
        public int ScaleOut1Qty { get; set; }

        [NinjaScriptProperty][Range(1, 10)]
        [Display(Name = "Scale-Out 2 Qty", Order = 2, GroupName = "04b. Scale-Out")]
        public int ScaleOut2Qty { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Scale-Out Mode", Order = 3, GroupName = "04b. Scale-Out",
            Description = "Disabled = off. Bars = partial exit by bars-in-trade. Ticks = partial exit by profit ticks.")]
        public ScaleOutMode ScaleOutMode { get; set; }

        [NinjaScriptProperty][Range(1, 200)]
        [Display(Name = "Scale-Out 1 Bars", Order = 4, GroupName = "04b. Scale-Out",
            Description = "Bars in trade before Stage 1 fires (Bars mode only).")]
        public int ScaleOut1Bars { get; set; }

        [NinjaScriptProperty][Range(1, 500)]
        [Display(Name = "Scale-Out 2 Bars", Order = 5, GroupName = "04b. Scale-Out",
            Description = "Bars in trade before Stage 2 fires (Bars mode, after Stage 1).")]
        public int ScaleOut2Bars { get; set; }

        [NinjaScriptProperty][Range(1, int.MaxValue)]
        [Display(Name = "Scale-Out Ticks 1", Order = 6, GroupName = "04b. Scale-Out",
            Description = "Profit ticks from entry for Stage 1 (Ticks mode only).")]
        public int ScaleOutTicks1 { get; set; }

        [NinjaScriptProperty][Range(1, int.MaxValue)]
        [Display(Name = "Scale-Out Ticks 2", Order = 7, GroupName = "04b. Scale-Out",
            Description = "Profit ticks from entry for Stage 2 (Ticks mode only, after Stage 1).")]
        public int ScaleOutTicks2 { get; set; }
    }
}
