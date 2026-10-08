"""Offline regressions for the implementation audit's reproduced failures."""

import asyncio
import hashlib
import http.client
import json
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from jarvis.application.conversations import ConversationService
from jarvis.application.jobs import JobRunner
from jarvis.application.resource_manager import CpuWorkerGate, ResourceManager
from jarvis.application.speech import SpeechService
from jarvis.application.spoken import SpokenPlaybackService
from jarvis.application.tools import ToolPolicy, ToolRegistry, ToolService
from jarvis.domain.jobs import JobStatus, same_profile_identity
from jarvis.domain.resources import (
    ResourceUnavailable,
    RuntimeProfile,
    canonical_gpu_uuid,
)
from jarvis.domain.speech import SpeechUnavailable, WorkerCleanupUnverified
from jarvis.domain.tools import InvocationStatus, ToolContextRecord
from jarvis.domain.types import (
    ActiveTurnError,
    RequestFailure,
    StaleTurnError,
    StreamDelta,
    StreamTerminal,
    StreamToolCall,
    SubmissionConflict,
    TurnStatus,
    VisionInput,
)
from jarvis.infrastructure.cpu_worker import run_cpu_worker
from jarvis.infrastructure.gpu_lock import NamedGpuLock
from jarvis.infrastructure.providers.comfyui import ComfyRuntime
from jarvis.infrastructure.providers.fake import FakeChatProvider
from jarvis.infrastructure.providers.llama_cpp import (
    LlamaChatProvider,
    LlamaEndpoint,
    LlamaServerRuntime,
)
from jarvis.infrastructure.resource_probe import NvidiaResourceProbe
from jarvis.infrastructure.sqlite_store import SqliteStore
from jarvis.infrastructure.web_fetch import PublicPageFetcher
from jarvis.infrastructure.windows_process import WindowsProcessSupervisor
from jarvis.interfaces.web import create_web_app

BASE = "http://127.0.0.1:8765"


class ClockBackend:
    def __init__(self, blocked=False, failure=False):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.blocked = blocked
        self.failure = failure

    def inspect(self, _name, _arguments):
        return "clock"

    def execute(self, _name, _arguments):
        self.calls += 1
        self.started.set()
        if self.blocked:
            assert self.release.wait(5)
        if self.failure:
            raise RequestFailure("unavailable", "Expected worker failure")
        return {"utc": "2026-10-08T00:00:00+00:00"}


class FollowupProvider(FakeChatProvider):
    def __init__(self, blocked=False):
        super().__init__()
        self.blocked = blocked
        self.started = threading.Event()
        self.release = threading.Event()

    def count_prompt_tokens_with_tools(self, _messages, _instructions, _tools):
        return 100

    def stream_with_tools(self, _messages, cancel, _instructions, _tools):
        self.started.set()
        if self.blocked:
            assert self.release.wait(5)
        if not cancel.is_set():
            yield StreamDelta("Finished.")
        yield StreamTerminal("cancelled" if cancel.is_set() else None)


async def waiting_job(store, runner, tools, label):
    conversation = store.create_conversation(label)
    job, _ = await runner.submit(conversation.id, label, label)
    job = await store.run(lambda: store.start_job(job.id))
    invocation = await tools.propose_and_run(
        job.turn_id, StreamToolCall("clock", "current_time", "{}"), job.attempt
    )
    await store.run(lambda: store.mark_job_waiting(job.id))
    return job, invocation


async def authenticate(client, token):
    await client.post("/api/setup", json={"token": token}, headers={"origin": BASE})
    csrf = (await client.get("/api/session")).json()["csrf_token"]
    return {"origin": BASE, "x-csrf-token": csrf}


def test_web_approval_queues_behind_inference_and_returns_before_action(tmp_path):
    with SqliteStore(tmp_path) as store:
        backend = ClockBackend()
        tools = ToolService(
            store, backend, ToolPolicy(approval_required=frozenset({"current_time"}))
        )
        provider = FollowupProvider(blocked=True)
        conversations = ConversationService(store, provider, tool_service=tools)
        runner = JobRunner(store, conversations)
        app, token = create_web_app(conversations, runner, tools, 8765)

        async def scenario():
            approved_job, invocation = await waiting_job(
                store, runner, tools, "approval"
            )
            other, _ = await runner.submit(
                store.create_conversation("inference").id, "run", "run"
            )
            running = runner.launch(other.id)
            assert await asyncio.to_thread(provider.started.wait, 2)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=BASE
            ) as client:
                headers = await authenticate(client, token)
                response = await client.post(
                    f"/api/approvals/{invocation.id}/approve", headers=headers
                )
                assert response.status_code == 202
                await asyncio.sleep(0.02)
                assert backend.calls == 0
                assert (
                    store.get_job(approved_job.id).status is JobStatus.WAITING_APPROVAL
                )
                assert (
                    store.get_invocation(invocation.id).status
                    is InvocationStatus.WAITING_APPROVAL
                )
                provider.blocked = False
                provider.release.set()
                await running
                await runner.wait_for_idle()
                assert backend.calls == 1
                assert store.get_job(approved_job.id).status is JobStatus.SUCCEEDED
                snapshot = (
                    await client.get(
                        f"/api/conversations/{approved_job.conversation_id}"
                    )
                ).json()
                assert snapshot["invocations"][0]["receipt_json"]

        asyncio.run(scenario())


@pytest.mark.parametrize("shutdown", [False, True])
def test_approved_actions_serialize_and_cancel_waits_for_receipt(tmp_path, shutdown):
    with SqliteStore(tmp_path) as store:
        backend = ClockBackend(blocked=True)
        tools = ToolService(
            store, backend, ToolPolicy(approval_required=frozenset({"current_time"}))
        )
        runner = JobRunner(
            store, ConversationService(store, FollowupProvider(), tool_service=tools)
        )

        async def scenario():
            first, first_call = await waiting_job(store, runner, tools, "first")
            second, second_call = await waiting_job(store, runner, tools, "second")
            first_approval = await tools.trusted_issue_approval(first_call.id)
            second_approval = await tools.trusted_issue_approval(second_call.id)
            first_task = runner.launch(
                first.id, approved=(first_call.id, first_approval)
            )
            assert await asyncio.to_thread(backend.started.wait, 2)
            runner.launch(second.id, approved=(second_call.id, second_approval))
            if shutdown:
                stop = asyncio.create_task(runner.shutdown())
                while not store.get_job(first.id).cancel_requested:
                    await asyncio.sleep(0.001)
            else:
                pending = await runner.cancel(first.id)
                assert pending.status is JobStatus.CANCEL_REQUESTED
                await runner.cancel(first.id)
            await asyncio.sleep(0.02)
            assert not first_task.done()
            assert backend.calls == 1
            backend.release.set()
            if shutdown:
                await stop
            else:
                await runner.wait_for_idle()
            assert store.get_job(first.id).status is JobStatus.CANCELLED
            receipt = store.get_invocation(first_call.id)
            assert receipt.status is InvocationStatus.COMPLETE
            assert json.loads(receipt.result_json)["status"] == "ok"
            assert store.get_answer(first.turn_id) is None
            assert backend.calls == (1 if shutdown else 2)
            assert store.get_job(second.id).status is (
                JobStatus.CANCELLED if shutdown else JobStatus.SUCCEEDED
            )

        asyncio.run(scenario())


@pytest.mark.parametrize("failure", [False, True])
def test_repeated_cancellation_retains_generic_worker_result(tmp_path, failure):
    with SqliteStore(tmp_path) as store:
        turn, _ = store.begin_turn(
            store.create_conversation("cancel").id, "time", "time"
        )
        backend = ClockBackend(blocked=True, failure=failure)
        tools = ToolService(store, backend)

        async def scenario():
            task = asyncio.create_task(
                tools.propose_and_run(
                    turn.id, StreamToolCall("clock", "current_time", "{}")
                )
            )
            assert await asyncio.to_thread(backend.started.wait, 2)
            task.cancel()
            await asyncio.sleep(0.01)
            task.cancel()
            backend.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            result = store.get_invocations(turn.id)[0]
            assert result.status is InvocationStatus.COMPLETE
            assert json.loads(result.result_json)["status"] == (
                "error" if failure else "ok"
            )
            assert result.receipt_json is not None
            assert backend.calls == 1

        asyncio.run(scenario())


@pytest.mark.parametrize(
    "text,vision", [("original", False), ("changed", False), ("original", True)]
)
def test_cancelled_submission_checks_stored_text_and_input_kind(tmp_path, text, vision):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("identity")
        runner = JobRunner(store, ConversationService(store, FakeChatProvider()))

        async def scenario():
            if vision:
                job_id = str(uuid4())
                artifact = str(uuid4())
                raw = b"test screenshot"
                relative = f"artifacts/{job_id}/{artifact}.png"
                path = tmp_path / relative
                path.parent.mkdir(parents=True)
                path.write_bytes(raw)
                source = VisionInput(
                    artifact,
                    job_id,
                    relative,
                    len(raw),
                    hashlib.sha256(raw).hexdigest(),
                    1,
                    1,
                    "vision",
                )
                turn, _ = store.begin_turn(
                    conversation.id, "original", "id", job_id, "vision", source
                )
                store.request_job_cancel(job_id)
                store.settle_job_cancel(job_id)
            else:
                job, _ = await runner.submit(conversation.id, "original", "id")
                await runner.run_job(job.id)
                turn = store.get_turn(job.turn_id)
            entered, release = threading.Event(), threading.Event()

            def hold_owner():
                entered.set()
                assert release.wait(3)

            block = asyncio.create_task(store.run(hold_owner))
            assert await asyncio.to_thread(entered.wait, 2)
            task = asyncio.create_task(runner.submit(conversation.id, text, "id"))
            await asyncio.sleep(0.01)
            task.cancel()
            release.set()
            if text != "original" or vision:
                with pytest.raises(SubmissionConflict):
                    await task
            else:
                recovered, duplicate = await task
                assert duplicate and recovered.turn_id == turn.id
            await block
            assert len(store.get_turns(conversation.id)) == 1

        asyncio.run(scenario())


def test_legacy_cancelled_duplicate_cannot_interrupt_original(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("legacy")
        original, _ = store.begin_turn(conversation.id, "original", "id")
        service = ConversationService(store, FakeChatProvider())

        async def scenario():
            entered, release = threading.Event(), threading.Event()
            begin = store.begin_turn

            def delayed(*args):
                result = begin(*args)
                entered.set()
                assert release.wait(3)
                return result

            store.begin_turn = delayed
            task = asyncio.create_task(
                service.send_async(conversation.id, "original", "id")
            )
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            release.set()
            with pytest.raises(RequestFailure, match="cancelled"):
                await task
            assert store.get_turn(original.id).status is TurnStatus.PENDING

        asyncio.run(scenario())


@pytest.mark.parametrize("failure_origin", ["transcription", "synthesis"])
@pytest.mark.parametrize("heavy_manager", [False, True])
@pytest.mark.parametrize("gate_type", [CpuWorkerGate, asyncio.Lock])
def test_uncertain_speech_cleanup_blocks_all_shared_admission(
    tmp_path, failure_origin, heavy_manager, gate_type
):
    class SpeechWorker:
        async def transcribe(self, *_args):
            raise WorkerCleanupUnverified("unverified")

        async def synthesize(self, *_args):
            raise WorkerCleanupUnverified("unverified")

    manager = (
        ResourceManager(
            SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace()
        )
        if heavy_manager
        else None
    )
    gate = gate_type()
    ram = SimpleNamespace(available_ram_gib=lambda: 8)
    with SqliteStore(tmp_path) as store:
        speech = SpeechService(SpeechWorker(), ram, manager, gate)
        spoken = SpokenPlaybackService(store, SpeechWorker(), ram, manager, gate)
        runner = JobRunner(store, ConversationService(store, FakeChatProvider()))

        async def scenario():
            job, _ = await runner.submit(
                store.create_conversation("spoken").id, "hello", "id"
            )
            await runner.run_job(job.id)
            answer = store.get_answer(job.turn_id)
            session, _ = await spoken.start(job.conversation_id, job.turn_id, answer.id)
            with pytest.raises(WorkerCleanupUnverified):
                if failure_origin == "transcription":
                    await speech.transcribe(b"audio", "audio/wav")
                else:
                    await spoken.chunk(session, 0)
            with pytest.raises((ResourceUnavailable, SpeechUnavailable)):
                await speech.transcribe(b"audio", "audio/wav")
            with pytest.raises((ResourceUnavailable, SpeechUnavailable)):
                await spoken.chunk(session, 0)
            if manager is not None:
                with pytest.raises(ResourceUnavailable):
                    await manager.acquire(RuntimeProfile("fake", 1, 1, 1, 1))

        asyncio.run(scenario())


@pytest.mark.parametrize(
    "index,uuid,rows,valid",
    [
        (0, "GPU-a", "GPU-b, 8192\n", False),
        (1, "GPU-a", "GPU-a, 8192\n", False),
        (0, "GPU-a", "GPU-b, 8192\nGPU-a, 8192\n", False),
        (1, "GPU-A", "GPU-b, 8192\nGPU-a, 8192\n", True),
    ],
)
def test_known_gpu_mismatch_never_uses_calibrated_fallback(
    monkeypatch, index, uuid, rows, valid
):
    monkeypatch.setattr(
        "jarvis.infrastructure.resource_probe.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=rows),
    )
    probe = NvidiaResourceProbe(uuid, index)
    if valid:
        assert probe.snapshot().available_vram_gib == 8
    else:
        with pytest.raises(ResourceUnavailable):
            probe.snapshot()


def test_gpu_uuid_case_variants_share_os_ownership(tmp_path):
    first = NamedGpuLock("GPU-aBc", tmp_path)
    second = NamedGpuLock("gpu-AbC", tmp_path)
    assert first.name == second.name and first.journal == second.journal
    assert first.name.endswith(hashlib.sha256(b"GPU-abc").hexdigest()[:32])


def test_saved_job_survives_gpu_uuid_case_canonicalization(tmp_path):
    old_identity = "model:GPU-BFA7EEDF-5D52-9CCF-76E2-86B3891AB4D7:0:hash:modelhash"
    new_identity = "model:GPU-bfa7eedf-5d52-9ccf-76e2-86b3891ab4d7:0:hash:modelhash"
    assert same_profile_identity(old_identity, new_identity)
    assert not same_profile_identity(old_identity, new_identity.replace(":0:", ":1:"))
    assert not same_profile_identity(old_identity, new_identity + "changed")
    with SqliteStore(tmp_path) as store:
        job_id = str(uuid4())
        store.begin_turn(
            store.create_conversation("saved").id, "hello", "id", job_id, old_identity
        )
        runner = JobRunner(
            store, ConversationService(store, FakeChatProvider()), new_identity
        )
        assert asyncio.run(runner.run_job(job_id)).status is JobStatus.SUCCEEDED


@pytest.mark.parametrize(
    "text",
    ["Привет, мир · 你好 · 🙂", "я" * 40_000],
    ids=["multilingual", "byte-limit"],
)
def test_real_windows_worker_protocol_is_utf8_and_byte_bounded(tmp_path, text):
    launcher = tmp_path / "protocol.py"
    source_root = str(Path(__file__).parents[1] / "src")
    launcher.write_text(
        "import sys\n"
        f"sys.path.insert(0, {source_root!r})\n"
        "sys.stdout.reconfigure(encoding='cp1252')\n"
        "from jarvis.infrastructure.speech_worker import emit_result\n"
        f"emit_result({{'version': 1, 'text': {text!r}}})\n",
        encoding="utf-8",
    )
    result = asyncio.run(
        run_cpu_worker(
            WindowsProcessSupervisor(),
            Path(sys.executable),
            launcher,
            tmp_path,
            {"version": 1},
            timeout=5,
            threads=1,
        )
    )
    if len(text.encode("utf-8")) > 65_536:
        assert result == {"version": 1, "error": "transcript_limit"}
    else:
        assert result == {"version": 1, "text": text}


@pytest.mark.parametrize("phase", ["headers", "body"])
@pytest.mark.parametrize("cancelled", [False, True])
def test_real_trickle_http_obeys_total_deadline_and_closes_socket(
    monkeypatch, phase, cancelled
):
    monkeypatch.setattr(
        "jarvis.infrastructure.web_fetch._DURATION", 0.2 if not cancelled else 5
    )
    client_socket, server_socket = socket.socketpair()
    cancel = threading.Event()
    server_done = threading.Event()
    started = threading.Event()

    def server():
        try:
            server_socket.recv(4096)
            started.set()
            if phase == "body":
                server_socket.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 30\r\nConnection: close\r\n\r\n"
                )
            for _ in range(30):
                server_socket.sendall(b"H" if phase == "headers" else b"a")
                time.sleep(0.04)
        except OSError:
            pass
        finally:
            server_socket.close()
            server_done.set()

    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("example.com", timeout=2)
    connection.sock = client_socket
    resolver = lambda *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 80))
    ]
    fetcher = PublicPageFetcher(resolver, lambda *_args: connection)
    timer = threading.Timer(0.1, cancel.set)
    if cancelled:
        timer.start()
    beginning = time.monotonic()
    try:
        with pytest.raises(RequestFailure) as failure:
            fetcher.download_cancellable("http://example.com/", cancel)
        assert failure.value.code == ("cancelled" if cancelled else "timeout")
        assert time.monotonic() - beginning < 0.65
        assert started.is_set()
        assert server_done.wait(1)
    finally:
        cancel.set()
        timer.cancel()
        client_socket.close()
        server_socket.close()
        thread.join(1)


def test_unknown_acknowledgment_preserves_uncertainty_without_replay(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("recovery")
        runner = JobRunner(store, ConversationService(store, FakeChatProvider()))
        job, _ = asyncio.run(runner.submit(conversation.id, "time", "id"))
        store.start_job(job.id)
        invocation = store.record_tool_proposal(
            job.turn_id,
            "clock",
            "current_time",
            1,
            "{}",
            "digest",
            "m5-v1",
            "clock",
            "allow",
            1,
        )
        store.record_tool_intent(invocation.id, "m5-v1", "clock")
    with SqliteStore(tmp_path) as store:
        uncertain = store.get_invocation(invocation.id)
        assert uncertain.status is InvocationStatus.OUTCOME_UNKNOWN
        with pytest.raises(StaleTurnError, match="Cancel"):
            store.acknowledge_unknown(invocation.id)
        store.request_job_cancel(job.id)
        store.settle_job_cancel(job.id)
        with pytest.raises(ActiveTurnError):
            store.begin_turn(conversation.id, "next", "next")
        service = ConversationService(store, FakeChatProvider())
        runner = JobRunner(store, service)
        app, token = create_web_app(service, runner, None, 8765)

        async def scenario():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=BASE
            ) as client:
                headers = await authenticate(client, token)
                shown = (await client.get("/api/approvals")).json()["uncertain_actions"]
                assert shown[0]["id"] == invocation.id
                route = f"/api/uncertain-actions/{invocation.id}/acknowledge"
                assert (await client.post(route)).status_code == 403
                response = await client.post(route, headers=headers)
                assert response.status_code == 200
                assert response.json()["status"] == "outcome_unknown"
            new, _ = await runner.submit(
                conversation.id, "independent new request", "next"
            )
            assert new.status is JobStatus.QUEUED
            prior, duplicate = await runner.submit(conversation.id, "time", "id")
            assert prior.id == job.id and duplicate

        asyncio.run(scenario())
        acknowledged = store.get_invocation(invocation.id)
        assert acknowledged.receipt_json is None and acknowledged.result_json is None
        assert acknowledged.status is InvocationStatus.OUTCOME_UNKNOWN
        assert (
            store.acknowledge_unknown(invocation.id).acknowledged_at
            == acknowledged.acknowledged_at
        )
        records, _ = store.get_recent_context(conversation.id, None)
        results = [
            item
            for item in records
            if isinstance(item, ToolContextRecord) and item.role == "tool"
        ]
        assert json.loads(results[0].text)["status"] == "outcome_unknown"
        with pytest.raises(StaleTurnError):
            store.record_tool_intent(invocation.id, "m5-v1", "clock")
    with SqliteStore(tmp_path) as store:
        assert store.get_invocation(invocation.id).acknowledged_at is not None
        assert not store.list_unknown_invocations()


def test_v7_migration_preserves_existing_user_data(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("keep")
        turn, _ = store.begin_turn(conversation.id, "original text", "id")
        store.complete_turn(turn.id, "original reply")
    with closing(sqlite3.connect(tmp_path / "jarvis.db")) as database:
        database.execute("ALTER TABLE tool_invocations DROP COLUMN acknowledged_at")
        database.execute("DELETE FROM schema_migrations WHERE version=8")
        database.commit()
    with SqliteStore(tmp_path) as store:
        assert [message.text for message in store.get_messages(conversation.id)] == [
            "original text",
            "original reply",
        ]
        assert store.get_turn(turn.id).status is TurnStatus.COMPLETE


@pytest.mark.parametrize(
    "name,content,valid",
    [("create_text_file", " " * 5000, True), ("current_time", " " * 5000, False)],
)
def test_provider_and_registry_share_tool_argument_limits(name, content, valid):
    arguments = (
        json.dumps({"path": r"C:\allowed\test.txt", "content": content})
        if name == "create_text_file"
        else "{" + content + "}"
    )
    delta = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ]
                },
                "finish_reason": None,
            }
        ]
    }
    body = (
        "data: "
        + json.dumps(delta)
        + '\ndata: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\ndata: [DONE]\n'
    )
    endpoint = LlamaEndpoint("http://127.0.0.1:8123", "key", SimpleNamespace())
    provider = LlamaChatProvider(
        SimpleNamespace(endpoint=endpoint),
        httpx.MockTransport(lambda _request: httpx.Response(200, text=body)),
    )
    if valid:
        events = list(
            provider.stream_with_tools(
                [], threading.Event(), "", ToolRegistry().schemas()
            )
        )
        proposal = events[0]
        assert isinstance(proposal, StreamToolCall)
        assert ToolRegistry().parse(proposal)[1]["content"] == content
    else:
        with pytest.raises(RequestFailure):
            list(
                provider.stream_with_tools(
                    [], threading.Event(), "", ToolRegistry().schemas()
                )
            )


def test_job_execution_uses_persisted_deadline(tmp_path):
    with SqliteStore(tmp_path) as store:
        runner = JobRunner(store, ConversationService(store, FakeChatProvider()))

        async def scenario():
            job, _ = await runner.submit(
                store.create_conversation("deadline").id, "hello", "id"
            )
            start = store.start_job

            def expired(job_id):
                start(job_id)
                store.run_sync(
                    lambda: store._conn.execute(
                        "UPDATE jobs SET deadline_at=? WHERE id=?",
                        (
                            (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                            job_id,
                        ),
                    )
                )
                return store.get_job(job_id)

            store.start_job = expired
            with pytest.raises(RequestFailure, match="deadline"):
                await runner.run_job(job.id)
            assert store.get_job(job.id).status is JobStatus.FAILED
            assert store.get_job(job.id).error_code == "timeout"
            assert store.get_answer(job.turn_id) is None

        asyncio.run(scenario())


@pytest.mark.parametrize("runtime_type", [LlamaServerRuntime, ComfyRuntime])
def test_model_launch_pins_physical_uuid_instead_of_cuda_index(
    tmp_path, monkeypatch, runtime_type
):
    device = "GPU-bfa7eedf-5d52-9ccf-76e2-86b3891ab4d7"
    profile = SimpleNamespace(
        name="test",
        device_uuid=device,
        device_index=3,
        verify_artifacts=lambda: None,
        executable=tmp_path / "runtime.exe",
        model=tmp_path / "model.gguf",
        python=tmp_path / "python.exe",
        root=tmp_path,
    )
    runtime = runtime_type(profile, tmp_path / "run")
    captured = {}

    def spawn(executable, arguments, cwd, environment, **kwargs):
        captured.update(arguments=arguments, environment=environment)
        return SimpleNamespace(pid=1, creation_time=1)

    async def ready(*_args):
        return None

    monkeypatch.setattr(runtime.supervisor, "spawn", spawn)
    monkeypatch.setattr(runtime, "_wait_ready", ready)
    asyncio.run(
        runtime.start(RuntimeProfile("test", 1, 1, 1, 1), 1, threading.Event(), "job")
    )
    assert captured["environment"]["CUDA_VISIBLE_DEVICES"] == device
    if runtime_type is LlamaServerRuntime:
        index = captured["arguments"].index("--main-gpu")
        assert captured["arguments"][index + 1] == "0"
    else:
        assert "--cuda-device" not in captured["arguments"]


@pytest.mark.parametrize(
    "invalid",
    [
        "GPU-partial",
        "GPU-REPLACE-WITH-YOUR-UUID",
        "GPU-bfa7eedf-5d52-9ccf-76e2-86b3891ab4d7,0",
    ],
)
def test_gpu_profile_rejects_ambiguous_or_partial_device_identifiers(invalid):
    with pytest.raises(ValueError):
        canonical_gpu_uuid(invalid)


def test_git_ignores_private_runtime_data_and_preserves_examples():
    private = [
        ".env",
        ".env.local",
        "model.toml",
        "profiles/private.toml",
        "models/model.gguf",
        "logs/events.jsonl",
        "artifacts/private.png",
        "backups/snapshot.bin",
        "runtimes/python.exe",
        "run/session.json",
    ]
    public = [
        "profiles/qwen3-4b-instruct-2507.example.toml",
        "tests/fixtures/m12/settings.png",
        ".env.example",
    ]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-z", "--stdin"],
        input=("\0".join(private + public) + "\0").encode(),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert set(result.stdout.decode().rstrip("\0").split("\0")) == set(private)


@pytest.mark.parametrize("browser", [False, True])
def test_application_cancellation_interrupts_downloader_before_releasing_task(
    tmp_path, browser
):
    from jarvis.application.browser import BrowserService

    class Downloader:
        def __init__(self):
            self.started = threading.Event()
            self.closed = threading.Event()

        def inspect(self, *_args):
            return "GET http://example.com/"

        def download(self, *_args):
            raise AssertionError("Uncancellable download was used")

        def download_cancellable(self, _url, cancel):
            self.started.set()
            assert cancel.wait(3)
            self.closed.set()
            raise RequestFailure("cancelled", "Download stopped")

        def execute(self, *_args):
            raise AssertionError("Uncancellable execution was used")

        def execute_cancellable(self, _name, arguments, cancel):
            self.download_cancellable(arguments["url"], cancel)

    downloader = Downloader()
    manager = ResourceManager(
        SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace()
    )
    with SqliteStore(tmp_path) as store:
        tools = ToolService(
            store,
            ClockBackend(),
            ToolPolicy(capabilities=frozenset({"browser" if browser else "network"})),
            web_backend=None if browser else downloader,
            browser_service=BrowserService(
                store,
                downloader,
                SimpleNamespace(identity="browser"),
                SimpleNamespace(available_ram_gib=lambda: 8),
                manager,
            )
            if browser
            else None,
        )
        runner = JobRunner(
            store, ConversationService(store, FollowupProvider(), tool_service=tools)
        )

        async def scenario():
            job, _ = await runner.submit(
                store.create_conversation("network").id, "fetch", "id"
            )
            job = store.start_job(job.id)
            invocation = await tools.propose_and_run(
                job.turn_id,
                StreamToolCall(
                    "fetch",
                    "render_public_page" if browser else "fetch_public_page",
                    '{"url":"http://example.com/"}',
                ),
                job.attempt,
            )
            store.mark_job_waiting(job.id)
            approval = await tools.trusted_issue_approval(invocation.id)
            task = runner.launch(job.id, approved=(invocation.id, approval))
            assert await asyncio.to_thread(downloader.started.wait, 2)
            await runner.cancel(job.id)
            await asyncio.wait_for(task, 2)
            assert downloader.closed.is_set()
            assert store.get_job(job.id).status is JobStatus.CANCELLED
            assert not manager._cpu_worker

        asyncio.run(scenario())
