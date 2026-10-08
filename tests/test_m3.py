"""M3 resource ownership and real Windows fake-process lifecycle tests."""

import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

from jarvis.application.resource_manager import ResourceManager
from jarvis.domain.resources import (
    AdmissionDenied,
    ResourceSnapshot,
    ResourceState,
    ResourceUnavailable,
    RuntimeProfile,
    RuntimeStartFailure,
    admit,
)
from jarvis.infrastructure.fake_runtime import FakeProcessRuntime
from jarvis.infrastructure.gpu_lock import NamedGpuLock
from jarvis.infrastructure.windows_process import (
    WindowsProcessSupervisor,
    creation_time_for_pid,
    same_process_alive,
)


@dataclass
class Probe:
    metrics: ResourceSnapshot = field(default_factory=lambda: ResourceSnapshot(8, 12))

    def snapshot(self) -> ResourceSnapshot:
        return self.metrics


class FakeClock:
    def __init__(self) -> None:
        self.time = 0.0
        self.waiters: list[tuple[float, asyncio.Future[None]]] = []

    def now(self) -> float:
        return self.time

    async def sleep(self, seconds: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.waiters.append((self.time + seconds, future))
        await future

    async def advance(self, seconds: float) -> None:
        self.time += seconds
        for deadline, future in self.waiters:
            if deadline <= self.time and not future.done():
                future.set_result(None)
        await asyncio.sleep(0)


TEXT = RuntimeProfile("text-fake", 4, 5, 0.5, 1, calibrated=True)
IMAGE = RuntimeProfile("image-fake", 5, 7, 1, 1, calibrated=True)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")
def test_creation_time_job_assignment_and_clean_stop(tmp_path):
    worker = (
        Path(__file__).parents[1]
        / "src"
        / "jarvis"
        / "infrastructure"
        / "fake_worker.py"
    )
    cwd = tmp_path / "worker"
    cwd.mkdir()
    root = os.environ.get("SystemRoot", r"C:\Windows")
    process = WindowsProcessSupervisor().spawn(
        Path(sys.executable),
        [
            str(worker),
            "--mode",
            "normal",
            "--ready-file",
            str(cwd / "ready"),
            "--stop-file",
            str(cwd / "stop"),
            "--child-pid-file",
            str(cwd / "child.pid"),
        ],
        cwd,
        {"SystemRoot": root, "WINDIR": root, "PATH": str(Path(root) / "System32")},
    )
    try:
        process.wait_ready(Event(), 2)
        assert process.active_count() >= 1
        assert not process.contains_pid(os.getpid())
        assert process.stop(0.5, 2)
        assert process.verify_stopped()
    finally:
        if not process.verify_stopped():
            assert process.stop(0, 2)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")
def test_child_and_grandchild_are_reaped_together(tmp_path):
    worker = (
        Path(__file__).parents[1]
        / "src"
        / "jarvis"
        / "infrastructure"
        / "fake_worker.py"
    )
    cwd = tmp_path / "tree"
    cwd.mkdir()
    root = os.environ.get("SystemRoot", r"C:\Windows")
    process = WindowsProcessSupervisor().spawn(
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
    )
    try:
        process.wait_ready(Event(), 2)
        child_pid = int((cwd / "child.pid").read_text(encoding="ascii"))
        child_created = creation_time_for_pid(child_pid)
        assert same_process_alive(child_pid, child_created)
        assert process.contains_pid(process.pid)
        assert process.contains_pid(child_pid)
        assert process.stop(0.1, 2)
        assert not same_process_alive(process.pid, process.creation_time)
        assert not same_process_alive(child_pid, child_created)
    finally:
        if not process.verify_stopped():
            assert process.stop(0, 2)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")
def test_supervisor_drains_chatty_child_with_bounded_output(tmp_path):
    worker = (
        Path(__file__).parents[1]
        / "src"
        / "jarvis"
        / "infrastructure"
        / "fake_worker.py"
    )
    cwd = tmp_path / "chatty"
    cwd.mkdir()
    root = os.environ.get("SystemRoot", r"C:\Windows")
    process = WindowsProcessSupervisor().spawn(
        Path(sys.executable),
        [
            str(worker),
            "--mode",
            "chatty",
            "--ready-file",
            str(cwd / "ready"),
            "--stop-file",
            str(cwd / "stop"),
            "--child-pid-file",
            str(cwd / "child.pid"),
        ],
        cwd,
        {"SystemRoot": root, "WINDIR": root, "PATH": str(Path(root) / "System32")},
    )
    try:
        process.wait_ready(Event(), 4)
        assert len(process.stdout.text) <= 65_536
        assert len(process.stderr.text) <= 65_536
        assert process.stop(0.5, 2)
    finally:
        if not process.verify_stopped():
            assert process.stop(0, 2)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")
def test_parent_death_during_spawn_kills_owned_tree(tmp_path):
    workspace = Path(__file__).parents[1]
    worker = workspace / "src" / "jarvis" / "infrastructure" / "fake_worker.py"
    helper = workspace / "tests" / "helpers" / "parent_die_during_spawn.py"
    marker = tmp_path / "identity.json"
    cwd = tmp_path / "spawn"
    key = f"test-{uuid4().hex}"
    shared_root = tmp_path / "shared"
    parent = subprocess.Popen(
        [
            sys.executable,
            str(helper),
            str(marker),
            str(cwd),
            str(worker),
            key,
            str(shared_root),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        deadline = time.monotonic() + 5
        while (
            not (
                marker.exists()
                and (cwd / "child.pid").exists()
                and (cwd / "child.pid").read_text(encoding="ascii").strip().isdigit()
            )
            and time.monotonic() < deadline
        ):
            time.sleep(0.025)
        assert marker.exists() and (cwd / "child.pid").exists()
        identity = json.loads(marker.read_text(encoding="utf-8"))
        child_pid = int((cwd / "child.pid").read_text(encoding="ascii"))
        child_created = creation_time_for_pid(child_pid)
        assert same_process_alive(identity["pid"], identity["created"])
        assert same_process_alive(child_pid, child_created)
        parent.terminate()
        assert parent.wait(timeout=3) is not None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and (
            same_process_alive(identity["pid"], identity["created"])
            or same_process_alive(child_pid, child_created)
        ):
            time.sleep(0.025)
        assert not same_process_alive(identity["pid"], identity["created"])
        assert not same_process_alive(child_pid, child_created)

        async def verify_abandoned_job():
            lock = NamedGpuLock(key, shared_root)
            await lock.acquire()
            try:
                assert await lock.verify_previous(True)
                await lock.clear()
            finally:
                await lock.release()

        asyncio.run(verify_abandoned_job())
    finally:
        if parent.poll() is None:
            parent.terminate()
            parent.wait(timeout=3)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named mutex")
def test_named_device_lock_serializes_separate_instances(tmp_path):
    async def scenario():
        first = NamedGpuLock("test-shared-device", tmp_path)
        second = NamedGpuLock("test-shared-device", tmp_path)
        assert not await first.acquire()
        assert await first.verify_previous(False)
        await first.record_spawn_intent(1)
        waiter = asyncio.create_task(second.acquire())
        await asyncio.sleep(0.15)
        assert not waiter.done()
        await first.clear()
        await first.release()
        assert not await asyncio.wait_for(waiter, 2)
        assert await second.verify_previous(False)
        await second.release()

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named mutex")
def test_abandoned_journal_verifies_old_worker_before_reuse(tmp_path):
    helper = Path(__file__).parent / "helpers" / "abandon_gpu_lock.py"
    worker = (
        Path(__file__).parents[1]
        / "src"
        / "jarvis"
        / "infrastructure"
        / "fake_worker.py"
    )
    key = f"test-{uuid4().hex}"
    root = tmp_path / "shared"
    marker = tmp_path / "owner.json"
    root.mkdir()
    unsafe_worker = subprocess.Popen(
        [
            sys.executable,
            str(worker),
            "--mode",
            "child",
            "--ready-file",
            str(tmp_path / "unused-ready"),
            "--stop-file",
            str(tmp_path / "unused-stop"),
            "--child-pid-file",
            str(tmp_path / "unused-child"),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    created = creation_time_for_pid(unsafe_worker.pid)
    try:
        owner = subprocess.run(
            [
                sys.executable,
                str(helper),
                key,
                str(root),
                str(marker),
                str(unsafe_worker.pid),
                str(created),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert owner.returncode == 0, owner.stderr
        assert marker.exists()

        async def blocked_then_recovered():
            lock = NamedGpuLock(key, root)
            await lock.acquire()
            try:
                assert not await lock.verify_previous(True)
                unsafe_worker.terminate()
                unsafe_worker.wait(timeout=3)
                assert await lock.verify_previous(True)
                await lock.clear()
            finally:
                await lock.release()

        asyncio.run(blocked_then_recovered())
    finally:
        if unsafe_worker.poll() is None:
            unsafe_worker.terminate()
            unsafe_worker.wait(timeout=3)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named mutex")
def test_abandoned_spawn_intent_has_no_orphan(tmp_path):
    helper = Path(__file__).parent / "helpers" / "abandon_gpu_lock.py"
    key = f"test-{uuid4().hex}"
    root = tmp_path / "shared"
    marker = tmp_path / "owner.json"
    root.mkdir()
    owner = subprocess.run(
        [sys.executable, str(helper), key, str(root), str(marker)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert owner.returncode == 0, owner.stderr

    async def recover():
        lock = NamedGpuLock(key, root)
        await lock.acquire()
        try:
            assert await lock.verify_previous(True)
            await lock.clear()
        finally:
            await lock.release()

    asyncio.run(recover())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named mutex")
def test_named_device_lock_serializes_separate_processes(tmp_path):
    helper = Path(__file__).parent / "helpers" / "hold_gpu_lock.py"
    key = f"test-{uuid4().hex}"
    root = tmp_path / "shared"
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    root.mkdir()
    owner = subprocess.Popen(
        [sys.executable, str(helper), key, str(root), str(ready), str(release)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.025)
        assert ready.exists()

        async def wait_other_process():
            lock = NamedGpuLock(key, root)
            waiting = asyncio.create_task(lock.acquire())
            await asyncio.sleep(0.15)
            assert not waiting.done()
            release.touch()
            await asyncio.wait_for(waiting, 3)
            assert await lock.verify_previous(False)
            await lock.release()

        asyncio.run(wait_other_process())
        assert owner.wait(timeout=3) == 0
    finally:
        if owner.poll() is None:
            owner.terminate()
            owner.wait(timeout=3)
        if owner.stderr is not None:
            owner.stderr.close()


def test_cold_and_warm_admission_boundaries_and_missing_telemetry():
    assert not admit(TEXT, ResourceSnapshot(5, 8), warm=False).degraded_gpu_telemetry
    with pytest.raises(AdmissionDenied):
        admit(TEXT, ResourceSnapshot(4.99, 8), warm=False)
    with pytest.raises(AdmissionDenied) as denied:
        admit(TEXT, ResourceSnapshot(5, 7.99), warm=False)
    assert denied.value.resource == "ram"
    assert denied.value.available_gib == 7.99
    assert denied.value.required_gib == 8
    assert not admit(TEXT, ResourceSnapshot(1.5, 4), warm=True).degraded_gpu_telemetry
    assert admit(TEXT, ResourceSnapshot(None, 8), warm=False).degraded_gpu_telemetry
    with pytest.raises(AdmissionDenied):
        admit(
            RuntimeProfile("uncalibrated", 4, 5, 0.5, 1),
            ResourceSnapshot(None, 8),
            warm=False,
        )
    with pytest.raises(AdmissionDenied) as denied:
        admit(TEXT, ResourceSnapshot(8, None), warm=False)
    assert denied.value.available_gib is None
    with pytest.raises(ValueError):
        RuntimeProfile("too-large", 6.1, 5, 0.5, 1)
    with pytest.raises(ValueError):
        RuntimeProfile("nan", float("nan"), 5, 0.5, 1)
    with pytest.raises(AdmissionDenied):
        admit(TEXT, ResourceSnapshot(float("inf"), 8), warm=False)
    with pytest.raises(AdmissionDenied):
        admit(TEXT, ResourceSnapshot(8, float("nan")), warm=False)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows fake processes")
def test_calibrated_profile_reports_degraded_gpu_telemetry(tmp_path):
    async def scenario():
        manager = ResourceManager(
            FakeProcessRuntime(tmp_path / "run"),
            Probe(ResourceSnapshot(None, 12)),
            NamedGpuLock(f"test-{uuid4().hex}", tmp_path / "locks"),
            FakeClock(),
        )
        try:
            handle = await manager.acquire(TEXT)
            assert manager.view.degraded_gpu_telemetry
            await handle.release()
        finally:
            await manager.close()

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows fake processes")
def test_denied_warm_reuse_preserves_idle_unload(tmp_path):
    async def scenario():
        clock = FakeClock()
        probe = Probe()
        manager = ResourceManager(
            FakeProcessRuntime(tmp_path / "run"),
            probe,
            NamedGpuLock(f"test-{uuid4().hex}", tmp_path / "locks"),
            clock,
        )
        try:
            handle = await manager.acquire(TEXT)
            await handle.release()
            probe.metrics = ResourceSnapshot(1, 3)
            with pytest.raises(AdmissionDenied):
                await manager.acquire(TEXT)
            assert manager.view.state is ResourceState.READY
            idle_task = manager._idle_task
            assert idle_task is not None
            await asyncio.sleep(0)
            await clock.advance(120)
            await asyncio.wait_for(idle_task, 3)
            assert manager.view.state is ResourceState.STOPPED
        finally:
            await manager.close()

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows fake processes")
def test_warm_reuse_and_text_image_text_switch_never_overlap(tmp_path):
    async def scenario():
        runtime = FakeProcessRuntime(tmp_path / "run")
        clock = FakeClock()
        probe = Probe()
        manager = ResourceManager(
            runtime,
            probe,
            NamedGpuLock(f"test-{uuid4().hex}", tmp_path / "locks"),
            clock,
        )
        try:
            first = await manager.acquire(TEXT)
            first_pid = manager.view.identity.pid
            assert manager.view.state is ResourceState.BUSY
            await first.release()
            assert manager.view.state is ResourceState.READY
            probe.metrics = ResourceSnapshot(1.5, 4)
            warm = await manager.acquire(TEXT)
            assert manager.view.identity.pid == first_pid
            assert len([x for x in runtime.intervals if x[0] == "spawn"]) == 1
            await warm.release()
            await manager.idle_expired(1, first.request_id)
            assert manager.view.state is ResourceState.READY
            probe.metrics = ResourceSnapshot(8, 12)
            image = await manager.acquire(IMAGE)
            assert manager.view.identity.profile_name == IMAGE.name
            await image.release()
            text_again = await manager.acquire(TEXT)
            await text_again.release()
            events = [event for event, _, _ in runtime.intervals]
            assert events == ["spawn", "exit", "spawn", "exit", "spawn"]
            assert not await manager.notify_ready(1)
            await manager.notify_exit(1)
            await manager.idle_expired(1, 1)
            await manager.release(first)
            assert manager.view.state is ResourceState.READY
            await clock.advance(119)
            assert manager.view.state is ResourceState.READY
            idle_task = manager._idle_task
            assert idle_task is not None
            await clock.advance(1)
            await asyncio.wait_for(idle_task, 3)
            assert manager.view.state is ResourceState.STOPPED
            assert [event for event, _, _ in runtime.intervals][-1] == "exit"
        finally:
            await manager.close()

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows fake processes")
def test_concurrent_profile_switch_waits_for_current_request_and_tree_exit(tmp_path):
    async def scenario():
        runtime = FakeProcessRuntime(tmp_path / "run")
        manager = ResourceManager(
            runtime,
            Probe(),
            NamedGpuLock(f"test-{uuid4().hex}", tmp_path / "locks"),
            FakeClock(),
        )
        try:
            current = await manager.acquire(TEXT)
            waiting = asyncio.create_task(manager.acquire(IMAGE))
            await asyncio.sleep(0.05)
            assert not waiting.done()
            assert manager.view.state is ResourceState.BUSY
            await current.release()
            next_handle = await asyncio.wait_for(waiting, 3)
            assert manager.view.identity.profile_name == IMAGE.name
            assert [event for event, _, _ in runtime.intervals] == [
                "spawn",
                "exit",
                "spawn",
            ]
            await next_handle.release()
        finally:
            await manager.close()

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows fake processes")
def test_cross_directory_device_ownership_blocks_warm_other_instance(tmp_path):
    async def scenario():
        key = f"test-{uuid4().hex}"
        first = ResourceManager(
            FakeProcessRuntime(tmp_path / "one"),
            Probe(),
            NamedGpuLock(key, tmp_path / "shared"),
            FakeClock(),
        )
        second = ResourceManager(
            FakeProcessRuntime(tmp_path / "two"),
            Probe(),
            NamedGpuLock(key, tmp_path / "shared"),
            FakeClock(),
        )
        try:
            handle = await first.acquire(TEXT)
            await handle.release()
            waiting = asyncio.create_task(second.acquire(IMAGE))
            await asyncio.sleep(0.15)
            assert not waiting.done()
            assert first.view.state is ResourceState.READY
            await first.close()
            other = await asyncio.wait_for(waiting, 3)
            assert second.view.state is ResourceState.BUSY
            await other.release()
        finally:
            await first.close()
            await second.close()

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows fake processes")
def test_cancelled_waiter_does_not_stop_current_owner(tmp_path):
    async def scenario():
        runtime = FakeProcessRuntime(tmp_path / "run")
        manager = ResourceManager(
            runtime,
            Probe(),
            NamedGpuLock(f"test-{uuid4().hex}", tmp_path / "locks"),
            FakeClock(),
        )
        try:
            current = await manager.acquire(TEXT)
            waiting = asyncio.create_task(manager.acquire(IMAGE))
            await asyncio.sleep(0)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            assert manager.view.state is ResourceState.BUSY
            assert len(runtime.processes) == 1
            await current.release()
            assert manager.view.state is ResourceState.READY
        finally:
            await manager.close()

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows fake processes")
@pytest.mark.parametrize("mode", ["crash", "stall"])
def test_readiness_failure_cleans_before_releasing_slot(tmp_path, mode):
    async def scenario():
        runtime = FakeProcessRuntime(
            tmp_path / "run", modes={TEXT.name: mode}, readiness_timeout=0.2
        )
        manager = ResourceManager(
            runtime,
            Probe(),
            NamedGpuLock(f"test-{uuid4().hex}", tmp_path / "locks"),
            FakeClock(),
        )
        with pytest.raises(RuntimeStartFailure):
            await manager.acquire(TEXT)
        assert manager.view.state is ResourceState.STOPPED
        assert runtime.processes == {}
        await manager.close()

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows fake processes")
def test_cancellation_during_load_waits_for_tree_cleanup(tmp_path):
    async def scenario():
        runtime = FakeProcessRuntime(
            tmp_path / "run", ready_delays={TEXT.name: 2}, readiness_timeout=3
        )
        manager = ResourceManager(
            runtime,
            Probe(),
            NamedGpuLock(f"test-{uuid4().hex}", tmp_path / "locks"),
            FakeClock(),
        )
        task = asyncio.create_task(manager.acquire(TEXT))
        for _ in range(100):
            if runtime.processes:
                break
            await asyncio.sleep(0.01)
        assert runtime.processes
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert manager.view.state is ResourceState.STOPPED
        assert runtime.processes == {}
        await manager.close()

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows fake processes")
def test_close_racing_start_waits_for_owned_cleanup(tmp_path):
    async def scenario():
        runtime = FakeProcessRuntime(tmp_path / "run", ready_delays={TEXT.name: 0.2})
        manager = ResourceManager(
            runtime,
            Probe(),
            NamedGpuLock(f"test-{uuid4().hex}", tmp_path / "locks"),
            FakeClock(),
        )
        starting = asyncio.create_task(manager.acquire(TEXT))
        for _ in range(100):
            if runtime.processes:
                break
            await asyncio.sleep(0.01)
        assert runtime.processes
        closing = asyncio.create_task(manager.close())
        handle = await asyncio.wait_for(starting, 3)
        await asyncio.wait_for(closing, 3)
        assert manager.view.state is ResourceState.STOPPED
        assert runtime.processes == {}
        await handle.release()
        with pytest.raises(ResourceUnavailable):
            await manager.acquire(TEXT)

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named mutex")
def test_cancelled_previous_owner_verification_retains_journal(tmp_path):
    async def scenario():
        started = asyncio.Event()

        class SlowVerification(NamedGpuLock):
            async def verify_previous(self, abandoned: bool) -> bool:
                started.set()
                await asyncio.Event().wait()
                return await super().verify_previous(abandoned)

        lock = SlowVerification(f"test-{uuid4().hex}", tmp_path / "locks")
        manager = ResourceManager(
            FakeProcessRuntime(tmp_path / "run"), Probe(), lock, FakeClock()
        )
        task = asyncio.create_task(manager.acquire(TEXT))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert manager.view.state is ResourceState.UNAVAILABLE
        assert manager._lock_held
        lock.verify_previous = NamedGpuLock.verify_previous.__get__(lock)
        assert await manager.recover()
        await manager.close()

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows fake processes")
def test_failed_start_cleanup_can_be_retried_without_releasing_owner(tmp_path):
    async def scenario():
        runtime = FakeProcessRuntime(
            tmp_path / "run", modes={TEXT.name: "stall"}, readiness_timeout=0.1
        )
        original_abort = runtime.abort_start

        async def fail_abort(_generation):
            return False

        runtime.abort_start = fail_abort
        manager = ResourceManager(
            runtime,
            Probe(),
            NamedGpuLock(f"test-{uuid4().hex}", tmp_path / "locks"),
            FakeClock(),
        )
        with pytest.raises(ResourceUnavailable):
            await manager.acquire(TEXT)
        assert manager.view.state is ResourceState.UNAVAILABLE
        assert manager._lock_held
        runtime.abort_start = original_abort
        assert await manager.recover()
        assert manager.view.state is ResourceState.STOPPED
        assert runtime.processes == {}
        await manager.close()

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows fake processes")
def test_unexpected_exit_revokes_busy_request_and_cleans_slot(tmp_path):
    async def scenario():
        runtime = FakeProcessRuntime(tmp_path / "run", modes={TEXT.name: "crash_later"})
        manager = ResourceManager(
            runtime,
            Probe(),
            NamedGpuLock(f"test-{uuid4().hex}", tmp_path / "locks"),
            FakeClock(),
        )
        try:
            handle = await manager.acquire(TEXT)
            deadline = asyncio.get_running_loop().time() + 3
            while (
                manager.view.state is not ResourceState.STOPPED
                and asyncio.get_running_loop().time() < deadline
            ):
                await asyncio.sleep(0.025)
            assert manager.view.state is ResourceState.STOPPED
            assert runtime.processes == {}
            await handle.release()
            assert manager.view.state is ResourceState.STOPPED
        finally:
            await manager.close()

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows fake processes")
def test_hung_stop_terminates_entire_job_and_failed_verification_blocks(tmp_path):
    async def scenario():
        runtime = FakeProcessRuntime(
            tmp_path / "run", modes={TEXT.name: "hang"}, graceful_seconds=0.1
        )
        manager = ResourceManager(
            runtime,
            Probe(),
            NamedGpuLock(f"test-{uuid4().hex}", tmp_path / "locks"),
            FakeClock(),
        )
        handle = await manager.acquire(TEXT)
        await handle.release()
        identity = manager.view.identity
        original_stop = runtime.stop

        async def unverified(_identity):
            return False

        runtime.stop = unverified
        with pytest.raises(ResourceUnavailable):
            await manager.acquire(IMAGE)
        assert manager.view.state is ResourceState.UNAVAILABLE
        with pytest.raises(ResourceUnavailable):
            await manager.acquire(TEXT)
        assert identity is not None
        runtime.stop = original_stop
        assert await runtime.stop(identity)
        assert await manager.recover()
        assert manager.view.state is ResourceState.STOPPED
        await manager.close()

    asyncio.run(scenario())
