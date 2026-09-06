// file name = HiLoRiderStopModes.cs
// HiLoRider — stop-loss and profit-target calculation.
// Primary (validated) stop: ChannelBand -- computed directly in
// GetHiLoRiderStopPrice() (HiLoRider.cs), reading hiloInd.UpperBand/LowerBand.
// This file provides CalcStopLevel()/CalcTargetLevel() for the toggleable
// ATR and HighLow stop modes (untested for this signal) and for
// CalcTargetLevel()'s FixedTicks/ATR/RiskReward target modes (FixedTicks is
// the validated live default).

#region Using declarations
using System;
using NinjaTrader.NinjaScript.Indicators;
using NinjaTrader.NinjaScript.Strategies;
#endregion

namespace NinjaTrader.NinjaScript.Strategies.HiLoRider
{
    public partial class HiLoRider : Strategy
    {
        // ── ATR cache ─────────────────────────────────────────────────────────
        internal double cachedATR         = 0;
        internal double cachedATRForTrail = 0;

        internal void CacheATRValue()
        {
            if (atr1 != null && atr1.IsValidDataPoint(0))
            {
                cachedATR         = atr1[0];
                cachedATRForTrail = atr1[0];
            }
        }

        internal double GetATRTicks() =>
            cachedATR > 0 ? cachedATR / TickSize : FixedSLTicks;

        // ── CalcTargetLevel ────────────────────────────────────────────────────
        internal double CalcTargetLevel(int sig, double entryPx)
        {
            switch (TargetMode)
            {
                case RPTargetMode.NoTarget:
                    return 0;

                case RPTargetMode.ATR:
                    if (cachedATR <= 0)
                        return entryPx + (sig == 1 ? 1 : -1) * FixedTPTicks * TickSize;
                    double tpAtr = Math.Max(TPATRMultiplier * (cachedATR / TickSize), FixedTPTicks * 0.25);
                    return Instrument.MasterInstrument.RoundToTickSize(
                        entryPx + (sig == 1 ? 1 : -1) * tpAtr * TickSize);

                case RPTargetMode.RiskReward:
                    double tpRR = FixedSLTicks * Math.Max(0.1, RiskRewardRatio);
                    return Instrument.MasterInstrument.RoundToTickSize(
                        entryPx + (sig == 1 ? 1 : -1) * tpRR * TickSize);

                default: // FixedTicks
                    return Instrument.MasterInstrument.RoundToTickSize(
                        entryPx + (sig == 1 ? 1 : -1) * FixedTPTicks * TickSize);
            }
        }

        // ── CalcStopLevel (used for ATR and HighLow modes) ────────────────────
        internal double CalcStopLevel(int sig, double entryPx)
        {
            switch (StopMode)
            {
                case RPStopMode.ATR:
                    if (cachedATR <= 0)
                        return Instrument.MasterInstrument.RoundToTickSize(
                            entryPx + (sig == 1 ? -1 : 1) * FixedSLTicks * TickSize);
                    double rawTicks = SLATRMultiplier * (cachedATR / TickSize);
                    double clamped  = Math.Max(ATRSLMinTicks, Math.Min(rawTicks, FixedSLTicks));
                    return Instrument.MasterInstrument.RoundToTickSize(
                        entryPx + (sig == 1 ? -1 : 1) * clamped * TickSize);

                case RPStopMode.HighLow:
                    return GetSwingBasedStop(sig, entryPx);

                default: // FixedTick fallback
                    return Instrument.MasterInstrument.RoundToTickSize(
                        entryPx + (sig == 1 ? -1 : 1) * FixedSLTicks * TickSize);
            }
        }

        // ── GetSwingBasedStop (HighLow stop mode) ─────────────────────────────
        private double GetSwingBasedStop(int sig, double entryPx)
        {
            double buffer    = StopBufferTicks * TickSize;
            double fixedStop = Instrument.MasterInstrument.RoundToTickSize(
                entryPx + (sig == 1 ? -1 : 1) * FixedSLTicks * TickSize);

            int lb = Math.Max(1, HLInitialStopLookbackBars);
            if (CurrentBar < lb) return fixedStop;

            double basis = (sig == 1) ? Low[lb] : High[lb];

            double stop = Instrument.MasterInstrument.RoundToTickSize(
                basis + (sig == 1 ? -1 : 1) * buffer);

            bool valid = (sig == 1) ? stop < entryPx : stop > entryPx;
            if (!valid) return fixedStop;

            double distTicks = Math.Abs(entryPx - stop) / TickSize;
            if (distTicks < MinStopTicks) return fixedStop;

            return stop;
        }
    }
}
