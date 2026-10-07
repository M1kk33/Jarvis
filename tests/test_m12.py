"""M12 screenshot bounds, durable source, isolation, and recovery contracts."""

import asyncio
import base64
import hashlib
import struct
import threading
import time
import zlib

import httpx
import pytest

from jarvis.application.conversations import ConversationService
from jarvis.application.jobs import JobRunner
from jarvis.application.vision import VisionService, validate_screenshot
from jarvis.domain.jobs import JobStatus
from jarvis.domain.resources import AdmissionDenied, RuntimeProfile
from jarvis.domain.types import (
    RequestFailure,
    StreamDelta,
    StreamTerminal,
    StreamToolCall,
    SubmissionConflict,
    Turn,
    TurnStatus,
)
from jarvis.infrastructure.backup import create_backup, restore_backup
from jarvis.infrastructure.providers.fake import FakeChatProvider
from jarvis.infrastructure.sqlite_store import SqliteStore
from jarvis.interfaces.web import create_web_app


def _chunk(kind: bytes, content: bytes) -> bytes:
    return (
        struct.pack(">I", len(content))
        + kind
        + content
        + struct.pack(">I", zlib.crc32(kind + content))
    )


def _png(width=16, height=12, red=35) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = (b"\0" + bytes([red, 50, 80]) * width) * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(pixels))
        + _chunk(b"IEND", b"")
    )


class Handle:
    async def release(self):
        return None


class Manager:
    def __init__(self):
        self.events = []
        self.fail = False

    async def acquire(self, profile):
        self.events.append(("acquire", profile.name))
        if self.fail:
            raise AdmissionDenied("RAM reserve")
        return Handle()

    async def abort_request(self, _handle):
        self.events.append(("abort", None))

    async def unload(self):
        self.events.append(("unload", None))

    async def verify_previous_worker(self):
        return True


class Vision:
    def __init__(self):
        self.calls = []
        self.count = 1500
        self.mode = "answer"
        self.started = threading.Event()

    def count_vision_tokens(self, question, image, instructions):
        self.calls.append((question, image, instructions))
        return self.count

    def stream_vision(self, question, image, cancel, instructions):
        self.started.set()
        if self.mode == "wait":
            while not cancel.is_set():
                time.sleep(0.01)
            return
        if self.mode == "tool":
            yield StreamToolCall("x", "create_text_file", "{}")
        else:
            yield StreamDelta(
                "The Save button is visible; the smaller label is unclear."
            )
        yield StreamTerminal()


def _runner(store, manager=None, provider=None):
    manager = manager or Manager()
    provider = provider or Vision()
    service = ConversationService(
        store,
        FakeChatProvider(),
        resource_manager=manager,
        runtime_profile=RuntimeProfile("text-fake", 1, 1, 0, 0),
    )
    vision = VisionService(
        store,
        provider,
        manager,
        RuntimeProfile("vision-fake", 1, 1, 0, 0),
        "vision-fake:hash",
    )
    return JobRunner(store, service, "text-fake:hash", vision), manager, provider


def test_screenshot_png_bounds_and_corruption():
    good = _png()
    assert validate_screenshot(good) == (16, 12)
    for bad in (
        b"not png",
        _png(1025, 1),
        _png(1, 1025),
        good[:-1],
        good.replace(b"IDAT", b"tEXt", 1),
        good + b"extra",
        _png(1024, 1024, 12)[:50],
    ):
        with pytest.raises(RequestFailure):
            validate_screenshot(bad)


def test_submission_answer_source_and_backup_survive_restart(tmp_path):
    data = tmp_path / "data"
    with SqliteStore(data) as store:
        conversation = store.create_conversation("Screenshot")
        runner, manager, provider = _runner(store)

        async def scenario():
            image = _png()
            job, duplicate = await runner.submit_vision(
                conversation.id, "Which button?", image, "stable"
            )
            assert not duplicate and job.status is JobStatus.QUEUED
            source = store.get_vision_input(job.turn_id)
            assert (
                source is not None
                and store.get_artifact_bytes(source.artifact_id) == image
            )
            assert source.sha256 == hashlib.sha256(image).hexdigest()
            with pytest.raises(SubmissionConflict):
                await runner.submit_vision(
                    conversation.id, "Which button?", _png(red=70), "stable"
                )
            with pytest.raises(SubmissionConflict):
                await runner.submit(conversation.id, "Which button?", "stable")
            done = await runner.run_job(job.id)
            assert done.status is JobStatus.SUCCEEDED
            assert store.get_turn(job.turn_id).status is TurnStatus.COMPLETE
            assert "unclear" in store.get_answer(job.turn_id).text
            again, duplicate = await runner.submit_vision(
                conversation.id, "Which button?", image, "stable"
            )
            assert duplicate and again.id == job.id and len(provider.calls) == 1
            assert "untrusted evidence" in provider.calls[0][2]
            assert manager.events == [("acquire", "vision-fake"), ("unload", None)]
            return job, source

        job, source = asyncio.run(scenario())
        backup = create_backup(store)
    restored = tmp_path / "restored"
    restore_backup(backup, restored)
    with SqliteStore(restored) as store:
        assert store.get_artifact_bytes(source.artifact_id) == _png()
        assert store.get_answer(job.turn_id) is not None


def test_budget_oom_injection_and_cancel_keep_source(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Failures")
        manager, provider = Manager(), Vision()
        runner, _, _ = _runner(store, manager, provider)

        async def scenario():
            provider.count = 4000
            first, _ = await runner.submit_vision(
                conversation.id, "Read this", _png(), "one"
            )
            with pytest.raises(RequestFailure, match="context budget"):
                await runner.run_job(first.id)
            assert store.get_job(first.id).status is JobStatus.FAILED
            assert store.get_vision_input(first.turn_id) is not None
            provider.count = 1000
            provider.mode = "tool"
            second, _ = await runner.submit_vision(
                conversation.id, "Read this", _png(), "two"
            )
            with pytest.raises(RequestFailure, match="Malformed vision stream"):
                await runner.run_job(second.id)
            assert store.get_job(second.id).status is JobStatus.FAILED
            provider.mode = "answer"
            manager.fail = True
            third, _ = await runner.submit_vision(
                conversation.id, "Read this", _png(), "three"
            )
            with pytest.raises(AdmissionDenied):
                await runner.run_job(third.id)
            assert store.get_job(third.id).status is JobStatus.FAILED
            manager.fail = False
            provider.mode = "wait"
            fourth, _ = await runner.submit_vision(
                conversation.id, "Read this", _png(), "four"
            )
            task = asyncio.create_task(runner.run_job(fourth.id))
            await asyncio.wait_for(asyncio.to_thread(provider.started.wait), 2)
            await runner.cancel(fourth.id)
            assert (await task).status is JobStatus.CANCELLED
            assert store.get_vision_input(fourth.turn_id) is not None

        asyncio.run(scenario())


def test_web_upload_requires_session_csrf_and_private_preview(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Web")
        runner, _, _ = _runner(store)
        app, token = create_web_app(runner.conversations, runner, None, 8765)
        image = _png()
        body = {
            "question": "What is shown?",
            "submission_id": "one",
            "image_base64": base64.b64encode(image).decode(),
        }

        async def scenario():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
            ) as client:
                path = f"/api/conversations/{conversation.id}/vision"
                assert (await client.post(path, json=body)).status_code == 401
                await client.post("/api/setup", json={"token": token})
                session = (await client.get("/api/session")).json()
                assert session["vision_enabled"] is True
                assert (await client.post(path, json=body)).status_code == 403
                headers = {"x-csrf-token": session["csrf_token"]}
                assert (
                    await client.post(
                        path, json={**body, "image_base64": "bad"}, headers=headers
                    )
                ).status_code == 422
                sent = await client.post(path, json=body, headers=headers)
                assert sent.status_code == 202
                job = sent.json()["job"]
                source = store.get_vision_input(job["turn_id"])
                assert source is not None
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://127.0.0.1:8765",
                ) as stranger:
                    assert (
                        await stranger.get(
                            f"/api/artifacts/{source.artifact_id}/preview"
                        )
                    ).status_code == 401
                shown = await client.get(f"/api/artifacts/{source.artifact_id}/preview")
                assert shown.status_code == 200 and shown.content == image
                await runner.wait_for_idle()

        asyncio.run(scenario())


def test_interrupted_vision_job_resumes_with_its_original_source(tmp_path):
    data = tmp_path / "restart"
    with SqliteStore(data) as store:
        conversation = store.create_conversation("Restart")
        runner, _, _ = _runner(store)

        async def submit():
            job, _ = await runner.submit_vision(
                conversation.id, "Which button?", _png(), "resume-id"
            )
            store.start_job(job.id)
            return job

        job = asyncio.run(submit())
    with SqliteStore(data) as store:
        assert store.get_job(job.id).status is JobStatus.INTERRUPTED
        assert store.get_turn(job.turn_id).status is TurnStatus.INTERRUPTED
        assert store.get_vision_input(job.turn_id) is not None
        runner, _, provider = _runner(store)
        result = asyncio.run(runner.run_job(job.id))
        assert result.status is JobStatus.SUCCEEDED
        assert len(provider.calls) == 1
        assert store.get_answer(job.turn_id) is not None


def test_cancelled_submission_await_reconciles_exact_input(tmp_path, monkeypatch):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Submission race")
        runner, _, _ = _runner(store)
        original = store.run
        interrupted = False

        async def cancel_after_commit(operation):
            nonlocal interrupted
            result = await original(operation)
            if (
                not interrupted
                and isinstance(result, tuple)
                and len(result) == 2
                and isinstance(result[0], Turn)
            ):
                interrupted = True
                asyncio.current_task().cancel()
                await asyncio.sleep(0)
            return result

        monkeypatch.setattr(store, "run", cancel_after_commit)

        async def scenario():
            image = _png()
            job, duplicate = await runner.submit_vision(
                conversation.id, "Which button?", image, "stable"
            )
            assert duplicate and store.get_vision_input(job.turn_id) is not None
            with pytest.raises(SubmissionConflict):
                await runner.submit_vision(
                    conversation.id, "Different question", image, "stable"
                )
            assert len(store.get_messages(conversation.id)) == 1

        asyncio.run(scenario())


def test_corrupt_screenshot_blocks_recovered_job(tmp_path):
    data = tmp_path / "corrupt"
    with SqliteStore(data) as store:
        conversation = store.create_conversation("Corrupt source")
        runner, _, _ = _runner(store)
        job, _ = asyncio.run(
            runner.submit_vision(conversation.id, "What is shown?", _png(), "once")
        )
        source = store.get_vision_input(job.turn_id)
        assert source is not None
    (data / source.relative_path).write_bytes(b"changed")
    with SqliteStore(data) as reopened:
        runner, _, provider = _runner(reopened)
        with pytest.raises(RequestFailure, match="unavailable or corrupt"):
            asyncio.run(runner.run_job(job.id))
        assert reopened.get_job(job.id).status is JobStatus.FAILED
        assert reopened.get_answer(job.turn_id) is None
        assert provider.calls == []
