"""One supervised Playwright worker with a pinned disposable Chromium shell."""

import asyncio
import os
import shutil
import stat
import sys
from pathlib import Path
from uuid import uuid4

from jarvis.domain.types import BrowserCleanupUnverified, RequestFailure
from jarvis.infrastructure.browser_profile import BrowserProfile
from jarvis.infrastructure.cpu_worker import (
    WorkerCleanupUnverified,
    run_cpu_worker,
    settle,
)
from jarvis.infrastructure.windows_process import WindowsProcessSupervisor

MAX_HTML_BYTES = 512 * 1024
MAX_SCREENSHOT_BYTES = 2 * 1024 * 1024
BROWSER_JOB_MEMORY_BYTES = 1536 * 1024 * 1024


class SafeBrowserRenderer:
    def __init__(self, profile: BrowserProfile, run_root: Path) -> None:
        self.profile = profile
        self.run_root = run_root.resolve()
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.supervisor = WindowsProcessSupervisor()
        self._unavailable = False

    @property
    def identity(self) -> str:
        return self.profile.identity + ":offline-inline-v1"

    async def render(self, html: bytes) -> tuple[str, bool, bytes]:
        if self._unavailable:
            raise RequestFailure("unavailable", "Browser cleanup is unverified")
        if not 1 <= len(html) <= MAX_HTML_BYTES:
            raise RequestFailure("invalid_request", "HTML is outside browser limits")
        await asyncio.to_thread(self.profile.verify)
        cwd = self.run_root / f"browser-{uuid4().hex}"
        cwd.mkdir()
        html_file = cwd / "page.html"
        screenshot = cwd / "page.png"
        try:
            descriptor = os.open(html_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(html)
                output.flush()
                os.fsync(output.fileno())
            result = await run_cpu_worker(
                self.supervisor,
                Path(sys.executable),
                Path(__file__).with_name("browser_worker.py"),
                cwd,
                {
                    "version": 1,
                    "html": str(html_file),
                    "screenshot": str(screenshot),
                    "browser": str(self.profile.executable),
                },
                timeout=30,
                threads=2,
                job_memory_bytes=BROWSER_JOB_MEMORY_BYTES,
            )
            text = result.get("text")
            truncated = result.get("truncated")
            if (
                "error" in result
                or type(text) is not str
                or type(truncated) is not bool
                or len(text) > 10240
            ):
                raise RequestFailure("unavailable", "Browser rendering failed")
            info = os.lstat(screenshot)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or getattr(info, "st_file_attributes", 0) & 0x400
                or info.st_size > MAX_SCREENSHOT_BYTES
            ):
                raise RequestFailure("unavailable", "Browser screenshot is invalid")
            png = screenshot.read_bytes()
            if len(png) != info.st_size:
                raise RequestFailure("unavailable", "Browser screenshot changed")
            return text, truncated, png
        except WorkerCleanupUnverified as exc:
            self._unavailable = True
            raise BrowserCleanupUnverified(
                "unavailable", "Browser cleanup is unverified"
            ) from exc
        finally:
            if not self._unavailable:
                if cwd.resolve().parent != self.run_root:
                    self._unavailable = True
                    raise RequestFailure("unavailable", "Browser staging path changed")
                try:
                    await settle(
                        asyncio.create_task(asyncio.to_thread(shutil.rmtree, cwd))
                    )
                except OSError as exc:
                    self._unavailable = True
                    raise RequestFailure(
                        "unavailable", "Browser staging cleanup failed"
                    ) from exc
