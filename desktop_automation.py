"""Runtime-only orchestration for explicit, scoped desktop automation."""
import queue
import threading
import time
import tkinter as tk
from tkinter import simpledialog, ttk

from windows_capture import CaptureError, list_windows
from wechat_reader import read_conversation


class AutomationMixin:
    def quick_analyze(self):
        if self.busy:
            return
        if not self.active_profile:
            name = simpledialog.askstring("为当前聊天起名", "这是谁的聊天？填写一个名称，之后会分别记住背景和表达风格。", parent=self.root)
            if not name or not name.strip():
                self.stop_watch("尚未选择会话")
                return
            requested = self._watch_start_requested
            self.select_profile(name.strip())
            self._watch_start_requested = requested
            if not self.active_profile:
                self.stop_watch("尚未选择会话")
                return
        if not self.key.get().strip():
            self.resume_action = self.quick_analyze
            self.show_settings()
            return
        # Clicking the named read-and-generate action explicitly submits this
        # selected conversation; permission is not inferred from a clipboard.
        self.consent.set(True)
        index = self.window_box.current()
        if 0 <= index < len(self.windows):
            if self._watch_start_requested:
                self._begin_watch(self.windows[index])
            if self.watch.active:
                self._read_watch()
            else:
                self.analyze_current_chat()
            return
        self.set_busy(True, operation="discover")
        self.status.set("正在查找微信窗口…找到后会读取当前聊天并生成草稿。")
        token, events = self._discovery_token, self.discovery_events
        def discover():
            try:
                events.put((token, True, list_windows()))
            except CaptureError as error:
                events.put((token, False, str(error)))
            except Exception:
                events.put((token, False, "未能找到微信。请打开电脑版微信，也可在更多工具中导入文字。"))
        threading.Thread(target=discover, daemon=True).start()

    def poll_discovery(self):
        try:
            token, success, value = self.discovery_events.get_nowait()
        except queue.Empty:
            return
        self.set_busy(False)
        if token != self._discovery_token:
            return
        if not success or not value:
            self.stop_watch("没有可用的微信窗口")
            self.status.set(value if not success else "没有找到微信。打开并登录微信后，再点一键分析。")
            return
        self.windows = value
        self.window_box.configure(values=[f"{index + 1}. {window.title}" for index, window in enumerate(value)])
        if len(value) == 1:
            self.window_box.current(0)
            self.quick_analyze()
            return
        self.status.set("检测到多个微信窗口，请选择一次要使用的窗口。")
        dialog = tk.Toplevel(self.root)
        dialog.title("选择微信窗口"); dialog.transient(self.root)
        panel = ttk.Frame(dialog, padding=20); panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="只读取你选中的这个窗口。请先打开允许分析的聊天。", wraplength=420).pack(anchor="w", pady=(0, 12))
        choice = ttk.Combobox(panel, values=[f"{index + 1}. {window.title}" for index, window in enumerate(value)], state="readonly", width=45)
        choice.pack(fill="x"); choice.current(0)
        def use():
            self.window_box.current(choice.current())
            dialog.destroy(); self.quick_analyze()
        def cancel():
            self.stop_watch("尚未选择窗口"); dialog.destroy()
        ttk.Button(panel, text="使用这个窗口", command=use, style="Accent.TButton").pack(anchor="e", pady=(14, 0))
        dialog.protocol("WM_DELETE_WINDOW", cancel); dialog.grab_set()

    def toggle_watch(self):
        if self.watch.active or self._watch_start_requested:
            self.stop_watch("你已暂停自动辅助")
            return
        if self.busy:
            self.status.set("当前操作完成后，即可开启自动辅助。")
            return
        self._watch_start_requested = True
        self.watch_button.configure(text="取消开启", style="Soft.TButton")
        self.watch_status.set("正在准备自动辅助…")
        self.quick_analyze()

    def _begin_watch(self, window):
        self.watch.interval = self.watch_interval.get()
        has_draft = any(editor.get("1.0", "end-1c").strip() for editor in self.reply_texts)
        baseline = self.last_reader_text if has_draft and self._last_reader_profile == self.active_profile else ""
        self.watch.start(self.active_profile, window, baseline_text=baseline)
        self._watch_start_requested = False
        self._pending_watch_snapshot = None
        self.watch_button.configure(text="暂停自动辅助", style="Accent.TButton")
        self.watch_status.set(f"自动辅助中 · 每 {self.watch.interval} 秒检查 · 请保持微信停留在此聊天")

    def stop_watch(self, reason="已暂停自动辅助"):
        self.watch.stop(reason)
        self._watch_start_requested = False
        self._pending_watch_snapshot = None
        self._discovery_token += 1
        self.watch_status.set(f"已暂停 · {reason}")
        if hasattr(self, "watch_button"):
            self.watch_button.configure(text="开启自动辅助", style="Soft.TButton")

    def _read_watch(self):
        if self.busy or not self.watch.active:
            return
        self.watch.mark_check(time.monotonic())
        token, window, profile = self.watch.generation, self.watch.window, self.watch.profile
        self.set_busy(True, operation="watch_read")
        self.watch_status.set("自动辅助中 · 正在检查新内容…")
        events = self.reader_events
        def read():
            try:
                current = next((candidate for candidate in list_windows() if candidate.handle == window.handle and candidate.pid == window.pid), None)
                if current is None or current.title != window.title:
                    raise CaptureError("微信窗口已关闭或标题发生变化，请核对聊天后重新开启自动辅助。")
                snapshot = read_conversation(window)
                events.put((True, {"__watch_token": token, "profile": profile, "snapshot": snapshot}))
            except CaptureError as error:
                events.put((False, {"__watch_token": token, "error": str(error)}))
            except Exception:
                events.put((False, {"__watch_token": token, "error": "微信文字读取失败，自动辅助已暂停。"}))
        threading.Thread(target=read, daemon=True).start()

    def consume_watch_read(self, success, value):
        if not isinstance(value, dict) or "__watch_token" not in value:
            return False
        if not self.watch.active or value["__watch_token"] != self.watch.generation:
            return True
        if not success:
            error = value["error"]
            self.stop_watch("读取未完成，请检查微信")
            self.status.set(error)
            return True
        if value["profile"] != self.active_profile:
            self.stop_watch("所选会话已改变")
            return True
        snapshot = value["snapshot"]
        try:
            change = self.watch.accept_snapshot(snapshot["text"])
        except ValueError:
            self.stop_watch("未能确认有效的新内容")
            return True
        if change == "discontinuous":
            self.stop_watch("聊天内容不连续，请核对当前联系人后重新开启")
            self.status.set("本次内容没有加入记忆或提交 AI。请确认微信仍打开的是这个聊天。")
        elif change == "changed":
            self._pending_watch_snapshot = snapshot
            if self._draft_waiting():
                self.watch_status.set("有新内容 · 当前草稿已保留，复制或记为已发送后继续")
            else:
                self._generate_pending_snapshot()
        else:
            self.watch_status.set("自动辅助中 · 暂无新内容" if not self._pending_watch_snapshot else "有新内容 · 先处理当前草稿，再继续生成")
        return True

    def _draft_waiting(self):
        current = tuple(editor.get("1.0", "end-1c") for editor in self.reply_texts)
        return any(text.strip() for text in current) and (not self._draft_reviewed or current != self._reviewed_reply_texts)

    def _generate_pending_snapshot(self):
        if not self.watch.active or self.busy or self._pending_watch_snapshot is None:
            return
        snapshot = self._pending_watch_snapshot
        self._pending_watch_snapshot = None
        self.remove_image()
        self.context.delete("1.0", "end"); self.context.insert("1.0", snapshot["text"])
        self.last_reader_text = snapshot["text"]
        self._last_reader_profile = self.active_profile
        self._next_generation_watch_token = self.watch.generation
        self._next_generation_watch_text = snapshot["text"]
        self.consent.set(True)
        self.request_generation()
        if not self.busy:
            self._next_generation_watch_token = None
            self.stop_watch("生成未能开始，请检查 AI 设置和会话")

    def automation_tick(self):
        if not self.watch.active:
            return
        if self.active_profile != self.watch.profile:
            self.stop_watch("所选会话已改变"); return
        index = self.window_box.current()
        if not 0 <= index < len(self.windows) or not self.watch.matches_window(self.windows[index]):
            self.stop_watch("所选窗口已改变"); return
        if self.busy:
            return
        if self._pending_watch_snapshot and not self._draft_waiting():
            self._generate_pending_snapshot()
        elif self.watch.due(time.monotonic()):
            self._read_watch()

    def remember_ui_preferences(self):
        try:
            if self.watch.active and self.watch.interval != self.watch_interval.get():
                self.watch.interval = self.watch_interval.get()
                self.watch.mark_check(time.monotonic())
                self.watch_status.set(f"自动辅助中 · 每 {self.watch.interval} 秒检查")
            fields = {"auto_copy": self.auto_copy.get(), "watch_interval": self.watch_interval.get(), "last_profile": self.active_profile if self.memory.persistent and self.active_profile else ""}
            # An encrypted key stays paired with the endpoint chosen when it
            # was explicitly saved. Runtime environment credentials may differ.
            if not self.settings.values()["remember_key"]:
                fields.update(base=self.base.get().strip(), model=self.model.get().strip(), allow_http=self.allow_http.get())
            self.settings.update(**fields)
        except (ValueError, OSError):
            self.status.set("本次设置仍可使用；未能保存界面偏好，请检查本地文件权限。")

    def generate_from_editor(self):
        self.consent.set(True)
        self.request_generation()
