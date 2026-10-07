"""Windows shortcut entry point. Uses the project's existing Python environment."""

import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

# pythonw has no console streams. Never write setup tokens or user text to a log.
with (
    open(os.devnull, "w", encoding="utf-8") as output,
    redirect_stdout(output),
    redirect_stderr(output),
):
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    try:
        from jarvis.bootstrap import desktop_main

        result = desktop_main(sys.argv[1:])
    except BaseException as exc:  # noqa: BLE001 -- console-free entry point boundary
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None,
            "Не удалось запустить Jarvis. Проверьте установленное окружение "
            f"и параметры ярлыка (тип ошибки: {type(exc).__name__}).",
            "Jarvis",
            0x10,
        )
        result = 1
raise SystemExit(result)
