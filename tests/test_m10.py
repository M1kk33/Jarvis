"""M10 offline contracts: bounded preview, admission, and owned worker cleanup."""

import asyncio
import json

import httpx
import pytest

from jarvis.application.conversations import ConversationService
from jarvis.application.jobs import JobRunner
from jarvis.application.resource_manager import ResourceManager
from jarvis.application.speech import MAX_CLIP_BYTES, SpeechService
from jarvis.domain.speech import SpeechInvalid, SpeechUnavailable
from jarvis.infrastructure.fake_runtime import FakeProcessRuntime
from jarvis.infrastructure.gpu_lock import NamedGpuLock
from jarvis.infrastructure.providers.fake import FakeChatProvider
from jarvis.infrastructure.speech import CpuSpeechTranscriber
from jarvis.infrastructure.speech_profile import SpeechProfile
from jarvis.infrastructure.sqlite_store import SqliteStore
from jarvis.interfaces.web import create_web_app

BASE = "http://127.0.0.1:8765"


class Ram:
    def __init__(self, available=8.0):
        self.available = available

    def available_ram_gib(self):
        return self.available


class FakeTranscriber:
    def __init__(self, text="create a file now"):
        self.text = text
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False

    async def transcribe(self, _clip, _media_type):
        self.calls += 1
        self.started.set()
        if self.block:
            await self.release.wait()
        return self.text


def test_web_preview_requires_explicit_send_and_auth(tmp_path):
    with SqliteStore(tmp_path) as store:
        provider = FakeChatProvider()
        conversations = ConversationService(store, provider)
        fake = FakeTranscriber()
        app, token = create_web_app(
            conversations,
            JobRunner(store, conversations),
            None,
            8765,
            SpeechService(fake, Ram()),
        )

        async def scenario():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=BASE
            ) as client:
                assert (
                    await client.post(
                        "/api/transcribe",
                        content=b"a",
                        headers={"content-type": "audio/webm"},
                    )
                ).status_code == 401
                assert (
                    await client.post("/api/setup", json={"token": token})
                ).status_code == 200
                session = (await client.get("/api/session")).json()
                assert session["speech_enabled"] is True
                csrf = {
                    "x-csrf-token": session["csrf_token"],
                    "content-type": "audio/webm",
                }
                assert (
                    await client.post(
                        "/api/transcribe",
                        content=b"a",
                        headers={"content-type": "audio/webm"},
                    )
                ).status_code == 403
                result = await client.post(
                    "/api/transcribe", content=b"a", headers=csrf
                )
                assert result.status_code == 200
                assert result.json() == {"text": "create a file now"}
                assert fake.calls == 1
                assert store.list_conversations() == []
                assert store.list_jobs() == []
                assert (
                    await client.post(
                        "/api/transcribe",
                        content=b"a",
                        headers={**csrf, "origin": "https://evil.test"},
                    )
                ).status_code == 403
                assert (
                    await client.post(
                        "/api/transcribe",
                        content=b"a",
                        headers={**csrf, "content-type": "application/octet-stream"},
                    )
                ).status_code == 422
                assert (
                    await client.post(
                        "/api/transcribe",
                        content=b"a" * (MAX_CLIP_BYTES + 1),
                        headers=csrf,
                    )
                ).status_code == 413
                fake.block = True
                fake.started.clear()
                pending = asyncio.create_task(
                    client.post("/api/transcribe", content=b"a", headers=csrf)
                )
                await asyncio.wait_for(fake.started.wait(), 2)
                cancelled = await client.post(
                    "/api/transcribe/cancel",
                    headers={"x-csrf-token": session["csrf_token"]},
                )
                assert cancelled.status_code == 200
                assert cancelled.json() == {"cancelled": True}
                assert (await pending).status_code == 409

        asyncio.run(scenario())


def test_admission_silence_and_cancel():
    async def scenario():
        ram = Ram(None)
        fake = FakeTranscriber("")
        service = SpeechService(fake, ram)
        with pytest.raises(SpeechUnavailable):
            await service.transcribe(b"x", "audio/wav")
        assert fake.calls == 0
        ram.available = 4.49
        with pytest.raises(SpeechUnavailable):
            await service.transcribe(b"x", "audio/wav")
        ram.available = 4.5
        assert await service.transcribe(b"x", "audio/wav") == ""
        with pytest.raises(SpeechInvalid):
            await service.transcribe(b"", "audio/wav")
        fake.block = True
        task = asyncio.create_task(service.transcribe(b"x", "audio/wav"))
        await fake.started.wait()
        await asyncio.sleep(0)
        with pytest.raises(SpeechUnavailable):
            await service.transcribe(b"x", "audio/wav")
        assert await service.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not await service.cancel()

    asyncio.run(scenario())


def test_pinned_speech_profile_rejects_changed_model(tmp_path):
    model = tmp_path / "model"
    model.mkdir()
    for name in ("model.bin", "tokenizer.json", "config.json", "vocabulary.txt"):
        (model / name).write_bytes(b"different")
    python = tmp_path / "python.exe"
    python.write_bytes(b"placeholder")
    profile = SpeechProfile(
        name="faster-whisper-base-cpu-int8",
        version="1.2.1",
        python=python,
        model_dir=model,
        model_sha256="0" * 64,
        tokenizer_sha256="0" * 64,
        config_sha256="0" * 64,
        vocabulary_sha256="0" * 64,
        ram_peak_gib=1.5,
        cpu_threads=4,
    )
    with pytest.raises(ValueError, match="hash mismatch"):
        profile.verify_artifacts()


def test_worker_crash_and_unverified_cleanup(tmp_path, monkeypatch):
    profile = SpeechProfile(
        name="faster-whisper-base-cpu-int8",
        version="1.2.1",
        python=tmp_path / "python.exe",
        model_dir=tmp_path,
        model_sha256="0" * 64,
        tokenizer_sha256="0" * 64,
        config_sha256="0" * 64,
        ram_peak_gib=1.5,
        cpu_threads=4,
        vocabulary_sha256="0" * 64,
    )
    monkeypatch.setattr(SpeechProfile, "verify_artifacts", lambda _self: None)

    class Output:
        text = json.dumps({"version": 1, "error": "worker_failure"}) + "\n"

        def wait_eof(self, _timeout):
            return True

    class Process:
        stdout = Output()
        stop_result = True
        stops = 0

        def root_exited(self):
            return True

        def stop(self, _graceful, _kill):
            self.stops += 1
            return self.stop_result

    process = Process()
    adapter = CpuSpeechTranscriber(profile, tmp_path / "run")
    monkeypatch.setattr(adapter.supervisor, "spawn", lambda *_args, **_kwargs: process)

    async def scenario():
        with pytest.raises(SpeechUnavailable, match="worker failed"):
            await adapter.transcribe(b"fixture", "audio/wav")
        assert process.stops == 1
        process.stop_result = False
        with pytest.raises(SpeechUnavailable, match="cleanup is unverified"):
            await adapter.transcribe(b"fixture", "audio/wav")
        with pytest.raises(SpeechUnavailable, match="cleanup is unverified"):
            await adapter.transcribe(b"fixture", "audio/wav")

    asyncio.run(scenario())


def test_cancelled_worker_terminates_owned_process(tmp_path, monkeypatch):
    profile = SpeechProfile(
        name="faster-whisper-base-cpu-int8",
        version="1.2.1",
        python=tmp_path / "python.exe",
        model_dir=tmp_path,
        model_sha256="0" * 64,
        tokenizer_sha256="0" * 64,
        config_sha256="0" * 64,
        vocabulary_sha256="0" * 64,
        ram_peak_gib=1.5,
        cpu_threads=4,
    )
    monkeypatch.setattr(SpeechProfile, "verify_artifacts", lambda _self: None)

    class Process:
        stops = 0

        def root_exited(self):
            return False

        def stop(self, _graceful, _kill):
            self.stops += 1
            return True

    process = Process()
    adapter = CpuSpeechTranscriber(profile, tmp_path / "run")
    monkeypatch.setattr(adapter.supervisor, "spawn", lambda *_args, **_kwargs: process)

    async def scenario():
        task = asyncio.create_task(adapter.transcribe(b"fixture", "audio/wav"))
        while not list((tmp_path / "run").glob("speech-*/clip.wav")):
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.stops == 1
        assert not list((tmp_path / "run").iterdir())

    asyncio.run(scenario())


def test_cpu_worker_gate_blocks_heavy_acquisition(tmp_path):
    class Probe:
        def snapshot(self):
            from jarvis.domain.resources import ResourceSnapshot

            return ResourceSnapshot(8, 12)

    class Clock:
        async def sleep(self, _seconds):
            await asyncio.Event().wait()

        def now(self):
            return 0.0

    manager = ResourceManager(
        FakeProcessRuntime(tmp_path / "run"),
        Probe(),
        NamedGpuLock(f"GPU-m10-{tmp_path.name}"),
        Clock(),
    )

    async def scenario():
        from jarvis.domain.resources import RuntimeProfile

        profile = RuntimeProfile("text-fake", 4, 5, 0.5, 1, calibrated=True)
        async with manager.cpu_worker():
            acquire = asyncio.create_task(manager.acquire(profile))
            await asyncio.sleep(0.05)
            assert not acquire.done()
        handle = await asyncio.wait_for(acquire, 5)
        await handle.release()
        await manager.close()

    asyncio.run(scenario())
