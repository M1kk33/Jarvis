"""Approved exact-URL retrieval followed by one offline browser rendering."""

import asyncio
import hashlib
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from jarvis.application.resource_manager import ResourceManager, _settle
from jarvis.application.vision import validate_screenshot
from jarvis.domain.ports import BrowserRenderer, BrowserStore, PageDownloader
from jarvis.domain.speech import RamProbe
from jarvis.domain.tools import ToolInvocation
from jarvis.domain.types import BrowserCleanupUnverified, RequestFailure

BROWSER_RAM_PEAK_GIB = 1.5
RAM_RESERVE_GIB = 3.0
MAX_RESULT_TEXT_BYTES = 9 * 1024


class BrowserService:
    def __init__(
        self,
        store: BrowserStore,
        downloader: PageDownloader,
        renderer: BrowserRenderer,
        ram: RamProbe,
        manager: ResourceManager,
    ) -> None:
        self.store = store
        self.downloader = downloader
        self.renderer = renderer
        self.ram = ram
        self.manager = manager

    def inspect(self, arguments: dict[str, object]) -> str:
        return (
            self.renderer.identity
            + ":"
            + self.downloader.inspect("fetch_public_page", arguments)
        )

    async def run(
        self, invocation: ToolInvocation, arguments: dict[str, object]
    ) -> ToolInvocation:
        if invocation.job_id is None:
            raise RequestFailure("invalid_request", "Browser requires a durable job")
        url = arguments.get("url")
        if type(url) is not str:
            raise RequestFailure("invalid_request", "Browser URL is invalid")
        async with self.manager.cpu_worker():
            available = self.ram.available_ram_gib()
            if (
                available is None
                or not math.isfinite(available)
                or available < BROWSER_RAM_PEAK_GIB + RAM_RESERVE_GIB
            ):
                raise RequestFailure("out_of_memory", "Not enough RAM for browser")
            fetch_task = asyncio.create_task(
                asyncio.to_thread(self.downloader.download, url)
            )
            try:
                html, content_type, status = await asyncio.shield(fetch_task)
            except asyncio.CancelledError:
                await _settle(fetch_task)
                raise
            if content_type != "text/html" or status != 200:
                raise RequestFailure("invalid_request", "Browser needs an HTML page")
            try:
                text, truncated, screenshot = await self.renderer.render(html)
            except BrowserCleanupUnverified:
                await _settle(asyncio.create_task(self.manager.mark_cpu_unavailable()))
                raise
            validate_screenshot(screenshot)
            text = text.encode("utf-8", errors="replace").decode("utf-8")
            encoded = text.encode("utf-8")
            if len(encoded) > MAX_RESULT_TEXT_BYTES:
                text = encoded[:MAX_RESULT_TEXT_BYTES].decode("utf-8", errors="ignore")
                truncated = True
            artifact_id = str(uuid4())
            relative = f"artifacts/{invocation.job_id}/{artifact_id}.png"
            staging = self.store.data_dir / "staging" / invocation.job_id
            final = self.store.data_dir / relative.replace("/", os.sep)
            await asyncio.to_thread(staging.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(final.parent.mkdir, parents=True, exist_ok=True)
            digest = hashlib.sha256(screenshot).hexdigest()
            await asyncio.to_thread(
                self._stage, staging / f"{artifact_id}.png.part", final, screenshot
            )
            retrieved_at = datetime.now(UTC).isoformat(timespec="seconds")
            while True:
                result_json = json.dumps(
                    {
                        "status": "ok",
                        "data": {
                            "url": url,
                            "text": text,
                            "truncated": truncated,
                            "artifact_id": artifact_id,
                            "retrieved_at": retrieved_at,
                            "trust": "untrusted",
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if len(result_json.encode("utf-8")) <= 16384:
                    break
                text = text[: len(text) // 2]
                truncated = True
            receipt_json = json.dumps(
                {"status": "complete", "artifact_id": artifact_id, "sha256": digest},
                sort_keys=True,
                separators=(",", ":"),
            )
            try:
                return await self.store.run(
                    lambda: self.store.commit_browser_result(
                        invocation.id,
                        artifact_id,
                        relative,
                        len(screenshot),
                        digest,
                        receipt_json,
                        result_json,
                    )
                )
            except asyncio.CancelledError:
                await self.store.run(lambda: self.store.get_invocation(invocation.id))
                raise

    @staticmethod
    def _stage(partial: Path, final: Path, data: bytes) -> None:
        with partial.open("xb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if (
            partial.stat().st_size != len(data)
            or hashlib.sha256(partial.read_bytes()).digest()
            != hashlib.sha256(data).digest()
        ):
            raise OSError("Browser screenshot staging failed")
        os.replace(partial, final)
