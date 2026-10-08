"""Durable chat-job state and step identity."""

from dataclasses import dataclass
from enum import StrEnum

from jarvis.domain.resources import canonical_gpu_uuid

JOB_EXECUTION_SECONDS = 600


def same_profile_identity(first: str, second: str) -> bool:
    """Treat only the physical GPU UUID's casing as an equivalent saved identity."""

    def canonical(identity: str) -> str:
        parts = identity.split(":")
        if len(parts) > 1:
            try:
                parts[1] = canonical_gpu_uuid(parts[1])
            except ValueError:
                return identity
        return ":".join(parts)

    return canonical(first) == canonical(second)


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
