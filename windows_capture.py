"""Explicit, single-window WeChat capture; no hooks, polling, or message sending."""
import base64
import ctypes
from dataclasses import dataclass
import io
import json
import ntpath
from pathlib import Path
import subprocess
import sys

from PIL import Image, ImageGrab

_CAPTURE_MESSAGES = {
    "unsupported": "微信窗口采集仅支持 Windows 10/11；仍可导入复制的文字。",
    "not_found": "所选微信窗口已关闭或发生变化，请重新查找并选择。",
    "minimized": "请先还原微信窗口并打开需要的聊天，不要将窗口最小化。",
    "too_small": "窗口过小，请展开微信后重新采集。",
    "blank": "微信窗口返回了空白或纯色画面，当前微信渲染方式可能不支持直接采集。",
    "capture_failed": "窗口采集失败，请打开对应聊天并保持窗口可见后重试。",
}
_SCREENSHOT_FALLBACK = "请按 Win+Shift+S 只截取聊天区域，再回助手点击“粘贴截图”。"


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
            raise CaptureError(_CAPTURE_MESSAGES["unsupported"])
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
        raise CaptureError(_CAPTURE_MESSAGES["not_found"])
    if api.user.IsIconic(window.handle):
        raise CaptureError(_CAPTURE_MESSAGES["minimized"])
    try:
        # The window argument must never be replaced with a full-desktop fallback.
        image = (grab or ImageGrab.grab)(window=window.handle)
        if image.width < 80 or image.height < 80:
            raise CaptureError(_CAPTURE_MESSAGES["too_small"])
        if all(low == high for low, high in image.convert("RGB").getextrema()):
            raise CaptureError(_CAPTURE_MESSAGES["blank"])
        return image.convert("RGB")
    except CaptureError:
        raise
    except Exception:
        raise CaptureError(_CAPTURE_MESSAGES["capture_failed"]) from None


def capture_window_isolated(window, timeout=8):
    """Bound native capture in a child process; return image bytes only in memory.

    Native GDI calls can block while holding Python's GIL. A separate process
    lets the GUI remain responsive and lets subprocess.run terminate a stalled
    capture. Only the selected window identity is passed to the helper.
    """
    command = [sys.executable, str(Path(__file__).resolve()), "--capture-window", str(window.handle), str(window.pid)]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, creationflags=flags, check=False)
    except subprocess.TimeoutExpired:
        raise CaptureError("微信窗口采集超时，已停止本次采集。\n" + _SCREENSHOT_FALLBACK) from None
    except OSError:
        raise CaptureError("无法启动窗口采集，请重新打开助手后重试。\n" + _SCREENSHOT_FALLBACK) from None
    if result.returncode != 0:
        message = _CAPTURE_MESSAGES["capture_failed"]
        try:
            error = json.loads(result.stderr)
            # Do not expose arbitrary subprocess stderr, titles or native errors.
            if isinstance(error, dict):
                message = _CAPTURE_MESSAGES.get(error.get("capture_error"), message)
        except (ValueError, TypeError, UnicodeDecodeError):
            pass
        raise CaptureError(message + "\n" + _SCREENSHOT_FALLBACK)
    try:
        with Image.open(io.BytesIO(result.stdout)) as image:
            if image.format != "PNG" or min(image.size) < 80 or image.width * image.height > 40_000_000:
                raise ValueError("invalid capture image")
            image.load()
            return image.convert("RGB")
    except Exception:
        raise CaptureError("采集程序未返回有效截图，请重试或改用系统截图。\n" + _SCREENSHOT_FALLBACK) from None


def _capture_cli(args):
    """Internal binary-image protocol. Never write capture data to a file."""
    try:
        if len(args) != 3 or args[0] != "--capture-window":
            raise ValueError("invalid arguments")
        handle, pid = int(args[1]), int(args[2])
        if handle <= 0 or pid <= 0:
            raise ValueError("invalid window identity")
    except (TypeError, ValueError):
        sys.stderr.write(json.dumps({"capture_error": "not_found", "message": _CAPTURE_MESSAGES["not_found"]}, ensure_ascii=True))
        return 2
    try:
        image = capture_window(Window(handle, pid, ""))
        image.save(sys.stdout.buffer, format="PNG")
        return 0
    except CaptureError as error:
        code = next((key for key, message in _CAPTURE_MESSAGES.items() if message == str(error)), "capture_failed")
    except Exception:
        code = "capture_failed"
    # Use fixed messages instead of printing tracebacks or native error details.
    # ASCII JSON also works with Windows pipe encodings such as GBK/cp1252.
    sys.stderr.write(json.dumps({"capture_error": code, "message": _CAPTURE_MESSAGES[code]}, ensure_ascii=True))
    return 1


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


if __name__ == "__main__":
    raise SystemExit(_capture_cli(sys.argv[1:]))
