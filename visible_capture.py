"""Explicit visible pixels from a bound WeChat window, with occlusion checks."""
import ctypes
import io
import json
from pathlib import Path
import subprocess
import sys

from PIL import Image, ImageGrab
from windows_capture import CaptureError, Window, WindowsAPI

MESSAGES = {
    "unsupported": "可见窗口截图仅支持 Windows 10/11。",
    "changed": "微信窗口已关闭或发生变化，请重新绑定单独的聊天窗口。",
    "minimized": "请还原微信聊天窗口；自动截图时不能最小化。",
    "resized": "聊天窗口尺寸已改变，请重新框选消息区域。移动窗口无需重新框选。",
    "covered": "聊天选区被其他窗口遮挡。请把微信和助手并排摆放，露出整个选区后重试。",
    "offscreen": "聊天选区已移出屏幕，请把整个消息选区放回屏幕内后重试。",
    "region": "聊天选区无效或过小，请重新框选消息区域。",
    "blank": "聊天选区是空白画面，请确认已打开聊天并露出消息内容。",
    "failed": "未能截取选定聊天区域，请检查窗口是否可见后重试。",
    "timeout": "聊天区域截图超时，已停止本次截图。",
}


class VisibleCaptureError(CaptureError):
    def __init__(self, code):
        self.code = code if code in MESSAGES else "failed"
        super().__init__(MESSAGES[self.code])


class VisibleWindowsAPI(WindowsAPI):
    def __init__(self):
        if sys.platform == "win32":
            try:
                user = ctypes.WinDLL("user32", use_last_error=True)
                user.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
                user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            except (AttributeError, OSError):
                pass
        super().__init__()
        from ctypes import wintypes as w
        for function, args, result in (
            (self.user.GetWindowRect, [w.HWND, ctypes.POINTER(w.RECT)], w.BOOL),
            (self.user.GetTopWindow, [w.HWND], w.HWND),
            (self.user.GetWindow, [w.HWND, w.UINT], w.HWND),
            (self.user.SetForegroundWindow, [w.HWND], w.BOOL),
            (self.user.GetSystemMetrics, [ctypes.c_int], ctypes.c_int),
        ):
            function.argtypes, function.restype = args, result
        self._cloaked = None
        try:
            function = ctypes.WinDLL("dwmapi").DwmGetWindowAttribute
            function.argtypes = [w.HWND, w.DWORD, ctypes.POINTER(w.DWORD), w.DWORD]
            function.restype = w.LONG
            self._cloaked = function
        except (OSError, AttributeError):
            pass

    def cloaked(self, handle):
        function = getattr(self, "_cloaked", None)
        if function is None:
            return False
        from ctypes import wintypes as w
        value = w.DWORD()
        return function(handle, 14, ctypes.byref(value), ctypes.sizeof(value)) == 0 and bool(value.value)

    def bounds(self, handle):
        from ctypes import wintypes as w
        rect = w.RECT()
        if not self.user.GetWindowRect(handle, ctypes.byref(rect)):
            raise VisibleCaptureError("changed")
        return rect.left, rect.top, rect.right, rect.bottom

    def uncovered(self, handle, box):
        metrics = getattr(self.user, "GetSystemMetrics", None)
        if metrics is not None:
            x, y, width, height = (metrics(index) for index in (76, 77, 78, 79))
            if not (x <= box[0] < box[2] <= x + width and y <= box[1] < box[3] <= y + height):
                raise VisibleCaptureError("offscreen")
        current, seen = self.user.GetTopWindow(None), set()
        while current and current not in seen and len(seen) < 5000:
            if int(current) == handle:
                return
            seen.add(current)
            if self.user.IsWindowVisible(current) and not self.user.IsIconic(current) and not self.cloaked(current):
                other = self.bounds(current)
                if min(box[2], other[2]) > max(box[0], other[0]) and min(box[3], other[3]) > max(box[1], other[1]):
                    raise VisibleCaptureError("covered")
            current = self.user.GetWindow(current, 2)  # GW_HWNDNEXT, in Z order.
        raise VisibleCaptureError("changed")


def activate_window(window):
    """Only on an explicit bind click, ask Windows to foreground this window."""
    api = VisibleWindowsAPI()
    current = api.identify(window.handle)
    if current is None or current.pid != window.pid:
        raise VisibleCaptureError("changed")
    api.user.SetForegroundWindow(window.handle)


def capture_visible(window, region=None, expected_size=None, api=None, grab=None):
    api = api or VisibleWindowsAPI()
    current = api.identify(window.handle)
    if current is None or current.pid != window.pid:
        raise VisibleCaptureError("changed")
    if api.user.IsIconic(window.handle):
        raise VisibleCaptureError("minimized")
    bounds = api.bounds(window.handle)
    size = (bounds[2] - bounds[0], bounds[3] - bounds[1])
    if min(size) < 80 or size[0] * size[1] > 40_000_000:
        raise VisibleCaptureError("region")
    if expected_size is not None and tuple(expected_size) != size:
        raise VisibleCaptureError("resized")
    if region is None:
        region = (0, 0, *size)
    if len(region) != 4 or any(type(value) is not int for value in region) or not (0 <= region[0] < region[2] <= size[0] and 0 <= region[1] < region[3] <= size[1]) or min(region[2] - region[0], region[3] - region[1]) < 20:
        raise VisibleCaptureError("region")
    box = (bounds[0] + region[0], bounds[1] + region[1], bounds[0] + region[2], bounds[1] + region[3])
    api.uncovered(window.handle, box)
    try:
        image = (grab or ImageGrab.grab)(bbox=box, all_screens=True)
    except Exception:
        raise VisibleCaptureError("failed") from None
    actual = api.identify(window.handle)
    if actual is None or actual.pid != window.pid or api.bounds(window.handle) != bounds:
        raise VisibleCaptureError("changed")
    api.uncovered(window.handle, box)
    if image.size != (region[2] - region[0], region[3] - region[1]):
        raise VisibleCaptureError("failed")
    image = image.convert("RGB")
    if all(low == high for low, high in image.getextrema()):
        raise VisibleCaptureError("blank")
    return image


def capture_visible_isolated(window, region=None, expected_size=None, timeout=8):
    command = [sys.executable, str(Path(__file__).resolve()), "--visible", str(window.handle), str(window.pid)]
    if region is not None:
        if expected_size is None or len(region) != 4 or len(expected_size) != 2 or any(type(value) is not int for value in (*region, *expected_size)):
            raise VisibleCaptureError("region")
        command.extend(str(value) for value in (*region, *expected_size))
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0
        response = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, creationflags=flags, check=False)
    except subprocess.TimeoutExpired:
        raise VisibleCaptureError("timeout") from None
    except OSError:
        raise VisibleCaptureError("failed") from None
    if response.returncode:
        try:
            code = json.loads(response.stderr).get("capture_error")
        except (ValueError, TypeError, AttributeError):
            code = "failed"
        raise VisibleCaptureError(code)
    try:
        with Image.open(io.BytesIO(response.stdout)) as image:
            if image.format != "PNG" or min(image.size) < 20 or image.width * image.height > 40_000_000:
                raise ValueError("invalid image")
            image.load()
            return image.convert("RGB")
    except Exception:
        raise VisibleCaptureError("failed") from None


def _cli(args):
    try:
        if len(args) not in (3, 9) or args[0] != "--visible":
            raise ValueError("invalid protocol")
        numbers = [int(value) for value in args[1:]]
        if min(numbers[:2]) <= 0:
            raise ValueError("invalid identity")
        region, size = (tuple(numbers[2:6]), tuple(numbers[6:8])) if len(args) == 9 else (None, None)
        image = capture_visible(Window(numbers[0], numbers[1], ""), region, size)
        image.save(sys.stdout.buffer, format="PNG")
        return 0
    except VisibleCaptureError as error:
        code = error.code
    except CaptureError:
        code = "unsupported" if sys.platform != "win32" else "failed"
    except Exception:
        code = "failed"
    sys.stderr.write(json.dumps({"capture_error": code}))
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
