"""Admission and bounds for one transient CPU transcription request."""

import asyncio
import math

from jarvis.application.resource_manager import CpuWorkerGate, ResourceManager, _settle
from jarvis.domain.speech import (
    RamProbe,
    SpeechInvalid,
    SpeechUnavailable,
    Transcriber,
    WorkerCleanupUnverified,
)

MAX_CLIP_BYTES = 3 * 1024 * 1024
MAX_TRANSCRIPT_CHARS = 65_536
ALLOWED_MEDIA = frozenset({"audio/webm", "audio/ogg", "audio/wav"})
RAM_PEAK_GIB = 1.5
RAM_RESERVE_GIB = 3.0


class SpeechService:
    def __init__(
        self,
        transcriber: Transcriber,
        ram: RamProbe,
        manager: ResourceManager | None = None,
        cpu_gate: asyncio.Lock | None = None,
    ) -> None:
        self.transcriber = transcriber
        self.ram = ram
        self.manager = manager
        self.cpu_gate = cpu_gate or CpuWorkerGate()
        self._lock = asyncio.Lock()
        self._active_task: asyncio.Task | None = None

    async def transcribe(self, clip: bytes, media_type: str) -> str:
        if media_type not in ALLOWED_MEDIA or not 0 < len(clip) <= MAX_CLIP_BYTES:
            raise SpeechInvalid("Unsupported or oversized audio clip")
        if self._lock.locked():
            raise SpeechUnavailable("A transcription is already running")
        async with self._lock:
            self._active_task = asyncio.current_task()
            try:
                async with self.cpu_gate:
                    if getattr(self.cpu_gate, "unavailable", False):
                        raise SpeechUnavailable(
                            "Previous CPU worker cleanup is unverified"
                        )
                    return await self._run(clip, media_type)
            finally:
                self._active_task = None

    async def _run(self, clip: bytes, media_type: str) -> str:
        if self.manager is None:
            return await self._admitted(clip, media_type)
        async with self.manager.cpu_worker():
            return await self._admitted(clip, media_type)

    async def _admitted(self, clip: bytes, media_type: str) -> str:
        available = self.ram.available_ram_gib()
        if (
            available is None
            or not math.isfinite(available)
            or available < RAM_PEAK_GIB + RAM_RESERVE_GIB
        ):
            raise SpeechUnavailable("Not enough verified available RAM for speech")
        try:
            text = await self.transcriber.transcribe(clip, media_type)
        except WorkerCleanupUnverified:
            self.cpu_gate.unavailable = True  # type: ignore[attr-defined]
            if self.manager is not None:
                await _settle(asyncio.create_task(self.manager.mark_cpu_unavailable()))
            raise
        if len(text) > MAX_TRANSCRIPT_CHARS:
            raise SpeechInvalid("Transcript exceeds the input limit")
        return text.strip()

    async def cancel(self) -> bool:
        task = self._active_task
        if task is None or task.done():
            return False
        task.cancel()
        try:
            await _settle(task)
        except asyncio.CancelledError:
            pass
        return True
