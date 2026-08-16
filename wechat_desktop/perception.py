"""Local-only visual guard utilities for screenshots of the visible client area."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from .models import CapturedFrame, Rect, VisualMatch
from .session import DesktopSessionError


@dataclass(frozen=True)
class StableMatch:
    match: VisualMatch
    frames_seen: int
    stable_frames: int
    elapsed: float


def crop_screen(frame: CapturedFrame, bounds: Rect):
    """Crop using screen coordinates while preserving a strict client boundary."""

    client = frame.screen_rect
    if not (
        client.left <= bounds.left < bounds.right <= client.right
        and client.top <= bounds.top < bounds.bottom <= client.bottom
    ):
        raise ValueError("裁剪区域必须完全位于本次微信客户区截图内。")
    return frame.image.crop(
        (
            bounds.left - client.left,
            bounds.top - client.top,
            bounds.right - client.left,
            bounds.bottom - client.top,
        )
    )


def normalized_image_difference(first, second) -> float:
    """Return a 0..1 mean absolute pixel difference for equal-sized images."""

    try:
        from PIL import ImageChops, ImageOps, ImageStat
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise DesktopSessionError(
            "missing_dependency",
            "缺少 Pillow，请先运行 first.bat 安装项目依赖。",
        ) from exc
    if first.size != second.size:
        raise ValueError("只有尺寸相同的画面才能比较稳定性。")
    gray_first = ImageOps.grayscale(first)
    gray_second = ImageOps.grayscale(second)
    difference = ImageChops.difference(gray_first, gray_second)
    return float(ImageStat.Stat(difference).mean[0]) / 255.0


class VisualWaiter:
    """Poll local screenshots until a detector agrees across stable frames."""

    def __init__(
        self,
        capture: Callable[[], CapturedFrame],
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.capture = capture
        self.sleep = sleep
        self.monotonic = monotonic

    @staticmethod
    def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise DesktopSessionError(
                "automation_cancelled",
                "自动化已停止，未继续等待视觉状态。",
            )

    def wait_for_stable_match(
        self,
        detector: Callable[[CapturedFrame], VisualMatch],
        *,
        timeout: float,
        minimum_confidence: float = 0.85,
        stable_frames: int = 2,
        poll_interval: float = 0.12,
        cancel_event: threading.Event | None = None,
    ) -> StableMatch:
        if timeout <= 0:
            raise ValueError("视觉等待超时必须大于 0。")
        if not 0.0 <= minimum_confidence <= 1.0:
            raise ValueError("最低视觉置信度必须位于 0 到 1 之间。")
        if stable_frames < 1:
            raise ValueError("稳定画面数量至少为 1。")
        if poll_interval < 0:
            raise ValueError("视觉轮询间隔不能为负数。")
        started = self.monotonic()
        deadline = started + timeout
        seen = 0
        consecutive = 0
        last_signature = None
        best: VisualMatch | None = None
        while True:
            self._raise_if_cancelled(cancel_event)
            frame = self.capture()
            seen += 1
            match = detector(frame)
            if best is None or match.confidence > best.confidence:
                best = match
            if match.matched and match.confidence >= minimum_confidence:
                signature = (
                    match.code,
                    match.bounds,
                    match.text,
                    match.details.get("stability_key"),
                )
                if signature == last_signature:
                    consecutive += 1
                else:
                    consecutive = 1
                    last_signature = signature
                if consecutive >= stable_frames:
                    return StableMatch(
                        match=match,
                        frames_seen=seen,
                        stable_frames=consecutive,
                        elapsed=max(0.0, self.monotonic() - started),
                    )
            else:
                consecutive = 0
                last_signature = None
            now = self.monotonic()
            if now >= deadline:
                raise DesktopSessionError(
                    "visual_guard_timeout",
                    "限定时间内没有得到稳定、可信的微信视觉状态。",
                    details={
                        "frames_seen": seen,
                        "best_code": best.code if best else "",
                        "best_confidence": best.confidence if best else 0.0,
                        "best_details": dict(best.details) if best else {},
                        "minimum_confidence": minimum_confidence,
                    },
                )
            wait = min(poll_interval, max(0.0, deadline - now))
            if cancel_event is not None:
                if cancel_event.wait(wait):
                    self._raise_if_cancelled(cancel_event)
            else:
                self.sleep(wait)
