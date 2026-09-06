using System;
using System.Diagnostics;
using System.IO;
using System.Threading.Tasks;

namespace NinjaTrader.NinjaScript.Strategies.KhanShared
{
    internal sealed class KhanPythonToolResult
    {
        public bool Success;
        public bool TimedOut;
        public int ExitCode = -1;
        public string StandardOutput = "";
        public string StandardError = "";
        public string Error = "";

        public string BestError
        {
            get
            {
                if (!string.IsNullOrWhiteSpace(Error)) return Error;
                if (!string.IsNullOrWhiteSpace(StandardError)) return StandardError;
                if (!string.IsNullOrWhiteSpace(StandardOutput)) return StandardOutput;
                return TimedOut ? "Python tool timed out." : "Python tool did not create a fresh output file.";
            }
        }
    }

    internal static class KhanPythonToolRunner
    {
        internal static KhanPythonToolResult Run(
            string pythonExe,
            string scriptPath,
            string toolArguments,
            string workingDirectory,
            string expectedOutput,
            int timeoutMilliseconds,
            string runId,
            string strategyVersion,
            string featureVersion,
            string configHash,
            bool appendProvenance = true,
            bool deleteExpectedOutput = true)
        {
            var result = new KhanPythonToolResult();
            try
            {
                if (string.IsNullOrWhiteSpace(expectedOutput))
                    throw new ArgumentException("Expected output path is required.");

                Directory.CreateDirectory(Path.GetDirectoryName(expectedOutput));
                if (deleteExpectedOutput && File.Exists(expectedOutput))
                    File.Delete(expectedOutput);

                string marker = expectedOutput + ".fresh.json";
                if (File.Exists(marker))
                    File.Delete(marker);

                DateTime startedUtc = DateTime.UtcNow;
                string provenance = appendProvenance
                    ? " --run-id " + Q(runId)
                        + " --strategy-version " + Q(strategyVersion)
                        + " --feature-version " + Q(featureVersion)
                        + " --config-hash " + Q(configHash)
                    : "";

                var psi = new ProcessStartInfo
                {
                    FileName = pythonExe,
                    Arguments = Q(scriptPath) + " " + (toolArguments ?? "") + provenance,
                    WorkingDirectory = workingDirectory,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true
                };

                using (Process process = Process.Start(psi))
                {
                    if (process == null)
                        throw new InvalidOperationException("Python process could not be started.");

                    Task<string> stdoutTask = process.StandardOutput.ReadToEndAsync();
                    Task<string> stderrTask = process.StandardError.ReadToEndAsync();
                    bool exited = process.WaitForExit(Math.Max(1000, timeoutMilliseconds));
                    if (!exited)
                    {
                        result.TimedOut = true;
                        try { process.Kill(); } catch { }
                        try { process.WaitForExit(5000); } catch { }
                    }

                    try { Task.WaitAll(new Task[] { stdoutTask, stderrTask }, 5000); } catch { }
                    if (stdoutTask.IsCompleted) result.StandardOutput = stdoutTask.Result ?? "";
                    if (stderrTask.IsCompleted) result.StandardError = stderrTask.Result ?? "";
                    if (process.HasExited) result.ExitCode = process.ExitCode;
                }

                bool fresh = File.Exists(expectedOutput)
                    && new FileInfo(expectedOutput).Length > 0
                    && File.GetLastWriteTimeUtc(expectedOutput) >= startedUtc.AddSeconds(-1);
                result.Success = !result.TimedOut && result.ExitCode == 0 && fresh;
            }
            catch (Exception ex)
            {
                result.Error = ex.Message;
            }
            return result;
        }

        private static string Q(string value)
        {
            return "\"" + (value ?? "").Replace("\"", "\\\"") + "\"";
        }
    }
}
