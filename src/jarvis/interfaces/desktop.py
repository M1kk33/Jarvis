"""Small console-free controller for the existing local web interface."""

import asyncio
import threading
import tkinter as tk
import webbrowser
from collections.abc import Callable
from pathlib import Path
from queue import Empty, Queue
from tkinter import messagebox, ttk
from typing import Protocol


class WebServer(Protocol):
    started: bool
    should_exit: bool

    async def serve(self) -> None: ...


class AppWindow(Protocol):
    def open(self, url: str) -> None: ...
    def state(self) -> str: ...
    def close(self) -> bool: ...


class WebControl:
    def __init__(self) -> None:
        self.stop = threading.Event()
        self.events: Queue[tuple[str, str]] = Queue()

    def error(self, message: str) -> None:
        self.events.put(("error", message))


async def serve_controlled(
    server: WebServer, control: WebControl, setup_url: str
) -> None:
    """Signal readiness only after bind; stop through normal server cleanup."""

    async def monitor() -> None:
        announced = False
        while True:
            if control.stop.is_set():
                server.should_exit = True
                return
            if server.started and not announced:
                control.events.put(("ready", setup_url))
                announced = True
            await asyncio.sleep(0.25)

    watcher = asyncio.create_task(monitor())
    try:
        await server.serve()
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


def run_desktop(
    run_server: Callable[[WebControl], int],
    title: str,
    app_window: AppWindow | None = None,
) -> int:
    root = tk.Tk()
    root.title(title)
    root.geometry("460x210")
    root.resizable(False, False)
    root.iconbitmap(str(Path(__file__).parent / "static" / "jarvis.ico"))
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("TFrame", background="#17191e")
    style.configure(
        "TLabel", background="#17191e", foreground="#e7e9ec", font=("Segoe UI", 10)
    )
    style.configure("TButton", background="#9ce4cf", foreground="#18362c", padding=8)
    panel = ttk.Frame(root, padding=20)
    panel.pack(fill="both", expand=True)
    ttk.Label(panel, text="Jarvis", font=("Segoe UI", 20)).pack(anchor="w")
    status = tk.StringVar(value="Запускается…")
    ttk.Label(panel, textvariable=status, wraplength=420).pack(anchor="w", pady=12)
    buttons = ttk.Frame(panel)
    buttons.pack(anchor="w")
    control = WebControl()
    setup_url = ""
    failed = False
    finished = False
    result = 0

    def open_interface() -> None:
        try:
            if not webbrowser.open(setup_url):
                raise OSError("Browser unavailable")
        except (OSError, webbrowser.Error):
            messagebox.showerror(
                "Jarvis",
                "Не удалось открыть браузер. Проверьте браузер по умолчанию.",
                parent=root,
            )

    def stop() -> None:
        if finished:
            root.destroy()
            return
        control.stop.set()
        open_button.configure(state="disabled")
        stop_button.configure(state="disabled")
        status.set("Остановка: ждём завершения работы и освобождения моделей…")

    open_button = ttk.Button(
        buttons, text="Открыть интерфейс", command=open_interface, state="disabled"
    )
    if app_window is None:
        open_button.pack(side="left", padx=(0, 12))
    stop_button = ttk.Button(buttons, text="Остановить Jarvis", command=stop)
    stop_button.pack(side="left")
    root.protocol("WM_DELETE_WINDOW", stop)

    def worker() -> None:
        code = 1
        try:
            code = run_server(control)
        except BaseException as exc:  # noqa: BLE001 -- surface thread failure to the GUI
            control.error(
                "Не удалось запустить Jarvis. Проверьте настройки, файлы моделей "
                f"и свободный порт (тип ошибки: {type(exc).__name__})."
            )
        finally:
            control.events.put(("finished", str(code)))

    def poll() -> None:
        nonlocal setup_url, failed, finished, result
        while True:
            try:
                event, value = control.events.get_nowait()
            except Empty:
                break
            if event == "ready" and not control.stop.is_set():
                setup_url = value
                if app_window is None:
                    status.set("Работает. Интерфейс открыт в браузере.")
                    open_button.configure(state="normal")
                    open_interface()
                else:
                    try:
                        app_window.open(setup_url)
                        root.title(title + " · запуск")
                        status.set("Открывается окно Jarvis…")
                    except Exception:  # noqa: BLE001 -- report launch errors in the GUI
                        control.error(
                            "Не удалось открыть окно Jarvis. Проверьте WebView2 Runtime и установленное окружение."
                        )
                        stop()
            elif event == "error":
                failed = True
                root.deiconify()
                status.set("Не удалось запустить или корректно остановить Jarvis.")
                messagebox.showerror("Jarvis", value, parent=root)
            elif event == "finished":
                finished = True
                result = int(value)
                open_button.configure(state="disabled")
                if app_window is not None and not app_window.close():
                    failed = True
                    result = 1
                    root.deiconify()
                    status.set("Не удалось подтвердить остановку окна Jarvis.")
                if result and not failed:
                    failed = True
                    status.set("Не удалось запустить Jarvis. Проверьте настройки.")
                if not failed:
                    root.destroy()
                    return
                stop_button.configure(state="normal", text="Закрыть")
        if app_window is not None and not finished and not control.stop.is_set():
            window_state = app_window.state()
            if window_state == "ready":
                root.withdraw()
            elif window_state == "closing":
                stop()
            elif window_state == "failed":
                control.error(
                    "Окно Jarvis не запустилось или неожиданно закрылось. Нужен Microsoft Edge WebView2 Runtime."
                )
                stop()
        root.after(250, poll)

    thread = threading.Thread(target=worker, name="jarvis-web", daemon=False)
    thread.start()
    root.after(100, poll)
    try:
        root.mainloop()
    finally:
        control.stop.set()
        thread.join()
        if app_window is not None and not app_window.close():
            raise RuntimeError("Desktop process tree exit is unverified")
    return result
