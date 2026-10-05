"""Named per-device Windows mutex plus crash-recovery ownership journal."""

import asyncio
import ctypes
import hashlib
import json
import os
import threading
from pathlib import Path
from uuid import uuid4

from jarvis.domain.resources import RuntimeIdentity
from jarvis.infrastructure.windows_process import (
    WAIT_OBJECT_0,
    _close,
    _kernel,
    named_job_tree_exited,
    process_creation_time,
    same_process_alive,
)

WAIT_ABANDONED = 0x80
WAIT_TIMEOUT = 258
_kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
_kernel.CreateMutexW.restype = ctypes.c_void_p
_kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
_kernel.ReleaseMutex.restype = ctypes.c_int
_kernel.GetCurrentProcess.argtypes = []
_kernel.GetCurrentProcess.restype = ctypes.c_void_p


class NamedGpuLock:
    def __init__(self, device_key: str, shared_root: Path | None = None) -> None:
        if not device_key or len(device_key) > 200:
            raise ValueError("A stable device key is required")
        self._digest = hashlib.sha256(device_key.encode("utf-8")).hexdigest()[:32]
        self.name = f"Global\\JarvisGpu_{self._digest}"
        root = shared_root or Path(os.environ["LOCALAPPDATA"]) / "Jarvis" / "gpu_locks"
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.journal = self.root / f"{self._digest}.json"
        self._job_name: str | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._release = threading.Event()
        self._acquired = False
        self._result: dict[str, bool | BaseException] = {}
        self._owner_created = process_creation_time(_kernel.GetCurrentProcess())

    async def acquire(self) -> bool:
        if self._thread is not None:
            raise RuntimeError("Named GPU lock is already acquired or waiting")
        self._stop.clear()
        self._release.clear()
        done = threading.Event()
        result: dict[str, bool | BaseException] = {}
        self._result = result

        def wait_owner() -> None:
            handle = _kernel.CreateMutexW(None, False, self.name)
            if not handle:
                result["error"] = OSError(
                    ctypes.get_last_error(), "CreateMutexW failed"
                )
                done.set()
                return
            try:
                while not self._stop.is_set():
                    status = _kernel.WaitForSingleObject(handle, 100)
                    if status in (WAIT_OBJECT_0, WAIT_ABANDONED):
                        result["abandoned"] = status == WAIT_ABANDONED
                        done.set()
                        self._release.wait()
                        if not _kernel.ReleaseMutex(handle):
                            result["release_error"] = OSError(
                                ctypes.get_last_error(), "ReleaseMutex failed"
                            )
                        return
                    if status != WAIT_TIMEOUT:
                        result["error"] = OSError(
                            ctypes.get_last_error(), "WaitForSingleObject(mutex) failed"
                        )
                        done.set()
                        return
                done.set()
            finally:
                _close(handle)

        self._thread = threading.Thread(target=wait_owner, daemon=True)
        self._thread.start()
        try:
            await asyncio.to_thread(done.wait)
        except asyncio.CancelledError:
            self._stop.set()
            self._release.set()
            await asyncio.to_thread(self._thread.join)
            self._thread = None
            raise
        if "error" in result:
            await asyncio.to_thread(self._thread.join)
            self._thread = None
            error = result["error"]
            assert isinstance(error, BaseException)
            raise error
        if "abandoned" not in result:
            self._thread = None
            raise RuntimeError("Named GPU lock was not acquired")
        self._acquired = True
        return bool(result["abandoned"])

    async def release(self) -> None:
        if self._thread is None:
            return
        self._release.set()
        await asyncio.to_thread(self._thread.join)
        self._thread = None
        self._acquired = False
        if "release_error" in self._result:
            error = self._result["release_error"]
            assert isinstance(error, BaseException)
            raise error

    async def verify_previous(self, abandoned: bool) -> bool:
        self._require_lock()
        if not self.journal.exists():
            # Before spawn, no worker identity exists. Creation-time assignment and
            # kill-on-close rule out an orphan from a parent crash in that gap.
            return True
        try:
            record = json.loads(self.journal.read_text(encoding="utf-8"))
            owner_pid = int(record["owner_pid"])
            owner_created = int(record["owner_created"])
            phase = record["phase"]
            if phase not in {"spawning", "running"}:
                return False
            job_name = record["job_name"]
            prefix = f"Global\\JarvisJob_{self._digest}_"
            if (
                not isinstance(job_name, str)
                or not job_name.startswith(prefix)
                or len(job_name) != len(prefix) + 32
                or any(
                    char not in "0123456789abcdef" for char in job_name[len(prefix) :]
                )
            ):
                return False
            if same_process_alive(owner_pid, owner_created):
                return False
            if phase == "running" and same_process_alive(
                int(record["pid"]), int(record["creation_time"])
            ):
                return False
            return await asyncio.to_thread(named_job_tree_exited, job_name)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return False

    async def record_spawn_intent(self, generation: int) -> str:
        self._require_lock()
        self._job_name = f"Global\\JarvisJob_{self._digest}_{uuid4().hex}"
        self._write(
            {
                "phase": "spawning",
                "generation": generation,
                "owner_pid": os.getpid(),
                "owner_created": self._owner_created,
                "job_name": self._job_name,
            }
        )
        return self._job_name

    async def record_running(self, identity: RuntimeIdentity) -> None:
        self._require_lock()
        if self._job_name is None:
            raise RuntimeError("Spawn intent must be recorded first")
        self._write(
            {
                "phase": "running",
                "generation": identity.generation,
                "owner_pid": os.getpid(),
                "owner_created": self._owner_created,
                "job_name": self._job_name,
                "pid": identity.pid,
                "creation_time": identity.creation_time,
            }
        )

    async def clear(self) -> None:
        self._require_lock()
        self.journal.unlink(missing_ok=True)
        self._job_name = None

    def _write(self, record: dict[str, str | int]) -> None:
        temporary = self.root / f"{self.journal.stem}.{uuid4().hex}.tmp"
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(record, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.journal)
        finally:
            temporary.unlink(missing_ok=True)

    def _require_lock(self) -> None:
        if not self._acquired:
            raise RuntimeError("GPU ownership journal requires the named lock")
