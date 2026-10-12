"""Durable reply versions preserve history, context groups and submission identity."""

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path
from threading import Event

import httpx
import pytest

from jarvis.application.conversations import ConversationService
from jarvis.application.jobs import JobRunner
from jarvis.application.vision import VisionService
from jarvis.domain.jobs import JobStatus
from jarvis.domain.types import (
    ActiveTurnError,
    StorageFailure,
    SubmissionConflict,
    TurnStatus,
)
from jarvis.infrastructure.providers.fake import FakeChatProvider
from jarvis.infrastructure.sqlite_store import SqliteStore
from jarvis.interfaces.web import create_web_app


class VersionProvider(FakeChatProvider):
    def _reply(self, messages):
        return super()._reply(messages) + f" version {len(self.requests)}"


def test_regeneration_reuses_original_context_and_latest_version_after_restart(
    tmp_path,
):
    async def scenario():
        with SqliteStore(tmp_path) as store:
            conversation = store.create_conversation("Versions")
            provider = VersionProvider()
            runner = JobRunner(store, ConversationService(store, provider))
            first, _ = await runner.submit(conversation.id, "Before", "first")
            await runner.run_job(first.id)
            source, _ = await runner.submit(conversation.id, "Question", "source")
            await runner.run_job(source.id)
            original = store.get_messages(conversation.id)
            replacement, duplicate = await runner.regenerate(
                conversation.id, source.turn_id, "retry"
            )
            assert not duplicate
            assert store.get_turn(replacement.turn_id).regeneration_of == source.turn_id
            assert store.get_answer(source.turn_id) == original[-1]
            await runner.run_job(replacement.id)
            assert [(m.role, m.text) for m in provider.requests[1]] == [
                (m.role, m.text) for m in provider.requests[2]
            ]
            assert store.get_messages(conversation.id)[:4] == original
            duplicate, repeated = await runner.regenerate(
                conversation.id, source.turn_id, "retry"
            )
            assert repeated and duplicate.id == replacement.id
            await runner.run_job(duplicate.id)
            assert len(provider.requests) == 3
            assert len(store.get_turns(conversation.id)) == 3
            records, _ = store.get_recent_context(conversation.id, None)
            assert {r.turn_id for r in records} == {first.turn_id, replacement.turn_id}
            return conversation, source, replacement

    conversation, source, replacement = asyncio.run(scenario())
    with SqliteStore(tmp_path) as store:
        provider = VersionProvider()
        service = ConversationService(store, provider)
        service.send(conversation.id, "Continue", "next")
        assert [m.turn_id for m in provider.requests[0]][-3:] == [
            replacement.turn_id,
            replacement.turn_id,
            store.get_turn_by_submission(conversation.id, "next").id,
        ]
        assert source.turn_id not in {m.turn_id for m in provider.requests[0]}
        assert len(store.get_messages(conversation.id)) == 8
        # Paging counts whole logical prompts, even when their version has a later position.
        page, cursor = store.get_recent_messages(conversation.id, None, 1)
        previous, _ = store.get_recent_messages(conversation.id, cursor, 1)
        assert (
            len(page) == 2
            and [m.turn_id for m in previous] == [replacement.turn_id] * 2
        )


def test_regeneration_rejects_old_other_incomplete_and_conflicting_submissions(
    tmp_path,
):
    async def scenario():
        with SqliteStore(tmp_path) as store:
            conversation = store.create_conversation("Versions")
            other = store.create_conversation("Other")
            runner = JobRunner(store, ConversationService(store, FakeChatProvider()))
            source, _ = await runner.submit(conversation.id, "Question", "source")
            with pytest.raises(SubmissionConflict):
                await runner.regenerate(conversation.id, source.turn_id, "incomplete")
            await runner.run_job(source.id)
            for target, identity in ((other.id, "other"), (conversation.id, "source")):
                with pytest.raises(SubmissionConflict):
                    await runner.regenerate(target, source.turn_id, identity)
            replacement, _ = await runner.regenerate(
                conversation.id, source.turn_id, "retry"
            )
            with pytest.raises(ActiveTurnError):
                await runner.regenerate(conversation.id, source.turn_id, "another")
            with pytest.raises(SubmissionConflict):
                await runner.submit(conversation.id, "Question", "retry")
            await runner.run_job(replacement.id)
            with pytest.raises(SubmissionConflict):
                await runner.regenerate(conversation.id, replacement.turn_id, "retry")
            next_job, _ = await runner.submit(conversation.id, "Continue", "next")
            await runner.run_job(next_job.id)
            with pytest.raises(SubmissionConflict):
                await runner.regenerate(conversation.id, source.turn_id, "too-old")
            # Retrying an already committed operation remains idempotent after continuation.
            repeated, duplicate = await runner.regenerate(
                conversation.id, source.turn_id, "retry"
            )
            assert duplicate and repeated.id == replacement.id

    asyncio.run(scenario())


def test_failed_version_retains_draft_and_original_complete_context(tmp_path):
    async def scenario():
        with SqliteStore(tmp_path) as store:
            conversation = store.create_conversation("Versions")
            runner = JobRunner(store, ConversationService(store, FakeChatProvider()))
            source, _ = await runner.submit(conversation.id, "Question", "source")
            await runner.run_job(source.id)
            replacement, _ = await runner.regenerate(
                conversation.id, source.turn_id, "retry"
            )
            store.start_job(replacement.id)
            store.checkpoint_draft(replacement.turn_id, "Partial version", 1)
            store.fail_turn(replacement.turn_id, "unavailable", 1)
            assert store.get_draft(replacement.turn_id).text == "Partial version"
            assert store.get_turn(replacement.turn_id).status is TurnStatus.FAILED
            page, _ = store.get_recent_messages(conversation.id, None)
            assert {m.turn_id for m in page} == {source.turn_id}
            followup, _ = await runner.submit(conversation.id, "Continue", "next")
            await runner.run_job(followup.id)
            assert len(store.get_turns(conversation.id)) == 3

    asyncio.run(scenario())


def test_regeneration_cancelled_await_verifies_commit_and_rollback_is_atomic(
    tmp_path, monkeypatch
):
    async def scenario():
        with SqliteStore(tmp_path) as store:
            conversation = store.create_conversation("Versions")
            runner = JobRunner(store, ConversationService(store, FakeChatProvider()))
            source, _ = await runner.submit(conversation.id, "Question", "source")
            await runner.run_job(source.id)
            original = store.begin_turn
            entered, release = Event(), Event()

            def delayed(*args):
                result = original(*args)
                entered.set()
                assert release.wait(3)
                return result

            monkeypatch.setattr(store, "begin_turn", delayed)
            task = asyncio.create_task(
                runner.regenerate(conversation.id, source.turn_id, "retry")
            )
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                task.cancel()
            finally:
                release.set()
            replacement, duplicate = await task
            assert duplicate and replacement.status is JobStatus.QUEUED
            assert len(store.get_turns(conversation.id)) == 2
            monkeypatch.setattr(store, "begin_turn", original)
            await runner.run_job(replacement.id)
            before = store.get_messages(conversation.id)
            store.run_sync(
                lambda: store._conn.executescript(
                    "CREATE TRIGGER reject_version BEFORE INSERT ON jobs BEGIN SELECT RAISE(ABORT, 'injected'); END;"
                )
            )
            with pytest.raises(StorageFailure):
                await runner.regenerate(conversation.id, source.turn_id, "rollback")
            assert store.get_messages(conversation.id) == before
            assert len(store.get_turns(conversation.id)) == 2

    asyncio.run(scenario())


def test_vision_regeneration_copies_verified_source_without_losing_original(tmp_path):
    async def scenario():
        with SqliteStore(tmp_path) as store:
            conversation = store.create_conversation("Screenshot")
            vision = VisionService(store, None, None, None, "vision")
            image = Path("tests/fixtures/m12/settings.png").read_bytes()
            source_input = await vision.stage("source-job", image)
            source, _ = store.begin_turn(
                conversation.id,
                "What is shown?",
                "source",
                "source-job",
                "vision",
                source_input,
            )
            store.start_job("source-job")
            store.complete_turn(source.id, "Settings", 1)
            runner = JobRunner(
                store, ConversationService(store, FakeChatProvider()), vision=vision
            )
            replacement, _ = await runner.regenerate(
                conversation.id, source.id, "retry"
            )
            copied = store.get_vision_input(replacement.turn_id)
            assert copied.artifact_id != source_input.artifact_id
            assert store.get_artifact_bytes(copied.artifact_id) == image
            assert store.get_artifact_bytes(source_input.artifact_id) == image
            repeated, duplicate = await runner.regenerate(
                conversation.id, source.id, "retry"
            )
            assert duplicate and repeated.id == replacement.id

    asyncio.run(scenario())


def test_reply_version_migration_rolls_back_and_preserves_v10_data(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Keep")
        service = ConversationService(store, FakeChatProvider())
        service.send(conversation.id, "Question", "source")
        before = store.get_messages(conversation.id)
    with closing(sqlite3.connect(tmp_path / "jarvis.db")) as db:
        db.executescript(
            "DROP INDEX turns_reply_versions; ALTER TABLE turns DROP COLUMN regeneration_of; ALTER TABLE turns DROP COLUMN regeneration_source; DELETE FROM schema_migrations WHERE version=11; CREATE TRIGGER reject_versions BEFORE INSERT ON schema_migrations WHEN NEW.version=11 BEGIN SELECT RAISE(ABORT, 'injected'); END;"
        )
    with pytest.raises(sqlite3.IntegrityError):
        SqliteStore(tmp_path)
    with closing(sqlite3.connect(tmp_path / "jarvis.db")) as db:
        assert "regeneration_of" not in {
            r[1] for r in db.execute("PRAGMA table_info(turns)")
        }
        db.execute("DROP TRIGGER reject_versions")
        db.commit()
    with SqliteStore(tmp_path) as store:
        assert store.get_messages(conversation.id) == before
        assert all(t.regeneration_of is None for t in store.get_turns(conversation.id))


def test_regeneration_endpoint_requires_session_csrf_and_launches_once(tmp_path):
    async def scenario():
        with SqliteStore(tmp_path) as store:
            conversation = store.create_conversation("Versions")
            provider = FakeChatProvider()
            service = ConversationService(store, provider)
            source = await service.send_async(conversation.id, "Question", "source")
            runner = JobRunner(store, service)
            app, token = create_web_app(service, runner, None, 8765)
            route = f"/api/conversations/{conversation.id}/turns/{source.turn.id}/regenerate"
            payload = {"submission_id": "retry"}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
            ) as client:
                assert (await client.post(route, json=payload)).status_code == 401
                await client.post("/api/setup", json={"token": token})
                assert (await client.post(route, json=payload)).status_code == 403
                session = (await client.get("/api/session")).json()
                headers = {"x-csrf-token": session["csrf_token"]}
                assert (
                    await client.post(
                        route, json={**payload, "text": "Changed"}, headers=headers
                    )
                ).status_code == 422
                response = await client.post(route, json=payload, headers=headers)
                assert response.status_code == 202
                await runner.wait_for_idle()
                repeated = await client.post(route, json=payload, headers=headers)
                assert repeated.status_code == 200 and repeated.json()["duplicate"]
                assert repeated.json()["job"]["id"] == response.json()["job"]["id"]
                assert len(provider.requests) == 2

    asyncio.run(scenario())
