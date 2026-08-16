"""Bounded Win32 detection for WeChat's transient @ candidate popup.

The detector deliberately does not require one exact Qt class name.  It uses
the popup's lifecycle, owning process, owner relationship, visible geometry
and the current class/title only as combined evidence.  It never reads WeChat
memory, traverses the UIA desktop, clicks, or types.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence

from .models import Rect


class PopupWindowApi(Protocol):
    def enum_windows(self) -> Sequence[int]: ...
    def is_visible(self, handle: int) -> bool: ...
    def process_id(self, handle: int) -> int: ...
    def owner(self, handle: int) -> int: ...
    def class_name(self, handle: int) -> str: ...
    def title(self, handle: int) -> str: ...
    def rectangle(self, handle: int) -> Rect: ...


class Win32PopupWindowApi:
    def __init__(self) -> None:
        try:
            import win32con
            import win32gui
            import win32process
        except ImportError as exc:  # pragma: no cover - runtime dependency
            raise RuntimeError("缺少 pywin32，无法检测微信 @ 候选框。") from exc
        self.win32con = win32con
        self.win32gui = win32gui
        self.win32process = win32process

    def enum_windows(self) -> Sequence[int]:
        handles: list[int] = []

        def collect(handle: int, _extra: Any) -> bool:
            handles.append(int(handle))
            return True

        self.win32gui.EnumWindows(collect, None)
        return handles

    def is_visible(self, handle: int) -> bool:
        return bool(self.win32gui.IsWindowVisible(handle))

    def process_id(self, handle: int) -> int:
        _thread, pid = self.win32process.GetWindowThreadProcessId(handle)
        return int(pid)

    def owner(self, handle: int) -> int:
        return int(self.win32gui.GetWindow(handle, self.win32con.GW_OWNER) or 0)

    def class_name(self, handle: int) -> str:
        return str(self.win32gui.GetClassName(handle) or "")

    def title(self, handle: int) -> str:
        return str(self.win32gui.GetWindowText(handle) or "")

    def rectangle(self, handle: int) -> Rect:
        left, top, right, bottom = self.win32gui.GetWindowRect(handle)
        return Rect(int(left), int(top), int(right), int(bottom))


@dataclass(frozen=True)
class MentionPopup:
    handle: int
    rectangle: Rect
    class_name: str
    title: str
    owner: int
    score: int


class MentionPopupDetector:
    """Find the one logical popup created after an @ composition starts."""

    def __init__(
        self,
        *,
        api: PopupWindowApi | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        poll_interval: float = 0.05,
    ) -> None:
        if not 0.01 <= poll_interval <= 1.0:
            raise ValueError("候选框检测间隔必须在 0.01 到 1 秒之间。")
        self.api = api or Win32PopupWindowApi()
        self.sleep = sleep
        self.monotonic = monotonic
        self.poll_interval = float(poll_interval)

    def visible_same_process_handles(self, main_handle: int, process_id: int) -> set[int]:
        result: set[int] = set()
        for handle in self.api.enum_windows():
            try:
                if (
                    int(handle) != int(main_handle)
                    and self.api.is_visible(handle)
                    and self.api.process_id(handle) == int(process_id)
                ):
                    result.add(int(handle))
            except Exception:
                continue
        return result

    @staticmethod
    def _near_main(popup: Rect, main: Rect) -> bool:
        horizontal_gap = max(main.left - popup.right, popup.left - main.right, 0)
        vertical_gap = max(main.top - popup.bottom, popup.top - main.bottom, 0)
        return horizontal_gap <= 280 and vertical_gap <= 180

    def candidates(
        self,
        *,
        main_handle: int,
        process_id: int,
        main_rectangle: Rect,
        baseline_handles: set[int],
    ) -> tuple[MentionPopup, ...]:
        matches: list[MentionPopup] = []
        for handle in self.api.enum_windows():
            try:
                handle = int(handle)
                if handle == int(main_handle) or handle in baseline_handles:
                    continue
                if not self.api.is_visible(handle):
                    continue
                if self.api.process_id(handle) != int(process_id):
                    continue
                rectangle = self.api.rectangle(handle)
                if not 90 <= rectangle.width <= 460 or not 28 <= rectangle.height <= 460:
                    continue
                owner = self.api.owner(handle)
                class_name = self.api.class_name(handle)
                title = self.api.title(handle)
                score = 2  # new visible same-process bounded window
                if owner == int(main_handle):
                    score += 4
                if "qwindowtoolsavebits" in class_name.casefold():
                    score += 3
                elif "popup" in class_name.casefold() or "tool" in class_name.casefold():
                    score += 1
                if title.strip().casefold() in {"weixin", "wechat", "微信"}:
                    score += 1
                if self._near_main(rectangle, main_rectangle):
                    score += 1
                # Owner + lifecycle is enough if a future WeChat update changes
                # its Qt class.  Without the owner relationship, require the
                # current generic Qt popup evidence as well.
                if score < 7:
                    continue
                matches.append(
                    MentionPopup(
                        handle=handle,
                        rectangle=rectangle,
                        class_name=class_name,
                        title=title,
                        owner=owner,
                        score=score,
                    )
                )
            except Exception:
                continue
        return tuple(sorted(matches, key=lambda item: (-item.score, item.rectangle.top, item.handle)))

    def best_candidate(self, **kwargs: Any) -> MentionPopup | None:
        candidates = self.candidates(**kwargs)
        if not candidates:
            return None
        if len(candidates) > 1 and candidates[0].score == candidates[1].score:
            return None
        return candidates[0]

    @staticmethod
    def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("自动化已停止，未继续等待 @ 候选框。")

    def wait_for_candidate(
        self,
        *,
        timeout: float,
        cancel_event: threading.Event | None = None,
        **kwargs: Any,
    ) -> MentionPopup | None:
        deadline = self.monotonic() + max(0.0, float(timeout))
        while True:
            self._raise_if_cancelled(cancel_event)
            candidate = self.best_candidate(**kwargs)
            if candidate is not None:
                return candidate
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                return None
            wait = min(self.poll_interval, remaining)
            if cancel_event is not None:
                if cancel_event.wait(wait):
                    self._raise_if_cancelled(cancel_event)
            else:
                self.sleep(wait)

    def wait_until_absent(
        self,
        *,
        timeout: float,
        cancel_event: threading.Event | None = None,
        **kwargs: Any,
    ) -> bool:
        deadline = self.monotonic() + max(0.0, float(timeout))
        while True:
            self._raise_if_cancelled(cancel_event)
            if self.best_candidate(**kwargs) is None:
                return True
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                return False
            wait = min(self.poll_interval, remaining)
            if cancel_event is not None:
                if cancel_event.wait(wait):
                    self._raise_if_cancelled(cancel_event)
            else:
                self.sleep(wait)


__all__ = [
    "MentionPopup",
    "MentionPopupDetector",
    "PopupWindowApi",
    "Win32PopupWindowApi",
]
