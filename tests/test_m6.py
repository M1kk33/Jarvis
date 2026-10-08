"""M6 durable-job, cancellation, recovery, and backup contracts."""

import asyncio
import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from importlib import resources
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from jarvis.application.conversations import ConversationService
from jarvis.application.jobs import JobRunner
from jarvis.application.tools import ToolService
from jarvis.bootstrap import main
from jarvis.domain.jobs import JobStatus
from jarvis.domain.resources import ResourceUnavailable
from jarvis.domain.tools import ToolContextRecord
from jarvis.domain.types import (
    ActiveTurnError,
    RequestFailure,
    StaleTurnError,
    StreamDelta,
    StreamTerminal,
    StreamToolCall,
    TurnStatus,
)
from jarvis.infrastructure.backup import create_backup, restore_backup
from jarvis.infrastructure.providers.fake import FakeChatProvider
from jarvis.infrastructure.sqlite_store import SqliteStore
from jarvis.interfaces.cli import dispatch, parse_args


def test_job_submission_completion_and_duplicate_are_atomic(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Jobs")
        provider = FakeChatProvider()
        runner = JobRunner(store, ConversationService(store, provider))

        async def scenario():
            job, duplicate = await runner.submit(conversation.id, "hello", "stable")
            assert not duplicate and job.status is JobStatus.QUEUED
            assert store.get_turn(job.turn_id).status is TurnStatus.PENDING
            assert store.get_job_steps(job.id)[0].status == "queued"
            with pytest.raises(ActiveTurnError):
                await runner.submit(conversation.id, "another", "two")
            done = await runner.run_job(job.id)
            assert done.status is JobStatus.SUCCEEDED
            assert store.get_turn(job.turn_id).status is TurnStatus.COMPLETE
            assert store.get_job_steps(job.id)[0].status == "complete"
            again, duplicate = await runner.submit(conversation.id, "hello", "stable")
            assert duplicate and again.id == job.id
            assert len(provider.requests) == 1
            return job

        created = asyncio.run(scenario())
    with SqliteStore(tmp_path) as reopened:
        assert reopened.get_job(created.id).status is JobStatus.SUCCEEDED
        assert reopened.get_answer(created.turn_id) is not None


def test_cancelled_completion_await_observes_committed_job(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Committed")
        runner = JobRunner(store, ConversationService(store, FakeChatProvider()))
        original_run = store.run

        async def cancel_after_commit(operation):
            result = await original_run(operation)
            code = getattr(operation, "__code__", None)
            if code is not None and "complete_turn" in code.co_names:
                raise asyncio.CancelledError
            return result

        async def scenario():
            job, _ = await runner.submit(conversation.id, "hello", "one")
            store.run = cancel_after_commit
            completed = await runner.run_job(job.id)
            assert completed.status is JobStatus.SUCCEEDED
            assert store.get_answer(job.turn_id) is not None

        asyncio.run(scenario())


def test_runner_links_tool_receipt_step_and_final_reply(tmp_path):
    class ClockBackend:
        calls = 0

        def inspect(self, _name, _arguments):
            return "clock"

        def execute(self, _name, _arguments):
            self.calls += 1
            return {"utc": "2026-10-06T00:00:00+00:00"}

    class ScriptedProvider:
        def count_prompt_tokens_with_tools(self, _messages, _instructions, _tools):
            return 100

        def stream_with_tools(self, messages, _cancel, _instructions, _tools):
            if any(isinstance(item, ToolContextRecord) for item in messages):
                yield StreamDelta("The time was read.")
            else:
                yield StreamToolCall("clock-call", "current_time", "{}")
            yield StreamTerminal()

    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Tool job")
        backend = ClockBackend()
        service = ConversationService(
            store, ScriptedProvider(), tool_service=ToolService(store, backend)
        )
        runner = JobRunner(store, service)

        async def scenario():
            job, _ = await runner.submit(conversation.id, "What time?", "one")
            done = await runner.run_job(job.id)
            assert done.status is JobStatus.SUCCEEDED
            assert done.next_step == 2
            assert backend.calls == 1
            steps = store.get_job_steps(job.id)
            assert [(step.kind, step.status) for step in steps] == [
                ("chat", "complete"),
                ("tool", "complete"),
            ]
            invocation = store.get_invocations(job.turn_id)[0]
            assert invocation.job_id == job.id
            assert invocation.job_attempt == done.attempt
            assert store.get_answer(job.turn_id).text == "The time was read."

        asyncio.run(scenario())


def test_cancel_status_stays_responsive_during_stalled_fake_request(tmp_path):
    started = Event()
    release = Event()

    class StalledProvider(FakeChatProvider):
        def stream(self, messages, cancel, instructions=""):
            started.set()
            release.wait(3)
            yield StreamTerminal("cancelled" if cancel.is_set() else None)

    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Cancel")
        runner = JobRunner(store, ConversationService(store, StalledProvider()))

        async def scenario():
            job, _ = await runner.submit(conversation.id, "wait", "one")
            task = runner.launch(job.id)
            assert await asyncio.to_thread(started.wait, 2)
            pending = await runner.cancel(job.id)
            assert pending.status is JobStatus.CANCEL_REQUESTED
            assert (await runner.status(job.id)).cancel_requested
            release.set()
            settled = await task
            assert settled.status is JobStatus.CANCELLED
            assert store.get_turn(job.turn_id).status is TurnStatus.INTERRUPTED

        asyncio.run(scenario())


def test_cancel_queued_and_waiting_approval_invalidates_permission(tmp_path):
    with SqliteStore(tmp_path) as store:
        first = store.create_conversation("Queued")
        second = store.create_conversation("Waiting")
        runner = JobRunner(store, ConversationService(store, FakeChatProvider()))

        async def scenario():
            queued, _ = await runner.submit(first.id, "never run", "one")
            assert (await runner.cancel(queued.id)).status is JobStatus.CANCELLED
            assert store.get_turn(queued.turn_id).status is TurnStatus.INTERRUPTED
            waiting, _ = await runner.submit(second.id, "approval", "two")
            started = store.start_job(waiting.id)
            invocation = store.record_tool_proposal(
                waiting.turn_id,
                "call",
                "current_time",
                1,
                "{}",
                "hash",
                "m5-v1",
                "clock",
                "approval",
                started.attempt,
            )
            store.mark_job_waiting(waiting.id)
            approval = store.issue_tool_approval(
                invocation.id, "2999-01-01T00:00:00+00:00"
            )
            assert (await runner.cancel(waiting.id)).status is JobStatus.CANCELLED
            assert store.get_tool_approval(invocation.id).status == "invalidated"
            with pytest.raises(StaleTurnError):
                store.record_tool_intent(invocation.id, "m5-v1", "clock", approval.id)

        asyncio.run(scenario())


def test_cancel_during_start_cannot_launch_provider(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Start cancellation")
        provider = FakeChatProvider()
        runner = JobRunner(store, ConversationService(store, provider))
        original_run = store.run
        entered = asyncio.Event()
        gate = asyncio.Event()

        async def delay_start(operation):
            if "start_job" in operation.__code__.co_names:
                entered.set()
                await gate.wait()
            return await original_run(operation)

        async def scenario():
            job, _ = await runner.submit(conversation.id, "never", "one")
            store.run = delay_start
            task = runner.launch(job.id)
            await asyncio.wait_for(entered.wait(), 2)
            requested = await runner.cancel(job.id)
            assert requested.status is JobStatus.CANCEL_REQUESTED
            gate.set()
            assert (await task).status is JobStatus.CANCELLED
            assert provider.requests == []
            store.run = original_run

        asyncio.run(scenario())


def test_cancel_during_cleanup_retains_pending_status_until_verified(tmp_path):
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()

    class CleanupConversation(ConversationService):
        async def generate_existing_turn_async(
            self, turn_id, conversation_id, cancel, on_delta=None, job_attempt=None
        ):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cleaning.set()
                await release.wait()
                raise

    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Cleanup")
        runner = JobRunner(store, CleanupConversation(store, FakeChatProvider()))

        async def scenario():
            job, _ = await runner.submit(conversation.id, "wait", "one")
            task = runner.launch(job.id)
            await asyncio.wait_for(entered.wait(), 2)
            assert (await runner.cancel(job.id)).status is JobStatus.CANCEL_REQUESTED
            await asyncio.wait_for(cleaning.wait(), 2)
            assert (await runner.status(job.id)).status is JobStatus.CANCEL_REQUESTED
            release.set()
            assert (await task).status is JobStatus.CANCELLED

        asyncio.run(scenario())


def test_restart_reconciles_queued_running_cancellation_and_unknown(tmp_path):
    with SqliteStore(tmp_path) as store:
        ids = []
        for name in ("queued", "running", "cancel", "unknown"):
            conversation = store.create_conversation(name)
            job_id = str(uuid4())
            turn, _ = store.begin_turn(conversation.id, name, name, job_id)
            ids.append((job_id, turn.id))
        store.request_job_cancel(ids[2][0])
        unknown_job = store.start_job(ids[3][0])
        invocation = store.record_tool_proposal(
            ids[3][1],
            "call",
            "current_time",
            1,
            "{}",
            "hash",
            "m5-v1",
            "clock",
            "allow",
            unknown_job.attempt,
        )
        store.record_tool_intent(invocation.id, "m5-v1", "clock")
        # Inject the crash snapshot after normal mutations; two running jobs
        # are now rejected by the tool intent boundary.
        store.run_sync(
            lambda: store._conn.execute(
                "UPDATE jobs SET status='running', attempt=1 WHERE id=?",
                (ids[1][0],),
            )
        )
    with SqliteStore(tmp_path) as reopened:
        assert reopened.get_job(ids[0][0]).status is JobStatus.QUEUED
        assert reopened.get_job(ids[1][0]).status is JobStatus.INTERRUPTED
        assert reopened.get_job(ids[2][0]).status is JobStatus.CANCELLED
        assert reopened.get_job(ids[3][0]).status is JobStatus.INTERRUPTED
        assert reopened.get_invocation(invocation.id).status.value == "outcome_unknown"
        with pytest.raises(ActiveTurnError):
            reopened.begin_turn(
                reopened.get_job(ids[3][0]).conversation_id, "new", "new"
            )
        with pytest.raises(StaleTurnError):
            reopened.start_job(ids[3][0])


def test_restart_classifies_every_persisted_job_status(tmp_path):
    names = (
        "queued",
        "interrupted",
        "waiting_approval",
        "succeeded",
        "failed",
        "cancelled",
        "cancel_requested",
        "running",
    )
    ids = {}
    with SqliteStore(tmp_path) as store:
        for name in names:
            conversation = store.create_conversation(name)
            job_id = str(uuid4())
            turn, _ = store.begin_turn(conversation.id, name, name, job_id)
            ids[name] = job_id
            if name in {"queued", "cancelled"}:
                if name == "cancelled":
                    store.request_job_cancel(job_id)
                    store.settle_job_cancel(job_id)
                continue
            started = store.start_job(job_id)
            if name == "interrupted":
                store.interrupt_turn(turn.id, expected_job_attempt=started.attempt)
            elif name == "waiting_approval":
                store.mark_job_waiting(job_id)
            elif name == "succeeded":
                store.complete_turn(turn.id, "done", started.attempt)
            elif name == "failed":
                store.fail_turn(turn.id, "backend_crashed", started.attempt)
            elif name == "cancel_requested":
                store.interrupt_turn(turn.id, expected_job_attempt=started.attempt)
        # Inject the pending-cancellation crash state after the other jobs exist;
        # live execution cannot overtake cancellation cleanup.
        store.run_sync(
            lambda: store._conn.execute(
                "UPDATE jobs SET status='cancel_requested', cancel_requested=1 WHERE id=?",
                (ids["cancel_requested"],),
            )
        )
    with SqliteStore(tmp_path) as reopened:
        expected = {
            "queued": JobStatus.QUEUED,
            "interrupted": JobStatus.INTERRUPTED,
            "waiting_approval": JobStatus.WAITING_APPROVAL,
            "succeeded": JobStatus.SUCCEEDED,
            "failed": JobStatus.FAILED,
            "cancelled": JobStatus.CANCELLED,
            "cancel_requested": JobStatus.CANCELLED,
            "running": JobStatus.INTERRUPTED,
        }
        assert {name: reopened.get_job(ids[name]).status for name in names} == expected


def test_real_job_cancellation_waits_for_previous_tree_verification(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Real cancellation")
        job_id = str(uuid4())
        store.begin_turn(conversation.id, "cancel", "one", job_id, "pinned-model")
        store.start_job(job_id)
        store.request_job_cancel(job_id)

    class OwnershipCheck:
        verified = False

        async def verify_previous_worker(self):
            return self.verified

    with SqliteStore(tmp_path) as reopened:
        assert reopened.get_job(job_id).status is JobStatus.CANCEL_REQUESTED
        manager = OwnershipCheck()
        runner = JobRunner(
            reopened,
            ConversationService(
                reopened,
                FakeChatProvider(),
                resource_manager=manager,
                runtime_profile=SimpleNamespace(name="pinned-model"),
            ),
        )

        async def scenario():
            with pytest.raises(ResourceUnavailable):
                await runner.cancel(job_id)
            assert reopened.get_job(job_id).status is JobStatus.CANCEL_REQUESTED
            manager.verified = True
            wrong = JobRunner(
                reopened,
                runner.conversations,
                profile_identity="different-device-or-model",
            )
            with pytest.raises(ResourceUnavailable):
                await wrong.cancel(job_id)
            assert reopened.get_job(job_id).status is JobStatus.CANCEL_REQUESTED
            assert (await runner.cancel(job_id)).status is JobStatus.CANCELLED

        asyncio.run(scenario())


def test_backend_oom_fails_job_and_next_conversation_still_works(tmp_path):
    class OomProvider(FakeChatProvider):
        def stream(self, messages, cancel, instructions=""):
            yield StreamTerminal("out_of_memory")

    with SqliteStore(tmp_path) as store:
        first = store.create_conversation("OOM")
        oom = JobRunner(store, ConversationService(store, OomProvider()))

        async def scenario():
            job, _ = await oom.submit(first.id, "oom", "one")
            with pytest.raises(RequestFailure):
                await oom.run_job(job.id)
            assert store.get_job(job.id).status is JobStatus.FAILED
            second = store.create_conversation("Recovered")
            good = JobRunner(store, ConversationService(store, FakeChatProvider()))
            next_job, _ = await good.submit(second.id, "works", "two")
            assert (await good.run_job(next_job.id)).status is JobStatus.SUCCEEDED

        asyncio.run(scenario())


def test_verified_backup_restores_without_original_data(tmp_path):
    data = tmp_path / "source"
    with SqliteStore(data) as store:
        conversation = store.create_conversation("Backup")
        job_id = str(uuid4())
        turn, _ = store.begin_turn(conversation.id, "save", "one", job_id)
        started = store.start_job(job_id)
        store.complete_turn(turn.id, "saved", started.attempt)
        relative = f"artifacts/{job_id}/fixture.txt"
        fixture = data / relative
        fixture.parent.mkdir(parents=True)
        fixture.write_bytes(b"M6 backup fixture")
        (data / "cache").mkdir()
        (data / "cache" / "disposable.bin").write_bytes(b"do not back up")
        digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
        store.run_sync(
            lambda: store._conn.execute(
                "INSERT INTO artifacts (id, job_id, relative_path, kind, size_bytes, "
                "sha256, status, created_at) VALUES (?, ?, ?, 'fixture', ?, ?, 'available', ?)",
                (
                    str(uuid4()),
                    job_id,
                    relative,
                    fixture.stat().st_size,
                    digest,
                    "2026-10-06T00:00:00+00:00",
                ),
            )
        )
        backup = create_backup(store)
    saved = tmp_path / "saved-backup"
    shutil.copytree(backup, saved)
    assert not (saved / "cache" / "disposable.bin").exists()
    tampered = tmp_path / "tampered-manifest"
    shutil.copytree(saved, tampered)
    manifest = tampered / "manifest.json"
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["artifacts"] = []
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError):
        restore_backup(tampered, tmp_path / "manifest-restore")
    data.rename(tmp_path / "original-unavailable")
    target = tmp_path / "restored"
    restore_backup(saved, target)
    with SqliteStore(target) as restored:
        assert restored.get_job(job_id).status is JobStatus.SUCCEEDED
        assert restored.get_answer(turn.id).text == "saved"
        assert (target / relative).read_bytes() == b"M6 backup fixture"
    with pytest.raises(ValueError):
        restore_backup(saved, target)
    (saved / relative).write_bytes(b"corrupt")
    with pytest.raises(ValueError):
        restore_backup(saved, tmp_path / "bad-restore")
    (saved / relative).unlink()
    with pytest.raises(ValueError):
        restore_backup(saved, tmp_path / "missing-restore")


def test_corrupt_artifact_becomes_unavailable_on_startup(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Artifact")
        job_id = str(uuid4())
        turn, _ = store.begin_turn(conversation.id, "artifact", "one", job_id)
        started = store.start_job(job_id)
        store.complete_turn(turn.id, "done", started.attempt)
        relative = f"artifacts/{job_id}/fixture.txt"
        target = tmp_path / relative
        target.parent.mkdir(parents=True)
        target.write_bytes(b"expected")
        store.run_sync(
            lambda: store._conn.execute(
                "INSERT INTO artifacts (id, job_id, relative_path, kind, size_bytes, "
                "sha256, status, created_at) VALUES ('artifact', ?, ?, 'fixture', 8, ?, "
                "'available', 'old')",
                (job_id, relative, hashlib.sha256(b"expected").hexdigest()),
            )
        )
    target.write_bytes(b"tampered")
    with SqliteStore(tmp_path) as reopened:
        status = reopened.run_sync(
            lambda: reopened._conn.execute(
                "SELECT status FROM artifacts WHERE id='artifact'"
            ).fetchone()[0]
        )
        assert status == "unavailable"
        assert reopened.get_job(job_id).status is JobStatus.SUCCEEDED


def test_third_interrupted_attempt_exhausts_safe_retry_budget(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Retries")
        job_id = str(uuid4())
        turn, _ = store.begin_turn(conversation.id, "retry", "one", job_id)
        for attempt in range(1, 4):
            running = store.start_job(job_id)
            assert running.attempt == attempt
            store.interrupt_turn(turn.id, expected_job_attempt=attempt)
        failed = store.start_job(job_id)
        assert failed.status is JobStatus.FAILED
        assert failed.progress == "attempt_limit"
        assert store.get_turn(turn.id).status is TurnStatus.FAILED


def test_stale_attempt_cannot_write_draft_proposal_or_final_reply(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Attempts")
        job_id = str(uuid4())
        turn, _ = store.begin_turn(conversation.id, "hello", "one", job_id)
        first = store.start_job(job_id)
        store.checkpoint_draft(turn.id, "old", first.attempt)
        store.interrupt_turn(turn.id, expected_job_attempt=first.attempt)
        second = store.start_job(job_id)
        assert second.attempt == first.attempt + 1
        with pytest.raises(StaleTurnError):
            store.checkpoint_draft(turn.id, "stale", first.attempt)
        with pytest.raises(StaleTurnError):
            store.record_tool_proposal(
                turn.id,
                "old",
                "current_time",
                1,
                "{}",
                "hash",
                "m5-v1",
                "clock",
                "allow",
                first.attempt,
            )
        with pytest.raises(StaleTurnError):
            store.complete_turn(turn.id, "stale", first.attempt)
        with pytest.raises(StaleTurnError):
            store.fail_turn(turn.id, "backend_crashed", first.attempt)
        assert store.get_draft(turn.id).text == "old"
        store.complete_turn(turn.id, "fresh", second.attempt)
        assert store.get_answer(turn.id).text == "fresh"


def test_job_submission_and_completion_roll_back_together_on_write_fault(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Fault")
        store.run_sync(
            lambda: store._conn.execute(
                "CREATE TRIGGER fail_job BEFORE INSERT ON jobs "
                "BEGIN SELECT RAISE(ABORT, 'database or disk is full'); END"
            )
        )
        with pytest.raises(sqlite3.Error):
            store.begin_turn(conversation.id, "hello", "one", str(uuid4()))
        assert store.get_turns(conversation.id) == []
        assert store.get_messages(conversation.id) == []
        store.run_sync(lambda: store._conn.execute("DROP TRIGGER fail_job"))
        job_id = str(uuid4())
        turn, _ = store.begin_turn(conversation.id, "hello", "one", job_id)
        started = store.start_job(job_id)
        store.run_sync(
            lambda: store._conn.execute(
                "CREATE TRIGGER fail_finish BEFORE UPDATE OF status ON jobs "
                "WHEN NEW.status='succeeded' BEGIN SELECT RAISE(ABORT, 'write fault'); END"
            )
        )
        with pytest.raises(sqlite3.Error):
            store.complete_turn(turn.id, "answer", started.attempt)
        assert store.get_turn(turn.id).status is TurnStatus.PENDING
        assert store.get_answer(turn.id) is None
        assert store.get_job(job_id).status is JobStatus.RUNNING
        assert store.get_job_steps(job_id)[0].status == "running"
        store.run_sync(lambda: store._conn.execute("DROP TRIGGER fail_finish"))
        store.complete_turn(turn.id, "answer", started.attempt)
        assert store.get_job(job_id).status is JobStatus.SUCCEEDED


def test_real_sqlite_page_limit_simulates_full_disk_without_erasing_data(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Full")
        pages = store.run_sync(
            lambda: store._conn.execute("PRAGMA page_count").fetchone()[0]
        )
        store.run_sync(lambda: store._conn.execute(f"PRAGMA max_page_count={pages}"))
        with pytest.raises(sqlite3.OperationalError):
            store.begin_turn(conversation.id, "x" * 65_536, "large", str(uuid4()))
        assert store.get_conversation(conversation.id).title == "Full"
        assert store.get_turns(conversation.id) == []


def test_job_tool_receipt_step_transition_is_atomic(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Tool fault")
        job_id = str(uuid4())
        turn, _ = store.begin_turn(conversation.id, "time", "one", job_id)
        started = store.start_job(job_id)
        invocation = store.record_tool_proposal(
            turn.id,
            "call",
            "current_time",
            1,
            "{}",
            "hash",
            "m5-v1",
            "clock",
            "allow",
            started.attempt,
        )
        store.record_tool_intent(invocation.id, "m5-v1", "clock")
        store.run_sync(
            lambda: store._conn.execute(
                "CREATE TRIGGER fail_receipt BEFORE UPDATE OF receipt_json ON tool_invocations "
                "BEGIN SELECT RAISE(ABORT, 'write fault'); END"
            )
        )
        with pytest.raises(sqlite3.Error):
            store.commit_tool_result(
                invocation.id, '{"status":"complete"}', '{"status":"ok"}'
            )
        assert store.get_invocation(invocation.id).status.value == "intent"
        assert store.get_job_steps(job_id)[1].status == "running"
        assert store.get_job(job_id).next_step == 1
    with SqliteStore(tmp_path) as reopened:
        assert reopened.get_invocation(invocation.id).status.value == "outcome_unknown"
        assert reopened.get_job_steps(job_id)[1].status == "outcome_unknown"


def test_durable_cancel_before_tool_action_prevents_fake_effect(tmp_path):
    entered = Event()
    release = Event()

    class FakeEffectBackend:
        inspections = 0
        effects = 0

        def inspect(self, _name, _arguments):
            self.inspections += 1
            if self.inspections == 3:
                entered.set()
                release.wait(2)
            return "target"

        def execute(self, _name, _arguments):
            self.effects += 1
            return {"utc": "2026-10-06T00:00:00+00:00"}

    backend = FakeEffectBackend()
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Cancel intent")
        job_id = str(uuid4())
        turn, _ = store.begin_turn(conversation.id, "time", "one", job_id)
        started = store.start_job(job_id)
        service = ToolService(store, backend)

        async def scenario():
            task = asyncio.create_task(
                service.propose_and_run(
                    turn.id,
                    StreamToolCall("call", "current_time", "{}"),
                    started.attempt,
                )
            )
            assert await asyncio.to_thread(entered.wait, 2)
            await store.run(lambda: store.request_job_cancel(job_id))
            release.set()
            with pytest.raises(StaleTurnError):
                await task
            assert backend.effects == 0
            assert store.get_invocations(turn.id)[0].status.value == "outcome_unknown"
            store.settle_job_cancel(job_id)

        asyncio.run(scenario())


def test_queue_is_bounded_and_conversation_occupancy_survives_restart(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversations = [store.create_conversation(str(index)) for index in range(17)]
        jobs = []
        for index in range(16):
            job_id = str(uuid4())
            store.begin_turn(conversations[index].id, "queued", str(index), job_id)
            jobs.append(job_id)
        with pytest.raises(ActiveTurnError):
            store.begin_turn(conversations[16].id, "too many", "last", str(uuid4()))
    with SqliteStore(tmp_path) as reopened:
        assert all(
            reopened.get_job(job_id).status is JobStatus.QUEUED for job_id in jobs
        )
        with pytest.raises(ActiveTurnError):
            reopened.begin_turn(conversations[0].id, "other", "other")
        runner = JobRunner(reopened, ConversationService(reopened, FakeChatProvider()))
        assert asyncio.run(runner.run_job(jobs[0])).status is JobStatus.SUCCEEDED


def test_terminal_job_with_unknown_invocation_keeps_conversation_claim(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Terminal unknown")
        job_id = str(uuid4())
        turn, _ = store.begin_turn(conversation.id, "done", "one", job_id)
        started = store.start_job(job_id)
        store.complete_turn(turn.id, "done", started.attempt)
        store.run_sync(
            lambda: store._conn.execute(
                "INSERT INTO tool_invocations (id, turn_id, conversation_id, ordinal, "
                "model_call_id, name, version, arguments_json, arguments_sha256, "
                "policy_version, target_identity, status, decision, created_at, job_id, "
                "job_attempt) VALUES ('unknown', ?, ?, 1, 'unknown', 'current_time', "
                "1, '{}', 'hash', 'm5-v1', 'clock', 'intent', 'allow', 'old', ?, ?)",
                (turn.id, conversation.id, job_id, started.attempt),
            )
        )
    with SqliteStore(tmp_path) as reopened:
        assert reopened.get_job(job_id).status is JobStatus.SUCCEEDED
        assert reopened.get_invocation("unknown").status.value == "outcome_unknown"
        with pytest.raises(ActiveTurnError):
            reopened.begin_turn(conversation.id, "blocked", "two")


def test_m5_database_migrates_without_replaying_old_approval(tmp_path):
    database = tmp_path / "jarvis.db"
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, "
            "checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        for version in (1, 2, 3):
            script = (
                resources.files("jarvis")
                .joinpath(
                    "infrastructure",
                    "migrations",
                    f"{version:03d}_"
                    + {1: "initial", 2: "drafts", 3: "tools"}[version]
                    + ".sql",
                )
                .read_text(encoding="utf-8")
            )
            db.executescript("BEGIN IMMEDIATE;\n" + script)
            db.execute(
                "INSERT INTO schema_migrations VALUES (?, ?, 'old')",
                (version, hashlib.sha256(script.encode()).hexdigest()),
            )
            db.commit()
        db.execute("INSERT INTO conversations VALUES ('c', 'old', 'old')")
        db.execute(
            "INSERT INTO turns VALUES ('t', 'c', 1, 'one', 'pending', 'old', NULL)"
        )
        db.execute(
            "INSERT INTO messages VALUES ('u', 'c', 't', 1, 'user', ?, 'complete', 'old')",
            ('[{"type":"text","text":"old"}]',),
        )
        db.execute(
            "INSERT INTO tool_invocations (id, turn_id, conversation_id, ordinal, "
            "model_call_id, name, version, arguments_json, arguments_sha256, "
            "policy_version, target_identity, status, decision, result_json, "
            "receipt_json, created_at) VALUES "
            "('done', 't', 'c', 1, 'done', 'current_time', 1, '{}', 'hash', "
            "'m5-v1', 'clock', 'complete', 'allow', '{\"status\":\"ok\"}', "
            "'{\"status\":\"complete\"}', 'old'), "
            "('waiting', 't', 'c', 2, 'waiting', 'current_time', 1, '{}', 'hash', "
            "'m5-v1', 'clock', 'waiting_approval', 'approval', NULL, NULL, 'old')"
        )
        db.execute(
            "INSERT INTO tool_approvals (id, invocation_id, turn_id, arguments_sha256, "
            "tool_version, target_identity, policy_version, session_id, expires_at, "
            "status, issued_at) VALUES "
            "('approval', 'waiting', 't', 'hash', 1, 'clock', 'm5-v1', 'old', "
            "'2999-01-01', 'issued', 'old')"
        )
        db.commit()
    with SqliteStore(tmp_path) as upgraded:
        assert upgraded.get_invocation("done").status.value == "complete"
        assert upgraded.get_invocation("waiting").status.value == "cancelled"
        assert upgraded.get_tool_approval("waiting").status == "invalidated"
        assert upgraded.get_job_by_turn("t") is None
        assert upgraded.get_turn("t").status is TurnStatus.INTERRUPTED


def test_job_approval_requires_fresh_invocation_after_restart(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Approval")
        job_id = str(uuid4())
        turn, _ = store.begin_turn(conversation.id, "approve", "one", job_id)
        first = store.start_job(job_id)
        waiting = store.record_tool_proposal(
            turn.id,
            "old-call",
            "current_time",
            1,
            "{}",
            "hash",
            "m5-v1",
            "clock",
            "approval",
            first.attempt,
        )
        store.mark_job_waiting(job_id)
        old_approval = store.issue_tool_approval(
            waiting.id, "2999-01-01T00:00:00+00:00"
        )
    with SqliteStore(tmp_path) as reopened:
        assert reopened.get_tool_approval(waiting.id).status == "invalidated"
        assert reopened.get_invocation(waiting.id).status.value == "cancelled"
        assert reopened.get_job(job_id).status is JobStatus.WAITING_APPROVAL
        with pytest.raises(StaleTurnError):
            reopened.record_tool_intent(waiting.id, "m5-v1", "clock", old_approval.id)
        second = reopened.start_job(job_id)
        fresh = reopened.record_tool_proposal(
            turn.id,
            "new-call",
            "current_time",
            1,
            "{}",
            "hash",
            "m5-v1",
            "clock",
            "approval",
            second.attempt,
        )
        approval = reopened.issue_tool_approval(fresh.id, "2999-01-01T00:00:00+00:00")
        reopened.record_tool_intent(fresh.id, "m5-v1", "clock", approval.id)
        reopened.commit_tool_result(
            fresh.id, '{"status":"complete"}', '{"status":"ok"}'
        )
        assert reopened.get_job_steps(job_id)[-1].status == "complete"


def test_cli_jobs_diagnostics_backup_and_restore(tmp_path, capsys):
    source = tmp_path / "cli-source"
    args = ["--data-dir", str(source)]
    assert main([*args, "create", "CLI M6"]) == 0
    conversation_id = capsys.readouterr().out.strip()
    assert (
        main([*args, "send", conversation_id, "hello", "--submission-id", "cli-one"])
        == 0
    )
    sent = capsys.readouterr().out
    job_id = next(
        line.split("=", 1)[1]
        for line in sent.splitlines()
        if line.startswith("job_id=")
    )
    assert "job_status=succeeded" in sent
    assert main([*args, "status", job_id]) == 0
    assert "succeeded" in capsys.readouterr().out
    assert main([*args, "diagnostics"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["jobs_by_status"]["succeeded"] == 1
    assert report["queue_depth"] == 0
    assert main([*args, "backup"]) == 0
    backup_path = Path(capsys.readouterr().out.strip())
    restored = tmp_path / "cli-restored"
    assert main(["--data-dir", str(restored), "restore", str(backup_path)]) == 0
    capsys.readouterr()
    assert main(["--data-dir", str(restored), "status", job_id]) == 0
    assert "succeeded" in capsys.readouterr().out


def test_interactive_session_streams_background_job_and_exits_cleanly(
    tmp_path, capsys, monkeypatch
):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Session")
        service = ConversationService(store, FakeChatProvider())
        runner = JobRunner(store, service)

        class FakeManager:
            closed = False

            async def unload(self):
                return None

            async def close(self):
                self.closed = True

        manager = FakeManager()
        inputs = iter(["hello", "/jobs", "/exit"])
        monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
        args = parse_args(["--data-dir", str(tmp_path), "session", conversation.id])
        assert dispatch(args, service, manager, runner) == 0
        output = capsys.readouterr().out
        assert "Fake reply: hello" in output
        assert "succeeded" in output
        assert manager.closed
