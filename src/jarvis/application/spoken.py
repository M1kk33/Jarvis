"""Explicit, bounded playback of committed assistant replies only."""

import asyncio
import math
import time
from dataclasses import dataclass
from uuid import uuid4

from jarvis.application.resource_manager import CpuWorkerGate, ResourceManager, _settle
from jarvis.domain.jobs import JobStatus
from jarvis.domain.speech import (
    RamProbe,
    SpeechInvalid,
    SpeechUnavailable,
    SpokenStore,
    Synthesizer,
    WorkerCleanupUnverified,
)
from jarvis.domain.types import Message, StaleTurnError, TurnStatus

MAX_TEXT_CHARS = 4096
CHUNK_CHARS = 256
MAX_CHUNKS = 24
MAX_AUDIO_BYTES = 1_000_000
RAM_PEAK_GIB = 0.8
RAM_RESERVE_GIB = 3.0
SESSION_IDLE_SECONDS = 120


def split_speech(text: str) -> tuple[str, ...]:
    remaining = text.strip()
    if not remaining or len(remaining) > MAX_TEXT_CHARS:
        raise SpeechInvalid("Reply is empty or too long to speak")
    chunks: list[str] = []
    while remaining:
        if len(remaining) <= CHUNK_CHARS:
            chunks.append(remaining)
            break
        cut = remaining.rfind(" ", 0, CHUNK_CHARS + 1)
        if cut < 192:
            cut = CHUNK_CHARS
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
        if len(chunks) >= MAX_CHUNKS:
            raise SpeechInvalid("Reply needs too many speech chunks")
    return tuple(chunks)


@dataclass
class _Session:
    id: str
    conversation_id: str
    turn_id: str
    answer_id: str
    chunks: tuple[str, ...]
    next_index: int
    expires_at: float


class SpokenPlaybackService:
    def __init__(
        self,
        store: SpokenStore,
        synthesizer: Synthesizer,
        ram: RamProbe,
        manager: ResourceManager | None = None,
        cpu_gate: asyncio.Lock | None = None,
    ) -> None:
        self.store = store
        self.synthesizer = synthesizer
        self.ram = ram
        self.manager = manager
        self.cpu_gate = cpu_gate or CpuWorkerGate()
        self._lock = asyncio.Lock()
        self._generation = 0
        self._session: _Session | None = None
        self._task: asyncio.Task | None = None

    async def _reply(
        self, conversation_id: str, turn_id: str, answer_id: str
    ) -> Message:
        def snapshot():
            turn = self.store.get_turn(turn_id)
            answer = self.store.get_answer(turn_id)
            job = self.store.get_job_by_turn(turn_id)
            return turn, answer, job

        turn, answer, job = await self.store.run(snapshot)
        if (
            turn.conversation_id != conversation_id
            or answer is None
            or answer.id != answer_id
        ):
            raise StaleTurnError("Assistant reply does not match this conversation")
        if (
            turn.status is not TurnStatus.COMPLETE
            or answer.role != "assistant"
            or answer.turn_status is not TurnStatus.COMPLETE
            or (job is not None and job.status is not JobStatus.SUCCEEDED)
        ):
            raise SpeechInvalid("Only a completed assistant reply can be spoken")
        return answer

    async def start(
        self, conversation_id: str, turn_id: str, answer_id: str
    ) -> tuple[str, int]:
        await self.stop()
        generation = self._generation
        answer = await self._reply(conversation_id, turn_id, answer_id)
        chunks = split_speech(answer.text)
        async with self._lock:
            if self._generation != generation:
                raise SpeechUnavailable("Playback request became stale")
            session = _Session(
                uuid4().hex,
                conversation_id,
                turn_id,
                answer_id,
                chunks,
                0,
                time.monotonic() + SESSION_IDLE_SECONDS,
            )
            self._session = session
            return session.id, len(chunks)

    async def chunk(self, session_id: str, index: int) -> bytes:
        async with self._lock:
            session = self._session
            if session is None or session.id != session_id:
                raise SpeechUnavailable("Playback session is stale or cancelled")
            if time.monotonic() > session.expires_at:
                self._session = None
                self._generation += 1
                raise SpeechUnavailable("Playback session expired")
            if (
                index != session.next_index
                or index >= len(session.chunks)
                or self._task
            ):
                raise SpeechInvalid("Speech chunks must be requested once in order")
            self._task = asyncio.current_task()
        try:
            await self._reply(
                session.conversation_id, session.turn_id, session.answer_id
            )
            async with self.cpu_gate:
                if getattr(self.cpu_gate, "unavailable", False):
                    raise SpeechUnavailable("Previous CPU worker cleanup is unverified")
                if self.manager is None:
                    audio = await self._admitted(session.chunks[index])
                else:
                    async with self.manager.cpu_worker():
                        audio = await self._admitted(session.chunks[index])
            async with self._lock:
                if (
                    self._session is not session
                    or self._task is not asyncio.current_task()
                ):
                    raise SpeechUnavailable("Playback was cancelled")
                session.next_index += 1
                session.expires_at = time.monotonic() + SESSION_IDLE_SECONDS
            return audio
        finally:
            await _settle(asyncio.create_task(self._clear_task(asyncio.current_task())))

    async def _clear_task(self, owner: asyncio.Task | None) -> None:
        async with self._lock:
            if self._task is owner:
                self._task = None

    async def _admitted(self, text: str) -> bytes:
        available = self.ram.available_ram_gib()
        if (
            available is None
            or not math.isfinite(available)
            or available < RAM_PEAK_GIB + RAM_RESERVE_GIB
        ):
            raise SpeechUnavailable("Not enough verified available RAM for speech")
        try:
            audio = await self.synthesizer.synthesize(text)
        except WorkerCleanupUnverified:
            self.cpu_gate.unavailable = True  # type: ignore[attr-defined]
            if self.manager is not None:
                await _settle(asyncio.create_task(self.manager.mark_cpu_unavailable()))
            raise
        if not 44 <= len(audio) <= MAX_AUDIO_BYTES or not audio.startswith(b"RIFF"):
            raise SpeechUnavailable("Piper returned invalid or oversized audio")
        return audio

    async def stop(self, session_id: str | None = None) -> bool:
        async with self._lock:
            session = self._session
            if session_id is not None and (session is None or session.id != session_id):
                return False
            self._session = None
            self._generation += 1
            task = self._task
            self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await _settle(task)
            except asyncio.CancelledError:
                pass
        return session is not None

    async def current(self, session_id: str) -> bool:
        async with self._lock:
            return self._session is not None and self._session.id == session_id
