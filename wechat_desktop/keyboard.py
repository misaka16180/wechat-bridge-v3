"""Ordinary visible keyboard input used by the v3 no-send probe."""

from __future__ import annotations

import ctypes
import logging
import random
import threading
import time
from ctypes import wintypes
from typing import Callable


log = logging.getLogger("wechat_automation.keyboard")


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    )


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    )


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    )


class _INPUTUNION(ctypes.Union):
    # INPUT is a tagged union.  Even for keyboard events Windows requires
    # cbSize to describe the complete union, whose largest member is
    # MOUSEINPUT (32 bytes on 64-bit Windows).
    _fields_ = (
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    )


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = (("type", wintypes.DWORD), ("u", _INPUTUNION))


class Win32KeyboardBackend:
    """Send normal foreground keyboard events; never inject into WeChat."""

    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004

    def __init__(
        self,
        *,
        character_delay: float = 0.015,
        character_delay_min: float | None = None,
        character_delay_max: float | None = None,
        user32=None,
        uniform: Callable[[float, float], float] = random.uniform,
        randint: Callable[[int, int], int] = random.randint,
        sleep: Callable[[float], None] = time.sleep,
        hotkey_delay_min: float = 0.018,
        hotkey_delay_max: float = 0.045,
        natural_typing_enabled: bool = True,
        typing_burst_chars_min: int = 2,
        typing_burst_chars_max: int = 6,
        typing_pause_min: float = 0.18,
        typing_pause_max: float = 0.65,
    ) -> None:
        minimum = character_delay if character_delay_min is None else character_delay_min
        maximum = character_delay if character_delay_max is None else character_delay_max
        if minimum < 0 or maximum < minimum or maximum > 2:
            raise ValueError("逐字输入间隔范围必须位于 0 到 2 秒之间，且最长值不能小于最短值。")
        if hotkey_delay_min < 0 or hotkey_delay_max < hotkey_delay_min or hotkey_delay_max > 0.2:
            raise ValueError("快捷键按键间隔必须位于 0 到 0.2 秒之间。")
        if not 1 <= typing_burst_chars_min <= typing_burst_chars_max <= 100:
            raise ValueError("连续输入字数必须位于 1 到 100 之间，且最大值不能小于最小值。")
        if typing_pause_min < 0 or typing_pause_max < typing_pause_min or typing_pause_max > 10:
            raise ValueError("输入思考停顿必须位于 0 到 10 秒之间，且最长值不能小于最短值。")
        # Keep the legacy scalar for adapters that still inspect it.
        self.character_delay = float(character_delay)
        self.character_delay_min = float(minimum)
        self.character_delay_max = float(maximum)
        self._uniform = uniform
        self._randint = randint
        self._sleep = sleep
        self.hotkey_delay_min = float(hotkey_delay_min)
        self.hotkey_delay_max = float(hotkey_delay_max)
        self.natural_typing_enabled = bool(natural_typing_enabled)
        self.typing_burst_chars_min = int(typing_burst_chars_min)
        self.typing_burst_chars_max = int(typing_burst_chars_max)
        self.typing_pause_min = float(typing_pause_min)
        self.typing_pause_max = float(typing_pause_max)
        expected_size = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        actual_size = ctypes.sizeof(_INPUT)
        if actual_size != expected_size:
            raise RuntimeError(
                f"Win32 INPUT 结构大小错误：应为 {expected_size}，实际为 {actual_size}。"
            )
        self._user32 = user32 or ctypes.WinDLL("user32", use_last_error=True)
        self._user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
        self._user32.SendInput.restype = wintypes.UINT

    def _send(self, *, virtual_key: int, scan_code: int, flags: int) -> None:
        event = _INPUT(
            type=1,
            ki=_KEYBDINPUT(virtual_key, scan_code, flags, 0, 0),
        )
        ctypes.set_last_error(0)
        sent = self._user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event))
        if sent != 1:
            error = ctypes.get_last_error()
            raise OSError(error, f"SendInput 失败: {error}")

    def key_down(self, virtual_key: int) -> None:
        self._send(virtual_key=virtual_key, scan_code=0, flags=0)

    def key_up(self, virtual_key: int) -> None:
        self._send(
            virtual_key=virtual_key,
            scan_code=0,
            flags=self.KEYEVENTF_KEYUP,
        )

    def press_key(self, virtual_key: int) -> None:
        self.key_down(virtual_key)
        self.key_up(virtual_key)

    def hotkey(self, modifier: int, key: int) -> None:
        self.key_down(modifier)
        try:
            self._sleep(float(self._uniform(self.hotkey_delay_min, self.hotkey_delay_max)))
            self.press_key(key)
        finally:
            self._sleep(float(self._uniform(self.hotkey_delay_min, self.hotkey_delay_max)))
            self.key_up(modifier)

    @staticmethod
    def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("自动化已停止，未继续执行键盘输入。")

    def type_text(
        self,
        text: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Type utility/search text with only the configured per-character jitter."""

        self._type_text(text, cancel_event=cancel_event, natural_rhythm=False)

    def type_message_text(
        self,
        text: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Type message text in short bursts separated by bounded thinking pauses."""

        self._type_text(
            text,
            cancel_event=cancel_event,
            natural_rhythm=self.natural_typing_enabled,
        )

    def _wait_interruptibly(
        self,
        delay: float,
        cancel_event: threading.Event | None,
    ) -> None:
        if delay <= 0:
            return
        if cancel_event is not None:
            if cancel_event.wait(delay):
                self._raise_if_cancelled(cancel_event)
        else:
            self._sleep(delay)

    def _type_text(
        self,
        text: str,
        *,
        cancel_event: threading.Event | None,
        natural_rhythm: bool,
    ) -> None:
        value = str(text).replace("\r\n", "\n").replace("\r", "\n")
        burst_target = (
            int(self._randint(self.typing_burst_chars_min, self.typing_burst_chars_max))
            if natural_rhythm and len(value) > 1
            else 0
        )
        burst_count = 0
        for index, character in enumerate(value):
            self._raise_if_cancelled(cancel_event)
            if character == "\n":
                # v3 requires WeChat's send shortcut to be Ctrl+Enter.  A bare
                # Enter therefore creates a line break and never submits the
                # message while text is being composed.
                self.press_key(0x0D)  # VK_RETURN
            else:
                codepoint = ord(character)
                if codepoint <= 0xFFFF:
                    units = (codepoint,)
                else:
                    encoded = character.encode("utf-16-le", "surrogatepass")
                    units = tuple(
                        int.from_bytes(encoded[i : i + 2], "little")
                        for i in range(0, len(encoded), 2)
                    )
                for unit in units:
                    self._send(
                        virtual_key=0,
                        scan_code=unit,
                        flags=self.KEYEVENTF_UNICODE,
                    )
                    self._send(
                        virtual_key=0,
                        scan_code=unit,
                        flags=self.KEYEVENTF_UNICODE | self.KEYEVENTF_KEYUP,
                    )
            if index >= len(value) - 1:
                continue
            burst_count += 1
            if self.character_delay_max:
                delay = float(
                    self._uniform(
                        self.character_delay_min,
                        self.character_delay_max,
                    )
                )
                self._wait_interruptibly(delay, cancel_event)
            if natural_rhythm and burst_count >= burst_target:
                pause = float(self._uniform(self.typing_pause_min, self.typing_pause_max))
                log.info(
                    "连续输入 %s 个字符后开始思考停顿：计划 %.3f 秒。",
                    burst_count,
                    pause,
                    extra={
                        "automation_operation": "input.typing_think_pause",
                    },
                )
                pause_started = time.monotonic()
                self._wait_interruptibly(pause, cancel_event)
                elapsed = max(0.0, time.monotonic() - pause_started)
                log.info(
                    "思考停顿结束：实际 %.3f 秒。",
                    elapsed,
                    extra={
                        "automation_operation": "input.typing_think_pause",
                        "automation_duration_ms": int(round(elapsed * 1000)),
                    },
                )
                burst_count = 0
                burst_target = int(
                    self._randint(
                        self.typing_burst_chars_min,
                        self.typing_burst_chars_max,
                    )
                )

    def ctrl_f(self) -> None:
        self.hotkey(0x11, 0x46)  # VK_CONTROL + F

    def ctrl_a(self) -> None:
        self.hotkey(0x11, 0x41)  # VK_CONTROL + A

    def ctrl_v(self) -> None:
        self.hotkey(0x11, 0x56)  # VK_CONTROL + V

    def ctrl_enter(self) -> None:
        self.hotkey(0x11, 0x0D)  # VK_CONTROL + RETURN

    def enter(self) -> None:
        self.press_key(0x0D)  # VK_RETURN

    def up(self) -> None:
        self.press_key(0x26)  # VK_UP

    def backspace(self) -> None:
        self.press_key(0x08)  # VK_BACK

    def escape(self) -> None:
        self.press_key(0x1B)  # VK_ESCAPE


__all__ = ["Win32KeyboardBackend"]
