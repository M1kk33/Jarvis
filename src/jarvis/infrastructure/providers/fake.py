"""Deterministic chat provider for development and acceptance tests."""

from collections.abc import Iterator, Sequence
from threading import Event

from jarvis.domain.types import Message, StreamDelta, StreamEvent, StreamTerminal


class FakeChatProvider:
    def __init__(self) -> None:
        self.requests: list[tuple[Message, ...]] = []
        self.instructions: list[str] = []

    def _reply(self, messages: Sequence[Message]) -> str:
        snapshot = tuple(messages)
        self.requests.append(snapshot)
        if not snapshot or snapshot[-1].role != "user":
            raise ValueError("A chat request must end with a user message")
        return f"Fake reply: {snapshot[-1].text} (context messages: {len(snapshot)})"

    def stream(
        self, messages: Sequence[Message], cancel: Event, instructions: str = ""
    ) -> Iterator[StreamEvent]:
        self.instructions.append(instructions)
        answer = self._reply(messages)
        for offset in range(0, len(answer), 32):
            if cancel.is_set():
                yield StreamTerminal("cancelled")
                return
            yield StreamDelta(answer[offset : offset + 32])
        yield StreamTerminal()
