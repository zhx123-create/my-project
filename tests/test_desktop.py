import gc
import os
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
from desktop import CropDialog, DesktopApp
from server import ServiceError


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
        for widget in (self.app.confirm_box, self.app.generate_button):
            bottom = widget.winfo_rooty() - self.root.winfo_rooty() + widget.winfo_height()
            self.assertLessEqual(bottom, self.root.winfo_height(), 'action clipped on smaller desktop')
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
