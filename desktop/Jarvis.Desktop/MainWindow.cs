using System.Diagnostics;
using System.Text;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace Jarvis.Desktop;

internal sealed partial class MainWindow : Form
{
    private Options options;
    private readonly Label status = new() { AutoSize = false, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleCenter };
    private readonly Panel landing = new() { Dock = DockStyle.Fill, Padding = new Padding(48) };
    private readonly FlowLayoutPanel actions = new() { Dock = DockStyle.Bottom, Height = 70, FlowDirection = FlowDirection.LeftToRight };
    private WebView2? browser;
    private Process? backend;
    private Task? protocolTask;
    private bool stopping, canClose, backendStopped, ready;
    private bool microphonePermissionTested, microphoneFakeStreamTested;
    private bool setupSmokeCompleted;
    private string? origin;
    private readonly string profileDirectory;
    private readonly List<CoreWebView2DownloadOperation> downloads = [];

    public MainWindow(Options options, string title)
    {
        this.options = options;
        Text = title;
        Width = 1240; Height = 820; MinimumSize = new Size(850, 620);
        StartPosition = FormStartPosition.CenterScreen;
        BackColor = Color.FromArgb(16, 18, 22); ForeColor = Color.FromArgb(230, 235, 238);
        Font = new Font("Segoe UI", 11);
        Icon = Icon.ExtractAssociatedIcon(Environment.ProcessPath!);
        InitializeWindowChrome();
        profileDirectory = Path.Combine(Path.GetTempPath(), "jarvis-view-" + Guid.NewGuid().ToString("N"));
        status.Text = "Jarvis\n\n…";
        landing.Controls.Add(status); landing.Controls.Add(actions); Controls.Add(landing);
        windowControls.BringToFront();
        Shown += async (_, _) => await StartAsync();
        FormClosing += OnClosingAsync;
    }

    private static Button MakeButton(string text) => new()
    {
        Text = text, AutoSize = true, Height = 36, FlatStyle = FlatStyle.Flat,
        BackColor = Color.FromArgb(156, 228, 207), ForeColor = Color.FromArgb(20, 40, 33),
        Padding = new Padding(10, 2, 10, 2), Cursor = Cursors.Hand, Margin = new Padding(0, 0, 12, 0)
    };

    private async Task StartAsync()
    {
        if (options.SetupPack is not null && !setupSmokeCompleted)
        {
            setupTask = InstallModelsAsync(options.SetupPack, options.DataDirectory!);
            setupSmokeCompleted = await setupTask;
            if (stopping) return;
            if (!setupSmokeCompleted) { await FailureAsync("SetupSmokeFailed"); return; }
        }
        if (options.DataDirectory is null)
        {
            ShowWelcome();
            return;
        }
        try
        {
            if (!File.Exists(options.Python)) throw new FileNotFoundException();
            var start = new ProcessStartInfo(options.Python)
            {
                UseShellExecute = false, CreateNoWindow = true,
                RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
                StandardInputEncoding = new UTF8Encoding(false), StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8, WorkingDirectory = AppContext.BaseDirectory
            };
            start.ArgumentList.Add("-B"); start.ArgumentList.Add("-u");
            start.ArgumentList.Add("-m"); start.ArgumentList.Add("jarvis.desktop_backend");
            start.Environment.Remove("__PYVENV_LAUNCHER__");
            start.Environment.Remove("PYTHONHOME"); start.Environment.Remove("PYTHONPATH");
            start.Environment["PYTHONUTF8"] = "1";
            if (options.DevelopmentSource is not null) start.Environment["PYTHONPATH"] = options.DevelopmentSource;
            backend = Process.Start(start) ?? throw new InvalidOperationException();
            if (Program.Tree is null || !Program.Tree.Contains(backend.Handle)) throw new InvalidOperationException("Ownership");
            _ = DrainAsync(backend.StandardError);
            await backend.StandardInput.WriteLineAsync(JsonSerializer.Serialize(new
            {
                protocol = 1, data_dir = options.DataDirectory, test_mode = options.SmokeOutput is not null,
                enable_network = options.EnableNetwork, enable_browser = options.EnableBrowser,
                read_roots = options.ReadRoots, write_roots = options.WriteRoots
            }));
            await backend.StandardInput.FlushAsync();
            protocolTask = ReadProtocolAsync();
            _ = StartupDeadlineAsync();
        }
        catch (Exception error) { await FailureAsync(error.GetType().Name); }
    }

    private static async Task DrainAsync(StreamReader reader)
    {
        try { var buffer = new char[1024]; while (await reader.ReadAsync(buffer) != 0) { } }
        catch (IOException) { }
    }

    private async Task StartupDeadlineAsync()
    {
        await Task.Delay(TimeSpan.FromSeconds(90));
        if (!ready && !stopping && !IsDisposed) await FailureAsync("StartupTimeout");
    }

    private static async Task<string?> ReadBoundedAsync(StreamReader reader)
    {
        var result = new StringBuilder();
        var one = new char[1];
        while (await reader.ReadAsync(one) != 0)
        {
            if (one[0] == '\n') return result.ToString();
            result.Append(one[0]);
            if (result.Length > 16384) throw new InvalidDataException();
        }
        if (result.Length != 0) throw new InvalidDataException();
        return null;
    }

    private async Task ReadProtocolAsync()
    {
        try
        {
            while (await ReadBoundedAsync(backend!.StandardOutput) is string line)
            {
                using var packet = JsonDocument.Parse(line);
                var root = packet.RootElement;
                if (root.GetProperty("protocol").GetInt32() != 1) throw new InvalidDataException();
                switch (root.GetProperty("type").GetString())
                {
                    case "ready":
                        if (ready || origin is not null) throw new InvalidDataException();
                        if (!stopping) await OpenBrowserAsync(root.GetProperty("url").GetString()!);
                        break;
                    case "stopped": backendStopped = root.GetProperty("success").GetBoolean(); break;
                    case "error":
                        if (!stopping) await FailureAsync("BackendUnavailable");
                        break;
                    default: throw new InvalidDataException();
                }
            }
            if (!stopping) await FailureAsync("BackendExited");
        }
        catch (Exception error) { if (!stopping) await FailureAsync(error.GetType().Name); }
    }

    private async Task OpenBrowserAsync(string setup)
    {
        var uri = new Uri(setup);
        if (uri.Scheme != "http" || uri.Host != "127.0.0.1" || uri.Port < 1024
            || uri.AbsolutePath != "/setup" || uri.UserInfo.Length != 0 || uri.Query.Length != 0
            || !uri.Fragment.StartsWith("#token=", StringComparison.Ordinal)) throw new InvalidDataException();
        origin = uri.GetLeftPart(UriPartial.Authority);
        browser = new WebView2 { Dock = DockStyle.Fill, DefaultBackgroundColor = BackColor };
        Controls.Add(browser); browser.BringToFront();
        windowControls.BringToFront();
        // Smoke tests exercise the real permission event with synthetic media,
        // never the user's physical microphone and never an auto-allow flag.
        var environmentOptions = options.SmokeOutput is null ? null
            : new CoreWebView2EnvironmentOptions("--use-fake-device-for-media-stream");
        var environment = await CoreWebView2Environment.CreateAsync(null, profileDirectory, environmentOptions);
        if (stopping) return;
        await browser.EnsureCoreWebView2Async(environment);
        if (stopping) return;
        var core = browser.CoreWebView2;
        core.Settings.AreDefaultContextMenusEnabled = false;
        core.Settings.AreDevToolsEnabled = false;
        core.Settings.AreBrowserAcceleratorKeysEnabled = false;
        core.Settings.IsStatusBarEnabled = false;
        core.Settings.AreHostObjectsAllowed = false;
        core.Settings.IsWebMessageEnabled = true;
        core.Settings.IsNonClientRegionSupportEnabled = true;
        core.Settings.IsZoomControlEnabled = false;
        // Only the two fixed chrome palettes are accepted from the ready root page.
        await core.AddScriptToExecuteOnDocumentCreatedAsync("""
            document.addEventListener('DOMContentLoaded',()=>{
              const root=document.documentElement;
              root.classList.add('desktop-window');
              const notify=()=>{window.chrome.webview.postMessage(root.dataset.theme==='light'?'jarvis:theme:light':'jarvis:theme:dark');window.chrome.webview.postMessage(root.lang==='en'?'jarvis:language:en':'jarvis:language:ru');};
              new MutationObserver(notify).observe(root,{attributes:true,attributeFilter:['data-theme','lang']});
              notify();
            },{once:true});
            """);
        core.Profile.PreferredColorScheme = CoreWebView2PreferredColorScheme.Auto;
        core.WebMessageReceived += (_, e) =>
        {
            if (stopping || !ready || e.Source != origin + "/" || core.Source != origin + "/") return;
            if (e.WebMessageAsJson == "\"jarvis:theme:light\"") ApplyWindowTheme(true);
            else if (e.WebMessageAsJson == "\"jarvis:theme:dark\"") ApplyWindowTheme(false);
            else if (e.WebMessageAsJson == "\"jarvis:language:en\"") ApplyWindowLanguage(true);
            else if (e.WebMessageAsJson == "\"jarvis:language:ru\"") ApplyWindowLanguage(false);
        };
        core.NavigationStarting += (_, e) =>
        {
            e.Cancel = stopping || !Trusted(e.Uri);
        };
        core.FrameNavigationStarting += (_, e) => e.Cancel = true;
        core.FrameCreated += (_, e) => e.Frame.PermissionRequested += (_, request) =>
        {
            request.Handled = true;
            request.SavesInProfile = false;
            request.State = CoreWebView2PermissionState.Deny;
        };
        core.NewWindowRequested += (_, e) => e.Handled = true;
        core.PermissionRequested += RequestPermission;
        core.DownloadStarting += DownloadStarting;
        core.ProcessFailed += async (_, _) => { if (!stopping) await FailureAsync("ViewUnavailable"); };
        core.NavigationCompleted += async (_, e) =>
        {
            if (stopping) return;
            if (!e.IsSuccess) { await FailureAsync("NavigationFailed"); return; }
            if (new Uri(core.Source).AbsolutePath != "/") return;
            ready = true;
            await core.ExecuteScriptAsync("window.chrome.webview.postMessage(document.documentElement.dataset.theme==='light'?'jarvis:theme:light':'jarvis:theme:dark')");
            await core.ExecuteScriptAsync("window.chrome.webview.postMessage(document.documentElement.lang==='en'?'jarvis:language:en':'jarvis:language:ru')");
            landing.Visible = false;
            LayoutWindowChrome();
            if (options.SmokeOutput is not null) await SmokeAsync();
        };
        core.Navigate(setup);
    }

    private bool Trusted(string value) => Uri.TryCreate(value, UriKind.Absolute, out var uri)
        && uri.UserInfo.Length == 0 && uri.GetLeftPart(UriPartial.Authority) == origin;

    private bool CanUseMicrophone(string requestUri, CoreWebView2PermissionKind kind, bool userInitiated)
        => !stopping && ready && userInitiated && kind == CoreWebView2PermissionKind.Microphone
            && Trusted(requestUri) && browser?.CoreWebView2 is not null
            && Trusted(browser.CoreWebView2.Source) && new Uri(browser.CoreWebView2.Source).AbsolutePath == "/";

    private void RequestPermission(object? sender, CoreWebView2PermissionRequestedEventArgs e)
    {
        e.Handled = true; e.SavesInProfile = false; e.State = CoreWebView2PermissionState.Deny;
        if (!CanUseMicrophone(e.Uri, e.PermissionKind, e.IsUserInitiated)) return;
        // Pressing Record is the user's action; no second consent panel is needed.
        // Re-evaluate every request instead of remembering a blanket permission.
        e.State = CoreWebView2PermissionState.Allow;
        if (options.SmokeOutput is not null) microphonePermissionTested = true;
    }

    private void DownloadStarting(object? sender, CoreWebView2DownloadStartingEventArgs e)
    {
        e.Handled = true;
        e.Cancel = true;
        var operation = e.DownloadOperation;
        if (stopping || !Trusted(operation.Uri)) return;
        var path = new Uri(operation.Uri).AbsolutePath;
        if (!path.StartsWith("/api/artifacts/", StringComparison.Ordinal)
            || !Guid.TryParseExact(path[15..], "D", out var id)
            || operation.MimeType != "image/png") return;
        using var picker = new SaveFileDialog
        {
            Title = Ui("Сохранить изображение", "Save image"), FileName = $"Jarvis-{id}.png", Filter = Ui("Изображение PNG|*.png", "PNG image|*.png"),
            DefaultExt = "png", AddExtension = true, OverwritePrompt = true, CheckPathExists = true
        };
        if (picker.ShowDialog(this) != DialogResult.OK || stopping) return;
        e.ResultFilePath = picker.FileName; e.Cancel = false;
        downloads.Add(operation);
        operation.StateChanged += DownloadChanged;
    }

    private async void DownloadChanged(object? sender, object e)
    {
        if (sender is not CoreWebView2DownloadOperation operation || operation.State == CoreWebView2DownloadState.InProgress) return;
        operation.StateChanged -= DownloadChanged; downloads.Remove(operation);
        if (!stopping && browser?.CoreWebView2 is not null)
        {
            var message = operation.State == CoreWebView2DownloadState.Completed ? Ui("Изображение сохранено", "Image saved") : Ui("Сохранение не завершено", "Save did not complete");
            try { await browser.CoreWebView2.ExecuteScriptAsync($"document.getElementById('notice').textContent={JsonSerializer.Serialize(message)}"); }
            catch (InvalidOperationException) { }
        }
    }

    private async Task FailureAsync(string code)
    {
        landing.Visible = true; landing.BringToFront();
        LayoutWindowChrome();
        status.Text = Ui("Jarvis не смог продолжить работу.\nВаши диалоги остаются в папке данных.\n\nКод: ", "Jarvis could not continue.\nYour chats are still in the data folder.\n\nCode: ") + code;
        if (options.SmokeOutput is not null)
        {
            File.WriteAllText(options.SmokeOutput, JsonSerializer.Serialize(new { success = false, code, stage = smokeStage, bounds = Bounds, client = RectangleToScreen(ClientRectangle), work_area = Screen.FromHandle(Handle).WorkingArea }));
            Environment.ExitCode = 1;
            Close();
        }
        else if (backend is not null && !backend.HasExited)
        {
            try { await backend.StandardInput.WriteLineAsync("{\"protocol\":1,\"type\":\"stop\"}"); await backend.StandardInput.FlushAsync(); }
            catch (IOException) { }
        }
    }

    private async void OnClosingAsync(object? sender, FormClosingEventArgs e)
    {
        if (canClose) return;
        e.Cancel = true;
        if (stopping) return;
        stopping = true;
        landing.Visible = true; landing.BringToFront(); actions.Visible = false;
        LayoutWindowChrome();
        status.Text = Ui("Завершаю работу…\nСохраняю состояние и освобождаю модели.", "Shutting down…\nSaving state and releasing models.");
        try
        {
            if (setupProcess is not null && !setupProcess.HasExited)
                await CancelSetupAsync();
            if (setupTask is not null) await setupTask;
            if (browser?.CoreWebView2 is not null)
            {
                try { await browser.CoreWebView2.ExecuteScriptAsync("document.body.inert=true; window.dispatchEvent(new Event('pagehide')); "); }
                catch (InvalidOperationException) { }
            }
            foreach (var download in downloads.ToArray()) { download.StateChanged -= DownloadChanged; download.Cancel(); }
            downloads.Clear();
            if (backend is not null && !backend.HasExited)
            {
                try { await backend.StandardInput.WriteLineAsync("{\"protocol\":1,\"type\":\"stop\"}"); await backend.StandardInput.FlushAsync(); }
                catch (IOException) { }
                // No timeout releases ownership. A slow cleanup remains visible.
                await backend.WaitForExitAsync();
            }
            if (protocolTask is not null) await protocolTask;
            browser?.Dispose(); browser = null;
            // The job counter is authoritative, including nested jobs and children of exited roots.
            while (Program.Tree!.ActiveProcesses != 1)
            {
                status.Text = Ui("Ожидаю завершения процессов Jarvis…\nРесурсы ещё не освобождены.", "Waiting for Jarvis processes to exit…\nResources have not been released yet.");
                await Task.Delay(100);
            }
            backend?.Dispose();
            selectedDataMutex?.ReleaseMutex(); selectedDataMutex?.Dispose(); selectedDataMutex = null;
            try { Directory.Delete(profileDirectory, true); } catch (IOException) { } catch (UnauthorizedAccessException) { }
            if (options.SmokeOutput is not null && Environment.ExitCode == 0)
                File.WriteAllText(options.SmokeOutput, JsonSerializer.Serialize(new { success = backendStopped, owned_processes = 1, data_dir = options.DataDirectory, microphone_permission_tested = microphonePermissionTested, microphone_fake_stream_tested = microphoneFakeStreamTested, setup_completed = setupSmokeCompleted, window_chrome_tested = windowChromeTested, language_switch_tested = languageSwitchTested }));
            canClose = true; Close();
        }
        catch (Exception error)
        {
            status.Text = Ui("Не удалось подтвердить завершение.\nРесурсы остаются под контролем Jarvis.\nКод: ", "Could not verify shutdown.\nResources remain under Jarvis control.\nCode: ") + error.GetType().Name;
            Environment.ExitCode = 1;
        }
    }

    private async Task VerifyMicrophoneAsync()
    {
        smokeStage = "microphone_permission_policy";
        string localDocument = origin + "/";
        if (!CanUseMicrophone(localDocument, CoreWebView2PermissionKind.Microphone, true)
            || CanUseMicrophone(localDocument, CoreWebView2PermissionKind.Microphone, false))
            throw new InvalidOperationException("MicrophoneGesturePolicy");
        foreach (var kind in Enum.GetValues<CoreWebView2PermissionKind>())
            if (kind != CoreWebView2PermissionKind.Microphone && CanUseMicrophone(localDocument, kind, true))
                throw new InvalidOperationException("UnexpectedPermissionKind");
        foreach (string address in new[] { "not-a-uri", "http://127.0.0.1:1/", "https://example.invalid/",
            new UriBuilder(origin!) { UserName = "blocked" }.Uri.AbsoluteUri })
            if (CanUseMicrophone(address, CoreWebView2PermissionKind.Microphone, true))
                throw new InvalidOperationException("UnexpectedPermissionOrigin");

        // Fake media only: verify denial without a gesture, then immediate grant
        // with a gesture and stopped tracks. No samples are read or saved.
        int requestIndex = 0;
        foreach (bool gesture in new[] { false, true, false })
        {
            smokeStage = gesture ? "microphone_fake_stream" : "microphone_background_denied";
            // Earlier ExecuteScriptAsync calls can leave transient user activation.
            // Let it expire before probing a genuinely background request.
            if (!gesture) await Task.Delay(TimeSpan.FromSeconds(6));
            var result = await browser!.CoreWebView2.CallDevToolsProtocolMethodAsync("Runtime.evaluate", JsonSerializer.Serialize(new
            {
                expression = gesture
                    ? "navigator.mediaDevices.getUserMedia({audio:true}).then(s=>{const t=s.getAudioTracks();const live=t.length>0&&t.every(x=>x.readyState==='live');s.getTracks().forEach(x=>x.stop());return live&&t.every(x=>x.readyState==='ended')?'allowed':'bad-stream'}).catch(e=>e.name)"
                    : "new Promise(resolve=>setTimeout(()=>navigator.mediaDevices.getUserMedia({audio:true}).then(s=>{s.getTracks().forEach(x=>x.stop());resolve('allowed')}).catch(e=>resolve(e.name)),1000))",
                userGesture = gesture, awaitPromise = true
            })).WaitAsync(TimeSpan.FromSeconds(10));
            using var value = JsonDocument.Parse(result);
            string? outcome = value.RootElement.GetProperty("result").GetProperty("value").GetString();
            if (outcome != (gesture ? "allowed" : "NotAllowedError"))
            {
                smokeStage = $"microphone_request_{requestIndex}_{outcome}";
                throw new InvalidOperationException("MicrophonePermissionOutcome");
            }
            requestIndex++;
        }
        if (!microphonePermissionTested) throw new InvalidOperationException("MicrophonePermissionEventMissing");
        microphoneFakeStreamTested = true;
    }

    private async Task SmokeAsync()
    {
        try
        {
            await VerifyWindowChromeAsync();
            await VerifySettingsThemeAsync();
            await VerifySettingsLanguageAsync();
            // A fixed integration scenario; there is no arbitrary page-to-native API.
            var result = await browser!.CoreWebView2.ExecuteScriptAsync("""
                (async()=>{const s=await (await fetch('/api/session')).json();
                const post=async(p,b)=>{const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':s.csrf_token},body:JSON.stringify(b)}); if(!r.ok)throw Error('request');return r.json()};
                const c=await post('/api/conversations',{title:'Native host smoke'});
                const sent=await post('/api/conversations/'+c.id+'/messages',{text:'Hello from the installed window',submission_id:crypto.randomUUID()});
                window.__jarvisSmoke={conversation:c.id,sent};return true})()
                """);
            // ExecuteScriptAsync does not await promises; inspect bounded completion separately.
            var deadline = DateTime.UtcNow.AddSeconds(20);
            bool complete = false;
            while (DateTime.UtcNow < deadline && !stopping)
            {
                await Task.Delay(200);
                var probe = await browser.CoreWebView2.ExecuteScriptAsync("JSON.stringify(window.__jarvisSmoke||null)");
                if (probe is "null" or "\"null\"") continue;
                using var value = JsonDocument.Parse(JsonSerializer.Deserialize<string>(probe)!);
                var conversation = value.RootElement.GetProperty("conversation").GetString();
                await browser.CoreWebView2.ExecuteScriptAsync($"fetch('/api/conversations/{conversation}').then(r=>r.json()).then(d=>window.__jarvisFinished=d.turns.some(t=>t.status==='complete'))");
                await Task.Delay(100);
                if (await browser.CoreWebView2.ExecuteScriptAsync("window.__jarvisFinished===true") == "true") { complete = true; break; }
            }
            if (!complete) throw new InvalidOperationException("SmokeIncomplete");
            await VerifyMicrophoneAsync();
            await browser.CoreWebView2.ExecuteScriptAsync("window.dispatchEvent(new Event('focus'))");
            await Task.Delay(300);
            using var screenshot = File.Create(Path.ChangeExtension(options.SmokeOutput!, ".png"));
            await browser.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, screenshot);
            // Include native caption buttons in a second visual qualification image.
            Activate();
            await Task.Delay(200);
            using var windowImage = new Bitmap(Width, Height);
            using (var graphics = Graphics.FromImage(windowImage))
                graphics.CopyFromScreen(Location, Point.Empty, Size);
            windowImage.Save(Path.ChangeExtension(options.SmokeOutput!, ".window.png"), System.Drawing.Imaging.ImageFormat.Png);
            closeWindow.PerformClick();
        }
        catch (Exception error) { await FailureAsync(error.GetType().Name); }
    }
}
