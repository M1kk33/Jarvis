"""Transient faster-whisper adapter; inference imports remain in its child process."""

import asyncio
import os
import shutil
from pathlib import Path
from uuid import uuid4

import psutil

from jarvis.domain.speech import SpeechInvalid, SpeechUnavailable
from jarvis.infrastructure.cpu_worker import (
    WorkerCleanupUnverified,
    run_cpu_worker,
    settle,
)
from jarvis.infrastructure.speech_profile import SpeechProfile
from jarvis.infrastructure.windows_process import WindowsProcessSupervisor


class SystemRamProbe:
    def available_ram_gib(self) -> float | None:
        try:
            return psutil.virtual_memory().available / 1024**3
        except OSError:
            return None


class CpuSpeechTranscriber:
    def __init__(self, profile: SpeechProfile, run_root: Path) -> None:
        self.profile = profile
        self.run_root = run_root.resolve()
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.supervisor = WindowsProcessSupervisor()
        self._unavailable = False

    async def transcribe(self, clip: bytes, media_type: str) -> str:
        if self._unavailable:
            raise SpeechUnavailable("Speech worker cleanup is unverified")
        await asyncio.to_thread(self.profile.verify_artifacts)
        cwd = self.run_root / f"speech-{uuid4().hex}"
        cwd.mkdir()
        audio_path = (
            cwd
            / {
                "audio/webm": "clip.webm",
                "audio/ogg": "clip.ogg",
                "audio/wav": "clip.wav",
            }[media_type]
        )
        try:
            descriptor = os.open(
                audio_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            with os.fdopen(descriptor, "wb") as output:
                output.write(clip)
                output.flush()
                os.fsync(output.fileno())
            result = await run_cpu_worker(
                self.supervisor,
                self.profile.python,
                Path(__file__).with_name("speech_worker.py"),
                cwd,
                {
                    "version": 1,
                    "audio": str(audio_path),
                    "model": str(self.profile.model_dir),
                },
                timeout=120,
                threads=4,
            )
            if result.get("error") in {
                "invalid_media",
                "duration_limit",
                "transcript_limit",
            }:
                raise SpeechInvalid(str(result["error"]))
            text = result.get("text")
            if "error" in result or type(text) is not str:
                raise SpeechUnavailable("Speech worker failed")
            return text
        except WorkerCleanupUnverified:
            self._unavailable = True
            raise
        finally:
            if not self._unavailable and cwd.parent == self.run_root:
                try:
                    await settle(
                        asyncio.create_task(asyncio.to_thread(shutil.rmtree, cwd))
                    )
                except OSError as exc:
                    self._unavailable = True
                    raise SpeechUnavailable("Speech staging cleanup failed") from exc
