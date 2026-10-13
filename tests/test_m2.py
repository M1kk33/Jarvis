"""M2 context, stream, draft, and cancelled-commit behavior."""

import asyncio
import hashlib
import sqlite3
from importlib import resources
from threading import Event

import pytest

from jarvis.application.context import ContextBuilder, ContextItem, FakeTokenEstimator
from jarvis.application.conversations import ConversationService
from jarvis.bootstrap import main
from jarvis.domain.types import (
    RequestFailure,
    StorageFailure,
    StreamDelta,
    StreamTerminal,
    TurnStatus,
)
from jarvis.infrastructure.diagnostics import Diagnostics
from jarvis.infrastructure.providers.fake import FakeChatProvider
from jarvis.infrastructure.sqlite_store import SqliteStore


def test_context_exact_boundary_and_atomic_groups():
    builder = ContextBuilder(
        FakeTokenEstimator(), window=30, output_allowance=5, safety_margin=2
    )
    items = [
        ContextItem("user", "12345", "old"),
        ContextItem("assistant", "12345", "old"),
        ContextItem("user", "abc", "new"),
    ]
    # Instructions 1+4, old group 18, required 7, reserves 7 = 37.
    assert [
        x.group_id
        for x in builder.build(items, instructions="i", required_group="new").items
    ] == ["new"]
    edge = ContextBuilder(
        FakeTokenEstimator(), window=37, output_allowance=5, safety_margin=2
    )
    selected = edge.build(items, instructions="i", required_group="new")
    assert selected.estimated_tokens == 37
    assert [x.group_id for x in selected.items] == ["old", "old", "new"]
    with pytest.raises(RequestFailure, match="Required prompt"):
        ContextBuilder(window=18, output_allowance=5, safety_margin=2).build(
            items, instructions="i", required_group="new", tool_schemas=("tool schema",)
        )


def test_context_keeps_interrupted_and_tool_group_atomic():
    items = [
        ContextItem("user", "prior", "interrupted"),
        ContextItem("assistant_tool_call", "call", "tool_group"),
        ContextItem("tool", "x" * 100, "tool_group"),
        ContextItem("user", "now", "current"),
    ]
    selected = ContextBuilder(window=40, output_allowance=5, safety_margin=2).build(
        items, instructions="rules", required_group="current"
    )
    assert [x.group_id for x in selected.items] == ["current"]
    selected = ContextBuilder(window=200, output_allowance=5, safety_margin=2).build(
        items, instructions="rules", required_group="current"
    )
    assert [x.group_id for x in selected.items] == [
        "interrupted",
        "tool_group",
        "tool_group",
        "current",
    ]
    with pytest.raises(RequestFailure, match="contiguous"):
        ContextBuilder().build(
            [items[0], items[-1], items[0]], instructions="", required_group="current"
        )


@pytest.mark.parametrize(
    "events",
    [
        [StreamDelta("partial")],
        [StreamDelta("one"), StreamTerminal(), StreamTerminal()],
        [StreamTerminal(), StreamDelta("late")],
        [StreamDelta("")],
        [StreamTerminal()],
    ],
)
def test_malformed_stream_preserves_interrupted_draft(tmp_path, events):
    class Provider:
        def stream(self, messages, cancel, instructions=""):
            yield from events

    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Malformed")
        app = ConversationService(store, Provider())
        with pytest.raises(RequestFailure):
            app.send(conversation.id, "input", "bad-stream")
        turn = store.get_turns(conversation.id)[0]
        assert turn.status is TurnStatus.FAILED
        assert store.get_answer(turn.id) is None
        draft = store.get_draft(turn.id)
        if events[0] == StreamDelta("partial") or events[0] == StreamDelta("one"):
            assert draft is not None and draft.status == "interrupted"
        else:
            assert draft is None


def test_terminal_error_is_normalized_and_output_limit_is_enforced(tmp_path):
    class ErrorProvider:
        def stream(self, messages, cancel, instructions=""):
            yield StreamDelta("partial")
            yield StreamTerminal("timeout")

    class OversizedProvider:
        def stream(self, messages, cancel, instructions=""):
            yield StreamDelta("x" * 513)
            yield StreamTerminal()

    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Limits")
        with pytest.raises(RequestFailure) as timeout:
            ConversationService(store, ErrorProvider()).send(
                conversation.id, "a", "timeout"
            )
        assert timeout.value.code == "timeout"
        assert store.get_turns(conversation.id)[0].error_code == "timeout"
        with pytest.raises(RequestFailure) as oversized:
            ConversationService(store, OversizedProvider()).send(
                conversation.id, "b", "oversized"
            )
        assert oversized.value.code == "invalid_request"
        assert store.get_draft(store.get_turns(conversation.id)[1].id) is None


def test_slow_consumer_backpressure_and_cancelled_fake_stream(tmp_path):
    class CountingProvider(FakeChatProvider):
        def __init__(self):
            super().__init__()
            self.produced = 0

        def stream(self, messages, cancel, instructions=""):
            for event in super().stream(messages, cancel, instructions):
                self.produced += 1
                yield event

    async def scenario():
        with SqliteStore(tmp_path) as store:
            conversation = store.create_conversation("Slow")
            provider = CountingProvider()
            app = ConversationService(store, provider)
            waiting = asyncio.Event()
            release = asyncio.Event()

            async def slow(_delta):
                waiting.set()
                await release.wait()

            task = asyncio.create_task(
                app.send_async(conversation.id, "input", "slow", on_delta=slow)
            )
            await asyncio.wait_for(waiting.wait(), 2)
            assert provider.produced == 1
            task.cancel()
            release.set()
            with pytest.raises(RequestFailure) as failure:
                await task
            assert failure.value.code == "cancelled"
            turn = store.get_turns(conversation.id)[0]
            assert turn.status is TurnStatus.INTERRUPTED
            assert store.get_draft(turn.id).status == "interrupted"
            assert store.get_answer(turn.id) is None

    asyncio.run(scenario())


def test_explicit_cancellation_signal_marks_draft_interrupted(tmp_path):
    async def scenario():
        with SqliteStore(tmp_path) as store:
            conversation = store.create_conversation("Signal")
            signal = Event()

            async def cancel_after_first(_delta):
                signal.set()

            app = ConversationService(store, FakeChatProvider())
            with pytest.raises(RequestFailure) as failure:
                await app.send_async(
                    conversation.id,
                    "input",
                    "signal-id",
                    on_delta=cancel_after_first,
                    cancel=signal,
                )
            assert failure.value.code == "cancelled"
            turn = store.get_turns(conversation.id)[0]
            assert turn.status is TurnStatus.INTERRUPTED
            assert store.get_draft(turn.id).status == "interrupted"

    asyncio.run(scenario())


def test_oversized_current_input_fails_explicitly_after_durable_submission(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Budget")
        app = ConversationService(
            store,
            FakeChatProvider(),
            context=ContextBuilder(window=30, output_allowance=5, safety_margin=2),
            instructions="rules",
        )
        with pytest.raises(RequestFailure) as failure:
            app.send(conversation.id, "x" * 30, "oversized")
        assert failure.value.code == "context_limit"
        turn = store.get_turns(conversation.id)[0]
        assert turn.error_code == "context_limit"
        assert store.get_messages(conversation.id)[0].text == "x" * 30


def test_prompt_reads_recent_turn_pages_instead_of_full_transcript(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Long history")
        for position in range(40):
            turn, _ = store.begin_turn(conversation.id, "x" * 200, f"seed-{position}")
            store.complete_turn(turn.id, "y" * 200)
        calls = 0
        original = store.get_recent_messages

        def counted(*args):
            nonlocal calls
            calls += 1
            return original(*args)

        store.get_recent_messages = counted
        fake = FakeChatProvider()
        result = ConversationService(store, fake).send(
            conversation.id, "latest", "latest"
        )
        assert result.turn.status is TurnStatus.COMPLETE
        assert fake.requests[0][-1].text == "latest"
        assert len(fake.requests[0]) < 82
        assert calls <= 2


def test_failed_completion_commit_preserves_pending_draft_for_recovery(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Commit error")
        store.run_sync(
            lambda: store._conn.executescript(
                "CREATE TRIGGER reject_reply BEFORE UPDATE OF status ON turns "
                "WHEN NEW.status='complete' BEGIN SELECT RAISE(ABORT, 'injected'); END;"
            )
        )
        app = ConversationService(store, FakeChatProvider())
        with pytest.raises(StorageFailure):
            app.send(conversation.id, "input", "commit-error")
        turn = store.get_turns(conversation.id)[0]
        assert turn.status is TurnStatus.PENDING
        assert store.get_answer(turn.id) is None
        assert store.get_draft(turn.id).status == "draft"
    with SqliteStore(tmp_path) as store:
        assert store.get_turn(turn.id).status is TurnStatus.INTERRUPTED
        assert store.get_draft(turn.id).status == "interrupted"


def test_cancelled_submission_await_resolves_commit(tmp_path):
    async def scenario():
        with SqliteStore(tmp_path) as store:
            conversation = store.create_conversation("Commit")
            entered, release = Event(), Event()
            original = store.begin_turn

            def delayed(*args):
                result = original(*args)
                entered.set()
                assert release.wait(2)
                return result

            store.begin_turn = delayed
            app = ConversationService(store, FakeChatProvider())
            task = asyncio.create_task(
                app.send_async(conversation.id, "input", "commit-id")
            )
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            release.set()
            with pytest.raises(RequestFailure) as failure:
                await task
            assert failure.value.code == "cancelled"
            assert store.get_turns(conversation.id)[0].status is TurnStatus.INTERRUPTED
            assert [m.text for m in store.get_messages(conversation.id)] == ["input"]

    asyncio.run(scenario())


def test_cancelled_completion_await_returns_committed_result(tmp_path):
    async def scenario():
        with SqliteStore(tmp_path) as store:
            conversation = store.create_conversation("Commit reply")
            entered, release = Event(), Event()
            original = store.complete_turn

            def delayed(*args):
                result = original(*args)
                entered.set()
                assert release.wait(2)
                return result

            store.complete_turn = delayed
            app = ConversationService(store, FakeChatProvider())
            task = asyncio.create_task(
                app.send_async(conversation.id, "input", "reply-id")
            )
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            release.set()
            result = await task
            assert result.turn.status is TurnStatus.COMPLETE
            assert result.answer is not None
            assert store.get_draft(result.turn.id) is None

    asyncio.run(scenario())


def test_restart_marks_draft_interrupted_and_upgrade_keeps_m1_data(tmp_path):
    script = (
        resources.files("jarvis")
        .joinpath("infrastructure", "migrations", "001_initial.sql")
        .read_text()
    )
    database = tmp_path / "jarvis.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(script)
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES (1, ?, 'now')",
            (hashlib.sha256(script.encode()).hexdigest(),),
        )
        connection.execute(
            "INSERT INTO conversations VALUES ('old', 'Retained', 'now')"
        )
    with SqliteStore(tmp_path) as store:
        assert store.get_conversation("old").title == "Retained"
        turn, _ = store.begin_turn("old", "input", "draft-id")
        store.checkpoint_draft(turn.id, "partial")
    with SqliteStore(tmp_path) as store:
        assert store.get_turn(turn.id).status is TurnStatus.INTERRUPTED
        assert store.get_draft(turn.id).status == "interrupted"
        assert store.get_draft(turn.id).text == "partial"


def test_changed_m2_migration_refuses_open_without_erasing_data(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Keep M2 data")
    with sqlite3.connect(tmp_path / "jarvis.db") as connection:
        connection.execute(
            "UPDATE schema_migrations SET checksum='wrong' WHERE version=2"
        )
    with pytest.raises(RuntimeError, match="Migration 002 changed"):
        SqliteStore(tmp_path)
    with sqlite3.connect(tmp_path / "jarvis.db") as connection:
        assert (
            connection.execute(
                "SELECT title FROM conversations WHERE id=?", (conversation.id,)
            ).fetchone()[0]
            == "Keep M2 data"
        )


def test_terminal_resume_labels_recovered_draft(tmp_path, capsys):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Recovered")
        turn, _ = store.begin_turn(conversation.id, "input", "resume-id")
        store.checkpoint_draft(turn.id, "partial response")
    assert main(["--data-dir", str(tmp_path), "resume", conversation.id]) == 0
    output = capsys.readouterr().out
    assert "turn 1 [interrupted]" in output
    assert "draft [interrupted]: partial response" in output
    assert "assistant: partial response" not in output


def test_default_diagnostics_exclude_prompt_and_secret(tmp_path):
    diagnostics = Diagnostics(tmp_path)
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Private")
        fake = FakeChatProvider()
        app = ConversationService(
            store, fake, diagnostics=diagnostics, instructions="system-secret-marker"
        )
        app.send(conversation.id, "prompt-secret-marker", "submission-secret-marker")
        assert fake.instructions == ["system-secret-marker"]
    diagnostics.close()
    logs = (tmp_path / "logs" / "diagnostics.jsonl").read_text()
    assert '"event":"turn_complete"' in logs
    assert "prompt-secret-marker" not in logs
    assert "submission-secret-marker" not in logs
    assert "system-secret-marker" not in logs


def test_provider_exception_detail_is_not_logged(tmp_path):
    class BrokenProvider:
        def stream(self, messages, cancel, instructions=""):
            raise RuntimeError("backend-secret-marker")
            yield  # pragma: no cover

    diagnostics = Diagnostics(tmp_path)
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Private error")
        app = ConversationService(store, BrokenProvider(), diagnostics=diagnostics)
        with pytest.raises(RequestFailure) as failure:
            app.send(conversation.id, "input", "error-id")
        assert failure.value.code == "backend_crashed"
    diagnostics.close()
    logs = (tmp_path / "logs" / "diagnostics.jsonl").read_text()
    assert "backend-secret-marker" not in logs
    assert '"code":"backend_crashed"' in logs
