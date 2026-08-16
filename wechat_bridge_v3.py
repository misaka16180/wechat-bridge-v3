"""Command-line entry point for the isolated v3 bridge and desktop console."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
import webbrowser
from pathlib import Path

from bridge_config import (
    ConfigError,
    create_default_config,
    ensure_initial_console_password,
    load_config,
)
from bridge_console import BridgeConsole
from bridge_logging import configure_logging
from bridge_service import BridgeService
from v3_version import VERSION


def _console_url(config) -> str:
    scheme = "https" if config.console.tls_cert else "http"
    host = config.console.host
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"{scheme}://{host}:{config.console.port}/"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="微信桥接 v3：WeFlow、AstrBot 和可见桌面自动化控制台。"
    )
    parser.add_argument(
        "--config",
        default="bridge_config.json",
        help="v3 JSON 配置路径，默认使用本目录 bridge_config.json。",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只校验 v3 配置和安全边界，不启动控制台或连接。",
    )
    parser.add_argument(
        "--open-browser",
        dest="open_browser",
        action="store_true",
        default=None,
        help="启动后打开本机控制台页面。",
    )
    parser.add_argument(
        "--no-open-browser",
        dest="open_browser",
        action="store_false",
        help="启动后不自动打开浏览器。",
    )
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--version", action="version", version=VERSION)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config_path = Path(args.config)
    if not config_path.exists():
        try:
            create_default_config(config_path)
        except OSError as exc:
            print(f"无法创建 v3 初始配置 {config_path}: {exc}", file=sys.stderr)
            return 2

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"v3 配置错误: {exc}", file=sys.stderr)
        return 2

    try:
        initial_credentials = ensure_initial_console_password(config_path)
    except (ConfigError, OSError) as exc:
        print(f"无法准备首次登录凭据: {exc}", file=sys.stderr)
        return 2
    if initial_credentials is not None:
        username, password = initial_credentials
        config = load_config(config_path)
        print()
        print("v3 首次登录信息（完成首次密码修改前，每次启动都会显示）：")
        print(f"  配置文件: {config_path.resolve()}")
        print(f"  控制台:   {_console_url(config)}")
        print(f"  用户名:   {username}")
        print(f"  初始密码: {password}")
        print("首次登录后必须设置新密码。")
        print()

    warnings = config.security_warnings()
    if args.check:
        print(f"v3 配置检查通过，安全警告 {len(warnings)} 项。")
        return 0

    log_paths = configure_logging(
        config.logging.directory,
        level=config.logging.level,
        max_bytes=config.logging.max_bytes,
        backup_count=config.logging.backup_count,
    )
    logger = logging.getLogger("wechat_bridge.main")
    logger.info(
        "v3 日志已启用: bridge=%s transport=%s",
        log_paths["bridge"],
        log_paths["transport"],
    )
    for warning in warnings:
        logging.getLogger("wechat_bridge.security").warning(warning)

    service = BridgeService(config)
    console = BridgeConsole(config.console, service, config_path=config_path)
    stop_event = threading.Event()
    stop_notice_lock = threading.Lock()
    stop_notice_printed = False

    def request_stop(signum=None, *_args):
        nonlocal stop_notice_printed
        with stop_notice_lock:
            first = not stop_notice_printed
            stop_notice_printed = True
        if first:
            signal_name = {
                getattr(signal, "SIGINT", None): "Ctrl+C / SIGINT",
                getattr(signal, "SIGTERM", None): "SIGTERM",
            }.get(signum, "中断请求")
            print(f"\n已捕获 {signal_name}，正在安全退出，请稍候…", flush=True)
        else:
            print("\n退出流程正在进行，请稍候…", flush=True)
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, request_stop)
        except (ValueError, OSError):
            pass

    exit_code = 0
    try:
        service.start()
        console.start()
        url = _console_url(config)
        print(f"微信桥接 v3 已启动，电脑网页控制台: {url}", flush=True)
        print("按 Ctrl+C 可安全停止。", flush=True)
        should_open = config.console.auto_open_browser if args.open_browser is None else args.open_browser
        if should_open:
            webbrowser.open(url)
        while not stop_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        request_stop(signal.SIGINT)
    except OSError as exc:
        exit_code = 1
        logger.error("v3 启动失败: %s", exc)
        print(f"v3 启动失败: {exc}", file=sys.stderr)
        print(f"详细日志: {log_paths['bridge']}", file=sys.stderr)
    finally:
        shutdown_started = time.monotonic()
        if stop_event.is_set():
            print("正在停止电脑网页控制台…", flush=True)
        console.stop()
        service.stop_automation()
        service.stop(
            progress=(
                lambda message: print(message, flush=True)
                if stop_event.is_set()
                else None
            )
        )
        if stop_event.is_set():
            elapsed = time.monotonic() - shutdown_started
            print(f"微信桥接 v3 已安全退出（耗时 {elapsed:.1f} 秒）。", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
