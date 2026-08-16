"""Authenticated HTTP console for the WeChat bridge."""

from __future__ import annotations

import hmac
import copy
import ipaddress
import json
import logging
import os
import secrets
import socket
import ssl
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, unquote, urlparse

from bridge_config import (
    BridgeConfig,
    ConfigError,
    ConsoleConfig,
    EDITABLE_PROFILE_NAMES,
    SENDER_PROFILE_NAMES,
    TEMPLATE_PROFILE_NAMES,
    default_sender_profiles,
    load_config,
    sender_profile_state,
    update_config_file,
)
from bridge_console_page import LOGIN_PAGE, PAGE
from bridge_protocol import ProtocolError
from bridge_security import hash_password, verify_password
from bridge_service import BridgeService
from wechat_qt_accessibility import QT_HOT_ACTIVATION_NOTICE_VERSION


log = logging.getLogger("wechat_bridge.console")
MAX_BODY_BYTES = 64 * 1024


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    """Prevent Windows from sharing one console port between old/new processes."""

    allow_reuse_address = False

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_EXCLUSIVEADDRUSE,
                1,
            )
        super().server_bind()


class BridgeConsole:
    def __init__(
        self,
        config: ConsoleConfig,
        service: BridgeService,
        *,
        config_path: str | Path | None = None,
    ) -> None:
        self.config = config
        self.service = service
        self.config_path = Path(config_path).resolve() if config_path else None
        self.server: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self._sessions: dict[str, tuple[str, float]] = {}
        self._login_failures: dict[str, tuple[int, float]] = {}
        self._auth_lock = threading.RLock()
        self._config_lock = threading.RLock()
        runtime_sender = dict((service.runtime_settings() or {}).get("sender") or {})
        self._runtime_sender_profiles = default_sender_profiles(runtime_sender)
        self._runtime_active_sender_profile = "custom"

    def _login(self, ip: str, username: str, password: str) -> tuple[str, float]:
        now = time.time()
        with self._auth_lock:
            failures, locked_until = self._login_failures.get(ip, (0, 0.0))
            if locked_until > now:
                raise ProtocolError(
                    "login_rate_limited",
                    f"登录失败次数过多，请 {int(locked_until - now) + 1} 秒后重试。",
                )
        # Always execute the expensive password verification, even when the
        # username is wrong.  Short-circuiting here would make unknown account
        # names measurably faster to reject on a LAN-facing console.
        username_valid = hmac.compare_digest(username, self.config.username)
        password_valid = verify_password(password, self.config.password_hash)
        valid = username_valid and password_valid
        if not valid:
            with self._auth_lock:
                failures, _ = self._login_failures.get(ip, (0, 0.0))
                failures += 1
                self._login_failures[ip] = (
                    failures,
                    now + 60 if failures >= 5 else 0.0,
                )
            raise ProtocolError("invalid_credentials", "用户名或密码错误。")
        token = secrets.token_urlsafe(32)
        expires_at = now + self.config.session_ttl_seconds
        with self._auth_lock:
            self._login_failures.pop(ip, None)
            self._sessions[token] = (username, expires_at)
        return token, expires_at

    def _session_valid(self, token: str) -> bool:
        if not token:
            return False
        now = time.time()
        with self._auth_lock:
            expired = [key for key, (_, expiry) in self._sessions.items() if expiry <= now]
            for key in expired:
                self._sessions.pop(key, None)
            session = self._sessions.get(token)
            return bool(session and session[1] > now)

    def _logout(self, token: str) -> None:
        with self._auth_lock:
            self._sessions.pop(token, None)

    def _invalidate_sessions(self) -> None:
        with self._auth_lock:
            self._sessions.clear()

    def _require_config_path(self) -> Path:
        if self.config_path is None:
            raise ProtocolError(
                "config_persistence_unavailable",
                "当前控制台没有配置文件路径，无法持久化该设置。",
            )
        return self.config_path

    def _persist(self, mutator: Callable[[dict[str, Any]], Any]) -> BridgeConfig:
        path = self._require_config_path()
        with self._config_lock:
            update_config_file(path, mutator)
            updated = load_config(path)
            apply = getattr(self.service, "apply_config", None)
            if callable(apply):
                apply(updated)
            self.config = updated.console
            return updated

    @staticmethod
    def _mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
        value = raw.get(key)
        if value is None:
            value = {}
            raw[key] = value
        if not isinstance(value, dict):
            raise ConfigError(f"{key} 必须是 JSON 对象。")
        return value

    def _account_payload(self) -> dict[str, Any]:
        return {
            "username": self.config.username,
            "setup_required": self.config.force_password_change,
        }

    def _change_account(self, body: dict[str, Any]) -> dict[str, Any]:
        username = str(body.get("username") or self.config.username).strip()
        password = str(body.get("password") or "")
        if len(username) < 3 or len(username) > 64:
            raise ProtocolError("invalid_username", "账号长度必须在 3 到 64 个字符之间。")
        if len(password) < 8 or len(password) > 256:
            raise ProtocolError("invalid_password", "新密码长度必须在 8 到 256 个字符之间。")
        if verify_password(password, self.config.password_hash):
            raise ProtocolError("password_unchanged", "新密码不能与当前密码相同。")
        if not self.config.force_password_change:
            current_password = str(body.get("current_password") or "")
            if not verify_password(current_password, self.config.password_hash):
                raise ProtocolError("invalid_current_password", "当前密码不正确。")

        password_hash = hash_password(password)

        def mutate(raw: dict[str, Any]) -> None:
            console = self._mapping(raw, "console")
            console["username"] = username
            console["password_hash"] = password_hash
            console["force_password_change"] = False
            console.pop("password", None)
            console.pop("initial_password_protected", None)

        self._persist(mutate)
        self._invalidate_sessions()
        log.info("控制台账号已更新，全部会话已注销。")
        return {"ok": True, "reauth_required": True, "username": username}

    @staticmethod
    def _connection_state(enabled: bool, token: str, client: Any) -> str:
        if not enabled:
            return "disabled"
        if bool(getattr(client, "connected", False)):
            return "connected"
        if str(getattr(client, "last_error", "") or "").strip():
            return "error"
        return "connecting"

    def _connections_payload(self) -> dict[str, Any]:
        config = getattr(self.service, "config", None)
        if config is None:
            raise ProtocolError("connections_unavailable", "服务未公开连接配置。")
        onebot = getattr(self.service, "onebot", None)
        weflow = getattr(self.service, "weflow", None)
        try:
            service_status = self.service.status()
            running = bool(service_status.get("running"))
            runtime_connections = service_status.get("connections") or {}
        except Exception:
            running = True
            runtime_connections = {}

        def runtime(name: str) -> dict[str, Any]:
            value = runtime_connections.get(name)
            return value if isinstance(value, dict) else {}

        def runtime_value(name: str, key: str, fallback: Any) -> Any:
            current = runtime(name)
            return current[key] if key in current else fallback

        def state(
            name: str,
            enabled: bool,
            token: str,
            client: Any,
            expected_url: str,
        ) -> str:
            current = runtime(name)
            current_state = str(current.get("state") or "")
            if current_state:
                return current_state
            if enabled and not running:
                return "stopped"
            client_url = str(
                getattr(getattr(client, "config", None), "url", "") or ""
            )
            if enabled and client_url and client_url != expected_url:
                return "error"
            return self._connection_state(enabled, token, client)
        return {
            "astrbot": {
                "enabled": config.astrbot.enabled,
                "url": config.astrbot.url,
                "heartbeat": config.astrbot.heartbeat,
                "reconnect": config.astrbot.reconnect,
                "token_configured": bool(config.astrbot.token),
                "client_url": str(
                    runtime_value(
                        "astrbot",
                        "client_url",
                        getattr(getattr(onebot, "config", None), "url", ""),
                    )
                    or ""
                ),
                "state": state(
                    "astrbot",
                    config.astrbot.enabled,
                    config.astrbot.token,
                    onebot,
                    config.astrbot.url,
                ),
                "last_error": (
                    str(
                        runtime_value(
                            "astrbot",
                            "last_error",
                            getattr(onebot, "last_error", ""),
                        )
                        or ""
                    )
                    if config.astrbot.enabled
                    else ""
                ),
                "generation": runtime("astrbot").get("generation"),
            },
            "weflow": {
                "enabled": config.weflow.enabled,
                "url": config.weflow.url,
                "reconnect": config.weflow.reconnect,
                "connect_timeout": config.weflow.connect_timeout,
                "read_timeout": config.weflow.read_timeout,
                "token_configured": bool(config.weflow.token),
                "client_url": str(
                    runtime_value(
                        "weflow",
                        "client_url",
                        getattr(getattr(weflow, "config", None), "url", ""),
                    )
                    or ""
                ),
                "state": state(
                    "weflow",
                    config.weflow.enabled,
                    config.weflow.token,
                    weflow,
                    config.weflow.url,
                ),
                "last_error": (
                    str(
                        runtime_value(
                            "weflow",
                            "last_error",
                            getattr(weflow, "last_error", ""),
                        )
                        or ""
                    )
                    if config.weflow.enabled
                    else ""
                ),
                "generation": runtime("weflow").get("generation"),
            },
        }

    def _connection_change_payload(
        self,
        name: str,
        *,
        changed: bool,
    ) -> dict[str, Any]:
        payload = self._connections_payload()
        item = payload[name]
        reconnect_requested = bool(changed and item.get("enabled"))
        if reconnect_requested:
            try:
                running = bool(self.service.status().get("running"))
            except Exception:
                running = True
            if running:
                # This response acknowledges the new connection cycle. Even if
                # a local test transport connects synchronously, callers first
                # see that the previous heartbeat was invalidated.
                item["state"] = "reconnecting"
                item["last_error"] = ""
        item["reconnect_requested"] = reconnect_requested
        return payload

    def _save_connections(self, body: dict[str, Any]) -> dict[str, Any]:
        """Backward-compatible endpoint that saves both connection cards."""

        astrbot_body = body.get("astrbot")
        weflow_body = body.get("weflow")
        if not isinstance(astrbot_body, dict) or not isinstance(weflow_body, dict):
            raise ProtocolError(
                "invalid_connections",
                "astrbot 和 weflow 必须是 JSON 对象。",
            )

        def mutate(raw: dict[str, Any]) -> None:
            for key, incoming in (("astrbot", astrbot_body), ("weflow", weflow_body)):
                target = self._mapping(raw, key)
                allowed = (
                    {"enabled", "url", "heartbeat", "reconnect"}
                    if key == "astrbot"
                    else {"enabled", "url", "reconnect", "connect_timeout", "read_timeout"}
                )
                for field in allowed:
                    if field in incoming:
                        target[field] = incoming[field]
                if bool(incoming.get("clear_token")):
                    target["token"] = ""
                elif str(incoming.get("token") or ""):
                    target["token"] = str(incoming["token"])

        previous = self.service.config
        updated = self._persist(mutate)
        payload = self._connections_payload()
        for name in ("astrbot", "weflow"):
            before = getattr(previous, name)
            after = getattr(updated, name)
            changed = before != after
            item = payload[name]
            item["reconnect_requested"] = bool(changed and item.get("enabled"))
            if item["reconnect_requested"] and bool(self.service.status().get("running")):
                item["state"] = "reconnecting"
                item["last_error"] = ""
        return {"ok": True, "restart_required": False, **payload}

    def _save_connection(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        if name not in {"astrbot", "weflow"}:
            raise ProtocolError("invalid_connection", "连接名称无效。")
        if not isinstance(body, dict):
            raise ProtocolError("invalid_connection", "连接配置必须是 JSON 对象。")

        def mutate(raw: dict[str, Any]) -> None:
            target = self._mapping(raw, name)
            allowed = (
                {"url", "heartbeat", "reconnect"}
                if name == "astrbot"
                else {"url", "reconnect", "connect_timeout", "read_timeout"}
            )
            for field in allowed:
                if field in body:
                    target[field] = body[field]
            if bool(body.get("clear_token")):
                target["token"] = ""
            elif str(body.get("token") or ""):
                target["token"] = str(body["token"])

        previous = getattr(self.service.config, name)
        updated = self._persist(mutate)
        changed = previous != getattr(updated, name)
        return {
            "ok": True,
            "connection": name,
            "restart_required": False,
            **self._connection_change_payload(name, changed=changed),
        }

    def _toggle_connection(self, body: dict[str, Any]) -> dict[str, Any]:
        name = str(body.get("connection") or "").strip().lower()
        enabled = body.get("enabled")
        if name not in {"astrbot", "weflow"}:
            raise ProtocolError("invalid_connection", "连接名称无效。")
        if not isinstance(enabled, bool):
            raise ProtocolError("invalid_connection_state", "enabled 必须是布尔值。")

        def mutate(raw: dict[str, Any]) -> None:
            self._mapping(raw, name)["enabled"] = enabled

        previous = getattr(self.service.config, name)
        updated = self._persist(mutate)
        changed = previous != getattr(updated, name)
        return {
            "ok": True,
            "connection": name,
            "enabled": enabled,
            **self._connection_change_payload(name, changed=changed),
        }

    def _active_api_payload(self) -> dict[str, Any]:
        config = getattr(self.service, "config", None)
        if config is None:
            raise ProtocolError("active_api_unavailable", "主动发送 API 配置不可用。")
        active_api = config.active_api
        scheme = "https" if config.console.tls_cert else "http"
        host = config.console.host
        if host in {"0.0.0.0", "::", ""}:
            host = "127.0.0.1"
        return {
            "enabled": bool(active_api.enabled),
            "token_configured": bool(active_api.token),
            "endpoint": f"{scheme}://{host}:{config.console.port}/api/v1/messages",
            "status_endpoint": f"{scheme}://{host}:{config.console.port}/api/v1/messages/{{task_id}}",
            "local_only_without_token": not bool(active_api.token),
            "bridge_required": False,
            "automation_required": True,
            "automation_enabled": bool(self.service.status()["automation_enabled"]),
            "description": "Agent 主动提交文字、图片或文件；不要求启动 WeFlow/AstrBot 桥接，但微信自动化必须开启。提交后轮询任务状态，request_id 用于防止网络重试造成重复发送。",
        }

    def _save_active_api(self, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise ProtocolError("invalid_active_api", "主动发送 API 配置必须是 JSON 对象。")
        enabled = body.get("enabled", self.service.config.active_api.enabled)
        if not isinstance(enabled, bool):
            raise ProtocolError("invalid_active_api", "enabled 必须是布尔值。")

        def mutate(raw: dict[str, Any]) -> None:
            target = self._mapping(raw, "active_api")
            target["enabled"] = enabled
            if bool(body.get("clear_token")):
                target["token"] = ""
            elif str(body.get("token") or ""):
                target["token"] = str(body["token"])

        self._persist(mutate)
        return {"ok": True, **self._active_api_payload()}

    def _basic_payload(self) -> dict[str, Any]:
        config = getattr(self.service, "config", None)
        if config is None:
            raise ProtocolError("settings_unavailable", "服务未公开基础配置。")
        return {
            "self_id": config.self_id,
            "bot_names": list(config.bot_names),
            "bot_wxid": config.bot_wxid,
            "group_trigger": config.group_trigger,
            "console": {
                "host": config.console.host,
                "port": config.console.port,
                "username": config.console.username,
                "allow_lan": config.console.allow_lan,
                "allow_insecure_lan": config.console.allow_insecure_lan,
                "auto_open_browser": config.console.auto_open_browser,
            },
            "media": {
                "temp_dir": config.media.temp_dir,
                "max_image_bytes": config.media.max_image_bytes,
                "max_file_bytes": config.media.max_file_bytes,
            },
            "logging": {
                "directory": config.logging.directory,
                "level": config.logging.level,
            },
        }

    def _save_basic(self, body: dict[str, Any]) -> dict[str, Any]:
        old = getattr(self.service, "config", None)
        if old is None:
            raise ProtocolError("settings_unavailable", "服务未公开基础配置。")
        bot_names = body.get("bot_names", [])
        if not isinstance(bot_names, list):
            raise ProtocolError("invalid_settings", "bot_names 必须是数组。")
        try:
            self_id = int(body.get("self_id", old.self_id))
        except (TypeError, ValueError) as exc:
            raise ProtocolError(
                "invalid_settings",
                "OneBot 机器人 ID 必须是正整数。",
            ) from exc
        console_body = body.get("console", {})
        media_body = body.get("media", {})
        logging_body = body.get("logging", {})
        if not all(isinstance(x, dict) for x in (console_body, media_body, logging_body)):
            raise ProtocolError("invalid_settings", "基础设置分组必须是 JSON 对象。")

        def mutate(raw: dict[str, Any]) -> None:
            raw["self_id"] = self_id
            raw["bot_names"] = [str(item).strip() for item in bot_names if str(item).strip()]
            raw["bot_wxid"] = str(body.get("bot_wxid") or "").strip()
            raw["group_trigger"] = str(body.get("group_trigger") or "all").strip()
            console = self._mapping(raw, "console")
            for key in ("host", "port", "allow_lan", "allow_insecure_lan", "auto_open_browser"):
                if key in console_body:
                    console[key] = console_body[key]
            media = self._mapping(raw, "media")
            # v3 media sending is a core capability, not an optional feature.
            # Drop the old opt-out field whenever basic settings are saved.
            media.pop("enabled", None)
            for key in ("temp_dir", "max_image_bytes", "max_file_bytes"):
                if key in media_body:
                    media[key] = media_body[key]
            logging_config = self._mapping(raw, "logging")
            for key in ("level", "directory"):
                if key in logging_body:
                    logging_config[key] = logging_body[key]

        updated = self._persist(mutate)
        restart_required = (
            old.console.host != updated.console.host
            or old.console.port != updated.console.port
            or old.console.tls_cert != updated.console.tls_cert
            or old.console.tls_key != updated.console.tls_key
            or old.logging != updated.logging
        )
        return {"ok": True, "restart_required": restart_required, **self._basic_payload()}

    def _save_sender_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        sender = body.get("sender")
        if not isinstance(sender, dict):
            raise ProtocolError("invalid_settings", "sender 必须是 JSON 对象。")
        profile = str(
            body.get("profile") or self._sender_profiles_payload()["active_profile"]
        ).strip().lower()
        if profile not in SENDER_PROFILE_NAMES:
            raise ProtocolError("invalid_sender_profile", "发送配置名称无效。")
        if profile not in EDITABLE_PROFILE_NAMES:
            raise ProtocolError(
                "readonly_sender_profile",
                "均衡、稳妥、快速是只读模板；请先复制到自定义配置后再修改。",
            )
        if sender.get("append_line_break_after_input") is True:
            automation = getattr(getattr(self.service, "config", None), "automation", None)
            if not bool(getattr(automation, "wechat_ctrl_enter_confirmed", False)):
                raise ProtocolError(
                    "ctrl_enter_confirmation_required",
                    "开启“输入结束后按 Enter 换行”前，请先确认微信的发送消息快捷键已设为 Ctrl+Enter。",
                )
        if self.config_path is None:
            candidate = dict(self._runtime_sender_profiles[profile])
            candidate.update(sender)
            if profile == self._runtime_active_sender_profile:
                self.service.update_runtime_settings({"sender": candidate})
            self._runtime_sender_profiles[profile] = candidate
            result = self._sender_profiles_payload()
            result["saved_profile"] = profile
            return result

        def mutate(raw: dict[str, Any]) -> None:
            active, profiles = sender_profile_state(raw)
            target = profiles[profile]
            target.update(sender)
            raw["active_sender_profile"] = active
            raw["sender_profiles"] = profiles
            raw["sender"] = profiles[active]

        self._persist(mutate)
        result = self._sender_profiles_payload()
        result["persistence"] = "config_file"
        result["restart_behavior"] = "saved_and_applied_if_active"
        result["saved_profile"] = profile
        return result

    def _sender_profiles_payload(self) -> dict[str, Any]:
        if self.config_path is None:
            active = self._runtime_active_sender_profile
            profiles = self._runtime_sender_profiles
        else:
            with self._config_lock:
                try:
                    raw = json.loads(self.config_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ProtocolError("settings_unavailable", str(exc)) from exc
                active, profiles = sender_profile_state(raw)
        service_config = getattr(self.service, "config", None)
        media_config = getattr(service_config, "media", None)
        return {
            "persistence": "config_file" if self.config_path is not None else "runtime_only",
            "active_profile": active,
            "profiles": profiles,
            "profile_meta": {
                name: {
                    "editable": name in EDITABLE_PROFILE_NAMES,
                    "template": name in TEMPLATE_PROFILE_NAMES,
                }
                for name in SENDER_PROFILE_NAMES
            },
            "sender": dict(profiles[active]),
            "media": {
                "max_image_bytes": int(
                    getattr(media_config, "max_image_bytes", 0) or 0
                ),
                "max_file_bytes": int(
                    getattr(media_config, "max_file_bytes", 0) or 0
                ),
            },
        }

    def _reset_sender_profile(self, body: dict[str, Any]) -> dict[str, Any]:
        """Reset the editable custom profile to the balanced baseline."""

        profile = str(body.get("profile") or "").strip().lower()
        if profile != "custom":
            raise ProtocolError(
                "readonly_sender_profile",
                "均衡、稳妥、快速始终使用内置模板，不需要也不允许重置。",
            )

        templates = default_sender_profiles()
        replacement = templates["balanced"]

        def mutate(raw: dict[str, Any]) -> None:
            active, profiles = sender_profile_state(raw)
            profiles[profile] = copy.deepcopy(replacement)
            raw["sender_profiles"] = profiles
            raw["active_sender_profile"] = active
            raw["sender"] = profiles[active]

        if self.config_path is None:
            self._runtime_sender_profiles[profile] = copy.deepcopy(replacement)
            if profile == self._runtime_active_sender_profile:
                self.service.update_runtime_settings({"sender": replacement})
            result = self._sender_profiles_payload()
        else:
            self._persist(mutate)
            result = self._sender_profiles_payload()
        result["reset_profile"] = profile
        return result

    def _automation_payload(self) -> dict[str, Any]:
        config = getattr(self.service, "config", None)
        if config is None:
            raise ProtocolError("settings_unavailable", "服务未公开自动化配置。")
        status = self.service.status()
        return {
            "stop_hotkey_enabled": bool(config.automation.stop_hotkey_enabled),
            "stop_hotkey": config.automation.stop_hotkey,
            "tray_activation_enabled": bool(
                config.automation.tray_activation_enabled
            ),
            "tray_activation_timeout": float(
                config.automation.tray_activation_timeout
            ),
            "dpi_scale_mode": config.automation.dpi_scale_mode,
            "dpi_scale_percent": int(config.automation.dpi_scale_percent),
            "dpi_auto_min_percent": int(
                config.automation.dpi_auto_min_percent
            ),
            "dpi_auto_max_percent": int(
                config.automation.dpi_auto_max_percent
            ),
            "dpi_auto_step_percent": int(
                config.automation.dpi_auto_step_percent
            ),
            "window_position_enabled": bool(
                config.automation.window_position_enabled
            ),
            "window_x": int(config.automation.window_x),
            "window_y": int(config.automation.window_y),
            "window_size_enabled": bool(config.automation.window_size_enabled),
            "window_width": int(config.automation.window_width),
            "window_height": int(config.automation.window_height),
            "wechat_ctrl_enter_confirmed": bool(
                config.automation.wechat_ctrl_enter_confirmed
            ),
            "automation_enabled": bool(status.get("automation_enabled", True)),
            "automation_active": int(status.get("automation_active", 0) or 0),
        }

    def _qt_accessibility_payload(self) -> dict[str, Any]:
        return {
            "enabled": False,
            "configured_enabled": False,
            "available": False,
            "memory_modified": False,
            "title": "v3 不使用 Qt/UIA 热激活",
            "summary": "v3 只使用截图、Win32 窗口元数据和普通鼠标键盘。",
        }

    def _save_qt_accessibility(self, body: dict[str, Any]) -> dict[str, Any]:
        raise ProtocolError(
            "unsupported_v3_setting",
            "v3 不支持 Qt/UIA 热激活、Hook 或内存修改。",
        )

    def _save_automation(self, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise ProtocolError("invalid_settings", "自动化全局配置必须是 JSON 对象。")
        allowed = {
            "stop_hotkey_enabled",
            "stop_hotkey",
            "tray_activation_enabled",
            "tray_activation_timeout",
            "dpi_scale_mode",
            "dpi_scale_percent",
            "dpi_auto_min_percent",
            "dpi_auto_max_percent",
            "dpi_auto_step_percent",
            "window_position_enabled",
            "window_x",
            "window_y",
            "window_size_enabled",
            "window_width",
            "window_height",
            "wechat_ctrl_enter_confirmed",
        }
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise ProtocolError(
                "unknown_setting",
                "未知自动化全局配置：" + "、".join(unknown),
            )
        changes: dict[str, Any] = {}
        for key in (
            "stop_hotkey_enabled",
            "tray_activation_enabled",
            "window_position_enabled",
            "window_size_enabled",
            "wechat_ctrl_enter_confirmed",
        ):
            if key not in body:
                continue
            if not isinstance(body[key], bool):
                raise ProtocolError("invalid_setting_type", f"{key} 必须是布尔值。")
            changes[key] = body[key]
        for key in (
            "window_x",
            "window_y",
            "window_width",
            "window_height",
            "dpi_scale_percent",
            "dpi_auto_min_percent",
            "dpi_auto_max_percent",
            "dpi_auto_step_percent",
        ):
            if key not in body:
                continue
            value = body[key]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ProtocolError("invalid_setting_type", f"{key} 必须是整数。")
            changes[key] = value
        if "tray_activation_timeout" in body:
            value = body["tray_activation_timeout"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ProtocolError(
                    "invalid_setting_type",
                    "tray_activation_timeout 必须是数字。",
                )
            changes["tray_activation_timeout"] = float(value)
        if "stop_hotkey" in body:
            changes["stop_hotkey"] = str(body.get("stop_hotkey") or "").strip()
        if "dpi_scale_mode" in body:
            mode = str(body.get("dpi_scale_mode") or "").strip().lower()
            if mode not in {"auto", "manual"}:
                raise ProtocolError(
                    "invalid_setting_value",
                    "dpi_scale_mode 只能是 auto 或 manual。",
                )
            changes["dpi_scale_mode"] = mode
        old = getattr(self.service, "config", None)
        if old is None:
            raise ProtocolError("settings_unavailable", "服务未公开自动化配置。")

        def mutate(raw: dict[str, Any]) -> None:
            automation = self._mapping(raw, "automation")
            automation.update(changes)

        try:
            self._persist(mutate)
        except ConfigError as exc:
            raise ProtocolError("invalid_automation_settings", str(exc)) from exc
        return {"ok": True, **self._automation_payload()}

    def _activate_sender_profile(self, body: dict[str, Any]) -> dict[str, Any]:
        profile = str(body.get("profile") or "").strip().lower()
        if profile not in SENDER_PROFILE_NAMES:
            raise ProtocolError("invalid_sender_profile", "发送配置名称无效。")
        if self.config_path is None:
            sender = dict(self._runtime_sender_profiles[profile])
            self.service.update_runtime_settings({"sender": sender})
            self._runtime_active_sender_profile = profile
            result = self._sender_profiles_payload()
            result["activated_profile"] = profile
            return result

        def mutate(raw: dict[str, Any]) -> None:
            _, profiles = sender_profile_state(raw)
            raw["active_sender_profile"] = profile
            raw["sender_profiles"] = profiles
            raw["sender"] = profiles[profile]

        self._persist(mutate)
        result = self._sender_profiles_payload()
        result["activated_profile"] = profile
        return result

    def _handler(self):
        console = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "WeChatBridge/2"

            def _origin_allowed(self) -> bool:
                origin = self.headers.get("Origin")
                if not origin:
                    return True
                if origin in console.config.allowed_origins:
                    return True
                parsed = urlparse(origin)
                return parsed.scheme in {"http", "https"} and parsed.netloc == self.headers.get("Host")

            def _security_headers(self) -> None:
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
                )
                self.send_header("Cache-Control", "no-store")

            def _json(
                self,
                data: Any,
                status: int = 200,
                *,
                headers: dict[str, str] | None = None,
            ) -> None:
                payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self._security_headers()
                origin = self.headers.get("Origin")
                if origin and self._origin_allowed():
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
                self.end_headers()
                self.wfile.write(payload)

            def _png(self, payload: bytes) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(payload)))
                self._security_headers()
                self.end_headers()
                self.wfile.write(payload)

            def _download(
                self,
                payload: bytes,
                *,
                content_type: str,
                filename: str,
            ) -> None:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="{filename}"',
                )
                self.send_header("Content-Length", str(len(payload)))
                self._security_headers()
                self.end_headers()
                self.wfile.write(payload)

            def _bearer_token(self) -> str:
                auth = self.headers.get("Authorization", "")
                return auth[7:] if auth.startswith("Bearer ") else ""

            def _cookie_token(self) -> str:
                cookies = SimpleCookie()
                try:
                    cookies.load(self.headers.get("Cookie", ""))
                except (TypeError, ValueError):
                    return ""
                morsel = cookies.get("bridge_session")
                return str(morsel.value) if morsel is not None else ""

            def _auth_token(self) -> str:
                return self._bearer_token() or self._cookie_token()

            def _authorized(self, *, allow_setup: bool = False) -> bool:
                if not self._origin_allowed():
                    self._json({"ok": False, "error": "Origin 不允许。"}, 403)
                    return False
                if not console._session_valid(self._auth_token()):
                    self._json({"ok": False, "error": "未授权。"}, 401)
                    return False
                if console.config.force_password_change and not allow_setup:
                    self._json(
                        {
                            "ok": False,
                            "error": "首次登录必须先设置新的账号和密码。",
                            "code": "setup_required",
                        },
                        428,
                    )
                    return False
                return True

            def _active_api_authorized(self) -> bool:
                if not self._origin_allowed():
                    self._json({"ok": False, "error": "Origin 不允许。"}, 403)
                    return False
                active_api = getattr(console.service.config, "active_api", None)
                if active_api is None or not active_api.enabled:
                    self._json(
                        {"ok": False, "error": "主动发送 API 尚未启用。", "code": "active_api_disabled"},
                        403,
                    )
                    return False
                supplied = self._bearer_token()
                configured = str(active_api.token or "")
                if configured:
                    if not supplied or not hmac.compare_digest(supplied, configured):
                        self._json({"ok": False, "error": "主动发送 API Token 无效。", "code": "active_api_unauthorized"}, 401)
                        return False
                    return True
                try:
                    local = ipaddress.ip_address(self._client_ip()).is_loopback
                except ValueError:
                    local = False
                if not local:
                    self._json(
                        {"ok": False, "error": "未配置 API Token 时只允许本机回环地址调用。", "code": "active_api_local_only"},
                        401,
                    )
                    return False
                if supplied:
                    self._json({"ok": False, "error": "当前 API 未配置 Token，请不要发送 Bearer 凭据。", "code": "active_api_unauthorized"}, 401)
                    return False
                return True

            def _client_ip(self) -> str:
                return str(self.client_address[0]) if self.client_address else "unknown"

            def _body(self) -> dict[str, Any]:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError as exc:
                    raise ProtocolError("invalid_length", "Content-Length 无效。") from exc
                if length < 0 or length > MAX_BODY_BYTES:
                    raise ProtocolError("body_too_large", "请求体超过 64 KiB。")
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    data = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ProtocolError("invalid_json", "请求 JSON 无效。") from exc
                if not isinstance(data, dict):
                    raise ProtocolError("invalid_json", "请求 JSON 必须是对象。")
                return data

            def do_OPTIONS(self) -> None:
                if not self._origin_allowed():
                    self._json({"ok": False, "error": "Origin 不允许。"}, 403)
                    return
                self.send_response(204)
                origin = self.headers.get("Origin")
                if origin:
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self._security_headers()
                self.end_headers()

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                path = parsed.path
                if path == "/healthz":
                    self._json({"ok": True})
                    return
                if path == "/":
                    authenticated = (
                        console._session_valid(self._auth_token())
                        and not console.config.force_password_change
                    )
                    page = PAGE if authenticated else LOGIN_PAGE
                    payload = page.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self._security_headers()
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if path == "/api/auth-state":
                    authenticated = console._session_valid(self._auth_token())
                    result = {
                        "ok": True,
                        "authenticated": authenticated,
                        "setup_required": bool(
                            authenticated and console.config.force_password_change
                        ),
                    }
                    if authenticated:
                        result["username"] = console.config.username
                    self._json(result)
                    return
                if path.startswith("/api/v1/messages/"):
                    if not self._active_api_authorized():
                        return
                    task_id = path.rstrip("/").rsplit("/", 1)[-1]
                    try:
                        self._json(console.service.active_api_task(task_id))
                    except ProtocolError as exc:
                        self._json({"ok": False, "error": exc.message, "code": exc.code}, 404)
                    return
                allow_setup = path == "/api/account"
                if not self._authorized(allow_setup=allow_setup):
                    return
                try:
                    if path == "/api/status":
                        result = console.service.status()
                        result["account"] = console._account_payload()
                        self._json(result)
                    elif path == "/api/account":
                        self._json(console._account_payload())
                    elif path == "/api/settings":
                        self._json(console._sender_profiles_payload())
                    elif path == "/api/automation-settings":
                        self._json(console._automation_payload())
                    elif path == "/api/qt-accessibility":
                        self._json(console._qt_accessibility_payload())
                    elif path == "/api/connections":
                        self._json(console._connections_payload())
                    elif path == "/api/active-api":
                        self._json(console._active_api_payload())
                    elif path == "/api/basic-settings":
                        self._json(console._basic_payload())
                    elif path == "/api/visual-compatibility":
                        self._json(console.service.visual_compatibility_status())
                    elif path == "/api/recognition-repair":
                        self._json(console.service.recognition_repair_status())
                    elif path == "/api/logs":
                        query = parse_qs(parsed.query)
                        kind = str((query.get("kind") or ["bridge"])[0])
                        try:
                            lines = int((query.get("lines") or ["200"])[0])
                        except ValueError:
                            lines = 200
                        self._json(console.service.read_logs(kind, lines))
                    elif path == "/api/recognition-snapshots":
                        query = parse_qs(parsed.query)
                        snapshots = console.service.recognition_snapshots(
                            outcome=str((query.get("outcome") or [""])[0]),
                            source=str((query.get("source") or [""])[0]),
                            run_id=str((query.get("run_id") or [""])[0]),
                            limit=(query.get("limit") or ["80"])[0],
                        )
                        self._json({"ok": True, "snapshots": snapshots})
                    elif path.startswith("/api/recognition-snapshots/"):
                        parts = path.strip("/").split("/")
                        if len(parts) == 4 and parts[3] == "diagnostic":
                            payload = console.service.recognition_snapshot_diagnostic(
                                parts[2]
                            )
                            self._download(
                                payload,
                                content_type="application/zip",
                                filename=f"wechat-visual-diagnostic-{parts[2]}.zip",
                            )
                            return
                        if len(parts) != 5 or parts[3] != "images":
                            self._json({"ok": False, "error": "Not found"}, 404)
                            return
                        image_path = console.service.recognition_snapshot_image(
                            parts[2],
                            parts[4],
                        )
                        self._png(image_path.read_bytes())
                    elif path.startswith("/api/debug-tasks/"):
                        task_id = path.rsplit("/", 1)[-1]
                        self._json(console.service.debug_task(task_id))
                    else:
                        self._json({"ok": False, "error": "Not found"}, 404)
                except ProtocolError as exc:
                    payload = {"ok": False, "error": exc.message, "code": exc.code}
                    if exc.details:
                        payload["details"] = exc.details
                    self._json(payload, 400)
                except Exception:
                    log.exception("控制台 GET 请求处理失败。")
                    self._json({"ok": False, "error": "内部错误。"}, 500)

            def do_POST(self) -> None:
                if not self._origin_allowed():
                    self._json({"ok": False, "error": "Origin 不允许。"}, 403)
                    return
                try:
                    if self.path == "/api/v1/messages":
                        if not self._active_api_authorized():
                            return
                        self._json(
                            console.service.start_active_api_send(self._body()),
                            202,
                        )
                        return
                    if self.path.startswith("/api/v1/messages/") and self.path.endswith("/cancel"):
                        if not self._active_api_authorized():
                            return
                        task_id = self.path.rstrip("/").rsplit("/", 1)[-2]
                        self._json(console.service.cancel_active_api_task(task_id))
                        return
                    if self.path == "/api/visual-compatibility/import":
                        if not self._authorized():
                            return
                        try:
                            length = int(self.headers.get("Content-Length", "0"))
                        except ValueError as exc:
                            raise ProtocolError(
                                "invalid_length",
                                "Content-Length 无效。",
                            ) from exc
                        encoded_name = str(self.headers.get("X-Screenshot-Name") or "")
                        if len(encoded_name) > 2048:
                            raise ProtocolError(
                                "invalid_filename",
                                "截图文件名过长。",
                            )
                        filename = unquote(
                            encoded_name,
                            encoding="utf-8",
                            errors="replace",
                        )
                        validated = console.service.validate_visual_screenshot_import(
                            filename=filename,
                            content_length=length,
                            scale_percent=self.headers.get("X-Screenshot-Scale"),
                            input_source=self.headers.get("X-Screenshot-Source"),
                        )
                        self.connection.settimeout(60.0)
                        payload = bytearray()
                        remaining = int(validated["size"])
                        while remaining:
                            chunk = self.rfile.read(min(1024 * 1024, remaining))
                            if not chunk:
                                break
                            payload.extend(chunk)
                            remaining -= len(chunk)
                        if remaining:
                            raise ProtocolError(
                                "incomplete_screenshot_upload",
                                "截图上传不完整，请重新导入。",
                            )
                        result = console.service.run_imported_visual_compatibility_check(
                            bytes(payload),
                            filename=str(validated["name"]),
                            scale_percent=validated["scale_percent"],
                            input_source=validated["input_source"],
                        )
                        self._json(result, 201)
                        return
                    if self.path == "/api/debug-upload":
                        if not self._authorized():
                            return
                        try:
                            length = int(self.headers.get("Content-Length", "0"))
                        except ValueError as exc:
                            raise ProtocolError(
                                "invalid_length",
                                "Content-Length 无效。",
                            ) from exc
                        media_type = str(self.headers.get("X-Media-Type") or "")
                        encoded_name = str(self.headers.get("X-File-Name") or "")
                        if len(encoded_name) > 2048:
                            raise ProtocolError("invalid_filename", "媒体文件名过长。")
                        filename = unquote(encoded_name, encoding="utf-8", errors="replace")
                        upload = console.service.begin_debug_upload(
                            media_type,
                            filename,
                            length,
                        )
                        written = 0
                        try:
                            self.connection.settimeout(60.0)
                            with open(upload["temporary_path"], "xb") as stream:
                                remaining = length
                                while remaining:
                                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                                    if not chunk:
                                        break
                                    stream.write(chunk)
                                    written += len(chunk)
                                    remaining -= len(chunk)
                            result = console.service.finish_debug_upload(
                                upload["id"],
                                written,
                            )
                        except Exception:
                            console.service.cancel_debug_upload(upload["id"])
                            raise
                        self._json({"ok": True, **result}, 201)
                        return
                    body = self._body()
                    if self.path == "/api/login":
                        try:
                            token, expires_at = console._login(
                                self._client_ip(),
                                str(body.get("username") or ""),
                                str(body.get("password") or ""),
                            )
                        except ProtocolError as exc:
                            self._json(
                                {"ok": False, "error": exc.message, "code": exc.code},
                                429 if exc.code == "login_rate_limited" else 401,
                            )
                            return
                        self._json(
                            {
                                "ok": True,
                                "token": token,
                                "expires_at": expires_at,
                                "token_type": "Bearer",
                                "username": console.config.username,
                                "setup_required": console.config.force_password_change,
                            },
                            headers={
                                "Set-Cookie": (
                                    "bridge_session="
                                    + token
                                    + f"; Max-Age={max(1, int(console.config.session_ttl_seconds))}"
                                    + "; HttpOnly; Path=/; SameSite=Strict"
                                    + ("; Secure" if console.config.tls_cert else "")
                                )
                            },
                        )
                        return
                    allow_setup = self.path in {"/api/account", "/api/logout"}
                    if not self._authorized(allow_setup=allow_setup):
                        return
                    if self.path == "/api/logout":
                        console._logout(self._auth_token())
                        self._json(
                            {"ok": True},
                            headers={
                                "Set-Cookie": "bridge_session=; Max-Age=0; HttpOnly; Path=/; SameSite=Strict"
                            },
                        )
                    elif self.path == "/api/account":
                        self._json(console._change_account(body))
                    elif self.path == "/api/control":
                        action = str(body.get("action") or "")
                        methods = {
                            "start": console.service.start,
                            "stop": console.service.stop,
                        }
                        method = methods.get(action)
                        if method is None:
                            raise ProtocolError("invalid_action", "未知控制动作。")
                        method()
                        self._json({"ok": True, "action": action})
                    elif self.path == "/api/automation/control":
                        action = str(body.get("action") or "").strip().lower()
                        if action == "start":
                            console.service.start_automation()
                        elif action == "stop":
                            console.service.stop_automation()
                        else:
                            raise ProtocolError("invalid_action", "未知自动化控制动作。")
                        self._json(
                            {
                                "ok": True,
                                "action": action,
                                **console.service.status(),
                            }
                        )
                    elif self.path == "/api/automation/window-geometry":
                        self._json(
                            {
                                "ok": True,
                                **console.service.read_wechat_window_geometry(),
                            }
                        )
                    elif self.path == "/api/automation/dpi-detect":
                        self._json(
                            {
                                "ok": True,
                                **console.service.read_wechat_window_dpi(),
                            }
                        )
                    elif self.path == "/api/visual-compatibility/check":
                        self._json(console.service.run_visual_compatibility_check())
                    elif self.path == "/api/recognition-repair/validate":
                        self._json(console.service.validate_recognition_repair(body))
                    elif self.path == "/api/recognition-repair/candidates":
                        self._json(
                            console.service.reload_recognition_repair_candidates(body)
                        )
                    elif self.path == "/api/recognition-repair/save":
                        self._json(console.service.save_recognition_repair(body))
                    elif self.path == "/api/recognition-repair/disable":
                        self._json(
                            console.service.disable_recognition_repair(
                                str(body.get("target_id") or "")
                            )
                        )
                    elif self.path == "/api/automation/tray-test":
                        result = console.service.test_wechat_tray_activation()
                        self._json(result, 200 if result.get("ok") else 409)
                    elif self.path == "/api/send":
                        result = console.service.manual_send(
                            str(body.get("kind") or ""),
                            str(body.get("name") or ""),
                            str(body.get("text") or ""),
                        )
                        self._json(result, 200 if result.get("ok") else 409)
                    elif self.path == "/api/settings":
                        result = console._save_sender_settings(body)
                        self._json({"ok": True, **result})
                    elif self.path == "/api/settings/reset":
                        self._json({"ok": True, **console._reset_sender_profile(body)})
                    elif self.path == "/api/automation-settings":
                        self._json(console._save_automation(body))
                    elif self.path == "/api/qt-accessibility":
                        self._json(console._save_qt_accessibility(body))
                    elif self.path == "/api/settings/activate":
                        self._json({"ok": True, **console._activate_sender_profile(body)})
                    elif self.path == "/api/connections":
                        self._json(console._save_connections(body))
                    elif self.path in {
                        "/api/connections/astrbot",
                        "/api/connections/weflow",
                    }:
                        self._json(
                            console._save_connection(
                                self.path.rsplit("/", 1)[-1],
                                body,
                            )
                        )
                    elif self.path == "/api/connections/toggle":
                        self._json(console._toggle_connection(body))
                    elif self.path == "/api/active-api":
                        self._json(console._save_active_api(body))
                    elif self.path == "/api/basic-settings":
                        self._json(console._save_basic(body))
                    elif self.path == "/api/debug-upload-check":
                        result = console.service.validate_debug_upload(
                            str(body.get("media_type") or ""),
                            str(body.get("filename") or ""),
                            body.get("size"),
                        )
                        self._json({"ok": True, **result})
                    elif self.path == "/api/debug-send":
                        result = console.service.start_debug_send(
                            str(body.get("kind") or ""),
                            str(body.get("name") or ""),
                            str(body.get("message_type") or ""),
                            text=str(body.get("text") or ""),
                            path=str(body.get("path") or ""),
                            upload_id=str(body.get("upload_id") or ""),
                        )
                        self._json({"ok": True, **result}, 202)
                    elif self.path.startswith("/api/debug-tasks/") and self.path.endswith("/cancel"):
                        task_id = self.path.split("/")[-2]
                        self._json({"ok": True, **console.service.cancel_debug_task(task_id)})
                    elif self.path == "/api/debug-upload-cancel":
                        console.service.cancel_debug_upload(
                            str(body.get("upload_id") or "")
                        )
                        self._json({"ok": True})
                    else:
                        self._json({"ok": False, "error": "Not found"}, 404)
                except (ProtocolError, ConfigError) as exc:
                    code = getattr(exc, "code", "invalid_config")
                    message = getattr(exc, "message", str(exc))
                    if self.path.startswith("/api/v1/messages"):
                        status = (
                            409
                            if code in {
                                "request_id_conflict",
                                "active_api_queue_full",
                                "service_stopped",
                                "automation_stopped",
                            }
                            else 400
                        )
                    else:
                        status = 400
                    payload = {"ok": False, "error": message, "code": code}
                    details = getattr(exc, "details", None)
                    if details:
                        payload["details"] = details
                    self._json(payload, status)
                except Exception:
                    log.exception("控制台 POST 请求处理失败。")
                    self._json({"ok": False, "error": "内部错误。"}, 500)

            def log_message(self, fmt: str, *args: Any) -> None:
                log.debug("HTTP %s", fmt % args)

        return Handler

    def start(self) -> None:
        if self.server is not None:
            return
        try:
            server = ExclusiveThreadingHTTPServer(
                (self.config.host, self.config.port),
                self._handler(),
            )
        except OSError as exc:
            raise OSError(
                exc.errno,
                (
                    f"控制台端口 {self.config.host}:{self.config.port} 已被占用；"
                    "请先关闭旧的微信桥接控制台，再重新启动。"
                ),
            ) from exc
        server.daemon_threads = True
        if self.config.tls_cert and self.config.tls_key:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(self.config.tls_cert, self.config.tls_key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
        self.server = server
        self.thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="bridge-console",
            daemon=True,
        )
        self.thread.start()
        scheme = "https" if self.config.tls_cert else "http"
        actual_port = server.server_address[1]
        log.info("控制台已启动: %s://%s:%s", scheme, self.config.host, actual_port)

    def stop(self) -> None:
        server = self.server
        self.server = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=3.0)
        self.thread = None
