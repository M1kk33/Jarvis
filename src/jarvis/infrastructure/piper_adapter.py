"""Pinned Piper synthesis through the shared owned CPU process boundary."""

import asyncio
import io
import shutil
import wave
from pathlib import Path
from uuid import uuid4

from jarvis.domain.speech import SpeechUnavailable
from jarvis.infrastructure.cpu_worker import (
    WorkerCleanupUnverified,
    run_cpu_worker,
    settle,
)
from jarvis.infrastructure.piper_profile import PiperProfile
from jarvis.infrastructure.windows_process import WindowsProcessSupervisor

MAX_WAV_BYTES = 20 * 22_050 * 2 + 4096


class CpuPiperSynthesizer:
    def __init__(self, profile: PiperProfile, run_root: Path) -> None:
        self.profile = profile
        self.run_root = run_root.resolve()
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.supervisor = WindowsProcessSupervisor()
        self._unavailable = False

    async def synthesize(self, text: str) -> bytes:
        if self._unavailable:
            raise SpeechUnavailable("Piper worker cleanup is unverified")
        if not 1 <= len(text) <= 256:
            raise ValueError("Piper text chunk is out of bounds")
        await asyncio.to_thread(self.profile.verify_artifacts)
        cwd = self.run_root / f"piper-{uuid4().hex}"
        cwd.mkdir()
        output = cwd / "audio.wav"
        try:
            result = await run_cpu_worker(
                self.supervisor,
                self.profile.python,
                Path(__file__).with_name("piper_worker.py"),
                cwd,
                {
                    "version": 1,
                    "voice": str(self.profile.voice_model),
                    "output": str(output),
                    "text": text,
                },
                timeout=60,
                threads=4,
            )
            if "error" in result:
                raise SpeechUnavailable("Piper worker failed")
            if (
                result.get("sample_rate") != 22_050
                or type(result.get("bytes")) is not int
            ):
                raise SpeechUnavailable("Piper worker result is invalid")
            if not output.is_file() or output.stat().st_size > MAX_WAV_BYTES:
                raise SpeechUnavailable("Piper audio is missing or oversized")
            audio = output.read_bytes()
            try:
                with wave.open(io.BytesIO(audio), "rb") as wav:
                    if (
                        wav.getnchannels() != 1
                        or wav.getsampwidth() != 2
                        or wav.getframerate() != 22_050
                        or wav.getnframes() > 20 * 22_050
                        or wav.getcomptype() != "NONE"
                        or wav.getnframes() * 2 != result["bytes"]
                    ):
                        raise SpeechUnavailable("Piper audio format is invalid")
            except (wave.Error, EOFError) as exc:
                raise SpeechUnavailable("Piper audio is corrupt") from exc
            return audio
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
                    raise SpeechUnavailable("Piper staging cleanup failed") from exc
