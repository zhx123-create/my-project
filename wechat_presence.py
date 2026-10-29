"""Foreground WeChat metadata only. Never read chats, capture, or call an AI."""
import ctypes
import queue
import sys
import threading

from visible_capture import VisibleWindowsAPI


def work_area(user, handle):
    from ctypes import wintypes as w
    class MonitorInfo(ctypes.Structure):
        _fields_ = [('size', w.DWORD), ('monitor', w.RECT), ('work', w.RECT), ('flags', w.DWORD)]
    user.MonitorFromWindow.argtypes, user.MonitorFromWindow.restype = [w.HWND, w.DWORD], w.HANDLE
    user.GetMonitorInfoW.argtypes, user.GetMonitorInfoW.restype = [w.HANDLE, ctypes.POINTER(MonitorInfo)], w.BOOL
    monitor = user.MonitorFromWindow(handle, 2)
    info = MonitorInfo(); info.size = ctypes.sizeof(info)
    if monitor and user.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return info.work.left, info.work.top, info.work.right, info.work.bottom
    x, y, width, height = (user.GetSystemMetrics(index) for index in (76, 77, 78, 79))
    return x, y, x + width, y + height


class WeChatPresence:
    def __init__(self, events):
        self.events = events
        self.stop_event = threading.Event()
        self.enabled = threading.Event(); self.enabled.set()

    def start(self):
        if sys.platform == 'win32':
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()

    def set_enabled(self, enabled):
        self.enabled.set() if enabled else self.enabled.clear()

    def stop(self):
        self.stop_event.set()
        thread = getattr(self, 'thread', None)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=.5)

    def put_latest(self, value):
        try:
            self.events.put_nowait(value)
        except queue.Full:
            try:
                self.events.get_nowait()
            except queue.Empty:
                pass
            try:
                self.events.put_nowait(value)
            except queue.Full:
                pass

    def run(self):
        try:
            api = VisibleWindowsAPI()
        except Exception:
            self.put_latest(None)
            return
        previous = None
        while not self.stop_event.is_set():
            current = None
            try:
                if self.enabled.is_set():
                    handle = api.user.GetForegroundWindow()
                    window = api.identify(handle) if handle else None
                    if window is not None and not api.user.IsIconic(handle):
                        current = (window, api.bounds(handle), work_area(api.user, handle))
            except Exception:
                current = None
            if current != previous:
                self.put_latest(current)
                previous = current
            self.stop_event.wait(.25)
