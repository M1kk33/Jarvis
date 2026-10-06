"""Static M5 tool registry, validation, policy, and durable execution boundary."""

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from jarvis.application.images import ImageService
from jarvis.application.resource_manager import _settle
from jarvis.domain.ports import ToolStore
from jarvis.domain.resources import AdmissionDenied
from jarvis.domain.tools import InvocationStatus, ToolInvocation
from jarvis.domain.types import RequestFailure, StreamToolCall


@dataclass(frozen=True)
class ToolSpec:
    name: str
    version: int
    schema: dict[str, object]
    risk: str
    capability: str
    timeout_seconds: float
    max_output_bytes: int
    side_effect: bool
    retry: str


SPECS = (
    ToolSpec(
        "generate_image",
        1,
        {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "minLength": 1, "maxLength": 1000},
                "seed": {"type": "integer", "minimum": 0, "maximum": 2147483647},
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
        "local_image",
        "image",
        600,
        1024,
        True,
        "never_without_reconciliation",
    ),
    ToolSpec(
        "current_time",
        1,
        {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "low",
        "clock",
        2,
        1024,
        False,
        "safe_after_verification",
    ),
    ToolSpec(
        "list_directory",
        1,
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "read",
        "read_files",
        5,
        8192,
        False,
        "safe_after_verification",
    ),
    ToolSpec(
        "read_text_file",
        1,
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": 16384},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "read",
        "read_files",
        5,
        32768,
        False,
        "safe_after_verification",
    ),
)


class ToolBackend(Protocol):
    def inspect(self, name: str, arguments: dict[str, object]) -> str: ...
    def execute(self, name: str, arguments: dict[str, object]) -> dict[str, object]: ...


@dataclass
class ToolPolicy:
    version: str = "m5-v1"
    approval_required: frozenset[str] = frozenset()
    capabilities: frozenset[str] = frozenset({"clock", "read_files"})

    def decision(self, spec: ToolSpec) -> str:
        if spec.capability not in self.capabilities:
            return "deny"
        return "approval" if spec.name in self.approval_required else "allow"


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(_value: str) -> None:
    raise ValueError("Non-finite JSON value")


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _valid_output(name: str, output: object) -> bool:
    if type(output) is not dict:
        return False
    if name == "current_time":
        return set(output) == {"utc"} and type(output["utc"]) is str
    if name == "read_text_file":
        return (
            set(output) == {"path", "text", "trust"}
            and type(output["path"]) is str
            and type(output["text"]) is str
            and output["trust"] == "untrusted"
        )
    if name == "list_directory":
        entries = output.get("entries")
        return (
            set(output) == {"path", "entries", "truncated", "trust"}
            and type(output["path"]) is str
            and type(output["truncated"]) is bool
            and output["trust"] == "untrusted"
            and type(entries) is list
            and len(entries) <= 100
            and all(
                type(entry) is dict
                and set(entry) == {"name", "kind"}
                and type(entry["name"]) is str
                and entry["kind"] in {"file", "directory"}
                for entry in entries
            )
        )
    return False


class ToolRegistry:
    def __init__(self) -> None:
        self._by_name = {spec.name: spec for spec in SPECS}

    def schemas(
        self, capabilities: frozenset[str] | None = None
    ) -> list[dict[str, object]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": {
                        "generate_image": "Generate one local 512 x 512 PNG from a bounded prompt.",
                        "current_time": "Return the current UTC time.",
                        "list_directory": "List names in an approved local directory.",
                        "read_text_file": "Read a bounded UTF-8 text file in an approved root.",
                    }[spec.name],
                    "parameters": spec.schema,
                },
            }
            for spec in SPECS
            if capabilities is None or spec.capability in capabilities
        ]

    def get(self, name: str) -> ToolSpec:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise RequestFailure("unsupported_capability", "Unknown tool") from exc

    def parse(
        self, proposal: StreamToolCall
    ) -> tuple[ToolSpec, dict[str, object], str, str]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", proposal.call_id):
            raise RequestFailure("invalid_request", "Invalid tool call ID")
        spec = self.get(proposal.name)
        if len(proposal.arguments_json.encode("utf-8")) > 4096:
            raise RequestFailure("invalid_request", "Tool arguments are too large")
        try:
            value = json.loads(
                proposal.arguments_json,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (ValueError, RecursionError) as exc:
            raise RequestFailure(
                "invalid_request", "Invalid tool arguments JSON"
            ) from exc
        if type(value) is not dict:
            raise RequestFailure("invalid_request", "Tool arguments must be an object")
        allowed = {
            "generate_image": frozenset({"prompt", "seed"}),
            "current_time": frozenset(),
            "list_directory": frozenset({"path"}),
            "read_text_file": frozenset({"path", "max_bytes"}),
        }[spec.name]
        if not value.keys() <= allowed:
            raise RequestFailure("invalid_request", "Unknown tool argument")
        if spec.name in {"list_directory", "read_text_file"}:
            path = value.get("path")
            if type(path) is not str or not 1 <= len(path) <= 1024:
                raise RequestFailure(
                    "invalid_request", "Tool path must be a bounded string"
                )
        if spec.name == "read_text_file" and "max_bytes" in value:
            limit = value["max_bytes"]
            if type(limit) is not int or not 1 <= limit <= 16384:
                raise RequestFailure("invalid_request", "Invalid file byte limit")
        if spec.name == "generate_image":
            prompt = value.get("prompt")
            seed = value.get("seed", 0)
            if (
                type(prompt) is not str
                or not 1 <= len(prompt) <= 1000
                or not prompt.strip()
            ):
                raise RequestFailure(
                    "invalid_request", "Image prompt must be 1–1000 characters"
                )
            if type(seed) is not int or not 0 <= seed <= 2147483647:
                raise RequestFailure("invalid_request", "Image seed is invalid")
            value["seed"] = seed
        canonical = _canonical(value)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return spec, value, canonical, digest


class ToolService:
    def __init__(
        self,
        store: ToolStore,
        backend: ToolBackend,
        policy: ToolPolicy | None = None,
        diagnostics: Callable[[str, dict[str, str | int]], None] | None = None,
        image_service: ImageService | None = None,
    ) -> None:
        self.store = store
        self.backend = backend
        self.policy = policy or ToolPolicy()
        self.registry = ToolRegistry()
        self.diagnostics = diagnostics or (lambda _event, _fields: None)
        self.image_service = image_service

    async def _inspect(self, name: str, arguments: dict[str, object]) -> str:
        if name == "generate_image":
            if self.image_service is None:
                raise RequestFailure(
                    "unsupported_capability", "Image generation is unavailable"
                )
            return self.image_service.target_identity
        return await asyncio.to_thread(self.backend.inspect, name, arguments)

    def _log_invocation(self, event: str, invocation: ToolInvocation) -> None:
        fields: dict[str, str | int] = {
            "turn_id": invocation.turn_id,
            "invocation_id": invocation.id,
        }
        if invocation.job_id is not None:
            fields["job_id"] = invocation.job_id
        self.diagnostics(event, fields)

    async def _mark_unknown(self, invocation_id: str) -> None:
        invocation = await self.store.run(
            lambda: self.store.mark_tool_unknown(invocation_id)
        )
        self._log_invocation("tool_unknown", invocation)

    def schemas(self) -> list[dict[str, object]]:
        return self.registry.schemas(self.policy.capabilities)

    async def propose_and_run(
        self,
        turn_id: str,
        proposal: StreamToolCall,
        expected_job_attempt: int | None = None,
    ) -> ToolInvocation:
        spec, arguments, canonical, digest = self.registry.parse(proposal)
        decision = self.policy.decision(spec)
        if decision == "deny":
            raise RequestFailure(
                "unsupported_capability", "Tool capability is disabled"
            )
        target = await self._inspect(spec.name, arguments)
        try:
            invocation = await self.store.run(
                lambda: self.store.record_tool_proposal(
                    turn_id,
                    proposal.call_id,
                    spec.name,
                    spec.version,
                    canonical,
                    digest,
                    self.policy.version,
                    target,
                    decision,
                    expected_job_attempt,
                )
            )
        except asyncio.CancelledError:
            await self.store.run(
                lambda: self.store.get_invocation_by_call(turn_id, proposal.call_id)
            )
            raise
        self._log_invocation("tool_proposed", invocation)
        if invocation.status is InvocationStatus.COMPLETE:
            return invocation
        if invocation.status is InvocationStatus.WAITING_APPROVAL:
            return invocation
        if invocation.status is not InvocationStatus.PROPOSED:
            raise RequestFailure("invalid_request", "Tool call cannot execute again")
        return await self.execute(invocation.id)

    async def trusted_issue_approval(self, invocation_id: str) -> str:
        """Called only by a trusted local UI/test mutator, never by model output."""
        expires_at = (datetime.now(UTC) + timedelta(minutes=5)).isoformat(
            timespec="microseconds"
        )
        try:
            approval = await self.store.run(
                lambda: self.store.issue_tool_approval(invocation_id, expires_at)
            )
        except asyncio.CancelledError:
            await self.store.run(lambda: self.store.get_tool_approval(invocation_id))
            raise
        return approval.id

    async def execute(
        self,
        invocation_id: str,
        approval_id: str | None = None,
    ) -> ToolInvocation:
        invocation = await self.store.run(
            lambda: self.store.get_invocation(invocation_id)
        )
        spec = self.registry.get(invocation.name)
        if (
            spec.version != invocation.version
            or self.policy.version != invocation.policy_version
            or self.policy.decision(spec) != invocation.decision
        ):
            raise RequestFailure("invalid_request", "Tool policy or version changed")
        _, arguments, canonical, digest = self.registry.parse(
            StreamToolCall(
                invocation.model_call_id,
                invocation.name,
                invocation.arguments_json,
            )
        )
        if (
            canonical != invocation.arguments_json
            or digest != invocation.arguments_sha256
        ):
            raise RequestFailure("invalid_request", "Tool arguments changed")
        target = await self._inspect(spec.name, arguments)
        if target != invocation.target_identity:
            raise RequestFailure("invalid_request", "Tool target or policy changed")
        try:
            await self.store.run(
                lambda: self.store.record_tool_intent(
                    invocation_id,
                    self.policy.version,
                    target,
                    approval_id,
                )
            )
        except asyncio.CancelledError:
            current = await self.store.run(
                lambda: self.store.get_invocation(invocation_id)
            )
            if current.status is InvocationStatus.INTENT:
                await self._mark_unknown(invocation_id)
            raise
        try:
            current_target = await self._inspect(spec.name, arguments)
            if (
                current_target != target
                or self.policy.version != invocation.policy_version
                or self.policy.decision(spec) != invocation.decision
            ):
                raise RequestFailure(
                    "invalid_request", "Tool target changed before execution"
                )
            await self.store.run(
                lambda: self.store.assert_tool_intent_active(invocation_id)
            )
        except BaseException:
            await _settle(asyncio.create_task(self._mark_unknown(invocation_id)))
            raise

        if spec.name == "generate_image":
            assert self.image_service is not None
            try:
                result = await self.image_service.run(invocation, arguments)
            except AdmissionDenied:
                error_receipt_json = _canonical(
                    {"status": "not_executed", "code": "out_of_memory"}
                )
                error_result_json = _canonical(
                    {"status": "error", "code": "out_of_memory"}
                )
                result = await self.store.run(
                    lambda: self.store.commit_tool_result(
                        invocation_id, error_receipt_json, error_result_json
                    )
                )
                self._log_invocation("tool_complete", result)
                return result
            except BaseException:
                current = await self.store.run(
                    lambda: self.store.get_invocation(invocation_id)
                )
                if current.status is InvocationStatus.INTENT:
                    await _settle(
                        asyncio.create_task(self._mark_unknown(invocation_id))
                    )
                raise
            self._log_invocation("tool_complete", result)
            return result

        worker = asyncio.create_task(
            asyncio.to_thread(self.backend.execute, spec.name, arguments)
        )
        cancelled = False
        try:
            output = await asyncio.wait_for(
                asyncio.shield(worker), spec.timeout_seconds
            )
        except asyncio.CancelledError:
            cancelled = True
            try:
                output = await asyncio.wait_for(
                    asyncio.shield(worker), spec.timeout_seconds
                )
            except (Exception, asyncio.CancelledError):
                await self._mark_unknown(invocation_id)
                raise
        except TimeoutError as exc:
            await self._mark_unknown(invocation_id)
            raise RequestFailure("timeout", "Tool execution timed out") from exc
        except Exception as exc:  # noqa: BLE001 - tool boundary records a typed failure
            code = exc.code if isinstance(exc, RequestFailure) else "unavailable"
            output = {"code": code}
            result_status = "error"
        else:
            result_status = "ok" if _valid_output(spec.name, output) else "error"
            if result_status == "error":
                output = {"code": "invalid_tool_output"}

        result_json = _canonical({"status": result_status, "data": output})
        if len(result_json.encode("utf-8")) > spec.max_output_bytes:
            result_json = _canonical({"status": "error", "code": "output_too_large"})
        receipt_json = _canonical(
            {
                "status": "complete",
                "result_bytes": len(result_json.encode("utf-8")),
            }
        )
        try:
            result = await self.store.run(
                lambda: self.store.commit_tool_result(
                    invocation_id, receipt_json, result_json
                )
            )
        except asyncio.CancelledError:
            current = await self.store.run(
                lambda: self.store.get_invocation(invocation_id)
            )
            if current.status is InvocationStatus.INTENT:
                await self._mark_unknown(invocation_id)
            raise
        except Exception:
            current = await self.store.run(
                lambda: self.store.get_invocation(invocation_id)
            )
            if current.status is not InvocationStatus.COMPLETE:
                raise
            result = current
        self._log_invocation("tool_complete", result)
        if cancelled:
            raise asyncio.CancelledError
        return result
