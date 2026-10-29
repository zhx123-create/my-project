"""Scoped UI Automation adapter. No clipboard, global keys, or coordinate clicks.

Sending is supported only when a selected WeChat exposes an unambiguous chat
header, message layout, writable ValuePattern and invokable Send button.
"""
from collections import deque
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

from visible_capture import VisibleWindowsAPI
from wechat_reader import _find_message_list, _message_text, _visible
from windows_capture import CaptureError, Window


class BotAdapterError(CaptureError):
    pass


class OperationCancellation:
    """Stop native children synchronously, including when the GUI is closing."""
    def __init__(self):
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.process = None

    def is_set(self):
        return self.event.is_set()

    def wait(self, timeout):
        return self.event.wait(timeout)

    def attach(self, process):
        with self.lock:
            self.process = process
            if self.event.is_set() and process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass

    def detach(self, process):
        with self.lock:
            if self.process is process:
                self.process = None

    def set(self):
        self.event.set()
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                try:
                    self.process.kill()
                except OSError:
                    pass


def digest_snapshot(snapshot):
    return hashlib.sha256(json.dumps([snapshot['contact'], snapshot['messages']], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _rect(control):
    rect = control.BoundingRectangle
    result = tuple(int(getattr(rect, key)) for key in ('left', 'top', 'right', 'bottom'))
    if result[2] <= result[0] or result[3] <= result[1]:
        raise BotAdapterError('微信未提供有效控件位置，机器人已暂停。')
    return result


def _controls(root):
    pending = deque([(root, 0)])
    visited = 0
    while pending:
        control, depth = pending.popleft()
        visited += 1
        if visited > 500 or depth > 12:
            raise BotAdapterError('微信控件结构过于复杂，无法确认发送目标。')
        if not _visible(control):
            continue
        kind = control.ControlTypeName
        if kind in ('ListControl', 'TreeControl', 'MenuControl'):
            continue
        yield control
        if kind not in ('TextControl', 'EditControl', 'ButtonControl', 'ListItemControl', 'DataItemControl'):
            pending.extend((child, depth + 1) for child in control.GetChildren())


def _direction(item, area):
    """Conservative layout check after the user confirms left=other/right=self."""
    boxes = []
    pending = deque([(item, 0)])
    count = 0
    while pending:
        control, depth = pending.popleft()
        count += 1
        if count > 80:
            return 'unknown'
        if not _visible(control):
            continue
        if control.ControlTypeName == 'TextControl':
            boxes.append(_rect(control))
        elif depth < 5 and control.ControlTypeName not in ('ButtonControl', 'EditControl', 'ListControl'):
            pending.extend((child, depth + 1) for child in control.GetChildren())
    boxes = boxes or [_rect(item)]
    left, top = min(box[0] for box in boxes), min(box[1] for box in boxes)
    right, bottom = max(box[2] for box in boxes), max(box[3] for box in boxes)
    width = area[2] - area[0]
    if top < area[1] or bottom > area[3] or left < area[0] or right > area[2]:
        return 'unknown'
    if left <= area[0] + width * .35 and right < area[0] + width * .75:
        return 'other'
    if left > area[0] + width * .35 and right >= area[0] + width * .75:
        return 'self'
    return 'unknown'


def inspect_chat(root, contact):
    message_list = _find_message_list(root)
    area = _rect(message_list)
    headers, editors, senders = [], [], []
    for control in _controls(root):
        kind = control.ControlTypeName
        if kind not in ('TextControl', 'EditControl', 'ButtonControl'):
            continue
        box = _rect(control)
        if box[0] < area[0] or box[2] > area[2]:
            continue
        if kind == 'TextControl' and box[3] <= area[1] and control.Name.strip() == contact:
            headers.append(control)
        if box[1] >= area[3]:
            if kind == 'EditControl':
                editors.append(control)
            elif kind == 'ButtonControl' and control.Name.strip() in ('发送', '发送(S)', '发送 (S)', 'Send', 'Send (S)'):
                senders.append(control)
    if len(headers) != 1:
        raise BotAdapterError('无法从聊天标题确认你填写的联系人。仅支持有明确聊天标题的窗口；未发送任何消息。')
    if len(editors) != 1 or len(senders) != 1:
        raise BotAdapterError('当前微信未提供唯一的输入框和发送按钮，不能启用自动发送；可继续使用自动草稿。')
    messages = []
    for item in message_list.GetChildren()[-60:]:
        if not _visible(item):
            continue
        text = _message_text(item)
        if text:
            if len(text) > 2000:
                raise BotAdapterError('可见消息过长，机器人已暂停。请缩小到近期聊天。')
            messages.append({'direction': _direction(item, area), 'text': text})
    if not messages or sum(len(item['text']) for item in messages) > 12000:
        raise BotAdapterError('没有有效的近期消息，或消息总量过大。请保持近期聊天可见。')
    snapshot = {'contact': contact, 'messages': messages}
    snapshot['digest'] = digest_snapshot(snapshot)
    return snapshot, (message_list, headers[0], editors[0], senders[0])


def _live(payload):
    if sys.platform != 'win32':
        raise BotAdapterError('自动回复机器人仅支持 Windows 10/11。')
    window = Window(**payload['window'])
    contact = payload['contact']
    if not isinstance(contact, str) or not contact.strip() or len(contact) > 100:
        raise BotAdapterError('请填写微信聊天顶部显示的联系人名称。')
    api = VisibleWindowsAPI()
    def verify_window():
        current = api.identify(window.handle)
        if current is None or (current.pid, current.title) != (window.pid, window.title) or api.user.IsIconic(window.handle):
            raise BotAdapterError('微信窗口身份或标题发生变化，机器人已暂停。')
    verify_window()
    import uiautomation as auto
    auto.Logger.SetLogFile('')
    with auto.UIAutomationInitializerInThread():
        root = auto.ControlFromHandle(window.handle)
        snapshot, controls = inspect_chat(root, contact)
        value = controls[2].GetValuePattern()
        invoke = controls[3].GetInvokePattern()
        if value is None or invoke is None or value.IsReadOnly:
            raise BotAdapterError('当前微信不支持控件直接填写和发送；未使用键盘、剪贴板或坐标点击代替。')
        for control in controls:
            if control.ProcessId != window.pid:
                raise BotAdapterError('聊天控件不属于绑定的微信进程，机器人已暂停。')
            api.uncovered(window.handle, _rect(control))
        verify_window()
        if payload['action'] == 'read':
            if value.Value:
                raise BotAdapterError('微信输入框里已有内容，请先处理自己的草稿再开启机器人。')
            return snapshot
        text = payload['text']
        if not isinstance(text, str) or not text.strip() or len(text) > 200 or any(ord(char) < 32 for char in text):
            raise BotAdapterError('自动回复正文无效，本次未发送。')
        if snapshot['digest'] != payload['expected_digest'] or snapshot['messages'][-1]['direction'] != 'other':
            raise BotAdapterError('等待期间聊天已变化，本次草稿已作废，未发送。')
        if value.Value:
            raise BotAdapterError('微信输入框已有你的文字，机器人没有覆盖或发送。')
        # After mutation, any exception is uncertain. No caller retries it.
        try:
            value.SetValue(text)
            verify_window()
            fresh, current_controls = inspect_chat(root, contact)
            if fresh['digest'] != snapshot['digest'] or current_controls[2].GetValuePattern().Value != text:
                raise BotAdapterError('changed after filling')
            for control in current_controls:
                if control.ProcessId != window.pid:
                    raise BotAdapterError('changed controls')
                api.uncovered(window.handle, _rect(control))
            verify_window()
            current_controls[3].GetInvokePattern().Invoke()
            # Only an observed outgoing message establishes confirmation.
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                verify_window()
                after, after_controls = inspect_chat(root, contact)
                last = after['messages'][-1]
                if last['direction'] == 'self' and last['text'].strip() == text and not after_controls[2].GetValuePattern().Value:
                    return {'sent': True, 'snapshot': after}
                time.sleep(.15)
        except Exception:
            raise BotAdapterError('机器人已开始填写或发送，但未能确认结果。已暂停，请手动核对微信输入框和聊天记录；不会重发。') from None
        raise BotAdapterError('已尝试点击发送，但未能确认消息出现在聊天中。机器人已暂停，请手动核对；不会重发。')


def call_adapter(window, contact, action='read', text='', expected_digest='', cancel=None, timeout=10):
    if cancel is not None and cancel.is_set():
        raise BotAdapterError('机器人已暂停，未开始新的控件操作。')
    payload = {'window': {'handle': window.handle, 'pid': window.pid, 'title': window.title}, 'contact': contact,
               'action': action, 'text': text, 'expected_digest': expected_digest}
    command = [sys.executable, str(Path(__file__).resolve()), '--robot-adapter']
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000) if sys.platform == 'win32' else 0
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
    try:
        if isinstance(cancel, OperationCancellation):
            cancel.attach(process)
        out, err = _exchange(process, payload, cancel, timeout)
    except BotAdapterError:
        raise
    except Exception:
        try:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        raise
    finally:
        if isinstance(cancel, OperationCancellation):
            cancel.detach(process)
    if process.returncode or len(out) > 180000:
        raise BotAdapterError('微信控件读取或发送未完成，机器人已暂停。请检查窗口及微信版本；如在发送阶段请手动核对，切勿直接重发。')
    try:
        result = json.loads(out)
        if not isinstance(result, dict) or result.get('ok') is not True:
            message = result.get('error') if isinstance(result, dict) else None
            raise BotAdapterError(message if isinstance(message, str) and 0 < len(message) <= 250 else '机器人控件操作未完成。')
        result = result['result']
        snapshot = result['snapshot'] if action == 'send' else result
        validate_snapshot(snapshot, contact)
        if action == 'send' and result.get('sent') is not True:
            raise ValueError('not confirmed')
        return result
    except (ValueError, TypeError, KeyError):
        raise BotAdapterError('机器人控件返回无效结果，已暂停；不会自动重试发送。') from None


def _exchange(process, payload, cancel, timeout):
    started = time.monotonic()
    first = True
    while True:
        if (cancel is not None and cancel.is_set()) or time.monotonic() - started >= timeout:
            if process.poll() is None:
                process.kill()
            process.communicate()
            raise BotAdapterError('机器人已停止或控件操作超时。若处于发送阶段，请手动核对微信；不会重发。')
        try:
            out, err = process.communicate(json.dumps(payload, ensure_ascii=True).encode() if first else None, timeout=.1)
            break
        except subprocess.TimeoutExpired:
            first = False
    return out, err


def validate_snapshot(snapshot, contact):
    if not isinstance(snapshot, dict) or snapshot.get('contact') != contact:
        raise ValueError('invalid contact')
    messages = snapshot.get('messages')
    if not isinstance(messages, list) or not 1 <= len(messages) <= 60:
        raise ValueError('invalid messages')
    for item in messages:
        if not isinstance(item, dict) or item.get('direction') not in ('other', 'self', 'unknown') or not isinstance(item.get('text'), str) or not 0 < len(item['text']) <= 2000:
            raise ValueError('invalid message')
    if sum(len(item['text']) for item in messages) > 12000 or snapshot.get('digest') != digest_snapshot(snapshot):
        raise ValueError('invalid digest')


def _cli():
    try:
        raw = sys.stdin.buffer.read(80001)
        if len(raw) > 80000:
            raise ValueError('oversize')
        payload = json.loads(raw)
        if payload.get('action') not in ('read', 'send'):
            raise ValueError('invalid action')
        window = payload['window']
        if type(window['handle']) is not int or type(window['pid']) is not int or window['handle'] <= 0 or window['pid'] <= 0 or not isinstance(window['title'], str) or len(window['title']) > 1000:
            raise ValueError('invalid window')
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = _live(payload)
        output = {'ok': True, 'result': result}
    except CaptureError as error:
        output = {'ok': False, 'error': str(error)}
    except ImportError:
        output = {'ok': False, 'error': '缺少 Windows 控件组件，请重新运行 start_windows.bat。'}
    except Exception:
        output = {'ok': False, 'error': '当前微信的控件不支持机器人操作，或操作未完成。已暂停，请手动检查微信；不会重试发送。'}
    sys.stdout.write(json.dumps(output, ensure_ascii=True))


if __name__ == '__main__' and sys.argv[1:] == ['--robot-adapter']:
    _cli()
