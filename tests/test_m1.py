"""M1 acceptance tests using real temporary SQLite databases."""

import sqlite3

import pytest

from jarvis.application.conversations import ConversationService
from jarvis.bootstrap import main
from jarvis.domain.types import (
    ActiveTurnError,
    ProviderFailure,
    StaleTurnError,
    SubmissionConflict,
    TurnStatus,
)
from jarvis.infrastructure.providers.fake import FakeChatProvider
from jarvis.infrastructure.sqlite_store import SqliteStore


def test_conversations_are_newest_first_including_equal_times_after_restart(
    tmp_path, monkeypatch
):
    with SqliteStore(tmp_path) as store:
        times = iter(
            [
                "2026-10-10T12:00:00+00:00",
                "2026-10-11T12:00:00+00:00",
                "2026-10-11T12:00:00+00:00",
            ]
        )
        ids = iter(["old", "z-recent", "a-newest"])
        with monkeypatch.context() as patch:
            patch.setattr(
                "jarvis.infrastructure.sqlite_store._now", lambda: next(times)
            )
            patch.setattr("jarvis.infrastructure.sqlite_store.uuid4", lambda: next(ids))
            oldest = store.create_conversation("Oldest")
            recent = store.create_conversation("Recent")
            newest = store.create_conversation("Newest")
        expected = [newest, recent, oldest]
        assert store.list_conversations() == expected

    with SqliteStore(tmp_path) as store:
        assert store.list_conversations() == expected


def test_two_turns_then_restart_reconstructs_ordered_history(tmp_path):
    with SqliteStore(tmp_path) as store:
        fake = FakeChatProvider()
        app = ConversationService(store, fake)
        conversation = app.create("Restart test")
        first = app.send(conversation.id, "first", "submission-1")
        second = app.send(conversation.id, "second", "submission-2")

        assert first.turn.status is TurnStatus.COMPLETE
        assert second.turn.status is TurnStatus.COMPLETE
        assert [m.text for m in fake.requests[1]] == [
            "first",
            first.answer.text,
            "second",
        ]
        assert [m.position for m in store.get_messages(conversation.id)] == [1, 2, 3, 4]

    with SqliteStore(tmp_path) as store:
        fake = FakeChatProvider()
        app = ConversationService(store, fake)
        assert [c.id for c in app.list_conversations()] == [conversation.id]
        reopened, turns, messages = app.resume(conversation.id)
        assert reopened.title == "Restart test"
        assert [t.position for t in turns] == [1, 2]
        assert [m.role for m in messages] == ["user", "assistant", "user", "assistant"]
        app.send(conversation.id, "third", "submission-3")
        assert [m.text for m in fake.requests[0]] == [
            "first",
            first.answer.text,
            "second",
            second.answer.text,
            "third",
        ]
        assert [m.turn_status for m in fake.requests[0]] == [
            TurnStatus.COMPLETE
        ] * 4 + [TurnStatus.PENDING]


def test_duplicate_submission_is_idempotent_across_restart(tmp_path):
    with SqliteStore(tmp_path) as store:
        fake = FakeChatProvider()
        app = ConversationService(store, fake)
        conversation = app.create("Duplicate test")
        original = app.send(conversation.id, "same", "stable-id")
        duplicate = app.send(conversation.id, "same", "stable-id")
        assert duplicate.duplicate
        assert duplicate.turn.id == original.turn.id
        assert duplicate.answer == original.answer
        assert len(fake.requests) == 1
        with pytest.raises(SubmissionConflict):
            app.send(conversation.id, "changed", "stable-id")
        assert len(store.get_messages(conversation.id)) == 2

    with SqliteStore(tmp_path) as store:
        fake = FakeChatProvider()
        result = ConversationService(store, fake).send(
            conversation.id, "same", "stable-id"
        )
        assert result.duplicate
        assert result.answer == original.answer
        assert fake.requests == []
        assert len(store.get_turns(conversation.id)) == 1


def test_provider_failure_preserves_user_input_and_failure_status(tmp_path):
    class BrokenProvider:
        def stream(self, messages, cancel, instructions=""):
            raise RuntimeError("injected provider failure")
            yield  # pragma: no cover

    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Failure test")
        app = ConversationService(store, BrokenProvider())
        with pytest.raises(ProviderFailure):
            app.send(conversation.id, "keep this", "failed-id")
        assert [t.status for t in store.get_turns(conversation.id)] == [
            TurnStatus.FAILED
        ]
        assert [m.text for m in store.get_messages(conversation.id)] == ["keep this"]

    with SqliteStore(tmp_path) as store:
        fake = FakeChatProvider()
        app = ConversationService(store, fake)
        app.send(conversation.id, "next", "next-id")
        assert [(m.text, m.turn_status) for m in fake.requests[0]] == [
            ("keep this", TurnStatus.FAILED),
            ("next", TurnStatus.PENDING),
        ]


def test_crash_after_submission_marks_interrupted_without_replay(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Interrupted test")
        pending, created = store.begin_turn(
            conversation.id, "stored first", "interrupted-id"
        )
        assert created and pending.status is TurnStatus.PENDING
        assert [m.text for m in store.get_messages(conversation.id)] == ["stored first"]

    with SqliteStore(tmp_path) as store:
        assert [t.status for t in store.get_turns(conversation.id)] == [
            TurnStatus.INTERRUPTED
        ]
        with pytest.raises(StaleTurnError):
            store.complete_turn(pending.id, "late answer")
        fake = FakeChatProvider()
        app = ConversationService(store, fake)
        old = app.send(conversation.id, "stored first", "interrupted-id")
        assert old.duplicate and old.answer is None
        assert old.turn.status is TurnStatus.INTERRUPTED
        assert fake.requests == []
        app.send(conversation.id, "new input", "new-id")
        assert [m.turn_status for m in fake.requests[0]] == [
            TurnStatus.INTERRUPTED,
            TurnStatus.PENDING,
        ]


def test_submission_rolls_back_turn_if_user_message_insert_fails(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Submission rollback")
        store.run_sync(
            lambda: store._conn.executescript(
                "CREATE TRIGGER reject_user BEFORE INSERT ON messages "
                "WHEN NEW.role='user' BEGIN SELECT RAISE(ABORT, 'injected'); END;"
            )
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.begin_turn(conversation.id, "must roll back", "rollback-id")
        assert store.get_turns(conversation.id) == []
        assert store.get_messages(conversation.id) == []
        store.run_sync(lambda: store._conn.execute("DROP TRIGGER reject_user"))
        ConversationService(store, FakeChatProvider()).send(
            conversation.id, "must roll back", "rollback-id"
        )
        assert len(store.get_turns(conversation.id)) == 1


def test_completion_rolls_back_reply_if_turn_update_cannot_finish(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Completion rollback")
        turn, _ = store.begin_turn(conversation.id, "input", "completion-id")
        store.run_sync(
            lambda: store._conn.executescript(
                "CREATE TRIGGER reject_completion BEFORE UPDATE OF status ON turns "
                "WHEN NEW.status='complete' BEGIN SELECT RAISE(ABORT, 'injected'); END;"
            )
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.complete_turn(turn.id, "reply that must roll back")
        assert [m.role for m in store.get_messages(conversation.id)] == ["user"]
        assert store.get_turns(conversation.id)[0].status is TurnStatus.PENDING
        store.run_sync(lambda: store._conn.execute("DROP TRIGGER reject_completion"))
        answer = store.complete_turn(turn.id, "actual reply")
        assert answer.text == "actual reply"
        with pytest.raises(StaleTurnError):
            store.complete_turn(turn.id, "stale duplicate")
        assert [m.position for m in store.get_messages(conversation.id)] == [1, 2]


def test_only_one_instance_may_open_a_directory(tmp_path):
    with SqliteStore(tmp_path) as first:
        conversation = first.create_conversation("Lock test")
        pending, _ = first.begin_turn(conversation.id, "still running", "lock-id")
        with pytest.raises(ActiveTurnError):
            first.begin_turn(conversation.id, "another input", "other-id")
        repeated, created = first.begin_turn(
            conversation.id, "still running", "lock-id"
        )
        assert not created and repeated.id == pending.id
        with pytest.raises(RuntimeError, match="Another Jarvis instance"):
            SqliteStore(tmp_path)
        assert first.get_turns(conversation.id)[0].status is TurnStatus.PENDING
    with SqliteStore(tmp_path) as reopened:
        assert reopened.get_turns(conversation.id)[0].id == pending.id
        assert reopened.get_turns(conversation.id)[0].status is TurnStatus.INTERRUPTED


def test_migration_checksum_mismatch_does_not_reset_data(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Keep me")
    with sqlite3.connect(tmp_path / "jarvis.db") as connection:
        connection.execute(
            "UPDATE schema_migrations SET checksum='wrong' WHERE version=1"
        )
    with pytest.raises(RuntimeError, match="Migration 001 changed"):
        SqliteStore(tmp_path)
    with sqlite3.connect(tmp_path / "jarvis.db") as connection:
        assert (
            connection.execute(
                "SELECT title FROM conversations WHERE id=?", (conversation.id,)
            ).fetchone()[0]
            == "Keep me"
        )


def test_terminal_create_list_send_resume_uses_same_persistent_data(tmp_path, capsys):
    data_args = ["--data-dir", str(tmp_path)]
    assert main(data_args + ["create", "CLI test"]) == 0
    conversation_id = capsys.readouterr().out.strip()
    assert (
        main(data_args + ["send", conversation_id, "Hello", "--submission-id", "cli-1"])
        == 0
    )
    first_output = capsys.readouterr().out
    assert "context messages: 1" in first_output
    assert (
        main(data_args + ["send", conversation_id, "Again", "--submission-id", "cli-2"])
        == 0
    )
    assert "context messages: 3" in capsys.readouterr().out
    assert main(data_args + ["list"]) == 0
    assert conversation_id in capsys.readouterr().out
    assert main(data_args + ["resume", conversation_id]) == 0
    transcript = capsys.readouterr().out
    assert "turn 1 [complete]" in transcript
    assert "turn 2 [complete]" in transcript
    assert "user: Hello" in transcript
    assert "user: Again" in transcript
