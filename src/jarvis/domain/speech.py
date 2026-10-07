"""Transient speech transcription contract; audio never becomes a turn here."""

from typing import Protocol


class SpeechError(Exception):
    pass


class SpeechUnavailable(SpeechError):
    pass


class SpeechInvalid(SpeechError):
    pass


class Transcriber(Protocol):
    async def transcribe(self, clip: bytes, media_type: str) -> str: ...


class RamProbe(Protocol):
    def available_ram_gib(self) -> float | None: ...
