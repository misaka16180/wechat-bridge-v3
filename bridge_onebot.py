"""AstrBot OneBot v11 reverse WebSocket client."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
from typing import Any, Awaitable, Callable, Optional

import websockets

from bridge_config import AstrBotConfig
from bridge_logging import transport_event
from bridge_protocol import failed_response


log = logging.getLogger("wechat_bridge.onebot")
ActionHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class OneBotReverseClient:
    def __init__(
        self,
        config: AstrBotConfig,
        self_id: int,
        action_handler: ActionHandler,
    ) -> None:
        self.config = config
        self.self_id = self_id
        self.action_handler = action_handler
        self._active = threading.Event()
        self._stop_requested = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws: Any = None
        self._connected = False
        self._lock = threading.Lock()
        self.last_error = ""

    def _safe_error(self, error: BaseException) -> str:
        message = str(error)
        if self.config.token:
            message = message.replace(self.config.token, "***")
        return message

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    def _set_connected(self, value: bool) -> None:
        with self._lock:
            self._connected = value

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_requested.clear()
        self._active.set()
        self._thread = threading.Thread(
            target=self._run,
            name="astrbot-onebot",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self.request_stop()
        self.wait_stopped()

    def wait_stopped(self, timeout: float = 2.0) -> bool:
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                log.warning(
                    "桥接器的 AstrBot OneBot WebSocket 连接在 %.1f 秒内未断开，"
                    "后台连接线程将自行退出。",
                    timeout,
                )
                return False
        return True

    def request_stop(self) -> bool:
        """Signal shutdown immediately; waiting is separated for parallel stop."""
        was_active = self._active.is_set() or bool(
            self._thread and self._thread.is_alive()
        )
        self._stop_requested.set()
        self._active.clear()
        loop = self._loop
        ws = self._ws
        if loop and ws:
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), loop)
            except Exception:
                pass
        if loop:
            try:
                loop.call_soon_threadsafe(lambda: None)
            except (RuntimeError, AttributeError):
                pass
        return was_active

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        finally:
            self._set_connected(False)
            self._loop = None
            try:
                loop.close()
            except Exception:
                pass

    def _connect_kwargs(self) -> dict[str, Any]:
        headers = {
            "X-Self-ID": str(self.self_id),
            "X-Client-Role": "Universal",
            "User-Agent": "OneBot/11",
        }
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"
        parameters = inspect.signature(websockets.connect).parameters
        kwargs: dict[str, Any] = {
            "ping_interval": self.config.heartbeat,
            "ping_timeout": max(5.0, self.config.heartbeat / 2),
            "close_timeout": 5,
            "max_size": self.config.max_message_bytes,
        }
        if "additional_headers" in parameters:
            kwargs["additional_headers"] = headers
        else:
            kwargs["extra_headers"] = headers
        if "user_agent_header" in parameters:
            kwargs["user_agent_header"] = None
        if "proxy" in parameters:
            kwargs["proxy"] = None
        return kwargs

    async def _main(self) -> None:
        while self._active.is_set():
            try:
                log.info(
                    "正在连接 AstrBot OneBot WebSocket：%s",
                    self.config.url,
                )
                async with websockets.connect(
                    self.config.url,
                    **self._connect_kwargs(),
                ) as ws:
                    self._ws = ws
                    self._set_connected(True)
                    self.last_error = ""
                    log.info(
                        "AstrBot OneBot WebSocket 已连接：%s",
                        self.config.url,
                    )
                    async for raw in ws:
                        if not self._active.is_set():
                            break
                        response = await self._handle_raw(raw)
                        if response is not None:
                            await ws.send(json.dumps(response, ensure_ascii=False))
                            transport_event(
                                "outbound",
                                "astrbot",
                                "action_response_sent",
                                response,
                            )
            except Exception as exc:
                if self._active.is_set():
                    self.last_error = self._safe_error(exc)
                    log.warning("AstrBot WebSocket 连接异常: %s", self.last_error)
            finally:
                self._ws = None
                self._set_connected(False)
            if self._active.is_set():
                await asyncio.to_thread(
                    self._stop_requested.wait,
                    self.config.reconnect,
                )
        log.info("桥接器的 AstrBot OneBot WebSocket 连接已断开。")

    async def _handle_raw(self, raw: Any) -> Optional[dict[str, Any]]:
        if not isinstance(raw, str):
            return failed_response("仅接受 JSON 文本帧。", code="binary_not_supported")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return failed_response("JSON 无效。", code="invalid_json")
        if not isinstance(data, dict):
            return failed_response("OneBot 请求必须是对象。", code="invalid_request")
        transport_event("inbound", "astrbot", "action_received", data)
        try:
            return await self.action_handler(data)
        except Exception:
            log.exception("OneBot 动作处理异常。")
            return failed_response(
                "桥接内部错误。",
                echo=data.get("echo"),
                code="internal_error",
            )

    def push(self, event: dict[str, Any], timeout: float = 5.0) -> bool:
        loop = self._loop
        ws = self._ws
        if not loop or not ws or not self.connected:
            return False
        payload = json.dumps(event, ensure_ascii=False)
        try:
            future = asyncio.run_coroutine_threadsafe(ws.send(payload), loop)
            future.result(timeout=timeout)
            transport_event("outbound", "astrbot", "message_event_sent", event)
            return True
        except Exception as exc:
            self.last_error = self._safe_error(exc)
            log.warning("推送 OneBot 事件失败: %s", self.last_error)
            return False
