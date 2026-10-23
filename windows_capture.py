"""Explicit, single-window WeChat capture; no hooks, polling, or message sending."""
import base64
import ctypes
from dataclasses import dataclass
import io
import ntpath
import sys

from PIL import Image, ImageGrab


class CaptureError(Exception):
    pass


@dataclass(frozen=True)
class Window:
    handle: int
    pid: int
    title: str


class WindowsAPI:
    def __init__(self):
        if sys.platform != "win32":
            raise CaptureError("微信窗口采集仅支持 Windows 10/11；仍可导入复制的文字。")
        from ctypes import wintypes as w
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        signatures = [
            (self.user.EnumWindows, [self.callback_type, w.LPARAM], w.BOOL),
            (self.user.IsWindow, [w.HWND], w.BOOL),
            (self.user.IsWindowVisible, [w.HWND], w.BOOL),
            (self.user.IsIconic, [w.HWND], w.BOOL),
            (self.user.GetWindowTextLengthW, [w.HWND], ctypes.c_int),
            (self.user.GetWindowTextW, [w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
            (self.user.GetWindowThreadProcessId, [w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
            (self.kernel.OpenProcess, [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            (self.kernel.QueryFullProcessImageNameW, [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)], w.BOOL),
            (self.kernel.CloseHandle, [w.HANDLE], w.BOOL),
        ]
        for function, args, result in signatures:
            function.argtypes, function.restype = args, result

    def identify(self, handle):
        from ctypes import wintypes as w
        if not self.user.IsWindow(handle) or not self.user.IsWindowVisible(handle):
            return None
        pid = w.DWORD()
        self.user.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        process = self.kernel.OpenProcess(0x1000, False, pid.value)
        if not process:
            return None
        try:
            path, length = ctypes.create_unicode_buffer(32768), w.DWORD(32768)
            if not self.kernel.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(length)):
                return None
            if ntpath.basename(path.value).casefold() not in {"wechat.exe", "weixin.exe"}:
                return None
            title = ctypes.create_unicode_buffer(self.user.GetWindowTextLengthW(handle) + 1)
            self.user.GetWindowTextW(handle, title, len(title))
            return Window(int(handle), pid.value, title.value or "微信窗口")
        finally:
            self.kernel.CloseHandle(process)

    def windows(self):
        found = []
        def visit(handle, _):
            window = self.identify(handle)
            if window:
                found.append(window)
            return True
        callback = self.callback_type(visit)
        if not self.user.EnumWindows(callback, 0):
            raise CaptureError("无法枚举微信窗口，请确认微信已经启动。")
        return found


def list_windows(api=None):
    return (api or WindowsAPI()).windows()


def capture_window(window, api=None, grab=None):
    api = api or WindowsAPI()
    actual = api.identify(window.handle)
    if actual is None or actual.pid != window.pid:
        raise CaptureError("所选微信窗口已关闭或发生变化，请重新选择。")
    if api.user.IsIconic(window.handle):
        raise CaptureError("请先还原微信窗口并打开需要的聊天。")
    try:
        # The window argument must never be replaced with a full-desktop fallback.
        image = (grab or ImageGrab.grab)(window=window.handle)
        if image.width < 80 or image.height < 80:
            raise CaptureError("窗口过小，请展开微信后重新采集。")
        if all(low == high for low, high in image.convert("RGB").getextrema()):
            raise CaptureError("未能采集到窗口内容；请使用微信截图或复制文字导入。")
        return image.convert("RGB")
    except CaptureError:
        raise
    except Exception:
        raise CaptureError("窗口采集失败。请打开聊天后重试，或使用微信截图导入。") from None


def image_payload(image):
    """Encode only the accepted crop; keep it in memory, not on disk."""
    image = image.convert("RGB")
    image.thumbnail((2000, 2000), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=87)
    if buffer.tell() > 3 * 1024 * 1024:
        raise CaptureError("截图过大，请缩小选区。")
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def crop_box(start, end, preview_size, original_size):
    """Translate a drag selection into bounded original-image coordinates."""
    width, height = preview_size
    left, right = sorted((max(0, min(width, start[0])), max(0, min(width, end[0]))))
    top, bottom = sorted((max(0, min(height, start[1])), max(0, min(height, end[1]))))
    if right - left < 20 or bottom - top < 20:
        raise CaptureError("请拖动选择完整的聊天内容，选区不能太小。")
    sx, sy = original_size[0] / width, original_size[1] / height
    return (int(left * sx), int(top * sy), min(original_size[0], int(right * sx)), min(original_size[1], int(bottom * sy)))
