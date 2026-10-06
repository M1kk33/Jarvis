"""Durable chat-job state and step identity."""

from dataclasses import dataclass
from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    CANCEL_REQUESTED = "cancel_requested"
    INTERRUPTED = "interrupted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_JOBS = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})


@dataclass(frozen=True)
class Job:
    id: str
    turn_id: str
    conversation_id: str
    profile_name: str
    status: JobStatus
    next_step: int
    attempt: int
    deadline_at: str
    progress: str
    cancel_requested: bool
    error_code: str | None


@dataclass(frozen=True)
class JobStep:
    id: str
    job_id: str
    ordinal: int
    kind: str
    invocation_id: str | None
    status: str
    attempt: int
