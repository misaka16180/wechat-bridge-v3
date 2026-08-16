"""Bounded WeFlow SSE client used by the v3 bridge."""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from datetime import datetime
from typing import Any, Callable, Iterable, Iterator, Optional

import requests

from bridge_config import WeFlowConfig
from bridge_logging import transport_event
from bridge_protocol import is_internal_group_name


log = logging.getLogger("wechat_bridge.weflow")


_MESSAGE_ID_KEYS = (
    "rawid",
    "rawId",
    "serverIdRaw",
    "server_id_raw",
    "serverId",
    "server_id",
    "messageId",
    "message_id",
    "msgId",
    "msg_id",
    "localId",
    "local_id",
    "id",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _message_ids(row: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in _MESSAGE_ID_KEYS:
        value = row.get(key)
        if value not in (None, ""):
            values.add(str(value))
    return values


def _member_profile(row: dict[str, Any]) -> dict[str, str]:
    """Keep only the identity fields needed by the bridge and UI mention."""

    return {
        "wxid": _text(row.get("wxid") or row.get("username")),
        "display_name": _text(row.get("displayName") or row.get("display_name")),
        "nickname": _text(row.get("nickname") or row.get("nickName")),
        "remark": _text(row.get("remark")),
        "alias": _text(row.get("alias")),
        "group_nickname": _text(
            row.get("groupNickname") or row.get("group_nickname")
        ),
    }


_GROUP_RENAME_PATTERNS = (
    re.compile(r"^你修改群名为[“\"](.+?)[”\"]$"),
    re.compile(r"^.+?修改群名为[“\"](.+?)[”\"]$"),
    re.compile(r"^群聊名称已修改为[“\"](.+?)[”\"]$"),
)


def _clean_group_name(value: Any) -> str:
    return re.sub(r"\s*\(\d+\)\s*$", "", _text(value))


def _row_timestamp(row: dict[str, Any]) -> float:
    """Return a sortable timestamp for the different WeFlow API versions."""

    value = (
        row.get("createTime")
        or row.get("lastTimestamp")
        or row.get("last_timestamp")
        or row.get("timestamp")
        or row.get("time")
        or 0
    )
    try:
        numeric = float(value)
        # Some endpoints use milliseconds while others use seconds.
        return numeric / 1000 if numeric > 1e12 else numeric
    except (TypeError, ValueError):
        pass
    text_value = _text(value)
    if not text_value:
        return 0.0
    try:
        return datetime.fromisoformat(
            text_value.replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _group_name_from_rename_message(row: dict[str, Any]) -> str:
    """Extract a group name only from a WeChat-style system rename notice."""

    content = _text(
        row.get("content")
        or row.get("text")
        or row.get("message")
        or row.get("raw_message")
    )
    if not content:
        return ""
    # A normal member can type similar text. WeFlow represents actual system
    # notices without a sender wxid (and its SSE adapter uses 未知发送者).
    sender_id = _text(
        row.get("senderUsername")
        or row.get("sender_username")
        or row.get("senderWxid")
        or row.get("sender_wxid")
    )
    source_name = _text(row.get("sourceName") or row.get("senderName"))
    if sender_id or (source_name and source_name not in {"未知", "未知发送者", "系统消息"}):
        return ""
    for pattern in _GROUP_RENAME_PATTERNS:
        matched = pattern.fullmatch(content)
        if matched:
            candidate = _clean_group_name(matched.group(1))
            return candidate if not is_internal_group_name(candidate) else ""
    return ""


def iter_sse_data(lines: Iterable[str | bytes], max_event_bytes: int) -> Iterator[str]:
    """Parse SSE data fields, including valid multi-line events."""
    chunks: list[str] = []
    size = 0
    dropping_oversized_event = False
    for raw_line in lines:
        if isinstance(raw_line, bytes):
            raw_line = raw_line.decode("utf-8", errors="replace")
        line = raw_line.rstrip("\r")
        if line == "":
            if chunks and not dropping_oversized_event:
                yield "\n".join(chunks)
            chunks = []
            size = 0
            dropping_oversized_event = False
            continue
        if dropping_oversized_event:
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if field != "data":
            continue
        if value.startswith(" "):
            value = value[1:]
        encoded_size = len(value.encode("utf-8"))
        if size + encoded_size > max_event_bytes:
            chunks = []
            size = 0
            dropping_oversized_event = True
            log.warning("WeFlow SSE 事件超过大小限制，已丢弃。")
            continue
        chunks.append(value)
        size += encoded_size
    if chunks:
        yield "\n".join(chunks)


class WeFlowSseClient:
    def __init__(
        self,
        config: WeFlowConfig,
        on_message: Callable[[dict[str, Any]], None],
        *,
        session: Optional[requests.Session] = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.on_message = on_message
        self.session = session or requests.Session()
        if session is None:
            # Do not leak local API traffic or access tokens through ambient
            # HTTP(S)_PROXY environment variables.
            self.session.trust_env = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._response: Optional[requests.Response] = None
        self._connected = False
        self._lock = threading.Lock()
        self._monotonic = monotonic
        self.last_error = ""
        self._member_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}
        self._message_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._group_name_cache: dict[str, tuple[float, str, str]] = {}

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"
        return headers

    def _get_json(
        self,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        endpoint = f"{self.config.url}{path}"
        try:
            response = self.session.get(
                endpoint,
                params=params,
                headers=self._headers(),
                timeout=self.config.connect_timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise requests.RequestException(self._safe_error(exc)) from exc
        except ValueError as exc:
            raise requests.RequestException("WeFlow REST 返回无效 JSON。") from exc
        if not isinstance(payload, dict):
            raise requests.RequestException("WeFlow REST 返回值不是 JSON 对象。")
        return payload

    def group_members(
        self,
        chatroom_id: str,
        *,
        force: bool = False,
        max_age: float = 300.0,
    ) -> list[dict[str, str]]:
        room = _text(chatroom_id)
        if not room or "@chatroom" not in room:
            return []
        now = time.monotonic()
        with self._lock:
            cached = self._member_cache.get(room)
            if not force and cached and now - cached[0] <= max_age:
                return [dict(item) for item in cached[1]]

        payload = self._get_json(
            "/api/v1/group-members",
            params={"chatroomId": room},
        )
        source = payload.get("members") or payload.get("data") or []
        if not isinstance(source, list):
            source = []
        members = [
            _member_profile(item)
            for item in source
            if isinstance(item, dict) and _member_profile(item)["wxid"]
        ]
        with self._lock:
            self._member_cache[room] = (now, members)
        return [dict(item) for item in members]

    @staticmethod
    def _rows(payload: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [dict(item) for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _session_id(row: dict[str, Any]) -> str:
        return _text(
            row.get("username")
            or row.get("sessionId")
            or row.get("talkerId")
            or row.get("id")
        )

    def _deliver_message(self, data: dict[str, Any], event_name: str) -> None:
        transport_event("inbound", "weflow", event_name, data)
        try:
            self.on_message(data)
        except Exception:
            log.exception("处理 WeFlow 消息失败。")

    @staticmethod
    def _name_from_rows(rows: list[dict[str, Any]], room: str) -> str:
        for row in rows:
            identity = _text(
                row.get("username")
                or row.get("sessionId")
                or row.get("talkerId")
                or row.get("id")
            )
            if identity != room:
                continue
            candidate = _clean_group_name(
                row.get("displayName")
                or row.get("groupName")
                or row.get("nickname")
                or row.get("name")
            )
            if not is_internal_group_name(candidate, room):
                return candidate
        return ""

    def resolve_group_name(
        self,
        data: dict[str, Any],
        *,
        force: bool = False,
        max_age: float = 300.0,
    ) -> tuple[str, str]:
        """Resolve an exact display name without ever returning ``@chatroom``.

        WeFlow's SSE/contact cache can lag immediately after a group is
        created or renamed. Sessions are queried first, contacts second, and
        recent WeChat system rename notices are the final bounded fallback.
        """

        room = _text(data.get("sessionId") or data.get("talkerId"))
        if not room or "@chatroom" not in room:
            return "", "not_group"
        event_name = _clean_group_name(data.get("groupName"))
        if not is_internal_group_name(event_name, room):
            with self._lock:
                self._group_name_cache[room] = (time.monotonic(), event_name, "event")
            return event_name, "event"

        now = time.monotonic()
        with self._lock:
            cached = self._group_name_cache.get(room)
            if not force and cached and now - cached[0] <= max_age:
                return cached[1], cached[2]

        attempts = (
            ("/api/v1/sessions", ("sessions", "data"), "sessions"),
            ("/api/v1/contacts", ("contacts", "data"), "contacts"),
        )
        for path, keys, source_name in attempts:
            try:
                payload = self._get_json(
                    path,
                    params={"keyword": room, "limit": 100},
                )
            except requests.RequestException:
                continue
            candidate = self._name_from_rows(self._rows(payload, *keys), room)
            if candidate:
                with self._lock:
                    self._group_name_cache[room] = (now, candidate, source_name)
                return candidate, source_name

        # The current SSE event may itself be the rename notice.
        candidate = _group_name_from_rename_message(data)
        if not candidate:
            try:
                rows = self.recent_messages(room, limit=100, force=True)
            except requests.RequestException:
                rows = []
            rows.sort(key=_row_timestamp, reverse=True)
            for row in rows:
                candidate = _group_name_from_rename_message(row)
                if candidate:
                    break
        if candidate:
            with self._lock:
                self._group_name_cache[room] = (now, candidate, "rename_notice")
            return candidate, "rename_notice"
        return "", "unresolved"

    def recent_messages(
        self,
        talker: str,
        *,
        limit: int = 50,
        force: bool = False,
        max_age: float = 2.0,
    ) -> list[dict[str, Any]]:
        session_id = _text(talker)
        if not session_id:
            return []
        bounded_limit = max(1, min(200, int(limit)))
        cache_key = f"{session_id}\0{bounded_limit}"
        now = time.monotonic()
        with self._lock:
            cached = self._message_cache.get(cache_key)
            if not force and cached and now - cached[0] <= max_age:
                return [dict(item) for item in cached[1].get("messages", [])]
        payload = self._get_json(
            "/api/v1/messages",
            params={"talker": session_id, "limit": bounded_limit},
        )
        source = payload.get("messages") or payload.get("data") or []
        if not isinstance(source, list):
            source = []
        rows = [dict(item) for item in source if isinstance(item, dict)]
        with self._lock:
            self._message_cache[cache_key] = (now, {"messages": rows})
        return [dict(item) for item in rows]

    def sender_wxid_for_message(
        self,
        talker: str,
        source_message_id: Any,
    ) -> str:
        wanted = _text(source_message_id)
        if not wanted:
            return ""
        for force in (False, True):
            for row in self.recent_messages(talker, limit=100, force=force):
                if wanted not in _message_ids(row):
                    continue
                return _text(
                    row.get("senderUsername") or row.get("sender_username")
                )
        return ""

    def enrich_message_metadata(self, data: dict[str, Any]) -> dict[str, Any]:
        """Merge trusted REST metadata into one sparse SSE event.

        WeFlow's ``message.new`` SSE payload intentionally stays compact and
        does not include ``localType`` or ``senderUsername``.  Its local REST
        message rows do include those fields.  Matching by the platform raw ID
        lets the bridge distinguish WeChat system rows without guessing from
        visible text.  Failure is non-fatal: callers keep the original event.
        """

        enriched = dict(data)
        session_id = _text(data.get("sessionId") or data.get("talkerId"))
        wanted_ids = _message_ids(data)
        if not session_id or not wanted_ids:
            return enriched
        try:
            rows = self.recent_messages(session_id, limit=100)
        except requests.RequestException as exc:
            log.debug("WeFlow 消息元数据核对失败: %s", self._safe_error(exc))
            return enriched
        matches = [row for row in rows if wanted_ids & _message_ids(row)]
        if len(matches) != 1:
            return enriched
        row = matches[0]
        aliases = {
            "localType": ("localType", "local_type", "WCDB_CT_local_type"),
            "senderUsername": (
                "senderUsername",
                "sender_username",
                "senderWxid",
                "sender_wxid",
            ),
            "isSend": ("isSend", "is_send"),
        }
        for target, keys in aliases.items():
            if enriched.get(target) not in (None, ""):
                continue
            for key in keys:
                value = row.get(key)
                if value not in (None, ""):
                    enriched[target] = value
                    break
        enriched["_message_metadata_source"] = "weflow_rest_rawid"
        return enriched

    def resolve_group_sender(
        self,
        data: dict[str, Any],
    ) -> tuple[str, Optional[dict[str, str]], str]:
        """Resolve a stable group member without guessing through duplicates."""

        room = _text(data.get("sessionId") or data.get("talkerId"))
        if not room or "@chatroom" not in room:
            return "", None, "not_group"
        wxid = _text(
            data.get("senderUsername")
            or data.get("sender_username")
            or data.get("senderWxid")
        )
        try:
            members = self.group_members(room)
        except requests.RequestException as exc:
            log.warning("WeFlow 群成员同步失败: %s", self._safe_error(exc))
            return wxid, None, "member_sync_failed"
        if wxid:
            matches = [item for item in members if item["wxid"] == wxid]
            return wxid, (matches[0] if len(matches) == 1 else None), (
                "message_wxid" if len(matches) == 1 else "wxid_profile_missing"
            )

        source_name = _text(
            data.get("senderName")
            or data.get("sender")
            or data.get("sourceName")
            or data.get("talkerName")
        )
        matches = [
            item
            for item in members
            if source_name
            and source_name
            in {
                item["display_name"],
                item["remark"],
                item["group_nickname"],
                item["nickname"],
                item["alias"],
            }
        ]
        if len(matches) == 1:
            return matches[0]["wxid"], matches[0], "unique_name"

        # The common path ends above and stays local after the member list is
        # cached. Only ambiguous/missing names need the slower message lookup.
        source_id = next(iter(_message_ids(data)), "")
        if source_id:
            try:
                wxid = self.sender_wxid_for_message(room, source_id)
            except requests.RequestException:
                wxid = ""
        if wxid:
            exact = [item for item in members if item["wxid"] == wxid]
            return wxid, (exact[0] if len(exact) == 1 else None), (
                "message_lookup" if len(exact) == 1 else "wxid_profile_missing"
            )
        return "", None, "ambiguous_name" if len(matches) > 1 else "name_not_found"

    def _safe_error(self, error: BaseException) -> str:
        message = str(error)
        if self.config.token:
            message = message.replace(self.config.token, "***")
        return message

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def sse_connected(self) -> bool:
        with self._lock:
            return self._connected

    def _set_connected(self, value: bool) -> None:
        with self._lock:
            self._connected = value

    def start(self) -> None:
        sse_alive = bool(self._thread and self._thread.is_alive())
        if not sse_alive:
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="weflow-sse",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self.request_stop()
        self.wait_stopped()

    def wait_stopped(self, timeout: float = 2.0) -> bool:
        deadline = self._monotonic() + max(0.0, float(timeout))
        active: list[str] = []
        for name, thread in (("SSE", self._thread),):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=max(0.0, deadline - self._monotonic()))
                if thread.is_alive():
                    active.append(name)
        if active:
            log.warning(
                "桥接器的 WeFlow %s 线程在 %.1f 秒内未断开，后台线程将自行退出。",
                "、".join(active),
                timeout,
            )
            return False
        return True

    def request_stop(self) -> bool:
        """Signal shutdown without blocking on a streaming HTTP close."""
        was_active = bool(
            self._thread and self._thread.is_alive()
        )
        self._stop.set()
        response = self._response
        if response is not None:
            # ``requests.Response.close()`` may wait for a blocked streaming
            # read on Windows. Running it in the Ctrl+C thread previously left
            # the console silent for several seconds before shutdown progress
            # appeared. Detach the response first so repeated stop requests do
            # not create duplicate closers, then let the daemon close it.
            self._response = None

            def close_response() -> None:
                try:
                    response.close()
                except Exception:
                    pass

            threading.Thread(
                target=close_response,
                name="weflow-sse-close",
                daemon=True,
            ).start()
        return was_active

    def _run(self) -> None:
        endpoint = f"{self.config.url}/api/v1/push/messages"
        while not self._stop.is_set():
            try:
                with self.session.get(
                    endpoint,
                    params=(
                        {"access_token": self.config.token}
                        if self.config.token
                        else None
                    ),
                    headers={
                        "Accept": "text/event-stream",
                        "Cache-Control": "no-cache",
                    },
                    stream=True,
                    timeout=(
                        self.config.connect_timeout,
                        self.config.read_timeout,
                    ),
                ) as response:
                    self._response = response
                    if response.status_code != 200:
                        raise requests.HTTPError(
                            f"WeFlow SSE HTTP {response.status_code}",
                            response=response,
                        )
                    content_type = response.headers.get("Content-Type", "")
                    if "text/event-stream" not in content_type.lower():
                        log.warning("WeFlow 响应 Content-Type 不是 text/event-stream。")
                    self._set_connected(True)
                    self.last_error = ""
                    log.info("WeFlow SSE 已连接：%s", endpoint)
                    # SSE is UTF-8 by specification.  Let ``iter_sse_data`` decode
                    # the raw bytes explicitly; requests otherwise falls back to
                    # ISO-8859-1 when a server omits ``charset=utf-8``, which
                    # corrupts Chinese text even though the payload is valid.
                    lines = response.iter_lines(decode_unicode=False, chunk_size=1)
                    for payload in iter_sse_data(lines, self.config.max_event_bytes):
                        if self._stop.is_set():
                            break
                        try:
                            data = json.loads(payload)
                        except json.JSONDecodeError:
                            log.warning("WeFlow SSE 收到无效 JSON，已忽略。")
                            continue
                        if isinstance(data, dict):
                            self._deliver_message(data, "sse_event_received")
            except requests.RequestException as exc:
                if not self._stop.is_set():
                    self.last_error = self._safe_error(exc)
                    log.warning("WeFlow SSE 连接失败: %s", self.last_error)
            except Exception as exc:
                if not self._stop.is_set():
                    self.last_error = self._safe_error(exc)
                    log.error("WeFlow SSE 异常: %s", self.last_error)
            finally:
                self._response = None
                self._set_connected(False)

            if self._stop.is_set():
                break
            delay = self.config.reconnect + random.uniform(0, min(1.0, self.config.reconnect / 4))
            self._stop.wait(delay)
        log.info("桥接器的 WeFlow SSE 连接已断开。")
