"""Authenticated, bounded llama.cpp chat and supervised server lifecycle."""

import asyncio
import base64
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
from jarvis.domain.tools import ContextRecord, ToolContextRecord, tool_argument_limit
from jarvis.domain.types import (
    Message,
    ProviderFailure,
    StreamDelta,
    StreamEvent,
    StreamTerminal,
    StreamToolCall,
)
from jarvis.infrastructure.local_profile import LocalTextProfile
from jarvis.infrastructure.vision_profile import LocalVisionProfile
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


def _messages(
    messages: Sequence[ContextRecord], instructions: str
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    if instructions:
        result.append({"role": "system", "content": instructions})
    for item in messages:
        if isinstance(item, ToolContextRecord):
            if item.role == "assistant_tool_call":
                result.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": item.model_call_id,
                                "type": "function",
                                "function": {
                                    "name": item.name,
                                    "arguments": item.content_json,
                                },
                            }
                        ],
                    }
                )
            else:
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": item.model_call_id,
                        "content": item.content_json,
                    }
                )
        else:
            result.append({"role": item.role, "content": item.text})
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
    # This is the configured runtime window, not the model's advertised maximum.
    # Admission profiles are calibrated for this value.
    context_tokens = 4096

    def __init__(
        self, profile: LocalTextProfile | LocalVisionProfile, run_root: Path
    ) -> None:
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
            raise RuntimeStartFailure("Wrong pinned llama.cpp profile")
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
            "CUDA_VISIBLE_DEVICES": self.profile.device_uuid,
        }
        arguments = [
            "--model",
            str(self.profile.model),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--ctx-size",
            str(self.context_tokens),
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
            "0",
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
        if isinstance(self.profile, LocalVisionProfile):
            arguments.extend(
                [
                    "--mmproj",
                    str(self.profile.projector),
                    "--image-max-tokens",
                    str(self.profile.image_max_tokens),
                    "--image-min-tokens",
                    "256",
                ]
            )
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
        self,
        runtime: LlamaServerRuntime,
        transport: httpx.BaseTransport | None = None,
        *,
        max_output_tokens: int = 512,
        request_seconds: float = 120,
    ) -> None:
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 2048:
            raise ValueError("Output token budget must be between 1 and 2048")
        if not 0 < request_seconds <= 300:
            raise ValueError("Request deadline must be between 0 and 300 seconds")
        self.runtime = runtime
        self.transport = transport
        self.max_output_tokens = max_output_tokens
        self.request_seconds = request_seconds

    def count_prompt_tokens(
        self, messages: Sequence[Message], instructions: str
    ) -> int:
        return self._count_tokens(messages, instructions, None)

    def count_prompt_tokens_with_tools(
        self,
        messages: Sequence[ContextRecord],
        instructions: str,
        tools: list[dict[str, object]],
    ) -> int:
        return self._count_tokens(messages, instructions, tools)

    def _count_tokens(
        self,
        messages: Sequence[ContextRecord],
        instructions: str,
        tools: list[dict[str, object]] | None,
    ) -> int:
        endpoint = self.runtime.endpoint
        with httpx.Client(
            base_url=endpoint.base_url,
            trust_env=False,
            timeout=15,
            transport=self.transport,
        ) as client:
            payload: dict[str, object] = {
                "messages": _messages(messages, instructions),
                "max_tokens": self.max_output_tokens,
            }
            if tools is not None:
                payload["tools"] = tools
                payload["parallel_tool_calls"] = False
            response = client.post(
                "/v1/chat/completions/input_tokens",
                headers={"Authorization": f"Bearer {endpoint.api_key}"},
                json=payload,
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
        return self._stream(messages, cancel, instructions, None)

    def stream_bounded(
        self,
        messages: Sequence[Message],
        cancel: Event,
        instructions: str,
        output_tokens: int,
        choices: tuple[str, ...] = (),
        response_schema: dict[str, object] | None = None,
    ) -> Iterator[StreamEvent]:
        if (
            type(output_tokens) is not int
            or not 1 <= output_tokens <= self.max_output_tokens
        ):
            raise ValueError("Invalid analysis output allowance")
        return self._stream(
            messages,
            cancel,
            instructions,
            None,
            output_tokens=output_tokens,
            selection_ids=choices,
            response_schema=response_schema,
        )

    @staticmethod
    def _vision_messages(
        question: str, image: bytes, instructions: str
    ) -> list[dict[str, object]]:
        return [
            {"role": "system", "content": instructions},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,"
                            + base64.b64encode(image).decode("ascii")
                        },
                    },
                ],
            },
        ]

    def count_vision_tokens(
        self, question: str, image: bytes, instructions: str
    ) -> int:
        endpoint = self.runtime.endpoint
        with httpx.Client(
            base_url=endpoint.base_url,
            trust_env=False,
            timeout=30,
            transport=self.transport,
        ) as client:
            response = client.post(
                "/v1/chat/completions/input_tokens",
                headers={"Authorization": f"Bearer {endpoint.api_key}"},
                json={
                    "messages": self._vision_messages(question, image, instructions),
                    "max_tokens": self.max_output_tokens,
                },
            )
            self._check_status(response)
            count = response.json().get("input_tokens")
            if type(count) is not int or count < 0:
                raise ProviderFailure("backend_crashed", "Invalid vision token count")
            return count

    def stream_vision(
        self, question: str, image: bytes, cancel: Event, instructions: str
    ) -> Iterator[StreamEvent]:
        return self._stream((), cancel, instructions, None, vision=(question, image))

    def stream_with_tools(
        self,
        messages: Sequence[ContextRecord],
        cancel: Event,
        instructions: str,
        tools: list[dict[str, object]],
    ) -> Iterator[StreamEvent]:
        return self._stream(messages, cancel, instructions, tools)

    def _stream(
        self,
        messages: Sequence[ContextRecord],
        cancel: Event,
        instructions: str,
        tools: list[dict[str, object]] | None,
        vision: tuple[str, bytes] | None = None,
        output_tokens: int | None = None,
        selection_ids: tuple[str, ...] = (),
        response_schema: dict[str, object] | None = None,
    ) -> Iterator[StreamEvent]:
        endpoint = self.runtime.endpoint
        deadline = time.monotonic() + self.request_seconds
        headers = {"Authorization": f"Bearer {endpoint.api_key}"}
        payload: dict[str, object] = {
            "messages": (
                self._vision_messages(vision[0], vision[1], instructions)
                if vision is not None
                else _messages(messages, instructions)
            ),
            "max_tokens": self.max_output_tokens
            if output_tokens is None
            else output_tokens,
            "stream": True,
            "parse_tool_calls": tools is not None,
        }
        if vision is not None:
            payload["temperature"] = 0
        if output_tokens is not None:
            payload["temperature"] = 0
            field = "selected" if selection_ids else "evidence"
            item_schema: dict[str, object] = (
                {"type": "string", "enum": list(selection_ids)}
                if selection_ids
                else {"type": "string", "minLength": 1, "maxLength": 600}
            )
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "page_evidence",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            field: {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 3,
                                "items": item_schema,
                            }
                        },
                        "required": [field],
                        "additionalProperties": False,
                    },
                },
            }
            if response_schema is not None:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "page_notes",
                        "strict": True,
                        "schema": response_schema,
                    },
                }
        if tools is not None:
            payload["tools"] = tools
            payload["parallel_tool_calls"] = False
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
                    json=payload,
                ) as response,
            ):
                self._check_status(response)
                finished = False
                done = False
                finish_reason = ""
                call_id = ""
                call_name = ""
                call_arguments = ""
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
                    if not isinstance(delta, dict) or "function_call" in delta:
                        raise ProviderFailure(
                            "backend_crashed", "Invalid model stream delta"
                        )
                    calls = delta.get("tool_calls")
                    if calls is not None:
                        if tools is None:
                            raise ProviderFailure(
                                "invalid_request", "Tool calls are unavailable"
                            )
                        if not isinstance(calls, list) or len(calls) != 1:
                            raise ProviderFailure(
                                "backend_crashed", "Parallel or malformed tool calls"
                            )
                        call = calls[0]
                        if (
                            not isinstance(call, dict)
                            or type(call.get("index")) is not int
                            or call["index"] != 0
                        ):
                            raise ProviderFailure(
                                "backend_crashed", "Invalid tool call index"
                            )
                        new_id = call.get("id")
                        if new_id is not None:
                            if not isinstance(new_id, str) or (
                                call_id and call_id != new_id
                            ):
                                raise ProviderFailure(
                                    "backend_crashed", "Invalid tool call ID"
                                )
                            call_id = new_id
                        if call.get("type", "function") != "function":
                            raise ProviderFailure(
                                "backend_crashed", "Unsupported tool call type"
                            )
                        function = call.get("function", {})
                        if not isinstance(function, dict):
                            raise ProviderFailure(
                                "backend_crashed", "Invalid tool function"
                            )
                        new_name = function.get("name")
                        if new_name is not None:
                            if not isinstance(new_name, str) or (
                                call_name and call_name != new_name
                            ):
                                raise ProviderFailure(
                                    "backend_crashed", "Invalid tool name"
                                )
                            call_name = new_name
                        fragment = function.get("arguments", "")
                        if not isinstance(fragment, str):
                            raise ProviderFailure(
                                "backend_crashed", "Invalid tool arguments"
                            )
                        call_arguments += fragment
                        if len(call_arguments.encode("utf-8")) > (
                            tool_argument_limit(call_name) if call_name else 32768
                        ):
                            raise ProviderFailure(
                                "invalid_request", "Tool arguments are too large"
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
                        if (
                            reason
                            not in (
                                {"stop", "tool_calls", "length"}
                                if tools is not None
                                else {"stop", "length"}
                            )
                            or finished
                        ):
                            raise ProviderFailure(
                                "backend_crashed", "Incomplete model reply"
                            )
                        finished = True
                        finish_reason = reason
                if not done or not finished:
                    raise ProviderFailure(
                        "backend_crashed", "Stream ended before completion"
                    )
                if finish_reason == "length":
                    # A bounded, unfinished reply is not a crashed backend or an executable tool call.
                    yield StreamTerminal("output_limit")
                    return
                if finish_reason == "tool_calls":
                    if not call_id or not call_name or not call_arguments:
                        raise ProviderFailure("backend_crashed", "Incomplete tool call")
                    if len(call_arguments.encode("utf-8")) > tool_argument_limit(
                        call_name
                    ):
                        raise ProviderFailure(
                            "invalid_request", "Tool arguments are too large"
                        )
                    yield StreamToolCall(call_id, call_name, call_arguments)
                elif call_id or call_name or call_arguments:
                    raise ProviderFailure("backend_crashed", "Unfinished tool call")
                yield StreamTerminal()
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise ProviderFailure("timeout", "Local model stream timed out") from exc
        except (httpx.TransportError, json.JSONDecodeError, ValueError) as exc:
            raise ProviderFailure(
                "backend_crashed", "Local model stream failed"
            ) from exc
