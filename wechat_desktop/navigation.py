"""Guarded, no-send conversation search for the v3 desktop backend.

This module may open search and click the primary result row located from the
visible layout.  It has no API for entering a message or pressing/clicking
Send.  Image detection supplies screen geometry and state guards only; it does
not read or identify the result text.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol, Sequence

from .interaction import BoundedListScroller, RandomizedInteraction
from .models import CapturedFrame, Rect, VisualMatch
from .perception import VisualWaiter
from .session import WeChatWindowSession


class SearchNavigationError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


class SearchTextInput(Protocol):
    def open_search(self, *, cancel_event: threading.Event | None = None) -> None:
        ...

    def type_search_text(
        self,
        text: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> None:
        ...


@dataclass(frozen=True)
class SearchResultObservation:
    clickable_rows: Sequence[VisualMatch]
    list_bounds: Rect
    can_scroll: bool
    scroll_direction: int = -1


class SearchPerception(Protocol):
    def search_panel(self, frame: CapturedFrame) -> VisualMatch:
        ...

    def result_list(self, frame: CapturedFrame) -> VisualMatch:
        ...

    def search_results(
        self,
        frame: CapturedFrame,
    ) -> SearchResultObservation:
        ...

    def chat_ready(
        self,
        frame: CapturedFrame,
    ) -> VisualMatch:
        ...


@dataclass(frozen=True)
class SearchNavigationResult:
    code: str
    target_name: str
    target_kind: str
    selected_bounds: Rect
    scroll_steps: int
    states: tuple[str, ...]


class NoSendSearchNavigator:
    """Type the full query, then click its primary visible row, never Enter."""

    VALID_KINDS = {"private", "group"}

    def __init__(
        self,
        *,
        session: WeChatWindowSession,
        search_input: SearchTextInput,
        perception: SearchPerception,
        interaction: RandomizedInteraction,
        waiter: VisualWaiter | None = None,
        minimum_confidence: float = 0.85,
        max_scroll_steps: int = 6,
        max_scroll_distance: int = 900,
    ) -> None:
        if not 0.0 < minimum_confidence <= 1.0:
            raise ValueError("搜索最低置信度必须位于 0（不含）到 1（含）之间。")
        self.session = session
        self.search_input = search_input
        self.perception = perception
        self.interaction = interaction
        self.waiter = waiter or VisualWaiter(session.capture_client)
        self.minimum_confidence = minimum_confidence
        self.max_scroll_steps = max_scroll_steps
        self.max_scroll_distance = max_scroll_distance

    def _confident_rows(
        self,
        observation: SearchResultObservation,
    ) -> list[VisualMatch]:
        low_confidence = [
            item
            for item in observation.clickable_rows
            if item.bounds is not None and item.confidence < self.minimum_confidence
        ]
        if low_confidence:
            raise SearchNavigationError(
                "search_result_row_low_confidence",
                "搜索结果行的位置不够稳定，未执行鼠标点击。",
                details={"candidate_count": len(low_confidence)},
            )
        rows = [
            item
            for item in observation.clickable_rows
            if item.bounds is not None and item.confidence >= self.minimum_confidence
        ]
        return sorted(rows, key=lambda item: (item.bounds.top, item.bounds.left))

    def _observe_results(self) -> tuple[SearchResultObservation, list[VisualMatch]]:
        observation = self.perception.search_results(self.session.capture_client())
        rows = self._confident_rows(observation)
        return observation, rows

    def open_chat(
        self,
        target_name: str,
        target_kind: str,
        *,
        timeout: float = 5.0,
        cancel_event: threading.Event | None = None,
    ) -> SearchNavigationResult:
        target_name = str(target_name or "").strip()
        target_kind = str(target_kind or "").strip().lower()
        if not target_name:
            raise SearchNavigationError("invalid_target", "会话名称不能为空。")
        if target_kind not in self.VALID_KINDS:
            raise SearchNavigationError(
                "invalid_target_kind",
                "会话类型只能是私聊或群聊。",
            )
        states = ["IDLE"]
        self.session.prepare(
            timeout=timeout,
            stable_for=0.15,
            cancel_event=cancel_event,
        )
        states.append("WINDOW_READY")

        self.search_input.open_search(cancel_event=cancel_event)
        self.waiter.wait_for_stable_match(
            self.perception.search_panel,
            timeout=timeout,
            minimum_confidence=self.minimum_confidence,
            cancel_event=cancel_event,
        )
        states.append("SEARCH_OPEN")
        self.interaction.wait_after_state(cancel_event)

        self.search_input.type_search_text(target_name, cancel_event=cancel_event)
        self.waiter.wait_for_stable_match(
            self.perception.result_list,
            timeout=timeout,
            minimum_confidence=self.minimum_confidence,
            cancel_event=cancel_event,
        )
        self.interaction.wait_after_state(cancel_event)
        observation, rows = self._observe_results()
        scroller = BoundedListScroller(
            self.interaction,
            observation.list_bounds,
            max_steps=self.max_scroll_steps,
            max_accumulated_distance=self.max_scroll_distance,
        )
        while not rows:
            if not observation.can_scroll:
                raise SearchNavigationError(
                    "search_result_not_found",
                    "搜索结果区域中没有出现可点击行。",
                )
            action = scroller.scroll_once(
                observation.scroll_direction,
                cancel_event=cancel_event,
            )
            if not action.performed:
                raise SearchNavigationError(
                    "search_scroll_limit",
                    "在安全滚动上限内仍未出现可点击结果行。",
                    details={
                        "scroll_steps": action.step,
                        "scroll_distance": action.accumulated_distance,
                    },
                )
            self.waiter.wait_for_stable_match(
                self.perception.result_list,
                timeout=timeout,
                minimum_confidence=self.minimum_confidence,
                cancel_event=cancel_event,
            )
            self.interaction.wait_after_state(cancel_event)
            try:
                observation, rows = self._observe_results()
            except Exception:
                scroller.record_recheck(target_visible=True)
                raise
            scroller.record_recheck(target_visible=bool(rows))

        selected = rows[0]
        if selected.bounds is None:  # guarded by _confident_rows
            raise AssertionError("已确认的搜索结果缺少屏幕矩形。")
        states.append("SEARCH_CLICK_TARGET_READY")
        selected_bounds = selected.bounds
        self.interaction.click_rect(selected_bounds, cancel_event=cancel_event)
        self.waiter.wait_for_stable_match(
            self.perception.chat_ready,
            timeout=timeout,
            minimum_confidence=self.minimum_confidence,
            cancel_event=cancel_event,
        )
        states.append("CHAT_READY")
        return SearchNavigationResult(
            code="chat_opened_no_send",
            target_name=target_name,
            target_kind=target_kind,
            selected_bounds=selected_bounds,
            scroll_steps=scroller.steps,
            states=tuple(states),
        )
