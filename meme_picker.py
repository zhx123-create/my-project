"""Explicit local/public reaction-image picker. It never sends to a chat app."""
from __future__ import annotations

from io import BytesIO
import os
from pathlib import Path
import queue
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from PIL import Image, ImageTk

from meme_library import MAX_FILE_BYTES


def _search_public(source, query: str, tags: list[str], results: queue.Queue):
    """Worker downloads public packs and matches the theme/tags locally."""
    try:
        candidates = source.search(query, tags=tags, limit=3)
        results.put(("results", candidates))
    except Exception as error:
        results.put(("error", str(error) or "联网查找失败，请稍后重试。"))


def _candidate_bytes(candidate: dict, library) -> bytes:
    if "data" in candidate:
        data = candidate["data"]
    else:
        with library.get_path(candidate["id"]).open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
    if not isinstance(data, bytes) or not data or len(data) > MAX_FILE_BYTES:
        raise ValueError("表情文件为空或超过 10 MB，请重新选择。")
    return data


def _write_image(destination: Path, data: bytes):
    """Keep the original GIF/WebP bytes; replace the selected file atomically."""
    destination = Path(destination)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".meme-save-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class MemePicker(tk.Toplevel):
    def __init__(self, parent, library, query="", tags=None, public_source=None):
        super().__init__(parent)
        self.title("表情候选 · 看图后手动发送")
        width = max(720, min(790, self.winfo_screenwidth() - 60))
        height = max(620, min(700, self.winfo_screenheight() - 90))
        self.geometry(f"{width}x{height}")
        self.minsize(720, 620)
        self.transient(parent)
        ttk.Style(self).configure("MemePicker.TButton", padding=(8, 3))
        self.library = library
        self.public_source = public_source
        self._closed = False
        self._searching = False
        self._results = queue.Queue()
        self._poll_id = None
        self._candidates = []
        self._photos = []
        self._local_items = {}
        self.query_var = tk.StringVar(self, value=str(query or "")[:120])
        self.tags_var = tk.StringVar(self, value="，".join(tags or []))
        self.status_var = tk.StringVar(self, value="主题和标签仅在本机匹配；联网会下载公开分类包。")
        self.source_var = tk.StringVar(self, value="选中图片后可保存原文件，再到微信手动发送。")
        self.selected_var = tk.IntVar(self, value=-1)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._build()
        self._refresh_library()
        self._recommend_local()

    def _build(self):
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text="表情主题").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(body, textvariable=self.query_var).grid(row=0, column=1, sticky="ew")
        controls = ttk.Frame(body)
        controls.grid(row=0, column=2, rowspan=2, padx=(8, 0))
        ttk.Button(controls, text="本地找候选", command=self._recommend_local, style="MemePicker.TButton").pack(fill="x")
        self.search_button = ttk.Button(controls, text="联网找候选", command=self._start_search, style="MemePicker.TButton")
        self.search_button.pack(fill="x", pady=(4, 0))
        ttk.Label(body, text="标签").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(body, textvariable=self.tags_var).grid(row=1, column=1, sticky="ew", pady=(6, 0))
        ttk.Label(body, text="例如：抽象，搞怪，无语，开心，拒绝；也用于导入图片。", foreground="#666666").grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 8))
        self.candidate_frame = ttk.Frame(body)
        self.candidate_frame.grid(row=3, column=0, columnspan=3, sticky="ew")
        for column in range(3):
            self.candidate_frame.columnconfigure(column, weight=1, uniform="candidate")
        source_row = ttk.Frame(body)
        source_row.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(7, 5))
        ttk.Label(source_row, textvariable=self.source_var, wraplength=510, foreground="#555555").pack(side="left", fill="x", expand=True)
        self.source_button = ttk.Button(source_row, text="查看来源", command=self._open_source, state="disabled", style="MemePicker.TButton")
        self.source_button.pack(side="right", padx=(6, 0))
        actions = ttk.Frame(body)
        actions.grid(row=5, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Button(actions, text="保存选中的图片…", command=self._save_selected, style="MemePicker.TButton").pack(side="left")
        ttk.Button(actions, text="把候选加入本地库", command=self._add_selected, style="MemePicker.TButton").pack(side="left", padx=(8, 0))
        ttk.Label(body, text="我的本地表情库（仅显示明确导入的图片）").grid(row=6, column=0, columnspan=3, sticky="w")
        local = ttk.Frame(body)
        local.grid(row=7, column=0, columnspan=3, sticky="nsew", pady=(5, 0))
        body.rowconfigure(7, weight=1, minsize=75)
        self.local_list = ttk.Treeview(local, columns=("name", "tags"), show="headings", height=3, selectmode="browse")
        self.local_list.heading("name", text="文件名")
        self.local_list.heading("tags", text="标签")
        self.local_list.column("name", width=310)
        self.local_list.column("tags", width=330)
        self.local_list.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(local, command=self.local_list.yview)
        scrollbar.pack(side="right", fill="y")
        self.local_list.configure(yscrollcommand=scrollbar.set)
        self.local_list.bind("<<TreeviewSelect>>", self._show_local_selection)
        library_actions = ttk.Frame(body)
        library_actions.grid(row=8, column=0, columnspan=3, sticky="w", pady=(7, 0))
        ttk.Button(library_actions, text="导入我选择的图片…", command=self._import_files, style="MemePicker.TButton").pack(side="left")
        ttk.Button(library_actions, text="移除选中的本地副本", command=self._remove_local, style="MemePicker.TButton").pack(side="left", padx=(8, 0))
        ttk.Label(body, textvariable=self.status_var, wraplength=735).grid(row=9, column=0, columnspan=3, sticky="w", pady=(9, 0))

    def _tags(self):
        import re
        raw = self.tags_var.get().strip()
        if len(raw) > 200:
            raise ValueError("标签最多 200 字。")
        tags = list(dict.fromkeys(tag for tag in re.split(r"[,，;；\s]+", raw) if tag))
        if len(tags) > 10 or any(len(tag) > 16 for tag in tags):
            raise ValueError("最多填写 10 个标签，每个最多 16 字。")
        return tags

    def _refresh_library(self):
        try:
            items = self.library.list()
            self.local_list.delete(*self.local_list.get_children())
            self._local_items = {item["id"]: item for item in items}
            for item in items:
                name = item["name"] + (" · 动图" if item.get("animated") else "")
                self.local_list.insert("", "end", iid=item["id"], values=(name, "、".join(item["tags"])))
        except (OSError, ValueError) as error:
            self.status_var.set(str(error))

    def _recommend_local(self):
        try:
            candidates = self.library.recommend(self.query_var.get().strip(), tags=self._tags(), limit=3)
            self._show_candidates(candidates)
            self.status_var.set("本地按文件名和标签匹配，请看图确认。" if candidates else "本地库还是空的，请先导入图片，或点击“联网找候选”。")
        except (OSError, ValueError) as error:
            self.status_var.set(str(error))

    def _show_candidates(self, candidates):
        self._candidates = candidates[:3]
        self._photos = []
        self.selected_var.set(0 if self._candidates else -1)
        for child in self.candidate_frame.winfo_children():
            child.destroy()
        for index in range(3):
            card = ttk.LabelFrame(self.candidate_frame, text=f"候选 {index + 1}", padding=6)
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 5, 0))
            if index >= len(self._candidates):
                ttk.Label(card, text="暂无候选", anchor="center", width=25).pack(pady=55)
                continue
            item = self._candidates[index]
            image_label = ttk.Label(card, anchor="center")
            image_label.pack(fill="x")
            try:
                data = _candidate_bytes(item, self.library)
                with Image.open(BytesIO(data)) as original:
                    original.seek(0)
                    preview = original.convert("RGBA")
                    preview.thumbnail((180, 90), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(preview, master=self)
                self._photos.append(photo)
                image_label.configure(image=photo)
            except (OSError, ValueError) as error:
                image_label.configure(text="无法预览此图片", width=25)
                self.status_var.set(str(error))
            image_label.bind("<Button-1>", lambda _event, selected=index: self._select(selected))
            name = item.get("name", "表情")
            short_name = name if len(name) <= 20 else name[:19] + "…"
            name_label = ttk.Label(card, text=short_name, wraplength=185)
            name_label.pack(anchor="w", pady=(3, 0))
            name_label.bind("<Double-Button-1>", lambda _event, selected=index: self._show_details(selected))
            selection_label = "选这张 · 动图首帧" if item.get("animated") else "选这张"
            ttk.Radiobutton(card, text=selection_label, variable=self.selected_var, value=index, command=self._selection_changed).pack(anchor="w", pady=(2, 0))
        self._selection_changed()

    def _select(self, index):
        self.selected_var.set(index)
        self._selection_changed()

    def _selection_changed(self):
        item = self._selected()
        if item is None:
            self.source_var.set("选中图片后可保存原文件，再到微信手动发送。")
            self.source_button.configure(state="disabled")
            return
        source = item.get("source", "我的本地库")
        detail = item.get("reason", "文件名/标签匹配，请看图确认")
        self.source_var.set(f"来源：{source}。{detail}")
        self.source_button.configure(state="normal" if item.get("source_url") else "disabled")

    def _open_source(self):
        item = self._selected()
        if item is not None and item.get("source_url"):
            # The only online provider is a fixed public collection. Open its
            # project page, rather than triggering a release ZIP download.
            from public_meme_source import SOURCE_PAGE
            webbrowser.open(SOURCE_PAGE)

    def _show_details(self, index):
        if 0 <= index < len(self._candidates):
            item = self._candidates[index]
            messagebox.showinfo("表情图片详情", f"文件名：{item.get('name', '表情')}\n标签：{'、'.join(item.get('tags', []))}", parent=self)

    def _selected(self):
        index = self.selected_var.get()
        return self._candidates[index] if 0 <= index < len(self._candidates) else None

    def _show_local_selection(self, _event=None):
        selection = self.local_list.selection()
        if selection and selection[0] in self._local_items:
            self._show_candidates([self._local_items[selection[0]]])

    def _start_search(self):
        if self._searching or self._closed:
            return
        try:
            query = self.query_var.get().strip()
            tags = self._tags()
            if len(query) > 120:
                raise ValueError("联网主题请控制在 120 字以内，只描述表情情绪，不要粘贴聊天记录。")
            if not query and not tags:
                raise ValueError("先填写想找的表情主题或标签，例如“抽象 无语”。")
            if self.public_source is None:
                # Construction allocates a cache only; the button starts the
                # worker that performs network access. Reuse it across clicks.
                from public_meme_source import PublicMemeSource
                self.public_source = PublicMemeSource()
        except (ImportError, ValueError) as error:
            self.status_var.set(str(error))
            return
        self._searching = True
        self.search_button.configure(state="disabled")
        self.status_var.set("正在下载公开分类包…主题和标签仅在本机匹配。")
        threading.Thread(target=_search_public, args=(self.public_source, query, tags, self._results), daemon=True).start()
        self._poll_id = self.after(100, self._poll_results)

    def _poll_results(self):
        self._poll_id = None
        if self._closed:
            return
        try:
            kind, payload = self._results.get_nowait()
        except queue.Empty:
            self._poll_id = self.after(100, self._poll_results)
            return
        self._searching = False
        self.search_button.configure(state="normal")
        if kind == "error":
            self.status_var.set(payload)
        else:
            self._show_candidates(payload)
            self.status_var.set("找到公开图库候选，请看图确认；保存会保留动图。" if payload else "没有找到合适的候选，试试更短的标签，或导入自己的表情。")

    def _save_selected(self):
        item = self._selected()
        if item is None:
            self.status_var.set("请先选择一张候选图片。")
            return
        extension = item["extension"]
        destination = filedialog.asksaveasfilename(parent=self, title="保存表情原文件", initialfile=Path(item["name"]).stem + extension, defaultextension=extension, filetypes=[("表情图片", "*" + extension)])
        if not destination:
            return
        try:
            _write_image(Path(destination), _candidate_bytes(item, self.library))
            self.status_var.set("已保存图片，请在微信中手动选择并发送。")
        except (OSError, ValueError) as error:
            self.status_var.set(f"未能保存：{error}")

    def _add_selected(self):
        item = self._selected()
        if item is None:
            self.status_var.set("请先选择一张候选图片。")
            return
        if "data" not in item:
            self.status_var.set("这张图片已经在本地表情库中。")
            return
        try:
            data = _candidate_bytes(item, self.library)
            with tempfile.TemporaryDirectory(prefix="jiehua-meme-") as directory:
                path = Path(directory) / (Path(item["name"]).stem[:80] + item["extension"])
                path.write_bytes(data)
                self.library.import_files([path], tags="，".join(item.get("tags", [])))
            self._refresh_library()
            self.status_var.set("已将候选加入本地库，原始动图保持不变。")
        except (OSError, ValueError) as error:
            self.status_var.set(str(error))

    def _import_files(self):
        paths = filedialog.askopenfilenames(parent=self, title="选择要导入的表情（每次最多 20 张）", filetypes=[("表情图片", "*.png *.jpg *.jpeg *.gif *.webp"), ("所有文件", "*.*")])
        if not paths:
            return
        try:
            items = self.library.import_files([Path(path) for path in paths], tags="，".join(self._tags()))
            self._refresh_library()
            self._show_candidates(items[-3:])
            self.status_var.set(f"已导入 {len(items)} 张表情的本地副本；原文件保持不变。")
        except (OSError, ValueError) as error:
            self.status_var.set(str(error))

    def _remove_local(self):
        selection = self.local_list.selection()
        if not selection:
            self.status_var.set("先在本地库列表中选中要移除的图片。")
            return
        if not messagebox.askyesno("移除本地副本", "仅删除表情库内这张图片的副本，原始图片保留。是否继续？", parent=self):
            return
        try:
            self.library.remove(selection[0])
            self._refresh_library()
            self._recommend_local()
            self.status_var.set("已移除表情库副本，原文件保持不变。")
        except (OSError, ValueError) as error:
            self.status_var.set(str(error))

    def _on_destroy(self, event):
        if event.widget is self:
            self._closed = True
            if self._poll_id is not None:
                try:
                    self.after_cancel(self._poll_id)
                except tk.TclError:
                    pass
                self._poll_id = None

    def _close(self):
        self._closed = True
        if self._poll_id is not None:
            self.after_cancel(self._poll_id)
            self._poll_id = None
        self.destroy()
