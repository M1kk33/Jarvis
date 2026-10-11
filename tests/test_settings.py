"""Durable settings, migration recovery and the authenticated preferences API."""

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from threading import Event

import httpx
import pytest

from jarvis.application.conversations import ConversationService
from jarvis.application.jobs import JobRunner
from jarvis.application.settings import SettingsService
from jarvis.domain.preferences import Preferences
from jarvis.domain.types import StorageFailure
from jarvis.infrastructure.backup import create_backup, restore_backup
from jarvis.infrastructure.providers.fake import FakeChatProvider
from jarvis.infrastructure.sqlite_store import SqliteStore
from jarvis.interfaces.web import create_web_app


@pytest.mark.parametrize("limit", [512, 1024, 2048])
def test_startup_uses_saved_limit_for_provider_and_context_together(
    tmp_path, monkeypatch, limit
):
    from jarvis import bootstrap
    from jarvis.infrastructure.gpu_lock import NamedGpuLock
    from jarvis.infrastructure.providers.llama_cpp import LlamaChatProvider
    from jarvis.interfaces import web

    with SqliteStore(tmp_path) as store:
        store.set_preferences(Preferences(output_tokens=limit))

    class Captured(Exception):
        pass

    def capture(service, *_args, settings, **_kwargs):
        assert isinstance(service.provider, LlamaChatProvider)
        assert service.provider.max_output_tokens == limit
        assert service.context.output_allowance == limit
        assert service.context.window == 4096
        assert service.context.safety_margin == 256
        assert settings.active_output_tokens == limit
        raise Captured()

    monkeypatch.setattr(web, "create_web_app", capture)
    monkeypatch.setattr(
        bootstrap,
        "NamedGpuLock",
        lambda device: NamedGpuLock(device, tmp_path / "locks"),
    )
    profile = (
        Path(__file__).resolve().parents[1]
        / "profiles/qwen3-4b-instruct-2507.example.toml"
    )
    with pytest.raises(Captured):
        bootstrap.main(["--data-dir", str(tmp_path), "--profile", str(profile), "web"])


def settings_service(store):
    return SettingsService(
        store,
        model_name="Installed model",
        active_output_tokens=2048,
        context_tokens=4096,
    )


def test_preferences_persist_across_restart_and_backup_without_changing_history(
    tmp_path,
):
    data = tmp_path / "data"
    selected = Preferences(
        theme="light",
        accent="blue",
        text_size=18,
        send_shortcut="ctrl_enter",
        voice_volume=35,
        output_tokens=1024,
    )
    with SqliteStore(data) as store:
        assert store.get_preferences() == Preferences()
        conversation = store.create_conversation("Keep")
        turn, _ = store.begin_turn(conversation.id, "Question", "one")
        store.complete_turn(turn.id, "Answer")
        messages = store.get_messages(conversation.id)
        assert store.set_preferences(selected) == selected
        backup = create_backup(store)
    with SqliteStore(data) as store:
        assert store.get_preferences() == selected
        assert store.get_messages(conversation.id) == messages
    restore_backup(backup, tmp_path / "restore")
    with SqliteStore(tmp_path / "restore") as store:
        assert store.get_preferences() == selected


def test_v9_preferences_migration_retries_after_failure_without_erasing_history(
    tmp_path,
):
    with SqliteStore(tmp_path) as store:
        conversation = store.create_conversation("Existing history")
        store.set_conversation_pinned(conversation.id, True)
    with closing(sqlite3.connect(tmp_path / "jarvis.db")) as db:
        db.executescript(
            "DROP TABLE app_preferences; DELETE FROM schema_migrations WHERE version=10; CREATE TRIGGER reject_settings BEFORE INSERT ON schema_migrations WHEN NEW.version=10 BEGIN SELECT RAISE(ABORT, 'injected'); END;"
        )
    with pytest.raises(sqlite3.IntegrityError):
        SqliteStore(tmp_path)
    with closing(sqlite3.connect(tmp_path / "jarvis.db")) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='app_preferences'"
            ).fetchone()[0]
            == 0
        )
        db.execute("DROP TRIGGER reject_settings")
        db.commit()
    with SqliteStore(tmp_path) as store:
        assert store.get_conversation(conversation.id).pinned
        assert store.get_preferences() == Preferences()


def test_save_failure_does_not_replace_committed_preferences(tmp_path):
    with SqliteStore(tmp_path) as store:
        store.set_preferences(Preferences(voice_volume=25))
        store.run_sync(
            lambda: store._conn.executescript(
                "CREATE TRIGGER reject_settings BEFORE UPDATE ON app_preferences BEGIN SELECT RAISE(ABORT, 'injected'); END;"
            )
        )
        with pytest.raises(StorageFailure):
            asyncio.run(settings_service(store).save(Preferences(voice_volume=70)))
        assert store.get_preferences().voice_volume == 25


def test_cancelled_settings_await_verifies_committed_preferences(tmp_path, monkeypatch):
    async def scenario():
        with SqliteStore(tmp_path) as store:
            entered, release = Event(), Event()
            original = store.set_preferences
            selected = Preferences(output_tokens=512)

            def delayed(value):
                result = original(value)
                entered.set()
                assert release.wait(3)
                return result

            monkeypatch.setattr(store, "set_preferences", delayed)
            task = asyncio.create_task(settings_service(store).save(selected))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                task.cancel()
            finally:
                release.set()
            assert await task == selected
            assert store.get_preferences() == selected

    asyncio.run(scenario())


def test_settings_api_is_strict_authenticated_and_does_not_mutate_active_budget(
    tmp_path,
):
    async def scenario():
        with SqliteStore(tmp_path) as store:
            provider = FakeChatProvider()
            service = ConversationService(store, provider)
            settings = settings_service(store)
            app, token = create_web_app(
                service, JobRunner(store, service), None, 8765, settings=settings
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
            ) as client:
                assert (await client.get("/api/settings")).status_code == 401
                await client.post("/api/setup", json={"token": token})
                session = (await client.get("/api/session")).json()
                assert session["settings_enabled"]
                selected = asdict(Preferences(output_tokens=512))
                assert (
                    await client.post("/api/settings", json=selected)
                ).status_code == 403
                headers = {"x-csrf-token": session["csrf_token"]}
                for invalid in (
                    {"output_tokens": 4096},
                    {"output_tokens": True},
                    {"text_size": "18"},
                    {"voice_volume": -1},
                    {"theme": "unknown"},
                    {"accent": []},
                    {"model": "another"},
                ):
                    assert (
                        await client.post(
                            "/api/settings",
                            json={**selected, **invalid},
                            headers=headers,
                        )
                    ).status_code == 422
                assert store.get_preferences() == Preferences()
                response = await client.post(
                    "/api/settings", json=selected, headers=headers
                )
                assert response.status_code == 200
                assert response.json()["restart_required"]
                assert response.json()["active_output_tokens"] == 2048
                assert settings.active_output_tokens == 2048
                assert service.context.output_allowance == 512
                assert provider.requests == []
                assert (await client.get("/api/settings")).json()[
                    "preferences"
                ] == selected
                restarted = SettingsService(
                    store,
                    model_name="Installed model",
                    active_output_tokens=store.get_preferences().output_tokens,
                    context_tokens=4096,
                )
                assert restarted.active_output_tokens == 512

    asyncio.run(scenario())
