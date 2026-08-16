"""Reliable v3 launcher.

This module intentionally imports only the Python standard library at module
load time.  Application and third-party imports happen inside the exception
boundary, so even an import failure is printed before the console is retained.
"""

from __future__ import annotations

import importlib
import json
import sys
import traceback
from collections.abc import Callable


def _console_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")


def _load_application() -> Callable[[], int]:
    module = importlib.import_module("wechat_bridge_v3")
    application = getattr(module, "main", None)
    if not callable(application):
        raise RuntimeError("wechat_bridge_v3.py 缺少可调用的 main() 入口。")
    return application


def execute(loader: Callable[[], Callable[[], int]] = _load_application) -> int:
    """Run the application and turn every startup failure into visible output."""

    try:
        application = loader()
        result = application()
        return int(result or 0)
    except SystemExit as exc:
        if exc.code is None:
            return 0
        if isinstance(exc.code, int):
            return exc.code
        print(str(exc.code), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已收到 Ctrl+C，程序停止。")
        return 130
    except BaseException as exc:
        print(
            f"\n启动或运行失败：{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        details = getattr(exc, "details", None)
        if details:
            print(
                "诊断信息："
                + json.dumps(details, ensure_ascii=False, default=str, sort_keys=True),
                file=sys.stderr,
            )
        traceback.print_exc()
        return 1


def wait_for_exit() -> None:
    try:
        input("\n按 Enter 关闭此窗口……")
    except (EOFError, KeyboardInterrupt):
        pass


def consume_launcher_wait_flag(argv: list[str]) -> bool:
    """Remove the launcher's private flag before application argparse runs."""

    wait = True
    forwarded = [argv[0]] if argv else []
    for argument in argv[1:]:
        if argument == "--no-wait":
            wait = False
        else:
            forwarded.append(argument)
    argv[:] = forwarded
    return wait


def main(*, wait: bool = True) -> int:
    _console_utf8()
    exit_code = execute()
    if exit_code:
        print(f"\n程序退出码：{exit_code}", file=sys.stderr)
    if wait:
        wait_for_exit()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(wait=consume_launcher_wait_flag(sys.argv)))
