"""Configuration and LAN security validation for the v3 bridge."""

from __future__ import annotations

import ipaddress
import copy
import json
import os
import re
import secrets
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.parse import parse_qsl

from bridge_security import (
    hash_password,
    is_password_hash,
    protect_local_secret,
    unprotect_local_secret,
)
from wechat_qt_accessibility import QT_HOT_ACTIVATION_NOTICE_VERSION


class ConfigError(ValueError):
    pass


SENDER_PROFILE_NAMES = ("balanced", "cautious", "fast", "custom")
TEMPLATE_PROFILE_NAMES = ("balanced", "cautious", "fast")
EDITABLE_PROFILE_NAMES = ("custom",)
DEFAULT_SENDER_SETTINGS: dict[str, Any] = {
    "timeout": 8,
    "settle": 0.35,
    "conversation_entry_mode": "keyboard_shortcut",
    "conversation_enter_delay_min": 0.20,
    "conversation_enter_delay_max": 0.50,
    "text_verification_timeout": 0,
    "media_verification_mode": "none",
    "soft_protection": True,
    "lock_mouse": True,
    "lock_keyboard": False,
    "min_reply_delay": 3,
    "click_before_delay_min": 0.10,
    "click_before_delay_max": 0.25,
    "click_hold_duration_min": 0.04,
    "click_hold_duration_max": 0.08,
    "auto_launch_wechat": False,
    "wechat_executable": "",
    "launch_timeout": 30,
    "adaptive_layout": True,
    "reuse_open_chat": True,
    "layout_cache": True,
    "mention_mode": "real",
    "mention_candidate_timeout": 2,
    "mention_after_at_delay_min": 0.12,
    "mention_after_at_delay_max": 0.32,
    "mention_min_wait": 0.25,
    "mention_before_enter_delay_min": 0.10,
    "mention_before_enter_delay_max": 0.28,
    "mention_confirm_timeout": 0.8,
    "mention_fallback_enabled": True,
    "file_launch_fallback": False,
    "render_mask_recovery": False,
    "mask_retry_count": 0,
    "mask_wait": 0,
    "retry_max_attempts": 4,
    "retry_delays": [2, 5, 5],
    "overall_timeout": 120,
    "input_mode": "clipboard",
    "append_line_break_after_input": False,
    "character_delay": 0.03,
    "character_delay_min": 0.025,
    "character_delay_max": 0.07,
    "paste_enabled": True,
    "verification_enabled": False,
}


def default_sender_profiles(
    custom_sender: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return the three built-in templates and the editable custom profile."""

    balanced = copy.deepcopy(DEFAULT_SENDER_SETTINGS)
    cautious = {
        **copy.deepcopy(balanced),
        "settle": 1,
        "min_reply_delay": 5,
        "click_before_delay_min": 0.18,
        "click_before_delay_max": 0.42,
        "click_hold_duration_min": 0.06,
        "click_hold_duration_max": 0.12,
        "input_mode": "keyboard",
        "character_delay": 0.08,
        "character_delay_min": 0.06,
        "character_delay_max": 0.14,
        "paste_enabled": False,
        "retry_max_attempts": 3,
        "retry_delays": [5, 10],
        "overall_timeout": 180,
        "text_verification_timeout": 5,
        "mention_after_at_delay_min": 0.20,
        "mention_after_at_delay_max": 0.45,
        "mention_min_wait": 0.40,
        "mention_before_enter_delay_min": 0.18,
        "mention_before_enter_delay_max": 0.42,
        "mention_confirm_timeout": 1.2,
    }
    fast = {
        **copy.deepcopy(balanced),
        "settle": 0.2,
        "min_reply_delay": 0,
        "click_before_delay_min": 0.04,
        "click_before_delay_max": 0.10,
        "click_hold_duration_min": 0.02,
        "click_hold_duration_max": 0.05,
        "input_mode": "clipboard",
        "character_delay": 0.015,
        "character_delay_min": 0.01,
        "character_delay_max": 0.03,
        "paste_enabled": True,
        "retry_max_attempts": 2,
        "retry_delays": [1],
        "overall_timeout": 60,
        "text_verification_timeout": 2,
        "mention_after_at_delay_min": 0.08,
        "mention_after_at_delay_max": 0.18,
        "mention_min_wait": 0.18,
        "mention_before_enter_delay_min": 0.06,
        "mention_before_enter_delay_max": 0.16,
        "mention_confirm_timeout": 0.6,
    }
    custom = copy.deepcopy(balanced)
    if custom_sender:
        custom.update(copy.deepcopy(custom_sender))
        if "character_delay" in custom_sender:
            # A legacy single sender block has no range fields.  ``custom``
            # already inherited the balanced range, so setdefault() would
            # silently keep that unrelated range instead of migrating the
            # user's saved scalar value.
            if "character_delay_min" not in custom_sender:
                custom["character_delay_min"] = custom_sender["character_delay"]
            if "character_delay_max" not in custom_sender:
                custom["character_delay_max"] = custom_sender["character_delay"]
    return {
        "balanced": balanced,
        "cautious": cautious,
        "fast": fast,
        "custom": custom,
    }


def sender_profile_state(
    raw: dict[str, Any],
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Read profile state while migrating a legacy single sender config in memory."""

    legacy = _mapping(raw.get("sender"), "sender")
    configured = _mapping(raw.get("sender_profiles"), "sender_profiles")
    profiles = default_sender_profiles(legacy)
    for name, values in configured.items():
        if name not in SENDER_PROFILE_NAMES:
            raise ConfigError(f"未知发送配置: {name}")
        incoming = _mapping(values, f"sender_profiles.{name}")
        # Built-in templates are definitions, not user-owned saved profiles.
        # Older development builds allowed them to drift in the config file;
        # ignore those legacy values and retain only the editable custom data.
        if name != "custom":
            continue
        profiles[name].update(copy.deepcopy(incoming))
        if "character_delay" in incoming:
            if "character_delay_min" not in incoming:
                profiles[name]["character_delay_min"] = incoming["character_delay"]
            if "character_delay_max" not in incoming:
                profiles[name]["character_delay_max"] = incoming["character_delay"]
    # v2 could persist UIA as the input backend.  v3 deliberately has no UIA
    # runtime, so old configuration files must migrate to the visible keyboard
    # path instead of making the new console fail during startup.
    for values in profiles.values():
        input_mode = str(values.get("input_mode") or "").strip().lower()
        if input_mode == "uia":
            values["input_mode"] = "keyboard"
        elif input_mode == "adaptive":
            values["input_mode"] = "clipboard"
    active_raw = str(raw.get("active_sender_profile") or "").strip().lower()
    if active_raw:
        if active_raw not in SENDER_PROFILE_NAMES:
            raise ConfigError("active_sender_profile 必须是 balanced/cautious/fast/custom。")
        active = active_raw
    elif configured:
        active = "balanced"
    else:
        active = "custom"
        for name in ("balanced", "cautious", "fast"):
            if legacy == profiles[name]:
                active = name
                break
    return active, profiles


def create_default_config(path: str | Path) -> str:
    """Create a usable first-run config and return its one-time console password."""

    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    console_password = secrets.token_urlsafe(16)
    payload = {
        "astrbot": {
            "enabled": False,
            "url": "ws://127.0.0.1:6199/ws",
            "token": "",
            "heartbeat": 20,
            "reconnect": 5,
            "max_message_bytes": 33554432,
        },
        "weflow": {
            "enabled": False,
            "url": "http://127.0.0.1:5031",
            "token": "",
            "reconnect": 5,
            "connect_timeout": 5,
            "read_timeout": 60,
        },
        "active_api": {
            "enabled": False,
            "token": "",
        },
        "active_sender_profile": "balanced",
        "sender_profiles": default_sender_profiles(),
        # Keep this mirror for older tools that only understand one sender block.
        "sender": copy.deepcopy(DEFAULT_SENDER_SETTINGS),
        "automation": {
            "stop_hotkey_enabled": True,
            "stop_hotkey": "Esc",
            "tray_activation_enabled": True,
            "tray_activation_timeout": 3,
            "dpi_scale_mode": "auto",
            "dpi_scale_percent": 100,
            "dpi_auto_min_percent": 70,
            "dpi_auto_max_percent": 150,
            "dpi_auto_step_percent": 5,
            "window_position_enabled": False,
            "window_x": 100,
            "window_y": 80,
            "window_size_enabled": False,
            "window_width": 900,
            "window_height": 700,
            "wechat_ctrl_enter_confirmed": False,
            "qt_hot_activation_enabled": False,
            "qt_hot_activation_notice_accepted": "",
            "qt_hot_activation_start_reminder_disabled": False,
        },
        "media": {
            "temp_dir": ".bridge_media",
            "max_image_bytes": 20971520,
            "max_file_bytes": 104857600,
            "download_timeout": 15,
            "allowed_local_roots": [],
            "allowed_private_hosts": ["localhost", "127.0.0.1", "::1"],
        },
        "console": {
            "host": "127.0.0.1",
            "port": 8765,
            "username": "admin",
            "password_hash": hash_password(console_password),
            "initial_password_protected": protect_local_secret(console_password),
            "session_ttl_seconds": 28800,
            "allow_lan": False,
            "allow_insecure_lan": False,
            "allowed_origins": [],
            "tls_cert": "",
            "tls_key": "",
            "auto_open_browser": True,
            "force_password_change": True,
        },
        "logging": {
            "directory": "logs",
            "level": "INFO",
            "max_bytes": 10485760,
            "backup_count": 5,
        },
        "self_id": 10001,
        "bot_names": [],
        "bot_wxid": "",
        "group_trigger": "all",
        "state_file": "bridge_state.json",
        "security": {"allow_insecure_lan_connections": False},
    }
    temp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(config_path)
    return console_password


def ensure_initial_console_password(path: str | Path) -> tuple[str, str] | None:
    """Return repeatable initial credentials while first setup is unfinished.

    New configs store the initial password encrypted with Windows DPAPI. Older
    unfinished configs that only contain a hash are migrated by generating a
    new initial password and replacing the obsolete hash.
    """

    config_path = Path(path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是 JSON 对象。")
    console = _mapping(raw.get("console"), "console")
    if not bool(console.get("force_password_change", False)):
        return None
    protected = str(console.get("initial_password_protected") or "")
    if protected:
        try:
            password = unprotect_local_secret(protected)
        except (OSError, ValueError):
            # An unfinished config may have been created under a different
            # Windows token (for example elevated vs. normal PowerShell).
            # Rotate only this temporary credential; preserve all other
            # settings and keep first-time setup mandatory.
            protected = ""
        else:
            if not is_password_hash(str(console.get("password_hash") or "")):
                raise ConfigError("控制台密码哈希无效。")
            if protected.startswith("dpapi:"):
                def rewrap(candidate: dict[str, Any]) -> None:
                    candidate_console = _mapping(candidate.get("console"), "console")
                    candidate_console["initial_password_protected"] = (
                        protect_local_secret(password)
                    )

                update_config_file(config_path, rewrap)
            return str(console.get("username") or "admin"), password

    password = secrets.token_urlsafe(16)

    def migrate(candidate: dict[str, Any]) -> None:
        candidate_console = _mapping(candidate.get("console"), "console")
        candidate_console.setdefault("username", "admin")
        candidate_console["password_hash"] = hash_password(password)
        candidate_console["initial_password_protected"] = protect_local_secret(password)
        candidate_console["force_password_change"] = True
        candidate_console.pop("password", None)

    update_config_file(config_path, migrate)
    return str(console.get("username") or "admin"), password


_ENV_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _secret(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    match = _ENV_PATTERN.fullmatch(text)
    if not match:
        return text
    env_name = match.group(1)
    resolved = os.environ.get(env_name)
    if resolved is None:
        raise ConfigError(f"{field_name} 引用的环境变量 {env_name} 未设置。")
    return resolved


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _validate_url(name: str, url: str, schemes: set[str]) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in schemes or not parsed.hostname:
        allowed = "/".join(sorted(schemes))
        raise ConfigError(f"{name} 必须是有效的 {allowed} 地址。")
    if parsed.username or parsed.password:
        raise ConfigError(f"{name} 不允许在 URL 中包含用户名或密码。")
    sensitive_query_keys = {"token", "access_token", "key", "password", "secret"}
    if any(key.lower() in sensitive_query_keys for key, _ in parse_qsl(parsed.query)):
        raise ConfigError(f"{name} 不允许在 URL 查询参数中包含凭据。")


def _insecure_remote(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme in {"http", "ws"}
        and parsed.hostname is not None
        and not _is_loopback_host(parsed.hostname)
    )


@dataclass(frozen=True)
class AstrBotConfig:
    enabled: bool = False
    url: str = "ws://127.0.0.1:6199/ws"
    token: str = ""
    heartbeat: float = 20.0
    reconnect: float = 5.0
    max_message_bytes: int = 32 * 1024 * 1024


@dataclass(frozen=True)
class WeFlowConfig:
    enabled: bool = False
    url: str = "http://127.0.0.1:5031"
    token: str = ""
    reconnect: float = 5.0
    connect_timeout: float = 5.0
    read_timeout: float = 60.0
    max_event_bytes: int = 1024 * 1024


@dataclass(frozen=True)
class ActiveApiConfig:
    """Machine-to-machine API for proactive visible WeChat sends."""

    enabled: bool = False
    token: str = ""


@dataclass(frozen=True)
class SenderConfig:
    timeout: float = 8.0
    settle: float = 0.5
    conversation_entry_mode: str = "keyboard_shortcut"
    conversation_enter_delay_min: float = 0.20
    conversation_enter_delay_max: float = 0.50
    text_verification_timeout: float = 3.0
    media_verification_mode: str = "none"
    soft_protection: bool = True
    lock_mouse: bool = True
    lock_keyboard: bool = False
    min_reply_delay: float = 3.0
    click_before_delay_min: float = 0.10
    click_before_delay_max: float = 0.25
    click_hold_duration_min: float = 0.04
    click_hold_duration_max: float = 0.08
    auto_launch_wechat: bool = True
    wechat_executable: str = ""
    launch_timeout: float = 30.0
    adaptive_layout: bool = True
    reuse_open_chat: bool = True
    layout_cache: bool = True
    mention_mode: str = "real"
    mention_candidate_timeout: float = 2.0
    mention_after_at_delay_min: float = 0.12
    mention_after_at_delay_max: float = 0.32
    mention_min_wait: float = 0.25
    mention_before_enter_delay_min: float = 0.10
    mention_before_enter_delay_max: float = 0.28
    mention_confirm_timeout: float = 0.8
    mention_fallback_enabled: bool = True
    file_launch_fallback: bool = False
    render_mask_recovery: bool = False
    mask_retry_count: int = 0
    mask_wait: float = 0.0
    retry_max_attempts: int = 4
    retry_delays: tuple[float, ...] = (2.0, 5.0, 5.0)
    overall_timeout: float = 120.0
    input_mode: str = "keyboard"
    append_line_break_after_input: bool = False
    character_delay: float = 0.03
    character_delay_min: float = 0.025
    character_delay_max: float = 0.07
    paste_enabled: bool = False
    verification_enabled: bool = False


@dataclass(frozen=True)
class AutomationConfig:
    stop_hotkey_enabled: bool = True
    stop_hotkey: str = "Esc"
    tray_activation_enabled: bool = True
    tray_activation_timeout: float = 3.0
    dpi_scale_mode: str = "auto"
    dpi_scale_percent: int = 100
    dpi_auto_min_percent: int = 70
    dpi_auto_max_percent: int = 150
    dpi_auto_step_percent: int = 5
    window_position_enabled: bool = False
    window_x: int = 100
    window_y: int = 80
    window_size_enabled: bool = False
    window_width: int = 900
    window_height: int = 700
    wechat_ctrl_enter_confirmed: bool = False
    qt_hot_activation_enabled: bool = False
    qt_hot_activation_notice_accepted: str = ""
    qt_hot_activation_start_reminder_disabled: bool = False


@dataclass(frozen=True)
class MediaConfig:
    """Limits and explicitly allowed sources for outbound media."""

    temp_dir: str = ""
    max_image_bytes: int = 20 * 1024 * 1024
    max_file_bytes: int = 100 * 1024 * 1024
    download_timeout: float = 15.0
    allowed_local_roots: tuple[str, ...] = ()
    allowed_private_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "::1")


@dataclass(frozen=True)
class ConsoleConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    username: str = "admin"
    password_hash: str = ""
    session_ttl_seconds: int = 8 * 60 * 60
    allow_lan: bool = False
    allow_insecure_lan: bool = False
    allowed_origins: tuple[str, ...] = ()
    tls_cert: str = ""
    tls_key: str = ""
    auto_open_browser: bool = True
    force_password_change: bool = False


@dataclass(frozen=True)
class LoggingConfig:
    directory: str = "logs"
    level: str = "INFO"
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5


@dataclass(frozen=True)
class BridgeConfig:
    astrbot: AstrBotConfig = field(default_factory=AstrBotConfig)
    weflow: WeFlowConfig = field(default_factory=WeFlowConfig)
    active_api: ActiveApiConfig = field(default_factory=ActiveApiConfig)
    sender: SenderConfig = field(default_factory=SenderConfig)
    automation: AutomationConfig = field(default_factory=AutomationConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
    console: ConsoleConfig = field(default_factory=ConsoleConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    self_id: int = 10001
    bot_names: tuple[str, ...] = ()
    bot_wxid: str = ""
    group_trigger: str = "all"
    state_file: str = "bridge_state.json"
    allow_insecure_lan_connections: bool = False

    def validate(self) -> "BridgeConfig":
        _validate_url("astrbot.url", self.astrbot.url, {"ws", "wss"})
        _validate_url("weflow.url", self.weflow.url, {"http", "https"})
        if self.astrbot.heartbeat < 3 or self.astrbot.reconnect < 1:
            raise ConfigError("AstrBot heartbeat 至少 3 秒，reconnect 至少 1 秒。")
        if self.weflow.reconnect < 1:
            raise ConfigError("WeFlow reconnect 至少 1 秒。")
        if self.sender.timeout <= 0 or self.sender.settle < 0:
            raise ConfigError("sender.timeout 必须大于 0，sender.settle 不能为负数。")
        if self.sender.conversation_entry_mode not in {
            "keyboard_shortcut",
            "mouse_click_unstable",
        }:
            raise ConfigError(
                "sender.conversation_entry_mode 只能是 keyboard_shortcut 或 mouse_click_unstable。"
            )
        if not (
            0
            <= self.sender.conversation_enter_delay_min
            <= self.sender.conversation_enter_delay_max
            <= 10
        ):
            raise ConfigError("按上方向键后等待时间必须设置在 0..10 秒，且最短值不能大于最长值。")
        if not 0 <= self.sender.text_verification_timeout <= 30:
            raise ConfigError(
                "sender.text_verification_timeout must be between 0 and 30 seconds."
            )
        if self.sender.media_verification_mode not in {"none"}:
            raise ConfigError(
                "sender.media_verification_mode currently only supports 'none'."
            )
        if self.sender.mention_mode not in {"real", "plain_text"}:
            raise ConfigError("sender.mention_mode 只能是 real 或 plain_text。")
        if not 0.2 <= self.sender.mention_candidate_timeout <= 30:
            raise ConfigError(
                "sender.mention_candidate_timeout 必须在 0.2..30 秒。"
            )
        if not 0 <= self.sender.mention_after_at_delay_min <= self.sender.mention_after_at_delay_max <= 10:
            raise ConfigError("输入 @ 后的随机等待范围必须在 0..10 秒且最小值不能大于最大值。")
        if not 0 <= self.sender.mention_min_wait <= self.sender.mention_candidate_timeout:
            raise ConfigError("真实 @ 最短响应等待不能超过候选框最长等待。")
        if not (
            0
            <= self.sender.mention_before_enter_delay_min
            <= self.sender.mention_before_enter_delay_max
            <= 10
        ):
            raise ConfigError("选择 @ 候选前的随机等待范围必须在 0..10 秒。")
        if not 0.1 <= self.sender.mention_confirm_timeout <= 10:
            raise ConfigError("真实 @ 选择确认等待必须在 0.1..10 秒。")
        if not isinstance(self.sender.mention_fallback_enabled, bool):
            raise ConfigError("真实 @ 出错自动降级必须是开启或关闭。")
        if not 0 <= self.sender.min_reply_delay <= 120:
            raise ConfigError("sender.min_reply_delay 必须在 0..120 秒。")
        if not (
            0
            <= self.sender.click_before_delay_min
            <= self.sender.click_before_delay_max
            <= 10
        ):
            raise ConfigError("鼠标点击前停顿时间必须设置在 0..10 秒，且最长值不能小于最短值。")
        if not (
            0
            <= self.sender.click_hold_duration_min
            <= self.sender.click_hold_duration_max
            <= 2
        ):
            raise ConfigError("鼠标按住时间必须设置在 0..2 秒，且最长值不能小于最短值。")
        if not 1 <= self.sender.launch_timeout <= 120:
            raise ConfigError("sender.launch_timeout 必须在 1..120 秒。")
        if not 0 <= self.sender.mask_retry_count <= 5:
            raise ConfigError("sender.mask_retry_count must be between 0 and 5.")
        if not 0 <= self.sender.mask_wait <= 10:
            raise ConfigError("sender.mask_wait must be between 0 and 10 seconds.")
        if not 1 <= self.sender.retry_max_attempts <= 10:
            raise ConfigError("sender.retry_max_attempts must be between 1 and 10.")
        if len(self.sender.retry_delays) > 9 or any(
            float(value) < 0 or float(value) > 120
            for value in self.sender.retry_delays
        ):
            raise ConfigError(
                "sender.retry_delays must contain at most 9 values between 0 and 120 seconds."
            )
        if not 1 <= self.sender.overall_timeout <= 600:
            raise ConfigError("sender.overall_timeout must be between 1 and 600 seconds.")
        if self.sender.input_mode not in {"adaptive", "clipboard", "keyboard"}:
            raise ConfigError(
                "sender.input_mode must be adaptive, clipboard, or keyboard."
            )
        if not isinstance(self.sender.append_line_break_after_input, bool):
            raise ConfigError("sender.append_line_break_after_input 必须是布尔值。")
        if not 0 <= self.sender.character_delay <= 2:
            raise ConfigError("sender.character_delay must be between 0 and 2 seconds.")
        if not (
            0
            <= self.sender.character_delay_min
            <= self.sender.character_delay_max
            <= 2
        ):
            raise ConfigError(
                "sender.character_delay_min/max must form a range between 0 and 2 seconds."
            )
        if not isinstance(self.automation.stop_hotkey_enabled, bool):
            raise ConfigError("automation.stop_hotkey_enabled 必须是布尔值。")
        if not isinstance(self.automation.wechat_ctrl_enter_confirmed, bool):
            raise ConfigError("automation.wechat_ctrl_enter_confirmed 必须是布尔值。")
        if (
            self.sender.append_line_break_after_input
            and not self.automation.wechat_ctrl_enter_confirmed
        ):
            raise ConfigError(
                "启用“输入结束后按 Enter 换行”前，必须确认微信的发送消息快捷键已设为 Ctrl+Enter。"
            )
        if not isinstance(self.automation.stop_hotkey, str):
            raise ConfigError("automation.stop_hotkey 必须是字符串。")
        if not isinstance(self.automation.window_position_enabled, bool):
            raise ConfigError("automation.window_position_enabled 必须是布尔值。")
        if not isinstance(self.automation.window_size_enabled, bool):
            raise ConfigError("automation.window_size_enabled 必须是布尔值。")
        if not isinstance(self.automation.tray_activation_enabled, bool):
            raise ConfigError("automation.tray_activation_enabled 必须是布尔值。")
        if not 0.1 <= self.automation.tray_activation_timeout <= 30:
            raise ConfigError("automation.tray_activation_timeout 必须在 0.1..30 秒。")
        if self.automation.dpi_scale_mode not in {"auto", "manual"}:
            raise ConfigError("automation.dpi_scale_mode 只能是 auto 或 manual。")
        if not 50 <= self.automation.dpi_scale_percent <= 300:
            raise ConfigError("automation.dpi_scale_percent 必须在 50..300 之间。")
        if not (
            50
            <= self.automation.dpi_auto_min_percent
            <= self.automation.dpi_auto_max_percent
            <= 300
        ):
            raise ConfigError(
                "automation.dpi_auto_min_percent/max 必须在 50..300 之间并按升序排列。"
            )
        if not 1 <= self.automation.dpi_auto_step_percent <= 50:
            raise ConfigError("automation.dpi_auto_step_percent 必须在 1..50 之间。")
        if not -100000 <= self.automation.window_x <= 100000:
            raise ConfigError("automation.window_x 必须在 -100000..100000。")
        if not -100000 <= self.automation.window_y <= 100000:
            raise ConfigError("automation.window_y 必须在 -100000..100000。")
        if not 480 <= self.automation.window_width <= 10000:
            raise ConfigError("automation.window_width 必须在 480..10000 像素。")
        if not 360 <= self.automation.window_height <= 10000:
            raise ConfigError("automation.window_height 必须在 360..10000 像素。")
        if not isinstance(self.automation.qt_hot_activation_enabled, bool):
            raise ConfigError("automation.qt_hot_activation_enabled 必须是布尔值。")
        if not isinstance(
            self.automation.qt_hot_activation_start_reminder_disabled,
            bool,
        ):
            raise ConfigError(
                "automation.qt_hot_activation_start_reminder_disabled 必须是布尔值。"
            )
        if not isinstance(self.automation.qt_hot_activation_notice_accepted, str):
            raise ConfigError(
                "automation.qt_hot_activation_notice_accepted 必须是字符串。"
            )
        if self.automation.qt_hot_activation_enabled:
            raise ConfigError("v3 不支持 Qt/UIA 热激活、Hook 或内存修改。")
        hotkey_text = self.automation.stop_hotkey.strip()
        hotkey_parts = [part.strip().casefold() for part in hotkey_text.split("+")]
        modifier_parts = [part for part in hotkey_parts[:-1] if part in {"ctrl", "alt", "shift"}]
        if len(modifier_parts) != len(set(modifier_parts)):
            raise ConfigError("automation.stop_hotkey 不能重复使用 Ctrl、Alt 或 Shift。")
        if not re.fullmatch(
            r"(?i)(?:Esc|F(?:[1-9]|1[0-2])|(?:(?:Ctrl|Alt|Shift)\+){1,3}(?:[A-Z0-9]|Esc|F(?:[1-9]|1[0-2])))",
            hotkey_text,
        ):
            raise ConfigError(
                "automation.stop_hotkey 必须是 Esc、F1..F12，或 Ctrl/Alt/Shift 加字母、数字、Esc、F1..F12。"
            )
        if self.sender.wechat_executable:
            executable = Path(self.sender.wechat_executable)
            if executable.name.lower() not in {"weixin.exe", "wechat.exe"}:
                raise ConfigError("sender.wechat_executable 必须指向 Weixin.exe/WeChat.exe。")
        media = self.media
        if media.max_image_bytes < 1024 or media.max_file_bytes < 1024:
            raise ConfigError("media 文件大小上限不能小于 1024 字节。")
        if media.max_image_bytes > media.max_file_bytes:
            raise ConfigError("media.max_image_bytes 不能大于 media.max_file_bytes。")
        if media.download_timeout <= 0 or media.download_timeout > 120:
            raise ConfigError("media.download_timeout 必须在 0..120 秒。")
        if self.group_trigger not in {"all", "mention"}:
            raise ConfigError("group_trigger 只能是 all 或 mention。")
        if not 1 <= self.self_id <= 9_007_199_254_740_991:
            raise ConfigError("self_id 必须是 1..9007199254740991 的正整数。")

        console = self.console
        if not console.username.strip():
            raise ConfigError("控制台用户名不能为空。")
        if not is_password_hash(console.password_hash):
            raise ConfigError("控制台密码哈希无效。")
        if not 300 <= console.session_ttl_seconds <= 7 * 24 * 60 * 60:
            raise ConfigError("控制台会话有效期必须在 5 分钟到 7 天之间。")
        if bool(console.tls_cert) != bool(console.tls_key):
            raise ConfigError("console.tls_cert 和 console.tls_key 必须同时配置。")
        if not 1 <= console.port <= 65535:
            raise ConfigError("console.port 必须在 1..65535。")
        log_config = self.logging
        if log_config.level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ConfigError("logging.level must be DEBUG, INFO, WARNING, or ERROR.")
        if not 64 * 1024 <= log_config.max_bytes <= 1024 * 1024 * 1024:
            raise ConfigError("logging.max_bytes must be between 65536 and 1073741824.")
        if not 1 <= log_config.backup_count <= 50:
            raise ConfigError("logging.backup_count must be between 1 and 50.")
        return self

    def security_warnings(self) -> list[str]:
        warnings: list[str] = []
        if (
            _insecure_remote(self.astrbot.url) or _insecure_remote(self.weflow.url)
        ) and not self.allow_insecure_lan_connections:
            warnings.append(
                "AstrBot/WeFlow 正通过局域网明文 ws/http 连接，Token 可能被旁路读取。"
            )
        if (
            not _is_loopback_host(self.console.host)
            and not self.console.allow_lan
        ):
            warnings.append(
                "控制台正在监听非回环地址；console.allow_lan 尚未设为 true，但服务不会阻止启动。"
            )
        if self.active_api.enabled and not self.active_api.token:
            warnings.append(
                "主动发送 API 已启用但未配置 Token；只有本机回环地址可以调用。"
            )
        if (
            not _is_loopback_host(self.console.host)
            and not (self.console.tls_cert and self.console.tls_key)
            and not self.console.allow_insecure_lan
        ):
            warnings.append(
                "控制台已开放到局域网但未启用 TLS，登录密码和会话 Token 将以明文传输。"
            )
        return warnings


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{name} 必须是 JSON 对象。")
    return value


def from_dict(raw: dict[str, Any]) -> BridgeConfig:
    astrbot_raw = _mapping(raw.get("astrbot"), "astrbot")
    weflow_raw = _mapping(raw.get("weflow"), "weflow")
    active_api_raw = _mapping(raw.get("active_api"), "active_api")
    active_sender_profile, sender_profiles = sender_profile_state(raw)
    sender_raw = sender_profiles[active_sender_profile]
    automation_raw = _mapping(raw.get("automation"), "automation")
    media_raw = _mapping(raw.get("media"), "media")
    console_raw = _mapping(raw.get("console"), "console")
    logging_raw = _mapping(raw.get("logging"), "logging")
    security_raw = _mapping(raw.get("security"), "security")
    console_password = _secret(
        console_raw.get("password_hash") or console_raw.get("password", ""),
        "console.password",
    )
    if console_password and not is_password_hash(console_password):
        if len(console_password) < 8:
            raise ConfigError("控制台密码至少需要 8 个字符。")
        console_password = hash_password(console_password)

    astrbot_token = _secret(astrbot_raw.get("token", ""), "astrbot.token")
    weflow_token = _secret(weflow_raw.get("token", ""), "weflow.token")
    active_api_token = _secret(
        active_api_raw.get("token", ""),
        "active_api.token",
    )
    astrbot_enabled = (
        bool(astrbot_raw.get("enabled"))
        if "enabled" in astrbot_raw
        else bool(astrbot_token)
    )
    weflow_enabled = (
        bool(weflow_raw.get("enabled"))
        if "enabled" in weflow_raw
        else bool(weflow_token)
    )

    config = BridgeConfig(
        astrbot=AstrBotConfig(
            enabled=astrbot_enabled,
            url=str(astrbot_raw.get("url", AstrBotConfig.url)).strip(),
            token=astrbot_token,
            heartbeat=float(astrbot_raw.get("heartbeat", 20)),
            reconnect=float(astrbot_raw.get("reconnect", 5)),
            max_message_bytes=int(
                astrbot_raw.get("max_message_bytes", 32 * 1024 * 1024)
            ),
        ),
        weflow=WeFlowConfig(
            enabled=weflow_enabled,
            url=str(weflow_raw.get("url", WeFlowConfig.url)).strip().rstrip("/"),
            token=weflow_token,
            reconnect=float(weflow_raw.get("reconnect", 5)),
            connect_timeout=float(weflow_raw.get("connect_timeout", 5)),
            read_timeout=float(weflow_raw.get("read_timeout", 60)),
            max_event_bytes=int(weflow_raw.get("max_event_bytes", 1024 * 1024)),
        ),
        active_api=ActiveApiConfig(
            enabled=bool(active_api_raw.get("enabled", False)),
            token=active_api_token,
        ),
        sender=SenderConfig(
            timeout=float(sender_raw.get("timeout", 8)),
            settle=float(sender_raw.get("settle", 0.5)),
            conversation_entry_mode=str(
                sender_raw.get("conversation_entry_mode", "keyboard_shortcut")
                or "keyboard_shortcut"
            ).strip().lower(),
            conversation_enter_delay_min=float(
                sender_raw.get("conversation_enter_delay_min", 0.20)
            ),
            conversation_enter_delay_max=float(
                sender_raw.get("conversation_enter_delay_max", 0.50)
            ),
            text_verification_timeout=float(
                sender_raw.get("text_verification_timeout", 3)
            ),
            media_verification_mode=str(
                sender_raw.get("media_verification_mode", "none") or "none"
            ).strip().lower(),
            soft_protection=bool(sender_raw.get("soft_protection", True)),
            lock_mouse=bool(sender_raw.get("lock_mouse", True)),
            lock_keyboard=bool(sender_raw.get("lock_keyboard", False)),
            min_reply_delay=float(sender_raw.get("min_reply_delay", 3)),
            click_before_delay_min=float(
                sender_raw.get("click_before_delay_min", 0.10)
            ),
            click_before_delay_max=float(
                sender_raw.get("click_before_delay_max", 0.25)
            ),
            click_hold_duration_min=float(
                sender_raw.get("click_hold_duration_min", 0.04)
            ),
            click_hold_duration_max=float(
                sender_raw.get("click_hold_duration_max", 0.08)
            ),
            auto_launch_wechat=bool(sender_raw.get("auto_launch_wechat", False)),
            wechat_executable=str(sender_raw.get("wechat_executable", "") or "").strip(),
            launch_timeout=float(sender_raw.get("launch_timeout", 30)),
            adaptive_layout=bool(sender_raw.get("adaptive_layout", True)),
            reuse_open_chat=bool(sender_raw.get("reuse_open_chat", True)),
            layout_cache=bool(sender_raw.get("layout_cache", True)),
            mention_mode=str(
                sender_raw.get("mention_mode", "real") or "real"
            ).strip().lower(),
            mention_candidate_timeout=float(
                sender_raw.get("mention_candidate_timeout", 2)
            ),
            mention_after_at_delay_min=float(
                sender_raw.get("mention_after_at_delay_min", 0.12)
            ),
            mention_after_at_delay_max=float(
                sender_raw.get("mention_after_at_delay_max", 0.32)
            ),
            mention_min_wait=float(sender_raw.get("mention_min_wait", 0.25)),
            mention_before_enter_delay_min=float(
                sender_raw.get("mention_before_enter_delay_min", 0.10)
            ),
            mention_before_enter_delay_max=float(
                sender_raw.get("mention_before_enter_delay_max", 0.28)
            ),
            mention_confirm_timeout=float(
                sender_raw.get("mention_confirm_timeout", 0.8)
            ),
            mention_fallback_enabled=bool(
                sender_raw.get("mention_fallback_enabled", True)
            ),
            file_launch_fallback=bool(
                sender_raw.get("file_launch_fallback", False)
            ),
            render_mask_recovery=bool(
                sender_raw.get("render_mask_recovery", False)
            ),
            mask_retry_count=int(sender_raw.get("mask_retry_count", 0)),
            mask_wait=float(sender_raw.get("mask_wait", 0)),
            retry_max_attempts=int(sender_raw.get("retry_max_attempts", 4)),
            retry_delays=tuple(
                float(value)
                for value in (sender_raw.get("retry_delays", [2, 5, 5]) or [])
            ),
            overall_timeout=float(sender_raw.get("overall_timeout", 120)),
            input_mode=str(
                sender_raw.get("input_mode", "keyboard") or "keyboard"
            ).strip().lower(),
            append_line_break_after_input=bool(
                sender_raw.get("append_line_break_after_input", False)
            ),
            character_delay=float(sender_raw.get("character_delay", 0.03)),
            character_delay_min=float(
                sender_raw.get(
                    "character_delay_min",
                    sender_raw.get("character_delay", 0.03),
                )
            ),
            character_delay_max=float(
                sender_raw.get(
                    "character_delay_max",
                    sender_raw.get("character_delay", 0.03),
                )
            ),
            paste_enabled=bool(sender_raw.get("paste_enabled", False)),
            verification_enabled=bool(
                sender_raw.get("verification_enabled", False)
            ),
        ),
        automation=AutomationConfig(
            stop_hotkey_enabled=bool(
                automation_raw.get("stop_hotkey_enabled", True)
            ),
            stop_hotkey=str(
                automation_raw.get("stop_hotkey", "Esc") or "Esc"
            ).strip(),
            # Compatibility: older v3 builds stored these two global runtime
            # options inside the active sender profile.
            tray_activation_enabled=bool(
                automation_raw.get(
                    "tray_activation_enabled",
                    sender_raw.get("tray_activation", True),
                )
            ),
            tray_activation_timeout=float(
                automation_raw.get(
                    "tray_activation_timeout",
                    sender_raw.get("tray_timeout", 3),
                )
            ),
            dpi_scale_mode=str(
                automation_raw.get("dpi_scale_mode", "auto") or "auto"
            ).strip().lower(),
            dpi_scale_percent=int(
                automation_raw.get("dpi_scale_percent", 100)
            ),
            dpi_auto_min_percent=int(
                automation_raw.get("dpi_auto_min_percent", 70)
            ),
            dpi_auto_max_percent=int(
                automation_raw.get("dpi_auto_max_percent", 150)
            ),
            dpi_auto_step_percent=int(
                automation_raw.get("dpi_auto_step_percent", 5)
            ),
            window_position_enabled=bool(
                automation_raw.get("window_position_enabled", False)
            ),
            window_x=int(automation_raw.get("window_x", 100)),
            window_y=int(automation_raw.get("window_y", 80)),
            window_size_enabled=bool(
                automation_raw.get("window_size_enabled", False)
            ),
            window_width=int(automation_raw.get("window_width", 900)),
            window_height=int(automation_raw.get("window_height", 700)),
            wechat_ctrl_enter_confirmed=bool(
                automation_raw.get("wechat_ctrl_enter_confirmed", False)
            ),
            qt_hot_activation_enabled=False,
            qt_hot_activation_notice_accepted=str(
                automation_raw.get("qt_hot_activation_notice_accepted", "") or ""
            ).strip(),
            qt_hot_activation_start_reminder_disabled=bool(
                automation_raw.get(
                    "qt_hot_activation_start_reminder_disabled",
                    False,
                )
            ),
        ),
        media=MediaConfig(
            temp_dir=str(media_raw.get("temp_dir", "") or ""),
            max_image_bytes=int(media_raw.get("max_image_bytes", 20 * 1024 * 1024)),
            max_file_bytes=int(media_raw.get("max_file_bytes", 100 * 1024 * 1024)),
            download_timeout=float(media_raw.get("download_timeout", 15)),
            allowed_local_roots=tuple(
                str(x) for x in (media_raw.get("allowed_local_roots", []) or [])
            ),
            allowed_private_hosts=tuple(
                str(x).strip().lower()
                for x in (
                    media_raw.get(
                        "allowed_private_hosts",
                        ["localhost", "127.0.0.1", "::1"],
                    )
                    or []
                )
                if str(x).strip()
            ),
        ),
        console=ConsoleConfig(
            host=str(console_raw.get("host", "127.0.0.1")).strip(),
            port=int(console_raw.get("port", 8765)),
            username=str(console_raw.get("username", "admin") or "admin").strip(),
            password_hash=console_password,
            session_ttl_seconds=int(
                console_raw.get("session_ttl_seconds", 8 * 60 * 60)
            ),
            allow_lan=bool(console_raw.get("allow_lan", False)),
            allow_insecure_lan=bool(
                console_raw.get("allow_insecure_lan", False)
            ),
            allowed_origins=tuple(console_raw.get("allowed_origins", []) or []),
            tls_cert=str(console_raw.get("tls_cert", "") or ""),
            tls_key=str(console_raw.get("tls_key", "") or ""),
            auto_open_browser=bool(console_raw.get("auto_open_browser", True)),
            force_password_change=bool(
                console_raw.get("force_password_change", False)
            ),
        ),
        logging=LoggingConfig(
            directory=str(logging_raw.get("directory", "logs") or "logs"),
            level=str(logging_raw.get("level", "INFO") or "INFO").strip().upper(),
            max_bytes=int(logging_raw.get("max_bytes", 10 * 1024 * 1024)),
            backup_count=int(logging_raw.get("backup_count", 5)),
        ),
        self_id=int(raw.get("self_id", 10001)),
        bot_names=tuple(str(x) for x in (raw.get("bot_names", []) or [])),
        bot_wxid=str(raw.get("bot_wxid", "") or ""),
        group_trigger=str(raw.get("group_trigger", "all") or "all").lower(),
        state_file=str(raw.get("state_file", "bridge_state.json")),
        allow_insecure_lan_connections=bool(
            security_raw.get("allow_insecure_lan_connections", False)
        ),
    )
    return config.validate()


def load_config(path: str | Path) -> BridgeConfig:
    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置 JSON 无效: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是 JSON 对象。")
    config = from_dict(raw)
    state_path = Path(config.state_file)
    if not state_path.is_absolute():
        config = replace(config, state_file=str(config_path.parent / state_path))
    if config.sender.wechat_executable:
        executable = Path(config.sender.wechat_executable)
        if not executable.is_absolute():
            executable = config_path.parent / executable
        config = replace(
            config,
            sender=replace(config.sender, wechat_executable=str(executable.resolve())),
        )
    media = config.media
    temp_dir = Path(media.temp_dir) if media.temp_dir else config_path.parent / ".bridge_media"
    if not temp_dir.is_absolute():
        temp_dir = config_path.parent / temp_dir
    local_roots = tuple(
        str((config_path.parent / Path(root)).resolve())
        if not Path(root).is_absolute()
        else str(Path(root).resolve())
        for root in media.allowed_local_roots
    )
    config = replace(
        config,
        media=replace(
            media,
            temp_dir=str(temp_dir.resolve()),
            allowed_local_roots=local_roots,
        ),
    )
    log_directory = Path(config.logging.directory)
    if not log_directory.is_absolute():
        log_directory = config_path.parent / log_directory
    config = replace(
        config,
        logging=replace(config.logging, directory=str(log_directory.resolve())),
    )
    return config


def update_config_file(
    path: str | Path,
    mutator: Any,
) -> dict[str, Any]:
    """Atomically update a config document without expanding secret placeholders.

    The mutator receives a deep copy of the raw JSON object. Unknown fields and
    values such as ``${ASTRBOT_TOKEN}`` are preserved unless the mutator changes
    them explicitly. The candidate is validated before it replaces the file.
    """

    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置 JSON 无效: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是 JSON 对象。")

    candidate = copy.deepcopy(raw)
    changed = mutator(candidate)
    if changed is not None:
        candidate = changed
    if not isinstance(candidate, dict):
        raise ConfigError("配置更新结果必须是 JSON 对象。")
    from_dict(candidate)
    if "sender_profiles" in candidate or "active_sender_profile" in candidate:
        _, profiles_to_validate = sender_profile_state(candidate)
        for profile_name in SENDER_PROFILE_NAMES:
            validation_copy = copy.deepcopy(candidate)
            validation_copy["active_sender_profile"] = profile_name
            validation_copy["sender_profiles"] = profiles_to_validate
            from_dict(validation_copy)
    if "sender_profiles" in candidate or "active_sender_profile" in candidate:
        active_sender_profile, sender_profiles = sender_profile_state(candidate)
        candidate["active_sender_profile"] = active_sender_profile
        candidate["sender_profiles"] = sender_profiles
        candidate["sender"] = copy.deepcopy(sender_profiles[active_sender_profile])

    config_path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=config_path.parent,
            prefix=config_path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temp_name = stream.name
            json.dump(candidate, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, config_path)
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
    return candidate
