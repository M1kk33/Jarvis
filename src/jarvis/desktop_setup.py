"""Bootstrap for the native first-run installer; no model is loaded here."""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from jarvis.domain.resources import canonical_gpu_uuid
from jarvis.infrastructure.component_pack import (
    SetupCancelled,
    install_pack,
    pinned_catalog,
)


def _send(kind: str, **values: object) -> None:
    print(json.dumps({"protocol": 1, "type": kind, **values}), flush=True)


def main() -> int:
    cancel = threading.Event()
    try:
        # Unbuffered fd reads let the daemon watcher remain blocked at interpreter
        # exit without holding Python's buffered-stdin finalization lock.
        raw = b""
        while len(raw) <= 16384 and not raw.endswith(b"\n"):
            byte = os.read(sys.stdin.fileno(), 1)
            if not byte:
                break
            raw += byte
        if len(raw) > 16384 or not raw.endswith(b"\n"):
            raise ValueError("Invalid setup packet")
        request = json.loads(raw)
        if (
            set(request) != {"protocol", "archive", "destination"}
            or type(request["protocol"]) is not int
            or request["protocol"] != 1
        ):
            raise ValueError("Invalid setup protocol")
        archive, destination = Path(request["archive"]), Path(request["destination"])
        if not archive.is_absolute() or not destination.is_absolute():
            raise ValueError("Setup paths must be absolute")

        def watch_owner() -> None:
            # Any following input, including EOF, cancels this one-shot operation.
            os.read(sys.stdin.fileno(), 1)
            cancel.set()

        threading.Thread(target=watch_owner, daemon=True).start()
        # Native host's job owns this child and nvidia-smi from their creation.
        executable = Path(os.environ["SystemRoot"]) / "System32/nvidia-smi.exe"
        result = subprocess.run(
            [str(executable), "--query-gpu=uuid", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        rows = result.stdout.splitlines()
        if not rows:
            raise RuntimeError("NvidiaUnavailable")
        device = canonical_gpu_uuid(rows[0].strip())
        previous = 0.0

        def progress(done: int, total: int) -> None:
            nonlocal previous
            now = time.monotonic()
            if done == 0 or done == total or now - previous >= 0.25:
                _send("progress", done=done, total=total)
                previous = now

        install_pack(archive, destination, pinned_catalog(), device, cancel, progress)
        _send("complete")
        return 0
    except SetupCancelled:
        _send("cancelled")
        return 2
    except Exception as error:  # noqa: BLE001 -- private protocol boundary
        # Only stable metadata; no raw exceptions, paths, environment or content.
        _send("error", code=type(error).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
