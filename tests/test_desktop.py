"""Console-free launch readiness, authentication, stop, and instance ownership."""

import asyncio
import base64
import json
import os
import runpy
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from queue import Empty

import httpx
import pytest

from jarvis.bootstrap import main
from jarvis.infrastructure.desktop_instance import DesktopInstance
from jarvis.infrastructure.sqlite_store import SqliteStore
from jarvis.infrastructure.windows_process import WindowsProcessSupervisor
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


def test_shortcut_wrapper_bypasses_missing_venv_redirector(tmp_path):
    # Exercise the actual Windows shortcut command, rather than launching Python
    # from an already active environment. No redirector exists in this fixture.
    project = tmp_path / "Jarvis тест & folder"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    venv = project / ".venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Lib" / "site-packages").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(
        f"home = {sys.base_prefix}\ninclude-system-site-packages = false\n",
        encoding="utf-8",
    )
    start = scripts / "start-jarvis.ps1"
    start.write_bytes(
        (Path(__file__).parents[1] / "scripts" / "start-jarvis.ps1").read_bytes()
    )
    probe = project / "probe.pyw"
    result = project / "result.json"
    probe.write_text(
        "import sys,json\nfrom pathlib import Path\n"
        "Path(sys.argv[1]).write_text(json.dumps({'prefix':sys.prefix,"
        "'executable':sys.executable,'input':sys.argv[2]}),encoding='utf-8')\n",
        encoding="utf-8",
    )
    literal = 'argument with spaces, $() & ` and a "quote"'
    arguments = base64.b64encode(
        subprocess.list2cmdline([str(probe), str(result), literal]).encode()
    ).decode()
    windows_shell = (
        Path(os.environ["SystemRoot"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    process = WindowsProcessSupervisor().spawn(
        windows_shell,
        [
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(start),
            "-ArgumentsBase64",
            arguments,
        ],
        project,
        dict(os.environ),
    )
    try:
        deadline = time.monotonic() + 10
        while not result.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert result.exists(), process.stderr.text
        value = json.loads(result.read_text(encoding="utf-8"))
        assert Path(value["prefix"]) == venv
        assert Path(value["executable"]) == venv / "Scripts" / "pythonw.exe"
        assert value["input"] == literal
    finally:
        assert process.stop(0, 10)
