"""Compact progress card; on Windows it never activates or takes input focus."""
import ctypes
from dataclasses import dataclass
import os
import sys
import tkinter as tk


ACTIVE_PHASES = frozenset(('choosing', 'reading', 'generating', 'filling'))


@dataclass
class FloatingProgress:
    phase: str = 'idle'
    started: float | None = None
    finished: float | None = None

    def begin(self, now, phase='reading'):
        self.phase, self.started, self.finished = phase, now, None

    def advance(self, phase, now):
        self.phase = phase
        if phase not in ACTIVE_PHASES and self.started is not None:
            self.finished = now

    def reset(self):
        self.phase, self.started, self.finished = 'idle', None, None

    def seconds(self, now):
        end = self.finished if self.finished is not None else now
        return max(0, int(end - self.started)) if self.started is not None else 0

    def view(self, now):
        titles = {'idle': '帮你接下一句', 'choosing': '等待选择输入框', 'reading': '正在读取聊天',
                  'generating': '正在生成回复', 'filling': '正在填入微信', 'filled': '回复已填入',
                  'submitted': '文字已提交', 'error': '本次未完成', 'cancelled': '已取消填入'}
        details = {'idle': '先点微信空白输入框', 'choosing': '请点击微信空白输入框',
                   'reading': '请暂勿操作键鼠', 'generating': '请暂勿操作键鼠', 'filling': '核对聊天及输入焦点',
                   'filled': '检查后由你发送', 'submitted': '请核对微信输入框',
                   'error': '查看原因', 'cancelled': '请先检查微信输入框'}
        active = self.phase in ACTIVE_PHASES
        return {'title': titles.get(self.phase, titles['idle']), 'detail': details.get(self.phase, details['idle']),
                'seconds': self.seconds(now), 'active': active, 'phase': self.phase, 'timed': self.started is not None,
                'action': '取消' if active else ('再试' if self.phase == 'error' else '下一句' if self.phase in ('filled', 'submitted') else '生成')}


def card_position(bounds, work, size=(272, 100), offset=None):
    left, top, right, bottom = bounds
    width, height = size
    if offset is not None:
        x, y = left + offset[0], top + offset[1]
    elif right + width + 12 <= work[2]:
        x, y = right + 12, top + 36
    elif left - width - 12 >= work[0]:
        x, y = left - width - 12, top + 36
    else:
        # Keep recent messages and the composer clear on a maximized chat.
        x, y = right - width - 16, top + 12
    return (max(work[0], min(int(x), work[2] - width)),
            max(work[1], min(int(y), work[3] - height)))


class NonActivatingWindow:
    def __init__(self, window):
        from ctypes import wintypes as w
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.get_long = getattr(self.user, 'GetWindowLongPtrW', None) or self.user.GetWindowLongW
        self.set_long = getattr(self.user, 'SetWindowLongPtrW', None) or self.user.SetWindowLongW
        for function, args, result in (
            (self.get_long, [w.HWND, ctypes.c_int], ctypes.c_ssize_t),
            (self.set_long, [w.HWND, ctypes.c_int, ctypes.c_ssize_t], ctypes.c_ssize_t),
            (self.user.GetAncestor, [w.HWND, w.UINT], w.HWND),
            (self.user.ShowWindow, [w.HWND, ctypes.c_int], w.BOOL),
            (self.user.SetWindowPos, [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT], w.BOOL),
            (self.user.CallWindowProcW, [ctypes.c_void_p, w.HWND, w.UINT, ctypes.c_size_t, ctypes.c_ssize_t], ctypes.c_ssize_t),
            (self.user.IsWindow, [w.HWND], w.BOOL),
            (self.user.GetWindowThreadProcessId, [w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
        ):
            function.argtypes, function.restype = args, result
        self.handle = self.user.GetAncestor(window.winfo_id(), 2) or window.winfo_id()
        pid = w.DWORD()
        self.user.GetWindowThreadProcessId(self.handle, ctypes.byref(pid))
        if not self.handle or pid.value != os.getpid():
            raise OSError('floating window unavailable')
        # Independent tool window: it remains available when the main UI is minimized.
        self.set_long(self.handle, -8, 0)  # GWLP_HWNDPARENT
        self.set_long(self.handle, -20, (self.get_long(self.handle, -20) | 0x08000080) & ~0x40000)
        prototype = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, ctypes.c_size_t, ctypes.c_ssize_t)
        self.previous = self.get_long(self.handle, -4)
        if not self.previous:
            raise OSError('floating window unavailable')
        self.callback = prototype(self.dispatch)
        previous = self.set_long(self.handle, -4, ctypes.cast(self.callback, ctypes.c_void_p).value)
        if not previous:
            raise OSError('floating window unavailable')
        self.previous = previous

    def dispatch(self, handle, message, wparam, lparam):
        if message == 0x21:  # WM_MOUSEACTIVATE
            return 3  # MA_NOACTIVATE: clicks still reach the card.
        return self.user.CallWindowProcW(self.previous, handle, message, wparam, lparam)

    def show(self, x, y, width, height):
        # Avoid Tk wm deiconify: on Windows that command explicitly activates.
        self.user.SetWindowPos(self.handle, -1, x, y, width, height, 0x0010 | 0x0040)
        self.user.ShowWindow(self.handle, 4)  # SW_SHOWNOACTIVATE

    def hide(self):
        self.user.ShowWindow(self.handle, 0)

    def close(self):
        if self.user.IsWindow(self.handle):
            self.set_long(self.handle, -4, self.previous)


class FloatingCard:
    width, height = 272, 100

    def __init__(self, root, action, dismiss, details):
        self.scale = max(1.0, min(3.0, root.winfo_fpixels('1i') / 96))
        self.width, self.height = round(272 * self.scale), round(100 * self.scale)
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.configure(background='#f4f1ff')
        self.window.attributes('-topmost', True)
        self.window.geometry(f'{self.width}x{self.height}')
        self.canvas = tk.Canvas(self.window, width=self.width, height=self.height, highlightthickness=0,
                                borderwidth=0, background='#f4f1ff', takefocus=False)
        self.canvas.pack(fill='both', expand=True)
        self.action, self.dismiss, self.details = action, dismiss, details
        self.visible, self.native, self.offset = False, None, None
        self.target_key, self.bounds, self.work = None, None, None
        self.drag = None
        self.last_stamp = None
        self.view = FloatingProgress().view(0)
        self.font = 'Microsoft YaHei UI' if sys.platform == 'win32' else 'Noto Sans CJK SC'
        self.canvas.bind('<ButtonPress-1>', lambda event: self.pointer(self.press, event))
        self.canvas.bind('<ButtonRelease-1>', lambda event: self.pointer(self.release, event))
        self.canvas.bind('<B1-Motion>', lambda event: self.pointer(self.move, event))
        self.canvas.bind('<Motion>', self.hover)
        self.window.update_idletasks()
        if sys.platform == 'win32':
            try:
                self.native = NonActivatingWindow(self.window)
            except Exception:
                self.window.destroy()
                raise

    @staticmethod
    def pointer(handler, event):
        handler(event)
        return 'break'  # Prevent Tk class bindings from explicitly taking focus.

    def present(self, target_key, bounds, work, view, now):
        changed = not self.visible or (target_key, bounds, work) != (self.target_key, self.bounds, self.work)
        if target_key != self.target_key:
            self.offset = None
        self.target_key, self.bounds, self.work = target_key, bounds, work
        x, y = card_position(bounds, work, (self.width, self.height), self.offset)
        if changed:
            if self.native is not None:
                # Tk "wm state normal" uses SW_SHOWNOACTIVATE, while
                # "wm deiconify" additionally requests input focus.
                self.window.state('normal')
                self.native.show(x, y, self.width, self.height)
            else:
                self.window.geometry(f'{self.width}x{self.height}{x:+d}{y:+d}')
                self.window.deiconify()
        self.visible = True
        self.render(view, now)

    def render(self, view, now):
        self.view = view
        stamp = (view['phase'], view['seconds'], view['title'], view['detail'], view['action'], int(now * 4) % 4 if view['active'] else 0)
        if stamp == self.last_stamp:
            return
        self.last_stamp = stamp
        canvas = self.canvas
        canvas.delete('all')
        width = 272
        small, title, button = -round(12 * self.scale), -round(15 * self.scale), -round(13 * self.scale)
        canvas.create_rectangle(1, 1, width - 1, 99, fill='#fcfbff', outline='#d9d1fa', width=2)
        canvas.create_oval(14, 13, 22, 21, fill='#7963df', outline='')
        canvas.create_text(29, 17, anchor='w', text='接话', font=(self.font, small, 'bold'), fill='#7963df')
        if view.get('timed', view['phase'] != 'idle'):
            timer = ('已等 ' if view['active'] else '共用 ') + str(view['seconds']) + ' 秒'
            canvas.create_text(width - 37, 17, anchor='e', text=timer, font=(self.font, small), fill='#82749f')
        canvas.create_text(width - 16, 17, text='×', font=(self.font, -round(18 * self.scale)), fill='#9d93b4')
        canvas.create_text(16, 43, anchor='w', text=view['title'], font=(self.font, title, 'bold'), fill='#26314a')
        canvas.create_text(16, 74, anchor='w', text=view['detail'], font=(self.font, small, 'underline' if view['phase'] == 'error' else 'normal'), fill='#82749f')
        canvas.create_rectangle(width - 88, 57, width - 14, 87, fill='#7963df', outline='')
        canvas.create_text(width - 51, 72, text=view['action'], font=(self.font, button, 'bold'), fill='white')
        if view['active']:
            canvas.create_arc(width - 29, 36, width - 17, 48, start=int(now * 240) % 360, extent=250, style='arc', outline='#7963df', width=2)
        canvas.scale('all', 0, 0, self.scale, self.scale)

    def press(self, event):
        self.drag = (event.x_root, event.y_root, self.window.winfo_x(), self.window.winfo_y(), event.x, event.y)

    def move(self, event):
        if not self.drag or self.drag[5] > 27 * self.scale or self.drag[4] >= self.width - 32 * self.scale or self.bounds is None:
            return
        x = self.drag[2] + event.x_root - self.drag[0]
        y = self.drag[3] + event.y_root - self.drag[1]
        self.offset = (x - self.bounds[0], y - self.bounds[1])
        x, y = card_position(self.bounds, self.work, (self.width, self.height), self.offset)
        if self.native:
            self.native.show(x, y, self.width, self.height)
        else:
            self.window.geometry(f'{self.width}x{self.height}{x:+d}{y:+d}')

    def release(self, event):
        press, self.drag = self.drag, None
        if press is None or abs(event.x_root - press[0]) + abs(event.y_root - press[1]) > 6:
            return
        x, y = event.x / self.scale, event.y / self.scale
        if x >= 240 and y < 30:
            self.dismiss()
        elif x >= 184 and 54 <= y <= 90:
            self.action()
        elif x < 178 and y >= 58 and self.view['phase'] == 'error':
            self.details()

    def hover(self, event):
        clickable = event.x >= self.width - 88 * self.scale or self.view['phase'] == 'error' and event.y >= 58 * self.scale
        self.canvas.configure(cursor='hand2' if clickable else 'fleur' if event.y < 28 * self.scale else '')

    def hide(self):
        if self.native:
            self.native.hide()
        else:
            self.window.withdraw()
        self.visible = False

    def close(self):
        if self.native:
            self.native.close()
        self.window.destroy()
