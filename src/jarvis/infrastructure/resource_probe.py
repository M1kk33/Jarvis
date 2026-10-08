"""Live RAM and NVIDIA free-memory sampling for M4 admission."""

import asyncio
import subprocess
import time

import psutil

from jarvis.domain.resources import ResourceSnapshot, ResourceUnavailable


class SystemClock:
    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class NvidiaResourceProbe:
    def __init__(self, device_uuid: str, device_index: int = 0) -> None:
        self.device_uuid = device_uuid
        self.device_index = device_index

    def snapshot(self) -> ResourceSnapshot:
        available_ram = psutil.virtual_memory().available / (1024**3)
        available_vram: float | None = None
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=uuid,memory.free",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            rows = result.stdout.splitlines()
            if not 0 <= self.device_index < len(rows):
                raise ResourceUnavailable("Configured GPU index is absent")
            uuid, separator, free = rows[self.device_index].partition(",")
            if uuid.strip().casefold() != self.device_uuid.casefold():
                raise ResourceUnavailable(
                    "Configured GPU UUID does not match its index"
                )
            if separator:
                available_vram = int(free.strip()) / 1024
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        return ResourceSnapshot(available_vram, available_ram)

    def driver_version(self) -> str | None:
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=driver_version",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            rows = result.stdout.splitlines()
            if self.device_index < len(rows):
                return rows[self.device_index].strip() or None
        except (OSError, subprocess.SubprocessError):
            pass
        return None
