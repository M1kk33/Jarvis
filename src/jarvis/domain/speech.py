"""Transient speech contracts; audio never becomes a durable turn here."""

from collections.abc import Callable
from typing import Protocol, TypeVar

from jarvis.domain.jobs import Job
from jarvis.domain.types import Message, Turn

T = TypeVar("T")


class SpeechError(Exception):
    pass


class SpeechUnavailable(SpeechError):
    pass


class WorkerCleanupUnverified(SpeechUnavailable):
    """CPU worker ownership must remain unavailable until process exit is verified."""


class SpeechInvalid(SpeechError):
    pass


class Transcriber(Protocol):
    async def transcribe(self, clip: bytes, media_type: str) -> str: ...


class RamProbe(Protocol):
    def available_ram_gib(self) -> float | None: ...


class Synthesizer(Protocol):
    async def synthesize(self, text: str) -> bytes: ...


class SpokenStore(Protocol):
    async def run(self, operation: Callable[[], T]) -> T: ...
    def get_turn(self, turn_id: str) -> Turn: ...
    def get_answer(self, turn_id: str) -> Message | None: ...
    def get_job_by_turn(self, turn_id: str) -> Job | None: ...
