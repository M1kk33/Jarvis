"""Bounded, explicitly requested page snapshots and resumable analysis records."""

from dataclasses import dataclass

from jarvis.domain.types import RequestFailure

MAX_PAGE_TEXT_BYTES = 256 * 1024
MAX_PAGE_CHUNKS = 256
MAX_PAGE_NOTES_BYTES = 8192


class PageAnalysisPaused(RequestFailure):
    def __init__(self) -> None:
        super().__init__("page_paused", "Page analysis paused at a saved checkpoint")


class PageAnalysisFailure(RequestFailure):
    """A model stage failed; retain its snapshot and require explicit continuation."""


@dataclass(frozen=True)
class PageSnapshot:
    invocation_id: str
    text: str
    sha256: str
    metadata_json: str


@dataclass(frozen=True)
class PageAnalysis:
    id: str
    invocation_id: str
    turn_id: str
    signature: str
    plan_json: str
    status: str
    result: str | None


@dataclass(frozen=True)
class PageNote:
    stage: str
    ordinal: int
    text: str
    input_tokens: int
    output_tokens: int | None
