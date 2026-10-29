"""Bound local OCR plus an explicit model-vision route for approved crops."""
from dataclasses import dataclass
import hashlib
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageGrab
from visible_capture import activate_window, capture_visible_isolated
from windows_capture import CaptureError, Window, list_windows, pick_foreground_window, selected_window_state
from windows_ocr import recognize_image

TEXT_SOURCE = "窗口文字"
SCREENSHOT_SOURCE = "截图转文字"
VISION_HEADER = "【大模型截图转写；可能有错字，发言者及会话身份请核对】\n"


@dataclass(frozen=True)
class ScreenshotBinding:
    window: Window
    profile: str
    region: tuple
    size: tuple


class ScreenshotReader:
    def __init__(self, binding):
        self.binding = binding
        self._digest = None
        self._snapshot = None

    def matches(self, profile, window):
        other = self.binding.window
        return self.binding.profile == profile and (other.handle, other.pid, other.title) == (window.handle, window.pid, window.title)

    def read(self, window):
        if not self.matches(self.binding.profile, window):
            raise CaptureError("截图绑定窗口已变化，请重新框选聊天区域。")
        image = capture_visible_isolated(window, self.binding.region, self.binding.size)
        current = selected_window_state(window) if getattr(window, "user_selected", False) else next((item for item in list_windows() if item.handle == window.handle and item.pid == window.pid), None)
        if current is None or not self.matches(self.binding.profile, current):
            raise CaptureError("聊天窗口已变化，本次截图未提交给 AI。请重新绑定。")
        return self.image_to_snapshot(image)

    def image_to_snapshot(self, image):
        digest = hashlib.sha256(image.convert("RGB").tobytes()).digest()
        if digest != self._digest:
            snapshot = recognize_image(image)
            self._snapshot, self._digest = snapshot, digest
        return dict(self._snapshot)


class ScreenshotMixin:
    def offer_manual_window_binding(self):
        if self.busy:
            return
        existing = getattr(self, "window_picker_dialog", None)
        if existing is not None and existing.winfo_exists():
            existing.lift(); return
        self.stop_watch("正在手动选择聊天窗口")
        dialog = tk.Toplevel(self.root)
        self.window_picker_dialog = dialog
        dialog.title("手动绑定聊天窗口"); dialog.transient(self.root)
        panel = ttk.Frame(dialog, padding=20); panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="自动找不到微信时，可以直接点选聊天窗口。\n\n点“开始选择”后，助手会缩小。请在 3 秒内点击你单独打开的微信聊天窗口，保持它在最前面。之后会预览这个窗口，只需框选消息区。\n\n程序不会自动验证它是否属于微信，请核对预览；选区确认前不会提交给 AI。", wraplength=440).pack(anchor="w", pady=(0, 14))
        def start():
            token = self._discovery_token
            dialog.destroy()
            self.status.set("请在 3 秒内点击微信聊天窗口，让它保持在最前面。")
            self.set_busy(True, operation="pick_window")
            self.root.iconify()
            def finish():
                try:
                    window = pick_foreground_window()
                except CaptureError as error:
                    window, error_text = None, str(error)
                except Exception:
                    window, error_text = None, "未能取得你选择的窗口，请重新开始并点击聊天窗口。"
                self.root.deiconify()
                if token != self._discovery_token:
                    if self.active_operation == "pick_window" and self._busy_source_token == token:
                        self.set_busy(False)
                    return
                self.set_busy(False)
                if window is None:
                    self.status.set(error_text); return
                self.windows = [window]
                self.window_box.configure(values=["手动选择：" + window.title]); self.window_box.current(0)
                self.shot_reader = None
                self.shot_readers.pop(self.active_profile, None)
                self.capture_source.set(SCREENSHOT_SOURCE)
                self.capture_status.set("已选定一个窗口。请核对预览确实是允许分析的聊天，再框选消息区。")
                self.quick_analyze()
            self.root.after(3000, finish)
        ttk.Button(panel, text="开始选择（3 秒）", command=start, style="Accent.TButton").pack(side="right")
        ttk.Button(panel, text="取消", command=dialog.destroy).pack(side="right", padx=8)
        dialog.grab_set(); dialog.lift()

    def screenshot_mode(self):
        return self.capture_source.get() == SCREENSHOT_SOURCE

    def primary_action_label(self):
        return "分析微信窗口" if self.screenshot_mode() else "一键分析当前聊天"

    def begin_manual_screenshot(self):
        # A system screenshot/file is already a complete image source. Stop
        # live reads before opening a modal preview or the system snip overlay;
        # modal Tk dialogs do not stop scheduled automation callbacks.
        self.stop_watch("已切换到手动截图，定时采集已暂停")
        self.status.set("使用已有截图，不再采集微信窗口。请导入图片并选择识别方式。")
        self.capture_status.set("手动截图不检查微信窗口的位置、遮挡或是否最小化。")

    def capture_source_changed(self, *_args):
        if self.capture_source.get() == self._active_capture_source:
            return
        self._active_capture_source = self.capture_source.get()
        if self.watch.active:
            self.stop_watch("已切换采集方式")
        self.last_reader_text = None
        self._last_reader_profile = None
        if hasattr(self, "analyze_button") and not self.busy:
            self.analyze_button.configure(text=self.primary_action_label())
        self.remember_ui_preferences()

    def start_screenshot_analysis(self):
        if self.busy:
            return
        self.capture_source.set(SCREENSHOT_SOURCE)
        self.quick_analyze(refresh_windows=True)

    def rebind_screenshot(self):
        if self.busy:
            return
        if self.watch.active:
            self.stop_watch("正在重新框选消息区")
        self.shot_reader = None
        self.shot_readers.pop(self.active_profile, None)
        self.start_screenshot_analysis()

    def bind_chat_region(self, window):
        token, profile, events = self._discovery_token, self.active_profile, self.screenshot_events
        try:
            activate_window(window)
        except CaptureError as error:
            self.stop_watch("聊天截图未能开始")
            self.status.set(str(error)); return
        except Exception:
            self.stop_watch("聊天截图未能开始")
            self.status.set("未能切换到聊天窗口。请把微信聊天单独打开并露出消息区后重试。")
            return
        self.remove_image()
        self.set_busy(True, operation="bind_region")
        self.status.set("正在截取你选中的微信窗口。接下来只需框选一次消息区，不要包含联系人列表。")
        def capture():
            try:
                events.put(("bind", token, profile, window, True, capture_visible_isolated(window, preview=True)))
            except CaptureError as error:
                events.put(("bind", token, profile, window, False, str(error)))
            except Exception:
                events.put(("bind", token, profile, window, False, "聊天窗口截图失败，请将单独聊天窗口露出后重试。"))
        threading.Thread(target=capture, daemon=True).start()

    def read_screenshot_once(self, window):
        reader, token, profile, events = self.shot_reader, self._discovery_token, self.active_profile, self.screenshot_events
        self.remove_image()
        self.set_busy(True, operation="ocr")
        self.status.set("正在自动截取绑定消息区并在本机识字；稍后只把文字交给 AI。")
        def read():
            try:
                events.put(("ocr", token, profile, window, True, reader.read(window)))
            except CaptureError as error:
                events.put(("ocr", token, profile, window, False, str(error)))
            except Exception:
                events.put(("ocr", token, profile, window, False, "截图识字未完成，请检查消息区域是否清晰可见。"))
        threading.Thread(target=read, daemon=True).start()

    def paste_screenshot(self):
        if self.busy:
            return
        self.begin_manual_screenshot()
        try:
            image = ImageGrab.grabclipboard()
            if not isinstance(image, Image.Image):
                self.status.set("剪贴板没有图片。请按 Win+Shift+S 截取聊天后再点“粘贴截图”；已保存的图片可从更多工具打开。")
                return
            self.capture_source.set(SCREENSHOT_SOURCE)
            self.show_crop(image.convert("RGB"), accept=self.analyze_screenshot_image, ocr=True)
        except Exception:
            self.status.set("未能读取剪贴板图片，请重新截图，或从更多工具打开截图文件。")

    def open_screenshot_file(self):
        if self.busy:
            return
        self.begin_manual_screenshot()
        path = filedialog.askopenfilename(parent=self.root, title="打开聊天截图", filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.bmp")])
        if not path:
            return
        try:
            with Image.open(path) as original:
                if original.width * original.height > 40_000_000:
                    raise ValueError("oversized")
                original.load()
                image = original.convert("RGB")
            self.capture_source.set(SCREENSHOT_SOURCE)
            self.show_crop(image, accept=self.analyze_screenshot_image, ocr=True)
        except Exception:
            self.status.set("无法打开这张截图，请选择正常的 PNG、JPEG、WebP 或 BMP 图片，最多 4000 万像素。")

    def analyze_vision_image(self, image):
        if self.busy:
            return
        self.begin_manual_screenshot()
        if not self.key.get().strip():
            self.resume_action = lambda: self.analyze_vision_image(image)
            self.show_settings()
            return
        # The explicit button approves only this cropped image, not future
        # automatic image uploads. Automatic screenshots retain local OCR.
        self.attach_image(image)
        self.consent.set(True)
        self.capture_status.set("已确认选区，将直接交给当前大模型看图；成功后在左侧显示转写文字。")
        self.request_generation()

    def show_vision_transcript(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 3000:
            return "模型未返回截图转写，请检查当前模型的图片输入支持。"
        text = text.strip()
        self.remove_image()
        snapshot = VISION_HEADER + text
        self.context.delete("1.0", "end"); self.context.insert("1.0", snapshot)
        self.last_reader_text = snapshot
        self._last_reader_profile = self.active_profile
        self.capture_status.set("大模型已返回截图转写，显示在左侧；请核对错字和说话者。之后可直接用文字继续分析。")
        if self.active_profile:
            try:
                self.memory.add_context(self.active_profile, text)
            except (ValueError, OSError):
                return "截图文字已显示，但本次会话记忆保存失败，请检查本地文件权限。"
        return "截图转写已显示在左侧，请核对后继续。"

    def analyze_screenshot_image(self, image, watch_seed=False, reader=None):
        if self.busy:
            return
        if not watch_seed and reader is None:
            self.begin_manual_screenshot()
        if not self.key.get().strip():
            self.resume_action = lambda: self.analyze_screenshot_image(image, watch_seed, reader)
            self.show_settings(); return
        self.remove_image()
        token, profile = self._discovery_token, self.active_profile
        watch_token = self.watch.generation
        events = self.reader_events if watch_seed else self.screenshot_events
        if watch_seed:
            self.watch.mark_check(time.monotonic())
        self.set_busy(True, operation="watch_read" if watch_seed else "ocr")
        self.status.set("正在本机识别截图文字；识别成功后会自动分析，不向模型上传图片。")
        def recognize():
            def result(success, value):
                if watch_seed:
                    payload = {"__watch_token": watch_token, "profile": profile, "snapshot": value} if success else {"__watch_token": watch_token, "error": value}
                    events.put((success, payload))
                else:
                    events.put(("ocr", token, profile, None, success, value))
            try:
                result(True, reader.image_to_snapshot(image) if reader is not None else recognize_image(image))
            except CaptureError as error:
                result(False, str(error))
            except Exception:
                result(False, "本机截图识字失败，请放大消息文字或先粘贴聊天文本。")
        threading.Thread(target=recognize, daemon=True).start()

    def poll_screenshots(self):
        try:
            kind, token, profile, window, success, value = self.screenshot_events.get_nowait()
        except queue.Empty:
            return
        if token != self._discovery_token or profile != self.active_profile:
            operation = "bind_region" if kind == "bind" else "ocr"
            if self.active_operation == operation and self._busy_source_token == token:
                self.set_busy(False)
            return
        self.set_busy(False)
        if not success:
            self.stop_watch("截图识字未完成")
            self.status.set(value)
            self.capture_status.set(value)
            messagebox.showwarning("截图识字未完成", value, parent=self.root)
            return
        if kind == "bind":
            blocks = value.info.get("capture_blocks", [])
            def accept_region(region, size, image):
                if token != self._discovery_token or profile != self.active_profile:
                    raise CaptureError("会话已改变，请重新绑定。")
                if any(min(region[2], block[2]) > max(region[0], block[0]) and min(region[3], block[3]) > max(region[1], block[1]) for block in blocks):
                    raise CaptureError("选区包含灰色遮挡部分。请只框选清楚可见的消息，排除输入框、输入法浮条和其他浮层。")
                self.shot_reader = ScreenshotReader(ScreenshotBinding(window, profile, tuple(region), tuple(size)))
                self.shot_readers[profile] = self.shot_reader
                self.capture_status.set("消息区域已绑定。请把微信与助手并排摆放，保持选区不被遮挡；改变窗口大小后需重新框选。")
                self.status.set("消息区已绑定，接下来自动截图识字并分析。截图只在本机处理。")
                watch_seed = self._watch_start_requested
                if watch_seed:
                    self._begin_watch(window)
                self.analyze_screenshot_image(image, watch_seed=watch_seed, reader=self.shot_reader)
            self.show_region_crop(value, accept_region, lambda: self.stop_watch("尚未完成消息区域绑定"))
            return
        self.remove_image()
        self.context.delete("1.0", "end"); self.context.insert("1.0", value["text"])
        self.last_reader_text = value["text"]
        self._last_reader_profile = profile
        self.capture_status.set(f"本机识别出 {value['count']} 行文字；请核对错字、说话者和当前联系人。图片未交给 AI。")
        self.consent.set(True)
        self.request_generation()
