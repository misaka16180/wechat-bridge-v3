"""Core v3 bridge: WeFlow events -> AstrBot, AstrBot actions -> visible UI send."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import secrets
import shutil
import threading
import time
from contextlib import ExitStack, contextmanager, nullcontext
from collections import OrderedDict, deque
from dataclasses import replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from bridge_config import BridgeConfig, ConfigError
from bridge_logging import read_log_tail, transport_event
from bridge_onebot import OneBotReverseClient
from bridge_protocol import (
    ContactRegistry,
    OutboundMessage,
    OutboundSegment,
    ProtocolError,
    build_weflow_event,
    classify_weflow_message,
    failed_response,
    is_internal_group_name,
    ok_response,
    parse_outbound_message,
    stable_id,
    weflow_contact,
)
from v3_version import VERSION
from bridge_weflow import WeFlowSseClient
from bridge_media import MediaResolver, ResolvedMedia, send_media
from wechat_sender import (
    SenderError,
    SendResult,
    WindowsAutomationStopHotkey,
    send_message,
)
from wechat_qt_accessibility import (
    QT_HOT_ACTIVATION_NOTICE_VERSION,
    compatibility_hint,
)
from wechat_desktop.recognition_snapshot import (
    RecognitionSnapshotStore,
    recognition_run,
    recognition_run_if_missing,
)
from wechat_desktop.recognition_repair import (
    RecognitionRepairError,
    RecognitionRepairManager,
)
from wechat_desktop.session import DesktopSessionError, WeChatWindowSession
from wechat_desktop.tray import TrayActivationError, WeChatTrayActivator
from wechat_desktop.visual_compatibility import (
    ImportedScreenshotError,
    VisualCompatibilityChecker,
    decode_imported_screenshot,
    validate_imported_screenshot_request,
)


log = logging.getLogger("wechat_bridge.service")

DEBUG_AUTOMATION_LOGGER_NAMES = (
    "wechat_bridge.service",
    "wechat_automation.target",
    "wechat_automation.interaction",
    "wechat_automation.sender",
    "wechat_automation.media",
    "wechat_automation.snapshot",
)


@contextmanager
def _best_effort_stop_hotkey(manager: Any):
    """Enter a task hotkey without turning registration failure into send failure."""

    with ExitStack() as stack:
        try:
            stack.enter_context(manager)
        except Exception as exc:
            log.warning("无法注册自动化停止快捷键，将继续执行：%s", exc)
        yield


class BridgeService:
    SEND_ACTIONS = {"send_msg", "send_private_msg", "send_group_msg"}

    def __init__(
        self,
        config: BridgeConfig,
        *,
        sender: Callable[..., SendResult] = send_message,
        media_sender: Callable[..., SendResult] = send_media,
        onebot_factory: Callable[..., OneBotReverseClient] = OneBotReverseClient,
        weflow_factory: Callable[..., WeFlowSseClient] = WeFlowSseClient,
        monotonic: Callable[[], float] = time.monotonic,
        async_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        stop_hotkey_factory: Callable[..., Any] = WindowsAutomationStopHotkey,
        window_session_factory: Callable[..., WeChatWindowSession] = WeChatWindowSession,
        tray_activator_factory: Callable[..., WeChatTrayActivator] = WeChatTrayActivator,
    ) -> None:
        self.config = config
        self.registry = ContactRegistry(config.state_file)
        self._sender = sender
        self._media_sender = media_sender
        self._onebot_factory = onebot_factory
        self._weflow_factory = weflow_factory
        self._send_lock = threading.Lock()
        # A new Event is created for each service run. Existing workers retain
        # the previous generation, so stop -> start can never revive a task
        # that was cancelled by the stop request.
        self._automation_cancel = threading.Event()
        self._automation_enabled = True
        self._automation_active = 0
        self._automation_stop_reason = ""
        self._automation_stop_sequence = 0
        self._stop_hotkey_factory = stop_hotkey_factory
        self._window_session_factory = window_session_factory
        self._tray_activator_factory = tray_activator_factory
        self._monotonic = monotonic
        self._async_sleep = async_sleep
        self._state_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._running = False
        self._started_at = 0.0
        self._processed: OrderedDict[str, float] = OrderedDict()
        self._response_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._recent_outbound: deque[tuple[float, str, str, str]] = deque(maxlen=500)
        self._last_inbound_at: dict[int, float] = {}
        self._events: deque[dict[str, Any]] = deque(maxlen=200)
        self._debug_tasks: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._debug_task_cancels: dict[str, threading.Event] = {}
        self._api_tasks: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._api_task_cancels: dict[str, threading.Event] = {}
        self._api_request_index: dict[str, tuple[str, str]] = {}
        self._api_queue: deque[str] = deque()
        self._api_worker: Optional[threading.Thread] = None
        self._api_uploads: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._debug_uploads: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._debug_lock = threading.RLock()
        self._api_lock = threading.RLock()
        self._recognition_store = RecognitionSnapshotStore(
            Path(config.logging.directory) / "recognition_snapshots"
        )
        self._recognition_repair = RecognitionRepairManager(self._recognition_store)
        self._counters = {
            "weflow_received": 0,
            "weflow_forwarded": 0,
            "weflow_deduplicated": 0,
            "weflow_system_filtered": 0,
            "astrbot_actions": 0,
            "send_succeeded": 0,
            "send_failed": 0,
        }
        self._onebot_generation = 1
        self._weflow_generation = 1
        self._onebot_client_generation = 1
        self._weflow_client_generation = 1
        self._onebot_reconnecting = False
        self._weflow_reconnecting = False
        self._onebot_needs_rebuild = False
        self._weflow_needs_rebuild = False
        self.onebot = self._make_onebot(
            config.astrbot,
            config.self_id,
            self._onebot_generation,
        )
        self.weflow = self._make_weflow(
            config.weflow,
            self._weflow_generation,
        )

    def _connection_is_current(self, channel: str, generation: int) -> bool:
        with self._state_lock:
            current = (
                self._onebot_generation
                if channel == "astrbot"
                else self._weflow_generation
            )
            return self._running and generation == current

    def _make_onebot(
        self,
        config: Any,
        self_id: int,
        generation: int,
    ) -> Any:
        async def guarded_action(request: dict[str, Any]) -> dict[str, Any]:
            if not self._connection_is_current("astrbot", generation):
                echo = request.get("echo") if isinstance(request, dict) else None
                return failed_response(
                    "该 AstrBot 连接已停止或已被新连接替换，动作未执行。",
                    echo=echo,
                    code="stale_connection",
                )
            return await self.handle_onebot_action(request)

        return self._onebot_factory(config, self_id, guarded_action)

    def _make_weflow(self, config: Any, generation: int) -> Any:
        def guarded_message(data: dict[str, Any]) -> None:
            if not self._connection_is_current("weflow", generation):
                return
            self.handle_weflow_message(data)

        return self._weflow_factory(config, guarded_message)

    @staticmethod
    def _request_client_stop(client: Any) -> None:
        request_stop = getattr(client, "request_stop", None)
        if callable(request_stop):
            request_stop()
        else:
            client.stop()

    @staticmethod
    def _wait_client_stopped(client: Any, *, timeout: float = 2.0) -> bool:
        """Wait for a replaced transport so an old endpoint cannot linger.

        Configuration changes are allowed to block briefly.  This is different
        from the web-console Stop action, which intentionally signals clients
        without waiting.  Waiting here prevents an old AstrBot/WeFlow worker
        from surviving long enough to look like the newly configured client.
        """

        wait_stopped = getattr(client, "wait_stopped", None)
        if not callable(wait_stopped):
            return True
        try:
            return bool(wait_stopped(timeout=timeout))
        except TypeError:
            return bool(wait_stopped())

    def _replace_onebot(self, config: Any, self_id: int) -> Any:
        old = self.onebot
        with self._state_lock:
            self._onebot_generation += 1
            generation = self._onebot_generation
            # Invalidate the old client's heartbeat before waiting for its
            # thread to leave. Concurrent status reads must not present the
            # previous endpoint as the newly configured connection.
            self._onebot_reconnecting = True
        self._request_client_stop(old)
        if not self._wait_client_stopped(old):
            log.warning(
                "旧 AstrBot OneBot 连接未在替换时限内退出，仍将切换到新目标。"
            )
        new = self._make_onebot(config, self_id, generation)
        with self._state_lock:
            self.onebot = new
            self._onebot_client_generation = generation
            self._onebot_needs_rebuild = False
        return old

    def _replace_weflow(self, config: Any) -> Any:
        old = self.weflow
        with self._state_lock:
            self._weflow_generation += 1
            generation = self._weflow_generation
            self._weflow_reconnecting = True
        self._request_client_stop(old)
        if not self._wait_client_stopped(old):
            log.warning(
                "旧 WeFlow SSE 连接未在替换时限内退出，仍将切换到新目标。"
            )
        new = self._make_weflow(config, generation)
        with self._state_lock:
            self.weflow = new
            self._weflow_client_generation = generation
            self._weflow_needs_rebuild = False
        return old

    def _event(self, level: str, code: str, message: str, **details: Any) -> None:
        item = {
            "time": int(time.time()),
            "level": level,
            "code": code,
            "message": message,
            "details": details,
        }
        with self._state_lock:
            self._events.append(item)
        getattr(log, level if level in {"debug", "info", "warning", "error"} else "info")(
            "%s: %s", code, message
        )

    def start(self) -> None:
        with self._lifecycle_lock:
            self._start_locked()

    def _start_locked(self) -> None:
        with self._state_lock:
            if self._running:
                return
            rebuild_onebot = self._onebot_needs_rebuild
            rebuild_weflow = self._weflow_needs_rebuild
        if rebuild_onebot:
            self._replace_onebot(self.config.astrbot, self.config.self_id)
        if rebuild_weflow:
            self._replace_weflow(self.config.weflow)
        with self._state_lock:
            self._running = True
            self._started_at = time.time()
        self._event("info", "bridge_started", "v3 桥接已启动。")
        if self.config.astrbot.enabled:
            self.onebot.start()
        else:
            self._event(
                "info",
                "astrbot_disabled",
                "AstrBot 连接未启用；可在控制台完成配置后启用。",
            )
        if self.config.weflow.enabled:
            self.weflow.start()
        else:
            self._event(
                "info",
                "weflow_disabled",
                "WeFlow 连接未启用；可在控制台完成配置后启用。",
            )
        if self.config.astrbot.enabled and not self.config.astrbot.token:
            self._event(
                "info",
                "astrbot_no_auth",
                "AstrBot 已启用且 Token 为空，将按无鉴权方式连接。",
            )
        if self.config.weflow.enabled and not self.config.weflow.token:
            self._event(
                "info",
                "weflow_no_auth",
                "WeFlow 已启用且 Token 为空，将按无鉴权方式连接。",
            )

    def stop(
        self,
        progress: Optional[Callable[[str], None]] = None,
    ) -> None:
        with self._lifecycle_lock:
            self._stop_locked(progress)

    def _stop_locked(
        self,
        progress: Optional[Callable[[str], None]] = None,
    ) -> None:
        with self._state_lock:
            self._running = False
            self._onebot_generation += 1
            self._weflow_generation += 1
            self._onebot_needs_rebuild = True
            self._weflow_needs_rebuild = True
        clients = (
            ("WeFlow SSE", self.weflow),
            ("AstrBot OneBot WebSocket", self.onebot),
        )
        if progress:
            progress(
                "已请求停止本地桥接器；正在断开桥接器与 WeFlow、AstrBot "
                "建立的连接…"
            )
        # Notify both clients before waiting for either one.  Previously the
        # 3-second and 5-second joins could run serially during Ctrl+C.
        signaled: list[tuple[str, Any]] = []
        fallback: list[tuple[str, Any]] = []
        for label, client in clients:
            request_stop = getattr(client, "request_stop", None)
            if callable(request_stop):
                request_stop()
                signaled.append((label, client))
            else:
                fallback.append((label, client))
        if progress and signaled:
            progress("连接断开信号已发出，正在等待桥接连接线程退出…")
        # Ctrl+C supplies progress and waits briefly for orderly process exit.
        # The web console deliberately does not wait: its Stop button is an
        # emergency control and signalled clients clean up in the background.
        if progress:
            wait_deadline = time.monotonic() + 2.0
            for label, client in signaled:
                wait_stopped = getattr(client, "wait_stopped", None)
                remaining = max(0.0, wait_deadline - time.monotonic())
                stopped = (
                    True
                    if not callable(wait_stopped)
                    else wait_stopped(timeout=remaining)
                )
                progress(
                    f"桥接器的 {label} 连接已断开。"
                    if stopped
                    else f"桥接器的 {label} 连接未在退出时限内断开，"
                    "已转入后台清理。"
                )
        for label, client in fallback:
            if progress:
                progress(f"正在断开桥接器的 {label} 连接…")
            client.stop()
            if progress:
                progress(f"桥接器的 {label} 连接已断开。")
        with self._debug_lock:
            pending_uploads = list(self._debug_uploads.values())
            self._debug_uploads.clear()
        # The proactive Agent API is a sibling of the WeFlow/AstrBot bridge,
        # not one of its transports. Stopping the bridge must leave its queue
        # and desktop worker alone. Only stop_automation() (or an explicit
        # per-task cancellation) is allowed to cancel those tasks.
        for upload in pending_uploads:
            self._remove_debug_upload(upload)
        self._event("info", "bridge_stopped", "v3 桥接已停止。")

    def apply_config(self, config: BridgeConfig) -> dict[str, Any]:
        """Apply config while rebuilding only the connection that changed."""

        with self._lifecycle_lock:
            return self._apply_config_locked(config)

    def _apply_config_locked(self, config: BridgeConfig) -> dict[str, Any]:
        """Serialized implementation for live configuration replacement."""

        config.validate()
        with self._state_lock:
            previous = self.config
            running = self._running
            reload_astrbot = (
                previous.astrbot != config.astrbot
                or previous.self_id != config.self_id
            )
            reload_weflow = previous.weflow != config.weflow
            state_file_changed = previous.state_file != config.state_file

        if reload_astrbot:
            self._replace_onebot(config.astrbot, config.self_id)

        if reload_weflow:
            self._replace_weflow(config.weflow)

        with self._state_lock:
            self.config = config
        if reload_astrbot and running and config.astrbot.enabled:
            self.onebot.start()
        if reload_weflow and running and config.weflow.enabled:
            self.weflow.start()

        if state_file_changed:
            self.registry = ContactRegistry(config.state_file)
        self._event(
            "info",
            "config_applied",
            "控制台配置已应用。",
            astrbot_reloaded=reload_astrbot,
            weflow_reloaded=reload_weflow,
            astrbot_url=config.astrbot.url,
            weflow_url=config.weflow.url,
        )
        return {
            "connections_reloaded": reload_astrbot or reload_weflow,
            "astrbot_reloaded": reload_astrbot,
            "weflow_reloaded": reload_weflow,
            "running": running,
        }

    def start_automation(self) -> dict[str, Any]:
        with self._state_lock:
            if self._automation_enabled and not self._automation_cancel.is_set():
                return self.status()
            self._automation_cancel = threading.Event()
            self._automation_enabled = True
            self._automation_stop_reason = ""
        qt_status = self._qt_accessibility_payload()
        self._event(
            "info",
            "automation_started",
            "微信自动化已启动，可以接收新的发送任务。",
            qt_hot_activation_enabled=qt_status["enabled"],
            qt_start_reminder_required=qt_status["start_reminder_required"],
        )
        return self.status()

    def _qt_accessibility_payload(self) -> dict[str, Any]:
        automation = self.config.automation
        acknowledged = (
            automation.qt_hot_activation_notice_accepted
            == QT_HOT_ACTIVATION_NOTICE_VERSION
        )
        enabled = bool(automation.qt_hot_activation_enabled and acknowledged)
        reminder_disabled = bool(
            automation.qt_hot_activation_start_reminder_disabled
        )
        return {
            "enabled": enabled,
            "configured_enabled": bool(automation.qt_hot_activation_enabled),
            "risk_acknowledged": acknowledged,
            "notice_version": QT_HOT_ACTIVATION_NOTICE_VERSION,
            "start_reminder_disabled": reminder_disabled,
            "start_reminder_required": not enabled and not reminder_disabled,
            "activation_timing": "on_demand_after_wechat_window_is_ready",
            "tested_wechat_versions": ["4.1.12.26"],
            "process_access": "0x0038",
            "process_rights": [
                "PROCESS_VM_OPERATION",
                "PROCESS_VM_READ",
                "PROCESS_VM_WRITE",
            ],
            "creates_remote_thread": False,
            "injects_dll_or_code": False,
            "installs_hook": False,
            "restart_wechat_to_restore": True,
        }

    def stop_automation(self) -> None:
        automation_active = self._request_automation_stop("manual")
        self._event(
            "warning" if automation_active else "info",
            "automation_stop_requested",
            (
                "已请求停止微信自动化，正在取消当前任务并释放输入保护。"
                if automation_active
                else "微信自动化已停止；桥接连接保持不变。"
            ),
            automation_active=automation_active,
        )

    def _request_automation_stop(self, reason: str = "hotkey") -> int:
        """Set cancellation state without logging from a low-level hook."""

        with self._state_lock:
            transitioned = self._automation_enabled
            self._automation_enabled = False
            if transitioned:
                self._automation_stop_reason = str(reason or "hotkey")
                self._automation_stop_sequence += 1
            cancel_event = self._automation_cancel
            automation_active = self._automation_active
        cancel_event.set()
        with self._debug_lock:
            debug_events = list(self._debug_task_cancels.values())
        for event in debug_events:
            event.set()
        self._cancel_active_api_tasks(
            reason=str(reason or "automation_stopped"),
            message="微信自动化已停止，主动发送任务已取消。",
        )
        return automation_active

    def read_wechat_window_geometry(self) -> dict[str, int]:
        """Read the unique visible WeChat outer rectangle without changing it."""

        try:
            bounds = self._window_session_factory().read_current_window_rect()
        except DesktopSessionError as exc:
            raise ProtocolError(exc.code, str(exc)) from exc
        return {
            "x": bounds.left,
            "y": bounds.top,
            "width": bounds.width,
            "height": bounds.height,
        }

    def read_wechat_window_dpi(self) -> dict[str, int | float]:
        """Read the visible WeChat window DPI without moving or activating it."""

        try:
            snapshot = self._window_session_factory().read_current_window_snapshot()
        except DesktopSessionError as exc:
            raise ProtocolError(exc.code, str(exc)) from exc
        dpi = max(48, min(288, int(snapshot.dpi)))
        return {
            "dpi": dpi,
            "scale": round(dpi / 96.0, 4),
            "scale_percent": int(round(dpi / 96.0 * 100)),
        }

    @staticmethod
    def _assess_visual_scale(
        checker: Any,
        probes: Any,
        *,
        reported_scale: float,
        attempted_scales: tuple[float, ...],
    ) -> dict[str, Any]:
        assessor = getattr(checker, "assess_scale", None)
        if callable(assessor):
            return dict(
                assessor(
                    probes,
                    reported_scale=reported_scale,
                    attempted_scales=attempted_scales,
                )
            )
        # Keep injected test/dummy checkers and older extensions readable while
        # the persisted protocol remains the new staged shape.
        accepted = all(getattr(item.result, "accepted", False) for item in probes)
        percent = int(round(float(reported_scale) * 100))
        return {
            "status": "confirmed" if accepted else "unresolved",
            "reported_scale_percent": percent,
            "effective_scale_percent": percent if accepted else None,
            "suggested_scale_percent": percent,
            "accepted_check_count": sum(
                1 for item in probes if getattr(item.result, "accepted", False)
            ),
            "check_count": len(probes),
            "evidence_count": 0,
            "attempted_scale_percents": [
                int(round(float(value) * 100)) for value in attempted_scales
            ],
            "message": "已按当前显示比例完成检查。" if accepted else "当前显示比例下未完成成组定位。",
        }

    @classmethod
    def _run_visual_pipeline(
        cls,
        checker: Any,
        image: Any,
        *,
        preferred_scales: tuple[float, ...],
        fallback_scales: tuple[float, ...],
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """Run the declared visual checks in one deterministic pipeline.

        The display DPI is read by the caller before this method is entered.
        This method then performs the image-scale evidence pass followed by
        the two required paired-anchor checks in UI order.  Keeping the order
        in one place prevents a new caller from accidentally running a single
        locator out of sequence or saving a partial, mixed-scale result.
        """

        check_ids = ("search_box", "chat_input")
        try:
            probes = tuple(
                checker.run(
                    image,
                    preferred_scales=preferred_scales,
                    fallback_scales=fallback_scales,
                    check_ids=check_ids,
                )
            )
        except TypeError:
            # Keep older injected checker extensions usable while production
            # checkers use the explicit ordered check_ids argument.
            probes = tuple(
                item
                for item in checker.run(
                    image,
                    preferred_scales=preferred_scales,
                    fallback_scales=fallback_scales,
                )
                if str(getattr(item, "check_id", "")) in check_ids
            )
        assessment = cls._assess_visual_scale(
            checker,
            probes,
            reported_scale=preferred_scales[0],
            attempted_scales=fallback_scales,
        )
        return probes, assessment

    @staticmethod
    def _visual_compatibility_summary(
        snapshots: list[dict[str, Any]],
        *,
        run_id: str,
    ) -> dict[str, Any]:
        order = {"search_box": 0, "chat_input": 1}
        checks: list[dict[str, Any]] = []
        environment: dict[str, Any] = {}
        for snapshot in snapshots:
            extra = dict(snapshot.get("extra") or {})
            check_id = str(extra.get("check_id") or "")
            if check_id not in order:
                continue
            if not environment:
                environment = dict(extra.get("environment") or {})
            checks.append(
                {
                    "id": check_id,
                    "label": str(snapshot.get("label") or check_id),
                    "outcome": str(snapshot.get("outcome") or "failure"),
                    "reason": str(snapshot.get("reason") or ""),
                    "snapshot_id": str(snapshot.get("id") or ""),
                }
            )
        checks.sort(key=lambda item: order.get(str(item["id"]), 99))
        checks_passed = len(checks) == len(order) and all(
            item["outcome"] == "success" for item in checks
        )
        input_source = (
            str(environment.get("input_source") or "live") if checks else ""
        )
        scale_assessment = dict(environment.get("scale_assessment") or {})
        if checks and not scale_assessment:
            scale_assessment = {
                "status": "confirmed" if checks_passed else "unresolved",
                "reported_scale_percent": environment.get("scale_percent"),
                "effective_scale_percent": (
                    environment.get("scale_percent") if checks_passed else None
                ),
                "suggested_scale_percent": environment.get("scale_percent"),
                "message": (
                    "旧检查记录未保存独立的图像比例证据。"
                ),
            }
        scale_confirmed = scale_assessment.get("status") == "confirmed"
        passed = checks_passed and scale_confirmed
        if scale_assessment:
            environment["scale_assessment"] = scale_assessment

        if not checks:
            status = "not_checked"
            message = "尚未开始检查。程序将先读取显示缩放，再依次检查微信关键位置。"
        elif passed:
            status = "compatible"
            if input_source == "live":
                message = "显示缩放、有效图像比例和两个关键位置均已通过。"
            else:
                message = "截图缩放信息、有效图像比例和两个关键位置均已通过。"
        else:
            status = "needs_attention"
            failed = "、".join(
                str(item["label"])
                for item in checks
                if item["outcome"] != "success"
            )
            if not failed and not scale_confirmed:
                failed = "有效图像比例"
            failed = failed or "必需定位点"
            if input_source == "live":
                message = (
                    f"检查停在需要处理的步骤：{failed}。可展开失败步骤查看候选证据。"
                )
            else:
                message = (
                    f"截图中的{failed}没有通过。请先核对截图来源电脑的缩放比例，"
                    "再查看候选证据。"
                )

        display_percent = environment.get("scale_percent")
        display_title = (
            "读取 Windows 显示缩放"
            if input_source == "live"
            else "确认截图来源的显示缩放"
        )
        display_summary = (
            f"Windows 报告微信窗口为 {display_percent}% 缩放。"
            if input_source == "live" and display_percent
            else (
                f"本次按截图来源电脑的 {display_percent}% 缩放检查。"
                if display_percent
                else "尚未读取显示缩放。"
            )
        )
        stages: list[dict[str, Any]] = [
            {
                "id": "display_scale",
                "number": 1,
                "title": display_title,
                "status": "success" if checks else "pending",
                "summary": display_summary,
            },
            {
                "id": "image_scale",
                "number": 2,
                "title": "确定有效图像比例",
                "status": (
                    "success"
                    if scale_confirmed
                    else ("attention" if checks else "pending")
                ),
                "summary": str(
                    scale_assessment.get("message")
                    or "等待根据成组元素确认有效图像比例。"
                ),
                "details": scale_assessment,
            },
        ]
        check_map = {str(item["id"]): item for item in checks}
        for number, check_id, title in (
            (3, "search_box", "检查微信搜索框"),
            (4, "chat_input", "检查消息输入区域"),
        ):
            item = check_map.get(check_id)
            stages.append(
                {
                    "id": check_id,
                    "number": number,
                    "title": title,
                    "status": (
                        "success"
                        if item and item["outcome"] == "success"
                        else ("attention" if item else "pending")
                    ),
                    "summary": (
                        str(item.get("reason") or "已通过组合定位。")
                        if item
                        else "等待前面的环境检查。"
                    ),
                    "snapshot_id": str(item.get("snapshot_id") or "") if item else "",
                }
            )
        return {
            "ok": True,
            "status": status,
            "message": message,
            "run_id": str(run_id or ""),
            "input_source": input_source,
            "checks": checks,
            "stages": stages,
            "environment": environment,
        }

    def visual_compatibility_status(self) -> dict[str, Any]:
        snapshots = self._recognition_store.list(
            source="compatibility",
            limit=120,
        )
        if not snapshots:
            return self._visual_compatibility_summary([], run_id="")
        run_id = str(snapshots[0].get("run_id") or "")
        current = [
            item for item in snapshots if str(item.get("run_id") or "") == run_id
        ]
        return self._visual_compatibility_summary(current, run_id=run_id)

    def recognition_repair_status(self) -> dict[str, Any]:
        return self._recognition_repair.status()

    def validate_recognition_repair(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._recognition_repair.validate(
                snapshot_id=str(body.get("snapshot_id") or ""),
                target_id=str(body.get("target_id") or ""),
                alternative_id=str(body.get("alternative_id") or ""),
                candidate_ids=body.get("candidate_ids"),
            )
        except RecognitionRepairError as exc:
            raise ProtocolError(exc.code, str(exc), details=exc.details) from exc

    def reload_recognition_repair_candidates(
        self,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            return self._recognition_repair.reload_candidates(
                snapshot_id=str(body.get("snapshot_id") or ""),
                target_id=str(body.get("target_id") or ""),
                minimum_score=body.get("minimum_score"),
            )
        except RecognitionRepairError as exc:
            raise ProtocolError(exc.code, str(exc), details=exc.details) from exc

    def save_recognition_repair(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._recognition_repair.save(
                snapshot_id=str(body.get("snapshot_id") or ""),
                target_id=str(body.get("target_id") or ""),
                alternative_id=str(body.get("alternative_id") or ""),
                candidate_ids=body.get("candidate_ids"),
            )
        except RecognitionRepairError as exc:
            raise ProtocolError(exc.code, str(exc), details=exc.details) from exc

    def disable_recognition_repair(self, target_id: str) -> dict[str, Any]:
        try:
            return self._recognition_repair.disable(target_id)
        except RecognitionRepairError as exc:
            raise ProtocolError(exc.code, str(exc), details=exc.details) from exc

    def run_visual_compatibility_check(self) -> dict[str, Any]:
        """Bring WeChat forward, then capture and inspect it without chat input."""

        if not self._send_lock.acquire(blocking=False):
            raise ProtocolError(
                "automation_busy",
                "当前有自动化任务正在运行，不能同时执行视觉兼容性检查。",
        )
        try:
            session = self._window_session_factory()
            automation = self.config.automation
            if hasattr(session, "tray_activation_enabled"):
                session.tray_activation_enabled = bool(
                    automation.tray_activation_enabled
                )
            if hasattr(session, "tray_activation_timeout"):
                session.tray_activation_timeout = float(
                    automation.tray_activation_timeout
                )
            capture_deadline = time.monotonic() + 5.0
            retryable_capture_errors = {
                "wechat_capture_invalidated",
                "wechat_not_foreground",
                "wechat_window_minimized",
            }
            while True:
                remaining = capture_deadline - time.monotonic()
                if remaining <= 0.30:
                    raise ProtocolError(
                        "wechat_capture_unstable",
                        "微信窗口持续发生最小化、切换或尺寸变化，未能取得稳定截图。",
                    )
                try:
                    window = session.prepare(
                        timeout=remaining,
                        stable_for=0.30,
                    )
                    frame = session.capture_client()
                    # The frame owns the geometry that was checked immediately
                    # before and after ImageGrab.  Do not reuse an older sample.
                    window = frame.window
                    break
                except DesktopSessionError as exc:
                    if (
                        exc.code not in retryable_capture_errors
                        or time.monotonic() >= capture_deadline
                    ):
                        raise ProtocolError(
                            exc.code,
                            str(exc),
                            details=exc.details,
                        ) from exc

            preferred, fallback, scale_description = (
                VisualCompatibilityChecker.scale_policy(
                    dpi=window.dpi,
                    mode=automation.dpi_scale_mode,
                    manual_percent=automation.dpi_scale_percent,
                    auto_min_percent=automation.dpi_auto_min_percent,
                    auto_max_percent=automation.dpi_auto_max_percent,
                    auto_step_percent=automation.dpi_auto_step_percent,
                )
            )
            checker = VisualCompatibilityChecker()
            probes, scale_assessment = self._run_visual_pipeline(
                checker,
                frame.image,
                preferred_scales=preferred,
                fallback_scales=fallback,
            )
            dpi = max(48, min(288, int(window.dpi)))
            environment = {
                "input_source": "live",
                "pipeline": [
                    "display_scale",
                    "image_scale",
                    "search_box",
                    "chat_input",
                ],
                "dpi": dpi,
                "scale_percent": int(round(dpi / 96.0 * 100)),
                "scale_policy": scale_description,
                "scale_assessment": scale_assessment,
                "theme": "light",
                "window": self._window_snapshot_payload(window),
                "client": {
                    "x": int(frame.screen_rect.left),
                    "y": int(frame.screen_rect.top),
                    "width": int(frame.screen_rect.width),
                    "height": int(frame.screen_rect.height),
                },
            }
            return self._save_visual_compatibility_run(
                frame.image,
                probes,
                environment=environment,
                input_source="live",
            )
        finally:
            self._send_lock.release()

    @staticmethod
    def validate_visual_screenshot_import(
        *,
        filename: str,
        content_length: Any,
        scale_percent: Any,
        input_source: Any,
    ) -> dict[str, Any]:
        """Validate imported screenshot headers before reading the body."""

        try:
            size, scale, source = validate_imported_screenshot_request(
                content_length=content_length,
                scale_percent=scale_percent,
                input_source=input_source,
            )
        except ImportedScreenshotError as exc:
            raise ProtocolError(exc.code, str(exc)) from exc
        safe_name = BridgeService._safe_upload_name(filename, "image")
        return {
            "name": safe_name,
            "size": size,
            "scale_percent": scale,
            "input_source": source,
        }

    def run_imported_visual_compatibility_check(
        self,
        payload: bytes,
        *,
        filename: str,
        scale_percent: Any,
        input_source: Any,
    ) -> dict[str, Any]:
        """Inspect an uploaded screenshot without querying or controlling WeChat."""

        request = self.validate_visual_screenshot_import(
            filename=filename,
            content_length=len(payload),
            scale_percent=scale_percent,
            input_source=input_source,
        )
        if not self._send_lock.acquire(blocking=False):
            raise ProtocolError(
                "automation_busy",
                "当前有自动化任务正在运行，不能同时执行视觉兼容性检查。",
            )
        try:
            try:
                imported = decode_imported_screenshot(payload)
            except ImportedScreenshotError as exc:
                raise ProtocolError(exc.code, str(exc)) from exc
            scale = round(int(request["scale_percent"]) / 100.0, 4)
            checker = VisualCompatibilityChecker()
            probes, scale_assessment = self._run_visual_pipeline(
                checker,
                imported.image,
                preferred_scales=(scale,),
                fallback_scales=(scale,),
            )
            width, height = imported.image.size
            source = str(request["input_source"])
            environment = {
                "input_source": source,
                "pipeline": [
                    "display_scale",
                    "image_scale",
                    "search_box",
                    "chat_input",
                ],
                "dpi": int(round(scale * 96)),
                "scale_percent": int(request["scale_percent"]),
                "scale_policy": (
                    f"导入截图固定使用 {request['scale_percent']}%；不使用当前电脑 DPI"
                ),
                "scale_assessment": scale_assessment,
                "theme": "light",
                "client": {
                    "x": 0,
                    "y": 0,
                    "width": int(width),
                    "height": int(height),
                },
                "imported": {
                    "name": str(request["name"]),
                    "format": imported.format,
                    "size_bytes": imported.size_bytes,
                },
            }
            return self._save_visual_compatibility_run(
                imported.image,
                probes,
                environment=environment,
                input_source=source,
            )
        finally:
            self._send_lock.release()

    def _save_visual_compatibility_run(
        self,
        image: Any,
        probes: Any,
        *,
        environment: dict[str, Any],
        input_source: str,
    ) -> dict[str, Any]:
        run_id = f"compat-{int(time.time() * 1000)}-{secrets.token_hex(3)}"
        saved: list[dict[str, Any]] = []
        for probe in probes:
            item = self._recognition_store.save(
                image,
                probe.result,
                run_id=run_id,
                source="compatibility",
                label=probe.label,
                operation=f"compatibility.{probe.check_id}",
                extra_metadata={
                    "check_id": probe.check_id,
                    "read_only": True,
                    "input_source": str(input_source),
                    "environment": environment,
                },
            )
            if item is None:
                raise ProtocolError(
                    "recognition_snapshot_save_failed",
                    "视觉检查已经完成，但识别快照保存失败，未生成不完整结果。",
                )
            saved.append(item)
        return self._visual_compatibility_summary(saved, run_id=run_id)

    @staticmethod
    def _window_snapshot_payload(snapshot: Any) -> dict[str, int]:
        bounds = snapshot.window_rect
        return {
            "x": int(bounds.left),
            "y": int(bounds.top),
            "width": int(bounds.width),
            "height": int(bounds.height),
        }

    def test_wechat_tray_activation(self) -> dict[str, Any]:
        """Wake WeChat from the tray without entering a chat or sending."""

        with self._state_lock:
            if self._automation_active:
                return {
                    "ok": False,
                    "code": "automation_busy",
                    "message": "当前有自动化任务正在运行，不能同时测试托盘唤醒。",
                }
        session = self._window_session_factory()
        try:
            snapshot = session.read_current_window_snapshot()
        except DesktopSessionError as exc:
            if exc.code != "wechat_not_found":
                return {
                    "ok": False,
                    "code": exc.code,
                    "message": str(exc),
                    "details": dict(exc.details),
                }
        else:
            return {
                "ok": True,
                "status": "already_visible",
                "message": "微信主窗口已经可见，没有操作系统托盘。",
                "window": self._window_snapshot_payload(snapshot),
            }

        timeout = float(self.config.automation.tray_activation_timeout)
        try:
            activation = self._tray_activator_factory().activate(timeout=timeout)
        except TrayActivationError as exc:
            return {
                "ok": False,
                "code": exc.code,
                "message": str(exc),
                "details": dict(exc.details),
            }

        deadline = self._monotonic() + timeout
        while True:
            try:
                snapshot = session.read_current_window_snapshot()
            except DesktopSessionError as exc:
                if exc.code != "wechat_not_found":
                    return {
                        "ok": False,
                        "code": exc.code,
                        "message": str(exc),
                        "details": dict(exc.details),
                    }
            else:
                return {
                    "ok": True,
                    "status": "activated",
                    "message": "已单击唯一微信托盘图标，并检测到微信主窗口出现。",
                    "tray": {
                        "name": activation.name,
                        "source": activation.source,
                        "bounds": [
                            activation.bounds.left,
                            activation.bounds.top,
                            activation.bounds.right,
                            activation.bounds.bottom,
                        ],
                    },
                    "window": self._window_snapshot_payload(snapshot),
                }
            if self._monotonic() >= deadline:
                return {
                    "ok": False,
                    "code": "wechat_tray_activation_timeout",
                    "message": "已单击微信托盘图标，但限定时间内没有检测到微信主窗口。",
                    "details": {"timeout": timeout},
                }
            time.sleep(0.05)

    def _dedupe_key(self, data: dict[str, Any]) -> str:
        raw_id = data.get("rawid") or data.get("rawId") or data.get("id")
        if raw_id:
            return f"id:{raw_id}"
        material = json.dumps(
            {
                "session": data.get("sessionId"),
                "talker": data.get("talkerId"),
                "content": data.get("content"),
                "time": data.get("timestamp") or data.get("time"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return "hash:" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _is_duplicate(self, data: dict[str, Any]) -> bool:
        key = self._dedupe_key(data)
        now = time.time()
        with self._state_lock:
            cutoff = now - 600
            while self._processed:
                first_key = next(iter(self._processed))
                if self._processed[first_key] >= cutoff and len(self._processed) <= 5000:
                    break
                self._processed.popitem(last=False)
            if key in self._processed:
                return True
            self._processed[key] = now
        return False

    def _is_recent_outbound(self, data: dict[str, Any]) -> bool:
        content = str(data.get("content") or "").strip()
        if not content:
            return False
        try:
            kind, name, _ = weflow_contact(data)
        except Exception:
            return False
        now = time.time()
        with self._state_lock:
            while self._recent_outbound and now - self._recent_outbound[0][0] > 120:
                self._recent_outbound.popleft()
            return any(
                item_kind == kind and item_name == name and item_text == content
                for _, item_kind, item_name, item_text in self._recent_outbound
            )

    def handle_weflow_message(self, data: dict[str, Any]) -> None:
        with self._state_lock:
            if not self._running:
                return
            self._counters["weflow_received"] += 1

        if self._is_duplicate(data):
            with self._state_lock:
                self._counters["weflow_deduplicated"] += 1
            return
        if self._is_recent_outbound(data):
            self._event("debug", "self_reply_skipped", "跳过刚由本桥发送的回显消息。")
            return

        timestamp = data.get("timestamp") or data.get("time")
        try:
            timestamp_value = float(timestamp)
            if timestamp_value > 1e12:
                timestamp_value /= 1000
        except (TypeError, ValueError):
            timestamp_value = time.time()
        if self._started_at and timestamp_value < self._started_at - 30:
            self._event("debug", "replay_skipped", "跳过桥接启动前的历史推送。")
            return

        event_data = dict(data)
        enrich_metadata = getattr(self.weflow, "enrich_message_metadata", None)
        if callable(enrich_metadata):
            event_data = enrich_metadata(event_data)
        classification = classify_weflow_message(event_data)
        if classification.kind == "system":
            with self._state_lock:
                self._counters["weflow_system_filtered"] += 1
            self._event(
                "debug",
                "weflow_system_message_filtered",
                "已识别并跳过 WeFlow 系统消息，未作为用户消息推送到 AstrBot。",
                classification_reason=classification.reason,
            )
            transport_event(
                "internal",
                "bridge",
                "weflow_system_message_filtered",
                {
                    "rawid": data.get("rawid"),
                    "classification_reason": classification.reason,
                },
            )
            return
        event_data = self._resolve_weflow_group_name(event_data)
        if event_data is None:
            return
        event = build_weflow_event(
            event_data,
            self_id=self.config.self_id,
            registry=self.registry,
            bot_names=self.config.bot_names,
            bot_wxid=self.config.bot_wxid,
            group_trigger=self.config.group_trigger,
            **self._weflow_group_identity(event_data),
        )
        if event is None:
            transport_event(
                "internal",
                "bridge",
                "weflow_event_filtered",
                {"rawid": data.get("rawid"), "content": data.get("content")},
            )
            return
        transport_event(
            "internal",
            "bridge",
            "weflow_event_normalized",
            event,
        )
        # AstrBot may synchronously call get_msg while converting a reply segment.
        # Cache before push so the referenced event is already available.
        self.registry.remember_message(event)
        target_id = event.get("group_id") or event.get("user_id")
        if target_id is not None:
            with self._state_lock:
                self._last_inbound_at[int(target_id)] = self._monotonic()
        if self.onebot.push(event):
            with self._state_lock:
                self._counters["weflow_forwarded"] += 1
            self._event(
                "info",
                "weflow_forwarded",
                "WeFlow 消息已推送到 AstrBot。",
                message_type=event.get("message_type"),
            )
        else:
            self._event(
                "warning",
                "astrbot_offline",
                "AstrBot 未连接，当前 WeFlow 消息未投递。",
            )

    def _resolve_weflow_group_name(
        self, data: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        """Replace a lagging WeFlow chatroom ID with a verified display name."""

        try:
            kind, raw_name, session_id = weflow_contact(data)
        except Exception:
            return dict(data)
        if kind != "group":
            return dict(data)
        if not is_internal_group_name(raw_name, session_id):
            return dict(data)

        resolver = getattr(self.weflow, "resolve_group_name", None)
        resolved_name = ""
        source = "unavailable"
        if callable(resolver):
            try:
                resolved_name, source = resolver(data)
            except Exception as exc:
                self._event(
                    "warning",
                    "weflow_group_name_lookup_failed",
                    "查询 WeFlow 群聊资料失败；本条群消息暂不推送。",
                    session_id=session_id,
                    error=type(exc).__name__,
                )
        if is_internal_group_name(resolved_name, session_id):
            self._event(
                "warning",
                "weflow_group_name_unresolved",
                "WeFlow 尚未同步到真实群名；已阻止使用 @chatroom 内部 ID 搜索微信，本条消息暂不推送。",
                session_id=session_id,
                resolution=source,
            )
            transport_event(
                "internal",
                "bridge",
                "weflow_group_name_unresolved",
                {
                    "sessionId": session_id,
                    "groupName": data.get("groupName"),
                    "resolution": source,
                    "rawid": data.get("rawid"),
                },
            )
            return None

        normalized = dict(data)
        normalized["groupName"] = str(resolved_name).strip()
        self._event(
            "info",
            "weflow_group_name_resolved",
            "已将 WeFlow 群聊内部 ID 解析为真实群名。",
            session_id=session_id,
            group_name=normalized["groupName"],
            resolution=source,
        )
        return normalized

    def _weflow_group_identity(self, data: dict[str, Any]) -> dict[str, Any]:
        try:
            kind, group_name, session_id = weflow_contact(data)
        except Exception:
            return {}
        if kind != "group":
            return {}
        group_contact = self.registry.remember("group", group_name, session_id)
        member_loader = getattr(self.weflow, "group_members", None)
        if callable(member_loader):
            try:
                self.registry.remember_group_members(
                    group_contact.target_id,
                    session_id,
                    member_loader(session_id),
                )
            except Exception as exc:
                self._event(
                    "warning",
                    "weflow_member_sync_failed",
                    "WeFlow 群成员资料同步失败，本条消息继续使用可用身份信息。",
                    error=type(exc).__name__,
                )
        resolver = getattr(self.weflow, "resolve_group_sender", None)
        if not callable(resolver):
            return {}
        try:
            wxid, profile, resolution = resolver(data)
        except Exception as exc:
            self._event(
                "warning",
                "weflow_member_resolution_failed",
                "WeFlow 群成员身份解析失败，本条消息使用显示名兼容映射。",
                error=type(exc).__name__,
            )
            return {"sender_resolution": "resolver_error"}
        level = "debug" if wxid and profile else "warning"
        self._event(
            level,
            "weflow_member_resolved" if wxid and profile else "weflow_member_unresolved",
            (
                "已将群消息发送者解析为稳定微信 wxid。"
                if wxid and profile
                else "未能唯一解析群消息发送者 wxid；该成员暂不支持真实 @。"
            ),
            resolution=resolution,
            wxid_available=bool(wxid),
            nickname_available=bool((profile or {}).get("nickname")),
        )
        return {
            "sender_wxid": wxid,
            "sender_profile": profile,
            "sender_resolution": resolution,
        }

    def _cache_key(self, echo: Any) -> Optional[str]:
        if echo is None:
            return None
        try:
            return json.dumps(echo, ensure_ascii=False, sort_keys=True)
        except TypeError:
            return repr(echo)

    def _cached_response(self, echo: Any) -> Optional[dict[str, Any]]:
        key = self._cache_key(echo)
        if key is None:
            return None
        now = time.time()
        with self._state_lock:
            while self._response_cache:
                first_key = next(iter(self._response_cache))
                if self._response_cache[first_key][0] >= now - 600:
                    break
                self._response_cache.popitem(last=False)
            item = self._response_cache.get(key)
            return copy.deepcopy(item[1]) if item else None

    def _remember_response(self, echo: Any, response: dict[str, Any]) -> None:
        key = self._cache_key(echo)
        if key is None:
            return
        with self._state_lock:
            self._response_cache[key] = (time.time(), copy.deepcopy(response))
            while len(self._response_cache) > 1000:
                self._response_cache.popitem(last=False)

    def _stop_hotkey_context(self, cancel_event: threading.Event) -> Any:
        """Create the event-driven emergency shortcut for one UI task.

        Shortcut registration is deliberately best-effort: a failed hotkey must not
        make ordinary UI automation fail, because the console Stop button and
        the normal cancellation event remain available.
        """

        with self._state_lock:
            automation = self.config.automation
        if not automation.stop_hotkey_enabled:
            return nullcontext()
        try:
            try:
                manager = self._stop_hotkey_factory(
                    automation.stop_hotkey,
                    cancel_event,
                    enabled=True,
                    on_trigger=lambda: self._request_automation_stop("hotkey"),
                )
            except TypeError as exc:
                try:
                    manager = self._stop_hotkey_factory(
                        automation.stop_hotkey,
                        cancel_event,
                        on_trigger=lambda: self._request_automation_stop("hotkey"),
                    )
                except TypeError as nested:
                    if "enabled" not in str(exc) and "on_trigger" not in str(exc):
                        raise
                    try:
                        manager = self._stop_hotkey_factory(
                            automation.stop_hotkey,
                            cancel_event,
                            enabled=True,
                        )
                    except TypeError as final:
                        if "enabled" not in str(final):
                            raise
                        manager = self._stop_hotkey_factory(automation.stop_hotkey, cancel_event)
        except Exception as exc:
            log.warning("无法创建自动化停止快捷键，将继续执行：%s", exc)
            return nullcontext()
        return _best_effort_stop_hotkey(manager)

    def _perform_send(
        self,
        kind: str,
        name: str,
        text: str,
        *,
        input_parts: tuple[tuple[str, str], ...] = (),
        cancel_event: Optional[threading.Event] = None,
    ) -> SendResult:
        sender = self.config.sender
        if cancel_event is None:
            with self._state_lock:
                cancel_event = self._automation_cancel
        with self._state_lock:
            self._automation_active += 1
        try:
            acquired = False
            while not self._send_lock.acquire(timeout=0.05):
                if cancel_event.is_set():
                    break
            else:
                acquired = True
            if not acquired or cancel_event.is_set():
                result = self._cancelled_result(kind, name)
            else:
                try:
                    with recognition_run_if_missing(
                        self._recognition_store,
                        run_id=f"runtime-{int(time.time() * 1000)}-{secrets.token_hex(3)}",
                        source="runtime",
                        capture_success=False,
                    ), self._stop_hotkey_context(cancel_event):
                        result = self._sender(
                            kind,
                            name,
                            text,
                            timeout=sender.timeout,
                            settle=sender.settle,
                            search_result_wait_min=sender.search_result_wait_min,
                            search_result_wait_max=sender.search_result_wait_max,
                            conversation_entry_mode=sender.conversation_entry_mode,
                            conversation_enter_delay_min=(
                                sender.conversation_enter_delay_min
                            ),
                            conversation_enter_delay_max=(
                                sender.conversation_enter_delay_max
                            ),
                            verification_timeout=sender.text_verification_timeout,
                            soft_protection=sender.soft_protection,
                            lock_mouse=sender.lock_mouse,
                            lock_keyboard=sender.lock_keyboard,
                            auto_launch_wechat=sender.auto_launch_wechat,
                            wechat_executable=sender.wechat_executable,
                            launch_timeout=sender.launch_timeout,
                            adaptive_layout=sender.adaptive_layout,
                            reuse_open_chat=sender.reuse_open_chat,
                            layout_cache=sender.layout_cache,
                            input_parts=input_parts,
                            mention_candidate_timeout=sender.mention_candidate_timeout,
                            mention_after_at_delay_min=sender.mention_after_at_delay_min,
                            mention_after_at_delay_max=sender.mention_after_at_delay_max,
                            mention_min_wait=sender.mention_min_wait,
                            mention_before_enter_delay_min=(
                                sender.mention_before_enter_delay_min
                            ),
                            mention_before_enter_delay_max=(
                                sender.mention_before_enter_delay_max
                            ),
                            mention_confirm_timeout=sender.mention_confirm_timeout,
                            mention_fallback_enabled=sender.mention_fallback_enabled,
                            tray_activation=(
                                self.config.automation.tray_activation_enabled
                            ),
                            tray_timeout=(
                                self.config.automation.tray_activation_timeout
                            ),
                            dpi_scale_mode=self.config.automation.dpi_scale_mode,
                            dpi_scale_percent=(
                                self.config.automation.dpi_scale_percent
                            ),
                            dpi_auto_min_percent=(
                                self.config.automation.dpi_auto_min_percent
                            ),
                            dpi_auto_max_percent=(
                                self.config.automation.dpi_auto_max_percent
                            ),
                            dpi_auto_step_percent=(
                                self.config.automation.dpi_auto_step_percent
                            ),
                            file_launch_fallback=sender.file_launch_fallback,
                            render_mask_recovery=sender.render_mask_recovery,
                            mask_retry_count=sender.mask_retry_count,
                            mask_wait=sender.mask_wait,
                            retry_max_attempts=sender.retry_max_attempts,
                            retry_delays=sender.retry_delays,
                            overall_timeout=sender.overall_timeout,
                            input_mode=sender.input_mode,
                            append_line_break_after_input=(
                                sender.append_line_break_after_input
                            ),
                            keyboard_clipboard_threshold_enabled=(
                                sender.keyboard_clipboard_threshold_enabled
                            ),
                            keyboard_clipboard_threshold_chars=(
                                sender.keyboard_clipboard_threshold_chars
                            ),
                            wechat_ctrl_enter_confirmed=(
                                self.config.automation.wechat_ctrl_enter_confirmed
                            ),
                            character_delay=sender.character_delay,
                            character_delay_min=sender.character_delay_min,
                            character_delay_max=sender.character_delay_max,
                            natural_typing_enabled=sender.natural_typing_enabled,
                            typing_burst_chars_min=sender.typing_burst_chars_min,
                            typing_burst_chars_max=sender.typing_burst_chars_max,
                            typing_pause_min=sender.typing_pause_min,
                            typing_pause_max=sender.typing_pause_max,
                            send_review_delay_min=sender.send_review_delay_min,
                            send_review_delay_max=sender.send_review_delay_max,
                            click_before_delay_min=sender.click_before_delay_min,
                            click_before_delay_max=sender.click_before_delay_max,
                            click_hold_duration_min=sender.click_hold_duration_min,
                            click_hold_duration_max=sender.click_hold_duration_max,
                            paste_enabled=sender.paste_enabled,
                            verification_enabled=sender.verification_enabled,
                            qt_hot_activation_enabled=False,
                            stop_hotkey=self.config.automation.stop_hotkey,
                            window_position_enabled=(
                                self.config.automation.window_position_enabled
                            ),
                            window_x=self.config.automation.window_x,
                            window_y=self.config.automation.window_y,
                            window_size_enabled=(
                                self.config.automation.window_size_enabled
                            ),
                            window_width=self.config.automation.window_width,
                            window_height=self.config.automation.window_height,
                            stop_callback=lambda: self._request_automation_stop("hotkey"),
                            cancel_event=cancel_event,
                        )
                finally:
                    self._send_lock.release()
        finally:
            with self._state_lock:
                self._automation_active = max(0, self._automation_active - 1)
        # v3 never suggests or enables Qt/UIA hot activation.  All actionable
        # diagnostics come from the visual/Win32 sender result itself.
        # Keep the bridge tolerant of the old sender contract: after a
        # confirmed click, missing UI evidence is diagnostic only and must not
        # ask AstrBot to retry the same message.
        if not result.ok and (
            result.code == "send_verification_uncertain"
            or result.details.get("send_clicked")
        ):
            result = SendResult(
                ok=True,
                code="sent_unverified",
                message="发送按钮已点击；未观察到对应气泡，但本次动作按成功处理且不会自动重发。",
                kind=result.kind or kind,
                name=result.name or name,
                details={
                    **result.details,
                    "send_action_completed": True,
                    "ui_verified": False,
                    "warning_code": result.code,
                    "warning_message": result.message,
                },
            )
        with self._state_lock:
            counter = "send_succeeded" if result.ok else "send_failed"
            self._counters[counter] += 1
            if cancel_event is self._automation_cancel and cancel_event.is_set():
                self._automation_enabled = False
            if result.ok:
                self._recent_outbound.append((time.time(), kind, name, text.strip()))
        ui_verified = bool(result.details.get("ui_verified", result.code == "sent"))
        send_details = result.details.get("send") or {}
        cancelled_after_click = bool(
            result.details.get("cancelled_after_send_click")
            or send_details.get("cancelled_after_send_click")
        )
        event_code = (
            "automation_cancelled_after_send"
            if cancelled_after_click
            else result.code
        )
        self._event(
            "info" if result.ok and ui_verified else "warning",
            event_code,
            (
                "发送按钮已经点击；应急停止已取消后续验证，消息无法撤回。"
                if cancelled_after_click
                else
                "应急停止已生效；微信消息未点击发送。"
                if result.code == "automation_cancelled"
                else
                "微信发送动作已完成。"
                if result.ok and ui_verified
                else "微信发送动作已完成，但未观察到对应气泡。"
                if result.ok
                else "微信消息发送失败。"
            ),
            kind=kind,
            target=name,
            text_length=len(text),
            qt_accessibility=result.details.get("qt_accessibility", {}),
            compatibility_hint=bool(result.details.get("compatibility_hint")),
        )
        transport_event(
            "outbound",
            "wechat_ui",
            "text_send_result",
            result.to_dict(),
        )
        return result

    @staticmethod
    def _cancelled_result(
        kind: str,
        name: str,
        *,
        media: bool = False,
    ) -> SendResult:
        return SendResult(
            ok=False,
            code="automation_cancelled",
            message="应急停止已生效；发送按钮尚未点击，当前自动化已取消。",
            kind=kind,
            name=name,
            details={
                "send_clicked": False,
                "cancelled": True,
                "cancelled_after_send_click": False,
                "automatic_retry": False,
                "media": media,
            },
        )

    def _perform_media_send(
        self,
        kind: str,
        name: str,
        media_type: str,
        data: dict[str, Any],
        *,
        cancel_event: Optional[threading.Event] = None,
        resolved_path: Optional[str] = None,
    ) -> SendResult:
        if cancel_event is None:
            with self._state_lock:
                cancel_event = self._automation_cancel
        if cancel_event.is_set():
            return self._cancelled_result(kind, name, media=True)
        resolver = MediaResolver(self.config.media)
        try:
            resolved = (
                ResolvedMedia(media_type, str(resolved_path))
                if resolved_path
                else resolver.resolve(media_type, data)
            )
        except SenderError as error:
            with self._state_lock:
                self._counters["send_failed"] += 1
            self._event(
                "warning",
                error.code,
                error.message,
                kind=kind,
                target=name,
                media_type=media_type,
            )
            return SendResult(
                ok=False,
                code=error.code,
                message=error.message,
                kind=kind,
                name=name,
                details=dict(error.details),
            )
        try:
            with self._state_lock:
                self._automation_active += 1
            try:
                acquired = False
                while not self._send_lock.acquire(timeout=0.05):
                    if cancel_event.is_set():
                        break
                else:
                    acquired = True
                if not acquired or cancel_event.is_set():
                    result = self._cancelled_result(kind, name, media=True)
                else:
                    try:
                        with recognition_run_if_missing(
                            self._recognition_store,
                            run_id=f"runtime-{int(time.time() * 1000)}-{secrets.token_hex(3)}",
                            source="runtime",
                            capture_success=False,
                        ), self._stop_hotkey_context(cancel_event):
                            result = self._media_sender(
                                kind,
                                name,
                                media_type,
                                resolved.path,
                                timeout=self.config.sender.timeout,
                                settle=self.config.sender.settle,
                                search_result_wait_min=(
                                    self.config.sender.search_result_wait_min
                                ),
                                search_result_wait_max=(
                                    self.config.sender.search_result_wait_max
                                ),
                                conversation_entry_mode=(
                                    self.config.sender.conversation_entry_mode
                                ),
                                conversation_enter_delay_min=(
                                    self.config.sender.conversation_enter_delay_min
                                ),
                                conversation_enter_delay_max=(
                                    self.config.sender.conversation_enter_delay_max
                                ),
                                soft_protection=self.config.sender.soft_protection,
                                lock_mouse=self.config.sender.lock_mouse,
                                lock_keyboard=self.config.sender.lock_keyboard,
                                auto_launch_wechat=self.config.sender.auto_launch_wechat,
                                wechat_executable=self.config.sender.wechat_executable,
                                launch_timeout=self.config.sender.launch_timeout,
                                adaptive_layout=self.config.sender.adaptive_layout,
                                reuse_open_chat=self.config.sender.reuse_open_chat,
                                layout_cache=self.config.sender.layout_cache,
                                send_review_delay_min=(
                                    self.config.sender.send_review_delay_min
                                ),
                                send_review_delay_max=(
                                    self.config.sender.send_review_delay_max
                                ),
                                click_before_delay_min=(
                                    self.config.sender.click_before_delay_min
                                ),
                                click_before_delay_max=(
                                    self.config.sender.click_before_delay_max
                                ),
                                click_hold_duration_min=(
                                    self.config.sender.click_hold_duration_min
                                ),
                                click_hold_duration_max=(
                                    self.config.sender.click_hold_duration_max
                                ),
                                tray_activation=(
                                    self.config.automation.tray_activation_enabled
                                ),
                                tray_timeout=(
                                    self.config.automation.tray_activation_timeout
                                ),
                                dpi_scale_mode=(
                                    self.config.automation.dpi_scale_mode
                                ),
                                dpi_scale_percent=(
                                    self.config.automation.dpi_scale_percent
                                ),
                                dpi_auto_min_percent=(
                                    self.config.automation.dpi_auto_min_percent
                                ),
                                dpi_auto_max_percent=(
                                    self.config.automation.dpi_auto_max_percent
                                ),
                                dpi_auto_step_percent=(
                                    self.config.automation.dpi_auto_step_percent
                                ),
                                file_launch_fallback=self.config.sender.file_launch_fallback,
                                render_mask_recovery=self.config.sender.render_mask_recovery,
                                mask_retry_count=self.config.sender.mask_retry_count,
                                mask_wait=self.config.sender.mask_wait,
                                retry_max_attempts=self.config.sender.retry_max_attempts,
                                retry_delays=self.config.sender.retry_delays,
                                overall_timeout=self.config.sender.overall_timeout,
                                stop_hotkey=self.config.automation.stop_hotkey,
                                window_position_enabled=(
                                    self.config.automation.window_position_enabled
                                ),
                                window_x=self.config.automation.window_x,
                                window_y=self.config.automation.window_y,
                                window_size_enabled=(
                                    self.config.automation.window_size_enabled
                                ),
                                window_width=self.config.automation.window_width,
                                window_height=self.config.automation.window_height,
                                stop_callback=lambda: self._request_automation_stop("hotkey"),
                                qt_hot_activation_enabled=(
                                    self._qt_accessibility_payload()["enabled"]
                                ),
                                cancel_event=cancel_event,
                            )
                    finally:
                        self._send_lock.release()
            finally:
                with self._state_lock:
                    self._automation_active = max(0, self._automation_active - 1)
            if (
                not result.ok
                and result.code
                in {
                    "element_not_found",
                    "search_list_not_found",
                    "wechat_login_required",
                }
                and not self._qt_accessibility_payload()["enabled"]
            ):
                result.details = {
                    **result.details,
                    "compatibility_hint": compatibility_hint(),
                }
            if not result.ok and (
                result.code == "send_verification_uncertain"
                or result.details.get("send_clicked")
            ):
                result = SendResult(
                    ok=True,
                    code="media_sent_unverified",
                    message="媒体发送按钮已点击；未观察到媒体气泡，但本次动作按成功处理。",
                    kind=result.kind or kind,
                    name=result.name or name,
                    details={
                        **result.details,
                        "send_action_completed": True,
                        "ui_verified": False,
                        "warning_code": result.code,
                        "warning_message": result.message,
                    },
                )
            ui_verified = bool(
                result.details.get("ui_verified", result.code == "media_sent")
            )
            send_details = result.details.get("send") or {}
            cancelled_after_click = bool(
                result.details.get("cancelled_after_send_click")
                or send_details.get("cancelled_after_send_click")
            )
            event_code = (
                "automation_cancelled_after_send"
                if cancelled_after_click
                else result.code
            )
            with self._state_lock:
                counter = "send_succeeded" if result.ok else "send_failed"
                self._counters[counter] += 1
                if cancel_event is self._automation_cancel and cancel_event.is_set():
                    self._automation_enabled = False
            self._event(
                "info" if result.ok and ui_verified else "warning",
                event_code,
                (
                    "媒体发送按钮已经点击；应急停止已取消后续步骤，消息无法撤回。"
                    if cancelled_after_click
                    else
                    "应急停止已生效；微信媒体未点击发送。"
                    if result.code == "automation_cancelled"
                    else
                    "微信媒体发送动作已完成。"
                    if result.ok and ui_verified
                    else "微信媒体发送动作已完成，但未强制验证媒体气泡。"
                    if result.ok
                    else "微信媒体发送失败。"
                ),
                kind=kind,
                target=name,
                media_type=media_type,
                qt_accessibility=result.details.get("qt_accessibility", {}),
                compatibility_hint=bool(result.details.get("compatibility_hint")),
            )
            transport_event(
                "outbound",
                "wechat_ui",
                "media_send_result",
                result.to_dict(),
                media_type=media_type,
            )
            return result
        finally:
            resolved.cleanup()

    @staticmethod
    def _member_mention_aliases(member: Any) -> set[str]:
        return {
            str(value or "").strip()
            for value in (
                member.name,
                member.display_name,
                member.nickname,
                member.remark,
                member.alias,
                member.group_nickname,
                member.visible_name,
                member.mention_name,
            )
            if str(value or "").strip()
        }

    @staticmethod
    def _has_explicit_at_token(value: Any) -> bool:
        text = str(value or "")
        return any(
            char == "@"
            and (
                index == 0
                or not (text[index - 1].isalnum() or text[index - 1] in "_-＠")
            )
            for index, char in enumerate(text)
        )

    def _expand_plain_text_mentions(self, outbound: Any) -> Any:
        """Turn convenient ``@昵称`` text into experimental real-at segments.

        AstrBot plugins may send a native OneBot ``at`` segment, but a model
        will often emit ordinary text instead.  In real mode both forms should
        work.  Matching is exact against this group's WeFlow member data and
        the longest known name wins; duplicate exact identities are rejected
        instead of guessing.
        """
        if self.config.sender.mention_mode != "real" or outbound.kind != "group":
            return outbound
        members = self.registry.list_group_members(outbound.target_id)
        if not members:
            if any(
                segment.type == "text"
                and self._has_explicit_at_token(segment.data.get("text"))
                for segment in outbound.segments
            ):
                raise ProtocolError(
                    "real_mention_members_unavailable",
                    "真实 @ 模式尚未获得当前群的 WeFlow 成员资料；未执行发送。"
                    "请等待一条群消息触发成员同步，或切换为纯文本 @。",
                )
            return outbound

        alias_map: dict[str, dict[int, Any]] = {}
        for member in members:
            for alias in self._member_mention_aliases(member):
                alias_map.setdefault(alias, {})[member.member_id] = member
        aliases = sorted(alias_map, key=len, reverse=True)
        if not aliases:
            return outbound

        expanded: list[OutboundSegment] = []
        converted = 0
        for segment in outbound.segments:
            if segment.type != "text":
                expanded.append(segment)
                continue
            value = str(segment.data.get("text") or "")
            cursor = 0
            plain_start = 0
            while cursor < len(value):
                if value[cursor] != "@":
                    cursor += 1
                    continue
                if cursor and (
                    value[cursor - 1].isalnum()
                    or value[cursor - 1] in "_-＠"
                ):
                    # Do not reinterpret email addresses or identifiers as
                    # mentions in the experimental real-mention mode.
                    cursor += 1
                    continue
                matches = [
                    alias
                    for alias in aliases
                    if value.startswith(alias, cursor + 1)
                    and (
                        cursor + 1 + len(alias) == len(value)
                        or value[cursor + 1 + len(alias)].isspace()
                        or value[cursor + 1 + len(alias)]
                        in "，。！？：；、）（】【.,!?;:)]}"
                    )
                ]
                if not matches:
                    unresolved = value[cursor + 1 :].split(maxsplit=1)[0]
                    raise ProtocolError(
                        "real_mention_text_unresolved",
                        f"无法把文本 @“{unresolved or '?'}”对应到当前群的唯一微信成员；"
                        "未执行发送。请等待 WeFlow 同步群成员资料、检查昵称，"
                        "或切换为纯文本 @。",
                    )
                longest = len(matches[0])
                matching_aliases = [alias for alias in matches if len(alias) == longest]
                candidates: dict[int, Any] = {}
                for alias in matching_aliases:
                    candidates.update(alias_map[alias])
                visible_token = value[cursor + 1 : cursor + 1 + longest]
                if len(candidates) != 1:
                    raise ProtocolError(
                        "real_mention_text_ambiguous",
                        f"群聊中有多个成员匹配文本 @“{visible_token}”；"
                        "无法安全判断要提及谁，未执行发送。请改用更唯一的昵称，"
                        "或切换为纯文本 @。",
                    )
                if cursor > plain_start:
                    expanded.append(
                        OutboundSegment(
                            "text", {"text": value[plain_start:cursor]}
                        )
                    )
                member = next(iter(candidates.values()))
                expanded.append(
                    OutboundSegment(
                        "at",
                        {
                            "qq": str(member.member_id),
                            "name": member.visible_name or visible_token,
                            "mention_name": member.mention_name,
                            "wxid": member.wxid,
                            "real_mention_available": bool(
                                member.wxid and member.nickname
                            ),
                            "source": "plain_text_at",
                        },
                    )
                )
                converted += 1
                cursor += 1 + longest
                # The real-at writer adds one normal space after the selected
                # candidate, so consume one equivalent separator from the
                # model text to avoid producing two spaces.
                if cursor < len(value) and value[cursor] in " \t\u2005\u3000":
                    cursor += 1
                plain_start = cursor
            if plain_start < len(value):
                expanded.append(
                    OutboundSegment("text", {"text": value[plain_start:]})
                )
        if not converted:
            return outbound
        self._event(
            "info",
            "plain_text_mention_resolved",
            "已将群聊文本中的 @昵称解析为实验性真实微信 @。",
            target=outbound.name,
            mention_count=converted,
        )
        return replace(outbound, segments=tuple(expanded))

    def _perform_outbound(
        self,
        outbound: Any,
        cancel_event: Optional[threading.Event] = None,
    ) -> SendResult:
        if cancel_event is None:
            with self._state_lock:
                cancel_event = self._automation_cancel
        segment_results: list[dict[str, Any]] = []
        all_verified = True
        operations: list[tuple[str, Any]] = []
        text_parts: list[tuple[str, str]] = []
        mention_preparation_warnings: list[dict[str, Any]] = []
        mention_mode = self.config.sender.mention_mode

        if mention_mode == "real":
            unavailable = [
                segment
                for segment in outbound.segments
                if segment.type == "at"
                and not bool(segment.data.get("real_mention_available"))
            ]
            if unavailable and not self.config.sender.mention_fallback_enabled:
                return SendResult(
                    ok=False,
                    code="real_mention_identity_unavailable",
                    message=(
                        "真实 @ 需要 WeFlow 同步到该成员的微信昵称；当前身份资料不完整，"
                        "而“真实 @ 失败时改用普通文字”已关闭，因此未执行发送。"
                    ),
                    kind=outbound.kind,
                    name=outbound.name,
                    details={
                        "send_clicked": False,
                        "mention_mode": mention_mode,
                        "unavailable_member_ids": [
                            str(item.data.get("qq") or "") for item in unavailable
                        ],
                    },
                )
            if unavailable:
                mention_preparation_warnings.extend(
                    {
                        "code": "mention_identity_downgraded",
                        "message": (
                            f"成员 {str(item.data.get('name') or item.data.get('qq') or '')} "
                            "缺少真实 @ 所需资料，已改用普通文字。"
                        ),
                        "member_id": str(item.data.get("qq") or ""),
                    }
                    for item in unavailable
                )

        def flush_text_parts() -> None:
            if text_parts:
                operations.append(("text", tuple(text_parts)))
                text_parts.clear()

        for segment in outbound.segments:
            if segment.type == "at":
                if mention_mode == "real" and bool(
                    segment.data.get("real_mention_available")
                ):
                    text_parts.append(
                        ("mention", f"@{segment.data['mention_name']} ")
                    )
                else:
                    text_parts.append(("text", f"@{segment.data['name']} "))
            elif segment.type == "text":
                value = str(segment.data.get("text") or "")
                if value:
                    text_parts.append(("text", value))
            elif segment.type in {"image", "file"}:
                flush_text_parts()
                operations.append((segment.type, segment.data))
            else:
                flush_text_parts()
                operations.append((segment.type, segment.data))
        flush_text_parts()

        for operation_type, data in operations:
            if cancel_event.is_set():
                result = self._cancelled_result(outbound.kind, outbound.name)
                segment_results.append(result.to_dict())
                return SendResult(
                    ok=False,
                    code=result.code,
                    message=result.message,
                    kind=outbound.kind,
                    name=outbound.name,
                    details={
                        **result.details,
                        "segments": segment_results,
                        "partial_send": len(segment_results) > 1,
                    },
                )
            if operation_type == "text":
                input_parts = tuple(data)
                text = "".join(value for _, value in input_parts)
                result = self._perform_send(
                    outbound.kind,
                    outbound.name,
                    text,
                    input_parts=input_parts,
                    cancel_event=cancel_event,
                )
            elif operation_type in {"image", "file"}:
                result = self._perform_media_send(
                    outbound.kind,
                    outbound.name,
                    operation_type,
                    data,
                    cancel_event=cancel_event,
                )
            else:
                result = SendResult(
                    ok=False,
                    code="unsupported_message_segment",
                    message=f"不支持消息段: {operation_type}",
                    kind=outbound.kind,
                    name=outbound.name,
                )
            segment_results.append(result.to_dict())
            if not result.ok:
                return SendResult(
                    ok=False,
                    code=result.code,
                    message=result.message,
                    kind=outbound.kind,
                    name=outbound.name,
                    details={
                        "segments": segment_results,
                        "partial_send": len(segment_results) > 1,
                    },
                )
            all_verified = all_verified and bool(
                result.details.get("ui_verified", result.code in {"sent", "media_sent"})
            )
        return SendResult(
            ok=True,
            code="sent" if all_verified else "sent_unverified",
            message=(
                "消息段发送动作已完成，并观察到可验证的本地 UI 结果。"
                if all_verified
                else "消息段发送动作已完成；部分媒体或气泡未被本地 UI 强制验证。"
            ),
            kind=outbound.kind,
            name=outbound.name,
            details={
                "segments": segment_results,
                "mention_mode": mention_mode,
                "warnings": mention_preparation_warnings,
                "send_action_completed": True,
                "ui_verified": all_verified,
                "verification": "new_message_visible_in_chat"
                if all_verified
                else "send_action_completed_unverified",
                "server_delivery_confirmed": False,
            },
        )

    def _remaining_reply_delay(self, target_id: int) -> float:
        minimum = self.config.sender.min_reply_delay
        if minimum <= 0:
            return 0.0
        with self._state_lock:
            received_at = self._last_inbound_at.get(target_id)
        if received_at is None:
            return 0.0
        return max(0.0, minimum - (self._monotonic() - received_at))

    async def _wait_for_reply_window(
        self,
        target_id: int,
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> float:
        total = 0.0
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return total
            remaining = self._remaining_reply_delay(target_id)
            if remaining <= 0:
                return total
            self._event(
                "debug",
                "minimum_reply_delay",
                "等待最短回复延迟窗口。",
                remaining_seconds=round(remaining, 3),
            )
            # Keep custom test clocks exact. Production asyncio.sleep is split
            # into short slices so an emergency stop does not wait out the
            # configured minimum reply delay.
            if self._async_sleep is not asyncio.sleep or cancel_event is None:
                await self._async_sleep(remaining)
                total += remaining
                continue
            while remaining > 0:
                if cancel_event.is_set():
                    return total
                sleep_for = min(0.1, remaining)
                await self._async_sleep(sleep_for)
                total += sleep_for
                remaining = self._remaining_reply_delay(target_id)

    async def handle_onebot_action(self, request: dict[str, Any]) -> dict[str, Any]:
        action = str(request.get("action") or "")
        params = request.get("params") or {}
        echo = request.get("echo")
        if not isinstance(params, dict):
            return failed_response(
                "params 必须是对象。", echo=echo, code="invalid_params"
            )
        with self._state_lock:
            self._counters["astrbot_actions"] += 1

        cached = self._cached_response(echo)
        if cached is not None:
            self._event("info", "duplicate_action", "重复 echo 已返回缓存结果，未再次发送。")
            return cached

        if action == "get_version_info":
            return ok_response(
                {
                    "app_name": "wechat-bridge-v3",
                    "app_version": VERSION,
                    "protocol_version": "v11",
                },
                echo=echo,
            )
        if action == "get_login_info":
            bot_nickname = self.config.bot_names[0] if self.config.bot_names else "微信机器人"
            return ok_response(
                {"user_id": self.config.self_id, "nickname": bot_nickname},
                echo=echo,
            )
        if action == "get_status":
            return ok_response(
                {
                    "online": self._running,
                    "good": bool(
                        self.config.astrbot.enabled and self.onebot.connected
                    ),
                },
                echo=echo,
            )
        if action in {"can_send_image", "can_send_file"}:
            return ok_response({"yes": True}, echo=echo)
        if action in {"can_send_record", "can_send_video"}:
            return ok_response({"yes": False}, echo=echo)
        if action == "get_friend_list":
            data = [
                {"user_id": item.target_id, "nickname": item.name, "remark": ""}
                for item in self.registry.list("private")
            ]
            return ok_response(data, echo=echo)
        if action == "get_group_list":
            data = [
                {
                    "group_id": item.target_id,
                    "group_name": item.name,
                    "member_count": len(
                        self.registry.list_group_members(item.target_id)
                    ),
                    "max_member_count": 0,
                }
                for item in self.registry.list("group")
            ]
            return ok_response(data, echo=echo)
        try:
            if action == "get_group_info":
                contact = self.registry.get(
                    params.get("group_id"), expected_kind="group"
                )
                return ok_response(
                    {
                        "group_id": contact.target_id,
                        "group_name": contact.name,
                        "member_count": len(
                            self.registry.list_group_members(contact.target_id)
                        ),
                        "max_member_count": 0,
                    },
                    echo=echo,
                )
            if action in {"get_group_member_info", "get_group_member_list"}:
                contact = self.registry.get(
                    params.get("group_id"), expected_kind="group"
                )
                if action == "get_group_member_info":
                    members = [
                        self.registry.get_group_member(
                            contact.target_id, params.get("user_id")
                        )
                    ]
                else:
                    members = self.registry.list_group_members(contact.target_id)
                member_data = [self._onebot_group_member(item) for item in members]
                return ok_response(
                    member_data[0] if action == "get_group_member_info" else member_data,
                    echo=echo,
                )
            if action == "get_stranger_info":
                user_id = int(params.get("user_id"))
                if user_id == self.config.self_id:
                    nickname = (
                        self.config.bot_names[0]
                        if self.config.bot_names
                        else "微信机器人"
                    )
                else:
                    try:
                        nickname = self.registry.get(
                            user_id, expected_kind="private"
                        ).name
                    except ProtocolError:
                        nickname = self.registry.find_group_member(user_id).name
                return ok_response(
                    {
                        "user_id": user_id,
                        "nickname": nickname,
                        "nick": nickname,
                        "sex": "unknown",
                        "age": 0,
                    },
                    echo=echo,
                )
            if action == "get_msg":
                return ok_response(
                    self.registry.get_message(params.get("message_id")), echo=echo
                )
        except (ProtocolError, TypeError, ValueError) as exc:
            if isinstance(exc, ProtocolError):
                code, message = exc.code, exc.message
            else:
                code, message = "invalid_query_id", "OneBot 查询 ID 无效。"
            return failed_response(message, echo=echo, code=code)
        if action not in self.SEND_ACTIONS:
            return failed_response(
                f"不支持 OneBot 动作: {action}",
                echo=echo,
                code="unsupported_action",
            )

        # Bridge lifecycle and desktop automation lifecycle are independent.
        # Stopping the bridge rejects new OneBot actions and disconnects the
        # transports; stopping automation rejects only new UI sends and
        # cancels work that is already inside the desktop automation layer.
        with self._state_lock:
            running = self._running
            automation_enabled = self._automation_enabled
            cancel_event = self._automation_cancel
        if not running:
            return failed_response(
                "桥接服务已停止，未执行微信发送动作。",
                echo=echo,
                code="service_stopped",
            )
        if not automation_enabled or cancel_event.is_set():
            return failed_response(
                "微信自动化已停止，未执行发送动作。",
                echo=echo,
                code="automation_stopped",
            )

        try:
            outbound = parse_outbound_message(action, params, self.registry)
            outbound = self._expand_plain_text_mentions(outbound)
        except ProtocolError as exc:
            response = failed_response(
                exc.message,
                echo=echo,
                code=exc.code,
            )
            self._remember_response(echo, response)
            return response

        delay_applied = await self._wait_for_reply_window(
            outbound.target_id,
            cancel_event=cancel_event,
        )

        result = await asyncio.to_thread(
            self._perform_outbound,
            outbound,
            cancel_event,
        )
        if result.ok:
            ui_verified = bool(
                result.details.get("ui_verified", result.code == "sent")
            )
            response = ok_response(
                {
                    "message_id": stable_id(
                        "outbound",
                        f"{time.time_ns()}\0{outbound.target_id}",
                    ),
                    "verification": result.details.get("verification")
                    or ("new_message_visible_in_chat" if ui_verified else "send_action_completed_unverified"),
                    "ui_verified": ui_verified,
                    "minimum_reply_delay_applied_seconds": round(delay_applied, 3),
                },
                echo=echo,
            )
        else:
            response = failed_response(
                result.message,
                echo=echo,
                code=result.code,
            )
        self._remember_response(echo, response)
        return response

    @staticmethod
    def _onebot_group_member(member: Any) -> dict[str, Any]:
        return {
            "group_id": member.group_id,
            "user_id": member.member_id,
            "nickname": member.nickname or member.visible_name,
            "card": member.group_nickname or member.remark or member.visible_name,
            "sex": "unknown",
            "age": 0,
            "area": "",
            "join_time": 0,
            "last_sent_time": int(member.updated_at),
            "level": "",
            "role": "member",
            "unfriendly": False,
            "title": "",
            "title_expire_time": 0,
            "card_changeable": False,
            "wxid": member.wxid,
            "display_name": member.display_name,
            "remark": member.remark,
            "alias": member.alias,
            "group_nickname": member.group_nickname,
            "bridge_platform": "wx",
            "real_mention_available": bool(member.wxid and member.nickname),
        }

    def manual_send(self, kind: str, name: str, text: str) -> dict[str, Any]:
        with self._state_lock:
            running = self._running
            automation_enabled = self._automation_enabled
        if not running:
            raise ProtocolError("service_stopped", "桥接服务已停止，未执行微信发送动作。")
        if not automation_enabled:
            raise ProtocolError("automation_stopped", "微信自动化已停止，未执行发送动作。")
        if kind not in {"private", "group"}:
            raise ProtocolError("invalid_kind", "kind 只能是 private 或 group。")
        if not name.strip() or not text.strip():
            raise ProtocolError("invalid_request", "name 和 text 不能为空。")
        result = self._perform_manual_text(kind, name, text)
        return result.to_dict()

    @staticmethod
    def _active_api_request_id(value: Any) -> str:
        request_id = str(value or "").strip()
        if not 8 <= len(request_id) <= 128 or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
            for character in request_id
        ):
            raise ProtocolError(
                "invalid_request_id",
                "request_id 必须是 8..128 个字母、数字、点、下划线、冒号或短横线。",
            )
        return request_id

    def _active_api_public_task(self, task: dict[str, Any]) -> dict[str, Any]:
        request = dict(task.get("request") or {})
        with self._api_lock:
            try:
                queue_position = list(self._api_queue).index(str(task["id"])) + 1
            except ValueError:
                queue_position = 0
        return {
            "ok": True,
            "task_id": str(task["id"]),
            "request_id": str(task["request_id"]),
            "status": str(task["status"]),
            "queue_position": queue_position,
            "progress": copy.deepcopy(task.get("progress") or {}),
            "logs": copy.deepcopy(task.get("logs") or []),
            "poll_after_ms": 500 if task.get("status") in {"queued", "running"} else 0,
            "created_at": task.get("created_at"),
            "started_at": task.get("started_at"),
            "finished_at": task.get("finished_at"),
            "request": request,
            "result": copy.deepcopy(task.get("result")),
        }

    def _finish_queued_active_api_cancel_locked(
        self,
        task_id: str,
        task: dict[str, Any],
        *,
        reason: str,
        message: str,
    ) -> None:
        """Finalize a queued API task while ``_api_lock`` is held."""

        task["status"] = "cancelled"
        task["finished_at"] = time.time()
        task["result"] = {
            "ok": False,
            "code": "automation_cancelled",
            "message": message,
            "reason": reason,
        }
        task["progress"] = {
            "percent": 0,
            "stage": "cancelled",
            "message": message,
        }
        request = task.pop("request_internal", None)
        self._cleanup_active_api_upload(request)
        self._api_task_cancels.pop(task_id, None)

    def _cancel_active_api_tasks(self, *, reason: str, message: str) -> None:
        """Cancel running work and immediately finalize every queued API task.

        Merely setting each cancellation event leaves queued tasks reporting
        ``queued`` until the task in front of them exits.  Machine callers must
        see the stop decision immediately, so queued tasks are removed and
        finalized atomically.  A running task remains cooperative because a
        send click that already happened cannot be rolled back.
        """

        with self._api_lock:
            queued_ids = list(self._api_queue)
            self._api_queue.clear()
            for task_id in queued_ids:
                task = self._api_tasks.get(task_id)
                event = self._api_task_cancels.get(task_id)
                if event is not None:
                    event.set()
                if task is not None and task.get("status") == "queued":
                    self._finish_queued_active_api_cancel_locked(
                        task_id,
                        task,
                        reason=reason,
                        message=message,
                    )
            for task_id, event in list(self._api_task_cancels.items()):
                task = self._api_tasks.get(task_id)
                if task is None or task.get("status") not in {"queued", "running"}:
                    continue
                event.set()
                if task.get("status") == "queued":
                    self._finish_queued_active_api_cancel_locked(
                        task_id,
                        task,
                        reason=reason,
                        message=message,
                    )
                else:
                    task.setdefault("logs", []).append(
                        {
                            "time": time.time(),
                            "level": "WARNING",
                            "source": "active_api",
                            "operation": "active_api.cancel",
                            "message": message,
                            "elapsed_ms": max(
                                0,
                                int(
                                    round(
                                        (time.time() - float(task.get("started_at") or time.time()))
                                        * 1000
                                    )
                                ),
                            ),
                        }
                    )

    def start_active_api_send(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Queue one proactive Agent send with an idempotency key.

        This is deliberately separate from the console's debug endpoint: it is
        a machine-facing API, records no browser session, and never retries a
        duplicate request_id with a second desktop action.
        """

        if not isinstance(payload, dict):
            raise ProtocolError("invalid_request", "请求 JSON 必须是对象。")
        with self._state_lock:
            automation_enabled = self._automation_enabled
        if not automation_enabled:
            raise ProtocolError("automation_stopped", "微信自动化已停止，未创建发送任务。")
        if not self.config.active_api.enabled:
            raise ProtocolError("active_api_disabled", "主动发送 API 尚未启用。")

        request_id = self._active_api_request_id(
            payload.get("request_id") or payload.get("idempotency_key")
        )
        kind = str(payload.get("kind") or "").strip().lower()
        name = str(payload.get("name") or "").strip()
        message_type = str(
            payload.get("message_type") or payload.get("type") or "text"
        ).strip().lower()
        if kind not in {"private", "group"}:
            raise ProtocolError("invalid_kind", "kind 只能是 private 或 group。")
        if not name:
            raise ProtocolError("invalid_request", "name 不能为空。")
        if message_type not in {"text", "image", "file"}:
            raise ProtocolError(
                "invalid_message_type",
                "message_type 只能是 text、image 或 file。",
            )
        text = str(payload.get("text") or "")
        source = ""
        upload_id = str(payload.get("upload_id") or "").strip()
        filename = str(payload.get("filename") or payload.get("name_hint") or "").strip()
        if message_type == "text":
            if not text.strip():
                raise ProtocolError("invalid_request", "text 不能为空。")
            if upload_id:
                raise ProtocolError(
                    "invalid_request",
                    "文字消息不能携带 upload_id。",
                )
        else:
            for key in ("source", "file", "url", "path"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    source = value.strip()
                    break
            if upload_id and source:
                raise ProtocolError(
                    "media_source_conflict",
                    "upload_id 与 source/file/url/path 只能选择一种媒体来源。",
                )
            if not source and not upload_id:
                raise ProtocolError(
                    "media_source_missing",
                    "图片或文件请求必须提供 upload_id、source、file、url 或 path。",
                )
            if len(source) > 4096:
                raise ProtocolError("media_source_invalid", "媒体来源地址过长。")
            if len(filename) > 180:
                raise ProtocolError("invalid_filename", "filename 不能超过 180 个字符。")

        fingerprint_payload = {
            "kind": kind,
            "name": name,
            "message_type": message_type,
            "text": text if message_type == "text" else "",
            "source": source if message_type != "text" else "",
            "upload_id": upload_id if message_type != "text" else "",
            "filename": filename if message_type != "text" else "",
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self._prune_active_api_uploads()
        with self._api_lock:
            existing = self._api_request_index.get(request_id)
            if existing is not None:
                task_id, old_fingerprint = existing
                if old_fingerprint != fingerprint:
                    raise ProtocolError(
                        "request_id_conflict",
                        "request_id 已用于另一条消息；请更换新的 request_id。",
                    )
                task = self._api_tasks.get(task_id)
                if task is not None:
                    return self._active_api_public_task(task)
                self._api_request_index.pop(request_id, None)
            pending = sum(
                1
                for task in self._api_tasks.values()
                if task.get("status") in {"queued", "running"}
            )
            if pending >= 20:
                raise ProtocolError(
                    "active_api_queue_full",
                    "主动发送 API 队列已满，请稍后重试；不要复用新的 request_id 重复提交。",
                )
            upload_cleanup = ""
            resolved_upload = False
            if upload_id:
                upload = self._api_uploads.get(upload_id)
                if not upload or upload.get("status") != "ready":
                    raise ProtocolError(
                        "active_api_upload_not_found",
                        "upload_id 不存在、尚未上传完成、已经使用或已经过期，请重新上传。",
                    )
                if upload.get("media_type") != message_type:
                    raise ProtocolError(
                        "active_api_upload_type_mismatch",
                        "上传文件类型与 message_type 不一致；文件仍然保留，可修正类型后重试。",
                    )
                upload = self._api_uploads.pop(upload_id)
                source = str(upload["path"])
                upload_cleanup = str(Path(source).parent)
                resolved_upload = True
                if not filename:
                    filename = str(upload.get("name") or Path(source).name)
            task_id = secrets.token_urlsafe(12)
            cancel_event = threading.Event()
            task = {
                "id": task_id,
                "request_id": request_id,
                "fingerprint": fingerprint,
                "status": "queued",
                "created_at": time.time(),
                "started_at": None,
                "finished_at": None,
                "request": {
                    "kind": kind,
                    "name": name,
                    "message_type": message_type,
                    "text_length": len(text) if message_type == "text" else 0,
                    "filename": filename if message_type != "text" else "",
                },
                "request_internal": {
                    "kind": kind,
                    "name": name,
                    "message_type": message_type,
                    "text": text,
                    "source": source,
                    "filename": filename,
                    "resolved_upload": resolved_upload,
                    "upload_cleanup": upload_cleanup,
                },
                "result": None,
                "progress": {
                    "percent": 0,
                    "stage": "queued",
                    "message": "请求已进入主动发送队列。",
                },
                "logs": [],
            }
            self._api_tasks[task_id] = task
            self._api_task_cancels[task_id] = cancel_event
            self._api_request_index[request_id] = (task_id, fingerprint)
            self._api_queue.append(task_id)
            while len(self._api_tasks) > 100:
                old_id, old_task = next(iter(self._api_tasks.items()))
                if old_task.get("status") in {"queued", "running"}:
                    break
                self._api_tasks.pop(old_id, None)
                self._api_task_cancels.pop(old_id, None)
                self._api_request_index.pop(str(old_task.get("request_id") or ""), None)
            worker = self._api_worker
            if worker is None or not worker.is_alive():
                worker = threading.Thread(
                    target=self._run_active_api_queue,
                    name="active-api-queue",
                    daemon=True,
                )
                self._api_worker = worker
                worker.start()
        return self._active_api_public_task(task)

    def _run_active_api_queue(self) -> None:
        """Drain API tasks in submission order; desktop input never overlaps."""

        while True:
            with self._api_lock:
                if not self._api_queue:
                    self._api_worker = None
                    return
                task_id = self._api_queue.popleft()
                task = self._api_tasks.get(task_id)
                cancel_event = self._api_task_cancels.get(task_id)
                if task is None or cancel_event is None:
                    continue
                request = dict(task.get("request_internal") or {})
                if task.get("status") == "cancelled":
                    continue
                if cancel_event.is_set():
                    self._finish_queued_active_api_cancel_locked(
                        task_id,
                        task,
                        reason="cancelled_before_desktop",
                        message="任务在开始桌面操作前已取消。",
                    )
                    continue
            self._run_active_api_send(
                task_id,
                request["kind"],
                request["name"],
                request["message_type"],
                request["text"],
                request["source"],
                request["filename"],
                bool(request.get("resolved_upload")),
                str(request.get("upload_cleanup") or ""),
                cancel_event,
            )

    @staticmethod
    def _active_api_progress(operation: str) -> tuple[int, str]:
        operation = str(operation or "")
        stages = (
            ("window.prepare", 12, "准备微信窗口"),
            ("chat.recover", 20, "恢复微信会话层"),
            ("find.微信搜索框", 30, "定位微信搜索框"),
            ("click.search_box", 38, "点击微信搜索框"),
            ("input.type_search_name", 46, "输入会话名称"),
            ("wait.search_settle", 52, "等待搜索结果刷新"),
            ("input.search_shortcut_up", 56, "选择搜索结果"),
            ("wait.search_shortcut_confirm", 58, "等待快捷键确认"),
            ("input.search_shortcut_enter", 61, "进入目标会话"),
            ("find.search_result", 54, "备用方案定位搜索结果"),
            ("click.search_result", 61, "备用鼠标方式进入会话"),
            ("find.聊天输入区域", 70, "定位聊天输入区"),
            ("click.chat_input", 76, "点击聊天输入区"),
            ("input.", 82, "写入消息内容"),
            ("find.发送按钮", 89, "等待可用发送按钮"),
            ("wait.pre_send_review", 93, "发送前检查内容"),
            ("click.send_button", 96, "点击发送按钮"),
            ("message.transaction", 98, "完成发送事务"),
        )
        for prefix, percent, label in stages:
            if operation.startswith(prefix):
                return percent, label
        return 8, "执行桌面自动化"

    def _append_active_api_log(
        self,
        task_id: str,
        level: str,
        message: str,
        *,
        source: str = "",
        operation: str = "",
        duration_ms: Any = None,
    ) -> None:
        now = time.time()
        with self._api_lock:
            task = self._api_tasks.get(task_id)
            if task is None:
                return
            started_at = task.get("started_at")
            item = {
                "time": now,
                "level": str(level or "INFO").upper(),
                "source": str(source or "active_api"),
                "operation": str(operation or ""),
                "message": str(message),
                "elapsed_ms": (
                    max(0, int(round((now - float(started_at)) * 1000)))
                    if started_at
                    else 0
                ),
            }
            if duration_ms is not None:
                item["duration_ms"] = max(0, int(duration_ms))
            task.setdefault("logs", []).append(item)
            if len(task["logs"]) > 300:
                del task["logs"][:-300]
            percent, stage = self._active_api_progress(operation)
            previous = int((task.get("progress") or {}).get("percent", 0))
            task["progress"] = {
                "percent": max(previous, percent),
                "stage": stage,
                "message": str(message),
            }

    def _run_active_api_send(
        self,
        task_id: str,
        kind: str,
        name: str,
        message_type: str,
        text: str,
        source: str,
        filename: str,
        resolved_upload: bool,
        upload_cleanup: str,
        cancel_event: threading.Event,
    ) -> None:
        service = self
        expected_thread_name = threading.current_thread().name

        class TaskHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                if record.threadName != expected_thread_name:
                    return
                try:
                    service._append_active_api_log(
                        task_id,
                        record.levelname,
                        self.format(record),
                        source=record.name,
                        operation=str(getattr(record, "automation_operation", "") or ""),
                        duration_ms=getattr(record, "automation_duration_ms", None),
                    )
                except Exception:
                    pass

        with self._api_lock:
            task = self._api_tasks.get(task_id)
            if task is None:
                return
            if task.get("status") == "cancelled" or (
                cancel_event.is_set() and not task.get("started_at")
            ):
                if task.get("status") != "cancelled":
                    self._finish_queued_active_api_cancel_locked(
                        task_id,
                        task,
                        reason="cancelled_before_desktop",
                        message="任务在开始桌面操作前已取消。",
                    )
                return
            task["status"] = "running"
            task["started_at"] = time.time()
            task["progress"] = {
                "percent": 5,
                "stage": "waiting_for_desktop",
                "message": "已轮到本任务，正在等待全局桌面自动化执行位置。",
            }
        handler = TaskHandler(level=logging.INFO)
        handler.setFormatter(logging.Formatter("%(message)s"))
        task_loggers = [
            logging.getLogger(name) for name in DEBUG_AUTOMATION_LOGGER_NAMES
        ]
        for task_logger in task_loggers:
            task_logger.addHandler(handler)
        self._append_active_api_log(
            task_id,
            "INFO",
            f"开始主动发送：{kind} / {name} / {message_type}",
            operation="active_api.start",
        )
        result_data: dict[str, Any]
        try:
            with recognition_run(
                self._recognition_store,
                run_id=f"api-{task_id}",
                source="runtime",
                capture_success=False,
            ):
                if message_type == "text":
                    result = self._perform_manual_text(
                        kind,
                        name,
                        text,
                        cancel_event=cancel_event,
                    )
                else:
                    result = self._perform_media_send(
                        kind,
                        name,
                        message_type,
                        {"file": source, "name": filename},
                        cancel_event=cancel_event,
                        resolved_path=source if resolved_upload else None,
                    )
            result_data = result.to_dict()
        except ProtocolError as exc:
            result_data = {"ok": False, "code": exc.code, "message": exc.message}
        except Exception as exc:
            log.exception("主动发送 API 任务执行异常。")
            result_data = {
                "ok": False,
                "code": "active_api_send_exception",
                "message": str(exc),
                "exception_type": type(exc).__name__,
            }
        finally:
            for task_logger in task_loggers:
                task_logger.removeHandler(handler)
            if upload_cleanup:
                shutil.rmtree(upload_cleanup, ignore_errors=True)
        with self._api_lock:
            task = self._api_tasks.get(task_id)
            if task is None:
                return
            task["result"] = result_data
            # If the send click was committed, the result remains successful
            # even when the stop signal arrived during post-send observation.
            # Reporting that task as cancelled would invite a dangerous retry.
            task["status"] = (
                "succeeded"
                if bool(result_data.get("ok"))
                else "cancelled"
                if cancel_event.is_set()
                else "failed"
            )
            task["finished_at"] = time.time()
            task["progress"] = {
                "percent": 100 if task["status"] in {"succeeded", "failed"} else int((task.get("progress") or {}).get("percent", 0)),
                "stage": task["status"],
                "message": str(result_data.get("message") or "主动发送任务已结束。"),
            }
            task.pop("request_internal", None)
            self._api_task_cancels.pop(task_id, None)

    def active_api_task(self, task_id: str) -> dict[str, Any]:
        with self._api_lock:
            task = self._api_tasks.get(str(task_id or ""))
            if task is None:
                raise ProtocolError("active_api_task_not_found", "主动发送任务不存在或已过期。")
            return self._active_api_public_task(task)

    def cancel_active_api_task(self, task_id: str) -> dict[str, Any]:
        with self._api_lock:
            task = self._api_tasks.get(str(task_id or ""))
            event = self._api_task_cancels.get(str(task_id or ""))
            if task is None:
                raise ProtocolError("active_api_task_not_found", "主动发送任务不存在或已过期。")
            if task.get("status") in {"queued", "running"} and event is not None:
                event.set()
                if task.get("status") == "queued":
                    try:
                        self._api_queue.remove(str(task_id))
                    except ValueError:
                        pass
                    self._finish_queued_active_api_cancel_locked(
                        str(task_id),
                        task,
                        reason="client_cancelled",
                        message="任务在开始桌面操作前已取消。",
                    )
            return self._active_api_public_task(task)

    def _perform_manual_text(
        self,
        kind: str,
        name: str,
        text: str,
        cancel_event: Optional[threading.Event] = None,
    ) -> SendResult:
        """Run a console text test, allowing WeChat to resolve ``@昵称``.

        A WeFlow member mapping is a useful stability enhancement because it
        can translate a remark/group alias to the profile nickname. It is not
        a prerequisite for a manual UI test: WeChat itself can search either
        form after the text is typed into the visible editor.
        """
        if not (
            kind == "group"
            and self.config.sender.mention_mode == "real"
            and self._has_explicit_at_token(text)
        ):
            return self._perform_send(
                kind,
                name,
                text,
                cancel_event=cancel_event,
            )
        groups = [
            contact
            for contact in self.registry.list("group")
            if contact.name == name
        ]
        if len(groups) == 1:
            outbound = OutboundMessage(
                kind="group",
                name=name,
                target_id=groups[0].target_id,
                segments=(OutboundSegment("text", {"text": text}),),
            )
            try:
                expanded = self._expand_plain_text_mentions(outbound)
            except ProtocolError as exc:
                if exc.code not in {
                    "real_mention_members_unavailable",
                    "real_mention_text_unresolved",
                    "real_mention_text_ambiguous",
                }:
                    raise
                self._event(
                    "info",
                    "debug_mention_mapping_fallback",
                    "调试 @ 未获得唯一 WeFlow 成员映射；将按输入昵称直接打开微信候选框。",
                    target=name,
                    reason=exc.code,
                )
            else:
                mapped_mentions = [
                    segment
                    for segment in expanded.segments
                    if segment.type == "at"
                ]
                if mapped_mentions and all(
                    bool(segment.data.get("real_mention_available"))
                    for segment in mapped_mentions
                ):
                    self._event(
                        "info",
                        "debug_mention_mapping_used",
                        "调试 @ 已使用 WeFlow 成员映射转换为更稳定的微信候选昵称。",
                        target=name,
                        mention_count=len(mapped_mentions),
                    )
                    return self._perform_outbound(
                        expanded,
                        cancel_event=cancel_event,
                    )
        else:
            self._event(
                "info",
                "debug_mention_mapping_fallback",
                (
                    "调试会话尚无 WeFlow 群映射；将按输入昵称直接打开微信候选框。"
                    if not groups
                    else "调试会话存在多个同名 WeFlow 群映射；将跳过映射并由微信候选框确认。"
                ),
                target=name,
                group_mapping_count=len(groups),
            )

        input_parts = self._direct_debug_mention_parts(text)
        return self._perform_send(
            kind,
            name,
            text,
            input_parts=input_parts,
            cancel_event=cancel_event,
        )

    @staticmethod
    def _direct_debug_mention_parts(text: str) -> tuple[tuple[str, str], ...]:
        """Split manual ``@昵称 正文`` text into visible UI input actions.

        Without a member directory the first whitespace terminates a nickname.
        Mapping-backed names may contain spaces because the mapped path above
        uses the registry's longest-alias parser.
        """

        value = str(text or "")
        parts: list[tuple[str, str]] = []
        cursor = 0
        plain_start = 0
        converted = 0
        while cursor < len(value):
            if value[cursor] != "@" or (
                cursor
                and (
                    value[cursor - 1].isalnum()
                    or value[cursor - 1] in "_-＠"
                )
            ):
                cursor += 1
                continue
            end = cursor + 1
            while end < len(value) and not value[end].isspace():
                end += 1
            mention_name = value[cursor + 1 : end].strip()
            if not mention_name:
                cursor += 1
                continue
            if cursor > plain_start:
                parts.append(("text", value[plain_start:cursor]))
            parts.append(("mention", f"@{mention_name} "))
            converted += 1
            cursor = end
            if cursor < len(value) and value[cursor] in " \t\u2005\u3000":
                cursor += 1
            plain_start = cursor
        if plain_start < len(value):
            parts.append(("text", value[plain_start:]))
        return tuple(parts) if converted else (("text", value),)

    def _append_debug_log(
        self,
        task_id: str,
        level: str,
        message: str,
        *,
        source: str = "automation",
        operation: str = "",
        duration_ms: int | None = None,
    ) -> None:
        now = time.time()
        item = {
            "time": now,
            "timestamp": (
                time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))
                + f".{int((now % 1) * 1000):03d}"
            ),
            "level": str(level or "INFO").upper(),
            "source": source,
            "message": str(message),
        }
        if operation:
            item["operation"] = str(operation)
        if duration_ms is not None:
            item["duration_ms"] = max(0, int(duration_ms))
        with self._debug_lock:
            task = self._debug_tasks.get(task_id)
            if task is not None:
                started_monotonic = task.get("started_monotonic")
                if started_monotonic is not None:
                    item["elapsed_ms"] = max(
                        0,
                        int(round((time.monotonic() - float(started_monotonic)) * 1000)),
                    )
                task["logs"].append(item)
                if len(task["logs"]) > 500:
                    del task["logs"][:-500]

    def start_debug_send(
        self,
        kind: str,
        name: str,
        message_type: str,
        *,
        text: str = "",
        path: str = "",
        upload_id: str = "",
    ) -> dict[str, Any]:
        # Debug sends are admitted by the desktop automation lifecycle, not by
        # whether the transport bridge happens to be connected.
        with self._state_lock:
            automation_enabled = self._automation_enabled
        if not automation_enabled:
            raise ProtocolError(
                "automation_stopped",
                "微信自动化已停止，未创建新的微信调试任务。",
            )
        cancel_event = threading.Event()
        kind = str(kind or "").strip().lower()
        name = str(name or "").strip()
        message_type = str(message_type or "").strip().lower()
        if kind not in {"private", "group"}:
            raise ProtocolError("invalid_kind", "会话类型只能是 private 或 group。")
        if not name:
            raise ProtocolError("invalid_request", "会话昵称不能为空。")
        if message_type not in {"text", "image", "file"}:
            raise ProtocolError(
                "invalid_message_type",
                "信息类型只能是 text、image 或 file。",
            )
        if message_type == "text" and not str(text).strip():
            raise ProtocolError("invalid_request", "文本消息不能为空。")
        upload_cleanup = ""
        media_name = ""
        if message_type in {"image", "file"}:
            if upload_id:
                with self._debug_lock:
                    upload = self._debug_uploads.pop(str(upload_id), None)
                if not upload or upload.get("status") != "ready":
                    raise ProtocolError(
                        "debug_upload_not_found",
                        "选择的媒体文件不存在、尚未上传完成或已经过期，请重新选择。",
                    )
                if upload.get("media_type") != message_type:
                    self._remove_debug_upload(upload)
                    raise ProtocolError(
                        "debug_upload_type_mismatch",
                        "上传文件类型与当前信息类型不一致，请重新选择。",
                    )
                path = str(upload["path"])
                upload_cleanup = str(Path(path).parent)
                media_name = str(upload.get("name") or Path(path).name)
            elif not str(path).strip():
                raise ProtocolError("invalid_request", "请选择媒体文件或填写本机媒体路径。")
            else:
                media_name = Path(str(path)).name

        task_id = secrets.token_hex(8)
        task = {
            "id": task_id,
            "status": "queued",
            "created_at": time.time(),
            "started_at": None,
            "started_monotonic": None,
            "finished_at": None,
            "request": {
                "kind": kind,
                "name": name,
                "message_type": message_type,
                "text_length": len(text) if message_type == "text" else 0,
                "media_name": media_name if message_type != "text" else "",
            },
            "logs": [],
            "result": None,
        }
        with self._debug_lock:
            self._debug_tasks[task_id] = task
            self._debug_task_cancels[task_id] = cancel_event
            while len(self._debug_tasks) > 20:
                oldest_id, oldest = next(iter(self._debug_tasks.items()))
                if oldest.get("status") in {"queued", "running"}:
                    break
                self._debug_tasks.pop(oldest_id, None)
                self._debug_task_cancels.pop(oldest_id, None)

        thread = threading.Thread(
            target=self._run_debug_send,
            args=(
                task_id,
                kind,
                name,
                message_type,
                text,
                path,
                upload_cleanup,
                cancel_event,
            ),
            name=f"automation-debug-{task_id}",
            daemon=True,
        )
        thread.start()
        return {"task_id": task_id, "status": "queued"}

    def _run_debug_send(
        self,
        task_id: str,
        kind: str,
        name: str,
        message_type: str,
        text: str,
        path: str,
        upload_cleanup: str,
        cancel_event: threading.Event,
    ) -> None:
        service = self
        expected_thread_name = threading.current_thread().name

        class TaskHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                if record.threadName != expected_thread_name:
                    return
                try:
                    service._append_debug_log(
                        task_id,
                        record.levelname,
                        self.format(record),
                        source=record.name,
                        operation=str(getattr(record, "automation_operation", "") or ""),
                        duration_ms=getattr(record, "automation_duration_ms", None),
                    )
                except Exception:
                    pass

        handler = TaskHandler(level=logging.INFO)
        handler.setFormatter(logging.Formatter("%(message)s"))
        task_loggers = [
            logging.getLogger(name) for name in DEBUG_AUTOMATION_LOGGER_NAMES
        ]
        for task_logger in task_loggers:
            task_logger.addHandler(handler)

        with self._debug_lock:
            task = self._debug_tasks[task_id]
            task["status"] = "running"
            task["started_at"] = time.time()
            task["started_monotonic"] = time.monotonic()
        self._append_debug_log(
            task_id,
            "INFO",
            f"开始调试发送：{kind} / {name} / {message_type}",
        )
        sender = self.config.sender
        try:
            with recognition_run(
                self._recognition_store,
                run_id=task_id,
                source="debug",
                capture_success=True,
            ):
                if message_type == "text":
                    result = self._perform_manual_text(
                        kind,
                        name,
                        text,
                        cancel_event=cancel_event,
                    )
                else:
                    self._append_debug_log(
                        task_id,
                        "INFO",
                        f"已读取媒体文件，准备通过剪贴板发送：{Path(path).name}",
                    )
                    result = self._perform_media_send(
                        kind,
                        name,
                        message_type,
                        {"file": path},
                        cancel_event=cancel_event,
                        resolved_path=path,
                    )
            result_data = result.to_dict()
            final_status = "succeeded" if result.ok else "failed"
            self._append_debug_log(
                task_id,
                "INFO" if result.ok else "ERROR",
                f"调试任务结束：{result.code} - {result.message}",
            )
        except ProtocolError as exc:
            final_status = "failed"
            result_data = {"ok": False, "code": exc.code, "message": exc.message}
            self._append_debug_log(task_id, "ERROR", f"配置或请求异常：{exc.message}")
        except Exception as exc:
            final_status = "failed"
            result_data = {
                "ok": False,
                "code": "debug_send_exception",
                "message": str(exc),
                "exception_type": type(exc).__name__,
            }
            log.exception("自动化调试任务执行异常。")
            self._append_debug_log(
                task_id,
                "ERROR",
                f"未捕获异常 {type(exc).__name__}: {exc}",
            )
        finally:
            for task_logger in task_loggers:
                task_logger.removeHandler(handler)
            with self._debug_lock:
                task = self._debug_tasks.get(task_id)
                if task is not None:
                    # Cleanup is part of task completion. Publish the terminal
                    # state only after the temporary upload directory is gone.
                    task["result"] = result_data
            if upload_cleanup:
                shutil.rmtree(upload_cleanup, ignore_errors=True)
            with self._debug_lock:
                task = self._debug_tasks.get(task_id)
                if task is not None:
                    if cancel_event.is_set() and final_status in {"failed", "succeeded"}:
                        final_status = "cancelled"
                        if not result_data or result_data.get("code") != "automation_cancelled":
                            result_data = {
                                "ok": False,
                                "code": "automation_cancelled",
                                "message": "本次调试已停止，未继续执行后续微信操作。",
                            }
                            task["result"] = result_data
                    task["status"] = final_status
                    task["finished_at"] = time.time()
                self._debug_task_cancels.pop(task_id, None)

    @staticmethod
    def _safe_upload_name(filename: str, media_type: str) -> str:
        name = Path(str(filename or "").replace("\\", "/")).name.strip()
        name = "".join(
            character
            for character in name
            if character not in '<>:"/\\|?*' and ord(character) >= 32
        ).strip(" .")
        if not name:
            name = "image.png" if media_type == "image" else "file.bin"
        if len(name) > 180:
            suffix = Path(name).suffix[:20]
            stem_limit = max(1, 180 - len(suffix))
            name = Path(name).stem[:stem_limit] + suffix
        return name

    @staticmethod
    def _cleanup_active_api_upload(request: Any) -> None:
        if not isinstance(request, dict):
            return
        cleanup = str(request.get("upload_cleanup") or "")
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)

    def _prune_active_api_uploads(self) -> None:
        cutoff = time.time() - 30 * 60
        expired: list[dict[str, Any]] = []
        with self._api_lock:
            for upload_id, upload in list(self._api_uploads.items()):
                if float(upload.get("created_at") or 0) < cutoff:
                    expired.append(self._api_uploads.pop(upload_id))
        for upload in expired:
            self._remove_debug_upload(upload)

    def begin_active_api_upload(
        self,
        media_type: str,
        filename: str,
        content_length: int,
    ) -> dict[str, Any]:
        """Reserve one authenticated, bounded upload for the proactive API."""

        if not self.config.active_api.enabled:
            raise ProtocolError("active_api_disabled", "主动发送 API 尚未启用。")
        self._prune_active_api_uploads()
        validated = self.validate_debug_upload(media_type, filename, content_length)
        media_type = str(validated["media_type"])
        safe_name = str(validated["name"])
        content_length = int(validated["size"])
        with self._api_lock:
            if len(self._api_uploads) >= 20:
                raise ProtocolError(
                    "active_api_upload_limit",
                    "待使用的主动 API 媒体上传已达 20 个；请先使用或取消已有上传。",
                )
            upload_id = secrets.token_urlsafe(24)
            root = Path(self.config.media.temp_dir) / "active-api-uploads"
            folder = root / secrets.token_hex(12)
            folder.mkdir(parents=True, exist_ok=False)
            final_path = folder / safe_name
            temporary_path = folder / (safe_name + ".uploading")
            upload = {
                "id": upload_id,
                "media_type": media_type,
                "name": safe_name,
                "size": content_length,
                "path": str(final_path),
                "temporary_path": str(temporary_path),
                "status": "uploading",
                "created_at": time.time(),
            }
            self._api_uploads[upload_id] = upload
        return dict(upload)

    def finish_active_api_upload(
        self,
        upload_id: str,
        bytes_written: int,
    ) -> dict[str, Any]:
        with self._api_lock:
            upload = self._api_uploads.get(str(upload_id))
            if upload is None or upload.get("status") != "uploading":
                raise ProtocolError(
                    "active_api_upload_not_found",
                    "主动 API 媒体上传任务不存在。",
                )
            if int(bytes_written) != int(upload["size"]):
                failed = self._api_uploads.pop(str(upload_id))
                self._remove_debug_upload(failed)
                raise ProtocolError(
                    "incomplete_upload",
                    "媒体文件上传不完整，请重新上传。",
                )
            Path(upload["temporary_path"]).replace(upload["path"])
            upload["status"] = "ready"
            return {
                "upload_id": upload["id"],
                "name": upload["name"],
                "size": upload["size"],
                "media_type": upload["media_type"],
                "expires_in_seconds": 30 * 60,
            }

    def cancel_active_api_upload(self, upload_id: str) -> bool:
        with self._api_lock:
            upload = self._api_uploads.pop(str(upload_id), None)
        if upload:
            self._remove_debug_upload(upload)
            return True
        return False

    @staticmethod
    def _remove_debug_upload(upload: dict[str, Any]) -> None:
        candidate = upload.get("path") or upload.get("temporary_path")
        if candidate:
            shutil.rmtree(Path(str(candidate)).parent, ignore_errors=True)

    def _prune_debug_uploads(self) -> None:
        cutoff = time.time() - 30 * 60
        expired: list[dict[str, Any]] = []
        with self._debug_lock:
            for upload_id, upload in list(self._debug_uploads.items()):
                if float(upload.get("created_at") or 0) < cutoff:
                    expired.append(self._debug_uploads.pop(upload_id))
        for upload in expired:
            self._remove_debug_upload(upload)

    def begin_debug_upload(
        self,
        media_type: str,
        filename: str,
        content_length: int,
    ) -> dict[str, Any]:
        """Reserve a bounded temporary file for an authenticated UI upload."""

        self._prune_debug_uploads()
        validated = self.validate_debug_upload(media_type, filename, content_length)
        media_type = str(validated["media_type"])
        safe_name = str(validated["name"])
        content_length = int(validated["size"])
        upload_id = secrets.token_urlsafe(24)
        root = Path(self.config.media.temp_dir) / "debug-uploads"
        folder = root / secrets.token_hex(12)
        folder.mkdir(parents=True, exist_ok=False)
        final_path = folder / safe_name
        temporary_path = folder / (safe_name + ".uploading")
        upload = {
            "id": upload_id,
            "media_type": media_type,
            "name": safe_name,
            "size": content_length,
            "path": str(final_path),
            "temporary_path": str(temporary_path),
            "status": "uploading",
            "created_at": time.time(),
        }
        with self._debug_lock:
            self._debug_uploads[upload_id] = upload
        return dict(upload)

    def validate_debug_upload(
        self,
        media_type: str,
        filename: str,
        content_length: Any,
    ) -> dict[str, Any]:
        """Validate upload metadata before the browser starts sending file bytes."""

        media_type = str(media_type or "").strip().lower()
        if media_type not in {"image", "file"}:
            raise ProtocolError("invalid_message_type", "上传类型只能是 image 或 file。")
        try:
            size = int(content_length)
        except (TypeError, ValueError) as exc:
            raise ProtocolError("invalid_length", "媒体文件大小无效。") from exc
        limit = (
            self.config.media.max_image_bytes
            if media_type == "image"
            else self.config.media.max_file_bytes
        )
        if size <= 0:
            raise ProtocolError("empty_upload", "选择的文件为空。")
        if size > limit:
            raise ProtocolError(
                "media_too_large",
                f"文件大小超过当前 {limit // (1024 * 1024)} MiB 上限。",
            )
        safe_name = self._safe_upload_name(filename, media_type)
        return {
            "media_type": media_type,
            "name": safe_name,
            "size": size,
            "max_bytes": int(limit),
        }

    def finish_debug_upload(self, upload_id: str, bytes_written: int) -> dict[str, Any]:
        with self._debug_lock:
            upload = self._debug_uploads.get(str(upload_id))
            if upload is None or upload.get("status") != "uploading":
                raise ProtocolError("debug_upload_not_found", "媒体上传任务不存在。")
            if int(bytes_written) != int(upload["size"]):
                failed = self._debug_uploads.pop(str(upload_id))
                self._remove_debug_upload(failed)
                raise ProtocolError("incomplete_upload", "媒体文件上传不完整，请重新选择。")
            Path(upload["temporary_path"]).replace(upload["path"])
            upload["status"] = "ready"
            result = {
                "upload_id": upload["id"],
                "name": upload["name"],
                "size": upload["size"],
                "media_type": upload["media_type"],
            }
        return result

    def cancel_debug_upload(self, upload_id: str) -> None:
        with self._debug_lock:
            upload = self._debug_uploads.pop(str(upload_id), None)
        if upload:
            self._remove_debug_upload(upload)

    def debug_task(self, task_id: str) -> dict[str, Any]:
        with self._debug_lock:
            task = self._debug_tasks.get(str(task_id))
            if task is None:
                raise ProtocolError("debug_task_not_found", "调试任务不存在或已过期。")
            result = copy.deepcopy(task)
            result.pop("started_monotonic", None)
            result["recognition_snapshots"] = self.recognition_snapshots(
                source="debug",
                run_id=str(task_id),
                limit=120,
            )
            return result

    def recognition_snapshots(
        self,
        *,
        outcome: str = "",
        source: str = "",
        run_id: str = "",
        limit: int = 80,
    ) -> list[dict[str, Any]]:
        normalized_outcome = str(outcome or "").strip().lower()
        normalized_source = str(source or "").strip().lower()
        if normalized_outcome not in {"", "success", "failure"}:
            raise ProtocolError(
                "invalid_snapshot_filter",
                "识别快照结果筛选只能是 success 或 failure。",
            )
        if normalized_source not in {"", "debug", "runtime", "compatibility"}:
            raise ProtocolError(
                "invalid_snapshot_filter",
                "识别快照来源筛选只能是 debug、runtime 或 compatibility。",
            )
        try:
            normalized_limit = max(1, min(int(limit), 120))
        except (TypeError, ValueError) as exc:
            raise ProtocolError(
                "invalid_snapshot_filter",
                "识别快照数量必须是整数。",
            ) from exc
        return self._recognition_store.list(
            outcome=normalized_outcome,
            source=normalized_source,
            run_id=str(run_id or "").strip(),
            limit=normalized_limit,
        )

    def recognition_snapshot_image(
        self,
        snapshot_id: str,
        image_name: str,
    ) -> Path:
        try:
            return self._recognition_store.image_path(snapshot_id, image_name)
        except KeyError as exc:
            raise ProtocolError(
                "recognition_snapshot_not_found",
                str(exc.args[0] if exc.args else "识别快照图片不存在。"),
            ) from exc

    def recognition_snapshot_diagnostic(self, snapshot_id: str) -> bytes:
        try:
            return self._recognition_store.diagnostic_package(snapshot_id)
        except KeyError as exc:
            raise ProtocolError(
                "recognition_snapshot_not_found",
                str(exc.args[0] if exc.args else "识别快照不存在。"),
            ) from exc

    def cancel_debug_task(self, task_id: str) -> dict[str, Any]:
        """Cancel one debug task without stopping the bridge or other sends."""

        with self._debug_lock:
            task = self._debug_tasks.get(str(task_id))
            cancel_event = self._debug_task_cancels.get(str(task_id))
            if task is None:
                raise ProtocolError("debug_task_not_found", "调试任务不存在或已过期。")
            status = str(task.get("status") or "")
            if status in {"queued", "running"} and cancel_event is not None:
                cancel_event.set()
                self._append_debug_log(
                    str(task_id),
                    "WARNING",
                    "已请求停止本次调试任务。",
                    source="wechat_bridge.service",
                    operation="message.cancel",
                )
                return {"task_id": str(task_id), "status": "cancelling"}
            return {"task_id": str(task_id), "status": status}

    def runtime_settings(self) -> dict[str, Any]:
        with self._state_lock:
            sender = self.config.sender
            return {
                "persistence": "runtime_only",
                "restart_behavior": "reload_from_config_file",
                "sender": {
                    "timeout": sender.timeout,
                    "settle": sender.settle,
                    "search_result_wait_min": sender.search_result_wait_min,
                    "search_result_wait_max": sender.search_result_wait_max,
                    "conversation_entry_mode": sender.conversation_entry_mode,
                    "conversation_enter_delay_min": (
                        sender.conversation_enter_delay_min
                    ),
                    "conversation_enter_delay_max": (
                        sender.conversation_enter_delay_max
                    ),
                    "text_verification_timeout": sender.text_verification_timeout,
                    "media_verification_mode": sender.media_verification_mode,
                    "soft_protection": sender.soft_protection,
                    "lock_mouse": sender.lock_mouse,
                    "lock_keyboard": sender.lock_keyboard,
                    "min_reply_delay": sender.min_reply_delay,
                    "send_review_delay_min": sender.send_review_delay_min,
                    "send_review_delay_max": sender.send_review_delay_max,
                    "click_before_delay_min": sender.click_before_delay_min,
                    "click_before_delay_max": sender.click_before_delay_max,
                    "click_hold_duration_min": sender.click_hold_duration_min,
                    "click_hold_duration_max": sender.click_hold_duration_max,
                    "auto_launch_wechat": sender.auto_launch_wechat,
                    "wechat_executable": sender.wechat_executable,
                    "launch_timeout": sender.launch_timeout,
                    "adaptive_layout": sender.adaptive_layout,
                    "reuse_open_chat": sender.reuse_open_chat,
                    "layout_cache": sender.layout_cache,
                    "mention_mode": sender.mention_mode,
                    "mention_candidate_timeout": sender.mention_candidate_timeout,
                    "mention_after_at_delay_min": sender.mention_after_at_delay_min,
                    "mention_after_at_delay_max": sender.mention_after_at_delay_max,
                    "mention_min_wait": sender.mention_min_wait,
                    "mention_before_enter_delay_min": sender.mention_before_enter_delay_min,
                    "mention_before_enter_delay_max": sender.mention_before_enter_delay_max,
                    "mention_confirm_timeout": sender.mention_confirm_timeout,
                    "mention_fallback_enabled": sender.mention_fallback_enabled,
                    "file_launch_fallback": sender.file_launch_fallback,
                    "render_mask_recovery": sender.render_mask_recovery,
                    "mask_retry_count": sender.mask_retry_count,
                    "mask_wait": sender.mask_wait,
                    "retry_max_attempts": sender.retry_max_attempts,
                    "retry_delays": list(sender.retry_delays),
                    "overall_timeout": sender.overall_timeout,
                    "input_mode": sender.input_mode,
                    "append_line_break_after_input": (
                        sender.append_line_break_after_input
                    ),
                    "keyboard_clipboard_threshold_enabled": (
                        sender.keyboard_clipboard_threshold_enabled
                    ),
                    "keyboard_clipboard_threshold_chars": (
                        sender.keyboard_clipboard_threshold_chars
                    ),
                    "character_delay": sender.character_delay,
                    "character_delay_min": sender.character_delay_min,
                    "character_delay_max": sender.character_delay_max,
                    "natural_typing_enabled": sender.natural_typing_enabled,
                    "typing_burst_chars_min": sender.typing_burst_chars_min,
                    "typing_burst_chars_max": sender.typing_burst_chars_max,
                    "typing_pause_min": sender.typing_pause_min,
                    "typing_pause_max": sender.typing_pause_max,
                    "paste_enabled": sender.paste_enabled,
                    "verification_enabled": sender.verification_enabled,
                },
                "schema": {
                    "timeout": {"type": "number", "minimum": 0.001, "maximum": 120},
                    "settle": {"type": "number", "minimum": 0, "maximum": 10},
                    "search_result_wait_min": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 10,
                        "default": 0.5,
                    },
                    "search_result_wait_max": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 10,
                        "default": 0.7,
                    },
                    "conversation_entry_mode": {
                        "type": "string",
                        "enum": ["mouse_click_sections", "keyboard_shortcut"],
                        "default": "mouse_click_sections",
                    },
                    "conversation_enter_delay_min": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 10,
                    },
                    "conversation_enter_delay_max": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 10,
                    },
                    "text_verification_timeout": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 30,
                        "zero_disables": True,
                    },
                    "media_verification_mode": {
                        "type": "string",
                        "enum": ["none"],
                        "reserved": True,
                    },
                    "soft_protection": {"type": "boolean"},
                    "lock_mouse": {"type": "boolean"},
                    "lock_keyboard": {"type": "boolean"},
                    "min_reply_delay": {"type": "number", "minimum": 0, "maximum": 120},
                    "send_review_delay_min": {"type": "number", "minimum": 0, "maximum": 10},
                    "send_review_delay_max": {"type": "number", "minimum": 0, "maximum": 10},
                    "click_before_delay_min": {"type": "number", "minimum": 0, "maximum": 10},
                    "click_before_delay_max": {"type": "number", "minimum": 0, "maximum": 10},
                    "click_hold_duration_min": {"type": "number", "minimum": 0, "maximum": 2},
                    "click_hold_duration_max": {"type": "number", "minimum": 0, "maximum": 2},
                    "auto_launch_wechat": {"type": "boolean"},
                    "wechat_executable": {"type": "string"},
                    "launch_timeout": {"type": "number", "minimum": 1, "maximum": 120},
                    "adaptive_layout": {"type": "boolean"},
                    "reuse_open_chat": {"type": "boolean"},
                    "layout_cache": {"type": "boolean"},
                    "mention_mode": {
                        "type": "string",
                        "enum": ["real", "plain_text"],
                        "default": "real",
                        "experimental": ["real"],
                    },
                    "mention_candidate_timeout": {
                        "type": "number",
                        "minimum": 0.2,
                        "maximum": 30,
                    },
                    "mention_after_at_delay_min": {"type": "number", "minimum": 0, "maximum": 10},
                    "mention_after_at_delay_max": {"type": "number", "minimum": 0, "maximum": 10},
                    "mention_min_wait": {"type": "number", "minimum": 0, "maximum": 30},
                    "mention_before_enter_delay_min": {"type": "number", "minimum": 0, "maximum": 10},
                    "mention_before_enter_delay_max": {"type": "number", "minimum": 0, "maximum": 10},
                    "mention_confirm_timeout": {"type": "number", "minimum": 0.1, "maximum": 10},
                    "mention_fallback_enabled": {"type": "boolean", "default": True},
                    "file_launch_fallback": {"type": "boolean"},
                    "render_mask_recovery": {"type": "boolean"},
                    "mask_retry_count": {"type": "integer", "minimum": 0, "maximum": 5},
                    "mask_wait": {"type": "number", "minimum": 0, "maximum": 10},
                    "retry_max_attempts": {"type": "integer", "minimum": 1, "maximum": 10},
                    "retry_delays": {
                        "type": "array",
                        "items": {"type": "number", "minimum": 0, "maximum": 120},
                        "maxItems": 9,
                    },
                    "overall_timeout": {"type": "number", "minimum": 1, "maximum": 600},
                    "input_mode": {
                        "type": "string",
                        "enum": ["adaptive", "clipboard", "keyboard"],
                    },
                    "append_line_break_after_input": {
                        "type": "boolean",
                        "default": False,
                        "requires": "automation.wechat_ctrl_enter_confirmed",
                    },
                    "keyboard_clipboard_threshold_enabled": {
                        "type": "boolean",
                        "default": False,
                        "requires": "input_mode=keyboard",
                    },
                    "keyboard_clipboard_threshold_chars": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100000,
                        "default": 40,
                    },
                    "character_delay": {"type": "number", "minimum": 0, "maximum": 2},
                    "character_delay_min": {"type": "number", "minimum": 0, "maximum": 2},
                    "character_delay_max": {"type": "number", "minimum": 0, "maximum": 2},
                    "natural_typing_enabled": {
                        "type": "boolean",
                        "default": True,
                        "requires": "input_mode=keyboard",
                    },
                    "typing_burst_chars_min": {"type": "integer", "minimum": 1, "maximum": 100},
                    "typing_burst_chars_max": {"type": "integer", "minimum": 1, "maximum": 100},
                    "typing_pause_min": {"type": "number", "minimum": 0, "maximum": 10},
                    "typing_pause_max": {"type": "number", "minimum": 0, "maximum": 10},
                    "paste_enabled": {"type": "boolean"},
                    "verification_enabled": {"type": "boolean"},
                },
            }

    def read_logs(self, kind: str, lines: int = 200) -> dict[str, Any]:
        try:
            return read_log_tail(self.config.logging.directory, kind, lines)
        except (TypeError, ValueError) as exc:
            raise ProtocolError("invalid_log_request", str(exc)) from exc

    def update_runtime_settings(self, raw: dict[str, Any]) -> dict[str, Any]:
        sender_raw = raw.get("sender")
        if not isinstance(sender_raw, dict):
            raise ProtocolError("invalid_settings", "sender must be a JSON object.")

        allowed = {
            "timeout",
            "settle",
            "search_result_wait_min",
            "search_result_wait_max",
            "conversation_entry_mode",
            "conversation_enter_delay_min",
            "conversation_enter_delay_max",
            "text_verification_timeout",
            "media_verification_mode",
            "soft_protection",
            "lock_mouse",
            "lock_keyboard",
            "min_reply_delay",
            "send_review_delay_min",
            "send_review_delay_max",
            "click_before_delay_min",
            "click_before_delay_max",
            "click_hold_duration_min",
            "click_hold_duration_max",
            "auto_launch_wechat",
            "wechat_executable",
            "launch_timeout",
            "adaptive_layout",
            "reuse_open_chat",
            "layout_cache",
            "mention_mode",
            "mention_candidate_timeout",
            "mention_after_at_delay_min",
            "mention_after_at_delay_max",
            "mention_min_wait",
            "mention_before_enter_delay_min",
            "mention_before_enter_delay_max",
            "mention_confirm_timeout",
            "mention_fallback_enabled",
            "file_launch_fallback",
            "render_mask_recovery",
            "mask_retry_count",
            "mask_wait",
            "retry_max_attempts",
            "retry_delays",
            "overall_timeout",
            "input_mode",
            "append_line_break_after_input",
            "keyboard_clipboard_threshold_enabled",
            "keyboard_clipboard_threshold_chars",
            "character_delay",
            "character_delay_min",
            "character_delay_max",
            "natural_typing_enabled",
            "typing_burst_chars_min",
            "typing_burst_chars_max",
            "typing_pause_min",
            "typing_pause_max",
            "paste_enabled",
            "verification_enabled",
        }
        unknown = sorted(set(sender_raw) - allowed)
        if unknown:
            raise ProtocolError(
                "unknown_setting",
                "Unknown sender settings: " + ", ".join(unknown),
            )

        bool_fields = {
            "soft_protection",
            "lock_mouse",
            "lock_keyboard",
            "auto_launch_wechat",
            "adaptive_layout",
            "reuse_open_chat",
            "layout_cache",
            "file_launch_fallback",
            "render_mask_recovery",
            "mention_fallback_enabled",
            "append_line_break_after_input",
            "keyboard_clipboard_threshold_enabled",
            "natural_typing_enabled",
            "paste_enabled",
            "verification_enabled",
        }
        integer_fields = {
            "mask_retry_count",
            "retry_max_attempts",
            "keyboard_clipboard_threshold_chars",
            "typing_burst_chars_min",
            "typing_burst_chars_max",
        }
        number_fields = {
            "timeout",
            "settle",
            "search_result_wait_min",
            "search_result_wait_max",
            "conversation_enter_delay_min",
            "conversation_enter_delay_max",
            "text_verification_timeout",
            "min_reply_delay",
            "send_review_delay_min",
            "send_review_delay_max",
            "click_before_delay_min",
            "click_before_delay_max",
            "click_hold_duration_min",
            "click_hold_duration_max",
            "launch_timeout",
            "mention_candidate_timeout",
            "mention_after_at_delay_min",
            "mention_after_at_delay_max",
            "mention_min_wait",
            "mention_before_enter_delay_min",
            "mention_before_enter_delay_max",
            "mention_confirm_timeout",
            "mask_wait",
            "overall_timeout",
            "character_delay",
            "character_delay_min",
            "character_delay_max",
            "typing_pause_min",
            "typing_pause_max",
        }
        changes: dict[str, Any] = {}
        for key, value in sender_raw.items():
            if key in bool_fields:
                if not isinstance(value, bool):
                    raise ProtocolError("invalid_setting_type", f"{key} must be boolean.")
                changes[key] = value
            elif key in integer_fields:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ProtocolError("invalid_setting_type", f"{key} must be an integer.")
                changes[key] = value
            elif key in number_fields:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ProtocolError("invalid_setting_type", f"{key} must be numeric.")
                changes[key] = float(value)
            elif key in {
                "media_verification_mode",
                "mention_mode",
                "input_mode",
                "conversation_entry_mode",
            }:
                changes[key] = str(value or "").strip().lower()
            elif key == "wechat_executable":
                changes[key] = str(value or "").strip()
            elif key == "retry_delays":
                if not isinstance(value, list) or any(
                    isinstance(item, bool) or not isinstance(item, (int, float))
                    for item in value
                ):
                    raise ProtocolError(
                        "invalid_setting_type", "retry_delays must be a numeric array."
                    )
                changes[key] = tuple(float(item) for item in value)

        try:
            with self._state_lock:
                candidate = replace(
                    self.config,
                    sender=replace(self.config.sender, **changes),
                ).validate()
                self.config = candidate
        except (ConfigError, TypeError, ValueError) as exc:
            raise ProtocolError("invalid_settings", str(exc)) from exc

        self._event(
            "info",
            "runtime_settings_updated",
            "Runtime sender settings were updated from the authenticated console.",
            changed=sorted(changes),
        )
        return self.runtime_settings()

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            def connection_state(
                enabled: bool,
                client: Any,
                expected_url: str,
                *,
                current_client: bool,
                reconnecting: bool,
            ) -> str:
                if not enabled:
                    return "disabled"
                if not self._running:
                    return "stopped"
                if not current_client:
                    return "reconnecting"
                client_url = str(
                    getattr(getattr(client, "config", None), "url", "") or ""
                )
                if client_url and client_url != expected_url:
                    return "error"
                if bool(client.connected):
                    return "connected"
                if str(client.last_error or "").strip():
                    return "error"
                return "reconnecting" if reconnecting else "connecting"

            astrbot_current = (
                self._onebot_client_generation == self._onebot_generation
            )
            weflow_current = (
                self._weflow_client_generation == self._weflow_generation
            )

            astrbot_state = connection_state(
                self.config.astrbot.enabled,
                self.onebot,
                self.config.astrbot.url,
                current_client=astrbot_current,
                reconnecting=self._onebot_reconnecting,
            )
            weflow_state = connection_state(
                self.config.weflow.enabled,
                self.weflow,
                self.config.weflow.url,
                current_client=weflow_current,
                reconnecting=self._weflow_reconnecting,
            )
            return {
                "version": VERSION,
                "running": self._running,
                "automation_enabled": self._automation_enabled,
                "automation_active": self._automation_active,
                "automation_stop_requested": self._automation_cancel.is_set(),
                "automation_stop_reason": self._automation_stop_reason,
                "automation_stop_sequence": self._automation_stop_sequence,
                "qt_accessibility": self._qt_accessibility_payload(),
                "uptime_seconds": (
                    max(0, int(time.time() - self._started_at))
                    if self._running and self._started_at
                    else 0
                ),
                "connections": {
                    "astrbot": {
                        "enabled": self.config.astrbot.enabled,
                        "state": astrbot_state,
                        "connected": bool(
                            self.config.astrbot.enabled
                            and astrbot_current
                            and self.onebot.connected
                        ),
                        "generation": self._onebot_generation,
                        "url": self.config.astrbot.url,
                        "client_url": str(
                            (
                                getattr(
                                    getattr(self.onebot, "config", None),
                                    "url",
                                    "",
                                )
                                if astrbot_current
                                else ""
                            )
                            or ""
                        ),
                        "last_error": (
                            self.onebot.last_error
                            if self.config.astrbot.enabled and astrbot_current
                            else ""
                        ),
                        "token_configured": bool(self.config.astrbot.token),
                    },
                    "weflow": {
                        "enabled": self.config.weflow.enabled,
                        "state": weflow_state,
                        "connected": bool(
                            self.config.weflow.enabled
                            and weflow_current
                            and self.weflow.connected
                        ),
                        "sse_connected": bool(
                            self.config.weflow.enabled
                            and weflow_current
                            and getattr(self.weflow, "sse_connected", False)
                        ),
                        "generation": self._weflow_generation,
                        "url": self.config.weflow.url,
                        "client_url": str(
                            (
                                getattr(
                                    getattr(self.weflow, "config", None),
                                    "url",
                                    "",
                                )
                                if weflow_current
                                else ""
                            )
                            or ""
                        ),
                        "last_error": (
                            self.weflow.last_error
                            if self.config.weflow.enabled and weflow_current
                            else ""
                        ),
                        "token_configured": bool(self.config.weflow.token),
                    },
                },
                "setup": {
                    "astrbot_token_configured": bool(self.config.astrbot.token),
                    "weflow_token_configured": bool(self.config.weflow.token),
                    "requires_attention": (
                        astrbot_state in {"unconfigured", "error"}
                        or weflow_state in {"unconfigured", "error"}
                    ),
                    "bot_names_configured": bool(self.config.bot_names),
                    "mention_detection_ready": bool(self.config.bot_names),
                    "mention_warning": (
                        "未填写机器人微信显示名称；文本中的 @机器人 将作为普通文本，"
                        "仅 @ 触发模式也无法可靠识别。"
                        if not self.config.bot_names
                        else ""
                    ),
                    "outbound_mention_mode": self.config.sender.mention_mode,
                    "outbound_real_mention_experimental": True,
                    "outbound_real_mention_ready_members": sum(
                        1
                        for group in self.registry.list("group")
                        for member in self.registry.list_group_members(group.target_id)
                        if member.wxid and member.nickname
                    ),
                },
                "platform_metadata": {
                    "business_platform": "wx",
                    "display_name": "微信个人号",
                    "transport_protocol": "OneBot v11",
                    "transport_adapter": "aiocqhttp",
                    "platform_id_hint": "微信个人号",
                    "numeric_id_semantics": "bridge_generated_stable_mapping",
                    "numeric_ids_are_qq_numbers": False,
                },
                "counters": dict(self._counters),
                "capabilities": {
                    "outbound_text": True,
                    "outbound_image": True,
                    "outbound_file": True,
                    "outbound_voice": False,
                    "inbound_text": True,
                    "inbound_media": "placeholder_text_only",
                    "outbound_mention": {
                        "mode": self.config.sender.mention_mode,
                        "real_experimental": True,
                        "plain_text_available": True,
                        "real_requires": [
                            "weflow_member_wxid",
                            "wechat_nickname",
                            "dedicated_mention_list_candidate_click_confirmation",
                        ],
                    },
                },
                "reply_policy": {
                    "minimum_delay_seconds": self.config.sender.min_reply_delay,
                    "basis": "latest_weflow_message_received_monotonic_time",
                },
                "send_verification": {
                    "text_timeout_seconds": self.config.sender.text_verification_timeout,
                    "text_zero_disables": True,
                    "media_mode": self.config.sender.media_verification_mode,
                    "post_click_retry": False,
                },
                "logging": {
                    "directory": self.config.logging.directory,
                    "bridge_file": "bridge.log",
                    "transport_file": "transport.jsonl",
                    "rotation_max_bytes": self.config.logging.max_bytes,
                    "rotation_backup_count": self.config.logging.backup_count,
                },
                "security_warnings": self.config.security_warnings(),
                "recent_events": list(self._events),
            }
