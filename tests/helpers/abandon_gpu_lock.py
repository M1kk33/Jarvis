"""Test helper: acquire named GPU ownership and exit without releasing it."""

import asyncio
import json
import os
import sys
from pathlib import Path

from jarvis.domain.resources import RuntimeIdentity
from jarvis.infrastructure.gpu_lock import NamedGpuLock


async def main() -> None:
    key, root, marker = sys.argv[1:4]
    lock = NamedGpuLock(key, Path(root))
    await lock.acquire()
    await lock.record_spawn_intent(1)
    if len(sys.argv) == 6:
        pid = int(sys.argv[4])
        created = int(sys.argv[5])
        await lock.record_running(RuntimeIdentity(1, "fake", pid, created))
    Path(marker).write_text(json.dumps({"owner_pid": os.getpid()}), encoding="utf-8")
    os._exit(0)


if __name__ == "__main__":
    asyncio.run(main())
