"""M8 fixed workflow, image result, recovery, and worker handoff contracts."""

import asyncio
import json
import os
import struct
import zlib
from pathlib import Path
from threading import Event
from uuid import uuid4

import httpx
import pytest

from jarvis.application.conversations import ConversationService
from jarvis.application.images import ImageService, _read_bounded_output, validate_png
from jarvis.application.jobs import JobRunner
from jarvis.application.resource_manager import ResourceManager, RuntimeRouter
from jarvis.application.tools import ToolPolicy, ToolRegistry, ToolService
from jarvis.domain.resources import AdmissionDenied, ResourceSnapshot, RuntimeProfile
from jarvis.domain.tools import ToolContextRecord
from jarvis.domain.types import (
    RequestFailure,
    StaleTurnError,
    StreamDelta,
    StreamTerminal,
    StreamToolCall,
)
from jarvis.infrastructure.backup import create_backup, restore_backup
from jarvis.infrastructure.fake_runtime import FakeProcessRuntime
from jarvis.infrastructure.gpu_lock import NamedGpuLock
from jarvis.infrastructure.image_launcher import authorized
from jarvis.infrastructure.providers.comfyui import (
    ComfyEndpoint,
    ComfyImageProvider,
    fixed_workflow,
)
from jarvis.infrastructure.read_tools import SafeReadTools
from jarvis.infrastructure.resource_probe import SystemClock
from jarvis.infrastructure.sqlite_store import SqliteStore
from jarvis.interfaces.web import create_web_app


def _chunk(kind: bytes, content: bytes) -> bytes:
    return (
        struct.pack(">I", len(content))
        + kind
        + content
        + struct.pack(">I", zlib.crc32(kind + content))
    )


def _png() -> bytes:
    header = struct.pack(">IIBBBBB", 512, 512, 8, 2, 0, 0, 0)
    pixels = (b"\0" + b"\x11\x22\x33" * 512) * 512
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(pixels))
        + _chunk(b"IEND", b"")
    )


class Probe:
    def snapshot(self):
        return ResourceSnapshot(8, 12)


class FakeImage:
    def __init__(self, root: Path):
        self.output_root = root
        root.mkdir()
        self.calls = 0
        self.source = root / "output.png"
        self.source.write_bytes(_png())

    async def generate(
        self, prompt: str, seed: int, invocation_id: str, cancel: Event
    ) -> Path:
        assert prompt == "a copper robot"
        assert seed == 17
        assert not cancel.is_set()
        self.calls += 1
        return self.source


class FakeHandle:
    async def release(self):
        return None


class FakeManager:
    def __init__(self):
        self.aborted = 0
        self.unloaded = 0

    async def acquire(self, profile):
        return FakeHandle()

    async def abort_request(self, handle):
        self.aborted += 1

    async def unload(self):
        self.unloaded += 1

    async def verify_previous_worker(self):
        return True


class ScriptedChat:
    def __init__(self):
        self.contexts = []

    def count_prompt_tokens_with_tools(self, messages, instructions, tools):
        return 100

    def stream_with_tools(self, messages, cancel, instructions, tools):
        self.contexts.append(tuple(messages))
        result = next(
            (
                item
                for item in messages
                if isinstance(item, ToolContextRecord) and item.role == "tool"
            ),
            None,
        )
        if result is None:
            yield StreamToolCall(
                "image-call", "generate_image", '{"prompt":"a copper robot","seed":17}'
            )
        else:
            artifact = json.loads(result.content_json)["data"]["artifact_id"]
            yield StreamDelta(f"Created image {artifact}.")
        yield StreamTerminal()


def test_fixed_workflow_and_arguments_are_bounded():
    graph = fixed_workflow(
        "a copper robot",
        17,
        "v1-5-pruned-emaonly-fp16.safetensors",
        "jarvis_" + "a" * 32,
    )
    assert graph["4"]["inputs"] == {"width": 512, "height": 512, "batch_size": 1}
    assert graph["5"]["inputs"]["steps"] == 25
    assert graph["5"]["inputs"]["seed"] == 17
    assert graph["7"]["inputs"]["filename_prefix"] == "jarvis_" + "a" * 32
    for changes in (("", 17), ("x" * 1001, 17), ("ok", -1), ("ok", True)):
        with pytest.raises(ValueError):
            fixed_workflow(
                changes[0],
                changes[1],
                "v1-5-pruned-emaonly-fp16.safetensors",
                "jarvis_" + "a" * 32,
            )
    with pytest.raises(ValueError):
        fixed_workflow("ok", 0, "other.safetensors", "jarvis_" + "a" * 32)
    registry = ToolRegistry()
    for payload in (
        '{"prompt":""}',
        '{"prompt":"ok","width":1024}',
        '{"prompt":"ok","seed":true}',
    ):
        with pytest.raises(RequestFailure):
            registry.parse(StreamToolCall("call", "generate_image", payload))


def test_png_and_output_path_validation(tmp_path):
    root = tmp_path / "output"
    root.mkdir()
    good = root / "good.png"
    good.write_bytes(_png())
    assert _read_bounded_output(root, good) == good.read_bytes()
    with pytest.raises(RequestFailure):
        _read_bounded_output(root, tmp_path / "outside.png")
    bad = good.read_bytes()[:-4] + b"bad!"
    with pytest.raises(RequestFailure):
        validate_png(bad)
    corrupt = good.read_bytes().replace(b"IDAT", b"IHDR", 1)
    with pytest.raises(RequestFailure):
        validate_png(corrupt)
    link = root / "link.png"
    try:
        os.symlink(good, link)
    except OSError:
        pass
    else:
        with pytest.raises(RequestFailure):
            _read_bounded_output(root, link)


def test_launcher_guard_rejects_direct_http_websocket_and_browser_origin():
    secret = "secret-0123456789abcdefghijklmnop"
    assert authorized({"Authorization": f"Bearer {secret}"}, secret)
    for headers in (
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"Bearer {secret}", "Origin": "https://evil.test"},
        {"Authorization": f"Bearer {secret}", "Sec-Fetch-Site": "cross-site"},
        {"Upgrade": "websocket"},
    ):
        assert not authorized(headers, secret)


def test_fake_comfy_protocol_accepts_only_one_fixed_output(tmp_path):
    class Runtime:
        profile = type(
            "Profile", (), {"checkpoint": Path("v1-5-pruned-emaonly-fp16.safetensors")}
        )()
        endpoint = ComfyEndpoint("http://127.0.0.1:8188", "key", tmp_path, None)

    requests = []
    state = {"prefix": ""}
    prompt_id = str(uuid4())

    def response(request):
        requests.append(request)
        if request.url.path == "/prompt":
            assert request.headers["authorization"] == "Bearer key"
            graph = json.loads(request.content)["prompt"]
            assert graph["4"]["inputs"]["width"] == 512
            state["prefix"] = graph["7"]["inputs"]["filename_prefix"]
            return httpx.Response(200, json={"prompt_id": prompt_id})
        return httpx.Response(
            200,
            json={
                prompt_id: {
                    "outputs": {
                        "7": {
                            "images": [
                                {
                                    "filename": state["prefix"] + "_00001_.png",
                                    "subfolder": "",
                                    "type": "output",
                                }
                            ]
                        }
                    }
                }
            },
        )

    async def scenario():
        provider = ComfyImageProvider(Runtime(), httpx.MockTransport(response))
        source = await provider.generate("x", 1, "invocation", Event())
        assert source.parent == tmp_path
        assert source.name == state["prefix"] + "_00001_.png"
        assert len(requests) >= 2

    asyncio.run(scenario())


def test_fake_comfy_rejects_history_path_escape(tmp_path):
    class Runtime:
        profile = type(
            "Profile", (), {"checkpoint": Path("v1-5-pruned-emaonly-fp16.safetensors")}
        )()
        endpoint = ComfyEndpoint("http://127.0.0.1:8188", "key", tmp_path, None)

    prompt_id = str(uuid4())

    def response(request):
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": prompt_id})
        return httpx.Response(
            200,
            json={
                prompt_id: {
                    "outputs": {
                        "7": {
                            "images": [
                                {
                                    "filename": "../escape.png",
                                    "subfolder": "..",
                                    "type": "output",
                                }
                            ]
                        }
                    }
                }
            },
        )

    async def scenario():
        provider = ComfyImageProvider(Runtime(), httpx.MockTransport(response))
        with pytest.raises(RequestFailure):
            await provider.generate("x", 1, "invocation", Event())

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure", ["flush", "rename", "commit", "missing", "corrupt", "escape"]
)
def test_image_failures_keep_unknown_intent_and_no_artifact(
    tmp_path, monkeypatch, failure
):
    data = tmp_path / "data"
    image = FakeImage(tmp_path / "output")
    if failure == "missing":
        image.source.unlink()
    elif failure == "corrupt":
        image.source.write_bytes(b"not a PNG")
    elif failure == "escape":
        image.source = tmp_path / "outside.png"
        image.source.write_bytes(_png())
    manager = FakeManager()
    with SqliteStore(data) as store:
        conversation = store.create_conversation("Failure")
        job_id = str(uuid4())
        turn, _ = store.begin_turn(
            conversation.id, "Make image", "once", job_id, "fake"
        )
        job = store.start_job(job_id)
        image_service = ImageService(
            store, image, manager, RuntimeProfile("image-fake", 4, 5, 0.2, 0.2)
        )
        tools = ToolService(
            store,
            SafeReadTools([], data),
            ToolPolicy(capabilities=frozenset({"image"})),
            image_service=image_service,
        )
        if failure == "flush":
            monkeypatch.setattr(
                "jarvis.application.images.os.fsync",
                lambda _fd: (_ for _ in ()).throw(OSError("disk full")),
            )
        elif failure == "rename":
            monkeypatch.setattr(
                "jarvis.application.images.os.replace",
                lambda *_args: (_ for _ in ()).throw(OSError("rename failed")),
            )
        elif failure == "commit":
            monkeypatch.setattr(
                store,
                "commit_image_result",
                lambda *_args: (_ for _ in ()).throw(OSError("DB commit failed")),
            )

        async def scenario():
            with pytest.raises((OSError, RequestFailure, FileNotFoundError)):
                await tools.propose_and_run(
                    turn.id,
                    StreamToolCall(
                        "image-call",
                        "generate_image",
                        '{"prompt":"a copper robot","seed":17}',
                    ),
                    job.attempt,
                )

        asyncio.run(scenario())
        assert manager.aborted == 1 and manager.unloaded == 1
        invocation = store.get_invocations(turn.id)[0]
        assert invocation.status.value == "outcome_unknown"
        assert store.list_artifacts_for_conversation(conversation.id) == []
        if failure == "commit":
            assert len(list((data / "artifacts" / job_id).glob("*.png"))) == 1
        if failure in {"flush", "rename"}:
            assert len(list((data / "staging" / job_id).glob("*.part"))) == 1
    with SqliteStore(data) as reopened:
        assert reopened.get_invocations(turn.id)[0].status.value == "outcome_unknown"
        assert reopened.list_artifacts_for_conversation(conversation.id) == []


def test_image_admission_failure_commits_known_no_effect_result(tmp_path):
    data = tmp_path / "data"

    class NoMemory(FakeManager):
        async def acquire(self, profile):
            raise AdmissionDenied("Not enough RAM")

    manager = NoMemory()
    with SqliteStore(data) as store:
        conversation = store.create_conversation("No memory")
        job_id = str(uuid4())
        turn, _ = store.begin_turn(
            conversation.id, "Make image", "once", job_id, "fake"
        )
        job = store.start_job(job_id)
        image_service = ImageService(
            store,
            FakeImage(tmp_path / "output"),
            manager,
            RuntimeProfile("image-fake", 4, 5, 0.2, 0.2),
        )
        tools = ToolService(
            store,
            SafeReadTools([], data),
            ToolPolicy(capabilities=frozenset({"image"})),
            image_service=image_service,
        )

        async def scenario():
            return await tools.propose_and_run(
                turn.id,
                StreamToolCall(
                    "image-call",
                    "generate_image",
                    '{"prompt":"a copper robot","seed":17}',
                ),
                job.attempt,
            )

        invocation = asyncio.run(scenario())
        assert invocation.status.value == "complete"
        assert json.loads(invocation.result_json) == {
            "status": "error",
            "code": "out_of_memory",
        }
        assert store.list_artifacts_for_conversation(conversation.id) == []
        assert manager.aborted == 0 and manager.unloaded == 0


def test_cancelling_image_stops_worker_and_keeps_unknown_outcome(tmp_path):
    data = tmp_path / "data"
    manager = FakeManager()
    started = asyncio.Event()

    class BlockingImage(FakeImage):
        async def generate(self, prompt, seed, invocation_id, cancel):
            started.set()
            await asyncio.sleep(60)
            return self.source

    with SqliteStore(data) as store:
        conversation = store.create_conversation("Cancel image")
        image_service = ImageService(
            store,
            BlockingImage(tmp_path / "output"),
            manager,
            RuntimeProfile("image-fake", 4, 5, 0.2, 0.2),
        )
        tools = ToolService(
            store,
            SafeReadTools([], data),
            ToolPolicy(capabilities=frozenset({"image"})),
            image_service=image_service,
        )
        service = ConversationService(
            store,
            ScriptedChat(),
            resource_manager=manager,
            runtime_profile=RuntimeProfile("text-fake", 2, 3, 0.2, 0.2),
            tool_service=tools,
        )
        runner = JobRunner(store, service)

        async def scenario():
            job, _ = await runner.submit(conversation.id, "Make an image", "one")
            task = runner.launch(job.id)
            await asyncio.wait_for(started.wait(), 5)
            await runner.cancel(job.id)
            settled = await asyncio.wait_for(task, 5)
            assert settled.status.value == "cancelled"
            assert manager.aborted == 1
            assert (
                store.get_invocations(job.turn_id)[0].status.value == "outcome_unknown"
            )
            assert store.list_artifacts_for_conversation(conversation.id) == []

        asyncio.run(scenario())


def test_image_tool_handoff_persistence_backup_and_browser_preview(tmp_path):
    data = tmp_path / "data"
    output = tmp_path / "image-output"
    image = FakeImage(output)
    provider = ScriptedChat()
    process = FakeProcessRuntime(tmp_path / "workers")
    text_profile = RuntimeProfile(
        "text-fake", 2, 3, 0.2, 0.2, calibrated=True, idle_seconds=120
    )
    image_profile = RuntimeProfile(
        "image-fake", 4, 5, 0.2, 0.2, calibrated=True, idle_seconds=0
    )
    router = RuntimeRouter({"text-fake": process, "image-fake": process})

    with SqliteStore(data) as store:
        conversation = store.create_conversation("Images")
        manager = ResourceManager(
            router, Probe(), NamedGpuLock("GPU-" + str(uuid4())), SystemClock()
        )
        image_service = ImageService(store, image, manager, image_profile)
        backend = SafeReadTools([], data)
        tools = ToolService(
            store,
            backend,
            ToolPolicy(capabilities=frozenset({"image"})),
            image_service=image_service,
        )
        service = ConversationService(
            store,
            provider,
            resource_manager=manager,
            runtime_profile=text_profile,
            tool_service=tools,
        )
        runner = JobRunner(store, service)

        async def scenario():
            job, duplicate = await runner.submit(
                conversation.id, "Make an image", "stable-image"
            )
            assert not duplicate
            done = await runner.run_job(job.id)
            assert done.status.value == "succeeded"
            assert image.calls == 1
            invocations = store.get_invocations(job.turn_id)
            assert len(invocations) == 1 and invocations[0].status.value == "complete"
            artifacts = store.list_artifacts_for_conversation(conversation.id)
            assert len(artifacts) == 1 and artifacts[0]["status"] == "available"
            artifact_id = artifacts[0]["id"]
            assert store.get_artifact_bytes(artifact_id) == _png()
            assert artifact_id in store.get_answer(job.turn_id).text
            again, duplicate = await runner.submit(
                conversation.id, "Make an image", "stable-image"
            )
            assert duplicate and again.id == job.id and image.calls == 1
            app, token = create_web_app(service, runner, tools, 8765)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
            ) as client:
                assert (
                    await client.get(f"/api/artifacts/{artifact_id}/preview")
                ).status_code == 401
                assert (
                    await client.post(
                        "/api/setup",
                        json={"token": token},
                        headers={"origin": "http://127.0.0.1:8765"},
                    )
                ).status_code == 200
                preview = await client.get(f"/api/artifacts/{artifact_id}/preview")
                assert (
                    preview.status_code == 200
                    and preview.headers["content-type"] == "image/png"
                )
                assert preview.content == _png()
            await manager.close()
            return job, artifact_id

        job, artifact_id = asyncio.run(scenario())
        events = [entry[0] for entry in process.intervals]
        assert events[:4] == ["spawn", "exit", "spawn", "exit"]
        assert image.calls == 1
        backup = create_backup(store)
    restored = tmp_path / "restored"
    restore_backup(backup, restored)
    with SqliteStore(restored) as reopened:
        assert reopened.get_artifact_bytes(artifact_id) == _png()
        assert reopened.get_job(job.id).status.value == "succeeded"
    (restored / "artifacts" / job.id / f"{artifact_id}.png").write_bytes(b"corrupt")
    with SqliteStore(restored) as reopened:
        assert (
            reopened.list_artifacts_for_conversation(conversation.id)[0]["status"]
            == "unavailable"
        )
        with pytest.raises(StaleTurnError):
            reopened.get_artifact_bytes(artifact_id)
