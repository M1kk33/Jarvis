"""Strict profile for the sole pinned M8 image worker."""

import hashlib
import subprocess
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jarvis.domain.resources import RuntimeProfile, canonical_gpu_uuid


class LocalImageProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: Literal["sd15-512-v1"]
    runtime_commit: Literal["b0b743566f65daafc423b4fea8a2fbda94b3384a"]
    packaged_source: bool = False
    root: Path
    python: Path
    checkpoint: Path
    checkpoint_sha256: Literal[
        "e9476a13728cd75d8279f6ec8bad753a66a1957ca375a1464dc63b37db6e3916"
    ]
    device_uuid: str
    device_index: int = Field(default=0, ge=0, le=15)
    cold_vram_gib: float = Field(gt=0, le=6)
    cold_ram_gib: float = Field(gt=0, le=8)
    warm_vram_headroom_gib: float = Field(ge=0, le=6)
    warm_ram_headroom_gib: float = Field(ge=0, le=8)
    calibrated: bool = False
    idle_seconds: float = Field(default=0, ge=0, le=3600)

    @field_validator("device_uuid")
    @classmethod
    def valid_uuid(cls, value: str) -> str:
        return canonical_gpu_uuid(value)

    @classmethod
    def from_file(cls, path: Path, data_dir: Path) -> "LocalImageProfile":
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        if (
            set(document) != {"image_model"}
            or type(document["image_model"]) is not dict
        ):
            raise ValueError("Image profile must contain only [image_model]")
        values = dict(document["image_model"])
        for field in ("root", "python", "checkpoint"):
            raw = values.get(field)
            if type(raw) is not str:
                raise ValueError(f"Image profile {field} must be a path")
            candidate = Path(raw)
            values[field] = (
                candidate if candidate.is_absolute() else data_dir / candidate
            ).resolve()
        return cls.model_validate(values)

    def verify_artifacts(self) -> None:
        if not self.python.is_file() or not (self.root / "main.py").is_file():
            raise FileNotFoundError("Pinned ComfyUI environment is missing")
        if self.packaged_source:
            from jarvis.infrastructure.component_pack import verify_comfy_source

            verify_comfy_source(self.root, self.runtime_commit)
        else:
            self._verify_checkout()
        if not self.checkpoint.is_file():
            raise FileNotFoundError("Pinned image checkpoint is missing")
        digest = hashlib.sha256()
        with self.checkpoint.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != self.checkpoint_sha256:
            raise ValueError("Image checkpoint hash mismatch")
        expected = self.root / "models" / "checkpoints" / self.checkpoint.name
        if expected.resolve() != self.checkpoint.resolve():
            raise ValueError(
                "Checkpoint must be in the pinned ComfyUI checkpoints directory"
            )

    def _verify_checkout(self) -> None:
        commit = subprocess.check_output(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            text=True,
            timeout=10,
        ).strip()
        if commit != self.runtime_commit:
            raise ValueError("ComfyUI commit does not match the pinned profile")
        changed = subprocess.check_output(
            [
                "git",
                "-C",
                str(self.root),
                "status",
                "--porcelain",
                "--untracked-files=no",
            ],
            text=True,
            timeout=10,
        )
        if changed.strip():
            raise ValueError("Pinned ComfyUI tracked files were modified")

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
