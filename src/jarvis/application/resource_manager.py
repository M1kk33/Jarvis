"""One serialized heavy-runtime slot, independent of conversation/job scheduling."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from threading import Event
from typing import Self

from jarvis.domain.resource_ports import (
    Clock,
    ManagedRuntime,
    OwnershipLock,
    ResourceProbe,
)
from jarvis.domain.resources import (
    Admission,
    ResourceState,
    ResourceUnavailable,
    RuntimeIdentity,
    RuntimeProfile,
    admit,
)


async def _settle[T](task: asyncio.Task[T]) -> T:
    """Finish bounded cleanup even if its caller is cancelled again."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


class RuntimeRouter:
    """Dispatch profiles through one ResourceManager and one device lock."""

    def __init__(self, runtimes: dict[str, ManagedRuntime]) -> None:
        self.runtimes = dict(runtimes)
        self._generations: dict[int, ManagedRuntime] = {}

    async def start(
        self, profile: RuntimeProfile, generation: int, cancel: Event, job_name: str
    ) -> RuntimeIdentity:
        runtime = self.runtimes.get(profile.name)
        if runtime is None:
            raise ResourceUnavailable("Unknown heavy-runtime profile")
        self._generations[generation] = runtime
        return await runtime.start(profile, generation, cancel, job_name)

    def _runtime(self, generation: int) -> ManagedRuntime:
        try:
            return self._generations[generation]
        except KeyError as exc:
            raise ResourceUnavailable("Unknown runtime generation") from exc

    async def stop(self, identity: RuntimeIdentity) -> bool:
        return await self._runtime(identity.generation).stop(identity)

    async def abort_start(self, generation: int) -> bool:
        return await self._runtime(generation).abort_start(generation)

    async def wait_exit(self, identity: RuntimeIdentity) -> None:
        await self._runtime(identity.generation).wait_exit(identity)

    async def verify_stopped(self, identity: RuntimeIdentity) -> bool:
        return await self._runtime(identity.generation).verify_stopped(identity)


@dataclass(frozen=True)
class ResourceView:
    state: ResourceState
    generation: int
    identity: RuntimeIdentity | None
    request_id: int
    degraded_gpu_telemetry: bool


class OperationHandle:
    def __init__(
        self, manager: "ResourceManager", generation: int, request_id: int
    ) -> None:
        self._manager = manager
        self.generation = generation
        self.request_id = request_id
        self._released = False

    async def release(self) -> None:
        if not self._released:
            await _settle(asyncio.create_task(self._manager.release(self)))
            self._released = True

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.release()


class ResourceManager:
    def __init__(
        self,
        runtime: ManagedRuntime,
        probe: ResourceProbe,
        ownership: OwnershipLock,
        clock: Clock,
    ) -> None:
        self.runtime = runtime
        self.probe = probe
        self.ownership = ownership
        self.clock = clock
        self._condition = asyncio.Condition()
        self._state = ResourceState.STOPPED
        self._generation = 0
        self._request_id = 0
        self._identity: RuntimeIdentity | None = None
        self._profile: RuntimeProfile | None = None
        self._admission = Admission(False)
        self._idle_task: asyncio.Task[None] | None = None
        self._exit_task: asyncio.Task[None] | None = None
        self._owner_task: asyncio.Task[object] | None = None
        self._lock_held = False
        self._own_spawn_intent = False
        self._closed = False
        self._cpu_worker = False

    @property
    def view(self) -> ResourceView:
        return ResourceView(
            self._state,
            self._generation,
            self._identity,
            self._request_id,
            self._admission.degraded_gpu_telemetry,
        )

    async def acquire(self, profile: RuntimeProfile) -> OperationHandle:
        caller = asyncio.current_task()
        async with self._condition:
            while self._state is ResourceState.BUSY or self._cpu_worker:
                if caller is self._owner_task:
                    raise ResourceUnavailable(
                        "Nested heavy-runtime acquisition is forbidden"
                    )
                await self._condition.wait()
            if self._closed or self._state is ResourceState.UNAVAILABLE:
                raise ResourceUnavailable("Heavy runtime is unavailable")
            self._cancel_idle()
            if self._state is ResourceState.READY and self._profile == profile:
                try:
                    self._admission = admit(profile, self.probe.snapshot(), warm=True)
                except BaseException:
                    self._schedule_idle()
                    raise
            else:
                if self._state is ResourceState.READY:
                    await self._stop_locked()
                if self._state is not ResourceState.STOPPED:
                    raise ResourceUnavailable("Previous runtime cleanup is unverified")
                await self._start_locked(profile)
            self._request_id += 1
            self._owner_task = caller
            self._state = ResourceState.BUSY
            return OperationHandle(self, self._generation, self._request_id)

    @asynccontextmanager
    async def cpu_worker(self) -> AsyncIterator[None]:
        """Keep heavy requests and model switches out of a CPU worker's RAM peak."""
        async with self._condition:
            while self._state is ResourceState.BUSY or self._cpu_worker:
                await self._condition.wait()
            if self._closed or self._state is ResourceState.UNAVAILABLE:
                raise ResourceUnavailable("Heavy-runtime cleanup is unverified")
            self._cpu_worker = True
        try:
            yield
        finally:
            await _settle(asyncio.create_task(self._release_cpu_worker()))

    async def _release_cpu_worker(self) -> None:
        async with self._condition:
            self._cpu_worker = False
            self._condition.notify_all()

    async def _start_locked(self, profile: RuntimeProfile) -> None:
        self._state = ResourceState.STARTING
        self._generation += 1
        generation = self._generation
        signal = Event()
        start_issued = False
        previous_verified = False
        try:
            abandoned = await self.ownership.acquire()
            self._lock_held = True
            if not await self.ownership.verify_previous(abandoned):
                self._state = ResourceState.UNAVAILABLE
                raise ResourceUnavailable("Previous owned worker cleanup is unverified")
            previous_verified = True
            self._admission = admit(profile, self.probe.snapshot(), warm=False)
            job_name = await self.ownership.record_spawn_intent(generation)
            self._own_spawn_intent = True
            start_issued = True
            start_task = asyncio.create_task(
                self.runtime.start(profile, generation, signal, job_name)
            )
            try:
                identity = await asyncio.shield(start_task)
            except asyncio.CancelledError:
                signal.set()
                try:
                    await _settle(start_task)
                except Exception:  # noqa: BLE001, S110 - cancellation owns the original outcome
                    pass
                raise
            if (
                identity.generation != generation
                or identity.profile_name != profile.name
            ):
                raise ResourceUnavailable("Runtime returned the wrong identity")
            self._identity = identity
            await self.ownership.record_running(identity)
            self._profile = profile
            self._state = ResourceState.READY
            self._exit_task = asyncio.create_task(self._watch_exit(identity))
        except BaseException:
            if self._lock_held and not previous_verified:
                self._state = ResourceState.UNAVAILABLE
                raise
            if start_issued:
                try:
                    stopped = await _settle(
                        asyncio.create_task(self.runtime.abort_start(generation))
                    )
                except Exception:  # noqa: BLE001 - runtime cleanup boundary
                    stopped = False
                if not stopped:
                    self._state = ResourceState.UNAVAILABLE
                    raise ResourceUnavailable("Startup cleanup is unverified") from None
            if self._lock_held:
                try:
                    await _settle(asyncio.create_task(self.ownership.clear()))
                    await _settle(asyncio.create_task(self.ownership.release()))
                except Exception as exc:
                    self._state = ResourceState.UNAVAILABLE
                    raise ResourceUnavailable(
                        "Startup ownership release is unverified"
                    ) from exc
                self._lock_held = False
                self._own_spawn_intent = False
            self._identity = None
            self._profile = None
            self._state = ResourceState.STOPPED
            raise

    async def release(self, handle: OperationHandle) -> None:
        async with self._condition:
            if (
                self._state is not ResourceState.BUSY
                or handle.generation != self._generation
                or handle.request_id != self._request_id
            ):
                return
            self._owner_task = None
            self._state = ResourceState.READY
            self._condition.notify_all()
            self._schedule_idle()

    def _schedule_idle(self) -> None:
        assert self._profile is not None and self._identity is not None
        self._idle_task = asyncio.create_task(
            self._idle_after(
                self._identity.generation,
                self._request_id,
                self._profile.idle_seconds,
            )
        )

    async def _idle_after(self, generation: int, request_id: int, delay: float) -> None:
        try:
            await self.clock.sleep(delay)
            await self.idle_expired(generation, request_id)
        except asyncio.CancelledError:
            return

    async def idle_expired(self, generation: int, request_id: int) -> None:
        async with self._condition:
            if (
                self._state is ResourceState.READY
                and generation == self._generation
                and request_id == self._request_id
            ):
                await self._stop_locked()

    async def notify_ready(self, generation: int) -> bool:
        async with self._condition:
            return (
                self._state is ResourceState.STARTING and generation == self._generation
            )

    async def notify_exit(self, generation: int) -> None:
        async with self._condition:
            if generation != self._generation or self._state not in {
                ResourceState.READY,
                ResourceState.BUSY,
            }:
                return
            self._state = ResourceState.FAULTED
            self._owner_task = None
            self._condition.notify_all()
            await self._stop_locked()

    async def _watch_exit(self, identity: RuntimeIdentity) -> None:
        try:
            await self.runtime.wait_exit(identity)
            await self.notify_exit(identity.generation)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 - runtime watcher boundary
            await self.notify_exit(identity.generation)

    async def _stop_locked(self) -> None:
        self._cancel_idle()
        identity = self._identity
        if identity is None:
            self._state = ResourceState.STOPPED
            return
        self._state = ResourceState.STOPPING
        try:
            stopped = await _settle(asyncio.create_task(self.runtime.stop(identity)))
        except Exception:  # noqa: BLE001 - runtime cleanup boundary
            stopped = False
        if not stopped:
            self._state = ResourceState.UNAVAILABLE
            self._condition.notify_all()
            raise ResourceUnavailable("Owned process-tree exit is unverified")
        if (
            self._exit_task is not None
            and self._exit_task is not asyncio.current_task()
        ):
            self._exit_task.cancel()
        try:
            await _settle(asyncio.create_task(self.ownership.clear()))
            await _settle(asyncio.create_task(self.ownership.release()))
        except Exception as exc:
            self._state = ResourceState.UNAVAILABLE
            self._condition.notify_all()
            raise ResourceUnavailable("Device ownership release is unverified") from exc
        self._lock_held = False
        self._own_spawn_intent = False
        self._identity = None
        self._profile = None
        self._state = ResourceState.STOPPED
        self._condition.notify_all()

    def _cancel_idle(self) -> None:
        if self._idle_task is not None:
            if self._idle_task is not asyncio.current_task():
                self._idle_task.cancel()
            self._idle_task = None

    async def recover(self) -> bool:
        """Explicitly retry verification after an unavailable cleanup."""
        async with self._condition:
            if self._state is not ResourceState.UNAVAILABLE:
                return self._state is ResourceState.STOPPED
            if self._identity is not None:
                try:
                    if not await _settle(
                        asyncio.create_task(self.runtime.stop(self._identity))
                    ):
                        return False
                except Exception:  # noqa: BLE001 - recovery boundary
                    return False
                if not await self.runtime.verify_stopped(self._identity):
                    return False
            elif self._own_spawn_intent:
                try:
                    if not await _settle(
                        asyncio.create_task(self.runtime.abort_start(self._generation))
                    ):
                        return False
                except Exception:  # noqa: BLE001 - recovery boundary
                    return False
            elif not await self.ownership.verify_previous(False):
                return False
            if self._lock_held:
                await self.ownership.clear()
                await self.ownership.release()
                self._lock_held = False
                self._own_spawn_intent = False
            self._identity = None
            self._profile = None
            self._state = ResourceState.STOPPED
            self._condition.notify_all()
            return True

    async def verify_previous_worker(self) -> bool:
        """Verify crash-era ownership before settling a persisted cancellation."""
        if self._state is ResourceState.UNAVAILABLE:
            return await self.recover()
        async with self._condition:
            if self._state is not ResourceState.STOPPED:
                return self._state is ResourceState.READY
            abandoned = await self.ownership.acquire()
            try:
                verified = await self.ownership.verify_previous(abandoned)
                if verified:
                    await self.ownership.clear()
                return verified
            finally:
                await self.ownership.release()

    async def abort_request(self, handle: OperationHandle) -> None:
        """Dispose a runtime after an unverified or cancelled inference request."""
        async with self._condition:
            if (
                self._state is ResourceState.BUSY
                and handle.generation == self._generation
                and handle.request_id == self._request_id
            ):
                self._owner_task = None
                await self._stop_locked()

    async def unload(self) -> None:
        """Explicitly release an idle resident runtime."""
        async with self._condition:
            if asyncio.current_task() is self._owner_task:
                raise ResourceUnavailable("Cannot unload during the active request")
            while self._state is ResourceState.BUSY:
                await self._condition.wait()
            if self._state is ResourceState.READY:
                await self._stop_locked()
            elif self._state is ResourceState.UNAVAILABLE:
                raise ResourceUnavailable("Owned runtime cleanup is unverified")

    async def close(self) -> None:
        needs_recovery = False
        async with self._condition:
            self._closed = True
            while self._cpu_worker:
                await self._condition.wait()
            self._cancel_idle()
            if self._state in {
                ResourceState.READY,
                ResourceState.BUSY,
                ResourceState.FAULTED,
            }:
                await self._stop_locked()
            needs_recovery = self._state is ResourceState.UNAVAILABLE
            self._condition.notify_all()
        if needs_recovery and not await _settle(asyncio.create_task(self.recover())):
            raise ResourceUnavailable("Owned process-tree cleanup remains unverified")
