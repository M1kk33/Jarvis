"""M11 contracts for committed replies, bounded playback, and worker cleanup."""

import asyncio
import io
import json
import wave

import httpx
import pytest

from jarvis.application.conversations import ConversationService
from jarvis.application.jobs import JobRunner
from jarvis.application.spoken import SpokenPlaybackService, split_speech
from jarvis.domain.speech import SpeechInvalid, SpeechUnavailable
from jarvis.domain.types import StaleTurnError
from jarvis.infrastructure.piper_adapter import CpuPiperSynthesizer
from jarvis.infrastructure.piper_profile import PiperProfile
from jarvis.infrastructure.providers.fake import FakeChatProvider
from jarvis.infrastructure.sqlite_store import SqliteStore
from jarvis.interfaces.web import create_web_app

BASE = "http://127.0.0.1:8765"


def _wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(22_050)
        audio.writeframes(b"\0\0" * 512)
    return output.getvalue()


class Ram:
    def __init__(self, available=8.0):
        self.available = available

    def available_ram_gib(self):
        return self.available


class FakeSynthesizer:
    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.cleaned = asyncio.Event()
        self.block = False
        self.fail = False

    async def synthesize(self, text):
        self.calls.append(text)
        self.started.set()
        try:
            if self.block:
                await asyncio.Event().wait()
            if self.fail:
                raise SpeechUnavailable("Fake worker crashed")
            return _wav()
        finally:
            self.cleaned.set()


async def _completed(store):
    conversation = store.create_conversation("Spoken")
    service = ConversationService(store, FakeChatProvider())
    runner = JobRunner(store, service)
    job, _ = await runner.submit(conversation.id, "hello", "one")
    await runner.run_job(job.id)
    answer = store.get_answer(job.turn_id)
    assert answer is not None
    return conversation, job, answer, service, runner


def test_text_chunks_are_bounded_and_complete():
    text = ("A sentence about local speech. " * 70).strip()
    chunks = split_speech(text)
    assert 1 < len(chunks) <= 24
    assert all(1 <= len(chunk) <= 256 for chunk in chunks)
    assert " ".join(chunks) == text
    with pytest.raises(SpeechInvalid):
        split_speech("")
    with pytest.raises(SpeechInvalid):
        split_speech("x" * 4097)


def test_committed_legacy_reply_without_job_can_be_spoken(tmp_path):
    with SqliteStore(tmp_path) as store:
        service = ConversationService(store, FakeChatProvider())
        spoken = SpokenPlaybackService(store, FakeSynthesizer(), Ram())
        conversation = store.create_conversation("Legacy completed reply")

        async def scenario():
            result = await service.send_async(conversation.id, "hello", "legacy")
            assert result.answer is not None
            assert store.get_job_by_turn(result.turn.id) is None
            session, count = await spoken.start(
                conversation.id, result.turn.id, result.answer.id
            )
            assert count == 1
            assert await spoken.chunk(session, 0) == _wav()

        asyncio.run(scenario())


def test_completed_reply_only_and_ordered_audio_queue(tmp_path):
    with SqliteStore(tmp_path) as store:
        fake = FakeSynthesizer()
        spoken = SpokenPlaybackService(store, fake, Ram())

        async def scenario():
            conversation, job, answer, _, _ = await _completed(store)
            assert fake.calls == []
            with pytest.raises(StaleTurnError):
                await spoken.start("wrong", job.turn_id, answer.id)
            with pytest.raises(StaleTurnError):
                await spoken.start(conversation.id, job.turn_id, "wrong")
            session, count = await spoken.start(conversation.id, job.turn_id, answer.id)
            assert count == 1
            with pytest.raises(SpeechInvalid):
                await spoken.chunk(session, 1)
            assert await spoken.chunk(session, 0) == _wav()
            with pytest.raises(SpeechInvalid):
                await spoken.chunk(session, 0)
            newer, _ = await spoken.start(conversation.id, job.turn_id, answer.id)
            assert not await spoken.stop(session)
            assert await spoken.chunk(newer, 0) == _wav()
            assert await spoken.stop(newer)
            with pytest.raises(SpeechUnavailable):
                await spoken.chunk(newer, 0)
            assert store.get_answer(job.turn_id) == answer

        asyncio.run(scenario())


def test_pending_turn_ram_pressure_failure_and_synthesis_cancel(tmp_path):
    with SqliteStore(tmp_path) as store:
        fake = FakeSynthesizer()
        ram = Ram(3.79)
        spoken = SpokenPlaybackService(store, fake, ram)

        async def scenario():
            conversation, job, answer, _, runner = await _completed(store)
            pending, _ = await runner.submit(conversation.id, "next", "two")
            with pytest.raises(StaleTurnError):
                await spoken.start(conversation.id, pending.turn_id, answer.id)
            session, _ = await spoken.start(conversation.id, job.turn_id, answer.id)
            with pytest.raises(SpeechUnavailable, match="RAM"):
                await spoken.chunk(session, 0)
            assert fake.calls == []
            ram.available = 8
            fake.fail = True
            with pytest.raises(SpeechUnavailable, match="crashed"):
                await spoken.chunk(session, 0)
            assert store.get_answer(job.turn_id) == answer
            fake.fail = False
            fake.block = True
            fake.started.clear()
            fake.cleaned.clear()
            in_flight = asyncio.create_task(spoken.chunk(session, 0))
            await asyncio.wait_for(fake.started.wait(), 2)
            assert await spoken.stop(session)
            with pytest.raises(asyncio.CancelledError):
                await in_flight
            assert fake.cleaned.is_set()
            assert not await spoken.current(session)

        asyncio.run(scenario())


def test_web_spoken_auth_identity_and_text_survives_failure(tmp_path):
    with SqliteStore(tmp_path) as store:
        fake = FakeSynthesizer()
        spoken = SpokenPlaybackService(store, fake, Ram())
        service = ConversationService(store, FakeChatProvider())
        runner = JobRunner(store, service)
        app, token = create_web_app(service, runner, None, 8765, spoken=spoken)

        async def scenario():
            conversation = store.create_conversation("Browser")
            job, _ = await runner.submit(conversation.id, "hello", "one")
            await runner.run_job(job.id)
            answer = store.get_answer(job.turn_id)
            assert answer is not None
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=BASE
            ) as client:
                body = {
                    "conversation_id": conversation.id,
                    "turn_id": job.turn_id,
                    "message_id": answer.id,
                }
                assert (
                    await client.post("/api/spoken/start", json=body)
                ).status_code == 401
                await client.post("/api/setup", json={"token": token})
                session = (await client.get("/api/session")).json()
                assert session["spoken_enabled"] is True
                csrf = {"x-csrf-token": session["csrf_token"]}
                assert (
                    await client.post("/api/spoken/start", json=body)
                ).status_code == 403
                assert (
                    await client.post(
                        "/api/spoken/start",
                        json={**body, "turn_id": "wrong"},
                        headers=csrf,
                    )
                ).status_code == 404
                started = (
                    await client.post("/api/spoken/start", json=body, headers=csrf)
                ).json()
                assert fake.calls == []
                audio = await client.post(
                    f"/api/spoken/{started['session_id']}/chunks/0",
                    json={},
                    headers=csrf,
                )
                assert audio.status_code == 200
                assert audio.headers["content-type"].startswith("audio/wav")
                assert audio.content == _wav()
                assert (
                    await client.post(
                        f"/api/spoken/{started['session_id']}/stop",
                        json={},
                        headers=csrf,
                    )
                ).json() == {"stopped": True}
                fake.fail = True
                restarted = (
                    await client.post("/api/spoken/start", json=body, headers=csrf)
                ).json()
                failed = await client.post(
                    f"/api/spoken/{restarted['session_id']}/chunks/0",
                    json={},
                    headers=csrf,
                )
                assert failed.status_code == 503
                page = (
                    await client.get(f"/api/conversations/{conversation.id}")
                ).json()
                assert any(
                    message["id"] == answer.id and message["text"] == answer.text
                    for message in page["messages"]
                )

        asyncio.run(scenario())


def test_piper_worker_crash_and_unverified_exit(tmp_path, monkeypatch):
    profile = PiperProfile(
        name="piper-norman-medium-cpu",
        version="1.8.0",
        voice_revision="c10ece1aade47bb51c153c893d14e5bf8e5b7117",
        engine_license="GPL-3.0-or-later",
        voice_license="MIT",
        voice_data_license="public-domain",
        python=tmp_path / "python.exe",
        voice_model=tmp_path / "en_US-norman-medium.onnx",
        model_sha256="0" * 64,
        config_sha256="0" * 64,
        model_card_sha256="0" * 64,
        ram_peak_gib=0.8,
        cpu_threads=4,
    )
    monkeypatch.setattr(PiperProfile, "verify_artifacts", lambda _self: None)

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
    adapter = CpuPiperSynthesizer(profile, tmp_path / "run")
    monkeypatch.setattr(adapter.supervisor, "spawn", lambda *_args, **_kwargs: process)

    async def scenario():
        with pytest.raises(SpeechUnavailable, match="worker failed"):
            await adapter.synthesize("hello")
        assert process.stops == 1
        process.stop_result = False
        with pytest.raises(SpeechUnavailable, match="cleanup is unverified"):
            await adapter.synthesize("hello")
        with pytest.raises(SpeechUnavailable, match="cleanup is unverified"):
            await adapter.synthesize("hello")

    asyncio.run(scenario())
