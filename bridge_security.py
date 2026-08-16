"""Password hashing helpers for the v3 web console."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

if os.name == "nt":
    import ctypes
    from ctypes import wintypes


ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 310_000
SALT_BYTES = 16
CRYPTPROTECT_LOCAL_MACHINE = 0x4


if os.name == "nt":
    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    _kernel32.LocalFree.restype = wintypes.HLOCAL


def _blob(data: bytes):
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def protect_local_secret(secret: str) -> str:
    """Encrypt a short bootstrap secret for this Windows machine with DPAPI.

    Machine scope deliberately allows the bridge to move between a normal and
    elevated process. The value only exists until first-time setup completes.
    """

    if os.name != "nt":
        raise OSError("本地初始密码保护仅支持 Windows。")
    raw = secret.encode("utf-8")
    input_blob, input_buffer = _blob(raw)
    output_blob = _DataBlob()
    if not _crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "WeChat Bridge initial password",
        None,
        None,
        None,
        CRYPTPROTECT_LOCAL_MACHINE,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        encrypted = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        return "dpapi-machine:" + base64.b64encode(encrypted).decode("ascii")
    finally:
        _kernel32.LocalFree(output_blob.pbData)
        del input_buffer


def unprotect_local_secret(protected: str) -> str:
    """Decrypt a current-user DPAPI value created by protect_local_secret."""

    if os.name != "nt":
        raise OSError("本地初始密码保护仅支持 Windows。")
    if not isinstance(protected, str):
        raise ValueError("初始密码密文格式无效。")
    if protected.startswith("dpapi-machine:"):
        encoded = protected[len("dpapi-machine:") :]
    elif protected.startswith("dpapi:"):
        # Compatibility with the short-lived current-user format. If a
        # different token cannot decrypt it, the configuration layer rotates
        # the unfinished bootstrap password automatically.
        encoded = protected[len("dpapi:") :]
    else:
        raise ValueError("初始密码密文格式无效。")
    encrypted = base64.b64decode(encoded, validate=True)
    input_blob, input_buffer = _blob(encrypted)
    output_blob = _DataBlob()
    if not _crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData).decode("utf-8")
    finally:
        _kernel32.LocalFree(output_blob.pbData)
        del input_buffer


def hash_password(password: str, *, iterations: int = ITERATIONS) -> str:
    salt = os.urandom(SALT_BYTES)
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return "$".join(
        [
            ALGORITHM,
            str(iterations),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(derived).decode("ascii"),
        ]
    )


def is_password_hash(value: str) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(ALGORITHM + "$")
        and value.count("$") == 3
    )


def verify_password(password: str, stored: str) -> bool:
    if not is_password_hash(stored):
        return False
    try:
        _, iterations, salt_text, expected_text = stored.split("$", 3)
        salt = base64.b64decode(salt_text, validate=True)
        expected = base64.b64decode(expected_text, validate=True)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            int(iterations),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)
