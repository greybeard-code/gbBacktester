// file name = HiLoRiderTradeLogger.cs
// HiLoRider — JSONL / CSV trade logger.
//
// Writes to two sets of files depending on run mode:
//   Live / Market Replay (State.Realtime):
//     HiLoRider_trades.jsonl / .csv          — per-partial fills
//     HiLoRider_summary.jsonl                — one row per closed position
//   Strategy Analyzer (State.Historical):
//     HiLoRider_trades_backtest.jsonl / .csv — per-partial fills
//     HiLoRider_summary_backtest.jsonl       — one row per closed position
//
// InitializeTradeLoggers() is called in State.DataLoaded so both modes
// always have open streams.  TryLogTrade / TryLogChartTradeSummary route
// by State and write to the appropriate stream.

#region Using declarations
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using NinjaTrader.Cbi;
using NinjaTrader.NinjaScript.Strategies;
#endregion

namespace NinjaTrader.NinjaScript.Strategies.HiLoRider
{
    // ── Lightweight JSONL logger ───────────────────────────────────────────────
    internal class HiLoRiderJsonLogger : IDisposable
    {
        private readonly string _path;
        private StreamWriter    _sw;
        private readonly object _lock = new object();

        public HiLoRiderJsonLogger(string path)
        {
            _path = path;
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(path));
                _sw = new StreamWriter(path, append: true, encoding: Encoding.UTF8) { AutoFlush = true };
            }
            catch (Exception ex) { System.Diagnostics.Trace.WriteLine($"[HiLoRiderJsonLogger] Failed to open {path}: {ex.Message}"); }
        }

        public void WriteLine(string json)
        {
            lock (_lock) { try { _sw?.WriteLine(json); } catch { } }
        }

        public void Dispose() { try { _sw?.Dispose(); } catch { } }
    }

    // ── Lightweight CSV logger ─────────────────────────────────────────────────
    internal class HiLoRiderCsvLogger : IDisposable
    {
        private readonly string _path;
        private StreamWriter    _sw;
        private readonly object _lock = new object();
        private static readonly string Header =
            "Strategy,StrategyVersion,FeatureVersion,RunId,InstanceTag,ConfigHash,EntryProvenance,EntrySignalSource,Instrument,Account,BarsPeriod,TradingHours,TradeNumber,EntryTime,ExitTime,Direction," +
            "EntryPrice,ExitPrice,ProfitTicks,ProfitCurrency,Commission," +
            "InitialSLTicks,InitialTPTicks,FinalStopTicksFromEntry,AcceptedTrailChanges,MaxTrailRiskReductionTicks,MfeTicks,MaeTicks,BreakevenRealized,ExitName," +
            "ADX_at_Entry,Diff_at_Entry,Bar1RangeTicks,Bar0RangeTicks," +
            "Mode," +
            "MLFeature0,MLFeature1,MLFeature2,MLFeature3,MLFeature4,MLFeature5,MLFeature6,MLFeature7";

        public HiLoRiderCsvLogger(string path)
        {
            _path = path;
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(path));
                bool exists = File.Exists(path);
                _sw = new StreamWriter(path, append: true, encoding: Encoding.UTF8) { AutoFlush = true };
                if (!exists) _sw.WriteLine(Header);
            }
            catch (Exception ex) { System.Diagnostics.Trace.WriteLine($"[HiLoRiderCsvLogger] Failed to open {path}: {ex.Message}"); }
        }

        public void WriteLine(string csv)
        {
            lock (_lock) { try { _sw?.WriteLine(csv); } catch { } }
        }

        public void Dispose() { try { _sw?.Dispose(); } catch { } }
    }

    public partial class HiLoRider : Strategy
    {
        // ── Live / Market Replay streams ──────────────────────────────────────
        private HiLoRiderJsonLogger _jsonLogger        = null;
        private HiLoRiderCsvLogger  _csvLogger         = null;
        private HiLoRiderJsonLogger _jsonSummaryLogger = null;

        // ── Strategy Analyzer / Historical streams ────────────────────────────
        private HiLoRiderJsonLogger _jsonBtLogger        = null;
        private HiLoRiderCsvLogger  _csvBtLogger         = null;
        private HiLoRiderJsonLogger _jsonBtSummaryLogger = null;

        // ── Historical dedup sets (prevents re-append on repeated backtest runs) ──
        private HashSet<string> _btLoggedKeys        = new HashSet<string>();
        private HashSet<string> _btSummaryLoggedKeys = new HashSet<string>();

        private int _tradeNumber   = 0;
        private int _btTradeNumber = 0;

        // ─────────────────────────────────────────────────────────────────────
        // InitializeTradeLoggers — call from State.DataLoaded so both
        // Historical and Realtime runs have streams ready.
        // ─────────────────────────────────────────────────────────────────────
        internal void InitializeTradeLoggers()
        {
            // Jul 2026: this used to return silently here with no Print at all when
            // EnableTradeLogging was false -- see CLAUDE.md "Reverter -- Live Trades
            // Not Logging" for the discovery (real position closes with zero JSONL/
            // CSV rows and zero errors anywhere, root-caused to a saved chart/
            // workspace instance silently carrying a stale false value forward).
            if (!EnableTradeLogging)
            {
                Print("[HiLoRider] Trade logging is DISABLED (EnableTradeLogging=false) -- " +
                      "no live/replay trades will be written to HiLoRider_trades.jsonl/.csv. " +
                      "Use the TRADE LOG ON/OFF button in the panel to turn it back on.");
                return;
            }
            try
            {
                string dir = string.IsNullOrWhiteSpace(TradeLogPath)
                    ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
                                   "NinjaTrader 8", "HiLoRiderLogs")
                    : TradeLogPath;

                // Every strategy instance writes to its own immutable run folder.
                dir = Path.Combine(dir, string.IsNullOrWhiteSpace(_runId) ? "UNASSIGNED_RUN" : _runId);

                if (!Directory.Exists(dir))
                    Directory.CreateDirectory(dir);

                // ── Live / Market Replay ───────────────────────────────────────
                _jsonLogger        = new HiLoRiderJsonLogger(Path.Combine(dir, "HiLoRider_trades.jsonl"));
                _csvLogger         = new HiLoRiderCsvLogger (Path.Combine(dir, "HiLoRider_trades.csv"));
                _jsonSummaryLogger = new HiLoRiderJsonLogger(Path.Combine(dir, "HiLoRider_summary.jsonl"));

                // ── Strategy Analyzer / Historical ─────────────────────────────
                if (EnableHistoricalTradeLogging)
                {
                    _jsonBtLogger        = new HiLoRiderJsonLogger(Path.Combine(dir, "HiLoRider_trades_backtest.jsonl"));
                    _btLoggedKeys        = LoadExistingBacktestKeys(Path.Combine(dir, "HiLoRider_trades_backtest.jsonl"));
                    _csvBtLogger         = new HiLoRiderCsvLogger (Path.Combine(dir, "HiLoRider_trades_backtest.csv"));
                    _jsonBtSummaryLogger = new HiLoRiderJsonLogger(Path.Combine(dir, "HiLoRider_summary_backtest.jsonl"));
                    _btSummaryLoggedKeys = LoadExistingBacktestKeys(Path.Combine(dir, "HiLoRider_summary_backtest.jsonl"));
                }

                Print($"[HiLoRider] Trade logging initialised → {dir}");
                Print($"[HiLoRider]   Live JSONL     : HiLoRider_trades.jsonl");
                Print($"[HiLoRider]   Backtest JSONL : HiLoRider_trades_backtest.jsonl");
            }
            catch (Exception ex)
            {
                Print($"[HiLoRider Logger] Init error: {ex.Message}");
            }
        }

        internal void DisposeTradeLoggers()
        {
            try { _jsonLogger?.Dispose(); } catch { }
            try { _csvLogger?.Dispose(); } catch { }
            try { _jsonSummaryLogger?.Dispose(); } catch { }
            try { _jsonBtLogger?.Dispose(); } catch { }
            try { _csvBtLogger?.Dispose(); } catch { }
            try { _jsonBtSummaryLogger?.Dispose(); } catch { }
            _jsonLogger = null; _csvLogger = null; _jsonSummaryLogger = null;
            _jsonBtLogger = null; _csvBtLogger = null; _jsonBtSummaryLogger = null;
        }

        // ─────────────────────────────────────────────────────────────────────
        // TryLogTrade — one row per NT8 Trade (partial fill)
        // Routes: Realtime → live streams | Historical → backtest streams
        // ─────────────────────────────────────────────────────────────────────
        internal void TryLogTrade(Trade t, double mfeTicks, double maeTicks, double snapFilledPrice)
        {
            if (!EnableTradeLogging) return;
            if (State == State.Historical && !EnableHistoricalTradeLogging) return;

                        // Aug 2026: State.Historical fires every time this strategy is simply
            // enabled/added to a live chart (NT8 always processes the chart's
            // already-loaded historical bars first, regardless of Market Replay),
            // not just during a genuine Playback101 backtest run -- both report
            // State.Historical identically. Without gating on the connected
            // account, every enable/re-enable silently appended a fresh batch of
            // routine historical-catch-up trades to the backtest log, indistinguishable
            // from real Market Replay results. Only Account.Name=="Playback101" is
            // treated as a real backtest from here on.
            if (State == State.Historical
                && !string.Equals(Account?.Name, "Playback101", StringComparison.OrdinalIgnoreCase))
                return;

            HiLoRiderJsonLogger jsonDest;
            HiLoRiderCsvLogger  csvDest;
            if      (State == State.Realtime)   { jsonDest = _jsonLogger;   csvDest = _csvLogger;   }
            else if (State == State.Historical) { jsonDest = _jsonBtLogger; csvDest = _csvBtLogger; }
            else return;

            if (jsonDest == null && csvDest == null)
            {
                // Self-heal: EnableTradeLogging read true above but the streams were
                // never created (e.g. false at State.DataLoaded on this instance,
                // later flipped true by something other than the TRADE LOG button's
                // own click handler). Jul 2026 -- see CLAUDE.md "DaMaster -- Live
                // Trades Not Logging Despite EnableTradeLogging=true".
                InitializeTradeLoggers();
                if      (State == State.Realtime)   { jsonDest = _jsonLogger;   csvDest = _csvLogger;   }
                else if (State == State.Historical) { jsonDest = _jsonBtLogger; csvDest = _csvBtLogger; }
                if (jsonDest == null && csvDest == null) return;
            }

            if (State == State.Historical)
            {
                string btKey = BuildHistoricalTradeKey(
                    Instrument?.FullName, Account?.Name, BarsPeriod?.ToString(), GetConfigHash(),
                    t.Entry?.Time.ToString("o"),
                    t.Entry?.MarketPosition == MarketPosition.Long ? "Long" : "Short");
                if (_btLoggedKeys.Contains(btKey)) return;
                _btLoggedKeys.Add(btKey);
            }

            try
            {
                int tradeNum = (State == State.Realtime) ? ++_tradeNumber : ++_btTradeNumber;

                double slTicks = snapFilledPrice > 0 && _initialAcceptedStopLevel > 0
                    ? Math.Abs((snapFilledPrice - _initialAcceptedStopLevel) / TickSize) : FixedSLTicks;
                double tpTicks = snapFilledPrice > 0 && _initialAcceptedTargetLevel > 0
                    ? Math.Abs((_initialAcceptedTargetLevel - snapFilledPrice) / TickSize) : FixedTPTicks;
                bool wasLong = t.Entry?.MarketPosition == MarketPosition.Long;
                double finalStopTicksFromEntry = stopLevel > 0 && snapFilledPrice > 0 && TickSize > 0
                    ? (wasLong ? stopLevel - snapFilledPrice : snapFilledPrice - stopLevel) / TickSize
                    : 0;
                string mlArr = _mlEntryFeatures != null
                    ? "[" + string.Join(",", Array.ConvertAll(_mlEntryFeatures,
                        x => x.ToString("F4", CultureInfo.InvariantCulture))) + "]"
                    : "[]";

                string json = "{" +
                    $"\"Strategy\":\"HiLoRider\"," +
                    $"\"StrategyVersion\":{Q(StrategyVersion)}," +
                    $"\"FeatureVersion\":{Q(FeatureVersion)}," +
                    $"\"RunId\":{Q(_runId)}," +
                    $"\"InstanceTag\":{Q(_effectiveInstanceTag)}," +
                    $"\"ConfigHash\":{Q(GetConfigHash())}," +
                    $"\"DailyLimitScope\":{Q(DailyLimitScope.ToString())}," +
                    $"\"EntryProvenance\":{Q(_isManualTrade ? "Manual" : "Automatic")}," +
                    $"\"EntrySignalSource\":{Q(entrySignalSource)}," +
                    $"\"Instrument\":{Q(Instrument?.FullName)}," +
                    $"\"Account\":{Q(Account?.Name)}," +
                    $"\"BarsPeriod\":{Q(BarsPeriod?.ToString())}," +
                    $"\"TradingHours\":{Q(Bars?.TradingHours?.Name)}," +
                    $"\"TradeNumber\":{tradeNum}," +
                    $"\"EntryTime\":{Q(t.Entry?.Time.ToString("o"))}," +
                    $"\"ExitTime\":{Q(t.Exit?.Time.ToString("o"))}," +
                    $"\"Direction\":{Q(t.Entry?.MarketPosition == MarketPosition.Long ? "Long" : "Short")}," +
                    $"\"EntryPrice\":{snapFilledPrice.ToString("F4", CultureInfo.InvariantCulture)}," +
                    $"\"ExitPrice\":{t.Exit?.Price.ToString("F4", CultureInfo.InvariantCulture) ?? "0"}," +
                    $"\"ProfitTicks\":{t.ProfitTicks.ToString("F2", CultureInfo.InvariantCulture)}," +
                    $"\"ProfitCurrency\":{t.ProfitCurrency.ToString("F2", CultureInfo.InvariantCulture)}," +
                    $"\"Commission\":{t.Commission.ToString("F2", CultureInfo.InvariantCulture)}," +
                    $"\"InitialSLTicks\":{slTicks.ToString("F1", CultureInfo.InvariantCulture)}," +
                    $"\"InitialTPTicks\":{tpTicks.ToString("F1", CultureInfo.InvariantCulture)}," +
                    $"\"FinalStopTicksFromEntry\":{finalStopTicksFromEntry.ToString("F1", CultureInfo.InvariantCulture)}," +
                    $"\"AcceptedTrailChanges\":{_acceptedTrailChanges}," +
                    $"\"MaxTrailRiskReductionTicks\":{_maxTrailRiskReductionTicks.ToString("F1", CultureInfo.InvariantCulture)}," +
                    $"\"MfeTicks\":{mfeTicks.ToString("F1", CultureInfo.InvariantCulture)}," +
                    $"\"MaeTicks\":{maeTicks.ToString("F1", CultureInfo.InvariantCulture)}," +
                    $"\"BreakevenRealized\":{(beRealized ? "true" : "false")}," +
                    $"\"ExitName\":{Q(_tbExitName)}," +
                    $"\"ADX_at_Entry\":{entryAdx.ToString("F2", CultureInfo.InvariantCulture)}," +
                    $"\"Diff_at_Entry\":{entryDiff.ToString("F4", CultureInfo.InvariantCulture)}," +
                    $"\"Bar1RangeTicks\":{entryBar1RangeTicks.ToString("F1", CultureInfo.InvariantCulture)}," +
                    $"\"Bar0RangeTicks\":{entryBar0RangeTicks.ToString("F1", CultureInfo.InvariantCulture)}," +
                    $"\"Mode\":{Q("Swing")}," +
                    $"\"MLFeatures\":{mlArr}" +
                    "}";
                jsonDest?.WriteLine(json);

                string csv = string.Join(",",
                    "HiLoRider",
                    Q2(StrategyVersion),
                    Q2(FeatureVersion),
                    Q2(_runId),
                    Q2(_effectiveInstanceTag),
                    Q2(GetConfigHash()),
                    Q2(_isManualTrade ? "Manual" : "Automatic"),
                    Q2(entrySignalSource),
                    Q2(Instrument?.FullName),
                    Q2(Account?.Name),
                    Q2(BarsPeriod?.ToString()),
                    Q2(Bars?.TradingHours?.Name),
                    tradeNum,
                    Q2(t.Entry?.Time.ToString("o")),
                    Q2(t.Exit?.Time.ToString("o")),
                    Q2(t.Entry?.MarketPosition == MarketPosition.Long ? "Long" : "Short"),
                    snapFilledPrice.ToString("F4", CultureInfo.InvariantCulture),
                    t.Exit?.Price.ToString("F4", CultureInfo.InvariantCulture) ?? "0",
                    t.ProfitTicks.ToString("F2", CultureInfo.InvariantCulture),
                    t.ProfitCurrency.ToString("F2", CultureInfo.InvariantCulture),
                    t.Commission.ToString("F2", CultureInfo.InvariantCulture),
                    slTicks.ToString("F1", CultureInfo.InvariantCulture),
                    tpTicks.ToString("F1", CultureInfo.InvariantCulture),
                    finalStopTicksFromEntry.ToString("F1", CultureInfo.InvariantCulture),
                    _acceptedTrailChanges,
                    _maxTrailRiskReductionTicks.ToString("F1", CultureInfo.InvariantCulture),
                    mfeTicks.ToString("F1", CultureInfo.InvariantCulture),
                    maeTicks.ToString("F1", CultureInfo.InvariantCulture),
                    beRealized ? "1" : "0",
                    Q2(_tbExitName),
                    entryAdx.ToString("F2", CultureInfo.InvariantCulture),
                    entryDiff.ToString("F4", CultureInfo.InvariantCulture),
                    entryBar1RangeTicks.ToString("F1", CultureInfo.InvariantCulture),
                    entryBar0RangeTicks.ToString("F1", CultureInfo.InvariantCulture),
                    "Swing");

                if (_mlEntryFeatures != null)
                    foreach (var fv in _mlEntryFeatures)
                        csv += "," + fv.ToString("F4", CultureInfo.InvariantCulture);
                else
                    csv += ",,,,,,,";

                csvDest?.WriteLine(csv);
            }
            catch (Exception ex)
            {
                Print($"[HiLoRider Logger] TryLogTrade error: {ex.Message}");
            }
        }

        // ─────────────────────────────────────────────────────────────────────
        // TryLogChartTradeSummary — one row per closed position (aggregated)
        // Routes: Realtime → live summary | Historical → backtest summary
        // ─────────────────────────────────────────────────────────────────────
        internal void TryLogChartTradeSummary(
            Trade firstPartial,
            double netTicks, double netProfit, double netCommission,
            int netContracts, int partialCount,
            HashSet<string> exitNames,
            DateTime firstExitTime, DateTime lastExitTime,
            double mfeTicks, double maeTicks, double snapFilledPrice)
        {
            if (!EnableTradeLogging) return;
            if (State == State.Historical && !EnableHistoricalTradeLogging) return;

            // Aug 2026: same Playback101-only gate as TryLogTrade() above -- see
            // that method's own comment for the full reasoning.
            if (State == State.Historical
                && !string.Equals(Account?.Name, "Playback101", StringComparison.OrdinalIgnoreCase))
                return;

            HiLoRiderJsonLogger summaryDest;
            if      (State == State.Realtime)   summaryDest = _jsonSummaryLogger;
            else if (State == State.Historical) summaryDest = _jsonBtSummaryLogger;
            else return;

            if (summaryDest == null)
            {
                InitializeTradeLoggers();
                if      (State == State.Realtime)   summaryDest = _jsonSummaryLogger;
                else if (State == State.Historical) summaryDest = _jsonBtSummaryLogger;
                if (summaryDest == null) return;
            }

            if (State == State.Historical)
            {
                string btSummaryKey = BuildHistoricalTradeKey(
                    Instrument?.FullName, Account?.Name, BarsPeriod?.ToString(), GetConfigHash(),
                    firstPartial.Entry?.Time.ToString("o"),
                    firstPartial.Entry?.MarketPosition == MarketPosition.Long ? "Long" : "Short");
                if (_btSummaryLoggedKeys.Contains(btSummaryKey)) return;
                _btSummaryLoggedKeys.Add(btSummaryKey);
            }

            try
            {
                string exitNamesStr = string.Join("|", exitNames);
                bool wasLong = firstPartial.Entry?.MarketPosition == MarketPosition.Long;
                double finalStopTicksFromEntry = stopLevel > 0 && snapFilledPrice > 0 && TickSize > 0
                    ? (wasLong ? stopLevel - snapFilledPrice : snapFilledPrice - stopLevel) / TickSize
                    : 0;
                double ledgerEntryAverage = _entryFillQty > 0
                    ? _entryFillValue / _entryFillQty : snapFilledPrice;
                double ledgerExitAverage = _exitFillQty > 0
                    ? _exitFillValue / _exitFillQty : 0;
                int ledgerQuantity = _exitFillQty;
                double ledgerPnlCurrency = 0;
                if (ledgerEntryAverage > 0 && ledgerExitAverage > 0 && ledgerQuantity > 0
                    && Instrument?.MasterInstrument != null)
                {
                    double ledgerDirection = wasLong ? 1.0 : -1.0;
                    ledgerPnlCurrency = (ledgerExitAverage - ledgerEntryAverage)
                        * ledgerDirection * ledgerQuantity * Instrument.MasterInstrument.PointValue;
                }
                string json = "{" +
                    $"\"Strategy\":\"HiLoRider\"," +
                    $"\"StrategyVersion\":{Q(StrategyVersion)}," +
                    $"\"FeatureVersion\":{Q(FeatureVersion)}," +
                    $"\"RunId\":{Q(_runId)}," +
                    $"\"InstanceTag\":{Q(_effectiveInstanceTag)}," +
                    $"\"ConfigHash\":{Q(GetConfigHash())}," +
                    $"\"DailyLimitScope\":{Q(DailyLimitScope.ToString())}," +
                    $"\"EntryProvenance\":{Q(_isManualTrade ? "Manual" : "Automatic")}," +
                    $"\"EntrySignalSource\":{Q(entrySignalSource)}," +
                    $"\"Instrument\":{Q(Instrument?.FullName)}," +
                    $"\"Account\":{Q(Account?.Name)}," +
                    $"\"BarsPeriod\":{Q(BarsPeriod?.ToString())}," +
                    $"\"TradingHours\":{Q(Bars?.TradingHours?.Name)}," +
                    $"\"EntryTime\":{Q(firstPartial.Entry?.Time.ToString("o"))}," +
                    $"\"FirstExitTime\":{Q(firstExitTime == DateTime.MaxValue ? "" : firstExitTime.ToString("o"))}," +
                    $"\"LastExitTime\":{Q(lastExitTime == DateTime.MinValue ? "" : lastExitTime.ToString("o"))}," +
                    $"\"Direction\":{Q(firstPartial.Entry?.MarketPosition == MarketPosition.Long ? "Long" : "Short")}," +
                    $"\"EntryPrice\":{snapFilledPrice.ToString("F4", CultureInfo.InvariantCulture)}," +
                    $"\"NetProfitTicks\":{netTicks.ToString("F2", CultureInfo.InvariantCulture)}," +
                    $"\"NetProfitCurrency\":{netProfit.ToString("F2", CultureInfo.InvariantCulture)}," +
                    $"\"NetCommission\":{netCommission.ToString("F2", CultureInfo.InvariantCulture)}," +
                    $"\"LedgerEntryAverage\":{ledgerEntryAverage.ToString("F4", CultureInfo.InvariantCulture)}," +
                    $"\"LedgerExitAverage\":{ledgerExitAverage.ToString("F4", CultureInfo.InvariantCulture)}," +
                    $"\"LedgerQuantity\":{ledgerQuantity}," +
                    $"\"LedgerPnlCurrency\":{ledgerPnlCurrency.ToString("F2", CultureInfo.InvariantCulture)}," +
                    $"\"LedgerExitReason\":{Q(_exitFillReason)}," +
                    $"\"Contracts\":{netContracts}," +
                    $"\"PartialExits\":{partialCount}," +
                    $"\"ExitNames\":{Q(exitNamesStr)}," +
                    $"\"MfeTicks\":{mfeTicks.ToString("F1", CultureInfo.InvariantCulture)}," +
                    $"\"MaeTicks\":{maeTicks.ToString("F1", CultureInfo.InvariantCulture)}," +
                    $"\"FinalStopTicksFromEntry\":{finalStopTicksFromEntry.ToString("F1", CultureInfo.InvariantCulture)}," +
                    $"\"AcceptedTrailChanges\":{_acceptedTrailChanges}," +
                    $"\"MaxTrailRiskReductionTicks\":{_maxTrailRiskReductionTicks.ToString("F1", CultureInfo.InvariantCulture)}," +
                    $"\"Bar1RangeTicks\":{entryBar1RangeTicks.ToString("F1", CultureInfo.InvariantCulture)}," +
                    $"\"Bar0RangeTicks\":{entryBar0RangeTicks.ToString("F1", CultureInfo.InvariantCulture)}," +
                    $"\"Mode\":{Q("Swing")}" +
                    "}";
                summaryDest.WriteLine(json);
            }
            catch (Exception ex) { Print($"[HiLoRider Logger] TryLogChartTradeSummary error: {ex.Message}"); }
        }

        private static string Q(string value)
        {
            var sb = new StringBuilder();
            sb.Append('"');
            foreach (char ch in value ?? "")
            {
                switch (ch)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\b': sb.Append("\\b"); break;
                    case '\f': sb.Append("\\f"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (ch < 32) sb.Append("\\u").Append(((int)ch).ToString("x4", CultureInfo.InvariantCulture));
                        else sb.Append(ch);
                        break;
                }
            }
            sb.Append('"');
            return sb.ToString();
        }
        private static string Q2(string s) => "\"" + (s ?? "").Replace("\"", "\"\"") + "\"";

        // ─────────────────────────────────────────────────────────────────────
        // Backtest dedup helpers — prevent re-appending trades already logged
        // by a prior Strategy Analyzer run into the same *_backtest.jsonl file.
        // Historical-path only; live/Realtime logging is untouched.
        // ─────────────────────────────────────────────────────────────────────
        private HashSet<string> LoadExistingBacktestKeys(string path)
        {
            var keys = new HashSet<string>();
            try
            {
                if (!File.Exists(path)) return keys;
                foreach (string line in File.ReadLines(path))
                {
                    string instrument = ExtractJsonStringField(line, "Instrument");
                    string account = ExtractJsonStringField(line, "Account");
                    string barsPeriod = ExtractJsonStringField(line, "BarsPeriod");
                    string configHash = ExtractJsonStringField(line, "ConfigHash");
                    string entryTime = ExtractJsonStringField(line, "EntryTime");
                    string direction = ExtractJsonStringField(line, "Direction");
                    if (instrument == null || account == null || barsPeriod == null
                        || configHash == null || entryTime == null || direction == null) continue;
                    keys.Add(BuildHistoricalTradeKey(instrument, account, barsPeriod,
                        configHash, entryTime, direction));
                }
            }
            catch (Exception ex) { Print($"[HiLoRider Logger] LoadExistingBacktestKeys: {ex.Message}"); }
            return keys;
        }

        private static string BuildHistoricalTradeKey(string instrument,
            string account, string barsPeriod, string configHash,
            string entryTime, string direction)
        {
            return string.Join("|", new[]
            {
                instrument ?? "", account ?? "", barsPeriod ?? "",
                configHash ?? "", entryTime ?? "", direction ?? ""
            });
        }

        private string ExtractJsonStringField(string json, string field)
        {
            string marker = $"\"{field}\":\"";
            int start = json.IndexOf(marker);
            if (start < 0) return null;
            start += marker.Length;
            int end = json.IndexOf('"', start);
            if (end < 0) return null;
            return json.Substring(start, end - start);
        }
    }
}
