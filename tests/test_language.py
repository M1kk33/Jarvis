"""Saved interface language, legacy data and safe localized pages."""

import asyncio
import json
import re
from dataclasses import asdict

import httpx
import pytest

from jarvis.application.conversations import ConversationService
from jarvis.application.jobs import JobRunner
from jarvis.application.settings import SettingsService
from jarvis.domain.preferences import Preferences
from jarvis.infrastructure.backup import create_backup, restore_backup
from jarvis.infrastructure.providers.fake import FakeChatProvider
from jarvis.infrastructure.sqlite_store import SqliteStore
from jarvis.interfaces.localization import render_page
from jarvis.interfaces.web import create_web_app


def test_legacy_defaults_and_english_persist_without_changing_history(tmp_path):
    data = tmp_path / "data"
    with SqliteStore(data) as store:
        legacy = asdict(Preferences(accent="violet"))
        del legacy["language"]
        store.run_sync(
            lambda: store._conn.execute(
                "INSERT INTO app_preferences VALUES (1,?)", (json.dumps(legacy),)
            )
        )
        assert store.get_preferences().language == "ru"
        chat = store.create_conversation("Новый диалог")
        turn, _ = store.begin_turn(chat.id, "Сохранить", "one")
        store.complete_turn(turn.id, "Настройки **не переводить**")
        messages = store.get_messages(chat.id)
        selected = Preferences(accent="violet", language="en")
        store.set_preferences(selected)
        backup = create_backup(store)
    with SqliteStore(data) as store:
        assert store.get_preferences() == selected
        assert store.get_messages(chat.id) == messages
        assert store.get_conversation(chat.id).title == "Новый диалог"
    restore_backup(backup, tmp_path / "restored")
    with SqliteStore(tmp_path / "restored") as store:
        assert store.get_preferences() == selected


@pytest.mark.parametrize("value", ["fr", "EN", "", None, True, 1, []])
def test_language_choices_are_strict(value):
    with pytest.raises(ValueError):
        Preferences(language=value)


def test_localized_pages_and_authenticated_language_save(tmp_path):
    async def scenario():
        with SqliteStore(tmp_path) as store:
            provider = FakeChatProvider()
            service = ConversationService(store, provider)
            settings = SettingsService(
                store,
                model_name="Pinned model",
                active_output_tokens=2048,
                context_tokens=4096,
            )
            app, token = create_web_app(
                service, JobRunner(store, service), None, 8765, settings=settings
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
            ) as client:
                original = asdict(Preferences())
                english = {**original, "language": "en"}
                assert (
                    await client.post("/api/settings", json=english)
                ).status_code == 401
                await client.post("/api/setup", json={"token": token})
                csrf = (await client.get("/api/session")).json()["csrf_token"]
                assert (
                    await client.post("/api/settings", json=english)
                ).status_code == 403
                headers = {"x-csrf-token": csrf}
                for language in ("fr", None, True):
                    assert (
                        await client.post(
                            "/api/settings",
                            json={**english, "language": language},
                            headers=headers,
                        )
                    ).status_code == 422
                response = await client.post(
                    "/api/settings", json=english, headers=headers
                )
                assert response.json()["preferences"] == english
                assert not response.json()["restart_required"]
                assert settings.active_output_tokens == 2048
                page = (await client.get("/")).text
                assert '<html lang="en">' in page
                assert ">New chat</span>" in page
                assert 'placeholder="Ask Jarvis"' in page
                assert 'aria-label="Close settings"' in page
                assert 'id="translations" type="application/json"' in page
                assert (await client.get("/static/i18n.js")).status_code == 200
                client.cookies.clear()
                assert ">Starting Jarvis…</p>" in (await client.get("/setup")).text
                assert provider.requests == []

    asyncio.run(scenario())


def test_dictionary_covers_marked_pages():
    from html.parser import HTMLParser
    from importlib import resources

    dictionary = json.loads(
        resources.files("jarvis")
        .joinpath("interfaces/static/translations.json")
        .read_text(encoding="utf-8")
    )
    assert all(isinstance(value, str) and value for value in dictionary.values())

    class Labels(HTMLParser):
        def handle_starttag(self, tag, attrs):
            for key, value in attrs:
                if key.startswith("data-i18n"):
                    assert value in dictionary, value

        def handle_data(self, data):
            assert not re.search("[А-Яа-яЁё]", data) or data.strip() == "Русский", data

    for name in ("index.html", "setup.html"):
        Labels().feed(render_page(name, "en"))


def test_page_renderer_escapes_translated_markup():
    from jarvis.interfaces.localization import _Page

    page = _Page("en", {"Настройки": '<img src=x onerror="bad()">'})
    page.feed('<h2 data-i18n="Настройки">Настройки</h2>')
    result = "".join(page.parts)
    assert "<img" not in result
    assert "&lt;img" in result
