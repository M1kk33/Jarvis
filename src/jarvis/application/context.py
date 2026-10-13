"""Deterministic prompt selection with atomic exchange groups."""

import json
from dataclasses import dataclass, replace
from typing import Protocol

from jarvis.domain.tools import ContextRecord, ToolContextRecord
from jarvis.domain.types import Message, RequestFailure


class TokenEstimator(Protocol):
    def count(self, text: str) -> int: ...


class FakeTokenEstimator:
    """Conservative offline seam; production profiles must supply their tokenizer."""

    def count(self, text: str) -> int:
        return len(text.encode("utf-8"))


class PreflightTokenEstimator:
    """Cheap candidate selection; the local server must check the final template."""

    def count(self, text: str) -> int:
        return max(1, (len(text.encode("utf-8")) + 3) // 4)


@dataclass(frozen=True)
class ContextItem:
    role: str
    text: str
    group_id: str


@dataclass(frozen=True)
class ContextSelection:
    instructions: str
    items: tuple[ContextItem, ...]
    estimated_tokens: int
    tool_schemas: tuple[str, ...] = ()
    attachments: tuple[str, ...] = ()


def shrink_web_evidence(records: list[ContextRecord]) -> list[ContextRecord] | None:
    """Shorten only page evidence in the current exchange, never durable records.

    Keep the call/result pair, source, timestamp, trust label and screenshot ID.
    Other tools, user input and instructions are indivisible. A small useful
    excerpt is required; a prompt that cannot fit even that still fails.
    """
    if not records:
        return None
    required_turn = records[-1].turn_id
    candidates = []
    for index, record in enumerate(records):
        if (
            not isinstance(record, ToolContextRecord)
            or record.turn_id != required_turn
            or record.role != "tool"
            or record.name not in {"fetch_public_page", "render_public_page"}
        ):
            continue
        try:
            result = json.loads(record.content_json)
        except (ValueError, TypeError):
            continue
        if not isinstance(result, dict) or result.get("status") != "ok":
            continue
        data = result.get("data")
        if not isinstance(data, dict) or data.get("trust") != "untrusted":
            continue
        if data.get("representation") in {"analyzed_notes", "full_text"}:
            continue
        text = data.get("text")
        if isinstance(text, str) and len(text) > 256:
            candidates.append((len(text), index, result))
    if not candidates:
        return None
    _, index, result = max(candidates, key=lambda item: item[0])
    data = result["data"]
    data["text"] = data["text"][: max(256, len(data["text"]) // 2)]
    data["truncated"] = True
    data["context_truncated"] = True
    record = records[index]
    assert isinstance(record, ToolContextRecord)
    shortened = replace(
        record,
        content_json=json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
    )
    return records[:index] + [shortened] + records[index + 1 :]


class ContextBuilder:
    def __init__(
        self,
        estimator: TokenEstimator | None = None,
        window: int = 4096,
        output_allowance: int = 512,
        safety_margin: int = 256,
    ) -> None:
        self.estimator = estimator or FakeTokenEstimator()
        self.window = window
        self.output_allowance = output_allowance
        self.safety_margin = safety_margin
        if min(window, output_allowance, safety_margin) < 0:
            raise ValueError("Context limits must be nonnegative")

    def _cost(self, item: ContextItem) -> int:
        return self.estimator.count(item.text) + 4

    def build(
        self,
        items: list[ContextItem],
        *,
        instructions: str,
        required_group: str,
        tool_schemas: tuple[str, ...] = (),
        attachments: tuple[str, ...] = (),
    ) -> ContextSelection:
        groups: dict[str, list[ContextItem]] = {}
        previous_group: str | None = None
        for item in items:
            if item.group_id in groups and previous_group != item.group_id:
                raise RequestFailure(
                    "invalid_request", "Context groups must be contiguous"
                )
            groups.setdefault(item.group_id, []).append(item)
            previous_group = item.group_id
        if required_group not in groups:
            raise RequestFailure("invalid_request", "Current input is missing")
        fixed = (
            self.estimator.count(instructions)
            + 4
            + sum(self.estimator.count(schema) + 4 for schema in tool_schemas)
            + sum(self.estimator.count(part) + 4 for part in attachments)
            + self.output_allowance
            + self.safety_margin
        )
        required = groups[required_group]
        used = fixed + sum(self._cost(item) for item in required)
        if used > self.window:
            raise RequestFailure(
                "context_limit", "Required prompt exceeds context budget"
            )
        selected = {required_group}
        # Older exchanges are indivisible. Prefer the newest contiguous suffix.
        ordered = list(groups)
        required_index = ordered.index(required_group)
        for group_id in reversed(ordered[:required_index]):
            cost = sum(self._cost(item) for item in groups[group_id])
            if used + cost > self.window:
                break
            selected.add(group_id)
            used += cost
        return ContextSelection(
            instructions,
            tuple(item for item in items if item.group_id in selected),
            used,
            tool_schemas,
            attachments,
        )

    def from_messages(
        self, messages: list[Message], *, instructions: str = ""
    ) -> ContextSelection:
        if not messages or messages[-1].role != "user":
            raise RequestFailure("invalid_request", "Current user input is missing")
        return self.build(
            [ContextItem(m.role, m.text, m.turn_id) for m in messages],
            instructions=instructions,
            required_group=messages[-1].turn_id,
        )

    def from_records(
        self,
        records: list[ContextRecord],
        *,
        instructions: str,
        tool_schemas: tuple[str, ...],
    ) -> ContextSelection:
        if not records or not any(
            record.turn_id == records[-1].turn_id and record.role == "user"
            for record in records
        ):
            raise RequestFailure("invalid_request", "Current user input is missing")
        return self.build(
            [
                ContextItem(record.role, record.text, record.turn_id)
                for record in records
            ],
            instructions=instructions,
            required_group=records[-1].turn_id,
            tool_schemas=tool_schemas,
        )

    def fit_web_evidence(
        self,
        records: list[ContextRecord],
        *,
        instructions: str,
        tool_schemas: tuple[str, ...],
    ) -> list[ContextRecord]:
        while True:
            try:
                self.from_records(
                    records, instructions=instructions, tool_schemas=tool_schemas
                )
                return records
            except RequestFailure as exc:
                if exc.code != "context_limit":
                    raise
                shortened = shrink_web_evidence(records)
                if shortened is None:
                    raise
                records = shortened
