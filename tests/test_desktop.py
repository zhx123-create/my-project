import gc
import os
import threading
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
from desktop import CropDialog, DesktopApp
from server import ServiceError
from windows_capture import CaptureError, Window


@unittest.skipUnless(os.environ.get('DISPLAY') or os.name == 'nt', 'requires a desktop display')
class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.app = DesktopApp(self.root)
        self.root.update()
        self.app.key.set('')

    def tearDown(self):
        self.app.close()
        del self.app
        del self.root
        # Tcl objects must be finalized on the thread that owns the interpreter.
        gc.collect()

    def wait_for_job(self):
        deadline = time.monotonic() + 3
        while self.app.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertFalse(self.app.busy, 'UI did not consume worker result')

    def test_explicit_clipboard_import_and_clear(self):
        self.root.clipboard_clear(); self.root.clipboard_append('对方：周末见吗？')
        self.app.consent.set(True)
        self.app.import_text()
        self.assertEqual(self.app.context.get('1.0', 'end-1c'), '对方：周末见吗？')
        self.assertFalse(self.app.consent.get())
        self.app.clear()
        self.assertEqual(self.app.context.get('1.0', 'end-1c'), '')

    def test_consent_and_key_are_required_before_network(self):
        with patch('desktop.generate') as generate:
            self.app.request_generation()
            self.assertIn('勾选', self.app.status.get())
            self.app.consent.set(True)
            self.app.request_generation()
            self.assertIn('密钥', self.app.status.get())
            generate.assert_not_called()

    def test_generate_edit_copy_and_clear_real_widgets(self):
        self.root.geometry('870x680'); self.root.update()
        self.assertGreaterEqual(self.app.input_canvas.winfo_width(), 350, 'input pane too narrow for capture controls')
        for widget in (self.app.confirm_box, self.app.generate_button):
            ancestor = widget.master
            while ancestor is not None:
                bottom = widget.winfo_rooty() - ancestor.winfo_rooty() + widget.winfo_height()
                self.assertLessEqual(bottom, ancestor.winfo_height(), 'action clipped on smaller desktop')
                ancestor = getattr(ancestor, 'master', None)
        self.app.context.insert('1.0', '对方：周末见吗？')
        self.app.key.set('test-only-key'); self.app.consent.set(True)
        replies = [{'label': '自然', 'text': f'回复 {i}'} for i in range(3)]
        with patch('desktop.generate', return_value=replies) as generate:
            self.app.generate_button.invoke()
            self.assertTrue(self.app.busy)
            self.wait_for_job()
            self.assertIn('对方', generate.call_args.args[1][1]['content'][0]['text'])
        self.assertEqual(self.app.reply_texts[2].get('1.0', 'end-1c'), '回复 2')
        self.app.reply_texts[0].delete('1.0', 'end'); self.app.reply_texts[0].insert('1.0', '修改过的回复')
        self.app.copy_reply(0)
        self.assertEqual(self.root.clipboard_get(), '修改过的回复')
        self.app.clear()
        self.assertEqual(self.app.reply_texts[0].get('1.0', 'end-1c'), '')

    def test_worker_failure_restores_interactive_state(self):
        self.app.context.insert('1.0', 'hi'); self.app.key.set('test-only-key'); self.app.consent.set(True)
        with patch('desktop.generate', side_effect=ServiceError('AI 服务额度不足')):
            self.app.request_generation(); self.wait_for_job()
        self.assertIn('额度不足', self.app.status.get())
        self.assertEqual(str(self.app.context.cget('state')), 'normal')

    def test_crop_requires_selection_and_does_not_submit(self):
        image = Image.new('RGB', (400, 200), 'white')
        with patch('desktop.generate') as generate:
            dialog = CropDialog(self.root, image, self.app.attach_image)
            self.root.update()
            self.assertIn('disabled', dialog.use.state())
            dialog.begin(SimpleNamespace(x=20, y=20)); dialog.drag(SimpleNamespace(x=220, y=120))
            dialog.use.invoke()
            self.assertTrue(self.app.screenshot.startswith('data:image/jpeg;base64,'))
            self.assertFalse(self.app.consent.get())
            generate.assert_not_called()
        self.app.remove_image()
        self.assertIsNone(self.app.screenshot)

    def select_window(self):
        self.app.windows = [Window(123, 456, '微信')]
        self.app.window_box.configure(values=['微信'])
        self.app.window_box.current(0)

    def test_capture_requires_selection_with_visible_popup(self):
        with patch('desktop.messagebox.showwarning') as warning, patch('desktop.capture_window_isolated') as capture:
            self.app.capture_button.invoke()
            warning.assert_called_once()
            self.assertIn('查找微信', warning.call_args.args[1])
            capture.assert_not_called()
        self.assertFalse(self.app.busy)

    def test_blank_window_capture_shows_fallback_and_restores_controls(self):
        self.select_window()
        with patch('desktop.capture_window_isolated', side_effect=CaptureError('未能采集到窗口内容。请按 Win+Shift+S 截图。')), patch('desktop.messagebox.showwarning') as warning, patch('desktop.CropDialog') as crop, patch('desktop.generate') as generate:
            self.app.capture_button.invoke()
            self.assertIn('正在采集', self.app.capture_button.cget('text'))
            self.wait_for_job()
            self.assertIn('Win+Shift+S', warning.call_args.args[1])
            self.assertIn('未能采集', self.app.status.get())
            self.assertEqual(self.app.capture_button.cget('text'), '采集选中窗口')
            crop.assert_not_called(); generate.assert_not_called()
        self.assertIsNone(self.app.screenshot)

    def test_capture_does_not_block_ui_and_opens_crop_after_completion(self):
        self.select_window()
        gate, pulses = threading.Event(), []
        image = Image.new('RGB', (300, 200), 'white')
        def delayed_capture(window):
            gate.wait(2)
            return image
        with patch('desktop.capture_window_isolated', side_effect=delayed_capture), patch('desktop.CropDialog') as crop:
            self.app.capture_button.invoke()
            self.root.after(0, lambda: pulses.append('responsive'))
            self.root.update()
            self.assertEqual(pulses, ['responsive'])
            self.assertTrue(self.app.busy)
            gate.set(); self.wait_for_job()
            crop.assert_called_once()
            self.assertIs(crop.call_args.args[1], image)
        self.assertIsNone(self.app.screenshot, 'capture must wait for explicit crop acceptance')

    def test_preview_creation_failure_is_visible(self):
        with patch('desktop.CropDialog', side_effect=tk.TclError('preview failed')), patch('desktop.messagebox.showwarning') as warning:
            self.app.show_crop(Image.new('RGB', (300, 200)))
            self.assertIn('预览窗口未能打开', warning.call_args.args[1])

    def test_system_screenshot_gives_import_steps_and_does_not_auto_import(self):
        with patch('desktop.launch_snipping_tool') as launch, patch.object(self.root, 'iconify') as minimize, patch('desktop.ImageGrab.grabclipboard') as clipboard:
            self.app.screen_button.invoke()
            launch.assert_called_once(); minimize.assert_called_once()
            clipboard.assert_not_called()
        self.assertIn('导入剪贴板截图', self.app.capture_status.get())

    def test_failed_system_screenshot_restores_assistant(self):
        with patch('desktop.launch_snipping_tool', side_effect=CaptureError('请手动按 Win+Shift+S')), patch.object(self.root, 'iconify'), patch.object(self.root, 'deiconify') as restore, patch('desktop.messagebox.showwarning') as warning:
            self.app.screen_button.invoke()
            restore.assert_called_once()
            self.assertIn('Win+Shift+S', warning.call_args.args[1])
