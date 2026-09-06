// HiLoRiderGEX.cs — Gamma Exposure (GEX) bridge: load JSON, gate entries, UI refresh
using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Controls;
using System.Windows.Media;
using NinjaTrader.NinjaScript;

namespace NinjaTrader.NinjaScript.Strategies.HiLoRider
{
    // ── GEX zone enum — defined at namespace level so all HiLoRider partial files can use it ──
    public enum GEXZone { Negative = -1, Neutral = 0, Positive = 1 }

    public partial class HiLoRider : Strategy
    {
        // ── GEX live state ────────────────────────────────────────────────────
        private GEXZone  _gexZone         = GEXZone.Neutral;
        private double   _gexFlipNQ       = 0;
        private double   _gexCallWallNQ   = 0;
        private double   _gexPutWallNQ    = 0;
        private double   _gexTotalBillion = 0;
        private DateTime _gexTimestamp    = DateTime.MinValue;
        private bool     _gexDataLoaded   = false;
        private string   _gexStatusMsg    = "No data — click Refresh GEX";

        // ── UI refs ───────────────────────────────────────────────────────────
        private System.Windows.Controls.TextBlock _tbGEXZone, _tbGEXFlip, _tbGEXWalls, _tbGEXAge;
        private System.Windows.Controls.Button    _btnRefreshGEX;

        // ── Properties ───────────────────────────────────────────────────────
        [NinjaTrader.NinjaScript.NinjaScriptProperty]
        [System.ComponentModel.DataAnnotations.Display(
            Name = "Enable GEX Filter", Order = 1, GroupName = "25. GEX Regime",
            Description = "Enable family-specific Gamma Exposure regime filter. Reads GEX_Data_<family>.json written by GEX_Bridge.py.")]
        public bool EnableGEXFilter { get; set; }

        [NinjaTrader.NinjaScript.NinjaScriptProperty]
        [System.ComponentModel.DataAnnotations.Range(1.0, 48.0)]
        [System.ComponentModel.DataAnnotations.Display(
            Name = "GEX Staleness Limit (hours)", Order = 2, GroupName = "25. GEX Regime",
            Description = "Ignore GEX data older than this many hours. Default 4.")]
        public double GEXStalenessHours { get; set; }

        [NinjaTrader.NinjaScript.NinjaScriptProperty]
        [System.ComponentModel.DataAnnotations.Display(
            Name = "GEX: Block Beyond Call Wall (Positive)", Order = 3, GroupName = "25. GEX Regime",
            Description = "In Positive GEX zone, block long entries when price is above call wall. Default true.")]
        public bool GEXCallWallGateEnabled { get; set; }

        [NinjaTrader.NinjaScript.NinjaScriptProperty]
        [System.ComponentModel.DataAnnotations.Display(
            Name = "GEX: Block Beyond Put Wall (Positive)", Order = 4, GroupName = "25. GEX Regime",
            Description = "In Positive GEX zone, block short entries when price is below put wall. Default true.")]
        public bool GEXPutWallGateEnabled { get; set; }

        // ═════════════════════════════════════════════════════════════════════
        // LoadGEXData — reads family-specific JSON written by GEX_Bridge.py
        // ═════════════════════════════════════════════════════════════════════
        internal void LoadGEXData()
        {
            if (!EnableGEXFilter) return;
            if (!GEXInstrumentSupported())
            {
                _gexDataLoaded = false;
                _gexStatusMsg = "Unsupported instrument — gating disabled";
                return;
            }
            string path = GEXDataFilePath();
            if (!File.Exists(path))
            {
                _gexStatusMsg = $"{System.IO.Path.GetFileName(path)} not found  ({path})";
                return;
            }
            try
            {
                string json = File.ReadAllText(path, Encoding.UTF8);
                ParseGEXJson(json);
            }
            catch (Exception ex)
            {
                _gexStatusMsg = $"Parse error: {ex.Message}";
                Print($"[GEX] LoadGEXData error: {ex.Message}");
            }
        }

        private void ParseGEXJson(string json)
        {
            _gexDataLoaded = false;
            try
            {
                string zone    = GEXParseStringField(json, "gex_zone");
                _gexZone       = zone == "Negative" ? GEXZone.Negative
                               : zone == "Positive" ? GEXZone.Positive
                               : GEXZone.Neutral;
                _gexFlipNQ       = GEXParseDoubleField(json, "flip_level");
                _gexCallWallNQ   = GEXParseDoubleField(json, "call_wall");
                _gexPutWallNQ    = GEXParseDoubleField(json, "put_wall");
                _gexTotalBillion = GEXParseDoubleField(json, "gex_total_billion");
                string ts        = GEXParseStringField(json, "timestamp");
                _gexTimestamp    = DateTime.TryParse(ts, out var dt) ? dt : DateTime.MinValue;
                _gexDataLoaded   = true;

                double ageH = GEXAgeHours();
                string ageSuffix = ageH < 1   ? $"{(int)(ageH * 60)}m ago"
                                 : ageH < 24  ? $"{ageH:F1}h ago"
                                 : "STALE";
                _gexStatusMsg = $"{_gexZone}  |  {ageSuffix}";
                Print($"[GEX] Loaded — zone={_gexZone}  flip={_gexFlipNQ:F0}" +
                      $"  cWall={_gexCallWallNQ:F0}  pWall={_gexPutWallNQ:F0}" +
                      $"  GEX={_gexTotalBillion:+0.00;-0.00}B  age={ageSuffix}");
            }
            catch (Exception ex)
            {
                _gexDataLoaded = false;
                _gexStatusMsg  = $"Parse error: {ex.Message}";
                Print($"[GEX] ParseGEXJson error: {ex.Message}");
            }
        }

        // ═════════════════════════════════════════════════════════════════════
        // CheckGEXFilter — entry gate, called from TryEnterFromBarUpdate
        // ═════════════════════════════════════════════════════════════════════
        internal bool CheckGEXFilter(int sig)
        {
            if (!EnableGEXFilter || !GEXInstrumentSupported() || !_gexDataLoaded) return true;

            if (GEXAgeHours() > GEXStalenessHours)
            {
                _gexStatusMsg = $"GEX STALE ({GEXAgeHours():F1}h) — gating disabled";
                return true;
            }

            if (_gexZone == GEXZone.Positive)
            {
                if (GEXCallWallGateEnabled && sig == 1 && _gexCallWallNQ > 0 && Close[0] > _gexCallWallNQ)
                {
                    Print($"[GEXFilter] Long blocked — above call wall {_gexCallWallNQ:F0}  price={Close[0]:F2}");
                    return false;
                }
                if (GEXPutWallGateEnabled && sig == -1 && _gexPutWallNQ > 0 && Close[0] < _gexPutWallNQ)
                {
                    Print($"[GEXFilter] Short blocked — below put wall {_gexPutWallNQ:F0}  price={Close[0]:F2}");
                    return false;
                }
            }
            return true;
        }

        // ═════════════════════════════════════════════════════════════════════
        // DoRefreshGEX — runs GEX_Bridge.py, then reloads JSON + updates UI
        // ═════════════════════════════════════════════════════════════════════
        internal void DoRefreshGEX()
        {
            if (!GEXInstrumentSupported())
            {
                System.Windows.MessageBox.Show(
                    "GEX supports NQ/MNQ, ES/MES, GC/MGC, and CL/MCL.\nGEX gating remains disabled for this instrument.",
                    "Refresh GEX", System.Windows.MessageBoxButton.OK,
                    System.Windows.MessageBoxImage.Information);
                return;
            }
            string script = GEXScriptPath();
            if (!File.Exists(script))
            {
                System.Windows.MessageBox.Show(
                    $"GEX_Bridge.py not found.\nExpected: {script}",
                    "Refresh GEX", System.Windows.MessageBoxButton.OK,
                    System.Windows.MessageBoxImage.Warning);
                return;
            }
            if (_btnRefreshGEX != null) { _btnRefreshGEX.Content = "Fetching…"; _btnRefreshGEX.IsEnabled = false; }

            string python = ResolvePython();
            System.Threading.ThreadPool.QueueUserWorkItem(_ =>
            {
                bool ok = false; string err = "";
                var run = KhanShared.KhanPythonToolRunner.Run(
                    python, script, $"--family {GEXFamily()}",
                    System.IO.Path.GetDirectoryName(script), GEXDataFilePath(), 60000,
                    _runId, StrategyVersion, FeatureVersion, GetConfigHash(), false, false);
                err = run.BestError;
                ok = run.Success;
                if (!string.IsNullOrWhiteSpace(run.StandardOutput))
                    Print($"[GEX_Bridge] {run.StandardOutput.Trim()}");

                ChartControl?.Dispatcher.InvokeAsync(() =>
                {
                    if (_btnRefreshGEX != null)
                    { _btnRefreshGEX.Content = "Refresh GEX"; _btnRefreshGEX.IsEnabled = true; }

                    if (ok)
                    {
                        LoadGEXData();
                        RefreshGEXCard();
                    }
                    else
                    {
                        _gexStatusMsg = "Script failed — see Output window";
                        if (!string.IsNullOrWhiteSpace(err))
                            Print($"[GEX] GEX_Bridge.py stderr: {err.Trim()}");
                        RefreshGEXCard();
                    }
                });
            });
        }

        // ═════════════════════════════════════════════════════════════════════
        // AddGEXRows — GEX regime readout merged into the SIGNAL STATE badge
        // card (called from BuildRegimeBadge in HiLoRiderUI.cs) to save panel space
        // ═════════════════════════════════════════════════════════════════════
        internal void AddGEXRows(System.Windows.Controls.StackPanel host)
        {
            var gexRow = new System.Windows.Controls.Grid
            { Margin = new System.Windows.Thickness(0, 7, 0, 0) };
            gexRow.ColumnDefinitions.Add(new System.Windows.Controls.ColumnDefinition());
            gexRow.ColumnDefinitions.Add(new System.Windows.Controls.ColumnDefinition
                { Width = System.Windows.GridLength.Auto });

            _tbGEXZone = Tx("GEX  --", 10, C_DIM, bold: true);
            _tbGEXAge  = Tx("--", 9, C_MUTED);
            _tbGEXAge.HorizontalAlignment = System.Windows.HorizontalAlignment.Right;
            _tbGEXAge.VerticalAlignment   = System.Windows.VerticalAlignment.Center;
            _tbGEXAge.ToolTip = "GEX data age";
            System.Windows.Controls.Grid.SetColumn(_tbGEXZone, 0);
            System.Windows.Controls.Grid.SetColumn(_tbGEXAge,  1);
            gexRow.Children.Add(_tbGEXZone);
            gexRow.Children.Add(_tbGEXAge);
            host.Children.Add(gexRow);

            var lvlRow = new System.Windows.Controls.StackPanel
            { Orientation = System.Windows.Controls.Orientation.Horizontal };
            _tbGEXFlip  = Tx("Flip: --", 9, C_MUTED);
            _tbGEXWalls = Tx("C-Wall: --  |  P-Wall: --", 9, C_MUTED);
            _tbGEXWalls.Margin = new System.Windows.Thickness(10, 0, 0, 0);
            lvlRow.Children.Add(_tbGEXFlip);
            lvlRow.Children.Add(_tbGEXWalls);
            host.Children.Add(lvlRow);

            _btnRefreshGEX = new System.Windows.Controls.Button
            {
                Content         = "Refresh GEX",
                Height          = 30,
                FontSize        = 11,
                FontWeight      = System.Windows.FontWeights.Bold,
                Margin          = new System.Windows.Thickness(6, 2, 6, 6),
                Cursor          = System.Windows.Input.Cursors.Hand,
                Background      = new SolidColorBrush(Color.FromRgb(20, 35, 55)),
                Foreground      = new SolidColorBrush(C_BLUE),
                BorderBrush     = new SolidColorBrush(C_BLUE),
                BorderThickness = new System.Windows.Thickness(1),
                ToolTip         = "Fetch the matching ETF options chain and write a family-specific GEX JSON file",
            };
            _btnRefreshGEX.Click += (s, e) => DoRefreshGEX();

            RefreshGEXCard();
        }

        // ── RefreshGEXCard — updates badge display ────────────────────────────
        internal void RefreshGEXCard()
        {
            if (_tbGEXZone == null) return;

            Color  zoneCol;
            string zoneStr;
            if (!_gexDataLoaded)
            {
                zoneCol = C_DIM; zoneStr = "--";
            }
            else if (GEXAgeHours() > GEXStalenessHours)
            {
                zoneCol = C_RED; zoneStr = "STALE";
            }
            else
            {
                zoneCol = _gexZone == GEXZone.Negative ? C_GREEN
                        : _gexZone == GEXZone.Positive ? C_AMBER
                        : C_BLUE;
                zoneStr = _gexZone == GEXZone.Negative ? "NEGATIVE  ▼  (trending)"
                        : _gexZone == GEXZone.Positive ? "POSITIVE  ▲  (mean rev)"
                        : "NEUTRAL";
            }

            SetTx(_tbGEXZone, "GEX  " + zoneStr, zoneCol);

            if (_gexDataLoaded)
            {
                SetTx(_tbGEXFlip,  $"Flip: {_gexFlipNQ:F0}", C_MUTED);
                SetTx(_tbGEXWalls, $"C-Wall: {_gexCallWallNQ:F0}  |  P-Wall: {_gexPutWallNQ:F0}", C_MUTED);

                double ageH  = GEXAgeHours();
                string ageTx = ageH < 1   ? $"{(int)(ageH * 60)}m ago"
                             : ageH < 24  ? $"{ageH:F1}h"
                             : $"{ageH:F0}h";
                Color  ageCol = ageH < 2                  ? C_GREEN
                              : ageH < GEXStalenessHours  ? C_AMBER
                              : C_RED;
                SetTx(_tbGEXAge, ageTx, ageCol);
            }
            else
            {
                SetTx(_tbGEXFlip,  "Flip: --", C_DIM);
                SetTx(_tbGEXWalls, "C-Wall: --  |  P-Wall: --", C_DIM);
                SetTx(_tbGEXAge,   "--", C_MUTED);
            }
        }

        // ── Helpers ───────────────────────────────────────────────────────────
        private double GEXAgeHours() =>
            _gexTimestamp != DateTime.MinValue
                ? (DateTime.Now - _gexTimestamp).TotalHours
                : 999;

        private bool GEXInstrumentSupported()
        {
            return !string.IsNullOrEmpty(GEXFamily());
        }

        private string GEXFamily()
        {
            string name = Instrument?.MasterInstrument?.Name ?? string.Empty;
            if (name.Equals("NQ", StringComparison.OrdinalIgnoreCase) || name.Equals("MNQ", StringComparison.OrdinalIgnoreCase)) return "NQ";
            if (name.Equals("ES", StringComparison.OrdinalIgnoreCase) || name.Equals("MES", StringComparison.OrdinalIgnoreCase)) return "ES";
            if (name.Equals("GC", StringComparison.OrdinalIgnoreCase) || name.Equals("MGC", StringComparison.OrdinalIgnoreCase)) return "GC";
            if (name.Equals("CL", StringComparison.OrdinalIgnoreCase) || name.Equals("MCL", StringComparison.OrdinalIgnoreCase)) return "CL";
            return string.Empty;
        }

        private string GEXDataFilePath() =>
            System.IO.Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
                "NinjaTrader 8", $"GEX_Data_{GEXFamily()}.json");

        private static string GEXScriptPath() =>
            System.IO.Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
                "NinjaTrader 8", "GEX_Bridge.py");

        private static string GEXParseStringField(string json, string key)
        {
            string search = $"\"{key}\"";
            int ki = json.IndexOf(search, StringComparison.Ordinal);
            if (ki < 0) return "";
            int ci = json.IndexOf(':', ki + search.Length);
            if (ci < 0) return "";
            int q1 = json.IndexOf('"', ci + 1);
            int q2 = q1 >= 0 ? json.IndexOf('"', q1 + 1) : -1;
            return (q1 >= 0 && q2 > q1) ? json.Substring(q1 + 1, q2 - q1 - 1) : "";
        }

        private static double GEXParseDoubleField(string json, string key)
        {
            string search = $"\"{key}\"";
            int ki = json.IndexOf(search, StringComparison.Ordinal);
            if (ki < 0) return 0;
            int ci = json.IndexOf(':', ki + search.Length);
            if (ci < 0) return 0;
            int s = ci + 1;
            while (s < json.Length && (json[s] == ' ' || json[s] == '\t' || json[s] == '\n' || json[s] == '\r')) s++;
            int e = s;
            while (e < json.Length && (char.IsDigit(json[e]) || json[e] == '.' || json[e] == '-' || json[e] == '+' || json[e] == 'e' || json[e] == 'E')) e++;
            return e > s && double.TryParse(json.Substring(s, e - s),
                System.Globalization.NumberStyles.Float,
                System.Globalization.CultureInfo.InvariantCulture, out double v) ? v : 0;
        }
    }
}
