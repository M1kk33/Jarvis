"""One bounded, explicit-resume runner for durable chat jobs."""

import asyncio
from collections.abc import Awaitable, Callable
from threading import Event
from uuid import uuid4

from jarvis.application.conversations import ConversationService, _error_code
from jarvis.domain.jobs import TERMINAL_JOBS, Job, JobStatus
from jarvis.domain.ports import JobStore
from jarvis.domain.resources import ResourceUnavailable
from jarvis.domain.types import (
    RequestFailure,
    StorageFailure,
    SubmissionConflict,
)


class JobRunner:
    def __init__(
        self,
        store: JobStore,
        conversations: ConversationService,
        profile_identity: str | None = None,
    ) -> None:
        self.store = store
        self.conversations = conversations
        self.profile_identity = profile_identity or (
            conversations.runtime_profile.name
            if conversations.runtime_profile is not None
            else "fake"
        )
        self._execution = asyncio.Lock()
        self._active: dict[str, tuple[Event, asyncio.Task[Job]]] = {}
        self._background: set[asyncio.Task[Job]] = set()
        self._launched: dict[str, asyncio.Task[Job]] = {}
        self._diagnostics = conversations.diagnostics

    async def submit(
        self, conversation_id: str, text: str, submission_id: str | None = None
    ) -> tuple[Job, bool]:
        stable_id = submission_id or str(uuid4())
        job_id = str(uuid4())
        profile_name = self.profile_identity
        try:
            turn, created = await self.store.run(
                lambda: self.store.begin_turn(
                    conversation_id, text, stable_id, job_id, profile_name
                )
            )
        except asyncio.CancelledError:
            prior = await self.conversations.store.run(
                lambda: self.conversations.store.get_turn_by_submission(
                    conversation_id, stable_id
                )
            )
            if prior is None:
                raise
            turn = prior
            created = False
        job = await self.store.run(lambda: self.store.get_job_by_turn(turn.id))
        if job is None:
            raise SubmissionConflict("Existing submission predates M6 jobs")
        if created:
            self._diagnostics("job_queued", {"job_id": job.id, "turn_id": turn.id})
        return job, not created

    def launch(
        self,
        job_id: str,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> asyncio.Task[Job]:
        existing = self._launched.get(job_id)
        if existing is not None and not existing.done():
            return existing
        task = asyncio.create_task(self.run_job(job_id, on_delta))
        self._background.add(task)
        self._launched[job_id] = task

        def finished(completed: asyncio.Task[Job]) -> None:
            self._background.discard(completed)
            if self._launched.get(job_id) is completed:
                self._launched.pop(job_id, None)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finished)
        return task

    async def wait_for_idle(self) -> None:
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

    async def run_job(
        self,
        job_id: str,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> Job:
        try:
            async with self._execution:
                return await self._run_locked(job_id, on_delta)
        except asyncio.CancelledError:
            return await self._settle_cancellation(job_id)

    async def _settle_cancellation(self, job_id: str) -> Job:
        job = await self.store.run(lambda: self.store.get_job(job_id))
        if job.status in TERMINAL_JOBS:
            return job
        if not job.cancel_requested:
            await self.store.run(lambda: self.store.request_job_cancel(job_id))
        if job.profile_name != "fake" and job.attempt > 0:
            manager = self.conversations.resource_manager
            profile = self.conversations.runtime_profile
            if (
                manager is None
                or profile is None
                or self.profile_identity != job.profile_name
                or not await manager.verify_previous_worker()
            ):
                raise ResourceUnavailable("Previous model-worker cleanup is unverified")
        settled = await self.store.run(lambda: self.store.settle_job_cancel(job_id))
        self._diagnostics("job_cancelled", {"job_id": job_id})
        return settled

    async def _run_locked(
        self,
        job_id: str,
        on_delta: Callable[[str], Awaitable[None]] | None,
    ) -> Job:
        job = await self.store.run(lambda: self.store.get_job(job_id))
        if job.status in TERMINAL_JOBS:
            return job
        expected_profile = self.profile_identity
        if job.profile_name != expected_profile:
            raise RequestFailure(
                "unsupported_capability", "Job requires its original model profile"
            )
        signal = Event()
        current = asyncio.current_task()
        assert current is not None
        self._active[job_id] = (signal, current)
        try:
            if job.cancel_requested:
                return await self._settle_cancellation(job_id)
            if job.status is JobStatus.RUNNING:
                invocations = await self.conversations.store.run(
                    lambda: self.conversations.store.get_invocations(job.turn_id)
                )
                if job.progress != "tool_complete" or not any(
                    item.decision == "approval" and item.status.value == "complete"
                    for item in invocations
                ):
                    raise RequestFailure("invalid_request", "Job is already running")
            else:
                job = await self.store.run(lambda: self.store.start_job(job_id))
                if job.status in TERMINAL_JOBS:
                    return job
            self._diagnostics("job_running", {"job_id": job_id, "attempt": job.attempt})
            latest = await self.store.run(lambda: self.store.get_job(job_id))
            if signal.is_set() or latest.cancel_requested:
                return await self._settle_cancellation(job_id)
            answer = await asyncio.wait_for(
                self.conversations.generate_existing_turn_async(
                    job.turn_id, job.conversation_id, signal, on_delta, job.attempt
                ),
                timeout=600,
            )
            if signal.is_set():
                return await self._settle_cancellation(job_id)
            if answer is None:
                waiting = await self.store.run(
                    lambda: self.store.mark_job_waiting(job_id)
                )
                self._diagnostics("job_waiting", {"job_id": job_id})
                return waiting
            await self.store.run(
                lambda: self.store.complete_turn(job.turn_id, answer, job.attempt)
            )
            completed = await self.store.run(lambda: self.store.get_job(job_id))
            self._diagnostics("job_succeeded", {"job_id": job_id})
            return completed
        except asyncio.CancelledError:
            signal.set()
            return await self._settle_cancellation(job_id)
        except Exception as exc:
            settled = await self.store.run(lambda: self.store.get_job(job_id))
            if settled.status in TERMINAL_JOBS:
                return settled
            if settled.cancel_requested or _error_code(exc) == "cancelled":
                return await self._settle_cancellation(job_id)
            if (
                not isinstance(exc, StorageFailure)
                and settled.status is JobStatus.RUNNING
            ):
                code = _error_code(exc)
                await self.store.run(
                    lambda: self.store.fail_turn(job.turn_id, code, job.attempt)
                )
                self._diagnostics("job_failed", {"job_id": job_id, "code": code})
            raise
        finally:
            self._active.pop(job_id, None)

    async def cancel(self, job_id: str) -> Job:
        job = await self.store.run(lambda: self.store.request_job_cancel(job_id))
        if job.status is JobStatus.CANCEL_REQUESTED:
            self._diagnostics("job_cancel_requested", {"job_id": job_id})
        active = self._active.get(job_id)
        if active is not None:
            signal, task = active
            signal.set()
            task.cancel()
            return job
        if job.status is JobStatus.CANCEL_REQUESTED:
            return await self._settle_cancellation(job_id)
        return job

    async def status(self, job_id: str) -> Job:
        return await self.store.run(lambda: self.store.get_job(job_id))

    async def list_jobs(self, conversation_id: str | None = None) -> list[Job]:
        return await self.store.run(lambda: self.store.list_jobs(conversation_id))
