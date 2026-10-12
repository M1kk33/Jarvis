"""Saved-text statistics; token totals never imply inference/context usage."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ChatStatistics:
    conversations: int
    user_messages: int
    assistant_messages: int
    regenerations: int
    generated_images: int
    failed_turns: int
    interrupted_turns: int
    pending_turns: int
    input_tokens: int | None
    output_tokens: int | None
    counted_user_messages: int
    counted_assistant_messages: int
    first_message_at: str | None
    last_message_at: str | None
