"""Console-free launch readiness, authentication, stop, and instance ownership."""

import asyncio
import runpy
import socket
import sys
import threading
from pathlib import Path
from queue import Empty

import httpx
import pytest

from jarvis.bootstrap import main
from jarvis.infrastructure.desktop_instance import DesktopInstance
from jarvis.infrastructure.sqlite_store import SqliteStore
from jarvis.interfaces.desktop import WebControl, serve_controlled


def test_readiness_waits_for_bind_and_stop_is_graceful():
    control = WebControl()

    class Server:
        started = False
        should_exit = False
        exited = False

        async def serve(self):
            await asyncio.sleep(0.15)
            assert control.events.empty()
            self.started = True
            while not self.should_exit:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)  # Cleanup must finish before returning.
            self.exited = True

    async def scenario():
        server = Server()
        task = asyncio.create_task(serve_controlled(server, control, "local-setup"))
        while control.events.empty():
            await asyncio.sleep(0.01)
        assert control.events.get_nowait() == ("ready", "local-setup")
        control.stop.set()
        await asyncio.wait_for(task, 2)
        assert server.exited
        assert control.events.empty()

    asyncio.run(scenario())


def test_startup_failure_never_opens_browser_and_monitor_exits():
    control = WebControl()

    class Server:
        started = False
        should_exit = False

        async def serve(self):
            raise OSError("Port already in use")

    async def scenario():
        with pytest.raises(OSError, match="Port already"):
            await serve_controlled(Server(), control, "unused-secret")
        control.stop.set()
        await asyncio.sleep(0.15)
        assert control.events.empty()

    asyncio.run(scenario())


def test_close_during_startup_does_not_announce_ready():
    control = WebControl()
    control.stop.set()

    class Server:
        started = False
        should_exit = False

        async def serve(self):
            await asyncio.sleep(0.15)
            self.started = True
            assert self.should_exit

    asyncio.run(serve_controlled(Server(), control, "unused-secret"))
    assert control.events.empty()


def test_repeated_launcher_ownership_and_release(tmp_path):
    owner = DesktopInstance(tmp_path)
    assert owner.acquired
    results = []

    def another_click():
        other = DesktopInstance(tmp_path)
        try:
            results.append(other.acquired)
            assert other.title == owner.title
        finally:
            other.close()

    try:
        thread = threading.Thread(target=another_click)
        thread.start()
        thread.join(timeout=2)
        assert results == [False]
    finally:
        owner.close()
    replacement = DesktopInstance(tmp_path)
    try:
        assert replacement.acquired
    finally:
        replacement.close()


def test_real_desktop_web_authentication_and_cleanup(tmp_path, capsys):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    control = WebControl()
    results = []
    thread = threading.Thread(
        target=lambda: results.append(
            main(
                ["--data-dir", str(tmp_path), "web", "--port", str(port)],
                web_control=control,
            )
        )
    )
    thread.start()
    try:
        event, url = control.events.get(timeout=10)
        assert event == "ready", url
        base, fragment = url.split("#", 1)
        token = fragment.removeprefix("token=")
        with httpx.Client(
            base_url=base.removesuffix("/setup"), trust_env=False
        ) as client:
            assert client.get("/api/session").status_code == 401
            assert client.post("/api/setup", json={"token": token}).status_code == 200
            assert client.get("/api/session").status_code == 200
            assert client.post("/api/setup", json={"token": token}).status_code == 403
    finally:
        control.stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert results == [0]
    output = capsys.readouterr()
    assert token not in output.out + output.err
    with SqliteStore(tmp_path) as store:
        assert store.list_conversations() == []
    with pytest.raises(Empty):
        control.events.get_nowait()


def test_desktop_reports_instance_conflict_without_token(tmp_path):
    control = WebControl()
    with SqliteStore(tmp_path):
        assert main(["--data-dir", str(tmp_path), "web"], web_control=control) == 1
    event, message = control.events.get_nowait()
    assert event == "error"
    assert "Another Jarvis instance" in message


def test_pythonw_entry_suppresses_console_output_and_restores_streams(
    monkeypatch, capsys
):
    def launch(argv):
        assert argv == ["--data-dir", "private", "web"]
        print("never-log-this-token")
        return 0

    monkeypatch.setattr("jarvis.bootstrap.desktop_main", launch)
    monkeypatch.setattr(sys, "argv", ["Jarvis.pyw", "--data-dir", "private", "web"])
    streams = sys.stdout, sys.stderr
    with pytest.raises(SystemExit) as exit_result:
        runpy.run_path(str(Path(__file__).parents[1] / "Jarvis.pyw"))
    assert exit_result.value.code == 0
    assert (sys.stdout, sys.stderr) == streams
    captured = capsys.readouterr()
    assert "never-log-this-token" not in captured.out + captured.err
