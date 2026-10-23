"""Explicit, read-only access to a selected WeChat window's message list.

This is a best-effort accessibility adapter, not a WeChat API. A Windows client
that does not expose its message list is unsupported. No screenshots, hooks,
database access, clipboard monitoring, scrolling, or message sending are used.
"""
from collections import deque
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys

from windows_capture import CaptureError, Window, WindowsAPI

MESSAGE_LIST_NAMES = frozenset(("消息", "消息列表", "聊天消息", "聊天记录"))
MAX_ENTRIES = 100
MAX_TEXT = 12000
_PREFIX = "【微信可访问消息文字；发言者及会话身份未自动核实，请核对】\n"
_ERRORS = {
    "unsupported_os": "直接读取微信文字仅支持 Windows 10/11。",
    "missing_dependency": "尚未安装 Windows 文字读取组件。请关闭助手后重新运行 start_windows.bat 安装依赖。",
    "not_found": "所选微信窗口已关闭或发生变化，请重新查找并选择。",
    "minimized": "请还原微信窗口并打开要分析的聊天，再尝试读取文字。",
    "unavailable": "当前微信没有公开可读取的消息列表文字。这种微信版本或画面不支持此读取方式；请先复制聊天文字导入。",
    "ambiguous": "检测到多个可读消息区域，无法确定目标聊天。请关闭额外聊天预览，再选择需要的微信窗口。",
    "empty": "当前消息区域没有可读取的文字。请打开具体聊天并显示近期消息；图片或未公开的文字不能直接读取。",
    "failed": "读取微信消息文字失败，请重新打开目标聊天后重试；也可先复制聊天文字导入。",
    "timeout": "读取微信消息文字超时，已停止本次读取。请保持目标聊天可见后重试。",
    "invalid": "文字读取程序没有返回有效的聊天内容，请重试或先复制聊天文字导入。",
}


class ReaderFailure(CaptureError):
    def __init__(self, code):
        self.code = code if code in _ERRORS else "failed"
        super().__init__(_ERRORS[self.code])


def _visible(control):
    try:
        return not bool(control.IsOffscreen)
    except Exception:
        return False


def _children(control):
    try:
        return control.GetChildren()
    except Exception:
        raise ReaderFailure("unavailable") from None


def _type(control):
    try:
        return control.ControlTypeName
    except Exception:
        raise ReaderFailure("unavailable") from None


def _name(control):
    try:
        value = control.Name
        return value.replace("\x00", "").strip() if isinstance(value, str) else ""
    except Exception:
        raise ReaderFailure("unavailable") from None


def _find_message_list(root):
    """Search structure only; never descend into unrelated named lists."""
    pending = deque(((root, 0),))
    found = []
    visited = 0
    truncated = False
    while pending and visited < 500:
        control, depth = pending.popleft()
        visited += 1
        if not _visible(control):
            continue
        kind = _type(control)
        if kind == "ListControl":
            if _name(control) in MESSAGE_LIST_NAMES:
                found.append(control)
            # An unrelated contact/session list must never be read or traversed.
            continue
        if kind in {"ListItemControl", "DataItemControl", "TextControl", "EditControl", "ButtonControl", "MenuControl", "TreeControl"}:
            continue
        if depth < 12:
            children = _children(control)
            available = max(0, 500 - visited - len(pending))
            truncated = truncated or len(children) > available
            pending.extend((child, depth + 1) for child in children[:available])
        elif _children(control):
            truncated = True
    if pending or truncated:
        # A partial search cannot establish that an already-found message list
        # is the only candidate in this selected window.
        raise ReaderFailure("unavailable")
    if len(found) > 1:
        raise ReaderFailure("ambiguous")
    if not found:
        raise ReaderFailure("unavailable")
    return found[0]


def _message_text(item):
    if not _visible(item):
        return ""
    if _type(item) not in {"ListItemControl", "DataItemControl", "TextControl", "CustomControl", "PaneControl"}:
        return ""
    names = []
    value = _name(item)
    if value:
        names.append(value)
    pending = deque((child, 1) for child in _children(item))
    seen = 0
    while pending and seen < 80:
        child, depth = pending.popleft()
        seen += 1
        if not _visible(child):
            continue
        kind = _type(child)
        if kind == "TextControl":
            value = _name(child)
            if value and value not in names and not any(value in name for name in names):
                names.append(value)
        elif kind in {"ListControl", "EditControl", "ButtonControl", "MenuControl", "TreeControl"}:
            continue
        if depth < 5:
            pending.extend((descendant, depth + 1) for descendant in _children(child)[:max(0, 80 - seen - len(pending))])
    return "\n".join(names)


def extract_messages(root):
    """Extract a bounded snapshot of visible entries, without speaker inference."""
    message_list = _find_message_list(root)
    items = _children(message_list)
    texts = [_message_text(item) for item in items[-MAX_ENTRIES:]]
    texts = [text for text in texts if text]
    if not texts:
        raise ReaderFailure("empty")
    budget = MAX_TEXT - len(_PREFIX)
    chosen = []
    used = 0
    for text in reversed(texts):
        if len(text) + used + (2 if chosen else 0) > budget:
            if chosen:
                break
            suffix = "…（消息已截断）"
            chosen.append(text[:budget - len(suffix)] + suffix)
            break
        chosen.append(text)
        used += len(text) + (2 if len(chosen) > 1 else 0)
    chosen.reverse()
    return {"text": _PREFIX + "\n\n".join(chosen), "count": len(chosen), "source": "uia", "identity_verified": False}


def _read_live(window):
    if sys.platform != "win32":
        raise ReaderFailure("unsupported_os")
    api = WindowsAPI()
    actual = api.identify(window.handle)
    if actual is None or actual.pid != window.pid:
        raise ReaderFailure("not_found")
    if api.user.IsIconic(window.handle):
        raise ReaderFailure("minimized")
    try:
        import uiautomation as auto
    except ImportError:
        raise ReaderFailure("missing_dependency") from None
    # The package defaults to an AutomationLog file; disable it before any COM
    # tree calls. No control names or messages should become log records.
    auto.Logger.SetLogFile("")
    with auto.UIAutomationInitializerInThread():
        root = auto.ControlFromHandle(window.handle)
        if root is None:
            raise ReaderFailure("unavailable")
        result = extract_messages(root)
    # Check for window-handle reuse while COM calls were being made. This does
    # not verify which conversation is open inside the same surviving window.
    actual = api.identify(window.handle)
    if actual is None or actual.pid != window.pid:
        raise ReaderFailure("not_found")
    return result


def read_conversation(window, timeout=8):
    """Read once in a bounded child process; never submit any text to an AI."""
    command = [sys.executable, str(Path(__file__).resolve()), "--read-window", str(window.handle), str(window.pid)]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, creationflags=flags, check=False)
    except subprocess.TimeoutExpired:
        raise ReaderFailure("timeout") from None
    except OSError:
        raise ReaderFailure("failed") from None
    if result.returncode:
        code = "failed"
        try:
            data = json.loads(result.stderr)
            if isinstance(data, dict) and isinstance(data.get("reader_error"), str):
                code = data["reader_error"]
        except (ValueError, UnicodeDecodeError, TypeError):
            pass
        raise ReaderFailure(code)
    try:
        if len(result.stdout) > 160000:
            raise ValueError("oversize protocol")
        data = json.loads(result.stdout)
        if not isinstance(data, dict) or not isinstance(data.get("text"), str) or not data["text"].strip() or len(data["text"]) > MAX_TEXT:
            raise ValueError("invalid text")
        if type(data.get("count")) is not int or not 1 <= data["count"] <= MAX_ENTRIES or data.get("source") != "uia" or data.get("identity_verified") is not False:
            raise ValueError("invalid metadata")
        return {key: data[key] for key in ("text", "count", "source", "identity_verified")}
    except (ValueError, TypeError, UnicodeDecodeError):
        raise ReaderFailure("invalid") from None


def _reader_cli(args):
    try:
        if len(args) != 3 or args[0] != "--read-window":
            raise ValueError("invalid arguments")
        handle, pid = int(args[1]), int(args[2])
        if handle <= 0 or pid <= 0:
            raise ValueError("invalid identity")
        # Third-party diagnostics must not contaminate the pipe or write chat
        # bodies to a console; only this function's fixed protocol is emitted.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            data = _read_live(Window(handle, pid, ""))
        sys.stdout.write(json.dumps(data, ensure_ascii=True))
        return 0
    except ReaderFailure as error:
        code = error.code
    except (ValueError, TypeError):
        code = "not_found"
    except Exception:
        code = "failed"
    sys.stderr.write(json.dumps({"reader_error": code}, ensure_ascii=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(_reader_cli(sys.argv[1:]))
