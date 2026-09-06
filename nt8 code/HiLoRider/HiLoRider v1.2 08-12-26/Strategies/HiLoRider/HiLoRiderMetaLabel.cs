// file name = HiLoRiderMetaLabel.cs
// HiLoRider — offline RF meta-label gate (stub).
// Loads a JSON model from MetaModelPath.  All gates pass when EnableMetaLabel is false.

#region Using declarations
using System;
using System.ComponentModel.DataAnnotations;
using System.IO;
using NinjaTrader.NinjaScript.Strategies;
#endregion

namespace NinjaTrader.NinjaScript.Strategies.HiLoRider
{
    public partial class HiLoRider : Strategy
    {
        private bool   _metaModelLoaded = false;

        internal void LoadMetaModel()
        {
            _metaModelLoaded = false;
            if (!EnableMetaLabel || string.IsNullOrEmpty(MetaModelPath)) return;
            try
            {
                if (File.Exists(MetaModelPath))
                {
                    _metaModelLoaded = true;
                    Print($"[HiLoRider MetaLabel] Model loaded from {MetaModelPath}");
                }
            }
            catch (Exception ex)
            {
                Print($"[HiLoRider MetaLabel] Load error: {ex.Message}");
            }
        }

        internal bool MetaLabelPasses(int sig)
        {
            if (!EnableMetaLabel || !_metaModelLoaded) return true;
            // Stub: always pass until a real RF model is wired in.
            return true;
        }

        // ── Properties ────────────────────────────────────────────────────────
        [NinjaScriptProperty]
        [Display(Name = "Enable Meta-Label Gate", Order = 1, GroupName = "15. Meta-Label")]
        public bool EnableMetaLabel { get; set; }

        [NinjaScriptProperty][Range(0.5, 1.0)]
        [Display(Name = "Meta Threshold", Order = 2, GroupName = "15. Meta-Label")]
        public double MetaThreshold { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Meta Model Path", Order = 3, GroupName = "15. Meta-Label")]
        public string MetaModelPath { get; set; }
    }
}
