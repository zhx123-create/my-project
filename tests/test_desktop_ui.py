import gc
import os
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from conversation_memory import ConversationMemory
from desktop import DesktopApp
from desktop_settings import DesktopSettings
from desktop_ui import build_desktop_ui
from meme_library import MemeLibrary


@unittest.skipUnless(os.environ.get("DISPLAY") or os.name == "nt", "requires a desktop display")
class DesktopLayoutTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="jiehua-layout-")
        self.root = tk.Tk()
        def build(app):
            if not hasattr(app, "watch_status"):
                app.watch_status = tk.StringVar(self.root, value="自动辅助已暂停")
            if not hasattr(app, "watch_interval"):
                app.watch_interval = tk.IntVar(self.root, value=10)
            if not hasattr(app, "auto_copy"):
                app.auto_copy = tk.BooleanVar(self.root, value=False)
            if not hasattr(app, "toggle_watch"):
                app.toggle_watch = Mock()
            build_desktop_ui(app)
        with patch.object(DesktopApp, "_build", build):
            self.app = DesktopApp(self.root, memory=ConversationMemory(), meme_library=MemeLibrary(Path(self.folder.name) / "memes"), settings=DesktopSettings())
        self.root.geometry("870x680")
        self.root.update()

    def tearDown(self):
        self.app.close()
        del self.app, self.root
        gc.collect()
        self.folder.cleanup()

    def test_small_window_keeps_primary_actions_and_composer_visible(self):
        self.assertGreaterEqual(self.app.input_canvas.winfo_width(), 350)
        self.assertGreaterEqual(self.app.context.winfo_height(), 65)
        self.assertGreaterEqual(self.app.intent.winfo_height(), 35)
        self.assertFalse(self.app.confirm_box.winfo_ismapped(), 'explicit action replaces the repeated permission checkbox')
        for widget in (self.app.analyze_button, self.app.generate_button, self.app.watch_button):
            self.assertTrue(widget.winfo_ismapped())
            ancestor = widget.master
            while ancestor is not None:
                x = widget.winfo_rootx() - ancestor.winfo_rootx()
                y = widget.winfo_rooty() - ancestor.winfo_rooty()
                self.assertGreaterEqual(x, 0)
                self.assertGreaterEqual(y, 0)
                self.assertLessEqual(x + widget.winfo_width(), ancestor.winfo_width())
                self.assertLessEqual(y + widget.winfo_height(), ancestor.winfo_height())
                ancestor = getattr(ancestor, "master", None)

    def test_capture_tools_expand_without_reading_or_overwriting_inputs(self):
        self.app.context.insert("1.0", "对方：周末一起吃饭吗？")
        self.assertFalse(self.app.window_box.winfo_ismapped())
        self.assertFalse(self.app.capture_button.winfo_ismapped())
        with patch("desktop.list_windows") as find_windows, patch("desktop.read_conversation") as read, patch("desktop.generate") as generate:
            self.app.advanced_button.invoke(); self.root.update()
            self.assertTrue(self.app.window_box.winfo_ismapped())
            self.assertTrue(self.app.capture_button.winfo_ismapped())
            self.assertLess(self.app.input_canvas.yview()[0], self.app.input_canvas.yview()[1])
            self.assertLess(self.app.input_canvas.yview()[1] - self.app.input_canvas.yview()[0], 1)
            self.app.advanced_button.invoke(); self.root.update()
            find_windows.assert_not_called(); read.assert_not_called(); generate.assert_not_called()
        self.assertFalse(self.app.window_box.winfo_ismapped())
        self.assertEqual(self.app.context.get("1.0", "end-1c"), "对方：周末一起吃饭吗？")
        self.assertGreaterEqual(self.app.context.winfo_height(), 65)

    def test_placeholders_do_not_become_chat_or_reply_content(self):
        self.assertEqual(self.app.context.get("1.0", "end-1c"), "")
        self.assertEqual(self.app.reply_texts[0].get("1.0", "end-1c"), "")
        self.app.reply_texts[0].insert("1.0", "可以呀，我确认一下时间。")
        self.root.update()
        self.app.copy_reply(0)
        self.assertEqual(self.root.clipboard_get(), "可以呀，我确认一下时间。")


if __name__ == "__main__":
    unittest.main()
