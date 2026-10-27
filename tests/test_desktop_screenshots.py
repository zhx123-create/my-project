import gc
import os
from pathlib import Path
import tempfile
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
from conversation_memory import ConversationMemory
from desktop import CropDialog, DesktopApp
from desktop_settings import DesktopSettings
from desktop_screenshots import SCREENSHOT_SOURCE
from meme_library import MemeLibrary
from windows_capture import CaptureError, Window
from windows_ocr import OCR_HEADER


def ocr_text(text):
    return {'text': OCR_HEADER + text, 'count': len(text.splitlines()), 'source': 'windows_ocr', 'identity_verified': False}


@unittest.skipUnless(os.environ.get('DISPLAY') or os.name == 'nt', 'requires a desktop display')
class ScreenshotFlowTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.app = DesktopApp(self.root, memory=ConversationMemory(), meme_library=MemeLibrary(Path(self.folder.name) / 'memes'), settings=DesktopSettings())
        self.app.select_profile('朋友'); self.app.key.set('test-only-key')
        self.window = Window(123, 456, '微信聊天')
        self.app.windows = [self.window]; self.app.window_box.configure(values=['微信聊天']); self.app.window_box.current(0)
        self.image = Image.new('RGB', (400, 300), 'white'); self.image.putpixel((50, 50), (0, 0, 0))
        self.result = {'understanding': '正在确认时间。', 'replies': [{'label': '自然', 'text': '我确认一下安排。'}]}
        self.root.update()

    def tearDown(self):
        self.app.close(); del self.app, self.root
        gc.collect(); self.folder.cleanup()

    def settle(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.root.update()
            if not self.app.busy:
                return
            time.sleep(0.01)
        self.fail('screenshot worker did not settle')

    def choose_region(self):
        dialogs = [widget for widget in self.root.winfo_children() if isinstance(widget, CropDialog)]
        self.assertEqual(len(dialogs), 1)
        dialog = dialogs[0]
        dialog.begin(SimpleNamespace(x=20, y=20)); dialog.drag(SimpleNamespace(x=220, y=160))
        dialog.confirm()

    def test_selected_window_binds_once_then_ocr_and_text_only_generation_follow(self):
        with patch('desktop_screenshots.activate_window') as activate, patch('desktop_screenshots.capture_visible_isolated', return_value=self.image) as capture, patch('desktop_screenshots.recognize_image', return_value=ocr_text('对方：周五有空吗？')) as ocr, patch('desktop.generate', return_value=self.result) as model:
            self.app.start_screenshot_analysis(); self.settle()
            model.assert_not_called(); ocr.assert_not_called()
            self.choose_region(); self.settle()
        activate.assert_called_once_with(self.window); capture.assert_called_once_with(self.window)
        ocr.assert_called_once(); model.assert_called_once()
        self.assertEqual(self.app.shot_reader.binding.region, (20, 20, 220, 160))
        self.assertEqual(self.app.capture_source.get(), SCREENSHOT_SOURCE)
        self.assertIsNone(self.app.screenshot)
        parts = model.call_args.args[1][1]['content']
        self.assertEqual(len(parts), 1, 'OCR must not upload an image to a text-only model')
        self.assertIn('周五有空', parts[0]['text'])
        self.assertNotIn('本机截图识别', self.app.memory.history_text('朋友'), 'the provenance header is not a chat message')

    def test_automatic_screenshot_mode_reuses_region_and_skips_unchanged_ocr_and_ai(self):
        self.app.capture_source.set(SCREENSHOT_SOURCE)
        with patch('desktop_screenshots.activate_window'), patch('desktop_screenshots.capture_visible_isolated', return_value=self.image), patch('desktop_screenshots.recognize_image', return_value=ocr_text('对方：周五有空吗？')) as ocr, patch('desktop.generate', return_value=self.result) as model:
            self.app.toggle_watch(); self.settle(); self.choose_region(); self.settle()
            self.assertTrue(self.app.watch.active)
            crop = self.image.crop(self.app.shot_reader.binding.region)
            with patch('desktop_automation.list_windows', return_value=[self.window]), patch('desktop_screenshots.list_windows', return_value=[self.window]), patch('desktop_screenshots.capture_visible_isolated', return_value=crop) as capture:
                self.app.watch.next_check_at = 0; self.app.automation_tick(); self.settle()
                capture.assert_called_once()
                captured_window, region, size = capture.call_args.args
                self.assertEqual((captured_window.handle, captured_window.pid), (123, 456))
                self.assertEqual((region, size), ((20, 20, 220, 160), (400, 300)))
            ocr.assert_called_once(); model.assert_called_once()

    def test_manual_clipboard_screenshot_is_recognized_before_model_call(self):
        with patch('desktop_screenshots.ImageGrab.grabclipboard', return_value=self.image), patch('desktop_screenshots.recognize_image', return_value=ocr_text('对方：晚上一起吃饭吗？')), patch('desktop.generate', return_value=self.result) as model:
            self.app.paste_screenshot(); self.choose_region(); self.settle()
        model.assert_called_once()
        self.assertIn('一起吃饭', self.app.context.get('1.0', 'end-1c'))
        self.assertEqual(len(model.call_args.args[1][1]['content']), 1)

    def test_ocr_failure_never_sends_old_attachment_or_context(self):
        self.app.attach_image(self.image)
        old_history = self.app.memory.history_text('朋友')
        with patch('desktop_screenshots.recognize_image', side_effect=CaptureError('Windows OCR 语言未安装')), patch('desktop_screenshots.messagebox.showwarning') as warning, patch('desktop.generate') as model:
            self.app.analyze_screenshot_image(self.image); self.settle()
        model.assert_not_called(); warning.assert_called_once()
        self.assertIsNone(self.app.screenshot)
        self.assertEqual(self.app.memory.history_text('朋友'), old_history)
        self.assertIn('未安装', self.app.status.get())

    def test_cancel_region_selection_disarms_pending_automatic_capture(self):
        self.app.capture_source.set(SCREENSHOT_SOURCE)
        with patch('desktop_screenshots.activate_window'), patch('desktop_screenshots.capture_visible_isolated', return_value=self.image), patch('desktop.generate') as model:
            self.app.toggle_watch(); self.settle()
            dialog = next(widget for widget in self.root.winfo_children() if isinstance(widget, CropDialog))
            dialog.cancel()
        self.assertFalse(self.app.watch.active)
        self.assertFalse(self.app._watch_start_requested)
        model.assert_not_called()


if __name__ == '__main__':
    unittest.main()
