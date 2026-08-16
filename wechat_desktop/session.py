"""Bounded Win32 window discovery, foreground preparation, and screenshots."""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes
from typing import Any, Callable, Protocol, Sequence

from .models import CapturedFrame, Rect, WindowSnapshot
from .tray import TrayActivationError, WeChatTrayActivator


log = logging.getLogger("wechat_automation.window")


class DesktopSessionError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


class WindowApi(Protocol):
    def enable_per_monitor_dpi(self) -> None:
        ...

    def enum_windows(self) -> Sequence[int]:
        ...

    def is_visible(self, handle: int) -> bool:
        ...

    def is_minimized(self, handle: int) -> bool:
        ...

    def is_cloaked(self, handle: int) -> bool:
        ...

    def title(self, handle: int) -> str:
        ...

    def class_name(self, handle: int) -> str:
        ...

    def restore(self, handle: int) -> None:
        ...

    def set_foreground(self, handle: int) -> None:
        ...

    def set_window_pos(
        self,
        handle: int,
        left: int,
        top: int,
        width: int,
        height: int,
    ) -> None:
        ...

    def foreground(self) -> int:
        ...

    def process_id(self, handle: int) -> int:
        ...

    def dpi(self, handle: int) -> int:
        ...

    def window_rect(self, handle: int) -> Rect:
        ...

    def client_rect(self, handle: int) -> Rect:
        ...


class ScreenGrabber(Protocol):
    def grab(self, bounds: Rect) -> Any:
        ...


class Win32WindowApi:
    """Read and activate top-level windows using documented Win32 APIs."""

    def __init__(self) -> None:
        try:
            import win32gui
            import win32process
        except ImportError as exc:  # pragma: no cover - runtime dependency
            raise DesktopSessionError(
                "missing_dependency",
                "缺少 pywin32，请先运行 first.bat 安装项目依赖。",
            ) from exc
        self.win32gui = win32gui
        self.win32process = win32process

    def enable_per_monitor_dpi(self) -> None:
        # DPI awareness can only be chosen once per process.  Access denied here
        # normally means a launcher or imported GUI framework already selected it.
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            setter = getattr(user32, "SetProcessDpiAwarenessContext", None)
            if setter is not None:
                setter.argtypes = [wintypes.HANDLE]
                setter.restype = wintypes.BOOL
                setter(ctypes.c_void_p(-4))  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        except Exception:
            pass

    def enum_windows(self) -> Sequence[int]:
        handles: list[int] = []

        def callback(handle: int, _extra: Any) -> bool:
            handles.append(int(handle))
            return True

        self.win32gui.EnumWindows(callback, None)
        return handles

    def is_visible(self, handle: int) -> bool:
        return bool(self.win32gui.IsWindowVisible(handle))

    def is_minimized(self, handle: int) -> bool:
        return bool(self.win32gui.IsIconic(handle))

    def is_cloaked(self, handle: int) -> bool:
        try:
            dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
            value = wintypes.DWORD(0)
            result = dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(handle),
                wintypes.DWORD(14),  # DWMWA_CLOAKED
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
            return result == 0 and bool(value.value)
        except Exception:
            return False

    def title(self, handle: int) -> str:
        return str(self.win32gui.GetWindowText(handle) or "")

    def class_name(self, handle: int) -> str:
        return str(self.win32gui.GetClassName(handle) or "")

    def restore(self, handle: int) -> None:
        self.win32gui.ShowWindow(handle, 9)  # SW_RESTORE

    def set_foreground(self, handle: int) -> None:
        # Bringing the top-level window to the front first helps when the
        # caller is a browser/console process and Windows has not granted it
        # the foreground lock yet.  SetForegroundWindow may still be rejected;
        # the session layer treats that as a retryable condition.
        bring_to_top = getattr(self.win32gui, "BringWindowToTop", None)
        if callable(bring_to_top):
            bring_to_top(handle)
        self.win32gui.SetForegroundWindow(handle)

    def set_window_pos(
        self,
        handle: int,
        left: int,
        top: int,
        width: int,
        height: int,
    ) -> None:
        # Keep z-order and activation unchanged.  ``prepare`` explicitly brings
        # the window to the foreground after applying its requested geometry.
        flags = 0x0004 | 0x0010  # SWP_NOZORDER | SWP_NOACTIVATE
        try:
            result = self.win32gui.SetWindowPos(
                handle,
                0,
                int(left),
                int(top),
                int(width),
                int(height),
                flags,
            )
        except Exception as exc:
            raise DesktopSessionError(
                "wechat_window_geometry_failed",
                "无法调整微信窗口的位置或大小；为避免使用错误坐标，自动化已停止。",
                details={"error": type(exc).__name__},
            ) from exc
        if result is False:
            raise DesktopSessionError(
                "wechat_window_geometry_failed",
                "Windows 拒绝调整微信窗口的位置或大小；自动化已停止。",
            )

    def foreground(self) -> int:
        return int(self.win32gui.GetForegroundWindow() or 0)

    def process_id(self, handle: int) -> int:
        _thread_id, process_id = self.win32process.GetWindowThreadProcessId(handle)
        return int(process_id)

    def dpi(self, handle: int) -> int:
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            getter = getattr(user32, "GetDpiForWindow", None)
            if getter is None:
                return 96
            getter.argtypes = [wintypes.HWND]
            getter.restype = wintypes.UINT
            return int(getter(handle) or 96)
        except Exception:
            return 96

    def window_rect(self, handle: int) -> Rect:
        left, top, right, bottom = self.win32gui.GetWindowRect(handle)
        return Rect(int(left), int(top), int(right), int(bottom))

    def client_rect(self, handle: int) -> Rect:
        left, top, right, bottom = self.win32gui.GetClientRect(handle)
        screen_left, screen_top = self.win32gui.ClientToScreen(handle, (left, top))
        screen_right, screen_bottom = self.win32gui.ClientToScreen(handle, (right, bottom))
        return Rect(
            int(screen_left),
            int(screen_top),
            int(screen_right),
            int(screen_bottom),
        )


class PillowScreenGrabber:
    def grab(self, bounds: Rect) -> Any:
        try:
            from PIL import ImageGrab
        except ImportError as exc:  # pragma: no cover - runtime dependency
            raise DesktopSessionError(
                "missing_dependency",
                "缺少 Pillow，请先运行 first.bat 安装项目依赖。",
            ) from exc
        return ImageGrab.grab(
            bbox=(bounds.left, bounds.top, bounds.right, bounds.bottom),
            all_screens=True,
        )


class WeChatWindowSession:
    """Own the unique, visible WeChat main window used by one transaction."""

    MAIN_TITLES = {"微信", "wechat", "weixin"}
    MAIN_CLASSES = {
        "Qt51514QWindowIcon",
        "WeChatMainWndForPC",
    }

    def __init__(
        self,
        *,
        api: WindowApi | None = None,
        grabber: ScreenGrabber | None = None,
        position_enabled: bool = False,
        target_x: int = 100,
        target_y: int = 80,
        size_enabled: bool = False,
        target_width: int = 900,
        target_height: int = 700,
        tray_activation_enabled: bool = True,
        tray_activation_timeout: float = 3.0,
        tray_activator: WeChatTrayActivator | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api = api or Win32WindowApi()
        self.grabber = grabber or PillowScreenGrabber()
        self.sleep = sleep
        self.monotonic = monotonic
        self.handle: int | None = None
        self.position_enabled = bool(position_enabled)
        self.target_x = int(target_x)
        self.target_y = int(target_y)
        self.size_enabled = bool(size_enabled)
        self.target_width = int(target_width)
        self.target_height = int(target_height)
        self.tray_activation_enabled = bool(tray_activation_enabled)
        self.tray_activation_timeout = float(tray_activation_timeout)
        self.tray_activator = tray_activator
        if not 0.1 <= self.tray_activation_timeout <= 30:
            raise ValueError("托盘唤醒等待时间必须在 0.1 到 30 秒之间。")

    @classmethod
    def _is_main_window(cls, title: str, class_name: str) -> bool:
        return (
            title.strip().casefold() in {item.casefold() for item in cls.MAIN_TITLES}
            and class_name in cls.MAIN_CLASSES
        )

    @staticmethod
    def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise DesktopSessionError(
                "automation_cancelled",
                "自动化已停止，未继续操作微信窗口。",
            )

    def _try_set_foreground(self, handle: int) -> str:
        """Request foreground activation without leaking a raw pywin32 error."""

        try:
            self.api.set_foreground(handle)
            return ""
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}".strip()
            log.warning(
                "Windows 暂时拒绝将微信窗口切换到前台，将在准备窗口期间重试：%s",
                detail,
                extra={"automation_operation": "window.foreground"},
            )
            return detail

    def discover(self) -> int:
        self.api.enable_per_monitor_dpi()
        candidates: list[int] = []
        for handle in self.api.enum_windows():
            try:
                # A taskbar-minimized WeChat HWND can temporarily report
                # IsWindowVisible=False while IsIconic=True. It is still the
                # safest recovery target and must be restored before falling
                # back to notification-area automation. Hidden, non-minimized
                # helper windows remain excluded.
                minimized = self.api.is_minimized(handle)
                if (
                    (not self.api.is_visible(handle) and not minimized)
                    or self.api.is_cloaked(handle)
                ):
                    continue
                if self._is_main_window(
                    self.api.title(handle),
                    self.api.class_name(handle),
                ):
                    candidates.append(int(handle))
            except Exception:
                continue
        if not candidates:
            raise DesktopSessionError(
                "wechat_not_found",
                "没有找到可恢复的微信主窗口。请先启动并登录 Windows 桌面微信。",
            )
        unique = sorted(set(candidates))
        if len(unique) != 1:
            raise DesktopSessionError(
                "wechat_window_ambiguous",
                "检测到多个微信主窗口，无法安全判断应操作哪一个。",
                details={"candidate_count": len(unique), "handles": unique},
            )
        self.handle = unique[0]
        return self.handle

    def read_current_window_snapshot(self) -> WindowSnapshot:
        """Read visible WeChat geometry and DPI without activating or clicking it."""

        handle = self.discover()
        if self.api.is_minimized(handle):
            raise DesktopSessionError(
                "wechat_window_minimized",
                "微信窗口当前已最小化。请先恢复窗口、摆放到目标位置，再重新读取。",
            )
        try:
            snapshot = self.snapshot()
        except Exception as exc:
            if isinstance(exc, DesktopSessionError):
                raise
            raise DesktopSessionError(
                "wechat_window_read_failed",
                "无法读取微信窗口的位置、大小或 DPI。",
                details={"error": type(exc).__name__},
            ) from exc
        if self._is_offscreen_minimized(snapshot.window_rect):
            raise DesktopSessionError(
                "wechat_window_minimized",
                "微信窗口仍处于最小化屏幕外坐标。请先恢复窗口再重新读取。",
            )
        return snapshot

    def read_current_window_rect(self) -> Rect:
        """Read the unique visible WeChat outer rectangle without activating it."""

        return self.read_current_window_snapshot().window_rect

    def _apply_requested_geometry(self, handle: int) -> Rect:
        current = self.api.window_rect(handle)
        if not self.position_enabled and not self.size_enabled:
            return current
        requested_left = self.target_x if self.position_enabled else current.left
        requested_top = self.target_y if self.position_enabled else current.top
        requested_width = self.target_width if self.size_enabled else current.width
        requested_height = self.target_height if self.size_enabled else current.height
        try:
            self.api.set_window_pos(
                handle,
                requested_left,
                requested_top,
                requested_width,
                requested_height,
            )
            actual = self.api.window_rect(handle)
        except DesktopSessionError:
            raise
        except Exception as exc:
            raise DesktopSessionError(
                "wechat_window_geometry_failed",
                "无法调整微信窗口的位置或大小；为避免使用错误坐标，自动化已停止。",
                details={"error": type(exc).__name__},
            ) from exc
        requested = (requested_left, requested_top, requested_width, requested_height)
        applied = (actual.left, actual.top, actual.width, actual.height)
        if applied != requested:
            log.warning(
                "微信窗口受 Windows 或微信尺寸限制，实际外框与设置值不同：请求=%s，实际=%s。",
                requested,
                applied,
                extra={"automation_operation": "window.geometry"},
            )
        else:
            log.info(
                "微信窗口外框已应用：位置=(%s, %s)，大小=%sx%s。",
                actual.left,
                actual.top,
                actual.width,
                actual.height,
                extra={"automation_operation": "window.geometry"},
            )
        return actual

    @staticmethod
    def _is_offscreen_minimized(bounds: Rect) -> bool:
        return bounds.left <= -30000 or bounds.top <= -30000

    def snapshot(self) -> WindowSnapshot:
        if self.handle is None:
            raise DesktopSessionError(
                "wechat_session_missing",
                "尚未建立微信窗口会话。",
            )
        handle = self.handle
        try:
            window_rect = self.api.window_rect(handle)
            client_rect = self.api.client_rect(handle)
            if self._is_offscreen_minimized(window_rect):
                raise DesktopSessionError(
                    "wechat_window_minimized",
                    "微信窗口仍处于最小化屏幕外坐标，不能进行视觉识别。",
                    details={
                        "window_rect": (
                            window_rect.left,
                            window_rect.top,
                            window_rect.right,
                            window_rect.bottom,
                        )
                    },
                )
            return WindowSnapshot(
                handle=handle,
                process_id=self.api.process_id(handle),
                title=self.api.title(handle),
                class_name=self.api.class_name(handle),
                window_rect=window_rect,
                client_rect=client_rect,
                dpi=self.api.dpi(handle),
                is_foreground=self.api.foreground() == handle,
            )
        except DesktopSessionError:
            raise
        except Exception as exc:
            raise DesktopSessionError(
                "wechat_window_read_failed",
                "无法读取微信窗口的屏幕位置。",
                details={"error": type(exc).__name__},
            ) from exc

    def prepare(
        self,
        *,
        timeout: float = 5.0,
        stable_for: float = 0.0,
        cancel_event: threading.Event | None = None,
    ) -> WindowSnapshot:
        if timeout <= 0:
            raise ValueError("窗口准备超时必须大于 0。")
        if stable_for < 0:
            raise ValueError("窗口稳定确认时间不能小于 0。")
        if stable_for >= timeout:
            raise ValueError("窗口稳定确认时间必须小于窗口准备超时。")
        self._raise_if_cancelled(cancel_event)
        try:
            handle = self.discover()
        except DesktopSessionError as exc:
            if exc.code != "wechat_not_found":
                raise
            if not self.tray_activation_enabled:
                raise DesktopSessionError(
                    "wechat_not_found",
                    (
                        "没有找到可恢复的微信主窗口，且“从通知区域唤醒微信”未开启。"
                        "请先恢复微信窗口，或在自动化配置的全局运行环境中开启该选项。"
                    ),
                ) from exc
            activator = self.tray_activator or WeChatTrayActivator()
            try:
                activator.activate(
                    timeout=self.tray_activation_timeout,
                    cancel_event=cancel_event,
                )
            except TrayActivationError as tray_error:
                raise DesktopSessionError(
                    tray_error.code,
                    str(tray_error),
                    details=tray_error.details,
                ) from tray_error
            # Finding and clicking the tray icon has its own timeout.  Start a
            # fresh budget only after that click for the main-window response.
            activation_deadline = self.monotonic() + self.tray_activation_timeout
            while True:
                self._raise_if_cancelled(cancel_event)
                try:
                    handle = self.discover()
                    break
                except DesktopSessionError as retry_error:
                    if retry_error.code != "wechat_not_found":
                        raise
                if self.monotonic() >= activation_deadline:
                    raise DesktopSessionError(
                        "wechat_tray_activation_timeout",
                        (
                            "已通过鼠标单击微信托盘图标，但微信主窗口未在限定时间内出现。"
                            "请检查托盘图标是否属于已登录的电脑端微信。"
                        ),
                        details={"timeout": self.tray_activation_timeout},
                    )
                if cancel_event is not None:
                    if cancel_event.wait(0.05):
                        self._raise_if_cancelled(cancel_event)
                else:
                    self.sleep(0.05)
        if self.api.is_minimized(handle):
            self.api.restore(handle)
        last_foreground_error = self._try_set_foreground(handle)
        deadline = self.monotonic() + timeout
        next_foreground_retry = self.monotonic() + 0.20
        geometry_applied = False
        stable_since: float | None = None
        stable_state: tuple[Rect, Rect, int] | None = None
        while True:
            self._raise_if_cancelled(cancel_event)
            now = self.monotonic()
            if self.api.is_minimized(handle):
                # The browser click that started a check can race with a WeChat
                # minimize animation.  Restore it and restart the stability
                # window instead of accepting an earlier foreground sample.
                self.api.restore(handle)
                error = self._try_set_foreground(handle)
                if error:
                    last_foreground_error = error
                next_foreground_retry = now + 0.20
                stable_since = None
                stable_state = None
            if (
                self.api.is_visible(handle)
                and not self.api.is_minimized(handle)
                and not self.api.is_cloaked(handle)
            ):
                if not geometry_applied:
                    self._apply_requested_geometry(handle)
                    geometry_applied = True
                    # SetWindowPos deliberately uses SWP_NOACTIVATE. Reassert
                    # the foreground afterwards before accepting a screenshot.
                    if self.position_enabled or self.size_enabled:
                        error = self._try_set_foreground(handle)
                        if error:
                            last_foreground_error = error
                if self.api.foreground() != handle:
                    stable_since = None
                    stable_state = None
                    if now >= next_foreground_retry:
                        error = self._try_set_foreground(handle)
                        if error:
                            last_foreground_error = error
                        next_foreground_retry = now + 0.20
                else:
                    snapshot = self.snapshot()
                    if not self._is_offscreen_minimized(snapshot.window_rect):
                        current_state = (
                            snapshot.window_rect,
                            snapshot.client_rect,
                            snapshot.dpi,
                        )
                        if stable_for == 0:
                            return snapshot
                        if current_state != stable_state:
                            stable_state = current_state
                            stable_since = now
                        elif stable_since is not None and now - stable_since >= stable_for:
                            return snapshot
            else:
                stable_since = None
                stable_state = None
            if self.monotonic() >= deadline:
                raise DesktopSessionError(
                    "wechat_foreground_timeout",
                    "微信窗口未能恢复到前台。请先手动点击一次微信窗口后重试；"
                    "为避免截错窗口，任务已停止。",
                    details={
                        "handle": handle,
                        "timeout": timeout,
                        "stable_for": stable_for,
                        "foreground_error": last_foreground_error,
                    },
                )
            if cancel_event is not None:
                if cancel_event.wait(0.05):
                    self._raise_if_cancelled(cancel_event)
            else:
                self.sleep(0.05)

    def capture_client(self) -> CapturedFrame:
        before = self.snapshot()
        if (
            not before.is_foreground
            or not self.api.is_visible(before.handle)
            or self.api.is_minimized(before.handle)
            or self.api.is_cloaked(before.handle)
        ):
            raise DesktopSessionError(
                "wechat_not_foreground",
                "微信已不在前台；为避免把其他窗口识别为微信，任务已停止。",
            )
        image = self.grabber.grab(before.client_rect)
        try:
            after = self.snapshot()
            stable = (
                after.is_foreground
                and self.api.is_visible(after.handle)
                and not self.api.is_minimized(after.handle)
                and not self.api.is_cloaked(after.handle)
                and after.window_rect == before.window_rect
                and after.client_rect == before.client_rect
                and after.dpi == before.dpi
            )
        except DesktopSessionError as exc:
            raise DesktopSessionError(
                "wechat_capture_invalidated",
                "微信窗口在截图过程中发生了最小化、切换或尺寸变化，本次截图已作废。",
                details={"cause": exc.code},
            ) from exc
        if not stable:
            raise DesktopSessionError(
                "wechat_capture_invalidated",
                "微信窗口在截图过程中发生了最小化、切换或尺寸变化，本次截图已作废。",
                details={
                    "before_window_rect": (
                        before.window_rect.left,
                        before.window_rect.top,
                        before.window_rect.right,
                        before.window_rect.bottom,
                    ),
                    "after_window_rect": (
                        after.window_rect.left,
                        after.window_rect.top,
                        after.window_rect.right,
                        after.window_rect.bottom,
                    ),
                    "before_client_rect": (
                        before.client_rect.left,
                        before.client_rect.top,
                        before.client_rect.right,
                        before.client_rect.bottom,
                    ),
                    "after_client_rect": (
                        after.client_rect.left,
                        after.client_rect.top,
                        after.client_rect.right,
                        after.client_rect.bottom,
                    ),
                    "before_dpi": before.dpi,
                    "after_dpi": after.dpi,
                    "after_foreground": after.is_foreground,
                },
            )
        return CapturedFrame(
            image=image,
            screen_rect=before.client_rect,
            window=before,
            captured_at=self.monotonic(),
        )
