"""File logging and redacted transport auditing for the bridge."""

from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


BRIDGE_LOGGER_NAME = "wechat_bridge"
TRANSPORT_LOGGER_NAME = "wechat_bridge.transport"
_HANDLER_MARKER = "wechat_bridge_handler"
_SECRET_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "password",
    "password_hash",
    "secret",
    "token",
}
_MEDIA_KEYS = {"base64", "data_uri", "media_local_path", "medialocalpath"}
_PATH_KEYS = {"file", "path", "mediaurl", "media_url", "url"}


def _safe_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        query = [
            (key, "[REDACTED]" if key.lower() in _SECRET_KEYS else item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
        ]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    except Exception:
        return "[URL_REDACTED]"


def _safe_value(key: str, value: Any, *, depth: int = 0) -> Any:
    normalized = re.sub(r"[^a-z0-9_]", "", str(key).lower())
    if normalized in _SECRET_KEYS or any(secret in normalized for secret in ("token", "password", "authorization")):
        return "[REDACTED]"
    if depth > 6:
        return "[DEPTH_LIMIT]"
    if isinstance(value, dict):
        return {
            str(item_key): _safe_value(str(item_key), item_value, depth=depth + 1)
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(key, item, depth=depth + 1) for item in value[:200]]
    if isinstance(value, bytes):
        return {"redacted_bytes": True, "length": len(value)}
    if isinstance(value, str):
        if normalized in _MEDIA_KEYS or normalized in {"base64", "data"} and len(value) > 4096:
            digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
            return {"redacted_media": True, "length": len(value), "sha256": digest}
        if normalized in _PATH_KEYS:
            if value.lower().startswith(("base64://", "data:")):
                digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
                return {"redacted_media": True, "length": len(value), "sha256": digest}
            if value.startswith(("http://", "https://")):
                return _safe_url(value)
            return {"redacted_path": True, "name": Path(value).name}
        return value[:20000] + ("...[TRUNCATED]" if len(value) > 20000 else "")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def sanitize_payload(payload: Any) -> Any:
    return _safe_value("payload", payload)


def transport_event(
    direction: str,
    channel: str,
    event: str,
    payload: Any = None,
    **details: Any,
) -> None:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "direction": direction,
        "channel": channel,
        "event": event,
        "payload": sanitize_payload(payload),
    }
    if details:
        record["details"] = sanitize_payload(details)
    logging.getLogger(TRANSPORT_LOGGER_NAME).info(
        json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
    )


def _level(value: str) -> int:
    return getattr(logging, str(value or "INFO").upper(), logging.INFO)


def configure_logging(
    directory: str,
    *,
    level: str = "INFO",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> dict[str, str]:
    log_dir = Path(directory).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    bridge_path = log_dir / "bridge.log"
    transport_path = log_dir / "transport.jsonl"

    bridge_logger = logging.getLogger(BRIDGE_LOGGER_NAME)
    bridge_logger.setLevel(_level(level))
    bridge_logger.propagate = False
    transport_logger = logging.getLogger(TRANSPORT_LOGGER_NAME)
    transport_logger.setLevel(logging.INFO)
    transport_logger.propagate = False

    for logger in (bridge_logger, transport_logger):
        for handler in list(logger.handlers):
            if getattr(handler, _HANDLER_MARKER, False):
                logger.removeHandler(handler)
                handler.close()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    bridge_file = logging.handlers.RotatingFileHandler(
        bridge_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    setattr(bridge_file, _HANDLER_MARKER, True)
    bridge_file.setFormatter(formatter)
    bridge_logger.addHandler(bridge_file)

    console_handler = logging.StreamHandler()
    setattr(console_handler, _HANDLER_MARKER, True)
    console_handler.setFormatter(formatter)
    bridge_logger.addHandler(console_handler)

    transport_file = logging.handlers.RotatingFileHandler(
        transport_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    setattr(transport_file, _HANDLER_MARKER, True)
    transport_file.setFormatter(logging.Formatter("%(message)s"))
    transport_logger.addHandler(transport_file)

    return {"bridge": str(bridge_path), "transport": str(transport_path)}


def read_log_tail(directory: str, kind: str, lines: int = 200) -> dict[str, Any]:
    if kind not in {"bridge", "transport"}:
        raise ValueError("kind must be bridge or transport")
    count = max(1, min(int(lines), 1000))
    path = Path(directory).resolve() / ("bridge.log" if kind == "bridge" else "transport.jsonl")
    if not path.is_file():
        return {
            "kind": kind,
            "path": str(path),
            "lines": [],
            "entries": [],
            "exists": False,
        }
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        tail = list(handle)[-count:]
    clean_lines = [line.rstrip("\r\n") for line in tail]
    entries: list[dict[str, Any]] = []
    for line in clean_lines:
        level = ""
        if kind == "bridge":
            match = re.match(
                r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:,\d+)?\s+"
                r"(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b",
                line,
            )
            if match:
                level = "ERROR" if match.group(1) == "CRITICAL" else match.group(1)
        else:
            try:
                record = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                record = None
            if isinstance(record, dict):
                candidate = str(record.get("level") or "INFO").upper()
                level = candidate if candidate in {"DEBUG", "INFO", "WARNING", "ERROR"} else "INFO"
        entries.append({"level": level, "text": line})
    return {
        "kind": kind,
        "path": str(path),
        "lines": clean_lines,
        "entries": entries,
        "exists": True,
    }
