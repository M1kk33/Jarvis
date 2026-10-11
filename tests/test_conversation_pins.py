"""Persistent pins, safe migration, committed outcomes and authenticated changes."""

import asyncio
import sqlite3
from contextlib import closing
from threading import Event

import httpx
import pytest

from jarvis.application.conversations import ConversationService
from jarvis.application.jobs import JobRunner
from jarvis.domain.types import ConversationNotFound, TurnStatus
from jarvis.infrastructure.backup import create_backup, restore_backup
from jarvis.infrastructure.providers.fake import FakeChatProvider
from jarvis.infrastructure.sqlite_store import SqliteStore
from jarvis.interfaces.web import create_web_app


def test_pins_survive_restart_backup_and_unpin_restores_creation_order(tmp_path):
    data = tmp_path / "data"
    with SqliteStore(data) as store:
        oldest = store.create_conversation("Oldest")
        middle = store.create_conversation("Middle")
        newest = store.create_conversation("Newest")
        for _ in range(2):
            assert store.set_conversation_pinned(oldest.id, True).pinned
        store.set_conversation_pinned(middle.id, True)
        assert [c.id for c in store.list_conversations()] == [
            middle.id,
            oldest.id,
            newest.id,
        ]
        backup = create_backup(store)
    with SqliteStore(data) as store:
        assert store.get_conversation(oldest.id).pinned
        store.set_conversation_pinned(oldest.id, False)
        store.set_conversation_pinned(middle.id, False)
        assert [c.id for c in store.list_conversations()] == [
            newest.id,
            middle.id,
            oldest.id,
        ]
        with pytest.raises(ConversationNotFound):
            store.set_conversation_pinned("missing", True)
        with pytest.raises(ValueError):
            store.set_conversation_pinned(oldest.id, "true")
    restored = tmp_path / "restored"
    restore_backup(backup, restored)
    with SqliteStore(restored) as store:
        assert [c.id for c in store.list_conversations() if c.pinned] == [
            middle.id,
            oldest.id,
        ]


def test_v8_upgrade_preserves_complete_failed_and_incomplete_history(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Keep history")
        complete, _ = store.begin_turn(conversation.id, "First", "one")
        store.complete_turn(complete.id, "Reply")
        failed, _ = store.begin_turn(conversation.id, "Failed", "two")
        store.fail_turn(failed.id, "unavailable")
        pending, _ = store.begin_turn(conversation.id, "Incomplete", "three")
        messages = store.get_messages(conversation.id)
    with closing(sqlite3.connect(tmp_path / "jarvis.db")) as db:
        db.execute("ALTER TABLE conversations DROP COLUMN pinned")
        db.execute("DELETE FROM schema_migrations WHERE version=9")
        db.commit()
    with SqliteStore(tmp_path) as store:
        assert not store.get_conversation(conversation.id).pinned
        assert [m.text for m in store.get_messages(conversation.id)] == [
            m.text for m in messages
        ]
        assert [t.status for t in store.get_turns(conversation.id)] == [
            TurnStatus.COMPLETE,
            TurnStatus.FAILED,
            TurnStatus.INTERRUPTED,
        ]
        store.set_conversation_pinned(conversation.id, True)
        assert [t.id for t in store.get_turns(conversation.id)] == [
            complete.id,
            failed.id,
            pending.id,
        ]


def test_pin_commit_failure_rolls_back(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Rollback")
        store.run_sync(
            lambda: store._conn.executescript(
                "CREATE TRIGGER reject_pin BEFORE UPDATE OF pinned ON conversations BEGIN SELECT RAISE(ABORT, 'injected'); END;"
            )
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.set_conversation_pinned(conversation.id, True)
        assert not store.get_conversation(conversation.id).pinned


def test_failed_pin_migration_rolls_back_ddl_and_retries_without_data_loss(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Keep after failed migration")
    with closing(sqlite3.connect(tmp_path / "jarvis.db")) as db:
        db.execute("ALTER TABLE conversations DROP COLUMN pinned")
        db.execute("DELETE FROM schema_migrations WHERE version=9")
        db.execute(
            "CREATE TRIGGER reject_pin_migration BEFORE INSERT ON schema_migrations "
            "WHEN NEW.version=9 BEGIN SELECT RAISE(ABORT, 'injected'); END;"
        )
        db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        SqliteStore(tmp_path)
    with closing(sqlite3.connect(tmp_path / "jarvis.db")) as db:
        assert "pinned" not in [
            row[1] for row in db.execute("PRAGMA table_info(conversations)")
        ]
        assert (
            db.execute(
                "SELECT count(*) FROM schema_migrations WHERE version=9"
            ).fetchone()[0]
            == 0
        )
        db.execute("DROP TRIGGER reject_pin_migration")
        db.commit()
    with SqliteStore(tmp_path) as store:
        assert store.get_conversation(conversation.id).title == conversation.title
        assert not store.get_conversation(conversation.id).pinned
        assert store.set_conversation_pinned(conversation.id, True).pinned


def test_cancelled_pin_await_verifies_committed_state(tmp_path, monkeypatch):
    async def scenario():
        with SqliteStore(tmp_path) as store:
            conversation = store.create_conversation("Cancel await")
            provider = FakeChatProvider()
            service = ConversationService(store, provider)
            entered, release = Event(), Event()
            original = store.set_conversation_pinned

            def delayed(*args):
                result = original(*args)
                entered.set()
                assert release.wait(3)
                return result

            monkeypatch.setattr(store, "set_conversation_pinned", delayed)
            task = asyncio.create_task(service.set_pinned(conversation.id, True))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                task.cancel()
            finally:
                release.set()
            assert (await task).pinned
            assert store.get_conversation(conversation.id).pinned
            assert provider.requests == []

    asyncio.run(scenario())


def test_pin_api_requires_auth_csrf_and_strict_boolean(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("API")
        provider = FakeChatProvider()
        service = ConversationService(store, provider)
        app, token = create_web_app(service, JobRunner(store, service), None, 8765)
        base = "http://127.0.0.1:8765"
        path = f"/api/conversations/{conversation.id}/pin"

        async def scenario():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=base
            ) as client:
                assert (
                    await client.post(path, json={"pinned": True})
                ).status_code == 401
                await client.post("/api/setup", json={"token": token})
                csrf = (await client.get("/api/session")).json()["csrf_token"]
                assert (
                    await client.post(path, json={"pinned": True})
                ).status_code == 403
                headers = {"x-csrf-token": csrf, "origin": base}
                assert (
                    await client.post(
                        path,
                        json={"pinned": True},
                        headers={**headers, "origin": "https://evil.test"},
                    )
                ).status_code == 403
                for payload in (
                    {"pinned": "true"},
                    {"pinned": 1},
                    {},
                    {"pinned": True, "extra": 1},
                ):
                    assert (
                        await client.post(path, json=payload, headers=headers)
                    ).status_code == 422
                assert not store.get_conversation(conversation.id).pinned
                assert (
                    await client.post(
                        "/api/conversations/missing/pin",
                        json={"pinned": True},
                        headers=headers,
                    )
                ).status_code == 404
                for pinned in (True, True, False):
                    response = await client.post(
                        path, json={"pinned": pinned}, headers=headers
                    )
                    assert response.status_code == 200
                    assert response.json()["pinned"] is pinned
                    assert (await client.get("/api/conversations")).json()[
                        "conversations"
                    ][0]["pinned"] is pinned
                assert store.get_messages(conversation.id) == []
                assert provider.requests == []

        asyncio.run(scenario())
