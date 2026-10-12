"""Statistics over durable history and exact, optional saved-text token counts."""

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import asdict
from threading import Event

import httpx
import pytest

from jarvis.application.conversations import ConversationService
from jarvis.application.jobs import JobRunner
from jarvis.application.statistics import StatisticsService, checkpoint_text_tokens
from jarvis.domain.jobs import JobStatus
from jarvis.domain.types import StaleTurnError, SubmissionConflict
from jarvis.infrastructure.backup import create_backup, restore_backup
from jarvis.infrastructure.providers.fake import FakeChatProvider
from jarvis.infrastructure.sqlite_store import SqliteStore
from jarvis.interfaces.web import create_web_app


class CountingFake(FakeChatProvider):
    """This fixture's exact tokenizer is one token per Unicode code point."""

    def count_output_tokens(self, text):
        return len(text)


def test_old_history_unknown_empty_and_read_only_statistics(tmp_path):
    with SqliteStore(tmp_path) as store:
        assert store.get_statistics().input_tokens is None
        assert store.get_statistics().user_messages == 0
        chat = store.create_conversation("Old")
        turn, _ = store.begin_turn(chat.id, "Привет", "old")
        store.complete_turn(turn.id, "Hello")
        before = store.run_sync(lambda: store._conn.total_changes)
        result = store.get_statistics(chat.id)
        assert (
            result.conversations,
            result.user_messages,
            result.assistant_messages,
        ) == (1, 1, 1)
        assert result.input_tokens is result.output_tokens is None
        assert result.counted_user_messages == result.counted_assistant_messages == 0
        assert result.first_message_at == store.get_user_message(turn.id).created_at
        assert result.last_message_at >= result.first_message_at
        assert store.run_sync(lambda: store._conn.total_changes) == before


def test_exact_counts_regeneration_idempotency_scope_restart_and_backup(tmp_path):
    async def scenario():
        with SqliteStore(tmp_path / "data") as store:
            chat = store.create_conversation("New")
            other = store.create_conversation("Old")
            old, _ = store.begin_turn(other.id, "not measured", "old")
            store.complete_turn(old.id, "old answer")
            provider = CountingFake()
            service = ConversationService(store, provider)
            runner = JobRunner(store, service)
            job, _ = await runner.submit(chat.id, "Привет!", "new")
            assert (await runner.run_job(job.id)).status is JobStatus.SUCCEEDED
            first = store.get_answer(job.turn_id)
            retry, duplicate = await runner.submit(chat.id, "Привет!", "new")
            assert duplicate and retry.id == job.id
            await runner.run_job(retry.id)
            regen, _ = await runner.regenerate(chat.id, job.turn_id, "regen")
            await runner.run_job(regen.id)
            again, duplicate = await runner.regenerate(chat.id, job.turn_id, "regen")
            assert duplicate and again.id == regen.id
            await runner.run_job(again.id)
            second = store.get_answer(regen.turn_id)
            result = await StatisticsService(store).read(chat.id)
            assert (
                result.user_messages,
                result.assistant_messages,
                result.regenerations,
            ) == (1, 2, 1)
            assert result.input_tokens == len("Привет!")
            assert result.output_tokens == len(first.text) + len(second.text)
            assert result.counted_user_messages == 1
            assert result.counted_assistant_messages == 2
            assert len(provider.requests) == 2
            total = store.get_statistics()
            assert total.user_messages == 2 and total.assistant_messages == 3
            assert total.input_tokens == result.input_tokens
            assert total.output_tokens == result.output_tokens
            assert total.counted_assistant_messages == 2
            backup = create_backup(store)
        with SqliteStore(tmp_path / "data") as store:
            assert store.get_statistics() == total
        restore_backup(backup, tmp_path / "restored")
        with SqliteStore(tmp_path / "restored") as store:
            assert store.get_statistics() == total

    asyncio.run(scenario())


def test_token_checkpoint_attempt_identity_final_text_and_atomic_completion(tmp_path):
    with SqliteStore(tmp_path) as store:
        chat = store.create_conversation("Counts")
        turn, _ = store.begin_turn(chat.id, "input", "id", "job")
        job = store.start_job("job")
        with pytest.raises(StaleTurnError):
            store.checkpoint_text_tokens(turn.id, "input", 1, "user", job.attempt + 1)
        with pytest.raises(SubmissionConflict):
            store.checkpoint_text_tokens(turn.id, "changed", 1, "user", job.attempt)
        store.checkpoint_text_tokens(turn.id, "input", 2, "user", job.attempt)
        store.checkpoint_text_tokens(turn.id, "draft", 3, "assistant", job.attempt)
        assert store.get_statistics().output_tokens is None
        store.run_sync(
            lambda: store._conn.executescript(
                "CREATE TRIGGER reject_answer BEFORE INSERT ON messages WHEN NEW.role='assistant' "
                "BEGIN SELECT RAISE(ABORT, 'injected'); END;"
            )
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.complete_turn(turn.id, "final", job.attempt)
        assert store.get_statistics().assistant_messages == 0
        assert store.get_statistics().output_tokens is None
        # Clearing a nonmatching checkpoint also rolls back with the reply.
        assert (
            store.run_sync(
                lambda: store._conn.execute(
                    "SELECT output_tokens FROM text_token_counts WHERE turn_id=?",
                    (turn.id,),
                ).fetchone()[0]
            )
            == 3
        )
        store.run_sync(lambda: store._conn.execute("DROP TRIGGER reject_answer"))
        store.complete_turn(turn.id, "final", job.attempt)
        assert store.get_statistics().output_tokens is None
        with pytest.raises(StaleTurnError):
            store.checkpoint_text_tokens(turn.id, "final", 4, "assistant", job.attempt)


def test_failed_interrupted_and_pending_counts_do_not_count_drafts(tmp_path):
    with SqliteStore(tmp_path) as store:
        chat = store.create_conversation("Outcomes")
        failed, _ = store.begin_turn(chat.id, "a", "a")
        store.checkpoint_text_tokens(failed.id, "a", 1, "user")
        store.checkpoint_draft(failed.id, "unfinished")
        store.checkpoint_text_tokens(failed.id, "unfinished", 2, "assistant")
        store.fail_turn(failed.id, "timeout")
        interrupted, _ = store.begin_turn(chat.id, "b", "b")
        store.interrupt_turn(interrupted.id)
        store.begin_turn(chat.id, "c", "c")
        stats = store.get_statistics(chat.id)
        assert (stats.failed_turns, stats.interrupted_turns, stats.pending_turns) == (
            1,
            1,
            1,
        )
        assert stats.user_messages == 3 and stats.assistant_messages == 0
        assert stats.input_tokens == 1 and stats.counted_user_messages == 1
        assert stats.output_tokens is None


def test_cancelled_tokenization_settles_before_return_without_checkpoint(tmp_path):
    async def scenario():
        entered, release = Event(), Event()

        class Slow(CountingFake):
            def count_output_tokens(self, text):
                entered.set()
                assert release.wait(3)
                return len(text)

        with SqliteStore(tmp_path) as store:
            chat = store.create_conversation("cancel")
            turn, _ = store.begin_turn(chat.id, "input", "id")
            task = asyncio.create_task(
                checkpoint_text_tokens(
                    store, Slow(), turn.id, "input", "user", Event(), None
                )
            )
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert store.get_statistics().input_tokens is None

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [True, -1, "1", None, OSError("offline")])
def test_unavailable_tokenizer_never_invents_counts_or_breaks_reply(tmp_path, failure):
    class Unavailable(CountingFake):
        def count_output_tokens(self, text):
            if isinstance(failure, Exception):
                raise failure
            return failure

    with SqliteStore(tmp_path) as store:
        chat = store.create_conversation("unknown")
        result = ConversationService(store, Unavailable()).send(chat.id, "input", "id")
        assert result.answer is not None
        assert store.get_statistics().input_tokens is None
        assert store.get_statistics().output_tokens is None


def test_v12_migration_rolls_back_and_retries_preserving_old_history(tmp_path):
    with SqliteStore(tmp_path) as store:
        chat = store.create_conversation("Old")
        turn, _ = store.begin_turn(chat.id, "Question", "id")
        store.complete_turn(turn.id, "Answer")
        messages = store.get_messages(chat.id)
    with closing(sqlite3.connect(tmp_path / "jarvis.db")) as db:
        db.executescript(
            "DROP TABLE text_token_counts; DELETE FROM schema_migrations WHERE version=12; "
            "CREATE TRIGGER reject_counts BEFORE INSERT ON schema_migrations WHEN NEW.version=12 "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END;"
        )
    with pytest.raises(sqlite3.IntegrityError):
        SqliteStore(tmp_path)
    with closing(sqlite3.connect(tmp_path / "jarvis.db")) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='text_token_counts'"
            ).fetchone()[0]
            == 0
        )
        db.execute("DROP TRIGGER reject_counts")
        db.commit()
    with SqliteStore(tmp_path) as store:
        assert store.get_messages(chat.id) == messages
        assert store.get_statistics().input_tokens is None


def test_statistics_api_is_authenticated_scoped_and_returns_only_metadata(tmp_path):
    async def scenario():
        with SqliteStore(tmp_path) as store:
            chat = store.create_conversation("Private title")
            service = ConversationService(store, CountingFake())
            service_result = await service.send_async(chat.id, "Private message", "id")
            app, token = create_web_app(
                service,
                JobRunner(store, service),
                None,
                8765,
                statistics=StatisticsService(store),
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
            ) as client:
                assert (await client.get("/api/statistics")).status_code == 401
                await client.post("/api/setup", json={"token": token})
                assert (await client.get("/api/session")).json()["statistics_enabled"]
                response = await client.get(
                    "/api/statistics", params={"conversation_id": chat.id}
                )
                assert response.status_code == 200
                assert response.json() == asdict(store.get_statistics(chat.id))
                assert "Private" not in response.text
                assert service_result.answer.text not in response.text
                assert (
                    await client.get("/api/statistics?conversation_id=missing")
                ).status_code == 404
                assert (
                    await client.get(
                        "/api/statistics", headers={"origin": "https://evil.test"}
                    )
                ).status_code == 403
                assert len(service.provider.requests) == 1

    asyncio.run(scenario())


def test_tool_rounds_count_only_user_text_and_final_reply_and_generated_images(
    tmp_path,
):
    from test_m8 import FakeImage, FakeManager, ScriptedChat

    from jarvis.application.images import ImageService
    from jarvis.application.tools import ToolPolicy, ToolService
    from jarvis.domain.resources import RuntimeProfile
    from jarvis.infrastructure.read_tools import SafeReadTools

    class CountedTools(ScriptedChat):
        def __init__(self):
            super().__init__()
            self.counted = []

        def count_output_tokens(self, text):
            self.counted.append(text)
            return len(text)

    async def scenario():
        with SqliteStore(tmp_path / "data") as store:
            chat = store.create_conversation("Image")
            provider = CountedTools()
            manager = FakeManager()
            image = FakeImage(tmp_path / "output")
            tools = ToolService(
                store,
                SafeReadTools([], store.data_dir),
                ToolPolicy(capabilities=frozenset({"image"})),
                image_service=ImageService(
                    store, image, manager, RuntimeProfile("image", 1, 1, 0, 0)
                ),
            )
            service = ConversationService(store, provider, tool_service=tools)
            runner = JobRunner(store, service)
            job, _ = await runner.submit(chat.id, "Make an image", "one")
            assert (await runner.run_job(job.id)).status is JobStatus.SUCCEEDED
            answer = store.get_answer(job.turn_id)
            assert provider.counted == ["Make an image", answer.text]
            stats = store.get_statistics()
            assert stats.generated_images == 1
            assert stats.input_tokens == len("Make an image")
            assert stats.output_tokens == len(answer.text)

    asyncio.run(scenario())


def test_vision_counts_question_and_reply_before_unload_and_excludes_uploads(tmp_path):
    from test_m12 import Vision, _png, _runner

    class CountedVision(Vision):
        def count_output_tokens(self, text):
            assert not any(event[0] == "unload" for event in self.manager.events)
            return len(text)

    async def scenario():
        with SqliteStore(tmp_path) as store:
            chat = store.create_conversation("Screenshot")
            provider = CountedVision()
            runner, manager, _ = _runner(store, provider=provider)
            provider.manager = manager
            job, _ = await runner.submit_vision(chat.id, "Что видно?", _png(), "one")
            assert (await runner.run_job(job.id)).status is JobStatus.SUCCEEDED
            stats = store.get_statistics()
            assert stats.generated_images == 0
            assert stats.input_tokens == len("Что видно?")
            assert stats.output_tokens == len(store.get_answer(job.turn_id).text)
            assert any(event[0] == "unload" for event in manager.events)

    asyncio.run(scenario())
