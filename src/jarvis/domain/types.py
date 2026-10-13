"""Backend-neutral conversation and streaming records."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class TurnStatus(StrEnum):
    PENDING = "pending"
    COMPLETE = "complete"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


NORMALIZED_ERROR_CODES = frozenset(
    {
        "invalid_request",
        "unsupported_capability",
        "unavailable",
        "out_of_memory",
        "timeout",
        "cancelled",
        "backend_crashed",
        "output_limit",
        "context_limit",
    }
)


class ConversationError(Exception):
    """Base class for expected conversation errors."""


class ConversationNotFound(ConversationError):
    pass


class ActiveTurnError(ConversationError):
    pass


class SubmissionConflict(ConversationError):
    pass


class StaleTurnError(ConversationError):
    pass


class RequestFailure(ConversationError):
    def __init__(self, code: str, detail: str) -> None:
        if code not in NORMALIZED_ERROR_CODES:
            raise ValueError("Unknown normalized error code")
        self.code = code
        super().__init__(detail)


class ProviderFailure(RequestFailure):
    pass


class NoEffectFailure(RequestFailure):
    """The operation failed before any external side effect began."""


class BrowserCleanupUnverified(RequestFailure):
    """The disposable browser tree could not be proven absent."""


class StorageFailure(ConversationError):
    pass


@dataclass(frozen=True)
class StreamDelta:
    text: str


@dataclass(frozen=True)
class StreamTerminal:
    error_code: str | None = None


@dataclass(frozen=True)
class StreamToolCall:
    call_id: str
    name: str
    arguments_json: str


StreamEvent = StreamDelta | StreamToolCall | StreamTerminal


@dataclass(frozen=True)
class Draft:
    turn_id: str
    text: str
    status: Literal["draft", "interrupted"]


@dataclass(frozen=True)
class TextPart:
    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True)
class Conversation:
    id: str
    title: str
    created_at: str
    pinned: bool = False


@dataclass(frozen=True)
class Turn:
    id: str
    conversation_id: str
    position: int
    submission_id: str
    status: TurnStatus
    created_at: str
    error_code: str | None
    regeneration_of: str | None = None
    regeneration_source: str | None = None


@dataclass(frozen=True)
class Message:
    id: str
    conversation_id: str
    turn_id: str
    position: int
    role: Literal["user", "assistant"]
    parts: tuple[TextPart, ...]
    status: Literal["complete"]
    turn_status: TurnStatus
    created_at: str

    @property
    def text(self) -> str:
        return "".join(part.text for part in self.parts)


@dataclass(frozen=True)
class SendResult:
    turn: Turn
    answer: Message | None
    duplicate: bool


@dataclass(frozen=True)
class VisionInput:
    artifact_id: str
    job_id: str
    relative_path: str
    size_bytes: int
    sha256: str
    width: int
    height: int
    profile_name: str
