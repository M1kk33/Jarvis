"""Windows read-only file tools with checked paths and final-handle identity."""

import ctypes
import msvcrt
import ntpath
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path

from jarvis.domain.types import RequestFailure

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
_kernel.GetFinalPathNameByHandleW.argtypes = [
    ctypes.c_void_p,
    ctypes.c_wchar_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
]
_kernel.GetFinalPathNameByHandleW.restype = ctypes.c_uint32
_kernel.CloseHandle.argtypes = [ctypes.c_void_p]
_kernel.CloseHandle.restype = ctypes.c_int

_READ = 0x80000000
_SHARE_ALL = 0x1 | 0x2 | 0x4
_OPEN_EXISTING = 3
_BACKUP_SEMANTICS = 0x02000000
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_REPARSE_POINT = 0x400
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE)
_SECRET_NAMES = {
    ".env",
    ".git",
    ".ssh",
    ".aws",
    ".codex",
    ".venv",
    "credentials",
    "secrets",
    "tokens",
    "config.toml",
    "model.toml",
    "agents.md",
    "architecture.md",
    "pyproject.toml",
    "uv.lock",
    "calibration.json",
    "id_rsa",
    "id_ed25519",
    "jarvis.db",
}
_SECRET_SUFFIXES = (".pem", ".key", ".pfx", ".p12", ".kdbx")
_SECRET_MARKERS = (
    "secret",
    "credential",
    "password",
    "token",
    "api_key",
    "apikey",
    "private",
)


def _sensitive(part: str) -> bool:
    folded = part.casefold()
    return (
        folded.startswith((".", "config."))
        or folded in _SECRET_NAMES
        or folded.endswith(_SECRET_SUFFIXES)
        or any(marker in folded for marker in _SECRET_MARKERS)
    )


def _final_path(handle: int) -> str:
    buffer = ctypes.create_unicode_buffer(32768)
    length = _kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
    if length == 0 or length >= len(buffer):
        raise OSError(ctypes.get_last_error(), "Cannot resolve final handle path")
    path = buffer.value
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def _inside(path: str, root: str) -> bool:
    try:
        return ntpath.commonpath(
            (ntpath.normcase(path), ntpath.normcase(root))
        ) == ntpath.normcase(root)
    except ValueError:
        return False


def _lexical_path(raw: str) -> str:
    if (
        not re.match(r"^[A-Za-z]:[\\/]", raw)
        or raw.startswith("\\\\")
        or ":" in raw[2:]
        or "\x00" in raw
        or any(ord(char) < 32 for char in raw)
    ):
        raise RequestFailure(
            "invalid_request", "A local absolute drive path is required"
        )
    parts = raw[3:].replace("/", "\\").split("\\")
    if any(
        part in {"", ".", ".."}
        or part.rstrip(" .") != part
        or _RESERVED.fullmatch(part)
        or _sensitive(part)
        for part in parts
    ):
        raise RequestFailure("invalid_request", "Unsafe or private path component")
    return ntpath.normpath(raw)


def _no_reparse(path: str) -> None:
    drive, tail = ntpath.splitdrive(path)
    current = drive + "\\"
    for part in tail.strip("\\").split("\\"):
        if not part:
            continue
        current = ntpath.join(current, part)
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise RequestFailure("invalid_request", "Tool path is unavailable") from exc
        if (
            stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
        ):
            raise RequestFailure("invalid_request", "Reparse-point traversal is denied")


def _directory_handle(path: str) -> int:
    handle = _kernel.CreateFileW(
        path, _READ, _SHARE_ALL, None, _OPEN_EXISTING, _BACKUP_SEMANTICS, None
    )
    if not handle or handle == _INVALID_HANDLE:
        raise RequestFailure("invalid_request", "Directory cannot be opened")
    return int(handle)


class SafeReadTools:
    def __init__(self, roots: list[Path], data_dir: Path) -> None:
        self.data_dir = ntpath.normpath(str(data_dir.resolve()))
        workspace = ntpath.join(self.data_dir, "workspaces")
        checked: list[str] = []
        for candidate in roots:
            root = _lexical_path(str(candidate))
            if _inside(self.data_dir, root) or (
                _inside(root, self.data_dir) and not _inside(root, workspace)
            ):
                raise ValueError("The private Jarvis data root is not a read root")
            _no_reparse(root)
            if not os.path.isdir(root):
                raise ValueError("Read root must be an existing directory")
            checked.append(root)
        self.roots = tuple(checked)

    def _allowed(self, raw: str) -> str:
        path = _lexical_path(raw)
        if not any(_inside(path, root) for root in self.roots):
            raise RequestFailure(
                "invalid_request", "Path is outside permitted read roots"
            )
        if _inside(path, self.data_dir) and not _inside(
            path, ntpath.join(self.data_dir, "workspaces")
        ):
            raise RequestFailure(
                "invalid_request", "Private Jarvis state is not readable"
            )
        _no_reparse(path)
        return path

    def _identity(self, path: str, *, directory: bool) -> str:
        if directory:
            handle = _directory_handle(path)
            try:
                final = _final_path(handle)
                info = os.stat(path, follow_symlinks=False)
            finally:
                _kernel.CloseHandle(handle)
        else:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_BINARY)
            except OSError as exc:
                raise RequestFailure(
                    "invalid_request", "File cannot be opened"
                ) from exc
            try:
                final = _final_path(msvcrt.get_osfhandle(fd))
                info = os.fstat(fd)
            finally:
                os.close(fd)
        if ntpath.normcase(ntpath.normpath(final)) != ntpath.normcase(path):
            raise RequestFailure(
                "invalid_request", "Tool target changed through a link"
            )
        if directory and not stat.S_ISDIR(info.st_mode):
            raise RequestFailure("invalid_request", "Tool target is not a directory")
        if not directory and not stat.S_ISREG(info.st_mode):
            raise RequestFailure("invalid_request", "Tool target is not a regular file")
        if not directory and info.st_nlink != 1:
            raise RequestFailure("invalid_request", "Hard-linked files are denied")
        return f"{ntpath.normcase(path)}|{info.st_dev}|{info.st_ino}|{info.st_size}|{info.st_mtime_ns}"

    def inspect(self, name: str, arguments: dict[str, object]) -> str:
        if name == "current_time":
            return "clock"
        raw_path = arguments.get("path")
        if type(raw_path) is not str:
            raise RequestFailure("invalid_request", "Tool path must be a string")
        path = self._allowed(raw_path)
        if name == "list_directory":
            return self._identity(path, directory=True)
        if name == "read_text_file":
            return self._identity(path, directory=False)
        raise RequestFailure("unsupported_capability", "Unknown tool")

    def execute(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        if name == "current_time":
            return {"utc": datetime.now(UTC).isoformat(timespec="seconds")}
        raw_path = arguments.get("path")
        if type(raw_path) is not str:
            raise RequestFailure("invalid_request", "Tool path must be a string")
        path = self._allowed(raw_path)
        before = self.inspect(name, arguments)
        if name == "list_directory":
            entries: list[dict[str, str]] = []
            truncated = False
            scanned = 0
            with os.scandir(path) as listing:
                for entry in listing:
                    scanned += 1
                    if len(entries) >= 100 or scanned > 200:
                        truncated = True
                        break
                    if _sensitive(entry.name):
                        continue
                    info = entry.stat(follow_symlinks=False)
                    if (
                        stat.S_ISLNK(info.st_mode)
                        or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
                    ):
                        continue
                    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                        continue
                    entries.append(
                        {
                            "name": entry.name,
                            "kind": "directory"
                            if stat.S_ISDIR(info.st_mode)
                            else "file",
                        }
                    )
            if self.inspect(name, arguments) != before:
                raise RequestFailure("unavailable", "Directory changed while listing")
            return {
                "path": path,
                "entries": sorted(entries, key=lambda item: item["name"].casefold()),
                "truncated": truncated,
                "trust": "untrusted",
            }
        if name == "read_text_file":
            limit_value = arguments.get("max_bytes", 16384)
            if type(limit_value) is not int or not 1 <= limit_value <= 16384:
                raise RequestFailure("invalid_request", "Invalid file byte limit")
            limit = limit_value
            fd = os.open(path, os.O_RDONLY | os.O_BINARY)
            try:
                final = _final_path(msvcrt.get_osfhandle(fd))
                info = os.fstat(fd)
                if (
                    ntpath.normcase(ntpath.normpath(final)) != ntpath.normcase(path)
                    or not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                ):
                    raise RequestFailure("invalid_request", "File target changed")
                if info.st_size > limit:
                    raise RequestFailure(
                        "invalid_request", "File exceeds the byte limit"
                    )
                content = os.read(fd, limit + 1)
                if len(content) > limit:
                    raise RequestFailure(
                        "invalid_request", "File exceeds the byte limit"
                    )
                if b"\x00" in content:
                    raise RequestFailure(
                        "invalid_request", "Binary file is not readable"
                    )
                text = content.decode("utf-8")
                after = os.fstat(fd)
                if (info.st_size, info.st_mtime_ns) != (
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise RequestFailure("unavailable", "File changed while reading")
            finally:
                os.close(fd)
            return {"path": path, "text": text, "trust": "untrusted"}
        raise RequestFailure("unsupported_capability", "Unknown tool")

    def read_document(self, raw_path: str) -> bytes:
        """Read one explicitly imported UTF-8 document through a checked handle."""
        path = self._allowed(raw_path)
        if Path(path).suffix.lower() not in {".txt", ".md"}:
            raise RequestFailure(
                "invalid_request", "Only .txt and .md documents can be indexed"
            )
        limit = 256 * 1024
        before = self._identity(path, directory=False)
        fd = os.open(path, os.O_RDONLY | os.O_BINARY)
        try:
            final = _final_path(msvcrt.get_osfhandle(fd))
            info = os.fstat(fd)
            if (
                ntpath.normcase(ntpath.normpath(final)) != ntpath.normcase(path)
                or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > limit
            ):
                raise RequestFailure(
                    "invalid_request", "Document target or size is invalid"
                )
            data = os.read(fd, limit + 1)
            after = os.fstat(fd)
            if len(data) != info.st_size or (info.st_size, info.st_mtime_ns) != (
                after.st_size,
                after.st_mtime_ns,
            ):
                raise RequestFailure("unavailable", "Document changed while reading")
        finally:
            os.close(fd)
        if self._identity(path, directory=False) != before:
            raise RequestFailure("unavailable", "Document changed while reading")
        if b"\x00" in data:
            raise RequestFailure("invalid_request", "Binary document cannot be indexed")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RequestFailure("invalid_request", "Document must be UTF-8") from exc
        return data


class SafeDocumentReader:
    """M15 source grants reuse the checked Windows read boundary."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()

    def normalize_root(self, path: Path) -> str:
        checked = SafeReadTools([path], self.data_dir).roots[0]
        if _inside(checked, str(self.data_dir)):
            raise ValueError("Private Jarvis data cannot be a retrieval root")
        return ntpath.normcase(checked)

    def read(self, root: str, path: str) -> bytes:
        return SafeReadTools([Path(root)], self.data_dir).read_document(path)
