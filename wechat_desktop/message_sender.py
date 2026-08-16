"""v3 visible desktop message transactions, including guarded real @ selection.

This module is the production boundary for text and media v3 automation.  It uses
screenshots, Win32 window metadata, mouse movement and ordinary foreground
keyboard input.  Read-only system accessibility may locate a tray icon, but
never activates WeChat content.  There is no OCR, injection, or private API.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
import logging
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from .chat_layer_recovery import (
    BackButtonDetector,
    ChatLayerRecovery,
    load_back_button_spec,
)
from .clipboard import ClipboardError, Win32Clipboard
from .derived_locator import (
    DerivedLocator,
    draw_derived_debug_overlay,
    load_derived_locator,
)
from .interaction import InteractionPolicy, RandomizedInteraction
from .keyboard import Win32KeyboardBackend
from .layout_cache import (
    GLOBAL_LAYOUT_CACHE,
    LayoutCacheKey,
    LayoutCacheStore,
)
from .mention_popup import MentionPopupDetector
from .models import CapturedFrame, Rect, WindowSnapshot
from .relative_locator import RelativeLocator, RelativeLocatorResult, load_relative_locator
from .recognition_snapshot import record_recognition_snapshot
from .session import DesktopSessionError, WeChatWindowSession
from .tray import WeChatTrayActivator


log = logging.getLogger("wechat_automation.target")


class AutomationTrace:
    """Emit one structured, timestamped event for each visible UI operation."""

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.monotonic = monotonic

    def begin(self, operation: str, message: str) -> float:
        self.event(operation, message)
        return self.monotonic()

    def end(
        self,
        operation: str,
        message: str,
        started: float,
        *,
        level: int = logging.INFO,
    ) -> int:
        duration_ms = max(0, int(round((self.monotonic() - started) * 1000)))
        self.event(operation, message, duration_ms=duration_ms, level=level)
        return duration_ms

    def event(
        self,
        operation: str,
        message: str,
        *,
        duration_ms: int | None = None,
        level: int = logging.INFO,
    ) -> None:
        extra: dict[str, Any] = {"automation_operation": str(operation)}
        if duration_ms is not None:
            extra["automation_duration_ms"] = int(duration_ms)
        log.log(level, str(message), extra=extra)


class DesktopMessageError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.details.setdefault("send_committed", False)
        self.details.setdefault("send_clicked", False)


class KeyboardLike(Protocol):
    def ctrl_a(self) -> None: ...
    def backspace(self) -> None: ...
    def enter(self) -> None: ...
    def up(self) -> None: ...
    def ctrl_v(self) -> None: ...
    def type_text(self, text: str, **kwargs: Any) -> None: ...


def _enabled_send_button_with_toolbar(result: RelativeLocatorResult) -> bool:
    """Return true only for the enabled Send + emoji-toolbar combination."""

    send = result.detections.get("send_button")
    emoji = result.detections.get("emoji_button")
    if (
        not result.accepted
        or result.click_bounds is None
        or send is None
        or not send.accepted
        or send.bounds is None
        or send.template is None
        or emoji is None
        or not emoji.accepted
        or emoji.bounds is None
        or emoji.template is None
    ):
        return False
    template_name = send.template.stem.casefold()
    return template_name == "send_enabled" or template_name.startswith("send_enabled_")


@dataclass(frozen=True)
class DesktopMessageSettings:
    locate_timeout: float = 8.0
    settle: float = 0.35
    conversation_entry_mode: str = "keyboard_shortcut"
    conversation_enter_delay_min: float = 0.20
    conversation_enter_delay_max: float = 0.50
    character_delay: float = 0.03
    character_delay_min: float = 0.02
    character_delay_max: float = 0.06
    natural_typing_enabled: bool = True
    typing_burst_chars_min: int = 2
    typing_burst_chars_max: int = 6
    typing_pause_min: float = 0.18
    typing_pause_max: float = 0.65
    send_review_delay_min: float = 0.60
    send_review_delay_max: float = 1.40
    click_before_delay_min: float = 0.10
    click_before_delay_max: float = 0.25
    click_hold_duration_min: float = 0.04
    click_hold_duration_max: float = 0.08
    after_at_delay: tuple[float, float] = (0.12, 0.32)
    mention_candidate_timeout: float = 2.0
    mention_min_wait: float = 0.25
    before_mention_enter_delay: tuple[float, float] = (0.10, 0.28)
    mention_confirm_timeout: float = 0.8
    mention_fallback_enabled: bool = True
    input_mode: str = "keyboard"
    append_line_break_after_input: bool = False
    keyboard_clipboard_threshold_enabled: bool = False
    keyboard_clipboard_threshold_chars: int = 40
    layout_cache: bool = True
    tray_activation_enabled: bool = True
    tray_activation_timeout: float = 3.0
    dpi_scale_mode: str = "auto"
    dpi_scale_percent: int = 100
    dpi_auto_min_percent: int = 70
    dpi_auto_max_percent: int = 150
    dpi_auto_step_percent: int = 5
    window_position_enabled: bool = False
    window_x: int = 100
    window_y: int = 80
    window_size_enabled: bool = False
    window_width: int = 900
    window_height: int = 700

    def __post_init__(self) -> None:
        if not 0.2 <= self.locate_timeout <= 120:
            raise ValueError("单阶段等待时间必须在 0.2 到 120 秒之间。")
        if self.dpi_scale_mode not in {"auto", "manual"}:
            raise ValueError("DPI 缩放模式只能是 auto 或 manual。")
        if not 50 <= self.dpi_scale_percent <= 300:
            raise ValueError("手动 DPI 缩放必须在 50% 到 300% 之间。")
        if not (
            50
            <= self.dpi_auto_min_percent
            <= self.dpi_auto_max_percent
            <= 300
        ):
            raise ValueError("DPI 自动比对范围必须在 50% 到 300% 之间并按升序排列。")
        if not 1 <= self.dpi_auto_step_percent <= 50:
            raise ValueError("DPI 自动扫描间隔必须在 1% 到 50% 之间。")
        if not 0 <= self.settle <= 10:
            raise ValueError("界面稳定等待必须在 0 到 10 秒之间。")
        if self.conversation_entry_mode not in {
            "keyboard_shortcut",
            "mouse_click_unstable",
        }:
            raise ValueError(
                "进入会话方式只能是 keyboard_shortcut 或 mouse_click_unstable。"
            )
        if not (
            0
            <= self.conversation_enter_delay_min
            <= self.conversation_enter_delay_max
            <= 10
        ):
            raise ValueError(
                "按上方向键后等待时间必须设置在 0 到 10 秒之间，且最长值不能小于最短值。"
            )
        if not 0 <= self.character_delay <= 2:
            raise ValueError("旧版逐字间隔必须在 0 到 2 秒之间。")
        if not (
            0 <= self.character_delay_min <= self.character_delay_max <= 2
        ):
            raise ValueError("逐字输入间隔范围必须位于 0 到 2 秒之间，且最长值不能小于最短值。")
        if not isinstance(self.natural_typing_enabled, bool):
            raise ValueError("自然输入节奏选项必须是布尔值。")
        if not (
            1
            <= self.typing_burst_chars_min
            <= self.typing_burst_chars_max
            <= 100
        ):
            raise ValueError("连续输入字数必须位于 1 到 100 之间，且最大值不能小于最小值。")
        if not (
            0 <= self.typing_pause_min <= self.typing_pause_max <= 10
        ):
            raise ValueError("输入思考停顿必须位于 0 到 10 秒之间，且最长值不能小于最短值。")
        if not (
            0
            <= self.send_review_delay_min
            <= self.send_review_delay_max
            <= 10
        ):
            raise ValueError("发送前检查停顿时间必须设置在 0 到 10 秒之间，且最长值不能小于最短值。")
        if not (
            0
            <= self.click_before_delay_min
            <= self.click_before_delay_max
            <= 10
        ):
            raise ValueError("鼠标点击前停顿时间必须设置在 0 到 10 秒之间，且最长值不能小于最短值。")
        if not (
            0
            <= self.click_hold_duration_min
            <= self.click_hold_duration_max
            <= 2
        ):
            raise ValueError("鼠标按住时间必须设置在 0 到 2 秒之间，且最长值不能小于最短值。")
        for label, values in (
            ("输入 @ 后等待", self.after_at_delay),
            ("选择候选前额外等待", self.before_mention_enter_delay),
        ):
            if len(values) != 2 or values[0] < 0 or values[1] < values[0] or values[1] > 10:
                raise ValueError(f"{label}范围无效。")
        if not 0.2 <= self.mention_candidate_timeout <= 30:
            raise ValueError("候选框最长等待必须在 0.2 到 30 秒之间。")
        if not 0 <= self.mention_min_wait <= self.mention_candidate_timeout:
            raise ValueError("候选框最短响应等待不能超过最长等待。")
        if not 0.1 <= self.mention_confirm_timeout <= 10:
            raise ValueError("候选选择确认等待必须在 0.1 到 10 秒之间。")
        if self.input_mode not in {"keyboard", "adaptive", "clipboard"}:
            raise ValueError("文字输入方式只能是 keyboard、adaptive 或 clipboard。")
        if not isinstance(self.append_line_break_after_input, bool):
            raise ValueError("输入结束后换行选项必须是布尔值。")
        if not isinstance(self.keyboard_clipboard_threshold_enabled, bool):
            raise ValueError("长文本自动改用剪贴板选项必须是布尔值。")
        if not 1 <= self.keyboard_clipboard_threshold_chars <= 100000:
            raise ValueError("长文本剪贴板阈值必须是 1 到 100000 之间的整数。")
        if not 0.1 <= self.tray_activation_timeout <= 30:
            raise ValueError("托盘唤醒等待时间必须在 0.1 到 30 秒之间。")
        if not -100000 <= self.window_x <= 100000 or not -100000 <= self.window_y <= 100000:
            raise ValueError("微信窗口 X/Y 坐标必须在 -100000 到 100000 之间。")
        if not 480 <= self.window_width <= 10000:
            raise ValueError("微信窗口宽度必须在 480 到 10000 像素之间。")
        if not 360 <= self.window_height <= 10000:
            raise ValueError("微信窗口高度必须在 360 到 10000 像素之间。")


@dataclass(frozen=True)
class MentionOutcome:
    name: str
    real: bool
    code: str
    popup_handle: int | None = None
    popup_class: str = ""


@dataclass(frozen=True)
class DesktopMessageResult:
    code: str
    states: tuple[str, ...]
    mentions: tuple[MentionOutcome, ...]
    warnings: tuple[dict[str, Any], ...]
    send_committed: bool
    send_button_bounds: Rect | None = None
    details: dict[str, Any] = field(default_factory=dict)


class MentionComposer:
    """Compose one mention once; it intentionally has no local retry loop."""

    def __init__(
        self,
        *,
        session: WeChatWindowSession,
        keyboard: KeyboardLike,
        interaction: RandomizedInteraction,
        detector: MentionPopupDetector,
        settings: DesktopMessageSettings,
        monotonic: Callable[[], float] = time.monotonic,
        trace: AutomationTrace | None = None,
    ) -> None:
        self.session = session
        self.keyboard = keyboard
        self.interaction = interaction
        self.detector = detector
        self.settings = settings
        self.monotonic = monotonic
        self.trace = trace or AutomationTrace(monotonic=monotonic)

    def _wait_random(
        self,
        operation: str,
        minimum: float,
        maximum: float,
        *,
        cancel_event: threading.Event | None,
    ) -> float:
        started = self.trace.begin(
            operation,
            f"等待开始：{minimum:.3f}–{maximum:.3f} 秒（随机等待）",
        )
        try:
            duration = self.interaction.wait_random(
                minimum,
                maximum,
                cancel_event=cancel_event,
            )
        except Exception as exc:
            self.trace.end(
                operation,
                f"等待失败：{type(exc).__name__}: {exc}",
                started,
                level=logging.ERROR,
            )
            raise
        self.trace.end(
            operation,
            f"等待结束：实际 {float(duration):.3f} 秒",
            started,
        )
        return float(duration)

    @staticmethod
    def _split_token(token: str) -> tuple[str, str]:
        raw = str(token or "")
        stripped = raw.strip()
        if not stripped.startswith("@") or not stripped[1:].strip():
            raise DesktopMessageError("invalid_mention", "真实 @ 的成员昵称不能为空。")
        name = stripped[1:].strip()
        suffix_index = raw.rfind(stripped) + len(stripped)
        return name, raw[suffix_index:]

    @staticmethod
    def _type(keyboard: KeyboardLike, text: str, cancel_event: threading.Event | None) -> None:
        try:
            keyboard.type_text(text, cancel_event=cancel_event)
        except TypeError as exc:
            if "cancel_event" not in str(exc):
                raise
            keyboard.type_text(text)

    @staticmethod
    def _cancelled(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise DesktopMessageError(
                "automation_cancelled",
                "自动化已停止，未继续处理真实 @。",
                details={"cancelled": True},
            )

    def compose(
        self,
        token: str,
        *,
        target_kind: str,
        cancel_event: threading.Event | None = None,
    ) -> tuple[MentionOutcome, dict[str, Any] | None]:
        name, suffix = self._split_token(token)
        self._cancelled(cancel_event)
        snapshot = self.session.snapshot()
        if self.session.handle is None:
            raise DesktopMessageError("wechat_session_missing", "微信窗口会话已经失效。")
        baseline = self.detector.visible_same_process_handles(
            self.session.handle,
            snapshot.process_id,
        )
        started = self.trace.begin("mention.type_at", "输入 @")
        try:
            self._type(self.keyboard, "@", cancel_event)
        except Exception as exc:
            self.trace.end(
                "mention.type_at",
                f"输入 @ 失败：{type(exc).__name__}: {exc}",
                started,
                level=logging.ERROR,
            )
            raise
        self.trace.end("mention.type_at", "已输入 @", started)
        self._wait_random(
            "mention.wait_after_at",
            *self.settings.after_at_delay,
            cancel_event=cancel_event,
        )
        started = self.trace.begin("mention.type_nickname", f"输入 @ 昵称：{name}")
        try:
            self._type(self.keyboard, name, cancel_event)
        except Exception as exc:
            self.trace.end(
                "mention.type_nickname",
                f"输入昵称失败：{type(exc).__name__}: {exc}",
                started,
                level=logging.ERROR,
            )
            raise
        self.trace.end("mention.type_nickname", "已输入 @ 昵称", started)

        if target_kind != "group":
            if self.settings.mention_fallback_enabled:
                started = self.trace.begin("mention.type_suffix", "私聊中保留普通文字 @ 后续文本")
                self._type(self.keyboard, suffix, cancel_event)
                self.trace.end("mention.type_suffix", "已保留私聊普通文字 @", started)
                warning = {
                    "code": "mention_downgraded_private_chat",
                    "message": f"私聊不执行真实 @，已保留普通文字 @{name}。",
                    "mention_name": name,
                }
                return MentionOutcome(name, False, warning["code"]), warning
            raise DesktopMessageError(
                "mention_requires_group",
                "真实 @ 只能用于群聊，当前操作已在发送前停止。",
                details={"mention_name": name},
            )

        nickname_finished_at = self.monotonic()
        deadline = nickname_finished_at + self.settings.mention_candidate_timeout
        candidate_kwargs = {
            "main_handle": self.session.handle,
            "process_id": snapshot.process_id,
            "main_rectangle": snapshot.window_rect,
            "baseline_handles": baseline,
        }
        candidate = None
        lookup_started = self.trace.begin(
            "mention.find_candidate",
            f"查找 @ 候选框：{name}（最多 {self.settings.mention_candidate_timeout:.3f} 秒）",
        )
        lookup_attempts = 0
        poll_wait_total = 0.0
        while self.monotonic() < deadline:
            self._cancelled(cancel_event)
            lookup_attempts += 1
            candidate = self.detector.best_candidate(**candidate_kwargs)
            if candidate is None:
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    break
                wait = min(self.detector.poll_interval, remaining)
                if cancel_event is not None:
                    if cancel_event.wait(wait):
                        self._cancelled(cancel_event)
                else:
                    self.detector.sleep(wait)
                poll_wait_total += wait
                continue

            elapsed = self.monotonic() - nickname_finished_at
            minimum_remaining = max(0.0, self.settings.mention_min_wait - elapsed)
            if minimum_remaining:
                self._wait_random(
                    "mention.wait_minimum",
                    minimum_remaining,
                    minimum_remaining,
                    cancel_event=cancel_event,
                )
            self._wait_random(
                "mention.wait_before_enter",
                *self.settings.before_mention_enter_delay,
                cancel_event=cancel_event,
            )
            # The Qt popup HWND may be destroyed and recreated while filtering.
            # Re-evaluate the logical popup and never require the same handle.
            candidate = self.detector.best_candidate(**candidate_kwargs)
            if candidate is None:
                continue
            self._cancelled(cancel_event)
            self.trace.end(
                "mention.find_candidate",
                (
                    f"候选框已找到，轮询 {lookup_attempts} 次；轮询等待累计 "
                    f"{poll_wait_total:.3f} 秒"
                ),
                lookup_started,
            )
            started = self.trace.begin("mention.press_enter", "按 Enter 选择候选项")
            try:
                self.keyboard.enter()
            except Exception as exc:
                self.trace.end(
                    "mention.press_enter",
                    f"按 Enter 失败：{type(exc).__name__}: {exc}",
                    started,
                    level=logging.ERROR,
                )
                raise
            self.trace.end("mention.press_enter", "已按 Enter 一次", started)
            started = self.trace.begin(
                "mention.confirm_candidate",
                f"确认 @ 候选框消失（最多 {self.settings.mention_confirm_timeout:.3f} 秒）",
            )
            if not self.detector.wait_until_absent(
                timeout=self.settings.mention_confirm_timeout,
                cancel_event=cancel_event,
                **candidate_kwargs,
            ):
                self.trace.end(
                    "mention.confirm_candidate",
                    "候选框仍存在，确认失败",
                    started,
                    level=logging.ERROR,
                )
                raise DesktopMessageError(
                    "mention_candidate_not_confirmed",
                    f"已按一次 Enter，但微信 @ 候选框没有消失；已停止发送。",
                    details={
                        "mention_name": name,
                        "popup_handle": candidate.handle,
                        "popup_class": candidate.class_name,
                    },
                )
            self.trace.end("mention.confirm_candidate", "候选框已消失", started)
            started = self.trace.begin("mention.type_suffix", "输入 @ 后续文本")
            self._type(self.keyboard, suffix, cancel_event)
            self.trace.end("mention.type_suffix", "已输入 @ 后续文本", started)
            return (
                MentionOutcome(
                    name=name,
                    real=True,
                    code="real_mention_selected",
                    popup_handle=candidate.handle,
                    popup_class=candidate.class_name,
                ),
                None,
            )

        if self.settings.mention_fallback_enabled:
            self.trace.end(
                "mention.find_candidate",
                (
                    f"候选框未找到，轮询 {lookup_attempts} 次；轮询等待累计 "
                    f"{poll_wait_total:.3f} 秒，降级为普通文字"
                ),
                lookup_started,
                level=logging.WARNING,
            )
            started = self.trace.begin("mention.type_suffix", "输入降级后的 @ 后续文本")
            self._type(self.keyboard, suffix, cancel_event)
            self.trace.end("mention.type_suffix", "已保留普通文字 @", started)
            warning = {
                "code": "mention_candidate_timeout_downgraded",
                "message": (
                    f"在 {self.settings.mention_candidate_timeout:.1f} 秒内没有检测到 @{name} "
                    "的候选框，已保留为普通文字。"
                ),
                "mention_name": name,
                "timeout": self.settings.mention_candidate_timeout,
            }
            return MentionOutcome(name, False, warning["code"]), warning
        self.trace.end(
            "mention.find_candidate",
            (
                f"候选框未找到，轮询 {lookup_attempts} 次；轮询等待累计 "
                f"{poll_wait_total:.3f} 秒，降级关闭"
            ),
            lookup_started,
            level=logging.ERROR,
        )
        raise DesktopMessageError(
            "mention_candidate_timeout",
            (
                f"在 {self.settings.mention_candidate_timeout:.1f} 秒内没有检测到 @{name} "
                "的候选框；自动降级已关闭，已停止发送。"
            ),
            details={"mention_name": name, "timeout": self.settings.mention_candidate_timeout},
        )


class VisualDesktopMessageSender:
    """Perform one complete pre-send transaction and one final Send click."""

    def __init__(
        self,
        *,
        settings: DesktopMessageSettings | None = None,
        session: WeChatWindowSession | None = None,
        interaction: RandomizedInteraction | None = None,
        keyboard: KeyboardLike | None = None,
        clipboard: Any = None,
        popup_detector: MentionPopupDetector | None = None,
        chat_layer_recovery: ChatLayerRecovery | None = None,
        layout_cache_store: LayoutCacheStore | None = None,
        locator_root: str | Path | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings or DesktopMessageSettings()
        self.interaction = interaction or RandomizedInteraction(
            policy=InteractionPolicy(
                click_before_delay_min=self.settings.click_before_delay_min,
                click_before_delay_max=self.settings.click_before_delay_max,
                click_hold_duration_min=self.settings.click_hold_duration_min,
                click_hold_duration_max=self.settings.click_hold_duration_max,
            )
        )
        self.session = session or WeChatWindowSession(
            position_enabled=self.settings.window_position_enabled,
            target_x=self.settings.window_x,
            target_y=self.settings.window_y,
            size_enabled=self.settings.window_size_enabled,
            target_width=self.settings.window_width,
            target_height=self.settings.window_height,
            tray_activation_enabled=self.settings.tray_activation_enabled,
            tray_activation_timeout=self.settings.tray_activation_timeout,
            tray_activator=WeChatTrayActivator(interaction=self.interaction),
        )
        self.keyboard = keyboard or Win32KeyboardBackend(
            character_delay=self.settings.character_delay,
            character_delay_min=self.settings.character_delay_min,
            character_delay_max=self.settings.character_delay_max,
            natural_typing_enabled=self.settings.natural_typing_enabled,
            typing_burst_chars_min=self.settings.typing_burst_chars_min,
            typing_burst_chars_max=self.settings.typing_burst_chars_max,
            typing_pause_min=self.settings.typing_pause_min,
            typing_pause_max=self.settings.typing_pause_max,
        )
        self.clipboard = clipboard or Win32Clipboard()
        self.popup_detector = popup_detector or MentionPopupDetector()
        self.monotonic = monotonic
        self.trace = AutomationTrace(monotonic=monotonic)
        self.layout_cache_store = layout_cache_store or GLOBAL_LAYOUT_CACHE
        self._layout_cache_key: LayoutCacheKey | None = None
        self._layout_cache_status = "disabled"
        self._cached_search_box: RelativeLocatorResult | None = None
        self._cached_chat_input: RelativeLocatorResult | None = None
        self._dpi_scale_policy: tuple[
            tuple[float, ...],
            tuple[float, ...],
        ] | None = None
        root = Path(locator_root or Path(__file__).resolve().parents[1] / "locators")
        self.search_box = RelativeLocator(load_relative_locator(root / "search_box_anchors.json"))
        self.search_result = DerivedLocator(load_derived_locator(root / "search_primary_result.json"))
        self.chat_input = RelativeLocator(load_relative_locator(root / "chat_input_by_toolbar.json"))
        self.chat_layer_recovery = chat_layer_recovery or ChatLayerRecovery(
            session=self.session,
            interaction=self.interaction,
            chat_locator=self.chat_input,
            back_detector=BackButtonDetector(
                load_back_button_spec(root / "chat_back_buttons.json")
            ),
        )

    @staticmethod
    def _auto_scale_range(
        minimum_percent: int,
        maximum_percent: int,
        step_percent: int = 5,
    ) -> tuple[float, ...]:
        values = list(
            range(minimum_percent, maximum_percent + 1, max(1, int(step_percent)))
        )
        if not values or values[-1] != maximum_percent:
            values.append(maximum_percent)
        return tuple(round(value / 100.0, 4) for value in values)

    def _configure_dpi_scale(self, snapshot: WindowSnapshot) -> None:
        reported = round(max(48, min(288, int(snapshot.dpi))) / 96.0, 4)
        if self.settings.dpi_scale_mode == "manual":
            preferred = (round(self.settings.dpi_scale_percent / 100.0, 4),)
            fallback = preferred
            source = f"手动 {self.settings.dpi_scale_percent}%"
        else:
            preferred = (reported,)
            scanned = self._auto_scale_range(
                self.settings.dpi_auto_min_percent,
                self.settings.dpi_auto_max_percent,
                self.settings.dpi_auto_step_percent,
            )
            fallback = tuple(dict.fromkeys((reported, *scanned)))
            source = (
                f"Windows {snapshot.dpi} DPI / {reported * 100:.0f}%；"
                f"失败后按 {self.settings.dpi_auto_step_percent}% 间隔扫描 "
                f"{self.settings.dpi_auto_min_percent}%–"
                f"{self.settings.dpi_auto_max_percent}%"
            )
        policy = (preferred, fallback)
        if policy == self._dpi_scale_policy:
            return
        self._dpi_scale_policy = policy
        self.search_box.set_scale_policy(preferred, fallback)
        self.search_result.set_scale_policy(preferred, fallback)
        self.chat_input.set_scale_policy(preferred, fallback)
        setter = getattr(self.chat_layer_recovery, "set_scale_policy", None)
        if callable(setter):
            setter(preferred, fallback)
        self._cached_search_box = None
        self._cached_chat_input = None
        log.info(
            "视觉定位 DPI 策略已应用：%s。",
            source,
            extra={"automation_operation": "window.dpi"},
        )

    @staticmethod
    def _cancelled(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise DesktopMessageError(
                "automation_cancelled",
                "自动化已停止，当前消息没有发送。",
                details={"cancelled": True},
            )

    @staticmethod
    def _screen_rect(frame: CapturedFrame, local: Rect) -> Rect:
        return Rect(
            frame.screen_rect.left + local.left,
            frame.screen_rect.top + local.top,
            frame.screen_rect.left + local.right,
            frame.screen_rect.top + local.bottom,
        )

    @staticmethod
    def _type(keyboard: KeyboardLike, text: str, cancel_event: threading.Event | None) -> None:
        MentionComposer._type(keyboard, text, cancel_event)

    @staticmethod
    def _type_message(
        keyboard: KeyboardLike,
        text: str,
        cancel_event: threading.Event | None,
    ) -> None:
        message_typist = getattr(keyboard, "type_message_text", None)
        if not callable(message_typist):
            MentionComposer._type(keyboard, text, cancel_event)
            return
        try:
            message_typist(text, cancel_event=cancel_event)
        except TypeError as exc:
            if "cancel_event" not in str(exc):
                raise
            message_typist(text)

    def _call_traced(
        self,
        operation: str,
        start_message: str,
        end_message: str,
        callback: Callable[[], Any],
    ) -> Any:
        started = self.trace.begin(operation, start_message)
        try:
            result = callback()
        except DesktopSessionError as exc:
            self.trace.end(
                operation,
                f"{end_message}失败：{exc}",
                started,
                level=logging.ERROR,
            )
            raise DesktopMessageError(
                exc.code,
                str(exc),
                details=exc.details,
            ) from exc
        except Exception as exc:
            self.trace.end(
                operation,
                f"{end_message}失败：{type(exc).__name__}: {exc}",
                started,
                level=logging.ERROR,
            )
            raise
        self.trace.end(operation, end_message, started)
        return result

    def _open_layout_cache(self, snapshot: WindowSnapshot) -> None:
        self._layout_cache_key = None
        self._cached_search_box = None
        self._cached_chat_input = None
        if not self.settings.layout_cache:
            self._layout_cache_status = "disabled"
            self.trace.event(
                "layout.cache",
                "布局缓存已关闭；本次全部使用完整图像定位",
            )
            return
        lookup = self.layout_cache_store.open(snapshot, theme="light")
        self._layout_cache_key = lookup.key
        self._layout_cache_status = lookup.status
        self._cached_search_box = lookup.search_box
        self._cached_chat_input = lookup.chat_input
        descriptions = {
            "hit": "窗口布局未变化，可尝试缓存位置附近验证",
            "moved": "窗口仅发生移动；保留客户端相对坐标并使用当前屏幕位置",
            "invalidated_geometry": "窗口大小或 DPI 已变化；旧布局缓存已失效",
            "invalidated_identity": "微信窗口句柄、进程或窗口类已变化；旧布局缓存已失效",
            "miss": "尚无该微信窗口的布局缓存",
        }
        self.trace.event(
            "layout.cache",
            descriptions.get(lookup.status, f"布局缓存状态：{lookup.status}"),
        )

    def _remember_layout(self, slot: str | None, result: RelativeLocatorResult) -> None:
        if (
            not getattr(getattr(self, "settings", None), "layout_cache", False)
            or slot is None
            or self._layout_cache_key is None
        ):
            return
        self.layout_cache_store.put(self._layout_cache_key, slot, result)
        if slot == "search_box":
            self._cached_search_box = result
        elif slot == "chat_input":
            self._cached_chat_input = result

    def _sync_layout_cache(self, snapshot: WindowSnapshot | None) -> str:
        if (
            not getattr(getattr(self, "settings", None), "layout_cache", False)
            or snapshot is None
        ):
            return "disabled"
        previous_key = self._layout_cache_key
        lookup = self.layout_cache_store.open(snapshot, theme="light")
        self._layout_cache_key = lookup.key
        self._cached_search_box = lookup.search_box
        self._cached_chat_input = lookup.chat_input
        if lookup.status in {"invalidated_geometry", "invalidated_identity"}:
            self._layout_cache_status = lookup.status
            reason = (
                "窗口大小或 DPI 在本次操作中发生变化，缓存已失效并回退完整识别"
                if lookup.status == "invalidated_geometry"
                else "微信窗口句柄或进程在本次操作中发生变化，缓存已失效并回退完整识别"
            )
            self.trace.event("layout.cache", reason, level=logging.WARNING)
        elif lookup.status == "moved":
            self._layout_cache_status = "moved"
            self.trace.event(
                "layout.cache",
                "微信窗口位置已移动；继续使用客户端相对坐标并按当前屏幕位置换算",
            )
        elif previous_key != lookup.key:
            self._layout_cache_status = lookup.status
        return lookup.status

    def _wait_relative(
        self,
        locator: RelativeLocator,
        *,
        timeout: float,
        cancel_event: threading.Event | None,
        label: str,
        cached: RelativeLocatorResult | None = None,
        cache_slot: str | None = None,
        result_ready: Callable[[RelativeLocatorResult], bool] | None = None,
        timeout_message: str | None = None,
    ) -> tuple[CapturedFrame, RelativeLocatorResult]:
        deadline = self.monotonic() + timeout
        last: RelativeLocatorResult | None = None
        operation = f"find.{label}"
        lookup_started = self.trace.begin(
            operation,
            f"开始查找{label}（最多 {timeout:.3f} 秒；截图与模板匹配计入此耗时）",
        )
        attempts = 0
        try:
            while True:
                self._cancelled(cancel_event)
                attempts += 1
                frame = self.session.capture_client()
                cache_status = self._sync_layout_cache(frame.window)
                if cache_status in {"miss", "invalidated_geometry", "invalidated_identity"}:
                    if cache_slot == "search_box":
                        cached = self._cached_search_box
                    elif cache_slot == "chat_input":
                        cached = self._cached_chat_input
                    else:
                        cached = None
                image = frame.image.convert("RGB")
                if self.settings.layout_cache and cached is not None:
                    local_started = self.trace.begin(
                        f"{operation}.cache_near",
                        f"在{label}缓存位置附近做小范围模板验证",
                    )
                    try:
                        last = locator.locate_near(
                            image,
                            cached,
                            skip_optional_anchors=True,
                        )
                    except Exception as exc:
                        last = None
                        self.trace.end(
                            f"{operation}.cache_near",
                            f"{label}局部验证异常，将回退完整识别：{type(exc).__name__}: {exc}",
                            local_started,
                            level=logging.WARNING,
                        )
                    else:
                        self.trace.end(
                            f"{operation}.cache_near",
                            (
                                f"{label}局部验证成功"
                                if last.accepted and last.click_bounds is not None
                                else f"{label}局部验证未通过，将在同一截图完整识别"
                            ),
                            local_started,
                            level=(
                                logging.INFO
                                if last.accepted and last.click_bounds is not None
                                else logging.WARNING
                            ),
                        )
                    cached = None
                    if (
                        last is not None
                        and last.accepted
                        and last.click_bounds is not None
                        and (result_ready is None or result_ready(last))
                    ):
                        record_recognition_snapshot(
                            image,
                            last,
                            label=label,
                            operation=operation,
                        )
                        self._remember_layout(cache_slot, last)
                        self.trace.end(
                            operation,
                            f"{label}查找成功：缓存附近验证，轮询 {attempts} 次",
                            lookup_started,
                        )
                        return frame, last
                last = locator.locate(image, skip_optional_anchors=True)
                if (
                    last.accepted
                    and last.click_bounds is not None
                    and (result_ready is None or result_ready(last))
                ):
                    record_recognition_snapshot(
                        image,
                        last,
                        label=label,
                        operation=operation,
                    )
                    self._remember_layout(cache_slot, last)
                    self.trace.end(
                        operation,
                        f"{label}查找成功：轮询 {attempts} 次",
                        lookup_started,
                    )
                    return frame, last
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    diagnostics: dict[str, Any] = {"target": label, "timeout": timeout}
                    result_was_located_but_not_ready = bool(
                        result_ready is not None
                        and last is not None
                        and last.accepted
                        and last.click_bounds is not None
                        and not result_ready(last)
                    )
                    if last is not None:
                        diagnostics.update(
                            {
                                "accepted_anchors": [
                                    anchor_id
                                    for anchor_id, detection in last.detections.items()
                                    if detection.accepted
                                ],
                                "anchor_scores": {
                                    anchor_id: round(detection.score, 4)
                                    for anchor_id, detection in last.detections.items()
                                },
                                "anchor_templates": {
                                    anchor_id: (
                                        detection.template.name
                                        if detection.template is not None
                                        else None
                                    )
                                    for anchor_id, detection in last.detections.items()
                                },
                                "rejected_alternatives": list(last.rejected_alternatives),
                                "locator_failure_code": last.failure_code,
                                "anchor_candidate_counts": {
                                    anchor_id: len(values)
                                    for anchor_id, values in (
                                        last.anchor_candidates or {}
                                    ).items()
                                },
                                "valid_combination_count": len(last.valid_combinations),
                                "distinct_target_count": len(last.distinct_combinations),
                                "ready_condition_satisfied": not result_was_located_but_not_ready,
                            }
                        )
                        if last.failure_code == "ambiguous_combinations":
                            error_code = "visual_target_ambiguous"
                            error_message = (
                                f"等待 {timeout:.3f} 秒后仍检测到 "
                                f"{len(last.distinct_combinations)} 个指向不同区域的"
                                f"{label}元素组合；为避免误点击已停止。"
                            )
                        elif result_was_located_but_not_ready:
                            error_code = "visual_target_not_ready"
                            error_message = timeout_message or (
                                f"已定位到{label}组合，但等待其进入可操作状态超时；"
                                "未继续操作。"
                            )
                        else:
                            error_code = "visual_target_not_found"
                            error_message = timeout_message or (
                                f"在限定时间内没有定位到{label}，未继续操作。"
                            )
                        snapshot = record_recognition_snapshot(
                            image,
                            last,
                            label=label,
                            operation=operation,
                            force=True,
                            error_message=error_message,
                        )
                        if snapshot:
                            diagnostics["recognition_snapshot_id"] = snapshot["id"]
                    else:
                        error_code = "visual_target_not_found"
                        error_message = timeout_message or (
                            f"在限定时间内没有定位到{label}，未继续操作。"
                        )
                    raise DesktopMessageError(error_code, error_message, details=diagnostics)
                if cancel_event is not None:
                    if cancel_event.wait(min(0.10, remaining)):
                        self._cancelled(cancel_event)
                else:
                    time.sleep(min(0.10, remaining))
        except Exception as exc:
            self.trace.end(
                operation,
                f"{label}查找失败：轮询 {attempts} 次；{type(exc).__name__}: {exc}",
                lookup_started,
                level=logging.ERROR,
            )
            raise

    def _wait_interruptible(
        self,
        operation: str,
        seconds: float,
        *,
        cancel_event: threading.Event | None,
    ) -> None:
        seconds = max(0.0, float(seconds))
        if seconds <= 0:
            self._cancelled(cancel_event)
            return
        started = self.trace.begin(operation, f"等待开始：{seconds:.3f} 秒")
        try:
            if cancel_event is not None:
                if cancel_event.wait(seconds):
                    self._cancelled(cancel_event)
            else:
                time.sleep(seconds)
        except Exception as exc:
            self.trace.end(
                operation,
                f"等待失败：{type(exc).__name__}: {exc}",
                started,
                level=logging.ERROR,
            )
            raise
        self.trace.end(operation, "等待结束", started)

    def _wait_random_traced(
        self,
        operation: str,
        minimum: float,
        maximum: float,
        *,
        cancel_event: threading.Event | None,
    ) -> float:
        started = self.trace.begin(
            operation,
            f"随机等待开始：{minimum:.3f}–{maximum:.3f} 秒",
        )
        try:
            duration = self.interaction.wait_random(
                minimum,
                maximum,
                cancel_event=cancel_event,
            )
        except Exception as exc:
            self.trace.end(
                operation,
                f"随机等待失败：{type(exc).__name__}: {exc}",
                started,
                level=logging.ERROR,
            )
            raise
        self.trace.end(
            operation,
            f"随机等待结束：实际等待 {duration:.3f} 秒",
            started,
        )
        return duration

    def _wait_search_result(
        self,
        *,
        timeout: float,
        minimum_wait: float = 0.0,
        cancel_event: threading.Event | None,
        base: RelativeLocatorResult | None = None,
        base_image_size: tuple[int, int] | None = None,
    ) -> tuple[CapturedFrame, Any]:
        lookup_started_at = self.monotonic()
        deadline = lookup_started_at + timeout
        minimum_deadline = lookup_started_at + max(0.0, minimum_wait)
        last = None
        operation = "find.search_result"
        lookup_started = self.trace.begin(
            operation,
            (
                f"开始查找搜索结果（最多 {timeout:.3f} 秒；查找耗时计入 "
                f"{max(0.0, minimum_wait):.3f} 秒刷新等待预算）"
            ),
        )
        attempts = 0
        try:
            while True:
                self._cancelled(cancel_event)
                attempts += 1
                frame = self.session.capture_client()
                image = frame.image.convert("RGB")
                cache_status = self._sync_layout_cache(frame.window)
                if (
                    cache_status in {"miss", "invalidated_geometry", "invalidated_identity"}
                    or (base_image_size is not None and image.size != base_image_size)
                ):
                    base = self._cached_search_box
                    base_image_size = image.size
                if base is None:
                    last = self.search_result.locate(image)
                else:
                    last = self.search_result.locate_from_base(image, base)
                verified_base = getattr(last, "base", None)
                if (
                    isinstance(verified_base, RelativeLocatorResult)
                    and verified_base.failure_code == "ambiguous_combinations"
                ):
                    snapshot = record_recognition_snapshot(
                        image,
                        verified_base,
                        label="第一条搜索结果",
                        operation=operation,
                        force=True,
                        overview=draw_derived_debug_overlay(image, last),
                        extra_metadata={
                            "derived_rejection": last.rejection_code,
                            "derived_details": dict(last.details),
                        },
                    )
                    raise DesktopMessageError(
                        "visual_target_ambiguous",
                        (
                            "搜索框基础定位检测到多个指向不同区域的元素组合，"
                            "无法安全推算第一条搜索结果。"
                        ),
                        details={
                            "target": "第一条搜索结果",
                            "locator_failure_code": verified_base.failure_code,
                            "valid_combination_count": len(
                                verified_base.valid_combinations
                            ),
                            "distinct_target_count": len(
                                verified_base.distinct_combinations
                            ),
                            "recognition_snapshot_id": (
                                snapshot.get("id") if snapshot else None
                            ),
                        },
                    )
                if last.accepted and isinstance(verified_base, RelativeLocatorResult):
                    self._remember_layout("search_box", verified_base)
                now = self.monotonic()
                if last.accepted and last.click_bounds is not None:
                    if now < minimum_deadline:
                        remaining_budget = min(minimum_deadline - now, deadline - now)
                        self._wait_interruptible(
                            "wait.search_settle",
                            remaining_budget,
                            cancel_event=cancel_event,
                        )
                        now = self.monotonic()
                    if now >= minimum_deadline:
                        if isinstance(verified_base, RelativeLocatorResult):
                            record_recognition_snapshot(
                                image,
                                verified_base,
                                label="第一条搜索结果",
                                operation=operation,
                                overview=draw_derived_debug_overlay(image, last),
                                extra_metadata={
                                    "derived_target": {
                                        "left": last.target.left,
                                        "top": last.target.top,
                                        "right": last.target.right,
                                        "bottom": last.target.bottom,
                                    }
                                },
                            )
                        self.trace.end(
                            operation,
                            f"搜索结果查找成功：轮询 {attempts} 次（刷新等待已计入查找耗时）",
                            lookup_started,
                        )
                        return frame, last
                remaining = deadline - now
                if remaining <= 0:
                    reason_text = {
                        "base_locator_rejected": "搜索框基础锚点未通过联合校验",
                        "source_bounds_missing": "搜索框参考区域缺失",
                        "target_rectangle_invalid": "第一项推算矩形无效",
                        "target_size_out_of_bounds": "第一项推算区域尺寸超出安全范围",
                        "target_out_of_image": "第一项推算区域超出微信窗口",
                    }.get(last.rejection_code, "第一项没有形成安全点击区域")
                    details = {
                        "timeout": timeout,
                        "locator_rejection": last.rejection_code,
                        "locator_details": dict(last.details),
                    }
                    if isinstance(verified_base, RelativeLocatorResult):
                        snapshot = record_recognition_snapshot(
                            image,
                            verified_base,
                            label="第一条搜索结果",
                            operation=operation,
                            force=True,
                            error_message=f"{reason_text}，未形成唯一安全点击区域。",
                            overview=draw_derived_debug_overlay(image, last),
                            extra_metadata={
                                "derived_rejection": last.rejection_code,
                                "derived_details": dict(last.details),
                            },
                        )
                        details.update(
                            {
                                "locator_failure_code": verified_base.failure_code,
                                "anchor_candidate_counts": {
                                    anchor_id: len(values)
                                    for anchor_id, values in (
                                        verified_base.anchor_candidates or {}
                                    ).items()
                                },
                                "valid_combination_count": len(
                                    verified_base.valid_combinations
                                ),
                                "distinct_target_count": len(
                                    verified_base.distinct_combinations
                                ),
                                "recognition_snapshot_id": (
                                    snapshot.get("id") if snapshot else None
                                ),
                            }
                        )
                    raise DesktopMessageError(
                        "search_primary_result_unsafe",
                        f"{reason_text}，未执行鼠标点击或盲按 Enter。",
                        details=details,
                    )
                wait_for = min(0.10, remaining)
                if cancel_event is not None:
                    if cancel_event.wait(wait_for):
                        self._cancelled(cancel_event)
                else:
                    time.sleep(wait_for)
        except Exception as exc:
            self.trace.end(
                operation,
                f"搜索结果查找失败：轮询 {attempts} 次；{type(exc).__name__}: {exc}",
                lookup_started,
                level=logging.ERROR,
            )
            raise

    def _open_chat(
        self,
        target_name: str,
        *,
        cancel_event: threading.Event | None,
        states: list[str],
    ) -> tuple[CapturedFrame, RelativeLocatorResult]:
        recovery = self._call_traced(
            "chat.recover_layer",
            "检查当前是否位于聊天层并按需恢复",
            "聊天层检查完成",
            lambda: self.chat_layer_recovery.recover(cancel_event=cancel_event),
        )
        if recovery.guard_failure_code == "ambiguous_combinations":
            raise DesktopMessageError(
                "visual_target_ambiguous",
                (
                    "返回按钮与聊天工具栏联合守卫检测到多个指向不同区域的"
                    "有效元素组合；为避免误点击，本次发送前流程已停止。"
                ),
                details={
                    "target": "返回按钮与聊天工具栏联合守卫",
                    "locator_failure_code": recovery.guard_failure_code,
                    "recognition_snapshot_id": (
                        recovery.recognition_snapshot_id or None
                    ),
                    "back_button_count": recovery.initial_back_count,
                    "back_click_count": len(recovery.clicked_bounds),
                    "retry_scope": "whole_message_before_send",
                },
            )
        if recovery.initial_back_count:
            states.append("NARROW_CHAT_DETECTED")
            states.extend(
                f"CHAT_BACK_CLICKED_{index}"
                for index in range(1, len(recovery.clicked_bounds) + 1)
            )
        if recovery.exhausted:
            raise DesktopMessageError(
                "chat_layer_exit_failed",
                "已依次点击检测到的返回按钮，但聊天发送栏仍然存在；本次发送前尝试已停止。",
                details={
                    "back_button_count": recovery.initial_back_count,
                    "back_click_count": len(recovery.clicked_bounds),
                    "retry_scope": "whole_message_before_send",
                },
            )
        if recovery.recovered:
            states.append("SEARCH_SHELL_READY")
        frame, search = self._wait_relative(
            self.search_box,
            timeout=self.settings.locate_timeout,
            cancel_event=cancel_event,
            label="微信搜索框",
            cached=self._cached_search_box,
            cache_slot="search_box",
        )
        self._call_traced(
            "click.search_box",
            "点击微信搜索框（包含鼠标移动、点击前停顿与点击后随机等待）",
            "微信搜索框点击完成",
            lambda: self.interaction.click_rect(
                self._screen_rect(frame, search.click_bounds),
                cancel_event=cancel_event,
            ),
        )
        self._call_traced(
            "input.clear_search",
            "清空搜索框",
            "搜索框已清空",
            lambda: (self.keyboard.ctrl_a(), self.keyboard.backspace()),
        )
        self._call_traced(
            "input.type_search_name",
            f"输入会话名称：{target_name}",
            "会话名称输入完成",
            lambda: self._type(self.keyboard, target_name, cancel_event),
        )
        if self.settings.conversation_entry_mode == "keyboard_shortcut":
            self._wait_interruptible(
                "wait.search_settle",
                self.settings.settle,
                cancel_event=cancel_event,
            )
            self._call_traced(
                "input.search_shortcut_up",
                "按一次上方向键选择微信搜索结果",
                "上方向键已执行",
                self.keyboard.up,
            )
            states.append("SEARCH_SHORTCUT_UP")
            self._wait_random_traced(
                "wait.search_shortcut_confirm",
                self.settings.conversation_enter_delay_min,
                self.settings.conversation_enter_delay_max,
                cancel_event=cancel_event,
            )
            self._call_traced(
                "input.search_shortcut_enter",
                "按一次 Enter 进入所选会话",
                "Enter 已执行，等待聊天输入区出现",
                self.keyboard.enter,
            )
            states.append("SEARCH_SHORTCUT_ENTER")
        else:
            result_frame, result = self._wait_search_result(
                timeout=self.settings.locate_timeout,
                minimum_wait=self.settings.settle,
                cancel_event=cancel_event,
                base=search,
                base_image_size=frame.image.size,
            )
            self._call_traced(
                "click.search_result",
                (
                    "使用不稳定备用方案：点击推算出的第一条搜索结果"
                    "（包含鼠标移动、点击前停顿与点击后随机等待）"
                ),
                "备用鼠标点击已完成",
                lambda: self.interaction.click_rect(
                    self._screen_rect(result_frame, result.click_bounds),
                    cancel_event=cancel_event,
                ),
            )
            states.append("SEARCH_RESULT_MOUSE_CLICKED_UNSTABLE")
        chat_frame, chat = self._wait_relative(
            self.chat_input,
            timeout=self.settings.locate_timeout,
            cancel_event=cancel_event,
            label="聊天输入区域",
            cached=self._cached_chat_input,
            cache_slot="chat_input",
        )
        return chat_frame, chat

    def _compose(
        self,
        input_parts: Sequence[tuple[str, str]],
        *,
        target_kind: str,
        cancel_event: threading.Event | None,
    ) -> tuple[list[MentionOutcome], list[dict[str, Any]]]:
        mentions: list[MentionOutcome] = []
        warnings: list[dict[str, Any]] = []
        message_length = sum(len(value) for _mode, value in input_parts)
        long_text_clipboard = (
            self.settings.input_mode == "keyboard"
            and self.settings.keyboard_clipboard_threshold_enabled
            and message_length > self.settings.keyboard_clipboard_threshold_chars
            and any(mode == "text" for mode, _value in input_parts)
        )
        if long_text_clipboard:
            self.trace.event(
                "input.long_text_clipboard",
                (
                    f"消息长度 {message_length} > 阈值 "
                    f"{self.settings.keyboard_clipboard_threshold_chars}，"
                    "普通文本片段改用剪贴板粘贴；真实 @ 仍按原流程处理"
                ),
            )
        composer = MentionComposer(
            session=self.session,
            keyboard=self.keyboard,
            interaction=self.interaction,
            detector=self.popup_detector,
            settings=self.settings,
            monotonic=self.monotonic,
            trace=self.trace,
        )
        for mode, value in input_parts:
            self._cancelled(cancel_event)
            if mode == "mention":
                outcome, warning = composer.compose(
                    value,
                    target_kind=target_kind,
                    cancel_event=cancel_event,
                )
                mentions.append(outcome)
                if warning is not None:
                    warnings.append(warning)
            elif mode in {"text", "keyboard"}:
                use_clipboard = mode == "text" and (
                    self.settings.input_mode == "clipboard" or long_text_clipboard
                )
                started = self.trace.begin(
                    "input.paste_text" if use_clipboard else "input.type_text",
                    (
                        f"剪贴板粘贴文本片段：{len(value)} 个字符"
                        if use_clipboard
                        else f"逐字输入文本片段：{len(value)} 个字符"
                    ),
                )
                try:
                    if use_clipboard:
                        self.clipboard.set_text(value)
                        self._cancelled(cancel_event)
                        self.keyboard.ctrl_v()
                    else:
                        self._type_message(self.keyboard, value, cancel_event)
                except Exception as exc:
                    self.trace.end(
                        "input.paste_text" if use_clipboard else "input.type_text",
                        f"文本片段输入失败：{type(exc).__name__}: {exc}",
                        started,
                        level=logging.ERROR,
                    )
                    if isinstance(exc, ClipboardError):
                        raise DesktopMessageError(
                            "clipboard_failed",
                            str(exc),
                        ) from exc
                    raise
                self.trace.end(
                    "input.paste_text" if use_clipboard else "input.type_text",
                    (
                        "Ctrl+V 已执行；这只代表快捷键已送出，仍需等待微信显示文字并启用发送按钮"
                        if use_clipboard
                        else "字符输入事件已发送；仍需等待微信显示文字并启用发送按钮"
                    ),
                    started,
                )
            else:
                raise DesktopMessageError(
                    "invalid_input_part",
                    f"不支持的输入片段：{mode}",
                )
        return mentions, warnings

    def send_media_once(
        self,
        *,
        target_kind: str,
        target_name: str,
        media_type: str,
        path: str | Path,
        cancel_event: threading.Event | None = None,
    ) -> DesktopMessageResult:
        """Paste one image/file and commit one visible Send-button click.

        The method deliberately returns an unverified success after the click.
        A missing media bubble must never trigger an automatic resend.
        """

        target_kind = str(target_kind or "").strip().lower()
        target_name = str(target_name or "").strip()
        media_type = str(media_type or "").strip().lower()
        media_path = Path(path).resolve()
        if target_kind not in {"private", "group"}:
            raise DesktopMessageError("invalid_target_kind", "会话类型只能是私聊或群聊。")
        if not target_name:
            raise DesktopMessageError("invalid_target", "会话名称不能为空。")
        if media_type not in {"image", "file"}:
            raise DesktopMessageError("unsupported_media_type", "媒体类型只能是图片或文件。")
        if not media_path.is_file():
            raise DesktopMessageError("media_file_not_found", f"媒体文件不存在：{media_path}")

        self.trace = AutomationTrace(monotonic=self.monotonic)
        transaction_started = self.trace.begin(
            "media.transaction",
            f"开始整条媒体自动化：{target_kind} / {target_name} / {media_type}",
        )
        states = ["IDLE"]
        snapshot = self._call_traced(
            "window.prepare",
            "查找、恢复并置前微信窗口",
            "微信窗口已准备就绪",
            lambda: self.session.prepare(
                timeout=self.settings.locate_timeout,
                stable_for=0.15,
                cancel_event=cancel_event,
            ),
        )
        self._configure_dpi_scale(snapshot)
        self._open_layout_cache(snapshot)
        states.append("WINDOW_READY")
        frame, chat = self._open_chat(
            target_name,
            cancel_event=cancel_event,
            states=states,
        )
        states.extend(("SEARCH_TARGET_CLICKED", "CHAT_INPUT_READY"))
        if chat.click_bounds is None:
            raise DesktopMessageError("chat_input_not_found", "聊天输入区域没有安全点击范围。")
        self._call_traced(
            "click.chat_input",
            "点击聊天输入区域（包含鼠标移动、点击前停顿与点击后随机等待）",
            "聊天输入区域点击完成",
            lambda: self.interaction.click_rect(
                self._screen_rect(frame, chat.click_bounds),
                cancel_event=cancel_event,
            ),
        )
        self._call_traced(
            "input.clear_message",
            "清空聊天输入区域，避免混入未发送内容",
            "聊天输入区域已清空",
            lambda: (self.keyboard.ctrl_a(), self.keyboard.backspace()),
        )
        self._cancelled(cancel_event)
        self._wait_interruptible(
            "wait.media_input_ready",
            0.08,
            cancel_event=cancel_event,
        )
        clipboard_started = self.trace.begin(
            "media.clipboard_write",
            f"把{('图片' if media_type == 'image' else '文件')}写入 Windows 剪贴板并校验格式",
        )
        try:
            if media_type == "image":
                self.clipboard.set_image(media_path)
            else:
                self.clipboard.set_files((media_path,))
        except ClipboardError as exc:
            self.trace.end(
                "media.clipboard_write",
                f"媒体写入剪贴板失败：{exc}",
                clipboard_started,
                level=logging.ERROR,
            )
            raise DesktopMessageError("clipboard_failed", str(exc)) from exc
        self.trace.end(
            "media.clipboard_write",
            "媒体已写入剪贴板，并确认目标格式仍然可用",
            clipboard_started,
        )
        self._wait_interruptible(
            "wait.clipboard_ready",
            0.05,
            cancel_event=cancel_event,
        )
        paste_started = self.trace.begin(
            "media.paste_shortcut",
            "向已聚焦的微信聊天输入区域执行 Ctrl+V",
        )
        try:
            self._cancelled(cancel_event)
            self.keyboard.ctrl_v()
        except Exception as exc:
            self.trace.end(
                "media.paste_shortcut",
                f"Ctrl+V 执行失败：{type(exc).__name__}: {exc}",
                paste_started,
                level=logging.ERROR,
            )
            raise DesktopMessageError(
                "media_paste_shortcut_failed",
                f"媒体已写入剪贴板，但 Ctrl+V 执行失败：{exc}",
            ) from exc
        self.trace.end(
            "media.paste_shortcut",
            "Ctrl+V 已执行；这只代表快捷键已送出，仍需等待微信启用发送按钮",
            paste_started,
        )
        states.append("MEDIA_PASTED")
        self._wait_interruptible(
            "wait.media_preview",
            self.settings.settle,
            cancel_event=cancel_event,
        )

        send_frame, located = self._wait_relative(
            self.chat_input,
            timeout=self.settings.locate_timeout,
            cancel_event=cancel_event,
            label="媒体粘贴后变为可用的发送按钮和聊天工具栏",
            cached=None,
            cache_slot="chat_input",
            result_ready=_enabled_send_button_with_toolbar,
            timeout_message=(
                "等待微信显示已粘贴的图片或文件并启用发送按钮超时；"
                "未执行发送，将按整条消息重试设置处理。"
            ),
        )
        send_detection = located.detections.get("send_button")
        if (
            send_detection is None
            or not send_detection.accepted
            or send_detection.bounds is None
            or send_detection.template is None
        ):
            raise DesktopMessageError("send_button_not_found", "媒体粘贴后没有定位到可用发送按钮。")
        send_bounds = self._screen_rect(send_frame, send_detection.bounds)
        self._cancelled(cancel_event)
        self._wait_random_traced(
            "wait.pre_send_review",
            self.settings.send_review_delay_min,
            self.settings.send_review_delay_max,
            cancel_event=cancel_event,
        )
        states.append("SEND_COMMITTED")
        try:
            self._call_traced(
                "click.send_button",
                "点击微信发送按钮（包含鼠标移动、点击前停顿与点击后随机等待）",
                "发送按钮点击完成",
                lambda: self.interaction.click_rect(send_bounds, cancel_event=cancel_event),
            )
        except Exception as exc:
            raise DesktopMessageError(
                "send_committed_unverified",
                "媒体发送动作已经开始，但无法确认点击后的状态；不会自动重试。",
                details={
                    "send_committed": True,
                    "send_clicked": True,
                    "media_type": media_type,
                    "error_type": type(exc).__name__,
                },
            ) from exc
        states.append("SENT_UNVERIFIED")
        self.trace.end(
            "media.transaction",
            "媒体发送动作已提交，最终送达未验证",
            transaction_started,
        )
        return DesktopMessageResult(
            code="media_sent_unverified",
            states=tuple(states),
            mentions=(),
            warnings=(),
            send_committed=True,
            send_button_bounds=send_bounds,
            details={
                "media_type": media_type,
                "path_name": media_path.name,
                "ui_verified": False,
                "server_delivery_confirmed": False,
                "layout_cache": {
                    "enabled": self.settings.layout_cache,
                    "window_status": self._layout_cache_status,
                },
            },
        )

    def send_once(
        self,
        *,
        target_kind: str,
        target_name: str,
        text: str,
        input_parts: Sequence[tuple[str, str]] = (),
        cancel_event: threading.Event | None = None,
    ) -> DesktopMessageResult:
        target_kind = str(target_kind or "").strip().lower()
        target_name = str(target_name or "").strip()
        if target_kind not in {"private", "group"}:
            raise DesktopMessageError("invalid_target_kind", "会话类型只能是私聊或群聊。")
        if not target_name:
            raise DesktopMessageError("invalid_target", "会话名称不能为空。")
        if not isinstance(text, str) or not text:
            raise DesktopMessageError("invalid_text", "待发送消息不能为空。")
        parts = tuple(input_parts) or (("text", text),)
        if "".join(value for _mode, value in parts) != text:
            raise DesktopMessageError("invalid_input_parts", "输入片段拼接结果与消息正文不一致。")

        self.trace = AutomationTrace(monotonic=self.monotonic)
        transaction_started = self.trace.begin(
            "message.transaction",
            f"开始整条消息自动化：{target_kind} / {target_name} / 文本长度 {len(text)}",
        )
        states = ["IDLE"]
        snapshot = self._call_traced(
            "window.prepare",
            "查找、恢复并置前微信窗口",
            "微信窗口已准备就绪",
            lambda: self.session.prepare(
                timeout=self.settings.locate_timeout,
                stable_for=0.15,
                cancel_event=cancel_event,
            ),
        )
        self._configure_dpi_scale(snapshot)
        self._open_layout_cache(snapshot)
        states.append("WINDOW_READY")
        frame, chat = self._open_chat(
            target_name,
            cancel_event=cancel_event,
            states=states,
        )
        states.extend(("SEARCH_TARGET_CLICKED", "CHAT_INPUT_READY"))
        if chat.click_bounds is None:
            raise DesktopMessageError("chat_input_not_found", "聊天输入区域没有安全点击范围。")
        self._call_traced(
            "click.chat_input",
            "点击聊天输入区域（包含鼠标移动、点击前停顿与点击后随机等待）",
            "聊天输入区域点击完成",
            lambda: self.interaction.click_rect(
                self._screen_rect(frame, chat.click_bounds),
                cancel_event=cancel_event,
            ),
        )
        self._call_traced(
            "input.clear_message",
            "清空聊天输入区域",
            "聊天输入区域已清空",
            lambda: (self.keyboard.ctrl_a(), self.keyboard.backspace()),
        )
        states.append("COMPOSING")
        mentions, warnings = self._compose(
            parts,
            target_kind=target_kind,
            cancel_event=cancel_event,
        )
        self._cancelled(cancel_event)

        line_break_pressed = False
        if self.settings.append_line_break_after_input:
            try:
                self._call_traced(
                    "input.append_line_break",
                    "输入完成后按 Enter 换行，使微信表情候选浮层收起",
                    "已在消息末尾增加一个换行；后续仍由鼠标点击发送按钮",
                    self.keyboard.enter,
                )
            except Exception as exc:
                raise DesktopMessageError(
                    "line_break_state_unverified",
                    "Enter 换行动作已经开始，但无法确认微信是否把它当成发送；为避免重复消息，本条消息不会整体重试。",
                    details={
                        "send_committed": True,
                        "send_clicked": False,
                        "line_break_requested": True,
                        "line_break_confirmed": False,
                    },
                ) from exc
            line_break_pressed = True
            states.append("LINE_BREAK_APPENDED")

        try:
            send_frame, located = self._wait_relative(
                self.chat_input,
                timeout=self.settings.locate_timeout,
                cancel_event=cancel_event,
                label="输入完成后变为可用的发送按钮和聊天工具栏",
                cached=chat,
                cache_slot="chat_input",
                result_ready=_enabled_send_button_with_toolbar,
                timeout_message=(
                    "等待微信显示输入内容并启用发送按钮超时；"
                    "未执行发送，将按整条消息重试设置处理。"
                ),
            )
        except Exception as exc:
            if line_break_pressed:
                raise DesktopMessageError(
                    "line_break_state_unverified",
                    "按 Enter 换行后没有重新定位到可用发送按钮。微信若仍是 Enter 发送，消息可能已经提前发出；为避免刷屏，本条消息不会整体重试。",
                    details={
                        "send_committed": True,
                        "send_clicked": False,
                        "line_break_requested": True,
                        "line_break_confirmed": True,
                    },
                ) from exc
            raise
        send_detection = located.detections.get("send_button")
        if (
            send_detection is None
            or not send_detection.accepted
            or send_detection.bounds is None
            or send_detection.template is None
        ):
            raise DesktopMessageError("send_button_not_found", "发送按钮定位失败，未执行发送。")
        states.append("READY_TO_SEND")
        send_bounds = self._screen_rect(send_frame, send_detection.bounds)
        self._wait_random_traced(
            "wait.pre_send_review",
            self.settings.send_review_delay_min,
            self.settings.send_review_delay_max,
            cancel_event=cancel_event,
        )
        states.append("SEND_COMMITTED")
        try:
            self._call_traced(
                "click.send_button",
                "点击微信发送按钮（包含鼠标移动、点击前停顿与点击后随机等待）",
                "发送按钮点击完成",
                lambda: self.interaction.click_rect(send_bounds, cancel_event=cancel_event),
            )
        except Exception as exc:
            raise DesktopMessageError(
                "send_committed_unverified",
                "发送按钮动作已经开始，但无法确认点击后的状态；不会自动重试。",
                details={
                    "send_committed": True,
                    "send_clicked": True,
                    "error_type": type(exc).__name__,
                },
            ) from exc
        states.append("SENT_UNVERIFIED")
        self.trace.end(
            "message.transaction",
            "整条消息自动化完成：发送动作已提交，最终送达未验证",
            transaction_started,
        )
        return DesktopMessageResult(
            code="sent_unverified",
            states=tuple(states),
            mentions=tuple(mentions),
            warnings=tuple(warnings),
            send_committed=True,
            send_button_bounds=send_bounds,
            details={
                "ui_verified": False,
                "server_delivery_confirmed": False,
                "input_mode_used": self.settings.input_mode,
                "line_break_after_input": line_break_pressed,
                "mention_downgrade_count": sum(1 for item in mentions if not item.real),
                "layout_cache": {
                    "enabled": self.settings.layout_cache,
                    "window_status": self._layout_cache_status,
                },
            },
        )


__all__ = [
    "AutomationTrace",
    "DesktopMessageError",
    "DesktopMessageResult",
    "DesktopMessageSettings",
    "MentionComposer",
    "MentionOutcome",
    "VisualDesktopMessageSender",
]
