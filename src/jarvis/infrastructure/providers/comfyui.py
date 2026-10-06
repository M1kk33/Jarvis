"""Pinned, authenticated ComfyUI worker and fixed SD 1.5 protocol."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import socket
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from threading import Event
from uuid import uuid4

import httpx

from jarvis.domain.resources import RuntimeIdentity, RuntimeProfile, RuntimeStartFailure
from jarvis.domain.types import ProviderFailure
from jarvis.infrastructure.image_profile import LocalImageProfile
from jarvis.infrastructure.windows_process import (
    OwnedProcess,
    WindowsProcessSupervisor,
    named_job_tree_exited,
)


@dataclass(frozen=True)
class ComfyEndpoint:
    base_url: str
    api_key: str
    output_root: Path
    identity: RuntimeIdentity


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def fixed_workflow(
    prompt: str, seed: int, checkpoint: str, prefix: str
) -> dict[str, object]:
    if (
        not isinstance(prompt, str)
        or not 1 <= len(prompt) <= 1000
        or not prompt.strip()
    ):
        raise ValueError("Image prompt is invalid")
    if type(seed) is not int or not 0 <= seed <= 2147483647:
        raise ValueError("Image seed is invalid")
    if not re.fullmatch(r"v1-5-pruned-emaonly-fp16\.safetensors", checkpoint):
        raise ValueError("Image checkpoint is not approved")
    if not re.fullmatch(r"jarvis_[0-9a-f]{32}", prefix):
        raise ValueError("Image output prefix is invalid")
    template = resources.files("jarvis.infrastructure").joinpath(
        "workflows/sd15_512_api.json"
    )
    graph = json.loads(template.read_text(encoding="utf-8"))
    graph["1"]["inputs"]["ckpt_name"] = checkpoint
    graph["2"]["inputs"]["text"] = prompt
    graph["5"]["inputs"]["seed"] = seed
    graph["7"]["inputs"]["filename_prefix"] = prefix
    return graph


class ComfyRuntime:
    def __init__(self, profile: LocalImageProfile, run_root: Path) -> None:
        self.profile = profile
        self.run_root = run_root.resolve()
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.supervisor = WindowsProcessSupervisor()
        self.processes: dict[int, OwnedProcess] = {}
        self._endpoints: dict[int, ComfyEndpoint] = {}
        self._job_names: dict[int, str] = {}

    @property
    def endpoint(self) -> ComfyEndpoint:
        if len(self._endpoints) != 1:
            raise ProviderFailure("unavailable", "Image worker is not ready")
        return next(iter(self._endpoints.values()))

    async def start(
        self, profile: RuntimeProfile, generation: int, cancel: Event, job_name: str
    ) -> RuntimeIdentity:
        if profile.name != self.profile.name:
            raise RuntimeStartFailure("Wrong pinned image profile")
        await asyncio.to_thread(self.profile.verify_artifacts)
        if cancel.is_set():
            raise RuntimeStartFailure("Image worker load was cancelled")
        cwd = self.run_root / f"comfy-{generation}-{uuid4().hex}"
        cwd.mkdir()
        output = cwd / "output"
        for name in ("output", "input", "temp", "user"):
            (cwd / name).mkdir()
        port = _port()
        key = secrets.token_urlsafe(32)
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        environment = {
            "SystemRoot": system_root,
            "WINDIR": system_root,
            "PATH": str(self.profile.python.parent)
            + os.pathsep
            + str(Path(system_root) / "System32"),
            "TEMP": str(cwd / "temp"),
            "TMP": str(cwd / "temp"),
            "JARVIS_COMFY_SECRET": key,
            "CUDA_VISIBLE_DEVICES": str(self.profile.device_index),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "DO_NOT_TRACK": "1",
        }
        launcher = Path(__file__).resolve().parents[1] / "image_launcher.py"
        arguments = [
            str(launcher),
            str(self.profile.root),
            "--listen",
            "127.0.0.1",
            "--port",
            str(port),
            "--disable-auto-launch",
            "--disable-all-custom-nodes",
            "--offline",
            "--disable-partner-nodes",
            "--disable-metadata",
            "--cache-none",
            "--cuda-device",
            str(self.profile.device_index),
            "--output-directory",
            str(output),
            "--input-directory",
            str(cwd / "input"),
            "--temp-directory",
            str(cwd / "temp"),
            "--user-directory",
            str(cwd / "user"),
            "--max-upload-size",
            "1",
        ]
        self._job_names[generation] = job_name
        process = await asyncio.to_thread(
            self.supervisor.spawn,
            self.profile.python,
            arguments,
            cwd,
            environment,
            job_name=job_name,
        )
        self.processes[generation] = process
        identity = RuntimeIdentity(
            generation, self.profile.name, process.pid, process.creation_time
        )
        endpoint = ComfyEndpoint(f"http://127.0.0.1:{port}", key, output, identity)
        await self._wait_ready(process, endpoint, cancel)
        self._endpoints[generation] = endpoint
        return identity

    async def _wait_ready(
        self, process: OwnedProcess, endpoint: ComfyEndpoint, cancel: Event
    ) -> None:
        deadline = time.monotonic() + 180
        async with httpx.AsyncClient(
            base_url=endpoint.base_url, trust_env=False, timeout=3
        ) as client:
            while time.monotonic() < deadline:
                if cancel.is_set() or process.root_exited():
                    raise RuntimeStartFailure(
                        "Image worker exited or was cancelled during load"
                    )
                try:
                    response = await client.get(
                        "/system_stats",
                        headers={"Authorization": f"Bearer {endpoint.api_key}"},
                    )
                    if response.status_code == 200:
                        return
                    if response.status_code == 403:
                        raise RuntimeStartFailure("Image worker authentication failed")
                except (httpx.ConnectError, httpx.TimeoutException):
                    pass
                await asyncio.sleep(0.3)
        raise RuntimeStartFailure("Image worker readiness exceeded 180 seconds")

    async def stop(self, identity: RuntimeIdentity) -> bool:
        process = self.processes.get(identity.generation)
        if (
            process is None
            or process.pid != identity.pid
            or process.creation_time != identity.creation_time
        ):
            return False
        stopped = await asyncio.to_thread(process.stop, 0, 15)
        if stopped:
            self.processes.pop(identity.generation, None)
            self._endpoints.pop(identity.generation, None)
            self._job_names.pop(identity.generation, None)
        return stopped

    async def abort_start(self, generation: int) -> bool:
        process = self.processes.get(generation)
        if process is None:
            name = self._job_names.get(generation)
            return name is None or await asyncio.to_thread(
                named_job_tree_exited, name, 10
            )
        stopped = await asyncio.to_thread(process.stop, 0, 15)
        if stopped:
            self.processes.pop(generation, None)
            self._endpoints.pop(generation, None)
            self._job_names.pop(generation, None)
        return stopped

    async def wait_exit(self, identity: RuntimeIdentity) -> None:
        process = self.processes.get(identity.generation)
        if process is not None:
            await asyncio.to_thread(process.wait_root_exit)

    async def verify_stopped(self, identity: RuntimeIdentity) -> bool:
        process = self.processes.get(identity.generation)
        return process is None or await asyncio.to_thread(process.verify_stopped)


class ComfyImageProvider:
    def __init__(
        self, runtime: ComfyRuntime, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.runtime = runtime
        self.transport = transport

    @property
    def output_root(self) -> Path:
        return self.runtime.endpoint.output_root

    async def generate(
        self, prompt: str, seed: int, invocation_id: str, cancel: Event
    ) -> Path:
        endpoint = self.runtime.endpoint
        prefix = f"jarvis_{uuid4().hex}"
        graph = fixed_workflow(
            prompt, seed, self.runtime.profile.checkpoint.name, prefix
        )
        headers = {"Authorization": f"Bearer {endpoint.api_key}"}
        async with httpx.AsyncClient(
            base_url=endpoint.base_url,
            trust_env=False,
            timeout=15,
            transport=self.transport,
        ) as client:
            response = await client.post(
                "/prompt", headers=headers, json={"prompt": graph}
            )
            if response.status_code != 200:
                raise ProviderFailure("backend_crashed", "Image workflow was rejected")
            payload = response.json()
            prompt_id = payload.get("prompt_id")
            if not isinstance(prompt_id, str) or not re.fullmatch(
                r"[0-9a-f-]{36}", prompt_id
            ):
                raise ProviderFailure(
                    "backend_crashed", "Image worker returned an invalid prompt ID"
                )
            deadline = time.monotonic() + 480
            while time.monotonic() < deadline:
                if cancel.is_set():
                    raise ProviderFailure("cancelled", "Image request was cancelled")
                history = await client.get(f"/history/{prompt_id}", headers=headers)
                if history.status_code != 200:
                    raise ProviderFailure(
                        "backend_crashed", "Image history is unavailable"
                    )
                record = history.json().get(prompt_id)
                if record is not None:
                    if record.get("status", {}).get("status_str") == "error":
                        raise ProviderFailure(
                            "backend_crashed", "Image workflow failed"
                        )
                    images = record.get("outputs", {}).get("7", {}).get("images", [])
                    if type(images) is not list or len(images) != 1:
                        raise ProviderFailure(
                            "backend_crashed",
                            "Image worker produced the wrong output count",
                        )
                    image = images[0]
                    if (
                        type(image) is not dict
                        or image.get("type") != "output"
                        or image.get("subfolder") not in ("", None)
                    ):
                        raise ProviderFailure(
                            "backend_crashed", "Image output location is invalid"
                        )
                    filename = image.get("filename")
                    if (
                        type(filename) is not str
                        or re.fullmatch(rf"{prefix}_[0-9]{{5}}_\.png", filename) is None
                    ):
                        raise ProviderFailure(
                            "backend_crashed", "Image output filename is invalid"
                        )
                    return endpoint.output_root / filename
                await asyncio.sleep(0.5)
        raise ProviderFailure("timeout", "Image generation exceeded its deadline")
