"""Test helper: hold GPU ownership until another process asks for release."""

import asyncio
import sys
from pathlib import Path

from jarvis.infrastructure.gpu_lock import NamedGpuLock


async def main() -> None:
    key, root, ready, release = sys.argv[1:5]
    lock = NamedGpuLock(key, Path(root))
    await lock.acquire()
    try:
        await lock.record_spawn_intent(1)
        Path(ready).touch()
        while not Path(release).exists():
            await asyncio.sleep(0.025)
        await lock.clear()
    finally:
        await lock.release()


if __name__ == "__main__":
    asyncio.run(main())
