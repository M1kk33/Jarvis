"""Offline M4 profile, transport, template-budget, and lifecycle contracts."""

import asyncio
import hashlib
import json
import time
from pathlib import Path
from threading import Event

import httpx
import pytest

from jarvis.application.context import ContextBuilder
from jarvis.application.conversations import ConversationService
from jarvis.domain.resources import RuntimeIdentity
from jarvis.domain.types import (
    ProviderFailure,
    RequestFailure,
    StreamDelta,
    StreamTerminal,
)
from jarvis.infrastructure.local_profile import LocalTextProfile
from jarvis.infrastructure.providers.fake import FakeChatProvider
from jarvis.infrastructure.providers.llama_cpp import (
    LlamaChatProvider,
    LlamaEndpoint,
    LlamaServerRuntime,
)
from jarvis.infrastructure.sqlite_store import SqliteStore

DEVICE = "GPU-bfa7eedf-5d52-9ccf-76e2-86b3891ab4d7"


def _profile(tmp_path: Path) -> LocalTextProfile:
    binary = tmp_path / "llama-server.exe"
    model = tmp_path / "qwen.gguf"
    binary.write_bytes(b"pinned executable")
    model.write_bytes(b"pinned model")
    values = {
        "name": "qwen3-4b-instruct-2507-q4_k_m",
        "runtime_version": "b11429",
        "executable": binary,
        "executable_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "model": model,
        "model_sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
        "device_uuid": DEVICE,
        "cold_vram_gib": 5.0,
        "cold_ram_gib": 6.0,
        "warm_vram_headroom_gib": 0.5,
        "warm_ram_headroom_gib": 0.5,
    }
    return LocalTextProfile.model_validate(values)


def test_profile_is_strict_and_fails_closed_on_artifact_change(tmp_path):
    profile = _profile(tmp_path)
    profile.verify_artifacts()
    with pytest.raises(ValueError):
        LocalTextProfile.model_validate(
            {**profile.model_dump(), "runtime_version": "unreviewed"}
        )
    with pytest.raises(ValueError):
        LocalTextProfile.model_validate({**profile.model_dump(), "extra": True})
    profile.model.write_bytes(b"changed model")
    with pytest.raises(ValueError, match="hash mismatch"):
        profile.verify_artifacts()


def _recorded_provider(lines: list[str], token_count: int = 9):
    identity = RuntimeIdentity(1, "qwen3-4b-instruct-2507-q4_k_m", 1234, 42)
    endpoint = LlamaEndpoint("http://127.0.0.1:8123", "private-test-key", identity)

    class Runtime:
        @property
        def endpoint(self):
            return endpoint

    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.headers.get("Authorization") != "Bearer private-test-key":
            return httpx.Response(401, json={"error": "unauthorized"})
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(200, json={"input_tokens": token_count})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": [1, 2, 3]})
        return httpx.Response(200, text="\n".join(lines) + "\n")

    transport = httpx.MockTransport(handler)
    provider = LlamaChatProvider(Runtime(), transport)  # type: ignore[arg-type]
    return provider, transport, requests


def _success_lines(content: str = "ready") -> list[str]:
    return [
        'data: {"choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}',
        "data: "
        + json.dumps(
            {"choices": [{"delta": {"content": content}, "finish_reason": None}]}
        ),
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]


def test_recorded_chat_contract_requires_auth_and_stream_terminal(tmp_path):
    provider, transport, requests = _recorded_provider(_success_lines())
    with httpx.Client(transport=transport) as direct:
        assert (
            direct.post("http://127.0.0.1:8123/v1/chat/completions").status_code == 401
        )
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("M4 HTTP fixture")
        turn, _ = store.begin_turn(conversation.id, "hello", "one")
        messages = store.get_messages(conversation.id)
        assert provider.count_prompt_tokens(messages, "instructions") == 9
        assert provider.count_output_tokens("ready") == 3
        events = list(provider.stream(messages, Event(), "instructions"))
        assert events == [StreamDelta("ready"), StreamTerminal()]
        body = json.loads(requests[-1].content)
        assert body["messages"][0] == {
            "role": "system",
            "content": "instructions",
        }
        assert body["messages"][1] == {"role": "user", "content": "hello"}
        assert body["parse_tool_calls"] is False
        assert body["max_tokens"] == 512
        assert turn.id == messages[0].turn_id


@pytest.mark.parametrize(
    "lines",
    [
        ['data: {"choices":[{"delta":{"tool_calls":[{}]},"finish_reason":null}]}'],
        ['data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}'],
        ['data: {"choices":[{"delta":{},"finish_reason":"length"}]}', "data: [DONE]"],
        ["data: {invalid", "data: [DONE]"],
        ["data: " + "x" * 65_537],
        ['data: {"choices":[0]}', "data: [DONE]"],
        ["data: []", "data: [DONE]"],
        [
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            'data: {"choices":[{"delta":{"content":"late"},"finish_reason":null}]}',
            "data: [DONE]",
        ],
    ],
)
def test_bad_tool_and_stream_payloads_never_become_a_reply(tmp_path, lines):
    provider, _, _ = _recorded_provider(lines)
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Malformed fixture")
        store.begin_turn(conversation.id, "hello", "one")
        with pytest.raises(ProviderFailure):
            list(provider.stream(store.get_messages(conversation.id), Event()))


def test_exact_template_count_trims_complete_older_turns(tmp_path):
    class Handle:
        async def release(self):
            pass

    class Manager:
        async def acquire(self, _profile):
            return Handle()

        async def abort_request(self, _handle):
            pass

    class CountingProvider:
        def __init__(self):
            self.requests = []

        def count_prompt_tokens(self, messages, _instructions):
            return 4000 if len(messages) > 1 else 12

        def stream(self, messages, _cancel, _instructions=""):
            self.requests.append(tuple(m.text for m in messages))
            yield StreamDelta("answer")
            yield StreamTerminal()

    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Exact context")
        ConversationService(store, FakeChatProvider()).send(
            conversation.id, "old input", "old"
        )
        provider = CountingProvider()
        app = ConversationService(
            store,
            provider,
            context=ContextBuilder(),
            resource_manager=Manager(),  # type: ignore[arg-type]
            runtime_profile=_profile(tmp_path).admission_profile(),
        )
        app.send(conversation.id, "new input", "new")
        assert provider.requests == [("new input",)]


def test_cancelled_stream_waits_for_reader_before_runtime_cleanup(tmp_path):
    started = Event()
    finished = Event()
    order = []

    class Handle:
        async def release(self):
            order.append("release")

    class Manager:
        async def acquire(self, _profile):
            return Handle()

        async def abort_request(self, _handle):
            assert finished.is_set()
            order.append("abort")

    class BlockingProvider:
        def stream(self, _messages, cancel, _instructions=""):
            started.set()
            while not cancel.is_set():
                time.sleep(0.01)
            finished.set()
            raise ProviderFailure("cancelled", "cancelled")
            yield  # pragma: no cover

    async def scenario():
        with SqliteStore(tmp_path) as store:
            conversation = store.create_conversation("Cancel reader")
            app = ConversationService(
                store,
                BlockingProvider(),
                resource_manager=Manager(),  # type: ignore[arg-type]
                runtime_profile=_profile(tmp_path).admission_profile(),
            )
            task = asyncio.create_task(app.send_async(conversation.id, "hello", "one"))
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            with pytest.raises(RequestFailure) as failure:
                await task
            assert failure.value.code == "cancelled"
            assert order == ["abort", "release"]

    asyncio.run(scenario())


def test_restart_reconstructs_complete_history_for_real_provider_contract(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Restart history")
        ConversationService(store, FakeChatProvider()).send(
            conversation.id, "alpha", "one"
        )
        ConversationService(store, FakeChatProvider()).send(
            conversation.id, "beta", "two"
        )

    class Handle:
        async def release(self):
            pass

    class Manager:
        async def acquire(self, _profile):
            return Handle()

        async def abort_request(self, _handle):
            pass

    class RecordingProvider:
        def __init__(self):
            self.seen = []

        def count_prompt_tokens(self, messages, _instructions):
            return 40

        def stream(self, messages, _cancel, _instructions=""):
            self.seen = [(message.role, message.text) for message in messages]
            yield StreamDelta("ready")
            yield StreamTerminal()

    provider = RecordingProvider()
    with SqliteStore(tmp_path) as store:
        app = ConversationService(
            store,
            provider,
            resource_manager=Manager(),  # type: ignore[arg-type]
            runtime_profile=_profile(tmp_path).admission_profile(),
        )
        result = app.send(conversation.id, "gamma", "three")
        assert result.answer is not None and result.answer.text == "ready"
    assert [
        text for _, text in provider.seen if text in {"alpha", "beta", "gamma"}
    ] == ["alpha", "beta", "gamma"]


def test_runtime_passes_one_slot_auth_and_memory_flags_to_supervisor(tmp_path):
    profile = _profile(tmp_path)
    runtime = LlamaServerRuntime(profile, tmp_path / "run")

    class Process:
        pid = 404
        creation_time = 55

        def stop(self, _graceful, _kill):
            return True

    captured = {}

    class Supervisor:
        def spawn(self, executable, arguments, cwd, environment, *, job_name):
            captured.update(
                executable=executable,
                arguments=arguments,
                cwd=cwd,
                environment=environment,
                job_name=job_name,
            )
            return Process()

    runtime.supervisor = Supervisor()  # type: ignore[assignment]

    async def ready(_process, _endpoint, _cancel):
        pass

    runtime._wait_ready = ready  # type: ignore[method-assign]

    async def scenario():
        identity = await runtime.start(profile.admission_profile(), 1, Event(), "job")
        assert identity.pid == 404
        args = captured["arguments"]
        for flag, value in (
            ("--ctx-size", "4096"),
            ("--parallel", "1"),
            ("--cache-ram", "0"),
            ("--gpu-layers", "all"),
            ("--fit", "off"),
            ("--no-webui", None),
            ("--no-agent", None),
            ("--cors-origins", "localhost"),
            ("--cors-headers", "Content-Type"),
            ("--no-cors-credentials", None),
        ):
            assert flag in args
            if value is not None:
                assert args[args.index(flag) + 1] == value
        assert captured["job_name"] == "job"
        assert "LLAMA_API_KEY" in captured["environment"]
        assert captured["environment"]["LLAMA_API_KEY"] not in " ".join(args)
        assert runtime.endpoint.base_url.startswith("http://127.0.0.1:")
        assert await runtime.stop(identity)

    asyncio.run(scenario())


def test_startup_without_process_handle_keeps_ownership_until_job_exit(
    tmp_path, monkeypatch
):
    profile = _profile(tmp_path)
    runtime = LlamaServerRuntime(profile, tmp_path / "run")
    runtime._job_names[1] = "Global\\JarvisJob_test"
    monkeypatch.setattr(
        "jarvis.infrastructure.providers.llama_cpp.named_job_tree_exited",
        lambda _name, _timeout: False,
    )
    assert not asyncio.run(runtime.abort_start(1))
    assert 1 in runtime._job_names
