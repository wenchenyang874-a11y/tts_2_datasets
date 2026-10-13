"""User-bound Windows encryption without putting a key beside the ciphertext."""
from __future__ import annotations

import base64
import ctypes
import json
import os
from ctypes import wintypes
from typing import Any


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]


def _crypt(payload: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Windows 凭据保护仅可在 Windows 上使用")
    # DPAPI uses the current Windows user's key, not a password saved in our
    # package. UI_FORBIDDEN ensures a failed decrypt reports an error rather
    # than opening an unexpected system dialog. Always free LocalAlloc output.
    buffer = ctypes.create_string_buffer(payload)
    incoming = _Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    outgoing = _Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise RuntimeError("无法使用当前 Windows 用户保护或读取供应商凭据")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel32.LocalFree(outgoing.data)


def encode_settings(payload: bytes) -> bytes:
    if os.getenv("TTS_DESKTOP_PROTECT_SETTINGS") != "1":
        return payload
    return json.dumps({"protection": "windows-dpapi-user-v1",
                       "ciphertext": base64.b64encode(_crypt(payload)).decode("ascii")}).encode("utf-8")


def decode_settings(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("protection") != "windows-dpapi-user-v1":
        return value
    try:
        payload = _crypt(base64.b64decode(value["ciphertext"], validate=True), decrypt=True)
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            raise ValueError("invalid payload")
        return decoded
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("供应商凭据文件损坏，不能自动重置") from exc
