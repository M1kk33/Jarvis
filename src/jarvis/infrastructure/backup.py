"""Verified SQLite snapshot and immutable artifact backup/restore."""

import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

from jarvis.infrastructure.artifacts import artifact_path, verify_artifact
from jarvis.infrastructure.sqlite_store import SqliteStore


class _Entry(TypedDict):
    path: str
    kind: str
    size: int
    sha256: str


def _digest(path: Path) -> tuple[int, str]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            size += len(chunk)
            hasher.update(chunk)
    return size, hasher.hexdigest()


def _verified(path: Path, size: int, sha256: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Referenced artifact is missing or not a regular file")
    actual_size, actual_hash = _digest(path)
    if actual_size != size or actual_hash != sha256:
        raise ValueError("Referenced artifact is missing or corrupt")


def _manifest(snapshot: Path) -> list[_Entry]:
    with closing(
        sqlite3.connect(f"file:{snapshot.as_posix()}?mode=ro", uri=True)
    ) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Backup SQLite snapshot is corrupt")
        rows = db.execute(
            "SELECT relative_path, kind, size_bytes, sha256 FROM artifacts "
            "WHERE status='available' ORDER BY relative_path"
        ).fetchall()
    return [
        {"path": row[0], "kind": row[1], "size": row[2], "sha256": row[3]}
        for row in rows
    ]


def create_backup(store: SqliteStore) -> Path:
    root = store.data_dir / "backups"
    root.mkdir(parents=True, exist_ok=True)
    name = str(uuid4())
    staging = root / f".incomplete-{name}"
    final = root / name
    staging.mkdir()
    snapshot = staging / "jarvis.db"
    store.backup_database(snapshot)
    artifacts = _manifest(snapshot)
    for item in artifacts:
        source = verify_artifact(
            store.data_dir, item["path"], item["size"], item["sha256"]
        )
        target = artifact_path(staging, item["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as origin, target.open("xb") as output:
            shutil.copyfileobj(origin, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        verify_artifact(staging, item["path"], item["size"], item["sha256"])
    database_size, database_hash = _digest(snapshot)
    document = {
        "version": 1,
        "database": {"size": database_size, "sha256": database_hash},
        "artifacts": artifacts,
    }
    with (staging / "manifest.json").open("x", encoding="utf-8") as output:
        json.dump(document, output, sort_keys=True, separators=(",", ":"))
        output.flush()
        os.fsync(output.fileno())
    staging.rename(final)
    return final


def restore_backup(backup_dir: Path, target_dir: Path) -> Path:
    """Restore only into an empty directory after independent verification."""
    source = backup_dir.resolve(strict=True)
    if (
        target_dir.is_symlink()
        or source == target_dir.resolve()
        or source in target_dir.resolve().parents
    ):
        raise ValueError("Restore destination must be separate from the backup")
    if target_dir.exists() and any(target_dir.iterdir()):
        raise ValueError("Restore destination must be empty")
    manifest_path = source / "manifest.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    if type(document) is not dict or document.get("version") != 1:
        raise ValueError("Unsupported backup manifest")
    db_info = document.get("database")
    artifacts = document.get("artifacts")
    if type(db_info) is not dict or type(artifacts) is not list:
        raise ValueError("Invalid backup manifest")
    if (
        set(db_info) != {"size", "sha256"}
        or type(db_info["size"]) is not int
        or db_info["size"] < 0
        or type(db_info["sha256"]) is not str
        or len(db_info["sha256"]) != 64
    ):
        raise ValueError("Invalid database digest in backup manifest")
    snapshot = source / "jarvis.db"
    _verified(snapshot, db_info["size"], db_info["sha256"])
    expected = _manifest(snapshot)
    if artifacts != expected:
        raise ValueError("Backup manifest does not match its SQLite snapshot")
    for item in expected:
        verify_artifact(source, item["path"], item["size"], item["sha256"])
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(snapshot, target_dir / "jarvis.db")
    for item in expected:
        destination = artifact_path(target_dir, item["path"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(artifact_path(source, item["path"]), destination)
        verify_artifact(target_dir, item["path"], item["size"], item["sha256"])
    return target_dir
