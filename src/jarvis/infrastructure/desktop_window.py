"""Own the lightweight WebView2 host and every descendant in a Windows job."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

from jarvis.infrastructure.windows_process import OwnedProcess, WindowsProcessSupervisor


class DesktopWindow:
    def __init__(self, title: str) -> None:
        self.title = title
        self.process: OwnedProcess | None = None
        self.directory: tempfile.TemporaryDirectory[str] | None = None
        self.started_at = 0.0

    def open(self, url: str) -> None:
        if self.process is not None:
            raise RuntimeError("Desktop window already started")
        self.directory = tempfile.TemporaryDirectory(
            prefix="jarvis-desktop-", ignore_cleanup_errors=True
        )
        cwd = Path(self.directory.name)
        environment = dict(os.environ)
        # Avoid the venv redirector, as in the console-free shortcut bootstrap.
        environment["__PYVENV_LAUNCHER__"] = str(
            Path(sys.prefix) / "Scripts" / "python.exe"
        )
        try:
            self.process = WindowsProcessSupervisor().spawn(
                Path(sys.base_prefix) / "python.exe",
                ["-m", "jarvis.interfaces.desktop_host"],
                cwd,
                environment,
                stdin_data=json.dumps({"url": url, "title": self.title}).encode(),
            )
            self.started_at = time.monotonic()
        except BaseException:
            self.directory.cleanup()
            self.directory = None
            raise

    def state(self) -> str:
        if self.process is None:
            return "idle"
        cwd = self.process.cwd
        if (cwd / "error").exists():
            return "failed"
        if (cwd / "closing").exists():
            return "closing"
        if self.process.root_exited():
            return "failed"
        if (cwd / "ready").exists():
            return "ready"
        return "failed" if time.monotonic() - self.started_at > 45 else "starting"

    def close(self) -> bool:
        if self.process is not None:
            try:
                stopped = self.process.stop(3, 10)
            except (OSError, RuntimeError):
                stopped = False
            if not stopped:
                return False
            self.process = None
        if self.directory is not None:
            self.directory.cleanup()
            self.directory = None
        return True
