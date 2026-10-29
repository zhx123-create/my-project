import gc
import base64
import io
import json
import os
from pathlib import Path
import tempfile
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
import urllib.error
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

    def test_pasted_screenshot_can_go_directly_to_model_then_become_context(self):
        content = {**self.result, 'screenshot_text': '未知：周五有空吗？\n未知：我看一下安排。'}
        upstream = {'choices': [{'message': {'content': json.dumps(content)}}]}
        with patch('desktop_screenshots.ImageGrab.grabclipboard', return_value=self.image), patch('desktop_screenshots.recognize_image') as ocr, patch('server.urllib.request.OpenerDirector.open', side_effect=lambda *a, **k: io.BytesIO(json.dumps(upstream).encode())) as opened:
            self.app.paste_screenshot()
            dialog = next(widget for widget in self.root.winfo_children() if isinstance(widget, CropDialog))
            self.assertIn('disabled', dialog.vision_use.state())
            opened.assert_not_called()
            dialog.begin(SimpleNamespace(x=20, y=20)); dialog.drag(SimpleNamespace(x=220, y=160))
            dialog.vision_use.invoke(); self.settle()
            opened.assert_called_once(); ocr.assert_not_called()
            payload = json.loads(opened.call_args.args[0].data)
            image_url = payload['messages'][1]['content'][1]['image_url']['url']
            with Image.open(io.BytesIO(base64.b64decode(image_url.split(',', 1)[1]))) as sent:
                self.assertEqual(sent.size, (200, 140), 'only the approved crop is sent')
            self.assertIn('screenshot_text', payload['messages'][0]['content'])
            self.assertIn(content['screenshot_text'], self.app.context.get('1.0', 'end-1c'))
            self.assertIn('周五有空', self.app.memory.history_text('朋友'))
            self.assertNotIn('大模型截图转写', self.app.memory.history_text('朋友'))
            self.assertIsNone(self.app.screenshot)
            self.app.generate_from_editor(); self.settle()
            followup = json.loads(opened.call_args.args[0].data)
            self.assertEqual(len(followup['messages'][1]['content']), 1, 'follow-up should reuse text without uploading the screenshot again')
            self.assertEqual(self.app.memory.history_text('朋友').count('周五有空'), 1)

    def test_vision_failure_does_not_display_drafts_or_invent_history(self):
        error = urllib.error.HTTPError('https://api.openai.com', 400, 'private upstream error', {}, None)
        with patch('server.urllib.request.OpenerDirector.open', side_effect=error), patch('desktop_screenshots.recognize_image') as ocr:
            self.app.analyze_vision_image(self.image); self.settle()
        ocr.assert_not_called()
        self.assertIn('image_url', self.app.status.get())
        self.assertNotIn('private upstream', self.app.status.get())
        self.assertEqual(self.app.memory.history_text('朋友'), '')
        self.assertEqual(self.app.reply_texts[0].get('1.0', 'end-1c'), '')

    def test_vision_without_transcript_does_not_claim_to_have_read_image(self):
        upstream = {'choices': [{'message': {'content': json.dumps(self.result)}}]}
        with patch('server.urllib.request.OpenerDirector.open', return_value=io.BytesIO(json.dumps(upstream).encode())):
            self.app.analyze_vision_image(self.image); self.settle()
        self.assertIn('模型未返回有效的截图文字', self.app.status.get())
        self.assertEqual(self.app.memory.history_text('朋友'), '')
        self.assertEqual(self.app.reply_texts[0].get('1.0', 'end-1c'), '')

    def test_cancel_screenshot_choice_uploads_nothing(self):
        with patch('desktop_screenshots.ImageGrab.grabclipboard', return_value=self.image), patch('server.urllib.request.OpenerDirector.open') as opened, patch('desktop_screenshots.recognize_image') as ocr:
            self.app.paste_screenshot()
            dialog = next(widget for widget in self.root.winfo_children() if isinstance(widget, CropDialog))
            dialog.cancel()
        opened.assert_not_called(); ocr.assert_not_called()
        self.assertIsNone(self.app.screenshot)

    def test_pasting_system_screenshot_stops_live_capture_before_preview(self):
        self.app.capture_source.set(SCREENSHOT_SOURCE)
        self.app.watch.start('朋友', self.window)
        self.app.watch.next_check_at = 0
        self.app._watch_start_requested = True
        with patch('desktop_screenshots.ImageGrab.grabclipboard', return_value=self.image), patch('desktop_screenshots.capture_visible_isolated', side_effect=CaptureError('选区移出屏幕')) as capture, patch('desktop_automation.list_windows') as windows, patch('desktop_screenshots.recognize_image', return_value=ocr_text('未知：晚上一起吃饭吗？')), patch('desktop.generate', return_value=self.result) as model:
            self.app.paste_screenshot()
            self.assertFalse(self.app.watch.active)
            self.assertFalse(self.app._watch_start_requested)
            self.app.automation_tick()
            capture.assert_not_called(); windows.assert_not_called(); model.assert_not_called()
            self.choose_region(); self.settle()
            capture.assert_not_called(); windows.assert_not_called(); model.assert_called_once()
        self.assertIn('一起吃饭', self.app.context.get('1.0', 'end-1c'))

    def test_screenshot_file_does_not_need_an_onscreen_wechat_window(self):
        path = Path(self.folder.name) / 'chat.png'
        self.image.save(path)
        self.app.windows = []; self.app.window_choice.set('')
        with patch('desktop_screenshots.filedialog.askopenfilename', return_value=str(path)), patch('desktop_screenshots.capture_visible_isolated', side_effect=CaptureError('选区移出屏幕')) as capture, patch('desktop_screenshots.recognize_image', return_value=ocr_text('未知：周五几点见？')), patch('desktop.generate', return_value=self.result) as model:
            self.app.open_screenshot_file(); self.choose_region(); self.settle()
        capture.assert_not_called(); model.assert_called_once()
        self.assertIn('周五几点', self.app.context.get('1.0', 'end-1c'))

    def test_system_snip_pauses_live_capture_before_minimizing(self):
        self.app.capture_source.set(SCREENSHOT_SOURCE)
        self.app.watch.start('朋友', self.window)
        self.app.watch.next_check_at = 0
        def launch():
            self.assertFalse(self.app.watch.active)
            self.app.automation_tick()
        with patch('desktop.launch_snipping_tool', side_effect=launch), patch.object(self.root, 'iconify'), patch('desktop_automation.list_windows') as windows:
            self.app.system_screenshot()
        windows.assert_not_called()
        self.assertIn('粘贴截图', self.app.status.get())

    def test_stale_window_error_cannot_override_manual_image_request(self):
        token = self.app._discovery_token
        self.app.begin_manual_screenshot()
        self.app.set_busy(True)
        self.app.status.set('正在等待大模型看图')
        self.app.screenshot_events.put(('ocr', token, '朋友', self.window, False, '选区移出屏幕'))
        self.app.discovery_events.put((token, False, '微信窗口查找失败'))
        with patch('desktop_screenshots.messagebox.showwarning') as warning:
            self.app.poll_screenshots(); self.app.poll_discovery()
        warning.assert_not_called()
        self.assertTrue(self.app.busy)
        self.assertEqual(self.app.status.get(), '正在等待大模型看图')

    def test_cancelling_window_capture_or_discovery_still_releases_its_controls(self):
        for operation in ('bind_region', 'ocr', 'discover'):
            with self.subTest(operation=operation):
                token = self.app._discovery_token
                self.app.set_busy(True, operation=operation)
                self.app.stop_watch('已取消')
                if operation == 'discover':
                    self.app.discovery_events.put((token, False, '选区移出屏幕'))
                    self.app.poll_discovery()
                else:
                    kind = 'bind' if operation == 'bind_region' else 'ocr'
                    self.app.screenshot_events.put((kind, token, '朋友', self.window, False, '选区移出屏幕'))
                    self.app.poll_screenshots()
                self.assertFalse(self.app.busy)
                self.assertNotIn('disabled', self.app.generate_button.state())


if __name__ == '__main__':
    unittest.main()
