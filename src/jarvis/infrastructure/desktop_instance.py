"""Own one Windows launcher per data directory; repeated clicks focus its window."""

import ctypes
import hashlib
from pathlib import Path

from jarvis.infrastructure.windows_process import WAIT_OBJECT_0, _close, _kernel

_kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
_kernel.CreateMutexW.restype = ctypes.c_void_p
_kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
_kernel.ReleaseMutex.restype = ctypes.c_int
_user = ctypes.WinDLL("user32", use_last_error=True)
_user.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
_user.FindWindowW.restype = ctypes.c_void_p
_user.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
_user.SetForegroundWindow.argtypes = [ctypes.c_void_p]


class DesktopInstance:
    def __init__(self, data_dir: Path) -> None:
        digest = hashlib.sha256(str(data_dir.resolve()).casefold().encode()).hexdigest()
        self.title = f"Jarvis · {digest[:8]}"
        self._handle = _kernel.CreateMutexW(
            None, False, f"Local\\JarvisDesktop_{digest}"
        )
        if not self._handle:
            raise OSError(ctypes.get_last_error(), "Cannot create launcher mutex")
        status = _kernel.WaitForSingleObject(self._handle, 0)
        self.acquired = status in (WAIT_OBJECT_0, 0x80)  # Abandoned owner exited.
        if status not in (WAIT_OBJECT_0, 0x80, 258):
            _close(self._handle)
            raise OSError(ctypes.get_last_error(), "Cannot acquire launcher mutex")

    def focus(self) -> bool:
        window = _user.FindWindowW(None, self.title)
        if not window:
            return False
        _user.ShowWindow(window, 9)  # Restore a minimized window.
        _user.SetForegroundWindow(window)
        return True

    def close(self) -> None:
        if self.acquired:
            _kernel.ReleaseMutex(self._handle)
        _close(self._handle)
