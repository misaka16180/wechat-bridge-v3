"""Native Windows clipboard payloads used by visible v3 automation."""

from __future__ import annotations

import ctypes
import io
import struct
import time
from pathlib import Path
from typing import Any, Sequence


class ClipboardError(RuntimeError):
    pass


class Win32Clipboard:
    """Write text, an image, or file paths without interacting with WeChat internals."""

    def __init__(self, *, retry_count: int = 5, retry_delay: float = 0.1) -> None:
        self.retry_count = max(1, int(retry_count))
        self.retry_delay = max(0.0, float(retry_delay))

    def _open(self, clipboard: Any) -> None:
        last_error: BaseException | None = None
        for _ in range(self.retry_count):
            try:
                clipboard.OpenClipboard()
                return
            except BaseException as exc:
                last_error = exc
                time.sleep(self.retry_delay)
        raise ClipboardError(f"无法打开 Windows 剪贴板：{last_error}")

    def set_text(self, text: str) -> None:
        try:
            import win32clipboard
        except ImportError as exc:
            raise ClipboardError("文字粘贴需要 pywin32。") from exc
        try:
            self._open(win32clipboard)
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(
                    win32clipboard.CF_UNICODETEXT,
                    str(text),
                )
            finally:
                win32clipboard.CloseClipboard()
        except ClipboardError:
            raise
        except BaseException as exc:
            try:
                win32clipboard.CloseClipboard()
            except BaseException:
                pass
            raise ClipboardError(f"无法把文字写入 Windows 剪贴板：{exc}") from exc

    @staticmethod
    def image_to_dib(path: str | Path) -> bytes:
        try:
            from PIL import Image
        except ImportError as exc:
            raise ClipboardError("图片发送需要 Pillow。") from exc
        with Image.open(path) as image:
            image.seek(0)
            output = io.BytesIO()
            image.convert("RGB").save(output, format="BMP")
            # CF_DIB excludes the 14-byte BMP file header.
            return output.getvalue()[14:]

    def set_image(self, path: str | Path) -> None:
        try:
            import win32clipboard
        except ImportError as exc:
            raise ClipboardError("图片发送需要 pywin32。") from exc
        try:
            dib = self.image_to_dib(path)
            self._open(win32clipboard)
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_DIB, dib)
            finally:
                win32clipboard.CloseClipboard()
            if not win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_DIB):
                raise ClipboardError("Windows 剪贴板未保留 CF_DIB 图片数据。")
        except ClipboardError:
            raise
        except BaseException as exc:
            try:
                win32clipboard.CloseClipboard()
            except BaseException:
                pass
            raise ClipboardError(f"无法把图片写入 Windows 剪贴板：{exc}") from exc

    @staticmethod
    def dropfiles_blob(paths: Sequence[str | Path]) -> bytes:
        normalized = [str(Path(path).resolve()) for path in paths]
        payload = ("\0".join(normalized) + "\0\0").encode("utf-16-le")
        return struct.pack("<IiiII", 20, 0, 0, 0, 1) + payload

    def set_files(self, paths: Sequence[str | Path]) -> None:
        try:
            import win32clipboard
            import win32con
        except ImportError as exc:
            raise ClipboardError("文件发送需要 pywin32。") from exc
        normalized = [str(Path(path).resolve()) for path in paths]
        if not normalized or any(not Path(path).is_file() for path in normalized):
            raise ClipboardError("要粘贴的文件不存在。")
        blob = self.dropfiles_blob(normalized)
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p
        handle = kernel32.GlobalAlloc(0x0042, len(blob))
        if not handle:
            raise ClipboardError("无法分配文件剪贴板内存。")
        try:
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                raise ClipboardError("无法锁定文件剪贴板内存。")
            ctypes.memmove(pointer, blob, len(blob))
            kernel32.GlobalUnlock(handle)
            self._open(win32clipboard)
            try:
                win32clipboard.EmptyClipboard()
                if not user32.SetClipboardData(win32con.CF_HDROP, handle):
                    raise ClipboardError("Windows 未接受文件剪贴板数据。")
                handle = None
            finally:
                win32clipboard.CloseClipboard()
            if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_HDROP):
                raise ClipboardError("Windows 剪贴板未保留 CF_HDROP 文件数据。")
        finally:
            if handle:
                kernel32.GlobalFree(handle)


__all__ = ["ClipboardError", "Win32Clipboard"]
