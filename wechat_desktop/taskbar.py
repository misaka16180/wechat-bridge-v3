"""Activate one visible WeChat taskbar button with an ordinary mouse click.

Windows may reject ``SetForegroundWindow`` when a long-running background
bridge does not own the foreground-input privilege.  In that narrow case the
taskbar button is a documented, visible user-interface fallback.  UI
Automation is used read-only to obtain the button rectangle; activation is an
ordinary ``SendInput`` mouse click.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence

from .interaction import InteractionCancelled, RandomizedInteraction
from .models import Rect
from .tray import (
    TrayActivationError,
    TrayNode,
    UiaTrayAccessibility,
    is_taskbar_button_source,
    is_wechat_shell_name,
)


log = logging.getLogger("wechat_automation.taskbar")


class TaskbarActivationError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


@dataclass(frozen=True)
class TaskbarActivationResult:
    name: str
    bounds: Rect
    source: str


class TaskbarAccessibility(Protocol):
    def scan(self, area: str) -> Sequence[TrayNode]:
        """Return visible Windows shell nodes below the main taskbar root."""


class WeChatTaskbarActivator:
    """Click only one explicit WeChat task-list button; reject ambiguity."""

    def __init__(
        self,
        *,
        accessibility: TaskbarAccessibility | None = None,
        interaction: RandomizedInteraction | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.accessibility = accessibility or UiaTrayAccessibility()
        self.interaction = interaction or RandomizedInteraction()
        self.monotonic = monotonic

    @staticmethod
    def _cancelled(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise TaskbarActivationError(
                "automation_cancelled",
                "自动化已停止，未继续操作 Windows 任务栏。",
            )

    @staticmethod
    def _dedupe(nodes: Sequence[TrayNode]) -> list[TrayNode]:
        unique: dict[tuple[str, int, int, int, int], TrayNode] = {}
        for node in nodes:
            key = (
                node.name.casefold(),
                node.bounds.left,
                node.bounds.top,
                node.bounds.right,
                node.bounds.bottom,
            )
            unique.setdefault(key, node)
        return list(unique.values())

    @staticmethod
    def _candidate_details(candidates: Sequence[TrayNode]) -> list[dict[str, Any]]:
        return [
            {
                "name": node.name,
                "source": node.source,
                "bounds": [
                    node.bounds.left,
                    node.bounds.top,
                    node.bounds.right,
                    node.bounds.bottom,
                ],
            }
            for node in candidates
        ]

    def candidates(self) -> list[TrayNode]:
        nodes = tuple(self.accessibility.scan("main"))
        return self._dedupe(
            [
                node
                for node in nodes
                if is_taskbar_button_source(node.source)
                and is_wechat_shell_name(node.name)
            ]
        )

    def activate(
        self,
        *,
        timeout: float = 2.0,
        cancel_event: threading.Event | None = None,
        still_needed: Callable[[], bool] | None = None,
    ) -> TaskbarActivationResult:
        if not 0.1 <= float(timeout) <= 30:
            raise ValueError("任务栏唤醒等待时间必须在 0.1 到 30 秒之间。")
        self._cancelled(cancel_event)
        started = self.monotonic()
        try:
            candidates = self.candidates()
        except TaskbarActivationError:
            raise
        except TrayActivationError as exc:
            raise TaskbarActivationError(
                "wechat_taskbar_accessibility_unavailable",
                "Windows 没有向程序公开可读取的任务栏按钮，无法执行任务栏兜底。",
                details={
                    "cause_code": exc.code,
                    "cause_message": str(exc),
                    "cause_details": exc.details,
                },
            ) from exc
        except Exception as exc:
            raise TaskbarActivationError(
                "wechat_taskbar_accessibility_unavailable",
                "读取 Windows 任务栏按钮时发生错误，未执行任何点击。",
                details={
                    "error": type(exc).__name__,
                    "message": str(exc).strip()[:300],
                },
            ) from exc
        if len(candidates) > 1:
            raise TaskbarActivationError(
                "wechat_taskbar_button_ambiguous",
                "任务栏中检测到多个微信运行按钮，无法安全判断应点击哪一个。",
                details={
                    "candidate_count": len(candidates),
                    "candidates": self._candidate_details(candidates),
                },
            )
        if not candidates:
            raise TaskbarActivationError(
                "wechat_taskbar_button_not_found",
                "没有在 Windows 任务栏中找到唯一的微信运行按钮。",
                details={"candidate_count": 0},
            )
        self._cancelled(cancel_event)
        if still_needed is not None and not still_needed():
            raise TaskbarActivationError(
                "wechat_taskbar_activation_no_longer_needed",
                "微信已自行回到前台，未再点击任务栏按钮。",
            )
        candidate = candidates[0]
        try:
            point = self.interaction.click_rect(
                candidate.bounds,
                horizontal_ratio=0.20,
                vertical_ratio=0.20,
                cancel_event=cancel_event,
            )
        except InteractionCancelled as exc:
            raise TaskbarActivationError("automation_cancelled", str(exc)) from exc
        except Exception as exc:
            raise TaskbarActivationError(
                "wechat_taskbar_click_failed",
                "鼠标未能单击 Windows 任务栏中的微信按钮。",
                details={
                    "error": type(exc).__name__,
                    "message": str(exc).strip()[:300],
                    "candidate": self._candidate_details((candidate,))[0],
                },
            ) from exc
        log.info(
            "已通过可见鼠标单击微信任务栏按钮：%s，坐标=(%s, %s)，耗时=%s ms。",
            candidate.name,
            point.x,
            point.y,
            max(0, int(round((self.monotonic() - started) * 1000))),
            extra={"automation_operation": "taskbar.wechat_click"},
        )
        return TaskbarActivationResult(
            name=candidate.name,
            bounds=candidate.bounds,
            source=candidate.source,
        )
