"""Compatibility shim proving that v3 does not use Qt/UIA hot activation."""

QT_HOT_ACTIVATION_NOTICE_VERSION = "v3-not-applicable"


def compatibility_hint() -> dict[str, object]:
    return {
        "available": False,
        "reason": "v3 使用截图、Win32 窗口元数据和普通鼠标键盘，不启用 Qt/UIA 热激活。",
        "memory_modified": False,
    }


__all__ = ["QT_HOT_ACTIVATION_NOTICE_VERSION", "compatibility_hint"]
