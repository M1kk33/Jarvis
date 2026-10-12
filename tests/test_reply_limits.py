"""Long and bounded replies: text, tool mode, terminal identity and retained drafts."""

import json
from threading import Event
from types import SimpleNamespace

import httpx
import pytest

from jarvis.application.context import ContextBuilder, PreflightTokenEstimator
from jarvis.application.conversations import ConversationService
from jarvis.application.tools import ToolService
from jarvis.domain.resources import RuntimeIdentity
from jarvis.domain.types import (
    ProviderFailure,
    RequestFailure,
    StreamTerminal,
    SubmissionConflict,
    TurnStatus,
)
from jarvis.infrastructure.providers.llama_cpp import LlamaChatProvider, LlamaEndpoint
from jarvis.infrastructure.read_tools import SafeReadTools
from jarvis.infrastructure.sqlite_store import SqliteStore


def _events(parts, reason="stop"):
    return [
        *[
            {"choices": [{"delta": {"content": text}, "finish_reason": None}]}
            for text in parts
        ],
        {"choices": [{"delta": {}, "finish_reason": reason}]},
    ]


def _provider(events):
    requests = []
    endpoint = LlamaEndpoint(
        "http://127.0.0.1:8123", "fixture", RuntimeIdentity(1, "fixture", 1, 1)
    )

    def handler(request):
        if request.url.path.endswith("input_tokens"):
            return httpx.Response(200, json={"input_tokens": 20})
        if request.url.path == "/tokenize":
            text = json.loads(request.content)["content"]
            # A deterministic fixture tokenizer; inference request assertions
            # below remain separate from these new statistics-only requests.
            return httpx.Response(200, json={"tokens": list(range(len(text)))})
        requests.append(json.loads(request.content))
        body = "\n".join("data: " + json.dumps(event) for event in events)
        return httpx.Response(200, text=body + "\ndata: [DONE]\n")

    return LlamaChatProvider(
        SimpleNamespace(endpoint=endpoint),
        httpx.MockTransport(handler),
        max_output_tokens=2048,
        request_seconds=300,
    ), requests


def _service(store, provider, tool_mode):
    return ConversationService(
        store,
        provider,
        context=ContextBuilder(PreflightTokenEstimator(), output_allowance=2048),
        tool_service=ToolService(store, SafeReadTools([], store.data_dir))
        if tool_mode
        else None,
    )


@pytest.mark.parametrize("tool_mode", [False, True])
def test_long_unicode_reply_exceeds_old_byte_limit_and_commits_once(
    tmp_path, tool_mode
):
    text = "Длинный ответ с объяснениями и примерами.\n" * 140
    assert 4096 < len(text.encode("utf-8")) < 2048 * 8
    events = _events([text[i : i + 100] for i in range(0, len(text), 100)])
    provider, requests = _provider(events)
    with SqliteStore(tmp_path) as store:
        service = _service(store, provider, tool_mode)
        conversation = store.create_conversation("Long fixture")
        result = service.send(conversation.id, "Explain", "stable")
        assert result.turn.status is TurnStatus.COMPLETE
        assert result.answer.text == text
        assert requests[0]["max_tokens"] == service.context.output_allowance == 2048
        assert bool(requests[0]["parse_tool_calls"]) == tool_mode
        assert service.send(conversation.id, "Explain", "stable").duplicate
        assert len(requests) == 1
        assert len(store.get_messages(conversation.id)) == 2


@pytest.mark.parametrize("tool_mode", [False, True])
def test_output_limit_retains_tail_is_not_complete_or_retried_and_next_request_works(
    tmp_path, tool_mode
):
    prefix, tail = "First checkpoint.\n", "Последние слова перед лимитом."
    events = _events([prefix, tail], "length")
    provider, requests = _provider(events)
    with SqliteStore(tmp_path) as store:
        service = _service(store, provider, tool_mode)
        conversation = store.create_conversation("Limited fixture")
        with pytest.raises(RequestFailure) as failure:
            service.send(conversation.id, "Long question", "stable")
        assert failure.value.code == "output_limit"
        turn = store.get_turn_by_submission(conversation.id, "stable")
        assert turn.status is TurnStatus.FAILED
        assert turn.error_code == "output_limit"
        assert store.get_answer(turn.id) is None
        assert store.get_draft(turn.id).text == prefix + tail
        assert store.get_invocations(turn.id) == []
        duplicate = service.send(conversation.id, "Long question", "stable")
        assert duplicate.duplicate and duplicate.answer is None
        assert len(requests) == 1
        with pytest.raises(SubmissionConflict):
            service.send(conversation.id, "Changed question", "stable")
        events[:] = _events(["Short next answer"])
        result = service.send(conversation.id, "Next question", "next")
        assert result.answer.text == "Short next answer"
        assert result.turn.status is TurnStatus.COMPLETE
        assert len(requests) == 2
        assert store.get_draft(turn.id).text == prefix + tail
    with SqliteStore(tmp_path) as reopened:
        assert reopened.get_turn(turn.id).error_code == "output_limit"
        assert reopened.get_draft(turn.id).text == prefix + tail


def test_length_terminated_tool_call_never_becomes_executable():
    events = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "current_time", "arguments": "{}"},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "length"}]},
    ]
    provider, _ = _provider(events)
    assert list(provider.stream_with_tools([], Event(), "", [])) == [
        StreamTerminal("output_limit")
    ]


def test_length_without_done_remains_a_broken_stream():
    provider, _ = _provider([])
    provider.transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            text='data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n',
        )
    )
    with pytest.raises(ProviderFailure) as failure:
        list(provider.stream([], Event()))
    assert failure.value.code == "backend_crashed"


@pytest.mark.parametrize("tool_mode", [False, True])
def test_long_reply_remains_byte_bounded(tmp_path, tool_mode):
    provider, _ = _provider(_events(["x" * 1000] * 17))
    with SqliteStore(tmp_path) as store:
        service = _service(store, provider, tool_mode)
        conversation = store.create_conversation("Oversized fixture")
        with pytest.raises(RequestFailure) as failure:
            service.send(conversation.id, "Explain", "bounded")
        assert failure.value.code == "invalid_request"
        turn = store.get_turn_by_submission(conversation.id, "bounded")
        assert store.get_answer(turn.id) is None
        assert len(store.get_draft(turn.id).text.encode("utf-8")) <= 2048 * 8
