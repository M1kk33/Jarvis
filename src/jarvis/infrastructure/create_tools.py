"""Create-only Windows text files under explicit grants and checked directory handles."""

from __future__ import annotations

import ctypes
import hashlib
import json
import msvcrt
import ntpath
import os
import re
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from jarvis.domain.types import NoEffectFailure, RequestFailure
from jarvis.infrastructure.read_tools import _final_path, _inside, _lexical_path

_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel.CreateFileW.argtypes = [
    ctypes.c_wchar_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_void_p,
]
_kernel.CreateFileW.restype = ctypes.c_void_p
_kernel.GetFileInformationByHandle.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_kernel.GetFileInformationByHandle.restype = ctypes.c_int
_kernel.CloseHandle.argtypes = [ctypes.c_void_p]
_kernel.CloseHandle.restype = ctypes.c_int
_kernel.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
_kernel.GetDriveTypeW.restype = ctypes.c_uint32

_READ = 0x80000000
_WRITE = 0x40000000
_ATTRIBUTES = 0x80
_SHARE_READ_WRITE = 0x1 | 0x2
_CREATE_NEW = 1
_OPEN_EXISTING = 3
_BACKUP_SEMANTICS = 0x02000000
_OPEN_REPARSE_POINT = 0x00200000
_DIRECTORY = 0x10
_REPARSE_POINT = 0x400
_INVALID_HANDLE = ctypes.c_void_p(-1).value
MAX_TEXT_BYTES = 8192


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class _FileInfo(ctypes.Structure):
    _fields_ = [
        ("attributes", ctypes.c_uint32),
        ("created", _FileTime),
        ("accessed", _FileTime),
        ("modified", _FileTime),
        ("volume", ctypes.c_uint32),
        ("size_high", ctypes.c_uint32),
        ("size_low", ctypes.c_uint32),
        ("links", ctypes.c_uint32),
        ("index_high", ctypes.c_uint32),
        ("index_low", ctypes.c_uint32),
    ]


def _info(handle: int) -> _FileInfo:
    result = _FileInfo()
    if not _kernel.GetFileInformationByHandle(handle, ctypes.byref(result)):
        raise OSError(ctypes.get_last_error(), "Cannot inspect file handle")
    return result


def _identity(info: _FileInfo) -> tuple[int, int]:
    return info.volume, (info.index_high << 32) | info.index_low


def _normal(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path))


def _open(path: str, access: int, sharing: int, disposition: int, flags: int) -> int:
    handle = _kernel.CreateFileW(path, access, sharing, None, disposition, flags, None)
    if not handle or handle == _INVALID_HANDLE:
        raise OSError(ctypes.get_last_error(), "Cannot open checked path")
    return int(handle)


@contextmanager
def _held_parent(path: str) -> Iterator[_FileInfo]:
    """Hold every ancestor without delete sharing until creation finishes."""
    drive, tail = ntpath.splitdrive(path)
    current = drive + "\\"
    handles: list[int] = []
    try:
        for part in ("", *tail.strip("\\").split("\\")):
            if part:
                current = ntpath.join(current, part)
            handle = _open(
                current,
                _READ,
                _SHARE_READ_WRITE,
                _OPEN_EXISTING,
                _BACKUP_SEMANTICS | _OPEN_REPARSE_POINT,
            )
            handles.append(handle)
            info = _info(handle)
            if (
                not info.attributes & _DIRECTORY
                or info.attributes & _REPARSE_POINT
                or _normal(_final_path(handle)) != _normal(current)
            ):
                raise RequestFailure(
                    "invalid_request", "Directory changed or is a link"
                )
        yield _info(handles[-1])
    finally:
        for handle in reversed(handles):
            _kernel.CloseHandle(handle)


class SafeFileCreator:
    def __init__(self, roots: list[Path], data_dir: Path) -> None:
        private = _normal(str(data_dir.resolve()))
        workspace = ntpath.join(private, "workspaces")
        package = _normal(str(Path(__file__).resolve().parents[1]))
        checked: list[str] = []
        for candidate in roots:
            root = _lexical_path(str(candidate))
            if (
                _inside(private, root)
                or (_inside(root, private) and not _inside(root, workspace))
                or _inside(package, root)
                or _inside(root, package)
            ):
                raise ValueError("Write root includes private application state")
            drive = ntpath.splitdrive(root)[0] + "\\"
            if _kernel.GetDriveTypeW(drive) != 3:
                raise ValueError("Write root must be on a local fixed drive")
            with _held_parent(root):
                pass
            checked.append(root)
        self.roots = tuple(checked)

    def _path(self, raw: str) -> str:
        if any(unicodedata.category(char) in {"Cc", "Cf"} for char in raw):
            raise RequestFailure("invalid_request", "Text path has hidden controls")
        path = _lexical_path(raw)
        stem = ntpath.basename(path).split(".", 1)[0].upper()
        if stem in {"CONIN$", "CONOUT$", "CLOCK$"} or re.fullmatch(
            r"(?:COM|LPT)[¹²³]", stem
        ):
            raise RequestFailure("invalid_request", "Reserved device name is denied")
        if not path.lower().endswith(".txt") or not any(
            _inside(path, root) and path != root for root in self.roots
        ):
            raise RequestFailure("invalid_request", "Text path is outside a write root")
        return path

    @staticmethod
    def _content(content: str) -> bytes:
        if type(content) is not str or any(
            unicodedata.category(char) == "Cf"
            or (unicodedata.category(char) == "Cc" and char not in "\t\n\r")
            for char in content
        ):
            raise RequestFailure("invalid_request", "Text content is invalid")
        try:
            data = content.encode("utf-8")
        except UnicodeError as exc:
            raise RequestFailure("invalid_request", "Text is not UTF-8") from exc
        if len(data) > MAX_TEXT_BYTES:
            raise RequestFailure("invalid_request", "Text exceeds the byte limit")
        return data

    def inspect(self, raw: str, content: str) -> str:
        path = self._path(raw)
        self._content(content)
        with _held_parent(ntpath.dirname(path)) as parent:
            if os.path.lexists(path):
                raise NoEffectFailure("invalid_request", "Destination already exists")
            return json.dumps(
                {
                    "path": _normal(path),
                    "parent": _identity(parent),
                    "modified": (parent.modified.high << 32) | parent.modified.low,
                    "absent": True,
                },
                sort_keys=True,
                separators=(",", ":"),
            )

    def create(self, raw: str, content: str, expected: str) -> dict[str, object]:
        path = self._path(raw)
        data = self._content(content)
        with _held_parent(ntpath.dirname(path)) as parent:
            if os.path.lexists(path):
                raise NoEffectFailure("invalid_request", "Destination already exists")
            identity = json.dumps(
                {
                    "path": _normal(path),
                    "parent": _identity(parent),
                    "modified": (parent.modified.high << 32) | parent.modified.low,
                    "absent": True,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            if identity != expected:
                raise NoEffectFailure(
                    "invalid_request", "Destination changed after approval"
                )
            try:
                handle = _open(
                    path,
                    _READ | _WRITE,
                    0,
                    _CREATE_NEW,
                    _ATTRIBUTES | _OPEN_REPARSE_POINT,
                )
            except OSError as exc:
                if exc.errno in {80, 183}:
                    raise NoEffectFailure(
                        "invalid_request", "Destination already exists"
                    ) from exc
                raise RequestFailure(
                    "unavailable", "File could not be created"
                ) from exc
            try:
                fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
            except OSError:
                _kernel.CloseHandle(handle)
                raise
            try:
                created_info = _info(handle)
                if (
                    _normal(_final_path(handle)) != _normal(path)
                    or created_info.volume != parent.volume
                    or created_info.attributes & (_DIRECTORY | _REPARSE_POINT)
                ):
                    raise RequestFailure(
                        "invalid_request", "Created file escaped its root"
                    )
                written = 0
                while written < len(data):
                    count = os.write(fd, data[written:])
                    if count <= 0:
                        raise OSError("Created file write made no progress")
                    written += count
                os.fsync(fd)
                os.lseek(fd, 0, os.SEEK_SET)
                actual = bytearray()
                while len(actual) < len(data):
                    part = os.read(fd, len(data) - len(actual))
                    if not part:
                        break
                    actual.extend(part)
                info = os.fstat(fd)
                if (
                    bytes(actual) != data
                    or info.st_size != len(data)
                    or info.st_nlink != 1
                ):
                    raise OSError("Created file verification failed")
            finally:
                os.close(fd)
        return {
            "path": path,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        }

    def reconcile(
        self, raw: str, content: str, expected: str
    ) -> dict[str, object] | None:
        path = self._path(raw)
        data = self._content(content)
        try:
            target = json.loads(expected)
            with _held_parent(ntpath.dirname(path)) as parent:
                if target["path"] != _normal(path) or tuple(
                    target["parent"]
                ) != _identity(parent):
                    return None
                handle = _open(path, _READ, 0x1, _OPEN_EXISTING, _OPEN_REPARSE_POINT)
                try:
                    info = _info(handle)
                    if (
                        info.attributes & (_DIRECTORY | _REPARSE_POINT)
                        or info.links != 1
                        or _normal(_final_path(handle)) != _normal(path)
                        or (info.size_high << 32) | info.size_low != len(data)
                    ):
                        return None
                    fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
                    handle = 0
                    try:
                        actual = os.read(fd, len(data) + 1)
                    finally:
                        os.close(fd)
                    if actual != data:
                        return None
                finally:
                    if handle:
                        _kernel.CloseHandle(handle)
        except (OSError, KeyError, TypeError, ValueError, RequestFailure):
            return None
        return {
            "path": path,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        }
