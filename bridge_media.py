"""Safe media resolution and visible image-locator sending for v3.

The resolver keeps v2's proven security boundary: bounded Base64/data URLs,
HTTPS downloads with redirect revalidation, and explicitly allow-listed local
paths.  The sender itself is v3-only and uses the same visible Win32/image
location transaction as text sending; it does not import v2 or use UIA.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import logging
import re
import socket
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence
from urllib.parse import unquote, urljoin, urlparse

import requests

from bridge_config import MediaConfig
from wechat_desktop.input_guard import InputGuardError, WindowsInputGuard
from wechat_desktop.message_sender import (
    DesktopMessageError,
    DesktopMessageSettings,
    VisualDesktopMessageSender,
)
from wechat_sender import (
    HARD_LOCK_MAX_SECONDS,
    KIND_ALIASES,
    SOFT_RETRY_DELAYS,
    SendResult,
    SenderError,
    interruptible_sleep,
    raise_if_cancelled,
)


log = logging.getLogger("wechat_automation.media")

_DATA_URL_RE = re.compile(r"^data:([^;,]+)?;base64,(.*)$", re.IGNORECASE | re.DOTALL)


class MediaError(SenderError):
    pass


@dataclass
class ResolvedMedia:
    media_type: str
    path: str
    temporary: bool = False
    temporary_parent: str = ""

    def cleanup(self) -> None:
        if self.temporary:
            try:
                Path(self.path).unlink(missing_ok=True)
            except OSError:
                pass
        if self.temporary_parent:
            try:
                Path(self.temporary_parent).rmdir()
            except OSError:
                pass


class MediaResolver:
    """Resolve OneBot media references into bounded, validated local files."""

    def __init__(
        self,
        config: MediaConfig,
        *,
        session: Any = requests,
        resolver: Callable[..., Any] = socket.getaddrinfo,
    ) -> None:
        self.config = config
        self.session = session
        self.resolver = resolver

    @staticmethod
    def _source(data: dict[str, Any], media_type: str) -> str:
        for key in ("file", "url", "path"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        raise MediaError("media_source_missing", f"{media_type} 消息段缺少 file/url/path。")

    @staticmethod
    def _safe_suffix(name: str, default: str) -> str:
        suffix = Path(unquote(str(name or ""))).suffix.lower()
        return suffix if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix) else default

    @staticmethod
    def _safe_name(name: str, default: str) -> str:
        candidate = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
        candidate = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", candidate)
        candidate = candidate.rstrip(" .")[:180]
        return candidate or default

    def _temporary_file(self, data: bytes, suffix: str, name: str = "") -> ResolvedMedia:
        temp_dir = Path(self.config.temp_dir)
        temp_dir.mkdir(parents=True, exist_ok=True)
        parent = Path(tempfile.mkdtemp(prefix="outbound-", dir=temp_dir))
        safe_name = self._safe_name(name, f"media{suffix}")
        if not Path(safe_name).suffix and suffix:
            safe_name += suffix
        raw_path = parent / safe_name
        try:
            raw_path.write_bytes(data)
        except Exception:
            raw_path.unlink(missing_ok=True)
            parent.rmdir()
            raise
        return ResolvedMedia("", str(raw_path), True, str(parent))

    def _decode_base64(self, source: str, *, media_type: str, name: str) -> ResolvedMedia:
        payload = source
        suffix = self._safe_suffix(name, ".png" if media_type == "image" else ".bin")
        match = _DATA_URL_RE.match(source)
        if match:
            mime = (match.group(1) or "").lower()
            payload = match.group(2)
            suffix = self._safe_suffix(
                name,
                {
                    "image/png": ".png",
                    "image/jpeg": ".jpg",
                    "image/webp": ".webp",
                    "image/gif": ".gif",
                    "application/pdf": ".pdf",
                }.get(mime, suffix),
            )
        elif payload.lower().startswith("base64://"):
            payload = payload[9:]
        limit = self.config.max_image_bytes if media_type == "image" else self.config.max_file_bytes
        if len(payload) > (limit * 4 // 3) + 4096:
            raise MediaError("media_too_large", f"{media_type} Base64 数据超过大小限制。")
        try:
            decoded = base64.b64decode(payload, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise MediaError("media_base64_invalid", f"{media_type} Base64 数据无效。") from exc
        if len(decoded) > limit:
            raise MediaError("media_too_large", f"{media_type} 超过大小限制。")
        result = self._temporary_file(decoded, suffix, name)
        result.media_type = media_type
        return result

    def _allowed_local(self, path: Path) -> bool:
        resolved = path.resolve()
        for root in self.config.allowed_local_roots:
            try:
                resolved.relative_to(Path(root).resolve())
                return True
            except ValueError:
                continue
        return False

    @staticmethod
    def _local_path_from_source(source: str) -> Path:
        if not source.lower().startswith("file://"):
            return Path(source).expanduser()
        parsed = urlparse(source)
        raw_path = unquote(parsed.path or "")
        if parsed.netloc and parsed.netloc.lower() not in {"", "localhost"}:
            raw_path = f"//{parsed.netloc}{raw_path}"
        elif re.match(r"^/[A-Za-z]:/", raw_path):
            raw_path = raw_path[1:]
        return Path(raw_path).expanduser()

    def _validate_url(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise MediaError("media_url_invalid", "媒体 URL 必须是 http 或 https 地址。")
        if parsed.username or parsed.password:
            raise MediaError("media_url_invalid", "媒体 URL 不允许包含用户名或密码。")
        host = parsed.hostname.lower().strip("[]")
        if host in set(self.config.allowed_private_hosts):
            return "allowed_private"
        try:
            addresses = {
                item[4][0]
                for item in self.resolver(
                    host,
                    parsed.port or (443 if parsed.scheme == "https" else 80),
                    0,
                    0,
                )
            }
        except OSError as exc:
            raise MediaError("media_host_unresolved", f"无法解析媒体主机 {host}。") from exc
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                continue
            if not ip.is_global:
                raise MediaError(
                    "media_private_url_blocked",
                    f"媒体 URL 指向非公网地址 {address}；请把主机加入 media.allowed_private_hosts。",
                )
        if parsed.scheme != "https":
            raise MediaError("media_insecure_url", "公网媒体下载必须使用 HTTPS。")
        return "public"

    def _download(self, source: str, *, media_type: str, name: str) -> ResolvedMedia:
        current = source
        limit = self.config.max_image_bytes if media_type == "image" else self.config.max_file_bytes
        suffix = self._safe_suffix(name, ".png" if media_type == "image" else ".bin")
        response = None
        initial_scope: Optional[str] = None
        try:
            for _ in range(5):
                scope = self._validate_url(current)
                if initial_scope is None:
                    initial_scope = scope
                elif scope != initial_scope:
                    raise MediaError(
                        "media_redirect_scope_changed",
                        "媒体重定向不能在公网与已允许私网之间切换。",
                    )
                response = self.session.get(
                    current,
                    stream=True,
                    allow_redirects=False,
                    timeout=self.config.download_timeout,
                    headers={"User-Agent": "wechat-bridge-v3/3.0"},
                )
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    response.close()
                    response = None
                    if not location:
                        raise MediaError("media_redirect_invalid", "媒体重定向缺少 Location。")
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    raise MediaError(
                        "media_download_failed",
                        f"媒体下载返回 HTTP {response.status_code}。",
                    )
                declared = int(response.headers.get("Content-Length", "0") or 0)
                if declared > limit:
                    raise MediaError("media_too_large", f"{media_type} 超过大小限制。")
                content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].lower()
                if media_type == "image" and content_type and not content_type.startswith("image/"):
                    raise MediaError("media_type_mismatch", "远程资源不是图片。")
                if suffix == ".bin":
                    suffix = self._safe_suffix(urlparse(current).path, suffix)
                temp_root = Path(self.config.temp_dir)
                temp_root.mkdir(parents=True, exist_ok=True)
                parent = Path(tempfile.mkdtemp(prefix="outbound-", dir=temp_root))
                source_name = name or unquote(Path(urlparse(current).path).name)
                safe_name = self._safe_name(source_name, f"media{suffix}")
                if not Path(safe_name).suffix and suffix:
                    safe_name += suffix
                raw_path = parent / safe_name
                total = 0
                try:
                    with raw_path.open("wb") as handle:
                        for chunk in response.iter_content(chunk_size=64 * 1024):
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > limit:
                                raise MediaError("media_too_large", f"{media_type} 超过大小限制。")
                            handle.write(chunk)
                except Exception:
                    raw_path.unlink(missing_ok=True)
                    parent.rmdir()
                    raise
                return ResolvedMedia(media_type, str(raw_path), True, str(parent))
            raise MediaError("media_redirect_limit", "媒体重定向次数过多。")
        finally:
            if response is not None:
                response.close()

    @staticmethod
    def verify_image(path: str | Path) -> None:
        try:
            from PIL import Image

            with Image.open(path) as image:
                image.verify()
        except Exception as exc:
            raise MediaError("image_invalid", "图片无法被 Pillow 解码。") from exc

    def resolve(self, media_type: str, data: dict[str, Any]) -> ResolvedMedia:
        if media_type not in {"image", "file"}:
            raise MediaError("unsupported_media_type", f"不支持媒体类型：{media_type}")
        source = self._source(data, media_type)
        name = str(data.get("name") or "")
        if source.lower().startswith("base64://") or _DATA_URL_RE.match(source):
            result = self._decode_base64(source, media_type=media_type, name=name)
        elif source.lower().startswith(("http://", "https://")):
            result = self._download(source, media_type=media_type, name=name)
        else:
            path = self._local_path_from_source(source)
            if not path.is_absolute() or not self._allowed_local(path):
                raise MediaError(
                    "media_local_path_blocked",
                    "本地媒体路径未被允许；请配置 media.allowed_local_roots。",
                )
            if not path.is_file():
                raise MediaError("media_file_not_found", f"媒体文件不存在：{path}")
            limit = self.config.max_image_bytes if media_type == "image" else self.config.max_file_bytes
            if path.stat().st_size > limit:
                raise MediaError("media_too_large", f"{media_type} 超过大小限制。")
            result = ResolvedMedia(media_type, str(path.resolve()))
        if media_type == "image":
            try:
                self.verify_image(result.path)
            except MediaError:
                result.cleanup()
                raise
        return result


def _retry_schedule(max_attempts: int, delays: Sequence[float], enabled: bool) -> tuple[float, ...]:
    if not enabled or max_attempts <= 1:
        return ()
    needed = max_attempts - 1
    values = tuple(float(value) for value in delays)
    if not values:
        return (0.0,) * needed
    return values[:needed] + (values[-1],) * max(0, needed - len(values))


def send_media(
    kind: str,
    name: str,
    media_type: str,
    path: str,
    *,
    timeout: float = 8.0,
    settle: float = 0.35,
    conversation_entry_mode: str = "keyboard_shortcut",
    conversation_enter_delay_min: float = 0.20,
    conversation_enter_delay_max: float = 0.50,
    soft_protection: bool = True,
    lock_mouse: bool = False,
    lock_keyboard: bool = False,
    layout_cache: bool = True,
    send_review_delay_min: float = 0.60,
    send_review_delay_max: float = 1.40,
    click_before_delay_min: float = 0.10,
    click_before_delay_max: float = 0.25,
    click_hold_duration_min: float = 0.04,
    click_hold_duration_max: float = 0.08,
    tray_activation: bool = True,
    tray_timeout: float = 3.0,
    dpi_scale_mode: str = "auto",
    dpi_scale_percent: int = 100,
    dpi_auto_min_percent: int = 70,
    dpi_auto_max_percent: int = 150,
    dpi_auto_step_percent: int = 5,
    retry_max_attempts: int = 4,
    retry_delays: Optional[Sequence[float]] = None,
    overall_timeout: float = 120.0,
    stop_hotkey: str = "Esc",
    window_position_enabled: bool = False,
    window_x: int = 100,
    window_y: int = 80,
    window_size_enabled: bool = False,
    window_width: int = 900,
    window_height: int = 700,
    stop_callback: Callable[[], Any] | None = None,
    cancel_event: Optional[threading.Event] = None,
    automation_factory: Callable[..., VisualDesktopMessageSender] = VisualDesktopMessageSender,
    input_guard_factory: Callable[..., Any] = WindowsInputGuard,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    **_unused: Any,
) -> SendResult:
    """Send one media item; only failures before the final click may retry."""

    started = monotonic()
    normalized_kind = KIND_ALIASES.get(str(kind or "").strip().lower())
    normalized_name = str(name or "").strip()
    normalized_type = str(media_type or "").strip().lower()
    media_path = Path(path).resolve()
    try:
        if normalized_kind is None:
            raise SenderError("invalid_kind", "会话类型只能是私聊或群聊。")
        if not normalized_name:
            raise SenderError("invalid_name", "会话名称不能为空。")
        if normalized_type not in {"image", "file"}:
            raise SenderError("unsupported_media_type", "媒体类型只能是图片或文件。")
        if not media_path.is_file():
            raise SenderError("media_file_not_found", f"媒体文件不存在：{media_path}")
        if normalized_type == "image":
            MediaResolver.verify_image(media_path)
        if not 1 <= int(retry_max_attempts) <= 10:
            raise SenderError("invalid_sender_settings", "最多尝试次数必须在 1 到 10 之间。")
        delays = tuple(SOFT_RETRY_DELAYS if retry_delays is None else retry_delays)
        if len(delays) > 9 or any(float(value) < 0 or float(value) > 120 for value in delays):
            raise SenderError("invalid_sender_settings", "整体重试等待设置无效。")
        if not 1 <= float(overall_timeout) <= 600:
            raise SenderError("invalid_sender_settings", "整条消息总时限必须在 1 到 600 秒之间。")
        settings = DesktopMessageSettings(
            locate_timeout=float(timeout),
            settle=float(settle),
            conversation_entry_mode=str(
                conversation_entry_mode or "keyboard_shortcut"
            ).strip().lower(),
            conversation_enter_delay_min=float(conversation_enter_delay_min),
            conversation_enter_delay_max=float(conversation_enter_delay_max),
            input_mode="clipboard",
            layout_cache=bool(layout_cache),
            send_review_delay_min=float(send_review_delay_min),
            send_review_delay_max=float(send_review_delay_max),
            click_before_delay_min=float(click_before_delay_min),
            click_before_delay_max=float(click_before_delay_max),
            click_hold_duration_min=float(click_hold_duration_min),
            click_hold_duration_max=float(click_hold_duration_max),
            tray_activation_enabled=bool(tray_activation),
            tray_activation_timeout=float(tray_timeout),
            dpi_scale_mode=str(dpi_scale_mode or "auto").strip().lower(),
            dpi_scale_percent=int(dpi_scale_percent),
            dpi_auto_min_percent=int(dpi_auto_min_percent),
            dpi_auto_max_percent=int(dpi_auto_max_percent),
            dpi_auto_step_percent=int(dpi_auto_step_percent),
            window_position_enabled=bool(window_position_enabled),
            window_x=int(window_x),
            window_y=int(window_y),
            window_size_enabled=bool(window_size_enabled),
            window_width=int(window_width),
            window_height=int(window_height),
        )
    except (SenderError, ValueError, TypeError) as exc:
        error = exc if isinstance(exc, SenderError) else SenderError("invalid_sender_settings", str(exc))
        return SendResult(
            False,
            error.code,
            error.message,
            kind=normalized_kind,
            name=normalized_name or None,
            elapsed_ms=int((monotonic() - started) * 1000),
            details=dict(error.details),
        )

    schedule = _retry_schedule(int(retry_max_attempts), delays, bool(soft_protection))
    if cancel_event is None and (lock_mouse or lock_keyboard):
        cancel_event = threading.Event()
    failures: list[dict[str, Any]] = []
    deadline = started + float(overall_timeout)
    for attempt_index in range(len(schedule) + 1):
        try:
            raise_if_cancelled(cancel_event)
            log.info(
                "整体媒体尝试开始：第 %s/%s 次。",
                attempt_index + 1,
                len(schedule) + 1,
                extra={"automation_operation": "media.attempt"},
            )
            if attempt_index:
                wait = schedule[attempt_index - 1]
                if monotonic() + wait >= deadline:
                    raise SenderError("automation_timeout", "剩余总时限不足以开始下一次媒体重试。")
                log.info(
                    "整体媒体重试前等待开始：%.3f 秒。",
                    wait,
                    extra={"automation_operation": "media.retry_wait"},
                )
                interruptible_sleep(wait, cancel_event=cancel_event, fallback=sleep)
                log.info(
                    "整体媒体重试前等待结束。",
                    extra={"automation_operation": "media.retry_wait"},
                )
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise SenderError("automation_timeout", "整条媒体自动化已超过总时限。")
            current_settings = DesktopMessageSettings(
                **{
                    **settings.__dict__,
                    "locate_timeout": min(settings.locate_timeout, max(0.2, remaining)),
                }
            )
            sender = automation_factory(settings=current_settings)
            guard_options = {
                "lock_mouse": bool(lock_mouse),
                "lock_keyboard": bool(lock_keyboard),
                "max_seconds": HARD_LOCK_MAX_SECONDS,
                "cancel_event": cancel_event,
                "emergency_hotkey": str(stop_hotkey or "Esc"),
            }
            if stop_callback is not None:
                guard_options["on_emergency_stop"] = stop_callback
            try:
                guard = input_guard_factory(**guard_options)
            except TypeError as exc:
                if "on_emergency_stop" not in str(exc):
                    raise
                guard_options.pop("on_emergency_stop", None)
                guard = input_guard_factory(**guard_options)
            with guard:
                result = sender.send_media_once(
                    target_kind=normalized_kind,
                    target_name=normalized_name,
                    media_type=normalized_type,
                    path=media_path,
                    cancel_event=cancel_event,
                )
            input_lock = dict(guard.details()) if hasattr(guard, "details") else {}
            return SendResult(
                True,
                "media_sent_unverified",
                "媒体发送动作已执行；本地未确认最终送达。",
                kind=normalized_kind,
                name=normalized_name,
                elapsed_ms=int((monotonic() - started) * 1000),
                details={
                    **result.details,
                    "send_committed": result.send_committed,
                    "send_clicked": result.send_committed,
                    "states": list(result.states),
                    "input_lock": input_lock,
                    "protection": {
                        "overall_attempts": attempt_index + 1,
                        "overall_max_attempts": len(schedule) + 1,
                        "overall_failures": failures,
                    },
                },
            )
        except DesktopMessageError as exc:
            error = SenderError(exc.code, exc.message, details=exc.details)
        except InputGuardError as exc:
            error = SenderError(exc.code, exc.message, details=exc.details)
        except SenderError as exc:
            error = exc
        except InterruptedError:
            error = SenderError("automation_cancelled", "自动化已停止，当前媒体没有发送。")
        except Exception as exc:
            error = SenderError(
                "unexpected_error",
                f"媒体桌面自动化出现未预期错误：{exc}",
                details={"error_type": type(exc).__name__},
            )

        failures.append(
            {"attempt": attempt_index + 1, "code": error.code, "message": error.message}
        )
        committed = bool(error.details.get("send_committed") or error.details.get("send_clicked"))
        if committed:
            return SendResult(
                True,
                "media_sent_unverified",
                "媒体发送动作已经开始，但后续状态无法确认；不会自动重试。",
                kind=normalized_kind,
                name=normalized_name,
                elapsed_ms=int((monotonic() - started) * 1000),
                details={
                    **error.details,
                    "ui_verified": False,
                    "automatic_retry": False,
                    "protection": {
                        "overall_attempts": attempt_index + 1,
                        "overall_failures": failures,
                    },
                },
            )
        retryable = error.code not in {
            "automation_cancelled",
            "invalid_kind",
            "invalid_name",
            "media_file_not_found",
            "unsupported_media_type",
            "image_invalid",
            "invalid_sender_settings",
        }
        if retryable and attempt_index < len(schedule):
            log.warning(
                "第 %s 次媒体自动化失败，将整体重试：%s - %s",
                attempt_index + 1,
                error.code,
                error.message,
            )
            continue
        return SendResult(
            False,
            error.code,
            error.message,
            kind=normalized_kind,
            name=normalized_name,
            elapsed_ms=int((monotonic() - started) * 1000),
            details={
                **error.details,
                "automatic_retry": False,
                "protection": {
                    "overall_attempts": attempt_index + 1,
                    "overall_max_attempts": len(schedule) + 1,
                    "overall_failures": failures,
                },
            },
        )
    raise AssertionError("媒体整体重试循环不应运行到这里。")


__all__ = ["MediaError", "MediaResolver", "ResolvedMedia", "send_media"]
