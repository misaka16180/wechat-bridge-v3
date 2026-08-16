"""Visible Windows desktop automation primitives for WeChat Bridge v3.

The package deliberately contains no private protocol, process injection, memory
access, packet capture, or database access.  Importing it never moves the mouse,
presses a key, focuses WeChat, or sends a message.
"""

from .interaction import (
    BoundedListScroller,
    InteractionPolicy,
    RandomizedInteraction,
    ScrollAction,
)
from .models import CapturedFrame, Point, Rect, WindowSnapshot
from .navigation import NoSendSearchNavigator, SearchNavigationResult
from .session import WeChatWindowSession

__all__ = [
    "BoundedListScroller",
    "CapturedFrame",
    "InteractionPolicy",
    "NoSendSearchNavigator",
    "Point",
    "RandomizedInteraction",
    "Rect",
    "ScrollAction",
    "SearchNavigationResult",
    "WeChatWindowSession",
    "WindowSnapshot",
]
