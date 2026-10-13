"""Saved network grants apply on startup, without changing live tool policy."""

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest

from jarvis.application.conversations import ConversationService
from jarvis.application.jobs import JobRunner
from jarvis.application.settings import SettingsService
from jarvis.domain.preferences import Preferences
from jarvis.infrastructure.providers.fake import FakeChatProvider
from jarvis.infrastructure.sqlite_store import SqliteStore
from jarvis.interfaces.web import create_web_app


@pytest.mark.parametrize("value", [0, 1, "true", "false", [], {}])
def test_network_preferences_reject_coercion(value):
    for name in ("enable_network", "enable_browser"):
        with pytest.raises(ValueError):
            Preferences(**{name: value})


def test_legacy_launch_grants_and_explicit_disable(tmp_path):
    with SqliteStore(tmp_path) as store:
        legacy = asdict(Preferences())
        del legacy["enable_network"], legacy["enable_browser"]
        store.run_sync(
            lambda: store._conn.execute(
                "INSERT INTO app_preferences VALUES (1,?)", (json.dumps(legacy),)
            )
        )
        assert store.get_preferences().network_options(True, True) == (True, True)
        store.set_preferences(Preferences(enable_network=False, enable_browser=False))
    with SqliteStore(tmp_path) as store:
        assert store.get_preferences().network_options(True, True) == (False, False)
    with pytest.raises(ValueError):
        Preferences(enable_network=False, enable_browser=True)


@pytest.mark.parametrize(
    "network,browser,available,launch",
    [
        (False, False, True, True),
        (True, False, True, False),
        (True, True, True, False),
        (True, True, False, False),
    ],
)
def test_startup_uses_committed_grants_and_missing_browser_keeps_chat_usable(
    tmp_path, monkeypatch, network, browser, available, launch
):
    from jarvis import bootstrap
    from jarvis.infrastructure.browser_profile import BrowserProfile
    from jarvis.infrastructure.gpu_lock import NamedGpuLock
    from jarvis.interfaces import web

    with SqliteStore(tmp_path) as store:
        store.set_preferences(
            Preferences(enable_network=network, enable_browser=browser)
        )

    class Captured(Exception):
        pass

    def profile():
        if not available:
            raise ValueError("Missing browser")
        return BrowserProfile(tmp_path / "test-browser.exe")

    def capture(service, runner, tools, *_args, settings, **_kwargs):
        assert settings.active_network == network
        assert settings.active_browser == (browser and available)
        assert settings.browser_available == available
        assert (tools.web_backend is not None) == network
        assert (tools.browser_service is not None) == (browser and available)
        assert ("network" in tools.policy.capabilities) == network
        assert ("browser" in tools.policy.capabilities) == (browser and available)
        assert settings.restart_required(
            Preferences(enable_network=network, enable_browser=browser)
        ) == (browser and not available)
        assert tools.policy.decision(tools.registry.get("fetch_public_page")) == (
            "approval" if network else "deny"
        )
        assert tools.policy.decision(tools.registry.get("render_public_page")) == (
            "approval" if browser and available else "deny"
        )
        raise Captured()

    monkeypatch.setattr(BrowserProfile, "installed", profile)
    monkeypatch.setattr(web, "create_web_app", capture)
    monkeypatch.setattr(
        bootstrap,
        "NamedGpuLock",
        lambda device: NamedGpuLock(device, tmp_path / "locks"),
    )
    pinned = (
        Path(__file__).resolve().parents[1]
        / "profiles/qwen3-4b-instruct-2507.example.toml"
    )
    args = ["--data-dir", str(tmp_path), "--profile", str(pinned)]
    if launch:
        args += ["--enable-network", "--enable-browser"]
    with pytest.raises(Captured):
        bootstrap.main(args + ["web"])


def test_api_requires_auth_and_csrf_and_only_changes_durable_choice(tmp_path):
    async def scenario():
        with SqliteStore(tmp_path) as store:
            service = ConversationService(store, FakeChatProvider())
            settings = SettingsService(
                store,
                model_name="Pinned",
                active_output_tokens=2048,
                context_tokens=4096,
                browser_available=True,
            )
            app, token = create_web_app(
                service, JobRunner(store, service), None, 8765, settings=settings
            )
            selected = asdict(Preferences(enable_network=True, enable_browser=True))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
            ) as client:
                assert (
                    await client.post("/api/settings", json=selected)
                ).status_code == 401
                await client.post("/api/setup", json={"token": token})
                assert (
                    await client.post("/api/settings", json=selected)
                ).status_code == 403
                csrf = (await client.get("/api/session")).json()["csrf_token"]
                headers = {"x-csrf-token": csrf}
                for invalid in (
                    {"enable_network": "true"},
                    {"enable_browser": 1},
                    {"enable_network": False},
                ):
                    assert (
                        await client.post(
                            "/api/settings",
                            json={**selected, **invalid},
                            headers=headers,
                        )
                    ).status_code == 422
                result = await client.post(
                    "/api/settings", json=selected, headers=headers
                )
                assert result.status_code == 200
                assert result.json()["restart_required"]
                assert not result.json()["active_network"]
                assert not settings.active_network and not settings.active_browser
                assert store.get_preferences().enable_browser is True
                settings.browser_available = False
                assert (
                    await client.post("/api/settings", json=selected, headers=headers)
                ).status_code == 422
                assert store.get_preferences().enable_browser is True
                disabled = asdict(
                    Preferences(enable_network=False, enable_browser=False)
                )
                result = await client.post(
                    "/api/settings", json=disabled, headers=headers
                )
                assert (
                    result.status_code == 200 and not result.json()["restart_required"]
                )
                settings.network_available = False
                selected["enable_browser"] = False
                assert (
                    await client.post("/api/settings", json=selected, headers=headers)
                ).status_code == 422

    asyncio.run(scenario())
