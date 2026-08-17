"""Stable bridge-facing contract for the v3 visible desktop text sender."""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional, Sequence

from wechat_desktop.message_sender import (
    DesktopMessageError,
    DesktopMessageSettings,
    VisualDesktopMessageSender,
)
from wechat_desktop.input_guard import InputGuardError, WindowsInputGuard


log = logging.getLogger("wechat_automation.sender")

KIND_ALIASES = {
    "private": "private",
    "direct": "private",
    "contact": "private",
    "私聊": "private",
    "好友": "private",
    "group": "group",
    "群": "group",
    "群聊": "group",
}
SOFT_RETRY_DELAYS = (2.0, 5.0, 5.0)
HARD_LOCK_MAX_SECONDS = 30.0


class SenderError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


@dataclass
class SendResult:
    ok: bool
    code: str
    message: str
    kind: Optional[str] = None
    name: Optional[str] = None
    elapsed_ms: Optional[int] = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def raise_if_cancelled(
    cancel_event: Optional[threading.Event],
    *,
    send_clicked: bool = False,
) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise SenderError(
            "automation_cancelled",
            "自动化已停止；发送动作已经开始，停止后续观察。"
            if send_clicked
            else "自动化已停止，当前消息没有发送。",
            details={
                "send_clicked": bool(send_clicked),
                "send_committed": bool(send_clicked),
                "cancelled": True,
                "automatic_retry": False,
            },
        )


def interruptible_sleep(
    seconds: float,
    *,
    cancel_event: Optional[threading.Event],
    fallback: Callable[[float], None] = time.sleep,
) -> None:
    if seconds <= 0:
        raise_if_cancelled(cancel_event)
        return
    if cancel_event is not None:
        if cancel_event.wait(seconds):
            raise_if_cancelled(cancel_event)
    else:
        fallback(seconds)


class WindowsAutomationStopHotkey:
    """Register one event-driven Windows stop shortcut while a task is active."""

    MODIFIERS = {"ALT": 0x0001, "CTRL": 0x0002, "CONTROL": 0x0002, "SHIFT": 0x0004, "WIN": 0x0008}
    KEYS = {
        "ESC": 0x1B,
        "ESCAPE": 0x1B,
        "ENTER": 0x0D,
        "SPACE": 0x20,
        "TAB": 0x09,
        "PAUSE": 0x13,
    }

    def __init__(
        self,
        hotkey: str,
        cancel_event: threading.Event,
        *,
        enabled: bool = True,
        on_trigger: Callable[[], Any] | None = None,
    ) -> None:
        self.hotkey = str(hotkey or "Esc")
        self.cancel_event = cancel_event
        self.enabled = bool(enabled)
        self.on_trigger = on_trigger
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._error: BaseException | None = None

    @classmethod
    def _parse(cls, value: str) -> tuple[int, int]:
        tokens = [part.strip().upper() for part in value.replace("-", "+").split("+") if part.strip()]
        if not tokens:
            raise ValueError("停止快捷键不能为空。")
        modifiers = 0
        key = 0
        for token in tokens:
            if token in cls.MODIFIERS:
                modifiers |= cls.MODIFIERS[token]
                continue
            if key:
                raise ValueError("停止快捷键只能包含一个普通按键。")
            if token in cls.KEYS:
                key = cls.KEYS[token]
            elif len(token) == 1 and token.isalnum():
                key = ord(token)
            elif token.startswith("F") and token[1:].isdigit() and 1 <= int(token[1:]) <= 24:
                key = 0x70 + int(token[1:]) - 1
            else:
                raise ValueError(f"不支持的停止快捷键：{token}")
        if not key:
            raise ValueError("停止快捷键缺少普通按键。")
        return modifiers, key

    def _run(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._thread_id = int(kernel32.GetCurrentThreadId())
        identifier = 0xB317
        registered = False
        try:
            modifiers, key = self._parse(self.hotkey)
            # MOD_NOREPEAT prevents a held key from repeatedly firing callbacks.
            if not user32.RegisterHotKey(None, identifier, modifiers | 0x4000, key):
                error = ctypes.get_last_error()
                raise OSError(error, f"无法注册停止快捷键 {self.hotkey}: {error}")
            registered = True
            self._ready.set()
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == 0x0312 and int(message.wParam) == identifier:
                    self.cancel_event.set()
                    if self.on_trigger is not None:
                        try:
                            self.on_trigger()
                        except Exception:
                            log.exception("停止快捷键回调失败。")
                    break
        except BaseException as exc:  # surfaced to __enter__
            self._error = exc
            self._ready.set()
        finally:
            if registered:
                user32.UnregisterHotKey(None, identifier)

    def __enter__(self) -> "WindowsAutomationStopHotkey":
        if not self.enabled:
            return self
        self._thread = threading.Thread(
            target=self._run,
            name="wechat-v3-stop-hotkey",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(1.0):
            raise RuntimeError("停止快捷键监听启动超时。")
        if self._error is not None:
            raise self._error
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._thread_id:
            try:
                ctypes.WinDLL("user32", use_last_error=True).PostThreadMessageW(
                    self._thread_id, 0x0012, 0, 0  # WM_QUIT
                )
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=0.5)


NON_RETRYABLE_CODES = {
    "automation_cancelled",
    "invalid_kind",
    "invalid_name",
    "invalid_text",
    "invalid_input_parts",
    "invalid_sender_settings",
}


def _normalize_parts(
    text: str,
    input_parts: Optional[Sequence[Sequence[str]]],
) -> tuple[tuple[str, str], ...]:
    if not input_parts:
        return (("text", text),)
    result: list[tuple[str, str]] = []
    for item in input_parts:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise SenderError("invalid_input_parts", "输入片段必须是二元组。")
        mode, value = str(item[0]), str(item[1])
        if mode not in {"text", "keyboard", "mention"} or not value:
            raise SenderError("invalid_input_parts", "输入片段类型或内容无效。")
        result.append((mode, value))
    if "".join(value for _mode, value in result) != text:
        raise SenderError("invalid_input_parts", "输入片段拼接结果与消息正文不一致。")
    return tuple(result)


def _retry_schedule(max_attempts: int, delays: Sequence[float], enabled: bool) -> tuple[float, ...]:
    if not enabled or max_attempts <= 1:
        return ()
    needed = max_attempts - 1
    values = tuple(float(item) for item in delays)
    if not values:
        return (0.0,) * needed
    if len(values) >= needed:
        return values[:needed]
    return values + (values[-1],) * (needed - len(values))


def send_message(
    kind: str,
    name: str,
    text: str,
    *,
    timeout: float = 8.0,
    settle: float = 0.35,
    search_result_wait_min: float = 0.50,
    search_result_wait_max: float = 0.70,
    conversation_entry_mode: str = "mouse_click_sections",
    conversation_enter_delay_min: float = 0.20,
    conversation_enter_delay_max: float = 0.50,
    verification_timeout: float = 0.0,
    soft_protection: bool = True,
    lock_mouse: bool = False,
    lock_keyboard: bool = False,
    auto_launch_wechat: bool = False,
    wechat_executable: str = "",
    launch_timeout: float = 30.0,
    adaptive_layout: bool = True,
    reuse_open_chat: bool = False,
    layout_cache: bool = True,
    input_parts: Optional[Sequence[Sequence[str]]] = None,
    mention_candidate_timeout: float = 2.0,
    mention_after_at_delay_min: float = 0.12,
    mention_after_at_delay_max: float = 0.32,
    mention_min_wait: float = 0.25,
    mention_before_enter_delay_min: float = 0.10,
    mention_before_enter_delay_max: float = 0.28,
    mention_confirm_timeout: float = 0.8,
    mention_fallback_enabled: bool = True,
    tray_activation: bool = True,
    tray_timeout: float = 3.0,
    dpi_scale_mode: str = "auto",
    dpi_scale_percent: int = 100,
    dpi_auto_min_percent: int = 70,
    dpi_auto_max_percent: int = 150,
    dpi_auto_step_percent: int = 5,
    file_launch_fallback: bool = False,
    render_mask_recovery: bool = False,
    mask_retry_count: int = 0,
    mask_wait: float = 0.0,
    retry_max_attempts: int = 4,
    retry_delays: Optional[Sequence[float]] = None,
    overall_timeout: float = 120.0,
    input_mode: str = "keyboard",
    append_line_break_after_input: bool = False,
    keyboard_clipboard_threshold_enabled: bool = False,
    keyboard_clipboard_threshold_chars: int = 40,
    wechat_ctrl_enter_confirmed: bool = False,
    character_delay: float = 0.03,
    character_delay_min: Optional[float] = None,
    character_delay_max: Optional[float] = None,
    natural_typing_enabled: bool = True,
    typing_burst_chars_min: int = 2,
    typing_burst_chars_max: int = 6,
    typing_pause_min: float = 0.18,
    typing_pause_max: float = 0.65,
    send_review_delay_min: float = 0.60,
    send_review_delay_max: float = 1.40,
    click_before_delay_min: float = 0.10,
    click_before_delay_max: float = 0.25,
    click_hold_duration_min: float = 0.04,
    click_hold_duration_max: float = 0.08,
    paste_enabled: bool = False,
    verification_enabled: bool = False,
    qt_hot_activation_enabled: bool = False,
    stop_hotkey: str = "Esc",
    window_position_enabled: bool = False,
    window_x: int = 100,
    window_y: int = 80,
    window_size_enabled: bool = False,
    window_width: int = 900,
    window_height: int = 700,
    stop_callback: Callable[[], Any] | None = None,
    cancel_event: Optional[threading.Event] = None,
    automation_factory: Callable[..., VisualDesktopMessageSender] = VisualDesktopMessageSender,
    input_guard_factory: Callable[..., Any] = WindowsInputGuard,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    **_unused: Any,
) -> SendResult:
    """Execute complete attempts; @ itself never has an independent retry."""

    started = monotonic()
    normalized_kind = KIND_ALIASES.get(str(kind or "").strip().lower())
    normalized_name = str(name or "").strip()
    try:
        if normalized_kind is None:
            raise SenderError("invalid_kind", "会话类型只能是私聊或群聊。")
        if not normalized_name:
            raise SenderError("invalid_name", "会话名称不能为空。")
        if not isinstance(text, str) or not text:
            raise SenderError("invalid_text", "发送文本不能为空。")
        parts = _normalize_parts(text, input_parts)
        if not 1 <= int(retry_max_attempts) <= 10:
            raise SenderError("invalid_sender_settings", "最多尝试次数必须在 1 到 10 之间。")
        delays = tuple(SOFT_RETRY_DELAYS if retry_delays is None else retry_delays)
        if len(delays) > 9 or any(float(item) < 0 or float(item) > 120 for item in delays):
            raise SenderError("invalid_sender_settings", "整体重试等待设置无效。")
        if not 1 <= float(overall_timeout) <= 600:
            raise SenderError("invalid_sender_settings", "整条消息总时限必须在 1 到 600 秒之间。")
        normalized_input_mode = "keyboard" if input_mode == "uia" else str(input_mode)
        if normalized_input_mode == "adaptive":
            normalized_input_mode = "clipboard" if paste_enabled else "keyboard"
        if append_line_break_after_input and not wechat_ctrl_enter_confirmed:
            raise SenderError(
                "ctrl_enter_confirmation_required",
                "启用输入结束后 Enter 换行前，必须确认微信发送消息快捷键已设为 Ctrl+Enter。",
            )
        delay_min = (
            float(character_delay)
            if character_delay_min is None
            else float(character_delay_min)
        )
        delay_max = (
            float(character_delay)
            if character_delay_max is None
            else float(character_delay_max)
        )
        settings = DesktopMessageSettings(
            locate_timeout=float(timeout),
            settle=float(settle),
            search_result_wait_min=float(search_result_wait_min),
            search_result_wait_max=float(search_result_wait_max),
            conversation_entry_mode=str(
                conversation_entry_mode or "mouse_click_sections"
            ).strip().lower(),
            conversation_enter_delay_min=float(conversation_enter_delay_min),
            conversation_enter_delay_max=float(conversation_enter_delay_max),
            character_delay=float(character_delay),
            character_delay_min=delay_min,
            character_delay_max=delay_max,
            natural_typing_enabled=bool(natural_typing_enabled),
            typing_burst_chars_min=int(typing_burst_chars_min),
            typing_burst_chars_max=int(typing_burst_chars_max),
            typing_pause_min=float(typing_pause_min),
            typing_pause_max=float(typing_pause_max),
            send_review_delay_min=float(send_review_delay_min),
            send_review_delay_max=float(send_review_delay_max),
            click_before_delay_min=float(click_before_delay_min),
            click_before_delay_max=float(click_before_delay_max),
            click_hold_duration_min=float(click_hold_duration_min),
            click_hold_duration_max=float(click_hold_duration_max),
            after_at_delay=(
                float(mention_after_at_delay_min),
                float(mention_after_at_delay_max),
            ),
            mention_candidate_timeout=float(mention_candidate_timeout),
            mention_min_wait=float(mention_min_wait),
            before_mention_enter_delay=(
                float(mention_before_enter_delay_min),
                float(mention_before_enter_delay_max),
            ),
            mention_confirm_timeout=float(mention_confirm_timeout),
            mention_fallback_enabled=bool(mention_fallback_enabled),
            input_mode=normalized_input_mode,
            append_line_break_after_input=bool(append_line_break_after_input),
            keyboard_clipboard_threshold_enabled=bool(
                keyboard_clipboard_threshold_enabled
            ),
            keyboard_clipboard_threshold_chars=int(
                keyboard_clipboard_threshold_chars
            ),
            layout_cache=bool(layout_cache),
            tray_activation_enabled=bool(tray_activation),
            tray_activation_timeout=float(tray_timeout),
            dpi_scale_mode=str(dpi_scale_mode or "auto").strip().lower(),
            dpi_scale_percent=int(dpi_scale_percent),
            dpi_auto_min_percent=int(dpi_auto_min_percent),
            dpi_auto_max_percent=int(dpi_auto_max_percent),
            dpi_auto_step_percent=int(dpi_auto_step_percent),
            window_position_enabled=bool(window_position_enabled),
            window_x=int(window_x),
            window_y=int(window_y),
            window_size_enabled=bool(window_size_enabled),
            window_width=int(window_width),
            window_height=int(window_height),
        )
    except (SenderError, ValueError, TypeError) as exc:
        error = exc if isinstance(exc, SenderError) else SenderError("invalid_sender_settings", str(exc))
        return SendResult(
            False,
            error.code,
            error.message,
            kind=normalized_kind,
            name=normalized_name or None,
            elapsed_ms=int((monotonic() - started) * 1000),
            details=error.details,
        )

    schedule = _retry_schedule(int(retry_max_attempts), delays, bool(soft_protection))
    if cancel_event is None and (lock_mouse or lock_keyboard):
        # A direct API caller may enable input protection without supplying a
        # cancellation event.  Create one so the protected stop hotkey can
        # still cancel the sender instead of merely waiting for the watchdog.
        cancel_event = threading.Event()
    failures: list[dict[str, Any]] = []
    deadline = started + float(overall_timeout)
    for attempt_index in range(len(schedule) + 1):
        input_lock_details: dict[str, Any] = {
            "mouse": bool(lock_mouse),
            "keyboard": bool(lock_keyboard),
            "enabled": bool(lock_mouse or lock_keyboard),
        }
        try:
            raise_if_cancelled(cancel_event)
            log.info(
                "整体消息尝试开始：第 %s/%s 次。",
                attempt_index + 1,
                len(schedule) + 1,
                extra={"automation_operation": "message.attempt"},
            )
            if attempt_index:
                wait = schedule[attempt_index - 1]
                if monotonic() + wait >= deadline:
                    raise SenderError(
                        "automation_timeout",
                        "剩余总时限不足以开始下一次整体重试。",
                        details={"attempt": attempt_index + 1},
                    )
                retry_wait_started = monotonic()
                log.info(
                    "整体重试前等待开始：%.3f 秒。",
                    wait,
                    extra={"automation_operation": "message.retry_wait"},
                )
                interruptible_sleep(wait, cancel_event=cancel_event, fallback=sleep)
                retry_wait_elapsed = max(0.0, monotonic() - retry_wait_started)
                log.info(
                    "整体重试前等待结束：实际 %.3f 秒。",
                    retry_wait_elapsed,
                    extra={
                        "automation_operation": "message.retry_wait",
                        "automation_duration_ms": int(round(retry_wait_elapsed * 1000)),
                    },
                )
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise SenderError("automation_timeout", "整条消息自动化已超过总时限。")
            current_settings = DesktopMessageSettings(
                **{
                    **settings.__dict__,
                    "locate_timeout": min(settings.locate_timeout, max(0.2, remaining)),
                }
            )
            sender = automation_factory(settings=current_settings)
            guard_options = {
                "lock_mouse": bool(lock_mouse),
                "lock_keyboard": bool(lock_keyboard),
                "max_seconds": HARD_LOCK_MAX_SECONDS,
                "cancel_event": cancel_event,
                "emergency_hotkey": str(stop_hotkey or "Esc"),
            }
            if stop_callback is not None:
                guard_options["on_emergency_stop"] = stop_callback
            try:
                guard = input_guard_factory(**guard_options)
            except TypeError as exc:
                if "on_emergency_stop" not in str(exc):
                    raise
                guard_options.pop("on_emergency_stop", None)
                guard = input_guard_factory(**guard_options)
            log.info(
                "本次桌面操作输入保护：鼠标锁=%s，键盘锁=%s。",
                bool(lock_mouse),
                bool(lock_keyboard),
                extra={"automation_operation": "input_guard"},
            )
            with guard:
                result = sender.send_once(
                    target_kind=normalized_kind,
                    target_name=normalized_name,
                    text=text,
                    input_parts=parts,
                    cancel_event=cancel_event,
                )
            if hasattr(guard, "details"):
                input_lock_details = dict(guard.details())
            details = {
                **result.details,
                "send_committed": result.send_committed,
                "send_clicked": result.send_committed,
                "states": list(result.states),
                "mentions": [asdict(item) for item in result.mentions],
                "warnings": list(result.warnings),
                "input_lock": input_lock_details,
                "protection": {
                    "overall_attempts": attempt_index + 1,
                    "overall_max_attempts": len(schedule) + 1,
                    "overall_failures": failures,
                    "mention_local_retries": 0,
                },
            }
            return SendResult(
                True,
                result.code,
                "消息发送动作已执行；本地未确认最终送达。",
                kind=normalized_kind,
                name=normalized_name,
                elapsed_ms=int((monotonic() - started) * 1000),
                details=details,
            )
        except DesktopMessageError as exc:
            error = SenderError(exc.code, exc.message, details=exc.details)
        except InputGuardError as exc:
            error = SenderError(exc.code, exc.message, details=exc.details)
        except SenderError as exc:
            error = exc
        except InterruptedError:
            error = SenderError(
                "automation_cancelled",
                "自动化已停止，当前消息没有发送。",
                details={"cancelled": True},
            )
        except Exception as exc:
            error = SenderError(
                "unexpected_error",
                f"桌面自动化出现未预期错误：{exc}",
                details={"error_type": type(exc).__name__},
            )

        failure = {
            "attempt": attempt_index + 1,
            "code": error.code,
            "message": error.message,
        }
        failures.append(failure)
        log.warning(
            "第 %s 次整体消息尝试结束：%s - %s。",
            attempt_index + 1,
            error.code,
            error.message,
            extra={"automation_operation": "message.attempt"},
        )
        committed = bool(error.details.get("send_committed") or error.details.get("send_clicked"))
        if committed:
            return SendResult(
                True,
                "sent_unverified",
                "发送动作已经开始，但后续状态无法确认；不会自动重试。",
                kind=normalized_kind,
                name=normalized_name,
                elapsed_ms=int((monotonic() - started) * 1000),
                details={
                    **error.details,
                    "ui_verified": False,
                    "automatic_retry": False,
                    "protection": {
                        "overall_attempts": attempt_index + 1,
                        "overall_failures": failures,
                        "mention_local_retries": 0,
                    },
                },
            )
        retryable = error.code not in NON_RETRYABLE_CODES
        has_retry = attempt_index < len(schedule)
        if retryable and has_retry:
            log.warning(
                "第 %s 次整条消息自动化失败，将整体重试：%s - %s",
                attempt_index + 1,
                error.code,
                error.message,
            )
            continue
        return SendResult(
            False,
            error.code,
            error.message,
            kind=normalized_kind,
            name=normalized_name,
            elapsed_ms=int((monotonic() - started) * 1000),
            details={
                **error.details,
                "automatic_retry": False,
                "protection": {
                    "overall_attempts": attempt_index + 1,
                    "overall_max_attempts": len(schedule) + 1,
                    "overall_exhausted": retryable and not has_retry,
                    "overall_failures": failures,
                    "mention_local_retries": 0,
                },
            },
        )

    raise AssertionError("整体重试循环不应运行到这里。")


__all__ = [
    "HARD_LOCK_MAX_SECONDS",
    "KIND_ALIASES",
    "SOFT_RETRY_DELAYS",
    "SendResult",
    "SenderError",
    "WindowsAutomationStopHotkey",
    "interruptible_sleep",
    "raise_if_cancelled",
    "send_message",
]
