"""Native desktop UI. Run with the Windows launcher or `python desktop.py`."""
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

from PIL import Image, ImageGrab, ImageTk

from server import Config, InputError, ServiceError, TONES, generate, make_messages
from conversation_memory import ConversationMemory
from windows_capture import CaptureError, capture_window_isolated, crop_box, image_payload, list_windows
from wechat_reader import read_conversation

FONT_FAMILY = "Microsoft YaHei UI" if sys.platform == "win32" else "Noto Sans CJK SC"
TEMP_CONVERSATION = "临时会话（不记忆）"


def default_memory_path():
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "JieHua" / "conversations.json"


def launch_snipping_tool():
    if sys.platform != "win32":
        raise CaptureError("系统截图按钮仅支持 Windows；也可复制聊天文字后导入。")
    try:
        os.startfile("ms-screenclip:")
    except OSError:
        raise CaptureError("无法打开系统截图工具。请手动按 Win+Shift+S 截取聊天，再点击“导入剪贴板截图”。") from None


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
        self.attributes("-topmost", bool(parent.attributes("-topmost")))
        self.update_idletasks()
        x = max(0, (self.winfo_screenwidth() - self.winfo_reqwidth()) // 2)
        y = max(0, (self.winfo_screenheight() - self.winfo_reqheight()) // 2)
        self.geometry(f"+{x}+{y}")
        self.lift()
        self.focus_set()
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
    def __init__(self, root, memory=None):
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
        self.capture_events = queue.Queue()
        self.reader_events = queue.Queue()
        self.active_operation = None
        self.job_started = None
        self.last_reader_text = None
        self.active_profile = None
        self.memory_warning = None
        if memory is not None:
            self.memory = memory
        else:
            try:
                self.memory = ConversationMemory(default_memory_path())
            except (ValueError, OSError):
                self.memory = ConversationMemory()
                self.memory_warning = "本地会话记忆未能读取。已使用临时记忆，原文件未改动。"
        self.screenshot = None
        self.preview_photo = None
        self.windows = []
        self.status = tk.StringVar(value="新建一个会话，填写背景和回复目的；查找微信后，可尝试一键读取文字并分析。")
        self.window_choice = tk.StringVar()
        self.capture_status = tk.StringVar(value="优先读取消息文字。请确认微信中打开的联系人，与下方记忆会话一致。")
        self.conversation_name = tk.StringVar(value=TEMP_CONVERSATION)
        self.reply_mode = tk.StringVar(value="先给 1 条（较快）")
        self.memory_note = tk.StringVar()
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
        self.refresh_memory_options()
        if self.memory_warning:
            self.status.set(self.memory_warning)
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
        split = tk.PanedWindow(frame, orient="horizontal", sashwidth=8, background="#f3f5ef", borderwidth=0)
        split.grid(row=1, column=0, sticky="nsew")
        self.root.after_idle(lambda: split.sash_place(0, split.winfo_width() // 2, 0))
        left_panel, right = ttk.Frame(split, padding=(0, 0, 12, 0)), ttk.Frame(split, padding=(12, 0, 0, 0))
        split.add(left_panel, minsize=390, stretch="always"); split.add(right, minsize=390, stretch="always")
        self.input_canvas = tk.Canvas(left_panel, highlightthickness=0, background="#f3f5ef")
        scroll = ttk.Scrollbar(left_panel, orient="vertical", command=self.input_canvas.yview)
        scroll.pack(side="right", fill="y")
        self.input_canvas.pack(side="left", fill="both", expand=True)
        self.input_canvas.configure(yscrollcommand=scroll.set)
        left = ttk.Frame(self.input_canvas)
        input_window = self.input_canvas.create_window(0, 0, window=left, anchor="nw")
        def resize_inputs(event=None):
            self.input_canvas.itemconfigure(input_window, width=self.input_canvas.winfo_width(), height=max(left.winfo_reqheight(), self.input_canvas.winfo_height()))
            self.input_canvas.configure(scrollregion=self.input_canvas.bbox("all"))
        left.bind("<Configure>", resize_inputs)
        self.input_canvas.bind("<Configure>", resize_inputs)
        def scroll_inputs(event):
            widget = event.widget
            if isinstance(widget, (tk.Text, ttk.Combobox)):
                return
            while widget is not None:
                if widget is left or widget is self.input_canvas:
                    self.input_canvas.yview_scroll(-int(event.delta / 120), "units")
                    return "break"
                widget = getattr(widget, "master", None)
        self.root.bind("<MouseWheel>", scroll_inputs, add="+")
        left.columnconfigure(0, weight=1); left.rowconfigure(3, weight=1, minsize=35)
        right.columnconfigure(0, weight=1)
        source_heading = ttk.Frame(left); source_heading.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(source_heading, text="01  选择允许分析的上下文").pack(anchor="w")
        ttk.Label(source_heading, textvariable=self.capture_status, wraplength=375, foreground="#365f45").pack(anchor="w", pady=(4, 0))
        memory_bar = ttk.Frame(source_heading); memory_bar.pack(fill="x", pady=(7, 0))
        ttk.Label(memory_bar, text="记忆会话").pack(side="left", padx=(0, 5))
        self.profile_box = ttk.Combobox(memory_bar, textvariable=self.conversation_name, width=13, state="readonly")
        self.profile_box.pack(side="left", fill="x", expand=True)
        self.profile_box.bind("<<ComboboxSelected>>", lambda event: self.select_profile(self.conversation_name.get()))
        self.controls.append((self.profile_box, "readonly"))
        self.button(memory_bar, "新建", self.new_profile, side="left", padx=4).configure(width=4)
        self.button(memory_bar, "背景/记忆", self.show_memory, side="left").configure(width=9)
        source = ttk.Frame(left); source.grid(row=1, column=0, sticky="ew")
        self.window_box = ttk.Combobox(source, textvariable=self.window_choice, state="readonly")
        self.window_box.pack(side="left", fill="x", expand=True); self.controls.append((self.window_box, "readonly"))
        self.window_box.bind("<<ComboboxSelected>>", lambda event: self.capture_status.set("已选择微信窗口。打开需要的会话，再点“采集选中窗口”。"))
        self.button(source, "查找微信", self.refresh_windows, side="right", padx=(6, 0))
        tools = ttk.Frame(left); tools.grid(row=2, column=0, sticky="ew", pady=6)
        tools.columnconfigure(0, weight=1); tools.columnconfigure(1, weight=1)
        self.analyze_button = ttk.Button(tools, text="读取当前会话文字并分析", command=self.analyze_current_chat)
        self.analyze_button.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.controls.append((self.analyze_button, "normal"))
        self.capture_button = ttk.Button(tools, text="采集选中窗口", command=self.capture)
        self.capture_button.grid(row=1, column=0, sticky="ew", padx=(0, 4), pady=(0, 5))
        self.screen_button = ttk.Button(tools, text="系统截图（备用）", command=self.system_screenshot)
        self.screen_button.grid(row=1, column=1, sticky="ew", padx=(4, 0), pady=(0, 5))
        for column, (text, command) in enumerate((("导入复制文字", self.import_text), ("导入剪贴板截图", self.import_image))):
            button = ttk.Button(tools, text=text, command=command)
            button.grid(row=2, column=column, sticky="ew", padx=(0, 4) if column == 0 else (4, 0))
            self.controls.append((button, "normal"))
        self.controls.extend(((self.capture_button, "normal"), (self.screen_button, "normal")))
        self.context = ScrolledText(left, wrap="word", height=5, font=(FONT_FAMILY, 10), relief="flat", padx=10, pady=10, undo=False)
        self.context.grid(row=3, column=0, sticky="nsew"); self.controls.append((self.context, "normal"))
        ttk.Label(left, text="本次消息；生成时会合并所选会话的已确认记忆。", foreground="#6b786d").grid(row=4, column=0, sticky="w", pady=5)
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
        mode = ttk.Combobox(left, textvariable=self.reply_mode, values=["先给 1 条（较快）", "生成 3 条候选"], state="readonly")
        mode.grid(row=9, column=0, sticky="ew", pady=(0, 5)); self.controls.append((mode, "readonly"))
        workflow_actions = ttk.Frame(frame)
        workflow_actions.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self.confirm_box = ttk.Checkbutton(workflow_actions, text="确认会话匹配并允许提交上下文给 AI", variable=self.consent)
        self.confirm_box.pack(side="left"); self.controls.append((self.confirm_box, "normal"))
        actions = ttk.Frame(workflow_actions); actions.pack(side="right")
        self.generate_button = self.button(actions, "分析并生成  Ctrl+Enter", self.request_generation, side="left")
        self.button(actions, "清空本次内容", self.clear, side="right")
        ttk.Label(right, text="03  AI 对上下文的理解（请核对）").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.understanding = ScrolledText(right, height=3, wrap="word", font=(FONT_FAMILY, 10), relief="flat", padx=8, pady=8)
        self.understanding.grid(row=1, column=0, sticky="ew", pady=(0, 10)); self.understanding.configure(state="disabled")
        self.reply_texts, self.reply_labels, self.reply_cards = [], [], []
        self.results_panel = right
        for i in range(3):
            card = ttk.LabelFrame(right, text=f"候选 {i + 1}", padding=8)
            right.rowconfigure(i + 2, weight=1 if i == 0 else 0, uniform="replies" if i == 0 else "")
            card.grid(row=i + 2, column=0, sticky="nsew", pady=(0, 8))
            self.reply_cards.append(card)
            if i:
                card.grid_remove()
            label = ttk.Label(card, text="尚未生成"); label.pack(anchor="w")
            reply_actions = ttk.Frame(card); reply_actions.pack(side="bottom", fill="x")
            self.button(reply_actions, "复制回复", lambda n=i: self.copy_reply(n), side="right")
            self.button(reply_actions, "记为已发送", lambda n=i: self.confirm_sent(n), side="left")
            text = ScrolledText(card, height=4, wrap="word", relief="flat", font=(FONT_FAMILY, 10), padx=6, pady=6)
            text.pack(fill="both", expand=True, pady=4)
            self.reply_labels.append(label); self.reply_texts.append(text)
        ttk.Label(frame, textvariable=self.status, wraplength=800, foreground="#365f45").grid(row=3, column=0, sticky="ew", pady=(12, 0))
        ttk.Label(frame, textvariable=self.memory_note, wraplength=800, foreground="#6b786d").grid(row=4, column=0, sticky="w", pady=(5, 0))

    def refresh_memory_options(self):
        self.profile_box.configure(values=[TEMP_CONVERSATION, *self.memory.names()])
        self.memory_note.set("已启用本地明文会话记忆，可在“背景/记忆”关闭并删除保存文件。密钥从不保存。" if self.memory.persistent else "会话记忆仅保留本次运行；可在“背景/记忆”开启本地保存。密钥从不保存。")

    def save_profile_fields(self):
        if self.active_profile:
            self.memory.update_profile(self.active_profile, relationship=self.relationship.get(), goal=self.intent.get("1.0", "end-1c"))

    def select_profile(self, name):
        if self.busy:
            return
        try:
            self.save_profile_fields()
            profile = self.memory.profile(name) if name != TEMP_CONVERSATION else None
        except (ValueError, OSError) as error:
            self.conversation_name.set(self.active_profile or TEMP_CONVERSATION)
            messagebox.showwarning("会话未切换", str(error), parent=self.root)
            return
        self.active_profile = profile["name"] if profile else None
        self.conversation_name.set(self.active_profile or TEMP_CONVERSATION)
        self.context.delete("1.0", "end")
        self.last_reader_text = None
        self.remove_image()
        self.intent.delete("1.0", "end")
        self.intent.insert("1.0", profile["goal"] if profile else "自然接话，简洁一点")
        self.relationship.set(profile["relationship"] if profile else "")
        self.clear_results()
        self.refresh_memory_options()
        self.status.set("已切换会话。请核对微信当前打开的联系人与此会话一致；程序不会自动核实联系人身份。")

    def new_profile(self):
        if self.busy:
            return
        name = simpledialog.askstring("新建会话记忆", "给这段会话起一个名称（例如：小林、项目群）。不同联系人请使用不同名称：", parent=self.root)
        if name is None:
            return
        name = name.strip()
        if name == TEMP_CONVERSATION:
            messagebox.showwarning("名称不可用", "请使用其他会话名称。", parent=self.root)
            return
        self.select_profile(name)
        if self.active_profile == name:
            self.show_memory()

    def show_memory(self):
        if self.busy:
            return
        if not self.active_profile:
            messagebox.showinfo("先选择会话", "点击“新建”为当前聊天命名，再填写关系、背景和沟通目标。", parent=self.root)
            return
        name = self.active_profile
        profile = self.memory.profile(name)
        dialog = tk.Toplevel(self.root)
        dialog.title(f"会话背景与记忆 · {name}")
        dialog.geometry("720x650"); dialog.transient(self.root)
        panel = ttk.Frame(dialog, padding=15); panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="关系和背景由你确认，助手不会把猜测自动记成事实。", wraplength=650).pack(anchor="w")
        ttk.Label(panel, text="与对方的关系").pack(anchor="w", pady=(10, 4))
        relation = ttk.Entry(panel); relation.insert(0, self.relationship.get()); relation.pack(fill="x")
        ttk.Label(panel, text="已确认的背景 / 重要事实（最多 3000 字）").pack(anchor="w", pady=(10, 4))
        background = ScrolledText(panel, height=4, wrap="word", font=(FONT_FAMILY, 10))
        background.insert("1.0", profile["background"]); background.pack(fill="x")
        ttk.Label(panel, text="你的沟通目标（最多 1000 字）").pack(anchor="w", pady=(10, 4))
        goal = ScrolledText(panel, height=2, wrap="word", font=(FONT_FAMILY, 10))
        goal.insert("1.0", self.intent.get("1.0", "end-1c")); goal.pack(fill="x")
        ttk.Label(panel, text="已记录的近期上下文（最多约 12000 字，不含未发送草稿）").pack(anchor="w", pady=(10, 4))
        history = ScrolledText(panel, height=6, wrap="word", font=(FONT_FAMILY, 10))
        history.insert("1.0", self.memory.history_text(name)); history.configure(state="disabled"); history.pack(fill="both", expand=True)
        persist = tk.BooleanVar(dialog, value=self.memory.persistent)
        ttk.Checkbutton(panel, text="允许把所有会话的文字记忆保存到本机（明文）；取消会删除已保存的记忆文件", variable=persist).pack(anchor="w", pady=8)
        ttk.Label(panel, text=f"保存位置：{self.memory.path or '当前为临时记忆，未指定保存位置'}", wraplength=650).pack(anchor="w")
        actions = ttk.Frame(panel); actions.pack(fill="x", pady=(10, 0))
        def save():
            try:
                self.memory.update_profile(name, background=background.get("1.0", "end-1c"), relationship=relation.get(), goal=goal.get("1.0", "end-1c"))
                self.memory.set_persistent(persist.get())
            except (ValueError, OSError) as error:
                messagebox.showwarning("记忆未保存", str(error), parent=dialog)
                return
            self.relationship.set(relation.get())
            self.intent.delete("1.0", "end"); self.intent.insert("1.0", goal.get("1.0", "end-1c"))
            self.consent.set(False); self.refresh_memory_options(); dialog.destroy()
        def erase_history():
            if not messagebox.askyesno("清空此会话历史", "将删除此会话已记录的聊天文字，保留关系和背景资料。是否继续？", parent=dialog):
                return
            try:
                self.memory.clear_history(name)
            except (ValueError, OSError) as error:
                messagebox.showwarning("未能清空", str(error), parent=dialog); return
            history.configure(state="normal"); history.delete("1.0", "end"); history.configure(state="disabled")
            self.context.delete("1.0", "end"); self.consent.set(False)
        def delete_profile():
            if not messagebox.askyesno("删除会话", "将删除此会话的背景资料和全部历史。是否继续？", parent=dialog):
                return
            try:
                self.memory.delete(name)
            except (ValueError, OSError) as error:
                messagebox.showwarning("未能删除", str(error), parent=dialog); return
            self.active_profile = None
            dialog.destroy(); self.select_profile(TEMP_CONVERSATION)
        ttk.Button(actions, text="保存资料", command=save).pack(side="right")
        ttk.Button(actions, text="清空历史", command=erase_history).pack(side="left")
        ttk.Button(actions, text="删除会话", command=delete_profile).pack(side="left", padx=8)
        dialog.grab_set(); dialog.lift()

    def clear_results(self):
        for text, label in zip(self.reply_texts, self.reply_labels):
            text.delete("1.0", "end"); label.configure(text="尚未生成")
        self.understanding.configure(state="normal"); self.understanding.delete("1.0", "end"); self.understanding.configure(state="disabled")

    def display_reply_count(self, count):
        for i, card in enumerate(self.reply_cards):
            if i < count:
                card.grid()
            else:
                card.grid_remove()
            self.results_panel.rowconfigure(i + 2, weight=1 if i < count else 0, uniform="replies" if i < count else "")

    def confirm_sent(self, index):
        if self.busy:
            return
        if not self.active_profile:
            messagebox.showinfo("先选择会话", "只有已命名的会话可以记录已发送内容。请先新建或选择记忆会话。", parent=self.root)
            return
        text = self.reply_texts[index].get("1.0", "end-1c").strip()
        if not text:
            return
        if not messagebox.askyesno("确认已经发送", "仅在你确实已把这条内容发送到微信后记录。是否将它记入当前会话？这不会代你发送消息。", parent=self.root):
            return
        try:
            self.memory.confirm_sent(self.active_profile, text)
            self.status.set("已按你的确认记入当前会话。复制草稿本身不会记录为已发送。")
        except (ValueError, OSError) as error:
            messagebox.showwarning("未能记录", str(error), parent=self.root)

    def analyze_current_chat(self):
        if self.busy:
            return
        if not self.active_profile:
            messagebox.showinfo("先建立会话记忆", "点击“新建”，为当前联系人建立独立会话并填写背景。请核对微信当前打开的会话，再勾选允许提交上下文。", parent=self.root)
            return
        if not self.consent.get():
            self.status.set("请确认微信当前联系人与所选记忆会话匹配，并勾选允许读取、提交上下文。")
            return
        if not self.key.get().strip():
            self.status.set("请先在“AI 设置”填写密钥，才能读取后直接生成。")
            return
        index = self.window_box.current()
        if not 0 <= index < len(self.windows):
            self.show_capture_error("请先“查找微信”并选中你允许读取的窗口，然后打开对应聊天。")
            return
        window, events = self.windows[index], self.reader_events
        self.set_busy(True, operation="read")
        self.status.set("正在读取所选微信窗口的可访问消息文字，最多等待 8 秒；不截取图片。")
        def work():
            try:
                events.put((True, read_conversation(window)))
            except CaptureError as error:
                events.put((False, str(error)))
            except Exception:
                events.put((False, "当前微信消息文字读取失败，未提交任何内容给 AI。请确认该版本是否提供可访问消息文字。"))
        threading.Thread(target=work, daemon=True).start()

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
                self.status.set("已找到微信窗口。打开需要的聊天，再点击“采集选中窗口”。")
                self.capture_status.set(f"找到 {len(self.windows)} 个微信窗口；请选中要分析的窗口，再点击采集。")
            else:
                self.show_capture_error("未找到微信窗口。请打开并登录电脑版微信，再点击“查找微信”。也可直接使用“系统截图（备用）”，无需选择窗口。")
        except CaptureError as error:
            self.show_capture_error(str(error))
        except Exception:
            self.show_capture_error("查找微信窗口失败。可先使用“系统截图（备用）”截取聊天，或复制聊天文字后导入。")

    def show_capture_error(self, message):
        self.status.set(message)
        self.capture_status.set("采集未完成，请查看弹窗说明。可点击“系统截图（备用）”继续。")
        messagebox.showwarning("未完成聊天采集", message, parent=self.root)

    def system_screenshot(self):
        if self.busy:
            return
        self.status.set("请框选微信聊天；截图后从任务栏回到助手，点击“导入剪贴板截图”。")
        self.capture_status.set("系统截图完成后，回到助手点“导入剪贴板截图”，再在预览中确认选区。")
        self.root.iconify()
        try:
            launch_snipping_tool()
        except CaptureError as error:
            self.root.deiconify()
            self.show_capture_error(str(error))

    def show_crop(self, image):
        before = set(self.root.winfo_children())
        try:
            CropDialog(self.root, image, self.attach_image)
        except Exception:
            for widget in set(self.root.winfo_children()) - before:
                if isinstance(widget, tk.Toplevel):
                    widget.destroy()
            self.show_capture_error("已取得截图，但预览窗口未能打开。请重启助手后重试，或先使用“导入复制文字”。")

    def capture(self):
        if self.busy:
            return
        index = self.window_box.current()
        if not 0 <= index < len(self.windows):
            self.show_capture_error("请先点击“查找微信”，在下拉框里选中微信窗口，再点击“采集选中窗口”。也可直接使用“系统截图（备用）”。")
            return
        window, events = self.windows[index], self.capture_events
        self.set_busy(True, operation="capture")
        self.status.set("正在采集选定窗口，最多等待 8 秒；成功后会打开聊天区域预览。")
        self.capture_status.set("正在采集…最多等待 8 秒。若微信画面无法捕获，会提示改用系统截图。")
        def work():
            try:
                events.put((True, capture_window_isolated(window)))
            except CaptureError as error:
                events.put((False, str(error)))
            except Exception:
                events.put((False, "微信窗口采集出现错误。请按 Win+Shift+S 截取聊天，再点击“导入剪贴板截图”。"))
        threading.Thread(target=work, daemon=True).start()

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
            self.last_reader_text = None
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
            self.show_crop(image.convert("RGB"))
        except Exception:
            self.status.set("无法读取剪贴板截图，请尝试复制聊天文字。")

    def attach_image(self, image):
        self.screenshot = image_payload(image)
        preview = image.copy(); preview.thumbnail((250, 80))
        self.preview_photo = ImageTk.PhotoImage(preview, master=self.root)
        self.preview.configure(image=self.preview_photo, text="")
        self.consent.set(False)
        self.status.set("已附加你确认的聊天选区；只有点击生成后才会提交给 AI。")
        self.capture_status.set("聊天选区已附加；不需要在左侧看到识别文字。填写回复意图，再确认生成即可。")

    def remove_image(self):
        if not self.busy:
            self.screenshot = self.preview_photo = None
            self.preview.configure(image="", text="未附加截图（最多 1 张）")
            self.consent.set(False)

    def set_busy(self, busy, operation="generate"):
        self.busy = busy
        self.active_operation = operation if busy else None
        for widget, normal in self.controls:
            widget.configure(state="disabled" if busy else normal)
        self.generate_button.configure(text="正在生成，请稍候…" if busy and operation == "generate" else "分析并生成  Ctrl+Enter")
        self.capture_button.configure(text="正在采集，请稍候…" if busy and operation == "capture" else "采集选中窗口")
        self.analyze_button.configure(text="正在读取会话文字…" if busy and operation == "read" else "读取当前会话文字并分析")

    def request_generation(self):
        if self.busy:
            return
        if not self.consent.get():
            self.status.set("请先检查聊天内容，并勾选确认发送给 AI 服务。")
            return
        if not self.key.get().strip():
            self.status.set("请点击右上角“AI 设置”填写 API 密钥；只在本次运行保留，不要发在聊天里。")
            return
        reply_count = 1 if self.reply_mode.get().startswith("先给") else 3
        try:
            context = self.context.get("1.0", "end-1c")
            # The reader's fixed provenance note is shown in the editor, but
            # not part of the observed message sequence used for overlap merge.
            if context == self.last_reader_text and context.startswith("【微信可访问消息文字；") and "\n" in context:
                context = context.split("\n", 1)[1]
            history, background = "", ""
            if self.active_profile:
                self.save_profile_fields()
                if context.strip():
                    self.memory.add_context(self.active_profile, context)
                profile = self.memory.profile(self.active_profile)
                history, background = self.memory.history_text(self.active_profile), profile["background"]
                context = ""
            messages = make_messages({"context": context, "history": history, "background": background, "conversation_name": self.active_profile or "", "intent": self.intent.get("1.0", "end-1c"), "relationship": self.relationship.get(), "tone": self.tone.get(), "images": [self.screenshot] if self.screenshot else [], "reply_count": reply_count, "include_analysis": True})
        except (InputError, ValueError, OSError) as error:
            self.status.set(str(error))
            return
        config = Config(self.key.get().strip(), self.base.get().strip().rstrip("/"), self.model.get().strip())
        if not config.model:
            self.status.set("请填写模型名称。")
            return
        self.clear_results()
        self.set_busy(True)
        self.job_started = time.monotonic()
        self.status.set(f"正在结合本次消息、背景和已记录历史分析，生成 {reply_count} 条草稿…")
        events = self.events
        def work():
            try:
                events.put((True, generate(config, messages, reply_count=reply_count, include_analysis=True)))
            except ServiceError as error:
                events.put((False, str(error)))
            except Exception:
                events.put((False, "生成遇到错误，请检查 AI 设置后重试。"))
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            success, value = self.reader_events.get_nowait()
            self.set_busy(False)
            if success:
                self.remove_image()
                self.context.delete("1.0", "end"); self.context.insert("1.0", value["text"])
                self.last_reader_text = value["text"]
                self.capture_status.set(f"已读取 {value['count']} 条可见消息文字；联系人和发言者身份仍需你核对。")
                # The user already opted into read-and-analyze for this named
                # conversation before the operation; no new target is selected.
                self.consent.set(True)
                self.request_generation()
            else:
                self.status.set(value)
                self.capture_status.set("当前微信未能提供可读消息文字。本次没有提交给 AI；可先导入文字验证会话记忆。")
                messagebox.showwarning("未能直接读取会话文字", value, parent=self.root)
        except queue.Empty:
            pass
        try:
            success, value = self.capture_events.get_nowait()
            self.set_busy(False)
            if success:
                self.status.set("已取得窗口画面。请在预览窗口拖动框选聊天内容，再点“确认使用选区”。")
                self.capture_status.set("已打开预览。拖动框选消息区域，再点“确认使用选区”。")
                self.show_crop(value)
            else:
                self.show_capture_error(value)
        except queue.Empty:
            pass
        try:
            success, value = self.events.get_nowait()
            self.set_busy(False)
            if success:
                replies = value["replies"] if isinstance(value, dict) else value
                understanding = value.get("understanding", "本次模型未返回会话理解摘要。") if isinstance(value, dict) else "本次模型未返回会话理解摘要。"
                self.understanding.configure(state="normal"); self.understanding.delete("1.0", "end")
                self.understanding.insert("1.0", understanding); self.understanding.configure(state="disabled")
                self.display_reply_count(len(replies))
                for i, reply in enumerate(replies):
                    self.reply_labels[i].configure(text=reply["label"])
                    self.reply_texts[i].delete("1.0", "end")
                    self.reply_texts[i].insert("1.0", reply["text"])
                seconds = time.monotonic() - self.job_started if self.job_started is not None else 0
                self.status.set(f"已生成 {len(replies)} 条草稿，用时 {seconds:.1f} 秒。请核对 AI 理解后编辑、复制；实际发出后可点“记为已发送”。")
            else:
                self.status.set(value)
        except queue.Empty:
            pass
        if self.busy and self.active_operation == "generate" and self.job_started is not None:
            seconds = int(time.monotonic() - self.job_started)
            self.status.set(f"正在等待 AI 分析和回复，已等待 {seconds} 秒；实际用时取决于模型、网络和上下文长度。")
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
        self.context.delete("1.0", "end"); self.remove_image()
        self.last_reader_text = None
        if not self.active_profile:
            self.intent.delete("1.0", "end"); self.relationship.set("")
        self.clear_results()
        self.status.set("已清空当前输入和草稿；已确认的会话记忆仍保留，可在“背景/记忆”中清除或删除。")

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
