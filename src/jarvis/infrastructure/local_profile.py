"""Strict, pinned configuration for the single M4 local text model."""

import hashlib
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jarvis.domain.resources import RuntimeProfile


class LocalTextProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: Literal["qwen3-4b-instruct-2507-q4_k_m"]
    runtime_version: Literal["b11429"]
    executable: Path
    executable_sha256: str
    model: Path
    model_sha256: str
    device_uuid: str
    device_index: int = Field(default=0, ge=0, le=15)
    cold_vram_gib: float = Field(gt=0, le=6)
    cold_ram_gib: float = Field(gt=0, le=8)
    warm_vram_headroom_gib: float = Field(ge=0, le=6)
    warm_ram_headroom_gib: float = Field(ge=0, le=8)
    calibrated: bool = False
    idle_seconds: float = Field(default=120, ge=0, le=3600)

    @field_validator("executable_sha256", "model_sha256")
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
    def from_file(cls, path: Path, data_dir: Path) -> "LocalTextProfile":
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
        if set(payload) != {"text_model"} or not isinstance(
            payload["text_model"], dict
        ):
            raise ValueError("Profile must contain only [text_model]")
        values = dict(payload["text_model"])
        for name in ("executable", "model"):
            if name not in values or not isinstance(values[name], str):
                raise ValueError(f"Profile {name} must be a path")
            candidate = Path(values[name])
            values[name] = (
                candidate if candidate.is_absolute() else data_dir / candidate
            ).resolve()
        return cls.model_validate(values)

    def verify_artifacts(self) -> None:
        for path, expected in (
            (self.executable, self.executable_sha256),
            (self.model, self.model_sha256),
        ):
            if not path.is_file():
                raise FileNotFoundError(f"Pinned artifact is missing: {path}")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                raise ValueError(f"Pinned artifact hash mismatch: {path}")

    def admission_profile(self) -> RuntimeProfile:
        return RuntimeProfile(
            self.name,
            self.cold_vram_gib,
            self.cold_ram_gib,
            self.warm_vram_headroom_gib,
            self.warm_ram_headroom_gib,
            calibrated=self.calibrated,
            idle_seconds=self.idle_seconds,
        )
