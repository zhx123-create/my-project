"""Read the explicitly focused WeChat window and fill its chosen input.

The fallback types Unicode into the focus explicitly chosen by the user. It
never presses Enter, clicks Send, clears an editor, or modifies the clipboard.
"""
from contextlib import redirect_stderr, redirect_stdout
import ctypes
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import time

from conversation_watch import normalize_snapshot
from visible_capture import VisibleWindowsAPI, capture_visible
from wechat_bot import OperationCancellation, _exchange
from wechat_reader import ReaderFailure, extract_messages
from windows_capture import CaptureError, Window
from windows_ocr import recognize_image


class QuickFillError(CaptureError):
    pass


def prepare_text(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 3000:
        raise QuickFillError('回复为空或超过3000字，未开始填入。')
    # A line break must never become an Enter event in WeChat.
    text = ' '.join(text.splitlines()).strip()
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in text):
        raise QuickFillError('回复包含控制字符，未开始填入。')
    try:
        text.encode('utf-16-le')
    except UnicodeError:
        raise QuickFillError('回复包含无效字符，未开始填入。') from None
    return text


def text_digest(text):
    return hashlib.sha256(normalize_snapshot(text).encode()).hexdigest()


class KeyboardInput(ctypes.Structure):
    _fields_ = [('virtual_key', ctypes.c_uint16), ('scan', ctypes.c_uint16),
                ('flags', ctypes.c_uint32), ('time', ctypes.c_uint32), ('extra', ctypes.c_size_t)]


class MouseInput(ctypes.Structure):
    _fields_ = [('x', ctypes.c_int32), ('y', ctypes.c_int32), ('data', ctypes.c_uint32),
                ('flags', ctypes.c_uint32), ('time', ctypes.c_uint32), ('extra', ctypes.c_size_t)]


class HardwareInput(ctypes.Structure):
    _fields_ = [('message', ctypes.c_uint32), ('low', ctypes.c_uint16), ('high', ctypes.c_uint16)]


class InputUnion(ctypes.Union):
    _fields_ = [('keyboard', KeyboardInput), ('mouse', MouseInput), ('hardware', HardwareInput)]


class Input(ctypes.Structure):
    _anonymous_ = ('data',)
    _fields_ = [('kind', ctypes.c_uint32), ('data', InputUnion)]


def unicode_inputs(text):
    encoded = prepare_text(text).encode('utf-16-le')
    units = [int.from_bytes(encoded[index:index + 2], 'little') for index in range(0, len(encoded), 2)]
    events = (Input * (len(units) * 2))()
    for index, unit in enumerate(units):
        for offset, flags in ((0, 4), (1, 6)):  # UNICODE; UNICODE | KEYUP
            events[index * 2 + offset].kind = 1  # INPUT_KEYBOARD
            events[index * 2 + offset].keyboard = KeyboardInput(0, unit, flags, 0, 0)
    return events


class FocusAPI(VisibleWindowsAPI):
    def __init__(self):
        super().__init__()
        from ctypes import wintypes as w
        class LastInput(ctypes.Structure):
            _fields_ = [('size', w.UINT), ('tick', w.DWORD)]
        class GUIInfo(ctypes.Structure):
            _fields_ = [('size', w.DWORD), ('flags', w.DWORD),
                        ('active', w.HWND), ('focus', w.HWND), ('capture', w.HWND),
                        ('menu', w.HWND), ('move', w.HWND), ('caret', w.HWND), ('rect', w.RECT)]
        self.LastInput, self.GUIInfo = LastInput, GUIInfo
        for function, args, result in (
            (self.user.GetLastInputInfo, [ctypes.POINTER(LastInput)], w.BOOL),
            (self.user.GetGUIThreadInfo, [w.DWORD, ctypes.POINTER(GUIInfo)], w.BOOL),
            (self.user.GetAsyncKeyState, [ctypes.c_int], ctypes.c_short),
            (self.user.SendInput, [w.UINT, ctypes.POINTER(Input), ctypes.c_int], w.UINT),
        ):
            function.argtypes, function.restype = args, result

    def keys_up(self):
        return not any(self.user.GetAsyncKeyState(key) & 0x8000 for key in (1, 2, 16, 17, 18, 0x5B, 0x5C, ord('R')))

    def focus_token(self, window):
        from ctypes import wintypes as w
        if self.user.GetForegroundWindow() != window.handle:
            raise QuickFillError('当前焦点已离开所选微信窗口，未继续填入。回复保留在助手中。')
        pid = w.DWORD()
        thread = self.user.GetWindowThreadProcessId(window.handle, ctypes.byref(pid))
        info = self.GUIInfo(); info.size = ctypes.sizeof(info)
        last = self.LastInput(); last.size = ctypes.sizeof(last)
        if not thread or pid.value != window.pid or not self.user.GetGUIThreadInfo(thread, ctypes.byref(info)) or not self.user.GetLastInputInfo(ctypes.byref(last)):
            raise QuickFillError('无法确认微信输入焦点，未开始填入。请点击空白输入框后重试。')
        if info.active != window.handle or not info.focus or info.flags & 0x1E:
            raise QuickFillError('微信焦点不在正常聊天输入状态，请关闭菜单并点击空白输入框。')
        focus_pid = w.DWORD()
        self.user.GetWindowThreadProcessId(info.focus, ctypes.byref(focus_pid))
        if focus_pid.value != window.pid:
            raise QuickFillError('输入焦点不属于所选微信进程，未开始填入。')
        return {'handle': int(info.focus), 'input_time': int(last.tick)}

    def type_unicode(self, text):
        events = unicode_inputs(text)
        if self.user.SendInput(len(events), events, ctypes.sizeof(Input)) != len(events):
            raise QuickFillError('文字输入未完整提交，请核对微信输入框；请让微信和助手均以普通权限运行。未执行发送，不会重试。')


def verify_focus(api, window, expected):
    current = api.identify(window.handle)
    if current is None or (current.pid, current.title) != (window.pid, window.title) or api.user.IsIconic(window.handle):
        raise QuickFillError('所选微信窗口已关闭、最小化或发生变化，未继续填入。')
    if api.focus_token(window) != expected or not api.keys_up():
        raise QuickFillError('等待期间发生键鼠操作或焦点变化，本次取消填入；回复保留在助手中。请保持微信输入框不动后重试。')


def read_context(window, api, source=None):
    if source != 'windows_ocr':
        try:
            import uiautomation as auto
            auto.Logger.SetLogFile('')
            with auto.UIAutomationInitializerInThread():
                result = extract_messages(auto.ControlFromHandle(window.handle))
            return normalize_snapshot(result['text']), 'uia'
        except Exception as error:
            if isinstance(error, ReaderFailure) and error.code == 'ambiguous':
                raise QuickFillError('窗口中有多个消息区域，无法确定目标聊天。请只打开一个独立微信聊天窗口，再点击其空白输入框。') from None
            if source == 'uia':
                raise QuickFillError('微信消息文字暂时不可读取，未填入旧回复。请重新操作。') from None
    # Capture only the explicitly selected independent chat window. Overlays
    # are masked, with no full-desktop or clipboard fallback.
    image = capture_visible(window, api=api, preview=True)
    result = recognize_image(image)
    return normalize_snapshot(result['text']), 'windows_ocr'


def focused_editor():
    """Optional ValuePattern support; no message list or Send button required."""
    try:
        import uiautomation as auto
        control = auto.GetFocusedControl()
        if control.ControlTypeName != 'EditControl':
            return None
        pattern = control.GetValuePattern()
        return (control, pattern) if pattern is not None else None
    except Exception:
        return None


def fill_focused(api, window, expected, text, editor=None):
    text = prepare_text(text)
    verify_focus(api, window, expected)
    if editor is not None:
        control, pattern = editor
        if control.ProcessId != window.pid or pattern.IsReadOnly:
            raise QuickFillError('所选输入框不可写或不属于微信，未开始填入。')
        if pattern.Value:
            raise QuickFillError('微信输入框已有文字，未覆盖你的草稿。请先处理原稿再重试。')
        verify_focus(api, window, expected)
        try:
            pattern.SetValue(text)
            if pattern.Value != text:
                raise ValueError('not confirmed')
        except Exception:
            raise QuickFillError('输入框填写结果未能确认，请查看微信；未执行发送，不会改用其他方式重复填入。') from None
        return {'filled': True, 'confirmed': True, 'method': 'uia'}
    verify_focus(api, window, expected)
    api.type_unicode(text)
    return {'filled': True, 'confirmed': False, 'method': 'unicode'}


def _live(payload, api=None):
    if sys.platform != 'win32':
        raise QuickFillError('一键填入微信仅支持 Windows 10/11。')
    api = api or FocusAPI()
    if payload['action'] == 'read_current':
        deadline = time.monotonic() + 2
        while not api.keys_up() and time.monotonic() < deadline:
            time.sleep(.03)
        window = api.identify(api.user.GetForegroundWindow())
        if window is None:
            raise QuickFillError('请先点击单独打开的微信聊天输入框，再按 Ctrl+Alt+R，或使用助手的“一键填入微信”。')
        expected = api.focus_token(window)
        verify_focus(api, window, expected)
        text, source = read_context(window, api)
        verify_focus(api, window, expected)
        return {'window': {'handle': window.handle, 'pid': window.pid, 'title': window.title},
                'focus': expected, 'context': text, 'digest': text_digest(text), 'source': source}
    window = Window(**payload['window'])
    expected = payload['focus']
    text = prepare_text(payload['text'])
    verify_focus(api, window, expected)
    current, _ = read_context(window, api, payload['source'])
    verify_focus(api, window, expected)
    if text_digest(current) != payload['digest']:
        raise QuickFillError('生成期间聊天有新内容，本次旧回复没有填入。请重新触发，助手会读取最新聊天。')
    try:
        import uiautomation as auto
    except ImportError:
        auto = None
    if auto is not None:
        auto.Logger.SetLogFile('')
        with auto.UIAutomationInitializerInThread():
            return fill_focused(api, window, expected, text, focused_editor())
    return fill_focused(api, window, expected, text)


def validate_snapshot(value):
    if not isinstance(value, dict) or value.get('source') not in ('uia', 'windows_ocr'):
        raise ValueError('invalid source')
    window, focus, context = value.get('window'), value.get('focus'), value.get('context')
    if not isinstance(window, dict) or set(window) != {'handle', 'pid', 'title'} or any(type(window[key]) is not int or window[key] <= 0 for key in ('handle', 'pid')) or not isinstance(window['title'], str) or len(window['title']) > 1000:
        raise ValueError('invalid window')
    if not isinstance(focus, dict) or set(focus) != {'handle', 'input_time'} or type(focus['handle']) is not int or focus['handle'] <= 0 or type(focus['input_time']) is not int or not 0 <= focus['input_time'] <= 0xFFFFFFFF:
        raise ValueError('invalid focus')
    if not isinstance(context, str) or not context.strip() or len(context) > 12000 or value.get('digest') != text_digest(context):
        raise ValueError('invalid context')


def call_native(payload, cancel=None, timeout=25):
    if cancel is not None and cancel.is_set():
        raise QuickFillError('本次一键填入已取消。')
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000) if sys.platform == 'win32' else 0
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--quickfill'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
    try:
        if isinstance(cancel, OperationCancellation):
            cancel.attach(process)
        raw, _ = _exchange(process, payload, cancel, timeout)
    except Exception:
        try:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        raise QuickFillError('一键填入已取消或读取超时。若已开始填入，请查看微信输入框；未执行发送，不会自动重试。') from None
    finally:
        if isinstance(cancel, OperationCancellation):
            cancel.detach(process)
    if process.returncode or len(raw) > 180000:
        raise QuickFillError('微信读取或填入未完成，请核对窗口及输入框；未执行发送，不会自动重试。')
    try:
        result = json.loads(raw)
        if not isinstance(result, dict) or result.get('ok') is not True:
            message = result.get('error') if isinstance(result, dict) else None
            raise QuickFillError(message if isinstance(message, str) and 0 < len(message) <= 250 else '一键填入未完成。')
        value = result['result']
        if payload['action'] == 'read_current':
            validate_snapshot(value)
        elif not isinstance(value, dict) or value.get('filled') is not True or type(value.get('confirmed')) is not bool or value.get('method') not in ('uia', 'unicode'):
            raise ValueError('invalid receipt')
        return value
    except (ValueError, TypeError, KeyError):
        raise QuickFillError('微信读取或填入返回格式无效，请核对输入框；未执行发送。') from None


def _cli():
    try:
        raw = sys.stdin.buffer.read(80001)
        if len(raw) > 80000:
            raise ValueError('oversize')
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get('action') not in ('read_current', 'fill'):
            raise ValueError('invalid action')
        if payload['action'] == 'fill':
            validate_snapshot({**payload, 'context': payload.get('context', '')})
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = _live(payload)
        output = {'ok': True, 'result': result}
    except CaptureError as error:
        output = {'ok': False, 'error': str(error)}
    except Exception:
        output = {'ok': False, 'error': '微信读取或填入未完成，请核对所选聊天和输入框；未执行发送。'}
    sys.stdout.write(json.dumps(output, ensure_ascii=True))


if __name__ == '__main__' and sys.argv[1:] == ['--quickfill']:
    _cli()
