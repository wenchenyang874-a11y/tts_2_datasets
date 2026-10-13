import os

import pytest

from app_version import windows_version


def test_stable_windows_version_sorts_after_its_betas():
    assert windows_version("2.0.0-beta.1") < windows_version("2.0.0-beta.2") < windows_version("2.0.0") < windows_version("2.0.1-beta.1")


@pytest.mark.parametrize("version", ["2.0", "2.0.0-beta.0", "2.0.0-beta.65535", "65536.0.0", "02.0.0", "2.0.0-rc.1", "2.0.0; bad"])
def test_invalid_or_unrepresentable_release_version_fails(version):
    with pytest.raises(ValueError):
        windows_version(version)


@pytest.mark.skipif(os.name != "nt", reason="Win32 presence contract")
def test_installer_guard_keeps_marker_until_last_owner_closes():
    import ctypes
    from ctypes import wintypes
    from desktop.installer_guard import InstallerGuard, MUTEX_NAME

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    def present():
        handle = kernel.OpenMutexW(0x00100000, False, MUTEX_NAME)
        if handle:
            kernel.CloseHandle(handle)
        return bool(handle)

    assert not present(), "Close running desktop apps before this isolated test"
    first, second = InstallerGuard(), InstallerGuard()
    try:
        assert present()
        first.close()
        assert present()
    finally:
        first.close()
        second.close()
    assert not present()
