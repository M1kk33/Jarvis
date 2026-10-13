"""Whole-source coverage, exact model budgets and durable page checkpoints."""

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import replace
from importlib import resources
from threading import Event
from uuid import uuid4

import httpx
import pytest

from jarvis.application.context import ContextBuilder, PreflightTokenEstimator
from jarvis.application.conversations import ConversationService
from jarvis.application.jobs import JobRunner
from jarvis.application.pages import PageAnalysisService
from jarvis.application.tools import ToolPolicy, ToolService
from jarvis.domain.pages import PageNote
from jarvis.domain.tools import ToolContextRecord
from jarvis.domain.types import (
    RequestFailure,
    StaleTurnError,
    StorageFailure,
    StreamDelta,
    StreamTerminal,
    StreamToolCall,
)
from jarvis.infrastructure.backup import create_backup, restore_backup
from jarvis.infrastructure.read_tools import SafeReadTools
from jarvis.infrastructure.sqlite_store import SqliteStore
from jarvis.infrastructure.web_fetch import _Article
from jarvis.interfaces.web import create_web_app


def sql(store, statement, params=()):
    return store._worker.submit(
        lambda: store._conn.execute(statement, params).fetchall()
    ).result()


URL = "https://example.com/long"


class PageBackend:
    def __init__(self, text):
        self.text = text
        self.calls = 0

    def inspect(self, name, arguments):
        return "GET " + arguments["url"]

    def execute(self, name, arguments):
        self.calls += 1
        return {
            "url": arguments["url"],
            "status": 200,
            "content_type": "text/html",
            "retrieved_at": "2026-10-13T00:00:00+00:00",
            "text": self.text,
            "truncated": False,
            "trust": "untrusted",
        }


class PageModel:
    def __init__(self, ratio=4):
        self.ratio = ratio
        self.calls = []
        self.final_records = None
        self.bad = False
        self.closed = 0

    def count_prompt_tokens(self, messages, instructions):
        return (
            sum(len(m.text) for m in messages) + len(instructions)
        ) // self.ratio + 20

    def count_prompt_tokens_with_tools(self, messages, instructions, tools):
        return (
            sum(len(m.text) for m in messages)
            + len(instructions)
            + len(json.dumps(tools))
        ) // self.ratio + 20

    def count_output_tokens(self, text):
        return len(text) // self.ratio + 1

    def stream_bounded(
        self,
        messages,
        cancel,
        instructions,
        output_tokens,
        choices=(),
        response_schema=None,
    ):
        payload = json.loads(messages[0].text)
        if payload.get("task") == "review":
            yield StreamDelta(json.dumps({"overview": False, "missing": []}))
            yield StreamTerminal()
            return
        self.calls.append(
            (payload, self.count_prompt_tokens(messages, instructions), output_tokens)
        )
        units = payload["DATA"]
        result = {}
        for region in payload["units"]:
            candidates = [unit for unit in units if unit["id"] in region["passages"]]
            chosen = next(
                (unit for unit in candidates if "TAIL_FACT" in unit["text"]),
                candidates[0],
            )
            selected = chosen["id"]
            assert selected in choices
            if self.bad:
                selected = "UNKNOWN_SOURCE"
            result[region["id"]] = {
                "topic": "Source facts",
                "summary": chosen["text"][:120],
                "selected": [selected],
            }
        try:
            yield StreamDelta(json.dumps({"units": result}))
            yield StreamTerminal()
        finally:
            self.closed += 1

    def stream_with_tools(self, records, cancel, instructions, tools):
        self.final_records = records
        yield StreamDelta("Answer grounded in saved notes.")
        yield StreamTerminal()


async def setup(store, text, *, window=2048, output=512, calls=12, ratio=4):
    chat = store.create_conversation("Large page")
    job_id = str(uuid4())
    turn, _ = store.begin_turn(
        chat.id, "Summarize this page: " + URL, "submission", job_id, "fake"
    )
    job = store.start_job(job_id)
    backend = PageBackend(text)
    tools = ToolService(
        store,
        SafeReadTools([], store.data_dir),
        ToolPolicy(capabilities=frozenset({"network"})),
        web_backend=backend,
        save_pages=True,
    )
    invocation = await tools.propose_and_run(
        turn.id,
        StreamToolCall("fetch", "fetch_public_page", json.dumps({"url": URL})),
        job.attempt,
    )
    approval = await tools.trusted_issue_approval(invocation.id)
    await tools.execute(invocation.id, approval)
    model = PageModel(ratio)
    context = ContextBuilder(
        PreflightTokenEstimator(),
        window=window,
        output_allowance=output,
        safety_margin=128,
    )
    pages = PageAnalysisService(
        store, model, model, context, "fixture-model", calls_per_segment=calls
    )
    service = ConversationService(
        store, model, context=context, tool_service=tools, page_analysis=pages
    )
    return (
        chat,
        turn,
        job,
        backend,
        model,
        service,
        JobRunner(store, service),
        invocation,
    )


def source(size=40000):
    return (
        "Paragraph with facts, numbers 123 and formula x = y + 1.\n\n" * (size // 55)
    ) + "TAIL_FACT: the end matters 987."


def test_full_source_tail_exact_budgets_merging_and_duplicate_submission(tmp_path):
    text = source(200000)

    async def scenario(store):
        chat, _turn, job, backend, model, service, runner, invocation = await setup(
            store, text
        )
        for _ in range(10):
            completed = await runner.run_job(job.id)
            if completed.status.value == "succeeded":
                break
            assert completed.progress == "page_paused"
        assert completed.status.value == "succeeded"
        assert backend.calls == 1
        assert len(model.calls) > 1 and model.closed == len(model.calls)
        assert any(payload["source"] == "merge" for payload, _, _ in model.calls)
        assert all(
            count + output + service.context.safety_margin <= service.context.window
            for _, count, output in model.calls
        )
        reads = [
            payload for payload, _, _ in model.calls if payload["source"] != "merge"
        ]
        assert any("TAIL_FACT" in json.dumps(item["DATA"]) for item in reads)
        prepared = next(
            record
            for record in model.final_records
            if isinstance(record, ToolContextRecord) and record.role == "tool"
        )
        data = json.loads(prepared.text)["data"]
        assert data["representation"] == "analyzed_notes"
        assert "TAIL_FACT" in data["text"] and not data["truncated"]
        assert (
            model.count_prompt_tokens_with_tools(
                model.final_records, "", service.tool_service.schemas()
            )
            <= service.context.window
            - service.context.output_allowance
            - service.context.safety_margin
        )
        info = store.page_analysis_metadata(chat.id)[0]
        assert info["processed"] == info["total"] and info["status"] == "complete"
        assert info["input_tokens"] > 0 and info["output_tokens"] > 0
        assert store.get_page_snapshot(invocation.id).text == text
        before = len(model.calls)
        duplicate, reused = await runner.submit(
            chat.id, "Summarize this page: " + URL, "submission"
        )
        assert reused and duplicate.id == job.id
        await runner.run_job(job.id)
        assert len(model.calls) == before and backend.calls == 1
        assert len(store.get_messages(chat.id)) == 2

    with SqliteStore(tmp_path) as store:
        asyncio.run(scenario(store))


def test_pause_restart_resume_reuses_notes_and_exceeds_three_segments(tmp_path):
    async def first(store):
        items = await setup(store, source(24000), calls=1)
        *_, runner, invocation = items
        job = items[2]
        paused = await runner.run_job(job.id)
        assert paused.status.value == "interrupted" and paused.progress == "page_paused"
        assert store.get_answer(job.turn_id) is None
        assert store.page_analysis_metadata(items[0].id)[0]["processed"] == 1
        # Leave the next attempt running on disk to exercise process-loss recovery.
        store.start_job(job.id)
        return items[0].id, job.id, invocation.id

    with SqliteStore(tmp_path) as store:
        chat_id, job_id, invocation_id = asyncio.run(first(store))
    with SqliteStore(tmp_path) as store:
        assert store.get_job(job_id).progress == "restart_interrupted"
        model = PageModel()
        backend = PageBackend("MUST NOT BE FETCHED")
        tools = ToolService(
            store,
            SafeReadTools([], store.data_dir),
            ToolPolicy(capabilities=frozenset({"network"})),
            web_backend=backend,
            save_pages=True,
        )
        context = ContextBuilder(
            PreflightTokenEstimator(),
            window=2048,
            output_allowance=512,
            safety_margin=128,
        )
        pages = PageAnalysisService(
            store, model, model, context, "fixture-model", calls_per_segment=1
        )
        service = ConversationService(
            store, model, tool_service=tools, context=context, page_analysis=pages
        )
        runner = JobRunner(store, service)

        async def resume():
            for _ in range(40):
                job = await runner.run_job(job_id)
                if job.status.value == "succeeded":
                    return job
                assert job.progress == "page_paused"
            pytest.fail("Analysis did not make bounded progress")

        completed = asyncio.run(resume())
        assert completed.attempt > 3
        assert backend.calls == 0
        read_ids = [p["source"] for p, _, _ in model.calls if p["source"] != "merge"]
        assert "P1" not in read_ids and len(read_ids) == len(set(read_ids))
        snapshot = store.get_page_snapshot(invocation_id)
        backup = create_backup(store)
        restored_dir = tmp_path.parent / "restored-page"
        restore_backup(backup, restored_dir)
        with SqliteStore(restored_dir) as restored:
            assert restored.get_page_snapshot(invocation_id) == snapshot
            assert restored.page_analysis_metadata(
                chat_id
            ) == store.page_analysis_metadata(chat_id)
        store.delete_conversation(chat_id)
        assert store.get_page_snapshot(invocation_id) is None
        assert store.page_analysis_metadata(chat_id) == []


@pytest.mark.parametrize(
    "window,output,ratio",
    [(2048, 512, 4), (4096, 1024, 4), (2048, 512, 2), (8192, 512, 4)],
)
def test_plan_uses_actual_window_tokenizer_and_complete_overlapping_ranges(
    tmp_path, window, output, ratio
):
    with SqliteStore(tmp_path) as store:

        async def scenario():
            (
                _chat,
                turn,
                _job,
                _backend,
                _model,
                service,
                _runner,
                invocation,
            ) = await setup(store, source(), window=window, output=output, ratio=ratio)
            pages = service.page_analysis
            snapshot = store.get_page_snapshot(invocation.id)
            user = store.get_user_message(turn.id)
            plan = await pages._plan(
                snapshot, user, min(512, output, window // 6), Event()
            )
            assert plan[0][0] == 0 and plan[-1][1] == len(snapshot.text)
            assert len(plan) > 1
            for index, (start, end) in enumerate(plan):
                assert end > start
                assert (
                    await pages._count(
                        user,
                        pages._payload(
                            user, snapshot, f"P{index + 1}", snapshot.text[start:end]
                        ),
                    )
                    + min(512, output, window // 6)
                    + context_reserve(service)
                    <= window
                )
                if index:
                    assert plan[index - 1][0] < start < plan[index - 1][1]

        asyncio.run(scenario())


def context_reserve(service):
    return service.context.safety_margin


def test_invalid_quotes_do_not_become_committed_evidence(tmp_path):
    with SqliteStore(tmp_path) as store:

        async def scenario():
            (
                chat,
                turn,
                job,
                _backend,
                model,
                _service,
                runner,
                invocation,
            ) = await setup(store, source())
            model.bad = True
            paused = await runner.run_job(job.id)
            assert (
                paused.status.value == "interrupted"
                and paused.error_code == "page_analysis_failed"
            )
            meta = store.page_analysis_metadata(chat.id)[0]
            assert meta["processed"] == 0 and meta["status"] == "reading"
            assert store.get_answer(turn.id) is None
            assert store.get_page_snapshot(invocation.id).text == source()
            assert model.closed == 1

        asyncio.run(scenario())


def test_checkpoint_stale_attempt_and_cancelled_turn_are_rejected(tmp_path):
    with SqliteStore(tmp_path) as store:

        async def scenario():
            (
                chat,
                _turn,
                job,
                _backend,
                _model,
                _service,
                runner,
                _invocation,
            ) = await setup(store, source(), calls=1)
            await runner.run_job(job.id)
            identity = store.page_analysis_metadata(chat.id)[0]["id"]
            original = store.get_page_notes(identity, "read")[0]
            active = store.start_job(job.id)
            with pytest.raises(StaleTurnError):
                store.commit_page_note(identity, original, job.attempt)
            store.commit_page_note(identity, original, active.attempt)
            with pytest.raises(StaleTurnError):
                store.commit_page_note(
                    identity, replace(original, text="changed"), active.attempt
                )
            store.request_job_cancel(job.id)
            with pytest.raises(StaleTurnError):
                store.commit_page_note(
                    identity, PageNote("read", 1, "later", 1, 1), active.attempt
                )
            store.settle_job_cancel(job.id)
            assert store.get_page_notes(identity, "read") == [original]

        asyncio.run(scenario())


def test_small_page_needs_no_intermediate_generation_and_saved_text_is_authenticated(
    tmp_path,
):
    with SqliteStore(tmp_path) as store:

        async def scenario():
            chat, _turn, job, backend, model, service, runner, invocation = await setup(
                store, "Small page with a fact."
            )
            await runner.run_job(job.id)
            assert model.calls == [] and backend.calls == 1
            app, token = create_web_app(service, runner, service.tool_service, 8765)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
            ) as client:
                path = f"/api/pages/{invocation.id}"
                assert (await client.get(path)).status_code == 401
                await client.post(
                    "/api/setup",
                    json={"token": token},
                    headers={"origin": "http://127.0.0.1:8765"},
                )
                result = (await client.get(path)).json()
                assert (
                    result["text"] == "Small page with a fact."
                    and result["source"]["url"] == URL
                )
                assert (await client.get("/api/pages/missing")).status_code == 404
                assert (await client.get(f"/api/conversations/{chat.id}")).json()[
                    "pages"
                ] == []

        asyncio.run(scenario())


def test_extractor_keeps_endnotes_tables_math_and_paragraph_boundaries():
    parser = _Article()
    parser.feed(
        "<html><head><title>Skip</title></head><nav>Menu</nav><main><h1>Title</h1><p>"
        + "Main facts. " * 30
        + "</p><table><tr><td>A</td><td>12</td></tr><tr><td>B</td><td>34</td></tr></table><script type='math/tex'>\\mathcal{F} = x^2</script><script>malicious()</script><span hidden>hidden</span></main><footer><p>Endnotes: TAIL_FACT 987.</p></footer></html>"
    )
    text = parser.text()
    assert "TAIL_FACT 987" in text and "\\mathcal{F} = x^2" in text
    assert "A 12\n\nB 34" in text and "Title\n\nMain facts" in text
    assert all(
        excluded not in text for excluded in ("Skip", "Menu", "malicious", "hidden")
    )


def test_evidence_that_cannot_fit_fails_explicitly_instead_of_repeat_pause(tmp_path):
    with SqliteStore(tmp_path) as store:

        async def scenario():
            chat, turn, job, backend, model, service, _runner, invocation = await setup(
                store, source(2000)
            )
            service.context.window = 1200
            service.context.output_allowance = 900
            records, _ = store.get_recent_context(chat.id, None)
            with pytest.raises(RequestFailure) as failure:
                await service.page_analysis.prepare(
                    records,
                    store.get_user_message(turn.id),
                    service.tool_service.schemas(),
                    "",
                    Event(),
                    job.attempt,
                )
            assert failure.value.code == "context_limit"
            assert 0 < len(model.calls) < 6 and backend.calls == 1
            assert store.get_answer(turn.id) is None
            assert store.get_page_snapshot(invocation.id).text == source(2000)

        asyncio.run(scenario())


def test_internal_provider_output_limit_is_per_call_and_tools_are_disabled():
    from test_reply_limits import _events, _provider

    provider, requests = _provider(_events(["notes"]))
    assert (
        list(provider.stream_bounded([], Event(), "Notes only", 256))[-1]
        == StreamTerminal()
    )
    assert provider.max_output_tokens == 2048
    assert requests[0]["max_tokens"] == 256 and not requests[0]["parse_tool_calls"]
    assert (
        requests[0]["response_format"]["json_schema"]["schema"]["properties"][
            "evidence"
        ]["maxItems"]
        == 3
    )
    for invalid in (0, 2049, True):
        with pytest.raises(ValueError):
            provider.stream_bounded([], Event(), "", invalid)
    list(provider.stream_bounded([], Event(), "Choose an ID", 128, ("S1", "S2")))
    schema = requests[-1]["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["selected"]["items"]["enum"] == ["S1", "S2"]


def test_full_fetch_keeps_text_after_old_ten_kib_limit_and_rejects_oversize():
    from test_m13 import Response, fetcher

    from jarvis.domain.types import RequestFailure

    text = "<p>" + "Visible facts. " * 2000 + "</p><p>TAIL_FACT 987.</p>"
    tool, connection, _ = fetcher(Response(text.encode()))
    tool.full_text = True
    result = tool.execute("fetch_public_page", {"url": URL})
    assert len(result["text"]) > 10240 and "TAIL_FACT 987" in result["text"]
    assert not result["truncated"] and len(connection.requests) == 1
    tool, _, _ = fetcher(
        Response(
            b"too big",
            headers={
                "Content-Type": "text/html",
                "Content-Length": str(256 * 1024 + 1),
            },
        )
    )
    tool.full_text = True
    with pytest.raises(RequestFailure) as failure:
        tool.execute("fetch_public_page", {"url": URL})
    assert failure.value.code == "page_too_large"


def test_cancel_during_second_chunk_settles_stream_and_keeps_first_checkpoint(tmp_path):
    with SqliteStore(tmp_path) as store:

        async def scenario():
            chat, turn, job, backend, model, _service, runner, invocation = await setup(
                store, source()
            )
            entered = Event()
            original = model.stream_bounded

            def blocking(
                messages, cancel, instructions, output, choices=(), response_schema=None
            ):
                if not model.calls:
                    yield from original(
                        messages, cancel, instructions, output, choices, response_schema
                    )
                else:
                    entered.set()
                    try:
                        assert cancel.wait(3)
                        yield StreamTerminal("cancelled")
                    finally:
                        model.closed += 1

            model.stream_bounded = blocking
            task = asyncio.create_task(runner.run_job(job.id))
            assert await asyncio.to_thread(entered.wait, 3)
            await runner.cancel(job.id)
            result = await task
            assert result.status.value == "cancelled" and model.closed == 2
            assert store.page_analysis_metadata(chat.id)[0]["processed"] == 1
            assert store.get_page_snapshot(invocation.id).text == source()
            assert store.get_answer(turn.id) is None and backend.calls == 1

        asyncio.run(scenario())


def test_cancelled_checkpoint_await_verifies_the_committed_note(tmp_path):
    with SqliteStore(tmp_path) as store:

        async def scenario():
            (
                chat,
                turn,
                job,
                _backend,
                _model,
                _service,
                runner,
                _invocation,
            ) = await setup(store, source())
            original = store.run
            verified = []

            async def cancel_after_commit(operation):
                result = await original(operation)
                names = getattr(getattr(operation, "__code__", None), "co_names", ())
                if "commit_page_note" in names:
                    raise asyncio.CancelledError
                if "get_page_notes" in names:
                    verified.append(True)
                return result

            store.run = cancel_after_commit
            result = await runner.run_job(job.id)
            assert result.status.value == "cancelled" and verified
            assert store.page_analysis_metadata(chat.id)[0]["processed"] == 1
            assert store.get_answer(turn.id) is None

        asyncio.run(scenario())


def test_snapshot_and_result_commit_roll_back_together(tmp_path):
    with SqliteStore(tmp_path) as store:

        async def scenario():
            (
                _chat,
                turn,
                job,
                _backend,
                _model,
                service,
                _runner,
                invocation,
            ) = await setup(store, "source")
            # A new invocation exercises the same transaction with a forced
            # result-write failure after the page insert.
            second = await service.tool_service.propose_and_run(
                turn.id,
                StreamToolCall("fetch2", "fetch_public_page", json.dumps({"url": URL})),
                job.attempt,
            )
            approval = await service.tool_service.trusted_issue_approval(second.id)
            sql(
                store,
                "CREATE TRIGGER reject_result BEFORE UPDATE OF result_json ON tool_invocations WHEN NEW.id='"
                + second.id
                + "' BEGIN SELECT RAISE(ABORT,'injected'); END",
            )
            with pytest.raises(StorageFailure):
                await service.tool_service.execute(second.id, approval)
            assert store.get_page_snapshot(second.id) is None
            assert store.get_invocation(second.id).status.value == "intent"
            assert store.get_page_snapshot(invocation.id).text == "source"

        asyncio.run(scenario())


@pytest.mark.parametrize("failure_version", [None, 14, 15])
def test_old_database_upgrade_preserves_rows_and_rebuild_rollback(
    tmp_path, failure_version
):
    root = resources.files("jarvis").joinpath("infrastructure", "migrations")
    with sqlite3.connect(tmp_path / "jarvis.db") as db:
        db.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL,applied_at TEXT NOT NULL)"
        )
        for item in sorted(root.iterdir(), key=lambda path: path.name):
            version = int(item.name.split("_")[0])
            if version > 13:
                continue
            script = item.read_text(encoding="utf-8")
            db.executescript(script)
            db.execute(
                "INSERT INTO schema_migrations VALUES(?,?,?)",
                (version, hashlib.sha256(script.encode()).hexdigest(), "then"),
            )
            db.commit()
        db.execute(
            "INSERT INTO conversations(id,title,created_at) VALUES('old','Existing history','then')"
        )
        db.execute(
            "INSERT INTO turns(id,conversation_id,position,submission_id,status,created_at) VALUES('turn','old',1,'stable','complete','then')"
        )
        db.execute(
            "INSERT INTO jobs VALUES('job','turn','old','chat',1,'fake','succeeded',1,1,'then','succeeded',0,NULL,'then','then')"
        )
        db.execute(
            "INSERT INTO job_steps VALUES('step','job',0,'chat',NULL,'complete',1,'then','then')"
        )
        db.execute(
            "INSERT INTO messages VALUES('message','old','turn',1,'user','[{\"type\":\"text\",\"text\":\"Preserved input\"}]','complete','then')"
        )
        before = {
            name: db.execute(f"SELECT * FROM {name}").fetchall()
            for name in ("jobs", "job_steps", "messages", "turns", "conversations")
        }
        if failure_version:
            db.execute(
                f"CREATE TRIGGER reject_migration BEFORE INSERT ON schema_migrations WHEN NEW.version={failure_version} BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
    if failure_version:
        with pytest.raises(sqlite3.IntegrityError):
            SqliteStore(tmp_path)
        with sqlite3.connect(tmp_path / "jarvis.db") as db:
            for name, rows in before.items():
                assert db.execute(f"SELECT * FROM {name}").fetchall() == rows
            assert not db.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('jobs_new','job_steps_new')"
            ).fetchall()
            if failure_version == 14:
                assert not db.execute(
                    "SELECT name FROM sqlite_master WHERE name='page_snapshots'"
                ).fetchall()
            db.execute("DROP TRIGGER reject_migration")
    with SqliteStore(tmp_path) as store:
        for name, rows in before.items():
            assert [tuple(row) for row in sql(store, f"SELECT * FROM {name}")] == rows
        assert sql(store, "PRAGMA foreign_keys")[0][0] == 1
        assert not sql(store, "PRAGMA foreign_key_check")
        assert sql(store, "PRAGMA quick_check")[0][0] == "ok"
        assert store.get_messages("old")[0].text == "Preserved input"
