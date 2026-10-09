using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace Jarvis.Desktop;

internal static class Program
{
    internal static Ownership? Tree;
    internal static string SettingsDirectory => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Jarvis");
    [STAThread]
    private static void Main(string[] args)
    {
        ApplicationConfiguration.Initialize();
        Application.SetUnhandledExceptionMode(UnhandledExceptionMode.ThrowException);
        try
        {
            var options = Options.Parse(args);
            Directory.CreateDirectory(SettingsDirectory);
            var identity = options.DataDirectory is null ? "setup" : Path.GetFullPath(options.DataDirectory).TrimEnd('\\').ToLowerInvariant();
            var digest = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(identity)))[..16];
            using var instance = new Mutex(false, @"Local\JarvisNative_" + digest);
            bool owned;
            try { owned = instance.WaitOne(0); } catch (AbandonedMutexException) { owned = true; }
            if (!owned)
            {
                var window = FindWindowW(null, "Jarvis · " + digest);
                if (window != 0) { ShowWindow(window, 9); SetForegroundWindow(window); }
                return;
            }
            Tree = new Ownership();
            try { Application.Run(new MainWindow(options, "Jarvis · " + digest)); }
            finally { instance.ReleaseMutex(); }
            GC.KeepAlive(Tree);
        }
        catch (Exception error)
        {
            // Metadata only; exception messages can contain paths or credentials.
            if (args.Contains("--smoke-test"))
            {
                Environment.ExitCode = 1;
                int index = Array.IndexOf(args, "--smoke-test");
                if (index + 1 < args.Length)
                    File.WriteAllText(args[index + 1], JsonSerializer.Serialize(new { success = false, code = error.GetType().Name, error.HResult, stack = error.StackTrace }));
            }
            else MessageBox.Show("Не удалось запустить Jarvis. Код: " + error.GetType().Name,
                "Jarvis", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern nint FindWindowW(string? className, string title);
    [DllImport("user32.dll")] private static extern bool ShowWindow(nint window, int command);
    [DllImport("user32.dll")] private static extern bool SetForegroundWindow(nint window);
}

internal sealed record Options(string? DataDirectory, string Python, string? DevelopmentSource, string? SmokeOutput,
    bool EnableNetwork, bool EnableBrowser, string[] ReadRoots, string[] WriteRoots, string? SetupPack)
{
    public static Options Parse(string[] args)
    {
        string? data = null, python = null, source = null, smoke = null, pack = null;
        for (int i = 0; i < args.Length; i++)
        {
            if (++i == args.Length) throw new ArgumentException("Missing option value");
            switch (args[i - 1])
            {
                case "--data-dir": data = Path.GetFullPath(args[i]); break;
                case "--python": python = Path.GetFullPath(args[i]); break;
                case "--development-source": source = Path.GetFullPath(args[i]); break;
                case "--smoke-test": smoke = Path.GetFullPath(args[i]); break;
                case "--setup-smoke-pack": pack = Path.GetFullPath(args[i]); break;
                default: throw new ArgumentException("Unknown option");
            }
        }
        bool network = false, browser = false;
        string[] reads = [], writes = [];
        var settings = Path.Combine(Program.SettingsDirectory, "desktop.json");
        if (smoke is null && data is null && File.Exists(settings))
        {
            using var document = JsonDocument.Parse(File.ReadAllText(settings));
            data = document.RootElement.GetProperty("data_dir").GetString();
            if (data is null || !Path.IsPathFullyQualified(data)) throw new InvalidDataException();
            network = document.RootElement.TryGetProperty("enable_network", out var n) && n.GetBoolean();
            browser = document.RootElement.TryGetProperty("enable_browser", out var b) && b.GetBoolean();
            if (document.RootElement.TryGetProperty("read_roots", out var r)) reads = r.Deserialize<string[]>() ?? [];
            if (document.RootElement.TryGetProperty("write_roots", out var w)) writes = w.Deserialize<string[]>() ?? [];
        }
        if (smoke is not null)
        {
            // Smoke mode must never open a supplied real database or load models.
            if (data is not null) throw new ArgumentException("Smoke data is isolated");
            data = Path.Combine(Path.GetTempPath(), "jarvis-native-test-" + Guid.NewGuid().ToString("N"));
        }
        if (pack is not null && smoke is null) throw new ArgumentException("Setup qualification requires isolated smoke mode");
        return new(data, python ?? Path.Combine(AppContext.BaseDirectory, "backend", "python.exe"), source, smoke,
            network, browser, reads, writes, pack);
    }
}
