"""User-managed local material and source-labelled retrieval records."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievalRoot:
    id: str
    path: str
    conversation_id: str | None
    active: bool


@dataclass(frozen=True)
class Memory:
    id: str
    conversation_id: str | None
    text: str


@dataclass(frozen=True)
class Document:
    id: str
    root_id: str
    path: str
    sha256: str
    status: str


@dataclass(frozen=True)
class RetrievalHit:
    kind: str
    source_id: str
    location: str
    source_hash: str
    text: str
    root_path: str | None
    path: str | None
