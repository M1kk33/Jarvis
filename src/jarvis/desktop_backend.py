"""Bootstrap the packaged backend over private, bounded lifecycle pipes."""

import json
import os
import socket
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from queue import Empty
from typing import TextIO

from jarvis.bootstrap import main
from jarvis.interfaces.web_control import WebControl

PROTOCOL = 1
MAX_LINE = 16_384
PROFILES = {
    "model.toml": "--profile",
    "image.toml": "--image-profile",
    "speech.toml": "--speech-profile",
    "piper.toml": "--piper-profile",
    "vision.toml": "--vision-profile",
}


def read_packet(stream: TextIO) -> dict:
    line = stream.readline(MAX_LINE + 1)
    if not line or len(line) > MAX_LINE or not line.endswith("\n"):
        raise ValueError("Invalid lifecycle packet")
    value = json.loads(line)
    if not isinstance(value, dict) or type(value.get("protocol")) is not int:
        raise ValueError("Invalid lifecycle protocol")
    if value["protocol"] != PROTOCOL:
        raise ValueError("Unsupported lifecycle protocol")
    return value


def launch_arguments(config: dict, port: int) -> list[str]:
    allowed = {
        "protocol",
        "data_dir",
        "test_mode",
        "enable_network",
        "enable_browser",
        "read_roots",
        "write_roots",
    }
    if config.keys() - allowed:
        raise ValueError("Unknown desktop configuration")
    data = config.get("data_dir")
    if not isinstance(data, str) or not Path(data).is_absolute():
        raise ValueError("An absolute data directory is required")
    for field in ("test_mode", "enable_network", "enable_browser"):
        if type(config.get(field, False)) is not bool:
            raise ValueError("Invalid desktop option")
    data_dir = Path(data).resolve()
    args = ["--data-dir", str(data_dir)]
    # Test mode is explicit; a missing model never silently selects the fake.
    if not config.get("test_mode", False):
        if not (data_dir / "model.toml").is_file():
            raise FileNotFoundError("model.toml is required")
        for name, flag in PROFILES.items():
            path = data_dir / name
            if path.is_file():
                args += [flag, str(path)]
    for field, flag in (("read_roots", "--read-root"), ("write_roots", "--write-root")):
        roots = config.get(field, [])
        if not isinstance(roots, list) or len(roots) > 32:
            raise ValueError("Invalid roots")
        for root in roots:
            if not isinstance(root, str) or not Path(root).is_absolute():
                raise ValueError("Invalid root")
            args += [flag, root]
    if config.get("enable_network", False):
        args.append("--enable-network")
    if config.get("enable_browser", False):
        args.append("--enable-browser")
    return args + ["web", "--port", str(port)]


def run(input_stream: TextIO, output_stream: TextIO) -> int:
    control = WebControl()
    done = threading.Event()

    def send(kind: str, **fields: object) -> None:
        try:
            output_stream.write(
                json.dumps({"protocol": PROTOCOL, "type": kind, **fields}) + "\n"
            )
            output_stream.flush()
        except (OSError, ValueError):
            control.stop.set()

    def receive() -> None:
        try:
            command = read_packet(input_stream)
            if command != {"protocol": PROTOCOL, "type": "stop"}:
                raise ValueError("Invalid control command")
        except (ValueError, OSError):
            pass
        finally:
            # EOF or malformed traffic means the owner can no longer supervise us.
            control.stop.set()

    def forward() -> None:
        while not done.is_set() or not control.events.empty():
            try:
                event, value = control.events.get(timeout=0.1)
            except Empty:
                continue
            if event == "ready" and not control.stop.is_set():
                send("ready", url=value)
            elif event == "error":
                send("error", code="backend_start_failed")

    try:
        config = read_packet(input_stream)
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(("127.0.0.1", 0))
            args = launch_arguments(config, listener.getsockname()[1])
            threading.Thread(target=receive, daemon=True).start()
            pump = threading.Thread(target=forward, daemon=True)
            pump.start()
            try:
                # Only the protocol writer may write to the inherited pipe.
                with (
                    open(os.devnull, "w", encoding="utf-8") as sink,
                    redirect_stdout(sink),
                    redirect_stderr(sink),
                ):
                    result = main(args, web_control=control, web_socket=listener)
            finally:
                done.set()
                pump.join()
        send("stopped", success=result == 0)
        return result
    except Exception:  # noqa: BLE001 -- protocol boundary never discloses secrets
        send("error", code="backend_start_failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(run(sys.stdin, sys.stdout))
