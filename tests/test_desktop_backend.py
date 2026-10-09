"""Private host protocol, real bind, authentication and graceful owner loss."""

import io
import json
import os
import threading
from queue import Queue

import httpx
import pytest

from jarvis.desktop_backend import MAX_LINE, launch_arguments, read_packet, run
from jarvis.infrastructure.sqlite_store import SqliteStore


def test_missing_models_do_not_select_fake_and_existing_profiles_are_preserved(
    tmp_path,
):
    config = {"protocol": 1, "data_dir": str(tmp_path)}
    with pytest.raises(FileNotFoundError):
        launch_arguments(config, 4567)
    for name in (
        "model.toml",
        "piper.toml",
        "speech.toml",
        "image.toml",
        "vision.toml",
    ):
        (tmp_path / name).write_text("profile fixture", encoding="utf-8")
    args = launch_arguments(config, 4567)
    assert all(
        flag in args
        for flag in (
            "--profile",
            "--piper-profile",
            "--speech-profile",
            "--image-profile",
            "--vision-profile",
        )
    )
    assert "--enable-network" not in args
    assert args[-3:] == ["web", "--port", "4567"]


@pytest.mark.parametrize(
    "packet",
    ["", "x" * (MAX_LINE + 1), '{"protocol":true}\n', '{"protocol":2}\n', "[]\n"],
)
def test_protocol_rejects_incompatible_or_unbounded_input(packet):
    with pytest.raises(ValueError):
        read_packet(io.StringIO(packet))


@pytest.mark.parametrize("stop", [True, False])
def test_real_hosted_backend_session_and_owner_loss_preserve_data(tmp_path, stop):
    read_fd, write_fd = os.pipe()
    incoming = os.fdopen(read_fd, "r", encoding="utf-8")
    outgoing = os.fdopen(write_fd, "w", encoding="utf-8")
    packets = Queue()
    result = []

    class Output:
        def write(self, value):
            packets.put(json.loads(value))

        def flush(self):
            pass

    outgoing.write(
        json.dumps({"protocol": 1, "data_dir": str(tmp_path), "test_mode": True}) + "\n"
    )
    outgoing.flush()
    thread = threading.Thread(target=lambda: result.append(run(incoming, Output())))
    thread.start()
    try:
        ready = packets.get(timeout=10)
        assert ready["type"] == "ready"
        address, token = ready["url"].split("/setup#token=")
        with httpx.Client(base_url=address, trust_env=False) as client:
            assert client.get("/api/session").status_code == 401
            response = client.post("/api/setup", json={"token": token})
            assert response.status_code == 200
            csrf = client.get("/api/session").json()["csrf_token"]
            saved = client.post(
                "/api/conversations",
                json={"title": "Keep after shutdown"},
                headers={"X-CSRF-Token": csrf},
            )
            assert saved.status_code == 201
    finally:
        if stop:
            outgoing.write('{"protocol":1,"type":"stop"}\n')
            outgoing.flush()
        outgoing.close()
        thread.join(timeout=10)
        if not thread.is_alive():
            incoming.close()
    assert not thread.is_alive()
    assert result == [0]
    assert packets.get(timeout=2) == {"protocol": 1, "type": "stopped", "success": True}
    with SqliteStore(tmp_path) as store:
        assert store.list_conversations()[0].title == "Keep after shutdown"
