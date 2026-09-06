// file name = HiLoRiderML.cs
// HiLoRider — online logistic regression ML gate.
// 8-feature vector retained from the source architecture. Only five slots are
// live for HiLoRider; the removed bar-range/longest-bar/ADX slots stay neutral.
//
// Features (indices 0-7):
//   0  ReversalBarStrength   constant 0.5 (source feature removed)
//   1  EntryBarRatioPct      range[0] / range[1]    ∈ [0,1]  (smaller = cleaner signal)
//   2  NormLongest           constant 0.5 (source feature removed)
//   3  NormADX               constant 0.5 (not wired into this vector)
//   4  NormDiff              |cachedDiff| / 20.0  ∈ [0,1]
//   5  HourOfDay             ToTimeET(Time[0]).Hour / 16.0
//   6  DayOfWeek             Time[0].DayOfWeek / 4.0
//   7  TradeIndexNorm        session trade index / 5.0

#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel.DataAnnotations;
using System.Globalization;
using System.IO;
using System.Text;
using NinjaTrader.Cbi;
using NinjaTrader.NinjaScript.Strategies;
#endregion

namespace NinjaTrader.NinjaScript.Strategies.HiLoRider
{
    public partial class HiLoRider : Strategy
    {
        private const int ML_FEATURES = 8;
        // ── OLR model state ────────────────────────────────────────────────────
        private double[] _mlWeights   = new double[ML_FEATURES];
        private double   _mlBias      = 0;
        private int      _mlSampleCount = 0;
        private bool     _mlLastBlocked = false;
        private double   _mlLastScore   = 0.5;
        private double[] _mlLastFeatures = new double[ML_FEATURES];

        // ── Sliding window ─────────────────────────────────────────────────────
        private List<double[]> _mlX      = new List<double[]>();
        private List<int>      _mlY      = new List<int>();

        // ── Entry feature snapshot ─────────────────────────────────────────────
        private double[] _mlEntryFeatures = null;
        private int      _mlEntryDir      = 0;

        private static double Sigmoid(double x) => 1.0 / (1.0 + Math.Exp(-x));

        private double[] BuildFeatureVector(int sig, string sigSource)
        {
            var f = new double[ML_FEATURES];
            double r0r1 = bar1RangeTicks > 0 ? Math.Min(1.0, bar0RangeTicks / bar1RangeTicks) : 0.5;

            f[0] = 0.5;   // ReversalBarStrength removed (no longestBarCache)
            f[1] = r0r1;
            f[2] = 0.5;   // NormLongest removed (no longestBarCache)
            f[3] = 0.5;
            f[4] = Math.Min(1.0, Math.Abs(cachedDiff) / 20.0);
            try { f[5] = ToTimeET(Time[0]) / 10000 / 16.0; } catch { f[5] = 0.5; }
            f[6] = ((int)Time[0].DayOfWeek) / 4.0;
            f[7] = Math.Min(1.0, (_tradesThisSession + 1) / 5.0);
            return f;
        }

        internal void CacheMlEntryFeatureSnapshot(int sig, string sigSource)
        {
            if (!EnableML) return;
            _mlEntryFeatures = BuildFeatureVector(sig, sigSource);
            _mlEntryDir      = sig;
        }

        internal void ClearMlEntryFeatureSnapshot()
        {
            _mlEntryFeatures = null;
            _mlEntryDir      = 0;
        }

        internal bool MLFilterPasses(int sig, string sigSource)
        {
            if (!EnableML) return true;
            if (_mlSampleCount < MLMinSamples) return true;

            double[] f = BuildFeatureVector(sig, sigSource);
            _mlLastFeatures = f;

            double score = _mlBias;
            for (int i = 0; i < ML_FEATURES; i++) score += _mlWeights[i] * f[i];
            double prob = Sigmoid(score);
            _mlLastScore   = prob;
            _mlLastBlocked = prob < MLThreshold;
            return !_mlLastBlocked;
        }

        internal void MLLearnFromLastTrade(Trade t)
        {
            if (!EnableML) return;
            double[] features = _mlEntryFeatures ?? BuildFeatureVector(_mlEntryDir, "HiLoRider");
            int label = t.ProfitTicks > 0 ? 1 : 0;

            if (_mlX.Count >= MLWindowSize)
            {
                _mlX.RemoveAt(0);
                _mlY.RemoveAt(0);
            }
            _mlX.Add(features);
            _mlY.Add(label);
            _mlSampleCount++;

            // SGD update on the newest sample
            double score = _mlBias;
            for (int i = 0; i < ML_FEATURES; i++) score += _mlWeights[i] * features[i];
            double pred = Sigmoid(score);
            double err  = label - pred;

            _mlBias += MLLearningRate * err;
            for (int i = 0; i < ML_FEATURES; i++)
                _mlWeights[i] += MLLearningRate * (err * features[i] - MLRegularisation * _mlWeights[i]);
        }

        internal void TryBootstrapMlFromJsonl()
        {
            if (!EnableML) return;
            try
            {
                string logFile = Path.Combine(TradeLogPath, "HiLoRider_trades.jsonl");
                if (!File.Exists(logFile)) return;

                string[] lines = ReadAllLinesShared(logFile);
                int startLine  = Math.Max(0, lines.Length - 500);

                for (int i = startLine; i < lines.Length; i++)
                {
                    string line = lines[i].Trim();
                    if (string.IsNullOrEmpty(line)) continue;

                    try
                    {
                        double[] features = JsonParseDoubleArray(line, "MLFeatures");
                        if (features == null || features.Length < ML_FEATURES) continue;

                        double profitTicks = JsonParseDouble(line, "ProfitTicks");
                        if (double.IsNaN(profitTicks)) continue;

                        int label = profitTicks > 0 ? 1 : 0;
                        if (_mlX.Count >= MLWindowSize) { _mlX.RemoveAt(0); _mlY.RemoveAt(0); }
                        _mlX.Add(features); _mlY.Add(label); _mlSampleCount++;

                        double sc = _mlBias;
                        for (int j = 0; j < ML_FEATURES; j++) sc += _mlWeights[j] * features[j];
                        double p = Sigmoid(sc);
                        double e = label - p;
                        _mlBias += MLLearningRate * e;
                        for (int j = 0; j < ML_FEATURES; j++)
                            _mlWeights[j] += MLLearningRate * (e * features[j] - MLRegularisation * _mlWeights[j]);
                    }
                    catch { }
                }
                Print($"[HiLoRider ML] Bootstrapped {_mlSampleCount} samples from JSONL.");
            }
            catch (Exception ex)
            {
                Print($"[HiLoRider ML] Bootstrap error: {ex.Message}");
            }
        }

        // ── Minimal JSON helpers — no external library needed ──────────────────
        // Reads the first scalar value for a given key: "key":value
        private static double JsonParseDouble(string json, string key)
        {
            string tag = "\"" + key + "\":";
            int idx = json.IndexOf(tag, StringComparison.Ordinal);
            if (idx < 0) return double.NaN;
            int start = idx + tag.Length;
            while (start < json.Length && json[start] == ' ') start++;
            int end = start;
            while (end < json.Length && json[end] != ',' && json[end] != '}') end++;
            string s = json.Substring(start, end - start).Trim();
            double v;
            return double.TryParse(s, NumberStyles.Any, CultureInfo.InvariantCulture, out v)
                ? v : double.NaN;
        }

        // Reads a JSON array of doubles: "key":[1.0,2.0,...]
        private static double[] JsonParseDoubleArray(string json, string key)
        {
            string tag = "\"" + key + "\":[";
            int idx = json.IndexOf(tag, StringComparison.Ordinal);
            if (idx < 0) return null;
            int start = idx + tag.Length;
            int end   = json.IndexOf(']', start);
            if (end < 0) return null;
            string inner = json.Substring(start, end - start);
            if (string.IsNullOrWhiteSpace(inner)) return new double[0];
            string[] parts = inner.Split(',');
            var result = new double[parts.Length];
            for (int i = 0; i < parts.Length; i++)
            {
                double v;
                if (!double.TryParse(parts[i].Trim(), NumberStyles.Any,
                        CultureInfo.InvariantCulture, out v))
                    return null;
                result[i] = v;
            }
            return result;
        }

        internal string GetMLStatusString()
        {
            if (!EnableML)                                return "Disabled";
            if (_mlSampleCount < MLMinSamples)            return $"Warming up ({_mlSampleCount}/{MLMinSamples})";
            if (_mlLastBlocked)                           return $"BLOCKED  p={_mlLastScore:F3}";
            return $"Pass  p={_mlLastScore:F3}  n={_mlSampleCount}";
        }

        internal double GetMLWinProbability()
        {
            return _mlSampleCount >= MLMinSamples ? _mlLastScore : -1;
        }

        // ── Properties ────────────────────────────────────────────────────────
        [NinjaScriptProperty]
        [Display(Name = "Enable ML Gate", Order = 1, GroupName = "14. Machine Learning")]
        public bool EnableML { get; set; }

        [NinjaScriptProperty][Range(0.5, 1.0)]
        [Display(Name = "ML Threshold", Order = 2, GroupName = "14. Machine Learning")]
        public double MLThreshold { get; set; }

        [NinjaScriptProperty][Range(5, 200)]
        [Display(Name = "ML Min Samples", Order = 3, GroupName = "14. Machine Learning")]
        public int MLMinSamples { get; set; }

        [NinjaScriptProperty][Range(10, 500)]
        [Display(Name = "ML Window Size", Order = 4, GroupName = "14. Machine Learning")]
        public int MLWindowSize { get; set; }

        [NinjaScriptProperty][Range(0.001, 1.0)]
        [Display(Name = "ML Learning Rate", Order = 5, GroupName = "14. Machine Learning")]
        public double MLLearningRate { get; set; }

        [NinjaScriptProperty][Range(0.0, 0.1)]
        [Display(Name = "ML Regularisation", Order = 6, GroupName = "14. Machine Learning")]
        public double MLRegularisation { get; set; }
    }
}
