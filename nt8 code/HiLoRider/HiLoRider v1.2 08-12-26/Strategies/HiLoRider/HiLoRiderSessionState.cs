// file name = HiLoRiderSessionState.cs
// HiLoRider — session state, MFE/MAE tracking, DrawHUD stub, OnPositionUpdate.

#region Using declarations
using System;
using System.Collections.Generic;
using NinjaTrader.Cbi;
using NinjaTrader.NinjaScript.Strategies;
#endregion

namespace NinjaTrader.NinjaScript.Strategies.HiLoRider
{
    public partial class HiLoRider : Strategy
    {
        // ── Session P&L baseline ──────────────────────────────────────────────
        internal double sessionRealizedBaseline = 0;

        // ── Win / loss counters ────────────────────────────────────────────────
        internal int hudWins   = 0;
        internal int hudLosses = 0;

        // ── Trade-level High / Low tracking ───────────────────────────────────
        internal double hudHighSinceEntry = 0;
        internal double hudLowSinceEntry  = 0;

        // ── Session-level peak MFE / MAE ──────────────────────────────────────
        internal double sessionMaxMfeTicks = 0;
        internal double sessionMaxMaeTicks = 0;

        // ── Per-trade MFE / MAE accumulators ──────────────────────────────────
        private double _tradeMfeTicks = 0;
        private double _tradeMaeTicks = 0;

        // ── AllTrades baseline for cross-instance guard ────────────────────────
        internal int _initialAllTradesCount = -1;

        // ── OI-4: last-logged index ────────────────────────────────────────────
        private int _lastLoggedTradeIdx = -1;
        private bool _deferredFlatStateReconciliationQueued = false;

        // ── Exit-name capture for ML ───────────────────────────────────────────
        private string _tbExitName = "";

        // ─────────────────────────────────────────────────────────────────────
        private double GetCurrentRealized()
        {
            try { return Account?.Get(AccountItem.RealizedProfitLoss, Currency.UsDollar) ?? 0; }
            catch { return 0; }
        }

        private double GetCurrentUnrealized()
        {
            try
            {
                if (Position == null || Position.MarketPosition == MarketPosition.Flat) return 0;
                double price = (State == State.Realtime)
                    ? (Position.MarketPosition == MarketPosition.Long ? GetCurrentBid() : GetCurrentAsk())
                    : Close[0];
                return Position.GetUnrealizedProfitLoss(PerformanceUnit.Currency, price);
            }
            catch { return 0; }
        }

        private void CaptureSessionBaseline()
        {
            sessionRealizedBaseline = GetCurrentRealized();
        }

        private void ResetSessionMfeMae()
        {
            sessionMaxMfeTicks = 0;
            sessionMaxMaeTicks = 0;
            _tradeMfeTicks     = 0;
            _tradeMaeTicks     = 0;
            hudHighSinceEntry  = 0;
            hudLowSinceEntry   = 0;
        }

        private string GetCurrentETTimeString()
        {
            try
            {
                DateTime et = TimeZoneInfo.ConvertTimeFromUtc(Time[0].ToUniversalTime(), EasternTZ);
                return et.ToString("HH:mm");
            }
            catch { return "--:--"; }
        }

        private void DrawHUD()
        {
            if (Position.MarketPosition != MarketPosition.Flat && filledPrice > 0)
            {
                if (hudHighSinceEntry == 0 || High[0] > hudHighSinceEntry) hudHighSinceEntry = High[0];
                if (hudLowSinceEntry  == 0 || Low[0]  < hudLowSinceEntry)  hudLowSinceEntry  = Low[0];

                bool isLong = Position.MarketPosition == MarketPosition.Long;
                double mfe = isLong
                    ? Math.Max(0, (hudHighSinceEntry - filledPrice) / TickSize)
                    : Math.Max(0, (filledPrice - hudLowSinceEntry)  / TickSize);
                double mae = isLong
                    ? Math.Max(0, (filledPrice - hudLowSinceEntry)  / TickSize)
                    : Math.Max(0, (hudHighSinceEntry - filledPrice) / TickSize);

                if (mfe > _tradeMfeTicks) _tradeMfeTicks = mfe;
                if (mae > _tradeMaeTicks) _tradeMaeTicks = mae;
            }

            if (State == State.Realtime)
                DispatchTextHudRefresh();
        }

        // ─────────────────────────────────────────────────────────────────────
        protected override void OnPositionUpdate(
            Position position, double averagePrice,
            int quantity, MarketPosition marketPosition)
        {
            // Use the immutable callback snapshot. During max-speed Playback an
            // entry and its exit can complete in the same dispatcher cycle, while
            // the mutable Position object (and BarsInProgress) can already reflect
            // another queued event. Returning on either value can strand the fill
            // ledger and permanently block IsEntryStateClear().
            if (marketPosition != MarketPosition.Flat) return;

            double snapFilledPrice = filledPrice;

            try
            {
                int preCount = SystemPerformance.AllTrades.Count;
                if (preCount > 0)
                    _tbExitName = SystemPerformance.AllTrades[preCount - 1].Exit?.Name ?? "";
            }
            catch { _tbExitName = ""; }

            try
            {
                int total = SystemPerformance.AllTrades.Count;
                if (total > 0)
                {
                    Trade last = SystemPerformance.AllTrades[total - 1];

                    if (_tradeMfeTicks > sessionMaxMfeTicks) sessionMaxMfeTicks = _tradeMfeTicks;
                    if (_tradeMaeTicks > sessionMaxMaeTicks) sessionMaxMaeTicks = _tradeMaeTicks;

                    int floor    = (_initialAllTradesCount < 0) ? 0 : _initialAllTradesCount;
                    int startIdx = Math.Max(_lastLoggedTradeIdx + 1, floor);

                    double posNetTicks      = 0;
                    double posNetProfit     = 0;
                    double posNetCommission = 0;
                    int    posNetContracts  = 0;
                    int    posPartialCount  = 0;
                    var    posExitNames     = new HashSet<string>();
                    DateTime posFirstExitTime = DateTime.MaxValue;
                    DateTime posLastExitTime  = DateTime.MinValue;

                    for (int i = startIdx; i < total; i++)
                    {
                        Trade t = SystemPerformance.AllTrades[i];
                        TryLogTrade(t, _tradeMfeTicks, _tradeMaeTicks, snapFilledPrice);

                        posNetTicks      += t.ProfitTicks;
                        posNetProfit     += t.ProfitCurrency;
                        posNetCommission += t.Commission;
                        posNetContracts  += t.Quantity;
                        posPartialCount++;
                        if (!string.IsNullOrEmpty(t.Exit?.Name)) posExitNames.Add(t.Exit.Name);
                        if (t.Exit != null)
                        {
                            if (t.Exit.Time < posFirstExitTime) posFirstExitTime = t.Exit.Time;
                            if (t.Exit.Time > posLastExitTime)  posLastExitTime  = t.Exit.Time;
                        }
                    }
                    _lastLoggedTradeIdx = total - 1;

                    if (posNetProfit >= 0) hudWins++;
                    else                   hudLosses++;

                    if (posPartialCount > 0)
                    {
                        Trade firstPartial = SystemPerformance.AllTrades[startIdx];
                        TryLogChartTradeSummary(
                            firstPartial,
                            posNetTicks, posNetProfit, posNetCommission,
                            posNetContracts, posPartialCount,
                            posExitNames, posFirstExitTime, posLastExitTime,
                            _tradeMfeTicks, _tradeMaeTicks, snapFilledPrice);
                    }

                    if (EnableML) MLLearnFromLastTrade(last);

                    OnTradeClosedRiskUpdate(last.ProfitCurrency);
                }
            }
            catch (Exception ex)
            {
                try { Print($"[HiLoRider] OnPositionUpdate: {ex.Message}"); } catch { }
            }
            finally
            {
                ClearMlEntryFeatureSnapshot();
            }

            ResetFlatRuntimeState();
            QueueDeferredFlatStateReconciliation();
        }

        private void ResetFlatRuntimeState()
        {
            // Clear only the target-loss pause that was guarding the now-closed
            // position. Daily profit/loss and other operator-visible pauses remain.
            if (_autoPaused && _autoPausedReason == "TARGET LOST - STOP ACTIVE")
            {
                _autoPaused       = false;
                _autoPausedReason = "";
            }

            lastExitBar       = CurrentBar;
            _tradeMfeTicks    = 0;
            _tradeMaeTicks    = 0;
            hudHighSinceEntry = 0;
            hudLowSinceEntry  = 0;
            filledPrice              = 0;
            bePriceTrigger           = 0;
            autoExitPending          = false;
            _isManualTrade           = false;
            _diffConfirmedSinceEntry = false;
            stagedTrailStage  = 0;
            beRealized        = false;

            ResetScaleOutState();
            ResetFixedStopTrailState();

            // Do not null order references before this call. The safety reset
            // must be able to cancel an orphaned entry, stop, or target first.
            ResetExecutionSafetyAfterFlat(true);

            if (_strategyLockedByManualTrade)
            {
                _strategyLockedByManualTrade = false;
                SetStrategyEnabled(_strategyWasEnabledBeforeManualTrade);
                _strategyWasEnabledBeforeManualTrade = false;
            }

            UpdateLivePanel();
        }

        internal void ReconcileSettledFlatState(string source)
        {
            if (Position.MarketPosition != MarketPosition.Flat) return;

            // Connection-loss gates remain fail-closed. Reconciliation will run
            // on a later completed bar after both connections are restored.
            if (_orderConnectionLost || _priceConnectionLost) return;

            // A working entry is a legitimate flat state. Platform/coordinated
            // exits are also allowed to reach a terminal callback before cleanup.
            if (IsOrderActive(entryOrder)
                || IsOrderActive(_platformSessionExitOrder)
                || IsOrderActive(_coordinatedExitOrder))
                return;

            bool hasStaleState = tradeDir != 0
                || _entryFillQty != 0
                || _exitFillQty != 0
                || _exitPending
                || _platformSessionExitPending
                || _sessionCloseProtectionGracePending
                || _partialProtectiveStopPending
                || _pendingBracketPlacement
                || _managedRecoveryRequested
                || _stopChangePending
                || _protectionFailureExitQueued
                || _bracketReconcileQueued
                || _partialStopTerminalReconcileQueued
                || IsOrderActive(stopOrder)
                || IsOrderActive(targetOrder);
            if (!hasStaleState) return;

            Print($"[HiLoRider] Flat-state recovery ({source ?? "settled flat"}): "
                + $"dir={tradeDir} entryQty={_entryFillQty} exitQty={_exitFillQty} "
                + $"entry={entryOrder?.OrderState} stop={stopOrder?.OrderState} "
                + $"target={targetOrder?.OrderState}; cancelling orphan protection and clearing terminal state.");

            ClearMlEntryFeatureSnapshot();
            ResetFlatRuntimeState();
        }

        private void QueueDeferredFlatStateReconciliation()
        {
            if (_deferredFlatStateReconciliationQueued) return;
            _deferredFlatStateReconciliationQueued = true;
            try
            {
                TriggerCustomEvent(_ =>
                {
                    _deferredFlatStateReconciliationQueued = false;
                    ReconcileSettledFlatState("deferred flat callback");
                }, null);
            }
            catch (Exception ex)
            {
                _deferredFlatStateReconciliationQueued = false;
                Print($"[HiLoRider] Deferred flat-state reconciliation queue failed: {ex.Message}");
            }
        }
    }
}
