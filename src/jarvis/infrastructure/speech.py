"""Supervised, transient CPU speech worker with no core inference imports."""

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from uuid import uuid4

import psutil

from jarvis.domain.speech import SpeechInvalid, SpeechUnavailable
from jarvis.infrastructure.speech_profile import SpeechProfile
from jarvis.infrastructure.windows_process import (
    OwnedProcess,
    WindowsProcessSupervisor,
    named_job_tree_exited,
)


class SystemRamProbe:
    def available_ram_gib(self) -> float | None:
        try:
            return psutil.virtual_memory().available / 1024**3
        except OSError:
            return None


async def _settle(task: asyncio.Task):
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


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
        identity = uuid4().hex
        cwd = self.run_root / f"speech-{identity}"
        job_name = f"Local\\JarvisSpeech-{identity}"
        cwd.mkdir()
        audio_path = (
            cwd
            / {
                "audio/webm": "clip.webm",
                "audio/ogg": "clip.ogg",
                "audio/wav": "clip.wav",
            }[media_type]
        )
        process: OwnedProcess | None = None
        spawn_issued = False
        try:
            descriptor = os.open(
                audio_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            with os.fdopen(descriptor, "wb") as output:
                output.write(clip)
                output.flush()
                os.fsync(output.fileno())
            system_root = os.environ.get("SystemRoot", r"C:\Windows")
            environment = {
                "SystemRoot": system_root,
                "WINDIR": system_root,
                "PATH": str(self.profile.python.parent)
                + os.pathsep
                + str(Path(system_root) / "System32"),
                "TEMP": str(cwd),
                "TMP": str(cwd),
                "PYTHONNOUSERSITE": "1",
                "CUDA_VISIBLE_DEVICES": "-1",
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "OMP_NUM_THREADS": "4",
                "OPENBLAS_NUM_THREADS": "1",
            }
            request = (
                json.dumps(
                    {
                        "version": 1,
                        "audio": str(audio_path),
                        "model": str(self.profile.model_dir),
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
            launcher = Path(__file__).with_name("speech_worker.py")
            spawn_issued = True
            spawn_task = asyncio.create_task(
                asyncio.to_thread(
                    self.supervisor.spawn,
                    self.profile.python,
                    [str(launcher)],
                    cwd,
                    environment,
                    job_name=job_name,
                    stdin_data=request,
                )
            )
            try:
                process = await asyncio.shield(spawn_task)
            except asyncio.CancelledError:
                process = await _settle(spawn_task)
                raise
            deadline = time.monotonic() + 120
            while not await asyncio.to_thread(process.root_exited):
                if time.monotonic() >= deadline:
                    raise SpeechUnavailable("Speech worker timed out")
                await asyncio.sleep(0.1)
            if not await asyncio.to_thread(process.stdout.wait_eof, 2):
                raise SpeechUnavailable("Speech worker output did not close")
            lines = process.stdout.text.splitlines()
            if len(lines) != 1:
                raise SpeechUnavailable("Speech worker returned no single result")
            try:
                result = json.loads(lines[0])
            except ValueError as exc:
                raise SpeechUnavailable("Speech worker result is invalid") from exc
            if type(result) is not dict or result.get("version") != 1:
                raise SpeechUnavailable("Speech worker protocol mismatch")
            if result.get("error") in {
                "invalid_media",
                "duration_limit",
                "transcript_limit",
            }:
                raise SpeechInvalid(str(result["error"]))
            if "error" in result or type(result.get("text")) is not str:
                raise SpeechUnavailable("Speech worker failed")
            return result["text"]
        finally:
            if process is not None:
                try:
                    stopped = await _settle(
                        asyncio.create_task(asyncio.to_thread(process.stop, 0, 10))
                    )
                except Exception:  # noqa: BLE001 - owned process boundary
                    stopped = False
                if not stopped:
                    self._unavailable = True
                    raise SpeechUnavailable(
                        "Speech worker cleanup is unverified"
                    ) from None
            elif spawn_issued:
                try:
                    exited = await _settle(
                        asyncio.create_task(
                            asyncio.to_thread(named_job_tree_exited, job_name, 10)
                        )
                    )
                except Exception:  # noqa: BLE001 - Windows ownership verification
                    exited = False
                if not exited:
                    self._unavailable = True
                    raise SpeechUnavailable(
                        "Speech worker startup cleanup is unverified"
                    ) from None
            if not self._unavailable and cwd.parent == self.run_root:
                try:
                    await _settle(
                        asyncio.create_task(asyncio.to_thread(shutil.rmtree, cwd))
                    )
                except OSError as exc:
                    self._unavailable = True
                    raise SpeechUnavailable("Speech staging cleanup failed") from exc
