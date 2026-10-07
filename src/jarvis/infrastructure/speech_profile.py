"""Pinned, separate CPU speech environment and model files."""

import hashlib
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SpeechProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: Literal["faster-whisper-base-cpu-int8"]
    version: Literal["1.2.1"]
    python: Path
    model_dir: Path
    model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tokenizer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    vocabulary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ram_peak_gib: float = Field(ge=1.5, le=1.5)
    cpu_threads: Literal[4]

    @classmethod
    def from_file(cls, path: Path, data_dir: Path) -> "SpeechProfile":
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        if set(document) != {"speech"} or type(document["speech"]) is not dict:
            raise ValueError("Speech profile must contain only [speech]")
        values = dict(document["speech"])
        for key in ("python", "model_dir"):
            raw = values.get(key)
            if type(raw) is not str:
                raise ValueError(f"Speech {key} must be a path")
            candidate = Path(raw)
            values[key] = (
                candidate if candidate.is_absolute() else data_dir / candidate
            ).resolve()
            if values[key].anchor.startswith("\\\\"):
                raise ValueError("Speech paths must be on a local drive")
        return cls.model_validate(values)

    def verify_artifacts(self) -> None:
        if not self.python.is_file() or not self.model_dir.is_dir():
            raise FileNotFoundError("Pinned speech environment or model is missing")
        for filename, expected in (
            ("model.bin", self.model_sha256),
            ("tokenizer.json", self.tokenizer_sha256),
            ("config.json", self.config_sha256),
            ("vocabulary.txt", self.vocabulary_sha256),
        ):
            path = self.model_dir / filename
            if not path.is_file() or path.is_symlink():
                raise FileNotFoundError(f"Pinned speech {filename} is missing")
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                raise ValueError(f"Speech {filename} hash mismatch")
