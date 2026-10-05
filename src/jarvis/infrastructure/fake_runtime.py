"""Managed fake processes for M3 ownership and cleanup tests."""

import asyncio
import os
import sys
from pathlib import Path
from threading import Event
from uuid import uuid4

from jarvis.domain.resources import RuntimeIdentity, RuntimeProfile, RuntimeStartFailure
from jarvis.infrastructure.windows_process import (
    OwnedProcess,
    WindowsProcessSupervisor,
    same_process_alive,
)


class FakeProcessRuntime:
    def __init__(
        self,
        run_root: Path,
        *,
        modes: dict[str, str] | None = None,
        ready_delays: dict[str, float] | None = None,
        readiness_timeout: float = 2,
        graceful_seconds: float = 0.2,
        kill_seconds: float = 2,
    ) -> None:
        self.run_root = run_root.resolve()
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.modes = modes or {}
        self.ready_delays = ready_delays or {}
        self.readiness_timeout = readiness_timeout
        self.graceful_seconds = graceful_seconds
        self.kill_seconds = kill_seconds
        self.supervisor = WindowsProcessSupervisor()
        self.processes: dict[int, OwnedProcess] = {}
        self.intervals: list[tuple[str, int, int]] = []

    async def start(
        self, profile: RuntimeProfile, generation: int, cancel: Event, job_name: str
    ) -> RuntimeIdentity:
        cwd = self.run_root / f"{profile.name}-{generation}-{uuid4().hex}"
        cwd.mkdir()
        worker = Path(__file__).with_name("fake_worker.py").resolve()
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        environment = {
            "SystemRoot": system_root,
            "WINDIR": system_root,
            "PATH": str(Path(system_root) / "System32"),
            "TEMP": os.environ.get("TEMP", str(cwd)),
            "TMP": os.environ.get("TMP", str(cwd)),
            "PYTHONUNBUFFERED": "1",
        }
        process = await asyncio.to_thread(
            self.supervisor.spawn,
            Path(sys.executable),
            [
                str(worker),
                "--mode",
                self.modes.get(profile.name, "normal"),
                "--ready-file",
                str(cwd / "ready"),
                "--stop-file",
                str(cwd / "stop"),
                "--child-pid-file",
                str(cwd / "child.pid"),
                "--ready-delay",
                str(self.ready_delays.get(profile.name, 0)),
            ],
            cwd,
            environment,
            job_name=job_name,
        )
        self.processes[generation] = process
        self.intervals.append(("spawn", generation, process.pid))
        try:
            await asyncio.to_thread(process.wait_ready, cancel, self.readiness_timeout)
        except Exception as exc:
            raise RuntimeStartFailure(str(exc)) from exc
        return RuntimeIdentity(
            generation, profile.name, process.pid, process.creation_time
        )

    async def stop(self, identity: RuntimeIdentity) -> bool:
        process = self.processes.get(identity.generation)
        if process is None:
            return not same_process_alive(identity.pid, identity.creation_time)
        if (
            process.pid != identity.pid
            or process.creation_time != identity.creation_time
        ):
            return False
        result = await asyncio.to_thread(
            process.stop, self.graceful_seconds, self.kill_seconds
        )
        if result:
            self.intervals.append(("exit", identity.generation, process.pid))
            del self.processes[identity.generation]
        return result

    async def abort_start(self, generation: int) -> bool:
        process = self.processes.get(generation)
        if process is None:
            return True
        result = await asyncio.to_thread(
            process.stop, self.graceful_seconds, self.kill_seconds
        )
        if result:
            self.intervals.append(("exit", generation, process.pid))
            del self.processes[generation]
        return result

    async def wait_exit(self, identity: RuntimeIdentity) -> None:
        process = self.processes.get(identity.generation)
        if process is not None:
            await asyncio.to_thread(process.wait_root_exit)

    async def verify_stopped(self, identity: RuntimeIdentity) -> bool:
        process = self.processes.get(identity.generation)
        if process is not None:
            return await asyncio.to_thread(process.verify_stopped)
        return not same_process_alive(identity.pid, identity.creation_time)
