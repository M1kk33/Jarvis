"""M6 artifact-fixture path and digest verification."""

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath


def artifact_path(root: Path, raw: str) -> Path:
    relative = PurePosixPath(raw)
    if (
        relative.is_absolute()
        or not raw.startswith("artifacts/")
        or "\\" in raw
        or ":" in raw
        or any(part in ("", ".", "..") for part in raw.split("/"))
        or len(relative.parts) < 3
    ):
        raise ValueError("Invalid artifact path")
    return root.joinpath(*relative.parts)


def verify_artifact(root: Path, raw: str, size: int, sha256: str) -> Path:
    path = artifact_path(root, raw)
    current = root
    try:
        for part in PurePosixPath(raw).parts:
            current = current / part
            info = os.lstat(current)
            if (
                stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
            ):
                raise ValueError("Artifact path contains a link")
    except OSError as exc:
        raise ValueError("Artifact is missing") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Artifact is not a standalone regular file")
    hasher = hashlib.sha256()
    actual_size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            actual_size += len(chunk)
            hasher.update(chunk)
    if actual_size != size or hasher.hexdigest() != sha256:
        raise ValueError("Artifact is missing or corrupt")
    return path
