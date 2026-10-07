"""Exact M14 Playwright/headless-shell pairing, outside any personal profile."""

import hashlib
import os
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

PLAYWRIGHT_VERSION = "1.63.0"
CHROMIUM_REVISION = "1243"
CHROMIUM_SHA256 = "addfa79abb060e1e514e155ed745d4bf96140bca402735958bb4e223aea0b98c"


@dataclass(frozen=True)
class BrowserProfile:
    executable: Path

    @classmethod
    def installed(cls) -> "BrowserProfile":
        try:
            installed_version = version("playwright")
        except PackageNotFoundError as exc:
            raise ValueError("Install the pinned browser extra first") from exc
        if installed_version != PLAYWRIGHT_VERSION:
            raise ValueError("Playwright version is not pinned M14 build")
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise ValueError("Browser cache location is unavailable")
        root = Path(local_app_data) / "ms-playwright"
        executable = (
            root
            / f"chromium_headless_shell-{CHROMIUM_REVISION}"
            / "chrome-headless-shell-win64"
            / "chrome-headless-shell.exe"
        )
        profile = cls(executable)
        profile.verify()
        return profile

    def verify(self) -> None:
        path = self.executable
        if not path.is_file() or path.is_symlink():
            raise ValueError("Pinned browser executable is unavailable")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != CHROMIUM_SHA256:
            raise ValueError("Pinned browser executable hash mismatch")

    @property
    def identity(self) -> str:
        return f"playwright-{PLAYWRIGHT_VERSION}:chromium-{CHROMIUM_REVISION}:{CHROMIUM_SHA256}"
