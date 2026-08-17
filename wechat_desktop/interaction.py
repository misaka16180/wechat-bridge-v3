"""Bounded mouse movement, clicks, list scrolling, and cancellable waits.

Random variation is used only after a visual target is known.  It is not a
claim that automation becomes indistinguishable from a person or that account
risk is eliminated.
"""

from __future__ import annotations

import ctypes
import math
import logging
import random
import secrets
import threading
import time
from dataclasses import dataclass
from ctypes import wintypes
from typing import Callable, Protocol

from .models import Point, Rect


log = logging.getLogger("wechat_automation.interaction")


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    )


class _MOUSEINPUTUNION(ctypes.Union):
    _fields_ = (("mi", _MOUSEINPUT),)


class _MOUSE_EVENT_INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = (("type", wintypes.DWORD), ("u", _MOUSEINPUTUNION))


class InteractionCancelled(RuntimeError):
    """Raised before another input event when the task is cancelled."""


class RandomLike(Protocol):
    def random(self) -> float:
        ...

    def uniform(self, a: float, b: float) -> float:
        ...

    def randint(self, a: int, b: int) -> int:
        ...


class InputBackend(Protocol):
    def cursor_position(self) -> Point:
        ...

    def move_cursor(self, point: Point) -> None:
        ...

    def left_click(self) -> None:
        ...

    def left_button_down(self) -> None:
        ...

    def left_button_up(self) -> None:
        ...

    def vertical_scroll(self, delta: int) -> None:
        ...


class Win32InputBackend:
    """Use ordinary Windows mouse input without UIA, hooks, or process access."""

    INPUT_MOUSE = 0
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004

    def __init__(self, *, send_input: Callable[..., int] | None = None) -> None:
        expected_size = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        actual_size = ctypes.sizeof(_MOUSE_EVENT_INPUT)
        if actual_size != expected_size:
            raise RuntimeError(
                f"Win32 鼠标 INPUT 结构大小错误：应为 {expected_size}，实际为 {actual_size}。"
            )
        self._send_input = send_input

    @staticmethod
    def _modules():
        try:
            import win32api
            import win32con
        except ImportError as exc:  # pragma: no cover - depends on runtime setup
            raise RuntimeError(
                "缺少 pywin32，请先运行 first.bat 安装项目依赖。"
            ) from exc
        return win32api, win32con

    def cursor_position(self) -> Point:
        win32api, _ = self._modules()
        x, y = win32api.GetCursorPos()
        return Point(int(x), int(y))

    def move_cursor(self, point: Point) -> None:
        win32api, _ = self._modules()
        win32api.SetCursorPos((int(point.x), int(point.y)))

    def _send_mouse_button(self, flag: int, action: str) -> None:
        send_input = self._send_input
        if send_input is None:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.SendInput.argtypes = (
                wintypes.UINT,
                ctypes.POINTER(_MOUSE_EVENT_INPUT),
                ctypes.c_int,
            )
            user32.SendInput.restype = wintypes.UINT
            send_input = user32.SendInput
            self._send_input = send_input
        event = _MOUSE_EVENT_INPUT(
            type=self.INPUT_MOUSE,
            mi=_MOUSEINPUT(0, 0, 0, int(flag), 0, 0),
        )
        set_last_error = getattr(ctypes, "set_last_error", None)
        if callable(set_last_error):
            set_last_error(0)
        sent = int(send_input(1, ctypes.byref(event), ctypes.sizeof(event)))
        if sent != 1:
            get_last_error = getattr(ctypes, "get_last_error", None)
            error = int(get_last_error()) if callable(get_last_error) else 0
            raise OSError(
                error,
                (
                    f"Windows 未接受鼠标左键{action}事件：SendInput 返回 {sent}，"
                    f"错误码 {error}。请确认微信和桥接程序使用相同的权限级别。"
                ),
            )

    def left_click(self) -> None:
        self.left_button_down()
        self.left_button_up()

    def left_button_down(self) -> None:
        self._send_mouse_button(self.MOUSEEVENTF_LEFTDOWN, "按下")

    def left_button_up(self) -> None:
        self._send_mouse_button(self.MOUSEEVENTF_LEFTUP, "抬起")

    def vertical_scroll(self, delta: int) -> None:
        win32api, win32con = self._modules()
        x, y = win32api.GetCursorPos()
        win32api.mouse_event(
            win32con.MOUSEEVENTF_WHEEL,
            x,
            y,
            int(delta),
            0,
        )


@dataclass(frozen=True)
class InteractionPolicy:
    """Small, user-facing bounds; every random value stays inside them."""

    state_wait_min: float = 0.12
    state_wait_max: float = 0.32
    mouse_duration_min: float = 0.18
    mouse_duration_max: float = 0.46
    click_before_delay_min: float = 0.10
    click_before_delay_max: float = 0.25
    click_hold_duration_min: float = 0.04
    click_hold_duration_max: float = 0.08
    click_wait_min: float = 0.08
    click_wait_max: float = 0.22
    safe_horizontal_ratio: float = 0.22
    safe_vertical_ratio: float = 0.20
    path_hz: int = 60

    def __post_init__(self) -> None:
        ranges = (
            ("状态等待", self.state_wait_min, self.state_wait_max),
            ("鼠标移动时长", self.mouse_duration_min, self.mouse_duration_max),
            ("鼠标点击前停顿", self.click_before_delay_min, self.click_before_delay_max),
            ("鼠标按住时间", self.click_hold_duration_min, self.click_hold_duration_max),
            ("点击后等待", self.click_wait_min, self.click_wait_max),
        )
        for name, minimum, maximum in ranges:
            if minimum < 0 or maximum < minimum:
                raise ValueError(f"{name}范围无效。")
        for name, ratio in (
            ("横向安全比例", self.safe_horizontal_ratio),
            ("纵向安全比例", self.safe_vertical_ratio),
        ):
            if not 0.0 <= ratio < 0.5:
                raise ValueError(f"{name}必须位于 0（含）到 0.5（不含）之间。")
        if self.path_hz < 10 or self.path_hz > 240:
            raise ValueError("鼠标轨迹采样率必须位于 10 到 240 之间。")


class RandomizedInteraction:
    """Generate safe target points and short curved paths around known targets."""

    def __init__(
        self,
        backend: InputBackend | None = None,
        *,
        policy: InteractionPolicy | None = None,
        rng: RandomLike | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.backend = backend or Win32InputBackend()
        self.policy = policy or InteractionPolicy()
        self.rng = rng or secrets.SystemRandom()
        self._sleep = sleep

    @staticmethod
    def _trace(
        operation: str,
        message: str,
        *,
        duration_seconds: float | None = None,
        level: int = logging.INFO,
    ) -> None:
        extra = {"automation_operation": operation}
        if duration_seconds is not None:
            extra["automation_duration_ms"] = max(
                0,
                int(round(float(duration_seconds) * 1000)),
            )
        log.log(level, message, extra=extra)

    @staticmethod
    def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise InteractionCancelled("自动化已停止，未执行后续输入。")

    def _wait(
        self,
        seconds: float,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self._raise_if_cancelled(cancel_event)
        if seconds <= 0:
            return
        if cancel_event is not None:
            if cancel_event.wait(seconds):
                self._raise_if_cancelled(cancel_event)
        else:
            self._sleep(seconds)
        self._raise_if_cancelled(cancel_event)

    def wait_after_state(
        self,
        cancel_event: threading.Event | None = None,
    ) -> float:
        """Add jitter only after the caller has confirmed the expected state."""

        duration = self.rng.uniform(
            self.policy.state_wait_min,
            self.policy.state_wait_max,
        )
        self._wait(duration, cancel_event)
        return duration

    def wait_random(
        self,
        minimum: float,
        maximum: float | None = None,
        *,
        cancel_event: threading.Event | None = None,
    ) -> float:
        """Wait for a caller-configured random duration."""

        maximum = minimum if maximum is None else maximum
        if minimum < 0 or maximum < minimum:
            raise ValueError("等待时间范围无效。")
        duration = self.rng.uniform(minimum, maximum)
        self._wait(duration, cancel_event)
        return duration

    def choose_random_duration(self, minimum: float, maximum: float) -> float:
        """Choose a duration without sleeping so other work can consume it."""

        if minimum < 0 or maximum < minimum:
            raise ValueError("等待时间范围无效。")
        return self.rng.uniform(minimum, maximum)

    def safe_rect(
        self,
        bounds: Rect,
        *,
        horizontal_ratio: float | None = None,
        vertical_ratio: float | None = None,
    ) -> Rect:
        horizontal_ratio = (
            self.policy.safe_horizontal_ratio
            if horizontal_ratio is None
            else horizontal_ratio
        )
        vertical_ratio = (
            self.policy.safe_vertical_ratio
            if vertical_ratio is None
            else vertical_ratio
        )
        if not 0.0 <= horizontal_ratio < 0.5:
            raise ValueError("横向安全比例必须位于 0 到 0.5 之间。")
        if not 0.0 <= vertical_ratio < 0.5:
            raise ValueError("纵向安全比例必须位于 0 到 0.5 之间。")
        horizontal = min(
            int(bounds.width * horizontal_ratio),
            max(0, (bounds.width - 1) // 2),
        )
        vertical = min(
            int(bounds.height * vertical_ratio),
            max(0, (bounds.height - 1) // 2),
        )
        if horizontal == 0 and vertical == 0:
            return bounds
        return bounds.inset(horizontal, vertical)

    def choose_safe_point(
        self,
        bounds: Rect,
        *,
        horizontal_ratio: float | None = None,
        vertical_ratio: float | None = None,
    ) -> Point:
        safe = self.safe_rect(
            bounds,
            horizontal_ratio=horizontal_ratio,
            vertical_ratio=vertical_ratio,
        )
        return Point(
            self.rng.randint(safe.left, safe.right - 1),
            self.rng.randint(safe.top, safe.bottom - 1),
        )

    def choose_fraction_point(
        self,
        bounds: Rect,
        *,
        x_range: tuple[float, float] = (0.22, 0.78),
        y_range: tuple[float, float] = (0.35, 0.65),
    ) -> Point:
        """Choose a point from explicit normalized x/y ranges in a rectangle."""

        for name, values in (("横向", x_range), ("纵向", y_range)):
            if len(values) != 2 or not 0.0 <= values[0] <= values[1] <= 1.0:
                raise ValueError(f"{name}随机范围必须是 0 到 1 之间的递增二元组。")
        left = bounds.left + int(bounds.width * x_range[0])
        right = bounds.left + max(
            int(bounds.width * x_range[1]) - 1,
            int(bounds.width * x_range[0]),
        )
        top = bounds.top + int(bounds.height * y_range[0])
        bottom = bounds.top + max(
            int(bounds.height * y_range[1]) - 1,
            int(bounds.height * y_range[0]),
        )
        return Point(
            self.rng.randint(left, min(bounds.right - 1, right)),
            self.rng.randint(top, min(bounds.bottom - 1, bottom)),
        )

    def _curved_path(self, start: Point, target: Point, steps: int) -> list[Point]:
        dx = target.x - start.x
        dy = target.y - start.y
        distance = math.hypot(dx, dy)
        if distance == 0:
            return [target]
        perpendicular_x = -dy / distance
        perpendicular_y = dx / distance
        bend = (self.rng.random() * 2.0 - 1.0) * min(42.0, distance * 0.12)
        control_1 = (
            start.x + dx * 0.32 + perpendicular_x * bend,
            start.y + dy * 0.32 + perpendicular_y * bend,
        )
        control_2 = (
            start.x + dx * 0.70 + perpendicular_x * bend * 0.55,
            start.y + dy * 0.70 + perpendicular_y * bend * 0.55,
        )
        # A cubic Bézier already avoids a mechanically straight segment.  Add
        # a very small *smooth* lateral variation in the middle of the path so
        # successive cursor samples are not mathematically perfect either.
        # The sin(pi*t) envelope forces the disturbance to zero at both ends:
        # the cursor still starts naturally and lands on the exact safe point.
        jitter_amplitude = min(3.2, distance * 0.009) * self.rng.uniform(0.65, 1.0)
        jitter_frequency = self.rng.uniform(1.15, 2.35)
        jitter_phase = self.rng.uniform(0.0, math.tau)
        points: list[Point] = []
        for index in range(1, steps + 1):
            t = index / steps
            inverse = 1.0 - t
            x = (
                inverse**3 * start.x
                + 3 * inverse**2 * t * control_1[0]
                + 3 * inverse * t**2 * control_2[0]
                + t**3 * target.x
            )
            y = (
                inverse**3 * start.y
                + 3 * inverse**2 * t * control_1[1]
                + 3 * inverse * t**2 * control_2[1]
                + t**3 * target.y
            )
            envelope = math.sin(math.pi * t)
            jitter = (
                jitter_amplitude
                * envelope
                * (
                    0.72 * math.sin(math.tau * jitter_frequency * t + jitter_phase)
                    + 0.28
                    * math.sin(
                        math.tau * (jitter_frequency * 1.83) * t
                        + jitter_phase * 0.47
                    )
                )
            )
            x += perpendicular_x * jitter
            y += perpendicular_y * jitter
            point = Point(round(x), round(y))
            if not points or point != points[-1]:
                points.append(point)
        if points[-1] != target:
            points.append(target)
        return points

    def move_to_rect(
        self,
        bounds: Rect,
        *,
        horizontal_ratio: float | None = None,
        vertical_ratio: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Point:
        self._raise_if_cancelled(cancel_event)
        target = self.choose_safe_point(
            bounds,
            horizontal_ratio=horizontal_ratio,
            vertical_ratio=vertical_ratio,
        )
        start = self.backend.cursor_position()
        duration = self.rng.uniform(
            self.policy.mouse_duration_min,
            self.policy.mouse_duration_max,
        )
        steps = max(2, min(80, round(duration * self.policy.path_hz)))
        path = self._curved_path(start, target, steps)
        interval = duration / max(1, len(path))
        for point in path:
            self._raise_if_cancelled(cancel_event)
            self.backend.move_cursor(point)
            self._wait(interval, cancel_event)
        return target

    def move_to_point(
        self,
        target: Point,
        *,
        cancel_event: threading.Event | None = None,
    ) -> Point:
        """Move along the bounded path to an already selected screen point."""

        self._raise_if_cancelled(cancel_event)
        start = self.backend.cursor_position()
        duration = self.rng.uniform(
            self.policy.mouse_duration_min,
            self.policy.mouse_duration_max,
        )
        steps = max(2, min(80, round(duration * self.policy.path_hz)))
        path = self._curved_path(start, target, steps)
        interval = duration / max(1, len(path))
        for point in path:
            self._raise_if_cancelled(cancel_event)
            self.backend.move_cursor(point)
            self._wait(interval, cancel_event)
        return target

    def click_fraction_point(
        self,
        bounds: Rect,
        *,
        x_range: tuple[float, float] = (0.22, 0.78),
        y_range: tuple[float, float] = (0.35, 0.65),
        cancel_event: threading.Event | None = None,
    ) -> Point:
        """Click once at a caller-configured normalized point range."""

        point = self.choose_fraction_point(
            bounds,
            x_range=x_range,
            y_range=y_range,
        )
        move_started = time.monotonic()
        self.move_to_point(point, cancel_event=cancel_event)
        self._trace(
            "mouse.move.completed",
            f"鼠标移动完成：最终坐标 ({point.x}, {point.y})。",
            duration_seconds=time.monotonic() - move_started,
        )
        self._click_at_current_position(point, cancel_event=cancel_event)
        return point

    def click_rect(
        self,
        bounds: Rect,
        *,
        horizontal_ratio: float | None = None,
        vertical_ratio: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Point:
        """Move into a known target's safe interior and click exactly once."""

        move_started = time.monotonic()
        point = self.move_to_rect(
            bounds,
            horizontal_ratio=horizontal_ratio,
            vertical_ratio=vertical_ratio,
            cancel_event=cancel_event,
        )
        self._trace(
            "mouse.move.completed",
            f"鼠标移动完成：最终坐标 ({point.x}, {point.y})。",
            duration_seconds=time.monotonic() - move_started,
        )
        self._click_at_current_position(point, cancel_event=cancel_event)
        return point

    def double_click_rect(
        self,
        bounds: Rect,
        *,
        horizontal_ratio: float | None = None,
        vertical_ratio: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Point:
        """Move once and perform one ordinary Windows left-button double click.

        The target must already have been identified by the caller.  The two
        press/release cycles deliberately share one pre-click pause and one
        post-click pause so their interval remains inside the normal Windows
        double-click window.
        """

        move_started = time.monotonic()
        point = self.move_to_rect(
            bounds,
            horizontal_ratio=horizontal_ratio,
            vertical_ratio=vertical_ratio,
            cancel_event=cancel_event,
        )
        self._trace(
            "mouse.move.completed",
            f"鼠标移动完成：最终坐标 ({point.x}, {point.y})。",
            duration_seconds=time.monotonic() - move_started,
        )
        pre_click_delay = self.rng.uniform(
            self.policy.click_before_delay_min,
            self.policy.click_before_delay_max,
        )
        self._trace(
            "mouse.double_click.pre_wait",
            f"鼠标双击前停顿开始：计划 {pre_click_delay:.3f} 秒。",
        )
        self._wait(pre_click_delay, cancel_event)
        self._trace(
            "mouse.double_click.pre_wait",
            "鼠标双击前停顿结束。",
        )
        self._press_and_release(
            point,
            operation_prefix="mouse.double_click.first",
            cancel_event=cancel_event,
        )
        interval = self.rng.uniform(0.06, 0.12)
        self._trace(
            "mouse.double_click.interval",
            f"两次点击间隔开始：计划 {interval:.3f} 秒。",
        )
        self._wait(interval, cancel_event)
        self._press_and_release(
            point,
            operation_prefix="mouse.double_click.second",
            cancel_event=cancel_event,
        )
        post_click_delay = self.rng.uniform(
            self.policy.click_wait_min,
            self.policy.click_wait_max,
        )
        self._wait(post_click_delay, cancel_event)
        self._trace(
            "mouse.double_click.completed",
            (
                f"鼠标左键双击完成：坐标 ({point.x}, {point.y})；"
                f"点击后等待 {post_click_delay:.3f} 秒。"
            ),
        )
        return point

    def _press_and_release(
        self,
        point: Point,
        *,
        operation_prefix: str,
        cancel_event: threading.Event | None,
    ) -> None:
        """Send one bounded press/release cycle and always release the button."""

        self._raise_if_cancelled(cancel_event)
        down = getattr(self.backend, "left_button_down", None)
        up = getattr(self.backend, "left_button_up", None)
        if not callable(down) or not callable(up):
            self.backend.left_click()
            self._trace(
                operation_prefix,
                f"鼠标左键单击事件已发送：坐标 ({point.x}, {point.y})。",
            )
            return
        down()
        self._trace(
            operation_prefix + ".down",
            f"Windows 已接受鼠标左键按下事件：坐标 ({point.x}, {point.y})。",
        )
        hold_duration = self.rng.uniform(
            self.policy.click_hold_duration_min,
            self.policy.click_hold_duration_max,
        )
        try:
            self._wait(hold_duration, cancel_event)
        finally:
            up()
            self._trace(
                operation_prefix + ".up",
                f"Windows 已接受鼠标左键抬起事件：坐标 ({point.x}, {point.y})。",
            )

    def _click_at_current_position(
        self,
        point: Point,
        *,
        cancel_event: threading.Event | None,
    ) -> None:
        """Pause before one click, then expose the exact input-event boundary."""

        pre_click_delay = self.rng.uniform(
            self.policy.click_before_delay_min,
            self.policy.click_before_delay_max,
        )
        wait_started = time.monotonic()
        self._trace(
            "mouse.pre_click_wait",
            (
                "鼠标点击前停顿开始：计划 %.3f 秒；坐标 (%s, %s)。"
                % (pre_click_delay, point.x, point.y)
            ),
        )
        try:
            self._wait(pre_click_delay, cancel_event)
        except InteractionCancelled:
            self._trace(
                "mouse.pre_click_wait",
                (
                    "鼠标点击前停顿期间任务已停止；坐标 (%s, %s)，"
                    "未发送鼠标按下事件。" % (point.x, point.y)
                ),
                duration_seconds=time.monotonic() - wait_started,
                level=logging.WARNING,
            )
            raise
        self._trace(
            "mouse.pre_click_wait",
            (
                "鼠标点击前停顿结束：实际 %.3f 秒；坐标 (%s, %s)。"
                % (time.monotonic() - wait_started, point.x, point.y)
            ),
            duration_seconds=time.monotonic() - wait_started,
        )
        self._raise_if_cancelled(cancel_event)

        down = getattr(self.backend, "left_button_down", None)
        up = getattr(self.backend, "left_button_up", None)
        if callable(down) and callable(up):
            down()
            self._trace(
                "mouse.click.down",
                f"Windows 已接受鼠标左键按下事件：坐标 ({point.x}, {point.y})。",
            )
            hold_duration = self.rng.uniform(
                self.policy.click_hold_duration_min,
                self.policy.click_hold_duration_max,
            )
            hold_started = time.monotonic()
            self._trace(
                "mouse.click.hold",
                f"鼠标左键按住开始：计划 {hold_duration:.3f} 秒。",
            )
            try:
                self._wait(hold_duration, cancel_event)
            except InteractionCancelled:
                self._trace(
                    "mouse.click.hold",
                    (
                        "鼠标按住期间任务已停止；将先安全抬起左键，"
                        "不再执行后续输入。"
                    ),
                    duration_seconds=time.monotonic() - hold_started,
                    level=logging.WARNING,
                )
                raise
            else:
                self._trace(
                    "mouse.click.hold",
                    f"鼠标左键按住结束：实际 {time.monotonic() - hold_started:.3f} 秒。",
                    duration_seconds=time.monotonic() - hold_started,
                )
            finally:
                up()
                self._trace(
                    "mouse.click.up",
                    f"Windows 已接受鼠标左键抬起事件：坐标 ({point.x}, {point.y})。",
                )
        else:
            # Keep test/custom backends compatible.  The production Win32
            # backend always takes the separately logged down/up branch.
            self.backend.left_click()
            self._trace(
                "mouse.click",
                f"鼠标左键单击事件已发送：坐标 ({point.x}, {point.y})。",
            )

        post_click_delay = self.rng.uniform(
            self.policy.click_wait_min,
            self.policy.click_wait_max,
        )
        post_started = time.monotonic()
        self._trace(
            "mouse.post_click_wait",
            f"鼠标点击后等待开始：计划 {post_click_delay:.3f} 秒。",
        )
        self._wait(post_click_delay, cancel_event)
        self._trace(
            "mouse.post_click_wait",
            f"鼠标点击后等待结束：实际 {time.monotonic() - post_started:.3f} 秒。",
            duration_seconds=time.monotonic() - post_started,
        )


@dataclass(frozen=True)
class ScrollAction:
    performed: bool
    delta: int
    step: int
    accumulated_distance: int
    cursor: Point | None
    reason: str


class BoundedListScroller:
    """Permit one small list scroll, then require a visual recheck.

    Calling :meth:`scroll_once` twice without :meth:`record_recheck` is an
    error.  This prevents a timing bug from turning bounded searching into a
    continuous blind scroll.
    """

    def __init__(
        self,
        interaction: RandomizedInteraction,
        list_bounds: Rect,
        *,
        max_steps: int = 6,
        max_accumulated_distance: int = 900,
        step_distance_min: int = 90,
        step_distance_max: int = 180,
    ) -> None:
        if max_steps < 1:
            raise ValueError("列表滚动上限至少为 1 次。")
        if max_accumulated_distance < 1:
            raise ValueError("列表累计滚动距离上限必须为正数。")
        if not 1 <= step_distance_min <= step_distance_max:
            raise ValueError("列表单次滚动距离范围无效。")
        self.interaction = interaction
        self.list_bounds = list_bounds
        self.max_steps = max_steps
        self.max_accumulated_distance = max_accumulated_distance
        self.step_distance_min = step_distance_min
        self.step_distance_max = step_distance_max
        self.steps = 0
        self.accumulated_distance = 0
        self.awaiting_recheck = False
        self.target_visible = False

    def record_recheck(self, *, target_visible: bool) -> None:
        if not self.awaiting_recheck:
            raise RuntimeError("当前没有等待确认的列表滚动。")
        self.awaiting_recheck = False
        self.target_visible = bool(target_visible)

    def scroll_once(
        self,
        direction: int,
        *,
        cancel_event: threading.Event | None = None,
    ) -> ScrollAction:
        if direction not in (-1, 1):
            raise ValueError("滚动方向只能是 -1（向下）或 1（向上）。")
        if self.awaiting_recheck:
            raise RuntimeError("必须先重新截图识别列表，不能连续盲滚。")
        if self.target_visible:
            return self._stopped("target_visible")
        if self.steps >= self.max_steps:
            return self._stopped("step_limit")
        remaining = self.max_accumulated_distance - self.accumulated_distance
        if remaining <= 0:
            return self._stopped("distance_limit")
        distance = min(
            remaining,
            self.interaction.rng.randint(
                self.step_distance_min,
                self.step_distance_max,
            ),
        )
        cursor = self.interaction.move_to_rect(
            self.list_bounds,
            cancel_event=cancel_event,
        )
        self.interaction._raise_if_cancelled(cancel_event)
        delta = direction * distance
        self.interaction.backend.vertical_scroll(delta)
        self.steps += 1
        self.accumulated_distance += distance
        self.awaiting_recheck = True
        return ScrollAction(
            performed=True,
            delta=delta,
            step=self.steps,
            accumulated_distance=self.accumulated_distance,
            cursor=cursor,
            reason="scrolled_recheck_required",
        )

    def _stopped(self, reason: str) -> ScrollAction:
        return ScrollAction(
            performed=False,
            delta=0,
            step=self.steps,
            accumulated_distance=self.accumulated_distance,
            cursor=None,
            reason=reason,
        )
