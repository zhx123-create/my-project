import gc
from io import BytesIO
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from meme_library import MemeLibrary
from meme_picker import MemePicker, _candidate_bytes, _search_public, _write_image


def animated_bytes():
    buffer = BytesIO()
    Image.new("RGB", (16, 12), "red").save(
        buffer, format="GIF", save_all=True,
        append_images=[Image.new("RGB", (16, 12), "blue")], duration=100, loop=0)
    return buffer.getvalue()


class PickerHelperTests(unittest.TestCase):
    def test_worker_uses_only_theme_and_tags_and_returns_original_bytes(self):
        source = Mock()
        data = animated_bytes()
        source.search.return_value = [{"name": "无语.gif", "data": data}]
        results = queue.Queue()
        worker = threading.Thread(target=_search_public, args=(source, "无语", ["搞怪"], results))
        worker.start()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        source.search.assert_called_once_with("无语", tags=["搞怪"], limit=3)
        kind, candidates = results.get_nowait()
        self.assertEqual(kind, "results")
        self.assertEqual(candidates[0]["data"], data)

    def test_worker_failure_is_returned_without_tk(self):
        source = Mock()
        source.search.side_effect = ValueError("图库暂时不可用")
        results = queue.Queue()
        _search_public(source, "开心", [], results)
        self.assertEqual(results.get_nowait(), ("error", "图库暂时不可用"))

    def test_public_and_local_images_keep_animation_bytes(self):
        data = animated_bytes()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = root / "原图.gif"
            original.write_bytes(data)
            library = MemeLibrary(root / "library")
            item = library.import_files([original])[0]
            self.assertEqual(_candidate_bytes(item, library), data)
            self.assertEqual(_candidate_bytes({"data": data}, library), data)
            saved = root / "保存.gif"
            _write_image(saved, _candidate_bytes(item, library))
            self.assertEqual(saved.read_bytes(), data)
            with Image.open(saved) as image:
                self.assertEqual(image.n_frames, 2)
            self.assertEqual(original.read_bytes(), data)

    def test_failed_save_preserves_existing_destination_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "表情.gif"
            destination.write_bytes(b"old")
            with patch("meme_picker.os.replace", side_effect=OSError("write denied")):
                with self.assertRaises(OSError):
                    _write_image(destination, animated_bytes())
            self.assertEqual(destination.read_bytes(), b"old")
            self.assertEqual(list(Path(folder).iterdir()), [destination])

    def test_invalid_public_data_is_rejected(self):
        for data in (None, "bytes", b""):
            with self.subTest(data=data), self.assertRaises(ValueError):
                _candidate_bytes({"data": data}, Mock())

    def test_closed_dialog_does_not_poll_or_touch_widgets(self):
        subject = SimpleNamespace(_closed=True, _poll_id="pending", after=Mock(), _results=Mock(), search_button=Mock())
        MemePicker._poll_results(subject)
        subject.after.assert_not_called()
        subject._results.get_nowait.assert_not_called()
        subject.search_button.configure.assert_not_called()

    def test_search_requires_explicit_nonempty_theme_or_tags(self):
        status = Mock()
        subject = SimpleNamespace(_searching=False, _closed=False,
                                  query_var=Mock(get=Mock(return_value="")),
                                  _tags=lambda: [], status_var=status)
        with patch("meme_picker.threading.Thread") as worker:
            MemePicker._start_search(subject)
        worker.assert_not_called()
        self.assertIn("主题或标签", status.set.call_args.args[0])

    def test_network_search_rejects_long_chat_text(self):
        status = Mock()
        subject = SimpleNamespace(_searching=False, _closed=False,
                                  query_var=Mock(get=Mock(return_value="聊天内容" * 40)),
                                  _tags=lambda: [], status_var=status)
        with patch("meme_picker.threading.Thread") as worker:
            MemePicker._start_search(subject)
        worker.assert_not_called()
        self.assertIn("不要粘贴聊天记录", status.set.call_args.args[0])

    def test_default_public_source_is_constructed_once_on_explicit_search(self):
        subject = SimpleNamespace(_searching=False, _closed=False, public_source=None,
                                  query_var=Mock(get=Mock(return_value="无语")), _tags=lambda: ["抽象"],
                                  status_var=Mock(), search_button=Mock(), _results=queue.Queue(),
                                  after=Mock(return_value="poll"), _poll_results=Mock())
        with patch("public_meme_source.PublicMemeSource") as source_class, patch("meme_picker.threading.Thread") as worker:
            source_class.assert_not_called()
            MemePicker._start_search(subject)
            first_source = subject.public_source
            subject._searching = False
            MemePicker._start_search(subject)
        source_class.assert_called_once_with()
        self.assertIs(subject.public_source, first_source)
        self.assertIs(worker.call_args.kwargs["args"][0], first_source)


@unittest.skipUnless(os.environ.get("DISPLAY") or os.name == "nt", "requires a desktop display")
class PickerWidgetTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.library = MemeLibrary(Path(self.folder.name) / "library")
        self.root = tk.Tk()
        self.source = Mock()
        self.source.search.return_value = []
        self.picker = MemePicker(self.root, self.library, query="无语", tags=["抽象"], public_source=self.source)
        self.root.update()

    def tearDown(self):
        if self.picker.winfo_exists():
            self.picker._close()
        self.root.destroy()
        del self.picker
        del self.root
        gc.collect()
        self.folder.cleanup()

    def test_opening_and_local_browsing_never_searches_online(self):
        self.source.search.assert_not_called()
        self.picker._recommend_local()
        self.root.update()
        self.source.search.assert_not_called()
        self.assertFalse(self.library.directory.exists())

    def test_explicit_search_consumes_result_on_ui_thread(self):
        self.picker._start_search()
        deadline = time.monotonic() + 2
        while self.picker._searching and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertFalse(self.picker._searching)
        self.source.search.assert_called_once_with("无语", tags=["抽象"], limit=3)
        self.assertIn("没有找到", self.picker.status_var.get())

    def test_close_during_search_leaves_worker_with_no_tk_references(self):
        released = threading.Event()
        finished = threading.Event()

        def search(*_args, **_kwargs):
            released.wait(2)
            finished.set()
            return []

        self.source.search.side_effect = search
        self.picker._start_search()
        self.picker._close()
        released.set()
        self.assertTrue(finished.wait(2))
        self.root.update()
        self.assertTrue(self.picker._closed)

    def test_long_names_and_animated_candidates_fit_small_window(self):
        # Match the main application's font and button padding: ordinary Tk
        # defaults hide layout problems that appear with Chinese desktop fonts.
        style = ttk.Style(self.root)
        style.theme_use("clam")
        font = ("Microsoft YaHei UI" if os.name == "nt" else "Noto Sans CJK SC", 10)
        style.configure("TLabel", font=font)
        style.configure("TButton", font=font, padding=(10, 7))
        for index, color in enumerate(("red", "blue")):
            path = Path(self.folder.name) / f"本地图{index}.png"
            Image.new("RGB", (30, 30), color).save(path)
            self.library.import_files([path])
        self.picker._refresh_library()
        self.picker.geometry("720x620")
        large_animation = BytesIO()
        Image.new("RGB", (400, 300), "red").save(
            large_animation, format="GIF", save_all=True,
            append_images=[Image.new("RGB", (400, 300), "blue")], duration=100, loop=0)
        item = {"name": "滑稽大佬-这是一个很长很长不能撑宽窗口的搞怪表情文件名00041.gif",
                "data": large_animation.getvalue(), "animated": True, "extension": ".gif",
                "tags": ["抽象", "搞怪"], "source": "ChineseBQB 公开图库",
                "source_url": "https://github.com/zhaoolee/ChineseBQB/releases/download/bqb-downloads/bqb-015-473f1fd0cb35ff60.zip",
                "reason": "分类匹配，请看图确认"}
        self.picker._show_candidates([dict(item) for _ in range(3)])
        self.root.update()
        self.assertGreaterEqual(self.picker.local_list.winfo_height(), 70)
        for image_id in self.picker.local_list.get_children()[:2]:
            _x, y, _width, height = self.picker.local_list.bbox(image_id)
            self.assertLessEqual(y + height, self.picker.local_list.winfo_height())
        for card in self.picker.candidate_frame.winfo_children():
            selection = next(child for child in card.winfo_children() if isinstance(child, ttk.Radiobutton))
            self.assertIn("动图首帧", selection.cget("text"))
            self.assertLessEqual(selection.winfo_reqwidth(), selection.winfo_width())
        self.assertNotIn("https://", self.picker.source_var.get())
        def check_visible_controls(widget):
            for child in widget.winfo_children():
                if isinstance(child, ttk.Button) and child.winfo_ismapped():
                    ancestor = child.master
                    while ancestor is not None and ancestor is not self.root:
                        right = child.winfo_rootx() - ancestor.winfo_rootx() + child.winfo_width()
                        bottom = child.winfo_rooty() - ancestor.winfo_rooty() + child.winfo_height()
                        self.assertLessEqual(right, ancestor.winfo_width())
                        self.assertLessEqual(bottom, ancestor.winfo_height())
                        ancestor = getattr(ancestor, "master", None)
                check_visible_controls(child)
        check_visible_controls(self.picker)

    def test_source_button_opens_project_page_only_after_click(self):
        item = {"name": "搞怪.gif", "data": animated_bytes(), "animated": True,
                "extension": ".gif", "source": "ChineseBQB 公开图库",
                "source_url": "https://github.com/zhaoolee/ChineseBQB/releases/download/bqb-downloads/bqb-015-473f1fd0cb35ff60.zip"}
        with patch("meme_picker.webbrowser.open") as open_page:
            self.picker._show_candidates([item])
            open_page.assert_not_called()
            self.picker.source_button.invoke()
        open_page.assert_called_once_with("https://github.com/zhaoolee/ChineseBQB")


if __name__ == "__main__":
    unittest.main()
