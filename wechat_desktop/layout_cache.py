"""Small process-local cache for verified WeChat client layout results.

Only client-relative rectangles are cached.  The current client origin is
always read from a fresh window snapshot, so moving the window does not make a
cached click use stale screen coordinates.  Geometry, DPI, process, handle,
class or theme changes create a different key and therefore require a fresh
full-image location.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass

from .models import WindowSnapshot
from .relative_locator import RelativeLocatorResult


@dataclass(frozen=True)
class LayoutCacheKey:
    handle: int
    process_id: int
    client_width: int
    client_height: int
    dpi: int
    class_name: str
    theme: str


@dataclass(frozen=True)
class LayoutCacheLookup:
    key: LayoutCacheKey
    status: str
    search_box: RelativeLocatorResult | None = None
    chat_input: RelativeLocatorResult | None = None


@dataclass
class _LayoutCacheEntry:
    key: LayoutCacheKey
    client_left: int
    client_top: int
    search_box: RelativeLocatorResult | None = None
    chat_input: RelativeLocatorResult | None = None


class LayoutCacheStore:
    """Thread-safe bounded cache shared by successive send transactions."""

    VALID_SLOTS = frozenset({"search_box", "chat_input"})

    def __init__(self, *, max_entries: int = 8) -> None:
        if max_entries < 1:
            raise ValueError("布局缓存至少需要保留一项。")
        self.max_entries = int(max_entries)
        self._entries: OrderedDict[LayoutCacheKey, _LayoutCacheEntry] = OrderedDict()
        self._lock = threading.RLock()

    @staticmethod
    def key_for(snapshot: WindowSnapshot, *, theme: str = "light") -> LayoutCacheKey:
        return LayoutCacheKey(
            handle=int(snapshot.handle),
            process_id=int(snapshot.process_id),
            client_width=int(snapshot.client_rect.width),
            client_height=int(snapshot.client_rect.height),
            dpi=int(snapshot.dpi),
            class_name=str(snapshot.class_name),
            theme=str(theme),
        )

    def open(self, snapshot: WindowSnapshot, *, theme: str = "light") -> LayoutCacheLookup:
        key = self.key_for(snapshot, theme=theme)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                moved = (
                    entry.client_left != snapshot.client_rect.left
                    or entry.client_top != snapshot.client_rect.top
                )
                entry.client_left = snapshot.client_rect.left
                entry.client_top = snapshot.client_rect.top
                self._entries.move_to_end(key)
                return LayoutCacheLookup(
                    key,
                    "moved" if moved else "hit",
                    entry.search_box,
                    entry.chat_input,
                )

            related = [
                old_key
                for old_key in self._entries
                if old_key.handle == key.handle or old_key.process_id == key.process_id
            ]
            status = "miss"
            if related:
                same_identity = any(
                    old_key.handle == key.handle
                    and old_key.process_id == key.process_id
                    and old_key.class_name == key.class_name
                    and old_key.theme == key.theme
                    for old_key in related
                )
                status = "invalidated_geometry" if same_identity else "invalidated_identity"
                for old_key in related:
                    self._entries.pop(old_key, None)

            self._entries[key] = _LayoutCacheEntry(
                key=key,
                client_left=snapshot.client_rect.left,
                client_top=snapshot.client_rect.top,
            )
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
            return LayoutCacheLookup(key, status)

    def put(
        self,
        key: LayoutCacheKey,
        slot: str,
        result: RelativeLocatorResult,
    ) -> None:
        if slot not in self.VALID_SLOTS:
            raise ValueError(f"未知布局缓存位置：{slot}")
        if not result.accepted or result.click_bounds is None:
            return
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            setattr(entry, slot, result)
            self._entries.move_to_end(key)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


GLOBAL_LAYOUT_CACHE = LayoutCacheStore()


__all__ = [
    "GLOBAL_LAYOUT_CACHE",
    "LayoutCacheKey",
    "LayoutCacheLookup",
    "LayoutCacheStore",
]
