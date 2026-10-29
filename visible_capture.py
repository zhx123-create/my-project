"""Explicit visible pixels from a bound WeChat window, with occlusion checks."""
import ctypes
import io
import json
from pathlib import Path
import subprocess
import sys

from PIL import Image, ImageDraw, ImageGrab, PngImagePlugin
from windows_capture import CaptureError, Window, WindowsAPI

MESSAGES = {
    "unsupported": "可见窗口截图仅支持 Windows 10/11。",
    "changed": "微信窗口已关闭或发生变化，请重新绑定单独的聊天窗口。",
    "minimized": "请还原微信聊天窗口；自动截图时不能最小化。",
    "resized": "聊天窗口尺寸已改变，请重新框选消息区域。移动窗口无需重新框选。",
    "covered": "消息选区被浮窗遮挡，请移开盖住消息的输入法浮条或弹窗，或重新框选清楚可见的消息区。消息区外的浮条不影响已绑定选区。",
    "offscreen": "实时截取微信失败：选区超出可见屏幕，尚未调用 AI。已有系统截图请点“粘贴截图”；继续实时截取时，请把消息选区放回屏幕内。",
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
        self._window_style = None
        self._layered_attributes = None
        try:
            style = getattr(self.user, "GetWindowLongPtrW", None) or self.user.GetWindowLongW
            style.argtypes, style.restype = [w.HWND, ctypes.c_int], ctypes.c_ssize_t
            self._window_style = style
            attributes = self.user.GetLayeredWindowAttributes
            attributes.argtypes = [w.HWND, ctypes.POINTER(w.DWORD), ctypes.POINTER(w.BYTE), ctypes.POINTER(w.DWORD)]
            attributes.restype = w.BOOL
            self._layered_attributes = attributes
        except AttributeError:
            pass
        try:
            function = ctypes.WinDLL("dwmapi").DwmGetWindowAttribute
            function.argtypes = [w.HWND, w.DWORD, ctypes.c_void_p, w.DWORD]
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
        # GetWindowRect includes invisible resize borders. On a maximized
        # window those can extend beyond the monitor despite visible content
        # being entirely onscreen. DWM reports the actual frame in pixels.
        function = getattr(self, "_cloaked", None)
        if function is not None:
            if function(handle, 9, ctypes.byref(rect), ctypes.sizeof(rect)) == 0 and rect.right > rect.left and rect.bottom > rect.top:
                return rect.left, rect.top, rect.right, rect.bottom
        if not self.user.GetWindowRect(handle, ctypes.byref(rect)):
            raise VisibleCaptureError("changed")
        return rect.left, rect.top, rect.right, rect.bottom

    def drawn(self, handle):
        style, attributes = getattr(self, "_window_style", None), getattr(self, "_layered_attributes", None)
        if style is not None and attributes is not None and style(handle, -20) & 0x80000:  # WS_EX_LAYERED
            from ctypes import wintypes as w
            color, alpha, flags = w.DWORD(), w.BYTE(), w.DWORD()
            if attributes(handle, ctypes.byref(color), ctypes.byref(alpha), ctypes.byref(flags)) and flags.value & 2 and alpha.value == 0:
                return False
        return True

    def covering_rectangles(self, handle, box):
        metrics = getattr(self.user, "GetSystemMetrics", None)
        if metrics is not None:
            x, y, width, height = (metrics(index) for index in (76, 77, 78, 79))
            if not (x <= box[0] < box[2] <= x + width and y <= box[1] < box[3] <= y + height):
                raise VisibleCaptureError("offscreen")
        current, seen, blocks = self.user.GetTopWindow(None), set(), []
        while current and current not in seen and len(seen) < 5000:
            if int(current) == handle:
                return blocks
            seen.add(current)
            if self.user.IsWindowVisible(current) and not self.user.IsIconic(current) and not self.cloaked(current) and self.drawn(current):
                other = self.bounds(current)
                if min(box[2], other[2]) > max(box[0], other[0]) and min(box[3], other[3]) > max(box[1], other[1]):
                    blocks.append((max(box[0], other[0]), max(box[1], other[1]), min(box[2], other[2]), min(box[3], other[3])))
            current = self.user.GetWindow(current, 2)  # GW_HWNDNEXT, in Z order.
        raise VisibleCaptureError("changed")

    def uncovered(self, handle, box):
        if self.covering_rectangles(handle, box):
            raise VisibleCaptureError("covered")


def activate_window(window):
    """Only on an explicit bind click, ask Windows to foreground this window."""
    api = VisibleWindowsAPI()
    current = api.describe(window.handle) if getattr(window, "user_selected", False) else api.identify(window.handle)
    if current is None or current.pid != window.pid:
        raise VisibleCaptureError("changed")
    api.user.SetForegroundWindow(window.handle)


def capture_visible(window, region=None, expected_size=None, api=None, grab=None, preview=False):
    if preview and region is not None:
        raise VisibleCaptureError("region")
    api = api or VisibleWindowsAPI()
    identify = api.describe if getattr(window, "user_selected", False) else api.identify
    current = identify(window.handle)
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
    blocks = list(api.covering_rectangles(window.handle, box)) if preview else []
    if not preview:
        api.uncovered(window.handle, box)
    try:
        image = (grab or ImageGrab.grab)(bbox=box, all_screens=True)
    except Exception:
        raise VisibleCaptureError("failed") from None
    actual = identify(window.handle)
    if actual is None or actual.pid != window.pid or api.bounds(window.handle) != bounds:
        raise VisibleCaptureError("changed")
    if preview:
        blocks.extend(api.covering_rectangles(window.handle, box))
    else:
        api.uncovered(window.handle, box)
    if image.size != (region[2] - region[0], region[3] - region[1]):
        raise VisibleCaptureError("failed")
    image = image.convert("RGB")
    if all(low == high for low, high in image.getextrema()):
        raise VisibleCaptureError("blank")
    if preview:
        relative = sorted({(left - box[0], top - box[1], right - box[0], bottom - box[1]) for left, top, right, bottom in blocks})
        if len(relative) > 512:
            raise VisibleCaptureError("covered")
        draw = ImageDraw.Draw(image)
        for left, top, right, bottom in relative:
            draw.rectangle((left, top, right - 1, bottom - 1), fill=(218, 222, 230))
        if relative and all(low == high for low, high in image.getextrema()):
            raise VisibleCaptureError("covered")
        image.info["capture_blocks"] = relative
    return image


def capture_visible_isolated(window, region=None, expected_size=None, timeout=8, preview=False):
    mode = "--preview" if preview else "--visible"
    if getattr(window, "user_selected", False):
        mode += "-selected"
    command = [sys.executable, str(Path(__file__).resolve()), mode, str(window.handle), str(window.pid)]
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
            result = image.convert("RGB")
            if preview:
                raw = image.info.get("capture_blocks")
                if not isinstance(raw, str) or len(raw) > 32768:
                    raise ValueError("missing preview scope")
                blocks = json.loads(raw)
                if not isinstance(blocks, list) or len(blocks) > 512 or any(not isinstance(block, list) or len(block) != 4 or any(type(value) is not int for value in block) or not (0 <= block[0] < block[2] <= image.width and 0 <= block[1] < block[3] <= image.height) for block in blocks):
                    raise ValueError("invalid preview scope")
                result.info["capture_blocks"] = blocks
            return result
    except Exception:
        raise VisibleCaptureError("failed") from None


def _cli(args):
    try:
        if len(args) not in (3, 9) or args[0] not in ("--visible", "--visible-selected", "--preview", "--preview-selected"):
            raise ValueError("invalid protocol")
        numbers = [int(value) for value in args[1:]]
        if min(numbers[:2]) <= 0:
            raise ValueError("invalid identity")
        region, size = (tuple(numbers[2:6]), tuple(numbers[6:8])) if len(args) == 9 else (None, None)
        preview = args[0].startswith("--preview")
        image = capture_visible(Window(numbers[0], numbers[1], "", args[0].endswith("-selected")), region, size, preview=preview)
        metadata = PngImagePlugin.PngInfo()
        if preview:
            metadata.add_text("capture_blocks", json.dumps(image.info["capture_blocks"]))
        image.save(sys.stdout.buffer, format="PNG", pnginfo=metadata)
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
