"""Temporary physical input protection for visible desktop automation."""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from ctypes import wintypes
from typing import Any, Callable


log = logging.getLogger("wechat_automation.input_guard")


class InputGuardError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


def _parse_hotkey(value: str) -> tuple[frozenset[int], int]:
    modifiers = {
        "CTRL": 0x11,
        "CONTROL": 0x11,
        "ALT": 0x12,
        "SHIFT": 0x10,
        "WIN": 0x5B,
    }
    keys = {
        "ESC": 0x1B,
        "ESCAPE": 0x1B,
        "ENTER": 0x0D,
        "SPACE": 0x20,
        "TAB": 0x09,
        "PAUSE": 0x13,
    }
    tokens = [part.strip().upper() for part in str(value or "Esc").replace("-", "+").split("+") if part.strip()]
    required: set[int] = set()
    key = 0
    for token in tokens:
        if token in modifiers:
            required.add(modifiers[token])
        elif token in keys:
            key = keys[token]
        elif len(token) == 1 and token.isalnum():
            key = ord(token)
        elif token.startswith("F") and token[1:].isdigit() and 1 <= int(token[1:]) <= 24:
            key = 0x70 + int(token[1:]) - 1
    return frozenset(required), key or 0x1B


def _configure_windows_apis(
    user32: Any,
    kernel32: Any,
    hook_proc: Any,
    lresult: Any,
) -> None:
    """Declare every pointer-sized Win32 argument used by the hook thread.

    Without explicit ``argtypes``, ctypes assumes ordinary C ``int``
    arguments.  That truncates hook handles and ``LPARAM`` callback pointers
    in a 64-bit process.  In particular, the truncated ``CallNextHookEx``
    arguments can prevent an injected SendInput click from reaching WeChat
    while the physical-input guard is active.
    """

    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int,
        hook_proc,
        wintypes.HINSTANCE,
        wintypes.DWORD,
    ]
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.CallNextHookEx.argtypes = [
        wintypes.HHOOK,
        ctypes.c_int,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.CallNextHookEx.restype = lresult
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    user32.PeekMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.PeekMessageW.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.TranslateMessage.restype = wintypes.BOOL
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = lresult
    user32.PostThreadMessageW.argtypes = [
        wintypes.DWORD,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.PostThreadMessageW.restype = wintypes.BOOL
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short


class WindowsInputGuard:
    """Block physical mouse/keyboard input but allow injected automation.

    The protection is independent for mouse and keyboard, automatically
    releases after ``max_seconds``, and releases immediately when the shared
    cancellation event is set.  When keyboard protection is active, the
    configured stop hotkey is handled inside the hook so it cannot be hidden
    by the protection itself.
    """

    WH_KEYBOARD_LL = 13
    WH_MOUSE_LL = 14
    WM_QUIT = 0x0012
    WM_KEYDOWN = 0x0100
    WM_SYSKEYDOWN = 0x0104
    LLKHF_INJECTED = 0x0010
    LLMHF_INJECTED = 0x0001

    def __init__(
        self,
        *,
        lock_mouse: bool = False,
        lock_keyboard: bool = False,
        max_seconds: float = 30.0,
        cancel_event: threading.Event | None = None,
        emergency_hotkey: str = "Esc",
        on_emergency_stop: Callable[[], Any] | None = None,
    ) -> None:
        self.lock_mouse = bool(lock_mouse)
        self.lock_keyboard = bool(lock_keyboard)
        self.max_seconds = float(max_seconds)
        self.cancel_event = cancel_event
        self.emergency_hotkey = str(emergency_hotkey or "Esc")
        self.on_emergency_stop = on_emergency_stop
        self._ready = threading.Event()
        self._released = threading.Event()
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._timer: threading.Timer | None = None
        self._cancel_watcher: threading.Thread | None = None
        self._thread_id = 0
        self._user32: Any = None
        self._callbacks: list[Any] = []
        self._error: BaseException | None = None
        self._active = False
        self._timed_out = False
        self._started_at: float | None = None
        self._released_at: float | None = None

    @property
    def enabled(self) -> bool:
        return self.lock_mouse or self.lock_keyboard

    def _run(self) -> None:
        hooks: list[Any] = []
        try:
            if sys.platform != "win32":
                raise OSError("输入锁定仅支持 Windows。")
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._user32 = user32
            lresult = ctypes.c_ssize_t
            hook_proc = ctypes.WINFUNCTYPE(
                lresult,
                ctypes.c_int,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )

            class KeyboardHookData(ctypes.Structure):
                _fields_ = [
                    ("vkCode", wintypes.DWORD),
                    ("scanCode", wintypes.DWORD),
                    ("flags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", wintypes.WPARAM),
                ]

            class MouseHookData(ctypes.Structure):
                _fields_ = [
                    ("pt", wintypes.POINT),
                    ("mouseData", wintypes.DWORD),
                    ("flags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", wintypes.WPARAM),
                ]

            _configure_windows_apis(user32, kernel32, hook_proc, lresult)
            self._thread_id = int(kernel32.GetCurrentThreadId())
            required_modifiers, emergency_key = _parse_hotkey(self.emergency_hotkey)

            @hook_proc
            def keyboard_callback(n_code: int, w_param: int, l_param: int) -> int:
                if n_code >= 0:
                    data = ctypes.cast(l_param, ctypes.POINTER(KeyboardHookData)).contents
                    if not (data.flags & self.LLKHF_INJECTED):
                        if (
                            int(w_param) in {self.WM_KEYDOWN, self.WM_SYSKEYDOWN}
                            and int(data.vkCode) == emergency_key
                            and all(user32.GetAsyncKeyState(vk) & 0x8000 for vk in required_modifiers)
                        ):
                            if self.cancel_event is not None:
                                self.cancel_event.set()
                            if self.on_emergency_stop is not None:
                                try:
                                    self.on_emergency_stop()
                                except Exception:
                                    log.exception("停止快捷键回调失败。")
                        return 1
                return int(user32.CallNextHookEx(None, n_code, w_param, l_param))

            @hook_proc
            def mouse_callback(n_code: int, w_param: int, l_param: int) -> int:
                if n_code >= 0:
                    data = ctypes.cast(l_param, ctypes.POINTER(MouseHookData)).contents
                    if not (data.flags & self.LLMHF_INJECTED):
                        return 1
                return int(user32.CallNextHookEx(None, n_code, w_param, l_param))

            self._callbacks = [keyboard_callback, mouse_callback]
            module_handle = kernel32.GetModuleHandleW(None)
            if self.lock_keyboard:
                hook = user32.SetWindowsHookExW(
                    self.WH_KEYBOARD_LL,
                    keyboard_callback,
                    module_handle,
                    0,
                )
                if not hook:
                    raise ctypes.WinError(ctypes.get_last_error())
                hooks.append(hook)
            if self.lock_mouse:
                hook = user32.SetWindowsHookExW(
                    self.WH_MOUSE_LL,
                    mouse_callback,
                    module_handle,
                    0,
                )
                if not hook:
                    raise ctypes.WinError(ctypes.get_last_error())
                hooks.append(hook)

            message = wintypes.MSG()
            user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
            self._active = True
            self._started_at = time.monotonic()
            self._ready.set()
            if not self._stop_requested.is_set():
                while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                    user32.TranslateMessage(ctypes.byref(message))
                    user32.DispatchMessageW(ctypes.byref(message))
        except BaseException as exc:
            self._error = exc
            self._ready.set()
        finally:
            if self._user32 is not None:
                for hook in reversed(hooks):
                    try:
                        self._user32.UnhookWindowsHookEx(hook)
                    except Exception:
                        pass
            self._active = False
            self._released_at = time.monotonic()
            self._released.set()

    def _watchdog_release(self) -> None:
        self._timed_out = True
        self.release()

    def _watch_cancel(self) -> None:
        while not self._released.wait(0.05):
            if self.cancel_event is not None and self.cancel_event.is_set():
                self.release()
                return

    def __enter__(self) -> "WindowsInputGuard":
        if not self.enabled:
            return self
        if sys.platform != "win32":
            raise InputGuardError("input_lock_unavailable", "鼠标和键盘锁定仅支持 Windows。")
        self._thread = threading.Thread(target=self._run, name="wechat-v3-input-guard", daemon=True)
        self._thread.start()
        if not self._ready.wait(3.0):
            self.release()
            self._thread.join(timeout=2.0)
            raise InputGuardError("input_lock_timeout", "等待鼠标或键盘锁定启用超时。")
        if self._error is not None or not self._active:
            self.release()
            self._thread.join(timeout=2.0)
            raise InputGuardError(
                "input_lock_failed",
                f"无法启用鼠标或键盘锁定：{self._error}",
            )
        self._timer = threading.Timer(self.max_seconds, self._watchdog_release)
        self._timer.daemon = True
        self._timer.start()
        if self.cancel_event is not None:
            self._cancel_watcher = threading.Thread(
                target=self._watch_cancel,
                name="wechat-v3-input-guard-cancel",
                daemon=True,
            )
            self._cancel_watcher.start()
        return self

    def release(self) -> None:
        self._stop_requested.set()
        if self._timer is not None:
            self._timer.cancel()
        if self._active and self._user32 is not None and self._thread_id:
            try:
                self._user32.PostThreadMessageW(self._thread_id, self.WM_QUIT, 0, 0)
            except Exception:
                pass

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._cancel_watcher is not None and self._cancel_watcher is not threading.current_thread():
            self._cancel_watcher.join(timeout=0.2)

    def details(self) -> dict[str, Any]:
        elapsed_ms = 0
        if self._started_at is not None:
            ended = self._released_at or time.monotonic()
            elapsed_ms = max(0, int((ended - self._started_at) * 1000))
        return {
            "mouse": self.lock_mouse,
            "keyboard": self.lock_keyboard,
            "enabled": self.enabled,
            "active": self._active,
            "timed_out": self._timed_out,
            "elapsed_ms": elapsed_ms,
            "max_seconds": self.max_seconds,
            "emergency_hotkey": self.emergency_hotkey,
        }


__all__ = ["InputGuardError", "WindowsInputGuard"]
