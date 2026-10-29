"""Register one explicit shortcut; no keyboard hook or key logging."""
import ctypes
import sys
import threading


class WeChatHotkey:
    def __init__(self, events):
        self.events = events
        self.stop_event = threading.Event()

    def start(self):
        if sys.platform == 'win32':
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        thread = getattr(self, 'thread', None)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=.3)

    def run(self):
        from ctypes import wintypes as w
        user = ctypes.WinDLL('user32', use_last_error=True)
        user.RegisterHotKey.argtypes = (w.HWND, ctypes.c_int, w.UINT, w.UINT)
        user.RegisterHotKey.restype = w.BOOL
        user.UnregisterHotKey.argtypes = (w.HWND, ctypes.c_int)
        user.UnregisterHotKey.restype = w.BOOL
        user.PeekMessageW.argtypes = (ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT)
        user.PeekMessageW.restype = w.BOOL
        identifier = 0x4A48
        registered = False
        try:
            # ALT | CONTROL | NOREPEAT, R. The registering thread owns the queue.
            registered = bool(user.RegisterHotKey(None, identifier, 0x4003, ord('R')))
            if not registered:
                self.events.put(('hotkey_error', 'Ctrl+Alt+R 快捷键未能启用，可能已被占用；仍可点击“一键填入微信”。'))
                return
            while not self.stop_event.is_set():
                message = w.MSG()
                while user.PeekMessageW(ctypes.byref(message), None, 0x0312, 0x0312, 1):
                    if message.wParam == identifier:
                        self.events.put(('hotkey', None))
                self.stop_event.wait(.03)
        except Exception:
            self.events.put(('hotkey_error', '快捷键暂不可用；仍可点击“一键填入微信”。'))
        finally:
            if registered:
                user.UnregisterHotKey(None, identifier)
