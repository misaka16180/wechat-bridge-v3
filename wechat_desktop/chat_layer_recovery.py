"""Recover from WeChat's narrow chat layer before locating global search.

A back-button image is never sufficient authority to click.  The inexpensive
top strip is checked first; only when a Back candidate exists does the sender
run the more expensive chat-toolbar guard.  Both signals are still required
before any click is allowed.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from PIL import Image, ImageDraw

from .models import CapturedFrame, Rect
from .recognition_snapshot import record_recognition_snapshot
from .relative_locator import RelativeLocatorResult, draw_debug_overlay
from .template_matching import OpenCVTemplateMatcher


log = logging.getLogger("wechat_automation.target")


def _trace(operation: str, message: str, *, duration_ms: int | None = None, level: int = logging.INFO) -> None:
    extra: dict[str, Any] = {"automation_operation": operation}
    if duration_ms is not None:
        extra["automation_duration_ms"] = int(duration_ms)
    log.log(level, message, extra=extra)


class ChatLayerRecoveryConfigError(ValueError):
    """The narrow-chat recovery locator is malformed or incomplete."""


@dataclass(frozen=True)
class BackButtonTemplate:
    path: Path
    minimum_score: float
    coarse_step: int


@dataclass(frozen=True)
class BackButtonSpec:
    source: Path
    theme: str
    roi: tuple[float, float, float, float]
    max_candidates: int
    templates: tuple[BackButtonTemplate, ...]


@dataclass(frozen=True)
class BackButtonCandidate:
    bounds: Rect
    score: float
    template: Path
    scale: float = 1.0


@dataclass(frozen=True)
class ChatLayerRecoveryResult:
    chat_detected: bool
    initial_back_count: int
    clicked_bounds: tuple[Rect, ...]
    recovered: bool
    exhausted: bool
    guard_failure_code: str = ""
    recognition_snapshot_id: str = ""


class CaptureSession(Protocol):
    def capture_client(self) -> CapturedFrame: ...


class ChatLocator(Protocol):
    def locate(self, image: Image.Image, *, skip_optional_anchors: bool = False) -> Any: ...


class ClickInteraction(Protocol):
    def click_rect(
        self,
        bounds: Rect,
        *,
        cancel_event: threading.Event | None = None,
    ) -> Any: ...

    def wait_after_state(self, cancel_event: threading.Event | None = None) -> float: ...


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ChatLayerRecoveryConfigError(f"{label} 必须是数字。")
    return float(value)


def load_back_button_spec(path: str | Path) -> BackButtonSpec:
    source = Path(path).resolve()
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ChatLayerRecoveryConfigError(f"返回按钮定位文件不存在：{source}") from exc
    except json.JSONDecodeError as exc:
        raise ChatLayerRecoveryConfigError(
            f"返回按钮定位文件无效：{source}（第 {exc.lineno} 行）"
        ) from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ChatLayerRecoveryConfigError("返回按钮定位文件版本无效。")
    theme = str(data.get("theme", "")).strip()
    if theme != "light":
        raise ChatLayerRecoveryConfigError("v3 当前只允许加载浅色模式返回按钮。")
    raw_roi = data.get("roi")
    if not isinstance(raw_roi, list) or len(raw_roi) != 4:
        raise ChatLayerRecoveryConfigError("返回按钮 roi 必须包含四个比例。")
    roi = tuple(_number(item, f"roi[{index}]") for index, item in enumerate(raw_roi))
    if not all(0.0 <= item <= 1.0 for item in roi):
        raise ChatLayerRecoveryConfigError("返回按钮 roi 必须位于 0 到 1 之间。")
    if roi[0] >= roi[2] or roi[1] >= roi[3]:
        raise ChatLayerRecoveryConfigError("返回按钮 roi 范围无效。")
    max_candidates = data.get("max_candidates", 8)
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int):
        raise ChatLayerRecoveryConfigError("max_candidates 必须是整数。")
    if not 1 <= max_candidates <= 16:
        raise ChatLayerRecoveryConfigError("max_candidates 必须在 1 到 16 之间。")
    raw_templates = data.get("templates")
    if not isinstance(raw_templates, list) or not raw_templates:
        raise ChatLayerRecoveryConfigError("返回按钮模板不能为空。")
    templates: list[BackButtonTemplate] = []
    for index, raw in enumerate(raw_templates):
        if not isinstance(raw, dict):
            raise ChatLayerRecoveryConfigError(f"templates[{index}] 必须是对象。")
        relative = str(raw.get("path", "")).strip()
        if not relative:
            raise ChatLayerRecoveryConfigError(f"templates[{index}].path 不能为空。")
        template_path = (source.parent / relative).resolve()
        if not template_path.is_file():
            raise ChatLayerRecoveryConfigError(f"返回按钮模板不存在：{template_path}")
        minimum_score = _number(
            raw.get("minimum_score", 0.985),
            f"templates[{index}].minimum_score",
        )
        if not 0.0 < minimum_score <= 1.0:
            raise ChatLayerRecoveryConfigError("返回按钮最低分数必须位于 0 到 1 之间。")
        coarse_step = raw.get("coarse_step", 2)
        if isinstance(coarse_step, bool) or not isinstance(coarse_step, int) or coarse_step < 1:
            raise ChatLayerRecoveryConfigError("返回按钮匹配步长必须是正整数。")
        templates.append(BackButtonTemplate(template_path, minimum_score, coarse_step))
    return BackButtonSpec(
        source=source,
        theme=theme,
        roi=(roi[0], roi[1], roi[2], roi[3]),
        max_candidates=max_candidates,
        templates=tuple(templates),
    )


def _pixel_roi(image: Image.Image, roi: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    return (
        round(image.width * roi[0]),
        round(image.height * roi[1]),
        round(image.width * roi[2]),
        round(image.height * roi[3]),
    )


def _same_button(left: Rect, right: Rect) -> bool:
    left_center = left.center
    right_center = right.center
    return (
        abs(left_center.x - right_center.x) < max(left.width, right.width)
        and abs(left_center.y - right_center.y) < max(left.height, right.height)
    )


class BackButtonDetector:
    """Find every supported Back-button state and order it top-left first."""

    def __init__(self, spec: BackButtonSpec):
        self.spec = spec
        self._preferred_scale_factors: tuple[float, ...] = (1.0,)
        self._fallback_scale_factors: tuple[float, ...] = (1.0,)
        self._matcher_cache: dict[
            tuple[str, tuple[float, ...]],
            OpenCVTemplateMatcher,
        ] = {}

    @staticmethod
    def _normalise_scales(values: tuple[float, ...] | list[float]) -> tuple[float, ...]:
        scales: list[float] = []
        for value in values:
            scale = round(float(value), 4)
            if not 0.5 <= scale <= 3.0:
                raise ValueError("返回按钮 DPI 缩放比例必须位于 0.5 到 3.0 之间。")
            if scale not in scales:
                scales.append(scale)
        if not scales:
            raise ValueError("返回按钮 DPI 缩放比例不能为空。")
        return tuple(scales)

    def set_scale_policy(
        self,
        preferred: tuple[float, ...] | list[float],
        fallback: tuple[float, ...] | list[float] | None = None,
    ) -> None:
        self._preferred_scale_factors = self._normalise_scales(preferred)
        self._fallback_scale_factors = self._normalise_scales(
            fallback or self._preferred_scale_factors
        )

    def _matchers_for(
        self,
        scale_factors: tuple[float, ...],
    ) -> tuple[tuple[BackButtonTemplate, OpenCVTemplateMatcher], ...]:
        items: list[tuple[BackButtonTemplate, OpenCVTemplateMatcher]] = []
        for template in self.spec.templates:
            key = (str(template.path), scale_factors)
            matcher = self._matcher_cache.get(key)
            if matcher is None:
                matcher = OpenCVTemplateMatcher(
                    template.path,
                    coarse_step=template.coarse_step,
                    minimum_score=template.minimum_score,
                    # Multiple Back buttons are expected; uniqueness is not a
                    # validity requirement for this target.
                    minimum_margin=0.0,
                    scale_factors=scale_factors,
                )
                self._matcher_cache[key] = matcher
            items.append((template, matcher))
        return tuple(items)

    def _find_for_template(
        self,
        image: Image.Image,
        template: BackButtonTemplate,
        matcher: OpenCVTemplateMatcher,
    ) -> list[BackButtonCandidate]:
        working = image.convert("RGB").copy()
        roi = _pixel_roi(working, self.spec.roi)
        found: list[BackButtonCandidate] = []
        for _ in range(self.spec.max_candidates):
            match = matcher.match(working, roi=roi)
            if not match.accepted or match.best_bounds is None:
                break
            bounds = match.best_bounds
            found.append(
                BackButtonCandidate(
                    bounds,
                    match.score,
                    template.path,
                    match.scale,
                )
            )
            if match.second_score < template.minimum_score:
                break
            # Remove this hit and its overlapping scan positions before asking
            # for the next candidate of the same template.
            padding_x = max(2, bounds.width // 2)
            padding_y = max(2, bounds.height // 2)
            mask = (
                max(roi[0], bounds.left - padding_x),
                max(roi[1], bounds.top - padding_y),
                min(roi[2] - 1, bounds.right + padding_x - 1),
                min(roi[3] - 1, bounds.bottom + padding_y - 1),
            )
            ImageDraw.Draw(working).rectangle(mask, fill=(255, 0, 255))
        return found

    def _find_at_scales(
        self,
        image: Image.Image,
        scale_factors: tuple[float, ...],
    ) -> tuple[BackButtonCandidate, ...]:
        candidates: list[BackButtonCandidate] = []
        for template, matcher in self._matchers_for(scale_factors):
            for candidate in self._find_for_template(image, template, matcher):
                duplicate_index = next(
                    (
                        index
                        for index, existing in enumerate(candidates)
                        if _same_button(existing.bounds, candidate.bounds)
                    ),
                    None,
                )
                if duplicate_index is None:
                    candidates.append(candidate)
                elif candidate.score > candidates[duplicate_index].score:
                    candidates[duplicate_index] = candidate
        candidates.sort(key=lambda item: (item.bounds.top, item.bounds.left))
        return tuple(candidates[: self.spec.max_candidates])

    def find(self, image: Image.Image) -> tuple[BackButtonCandidate, ...]:
        preferred = self._find_at_scales(image, self._preferred_scale_factors)
        if preferred or self._fallback_scale_factors == self._preferred_scale_factors:
            return preferred
        return self._find_at_scales(image, self._fallback_scale_factors)


class ChatLayerRecovery:
    """Click each current Back candidate once while the chat guard remains."""

    def __init__(
        self,
        *,
        session: CaptureSession,
        interaction: ClickInteraction,
        chat_locator: ChatLocator,
        back_detector: BackButtonDetector,
    ) -> None:
        self.session = session
        self.interaction = interaction
        self.chat_locator = chat_locator
        self.back_detector = back_detector

    def set_scale_policy(
        self,
        preferred: tuple[float, ...] | list[float],
        fallback: tuple[float, ...] | list[float] | None = None,
    ) -> None:
        setter = getattr(self.chat_locator, "set_scale_policy", None)
        if callable(setter):
            setter(preferred, fallback)
        self.back_detector.set_scale_policy(preferred, fallback)

    @staticmethod
    def _screen_rect(frame: CapturedFrame, local: Rect) -> Rect:
        return Rect(
            frame.screen_rect.left + local.left,
            frame.screen_rect.top + local.top,
            frame.screen_rect.left + local.right,
            frame.screen_rect.top + local.bottom,
        )

    @staticmethod
    def _guard_overview(
        image: Image.Image,
        located: RelativeLocatorResult,
        back_candidates: tuple[BackButtonCandidate, ...],
    ) -> Image.Image:
        overlay = draw_debug_overlay(image, located)
        draw = ImageDraw.Draw(overlay)
        for index, candidate in enumerate(back_candidates, start=1):
            bounds = candidate.bounds
            draw.rectangle(
                (bounds.left, bounds.top, bounds.right - 1, bounds.bottom - 1),
                outline=(0, 140, 210),
                width=2,
            )
            draw.text(
                (bounds.left, max(0, bounds.top - 12)),
                f"BACK {index} {candidate.score:.3f} @{candidate.scale:.2f}x",
                fill=(0, 100, 165),
            )
        return overlay

    def _chat_guard(
        self,
        frame: CapturedFrame,
        back_candidates: tuple[BackButtonCandidate, ...],
        *,
        operation: str,
    ) -> tuple[bool, str, str]:
        located = self.chat_locator.locate(
            frame.image.convert("RGB"),
            skip_optional_anchors=True,
        )
        send = located.detections.get("send_button")
        chat_present = bool(
            located.accepted
            and send is not None
            and send.accepted
            and send.bounds is not None
        )
        snapshot = record_recognition_snapshot(
            frame.image.convert("RGB"),
            located,
            label="返回按钮与聊天工具栏联合守卫",
            operation=operation,
            force=not located.accepted,
            overview=self._guard_overview(frame.image, located, back_candidates),
            extra_metadata={
                "joint_guard": "back_button + send_button + emoji_button",
                "back_candidates": [
                    {
                        "score": round(candidate.score, 6),
                        "scale": round(candidate.scale, 4),
                        "template": candidate.template.name,
                        "bounds": {
                            "left": candidate.bounds.left,
                            "top": candidate.bounds.top,
                            "right": candidate.bounds.right,
                            "bottom": candidate.bounds.bottom,
                        },
                    }
                    for candidate in back_candidates
                ],
            },
        )
        failure_code = (
            located.failure_code
            if located.failure_code == "ambiguous_combinations"
            else ""
        )
        snapshot_id = str(snapshot.get("id") or "") if snapshot else ""
        return chat_present, failure_code, snapshot_id

    def recover(
        self,
        *,
        cancel_event: threading.Event | None = None,
    ) -> ChatLayerRecoveryResult:
        started = time.monotonic()
        _trace("chat.recovery", "开始检查聊天层返回按钮")
        frame = self.session.capture_client()
        find_started = time.monotonic()
        candidates = self.back_detector.find(frame.image)
        _trace(
            "find.back_buttons",
            f"查找返回按钮完成：找到 {len(candidates)} 个候选",
            duration_ms=int(round((time.monotonic() - find_started) * 1000)),
        )
        if not candidates:
            _trace(
                "chat.recovery",
                "顶部没有返回按钮，无需执行聊天工具栏完整识别",
                duration_ms=int(round((time.monotonic() - started) * 1000)),
            )
            return ChatLayerRecoveryResult(False, 0, (), False, False)

        guard_started = time.monotonic()
        chat_present, guard_failure_code, snapshot_id = self._chat_guard(
            frame,
            candidates,
            operation="find.chat_layer_guard",
        )
        _trace(
            "find.chat_layer_guard",
            "聊天输入区域/发送按钮联合检查完成",
            duration_ms=int(round((time.monotonic() - guard_started) * 1000)),
        )
        if guard_failure_code:
            _trace(
                "chat.recovery",
                "聊天层联合守卫检测到多个指向不同区域的有效元素组合，已停止恢复流程",
                duration_ms=int(round((time.monotonic() - started) * 1000)),
                level=logging.ERROR,
            )
            return ChatLayerRecoveryResult(
                False,
                len(candidates),
                (),
                False,
                False,
                guard_failure_code,
                snapshot_id,
            )
        if not chat_present:
            _trace(
                "chat.recovery",
                "当前不是聊天发送层，无需点击返回按钮",
                duration_ms=int(round((time.monotonic() - started) * 1000)),
            )
            return ChatLayerRecoveryResult(False, 0, (), False, False)
        initial_count = len(candidates)
        clicked: list[Rect] = []
        while len(clicked) < self.back_detector.spec.max_candidates:
            next_candidate = next(
                (
                    candidate
                    for candidate in candidates
                    if not any(_same_button(candidate.bounds, old) for old in clicked)
                ),
                None,
            )
            if next_candidate is None:
                return ChatLayerRecoveryResult(
                    True,
                    initial_count,
                    tuple(clicked),
                    False,
                    True,
                )
            click_started = time.monotonic()
            _trace(
                "click.chat_back",
                f"点击第 {len(clicked) + 1} 个返回按钮",
            )
            try:
                self.interaction.click_rect(
                    self._screen_rect(frame, next_candidate.bounds),
                    cancel_event=cancel_event,
                )
            finally:
                _trace(
                    "click.chat_back",
                    f"第 {len(clicked) + 1} 个返回按钮点击完成",
                    duration_ms=int(round((time.monotonic() - click_started) * 1000)),
                )
            clicked.append(next_candidate.bounds)
            wait_started = time.monotonic()
            _trace("chat.wait_after_back", "等待返回操作后的界面稳定")
            self.interaction.wait_after_state(cancel_event)
            _trace(
                "chat.wait_after_back",
                "返回操作后的界面稳定等待完成",
                duration_ms=int(round((time.monotonic() - wait_started) * 1000)),
            )
            frame = self.session.capture_client()
            find_started = time.monotonic()
            candidates = self.back_detector.find(frame.image)
            _trace(
                "find.back_buttons",
                f"重新查找返回按钮完成：找到 {len(candidates)} 个候选",
                duration_ms=int(round((time.monotonic() - find_started) * 1000)),
            )
            guard_started = time.monotonic()
            chat_present, guard_failure_code, snapshot_id = self._chat_guard(
                frame,
                candidates,
                operation="find.chat_layer_guard_after_back",
            )
            _trace(
                "find.chat_layer_guard",
                "返回后重新检查聊天输入区域/发送按钮",
                duration_ms=int(round((time.monotonic() - guard_started) * 1000)),
            )
            if guard_failure_code:
                _trace(
                    "chat.recovery",
                    "返回后聊天层联合守卫出现定位歧义，已停止后续点击",
                    duration_ms=int(round((time.monotonic() - started) * 1000)),
                    level=logging.ERROR,
                )
                return ChatLayerRecoveryResult(
                    True,
                    initial_count,
                    tuple(clicked),
                    False,
                    False,
                    guard_failure_code,
                    snapshot_id,
                )
            if not chat_present:
                _trace(
                    "chat.recovery",
                    f"聊天层恢复完成，共点击 {len(clicked)} 个返回按钮",
                    duration_ms=int(round((time.monotonic() - started) * 1000)),
                )
                return ChatLayerRecoveryResult(
                    True,
                    initial_count,
                    tuple(clicked),
                    True,
                    False,
                )
        _trace(
            "chat.recovery",
            f"聊天层恢复未完成，共点击 {len(clicked)} 个返回按钮",
            duration_ms=int(round((time.monotonic() - started) * 1000)),
            level=logging.ERROR,
        )
        return ChatLayerRecoveryResult(
            True,
            initial_count,
            tuple(clicked),
            False,
            True,
        )


__all__ = [
    "BackButtonCandidate",
    "BackButtonDetector",
    "BackButtonSpec",
    "ChatLayerRecovery",
    "ChatLayerRecoveryConfigError",
    "ChatLayerRecoveryResult",
    "load_back_button_spec",
]
