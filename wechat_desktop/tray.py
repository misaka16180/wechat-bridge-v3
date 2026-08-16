"""Locate WeChat in the Windows notification area and wake it by mouse.

Accessibility is read-only here: it supplies a visible button name and screen
rectangle.  Activation itself always goes through ``RandomizedInteraction``;
no accessibility Invoke action, hook, injection, or process launch is used.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence

from .interaction import InteractionCancelled, RandomizedInteraction
from .models import Rect


log = logging.getLogger("wechat_automation.tray")


TASKBAR_BUTTON_CLASSES = {
    "Taskbar.TaskListButtonAutomationPeer",
    "Taskbar.TaskListLabeledButtonAutomationPeer",
}


def is_taskbar_button_source(source: str) -> bool:
    """Return true only for explicit Windows task-list button UIA classes."""

    class_name = str(source or "").rsplit(":", 1)[-1].strip()
    return class_name in TASKBAR_BUTTON_CLASSES


def is_wechat_shell_name(name: str) -> bool:
    """Match WeChat shell labels without accepting similarly named software."""

    compact = " ".join(str(name or "").split()).strip()
    if compact == "微信":
        return True
    if compact.startswith("微信"):
        suffix = compact[2:3]
        return not suffix or suffix in " -–—:：([（【\r\n"
    folded = compact.casefold()
    for prefix in ("wechat", "weixin"):
        if folded == prefix:
            return True
        if folded.startswith(prefix) and folded[len(prefix) : len(prefix) + 1] in {
            " ", "-", "–", "—", ":", "(", "["
        }:
            return True
    return False


class TrayActivationError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


@dataclass(frozen=True)
class TrayNode:
    name: str
    bounds: Rect
    source: str


@dataclass(frozen=True)
class TrayActivationResult:
    name: str
    bounds: Rect
    source: str


class TrayAccessibility(Protocol):
    def scan(self, area: str) -> Sequence[TrayNode]:
        """Return accessible visible nodes for ``main`` or ``overflow``."""


MAIN_TRAY_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
OVERFLOW_TRAY_CLASSES = {
    "NotifyIconOverflowWindow",
    "TopLevelWindowForOverflowXamlIsland",
}


def _visible_tray_handles(area: str) -> list[int]:
    """Enumerate known shell tray roots without pywin32 callback error leakage."""

    if area not in {"main", "overflow"}:
        raise ValueError("托盘区域只能是 main 或 overflow。")
    wanted = MAIN_TRAY_CLASSES if area == "main" else OVERFLOW_TRAY_CLASSES
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetClassNameW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    handles: list[int] = []

    def callback(handle: int, _extra: int) -> bool:
        try:
            buffer = ctypes.create_unicode_buffer(256)
            length = int(
                user32.GetClassNameW(wintypes.HWND(handle), buffer, len(buffer))
            )
            class_name = buffer.value[:length] if length else ""
            if class_name in wanted and user32.IsWindowVisible(wintypes.HWND(handle)):
                handles.append(int(handle))
        except Exception:
            pass
        return True

    ctypes.set_last_error(0)
    wrapped = callback_type(callback)
    if not user32.EnumWindows(wrapped, 0):
        error = int(ctypes.get_last_error())
        if error:
            raise TrayActivationError(
                "tray_window_enumeration_failed",
                "Windows 未能枚举系统托盘窗口。",
                details={"error": error},
            )
    return sorted(set(handles))


class MsaaTrayAccessibility:
    """Bounded MSAA traversal under known notification-area windows."""

    MAIN_CLASSES = MAIN_TRAY_CLASSES
    OVERFLOW_CLASSES = OVERFLOW_TRAY_CLASSES
    OBJID_CLIENT = -4
    WM_GETOBJECT = 0x003D
    STATE_SYSTEM_UNAVAILABLE = 0x00000001
    STATE_SYSTEM_INVISIBLE = 0x00008000
    STATE_SYSTEM_OFFSCREEN = 0x00010000

    def __init__(self, *, max_nodes: int = 512, max_depth: int = 18) -> None:
        self.max_nodes = int(max_nodes)
        self.max_depth = int(max_depth)

    @staticmethod
    def _modules():
        try:
            import pythoncom
            import pywintypes
            import win32com.client
            import win32con
            import win32gui
        except ImportError as exc:  # pragma: no cover - runtime dependency
            raise TrayActivationError(
                "missing_dependency",
                "缺少 pywin32，无法读取 Windows 系统托盘。请先运行 first.bat。",
            ) from exc
        return pythoncom, pywintypes, win32com.client, win32con, win32gui

    def _root_handles(self, area: str) -> list[int]:
        return _visible_tray_handles(area)

    @staticmethod
    def _guid(text: str) -> Any:
        class Guid(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        value = Guid()
        ctypes.OleDLL("ole32").CLSIDFromString(text, ctypes.byref(value))
        return value

    @staticmethod
    def _message_lresult(value: Any) -> int:
        """Normalize pywin32's version-dependent SendMessageTimeout result."""

        # pywin32 builds have exposed either the LRESULT itself or a
        # ``(status, LRESULT)`` pair.  Treating the integer form as a pair was
        # the cause of the tray ``TypeError`` seen with pywin32 311.
        if isinstance(value, (tuple, list)):
            if not value:
                return 0
            value = value[-1]
        return int(value or 0)

    @staticmethod
    def _error_detail(phase: str, exc: BaseException) -> dict[str, str]:
        return {
            "phase": phase,
            "error": type(exc).__name__,
            "message": str(exc).strip()[:300],
        }

    def _accessible_from_window(self, handle: int) -> Any:
        pythoncom, pywintypes, client, win32con, win32gui = self._modules()
        iid_text = "{618736E0-3C3D-11CF-810C-00AA00389B71}"
        iid = pywintypes.IID(iid_text)
        phase_errors: list[dict[str, str]] = []
        try:
            result = self._message_lresult(
                win32gui.SendMessageTimeout(
                    handle,
                    self.WM_GETOBJECT,
                    0,
                    self.OBJID_CLIENT,
                    win32con.SMTO_ABORTIFHUNG,
                    1000,
                )
            )
            if result:
                return client.Dispatch(pythoncom.ObjectFromLresult(result, iid, 0))
        except Exception as exc:
            phase_errors.append(self._error_detail("wm_getobject", exc))

        # Some shell builds expose MSAA through AccessibleObjectFromWindow but
        # do not return it from WM_GETOBJECT until another accessibility client
        # has queried the window.  This documented fallback avoids that order
        # dependency without interacting with the target process.
        pointer = ctypes.c_void_p()
        iid_guid = self._guid(iid_text)
        try:
            accessible_from_window = ctypes.OleDLL(
                "oleacc"
            ).AccessibleObjectFromWindow
            accessible_from_window.argtypes = (
                wintypes.HWND,
                wintypes.DWORD,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            )
            accessible_from_window.restype = ctypes.HRESULT
            status = accessible_from_window(
                wintypes.HWND(int(handle)),
                wintypes.DWORD(self.OBJID_CLIENT & 0xFFFFFFFF),
                ctypes.byref(iid_guid),
                ctypes.byref(pointer),
            )
            if int(status) < 0:
                raise OSError(int(status), "AccessibleObjectFromWindow failed")
            if not pointer.value:
                raise OSError("AccessibleObjectFromWindow returned null")
            converter = ctypes.PyDLL(pythoncom.__file__).PyCom_PyObjectFromIUnknown
            converter.restype = ctypes.py_object
            converter.argtypes = (
                ctypes.c_void_p,
                ctypes.c_void_p,
                wintypes.BOOL,
            )
            # AccessibleObjectFromWindow already returns one owned reference;
            # the Python wrapper should consume it instead of leaking an
            # additional AddRef on every tray scan.
            wrapped = converter(pointer, ctypes.byref(iid_guid), False)
            if wrapped is None:
                raise OSError("PyCom_PyObjectFromIUnknown returned null")
            return client.Dispatch(wrapped)
        except Exception as exc:
            phase_errors.append(
                self._error_detail("accessible_object_from_window", exc)
            )
            raise TrayActivationError(
                "tray_accessibility_unavailable",
                "Windows 没有向程序公开系统托盘的可访问性信息。",
                details={
                    "handle": int(handle),
                    "error": type(exc).__name__,
                    "phase_errors": phase_errors,
                },
            ) from exc

    @staticmethod
    def _member(accessible: Any, name: str, child_id: int, default: Any = None) -> Any:
        try:
            value = getattr(accessible, name)
            return value(child_id) if callable(value) else value
        except Exception:
            return default

    def _snapshot(self, accessible: Any, child_id: int, source: str) -> TrayNode | None:
        name = str(self._member(accessible, "accName", child_id, "") or "").strip()
        if not name:
            return None
        state = int(self._member(accessible, "accState", child_id, 0) or 0)
        blocked = (
            self.STATE_SYSTEM_UNAVAILABLE
            | self.STATE_SYSTEM_INVISIBLE
            | self.STATE_SYSTEM_OFFSCREEN
        )
        if state & blocked:
            return None
        location = self._member(accessible, "accLocation", child_id)
        if not isinstance(location, (tuple, list)) or len(location) != 4:
            return None
        left, top, width, height = (int(round(float(value))) for value in location)
        if width < 3 or height < 3:
            return None
        return TrayNode(
            name=name,
            bounds=Rect(left, top, left + width, top + height),
            source=source,
        )

    def _walk(self, root: Any, source: str) -> list[TrayNode]:
        nodes: list[TrayNode] = []
        visited = 0

        def visit(accessible: Any, child_id: int, depth: int) -> None:
            nonlocal visited
            if depth > self.max_depth or visited >= self.max_nodes:
                return
            visited += 1
            node = self._snapshot(accessible, child_id, source)
            if node is not None:
                nodes.append(node)
            if child_id != 0:
                return
            try:
                count = min(int(accessible.accChildCount or 0), self.max_nodes - visited)
            except Exception:
                return
            for index in range(1, count + 1):
                if visited >= self.max_nodes:
                    break
                try:
                    child = accessible.accChild(index)
                except Exception:
                    child = None
                if child is None or isinstance(child, int):
                    visit(accessible, int(child or index), depth + 1)
                else:
                    visit(child, 0, depth + 1)

        visit(root, 0, 0)
        return nodes

    def scan(self, area: str) -> Sequence[TrayNode]:
        if area not in {"main", "overflow"}:
            raise ValueError("托盘区域只能是 main 或 overflow。")
        pythoncom, _pywintypes, _client, _win32con, _win32gui = self._modules()
        pythoncom.CoInitialize()
        nodes: list[TrayNode] = []
        errors: list[TrayActivationError] = []
        try:
            for handle in self._root_handles(area):
                try:
                    root = self._accessible_from_window(handle)
                    nodes.extend(self._walk(root, f"{area}:{handle}"))
                except TrayActivationError as exc:
                    errors.append(exc)
        finally:
            pythoncom.CoUninitialize()
        if not nodes and errors and area == "main":
            raise errors[0]
        return tuple(nodes)


class UiaTrayAccessibility:
    """Read bounded Windows Shell tray nodes through the documented UIA API.

    This scanner never queries the WeChat process.  It reads only Windows
    taskbar/overflow elements to obtain their accessible names and screen
    rectangles.  Activation remains an ordinary mouse click.
    """

    def __init__(self, *, max_nodes: int = 512) -> None:
        self.max_nodes = int(max_nodes)
        if self.max_nodes < 1:
            raise ValueError("UIA 托盘扫描节点上限必须大于 0。")

    @staticmethod
    def _modules():
        try:
            import comtypes
            import comtypes.client

            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient
        except ImportError as exc:  # pragma: no cover - runtime dependency
            raise TrayActivationError(
                "missing_dependency",
                "缺少 comtypes，无法读取 Windows 系统托盘。请先运行 first.bat。",
            ) from exc
        except Exception as exc:  # pragma: no cover - machine-specific COM setup
            raise TrayActivationError(
                "tray_uia_initialization_failed",
                "Windows UI Automation 初始化失败，无法读取系统托盘。",
                details={"error": type(exc).__name__, "message": str(exc)[:300]},
            ) from exc
        return comtypes, comtypes.client, UIAutomationClient

    @staticmethod
    def _rect_values(value: Any) -> tuple[int, int, int, int]:
        if all(hasattr(value, name) for name in ("left", "top", "right", "bottom")):
            return tuple(
                int(round(float(getattr(value, name))))
                for name in ("left", "top", "right", "bottom")
            )
        if isinstance(value, (tuple, list)) and len(value) == 4:
            return tuple(int(round(float(item))) for item in value)
        raise ValueError("UIA 元素没有返回有效矩形。")

    def _snapshot(self, element: Any, source: str) -> TrayNode | None:
        try:
            name = str(element.CurrentName or "").strip()
            if not name or bool(element.CurrentIsOffscreen):
                return None
            if hasattr(element, "CurrentIsEnabled") and not bool(element.CurrentIsEnabled):
                return None
            left, top, right, bottom = self._rect_values(
                element.CurrentBoundingRectangle
            )
            if right - left < 3 or bottom - top < 3:
                return None
            control_type = int(getattr(element, "CurrentControlType", 0) or 0)
            class_name = str(getattr(element, "CurrentClassName", "") or "")
        except Exception:
            return None
        return TrayNode(
            name=name,
            bounds=Rect(left, top, right, bottom),
            source=f"{source}:{control_type}:{class_name}",
        )

    def scan(self, area: str) -> Sequence[TrayNode]:
        if area not in {"main", "overflow"}:
            raise ValueError("托盘区域只能是 main 或 overflow。")
        comtypes, client, uia_module = self._modules()
        handles = _visible_tray_handles(area)
        nodes: list[TrayNode] = []
        errors: list[dict[str, Any]] = []
        comtypes.CoInitialize()
        try:
            automation = client.CreateObject(
                uia_module.CUIAutomation,
                interface=uia_module.IUIAutomation,
            )
            condition = automation.CreateTrueCondition()
            for handle in handles:
                try:
                    root = automation.ElementFromHandle(int(handle))
                    elements = root.FindAll(uia_module.TreeScope_Subtree, condition)
                    count = min(int(elements.Length), self.max_nodes)
                    for index in range(count):
                        element = elements.GetElement(index)
                        node = self._snapshot(element, f"uia:{area}:{handle}:{index}")
                        if node is not None:
                            nodes.append(node)
                except Exception as exc:
                    errors.append(
                        {
                            "handle": int(handle),
                            "error": type(exc).__name__,
                            "message": str(exc).strip()[:300],
                        }
                    )
        finally:
            comtypes.CoUninitialize()
        if not nodes and errors and area == "main":
            raise TrayActivationError(
                "tray_accessibility_unavailable",
                "Windows 没有向程序公开可读取的系统托盘元素。",
                details={"phase": "windows_uia", "errors": errors},
            )
        return tuple(nodes)


class WeChatTrayActivator:
    """Select one accessible WeChat tray icon, then wake it by visible mouse."""

    def __init__(
        self,
        *,
        accessibility: TrayAccessibility | None = None,
        interaction: RandomizedInteraction | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.accessibility = accessibility or UiaTrayAccessibility()
        self.interaction = interaction or RandomizedInteraction()
        self.monotonic = monotonic
        self.sleep = sleep

    @staticmethod
    def _cancelled(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise TrayActivationError(
                "automation_cancelled",
                "自动化已停止，未继续操作系统托盘。",
            )

    @staticmethod
    def _is_wechat_name(name: str) -> bool:
        return is_wechat_shell_name(name)

    @staticmethod
    def _is_overflow_button(name: str) -> bool:
        compact = " ".join(str(name or "").split()).strip().casefold()
        return (
            "显示隐藏的图标" in compact
            or "显示隐藏图标" in compact
            or "show hidden icons" in compact
            or "show hidden icon" in compact
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

    def _wechat_candidates(self, nodes: Sequence[TrayNode]) -> list[TrayNode]:
        # Shell_TrayWnd contains both the notification area and the task list.
        # A task-list button toggles a visible window and must never be treated
        # as a notification-area icon: doing so can minimize WeChat instead of
        # restoring it.
        return self._dedupe(
            [
                node
                for node in nodes
                if self._is_wechat_name(node.name)
                and not is_taskbar_button_source(node.source)
            ]
        )

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

    def _require_unique(self, candidates: Sequence[TrayNode]) -> TrayNode | None:
        if len(candidates) > 1:
            raise TrayActivationError(
                "wechat_tray_icon_ambiguous",
                "系统托盘中检测到多个微信候选图标，无法安全判断应点击哪一个。",
                details={
                    "candidate_count": len(candidates),
                    "candidates": self._candidate_details(candidates),
                },
            )
        return candidates[0] if candidates else None

    def _wait_overflow_candidate(
        self,
        deadline: float,
        cancel_event: threading.Event | None,
    ) -> TrayNode | None:
        while True:
            self._cancelled(cancel_event)
            nodes = self.accessibility.scan("overflow")
            candidate = self._require_unique(self._wechat_candidates(nodes))
            if candidate is not None:
                return candidate
            if self.monotonic() >= deadline:
                return None
            if cancel_event is not None:
                if cancel_event.wait(0.05):
                    self._cancelled(cancel_event)
            else:
                self.sleep(0.05)

    def activate(
        self,
        *,
        timeout: float = 3.0,
        cancel_event: threading.Event | None = None,
    ) -> TrayActivationResult:
        if not 0.1 <= float(timeout) <= 30:
            raise ValueError("托盘唤醒等待时间必须在 0.1 到 30 秒之间。")
        self._cancelled(cancel_event)
        main_nodes = tuple(self.accessibility.scan("main"))
        overflow_nodes = tuple(self.accessibility.scan("overflow"))
        candidate = self._require_unique(
            self._wechat_candidates((*main_nodes, *overflow_nodes))
        )
        if candidate is None:
            overflow_buttons = self._dedupe(
                [node for node in main_nodes if self._is_overflow_button(node.name)]
            )
            if len(overflow_buttons) > 1:
                raise TrayActivationError(
                    "tray_overflow_button_ambiguous",
                    "检测到多个“显示隐藏图标”按钮，无法安全打开托盘隐藏区。",
                    details={"candidate_count": len(overflow_buttons)},
                )
            if overflow_buttons:
                try:
                    self.interaction.click_rect(
                        overflow_buttons[0].bounds,
                        horizontal_ratio=0.20,
                        vertical_ratio=0.20,
                        cancel_event=cancel_event,
                    )
                except InteractionCancelled as exc:
                    raise TrayActivationError("automation_cancelled", str(exc)) from exc
                except Exception as exc:
                    raise TrayActivationError(
                        "tray_overflow_click_failed",
                        "鼠标未能打开系统托盘的隐藏图标区域。",
                        details={"error": type(exc).__name__},
                    ) from exc
                # The configured timeout belongs to the panel refresh itself.
                # Initial scans and the visible mouse click must not consume it.
                overflow_deadline = self.monotonic() + float(timeout)
                candidate = self._wait_overflow_candidate(
                    overflow_deadline,
                    cancel_event,
                )
        if candidate is None:
            raise TrayActivationError(
                "wechat_tray_icon_not_found",
                (
                    "未找到可恢复的微信窗口，也未在 Windows 通知区域找到唯一微信图标。"
                    "请确认电脑端微信已启动并登录；若微信仍显示在任务栏，请先手动恢复一次，"
                    "若已缩到通知区域，请确认微信图标可见。"
                ),
            )
        self._cancelled(cancel_event)
        try:
            point = self.interaction.click_rect(
                candidate.bounds,
                horizontal_ratio=0.20,
                vertical_ratio=0.20,
                cancel_event=cancel_event,
            )
        except InteractionCancelled as exc:
            raise TrayActivationError("automation_cancelled", str(exc)) from exc
        except Exception as exc:
            raise TrayActivationError(
                "wechat_tray_click_failed",
                "鼠标未能单击系统托盘中的微信图标。",
                details={"error": type(exc).__name__},
            ) from exc
        log.info(
            "已通过可见鼠标单击微信托盘图标：%s，坐标=(%s, %s)。",
            candidate.name,
            point.x,
            point.y,
            extra={"automation_operation": "tray.wechat_click"},
        )
        return TrayActivationResult(
            name=candidate.name,
            bounds=candidate.bounds,
            source=candidate.source,
        )
