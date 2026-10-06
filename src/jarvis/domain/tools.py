"""Durable tool identities and context records; no tool implementation lives here."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from jarvis.domain.types import Message


class InvocationStatus(StrEnum):
    PROPOSED = "proposed"
    WAITING_APPROVAL = "waiting_approval"
    INTENT = "intent"
    COMPLETE = "complete"
    DENIED = "denied"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True)
class ToolInvocation:
    id: str
    turn_id: str
    conversation_id: str
    ordinal: int
    model_call_id: str
    name: str
    version: int
    arguments_json: str
    arguments_sha256: str
    policy_version: str
    target_identity: str
    status: InvocationStatus
    decision: str
    result_json: str | None
    receipt_json: str | None
    job_id: str | None = None
    job_attempt: int | None = None


@dataclass(frozen=True)
class Approval:
    id: str
    turn_id: str
    invocation_id: str
    arguments_sha256: str
    tool_version: int
    target_identity: str
    policy_version: str
    session_id: str
    expires_at: str
    status: Literal["issued", "consumed", "invalidated"]
    job_id: str | None = None
    job_attempt: int | None = None


@dataclass(frozen=True)
class ToolContextRecord:
    invocation_id: str
    turn_id: str
    conversation_id: str
    model_call_id: str
    name: str
    content_json: str
    role: Literal["assistant_tool_call", "tool"]

    @property
    def text(self) -> str:
        return self.content_json


ContextRecord = Message | ToolContextRecord
