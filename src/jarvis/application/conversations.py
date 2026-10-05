"""Conversation use cases with bounded, cancellable fake streaming."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from functools import partial
from threading import Event
from uuid import uuid4

from jarvis.application.context import ContextBuilder
from jarvis.domain.ports import ChatProvider, ConversationStore
from jarvis.domain.types import (
    NORMALIZED_ERROR_CODES,
    Conversation,
    ConversationError,
    Draft,
    Message,
    ProviderFailure,
    RequestFailure,
    SendResult,
    StorageFailure,
    StreamDelta,
    StreamTerminal,
    Turn,
    TurnStatus,
)


def _error_code(exc: Exception) -> str:
    if isinstance(exc, RequestFailure):
        return exc.code
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, MemoryError):
        return "out_of_memory"
    if isinstance(exc, (ConnectionError, OSError)):
        return "unavailable"
    return "backend_crashed"


class ConversationService:
    def __init__(
        self,
        store: ConversationStore,
        provider: ChatProvider,
        *,
        context: ContextBuilder | None = None,
        instructions: str = "",
        diagnostics: Callable[[str, dict[str, str | int]], None] | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.context = context or ContextBuilder()
        self.instructions = instructions
        self.diagnostics = diagnostics or (lambda _event, _fields: None)

    def create(self, title: str) -> Conversation:
        return self.store.create_conversation(title)

    def list_conversations(self) -> list[Conversation]:
        return self.store.list_conversations()

    def resume(
        self, conversation_id: str
    ) -> tuple[Conversation, list[Turn], list[Message]]:
        return (
            self.store.get_conversation(conversation_id),
            self.store.get_turns(conversation_id),
            self.store.get_messages(conversation_id),
        )

    def get_draft(self, turn_id: str) -> Draft | None:
        return self.store.get_draft(turn_id)

    def send(
        self, conversation_id: str, text: str, submission_id: str | None = None
    ) -> SendResult:
        return asyncio.run(self.send_async(conversation_id, text, submission_id))

    async def send_async(
        self,
        conversation_id: str,
        text: str,
        submission_id: str | None = None,
        *,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        cancel: Event | None = None,
    ) -> SendResult:
        stable_id = submission_id or str(uuid4())
        signal = cancel or Event()
        turn: Turn | None = None
        created = False
        phase = "submission"
        try:
            active_turn, created = await self.store.run(
                lambda: self.store.begin_turn(conversation_id, text, stable_id)
            )
            turn = active_turn
            if not created:
                answer = await self.store.run(
                    lambda: self.store.get_answer(active_turn.id)
                )
                return SendResult(active_turn, answer, duplicate=True)
            phase = "generation"
            messages: list[Message] = []
            cursor: int | None = None
            while True:
                page, cursor = await self.store.run(
                    partial(self.store.get_recent_messages, conversation_id, cursor)
                )
                if not page:
                    break
                messages = page + messages
                selection = self.context.from_messages(
                    messages, instructions=self.instructions
                )
                if len(selection.items) < len(messages):
                    break
            selection = self.context.from_messages(
                messages, instructions=self.instructions
            )
            selected_groups = {item.group_id for item in selection.items}
            selected = [m for m in messages if m.turn_id in selected_groups]
            answer_text = await self._consume_stream(
                active_turn.id, selected, signal, on_delta
            )
            phase = "completion"
            answer = await self.store.run(
                lambda: self.store.complete_turn(active_turn.id, answer_text)
            )
            settled = await self.store.run(lambda: self.store.get_turn(active_turn.id))
            self.diagnostics(
                "turn_complete",
                {"turn_id": active_turn.id, "bytes": len(answer_text.encode("utf-8"))},
            )
            return SendResult(settled, answer, duplicate=False)
        except asyncio.CancelledError:
            signal.set()
            prior = await self.store.run(
                lambda: self.store.get_turn_by_submission(conversation_id, stable_id)
            )
            if prior is not None:
                if prior.status is TurnStatus.COMPLETE:
                    answer = await self.store.run(
                        lambda: self.store.get_answer(prior.id)
                    )
                    return SendResult(prior, answer, duplicate=not created)
                if prior.status is TurnStatus.PENDING:
                    await self.store.run(lambda: self.store.interrupt_turn(prior.id))
            self.diagnostics("turn_cancelled", {})
            raise RequestFailure("cancelled", "Chat request was cancelled") from None
        except Exception as exc:
            if (
                phase == "generation"
                and turn is not None
                and turn.status is TurnStatus.PENDING
                and not isinstance(exc, StorageFailure)
            ):
                code = _error_code(exc)
                if code == "cancelled":
                    await self.store.run(lambda: self.store.interrupt_turn(turn.id))
                else:
                    await self.store.run(lambda: self.store.fail_turn(turn.id, code))
                self.diagnostics("turn_failed", {"turn_id": turn.id, "code": code})
            if isinstance(exc, ConversationError):
                raise
            if phase != "generation":
                raise
            raise ProviderFailure(
                _error_code(exc),
                f"Chat request failed ({_error_code(exc)}); the input remains stored",
            ) from exc

    async def _consume_stream(
        self,
        turn_id: str,
        messages: Sequence[Message],
        cancel: Event,
        on_delta: Callable[[str], Awaitable[None]] | None,
    ) -> str:
        iterator = iter(self.provider.stream(messages, cancel, self.instructions))
        chunks: list[str] = []
        received_bytes = 0
        max_bytes = max(1, self.context.output_allowance * 8)
        bytes_since_checkpoint = 0
        last_checkpoint = time.monotonic()
        terminal: StreamTerminal | None = None
        while True:
            if cancel.is_set():
                raise RequestFailure("cancelled", "Chat request was cancelled")
            event = await asyncio.to_thread(next, iterator, None)
            if event is None:
                break
            if isinstance(event, StreamTerminal):
                if terminal is not None:
                    raise RequestFailure(
                        "backend_crashed", "Multiple stream terminal results"
                    )
                terminal = event
                continue
            if (
                terminal is not None
                or not isinstance(event, StreamDelta)
                or not event.text
            ):
                raise RequestFailure("backend_crashed", "Malformed stream event")
            if len(event.text) > max_bytes - received_bytes:
                raise RequestFailure("invalid_request", "Reply exceeds byte limit")
            delta_bytes = len(event.text.encode("utf-8"))
            if received_bytes + delta_bytes > max_bytes:
                raise RequestFailure("invalid_request", "Reply exceeds byte limit")
            received_bytes += delta_bytes
            if len(event.text) > self.context.output_allowance:
                raise RequestFailure(
                    "invalid_request", "Reply delta exceeds output allowance"
                )
            chunks.append(event.text)
            text = "".join(chunks)
            if self.context.estimator.count(text) > self.context.output_allowance:
                raise RequestFailure(
                    "invalid_request", "Reply exceeds output allowance"
                )
            bytes_since_checkpoint += delta_bytes
            now = time.monotonic()
            if (
                len(chunks) == 1
                or bytes_since_checkpoint >= 2048
                or now - last_checkpoint >= 1
            ):
                await self.store.run(
                    partial(self.store.checkpoint_draft, turn_id, text)
                )
                bytes_since_checkpoint = 0
                last_checkpoint = now
            if on_delta is not None:
                await on_delta(event.text)
        if terminal is None:
            raise RequestFailure(
                "backend_crashed", "Stream ended without a terminal result"
            )
        if terminal.error_code is not None:
            if terminal.error_code not in NORMALIZED_ERROR_CODES:
                raise RequestFailure("backend_crashed", "Unknown stream error")
            raise RequestFailure(
                terminal.error_code, "Provider returned a terminal error"
            )
        if not chunks:
            raise RequestFailure("backend_crashed", "Stream completed without a reply")
        return "".join(chunks)
