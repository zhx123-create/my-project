"""The desktop presentation: a quiet workspace with one obvious next action."""
from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from server import TONES

FONT = "Microsoft YaHei UI" if sys.platform == "win32" else "Noto Sans CJK SC"
COLORS = {
    "background": "#f5f6fb", "surface": "#ffffff", "ink": "#24283e",
    "muted": "#6f778f", "line": "#e7e9f2", "accent": "#7764df",
    "accent_hover": "#6652d0", "accent_light": "#efecfc", "editor": "#fafbfe",
}


def apply_theme(root):
    """Use the bundled ttk theme, with no platform-specific extra dependency."""
    root.configure(background=COLORS["background"])
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", font=(FONT, 10), foreground=COLORS["ink"], background=COLORS["background"])
    style.configure("TFrame", background=COLORS["background"])
    style.configure("TLabel", background=COLORS["background"], foreground=COLORS["ink"])
    style.configure("TButton", padding=(12, 8), borderwidth=0, relief="flat", background=COLORS["surface"], foreground=COLORS["ink"], anchor="center")
    style.map("TButton", background=[("active", "#eceefa"), ("disabled", "#f1f2f7")], foreground=[("disabled", "#afb4c4")])
    style.configure("Accent.TButton", background=COLORS["accent"], foreground="white", font=(FONT, 11, "bold"), padding=(18, 11))
    style.map("Accent.TButton", background=[("active", COLORS["accent_hover"]), ("disabled", "#b9afea")], foreground=[("disabled", "#f6f4ff")])
    style.configure("Soft.TButton", background=COLORS["accent_light"], foreground=COLORS["accent"])
    style.map("Soft.TButton", background=[("active", "#e4def9"), ("disabled", "#f1eff8")])
    style.configure("Quiet.TButton", background=COLORS["surface"], foreground=COLORS["muted"], padding=(6, 5))
    style.map("Quiet.TButton", foreground=[("active", COLORS["accent"])], background=[("active", COLORS["accent_light"])])
    style.configure("Card.TFrame", background=COLORS["surface"])
    style.configure("Card.TLabel", background=COLORS["surface"], foreground=COLORS["ink"])
    style.configure("Muted.Card.TLabel", background=COLORS["surface"], foreground=COLORS["muted"], font=(FONT, 9))
    style.configure("Title.Card.TLabel", background=COLORS["surface"], foreground=COLORS["ink"], font=(FONT, 12, "bold"))
    style.configure("Brand.TLabel", font=(FONT, 24, "bold"), foreground=COLORS["ink"])
    style.configure("Muted.TLabel", foreground=COLORS["muted"], font=(FONT, 9))
    style.configure("Caption.TLabel", foreground=COLORS["accent"], font=(FONT, 9, "bold"))
    style.configure("TCheckbutton", background=COLORS["background"], padding=2)
    style.map("TCheckbutton", background=[("active", COLORS["background"])])
    style.configure("Card.TCheckbutton", background=COLORS["surface"], foreground=COLORS["muted"], font=(FONT, 9))
    style.map("Card.TCheckbutton", background=[("active", COLORS["surface"])])
    style.configure("TEntry", fieldbackground=COLORS["surface"], bordercolor=COLORS["line"], lightcolor=COLORS["line"], darkcolor=COLORS["line"], padding=7)
    style.configure("TCombobox", padding=6, fieldbackground=COLORS["surface"], background=COLORS["surface"], bordercolor=COLORS["line"], lightcolor=COLORS["line"], darkcolor=COLORS["line"], arrowcolor=COLORS["muted"])
    style.map("TCombobox", fieldbackground=[("readonly", COLORS["surface"])], selectbackground=[("readonly", COLORS["surface"])], selectforeground=[("readonly", COLORS["ink"])])
    style.configure("Vertical.TScrollbar", background="#e5e7f1", troughcolor=COLORS["editor"], borderwidth=0, arrowsize=10, arrowcolor=COLORS["muted"])
    style.configure("TLabelframe", background=COLORS["surface"], bordercolor=COLORS["line"], relief="flat")
    style.configure("TLabelframe.Label", background=COLORS["surface"], foreground=COLORS["ink"])
    root.option_add("*TCombobox*Listbox.background", COLORS["surface"])
    root.option_add("*TCombobox*Listbox.foreground", COLORS["ink"])
    root.option_add("*TCombobox*Listbox.selectBackground", COLORS["accent_light"])
    root.option_add("*TCombobox*Listbox.selectForeground", COLORS["ink"])
    return style


def _card(parent, *, padding=14):
    border = tk.Frame(parent, background=COLORS["surface"], highlightthickness=1, highlightbackground=COLORS["line"], borderwidth=0)
    body = ttk.Frame(border, style="Card.TFrame", padding=padding)
    body.pack(fill="both", expand=True)
    return border, body


def _text(parent, *, height, background=None, padding=12):
    widget = ScrolledText(parent, height=height, width=1, wrap="word", font=(FONT, 10), relief="flat", borderwidth=0,
                          background=background or COLORS["editor"], foreground=COLORS["ink"], insertbackground=COLORS["accent"],
                          selectbackground="#e5defb", selectforeground=COLORS["ink"], padx=padding, pady=padding,
                          highlightthickness=0, undo=False, spacing1=3, spacing3=3)
    widget.vbar.configure(width=9, borderwidth=0, highlightthickness=0, relief="flat", background=COLORS["line"], activebackground="#cbd0e1", troughcolor=background or COLORS["editor"])
    return widget


def _placeholder(widget, text):
    label = tk.Label(widget, text=text, font=(FONT, 10), foreground="#a1a7ba", background=widget.cget("background"),
                     justify="left", anchor="nw", wraplength=260, borderwidth=0)
    def update(_event=None):
        if widget.get("1.0", "end-1c"):
            label.place_forget()
        else:
            label.place(x=12, y=14)
        if widget.edit_modified():
            widget.edit_modified(False)
    def focus(_event=None):
        widget.focus_set()
        label.place_forget()
    label.bind("<Button-1>", focus)
    widget.bind("<<Modified>>", update, add="+")
    widget.bind("<FocusIn>", lambda _event: label.place_forget(), add="+")
    widget.bind("<FocusOut>", update, add="+")
    update()


def build_desktop_ui(app):
    """Build presentation only; all reads and AI requests stay on DesktopApp."""
    apply_theme(app.root)
    root = app.root

    def button(parent, label, command, *, style="TButton", side="left", **packing):
        widget = ttk.Button(parent, text=label, command=command, style=style)
        widget.pack(side=side, **packing)
        app.controls.append((widget, "normal"))
        return widget

    page = ttk.Frame(root, padding=(22, 16, 22, 12)); page.pack(fill="both", expand=True)
    page.columnconfigure(0, weight=1); page.rowconfigure(2, weight=1)
    header = ttk.Frame(page); header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
    ttk.Label(header, text="接话", style="Brand.TLabel").pack(side="left")
    tagline = ttk.Frame(header); tagline.pack(side="left", padx=(14, 0))
    ttk.Label(tagline, text="让每句话，都更像你", style="Caption.TLabel").pack(anchor="w")
    ttk.Label(tagline, text="你的微信聊天搭子", style="Muted.TLabel").pack(anchor="w", pady=(2, 0))
    button(header, "AI 设置", app.show_settings, side="right", padx=(8, 0))
    button(header, "表达风格", app.show_reply_style, side="right", style="Soft.TButton")

    profile_card, profile = _card(page, padding=(12, 9))
    profile_card.grid(row=1, column=0, sticky="ew", pady=(0, 14))
    ttk.Label(profile, text="当前会话", style="Muted.Card.TLabel").pack(side="left", padx=(0, 10))
    app.profile_box = ttk.Combobox(profile, textvariable=app.conversation_name, width=18, state="readonly")
    app.profile_box.pack(side="left", fill="x", expand=True)
    app.profile_box.bind("<<ComboboxSelected>>", lambda _event: app.select_profile(app.conversation_name.get()))
    app.controls.append((app.profile_box, "readonly"))
    button(profile, "+ 新建", app.new_profile, style="Quiet.TButton", padx=(8, 3))
    button(profile, "背景与记忆", app.show_memory, style="Quiet.TButton")
    ttk.Separator(profile, orient="vertical").pack(side="left", fill="y", padx=12)
    ttk.Checkbutton(profile, text="置顶", variable=app.topmost, command=lambda: root.attributes("-topmost", app.topmost.get()), style="Card.TCheckbutton").pack(side="left")

    columns = tk.PanedWindow(page, orient="horizontal", background=COLORS["background"], sashwidth=14, borderwidth=0, sashrelief="flat")
    columns.grid(row=2, column=0, sticky="nsew")
    left_card, left_body = _card(columns, padding=14)
    right_card, right = _card(columns, padding=14)
    columns.add(left_card, minsize=390, stretch="always")
    columns.add(right_card, minsize=390, stretch="always")
    root.after_idle(lambda: columns.sash_place(0, max(390, int(columns.winfo_width() * .45)), 0))
    left_body.columnconfigure(0, weight=1); left_body.rowconfigure(1, weight=1)
    left_heading = ttk.Frame(left_body, style="Card.TFrame"); left_heading.grid(row=0, column=0, sticky="ew", pady=(0, 12))
    ttk.Label(left_heading, text="聊天上下文", style="Title.Card.TLabel").pack(side="left")
    button(left_heading, "粘贴聊天", app.import_text, style="Quiet.TButton", side="right")
    button(left_heading, "粘贴截图", app.paste_screenshot, style="Quiet.TButton", side="right", padx=(0, 5))
    app.input_canvas = tk.Canvas(left_body, background=COLORS["surface"], highlightthickness=0, borderwidth=0)
    app.input_canvas.grid(row=1, column=0, sticky="nsew")
    scroll = ttk.Scrollbar(app.input_canvas, orient="vertical", command=app.input_canvas.yview)
    inputs = ttk.Frame(app.input_canvas, style="Card.TFrame")
    content_id = app.input_canvas.create_window(0, 0, window=inputs, anchor="nw")
    app.input_canvas.configure(yscrollcommand=scroll.set)
    inputs.columnconfigure(0, weight=1); inputs.rowconfigure(0, weight=1)
    def resize_inputs(_event=None):
        height = app.input_canvas.winfo_height()
        if getattr(app, "advanced_open", False):
            height = max(inputs.winfo_reqheight(), height)
        width = app.input_canvas.winfo_width() - (12 if getattr(app, "advanced_open", False) else 0)
        app.input_canvas.itemconfigure(content_id, width=max(1, width), height=height)
        app.input_canvas.configure(scrollregion=app.input_canvas.bbox("all"))
    inputs.bind("<Configure>", resize_inputs)
    app.input_canvas.bind("<Configure>", resize_inputs)
    def scroll_inputs(event):
        if not getattr(app, "advanced_open", False) or isinstance(event.widget, (tk.Text, ttk.Combobox)):
            return
        owner = event.widget
        while owner is not None:
            if owner in (inputs, app.input_canvas):
                app.input_canvas.yview_scroll(-int(event.delta / 120), "units")
                return "break"
            owner = getattr(owner, "master", None)
    root.bind("<MouseWheel>", scroll_inputs, add="+")
    app.context = _text(inputs, height=6)
    app.context.grid(row=0, column=0, sticky="nsew")
    app.controls.append((app.context, "normal"))
    _placeholder(app.context, "打开微信中的聊天，点击下方一键分析。\n也可以直接粘贴最近的对话。")
    ttk.Label(inputs, text="这次想怎么聊？", style="Title.Card.TLabel").grid(row=2, column=0, sticky="w", pady=(13, 8))
    app.intent = _text(inputs, height=1, padding=9)
    app.intent.grid(row=3, column=0, sticky="ew")
    app.intent.insert("1.0", "自然接话，简洁一点")
    app.controls.append((app.intent, "normal"))
    preference = ttk.Frame(inputs, style="Card.TFrame"); preference.grid(row=4, column=0, sticky="ew", pady=(9, 0))
    short_style = tk.StringVar(root, value=app.style_summary.get().split("\n", 1)[0])
    app.style_summary.trace_add("write", lambda *_args: short_style.set(app.style_summary.get().split("\n", 1)[0]))
    ttk.Label(preference, textvariable=short_style, style="Muted.Card.TLabel", wraplength=260).pack(side="left", fill="x", expand=True)
    button(preference, "调整", app.show_reply_style, style="Quiet.TButton", side="right")
    app.confirm_box = ttk.Checkbutton(left_body, text="允许此会话的上下文提交给 AI", variable=app.consent, style="Card.TCheckbutton")
    ttk.Label(left_body, text="分析时会将此会话内容提交给 AI。", style="Muted.Card.TLabel", wraplength=345).grid(row=2, column=0, sticky="w", pady=(10, 0))
    app.controls.append((app.confirm_box, "normal"))

    advanced_heading = ttk.Frame(left_body, style="Card.TFrame"); advanced_heading.grid(row=3, column=0, sticky="ew", pady=(8, 0))
    app.advanced_panel = ttk.Frame(inputs, style="Card.TFrame")
    app.advanced_panel.grid(row=5, column=0, sticky="ew", pady=(14, 0)); app.advanced_panel.grid_remove()
    app.advanced_open = False
    def toggle_advanced():
        app.advanced_open = not app.advanced_open
        if app.advanced_open:
            app.advanced_panel.grid()
            scroll.place(relx=1, y=0, relheight=1, anchor="ne")
            scroll.lift()
            def reveal_tools():
                resize_inputs()
                root.after_idle(lambda: app.input_canvas.yview_moveto(1))
            root.after_idle(reveal_tools)
        else:
            app.advanced_panel.grid_remove()
            scroll.place_forget()
            app.input_canvas.yview_moveto(0)
        app.advanced_button.configure(text="收起工具  −" if app.advanced_open else "更多工具  +")
    app.toggle_advanced = toggle_advanced
    app.advanced_button = button(advanced_heading, "更多工具  +", toggle_advanced, style="Quiet.TButton")
    button(advanced_heading, "清空", app.clear, style="Quiet.TButton", side="right")
    tools = app.advanced_panel
    tools.columnconfigure(0, weight=1)
    ttk.Label(tools, textvariable=app.capture_status, style="Muted.Card.TLabel", wraplength=330).grid(row=0, column=0, sticky="w", pady=(0, 7))
    source = ttk.Frame(tools, style="Card.TFrame"); source.grid(row=1, column=0, sticky="ew")
    app.window_box = ttk.Combobox(source, textvariable=app.window_choice, state="readonly", width=10)
    app.window_box.pack(side="left", fill="x", expand=True); app.controls.append((app.window_box, "readonly"))
    app.window_box.bind("<<ComboboxSelected>>", lambda _event: app.capture_status.set("已选择微信窗口；请打开要分析的聊天。"))
    button(source, "查找微信", app.refresh_windows, style="Quiet.TButton", side="right", padx=(5, 0))
    capture_tools = ttk.Frame(tools, style="Card.TFrame"); capture_tools.grid(row=2, column=0, sticky="ew", pady=(7, 0))
    app.capture_button = button(capture_tools, "采集选中窗口", app.capture, style="Soft.TButton", fill="x", expand=True)
    app.screen_button = button(capture_tools, "系统截图", app.system_screenshot, style="Soft.TButton", padx=(5, 0), fill="x", expand=True)
    image_tools = ttk.Frame(tools, style="Card.TFrame"); image_tools.grid(row=3, column=0, sticky="ew", pady=5)
    button(image_tools, "粘贴截图给大模型", app.import_image, style="Quiet.TButton")
    button(image_tools, "移除截图", app.remove_image, style="Quiet.TButton", side="right")
    app.preview = ttk.Label(tools, text="未附加截图（最多 1 张）", style="Muted.Card.TLabel")
    app.preview.grid(row=4, column=0, sticky="w", pady=(0, 7))
    relationship = ttk.Frame(tools, style="Card.TFrame"); relationship.grid(row=5, column=0, sticky="ew")
    ttk.Label(relationship, text="基础语气", style="Muted.Card.TLabel").pack(side="left")
    tone = ttk.Combobox(relationship, textvariable=app.tone, values=list(TONES), width=5, state="readonly")
    tone.pack(side="left", padx=(6, 10)); app.controls.append((tone, "readonly"))
    ttk.Label(relationship, text="关系", style="Muted.Card.TLabel").pack(side="left")
    relation = ttk.Entry(relationship, textvariable=app.relationship, width=7)
    relation.pack(side="left", fill="x", expand=True, padx=(5, 0)); app.controls.append((relation, "normal"))
    mode = ttk.Combobox(tools, textvariable=app.reply_mode, values=["先给 1 条（较快）", "生成 3 条候选"], state="readonly")
    mode.grid(row=6, column=0, sticky="ew", pady=(8, 0)); app.controls.append((mode, "readonly"))
    if hasattr(app, "watch_interval"):
        interval = ttk.Frame(tools, style="Card.TFrame"); interval.grid(row=7, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(interval, text="自动辅助检查间隔（秒）", style="Muted.Card.TLabel").pack(side="left")
        chooser = ttk.Combobox(interval, textvariable=app.watch_interval, values=[5, 10, 20, 30], width=5, state="readonly")
        chooser.pack(side="right"); app.controls.append((chooser, "readonly"))
        if hasattr(app, "remember_ui_preferences"):
            chooser.bind("<<ComboboxSelected>>", lambda _event: app.remember_ui_preferences())
    mode_row = ttk.Frame(tools, style="Card.TFrame"); mode_row.grid(row=8, column=0, sticky="ew", pady=(8, 0))
    ttk.Label(mode_row, text="采集方式", style="Muted.Card.TLabel").pack(side="left")
    source_mode = ttk.Combobox(mode_row, textvariable=app.capture_source, values=["窗口文字", "截图转文字"], state="readonly", width=12)
    source_mode.pack(side="left", padx=8); app.controls.append((source_mode, "readonly"))
    button(mode_row, "重选区域", app.rebind_screenshot, style="Quiet.TButton", side="right")
    file_tools = ttk.Frame(tools, style="Card.TFrame"); file_tools.grid(row=9, column=0, sticky="ew", pady=(6, 0))
    button(file_tools, "打开截图文件", app.open_screenshot_file, style="Quiet.TButton")

    right.columnconfigure(0, weight=1)
    ttk.Label(right, text="回复工作台", style="Title.Card.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 12))
    insight = ttk.Frame(right, style="Card.TFrame"); insight.grid(row=1, column=0, sticky="ew", pady=(0, 10))
    ttk.Label(insight, text="AI 理解", style="Muted.Card.TLabel").pack(anchor="w", pady=(0, 5))
    app.understanding = _text(insight, height=2, background=COLORS["accent_light"], padding=9)
    app.understanding.pack(fill="x"); app.understanding.configure(state="disabled")
    app.reply_texts, app.reply_labels, app.reply_cards = [], [], []
    app.reply_action_buttons = []
    app.results_panel = right
    for index in range(3):
        card = ttk.Frame(right, style="Card.TFrame")
        card.grid(row=index + 2, column=0, sticky="nsew", pady=(0, 8))
        right.rowconfigure(index + 2, weight=1 if index == 0 else 0, uniform="replies" if index == 0 else "")
        app.reply_cards.append(card)
        if index:
            card.grid_remove()
        title = ttk.Frame(card, style="Card.TFrame"); title.pack(fill="x", pady=(0, 7))
        label = ttk.Label(title, text="尚未生成", style="Muted.Card.TLabel"); label.pack(side="left")
        ttk.Label(title, text=f"草稿 {index + 1:02}", style="Muted.Card.TLabel").pack(side="right")
        actions = ttk.Frame(card, style="Card.TFrame"); actions.pack(side="bottom", fill="x", pady=(7, 0))
        app.reply_action_buttons.append(button(actions, "复制回复", lambda position=index: app.copy_reply(position), style="Soft.TButton", side="right"))
        app.reply_action_buttons.append(button(actions, "记为已发送", lambda position=index: app.confirm_sent(position), style="Quiet.TButton"))
        reply = _text(card, height=3, padding=12)
        reply.pack(fill="both", expand=True)
        _placeholder(reply, "把聊天交给我，\n一起想一句合适的回复。")
        app.reply_labels.append(label); app.reply_texts.append(reply)
    meme_bar = ttk.Frame(right, style="Card.TFrame"); meme_bar.grid(row=5, column=0, sticky="ew", pady=(3, 0))
    button(meme_bar, "找个表情", app.show_memes, style="Quiet.TButton", side="right", padx=(6, 0))
    ttk.Label(meme_bar, textvariable=app.meme_note, style="Muted.Card.TLabel", wraplength=245).pack(side="left", fill="x", expand=True)

    action_bar = ttk.Frame(page); action_bar.grid(row=3, column=0, sticky="ew", pady=(14, 0))
    app.analyze_button = button(action_bar, "一键分析当前聊天", getattr(app, "quick_analyze", app.analyze_current_chat), style="Accent.TButton")
    app.generate_button = button(action_bar, "用编辑内容生成", getattr(app, "generate_from_editor", app.request_generation), padx=(10, 0))
    app.screenshot_button = button(action_bar, "截图识字", app.start_screenshot_analysis, style="Soft.TButton", padx=(8, 0))
    if hasattr(app, "toggle_watch"):
        app.watch_button = ttk.Button(action_bar, text="开启自动辅助", command=app.toggle_watch, style="Soft.TButton")
        app.watch_button.pack(side="right")
    if hasattr(app, "auto_copy"):
        automatic = ttk.Checkbutton(action_bar, text="生成后复制", variable=app.auto_copy)
        automatic.pack(side="right", padx=(8, 10)); app.controls.append((automatic, "normal"))
        if hasattr(app, "remember_ui_preferences"):
            automatic.configure(command=app.remember_ui_preferences)
    status = ttk.Frame(page); status.grid(row=4, column=0, sticky="ew", pady=(12, 0))
    status.columnconfigure(0, weight=1)
    if hasattr(app, "watch_status"):
        ttk.Label(status, textvariable=app.watch_status, style="Caption.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
    status_label = ttk.Label(status, textvariable=app.status, style="Muted.TLabel", wraplength=1000)
    status_label.grid(row=1, column=0, sticky="ew")
    memory_label = ttk.Label(status, textvariable=app.memory_note, style="Muted.TLabel", wraplength=1000)
    memory_label.grid(row=2, column=0, sticky="ew", pady=(3, 0))
    def resize_status(_event=None):
        width = max(500, status.winfo_width())
        status_label.configure(wraplength=width); memory_label.configure(wraplength=width)
        if root.winfo_height() < 760:
            memory_label.grid_remove()
        else:
            memory_label.grid()
    status.bind("<Configure>", resize_status)
