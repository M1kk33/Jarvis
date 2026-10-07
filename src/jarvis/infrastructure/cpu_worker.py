"""One-shot JSON-lines CPU worker, sharing Windows tree cleanup semantics."""

import asyncio
import json
import os
import time
from pathlib import Path

from jarvis.domain.speech import SpeechUnavailable
from jarvis.infrastructure.windows_process import (
    OwnedProcess,
    WindowsProcessSupervisor,
    named_job_tree_exited,
)


class WorkerCleanupUnverified(SpeechUnavailable):
    pass


async def settle(task: asyncio.Task):
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


def cpu_environment(python: Path, cwd: Path, threads: int) -> dict[str, str]:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    return {
        "SystemRoot": system_root,
        "WINDIR": system_root,
        "PATH": str(python.parent) + os.pathsep + str(Path(system_root) / "System32"),
        "TEMP": str(cwd),
        "TMP": str(cwd),
        "PYTHONNOUSERSITE": "1",
        "CUDA_VISIBLE_DEVICES": "-1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "OMP_NUM_THREADS": str(threads),
        "OPENBLAS_NUM_THREADS": "1",
    }


async def run_cpu_worker(
    supervisor: WindowsProcessSupervisor,
    python: Path,
    launcher: Path,
    cwd: Path,
    request: dict[str, object],
    *,
    timeout: float,
    threads: int,
    job_memory_bytes: int | None = None,
) -> dict[str, object]:
    job_name = f"Local\\JarvisCpu-{cwd.name}"
    raw = json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n"
    process: OwnedProcess | None = None
    try:
        spawn_task = asyncio.create_task(
            asyncio.to_thread(
                supervisor.spawn,
                python,
                [str(launcher)],
                cwd,
                cpu_environment(python, cwd, threads),
                job_name=job_name,
                stdin_data=raw,
                job_memory_bytes=job_memory_bytes,
            )
        )
        try:
            process = await asyncio.shield(spawn_task)
        except asyncio.CancelledError:
            process = await settle(spawn_task)
            raise
        deadline = time.monotonic() + timeout
        while not await asyncio.to_thread(process.root_exited):
            if time.monotonic() >= deadline:
                raise SpeechUnavailable("CPU speech worker timed out")
            await asyncio.sleep(0.1)
        if not await asyncio.to_thread(process.stdout.wait_eof, 2):
            raise SpeechUnavailable("CPU speech worker output did not close")
        lines = process.stdout.text.splitlines()
        if len(lines) != 1:
            raise SpeechUnavailable("CPU speech worker returned no single result")
        try:
            result = json.loads(lines[0])
        except ValueError as exc:
            raise SpeechUnavailable("CPU speech worker result is invalid") from exc
        if type(result) is not dict or result.get("version") != 1:
            raise SpeechUnavailable("CPU speech worker protocol mismatch")
        return result
    finally:
        if process is not None:
            try:
                stopped = await settle(
                    asyncio.create_task(asyncio.to_thread(process.stop, 0, 10))
                )
            except Exception:  # noqa: BLE001 - process ownership boundary
                stopped = False
            if not stopped:
                raise WorkerCleanupUnverified(
                    "CPU speech worker cleanup is unverified"
                ) from None
        else:
            try:
                exited = await settle(
                    asyncio.create_task(
                        asyncio.to_thread(named_job_tree_exited, job_name, 10)
                    )
                )
            except Exception:  # noqa: BLE001 - Windows ownership verification
                exited = False
            if not exited:
                raise WorkerCleanupUnverified(
                    "CPU speech worker startup cleanup is unverified"
                ) from None
