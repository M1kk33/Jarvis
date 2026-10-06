"""Authenticated, bounded llama.cpp chat and supervised server lifecycle."""

import asyncio
import json
import os
import secrets
import socket
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from uuid import uuid4

import httpx

from jarvis.domain.resources import RuntimeIdentity, RuntimeProfile, RuntimeStartFailure
from jarvis.domain.types import (
    Message,
    ProviderFailure,
    StreamDelta,
    StreamEvent,
    StreamTerminal,
)
from jarvis.infrastructure.local_profile import LocalTextProfile
from jarvis.infrastructure.windows_process import (
    OwnedProcess,
    WindowsProcessSupervisor,
    named_job_tree_exited,
)


@dataclass(frozen=True)
class LlamaEndpoint:
    base_url: str
    api_key: str
    identity: RuntimeIdentity


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _messages(messages: Sequence[Message], instructions: str) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    if instructions:
        result.append({"role": "system", "content": instructions})
    result.extend({"role": item.role, "content": item.text} for item in messages)
    return result


def _bounded_lines(response: httpx.Response) -> Iterator[str]:
    """Frame SSE bytes without allowing an unbounded line buffer."""
    pending = bytearray()
    for chunk in response.iter_bytes(chunk_size=4096):
        parts = chunk.split(b"\n")
        for index, part in enumerate(parts):
            pending.extend(part)
            if len(pending) > 65536:
                raise ProviderFailure("backend_crashed", "Oversized stream event")
            if index < len(parts) - 1:
                yield pending.rstrip(b"\r").decode("utf-8")
                pending.clear()
    if pending:
        yield pending.rstrip(b"\r").decode("utf-8")


class LlamaServerRuntime:
    def __init__(self, profile: LocalTextProfile, run_root: Path) -> None:
        self.profile = profile
        self.run_root = run_root.resolve()
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.supervisor = WindowsProcessSupervisor()
        self.processes: dict[int, OwnedProcess] = {}
        self._endpoints: dict[int, LlamaEndpoint] = {}
        self._job_names: dict[int, str] = {}

    @property
    def endpoint(self) -> LlamaEndpoint:
        if len(self._endpoints) != 1:
            raise ProviderFailure("unavailable", "Local model is not ready")
        return next(iter(self._endpoints.values()))

    async def start(
        self, profile: RuntimeProfile, generation: int, cancel: Event, job_name: str
    ) -> RuntimeIdentity:
        if profile.name != self.profile.name:
            raise RuntimeStartFailure("Wrong pinned text profile")
        await asyncio.to_thread(self.profile.verify_artifacts)
        if cancel.is_set():
            raise RuntimeStartFailure("Local model load was cancelled")
        cwd = self.run_root / f"llama-{generation}-{uuid4().hex}"
        cwd.mkdir()
        port = _free_loopback_port()
        api_key = secrets.token_urlsafe(32)
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        environment = {
            "SystemRoot": system_root,
            "WINDIR": system_root,
            "PATH": str(self.profile.executable.parent)
            + os.pathsep
            + str(Path(system_root) / "System32"),
            "TEMP": os.environ.get("TEMP", str(cwd)),
            "TMP": os.environ.get("TMP", str(cwd)),
            "LLAMA_API_KEY": api_key,
        }
        arguments = [
            "--model",
            str(self.profile.model),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--ctx-size",
            "4096",
            "--parallel",
            "1",
            "--batch-size",
            "256",
            "--ubatch-size",
            "128",
            "--cache-ram",
            "0",
            "--cache-type-k",
            "f16",
            "--cache-type-v",
            "f16",
            "--gpu-layers",
            "all",
            "--split-mode",
            "none",
            "--main-gpu",
            str(self.profile.device_index),
            "--fit",
            "off",
            "--threads",
            "4",
            "--no-webui",
            "--no-agent",
            "--cors-origins",
            "localhost",
            "--cors-headers",
            "Content-Type",
            "--no-cors-credentials",
            "--no-context-shift",
            "--jinja",
        ]
        self._job_names[generation] = job_name
        process = await asyncio.to_thread(
            self.supervisor.spawn,
            self.profile.executable,
            arguments,
            cwd,
            environment,
            job_name=job_name,
        )
        self.processes[generation] = process
        identity = RuntimeIdentity(
            generation, self.profile.name, process.pid, process.creation_time
        )
        endpoint = LlamaEndpoint(f"http://127.0.0.1:{port}", api_key, identity)
        try:
            await self._wait_ready(process, endpoint, cancel)
        except Exception as exc:
            raise RuntimeStartFailure(
                "Pinned local model did not become ready"
            ) from exc
        self._endpoints[generation] = endpoint
        return identity

    async def _wait_ready(
        self, process: OwnedProcess, endpoint: LlamaEndpoint, cancel: Event
    ) -> None:
        deadline = time.monotonic() + 180
        headers = {"Authorization": f"Bearer {endpoint.api_key}"}
        async with httpx.AsyncClient(
            base_url=endpoint.base_url, trust_env=False, timeout=2
        ) as client:
            while time.monotonic() < deadline:
                if cancel.is_set():
                    raise RuntimeStartFailure("Local model load was cancelled")
                if process.root_exited():
                    raise RuntimeStartFailure("Local model exited during load")
                try:
                    health = await client.get("/health")
                    if health.status_code == 200:
                        models = await client.get("/v1/models", headers=headers)
                        if models.status_code != 200:
                            raise RuntimeStartFailure("Authenticated readiness failed")
                        props = await client.get("/props", headers=headers)
                        if props.status_code != 200:
                            raise RuntimeStartFailure("Model metadata is unavailable")
                        metadata = props.json()
                        if metadata.get("total_slots") != 1 or not metadata.get(
                            "chat_template"
                        ):
                            raise RuntimeStartFailure(
                                "Template or one-slot validation failed"
                            )
                        warmup = await client.post(
                            "/v1/chat/completions",
                            headers=headers,
                            json={
                                "messages": [{"role": "user", "content": "Hi"}],
                                "max_tokens": 1,
                                "stream": False,
                                "parse_tool_calls": False,
                            },
                            timeout=30,
                        )
                        if warmup.status_code != 200:
                            raise RuntimeStartFailure("Model warmup failed")
                        return
                except (httpx.ConnectError, httpx.TimeoutException):
                    pass
                await asyncio.sleep(0.25)
        raise TimeoutError("Local model readiness exceeded 180 seconds")

    async def stop(self, identity: RuntimeIdentity) -> bool:
        process = self.processes.get(identity.generation)
        if process is None:
            return False
        if (
            process.pid != identity.pid
            or process.creation_time != identity.creation_time
        ):
            return False
        # llama-server has no documented shutdown endpoint; terminate its owned job.
        stopped = await asyncio.to_thread(process.stop, 0, 10)
        if stopped:
            self.processes.pop(identity.generation, None)
            self._endpoints.pop(identity.generation, None)
            self._job_names.pop(identity.generation, None)
        return stopped

    async def abort_start(self, generation: int) -> bool:
        process = self.processes.get(generation)
        if process is None:
            job_name = self._job_names.get(generation)
            if job_name is None:
                return True
            stopped = await asyncio.to_thread(named_job_tree_exited, job_name, 10)
            if stopped:
                self._job_names.pop(generation, None)
            return stopped
        stopped = await asyncio.to_thread(process.stop, 0, 10)
        if stopped:
            self.processes.pop(generation, None)
            self._endpoints.pop(generation, None)
            self._job_names.pop(generation, None)
        return stopped

    async def wait_exit(self, identity: RuntimeIdentity) -> None:
        process = self.processes.get(identity.generation)
        if process is not None:
            await asyncio.to_thread(process.wait_root_exit)

    async def verify_stopped(self, identity: RuntimeIdentity) -> bool:
        process = self.processes.get(identity.generation)
        return process is None or await asyncio.to_thread(process.verify_stopped)


class LlamaChatProvider:
    def __init__(
        self, runtime: LlamaServerRuntime, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.runtime = runtime
        self.transport = transport

    def count_prompt_tokens(
        self, messages: Sequence[Message], instructions: str
    ) -> int:
        endpoint = self.runtime.endpoint
        with httpx.Client(
            base_url=endpoint.base_url,
            trust_env=False,
            timeout=15,
            transport=self.transport,
        ) as client:
            response = client.post(
                "/v1/chat/completions/input_tokens",
                headers={"Authorization": f"Bearer {endpoint.api_key}"},
                json={"messages": _messages(messages, instructions), "max_tokens": 512},
            )
            self._check_status(response)
            value = response.json().get("input_tokens")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ProviderFailure("backend_crashed", "Invalid token-count response")
            return value

    def count_output_tokens(self, text: str) -> int:
        endpoint = self.runtime.endpoint
        with httpx.Client(
            base_url=endpoint.base_url,
            trust_env=False,
            timeout=15,
            transport=self.transport,
        ) as client:
            response = client.post(
                "/tokenize",
                headers={"Authorization": f"Bearer {endpoint.api_key}"},
                json={"content": text, "add_special": False},
            )
            self._check_status(response)
            tokens = response.json().get("tokens")
            if not isinstance(tokens, list) or any(
                isinstance(token, bool) or not isinstance(token, int)
                for token in tokens
            ):
                raise ProviderFailure("backend_crashed", "Invalid output token count")
            return len(tokens)

    async def check_unauthenticated_routes(self) -> None:
        endpoint = self.runtime.endpoint
        async with httpx.AsyncClient(
            base_url=endpoint.base_url, trust_env=False, timeout=5
        ) as client:
            checks = (
                ("GET", "/v1/models", None),
                (
                    "POST",
                    "/v1/chat/completions/input_tokens",
                    {"messages": [{"role": "user", "content": "Hi"}]},
                ),
                (
                    "POST",
                    "/v1/chat/completions",
                    {"messages": [{"role": "user", "content": "Hi"}], "max_tokens": 1},
                ),
                ("POST", "/completion", {"prompt": "Hi", "n_predict": 1}),
                ("POST", "/tokenize", {"content": "Hi"}),
                ("POST", "/slots/0?action=erase", None),
            )
            for method, path, payload in checks:
                response = await client.request(method, path, json=payload)
                if response.status_code not in {401, 404}:
                    raise ProviderFailure(
                        "unavailable",
                        f"Unauthenticated model route is accessible: {path}",
                    )
            cross_origin = await client.options(
                "/v1/chat/completions",
                headers={
                    "Origin": "https://example.invalid",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "authorization",
                },
            )
            if (
                cross_origin.headers.get("access-control-allow-origin")
                == "https://example.invalid"
                or cross_origin.headers.get("access-control-allow-credentials")
                == "true"
            ):
                raise ProviderFailure("unavailable", "Local model CORS is too broad")

    @staticmethod
    def _check_status(response: httpx.Response) -> None:
        if response.status_code == 200:
            return
        if response.status_code == 401:
            raise ProviderFailure("unavailable", "Local model authentication failed")
        if response.status_code in {408, 504}:
            raise ProviderFailure("timeout", "Local model request timed out")
        if response.status_code in {500, 507}:
            excerpt = next(response.iter_bytes(chunk_size=4096), b"")
            if b"out of memory" in excerpt.lower():
                raise ProviderFailure("out_of_memory", "Local model ran out of memory")
        raise ProviderFailure("backend_crashed", "Local model returned an error")

    def stream(
        self, messages: Sequence[Message], cancel: Event, instructions: str = ""
    ) -> Iterator[StreamEvent]:
        endpoint = self.runtime.endpoint
        deadline = time.monotonic() + 120
        headers = {"Authorization": f"Bearer {endpoint.api_key}"}
        try:
            with (
                httpx.Client(
                    base_url=endpoint.base_url,
                    trust_env=False,
                    timeout=httpx.Timeout(connect=3, read=20, write=5, pool=3),
                    transport=self.transport,
                ) as client,
                client.stream(
                    "POST",
                    "/v1/chat/completions",
                    headers=headers,
                    json={
                        "messages": _messages(messages, instructions),
                        "max_tokens": 512,
                        "stream": True,
                        "parse_tool_calls": False,
                    },
                ) as response,
            ):
                self._check_status(response)
                finished = False
                done = False
                for line in _bounded_lines(response):
                    if cancel.is_set():
                        raise ProviderFailure(
                            "cancelled", "Local model request cancelled"
                        )
                    if time.monotonic() >= deadline:
                        raise ProviderFailure(
                            "timeout", "Local model request deadline exceeded"
                        )
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data: "):
                        raise ProviderFailure(
                            "backend_crashed", "Malformed stream event"
                        )
                    data = line[6:]
                    if data == "[DONE]":
                        done = True
                        break
                    if finished:
                        raise ProviderFailure(
                            "backend_crashed", "Stream continued after completion"
                        )
                    event = json.loads(data)
                    if not isinstance(event, dict):
                        raise ProviderFailure(
                            "backend_crashed", "Invalid stream object"
                        )
                    choices = event.get("choices")
                    if not isinstance(choices, list) or len(choices) != 1:
                        raise ProviderFailure(
                            "backend_crashed", "Invalid stream choice"
                        )
                    choice = choices[0]
                    if not isinstance(choice, dict):
                        raise ProviderFailure(
                            "backend_crashed", "Invalid stream choice"
                        )
                    delta = choice.get("delta")
                    if not isinstance(delta, dict) or any(
                        key in delta for key in ("tool_calls", "function_call")
                    ):
                        raise ProviderFailure(
                            "invalid_request", "Tool calls are unavailable in M4"
                        )
                    content = delta.get("content")
                    if content is not None:
                        if not isinstance(content, str):
                            raise ProviderFailure(
                                "backend_crashed", "Invalid stream content"
                            )
                        if content:
                            yield StreamDelta(content)
                    reason = choice.get("finish_reason")
                    if reason is not None:
                        if reason != "stop" or finished:
                            raise ProviderFailure(
                                "backend_crashed", "Incomplete model reply"
                            )
                        finished = True
                if not done or not finished:
                    raise ProviderFailure(
                        "backend_crashed", "Stream ended before completion"
                    )
                yield StreamTerminal()
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise ProviderFailure("timeout", "Local model stream timed out") from exc
        except (httpx.TransportError, json.JSONDecodeError, ValueError) as exc:
            raise ProviderFailure(
                "backend_crashed", "Local model stream failed"
            ) from exc
