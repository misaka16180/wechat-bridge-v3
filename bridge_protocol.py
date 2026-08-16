"""Small, strict OneBot v11 protocol layer for the v3 bridge."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional


class ProtocolError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


@dataclass(frozen=True)
class WeFlowMessageClassification:
    """Conservative classification of one WeFlow message payload.

    Only explicit protocol fields and narrowly verified WeChat system shapes
    produce ``system``.  ``unknown`` is intentionally forwarded as a user
    message by the bridge so a new WeFlow version cannot silently discard real
    messages merely because it omitted identity metadata.
    """

    kind: str
    reason: str


_WEFLOW_SYSTEM_LOCAL_TYPES = {10000, 10002}
_WEFLOW_SYSTEM_EVENT_NAMES = {
    "message.revoke",
    "message.system",
    "message.notice",
    "system",
    "system.message",
}
_WEFLOW_SYSTEM_TYPE_NAMES = {
    "system",
    "system_message",
    "systemmessage",
    "notice",
    "notification",
}
_WEFLOW_UNKNOWN_SENDER_NAMES = {"未知", "未知发送者", "系统消息"}
_WEFLOW_GROUP_RENAME_PATTERNS = (
    re.compile(r"^你修改群名为[‘“\"](.+?)[’”\"]$"),
    re.compile(r"^.+?修改群名为[‘“\"](.+?)[’”\"]$"),
    re.compile(r"^群聊名称已修改为[‘“\"](.+?)[’”\"]$"),
)


def _normalized_protocol_token(value: Any) -> str:
    return re.sub(r"[\s.-]+", "_", str(value or "").strip().lower())


def classify_weflow_message(data: Any) -> WeFlowMessageClassification:
    """Classify a WeFlow payload without treating message text as authority.

    WeFlow 0.2.x emits explicit ``message.revoke`` events for revocations and
    its REST rows preserve WeChat ``localType`` (10000/10002) for system rows.
    Some versions additionally expose boolean or textual system markers.  A
    rename notice is accepted only when no real sender identity is present;
    therefore a member typing the same sentence remains a user message.
    """

    if not isinstance(data, dict):
        return WeFlowMessageClassification("unknown", "payload_not_object")

    for key in ("isSystem", "is_system", "systemMessage", "system_message"):
        value = data.get(key)
        if value is True:
            return WeFlowMessageClassification("system", f"explicit_flag:{key}")

    event_name = str(data.get("event") or "").strip().lower()
    if event_name in _WEFLOW_SYSTEM_EVENT_NAMES:
        return WeFlowMessageClassification("system", f"event:{event_name}")

    for key in ("localType", "local_type", "WCDB_CT_local_type"):
        value = data.get(key)
        if value in (None, ""):
            continue
        try:
            local_type = int(value)
        except (TypeError, ValueError):
            break
        if local_type in _WEFLOW_SYSTEM_LOCAL_TYPES:
            return WeFlowMessageClassification(
                "system", f"wechat_local_type:{local_type}"
            )
        return WeFlowMessageClassification("user", f"wechat_local_type:{local_type}")

    for key in (
        "messageType",
        "message_type",
        "msgType",
        "msg_type",
        "typeName",
        "type_name",
        "category",
    ):
        value = data.get(key)
        if value in (None, ""):
            continue
        token = _normalized_protocol_token(value)
        if token in _WEFLOW_SYSTEM_TYPE_NAMES:
            return WeFlowMessageClassification("system", f"explicit_type:{key}")

    sender_id = str(
        data.get("senderUsername")
        or data.get("sender_username")
        or data.get("senderWxid")
        or data.get("sender_wxid")
        or ""
    ).strip()
    source_name = str(
        data.get("senderName")
        or data.get("sender")
        or data.get("sourceName")
        or data.get("talkerName")
        or ""
    ).strip()
    if sender_id or (source_name and source_name not in _WEFLOW_UNKNOWN_SENDER_NAMES):
        return WeFlowMessageClassification("user", "sender_identity_present")

    content = str(
        data.get("content")
        or data.get("text")
        or data.get("message")
        or data.get("raw_message")
        or ""
    ).strip()
    if content and any(pattern.fullmatch(content) for pattern in _WEFLOW_GROUP_RENAME_PATTERNS):
        return WeFlowMessageClassification(
            "system", "verified_senderless_group_rename"
        )

    return WeFlowMessageClassification("unknown", "no_reliable_type_or_sender")


def stable_id(namespace: str, value: str) -> int:
    digest = hashlib.sha256(f"{namespace}\0{value}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF


def is_internal_group_name(value: Any, session_id: str = "") -> bool:
    """Return True when a supposed display name is a WeChat chatroom ID.

    Newly-created groups can briefly be emitted by WeFlow with ``groupName``
    equal to ``sessionId`` (for example ``57665029491@chatroom``). Such a
    value is an internal routing key, never a safe WeChat UI search term.
    """

    name = str(value or "").strip()
    session = str(session_id or "").strip()
    if not name:
        return True
    return name.lower().endswith("@chatroom") or bool(session and name == session)


def _first_present(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def _message_source_id(data: dict[str, Any]) -> str:
    value = _first_present(
        data,
        "rawid",
        "rawId",
        "serverIdRaw",
        "serverId",
        "messageId",
        "msgId",
        "id",
    )
    if value not in (None, ""):
        return str(value)
    return json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)


def _quoted_source_id(data: dict[str, Any]) -> Optional[str]:
    value = _first_present(
        data,
        "quotedMessageId",
        "quoteMessageId",
        "replyMessageId",
        "referMessageId",
        "quotedServerIdRaw",
        "quoteServerIdRaw",
        "replyServerIdRaw",
        "referServerIdRaw",
    )
    if value not in (None, ""):
        return str(value)
    for key in ("quote", "quoted", "reply", "refer", "refermsg"):
        nested = data.get(key)
        if not isinstance(nested, dict):
            continue
        nested_value = _first_present(
            nested,
            "rawid",
            "rawId",
            "serverIdRaw",
            "serverId",
            "messageId",
            "msgId",
            "id",
            "svrid",
        )
        if nested_value not in (None, ""):
            return str(nested_value)
    return None


def _message_segments(data: dict[str, Any], text: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    quoted_source_id = _quoted_source_id(data)
    if quoted_source_id:
        segments.append(
            {
                "type": "reply",
                "data": {"id": str(stable_id("message", quoted_source_id))},
            }
        )
    segments.append({"type": "text", "data": {"text": text}})
    return segments


def _bridge_metadata() -> dict[str, Any]:
    """OneBot-compatible extension metadata consumed by the AstrBot plugin."""

    return {
        "platform": "wx",
        "platform_name": "微信个人号",
        "transport": "onebot_v11",
        "adapter": "aiocqhttp",
        "id_semantics": "bridge_generated_stable_mapping",
        "ids_are_qq_numbers": False,
    }


@dataclass
class Contact:
    target_id: int
    kind: str
    name: str
    stable_key: str
    updated_at: float


@dataclass
class GroupMember:
    group_id: int
    member_id: int
    name: str
    updated_at: float
    wxid: str = ""
    display_name: str = ""
    nickname: str = ""
    remark: str = ""
    alias: str = ""
    group_nickname: str = ""

    @property
    def mention_name(self) -> str:
        """Name typed into WeChat's real @ candidate search.

        ``display_name`` is often a local remark (for example ``刘冠英``),
        while WeChat's profile nickname can be different (for example
        ``傷心的鴿子``).  Keep those meanings separate and prefer the actual
        profile nickname for mention lookup.
        """

        return next(
            (
                value
                for value in (
                    self.nickname,
                    self.group_nickname,
                    self.display_name,
                    self.remark,
                    self.alias,
                    self.name,
                    self.wxid,
                )
                if str(value or "").strip()
            ),
            "",
        )

    @property
    def visible_name(self) -> str:
        return next(
            (
                value
                for value in (
                    self.group_nickname,
                    self.display_name,
                    self.remark,
                    self.nickname,
                    self.name,
                    self.wxid,
                )
                if str(value or "").strip()
            ),
            "",
        )


@dataclass
class CachedMessage:
    message_id: int
    event: dict[str, Any]
    updated_at: float


class ContactRegistry:
    """Persist OneBot numeric IDs back to exact WeChat display names."""

    def __init__(self, state_file: str | Path):
        self.state_file = Path(state_file)
        self._lock = threading.RLock()
        self._contacts: dict[int, Contact] = {}
        self._group_members: dict[tuple[int, int], GroupMember] = {}
        self._messages: dict[int, CachedMessage] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        contacts = raw.get("contacts", []) if isinstance(raw, dict) else []
        for item in contacts:
            try:
                contact = Contact(
                    target_id=int(item["target_id"]),
                    kind=str(item["kind"]),
                    name=str(item["name"]),
                    stable_key=str(item["stable_key"]),
                    updated_at=float(item.get("updated_at", 0)),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if contact.kind in {"private", "group"} and contact.name:
                self._contacts[contact.target_id] = contact
        members = raw.get("group_members", []) if isinstance(raw, dict) else []
        for item in members:
            try:
                member = GroupMember(
                    group_id=int(item["group_id"]),
                    member_id=int(item["member_id"]),
                    name=str(item["name"]).strip(),
                    updated_at=float(item.get("updated_at", 0)),
                    wxid=str(item.get("wxid") or "").strip(),
                    display_name=str(item.get("display_name") or "").strip(),
                    nickname=str(item.get("nickname") or "").strip(),
                    remark=str(item.get("remark") or "").strip(),
                    alias=str(item.get("alias") or "").strip(),
                    group_nickname=str(item.get("group_nickname") or "").strip(),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if member.name:
                self._group_members[(member.group_id, member.member_id)] = member
        messages = raw.get("messages", []) if isinstance(raw, dict) else []
        for item in messages:
            try:
                message_id = int(item["message_id"])
                event = item["event"]
                updated_at = float(item.get("updated_at", 0))
            except (KeyError, TypeError, ValueError):
                continue
            if isinstance(event, dict) and event.get("message_type") in {
                "private",
                "group",
            }:
                self._messages[message_id] = CachedMessage(
                    message_id=message_id,
                    event=event,
                    updated_at=updated_at,
                )

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 4,
            "contacts": [asdict(item) for item in self._contacts.values()],
            "group_members": [
                asdict(item) for item in self._group_members.values()
            ],
            "messages": [
                asdict(item)
                for item in sorted(
                    self._messages.values(),
                    key=lambda value: value.updated_at,
                    reverse=True,
                )[:500]
            ],
        }
        temp_path = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(self.state_file)

    def remember(self, kind: str, name: str, stable_key: str) -> Contact:
        if kind not in {"private", "group"} or not name.strip():
            raise ProtocolError("invalid_contact", "会话类型或名称无效。")
        target_id = stable_id(kind, stable_key or name)
        contact = Contact(
            target_id=target_id,
            kind=kind,
            name=name.strip(),
            stable_key=stable_key or name.strip(),
            updated_at=time.time(),
        )
        with self._lock:
            previous = self._contacts.get(target_id)
            self._contacts[target_id] = contact
            changed = previous is None or (
                previous.kind,
                previous.name,
                previous.stable_key,
            ) != (
                contact.kind,
                contact.name,
                contact.stable_key,
            )
            if changed:
                self._save()
        return contact

    def get(self, target_id: Any, expected_kind: Optional[str] = None) -> Contact:
        try:
            numeric = int(target_id)
        except (TypeError, ValueError) as exc:
            raise ProtocolError("invalid_target_id", "OneBot 目标 ID 无效。") from exc
        with self._lock:
            contact = self._contacts.get(numeric)
        if contact is None:
            raise ProtocolError(
                "target_not_mapped",
                "目标 ID 尚未由 WeFlow 入站消息建立映射，拒绝盲目发送。",
            )
        if expected_kind and contact.kind != expected_kind:
            raise ProtocolError(
                "target_kind_mismatch",
                f"目标映射类型为 {contact.kind}，请求类型为 {expected_kind}。",
            )
        return contact

    def list(self, kind: Optional[str] = None) -> list[Contact]:
        with self._lock:
            values = list(self._contacts.values())
        if kind:
            values = [item for item in values if item.kind == kind]
        return sorted(values, key=lambda item: item.updated_at, reverse=True)

    def remember_group_member(
        self,
        group_id: Any,
        member_id: Any,
        name: str,
        *,
        wxid: str = "",
        display_name: str = "",
        nickname: str = "",
        remark: str = "",
        alias: str = "",
        group_nickname: str = "",
    ) -> GroupMember:
        try:
            numeric_group_id = int(group_id)
            numeric_member_id = int(member_id)
        except (TypeError, ValueError) as exc:
            raise ProtocolError("invalid_group_member", "群聊或成员 ID 无效。") from exc
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ProtocolError("invalid_group_member", "群成员昵称不能为空。")
        member = GroupMember(
            group_id=numeric_group_id,
            member_id=numeric_member_id,
            name=normalized_name,
            updated_at=time.time(),
            wxid=str(wxid or "").strip(),
            display_name=str(display_name or "").strip(),
            nickname=str(nickname or "").strip(),
            remark=str(remark or "").strip(),
            alias=str(alias or "").strip(),
            group_nickname=str(group_nickname or "").strip(),
        )
        key = (member.group_id, member.member_id)
        with self._lock:
            previous = self._group_members.get(key)
            self._group_members[key] = member
            previous_identity = (
                None
                if previous is None
                else (
                    previous.name,
                    previous.wxid,
                    previous.display_name,
                    previous.nickname,
                    previous.remark,
                    previous.alias,
                    previous.group_nickname,
                )
            )
            member_identity = (
                member.name,
                member.wxid,
                member.display_name,
                member.nickname,
                member.remark,
                member.alias,
                member.group_nickname,
            )
            if previous_identity != member_identity:
                self._save()
        return member

    def find_group_member_by_wxid(self, group_id: Any, wxid: str) -> GroupMember:
        try:
            numeric_group_id = int(group_id)
        except (TypeError, ValueError) as exc:
            raise ProtocolError("invalid_group_member", "群聊 ID 无效。") from exc
        normalized_wxid = str(wxid or "").strip()
        if not normalized_wxid:
            raise ProtocolError("invalid_group_member", "群成员 wxid 不能为空。")
        with self._lock:
            candidates = [
                member
                for member in self._group_members.values()
                if member.group_id == numeric_group_id
                and member.wxid == normalized_wxid
            ]
        if not candidates:
            raise ProtocolError(
                "group_member_not_mapped",
                "该群成员 wxid 尚未同步到桥接器。",
            )
        return max(candidates, key=lambda member: member.updated_at)

    def remember_group_members(
        self,
        group_id: Any,
        session_id: str,
        profiles: list[dict[str, str]],
    ) -> list[GroupMember]:
        """Batch a WeFlow member refresh into one atomic state-file write."""

        try:
            numeric_group_id = int(group_id)
        except (TypeError, ValueError) as exc:
            raise ProtocolError("invalid_group_member", "群聊 ID 无效。") from exc
        stable_session_id = str(session_id or "").strip()
        if not stable_session_id:
            raise ProtocolError("invalid_group_member", "群聊 sessionId 不能为空。")

        now = time.time()
        members: list[GroupMember] = []
        for profile in profiles:
            wxid = str(profile.get("wxid") or "").strip()
            if not wxid:
                continue
            visible_name = str(
                profile.get("group_nickname")
                or profile.get("display_name")
                or profile.get("remark")
                or profile.get("nickname")
                or wxid
            ).strip()
            members.append(
                GroupMember(
                    group_id=numeric_group_id,
                    member_id=stable_id(
                        "group-user", f"{stable_session_id}\0{wxid}"
                    ),
                    name=visible_name,
                    updated_at=now,
                    wxid=wxid,
                    display_name=str(profile.get("display_name") or "").strip(),
                    nickname=str(profile.get("nickname") or "").strip(),
                    remark=str(profile.get("remark") or "").strip(),
                    alias=str(profile.get("alias") or "").strip(),
                    group_nickname=str(
                        profile.get("group_nickname") or ""
                    ).strip(),
                )
            )

        changed = False
        identity_fields = (
            "name",
            "wxid",
            "display_name",
            "nickname",
            "remark",
            "alias",
            "group_nickname",
        )
        with self._lock:
            for member in members:
                key = (member.group_id, member.member_id)
                previous = self._group_members.get(key)
                self._group_members[key] = member
                if previous is None or any(
                    getattr(previous, field) != getattr(member, field)
                    for field in identity_fields
                ):
                    changed = True
            if changed:
                self._save()
        return members

    def get_group_member(self, group_id: Any, member_id: Any) -> GroupMember:
        try:
            key = (int(group_id), int(member_id))
        except (TypeError, ValueError) as exc:
            raise ProtocolError("invalid_group_member", "群聊或成员 ID 无效。") from exc
        with self._lock:
            member = self._group_members.get(key)
        if member is None:
            raise ProtocolError(
                "group_member_not_mapped",
                "该 OneBot 群成员 ID 尚未从 WeFlow 入站消息建立微信昵称映射；未执行发送。",
            )
        return member

    def find_group_member(self, member_id: Any) -> GroupMember:
        try:
            numeric_member_id = int(member_id)
        except (TypeError, ValueError) as exc:
            raise ProtocolError("invalid_group_member", "群成员 ID 无效。") from exc
        with self._lock:
            candidates = [
                member
                for member in self._group_members.values()
                if member.member_id == numeric_member_id
            ]
        if not candidates:
            raise ProtocolError(
                "group_member_not_mapped",
                "该 OneBot 群成员 ID 尚未从 WeFlow 入站消息建立微信昵称映射。",
            )
        return max(candidates, key=lambda member: member.updated_at)

    def list_group_members(self, group_id: Any) -> list[GroupMember]:
        try:
            numeric_group_id = int(group_id)
        except (TypeError, ValueError) as exc:
            raise ProtocolError("invalid_group", "群聊 ID 无效。") from exc
        with self._lock:
            values = [
                member
                for member in self._group_members.values()
                if member.group_id == numeric_group_id
            ]
        # Version 2/3 state files may contain a display-name-derived legacy ID
        # next to the new wxid-derived stable ID.  Keep the legacy record
        # queryable for in-flight OneBot actions but suppress it from lists.
        preferred_wxids = {member.wxid for member in values if member.wxid}
        filtered = [
            member
            for member in values
            if member.wxid
            or not any(
                candidate.wxid in preferred_wxids
                and candidate.visible_name == member.visible_name
                for candidate in values
            )
        ]
        return sorted(
            filtered,
            key=lambda member: (member.visible_name, member.member_id),
        )

    def remember_message(self, event: dict[str, Any]) -> None:
        try:
            message_id = int(event["message_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProtocolError("invalid_message_id", "消息 ID 无效。") from exc
        if event.get("message_type") not in {"private", "group"}:
            raise ProtocolError("invalid_message", "只缓存私聊或群聊消息。")
        cached = CachedMessage(
            message_id=message_id,
            event=copy.deepcopy(event),
            updated_at=time.time(),
        )
        with self._lock:
            self._messages[message_id] = cached
            if len(self._messages) > 500:
                oldest = min(
                    self._messages.values(),
                    key=lambda value: value.updated_at,
                )
                self._messages.pop(oldest.message_id, None)
            self._save()

    def get_message(self, message_id: Any) -> dict[str, Any]:
        try:
            numeric_message_id = int(message_id)
        except (TypeError, ValueError) as exc:
            raise ProtocolError("invalid_message_id", "消息 ID 无效。") from exc
        with self._lock:
            cached = self._messages.get(numeric_message_id)
        if cached is None:
            raise ProtocolError(
                "message_not_found",
                "引用消息不在桥接器最近 500 条消息缓存中。",
            )
        return copy.deepcopy(cached.event)


def ok_response(data: Any = None, *, echo: Any = None) -> dict[str, Any]:
    response = {
        "status": "ok",
        "retcode": 0,
        "data": data,
        "message": "",
        "wording": "",
    }
    if echo is not None:
        response["echo"] = echo
    return response


def failed_response(
    message: str,
    *,
    echo: Any = None,
    retcode: int = 100,
    code: str = "failed",
) -> dict[str, Any]:
    response = {
        "status": "failed",
        "retcode": retcode,
        "data": {"code": code},
        "message": message,
        "wording": message,
    }
    if echo is not None:
        response["echo"] = echo
    return response


def weflow_contact(data: dict[str, Any]) -> tuple[str, str, str]:
    source_name = str(
        data.get("sourceName") or data.get("talkerName") or "未知"
    ).strip()
    session_id = str(data.get("sessionId") or data.get("talkerId") or source_name)
    group_name = re.sub(
        r"\s*\(\d+\)\s*$",
        "",
        str(data.get("groupName") or "").strip(),
    )
    is_group = (
        str(data.get("sessionType") or "").lower() == "group"
        or bool(group_name)
        or "@chatroom" in session_id
    )
    if is_group:
        return "group", (group_name or source_name), session_id
    return "private", source_name, session_id


def build_weflow_event(
    data: dict[str, Any],
    *,
    self_id: int,
    registry: ContactRegistry,
    bot_names: tuple[str, ...] = (),
    bot_wxid: str = "",
    group_trigger: str = "all",
    sender_wxid: str = "",
    sender_profile: Optional[dict[str, str]] = None,
    sender_resolution: str = "unresolved",
) -> Optional[dict[str, Any]]:
    if not isinstance(data, dict):
        return None
    if classify_weflow_message(data).kind == "system":
        return None
    if data.get("isSelf") is True or data.get("is_self") is True:
        return None
    if bot_wxid and str(data.get("talkerId") or "") == bot_wxid:
        return None
    source_name = str(data.get("sourceName") or data.get("talkerName") or "")
    if source_name and source_name in bot_names:
        return None

    content = str(data.get("content") or "").strip()
    if not content:
        return None

    kind, target_name, session_id = weflow_contact(data)
    contact = registry.remember(kind, target_name, session_id)
    timestamp = data.get("timestamp") or data.get("time") or time.time()
    try:
        timestamp_value = float(timestamp)
        if timestamp_value > 1e12:
            timestamp_value /= 1000
    except (TypeError, ValueError):
        timestamp_value = time.time()

    message_id = stable_id("message", _message_source_id(data))
    base_message = _message_segments(data, content)

    if kind == "private":
        return {
            "time": int(timestamp_value),
            "self_id": self_id,
            "post_type": "message",
            "bridge_metadata": _bridge_metadata(),
            "message_type": "private",
            "sub_type": "friend",
            "user_id": contact.target_id,
            "message_id": message_id,
            "message": base_message,
            "raw_message": content,
            "font": 0,
            "sender": {
                "user_id": contact.target_id,
                "nickname": source_name or target_name,
                "card": "",
                "sex": "unknown",
                "age": 0,
            },
        }

    mentioned = [name for name in bot_names if f"@{name}" in content]
    explicit_mention = any(
        data.get(key) is True
        for key in ("isMentioned", "isAtMe", "mentionedMe", "atMe")
    )
    was_mentioned = bool(mentioned) or explicit_mention
    if group_trigger == "mention" and not was_mentioned:
        return None
    cleaned = content
    for name in mentioned:
        cleaned = cleaned.replace(f"@{name}", "").strip()
    sender_name = str(
        data.get("senderName") or data.get("sender") or source_name or "未知"
    ).strip()
    profile = {
        "wxid": str((sender_profile or {}).get("wxid") or sender_wxid or "").strip(),
        "display_name": str((sender_profile or {}).get("display_name") or "").strip(),
        "nickname": str((sender_profile or {}).get("nickname") or "").strip(),
        "remark": str((sender_profile or {}).get("remark") or "").strip(),
        "alias": str((sender_profile or {}).get("alias") or "").strip(),
        "group_nickname": str(
            (sender_profile or {}).get("group_nickname") or ""
        ).strip(),
    }
    # Keep old behavior only when WeFlow did not provide an identity. Once a
    # wxid is known, the numeric OneBot ID must remain stable across nickname
    # and remark changes.
    stable_member_key = profile["wxid"] or sender_name
    user_id = stable_id("group-user", f"{session_id}\0{stable_member_key}")
    visible_name = (
        profile["group_nickname"]
        or profile["display_name"]
        or profile["remark"]
        or sender_name
        or profile["nickname"]
        or "未知"
    )
    if sender_name and sender_name != "未知":
        registry.remember_group_member(
            contact.target_id,
            user_id,
            visible_name,
            wxid=profile["wxid"],
            display_name=profile["display_name"] or sender_name,
            nickname=profile["nickname"],
            remark=profile["remark"],
            alias=profile["alias"],
            group_nickname=profile["group_nickname"],
        )
    message = []
    if base_message and base_message[0].get("type") == "reply":
        message.append(base_message[0])
    if was_mentioned:
        message.append({"type": "at", "data": {"qq": str(self_id)}})
    if cleaned:
        message.append({"type": "text", "data": {"text": cleaned}})
    return {
        "time": int(timestamp_value),
        "self_id": self_id,
        "post_type": "message",
        "bridge_metadata": _bridge_metadata(),
        "message_type": "group",
        "sub_type": "normal",
        "group_id": contact.target_id,
        "group_name": target_name,
        "user_id": user_id,
        "message_id": message_id,
        "message": message,
        "raw_message": content,
        "font": 0,
        "sender": {
            "user_id": user_id,
            "nickname": visible_name,
            "card": profile["group_nickname"] or profile["remark"] or sender_name,
            "sex": "unknown",
            "age": 0,
            "role": "member",
        },
        "bridge_member": {
            **profile,
            "source_name": sender_name,
            "resolution": sender_resolution,
            "stable_id": user_id,
            "real_mention_available": bool(profile["wxid"] and profile["nickname"]),
        },
    }


@dataclass(frozen=True)
class OutboundSegment:
    type: str
    data: dict[str, Any]


@dataclass(frozen=True)
class OutboundMessage:
    kind: str
    name: str
    target_id: int
    segments: tuple[OutboundSegment, ...]

    @property
    def text(self) -> str:
        return "".join(
            str(segment.data.get("text") or "")
            for segment in self.segments
            if segment.type == "text"
        )


def _outbound_kind(action: str, params: dict[str, Any]) -> str:
    if action == "send_group_msg":
        return "group"
    if action == "send_private_msg":
        return "private"
    declared = str(params.get("message_type") or "").lower()
    return "group" if declared == "group" or "group_id" in params else "private"


def parse_outbound_message(
    action: str,
    params: dict[str, Any],
    registry: ContactRegistry,
) -> OutboundMessage:
    if action not in {"send_msg", "send_private_msg", "send_group_msg"}:
        raise ProtocolError("unsupported_action", f"不支持 OneBot 动作: {action}")

    kind = _outbound_kind(action, params)

    id_key = "group_id" if kind == "group" else "user_id"
    contact = registry.get(params.get(id_key), expected_kind=kind)
    if kind == "group" and is_internal_group_name(contact.name, contact.stable_key):
        raise ProtocolError(
            "group_name_unresolved",
            "WeFlow 尚未同步到该群的真实群名；已拒绝使用 @chatroom 内部 ID 搜索微信。"
            "请等待群资料同步或在群内产生一条新消息后重试。",
        )
    message = params.get("message", "")
    if isinstance(message, str):
        segments = (OutboundSegment("text", {"text": message}),)
    elif isinstance(message, list):
        parsed: list[OutboundSegment] = []
        unsupported: list[str] = []
        for segment in message:
            if not isinstance(segment, dict):
                unsupported.append(type(segment).__name__)
                continue
            segment_type = str(segment.get("type") or "")
            if segment_type == "text":
                data = segment.get("data") or {}
                parsed.append(OutboundSegment("text", {"text": str(data.get("text") or "")}))
            elif segment_type in {"image", "file"}:
                data = segment.get("data") or {}
                if not isinstance(data, dict):
                    unsupported.append(segment_type)
                    continue
                parsed.append(OutboundSegment(segment_type, dict(data)))
            elif segment_type == "at":
                if kind != "group":
                    raise ProtocolError(
                        "at_requires_group",
                        "OneBot at 消息段只能发送到群聊。",
                    )
                data = segment.get("data") or {}
                if not isinstance(data, dict):
                    unsupported.append(segment_type)
                    continue
                member = registry.get_group_member(
                    contact.target_id,
                    data.get("qq"),
                )
                parsed.append(
                    OutboundSegment(
                        "at",
                        {
                            "qq": str(member.member_id),
                            "name": member.name,
                            "mention_name": member.mention_name,
                            "wxid": member.wxid,
                            "real_mention_available": bool(
                                member.wxid and member.nickname
                            ),
                        },
                    )
                )
            elif segment_type not in {"reply"}:
                unsupported.append(segment_type or "unknown")
        if unsupported:
            raise ProtocolError(
                "unsupported_message_segment",
                "当前高精度发送器支持文本、@、图片和文件；收到不支持的消息段: "
                + ", ".join(sorted(set(unsupported))),
            )
        segments = tuple(parsed)
    else:
        raise ProtocolError("invalid_message", "OneBot message 必须是字符串或消息段数组。")
    if not segments or not any(
        segment.type == "text" and str(segment.data.get("text") or "").strip()
        or segment.type in {"at", "image", "file"}
        for segment in segments
    ):
        raise ProtocolError("empty_message", "没有可发送的文本或媒体内容。")
    return OutboundMessage(
        kind=kind,
        name=contact.name,
        target_id=contact.target_id,
        segments=segments,
    )


def parse_outbound_text(
    action: str,
    params: dict[str, Any],
    registry: ContactRegistry,
) -> OutboundMessage:
    """Backward-compatible text-only parser name.

    The bridge now parses image/file segments too; callers that require a
    text-only request can inspect the returned segments and reject media.
    """

    outbound = parse_outbound_message(action, params, registry)
    unsupported = [segment.type for segment in outbound.segments if segment.type not in {"text"}]
    if unsupported:
        raise ProtocolError(
            "unsupported_message_segment",
            "当前调用方仅支持纯文本；收到不支持的消息段: "
            + ", ".join(sorted(set(unsupported))),
        )
    text = outbound.text
    if not text.strip():
        raise ProtocolError("empty_text", "没有可发送的文本内容。")
    return outbound
