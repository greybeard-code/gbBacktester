// file name = HiLoRiderFixedStopTrail.cs
// HiLoRider — FixedStopTrail 4-stage ladder (same logic as TrendMaster).

#region Using declarations
using System;
using NinjaTrader.Cbi;
using NinjaTrader.NinjaScript.Strategies;
#endregion

namespace NinjaTrader.NinjaScript.Strategies.HiLoRider
{
    public partial class HiLoRider : Strategy
    {
        private int    _fstStage     = 0;
        private double _fstPeakPrice = 0;

        internal void CheckFixedStopTrail()
        {
            if (TrailMode != RPTrailMode.FixedStopTrail) return;
            if (Position.Quantity <= 0) return;
            if (filledPrice == 0)       return;
            if (stopOrder   == null)    return;
            if (targetTicksHeld <= 0)   return;

            bool   isLong      = tradeDir == 1;
            double currentPx   = isLong ? GetCurrentBid() : GetCurrentAsk();
            double profitTicks = isLong ? (currentPx - filledPrice) / TickSize
                                        : (filledPrice - currentPx) / TickSize;

            // Update rolling peak
            if (isLong)  { if (currentPx > _fstPeakPrice || _fstPeakPrice == 0) _fstPeakPrice = currentPx; }
            else         { if (currentPx < _fstPeakPrice || _fstPeakPrice == 0) _fstPeakPrice = currentPx; }

            double stage1Trigger = targetTicksHeld * FST_Stage1TriggerPct / 100.0;
            double stage2Trigger = targetTicksHeld * FST_Stage2TriggerPct / 100.0;
            double stage3Trigger = targetTicksHeld * FST_Stage3TriggerPct / 100.0;
            double stage4Trigger = targetTicksHeld * FST_Stage4TriggerPct / 100.0;

            double newStop = stopLevel;

            if (profitTicks >= stage4Trigger && _fstStage < 4)
            {
                _fstStage = 4;
                double trail = _fstPeakPrice + (isLong ? -1 : 1) * FST_Stage4TrailTicks * TickSize;
                newStop = isLong ? Math.Max(stopLevel, trail) : Math.Min(stopLevel, trail);
            }
            else if (profitTicks >= stage3Trigger && _fstStage < 3)
            {
                _fstStage = 3;
                double locked = filledPrice + (isLong ? 1 : -1) * targetTicksHeld * FST_Stage3LockPct / 100.0 * TickSize;
                newStop = isLong ? Math.Max(stopLevel, locked) : Math.Min(stopLevel, locked);
            }
            else if (profitTicks >= stage2Trigger && _fstStage < 2)
            {
                _fstStage = 2;
                double locked = filledPrice + (isLong ? 1 : -1) * targetTicksHeld * FST_Stage2LockPct / 100.0 * TickSize;
                newStop = isLong ? Math.Max(stopLevel, locked) : Math.Min(stopLevel, locked);
            }
            else if (profitTicks >= stage1Trigger && _fstStage < 1)
            {
                _fstStage = 1;
                double locked = filledPrice + (isLong ? 1 : -1) * FST_Stage1OffsetTicks * TickSize;
                newStop = isLong ? Math.Max(stopLevel, locked) : Math.Min(stopLevel, locked);
            }

            if (_fstStage == 4)
            {
                double trail = _fstPeakPrice + (isLong ? -1 : 1) * FST_Stage4TrailTicks * TickSize;
                newStop = isLong ? Math.Max(stopLevel, trail) : Math.Min(stopLevel, trail);
            }

            if (newStop != stopLevel)
                TryMoveStop(Instrument.MasterInstrument.RoundToTickSize(newStop));
        }

        internal void ResetFixedStopTrailState()
        {
            _fstStage     = 0;
            _fstPeakPrice = 0;
        }
    }
}
