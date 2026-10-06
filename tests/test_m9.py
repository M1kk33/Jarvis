"""M9 approval, exclusive creation, checked paths, and crash reconciliation."""

import asyncio
import json
import os
from threading import Event
from uuid import uuid4

import httpx
import pytest

from jarvis.application.conversations import ConversationService
from jarvis.application.file_creation import FileCreateService
from jarvis.application.jobs import JobRunner
from jarvis.application.tools import ToolPolicy, ToolRegistry, ToolService
from jarvis.bootstrap import main
from jarvis.domain.tools import InvocationStatus
from jarvis.domain.types import (
    NoEffectFailure,
    RequestFailure,
    StaleTurnError,
    StreamDelta,
    StreamTerminal,
    StreamToolCall,
)
from jarvis.infrastructure import create_tools
from jarvis.infrastructure.create_tools import SafeFileCreator
from jarvis.infrastructure.read_tools import SafeReadTools
from jarvis.infrastructure.sqlite_store import SqliteStore
from jarvis.interfaces.web import create_web_app

BASE = "http://127.0.0.1:8765"
CONTENT = "Approved text\nSecond line\n"


class FollowupChat:
    def count_prompt_tokens_with_tools(self, messages, instructions, tools):
        return 100

    def stream_with_tools(self, messages, cancel, instructions, tools):
        yield StreamDelta("The approved file was created.")
        yield StreamTerminal()


def _tools(store, root, *, creator=None):
    creator = creator or SafeFileCreator([root], store.data_dir)
    return ToolService(
        store,
        SafeReadTools([], store.data_dir),
        ToolPolicy(version="m9-v1", capabilities=frozenset({"write_files"})),
        file_service=FileCreateService(store, creator),
    )


async def _waiting(store, tools, path, content=CONTENT):
    conversation = store.create_conversation("Create a text file")
    job_id = str(uuid4())
    turn, _ = store.begin_turn(
        conversation.id, "Create the requested file", "submission", job_id, "fake"
    )
    job = store.start_job(job_id)
    proposal = StreamToolCall(
        "file-call",
        "create_text_file",
        json.dumps({"path": str(path), "content": content}),
    )
    invocation = await tools.propose_and_run(turn.id, proposal, job.attempt)
    assert invocation.status is InvocationStatus.WAITING_APPROVAL
    assert not path.exists()
    store.mark_job_waiting(job_id)
    return conversation, job, turn, proposal, invocation


def test_web_exact_preview_requires_fresh_approval_and_never_overwrites(tmp_path):
    data = tmp_path / "data"
    root = tmp_path / "grant"
    root.mkdir()
    target = root / "letter.txt"
    with SqliteStore(data) as store:
        tools = _tools(store, root)
        conversation, job, turn, _, invocation = asyncio.run(
            _waiting(store, tools, target)
        )
        service = ConversationService(store, FollowupChat(), tool_service=tools)
        runner = JobRunner(store, service)
        app, token = create_web_app(service, runner, tools, 8765)

        async def scenario():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=BASE
            ) as client:
                assert (await client.get("/api/approvals")).status_code == 401
                response = await client.post(
                    "/api/setup", json={"token": token}, headers={"origin": BASE}
                )
                assert response.status_code == 200
                csrf = (await client.get("/api/session")).json()["csrf_token"]
                shown = (await client.get("/api/approvals")).json()["approvals"]
                assert shown[0]["preview"] == {"path": str(target), "content": CONTENT}
                assert shown[0]["job_id"] == job.id
                route = f"/api/approvals/{invocation.id}/approve"
                assert (await client.post(route)).status_code == 403
                assert not target.exists()
                headers = {"origin": BASE, "x-csrf-token": csrf}
                assert (await client.post(route, headers=headers)).status_code == 202
                await runner.wait_for_idle()
                assert target.read_text(encoding="utf-8") == CONTENT
                assert (await client.post(route, headers=headers)).status_code == 409
                assert target.read_text(encoding="utf-8") == CONTENT

        asyncio.run(scenario())
        assert store.get_invocation(invocation.id).status is InvocationStatus.COMPLETE
        assert store.get_answer(turn.id) is not None
        assert store.get_job(job.id).status.value == "succeeded"
        assert conversation.id


def test_changed_destination_invalidates_approval_even_if_removed(tmp_path):
    data = tmp_path / "data"
    root = tmp_path / "grant"
    root.mkdir()
    target = root / "note.txt"
    with SqliteStore(data) as store:
        tools = _tools(store, root)
        _, _, _, _, invocation = asyncio.run(_waiting(store, tools, target))
        approval = asyncio.run(tools.trusted_issue_approval(invocation.id))
        target.write_text("someone else's file", encoding="utf-8")
        target.unlink()
        with pytest.raises(RequestFailure):
            asyncio.run(tools.execute(invocation.id, approval))
        assert not target.exists()
        assert store.get_tool_approval(invocation.id).status == "invalidated"
        with pytest.raises((RequestFailure, StaleTurnError)):
            asyncio.run(tools.execute(invocation.id, approval))
        assert not target.exists()


def test_expired_approval_and_restart_before_action_write_nothing(tmp_path):
    data = tmp_path / "data"
    root = tmp_path / "grant"
    root.mkdir()
    target = root / "note.txt"
    with SqliteStore(data) as store:
        tools = _tools(store, root)
        _, _, _, _, invocation = asyncio.run(_waiting(store, tools, target))
        approval = store.issue_tool_approval(invocation.id, "2000-01-01T00:00:00+00:00")
        with pytest.raises(StaleTurnError):
            asyncio.run(tools.execute(invocation.id, approval.id))
        assert not target.exists()
    with SqliteStore(data) as reopened:
        assert (
            reopened.get_invocation(invocation.id).status is InvocationStatus.CANCELLED
        )
        assert reopened.get_tool_approval(invocation.id).status == "invalidated"
        assert not target.exists()


def test_crash_after_creation_reconciles_exact_file_without_replay(tmp_path):
    data = tmp_path / "data"
    root = tmp_path / "grant"
    root.mkdir()
    target = root / "note.txt"
    with SqliteStore(data) as store:
        creator = SafeFileCreator([root], data)
        tools = _tools(store, root, creator=creator)
        _, job, turn, proposal, invocation = asyncio.run(_waiting(store, tools, target))
        approval = asyncio.run(tools.trusted_issue_approval(invocation.id))
        store.record_tool_intent(
            invocation.id, "m9-v1", invocation.target_identity, approval
        )
        creator.create(str(target), CONTENT, invocation.target_identity)
        assert target.read_text(encoding="utf-8") == CONTENT
        assert store.get_invocation(invocation.id).status is InvocationStatus.INTENT
    assert (
        main(
            [
                "--data-dir",
                str(data),
                "--write-root",
                str(root),
                "reconcile-file",
                invocation.id,
            ]
        )
        == 0
    )
    with SqliteStore(data) as reopened:
        tools = _tools(reopened, root)
        result = reopened.get_invocation(invocation.id)
        assert result.status is InvocationStatus.COMPLETE
        assert json.loads(result.result_json)["data"]["sha256"]
        assert reopened.get_job_steps(job.id)[1].status == "complete"
        assert reopened.get_job(job.id).next_step == 2
        assert reopened.start_job(job.id).status.value == "running"
        replay = asyncio.run(tools.propose_and_run(turn.id, proposal))
        assert replay.id == invocation.id and replay.status is InvocationStatus.COMPLETE
        assert target.read_text(encoding="utf-8") == CONTENT


def test_unresolved_hash_remains_unknown_and_cannot_replay(tmp_path):
    data = tmp_path / "data"
    root = tmp_path / "grant"
    root.mkdir()
    target = root / "note.txt"
    with SqliteStore(data) as store:
        creator = SafeFileCreator([root], data)
        tools = _tools(store, root, creator=creator)
        conversation, _, turn, proposal, invocation = asyncio.run(
            _waiting(store, tools, target)
        )
        approval = asyncio.run(tools.trusted_issue_approval(invocation.id))
        store.record_tool_intent(
            invocation.id, "m9-v1", invocation.target_identity, approval
        )
        creator.create(str(target), CONTENT, invocation.target_identity)
    target.write_text("different", encoding="utf-8")
    with SqliteStore(data) as reopened:
        tools = _tools(reopened, root)
        assert asyncio.run(tools.reconcile_file(invocation.id)) is None
        assert (
            reopened.get_invocation(invocation.id).status
            is InvocationStatus.OUTCOME_UNKNOWN
        )
        assert target.read_text(encoding="utf-8") == "different"
        records, _ = reopened.get_recent_context(conversation.id, None)
        assert any(
            item.role == "tool" and "outcome_unknown" in item.text
            for item in records
            if hasattr(item, "role")
        )
        with pytest.raises(RequestFailure):
            asyncio.run(tools.propose_and_run(turn.id, proposal))
        service = ConversationService(reopened, FollowupChat(), tool_service=tools)
        app, token = create_web_app(service, JobRunner(reopened, service), tools, 8765)

        async def check_browser_reconciliation():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=BASE
            ) as client:
                assert (
                    await client.post(f"/api/file-reconciliation/{invocation.id}")
                ).status_code == 401
                assert (
                    await client.post(
                        "/api/setup", json={"token": token}, headers={"origin": BASE}
                    )
                ).status_code == 200
                csrf = (await client.get("/api/session")).json()["csrf_token"]
                listing = (await client.get("/api/approvals")).json()["uncertain_files"]
                assert listing[0]["path"] == str(target)
                assert (
                    await client.post(
                        f"/api/file-reconciliation/{invocation.id}",
                        headers={"origin": BASE, "x-csrf-token": csrf},
                    )
                ).status_code == 409

        asyncio.run(check_browser_reconciliation())


def test_parent_replacement_and_reparse_attempt_are_rejected(tmp_path):
    data = tmp_path / "data"
    root = tmp_path / "grant"
    parent = root / "parent"
    outside = tmp_path / "outside"
    parent.mkdir(parents=True)
    outside.mkdir()
    creator = SafeFileCreator([root], data)
    target = parent / "note.txt"
    identity = creator.inspect(str(target), CONTENT)
    parent.rename(root / "old-parent")
    parent.mkdir()
    with pytest.raises(NoEffectFailure):
        creator.create(str(target), CONTENT, identity)
    assert not target.exists()
    parent.rmdir()
    try:
        os.symlink(outside, parent, target_is_directory=True)
    except OSError:
        return
    with pytest.raises((RequestFailure, OSError)):
        creator.create(str(target), CONTENT, identity)
    assert not (outside / "note.txt").exists()


def test_ancestor_handles_block_rename_during_exclusive_create(tmp_path, monkeypatch):
    data = tmp_path / "data"
    root = tmp_path / "grant"
    parent = root / "parent"
    parent.mkdir(parents=True)
    creator = SafeFileCreator([root], data)
    target = parent / "note.txt"
    identity = creator.inspect(str(target), CONTENT)
    original_open = create_tools._open
    attempted = False

    def race(path, access, sharing, disposition, flags):
        nonlocal attempted
        if disposition == create_tools._CREATE_NEW:
            attempted = True
            with pytest.raises(OSError):
                parent.rename(root / "moved-parent")
        return original_open(path, access, sharing, disposition, flags)

    monkeypatch.setattr(create_tools, "_open", race)
    creator.create(str(target), CONTENT, identity)
    assert attempted and target.read_text(encoding="utf-8") == CONTENT


def test_collision_and_disk_full_keep_effect_inspectable(tmp_path, monkeypatch):
    data = tmp_path / "data"
    root = tmp_path / "grant"
    root.mkdir()
    creator = SafeFileCreator([root], data)
    target = root / "note.txt"
    with pytest.raises(RequestFailure):
        ToolRegistry().parse(
            StreamToolCall(
                "invalid-utf8",
                "create_text_file",
                json.dumps({"path": str(target), "content": "\ud800"}),
            )
        )
    with pytest.raises(RequestFailure):
        creator.inspect(str(target), "\ud800")
    with pytest.raises(RequestFailure):
        creator.inspect(str(target), "hidden\u202econtrol")
    with pytest.raises(RequestFailure):
        ToolRegistry().parse(
            StreamToolCall(
                "hidden-control",
                "create_text_file",
                json.dumps({"path": str(target), "content": "hidden\u202econtrol"}),
            )
        )
    identity = creator.inspect(str(target), CONTENT)
    target.write_text("existing", encoding="utf-8")
    with pytest.raises(NoEffectFailure):
        creator.create(str(target), CONTENT, identity)
    assert target.read_text(encoding="utf-8") == "existing"
    with pytest.raises(RequestFailure):
        creator.inspect(str(root / ".." / "escaped.txt"), CONTENT)
    with pytest.raises(RequestFailure):
        creator.inspect(str(root / "config.txt"), CONTENT)
    with pytest.raises(RequestFailure):
        creator.inspect(str(root / "CONIN$.txt"), CONTENT)
    with pytest.raises(ValueError):
        SafeFileCreator([data], data)
    target.unlink()
    with SqliteStore(data) as store:
        tools = _tools(store, root)
        _, _, _, _, invocation = asyncio.run(_waiting(store, tools, target))
        approval = asyncio.run(tools.trusted_issue_approval(invocation.id))
        monkeypatch.setattr(
            "jarvis.infrastructure.create_tools.os.fsync",
            lambda _fd: (_ for _ in ()).throw(OSError("disk full")),
        )
        with pytest.raises(OSError):
            asyncio.run(tools.execute(invocation.id, approval))
        assert (
            store.get_invocation(invocation.id).status
            is InvocationStatus.OUTCOME_UNKNOWN
        )
        assert target.exists()


def test_collision_after_approval_commits_no_effect_result(tmp_path):
    data = tmp_path / "data"
    root = tmp_path / "grant"
    root.mkdir()
    target = root / "note.txt"

    class CollidingCreator(SafeFileCreator):
        def create(self, path, content, expected):
            target.write_text("other owner", encoding="utf-8")
            return super().create(path, content, expected)

    with SqliteStore(data) as store:
        tools = _tools(store, root, creator=CollidingCreator([root], data))
        _, _, _, _, invocation = asyncio.run(_waiting(store, tools, target))
        approval = asyncio.run(tools.trusted_issue_approval(invocation.id))
        result = asyncio.run(tools.execute(invocation.id, approval))
        assert result.status is InvocationStatus.COMPLETE
        assert json.loads(result.result_json) == {
            "code": "invalid_request",
            "status": "error",
        }
        assert json.loads(result.receipt_json)["status"] == "not_executed"
        assert target.read_text(encoding="utf-8") == "other owner"


def test_empty_text_file_is_a_valid_approved_creation(tmp_path):
    data = tmp_path / "data"
    root = tmp_path / "grant"
    root.mkdir()
    target = root / "empty.txt"
    with SqliteStore(data) as store:
        tools = _tools(store, root)
        _, _, _, _, invocation = asyncio.run(_waiting(store, tools, target, ""))
        approval = asyncio.run(tools.trusted_issue_approval(invocation.id))
        result = asyncio.run(tools.execute(invocation.id, approval))
        assert result.status is InvocationStatus.COMPLETE
        assert target.read_bytes() == b""


def test_destination_change_after_intent_is_known_no_effect(tmp_path):
    data = tmp_path / "data"
    root = tmp_path / "grant"
    root.mkdir()
    target = root / "note.txt"

    class ChangeAfterIntent(SafeFileCreator):
        calls = 0

        def inspect(self, path, content):
            self.calls += 1
            if self.calls == 3:
                target.write_text("external change", encoding="utf-8")
            return super().inspect(path, content)

    with SqliteStore(data) as store:
        tools = _tools(store, root, creator=ChangeAfterIntent([root], data))
        _, _, _, _, invocation = asyncio.run(_waiting(store, tools, target))
        approval = asyncio.run(tools.trusted_issue_approval(invocation.id))
        result = asyncio.run(tools.execute(invocation.id, approval))
        assert result.status is InvocationStatus.COMPLETE
        assert json.loads(result.receipt_json)["status"] == "not_executed"
        assert target.read_text(encoding="utf-8") == "external change"


def test_cancellation_after_creation_keeps_completed_receipt(tmp_path):
    data = tmp_path / "data"
    root = tmp_path / "grant"
    root.mkdir()
    created = Event()
    release = Event()

    class SlowCreator(SafeFileCreator):
        def create(self, path, content, expected):
            result = super().create(path, content, expected)
            created.set()
            assert release.wait(5)
            return result

    with SqliteStore(data) as store:
        tools = _tools(store, root, creator=SlowCreator([root], data))
        target = root / "note.txt"
        _, _, _, _, invocation = asyncio.run(_waiting(store, tools, target))
        approval = asyncio.run(tools.trusted_issue_approval(invocation.id))

        async def scenario():
            task = asyncio.create_task(tools.execute(invocation.id, approval))
            assert await asyncio.to_thread(created.wait, 5)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        assert target.read_text(encoding="utf-8") == CONTENT
        assert store.get_invocation(invocation.id).status is InvocationStatus.COMPLETE
        assert json.loads(store.get_invocation(invocation.id).receipt_json)["sha256"]
