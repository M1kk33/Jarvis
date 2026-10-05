"""Backend-neutral ownership and admission records for M3."""

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from re import fullmatch


class ResourceState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    STOPPING = "stopping"
    FAULTED = "faulted"
    UNAVAILABLE = "unavailable"


class ResourceError(Exception):
    """Expected runtime ownership failure."""


class AdmissionDenied(ResourceError):
    pass


class ResourceUnavailable(ResourceError):
    pass


class RuntimeStartFailure(ResourceError):
    pass


@dataclass(frozen=True)
class RuntimeProfile:
    name: str
    cold_vram_gib: float
    cold_ram_gib: float
    warm_vram_headroom_gib: float
    warm_ram_headroom_gib: float
    calibrated: bool = False
    idle_seconds: float = 120.0

    def __post_init__(self) -> None:
        if fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", self.name) is None or any(
            not isfinite(value) or value < 0
            for value in (
                self.cold_vram_gib,
                self.cold_ram_gib,
                self.warm_vram_headroom_gib,
                self.warm_ram_headroom_gib,
                self.idle_seconds,
            )
        ):
            raise ValueError("Profile name and resource bounds must be valid")
        if self.cold_vram_gib > 6 or self.cold_ram_gib > 8:
            raise ValueError(
                "Profile exceeds the initial 6 GiB VRAM / 8 GiB RAM bounds"
            )


@dataclass(frozen=True)
class ResourceSnapshot:
    available_vram_gib: float | None
    available_ram_gib: float | None


@dataclass(frozen=True)
class RuntimeIdentity:
    generation: int
    profile_name: str
    pid: int
    creation_time: int


@dataclass(frozen=True)
class Admission:
    degraded_gpu_telemetry: bool


def admit(
    profile: RuntimeProfile, snapshot: ResourceSnapshot, *, warm: bool
) -> Admission:
    """Current free memory already excludes a warm resident worker."""
    vram = profile.warm_vram_headroom_gib if warm else profile.cold_vram_gib
    ram = profile.warm_ram_headroom_gib if warm else profile.cold_ram_gib
    if snapshot.available_ram_gib is not None and not isfinite(
        snapshot.available_ram_gib
    ):
        raise AdmissionDenied("Available RAM telemetry is invalid")
    if snapshot.available_vram_gib is not None and not isfinite(
        snapshot.available_vram_gib
    ):
        raise AdmissionDenied("Available VRAM telemetry is invalid")
    if snapshot.available_ram_gib is None or snapshot.available_ram_gib - ram < 3:
        raise AdmissionDenied("Available RAM cannot preserve the 3 GiB reserve")
    if snapshot.available_vram_gib is None:
        if not profile.calibrated:
            raise AdmissionDenied(
                "GPU telemetry is unavailable for an uncalibrated profile"
            )
        return Admission(degraded_gpu_telemetry=True)
    if snapshot.available_vram_gib - vram < 1:
        raise AdmissionDenied("Available VRAM cannot preserve the 1 GiB reserve")
    return Admission(degraded_gpu_telemetry=False)
