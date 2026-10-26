"""Windows user-bound secret encryption; no plaintext fallback."""
import base64
import ctypes
from ctypes import wintypes
import os


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(value: bytes, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise OSError("Credentials require Windows DPAPI")
    source_buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        function = crypt.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        function = crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = function(ctypes.byref(source), "Persona Runtime", None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise OSError("Windows credential encryption/decryption failed")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def protect(value: str) -> str:
    return base64.b64encode(_crypt(value.encode("utf-8"), False)).decode("ascii")


def unprotect(value: str) -> str:
    return _crypt(base64.b64decode(value, validate=True), True).decode("utf-8")
