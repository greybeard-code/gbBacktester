// HiLoRider execution-safety transfer from the validated v3.3.15 strategy.
// Keeps the Reaper-family signal/filter architecture while using executions,
// not order-state notifications, as the fill/quantity source of truth.

#region Using declarations
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Strategies;
#endregion

namespace NinjaTrader.NinjaScript.Strategies.HiLoRider
{
    public partial class HiLoRider : Strategy
    {
        internal const string StrategyVersion = "HiLoRider-1.2-safety-3.3.15-performance-ui-r2";
        internal const string FeatureVersion  = "HLR_KHAN_FEATURE_V1";

        private enum CoordinatedExitPhase
        {
            None,
            CancelTarget,
            CancelStop,
            SubmitMarket,
            MarketWorking
        }

        // Execution ledger and coordinated-exit state.
        private int    _entryFillQty = 0;
        private double _entryFillValue = 0;
        private int    _exitFillQty = 0;
        private double _exitFillValue = 0;
        private string _exitFillReason = "";
        private bool   _tradeCounted = false;
        private bool   _exitPending = false;
        private CoordinatedExitPhase _coordinatedExitPhase = CoordinatedExitPhase.None;
        private Order  _coordinatedExitOrder = null;
        private string _coordinatedExitName = "";
        private bool   _coordinatedExitAll = false;
        private int    _coordinatedExitRequestedQty = 0;
        private int    _coordinatedExitFilledQty = 0;
        private int    _coordinatedExitRetryCount = 0;

        // Protection/OCO state.
        private int    _bracketRetryCount = 0;
        private bool   _bracketReconcileQueued = false;
        private string _bracketReconcileReason = "";
        private bool   _protectionFailureExitQueued = false;
        private bool   _partialProtectiveStopPending = false;
        private bool   _partialStopTerminalReconcileQueued = false;
        private bool   _managedRecoveryRequested = false;
        private bool   _stopChangePending = false;
        private bool   _requestedStopChangeIsTrail = false;
        private double _requestedStopLevel = 0;
        private double _initialAcceptedStopLevel = 0;
        private double _initialAcceptedTargetLevel = 0;
        internal int    _acceptedTrailChanges = 0;
        internal double _maxTrailRiskReductionTicks = 0;

        // NinjaTrader platform session-close coordination.
        private SessionIterator _sessionIterator = null;
        private Order    _platformSessionExitOrder = null;
        private bool     _platformSessionExitPending = false;
        private bool     _sessionCloseProtectionGracePending = false;
        private string   _sessionCloseProtectionGraceDetail = "";
        private DateTime _sessionCloseProtectionGraceDeadline = DateTime.MinValue;
        private DateTime _currentSessionBegin = DateTime.MinValue;
        private DateTime _currentSessionEnd = DateTime.MinValue;

        // Run identity and direct spread/liquidity telemetry.
        internal string _runId = "";
        private string _configHash = "";
        private double _lastBid = 0;
        private double _lastAsk = 0;
        private DateTime _lastBidTime = DateTime.MinValue;
        private DateTime _lastAskTime = DateTime.MinValue;
        private DateTime _lastLiquidityEventTime = DateTime.MinValue;
        private readonly Queue<LiquidityTradeEvent> _recentLastTrades =
            new Queue<LiquidityTradeEvent>();
        private double _recentLastTradeVolume5s = 0;
        private int _recentLastTradeCount5s = 0;
        internal double _snapSignalBid = 0;
        internal double _snapSignalAsk = 0;
        internal double _snapSpreadTicks = 0;
        internal bool _snapSignalQuoteValid = false;
        internal string _snapLiquidityBlockReason = "";
        internal double _snapRecentTradeVolume5s = 0;
        internal int _snapRecentTradeCount5s = 0;

        private struct LiquidityTradeEvent
        {
            public DateTime Time;
            public double Volume;
        }

        private static readonly object LiquidityLogLock = new object();

        internal void InitializeExecutionSafety()
        {
            try { _sessionIterator = new SessionIterator(Bars); }
            catch { _sessionIterator = null; }

            _runId = DateTime.UtcNow.ToString("yyyyMMddTHHmmssfffZ", CultureInfo.InvariantCulture)
                + "-" + Guid.NewGuid().ToString("N").Substring(0, 8);
            InitializeOperationalParity();
            _configHash = BuildConfigHash();
            ResetLiquidityMarketState();
            Print($"[HiLoRider] RunId={_runId} Version={StrategyVersion} ConfigHash={_configHash}");
        }

        internal void HandleRealtimeSafetyTransition()
        {
            if (Position.MarketPosition != MarketPosition.Flat)
                return;

            entryOrder = null;
            stopOrder = null;
            targetOrder = null;
            bracketOcoId = "";
            _pendingBracketPlacement = false;
            _pendingBracketQty = 0;
            filledPrice = 0;
            tradeDir = 0;
            stopLevel = 0;
            targetLevel = 0;
            autoExitPending = false;
            _emergencyStopSubmitted = false;
            ResetExecutionLedgerOnly();
            ClearCoordinatedExitState();
        }

        internal string GetConfigHash()
        {
            if (string.IsNullOrEmpty(_configHash))
                _configHash = BuildConfigHash();
            return _configHash;
        }

        private string BuildConfigHash()
        {
            string identity = string.Join("|", new[]
            {
                StrategyVersion,
                Instrument?.FullName ?? "",
                BarsPeriod == null ? "" : BarsPeriod.ToString(),
                "Diff=" + DiffLookback + ":" + DiffMinPercent + ":" + DiffMaxPercent,
                "HiLo=" + LookbackPeriod + ":" + HiLoWidth,
                "Stop=" + StopMode + ":" + FixedSLTicks + ":" + StopBufferTicks,
                "Target=" + TargetMode + ":" + FixedTPTicks,
                "Trail=" + TrailMode,
                "Contracts=" + Contracts,
                "MaxBars=" + MaxBarsInTrade,
                "Spread=" + MaxEntrySpreadTicks,
                "DailyLimitScope=" + DailyLimitScope,
                "HistoricalLogging=" + EnableHistoricalTradeLogging,
                "InstrumentProfile=" + UseInstrumentProfileDefaults,
                "ML=" + EnableML + ":" + MLThreshold.ToString("R", CultureInfo.InvariantCulture)
            });
            using (SHA256 sha = SHA256.Create())
            {
                byte[] hash = sha.ComputeHash(Encoding.UTF8.GetBytes(identity));
                return BitConverter.ToString(hash).Replace("-", "").Substring(0, 16);
            }
        }

        private static bool IsOrderActive(Order order)
        {
            if (order == null) return false;
            return order.OrderState == OrderState.Initialized
                || order.OrderState == OrderState.Submitted
                || order.OrderState == OrderState.Accepted
                || order.OrderState == OrderState.AcceptedByRisk
                || order.OrderState == OrderState.Working
                || order.OrderState == OrderState.PartFilled
                || order.OrderState == OrderState.TriggerPending
                || order.OrderState == OrderState.ChangePending
                || order.OrderState == OrderState.ChangeSubmitted
                || order.OrderState == OrderState.CancelPending
                || order.OrderState == OrderState.CancelSubmitted;
        }

        private static bool IsCancellationPending(Order order)
        {
            return order != null
                && (order.OrderState == OrderState.CancelPending
                    || order.OrderState == OrderState.CancelSubmitted);
        }

        private static bool IsOrderChangePending(Order order)
        {
            return order != null
                && (order.OrderState == OrderState.ChangePending
                    || order.OrderState == OrderState.ChangeSubmitted);
        }

        private static int GetOrderRemainingQuantity(Order order)
        {
            return order == null ? 0 : Math.Max(0, order.Quantity - order.Filled);
        }

        private static bool IsPlatformSessionCloseExit(string orderName)
        {
            return string.Equals(orderName, "Exit on session close",
                StringComparison.OrdinalIgnoreCase);
        }

        private bool IsBracketPeerFillAwaitingExecution(string terminalLegName,
            OrderState terminalState)
        {
            if (terminalState != OrderState.Cancelled
                && terminalState != OrderState.Rejected)
                return false;
            Order peer = terminalLegName == "HLR Target" ? stopOrder
                : terminalLegName == "HLR Stop" ? targetOrder : null;
            return peer != null && peer.OrderState == OrderState.Filled;
        }

        internal bool IsEntryStateClear()
        {
            return Position.MarketPosition == MarketPosition.Flat
                && !_orderConnectionLost
                && !_priceConnectionLost
                && !_exitPending
                && !_platformSessionExitPending
                && !_sessionCloseProtectionGracePending
                && !_partialProtectiveStopPending
                && tradeDir == 0
                && _entryFillQty == 0
                && !IsOrderActive(entryOrder)
                && !IsOrderActive(stopOrder)
                && !IsOrderActive(targetOrder)
                && !IsOrderActive(_platformSessionExitOrder)
                && !IsOrderActive(_coordinatedExitOrder);
        }

        internal void AdvanceExecutionSafety(DateTime eventTime)
        {
            if (AdvanceSessionCloseProtectionGrace(eventTime))
                return;
            if (_exitPending)
                AdvanceCoordinatedExit();
            if (_pendingBracketPlacement && Position.MarketPosition != MarketPosition.Flat
                && filledPrice > 0 && !_exitPending)
                ReconcileProtection(_pendingBracketIsLong,
                    Math.Min(Math.Max(0, Position.Quantity), Math.Max(0, _pendingBracketQty)));
        }

        private void UpdateCurrentSessionEnd(DateTime eventTime)
        {
            if (_sessionIterator == null || eventTime == DateTime.MinValue) return;
            if (_currentSessionBegin != DateTime.MinValue
                && _currentSessionEnd != DateTime.MinValue
                && eventTime >= _currentSessionBegin.AddSeconds(-2)
                && eventTime <= _currentSessionEnd.AddSeconds(2))
                return;
            try
            {
                _sessionIterator.GetNextSession(eventTime, true);
                _currentSessionBegin = _sessionIterator.ActualSessionBegin;
                _currentSessionEnd = _sessionIterator.ActualSessionEnd;
            }
            catch
            {
                _currentSessionBegin = DateTime.MinValue;
                _currentSessionEnd = DateTime.MinValue;
            }
        }

        private bool IsWithinPlatformSessionCloseWindow(DateTime eventTime)
        {
            if (State != State.Realtime || !IsExitOnSessionCloseStrategy
                || eventTime == DateTime.MinValue)
                return false;
            UpdateCurrentSessionEnd(eventTime);
            if (_currentSessionEnd == DateTime.MinValue) return false;
            DateTime start = _currentSessionEnd.AddSeconds(-Math.Max(0, ExitOnSessionCloseSeconds) - 2);
            DateTime end = _currentSessionEnd.AddSeconds(2);
            return eventTime >= start && eventTime <= end;
        }

        private void ArmSessionCloseProtectionGrace(string detail, DateTime eventTime)
        {
            _sessionCloseProtectionGracePending = true;
            _sessionCloseProtectionGraceDetail = detail ?? "protective OCO became terminal";
            _sessionCloseProtectionGraceDeadline = eventTime.AddSeconds(2);
            _pendingBracketPlacement = false;
            currentRegimeLabel = "SESSION EXITING";
            Print($"[HiLoRider] {_sessionCloseProtectionGraceDetail}; waiting for platform session-close exit.");
        }

        private void ClearSessionCloseProtectionGrace()
        {
            _sessionCloseProtectionGracePending = false;
            _sessionCloseProtectionGraceDetail = "";
            _sessionCloseProtectionGraceDeadline = DateTime.MinValue;
        }

        private bool AdvanceSessionCloseProtectionGrace(DateTime eventTime)
        {
            if (!_sessionCloseProtectionGracePending) return false;
            if (Position.MarketPosition == MarketPosition.Flat)
            {
                ClearSessionCloseProtectionGrace();
                return false;
            }
            if (_platformSessionExitPending || IsOrderActive(_platformSessionExitOrder))
            {
                ClearSessionCloseProtectionGrace();
                return false;
            }
            if (eventTime == DateTime.MinValue
                || eventTime < _sessionCloseProtectionGraceDeadline)
                return true;
            string detail = _sessionCloseProtectionGraceDetail;
            ClearSessionCloseProtectionGrace();
            QueueProtectionFailureExit(detail + "; platform session-close exit did not appear");
            return true;
        }

        private double GetProtectiveStopMarketReference(bool isLong)
        {
            try
            {
                double value = isLong ? GetCurrentBid() : GetCurrentAsk();
                if (value > 0) return value;
            }
            catch { }
            try { if (CurrentBar >= 0 && Close[0] > 0) return Close[0]; }
            catch { }
            return 0;
        }

        private bool ExitInsteadOfCrossedProtectiveStop(bool isLong,
            double requestedStop, string context)
        {
            if (requestedStop <= 0 || Position.MarketPosition == MarketPosition.Flat)
                return false;
            if (_exitPending || _platformSessionExitPending
                || _sessionCloseProtectionGracePending
                || _partialProtectiveStopPending)
                return true;
            double market = GetProtectiveStopMarketReference(isLong);
            if (market <= 0) return false;
            bool crossed = isLong ? requestedStop >= market : requestedStop <= market;
            if (!crossed) return false;
            _pendingBracketPlacement = false;
            bracketOcoId = "";
            Print($"[HiLoRider] Stop {requestedStop:F2} crossed market {market:F2} during {context}; exiting residual.");
            BeginCoordinatedExit("HLR StopCrossGuard", Position.Quantity, true);
            return true;
        }

        private static bool IsCrossedMarketStopChangeError(ErrorCode error,
            string nativeError)
        {
            if (error != ErrorCode.UnableToChangeOrder
                || string.IsNullOrWhiteSpace(nativeError))
                return false;
            return nativeError.IndexOf("stop price can't be changed above the market",
                       StringComparison.OrdinalIgnoreCase) >= 0
                || nativeError.IndexOf("stop price can't be changed below the market",
                       StringComparison.OrdinalIgnoreCase) >= 0;
        }

        internal void ReconcileProtection(bool isLong, int qty)
        {
            try
            {
                if (qty <= 0 || filledPrice <= 0
                    || Position.MarketPosition == MarketPosition.Flat)
                    return;
                if (_exitPending || _platformSessionExitPending
                    || _sessionCloseProtectionGracePending
                    || _partialProtectiveStopPending)
                    return;
                if (IsCancellationPending(stopOrder) || IsCancellationPending(targetOrder)
                    || IsOrderChangePending(stopOrder) || IsOrderChangePending(targetOrder))
                    return;

                if (stopLevel <= 0)
                {
                    double distance = Math.Max(FixedSLTicks, 1) * TickSize;
                    stopLevel = Instrument.MasterInstrument.RoundToTickSize(
                        isLong ? filledPrice - distance : filledPrice + distance);
                }
                if (ExitInsteadOfCrossedProtectiveStop(isLong, stopLevel, "bracket submission"))
                    return;

                bool expectsTarget = State != State.Historical
                    && TargetMode != RPTargetMode.NoTarget && targetLevel > 0;

                if (!IsOrderActive(stopOrder))
                {
                    stopOrder = null;
                    if (IsOrderActive(targetOrder))
                    {
                        // Never leave a stop-cancellation gap to rebuild a consumed OCO.
                        HandleTerminalTargetLoss("protective stop missing");
                        return;
                    }
                    if (string.IsNullOrEmpty(bracketOcoId))
                        bracketOcoId = State == State.Historical
                            ? Guid.NewGuid().ToString("N") : GetAtmStrategyUniqueId();

                    string submitOco = bracketOcoId;
                    Order submittedStop = SubmitOrderUnmanaged(0,
                        isLong ? OrderAction.Sell : OrderAction.BuyToCover,
                        OrderType.StopMarket, qty, 0, stopLevel, submitOco, "HLR Stop");
                    if (submittedStop == null)
                        throw new InvalidOperationException("stop submission returned null");
                    if (IsOrderActive(submittedStop)) stopOrder = submittedStop;
                    if (!IsOrderActive(stopOrder) || _exitPending
                        || Position.MarketPosition == MarketPosition.Flat
                        || !string.Equals(bracketOcoId, submitOco, StringComparison.Ordinal))
                        return;
                }
                else if (GetOrderRemainingQuantity(stopOrder) != qty)
                {
                    int desiredTotal = stopOrder.Filled + qty;
                    ChangeOrder(stopOrder, desiredTotal, 0, stopOrder.StopPrice > 0
                        ? stopOrder.StopPrice : stopLevel);
                }

                if (expectsTarget && !IsOrderActive(targetOrder))
                {
                    targetOrder = null;
                    Order submittedTarget = SubmitOrderUnmanaged(0,
                        isLong ? OrderAction.Sell : OrderAction.BuyToCover,
                        OrderType.Limit, qty, targetLevel, 0, bracketOcoId, "HLR Target");
                    if (submittedTarget == null)
                        throw new InvalidOperationException("target submission returned null");
                    if (IsOrderActive(submittedTarget)) targetOrder = submittedTarget;
                }
                else if (expectsTarget && GetOrderRemainingQuantity(targetOrder) != qty)
                {
                    int desiredTotal = targetOrder.Filled + qty;
                    ChangeOrder(targetOrder, desiredTotal,
                        targetOrder.LimitPrice > 0 ? targetOrder.LimitPrice : targetLevel, 0);
                }

                _pendingBracketPlacement = !IsOrderActive(stopOrder)
                    || GetOrderRemainingQuantity(stopOrder) != qty
                    || (expectsTarget && (!IsOrderActive(targetOrder)
                        || GetOrderRemainingQuantity(targetOrder) != qty));
                _pendingBracketQty = qty;
                _pendingBracketIsLong = isLong;
                _bracketRetryCount = 0;
            }
            catch (Exception ex)
            {
                _pendingBracketPlacement = true;
                _bracketRetryCount++;
                Print($"[HiLoRider] Protection retry {_bracketRetryCount}: {ex.Message}");
                if (_bracketRetryCount >= 3
                    && Position.MarketPosition != MarketPosition.Flat)
                    QueueProtectionFailureExit("protective bracket could not be established");
            }
        }

        internal bool BeginCoordinatedExit(string name, int quantity, bool exitAll)
        {
            if (_exitPending || _platformSessionExitPending
                || _sessionCloseProtectionGracePending
                || Position.MarketPosition == MarketPosition.Flat)
                return false;
            if (_partialProtectiveStopPending)
            {
                Print($"[HiLoRider] Deferred {name} while triggered stop drains.");
                return false;
            }
            _exitPending = true;
            autoExitPending = true;
            _pendingBracketPlacement = false;
            _coordinatedExitName = string.IsNullOrWhiteSpace(name) ? "HLR Exit" : name;
            _coordinatedExitAll = exitAll;
            _coordinatedExitRequestedQty = Math.Max(1, quantity);
            _coordinatedExitFilledQty = 0;
            _coordinatedExitRetryCount = 0;
            _coordinatedExitOrder = null;
            _coordinatedExitPhase = CoordinatedExitPhase.CancelTarget;
            currentRegimeLabel = "EXITING";
            AdvanceCoordinatedExit();
            return true;
        }

        internal void SubmitFullExit(string name)
        {
            BeginCoordinatedExit(name, Math.Max(1, Position.Quantity), true);
        }

        internal bool SubmitPartialExit(string name, int quantity)
        {
            return BeginCoordinatedExit(name, quantity, false);
        }

        private void AdvanceCoordinatedExit()
        {
            if (!_exitPending) return;
            if (Position.MarketPosition == MarketPosition.Flat)
            {
                ClearCoordinatedExitState();
                return;
            }
            while (_exitPending)
            {
                if (_coordinatedExitPhase == CoordinatedExitPhase.CancelTarget)
                {
                    if (IsOrderActive(targetOrder))
                    {
                        if (!IsCancellationPending(targetOrder))
                            try { CancelOrder(targetOrder); }
                            catch (Exception ex) { Print($"[HiLoRider] Target cancel failed: {ex.Message}"); }
                        return;
                    }
                    targetOrder = null;
                    _coordinatedExitPhase = CoordinatedExitPhase.CancelStop;
                    continue;
                }
                if (_coordinatedExitPhase == CoordinatedExitPhase.CancelStop)
                {
                    if (IsOrderActive(stopOrder))
                    {
                        if (!IsCancellationPending(stopOrder))
                            try { CancelOrder(stopOrder); }
                            catch (Exception ex) { Print($"[HiLoRider] Stop cancel failed: {ex.Message}"); }
                        return;
                    }
                    stopOrder = null;
                    bracketOcoId = "";
                    _coordinatedExitPhase = CoordinatedExitPhase.SubmitMarket;
                    continue;
                }
                if (_coordinatedExitPhase == CoordinatedExitPhase.SubmitMarket)
                {
                    int requested = _coordinatedExitAll ? Position.Quantity
                        : Math.Max(0, _coordinatedExitRequestedQty - _coordinatedExitFilledQty);
                    int exitQty = Math.Min(Position.Quantity, requested);
                    if (exitQty <= 0)
                    {
                        CompleteCoordinatedPartialExit();
                        return;
                    }
                    try
                    {
                        bool isLong = Position.MarketPosition == MarketPosition.Long;
                        Order submitted = SubmitOrderUnmanaged(0,
                            isLong ? OrderAction.Sell : OrderAction.BuyToCover,
                            OrderType.Market, exitQty, 0, 0, "", _coordinatedExitName);
                        if (submitted == null)
                            throw new InvalidOperationException("exit submission returned null");
                        if (!_exitPending) return;
                        if (_coordinatedExitOrder == null) _coordinatedExitOrder = submitted;
                        _coordinatedExitPhase = CoordinatedExitPhase.MarketWorking;
                    }
                    catch (Exception ex)
                    {
                        HandleCoordinatedExitFailure("local submit failure: " + ex.Message);
                    }
                    return;
                }
                if (_coordinatedExitPhase == CoordinatedExitPhase.MarketWorking)
                {
                    if (IsOrderActive(_coordinatedExitOrder)) return;
                    if (Position.MarketPosition == MarketPosition.Flat)
                    {
                        ClearCoordinatedExitState();
                        return;
                    }
                    _coordinatedExitPhase = CoordinatedExitPhase.SubmitMarket;
                    continue;
                }
                return;
            }
        }

        private void CompleteCoordinatedPartialExit()
        {
            ClearCoordinatedExitState();
            if (Position.MarketPosition == MarketPosition.Flat) return;
            _pendingBracketQty = Position.Quantity;
            _pendingBracketIsLong = Position.MarketPosition == MarketPosition.Long;
            _pendingBracketPlacement = _pendingBracketQty > 0;
            QueueProtectionReconciliation("coordinated partial exit");
        }

        private void HandleCoordinatedExitFailure(string detail)
        {
            _coordinatedExitOrder = null;
            _coordinatedExitRetryCount++;
            Print($"[HiLoRider] Exit attempt {_coordinatedExitRetryCount} failed: {detail}");
            if (_coordinatedExitRetryCount < 3
                && Position.MarketPosition != MarketPosition.Flat)
            {
                _coordinatedExitPhase = CoordinatedExitPhase.SubmitMarket;
                AdvanceCoordinatedExit();
                return;
            }
            _autoPaused = true;
            _autoPausedReason = "EXIT REJECTED - CHECK POSITION";
            currentRegimeLabel = _autoPausedReason;
            ClearCoordinatedExitState();
            if (Position.MarketPosition != MarketPosition.Flat)
                QueueProtectionReconciliation("exit rejected");
        }

        private void ClearCoordinatedExitState()
        {
            _exitPending = false;
            autoExitPending = false;
            _coordinatedExitPhase = CoordinatedExitPhase.None;
            if (!IsOrderActive(_coordinatedExitOrder)) _coordinatedExitOrder = null;
            _coordinatedExitName = "";
            _coordinatedExitAll = false;
            _coordinatedExitRequestedQty = 0;
            _coordinatedExitFilledQty = 0;
            _coordinatedExitRetryCount = 0;
        }

        internal void RequestStopChange(double candidate, bool isAutomaticTrail)
        {
            if (_exitPending || _platformSessionExitPending
                || _sessionCloseProtectionGracePending
                || _partialProtectiveStopPending
                || _protectionFailureExitQueued
                || !IsOrderActive(stopOrder) || _stopChangePending)
                return;
            bool isLong = tradeDir == 1;
            bool improved = isLong ? candidate > stopLevel : candidate < stopLevel;
            if (!improved) return;
            if (ExitInsteadOfCrossedProtectiveStop(isLong, candidate, "stop change")) return;
            _requestedStopLevel = candidate;
            _stopChangePending = true;
            _requestedStopChangeIsTrail = isAutomaticTrail;
            try
            {
                ChangeOrder(stopOrder, stopOrder.Quantity, 0, candidate);
            }
            catch (Exception ex)
            {
                _requestedStopLevel = 0;
                _stopChangePending = false;
                _requestedStopChangeIsTrail = false;
                Print($"[HiLoRider] Stop change failed locally: {ex.Message}");
            }
        }

        internal void HandleSafetyOrderUpdate(Order order, double limitPrice,
            double stopPrice, int quantity, int filled, double averageFillPrice,
            OrderState orderState, DateTime time, ErrorCode error, string nativeError)
        {
            if (order == null) return;
            if (State == State.Realtime)
            {
                if (entryOrder != null && entryOrder.IsBacktestOrder) entryOrder = GetRealtimeOrder(entryOrder);
                if (stopOrder != null && stopOrder.IsBacktestOrder) stopOrder = GetRealtimeOrder(stopOrder);
                if (targetOrder != null && targetOrder.IsBacktestOrder) targetOrder = GetRealtimeOrder(targetOrder);
            }

            string n = order.Name ?? "";
            bool isEntry = n == "HLR LE" || n == "HLR SE" || n.StartsWith("HLR Add1", StringComparison.Ordinal);
            bool isPlatformExit = IsPlatformSessionCloseExit(n);
            bool isCoordinated = _exitPending && n == _coordinatedExitName;

            if (isPlatformExit)
            {
                _platformSessionExitOrder = order;
                ClearSessionCloseProtectionGrace();
                if (orderState == OrderState.Cancelled || orderState == OrderState.Rejected)
                {
                    _platformSessionExitPending = false;
                    _platformSessionExitOrder = null;
                    if (Position.MarketPosition != MarketPosition.Flat && !IsOrderActive(stopOrder))
                        QueueProtectionFailureExit("session-close exit failed with no active stop");
                }
                else
                {
                    _platformSessionExitPending = true;
                    _pendingBracketPlacement = false;
                    currentRegimeLabel = "SESSION EXITING";
                }
            }

            if (n == "HLR Stop")
            {
                stopOrder = order;
                bool stopUpdateAccepted = error == ErrorCode.NoError
                    && (orderState == OrderState.Accepted || orderState == OrderState.AcceptedByRisk
                    || orderState == OrderState.Working || orderState == OrderState.PartFilled)
                    && stopPrice > 0;
                if (stopUpdateAccepted)
                {
                    double acceptedStop = Instrument.MasterInstrument.RoundToTickSize(stopPrice);
                    if (_stopChangePending && _requestedStopChangeIsTrail
                        && filledPrice > 0 && _initialAcceptedStopLevel > 0 && TickSize > 0)
                    {
                        _acceptedTrailChanges++;
                        double initialRiskTicks = Math.Abs(filledPrice - _initialAcceptedStopLevel) / TickSize;
                        double signedStopTicks = tradeDir == 1
                            ? (acceptedStop - filledPrice) / TickSize
                            : (filledPrice - acceptedStop) / TickSize;
                        _maxTrailRiskReductionTicks = Math.Max(
                            _maxTrailRiskReductionTicks, initialRiskTicks + signedStopTicks);
                    }
                    stopLevel = acceptedStop;
                    if (_initialAcceptedStopLevel <= 0) _initialAcceptedStopLevel = stopLevel;
                    _requestedStopLevel = 0;
                    _stopChangePending = false;
                    _requestedStopChangeIsTrail = false;
                }
                else if (_stopChangePending && error != ErrorCode.NoError)
                {
                    // NinjaTrader can reject a change while returning the original
                    // protective order in Accepted state. Never count that callback
                    // as an accepted trail or overwrite stopLevel with the old price.
                    // A crossed-market rejection means price has already passed the
                    // requested protective level, so queue exactly one coordinated
                    // fail-safe exit while the original stop remains active.
                    double rejectedStop = _requestedStopLevel;
                    _requestedStopLevel = 0;
                    _stopChangePending = false;
                    _requestedStopChangeIsTrail = false;
                    if (IsCrossedMarketStopChangeError(error, nativeError)
                        && Position.MarketPosition != MarketPosition.Flat)
                    {
                        _pendingBracketPlacement = false;
                        Print($"[HiLoRider] Stop change {rejectedStop:F2} crossed the market; retaining accepted stop and queuing one fail-safe exit.");
                        QueueProtectionFailureExit("protective stop change crossed market");
                    }
                }
            }
            else if (n == "HLR Target")
            {
                targetOrder = order;
                if ((orderState == OrderState.Accepted || orderState == OrderState.AcceptedByRisk
                    || orderState == OrderState.Working || orderState == OrderState.PartFilled)
                    && limitPrice > 0)
                {
                    targetLevel = Instrument.MasterInstrument.RoundToTickSize(limitPrice);
                    if (_initialAcceptedTargetLevel <= 0) _initialAcceptedTargetLevel = targetLevel;
                }
            }
            else if (isCoordinated)
                _coordinatedExitOrder = order;

            if (error != ErrorCode.NoError)
                Print($"[HiLoRider] Order error {n}: {error} {nativeError}");

            if (isEntry && orderState == OrderState.Filled) entryOrder = null;
            if (isEntry && (orderState == OrderState.Cancelled || orderState == OrderState.Rejected))
            {
                entryOrder = null;
                if (_entryFillQty == 0) ResetExecutionSafetyAfterFlat();
                else QueueProtectionReconciliation("entry terminal after partial fill");
            }

            if ((n == "HLR Stop" || n == "HLR Target")
                && (orderState == OrderState.Cancelled || orderState == OrderState.Rejected))
            {
                bool peerFillAwaiting = IsBracketPeerFillAwaitingExecution(n, orderState);
                if (n == "HLR Stop")
                {
                    stopOrder = null;
                    _requestedStopLevel = 0;
                    _stopChangePending = false;
                    _requestedStopChangeIsTrail = false;
                }
                else targetOrder = null;

                int remaining = Math.Max(0, _entryFillQty - _exitFillQty);
                if (remaining > 0 && !_exitPending && !_platformSessionExitPending)
                {
                    _pendingBracketQty = Math.Min(remaining, Math.Max(0, Position.Quantity));
                    _pendingBracketIsLong = tradeDir == 1;
                    _pendingBracketPlacement = false;
                    if (peerFillAwaiting)
                    {
                        Print($"[HiLoRider] {n} {orderState} after filled OCO peer; waiting for execution.");
                    }
                    else if (_partialProtectiveStopPending)
                    {
                        if (n == "HLR Stop")
                            QueuePartialStopTerminalReconciliation("protective stop " + orderState);
                    }
                    else if (IsWithinPlatformSessionCloseWindow(time))
                    {
                        ArmSessionCloseProtectionGrace(n + " " + orderState, time);
                    }
                    else if (n == "HLR Target")
                    {
                        HandleTerminalTargetLoss(n + " " + orderState);
                    }
                    else
                    {
                        QueueProtectionFailureExit(n + " " + orderState + "; stop terminal");
                    }
                }
                if (_exitPending) QueueCoordinatedExitAdvance();
            }

            if (isCoordinated
                && (orderState == OrderState.Cancelled || orderState == OrderState.Rejected))
                HandleCoordinatedExitFailure((orderState + ": " + error + " " + nativeError).Trim());
        }

        protected override void OnExecutionUpdate(Execution execution, string executionId,
            double price, int quantity, MarketPosition marketPosition,
            string orderId, DateTime time)
        {
            if (execution?.Order == null || quantity <= 0) return;
            string name = execution.Order.Name ?? "";
            bool entryExecution = name == "HLR LE" || name == "HLR SE"
                || name.StartsWith("HLR Add1", StringComparison.Ordinal);
            bool platformExit = IsPlatformSessionCloseExit(name);

            if (platformExit)
            {
                _platformSessionExitOrder = execution.Order;
                _platformSessionExitPending = true;
                ClearSessionCloseProtectionGrace();
                _pendingBracketPlacement = false;
            }

            if (entryExecution)
            {
                bool isLong = execution.Order.OrderAction == OrderAction.Buy;
                if (_entryFillQty == 0)
                {
                    tradeDir = isLong ? 1 : -1;
                    entryBarsInTrade = 0;
                    stagedTrailStage = 0;
                    _hlPeakPrice = 0;
                    _emergencyStopSubmitted = false;
                    _newSOEntryBar = CurrentBar;
                    if (!_tradeCounted)
                    {
                        _tradesThisSession++;
                        _tradeCounted = true;
                    }
                }
                _entryFillValue += price * quantity;
                _entryFillQty += quantity;
                filledPrice = _entryFillValue / _entryFillQty;
                bePriceTrigger = filledPrice + (tradeDir == 1 ? 1 : -1) * BE_TriggerTicks * TickSize;
                hudHighSinceEntry = filledPrice;
                hudLowSinceEntry = filledPrice;
                _pendingBracketIsLong = isLong;
                _pendingBracketQty = Math.Max(0, _entryFillQty - _exitFillQty);
                _pendingBracketPlacement = _pendingBracketQty > 0;
                currentRegimeLabel = isLong ? "IN TRADE ▲" : "IN TRADE ▼";
                if (!_exitPending) ReconcileProtection(isLong, _pendingBracketQty);
                else QueueCoordinatedExitAdvance();
                UpdateLivePanel();
                return;
            }

            if (tradeDir == 0 || _entryFillQty <= 0)
            {
                if ((name.StartsWith("HLR ", StringComparison.Ordinal) || platformExit)
                    && marketPosition != MarketPosition.Flat)
                    QueueManagedPositionRecovery(name);
                return;
            }
            bool reduces = tradeDir == 1
                ? execution.Order.OrderAction == OrderAction.Sell
                : execution.Order.OrderAction == OrderAction.BuyToCover;
            if (!reduces) return;

            _exitFillValue += price * quantity;
            _exitFillQty += quantity;
            RecordStrategyRealizedExecution(price, quantity);
            AppendExitReason(NormalizeExitReason(name));
            if (_exitPending) _coordinatedExitFilledQty += quantity;
            int open = Math.Max(0, _entryFillQty - _exitFillQty);

            if (open > 0)
            {
                _pendingBracketQty = Math.Min(open, Math.Max(0, Position.Quantity));
                _pendingBracketIsLong = tradeDir == 1;
                if (platformExit)
                {
                    _pendingBracketPlacement = false;
                    currentRegimeLabel = "SESSION EXITING";
                    return;
                }
                if (name == "HLR Stop")
                {
                    _pendingBracketPlacement = false;
                    _partialProtectiveStopPending = true;
                    _partialStopTerminalReconcileQueued = false;
                    currentRegimeLabel = "STOP FILLING";
                    return;
                }
                if (_partialProtectiveStopPending)
                {
                    _pendingBracketPlacement = false;
                    return;
                }
                if (_exitPending)
                {
                    if (!_coordinatedExitAll
                        && _coordinatedExitFilledQty >= _coordinatedExitRequestedQty)
                        CompleteCoordinatedPartialExit();
                    else QueueCoordinatedExitAdvance();
                }
                else if (name == "HLR Target" && !IsOrderActive(execution.Order))
                    QueueProtectionFailureExit("filled target left residual position");
                else
                    QueueProtectionReconciliation("partial " + name + " fill");
                return;
            }

            _pendingBracketPlacement = false;
            _partialProtectiveStopPending = false;
            _platformSessionExitPending = false;
            ClearSessionCloseProtectionGrace();
            ClearCoordinatedExitState();
            if (IsOrderActive(stopOrder)) try { CancelOrder(stopOrder); } catch { }
            if (IsOrderActive(targetOrder)) try { CancelOrder(targetOrder); } catch { }
            if (marketPosition != MarketPosition.Flat
                || Position.MarketPosition != MarketPosition.Flat)
                QueueManagedPositionRecovery(name);
        }

        private void AppendExitReason(string reason)
        {
            if (string.IsNullOrWhiteSpace(reason)) return;
            foreach (string existing in _exitFillReason.Split(new[] { '+' },
                StringSplitOptions.RemoveEmptyEntries))
                if (existing.Trim() == reason) return;
            _exitFillReason = string.IsNullOrEmpty(_exitFillReason)
                ? reason : _exitFillReason + "+" + reason;
        }

        private static string NormalizeExitReason(string orderName)
        {
            if (orderName == "HLR Stop") return "Stop Hit";
            if (orderName == "HLR Target") return "Target Hit";
            if (orderName == "MaxBars Exit") return "TimeStop";
            if (orderName.IndexOf("DiffX", StringComparison.Ordinal) >= 0) return "DiffCross";
            if (orderName.IndexOf("Close1", StringComparison.Ordinal) >= 0) return "Close1";
            if (string.IsNullOrWhiteSpace(orderName)) return "ExternalExit";
            return orderName.StartsWith("HLR ", StringComparison.Ordinal)
                ? orderName.Substring(3) : orderName;
        }

        private void QueueCoordinatedExitAdvance()
        {
            try { TriggerCustomEvent(_ => AdvanceCoordinatedExit(), null); }
            catch (Exception ex) { Print($"[HiLoRider] Exit reconciliation queue failed: {ex.Message}"); }
        }

        private void QueueProtectionReconciliation(string detail)
        {
            if (!string.IsNullOrWhiteSpace(detail)) _bracketReconcileReason = detail;
            if (_bracketReconcileQueued) return;
            _bracketReconcileQueued = true;
            try
            {
                TriggerCustomEvent(_ =>
                {
                    _bracketReconcileQueued = false;
                    string reason = _bracketReconcileReason;
                    _bracketReconcileReason = "";
                    if (_exitPending || _protectionFailureExitQueued
                        || _platformSessionExitPending || _sessionCloseProtectionGracePending
                        || _partialProtectiveStopPending
                        || Position.MarketPosition == MarketPosition.Flat)
                        return;
                    int ledger = Math.Max(0, _entryFillQty - _exitFillQty);
                    int remaining = Math.Min(ledger, Math.Max(0, Position.Quantity));
                    if (remaining <= 0 || tradeDir == 0 || filledPrice <= 0) return;
                    _pendingBracketQty = remaining;
                    _pendingBracketIsLong = Position.MarketPosition == MarketPosition.Long;
                    _pendingBracketPlacement = true;
                    if (!IsOrderActive(stopOrder))
                    {
                        _pendingBracketPlacement = false;
                        QueueProtectionFailureExit(reason + "; no active protective stop");
                        return;
                    }
                    if (State != State.Historical && TargetMode != RPTargetMode.NoTarget
                        && targetLevel > 0 && !IsOrderActive(targetOrder))
                    {
                        _pendingBracketPlacement = false;
                        HandleTerminalTargetLoss(reason + "; no active target");
                        return;
                    }
                    ReconcileProtection(_pendingBracketIsLong, remaining);
                }, null);
            }
            catch (Exception ex)
            {
                _bracketReconcileQueued = false;
                Print($"[HiLoRider] Protection reconciliation queue failed: {ex.Message}");
            }
        }

        private void HandleTerminalTargetLoss(string detail)
        {
            _pendingBracketPlacement = false;
            if (!IsOrderActive(stopOrder))
            {
                QueueProtectionFailureExit(detail + "; target and stop terminal");
                return;
            }
            _autoPaused = true;
            _autoPausedReason = "TARGET LOST - STOP ACTIVE";
            currentRegimeLabel = _autoPausedReason;
            Print($"[HiLoRider] {detail}; retaining accepted stop {stopLevel:F2}. Strategy paused.");
        }

        private void QueueProtectionFailureExit(string detail)
        {
            if (_protectionFailureExitQueued || _exitPending
                || _platformSessionExitPending || _sessionCloseProtectionGracePending
                || IsOrderActive(_platformSessionExitOrder))
                return;
            _protectionFailureExitQueued = true;
            try
            {
                TriggerCustomEvent(_ =>
                {
                    _protectionFailureExitQueued = false;
                    if (Position.MarketPosition == MarketPosition.Flat
                        || _platformSessionExitPending || _sessionCloseProtectionGracePending)
                        return;
                    Print($"[HiLoRider] Protection failure ({detail}); starting fail-safe exit.");
                    BeginCoordinatedExit("HLR ProtectionFail", Position.Quantity, true);
                }, null);
            }
            catch (Exception ex)
            {
                _protectionFailureExitQueued = false;
                Print($"[HiLoRider] Protection fail-safe queue failed: {ex.Message}");
            }
        }

        private void QueuePartialStopTerminalReconciliation(string detail)
        {
            if (!_partialProtectiveStopPending || _partialStopTerminalReconcileQueued) return;
            _partialStopTerminalReconcileQueued = true;
            try
            {
                TriggerCustomEvent(_ =>
                {
                    _partialStopTerminalReconcileQueued = false;
                    if (!_partialProtectiveStopPending) return;
                    int remaining = Math.Max(0, _entryFillQty - _exitFillQty);
                    if (remaining <= 0 || Position.MarketPosition == MarketPosition.Flat) return;
                    if (IsOrderActive(stopOrder)) return;
                    _partialProtectiveStopPending = false;
                    BeginCoordinatedExit("HLR StopResidual",
                        Math.Min(remaining, Position.Quantity), true);
                }, null);
            }
            catch (Exception ex)
            {
                _partialStopTerminalReconcileQueued = false;
                Print($"[HiLoRider] Partial-stop reconciliation queue failed: {ex.Message}");
            }
        }

        private void QueueManagedPositionRecovery(string sourceOrderName)
        {
            if (_managedRecoveryRequested) return;
            _managedRecoveryRequested = true;
            try
            {
                TriggerCustomEvent(_ =>
                {
                    if (Position.MarketPosition == MarketPosition.Flat)
                    {
                        _managedRecoveryRequested = false;
                        return;
                    }
                    Print($"[HiLoRider] Unexpected residual after {sourceOrderName}; recovering position.");
                    if (!_exitPending)
                        BeginCoordinatedExit("HLR PositionRecovery", Position.Quantity, true);
                    else AdvanceCoordinatedExit();
                }, null);
            }
            catch (Exception ex)
            {
                _managedRecoveryRequested = false;
                Print($"[HiLoRider] Position recovery queue failed: {ex.Message}");
            }
        }

        internal void ResetExecutionSafetyAfterFlat(bool positionCallbackConfirmedFlat = false)
        {
            // Normal callers still require the current strategy Position to be
            // flat. OnPositionUpdate may additionally pass its authoritative
            // immutable callback snapshot so a same-cycle entry/exit cannot be
            // rejected because the mutable Position object is temporarily stale.
            if (!positionCallbackConfirmedFlat
                && Position.MarketPosition != MarketPosition.Flat) return;
            if (IsOrderActive(stopOrder)) try { CancelOrder(stopOrder); } catch { }
            if (IsOrderActive(targetOrder)) try { CancelOrder(targetOrder); } catch { }
            if (IsOrderActive(entryOrder)) try { CancelOrder(entryOrder); } catch { }
            entryOrder = null;
            stopOrder = null;
            targetOrder = null;
            bracketOcoId = "";
            _pendingBracketPlacement = false;
            _pendingBracketQty = 0;
            _bracketRetryCount = 0;
            _bracketReconcileQueued = false;
            _bracketReconcileReason = "";
            _protectionFailureExitQueued = false;
            _partialProtectiveStopPending = false;
            _partialStopTerminalReconcileQueued = false;
            _platformSessionExitPending = false;
            _platformSessionExitOrder = null;
            ClearSessionCloseProtectionGrace();
            _initialAcceptedStopLevel = 0;
            _initialAcceptedTargetLevel = 0;
            _acceptedTrailChanges = 0;
            _maxTrailRiskReductionTicks = 0;
            _requestedStopLevel = 0;
            _stopChangePending = false;
            _requestedStopChangeIsTrail = false;
            _managedRecoveryRequested = false;
            tradeDir = 0;
            ResetExecutionLedgerOnly();
            ClearCoordinatedExitState();
        }

        private void ResetExecutionLedgerOnly()
        {
            _entryFillQty = 0;
            _entryFillValue = 0;
            _exitFillQty = 0;
            _exitFillValue = 0;
            _exitFillReason = "";
            _tradeCounted = false;
        }

        internal void TrackLiquidityMarketData(MarketDataEventArgs update)
        {
            if (update == null) return;
            DateTime eventTime = update.Time;
            if (_lastLiquidityEventTime != DateTime.MinValue
                && eventTime < _lastLiquidityEventTime)
                ResetLiquidityMarketState();
            _lastLiquidityEventTime = eventTime;
            if (update.MarketDataType == MarketDataType.Bid) { _lastBid = update.Price; _lastBidTime = eventTime; }
            else if (update.MarketDataType == MarketDataType.Ask) { _lastAsk = update.Price; _lastAskTime = eventTime; }
            else if (update.MarketDataType == MarketDataType.Last && update.Volume > 0)
            {
                _recentLastTrades.Enqueue(new LiquidityTradeEvent { Time = eventTime, Volume = update.Volume });
                _recentLastTradeVolume5s += update.Volume;
                _recentLastTradeCount5s++;
            }
            PruneLiquidityWindow(eventTime);
        }

        private void ResetLiquidityMarketState()
        {
            _recentLastTrades.Clear();
            _recentLastTradeVolume5s = 0;
            _recentLastTradeCount5s = 0;
            _lastLiquidityEventTime = DateTime.MinValue;
            _lastBid = 0;
            _lastAsk = 0;
            _lastBidTime = DateTime.MinValue;
            _lastAskTime = DateTime.MinValue;
        }

        private void PruneLiquidityWindow(DateTime now)
        {
            DateTime cutoff = now.AddSeconds(-5);
            while (_recentLastTrades.Count > 0 && _recentLastTrades.Peek().Time < cutoff)
            {
                LiquidityTradeEvent old = _recentLastTrades.Dequeue();
                _recentLastTradeVolume5s -= old.Volume;
                _recentLastTradeCount5s--;
            }
            if (_recentLastTradeVolume5s < 0) _recentLastTradeVolume5s = 0;
            if (_recentLastTradeCount5s < 0) _recentLastTradeCount5s = 0;
        }

        internal bool LiquidityFilterPasses(out string blockReason)
        {
            blockReason = "";
            _snapSignalBid = _lastBid > 0 ? _lastBid : GetCurrentBid();
            _snapSignalAsk = _lastAsk > 0 ? _lastAsk : GetCurrentAsk();
            _snapRecentLastTradeActivity();
            _snapSignalQuoteValid = IsFiniteNumber(_snapSignalBid) && IsFiniteNumber(_snapSignalAsk)
                && TickSize > 0 && _snapSignalBid > 0 && _snapSignalAsk > 0
                && _snapSignalAsk >= _snapSignalBid
                && _lastBidTime != DateTime.MinValue && _lastAskTime != DateTime.MinValue
                && Math.Abs((_lastLiquidityEventTime - _lastBidTime).TotalSeconds) <= 5
                && Math.Abs((_lastLiquidityEventTime - _lastAskTime).TotalSeconds) <= 5;
            _snapSpreadTicks = _snapSignalQuoteValid
                ? (_snapSignalAsk - _snapSignalBid) / TickSize : 0;
            if (MaxEntrySpreadTicks <= 0) return true;
            if (!_snapSignalQuoteValid)
            {
                blockReason = "invalid-quotes";
                _snapLiquidityBlockReason = blockReason;
                return false;
            }
            double tolerance = 1e-9;
            if (_snapSpreadTicks - MaxEntrySpreadTicks > tolerance)
            {
                blockReason = "spread>" + MaxEntrySpreadTicks.ToString(CultureInfo.InvariantCulture);
                _snapLiquidityBlockReason = blockReason;
                return false;
            }
            _snapLiquidityBlockReason = "";
            return true;
        }

        private void _snapRecentLastTradeActivity()
        {
            DateTime now = _lastLiquidityEventTime != DateTime.MinValue
                ? _lastLiquidityEventTime : DateTime.Now;
            PruneLiquidityWindow(now);
            _snapRecentTradeVolume5s = _recentLastTradeVolume5s;
            _snapRecentTradeCount5s = _recentLastTradeCount5s;
        }

        private static bool IsFiniteNumber(double value)
        {
            return !double.IsNaN(value) && !double.IsInfinity(value);
        }

        internal void LogLiquiditySignalCandidate(int signal, bool passed, string blockReason)
        {
            if (!EnableTradeLogging) return;
            if (State == State.Historical && !EnableHistoricalTradeLogging) return;
            if (State == State.Historical
                && !string.Equals(Account?.Name, "Playback101", StringComparison.OrdinalIgnoreCase))
                return;
            try
            {
                string dir = string.IsNullOrWhiteSpace(TradeLogPath)
                    ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
                        "NinjaTrader 8", "HiLoRiderLogs") : TradeLogPath;
                dir = Path.Combine(dir, string.IsNullOrWhiteSpace(_runId) ? "UNASSIGNED_RUN" : _runId);
                Directory.CreateDirectory(dir);
                string file = Path.Combine(dir, State == State.Historical
                    ? "HiLoRiderLiquiditySignals_backtest.jsonl"
                    : "HiLoRiderLiquiditySignals.jsonl");
                string json = "{"
                    + "\"EventType\":\"LiquiditySignal\","
                    + "\"RunId\":" + SafetyJsonString(_runId) + ","
                    + "\"InstanceTag\":" + SafetyJsonString(_effectiveInstanceTag) + ","
                    + "\"StrategyVersion\":" + SafetyJsonString(StrategyVersion) + ","
                    + "\"FeatureVersion\":" + SafetyJsonString(FeatureVersion) + ","
                    + "\"ConfigHash\":" + SafetyJsonString(GetConfigHash()) + ","
                    + "\"Instrument\":" + SafetyJsonString(Instrument?.FullName) + ","
                    + "\"Account\":" + SafetyJsonString(Account?.Name) + ","
                    + "\"SignalTime\":" + SafetyJsonString(Time[0].ToString("o")) + ","
                    + "\"Direction\":" + signal.ToString(CultureInfo.InvariantCulture) + ","
                    + "\"SignalBid\":" + JsonNumber(_snapSignalBid) + ","
                    + "\"SignalAsk\":" + JsonNumber(_snapSignalAsk) + ","
                    + "\"SpreadTicks\":" + JsonNumber(_snapSpreadTicks) + ","
                    + "\"QuoteValid\":" + (_snapSignalQuoteValid ? "true" : "false") + ","
                    + "\"MaxEntrySpreadTicks\":" + MaxEntrySpreadTicks.ToString(CultureInfo.InvariantCulture) + ","
                    + "\"RecentTradeVolume5s\":" + JsonNumber(_snapRecentTradeVolume5s) + ","
                    + "\"RecentTradeCount5s\":" + _snapRecentTradeCount5s.ToString(CultureInfo.InvariantCulture) + ","
                    + "\"Pass\":" + (passed ? "true" : "false") + ","
                    + "\"BlockReason\":" + SafetyJsonString(blockReason) + "}";
                lock (LiquidityLogLock)
                    File.AppendAllText(file, json + Environment.NewLine, Encoding.UTF8);
            }
            catch (Exception ex)
            {
                Print($"[HiLoRider] Liquidity telemetry error: {ex.Message}");
            }
        }

        private static string JsonNumber(double value)
        {
            return IsFiniteNumber(value) ? value.ToString("R", CultureInfo.InvariantCulture) : "null";
        }

        private static string SafetyJsonString(string value)
        {
            string s = value ?? "";
            return "\"" + s.Replace("\\", "\\\\").Replace("\"", "\\\"")
                .Replace("\r", "\\r").Replace("\n", "\\n") + "\"";
        }
    }
}
