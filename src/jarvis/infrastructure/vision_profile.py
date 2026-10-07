"""Pinned M12 vision GGUF and matching projector profile."""

import hashlib
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jarvis.domain.resources import RuntimeProfile


class LocalVisionProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: Literal["qwen25-vl-3b-q4_k_m"]
    runtime_version: Literal["b11429"]
    model_revision: Literal["5037fcf163dd95d1e41d1974465f0898ed108ca2"]
    executable: Path
    executable_sha256: str
    model: Path
    model_sha256: str
    projector: Path
    projector_sha256: str
    device_uuid: str
    device_index: int = Field(ge=0, le=15)
    cold_vram_gib: float = Field(gt=0, le=6)
    cold_ram_gib: float = Field(gt=0, le=8)
    warm_vram_headroom_gib: float = Field(ge=0, le=6)
    warm_ram_headroom_gib: float = Field(ge=0, le=8)
    calibrated: bool = False
    image_max_tokens: int = Field(ge=256, le=1024)

    @field_validator("executable_sha256", "model_sha256", "projector_sha256")
    @classmethod
    def valid_digest(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("Expected a lowercase SHA-256 digest")
        return value

    @field_validator("device_uuid")
    @classmethod
    def valid_device_uuid(cls, value: str) -> str:
        if not value.startswith("GPU-") or len(value) > 80:
            raise ValueError("Expected an NVIDIA GPU UUID")
        return value

    @classmethod
    def from_file(cls, path: Path, data_dir: Path) -> "LocalVisionProfile":
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        if (
            set(document) != {"vision_model"}
            or type(document["vision_model"]) is not dict
        ):
            raise ValueError("Vision profile must contain only [vision_model]")
        values = dict(document["vision_model"])
        for key in ("executable", "model", "projector"):
            raw = values.get(key)
            if type(raw) is not str:
                raise ValueError(f"Vision {key} must be a path")
            candidate = Path(raw)
            values[key] = (
                candidate if candidate.is_absolute() else data_dir / candidate
            ).resolve()
        return cls.model_validate(values)

    def verify_artifacts(self) -> None:
        if (
            self.model.name != "Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf"
            or self.projector.name != "mmproj-Qwen2.5-VL-3B-Instruct-Q8_0.gguf"
        ):
            raise ValueError("Vision model and projector must be the pinned pair")
        for path, expected in (
            (self.executable, self.executable_sha256),
            (self.model, self.model_sha256),
            (self.projector, self.projector_sha256),
        ):
            if not path.is_file() or path.is_symlink():
                raise FileNotFoundError(f"Pinned vision artifact is missing: {path}")
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                raise ValueError(f"Pinned vision artifact hash mismatch: {path}")

    def admission_profile(self) -> RuntimeProfile:
        return RuntimeProfile(
            self.name,
            self.cold_vram_gib,
            self.cold_ram_gib,
            self.warm_vram_headroom_gib,
            self.warm_ram_headroom_gib,
            calibrated=self.calibrated,
            idle_seconds=0,
        )
