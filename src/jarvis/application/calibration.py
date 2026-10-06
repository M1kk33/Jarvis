"""Optional M4 hardware qualification with metadata-only memory sampling."""

import asyncio
import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Event
from typing import Protocol

from jarvis.application.resource_manager import ResourceManager, _settle
from jarvis.domain.resources import ResourceSnapshot, RuntimeProfile
from jarvis.domain.types import (
    Message,
    StreamDelta,
    StreamEvent,
    StreamTerminal,
    TextPart,
    TurnStatus,
)


class CalibrationProvider(Protocol):
    def count_prompt_tokens(
        self, messages: list[Message], instructions: str
    ) -> int: ...
    def count_output_tokens(self, text: str) -> int: ...
    def stream(
        self, messages: list[Message], cancel: Event
    ) -> Iterator[StreamEvent]: ...
    async def check_unauthenticated_routes(self) -> None: ...


class CalibrationProbe(Protocol):
    def snapshot(self) -> ResourceSnapshot: ...
    def driver_version(self) -> str | None: ...


async def _thread_call[T](function: Callable[..., T], *args: object) -> T:
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await _settle(task)
        except Exception:  # noqa: BLE001, S110 - wait for the reader before unloading
            pass
        raise


def _message(text: str) -> Message:
    return Message(
        "calibration",
        "calibration",
        "calibration",
        1,
        "user",
        (TextPart(text),),
        "complete",
        TurnStatus.PENDING,
        "",
    )


def _minimum(values: list[float | None]) -> float | None:
    known = [value for value in values if value is not None]
    return min(known) if known else None


def _drop(start: float | None, minimum: float | None) -> float | None:
    return (
        round(max(0.0, start - minimum), 3)
        if start is not None and minimum is not None
        else None
    )


async def calibrate(
    profile: RuntimeProfile,
    manager: ResourceManager,
    provider: CalibrationProvider,
    probe: CalibrationProbe,
    data_dir: Path,
    artifacts: dict[str, str],
) -> dict[str, object]:
    """Load once, run a near-budget prompt, unload, and record observed peaks."""
    baseline = await asyncio.to_thread(probe.snapshot)
    driver_version = await asyncio.to_thread(probe.driver_version)
    samples: dict[str, list[ResourceSnapshot]] = {"load": [], "warm": [], "request": []}
    phase = "load"
    sampling = True

    async def sample() -> None:
        while sampling:
            snapshot = await asyncio.to_thread(probe.snapshot)
            if phase in samples:
                samples[phase].append(snapshot)
            await asyncio.sleep(0.25)

    sampler = asyncio.create_task(sample())
    started = time.perf_counter()
    try:
        handle = await manager.acquire(profile)
        load_seconds = round(time.perf_counter() - started, 3)
        phase = "warm"
        try:
            await asyncio.sleep(1)
            warm = await asyncio.to_thread(probe.snapshot)
            await provider.check_unauthenticated_routes()
            phase = "request"

            instruction = (
                "Return the integers from 1 through 120 in order, separated by spaces."
            )
            repetitions = 400
            text = (
                "This is a repeated calibration sentence for a long context request. "
                * repetitions
                + instruction
            )
            tokens = await _thread_call(
                provider.count_prompt_tokens, [_message(text)], ""
            )
            for _ in range(3):
                if 2400 <= tokens <= 3200:
                    break
                repetitions = max(
                    1, min(1100, round(repetitions * 2800 / max(tokens, 1)))
                )
                text = (
                    "This is a repeated calibration sentence for a long context request. "
                    * repetitions
                    + instruction
                )
                tokens = await _thread_call(
                    provider.count_prompt_tokens, [_message(text)], ""
                )
            if not 2000 <= tokens <= 3328:
                raise RuntimeError(
                    "Could not construct a long prompt within the context budget"
                )

            request_start = time.perf_counter()
            request_cancel = Event()

            def run_request() -> tuple[str, float]:
                first_token = 0.0
                parts: list[str] = []
                terminal = False
                for event in provider.stream([_message(text)], request_cancel):
                    if isinstance(event, StreamDelta):
                        if not parts:
                            first_token = time.perf_counter() - request_start
                        parts.append(event.text)
                    elif isinstance(event, StreamTerminal):
                        terminal = True
                if not terminal or not parts:
                    raise RuntimeError("Calibration stream did not complete")
                return "".join(parts), first_token

            request_task = asyncio.create_task(asyncio.to_thread(run_request))
            try:
                reply, first_token_seconds = await asyncio.shield(request_task)
            except asyncio.CancelledError:
                request_cancel.set()
                try:
                    await _settle(request_task)
                except Exception:  # noqa: BLE001, S110 - reader must finish before unload
                    pass
                raise
            request_seconds = time.perf_counter() - request_start
            output_tokens = await _thread_call(provider.count_output_tokens, reply)
            request_end = await asyncio.to_thread(probe.snapshot)
        finally:
            phase = "warm"
            await handle.release()
            await manager.unload()
        post_exit = await asyncio.to_thread(probe.snapshot)
    finally:
        sampling = False
        await sampler
        await manager.close()

    cold_vram = _drop(
        baseline.available_vram_gib,
        _minimum(
            [
                s.available_vram_gib
                for s in samples["load"] + samples["warm"] + samples["request"]
            ]
        ),
    )
    cold_ram = _drop(
        baseline.available_ram_gib,
        _minimum(
            [
                s.available_ram_gib
                for s in samples["load"] + samples["warm"] + samples["request"]
            ]
        ),
    )
    additional_vram = _drop(
        warm.available_vram_gib,
        _minimum(
            [s.available_vram_gib for s in samples["request"]]
            + [request_end.available_vram_gib]
        ),
    )
    additional_ram = _drop(
        warm.available_ram_gib,
        _minimum(
            [s.available_ram_gib for s in samples["request"]]
            + [request_end.available_ram_gib]
        ),
    )
    lowest_vram = _minimum(
        [s.available_vram_gib for group in samples.values() for s in group]
    )
    lowest_ram = _minimum(
        [s.available_ram_gib for group in samples.values() for s in group]
    )
    passed = (
        cold_vram is not None
        and cold_vram <= profile.cold_vram_gib
        and cold_ram is not None
        and cold_ram <= profile.cold_ram_gib
        and additional_vram is not None
        and additional_vram <= profile.warm_vram_headroom_gib
        and additional_ram is not None
        and additional_ram <= profile.warm_ram_headroom_gib
        and lowest_vram is not None
        and lowest_vram >= 1
        and lowest_ram is not None
        and lowest_ram >= 3
    )
    report: dict[str, object] = {
        "profile": profile.name,
        **artifacts,
        "driver_version": driver_version,
        "sample_interval_seconds": 0.25,
        "sample_counts": {name: len(group) for name, group in samples.items()},
        "baseline_free_gib": {
            "vram": baseline.available_vram_gib,
            "ram": baseline.available_ram_gib,
        },
        "warm_free_gib": {
            "vram": warm.available_vram_gib,
            "ram": warm.available_ram_gib,
        },
        "minimum_sampled_free_gib": {"vram": lowest_vram, "ram": lowest_ram},
        "observed_cold_peak_gib": {"vram": cold_vram, "ram": cold_ram},
        "observed_request_additional_gib": {
            "vram": additional_vram,
            "ram": additional_ram,
        },
        "post_exit_free_gib": {
            "vram": post_exit.available_vram_gib,
            "ram": post_exit.available_ram_gib,
        },
        "load_seconds": load_seconds,
        "first_token_seconds": round(first_token_seconds, 3),
        "request_seconds": round(request_seconds, 3),
        "input_tokens": tokens,
        "output_tokens": output_tokens,
        "output_tokens_per_second": round(
            output_tokens / max(request_seconds - first_token_seconds, 0.001), 2
        ),
        "unauthenticated_routes_rejected": True,
        "passed_profile_bounds": passed,
        "measurement_note": "Free-memory sampling is system-wide and may miss peaks between samples; no other heavy GPU workload should run during calibration.",
    }
    target = data_dir / "calibration.json"
    temporary = data_dir / "calibration.json.tmp"
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return report
