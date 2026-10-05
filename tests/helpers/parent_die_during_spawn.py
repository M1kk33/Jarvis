"""Test helper: expose the child identity, then hold the creation window open."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from jarvis.infrastructure.gpu_lock import NamedGpuLock
from jarvis.infrastructure.windows_process import WindowsProcessSupervisor


def main() -> None:
    marker = Path(sys.argv[1])
    cwd = Path(sys.argv[2])
    worker = Path(sys.argv[3])
    key = sys.argv[4]
    shared_root = Path(sys.argv[5])
    cwd.mkdir()
    root = os.environ.get("SystemRoot", r"C:\Windows")
    lock = NamedGpuLock(key, shared_root)
    asyncio.run(lock.acquire())
    job_name = asyncio.run(lock.record_spawn_intent(1))

    def hold(pid: int, created: int) -> None:
        marker.write_text(
            json.dumps({"pid": pid, "created": created}), encoding="utf-8"
        )
        time.sleep(60)

    WindowsProcessSupervisor().spawn(
        Path(sys.executable),
        [
            str(worker),
            "--mode",
            "child_tree",
            "--ready-file",
            str(cwd / "ready"),
            "--stop-file",
            str(cwd / "stop"),
            "--child-pid-file",
            str(cwd / "child.pid"),
        ],
        cwd,
        {"SystemRoot": root, "WINDIR": root, "PATH": str(Path(root) / "System32")},
        after_create=hold,
        job_name=job_name,
    )


if __name__ == "__main__":
    main()
