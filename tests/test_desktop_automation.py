import gc
import os
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from conversation_memory import ConversationMemory
from desktop import DesktopApp
from desktop_settings import DesktopSettings
from meme_library import MemeLibrary
from server import Config, ServiceError
from windows_capture import CaptureError, Window


def snapshot(text):
    return {'text': text, 'count': len(text.splitlines()), 'source': 'uia', 'identity_verified': False}


def generated(text='我确认一下，稍后回复你。'):
    return {'understanding': '正在确认时间。', 'replies': [{'label': '自然', 'text': text}], 'meme_hint': '', 'meme_tags': []}


@unittest.skipUnless(os.environ.get('DISPLAY') or os.name == 'nt', 'requires a desktop display')
class AutomationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='jiehua-automation-')
        self.root = tk.Tk()
        self.app = DesktopApp(self.root, memory=ConversationMemory(), meme_library=MemeLibrary(Path(self.folder.name) / 'memes'), settings=DesktopSettings())
        self.app.select_profile('项目同事')
        self.app.key.set('test-only-key')
        self.window = Window(123, 456, '微信')
        self.app.windows = [self.window]
        self.app.window_box.configure(values=['微信']); self.app.window_box.current(0)
        self.first = snapshot('对方：周五交稿可以吗？')
        self.second = snapshot('对方：周五交稿可以吗？\n对方：下午两点前呢？')
        self.root.update()

    def tearDown(self):
        self.app.close()
        del self.app, self.root
        gc.collect()
        self.folder.cleanup()

    def settle(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.root.update()
            if not self.app.busy:
                return
            time.sleep(0.01)
        self.fail('worker did not finish')

    def next_check(self):
        self.app.watch.next_check_at = 0
        self.app.automation_tick()
        self.settle()

    def test_one_click_discovers_unique_window_and_generates_without_checkbox(self):
        self.app.windows = []; self.app.window_choice.set('')
        self.app.consent.set(False)
        with patch('desktop_automation.list_windows', return_value=[self.window]) as find, patch('desktop.read_conversation', return_value=self.first) as read, patch('desktop.generate', return_value=generated()) as model:
            self.app.analyze_button.invoke(); self.settle()
        find.assert_called_once(); read.assert_called_once(); model.assert_called_once()
        self.assertEqual(self.app.reply_texts[0].get('1.0', 'end-1c'), generated()['replies'][0]['text'])

    def test_explicit_editor_action_does_not_need_repeated_checkbox(self):
        self.app.context.insert('1.0', '对方：你好')
        self.app.consent.set(False)
        with patch('desktop.generate', return_value=generated()) as model:
            self.app.generate_button.invoke(); self.settle()
        model.assert_called_once()

    def test_first_watch_names_conversation_once_without_forcing_background_dialog(self):
        self.app.select_profile('临时会话（不记忆）')
        with patch('desktop_automation.simpledialog.askstring', return_value='新朋友') as name, patch.object(self.app, 'show_memory') as background, patch('desktop_automation.list_windows', return_value=[self.window]), patch('desktop_automation.read_conversation', return_value=self.first), patch('desktop.generate', return_value=generated()) as model:
            self.app.toggle_watch(); self.settle()
        name.assert_called_once(); background.assert_not_called(); model.assert_called_once()
        self.assertTrue(self.app.watch.active)
        self.assertEqual(self.app.watch.profile, '新朋友')

    def test_multiple_windows_wait_for_explicit_choice_then_continue(self):
        self.app.windows = []; self.app.window_choice.set('')
        other = Window(789, 999, '另一个微信窗口')
        with patch('desktop_automation.list_windows', return_value=[self.window, other]), patch('desktop.read_conversation', return_value=self.first) as read, patch('desktop.generate', return_value=generated()) as model:
            self.app.quick_analyze(); self.settle()
            read.assert_not_called(); model.assert_not_called()
            dialogs = [widget for widget in self.root.winfo_children() if isinstance(widget, tk.Toplevel)]
            self.assertEqual(len(dialogs), 1)
            import tkinter.ttk as ttk
            def choose(widget):
                for child in widget.winfo_children():
                    if isinstance(child, ttk.Button) and child.cget('text') == '使用这个窗口':
                        child.invoke(); return True
                    if choose(child):
                        return True
                return False
            self.assertTrue(choose(dialogs[0])); self.settle()
        read.assert_called_once_with(self.window); model.assert_called_once()

    def test_watch_deduplicates_reads_and_never_marks_drafts_sent(self):
        with patch('desktop_automation.list_windows', return_value=[self.window]), patch('desktop_automation.read_conversation', return_value=self.first) as read, patch('desktop.generate', return_value=generated()) as model:
            self.app.toggle_watch(); self.settle()
            self.next_check(); self.next_check()
        self.assertEqual(read.call_count, 3)
        model.assert_called_once()
        self.assertTrue(self.app.watch.active)
        self.assertTrue(all(entry['kind'] == 'context' for entry in self.app.memory.profile('项目同事')['history']))

    def test_new_content_waits_for_current_draft_then_continues_after_copy(self):
        with patch('desktop_automation.list_windows', return_value=[self.window]), patch('desktop_automation.read_conversation', side_effect=[self.first, self.second]), patch('desktop.generate', side_effect=[generated('原始草稿'), generated('新草稿')]) as model:
            self.app.toggle_watch(); self.settle()
            self.app.reply_texts[0].delete('1.0', 'end'); self.app.reply_texts[0].insert('1.0', '我正在修改的草稿')
            self.next_check()
            self.assertEqual(model.call_count, 1)
            self.assertEqual(self.app.reply_texts[0].get('1.0', 'end-1c'), '我正在修改的草稿')
            self.assertNotIn('下午两点', self.app.memory.history_text('项目同事'))
            self.app.copy_reply(0); self.app.automation_tick(); self.settle()
        self.assertEqual(model.call_count, 2)
        self.assertEqual(self.app.reply_texts[0].get('1.0', 'end-1c'), '新草稿')
        self.assertEqual(self.app.memory.history_text('项目同事').count('下午两点'), 1)

    def test_auto_copy_is_optional_and_later_edits_remain_protected(self):
        self.app.auto_copy.set(True)
        with patch('desktop_automation.list_windows', return_value=[self.window]), patch('desktop_automation.read_conversation', side_effect=[self.first, self.second]), patch('desktop.generate', return_value=generated('自动复制草稿')) as model:
            self.app.toggle_watch(); self.settle()
            self.assertEqual(self.root.clipboard_get(), '自动复制草稿')
            self.app.reply_texts[0].insert('end', '，我又改了一点')
            self.next_check()
        model.assert_called_once()
        self.assertIn('我又改了一点', self.app.reply_texts[0].get('1.0', 'end-1c'))
        self.assertTrue(all(entry['kind'] == 'context' for entry in self.app.memory.profile('项目同事')['history']))

    def test_pause_during_read_invalidates_queued_content(self):
        gate = threading.Event()
        def read(_window):
            gate.wait(2); return self.first
        with patch('desktop_automation.list_windows', return_value=[self.window]), patch('desktop_automation.read_conversation', side_effect=read), patch('desktop.generate') as model:
            self.app.toggle_watch()
            self.assertTrue(self.app.busy)
            self.assertNotIn('disabled', self.app.watch_button.state())
            self.app.watch_button.invoke(); gate.set(); self.settle()
        self.assertFalse(self.app.watch.active)
        model.assert_not_called()
        self.assertEqual(self.app.memory.history_text('项目同事'), '')

    def test_periodic_reads_leave_draft_editing_and_copy_available(self):
        gate = threading.Event()
        calls = []
        def read(_window):
            calls.append(1)
            if len(calls) > 1:
                gate.wait(2)
            return self.first
        with patch('desktop_automation.list_windows', return_value=[self.window]), patch('desktop_automation.read_conversation', side_effect=read), patch('desktop.generate', return_value=generated('正在处理的草稿')):
            self.app.toggle_watch(); self.settle()
            self.app.watch.next_check_at = 0; self.app.automation_tick()
            self.assertTrue(self.app.busy)
            self.assertEqual(str(self.app.reply_texts[0].cget('state')), 'normal')
            self.assertNotIn('disabled', self.app.reply_action_buttons[0].state())
            self.app.reply_texts[0].insert('end', '，已经编辑')
            self.app.reply_action_buttons[0].invoke()
            self.assertEqual(self.root.clipboard_get(), '正在处理的草稿，已经编辑')
            gate.set(); self.settle()

    def test_pause_during_ai_request_drops_inflight_result(self):
        gate = threading.Event()
        entered = threading.Event()
        def generate(*_args, **_kwargs):
            entered.set(); gate.wait(2); return generated('暂停后不应出现')
        with patch('desktop_automation.list_windows', return_value=[self.window]), patch('desktop_automation.read_conversation', return_value=self.first), patch('desktop.generate', side_effect=generate):
            self.app.toggle_watch()
            deadline = time.monotonic() + 2
            while not entered.is_set() and time.monotonic() < deadline:
                self.root.update(); time.sleep(0.01)
            self.assertTrue(entered.is_set())
            self.app.watch_button.invoke(); gate.set(); self.settle()
        self.assertFalse(self.app.watch.active)
        self.assertEqual(self.app.reply_texts[0].get('1.0', 'end-1c'), '')

    def test_discontinuous_content_is_not_merged_or_sent_to_ai(self):
        unrelated = snapshot('另一个人：发票已经寄出了。')
        with patch('desktop_automation.list_windows', return_value=[self.window]), patch('desktop_automation.read_conversation', side_effect=[self.first, unrelated]), patch('desktop.generate', return_value=generated()) as model:
            self.app.toggle_watch(); self.settle(); self.next_check()
        self.assertFalse(self.app.watch.active)
        model.assert_called_once()
        self.assertNotIn('发票', self.app.memory.history_text('项目同事'))

    def test_window_change_and_reader_failure_pause_without_retry_popups(self):
        for windows in ([], [Window(123, 456, '另一个聊天')]):
            with self.subTest(windows=windows), patch('desktop_automation.list_windows', return_value=windows), patch('desktop_automation.read_conversation') as read, patch('desktop.generate') as model, patch('desktop.messagebox.showwarning') as popup:
                self.app.toggle_watch(); self.settle()
                self.app.automation_tick()
                self.assertFalse(self.app.watch.active)
                read.assert_not_called(); model.assert_not_called(); popup.assert_not_called()
        with patch('desktop_automation.list_windows', return_value=[self.window]), patch('desktop_automation.read_conversation', side_effect=CaptureError('当前微信不支持文字读取')), patch('desktop.generate') as model:
            self.app.toggle_watch(); self.settle()
        self.assertFalse(self.app.watch.active); model.assert_not_called()

    def test_ai_failure_pauses_and_profile_switch_disarms_watch(self):
        with patch('desktop_automation.list_windows', return_value=[self.window]), patch('desktop_automation.read_conversation', return_value=self.first), patch('desktop.generate', side_effect=ServiceError('额度不足')) as model:
            self.app.toggle_watch(); self.settle()
            self.assertFalse(self.app.watch.active)
            self.app.automation_tick(); model.assert_called_once()
        self.app.watch.start('项目同事', self.window)
        self.app.select_profile('朋友')
        self.assertFalse(self.app.watch.active)

    def test_restart_restores_opted_in_profile_but_never_starts_watch(self):
        path = Path(self.folder.name)
        memory = ConversationMemory(path / 'memory.json')
        memory.profile('保存的会话'); memory.set_persistent(True)
        settings = DesktopSettings(path / 'settings.json')
        settings.update(last_profile='保存的会话', auto_copy=True, watch_interval=20)
        self.app.close()
        self.root = tk.Tk()
        self.app = DesktopApp(self.root, memory=ConversationMemory(memory.path), meme_library=MemeLibrary(path / 'memes'), settings=DesktopSettings(settings.path))
        self.assertEqual(self.app.active_profile, '保存的会话')
        self.assertTrue(self.app.auto_copy.get())
        self.assertFalse(self.app.watch.active)
        self.assertEqual(self.app.watch_interval.get(), 20)

    def test_environment_key_keeps_its_endpoint_instead_of_pairing_with_saved_service(self):
        settings = DesktopSettings()
        settings.update(base='https://saved.example/v1', model='saved-model')
        self.app.close()
        self.root = tk.Tk()
        config = Config('environment-test-key', 'https://environment.example/v1', 'environment-model')
        with patch('desktop.Config.from_env', return_value=config):
            self.app = DesktopApp(self.root, memory=ConversationMemory(), meme_library=MemeLibrary(Path(self.folder.name) / 'memes'), settings=settings)
        self.assertEqual(self.app.key.get(), config.key)
        self.assertEqual(self.app.base.get(), config.base)
        self.assertEqual(self.app.model.get(), config.model)

    def test_environment_endpoint_without_key_is_preserved_when_preferences_are_saved(self):
        self.app.close(); self.root = tk.Tk()
        folder = Path(self.folder.name)
        config = Config('', 'https://configured.example/v1', 'configured-model')
        with patch('desktop.Config.from_env', return_value=config), patch('desktop.default_memory_path', return_value=folder / 'conversations.json'):
            self.app = DesktopApp(self.root, memory=ConversationMemory(), meme_library=MemeLibrary(folder / 'memes'))
            self.app.select_profile('测试会话')
        saved = DesktopSettings(folder / 'settings.json').values()
        self.assertEqual(saved['base'], config.base)
        self.assertEqual(saved['model'], config.model)

    def test_ui_preferences_do_not_repair_saved_key_with_runtime_environment_endpoint(self):
        self.app.close(); self.root = tk.Tk()
        settings = DesktopSettings(Path(self.folder.name) / 'secure-settings.json')
        config = Config('runtime-test-key', 'https://runtime.example/v1', 'runtime-model')
        with patch('desktop_settings.supports_secure_storage', return_value=True), patch('desktop_settings._dpapi', side_effect=lambda data, protect: data[::-1]):
            settings.set_api('https://saved.example/v1', 'saved-model', 'saved-test-key', True)
            with patch('desktop.Config.from_env', return_value=config):
                self.app = DesktopApp(self.root, memory=ConversationMemory(), meme_library=MemeLibrary(Path(self.folder.name) / 'memes'), settings=settings)
            self.app.select_profile('测试会话')
            self.assertEqual(settings.values()['base'], 'https://saved.example/v1')
            self.assertEqual(settings.api_key(), 'saved-test-key')


if __name__ == '__main__':
    unittest.main()
