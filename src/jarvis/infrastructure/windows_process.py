"""Windows Job Object supervisor with assignment during CreateProcessW."""

import ctypes
import msvcrt
import os
import subprocess
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path

_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_HANDLE = wintypes.HANDLE
_DWORD = wintypes.DWORD
_SIZE_T = ctypes.c_size_t
_NULL = None

EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
STARTF_USESTDHANDLES = 0x00000100
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
JOB_OBJECT_QUERY = 0x0004


class FILETIME(ctypes.Structure):
    _fields_ = [("low", _DWORD), ("high", _DWORD)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", _DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", _DWORD),
        ("dwY", _DWORD),
        ("dwXSize", _DWORD),
        ("dwYSize", _DWORD),
        ("dwXCountChars", _DWORD),
        ("dwYCountChars", _DWORD),
        ("dwFillAttribute", _DWORD),
        ("dwFlags", _DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", _HANDLE),
        ("hStdOutput", _HANDLE),
        ("hStdError", _HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", _HANDLE),
        ("hThread", _HANDLE),
        ("dwProcessId", _DWORD),
        ("dwThreadId", _DWORD),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", _DWORD),
        ("MinimumWorkingSetSize", _SIZE_T),
        ("MaximumWorkingSetSize", _SIZE_T),
        ("ActiveProcessLimit", _DWORD),
        ("Affinity", _SIZE_T),
        ("PriorityClass", _DWORD),
        ("SchedulingClass", _DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", _SIZE_T),
        ("JobMemoryLimit", _SIZE_T),
        ("PeakProcessMemoryUsed", _SIZE_T),
        ("PeakJobMemoryUsed", _SIZE_T),
    ]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION_STRUCT(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", _DWORD),
        ("TotalProcesses", _DWORD),
        ("ActiveProcesses", _DWORD),
        ("TotalTerminatedProcesses", _DWORD),
    ]


_kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_kernel.CreateJobObjectW.restype = _HANDLE
_kernel.OpenJobObjectW.argtypes = [_DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_kernel.OpenJobObjectW.restype = _HANDLE
_kernel.SetInformationJobObject.argtypes = [
    _HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    _DWORD,
]
_kernel.SetInformationJobObject.restype = wintypes.BOOL
_kernel.QueryInformationJobObject.argtypes = [
    _HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    _DWORD,
    ctypes.c_void_p,
]
_kernel.QueryInformationJobObject.restype = wintypes.BOOL
_kernel.InitializeProcThreadAttributeList.argtypes = [
    ctypes.c_void_p,
    _DWORD,
    _DWORD,
    ctypes.POINTER(_SIZE_T),
]
_kernel.InitializeProcThreadAttributeList.restype = wintypes.BOOL
_kernel.UpdateProcThreadAttribute.argtypes = [
    ctypes.c_void_p,
    _DWORD,
    _SIZE_T,
    ctypes.c_void_p,
    _SIZE_T,
    ctypes.c_void_p,
    ctypes.c_void_p,
]
_kernel.UpdateProcThreadAttribute.restype = wintypes.BOOL
_kernel.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
_kernel.CreateProcessW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.BOOL,
    _DWORD,
    ctypes.c_void_p,
    wintypes.LPCWSTR,
    ctypes.c_void_p,
    ctypes.POINTER(PROCESS_INFORMATION),
]
_kernel.CreateProcessW.restype = wintypes.BOOL
_kernel.CloseHandle.argtypes = [_HANDLE]
_kernel.CloseHandle.restype = wintypes.BOOL
_kernel.WaitForSingleObject.argtypes = [_HANDLE, _DWORD]
_kernel.WaitForSingleObject.restype = _DWORD
_kernel.TerminateJobObject.argtypes = [_HANDLE, _DWORD]
_kernel.TerminateJobObject.restype = wintypes.BOOL
_kernel.GetProcessTimes.argtypes = [
    _HANDLE,
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
]
_kernel.GetProcessTimes.restype = wintypes.BOOL
_kernel.OpenProcess.argtypes = [_DWORD, wintypes.BOOL, _DWORD]
_kernel.OpenProcess.restype = _HANDLE
_kernel.IsProcessInJob.argtypes = [_HANDLE, _HANDLE, ctypes.POINTER(wintypes.BOOL)]
_kernel.IsProcessInJob.restype = wintypes.BOOL


def _check(ok: object, action: str) -> None:
    if not ok:
        raise OSError(ctypes.get_last_error(), f"Windows {action} failed")


def _close(handle: int | None) -> None:
    if handle:
        _kernel.CloseHandle(handle)


def process_creation_time(handle: int) -> int:
    created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
    _check(
        _kernel.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ),
        "GetProcessTimes",
    )
    return (created.high << 32) | created.low


def same_process_alive(pid: int, creation_time: int) -> bool:
    handle = _kernel.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid
    )
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # invalid PID: confirmed gone
            return False
        raise OSError(error, "Cannot verify previous worker process")
    try:
        if process_creation_time(handle) != creation_time:
            return False
        result = _kernel.WaitForSingleObject(handle, 0)
        if result == WAIT_OBJECT_0:
            return False
        if result == WAIT_TIMEOUT:
            return True
        raise OSError(ctypes.get_last_error(), "Cannot query previous worker exit")
    finally:
        _close(handle)


def creation_time_for_pid(pid: int) -> int:
    handle = _kernel.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid
    )
    _check(handle, "OpenProcess")
    try:
        return process_creation_time(handle)
    finally:
        _close(handle)


def named_job_tree_exited(job_name: str, timeout: float = 2) -> bool:
    """Absence proves the previous kill-on-close job and all members are gone."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        handle = _kernel.OpenJobObjectW(JOB_OBJECT_QUERY, False, job_name)
        if not handle:
            if (
                ctypes.get_last_error() == 2
            ):  # never created, or destroyed after tree exit
                return True
            raise OSError(ctypes.get_last_error(), "Cannot inspect previous owned job")
        _close(handle)
        time.sleep(0.025)
    return False


class _TailReader:
    def __init__(self, fd: int, limit: int = 65_536) -> None:
        self._fd = fd
        self._limit = limit
        self._tail = bytearray()
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        try:
            while chunk := os.read(self._fd, 4096):
                self._tail.extend(chunk)
                if len(self._tail) > self._limit:
                    del self._tail[: len(self._tail) - self._limit]
        except OSError:
            pass
        finally:
            os.close(self._fd)

    @property
    def text(self) -> str:
        return bytes(self._tail).decode("utf-8", errors="replace")

    def wait_eof(self, timeout: float) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()


class OwnedProcess:
    def __init__(
        self,
        job: int,
        process: int,
        pid: int,
        created: int,
        cwd: Path,
        stdout: _TailReader,
        stderr: _TailReader,
    ) -> None:
        self.job = job
        self.process = process
        self.pid = pid
        self.creation_time = created
        self.cwd = cwd
        self.stdout = stdout
        self.stderr = stderr
        self._closed = False
        self._member_handles: dict[int, int] = {}

    def active_count(self) -> int:
        info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION_STRUCT()
        _check(
            _kernel.QueryInformationJobObject(
                self.job,
                JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
                None,
            ),
            "QueryInformationJobObject",
        )
        return int(info.ActiveProcesses)

    def contains_pid(self, pid: int) -> bool:
        handle = _kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        _check(handle, "OpenProcess")
        try:
            present = wintypes.BOOL()
            _check(
                _kernel.IsProcessInJob(handle, self.job, ctypes.byref(present)),
                "IsProcessInJob",
            )
            return bool(present.value)
        finally:
            _close(handle)

    def _capture_members(self) -> None:
        capacity = 8
        while capacity <= 1024:
            buffer = ctypes.create_string_buffer(8 + ctypes.sizeof(_SIZE_T) * capacity)
            ok = _kernel.QueryInformationJobObject(
                self.job,
                JOB_OBJECT_BASIC_PROCESS_ID_LIST,
                buffer,
                ctypes.sizeof(buffer),
                None,
            )
            header = ctypes.cast(buffer, ctypes.POINTER(_DWORD))
            assigned, count = int(header[0]), int(header[1])
            if not ok and ctypes.get_last_error() != 234:
                raise OSError(ctypes.get_last_error(), "Cannot list owned processes")
            if assigned > capacity or count < assigned:
                capacity = max(capacity * 2, assigned)
                continue
            entries = ctypes.cast(ctypes.byref(buffer, 8), ctypes.POINTER(_SIZE_T))
            for index in range(count):
                pid = int(entries[index])
                if pid in self._member_handles:
                    continue
                handle = _kernel.OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid
                )
                if not handle:
                    if ctypes.get_last_error() == 87:
                        continue
                    raise OSError(
                        ctypes.get_last_error(), "Cannot inspect owned process"
                    )
                self._member_handles[pid] = handle
            return
        raise RuntimeError("Owned process list exceeds the safe bound")

    def _members_exited(self) -> bool:
        for handle in self._member_handles.values():
            result = _kernel.WaitForSingleObject(handle, 0)
            if result == WAIT_TIMEOUT:
                return False
            if result != WAIT_OBJECT_0:
                raise OSError(ctypes.get_last_error(), "Cannot verify owned child exit")
        return True

    def wait_root_exit(self) -> None:
        result = _kernel.WaitForSingleObject(self.process, 0xFFFFFFFF)
        if result != WAIT_OBJECT_0:
            raise OSError(ctypes.get_last_error(), "Cannot wait for owned process")

    def root_exited(self) -> bool:
        result = _kernel.WaitForSingleObject(self.process, 0)
        if result == WAIT_OBJECT_0:
            return True
        if result == WAIT_TIMEOUT:
            return False
        raise OSError(ctypes.get_last_error(), "Cannot verify owned process exit")

    def wait_ready(self, cancel: threading.Event, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        ready = self.cwd / "ready"
        while time.monotonic() < deadline:
            if cancel.is_set():
                raise RuntimeError("Runtime load was cancelled")
            if ready.is_file():
                return
            if _kernel.WaitForSingleObject(self.process, 0) == WAIT_OBJECT_0:
                raise RuntimeError("Runtime exited before readiness")
            time.sleep(0.025)
        raise TimeoutError("Runtime readiness deadline exceeded")

    def stop(self, graceful_seconds: float, kill_seconds: float) -> bool:
        (self.cwd / "stop").touch()
        deadline = time.monotonic() + graceful_seconds
        while time.monotonic() < deadline:
            self._capture_members()
            if (
                self.active_count() == 0
                and self.root_exited()
                and self._members_exited()
            ):
                self.close_verified()
                return True
            time.sleep(0.025)
        self._capture_members()
        if self.active_count() != 0:
            _check(_kernel.TerminateJobObject(self.job, 1), "TerminateJobObject")
        deadline = time.monotonic() + kill_seconds
        while time.monotonic() < deadline:
            self._capture_members()
            if (
                self.active_count() == 0
                and self.root_exited()
                and self._members_exited()
            ):
                self.close_verified()
                return True
            time.sleep(0.025)
        return False

    def verify_stopped(self) -> bool:
        if self._closed:
            return not same_process_alive(self.pid, self.creation_time)
        self._capture_members()
        return (
            self.active_count() == 0 and self.root_exited() and self._members_exited()
        )

    def close_verified(self) -> None:
        if not self._closed:
            if (
                self.active_count() != 0
                or not self.root_exited()
                or not self._members_exited()
            ):
                raise RuntimeError("Cannot release a job with active processes")
            for handle in self._member_handles.values():
                _close(handle)
            self._member_handles.clear()
            _close(self.process)
            _close(self.job)
            self._closed = True


class WindowsProcessSupervisor:
    def spawn(
        self,
        executable: Path,
        arguments: list[str],
        cwd: Path,
        environment: dict[str, str],
        after_create: Callable[[int, int], None] | None = None,
        job_name: str | None = None,
        stdin_data: bytes | None = None,
        job_memory_bytes: int | None = None,
    ) -> OwnedProcess:
        executable = executable.resolve(strict=True)
        cwd = cwd.resolve(strict=True)
        if not executable.is_absolute() or not cwd.is_dir():
            raise ValueError("Explicit executable and working directory are required")
        if job_memory_bytes is not None and job_memory_bytes <= 0:
            raise ValueError("Job memory limit must be positive")
        job = _kernel.CreateJobObjectW(None, job_name)
        _check(job, "CreateJobObjectW")
        if job_name is not None and ctypes.get_last_error() == 183:
            _close(job)
            raise RuntimeError("Named Job Object already exists")
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if job_memory_bytes is not None:
            info.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_JOB_MEMORY
            info.JobMemoryLimit = job_memory_bytes
        created_process = False
        stdout_read = stderr_read = -1
        stdout_write = stderr_write = -1
        input_file = None
        stdin_read = stdin_write = -1
        attribute_list = None
        try:
            _check(
                _kernel.SetInformationJobObject(
                    job,
                    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    ctypes.byref(info),
                    ctypes.sizeof(info),
                ),
                "SetInformationJobObject",
            )
            stdout_read, stdout_write = os.pipe()
            stderr_read, stderr_write = os.pipe()
            if stdin_data is None:
                input_file = open(os.devnull, "rb")  # noqa: SIM115 - closed in finally
                input_handle = input_file.fileno()
            else:
                if len(stdin_data) > 4096:
                    raise ValueError("Worker request is too large")
                stdin_read, stdin_write = os.pipe()
                input_handle = stdin_read
            for fd in (stdout_write, stderr_write, input_handle):
                os.set_inheritable(fd, True)
            handles = (_HANDLE * 3)(
                msvcrt.get_osfhandle(input_handle),
                msvcrt.get_osfhandle(stdout_write),
                msvcrt.get_osfhandle(stderr_write),
            )
            jobs = (_HANDLE * 1)(job)
            size = _SIZE_T()
            _kernel.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            attribute_list = ctypes.cast(buffer, ctypes.c_void_p)
            _check(
                _kernel.InitializeProcThreadAttributeList(
                    attribute_list, 2, 0, ctypes.byref(size)
                ),
                "InitializeProcThreadAttributeList",
            )
            _check(
                _kernel.UpdateProcThreadAttribute(
                    attribute_list,
                    0,
                    PROC_THREAD_ATTRIBUTE_JOB_LIST,
                    ctypes.cast(jobs, ctypes.c_void_p),
                    ctypes.sizeof(jobs),
                    None,
                    None,
                ),
                "UpdateProcThreadAttribute(job list)",
            )
            _check(
                _kernel.UpdateProcThreadAttribute(
                    attribute_list,
                    0,
                    PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                    ctypes.cast(handles, ctypes.c_void_p),
                    ctypes.sizeof(handles),
                    None,
                    None,
                ),
                "UpdateProcThreadAttribute(handle list)",
            )
            startup = STARTUPINFOEXW()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = handles[0]
            startup.StartupInfo.hStdOutput = handles[1]
            startup.StartupInfo.hStdError = handles[2]
            startup.lpAttributeList = attribute_list
            process_info = PROCESS_INFORMATION()
            command = ctypes.create_unicode_buffer(
                subprocess.list2cmdline([str(executable), *arguments])
            )
            env = (
                "\0".join(
                    f"{key}={value}" for key, value in sorted(environment.items())
                )
                + "\0\0"
            )
            env_buffer = ctypes.create_unicode_buffer(env)
            _check(
                _kernel.CreateProcessW(
                    str(executable),
                    command,
                    None,
                    None,
                    True,
                    EXTENDED_STARTUPINFO_PRESENT
                    | CREATE_UNICODE_ENVIRONMENT
                    | CREATE_NO_WINDOW,
                    env_buffer,
                    str(cwd),
                    ctypes.byref(startup),
                    ctypes.byref(process_info),
                ),
                "CreateProcessW with creation-time Job Object",
            )
            created_process = True
            _close(process_info.hThread)
            created = process_creation_time(process_info.hProcess)
            if after_create is not None:
                after_create(process_info.dwProcessId, created)
            if stdin_data is not None:
                os.close(stdin_read)
                stdin_read = -1
                pending = memoryview(stdin_data)
                while pending:
                    written = os.write(stdin_write, pending)
                    if written <= 0:
                        raise OSError("Cannot write worker request")
                    pending = pending[written:]
                os.close(stdin_write)
                stdin_write = -1
            os.close(stdout_write)
            os.close(stderr_write)
            stdout_write = stderr_write = -1
            if input_file is not None:
                input_file.close()
                input_file = None
            stdout = _TailReader(stdout_read)
            stderr = _TailReader(stderr_read)
            stdout_read = stderr_read = -1
            return OwnedProcess(
                job,
                process_info.hProcess,
                process_info.dwProcessId,
                created,
                cwd,
                stdout,
                stderr,
            )
        except BaseException:
            if created_process:
                _kernel.TerminateJobObject(job, 1)
                _close(process_info.hProcess)
            _close(job)
            raise
        finally:
            if attribute_list is not None:
                _kernel.DeleteProcThreadAttributeList(attribute_list)
            for fd in (
                stdout_read,
                stdout_write,
                stderr_read,
                stderr_write,
                stdin_read,
                stdin_write,
            ):
                if fd >= 0:
                    os.close(fd)
            if input_file is not None:
                input_file.close()
