"""Backend-neutral application boundaries."""

from collections.abc import Callable, Iterator, Sequence
from threading import Event
from typing import Protocol, TypeVar, runtime_checkable

from jarvis.domain.jobs import Job, JobStep
from jarvis.domain.tools import Approval, ContextRecord, ToolInvocation
from jarvis.domain.types import Conversation, Draft, Message, StreamEvent, Turn

T = TypeVar("T")


class ChatProvider(Protocol):
    def stream(
        self, messages: Sequence[Message], cancel: Event, instructions: str = ""
    ) -> Iterator[StreamEvent]: ...


@runtime_checkable
class ToolChatProvider(Protocol):
    def stream_with_tools(
        self,
        messages: Sequence[ContextRecord],
        cancel: Event,
        instructions: str,
        tools: list[dict[str, object]],
    ) -> Iterator[StreamEvent]: ...
    def count_prompt_tokens_with_tools(
        self,
        messages: Sequence[ContextRecord],
        instructions: str,
        tools: list[dict[str, object]],
    ) -> int: ...


@runtime_checkable
class ExactTokenCounter(Protocol):
    def count_prompt_tokens(
        self, messages: Sequence[Message], instructions: str
    ) -> int: ...


class ConversationStore(Protocol):
    async def run(self, operation: Callable[[], T]) -> T: ...
    def create_conversation(self, title: str) -> Conversation: ...
    def list_conversations(self) -> list[Conversation]: ...
    def get_conversation(self, conversation_id: str) -> Conversation: ...
    def get_turns(self, conversation_id: str) -> list[Turn]: ...
    def get_messages(self, conversation_id: str) -> list[Message]: ...
    def get_invocations(self, turn_id: str) -> list[ToolInvocation]: ...
    def get_recent_messages(
        self, conversation_id: str, before_position: int | None, limit_turns: int = 8
    ) -> tuple[list[Message], int | None]: ...
    def get_answer(self, turn_id: str) -> Message | None: ...
    def get_draft(self, turn_id: str) -> Draft | None: ...
    def get_turn_by_submission(
        self, conversation_id: str, submission_id: str
    ) -> Turn | None: ...
    def get_turn(self, turn_id: str) -> Turn: ...
    def begin_turn(
        self, conversation_id: str, text: str, submission_id: str
    ) -> tuple[Turn, bool]: ...
    def complete_turn(
        self, turn_id: str, answer: str, expected_job_attempt: int | None = None
    ) -> Message: ...
    def checkpoint_draft(
        self, turn_id: str, text: str, expected_job_attempt: int | None = None
    ) -> None: ...
    def interrupt_turn(
        self,
        turn_id: str,
        code: str = "cancelled",
        expected_job_attempt: int | None = None,
    ) -> Turn: ...
    def fail_turn(
        self,
        turn_id: str,
        code: str = "provider_error",
        expected_job_attempt: int | None = None,
    ) -> Turn: ...


class ToolStore(Protocol):
    async def run(self, operation: Callable[[], T]) -> T: ...
    def get_invocation(self, invocation_id: str) -> ToolInvocation: ...
    def get_invocation_by_call(
        self, turn_id: str, model_call_id: str
    ) -> ToolInvocation | None: ...
    def get_invocations(self, turn_id: str) -> list[ToolInvocation]: ...
    def get_recent_context(
        self,
        conversation_id: str,
        before_position: int | None,
        limit_turns: int = 8,
    ) -> tuple[list[ContextRecord], int | None]: ...
    def record_tool_proposal(
        self,
        turn_id: str,
        model_call_id: str,
        name: str,
        version: int,
        arguments_json: str,
        arguments_sha256: str,
        policy_version: str,
        target_identity: str,
        decision: str,
        expected_job_attempt: int | None = None,
    ) -> ToolInvocation: ...
    def issue_tool_approval(self, invocation_id: str, expires_at: str) -> Approval: ...
    def get_tool_approval(self, invocation_id: str) -> Approval | None: ...
    def record_tool_intent(
        self,
        invocation_id: str,
        policy_version: str,
        target_identity: str,
        approval_id: str | None = None,
    ) -> ToolInvocation: ...
    def commit_tool_result(
        self,
        invocation_id: str,
        receipt_json: str,
        result_json: str,
    ) -> ToolInvocation: ...
    def mark_tool_unknown(self, invocation_id: str) -> ToolInvocation: ...
    def assert_tool_intent_active(self, invocation_id: str) -> None: ...


class JobStore(Protocol):
    async def run(self, operation: Callable[[], T]) -> T: ...
    def begin_turn(
        self,
        conversation_id: str,
        text: str,
        submission_id: str,
        job_id: str | None = None,
        profile_name: str = "fake",
    ) -> tuple[Turn, bool]: ...
    def get_job(self, job_id: str) -> Job: ...
    def get_job_by_turn(self, turn_id: str) -> Job | None: ...
    def list_jobs(self, conversation_id: str | None = None) -> list[Job]: ...
    def get_job_steps(self, job_id: str) -> list[JobStep]: ...
    def start_job(self, job_id: str) -> Job: ...
    def request_job_cancel(self, job_id: str) -> Job: ...
    def settle_job_cancel(self, job_id: str) -> Job: ...
    def mark_job_waiting(self, job_id: str) -> Job: ...
    def get_turn(self, turn_id: str) -> Turn: ...
    def get_answer(self, turn_id: str) -> Message | None: ...
    def complete_turn(
        self, turn_id: str, answer: str, expected_job_attempt: int | None = None
    ) -> Message: ...
    def fail_turn(
        self,
        turn_id: str,
        code: str,
        expected_job_attempt: int | None = None,
    ) -> Turn: ...
