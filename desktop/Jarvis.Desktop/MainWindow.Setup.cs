using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace Jarvis.Desktop;

internal sealed partial class MainWindow
{
    private Process? setupProcess;
    private Task<bool>? setupTask;
    private Mutex? selectedDataMutex;

    private void ShowWelcome()
    {
        actions.Controls.Clear();
        status.Text = "Добро пожаловать в Jarvis\n\nВаш помощник на этом компьютере.\n\nУстановите комплект моделей или подключите существующую папку.\nДля текущего комплекта нужна видеокарта NVIDIA с установленным драйвером.";
        var install = MakeButton("Установить комплект моделей");
        var connect = MakeButton("У меня уже есть Jarvis");
        actions.Controls.AddRange([install, connect]);
        connect.Click += async (_, _) =>
        {
            using var picker = new FolderBrowserDialog { Description = "Выберите папку данных Jarvis", UseDescriptionForTitle = true };
            if (picker.ShowDialog(this) != DialogResult.OK) return;
            if (!File.Exists(Path.Combine(picker.SelectedPath, "model.toml")))
            {
                status.Text = "В этой папке не найдены настройки Jarvis.\nВыберите существующую папку данных или установите комплект моделей.";
                return;
            }
            await ConnectDataAsync(picker.SelectedPath);
        };
        install.Click += async (_, _) =>
        {
            using var package = new OpenFileDialog { Title = "Выберите комплект моделей Jarvis", Filter = "Комплект моделей Jarvis|Jarvis-models-*.zip", CheckFileExists = true };
            if (package.ShowDialog(this) != DialogResult.OK) return;
            using var parent = new FolderBrowserDialog { Description = "Где хранить модели и диалоги? Здесь будет создана папка JarvisData.", UseDescriptionForTitle = true };
            if (parent.ShowDialog(this) != DialogResult.OK) return;
            var destination = Path.Combine(parent.SelectedPath, "JarvisData");
            if (Directory.Exists(destination) || File.Exists(destination))
            {
                status.Text = "Папка JarvisData здесь уже существует.\nПодключите её или выберите другое место для новой установки.";
                return;
            }
            actions.Controls.Clear();
            var cancel = MakeButton("Отменить установку");
            cancel.Click += async (_, _) => { cancel.Enabled = false; await CancelSetupAsync(); };
            actions.Controls.Add(cancel);
            setupTask = InstallModelsAsync(package.FileName, destination);
            bool installed = await setupTask;
            if (stopping) return;
            if (installed) await ConnectDataAsync(destination);
            else
            {
                actions.Controls.Clear();
                var retry = MakeButton("Вернуться к настройке");
                retry.Click += (_, _) => ShowWelcome();
                actions.Controls.Add(retry);
            }
        };
    }

    private async Task ConnectDataAsync(string directory)
    {
        try
        {
            // First-run began with a setup mutex. Also acquire the chosen data identity
            // before persisting it or opening a database, just like a normal launch.
            var identity = Path.GetFullPath(directory).TrimEnd('\\').ToLowerInvariant();
            var digest = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(identity)))[..16];
            var mutex = new Mutex(false, @"Local\JarvisNative_" + digest);
            bool owned;
            try { owned = mutex.WaitOne(0); } catch (AbandonedMutexException) { owned = true; }
            if (!owned) { mutex.Dispose(); status.Text = "Эта папка уже открыта в другом окне Jarvis.\nПереключитесь в него, чтобы продолжить."; return; }
            selectedDataMutex = mutex;
            var settings = Path.Combine(Program.SettingsDirectory, "desktop.json");
            var temporary = settings + "." + Guid.NewGuid().ToString("N") + ".tmp";
            File.WriteAllText(temporary, JsonSerializer.Serialize(new { data_dir = directory }), new UTF8Encoding(false));
            File.Move(temporary, settings, true);
            options = options with { DataDirectory = directory };
            Text = "Jarvis · " + digest;
            actions.Controls.Clear();
            status.Text = "Подготавливаю ваше рабочее пространство…";
            await StartAsync();
        }
        catch (Exception error)
        {
            selectedDataMutex?.ReleaseMutex(); selectedDataMutex?.Dispose(); selectedDataMutex = null;
            status.Text = "Не удалось подключить папку.\nКод: " + error.GetType().Name;
        }
    }

    private async Task CancelSetupAsync()
    {
        status.Text = "Отменяю установку…\nУдаляю только временные файлы этой попытки.";
        if (setupProcess is not null && !setupProcess.HasExited)
        {
            try { await setupProcess.StandardInput.WriteLineAsync("cancel"); await setupProcess.StandardInput.FlushAsync(); }
            catch (IOException) { }
        }
    }

    private async Task<bool> InstallModelsAsync(string archive, string destination)
    {
        bool completed = false;
        status.Text = "Проверяю комплект моделей и свободное место…";
        try
        {
            // The helper inherits the root's kill-on-close job at process creation.
            var start = new ProcessStartInfo(options.Python)
            {
                UseShellExecute = false, CreateNoWindow = true,
                RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
                StandardInputEncoding = new UTF8Encoding(false), StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8, WorkingDirectory = AppContext.BaseDirectory
            };
            start.ArgumentList.Add("-B"); start.ArgumentList.Add("-u");
            start.ArgumentList.Add("-m"); start.ArgumentList.Add("jarvis.desktop_setup");
            start.Environment.Remove("__PYVENV_LAUNCHER__"); start.Environment.Remove("PYTHONHOME"); start.Environment.Remove("PYTHONPATH");
            start.Environment["PYTHONUTF8"] = "1";
            if (options.DevelopmentSource is not null) start.Environment["PYTHONPATH"] = options.DevelopmentSource;
            setupProcess = Process.Start(start) ?? throw new InvalidOperationException();
            if (Program.Tree is null || !Program.Tree.Contains(setupProcess.Handle)) throw new InvalidOperationException("Ownership");
            _ = DrainAsync(setupProcess.StandardError);
            await setupProcess.StandardInput.WriteLineAsync(JsonSerializer.Serialize(new { protocol = 1, archive, destination }));
            await setupProcess.StandardInput.FlushAsync();
            bool terminal = false;
            while (await ReadBoundedAsync(setupProcess.StandardOutput) is string line)
            {
                using var packet = JsonDocument.Parse(line);
                var root = packet.RootElement;
                if (root.GetProperty("protocol").GetInt32() != 1 || terminal) throw new InvalidDataException();
                switch (root.GetProperty("type").GetString())
                {
                    case "progress":
                        long done = root.GetProperty("done").GetInt64(), total = root.GetProperty("total").GetInt64();
                        if (total <= 0 || done < 0 || done > total) throw new InvalidDataException();
                        if (!stopping) status.Text = $"Устанавливаю модели · {100 * done / total}%\n\n{done / 1073741824.0:F1} из {total / 1073741824.0:F1} ГиБ\nПроверяю целостность каждого файла. Это нужно только при установке.";
                        break;
                    case "complete": completed = terminal = true; break;
                    case "cancelled": terminal = true; status.Text = "Установка отменена.\nВы можете начать её снова в удобное время."; break;
                    case "error":
                        terminal = true;
                        var code = root.GetProperty("code").GetString();
                        status.Text = code switch
                        {
                            "FileExistsError" => "Папка уже существует. Выберите другое место или подключите готовый Jarvis.",
                            "OSError" => "Не удалось записать файлы. Проверьте свободное место и доступ к выбранной папке.",
                            "CalledProcessError" or "FileNotFoundError" or "RuntimeError" => "Не удалось подготовить компоненты. Проверьте комплект и установленный драйвер NVIDIA.",
                            _ => "Комплект моделей не прошёл проверку.\nИспользуйте комплект, поставляемый с этой версией Jarvis."
                        };
                        break;
                    default: throw new InvalidDataException();
                }
            }
            await setupProcess.WaitForExitAsync();
            if (!terminal || (completed && setupProcess.ExitCode != 0)) throw new InvalidDataException();
            return completed;
        }
        catch (Exception error)
        {
            status.Text = "Установка не завершена.\nКод: " + error.GetType().Name;
            return false;
        }
        finally
        {
            if (setupProcess is not null)
            {
                setupProcess.StandardInput.Close();
                await setupProcess.WaitForExitAsync();
                // Do not report completion or start the backend with an unverified tree.
                while (Program.Tree!.ActiveProcesses != 1) await Task.Delay(100);
                setupProcess.Dispose(); setupProcess = null;
            }
        }
    }
}
