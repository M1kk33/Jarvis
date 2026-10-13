using System.Drawing.Drawing2D;
using System.Runtime.InteropServices;

namespace Jarvis.Desktop;

internal sealed partial class MainWindow
{
    private readonly CaptionPanel windowControls = new();
    private readonly CaptionButton minimizeWindow = new(CaptionAction.Minimize, "Свернуть");
    private readonly CaptionButton maximizeWindow = new(CaptionAction.Maximize, "Развернуть");
    private readonly CaptionButton closeWindow = new(CaptionAction.Close, "Закрыть");
    private const int DwmWindowCornerPreference = 33, DwmRound = 2;
    private bool englishUI;
    private bool languageSwitchTested;
    private string Ui(string russian, string english) => englishUI ? english : russian;

    private void ApplyWindowLanguage(bool english)
    {
        englishUI = english;
        minimizeWindow.AccessibleName = Ui("Свернуть", "Minimize");
        closeWindow.AccessibleName = Ui("Закрыть", "Close");
        LayoutWindowChrome();
    }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        // A full client area disables Windows' automatic corner heuristics.
        // Opt in per HWND; DWM handles maximized/snapped windows and DPI itself.
        // Older Windows keeps its normal square frame without a custom region.
        if (OperatingSystem.IsWindowsVersionAtLeast(10, 0, 22000))
        {
            int preference = DwmRound;
            _ = DwmSetWindowAttribute(Handle, DwmWindowCornerPreference, ref preference, sizeof(int));
        }
    }

    private void InitializeWindowChrome()
    {
        // Keep Windows' sizing, system menu, Alt+F4 and Win+Arrow behavior. Only
        // the non-client painting is removed; window commands stay in the host.
        windowControls.BackColor = BackColor;
        windowControls.Controls.AddRange([minimizeWindow, maximizeWindow, closeWindow]);
        Controls.Add(windowControls);
        minimizeWindow.Click += (_, _) => WindowState = FormWindowState.Minimized;
        maximizeWindow.Click += (_, _) => ToggleMaximize();
        closeWindow.Click += (_, _) => Close();
        Resize += (_, _) => LayoutWindowChrome();
        DpiChanged += (_, _) => LayoutWindowChrome();
        landing.VisibleChanged += (_, _) => LayoutWindowChrome();
        Shown += (_, _) => LayoutWindowChrome();
        LayoutWindowChrome();
    }

    private void ToggleMaximize() => WindowState = WindowState == FormWindowState.Maximized
        ? FormWindowState.Normal : FormWindowState.Maximized;

    private void ApplyWindowTheme(bool light)
    {
        BackColor = light ? Color.FromArgb(248, 249, 251) : Color.FromArgb(16, 18, 22);
        ForeColor = light ? Color.FromArgb(32, 38, 47) : Color.FromArgb(230, 235, 238);
        windowControls.BackColor = BackColor;
        foreach (var button in new[] { minimizeWindow, maximizeWindow, closeWindow })
        {
            button.Light = light;
            button.BackColor = BackColor;
            button.ForeColor = light ? Color.FromArgb(88, 100, 117) : Color.FromArgb(187, 193, 203);
            button.Invalidate();
        }
    }

    private async Task VerifySettingsThemeAsync()
    {
        smokeStage = "settings_theme";
        await browser!.CoreWebView2.ExecuteScriptAsync("""
            (async()=>{
              const s=await(await fetch('/api/session')).json();
              const original=await(await fetch('/api/settings')).json();
              window.__jarvisThemeOriginal=original.preferences;
              const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':s.csrf_token},body:JSON.stringify({...original.preferences,theme:'light'})});
              if(!r.ok)throw Error('settings');
              document.documentElement.dataset.theme='light';
            })()
            """);
        var deadline = DateTime.UtcNow.AddSeconds(5);
        while (BackColor != Color.FromArgb(248, 249, 251) && DateTime.UtcNow < deadline) await Task.Delay(50);
        if (!minimizeWindow.Light || !maximizeWindow.Light || !closeWindow.Light) throw new InvalidOperationException("SettingsLightChrome");
        await browser.CoreWebView2.ExecuteScriptAsync("window.chrome.webview.postMessage({theme:'dark'});window.chrome.webview.postMessage('arbitrary-command')");
        await Task.Delay(100);
        if (!closeWindow.Light) throw new InvalidOperationException("SettingsMessageBoundary");
        await browser.CoreWebView2.ExecuteScriptAsync("""
            (async()=>{
              const s=await(await fetch('/api/session')).json();
              const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':s.csrf_token},body:JSON.stringify(window.__jarvisThemeOriginal)});
              if(!r.ok)throw Error('settings');
              document.documentElement.dataset.theme='dark';
            })()
            """);
        deadline = DateTime.UtcNow.AddSeconds(5);
        while (closeWindow.Light && DateTime.UtcNow < deadline) await Task.Delay(50);
        if (closeWindow.Light) throw new InvalidOperationException("SettingsDarkChrome");
        smokeStage = "conversation";
    }

    private void LayoutWindowChrome()
    {
        if (windowControls.IsDisposed || WindowState == FormWindowState.Minimized) return;
        int edge = WindowState == FormWindowState.Maximized ? 0 : Math.Max(4, DeviceDpi * 6 / 96);
        Padding = new Padding(edge);
        int width = DeviceDpi * 46 / 96, height = DeviceDpi * 36 / 96;
        // During startup/setup/shutdown, this also supplies a native drag area.
        int panelWidth = landing.Visible ? ClientSize.Width - edge * 2 : width * 3;
        windowControls.SetBounds(ClientSize.Width - edge - panelWidth, edge, panelWidth, height);
        minimizeWindow.SetBounds(panelWidth - width * 3, 0, width, height);
        maximizeWindow.SetBounds(panelWidth - width * 2, 0, width, height);
        closeWindow.SetBounds(panelWidth - width, 0, width, height);
        maximizeWindow.Restore = WindowState == FormWindowState.Maximized;
        maximizeWindow.AccessibleName = maximizeWindow.Restore ? Ui("Восстановить размер", "Restore") : Ui("Развернуть", "Maximize");
        maximizeWindow.Invalidate();
        windowControls.BringToFront();
    }

    private async Task VerifySettingsLanguageAsync()
    {
        smokeStage = "settings_language";
        await browser!.CoreWebView2.ExecuteScriptAsync("""
            (async()=>{
              const s=await(await fetch('/api/session')).json();
              const original=await(await fetch('/api/settings')).json();
              window.__jarvisLanguageOriginal=original.preferences;
              const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':s.csrf_token},body:JSON.stringify({...original.preferences,language:'en'})});
              if(!r.ok)throw Error('language');
              window.JarvisI18n.setLanguage('en');
            })()
            """);
        var deadline = DateTime.UtcNow.AddSeconds(5);
        while (!englishUI && DateTime.UtcNow < deadline) await Task.Delay(50);
        if (!englishUI || closeWindow.AccessibleName != "Close" || minimizeWindow.AccessibleName != "Minimize") throw new InvalidOperationException("SettingsEnglishChrome");
        await browser.CoreWebView2.ExecuteScriptAsync("window.chrome.webview.postMessage({language:'ru'});window.chrome.webview.postMessage('arbitrary-command')");
        await Task.Delay(100);
        if (!englishUI) throw new InvalidOperationException("LanguageMessageBoundary");
        await browser.CoreWebView2.ExecuteScriptAsync("""
            (async()=>{
              const s=await(await fetch('/api/session')).json();
              const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':s.csrf_token},body:JSON.stringify(window.__jarvisLanguageOriginal)});
              if(!r.ok)throw Error('restore-language');
              window.JarvisI18n.setLanguage(window.__jarvisLanguageOriginal.language);
            })()
            """);
        deadline = DateTime.UtcNow.AddSeconds(5);
        while (englishUI && DateTime.UtcNow < deadline) await Task.Delay(50);
        if (englishUI || closeWindow.AccessibleName != "Закрыть") throw new InvalidOperationException("SettingsRussianChrome");
        languageSwitchTested = true;
        smokeStage = "conversation";
    }

    protected override void WndProc(ref Message m)
    {
        const int WmNcCalcSize = 0x83, WmNcHitTest = 0x84;
        if (m.Msg == WmNcCalcSize)
        {
            if (IsZoomed(Handle))
            {
                // Windows keeps its sizing frame outside the work area when
                // maximized. Exclude that invisible frame from our client area.
                var proposed = Marshal.PtrToStructure<NativeRect>(m.LParam);
                var screen = Screen.FromRectangle(Rectangle.FromLTRB(proposed.Left, proposed.Top, proposed.Right, proposed.Bottom));
                var area = screen.WorkingArea;
                Marshal.StructureToPtr(new NativeRect { Left = area.Left, Top = area.Top, Right = area.Right, Bottom = area.Bottom }, m.LParam, false);
            }
            m.Result = 0; // Client content occupies the former caption too.
            return;
        }
        if (m.Msg == WmNcHitTest)
        {
            var point = PointToClient(new Point(unchecked((short)(m.LParam.ToInt64() & 0xffff)), unchecked((short)((m.LParam.ToInt64() >> 16) & 0xffff))));
            if (WindowState == FormWindowState.Normal)
            {
                int edge = Padding.Left;
                bool left = point.X < edge, right = point.X >= ClientSize.Width - edge;
                bool top = point.Y < edge, bottom = point.Y >= ClientSize.Height - edge;
                int hit = top ? (left ? 13 : right ? 14 : 12) : bottom ? (left ? 16 : right ? 17 : 15) : left ? 10 : right ? 11 : 0;
                if (hit != 0) { m.Result = hit; return; }
            }
            // CaptionPanel delegates only its empty space, never its buttons.
            if (windowControls.Bounds.Contains(point)) { m.Result = 2; return; }
            m.Result = 1;
            return;
        }
        base.WndProc(ref m);
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct NativeRect
    {
        public int Left, Top, Right, Bottom;
    }

    private sealed class CaptionPanel : Panel
    {
        protected override void WndProc(ref Message m)
        {
            if (m.Msg == 0x84) { m.Result = -1; return; } // HTTRANSPARENT to owning form.
            base.WndProc(ref m);
        }
    }

    private enum CaptionAction { Minimize, Maximize, Close }

    private sealed class CaptionButton : Button
    {
        private readonly CaptionAction action;
        private bool hovered;
        internal bool Restore;
        internal bool Light;

        public CaptionButton(CaptionAction action, string label)
        {
            this.action = action;
            AccessibleName = label;
            AccessibleRole = AccessibleRole.PushButton;
            FlatStyle = FlatStyle.Flat;
            FlatAppearance.BorderSize = 0;
            BackColor = Color.FromArgb(16, 18, 22);
            ForeColor = Color.FromArgb(187, 193, 203);
            SetStyle(ControlStyles.OptimizedDoubleBuffer | ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint, true);
        }

        protected override void OnMouseEnter(EventArgs e) { hovered = true; Invalidate(); base.OnMouseEnter(e); }
        protected override void OnMouseLeave(EventArgs e) { hovered = false; Invalidate(); base.OnMouseLeave(e); }

        protected override void OnPaint(PaintEventArgs e)
        {
            var hoverColor = Light ? Color.FromArgb(228, 233, 238) : Color.FromArgb(37, 42, 50);
            e.Graphics.Clear(hovered ? (action == CaptionAction.Close ? Color.FromArgb(196, 43, 60) : hoverColor) : BackColor);
            float scale = DeviceDpi / 96f, size = 10 * scale;
            float x = (Width - size) / 2, y = (Height - size) / 2;
            using var pen = new Pen(hovered && (action == CaptionAction.Close || !Light) ? Color.White : ForeColor, scale);
            e.Graphics.SmoothingMode = SmoothingMode.None;
            if (action == CaptionAction.Minimize) e.Graphics.DrawLine(pen, x, y + size / 2, x + size, y + size / 2);
            else if (action == CaptionAction.Close)
            {
                e.Graphics.DrawLine(pen, x, y, x + size, y + size);
                e.Graphics.DrawLine(pen, x + size, y, x, y + size);
            }
            else if (Restore)
            {
                e.Graphics.DrawRectangle(pen, x + 3 * scale, y, size - 3 * scale, size - 3 * scale);
                using var fill = new SolidBrush(hovered ? hoverColor : BackColor);
                e.Graphics.FillRectangle(fill, x, y + 3 * scale, size - 3 * scale, size - 3 * scale);
                e.Graphics.DrawRectangle(pen, x, y + 3 * scale, size - 3 * scale, size - 3 * scale);
            }
            else e.Graphics.DrawRectangle(pen, x, y, size, size);
            if (Focused && ShowFocusCues) ControlPaint.DrawFocusRectangle(e.Graphics, new Rectangle(4, 4, Width - 8, Height - 8), ForeColor, BackColor);
        }
    }

    private bool windowChromeTested;
    private string smokeStage = "startup";

    private async Task VerifyWindowChromeAsync()
    {
        VerifyWindowCorners();
        smokeStage = "caption_removed";
        if (RectangleToScreen(ClientRectangle) != Bounds) throw new InvalidOperationException("SystemCaptionVisible");
        smokeStage = "resize_edges";
        int inset = Math.Max(1, Padding.Left / 2);
        (Point Point, int Hit)[] edges = [
            (new(inset, inset), 13), (new(ClientSize.Width / 2, inset), 12),
            (new(ClientSize.Width - inset, inset), 14),
            (new(inset, ClientSize.Height / 2), 10), (new(ClientSize.Width - inset, ClientSize.Height / 2), 11),
            (new(inset, ClientSize.Height - inset), 16),
            (new(ClientSize.Width / 2, ClientSize.Height - inset), 15),
            (new(ClientSize.Width - inset, ClientSize.Height - inset), 17)
        ];
        foreach (var probe in edges)
        {
            var point = PointToScreen(probe.Point);
            var packed = (nint)((point.X & 0xffff) | ((point.Y & 0xffff) << 16));
            if (SendMessageW(Handle, 0x84, 0, packed) != probe.Hit) throw new InvalidOperationException("ResizeHitTest");
        }
        var original = Bounds;
        smokeStage = "maximize_work_area";
        maximizeWindow.PerformClick();
        await Task.Delay(200);
        if (WindowState != FormWindowState.Maximized || RectangleToScreen(ClientRectangle) != Screen.FromHandle(Handle).WorkingArea)
            throw new InvalidOperationException("MaximizeWorkArea");
        maximizeWindow.PerformClick();
        await Task.Delay(200);
        smokeStage = "restore_bounds";
        if (WindowState != FormWindowState.Normal || Bounds != original) throw new InvalidOperationException("RestoreBounds");
        smokeStage = "minimize";
        minimizeWindow.PerformClick();
        await Task.Delay(100);
        if (WindowState != FormWindowState.Minimized) throw new InvalidOperationException("MinimizeWindow");
        WindowState = FormWindowState.Normal;
        await Task.Delay(200);
        smokeStage = "restore_from_taskbar";
        if (Bounds != original || !windowControls.Visible || windowControls.Bottom > browser!.Height)
            throw new InvalidOperationException("RestoreFromTaskbar");
        VerifyWindowCorners();
        var dragStyle = await browser.CoreWebView2.ExecuteScriptAsync("getComputedStyle(document.querySelector('.chat header')).getPropertyValue('app-region')==='drag' && getComputedStyle(document.querySelector('.header-actions')).getPropertyValue('app-region')==='no-drag'");
        smokeStage = "web_drag_regions";
        if (dragStyle != "true" || !browser.CoreWebView2.Settings.IsNonClientRegionSupportEnabled)
            throw new InvalidOperationException("CaptionDragRegion");
        windowChromeTested = true;
        smokeStage = "conversation";
    }

    private void VerifyWindowCorners()
    {
        if (!OperatingSystem.IsWindowsVersionAtLeast(10, 0, 22000)) return;
        smokeStage = "system_corner_preference";
        if (DwmGetWindowAttribute(Handle, DwmWindowCornerPreference, out int preference, sizeof(int)) < 0
            || preference != DwmRound || Region is not null || AllowTransparency)
            throw new InvalidOperationException("SystemCornerPreference");
    }

    [DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(nint window, int attribute, ref int value, int size);
    [DllImport("dwmapi.dll")]
    private static extern int DwmGetWindowAttribute(nint window, int attribute, out int value, int size);

    [DllImport("user32.dll")]
    private static extern nint SendMessageW(nint window, uint message, nint wParam, nint lParam);
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool IsZoomed(nint window);
}
