"""Atomic chat deletion, repeat identity and safe artifact cleanup."""

import asyncio
import hashlib
import sqlite3
from pathlib import Path
from threading import Event

import httpx
import pytest

from jarvis.application.conversations import ConversationService
from jarvis.application.jobs import JobRunner
from jarvis.application.retrieval import RetrievalService
from jarvis.domain.types import ActiveTurnError, ConversationNotFound
from jarvis.infrastructure.providers.fake import FakeChatProvider
from jarvis.infrastructure.read_tools import SafeDocumentReader
from jarvis.infrastructure.sqlite_store import SqliteStore
from jarvis.interfaces.web import create_web_app


def sql(store, statement, args=()):
    return store.run_sync(lambda: store._conn.execute(statement, args).fetchall())


class CountingFake(FakeChatProvider):
    def count_output_tokens(self, text):
        return len(text)


def test_delete_versions_drafts_memory_statistics_and_preserve_other_chat(tmp_path):
    async def scenario():
        with SqliteStore(tmp_path / "data") as store:
            chat, other = (
                store.create_conversation("Delete"),
                store.create_conversation("Keep"),
            )
            provider = CountingFake()
            service = ConversationService(store, provider)
            runner = JobRunner(store, service)
            job, _ = await runner.submit(chat.id, "Question", "submission")
            await runner.run_job(job.id)
            version, _ = await runner.regenerate(chat.id, job.turn_id, "version")
            await runner.run_job(version.id)
            failed, _ = store.begin_turn(chat.id, "Failed", "failed")
            store.checkpoint_draft(failed.id, "Partial text")
            store.fail_turn(failed.id)
            keep, _ = store.begin_turn(other.id, "Keep this", "keep")
            store.complete_turn(keep.id, "Unchanged answer")
            retrieval = RetrievalService(store, SafeDocumentReader(store.data_dir))
            retrieval.add_memory("Private note", chat.id)
            global_note = retrieval.add_memory("Global note")
            docs = tmp_path / "docs"
            docs.mkdir()
            original = docs / "original.txt"
            original.write_text("Source file must survive", encoding="utf-8")
            root = retrieval.grant_root(docs, chat.id)
            retrieval.import_document(root.id, original)
            before = store.get_messages(other.id)
            assert store.get_statistics(chat.id).output_tokens > 0
            await service.delete(chat.id)
            await service.delete(chat.id)
            assert store.list_conversations() == [other]
            assert store.get_messages(other.id) == before
            assert (
                store.get_statistics().user_messages
                == store.get_statistics().assistant_messages
                == 1
            )
            for table in (
                "turns",
                "messages",
                "tool_invocations",
                "jobs",
                "memories",
                "retrieval_roots",
            ):
                assert not sql(
                    store, f"SELECT 1 FROM {table} WHERE conversation_id=?", (chat.id,)
                )
            for table in (
                "turn_drafts",
                "text_token_counts",
                "job_steps",
                "retrieval_documents",
            ):
                assert not sql(store, f"SELECT 1 FROM {table}")
            assert sql(store, "SELECT id FROM memories")[0][0] == global_note.id
            assert not sql(
                store, "SELECT 1 FROM retrieval_fts WHERE conversation_id=?", (chat.id,)
            )
            assert original.read_text(encoding="utf-8") == "Source file must survive"
            with pytest.raises(ConversationNotFound):
                await runner.submit(chat.id, "Question", "submission")
            assert len(provider.requests) == 2
            assert not sql(store, "PRAGMA foreign_key_check")
            return chat.id, other.id

    removed, remaining = asyncio.run(scenario())
    with SqliteStore(tmp_path / "data") as store:
        store.delete_conversation(removed)
        assert [c.id for c in store.list_conversations()] == [remaining]
        with pytest.raises(ConversationNotFound):
            store.delete_conversation("missing")


@pytest.mark.parametrize(
    "state",
    ["pending", "queued", "running", "waiting_approval", "cancel_requested", "unknown"],
)
def test_active_or_unacknowledged_work_blocks_deletion(tmp_path, state):
    with SqliteStore(tmp_path) as store:
        chat = store.create_conversation("Busy")
        turn, _ = store.begin_turn(
            chat.id,
            "Question",
            "one",
            None if state in {"pending", "unknown"} else "job",
        )
        if state == "unknown":
            invocation = store.record_tool_proposal(
                turn.id,
                "call",
                "write_file",
                1,
                "{}",
                "digest",
                "policy",
                "target",
                "allow",
            )
            sql(
                store,
                "UPDATE tool_invocations SET status='outcome_unknown' WHERE id=?",
                (invocation.id,),
            )
            store.fail_turn(turn.id)
        elif state != "pending":
            sql(store, "UPDATE jobs SET status=? WHERE id='job'", (state,))
            sql(store, "UPDATE turns SET status='interrupted' WHERE id=?", (turn.id,))
        with pytest.raises(ActiveTurnError):
            store.delete_conversation(chat.id)
        assert store.get_conversation(chat.id) == chat
        assert not store.is_conversation_deleted(chat.id)


def test_rollback_preserves_history(tmp_path):
    with SqliteStore(tmp_path) as store:
        chat = store.create_conversation("Rollback")
        turn, _ = store.begin_turn(chat.id, "Question", "one")
        store.complete_turn(turn.id, "Answer")
        before = store.get_messages(chat.id)
        sql(
            store,
            "CREATE TRIGGER reject_delete BEFORE DELETE ON conversations BEGIN SELECT RAISE(ABORT, 'injected'); END;",
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.delete_conversation(chat.id)
        assert store.get_messages(chat.id) == before
        assert not store.is_conversation_deleted(chat.id)
        assert not sql(store, "PRAGMA foreign_key_check")


def test_v12_migration_failure_rolls_back_without_changing_history(tmp_path):
    with SqliteStore(tmp_path) as store:
        chat = store.create_conversation("Existing")
        turn, _ = store.begin_turn(chat.id, "Existing input", "one")
        store.complete_turn(turn.id, "Existing answer")
        before = store.get_messages(chat.id)
    with sqlite3.connect(tmp_path / "jarvis.db") as db:
        db.executescript(
            "DROP TABLE deleted_artifact_files; DROP TABLE deleted_conversations; DELETE FROM schema_migrations WHERE version=13; CREATE TRIGGER reject_migration BEFORE INSERT ON schema_migrations WHEN NEW.version=13 BEGIN SELECT RAISE(ABORT,'injected'); END;"
        )
    with pytest.raises(sqlite3.IntegrityError):
        SqliteStore(tmp_path)
    with sqlite3.connect(tmp_path / "jarvis.db") as db:
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name='deleted_conversations'"
        ).fetchall()
        db.execute("DROP TRIGGER reject_migration")
    with SqliteStore(tmp_path) as store:
        assert store.get_messages(chat.id) == before


def test_image_vision_tool_rows_deleted_atomically(tmp_path):
    with SqliteStore(tmp_path) as store:
        chat = store.create_conversation("Attachments")
        turn, _ = store.begin_turn(chat.id, "Image", "one", "job")
        store.start_job("job")
        invocation = store.record_tool_proposal(
            turn.id,
            "call",
            "generate_image",
            1,
            "{}",
            "digest",
            "policy",
            "target",
            "allow",
            1,
        )
        sql(
            store,
            "UPDATE tool_invocations SET status='complete' WHERE id=?",
            (invocation.id,),
        )
        store.complete_turn(turn.id, "Image", 1)
        relative = "artifacts/job/image.png"
        path = tmp_path / relative
        path.parent.mkdir(parents=True)
        path.write_bytes(b"png")
        sql(
            store,
            "INSERT INTO artifacts VALUES ('image','job',?,'image/png',3,?,'available','now')",
            (relative, hashlib.sha256(b"png").hexdigest()),
        )
        sql(
            store,
            "UPDATE image_steps SET status='complete',artifact_id='image' WHERE invocation_id=?",
            (invocation.id,),
        )
        sql(
            store, "INSERT INTO vision_inputs VALUES (?,'image','fake',1,1)", (turn.id,)
        )
        sql(
            store,
            "UPDATE job_steps SET status='complete' WHERE invocation_id=?",
            (invocation.id,),
        )
        store.delete_conversation(chat.id)
        for table in (
            "tool_invocations",
            "image_steps",
            "vision_inputs",
            "artifacts",
            "jobs",
            "job_steps",
        ):
            assert not sql(store, f"SELECT * FROM {table}")
        assert not path.exists()
        assert not sql(store, "PRAGMA foreign_key_check")


def test_cancelled_delete_await_verifies_commit(tmp_path, monkeypatch):
    async def scenario():
        with SqliteStore(tmp_path) as store:
            chat = store.create_conversation("Cancel await")
            service = ConversationService(store, FakeChatProvider())
            entered, release = Event(), Event()
            original = store.delete_conversation

            def delayed(identity):
                original(identity)
                entered.set()
                assert release.wait(3)

            monkeypatch.setattr(store, "delete_conversation", delayed)
            task = asyncio.create_task(service.delete(chat.id))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                task.cancel()
            finally:
                release.set()
            assert await task is None
            assert store.is_conversation_deleted(chat.id)

    asyncio.run(scenario())


def test_artifact_cleanup_retries_after_restart_and_rejects_replacement(
    tmp_path, monkeypatch
):
    data = tmp_path / "data"
    relative = "artifacts/job/image.png"
    path = data / relative
    with SqliteStore(data) as store:
        chat = store.create_conversation("Image")
        turn, _ = store.begin_turn(chat.id, "Image", "one", "job")
        store.start_job("job")
        store.complete_turn(turn.id, "Image", 1)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"original")
        sql(
            store,
            "INSERT INTO artifacts VALUES ('image','job',?,'image/png',8,?,'available','now')",
            (relative, hashlib.sha256(b"original").hexdigest()),
        )
        unlink = Path.unlink

        def locked(target, *args, **kwargs):
            if target == path:
                raise PermissionError("Simulated open file")
            return unlink(target, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "unlink", locked)
            store.delete_conversation(chat.id)
        assert path.exists()
        assert len(sql(store, "SELECT * FROM deleted_artifact_files")) == 1
    path.write_bytes(b"replacement")
    with SqliteStore(data) as store:
        assert path.read_bytes() == b"replacement"
    path.write_bytes(b"original")
    with SqliteStore(data) as store:
        assert not path.exists()
        assert not sql(store, "SELECT * FROM deleted_artifact_files")


def test_delete_api_auth_csrf_missing_retry_busy_and_history(tmp_path):
    async def scenario():
        with SqliteStore(tmp_path) as store:
            chat = store.create_conversation("Delete")
            service = ConversationService(store, FakeChatProvider())
            app, token = create_web_app(service, JobRunner(store, service), None, 8765)
            base = "http://127.0.0.1:8765"
            path = f"/api/conversations/{chat.id}"
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=base
            ) as client:

                async def remove(path, headers=None):
                    return await client.request(
                        "DELETE", path, json={}, headers=headers
                    )

                assert (await remove(path)).status_code == 401
                await client.post("/api/setup", json={"token": token})
                csrf = (await client.get("/api/session")).json()["csrf_token"]
                assert (await remove(path)).status_code == 403
                headers = {"x-csrf-token": csrf, "origin": base}
                assert (
                    await remove(
                        path, headers={**headers, "origin": "https://evil.test"}
                    )
                ).status_code == 403
                assert (
                    await remove("/api/conversations/missing", headers=headers)
                ).status_code == 404
                turn, _ = store.begin_turn(chat.id, "Busy", "one")
                assert (await remove(path, headers=headers)).status_code == 409
                store.interrupt_turn(turn.id)
                for _ in range(2):
                    assert (await remove(path, headers=headers)).json() == {
                        "deleted": True
                    }
                assert (await client.get(path)).status_code == 404
                assert (await client.get("/api/conversations")).json() == {
                    "conversations": []
                }

    asyncio.run(scenario())
