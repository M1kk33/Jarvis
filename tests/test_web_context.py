"""Page evidence fits both prompt checks without rewriting approved receipts."""

import asyncio
import json
from dataclasses import replace

import pytest

from jarvis.application.context import (
    ContextBuilder,
    PreflightTokenEstimator,
    shrink_web_evidence,
)
from jarvis.application.conversations import ConversationService
from jarvis.application.tools import ToolPolicy, ToolService
from jarvis.domain.tools import ToolContextRecord
from jarvis.domain.types import (
    Message,
    RequestFailure,
    StreamDelta,
    StreamTerminal,
    StreamToolCall,
    TextPart,
    TurnStatus,
)
from jarvis.infrastructure.read_tools import SafeReadTools
from jarvis.infrastructure.sqlite_store import SqliteStore


def records(name="fetch_public_page", text="Урок 📚 " * 1500):
    user = Message(
        id="user",
        turn_id="turn",
        conversation_id="chat",
        role="user",
        parts=(TextPart("Explain the page"),),
        position=1,
        status="complete",
        turn_status=TurnStatus.PENDING,
        created_at="now",
    )
    call = ToolContextRecord(
        "invocation",
        "turn",
        "chat",
        "call",
        name,
        '{"url":"https://example.com/lesson"}',
        "assistant_tool_call",
    )
    result = {
        "status": "ok",
        "data": {
            "url": "https://example.com/lesson",
            "text": text,
            "retrieved_at": "now",
            "trust": "untrusted",
            "truncated": False,
            "artifact_id": "screenshot",
        },
    }
    return [
        user,
        call,
        replace(call, role="tool", content_json=json.dumps(result, ensure_ascii=False)),
    ]


@pytest.mark.parametrize("name", ["fetch_public_page", "render_public_page"])
def test_preflight_preserves_exchange_and_source_with_explicit_partial_evidence(name):
    original = records(name)
    context = ContextBuilder(PreflightTokenEstimator(), output_allowance=2048)
    fitted = context.fit_web_evidence(
        original, instructions="Required", tool_schemas=()
    )
    assert fitted[:2] == original[:2]
    full = json.loads(original[-1].text)["data"]
    data = json.loads(fitted[-1].text)["data"]
    assert full["text"].startswith(data["text"]) and len(data["text"]) >= 256
    assert data["truncated"] and data["context_truncated"]
    assert not full["truncated"] and "context_truncated" not in full
    for key in ("url", "retrieved_at", "trust", "artifact_id"):
        assert data[key] == full[key]
    context.from_records(fitted, instructions="Required", tool_schemas=())


def test_only_current_page_evidence_can_shrink_and_impossible_required_input_fails():
    original = records()
    older = [replace(item, turn_id="older") for item in original]
    current = records(text="Short")
    assert shrink_web_evidence(older + current) is None
    assert shrink_web_evidence(records("read_text_file")) is None
    assert shrink_web_evidence([]) is None
    original[0] = replace(original[0], parts=(TextPart("Required user text" * 2000),))
    context = ContextBuilder(PreflightTokenEstimator(), output_allowance=2048)
    with pytest.raises(RequestFailure) as raised:
        context.fit_web_evidence(original, instructions="Keep", tool_schemas=())
    assert raised.value.code == "context_limit"
    assert len(json.loads(original[-1].text)["data"]["text"]) > 10000
    assert original[0].text == "Required user text" * 2000


def test_approval_continuation_fits_exact_tokenizer_without_replaying_fetch(tmp_path):
    page = "Probability and discrete random variables. " * 230

    class Fetcher:
        calls = 0

        def inspect(self, name, arguments):
            return "GET " + arguments["url"]

        def execute(self, name, arguments):
            self.calls += 1
            return {
                "url": arguments["url"],
                "text": page,
                "retrieved_at": "2026-10-13T15:30:29+00:00",
                "trust": "untrusted",
                "truncated": False,
                "status": 200,
                "content_type": "text/html",
            }

    class Provider:
        calls = 0

        def __init__(self):
            self.counts = []
            self.received = None

        def count_prompt_tokens_with_tools(self, messages, instructions, tools):
            # Deliberately larger than the preflight estimate, like a tokenizer
            # encountering formula-heavy or non-ASCII page text.
            count = sum(len(item.text.encode("utf-8")) for item in messages) // 2 + 1200
            self.counts.append(count)
            return count

        def stream_with_tools(self, messages, cancel, instructions, tools):
            self.calls += 1
            assert self.counts[-1] <= 4096 - 2048 - 256
            if messages[-1].role != "tool":
                yield StreamToolCall(
                    "call", "fetch_public_page", '{"url":"https://example.com/lesson"}'
                )
            else:
                self.received = messages
                yield StreamDelta("Here is a summary of the available excerpt.")
            yield StreamTerminal()

    with SqliteStore(tmp_path) as store:
        chat = store.create_conversation("Web")
        fetcher, provider = Fetcher(), Provider()
        tools = ToolService(
            store,
            SafeReadTools([], store.data_dir),
            ToolPolicy(version="m13-v1", capabilities=frozenset({"network"})),
            web_backend=fetcher,
        )
        service = ConversationService(
            store,
            provider,
            tool_service=tools,
            context=ContextBuilder(PreflightTokenEstimator(), output_allowance=2048),
        )
        waiting = service.send(chat.id, "Summarize this page", "stable")
        assert waiting.turn.status is TurnStatus.PENDING and fetcher.calls == 0
        invocation = store.get_invocations(waiting.turn.id)[0]
        approval = asyncio.run(tools.trusted_issue_approval(invocation.id))
        asyncio.run(tools.execute(invocation.id, approval))
        committed = store.get_invocation(invocation.id).result_json
        completed = asyncio.run(service.continue_turn_async(waiting.turn.id))
        assert completed.turn.status is TurnStatus.COMPLETE
        assert fetcher.calls == 1 and provider.calls == 2
        assert any(count > 1792 for count in provider.counts), (
            provider.counts,
            [(r.role, len(r.text)) for r in provider.received],
            json.loads(committed).get("status"),
        )
        excerpt = json.loads(provider.received[-1].text)["data"]
        assert excerpt["context_truncated"] and excerpt["trust"] == "untrusted"
        assert page.startswith(excerpt["text"]) and len(excerpt["text"]) < len(page)
        assert store.get_invocation(invocation.id).result_json == committed
        assert json.loads(committed)["data"]["text"] == page
        duplicate = service.send(chat.id, "Summarize this page", "stable")
        assert duplicate.duplicate and fetcher.calls == 1 and provider.calls == 2
