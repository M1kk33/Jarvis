"""Offline, bounded installation of an application-pinned component archive.

The trusted catalog belongs to the application, never to the selected archive.
Only a new data directory is published; existing histories are never modified.
"""

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from threading import Event

from jarvis.domain.resources import canonical_gpu_uuid

ASSETS = Path(__file__).with_name("assets")
PROFILES = ("model.toml", "image.toml", "vision.toml", "speech.toml", "piper.toml")
CHUNK = 1024 * 1024


class SetupCancelled(Exception):
    pass


def safe_name(name: str) -> str:
    """Reject Windows aliases, streams, devices and traversal on every platform."""
    if not isinstance(name, str) or len(name) > 220:
        raise ValueError("Invalid component path")
    for part in name.split("/"):
        if (
            not part
            or part in (".", "..")
            or part[-1] in " ."
            or re.search(r'[\\:<>"|?*\x00-\x1f]', part)
            or re.fullmatch(
                r"(?i)(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(?:\..*)?", part
            )
        ):
            raise ValueError("Unsafe component path")
    return name


def pinned_catalog() -> dict:
    return json.loads((ASSETS / "model-pack.json").read_text(encoding="utf-8"))


def _cancelled(cancel: Event) -> None:
    if cancel.is_set():
        raise SetupCancelled()


def install_pack(
    archive: Path,
    destination: Path,
    catalog: dict,
    device_uuid: str,
    cancel: Event,
    progress: Callable[[int, int], None],
) -> None:
    """Verify while extracting to a private sibling, then publish with one rename."""
    device_uuid = canonical_gpu_uuid(device_uuid)
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Setup requires a new data directory")
    parent = destination.parent.resolve(strict=True)
    if parent.anchor.startswith("\\\\"):
        raise ValueError("Setup requires a local drive")
    destination = parent / destination.name
    safe_name(destination.name)
    stage: Path | None = None
    try:
        _cancelled(cancel)
        with zipfile.ZipFile(archive) as package:
            if len(package.infolist()) > 100_000:
                raise ValueError("Too many component files")
            manifest_info = package.getinfo("manifest.json")
            if manifest_info.file_size > 20 * CHUNK:
                raise ValueError("Component manifest is too large")
            raw = package.read(manifest_info)
            if hashlib.sha256(raw).hexdigest() != catalog["manifest_sha256"]:
                raise ValueError("This component pack does not match the application")
            manifest = json.loads(raw)
            if manifest["format"] != 1 or manifest["id"] != catalog["id"]:
                raise ValueError("Unsupported component pack")
            files = manifest["files"]
            if not isinstance(files, dict) or not files:
                raise ValueError("Empty component pack")
            if not set(PROFILES).issubset(files):
                raise ValueError("Incomplete profile set")
            seen: set[str] = set()
            for name in files:
                key = safe_name(name).casefold()
                if key in seen or key == "manifest.json":
                    raise ValueError("Duplicate component path")
                seen.add(key)
                item = files[name]
                if (
                    type(item["size"]) is not int
                    or item["size"] < 0
                    or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                ):
                    raise ValueError("Invalid component metadata")
            entries = package.infolist()
            if len(entries) != len(files) + 1 or {e.filename for e in entries} != {
                *files,
                "manifest.json",
            }:
                raise ValueError("Unexpected or duplicate archive entries")
            for entry in entries:
                mode = entry.external_attr >> 16
                if (
                    entry.is_dir()
                    or (stat.S_IFMT(mode) not in (0, stat.S_IFREG))
                    or entry.flag_bits & 1
                ):
                    raise ValueError("Unsupported archive entry")
                if (
                    entry.filename != "manifest.json"
                    and entry.file_size != files[entry.filename]["size"]
                ):
                    raise ValueError("Component size mismatch")
            total = sum(item["size"] for item in files.values())
            if total != catalog["unpacked_bytes"] or total <= 0:
                raise ValueError("Component total size mismatch")
            if shutil.disk_usage(parent).free < total + 1024**3:
                raise OSError("Insufficient disk space")
            stage = Path(tempfile.mkdtemp(prefix=".jarvis-install-", dir=parent))
            done = 0
            progress(done, total)
            for name, item in files.items():
                _cancelled(cancel)
                target = stage.joinpath(*name.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                with package.open(name) as source, target.open("xb") as output:
                    while chunk := source.read(CHUNK):
                        _cancelled(cancel)
                        size += len(chunk)
                        if size > item["size"]:
                            raise ValueError("Component exceeds declared size")
                        digest.update(chunk)
                        output.write(chunk)
                        done += len(chunk)
                        progress(done, total)
                if size != item["size"] or digest.hexdigest() != item["sha256"]:
                    raise ValueError("Component checksum mismatch")
            for filename in PROFILES:
                path = stage / filename
                template = path.read_text(encoding="utf-8")
                path.write_text(
                    template.replace("@DEVICE_UUID@", device_uuid), encoding="utf-8"
                )
            (stage / "installed-components.json").write_text(
                json.dumps(
                    {"id": catalog["id"], "manifest_sha256": catalog["manifest_sha256"]}
                ),
                encoding="utf-8",
            )
            _cancelled(cancel)
            # Windows rename fails if destination exists, including an empty folder.
            if destination.exists():
                raise FileExistsError("Destination was created during installation")
            os.rename(stage, destination)
            stage = None
    finally:
        # Only our uniquely-created staging tree may be removed, never destination.
        if stage is not None:
            if stage.resolve().parent != parent or not stage.name.startswith(
                ".jarvis-install-"
            ):
                raise RuntimeError("Invalid staging ownership")
            shutil.rmtree(stage)


def verify_comfy_source(root: Path, commit: str) -> None:
    """Installed source is checked against hashes shipped inside Jarvis itself."""
    manifest = json.loads((ASSETS / "comfy-source.json").read_text(encoding="utf-8"))
    if manifest["commit"] != commit:
        raise ValueError("Packaged ComfyUI version mismatch")
    for name, expected in manifest["files"].items():
        path = root / safe_name(name)
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Packaged source must not redirect outside its root")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Packaged ComfyUI source was modified")
