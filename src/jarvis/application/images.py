"""One fixed image operation and its durable artifact boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import struct
import zlib
from pathlib import Path
from threading import Event
from uuid import uuid4

from jarvis.application.resource_manager import ResourceManager, _settle
from jarvis.domain.ports import ImageProvider, ImageStore
from jarvis.domain.resources import RuntimeProfile
from jarvis.domain.tools import ToolInvocation
from jarvis.domain.types import RequestFailure

MAX_IMAGE_BYTES = 16 * 1024 * 1024


def validate_png(data: bytes) -> None:
    if not 45 <= len(data) <= MAX_IMAGE_BYTES or not data.startswith(
        b"\x89PNG\r\n\x1a\n"
    ):
        raise RequestFailure("invalid_request", "Image output is not a bounded PNG")
    offset = 8
    seen_ihdr = seen_idat = seen_iend = False
    image_data = bytearray()
    expected_raw = 0
    while offset + 12 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        end = offset + 12 + length
        if length > MAX_IMAGE_BYTES or end > len(data):
            raise RequestFailure("invalid_request", "Image PNG chunk is invalid")
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : end - 4]
        crc = struct.unpack_from(">I", data, end - 4)[0]
        if zlib.crc32(kind + payload) != crc:
            raise RequestFailure("invalid_request", "Image PNG checksum is invalid")
        if not seen_ihdr:
            if kind != b"IHDR" or length != 13:
                raise RequestFailure("invalid_request", "Image PNG header is invalid")
            width, height = struct.unpack_from(">II", payload)
            if (
                (width, height) != (512, 512)
                or payload[8] != 8
                or payload[9] not in (2, 6)
                or payload[10:13] != b"\x00\x00\x00"
            ):
                raise RequestFailure("invalid_request", "Image must be 512 x 512")
            expected_raw = (512 * (3 if payload[9] == 2 else 4) + 1) * 512
            seen_ihdr = True
        elif kind == b"IDAT":
            seen_idat = True
            image_data.extend(payload)
        elif kind == b"IEND":
            if length != 0 or not seen_idat or end != len(data):
                raise RequestFailure("invalid_request", "Image PNG is incomplete")
            seen_iend = True
            break
        elif kind == b"IHDR":
            raise RequestFailure("invalid_request", "Duplicate image header")
        offset = end
    if not seen_iend:
        raise RequestFailure("invalid_request", "Image PNG has no end marker")
    try:
        inflater = zlib.decompressobj()
        raw = inflater.decompress(image_data, expected_raw + 1)
        if (
            len(raw) != expected_raw
            or not inflater.eof
            or inflater.unused_data
            or inflater.unconsumed_tail
        ):
            raise ValueError("PNG pixel data is incomplete or oversized")
    except zlib.error as exc:
        raise RequestFailure("invalid_request", "Image pixels are corrupt") from exc
    except ValueError as exc:
        raise RequestFailure("invalid_request", "Image pixels are invalid") from exc


def _read_bounded_output(root: Path, candidate: Path) -> bytes:
    root = root.resolve(strict=True)
    path = candidate.absolute()
    if root not in path.parents or path == root:
        raise RequestFailure("invalid_request", "Image output escaped its directory")
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RequestFailure("invalid_request", "Image output contains a link")
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size > MAX_IMAGE_BYTES
    ):
        raise RequestFailure("invalid_request", "Image output is not a bounded file")
    with path.open("rb") as source:
        data = source.read(MAX_IMAGE_BYTES + 1)
    validate_png(data)
    return data


class ImageService:
    def __init__(
        self,
        store: ImageStore,
        provider: ImageProvider,
        manager: ResourceManager,
        profile: RuntimeProfile,
    ) -> None:
        self.store = store
        self.provider = provider
        self.manager = manager
        self.profile = profile

    @property
    def target_identity(self) -> str:
        return f"image:{self.profile.name}:workflow-v1"

    async def run(
        self, invocation: ToolInvocation, arguments: dict[str, object]
    ) -> ToolInvocation:
        if invocation.job_id is None:
            raise RequestFailure(
                "invalid_request", "Image generation requires a durable job"
            )
        signal = Event()
        await self.store.run(
            lambda: self.store.set_image_progress(invocation.id, "image_loading")
        )
        handle = await self.manager.acquire(self.profile)
        try:
            await self.store.run(
                lambda: self.store.set_image_progress(invocation.id, "image_generating")
            )
            seed = arguments["seed"]
            assert type(seed) is int
            source = await self.provider.generate(
                str(arguments["prompt"]), seed, invocation.id, signal
            )
            data = await asyncio.to_thread(
                _read_bounded_output, self.provider.output_root, source
            )
            await self.store.run(
                lambda: self.store.set_image_progress(invocation.id, "image_finalizing")
            )
            artifact_id = str(uuid4())
            relative = f"artifacts/{invocation.job_id}/{artifact_id}.png"
            staging = self.store.data_dir / "staging" / invocation.job_id
            final = self.store.data_dir / relative.replace("/", os.sep)
            await asyncio.to_thread(staging.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(final.parent.mkdir, parents=True, exist_ok=True)
            partial = staging / f"{artifact_id}.png.part"
            digest = hashlib.sha256(data).hexdigest()
            await asyncio.to_thread(self._stage, partial, final, data)
            result_json = json.dumps(
                {
                    "status": "ok",
                    "data": {
                        "artifact_id": artifact_id,
                        "format": "png",
                        "width": 512,
                        "height": 512,
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            receipt_json = json.dumps(
                {"status": "complete", "artifact_id": artifact_id, "sha256": digest},
                sort_keys=True,
                separators=(",", ":"),
            )
            try:
                result = await self.store.run(
                    lambda: self.store.commit_image_result(
                        invocation.id,
                        artifact_id,
                        relative,
                        len(data),
                        digest,
                        receipt_json,
                        result_json,
                    )
                )
            except asyncio.CancelledError:
                await self.store.run(lambda: self.store.get_invocation(invocation.id))
                raise
            return result
        except BaseException:
            signal.set()
            await _settle(asyncio.create_task(self.manager.abort_request(handle)))
            raise
        finally:
            await handle.release()
            # A completed tool may be followed immediately by text inference.
            await _settle(asyncio.create_task(self.manager.unload()))

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
            raise OSError("Staged image verification failed")
        os.replace(partial, final)
