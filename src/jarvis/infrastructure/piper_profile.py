"""One pinned, locally licensed Piper voice and separate CPU environment."""

import hashlib
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PiperProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: Literal["piper-norman-medium-cpu"]
    version: Literal["1.8.0"]
    voice_revision: Literal["c10ece1aade47bb51c153c893d14e5bf8e5b7117"]
    engine_license: Literal["GPL-3.0-or-later"]
    voice_license: Literal["MIT"]
    voice_data_license: Literal["public-domain"]
    python: Path
    voice_model: Path
    model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_card_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ram_peak_gib: float = Field(ge=0.8, le=0.8)
    cpu_threads: Literal[4]

    @classmethod
    def from_file(cls, path: Path, data_dir: Path) -> "PiperProfile":
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        if set(document) != {"piper"} or type(document["piper"]) is not dict:
            raise ValueError("Piper profile must contain only [piper]")
        values = dict(document["piper"])
        for key in ("python", "voice_model"):
            raw = values.get(key)
            if type(raw) is not str:
                raise ValueError(f"Piper {key} must be a path")
            candidate = Path(raw)
            values[key] = (
                candidate if candidate.is_absolute() else data_dir / candidate
            ).resolve()
            if values[key].anchor.startswith("\\\\"):
                raise ValueError("Piper paths must be on a local drive")
        return cls.model_validate(values)

    def verify_artifacts(self) -> None:
        if not self.python.is_file() or not self.voice_model.is_file():
            raise FileNotFoundError("Pinned Piper environment or voice is missing")
        if self.voice_model.name != "en_US-norman-medium.onnx":
            raise ValueError("Only the pinned Norman voice is permitted")
        for path, expected in (
            (self.voice_model, self.model_sha256),
            (Path(str(self.voice_model) + ".json"), self.config_sha256),
            (self.voice_model.with_name("MODEL_CARD"), self.model_card_sha256),
        ):
            if not path.is_file() or path.is_symlink():
                raise FileNotFoundError(f"Pinned Piper {path.name} is missing")
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                raise ValueError(f"Piper {path.name} hash mismatch")
