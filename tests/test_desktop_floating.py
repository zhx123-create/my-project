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
from meme_library import MemeLibrary
from windows_capture import Window


def generated():
    return {'understanding': '日常问候。', 'replies': [{'label': '自然', 'text': '下午好呀。'}], 'meme_hint': '', 'meme_tags': []}


@unittest.skipUnless(os.environ.get('DISPLAY') or os.name == 'nt', 'requires a desktop display')
class FloatingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='jiehua-floating-ui-')
        self.root = tk.Tk()
        self.app = DesktopApp(self.root, memory=ConversationMemory(), settings=DesktopSettings(), meme_library=MemeLibrary(Path(self.folder.name) / 'memes'))
        self.app.select_profile('小林'); self.app.key.set('test-only-key')
        self.window = Window(123, 456, '小林')
        self.card = Mock()
        self.card_factory = patch('desktop_floating.FloatingCard', return_value=self.card)
        self.card_factory.start()
        self.app.presence_events.put((self.window, (100, 100, 700, 900), (0, 0, 1920, 1040)))
        self.app.poll_floating()
        self.root.update()

    def tearDown(self):
        self.app.close()
        self.card_factory.stop()
        del self.app, self.root
        gc.collect()
        self.folder.cleanup()

    def start(self):
        with patch('desktop_quickfill.QuickFillJob.start') as start, patch.object(self.root, 'iconify') as shrink:
            self.app.floating_action()
        start.assert_called_once(); shrink.assert_not_called()
        return self.app.quickfill_job

    def test_presence_shows_mouse_entry_without_reading_chats_or_calling_model(self):
        with patch('desktop_quickfill.call_native') as native, patch('desktop_quickfill.generate') as model:
            self.app.poll_floating()
        native.assert_not_called(); model.assert_not_called()
        self.assertEqual(self.card.present.call_args.args[3]['action'], '生成')

    def test_click_starts_immediately_and_pins_the_window_without_countdown(self):
        job = self.start()
        self.assertIsNone(self.app.quickfill_countdown)
        self.assertEqual(job.expected_window, {'handle': 123, 'pid': 456, 'title': '小林'})
        self.assertEqual(self.app.floating_progress.phase, 'reading')
        self.assertTrue(self.app.quickfill_from_float)

    def test_phase_and_total_seconds_update_and_freeze_after_fill(self):
        with patch('desktop_floating.time.monotonic', return_value=100):
            job = self.start()
        self.app.quickfill_events.put((job, 'generating', {}))
        with patch('desktop_floating.time.monotonic', return_value=104):
            self.app.poll_quickfill()
        with patch('desktop_floating.time.monotonic', return_value=112):
            self.app.poll_floating()
        view = self.card.present.call_args.args[3]
        self.assertEqual(view['title'], '正在生成回复'); self.assertEqual(view['seconds'], 12)
        self.app.quickfill_events.put((job, 'generated', {'result': generated()}))
        self.app.quickfill_events.put((job, 'filled', {'receipt': {'confirmed': True}}))
        self.app.quickfill_events.put((job, 'done', {}))
        with patch('desktop_floating.time.monotonic', return_value=116):
            self.app.poll_quickfill()
        with patch('desktop_floating.time.monotonic', return_value=999):
            self.app.poll_floating()
        view = self.card.present.call_args.args[3]
        self.assertEqual(view['title'], '回复已填入'); self.assertEqual(view['seconds'], 16)
        self.assertFalse(self.app.busy)
        self.assertFalse(any(entry['kind'] == 'sent' for entry in self.app.memory.profile('小林')['history']))

    def test_switching_window_cancels_old_job_before_showing_new_entry(self):
        job = self.start()
        other = Window(789, 999, '另一个聊天')
        self.app.presence_events.put((other, (100, 100, 700, 900), (0, 0, 1920, 1040)))
        with patch.object(self.root, 'deiconify') as show:
            self.app.poll_floating()
        show.assert_not_called()
        self.assertTrue(job.cancel.is_set())
        self.assertIsNone(self.app.quickfill_job)
        self.assertEqual(self.app.floating_target, other)
        self.assertEqual(self.card.present.call_args.args[3]['action'], '生成')

    def test_error_stays_in_small_card_and_main_window_opens_only_on_details(self):
        job = self.start()
        self.app.quickfill_events.put((job, 'generated', {'result': generated()}))
        self.app.quickfill_events.put((job, 'error', {'message': '焦点已变化，未填入。'}))
        self.app.quickfill_events.put((job, 'done', {}))
        with patch.object(self.root, 'deiconify') as show, patch.object(self.root, 'lift') as lift:
            self.app.poll_quickfill()
            show.assert_not_called(); lift.assert_not_called()
            self.app.floating_details()
            show.assert_called_once(); lift.assert_called_once()
        self.assertEqual(self.app.floating_progress.phase, 'error')
        self.assertIn('焦点已变化', self.app.understanding.get('1.0', 'end-1c'))
        self.assertEqual(self.app.reply_texts[0].get('1.0', 'end-1c'), '下午好呀。')

    def test_dismiss_cancels_and_saves_the_floating_preference(self):
        job = self.start()
        self.app.dismiss_floating()
        self.assertTrue(job.cancel.is_set())
        self.assertFalse(self.app.floating_enabled.get())
        self.assertFalse(self.app.settings.values()['floating_enabled'])
        self.assertFalse(self.app.presence_watcher.enabled.is_set())
        self.card.hide.assert_called()

    def test_other_generation_shows_its_elapsed_time_and_opens_existing_task(self):
        self.app.set_busy(True, operation='generate')
        self.app.job_started = 100
        view = self.app.floating_view(112)
        self.assertEqual(view['title'], '正在生成草稿')
        self.assertEqual(view['seconds'], 12)
        self.assertEqual(view['action'], '查看')
        with patch.object(self.root, 'deiconify') as show, patch('desktop_quickfill.QuickFillJob.start') as start:
            self.app.floating_action()
        show.assert_called_once(); start.assert_not_called()
        self.app.set_busy(False)


if __name__ == '__main__':
    unittest.main()
