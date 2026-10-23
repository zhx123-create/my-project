"""Native desktop UI. Run with the Windows launcher or `python desktop.py`."""
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from PIL import Image, ImageGrab, ImageTk

from server import Config, InputError, ServiceError, TONES, generate, make_messages
from windows_capture import CaptureError, capture_window, crop_box, image_payload, list_windows

FONT_FAMILY = "Microsoft YaHei UI" if sys.platform == "win32" else "Noto Sans CJK SC"


class CropDialog(tk.Toplevel):
    def __init__(self, parent, image, accept):
        super().__init__(parent)
        self.title("选择允许分析的聊天区域")
        self.transient(parent)
        self.image, self.accept, self.start, self.end = image, accept, None, None
        self.result = None
        ttk.Label(self, text="拖动框选聊天区域，再确认。只发送选区，联系人列表请留在选区外。", padding=12).pack(anchor="w")
        preview = image.copy()
        preview.thumbnail((min(1050, self.winfo_screenwidth() - 100), min(650, self.winfo_screenheight() - 220)), Image.Resampling.LANCZOS)
        self.preview_size = preview.size
        self.photo = ImageTk.PhotoImage(preview, master=self)
        self.canvas = tk.Canvas(self, width=preview.width, height=preview.height, highlightthickness=0, cursor="crosshair")
        self.canvas.pack(padx=12)
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        self.canvas.bind("<ButtonPress-1>", self.begin)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.drag)
        self.info = tk.StringVar(value="尚未选择区域。取消后不会保留本次截图。")
        ttk.Label(self, textvariable=self.info, padding=12).pack(anchor="w")
        controls = ttk.Frame(self, padding=(12, 0, 12, 12)); controls.pack(fill="x")
        ttk.Button(controls, text="取消", command=self.destroy).pack(side="right")
        self.use = ttk.Button(controls, text="确认使用选区", command=self.confirm, state="disabled")
        self.use.pack(side="right", padx=8)
        self.grab_set()
        self.bind("<Escape>", lambda event: self.destroy())

    def begin(self, event):
        self.start = (event.x, event.y)
        self.end = self.start
        self.canvas.delete("selection")
        self.canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#2b9e68", width=3, tags="selection")
        self.use.configure(state="disabled")

    def drag(self, event):
        if self.start is None:
            return
        self.end = (max(0, min(self.preview_size[0], event.x)), max(0, min(self.preview_size[1], event.y)))
        self.canvas.coords("selection", *self.start, *self.end)
        try:
            self.result = crop_box(self.start, self.end, self.preview_size, self.image.size)
            self.use.configure(state="normal")
            self.info.set("仅将框选区域加入本次上下文；此时尚未发送给 AI。")
        except CaptureError as error:
            self.result = None
            self.use.configure(state="disabled")
            self.info.set(str(error))

    def confirm(self):
        if self.result is not None:
            try:
                self.accept(self.image.crop(self.result))
                self.destroy()
            except CaptureError as error:
                self.info.set(str(error))


class DesktopApp:
    def __init__(self, root):
        self.root = root
        self.root.title("接话 · Windows 微信回复助手")
        height = max(680, min(850, self.root.winfo_screenheight() - 90))
        width = max(870, min(1080, self.root.winfo_screenwidth() - 80))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(870, 680)
        self.root.configure(bg="#f3f5ef")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f5ef")
        style.configure("TLabel", background="#f3f5ef", foreground="#294637", font=(FONT_FAMILY, 10))
        style.configure("TButton", font=(FONT_FAMILY, 10), padding=(10, 7))
        style.configure("Heading.TLabel", font=(FONT_FAMILY, 20, "bold"))
        self.config = Config.from_env()
        self.events, self.busy, self.poll_id = queue.Queue(), False, None
        self.screenshot = None
        self.preview_photo = None
        self.windows = []
        self.status = tk.StringVar(value="先打开微信聊天，再选择窗口采集，或导入主动复制的文字。")
        self.window_choice = tk.StringVar()
        self.key = tk.StringVar(value=self.config.key)
        self.base = tk.StringVar(value=self.config.base)
        self.model = tk.StringVar(value=self.config.model)
        self.tone = tk.StringVar(value="自然")
        self.relationship = tk.StringVar()
        self.consent = tk.BooleanVar(value=False)
        self.topmost = tk.BooleanVar(value=False)
        self.controls = []
        self.settings_dialog = None
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Control-Return>", lambda event: self.request_generation())
        self.poll_id = self.root.after(80, self.poll)

    def button(self, parent, text, command, **kwargs):
        button = ttk.Button(parent, text=text, command=command)
        button.pack(**kwargs)
        self.controls.append((button, "normal"))
        return button

    def _build(self):
        frame = ttk.Frame(self.root, padding=18); frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1); frame.rowconfigure(1, weight=1)
        heading = ttk.Frame(frame); heading.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(heading, text="接话", style="Heading.TLabel").pack(side="left")
        ttk.Label(heading, text="  微信桌面回复助手 · 你确认，再发送").pack(side="left", pady=8)
        ttk.Checkbutton(heading, text="窗口置顶", variable=self.topmost, command=lambda: self.root.attributes("-topmost", self.topmost.get())).pack(side="right")
        self.button(heading, "AI 设置", self.show_settings, side="right", padx=12)
        split = ttk.Panedwindow(frame, orient="horizontal"); split.grid(row=1, column=0, sticky="nsew")
        left, right = ttk.Frame(split, padding=(0, 0, 12, 0)), ttk.Frame(split, padding=(12, 0, 0, 0))
        split.add(left, weight=1); split.add(right, weight=1)
        left.columnconfigure(0, weight=1); left.rowconfigure(3, weight=1, minsize=35)
        right.columnconfigure(0, weight=1)
        ttk.Label(left, text="01  选择允许分析的上下文").grid(row=0, column=0, sticky="w", pady=(0, 8))
        source = ttk.Frame(left); source.grid(row=1, column=0, sticky="ew")
        self.window_box = ttk.Combobox(source, textvariable=self.window_choice, state="readonly")
        self.window_box.pack(side="left", fill="x", expand=True); self.controls.append((self.window_box, "readonly"))
        self.button(source, "查找微信", self.refresh_windows, side="right", padx=(6, 0))
        tools = ttk.Frame(left); tools.grid(row=2, column=0, sticky="ew", pady=6)
        self.button(tools, "采集选中窗口", self.capture, side="left")
        self.button(tools, "导入复制文字", self.import_text, side="left", padx=5)
        self.button(tools, "导入剪贴板截图", self.import_image, side="left")
        self.context = ScrolledText(left, wrap="word", height=9, font=(FONT_FAMILY, 10), relief="flat", padx=10, pady=10, undo=False)
        self.context.grid(row=3, column=0, sticky="nsew"); self.controls.append((self.context, "normal"))
        ttk.Label(left, text="文字中请标注“我 / 对方”；仅分析本次导入的内容。", foreground="#6b786d").grid(row=4, column=0, sticky="w", pady=5)
        preview_row = ttk.Frame(left); preview_row.grid(row=5, column=0, sticky="ew")
        self.preview = ttk.Label(preview_row, text="未附加截图（最多 1 张）")
        self.preview.pack(side="left")
        self.button(preview_row, "移除截图", self.remove_image, side="right", pady=(4, 8))
        ttk.Label(left, text="02  想表达什么？").grid(row=6, column=0, sticky="w", pady=(6, 5))
        self.intent = ScrolledText(left, height=2, wrap="word", font=(FONT_FAMILY, 10), relief="flat", padx=8, pady=8)
        self.intent.grid(row=7, column=0, sticky="ew"); self.controls.append((self.intent, "normal"))
        self.intent.insert("1.0", "自然接话，简洁一点")
        options = ttk.Frame(left); options.grid(row=8, column=0, sticky="ew", pady=8)
        ttk.Label(options, text="语气").pack(side="left")
        tone = ttk.Combobox(options, textvariable=self.tone, values=list(TONES), width=6, state="readonly")
        tone.pack(side="left", padx=(6, 10)); self.controls.append((tone, "readonly"))
        ttk.Label(options, text="对方是").pack(side="left")
        relation = ttk.Entry(options, textvariable=self.relationship, width=12)
        relation.pack(side="left", padx=6, fill="x", expand=True); self.controls.append((relation, "normal"))
        self.confirm_box = ttk.Checkbutton(left, text="确认将本次文字和截图提交给 AI", variable=self.consent)
        self.confirm_box.grid(row=9, column=0, sticky="w", pady=8); self.controls.append((self.confirm_box, "normal"))
        actions = ttk.Frame(left); actions.grid(row=10, column=0, sticky="ew")
        self.generate_button = self.button(actions, "生成 3 条回复  Ctrl+Enter", self.request_generation, side="left")
        self.button(actions, "清空本次内容", self.clear, side="right")
        ttk.Label(right, text="03  编辑、复制，再回微信发送").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.reply_texts, self.reply_labels = [], []
        for i in range(3):
            card = ttk.LabelFrame(right, text=f"候选 {i + 1}", padding=8)
            right.rowconfigure(i + 1, weight=1, uniform="replies")
            card.grid(row=i + 1, column=0, sticky="nsew", pady=(0, 8))
            label = ttk.Label(card, text="尚未生成"); label.pack(anchor="w")
            self.button(card, "复制这条回复", lambda n=i: self.copy_reply(n), side="bottom", anchor="e")
            text = ScrolledText(card, height=4, wrap="word", relief="flat", font=(FONT_FAMILY, 10), padx=6, pady=6)
            text.pack(fill="both", expand=True, pady=4)
            self.reply_labels.append(label); self.reply_texts.append(text)
        ttk.Label(frame, textvariable=self.status, wraplength=800, foreground="#365f45").grid(row=2, column=0, sticky="ew", pady=(12, 0))
        ttk.Label(frame, text="应用不保存聊天和密钥；AI 服务的数据政策另行适用。采集后先裁剪预览，不自动发送微信消息。", wraplength=800, foreground="#6b786d").grid(row=3, column=0, sticky="w", pady=(5, 0))

    def show_settings(self):
        if self.busy:
            return
        if self.settings_dialog is not None and self.settings_dialog.winfo_exists():
            self.settings_dialog.lift()
            return
        dialog = tk.Toplevel(self.root); self.settings_dialog = dialog
        dialog.title("AI 设置 · 仅本次运行保留")
        dialog.transient(self.root); dialog.columnconfigure(1, weight=1)
        dialog.geometry("650x240")
        for row, (label, variable) in enumerate((("API 密钥", self.key), ("接口地址", self.base), ("模型", self.model))):
            ttk.Label(dialog, text=label).grid(row=row, column=0, sticky="w", padx=12, pady=8)
            entry = ttk.Entry(dialog, textvariable=variable, show="•" if row == 0 else "")
            entry.grid(row=row, column=1, sticky="ew", padx=(0, 12), pady=8)
        ttk.Label(dialog, text="默认 OpenAI API，需单独开通 API 额度。密钥不会写入磁盘。", wraplength=600).grid(row=3, column=0, columnspan=2, sticky="w", padx=12, pady=8)
        def finish():
            self.status.set("AI 设置已保留在本次运行内，尚未验证连接。" if self.key.get().strip() else "尚未填写 API 密钥。可先导入并整理聊天内容。")
            dialog.destroy()
        ttk.Button(dialog, text="完成", command=finish).grid(row=4, column=1, sticky="e", padx=12, pady=8)
        dialog.protocol("WM_DELETE_WINDOW", finish)
        dialog.grab_set()

    def refresh_windows(self):
        if self.busy:
            return
        try:
            self.windows = list_windows()
            self.window_box.configure(values=[f"{i + 1}. {w.title}" for i, w in enumerate(self.windows)])
            self.window_choice.set("")
            if self.windows:
                self.window_box.current(0)
                self.status.set("请选择需要分析的微信窗口，打开对应聊天，再点击采集。")
            else:
                self.status.set("未找到微信窗口。请启动微信并登录；也可先复制聊天文字导入。")
        except CaptureError as error:
            self.status.set(str(error))

    def capture(self):
        if self.busy:
            return
        index = self.window_box.current()
        if not 0 <= index < len(self.windows):
            self.status.set("请先点击“查找微信”，明确选择允许采集的窗口。")
            return
        try:
            image = capture_window(self.windows[index])
            CropDialog(self.root, image, self.attach_image)
        except CaptureError as error:
            self.status.set(str(error))

    def import_text(self):
        if self.busy:
            return
        try:
            text = self.root.clipboard_get()
            if not text.strip():
                self.status.set("剪贴板没有文字，请先在微信复制选定的消息。")
                return
            if len(text) > 20000:
                self.status.set("复制内容超过 20000 字，请仅选择最近的聊天。")
                return
            self.context.delete("1.0", "end"); self.context.insert("1.0", text)
            self.consent.set(False)
            self.status.set("已导入复制的文字，请检查说话者和内容，再勾选确认生成。")
        except tk.TclError:
            self.status.set("无法读取文字，请在微信复制消息后重试，或直接在左侧粘贴。")

    def import_image(self):
        if self.busy:
            return
        try:
            image = ImageGrab.grabclipboard()
            if not isinstance(image, Image.Image):
                self.status.set("剪贴板没有截图。请用微信截图或 Win+Shift+S 截取需要的聊天区域。")
                return
            CropDialog(self.root, image.convert("RGB"), self.attach_image)
        except Exception:
            self.status.set("无法读取剪贴板截图，请尝试复制聊天文字。")

    def attach_image(self, image):
        self.screenshot = image_payload(image)
        preview = image.copy(); preview.thumbnail((250, 80))
        self.preview_photo = ImageTk.PhotoImage(preview, master=self.root)
        self.preview.configure(image=self.preview_photo, text="")
        self.consent.set(False)
        self.status.set("已附加你确认的聊天选区；只有点击生成后才会提交给 AI。")

    def remove_image(self):
        if not self.busy:
            self.screenshot = self.preview_photo = None
            self.preview.configure(image="", text="未附加截图（最多 1 张）")
            self.consent.set(False)

    def set_busy(self, busy):
        self.busy = busy
        for widget, normal in self.controls:
            widget.configure(state="disabled" if busy else normal)
        self.generate_button.configure(text="正在生成，请稍候…" if busy else "生成 3 条回复  Ctrl+Enter")

    def request_generation(self):
        if self.busy:
            return
        if not self.consent.get():
            self.status.set("请先检查聊天内容，并勾选确认发送给 AI 服务。")
            return
        if not self.key.get().strip():
            self.status.set("请点击右上角“AI 设置”填写 API 密钥；只在本次运行保留，不要发在聊天里。")
            return
        try:
            messages = make_messages({"context": self.context.get("1.0", "end-1c"), "intent": self.intent.get("1.0", "end-1c"), "relationship": self.relationship.get(), "tone": self.tone.get(), "images": [self.screenshot] if self.screenshot else []})
        except InputError as error:
            self.status.set(str(error))
            return
        config = Config(self.key.get().strip(), self.base.get().strip().rstrip("/"), self.model.get().strip())
        if not config.model:
            self.status.set("请填写模型名称。")
            return
        self.set_busy(True)
        self.status.set("正在分析本次上下文并生成三条回复；通常需要数秒。")
        events = self.events
        def work():
            try:
                events.put((True, generate(config, messages)))
            except ServiceError as error:
                events.put((False, str(error)))
            except Exception:
                events.put((False, "生成遇到错误，请检查 AI 设置后重试。"))
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            success, value = self.events.get_nowait()
            self.set_busy(False)
            if success:
                for i, reply in enumerate(value):
                    self.reply_labels[i].configure(text=reply["label"])
                    self.reply_texts[i].delete("1.0", "end")
                    self.reply_texts[i].insert("1.0", reply["text"])
                self.status.set("已生成三条草稿。你可以编辑、复制，再切回微信粘贴发送。")
            else:
                self.status.set(value)
        except queue.Empty:
            pass
        self.poll_id = self.root.after(80, self.poll)

    def copy_reply(self, index):
        text = self.reply_texts[index].get("1.0", "end-1c").strip()
        if not text:
            self.status.set("这条候选回复还没有内容。")
            return
        try:
            self.root.clipboard_clear(); self.root.clipboard_append(text)
            self.root.update_idletasks()
            self.status.set("已复制。请回微信粘贴，并自行确认发送。")
        except tk.TclError:
            self.status.set("复制失败，请在回复框选中文字后按 Ctrl+C。")

    def clear(self):
        if self.busy:
            return
        self.context.delete("1.0", "end"); self.intent.delete("1.0", "end")
        self.relationship.set(""); self.remove_image()
        for text, label in zip(self.reply_texts, self.reply_labels):
            text.delete("1.0", "end"); label.configure(text="尚未生成")
        self.status.set("已清空本次上下文与回复。API 设置仅在本次运行保留。")

    def close(self):
        if self.poll_id is not None:
            self.root.after_cancel(self.poll_id)
        self.key.set("")
        self.screenshot = self.preview_photo = None
        self.root.destroy()


def main():
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    DesktopApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
