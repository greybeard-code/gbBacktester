// file name = HiLoRiderRegime.cs
// HiLoRider — regime detection stubs.
// DetectHiLoRiderRegime() (the main regime logic) lives in HiLoRider.cs.
// This file provides HMM stubs so the codebase compiles with HMM properties.

#region Using declarations
using System;
using System.ComponentModel.DataAnnotations;
using NinjaTrader.NinjaScript.Strategies;
#endregion

namespace NinjaTrader.NinjaScript.Strategies.HiLoRider
{
    public partial class HiLoRider : Strategy
    {
        // HMM stubs — full implementation can be added later
        internal bool   hmmWarmup    = true;
        internal int    hmmState     = 0;
        internal double hmmProb      = 0.5;
        internal double hmmConfidence = 0;

        internal void UpdateHMM()
        {
            // Stub — no runtime HMM in v1.0
        }

        internal void ResetHMMSessionState()
        {
            hmmWarmup     = true;
            hmmState      = 0;
            hmmProb       = 0.5;
            hmmConfidence = 0;
        }

        internal bool CheckHMMDirectionGate(int sig)
        {
            if (!EnableHMM) return true;
            return true;  // Stub: always pass
        }

        // ── Properties ────────────────────────────────────────────────────────
        [NinjaScriptProperty]
        [Display(Name = "Enable HMM Regime", Order = 1, GroupName = "16. Regime (HMM)")]
        public bool EnableHMM { get; set; }

        [NinjaScriptProperty][Range(60, 2000)]
        [Display(Name = "HMM Window Bars", Order = 2, GroupName = "16. Regime (HMM)")]
        public int HMMWindowBars { get; set; }

        [NinjaScriptProperty][Range(60, 2000)]
        [Display(Name = "HMM Update Bars", Order = 3, GroupName = "16. Regime (HMM)")]
        public int HMMUpdateBars { get; set; }

        [NinjaScriptProperty][Range(0, 200)]
        [Display(Name = "Regime Bull Size %", Order = 4, GroupName = "16. Regime (HMM)")]
        public int RegimeBullSizePct { get; set; }

        [NinjaScriptProperty][Range(0, 200)]
        [Display(Name = "Regime Bear Size %", Order = 5, GroupName = "16. Regime (HMM)")]
        public int RegimeBearSizePct { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Regime Bear: No Longs", Order = 6, GroupName = "16. Regime (HMM)")]
        public bool RegimeBearNoLongs { get; set; }
    }
}
