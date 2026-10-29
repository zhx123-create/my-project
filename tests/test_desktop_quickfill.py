import gc
import os
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from conversation_memory import ConversationMemory
from desktop import DesktopApp
from desktop_settings import DesktopSettings
from meme_library import MemeLibrary
from quickfill_native import QuickFillError, text_digest


def snapshot():
    context = '对方：下午好。'
    return {'window': {'handle': 123, 'pid': 456, 'title': '小林'}, 'focus': {'handle': 123, 'input_time': 789},
            'context': context, 'digest': text_digest(context), 'source': 'windows_ocr'}


def result():
    return {'understanding': '日常问候。', 'replies': [{'label': '自然', 'text': '下午好呀。'}], 'meme_hint': '', 'meme_tags': []}


@unittest.skipUnless(os.environ.get('DISPLAY') or os.name == 'nt', 'requires a desktop display')
class QuickFillDesktopTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='jiehua-quickfill-ui-')
        self.root = tk.Tk()
        self.app = DesktopApp(self.root, memory=ConversationMemory(), settings=DesktopSettings(), meme_library=MemeLibrary(Path(self.folder.name) / 'memes'))
        self.app.select_profile('小林')
        self.app.key.set('test-only-key')
        self.root.update()

    def tearDown(self):
        self.app.close()
        del self.app, self.root
        gc.collect()
        self.folder.cleanup()

    def settle(self):
        deadline = time.monotonic() + 3
        while self.app.quickfill_job is not None and time.monotonic() < deadline:
            self.root.update(); time.sleep(.01)
        self.assertIsNone(self.app.quickfill_job)
        self.assertFalse(self.app.busy)

    def test_hotkey_runs_read_generate_fill_without_crop_copy_or_sent_history(self):
        self.app.auto_copy.set(True)
        self.root.clipboard_clear(); self.root.clipboard_append('不应被覆盖的剪贴板')
        with patch('desktop_quickfill.call_native', side_effect=[snapshot(), {'filled': True, 'confirmed': True, 'method': 'uia'}]) as native, patch('desktop_quickfill.generate', return_value=result()) as model, patch.object(self.app, 'show_crop') as crop, patch.object(self.app, 'show_region_crop') as region:
            self.app.hotkey_events.put(('hotkey', None))
            self.app.poll_quickfill(); self.settle()
        self.assertEqual(native.call_count, 2); model.assert_called_once()
        crop.assert_not_called(); region.assert_not_called()
        self.assertEqual(self.app.reply_texts[0].get('1.0', 'end-1c'), '下午好呀。')
        self.assertIn('已填入微信', self.app.reply_labels[0].cget('text'))
        self.assertEqual(self.root.clipboard_get(), '不应被覆盖的剪贴板')
        self.assertTrue(all(item['kind'] == 'context' for item in self.app.memory.profile('小林')['history']))

    def test_button_gives_three_seconds_to_pick_input_and_does_no_early_capture(self):
        callbacks = []
        original = self.root.after
        def record(delay, callback=None, *args):
            if delay == 3000:
                callbacks.append(callback)
            return original(delay, callback, *args)
        with patch.object(self.root, 'after', side_effect=record), patch('desktop_quickfill.QuickFillJob.start') as started:
            self.app.quickfill_button.invoke()
            started.assert_not_called()
            self.assertEqual(len(callbacks), 1)
            self.assertTrue(self.app.busy)
            self.assertNotIn('disabled', self.app.quickfill_button.state())
            callbacks[0]()
            started.assert_called_once()
        self.app.cancel_quickfill()
        self.assertFalse(self.app.busy)

    def test_fill_failure_preserves_draft_and_explains_full_error(self):
        with patch('desktop_quickfill.call_native', side_effect=[snapshot(), QuickFillError('焦点已变化，未填入。')]), patch('desktop_quickfill.generate', return_value=result()):
            self.app.start_quickfill(from_hotkey=True); self.settle()
        self.assertEqual(self.app.reply_texts[0].get('1.0', 'end-1c'), '下午好呀。')
        self.assertIn('焦点已变化', self.app.understanding.get('1.0', 'end-1c'))
        self.assertEqual(self.app.reply_labels[0].cget('text'), '填入未完成 · 草稿已保留')
        self.assertFalse(any(item['kind'] == 'sent' for item in self.app.memory.profile('小林')['history']))

    def test_cancelled_worker_cannot_replace_a_newer_reply_or_unlock_controls(self):
        with patch('desktop_quickfill.QuickFillJob.start'):
            self.app.start_quickfill(from_hotkey=True)
        old = self.app.quickfill_job
        self.app.cancel_quickfill()
        self.app.reply_texts[0].insert('1.0', '用户的新草稿')
        self.app.set_busy(True, operation='generate')
        self.app.quickfill_events.put((old, 'generated', {'result': result()}))
        self.app.quickfill_events.put((old, 'done', {}))
        self.app.poll_quickfill()
        self.assertEqual(self.app.reply_texts[0].get('1.0', 'end-1c'), '用户的新草稿')
        self.assertTrue(self.app.busy)
        self.app.set_busy(False)

    def test_second_hotkey_cancels_without_starting_another_job(self):
        with patch('desktop_quickfill.QuickFillJob.start') as start:
            self.app.start_quickfill(from_hotkey=True)
            job = self.app.quickfill_job
            self.app.hotkey_events.put(('hotkey', None))
            self.app.poll_quickfill()
        start.assert_called_once()
        self.assertTrue(job.cancel.is_set())
        self.assertIsNone(self.app.quickfill_job)
        self.assertFalse(self.app.busy)


if __name__ == '__main__':
    unittest.main()
