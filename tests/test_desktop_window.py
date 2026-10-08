"""Desktop host ownership, startup failure and verified cleanup, without a GPU."""

import json
from pathlib import Path

import pytest

from jarvis.infrastructure.desktop_window import DesktopWindow


@pytest.fixture
def host(monkeypatch):
    class Process:
        exited = False
        stopped = True

        def root_exited(self):
            return self.exited

        def stop(self, graceful_seconds, kill_seconds):
            self.stop_args = (graceful_seconds, kill_seconds)
            return self.stopped

    process = Process()

    def spawn(_self, executable, arguments, cwd, environment, **kwargs):
        process.cwd = cwd
        process.arguments = arguments
        process.config = json.loads(kwargs["stdin_data"])
        process.environment = environment
        return process

    monkeypatch.setattr(
        "jarvis.infrastructure.desktop_window.WindowsProcessSupervisor.spawn", spawn
    )
    window = DesktopWindow("Jarvis test")
    yield window, process
    process.stopped = True
    window.close()


def test_window_secret_is_pipe_only_and_readiness_requires_loaded_page(host):
    window, process = host
    assert window.state() == "idle"
    window.open("http://127.0.0.1:8123/setup#token=private-secret")
    assert "private-secret" not in " ".join(process.arguments)
    assert process.config["url"].endswith("private-secret")
    assert window.state() == "starting"
    (process.cwd / "ready").touch()
    assert window.state() == "ready"
    (process.cwd / "closing").touch()
    assert window.state() == "closing"
    with pytest.raises(RuntimeError, match="already started"):
        window.open("unused")


def test_unverified_exit_keeps_process_and_private_directory(host):
    window, process = host
    window.open("local")
    directory = Path(window.directory.name)
    process.stopped = False
    assert not window.close()
    assert window.process is process
    assert directory.exists()
    process.stopped = True
    assert window.close()
    assert window.process is None
    assert not directory.exists()
    assert window.close()


@pytest.mark.parametrize("failure", ["error", "exit", "timeout"])
def test_failed_start_never_announces_ready(host, failure, monkeypatch):
    window, process = host
    window.open("local")
    if failure == "error":
        (process.cwd / "error").touch()
    elif failure == "exit":
        process.exited = True
    else:
        monkeypatch.setattr(
            "jarvis.infrastructure.desktop_window.time.monotonic",
            lambda: window.started_at + 46,
        )
    assert window.state() == "failed"
