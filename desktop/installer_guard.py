"""Keep installers from replacing files while this UI/backend is alive."""
import ctypes
import os
from ctypes import wintypes

MUTEX_NAME = r"Local\TTS_Dataset_Studio_Installer_Guard"


class InstallerGuard:
    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        # This is a presence marker, not a single-instance lock: separate data
        # directories may run concurrently. Each UI keeps a reference until its
        # backend closes; Inno Setup refuses upgrade/uninstall while any remains.
        # Local namespace matches the per-user interactive installer scope.
        self.handle = self.kernel.CreateMutexW(None, False, MUTEX_NAME)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
