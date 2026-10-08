"""One bounded, explicit-resume runner for durable chat jobs."""

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from threading import Event
from uuid import uuid4

from jarvis.application.conversations import ConversationService, _error_code
from jarvis.application.resource_manager import _settle
from jarvis.application.vision import (
    MAX_QUESTION_CHARS,
    VisionService,
    validate_screenshot,
)
from jarvis.domain.jobs import TERMINAL_JOBS, Job, JobStatus, same_profile_identity
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
        vision: VisionService | None = None,
    ) -> None:
        self.store = store
        self.conversations = conversations
        self.profile_identity = profile_identity or (
            conversations.runtime_profile.name
            if conversations.runtime_profile is not None
            else "fake"
        )
        self.vision = vision
        self._execution = asyncio.Lock()
        self._active: dict[str, tuple[Event, asyncio.Task[Job]]] = {}
        self._background: set[asyncio.Task[Job]] = set()
        self._launched: dict[str, asyncio.Task[Job]] = {}
        self._approved_launches: set[str] = set()
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
            user, vision = await self.store.run(
                lambda: (
                    self.store.get_user_message(prior.id),
                    self.store.get_vision_input(prior.id),
                )
            )
            if user.text != text or vision is not None:
                raise SubmissionConflict("Submission ID already has different input")
            turn = prior
            created = False
        job = await self.store.run(lambda: self.store.get_job_by_turn(turn.id))
        if job is None:
            raise SubmissionConflict("Existing submission predates M6 jobs")
        if created:
            self._diagnostics("job_queued", {"job_id": job.id, "turn_id": turn.id})
        return job, not created

    async def submit_vision(
        self,
        conversation_id: str,
        question: str,
        image: bytes,
        submission_id: str,
    ) -> tuple[Job, bool]:
        if self.vision is None:
            raise RequestFailure("unsupported_capability", "Vision is not configured")
        vision_service = self.vision
        if not question.strip() or len(question) > MAX_QUESTION_CHARS:
            raise RequestFailure(
                "invalid_request", "Screenshot question is out of bounds"
            )
        if not submission_id or len(submission_id) > 200:
            raise RequestFailure("invalid_request", "Submission ID is out of bounds")
        validate_screenshot(image)
        digest = hashlib.sha256(image).hexdigest()

        def previous():
            self.conversations.store.get_conversation(conversation_id)
            prior = self.conversations.store.get_turn_by_submission(
                conversation_id, submission_id
            )
            if prior is None:
                return None
            return (
                prior,
                self.store.get_user_message(prior.id),
                self.store.get_vision_input(prior.id),
                self.store.get_job_by_turn(prior.id),
            )

        prior = await self.store.run(previous)
        if prior is not None:
            turn, user, source, existing_job = prior
            if (
                user.text != question
                or source is None
                or source.sha256 != digest
                or not same_profile_identity(
                    source.profile_name, vision_service.profile_identity
                )
            ):
                raise SubmissionConflict("Submission ID already has different input")
            if existing_job is None:
                raise SubmissionConflict("Screenshot submission has no durable job")
            return existing_job, True
        job_id = str(uuid4())
        attachment = await vision_service.stage(job_id, image)
        try:
            turn, created = await self.store.run(
                lambda: self.store.begin_turn(
                    conversation_id,
                    question,
                    submission_id,
                    job_id,
                    vision_service.profile_identity,
                    attachment,
                )
            )
        except asyncio.CancelledError:
            prior = await self.store.run(
                lambda: self.conversations.store.get_turn_by_submission(
                    conversation_id, submission_id
                )
            )
            if prior is None:
                raise
            stored, user = await self.store.run(
                lambda: (
                    self.store.get_vision_input(prior.id),
                    self.store.get_user_message(prior.id),
                )
            )
            if (
                stored is None
                or user.text != question
                or stored.sha256 != attachment.sha256
                or not same_profile_identity(
                    stored.profile_name, attachment.profile_name
                )
            ):
                raise SubmissionConflict(
                    "Screenshot submission outcome differs"
                ) from None
            turn, created = prior, False
        job = await self.store.run(lambda: self.store.get_job_by_turn(turn.id))
        if job is None:
            raise SubmissionConflict("Screenshot submission has no durable job")
        return job, not created

    def launch(
        self,
        job_id: str,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        approved: tuple[str, str] | None = None,
    ) -> asyncio.Task[Job]:
        existing = self._launched.get(job_id)
        if (
            existing is not None
            and not existing.done()
            and (approved is None or job_id in self._approved_launches)
        ):
            return existing

        async def dispatch() -> Job:
            if existing is not None and not existing.done():
                await asyncio.shield(existing)
            return await self.run_job(job_id, on_delta, approved)

        task = asyncio.create_task(dispatch())
        if approved is not None:
            self._approved_launches.add(job_id)
        self._background.add(task)
        self._launched[job_id] = task

        def finished(completed: asyncio.Task[Job]) -> None:
            self._background.discard(completed)
            if self._launched.get(job_id) is completed:
                self._launched.pop(job_id, None)
                self._approved_launches.discard(job_id)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finished)
        return task

    async def wait_for_idle(self) -> None:
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

    async def shutdown(self) -> None:
        """Cancel dispatched work and wait for its receipts and cleanup to settle."""
        for job_id in tuple(self._launched):
            await self.cancel(job_id)
        await self.wait_for_idle()

    async def run_job(
        self,
        job_id: str,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        approved: tuple[str, str] | None = None,
    ) -> Job:
        try:
            async with self._execution:
                return await self._run_locked(job_id, on_delta, approved)
        except asyncio.CancelledError:
            return await _settle(asyncio.create_task(self._settle_cancellation(job_id)))

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
                or not any(
                    same_profile_identity(job.profile_name, identity)
                    for identity in (
                        self.profile_identity,
                        self.vision.profile_identity if self.vision is not None else "",
                    )
                )
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
        approved: tuple[str, str] | None = None,
    ) -> Job:
        job = await self.store.run(lambda: self.store.get_job(job_id))
        if job.status in TERMINAL_JOBS:
            return job
        vision_input = await self.store.run(
            lambda: self.store.get_vision_input(job.turn_id)
        )
        is_vision = vision_input is not None
        expected_profile = (
            self.vision.profile_identity
            if is_vision and self.vision is not None
            else self.profile_identity
        )
        if not same_profile_identity(job.profile_name, expected_profile) or (
            is_vision and self.vision is None
        ):
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
            if approved is not None:
                tools = self.conversations.tool_service
                if tools is None:
                    raise RequestFailure("invalid_request", "Tools are not configured")
                invocation_id, approval_id = approved
                invocation = await tools.store.run(
                    lambda: tools.store.get_invocation(invocation_id)
                )
                if invocation.job_id != job_id or invocation.job_attempt != job.attempt:
                    raise RequestFailure(
                        "invalid_request", "Approval belongs to another attempt"
                    )
                job = await self.store.run(
                    lambda: self.store.continue_approved_job(job_id, job.attempt)
                )
                remaining = self._remaining(job)
                await asyncio.wait_for(
                    tools.execute(invocation_id, approval_id), remaining
                )
            elif job.status is JobStatus.RUNNING:
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
            answer: str | None
            remaining = self._remaining(job)
            if is_vision:
                assert self.vision is not None and vision_input is not None
                answer = await asyncio.wait_for(
                    self.vision.generate(
                        job.turn_id, vision_input, signal, on_delta, job.attempt
                    ),
                    timeout=remaining,
                )
            else:
                answer = await asyncio.wait_for(
                    self.conversations.generate_existing_turn_async(
                        job.turn_id, job.conversation_id, signal, on_delta, job.attempt
                    ),
                    timeout=remaining,
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
            return await _settle(asyncio.create_task(self._settle_cancellation(job_id)))
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

    @staticmethod
    def _remaining(job: Job) -> float:
        remaining = (
            datetime.fromisoformat(job.deadline_at) - datetime.now(UTC)
        ).total_seconds()
        if remaining <= 0:
            raise RequestFailure("timeout", "Job execution deadline expired")
        return remaining

    async def cancel(self, job_id: str) -> Job:
        job = await self.store.run(lambda: self.store.request_job_cancel(job_id))
        if job.status is JobStatus.CANCEL_REQUESTED:
            self._diagnostics("job_cancel_requested", {"job_id": job_id})
        active = self._active.get(job_id)
        if active is not None:
            signal, task = active
            if not signal.is_set():
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
