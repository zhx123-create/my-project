"""Mouse-only entry and progress near the currently focused WeChat window."""
import queue
import time

from floating_window import FloatingCard


def window_key(window):
    return window.handle, window.pid, window.title


class FloatingMixin:
    def toggle_floating(self):
        enabled = self.floating_enabled.get()
        self.presence_watcher.set_enabled(enabled)
        if not enabled:
            self.cancel_quickfill(preserve_focus=True)
            if self.floating_card is not None:
                self.floating_card.hide()
        self.remember_ui_preferences()

    def dismiss_floating(self):
        self.floating_enabled.set(False)
        self.toggle_floating()

    def floating_action(self):
        if self.quickfill_job is not None:
            self.cancel_quickfill(preserve_focus=True)
        elif self.busy or self.robot is not None:
            self.floating_details()
        elif self.floating_target is not None:
            self.start_quickfill(from_hotkey=True, from_float=True)

    def floating_details(self):
        self.root.deiconify()
        self.root.lift()

    def floating_begin(self, choosing=False):
        self.floating_progress.begin(time.monotonic(), 'choosing' if choosing else 'reading')

    def floating_phase(self, phase):
        self.floating_progress.advance(phase, time.monotonic())

    def floating_view(self, now):
        if self.quickfill_job is None and (self.busy or self.robot is not None):
            generating = self.active_operation == 'generate' and self.job_started is not None
            return {'title': '正在生成草稿' if generating else '助手正在处理中',
                    'detail': '点查看返回主窗口', 'action': '查看', 'phase': 'other_generation' if generating else 'other_task',
                    'active': generating, 'timed': generating,
                    'seconds': max(0, int(now - self.job_started)) if generating else 0}
        return self.floating_progress.view(now)

    def poll_floating(self):
        while True:
            try:
                presence = self.presence_events.get_nowait()
            except queue.Empty:
                break
            if not self.floating_enabled.get():
                presence = None
            target = presence[0] if presence is not None else None
            bound = self.quickfill_window
            if self.quickfill_job is not None and bound is not None and (target is None or window_key(target) != window_key(bound)):
                # A new foreground window never silently retargets a running fill.
                self.cancel_quickfill(preserve_focus=True)
            if target is not None:
                key = window_key(target)
                if key != self.floating_last_key and self.quickfill_job is None:
                    self.floating_progress.reset()
                self.floating_last_key = key
                self.floating_target, self.floating_bounds, self.floating_work = presence
            else:
                self.floating_target = None
                if self.floating_card is not None:
                    self.floating_card.hide()
        if not self.floating_enabled.get() or self.floating_target is None:
            return
        if self.floating_card is None:
            try:
                self.floating_card = FloatingCard(self.root, self.floating_action, self.dismiss_floating, self.floating_details)
            except Exception:
                self.floating_enabled.set(False)
                self.presence_watcher.set_enabled(False)
                self.status.set('微信浮窗暂时无法显示，仍可使用主界面的“一键填入微信”。')
                return
        now = time.monotonic()
        self.floating_card.present(window_key(self.floating_target), self.floating_bounds, self.floating_work,
                                   self.floating_view(now), now)

    def stop_floating(self):
        self.presence_watcher.stop()
        if self.floating_card is not None:
            self.floating_card.close()
            self.floating_card = None
