import gc
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
from desktop import CropDialog, DesktopApp, TEMP_CONVERSATION
from conversation_memory import ConversationMemory
from meme_library import MemeLibrary
from desktop_settings import DesktopSettings
from server import DEFAULT_STYLE, ServiceError
from windows_capture import CaptureError, Window


@unittest.skipUnless(os.environ.get('DISPLAY') or os.name == 'nt', 'requires a desktop display')
class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='jiehua-desktop-test-')
        self.library = MemeLibrary(Path(self.temp.name) / 'memes')
        self.root = tk.Tk()
        self.app = DesktopApp(self.root, memory=ConversationMemory(), meme_library=self.library, settings=DesktopSettings())
        self.root.update()
        self.app.key.set('')

    def tearDown(self):
        self.app.close()
        del self.app
        del self.root
        # Tcl objects must be finalized on the thread that owns the interpreter.
        gc.collect()
        self.temp.cleanup()

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
        self.app.reply_mode.set('生成 3 条候选')
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
        self.assertIn('粘贴截图', self.app.capture_status.get())

    def test_failed_system_screenshot_restores_assistant(self):
        with patch('desktop.launch_snipping_tool', side_effect=CaptureError('请手动按 Win+Shift+S')), patch.object(self.root, 'iconify'), patch.object(self.root, 'deiconify') as restore, patch('desktop.messagebox.showwarning') as warning:
            self.app.screen_button.invoke()
            restore.assert_called_once()
            self.assertIn('Win+Shift+S', warning.call_args.args[1])

    def test_profile_switch_clears_unsubmitted_inputs_and_keeps_separate_memories(self):
        self.app.memory.update_profile('项目同事', background='正在确认项目交付', relationship='同事', goal='确认周五交稿')
        self.app.memory.add_context('项目同事', '对方：初稿什么时候给？')
        self.app.memory.update_profile('朋友', background='正在约周末见面', relationship='朋友', goal='确认见面时间')
        self.app.memory.add_context('朋友', '对方：周六吃饭吗？')
        self.app.select_profile('项目同事')
        self.app.context.insert('1.0', '这段尚未提交的输入不应带到另一会话')
        self.app.reply_texts[0].insert('1.0', '这是一条未发送草稿')
        self.app.attach_image(Image.new('RGB', (80, 80), 'white'))
        self.app.consent.set(True)

        self.app.select_profile('朋友')

        self.assertEqual(self.app.active_profile, '朋友')
        self.assertEqual(self.app.context.get('1.0', 'end-1c'), '')
        self.assertTrue(all(text.get('1.0', 'end-1c') == '' for text in self.app.reply_texts))
        self.assertIsNone(self.app.screenshot)
        self.assertFalse(self.app.consent.get())
        self.assertEqual(self.app.relationship.get(), '朋友')
        self.assertEqual(self.app.intent.get('1.0', 'end-1c'), '确认见面时间')
        self.assertIn('周六吃饭', self.app.memory.history_text('朋友'))
        self.assertNotIn('初稿', self.app.memory.history_text('朋友'))
        self.assertEqual(self.app.memory.profile('项目同事')['background'], '正在确认项目交付')
        self.assertNotIn('尚未提交', self.app.memory.history_text('项目同事'))
        self.assertNotIn('未发送草稿', self.app.memory.history_text('项目同事'))
        self.app.select_profile('项目同事')
        self.assertEqual(self.app.relationship.get(), '同事')
        self.assertIn('初稿', self.app.memory.history_text('项目同事'))

    def test_named_profile_generation_merges_history_and_shows_one_reply_with_analysis(self):
        self.app.memory.update_profile('项目同事', background='每周项目交付同步', relationship='同事', goal='确认周五下午交付')
        self.app.memory.add_context('项目同事', '对方：初稿什么时候给？\n我：周五前。')
        self.app.select_profile('项目同事')
        self.app.context.insert('1.0', '对方：周五下午可以吗？')
        self.app.key.set('test-only-key'); self.app.consent.set(True)
        result = {'understanding': '双方正在确认初稿交付时间，下午的具体时刻尚未确定。', 'replies': [{'label': '自然', 'text': '我确认一下安排，稍后给你具体时间。'}]}

        with patch('desktop.generate', return_value=result) as generate:
            self.app.generate_button.invoke(); self.wait_for_job()

        generate.assert_called_once()
        self.assertEqual(generate.call_args.kwargs, {'reply_count': 1, 'include_analysis': True})
        messages = generate.call_args.args[1]
        parts = messages[1]['content']
        self.assertEqual(len(parts), 1, 'text path must not include screenshots')
        data = json.loads(parts[0]['text'])
        self.assertEqual(data['聊天上下文'], '')
        self.assertEqual(data['会话标记'], '项目同事')
        self.assertIn('初稿什么时候给', data['已记录的历史'])
        self.assertEqual(data['已记录的历史'].count('周五下午可以吗'), 1)
        self.assertEqual(data['用户填写的背景'], '每周项目交付同步')
        self.assertEqual(data['我的回复意图'], '确认周五下午交付')
        self.assertEqual(self.app.understanding.get('1.0', 'end-1c'), result['understanding'])
        self.assertEqual(self.app.reply_texts[0].get('1.0', 'end-1c'), result['replies'][0]['text'])
        self.root.update()
        self.assertTrue(self.app.reply_cards[0].winfo_ismapped())
        self.assertGreater(self.app.reply_cards[0].winfo_height(), 200, 'single-reply editor must remain usable')
        self.assertFalse(self.app.reply_cards[1].winfo_ismapped())
        self.assertFalse(self.app.reply_cards[2].winfo_ismapped())
        self.assertTrue(all(item['kind'] == 'context' for item in self.app.memory.profile('项目同事')['history']))
        self.assertNotIn('稍后给你具体时间', self.app.memory.history_text('项目同事'))

    def test_copy_is_not_sent_memory_until_user_explicitly_confirms(self):
        self.app.select_profile('朋友')
        self.app.reply_texts[0].insert('1.0', '周六中午见吧。')
        self.app.copy_reply(0)
        self.assertEqual(self.root.clipboard_get(), '周六中午见吧。')
        self.assertEqual(self.app.memory.profile('朋友')['history'], [])
        with patch('desktop.messagebox.askyesno', return_value=False):
            self.app.confirm_sent(0)
        self.assertEqual(self.app.memory.profile('朋友')['history'], [])
        with patch('desktop.messagebox.askyesno', return_value=True) as confirm:
            self.app.confirm_sent(0)
        confirm.assert_called_once()
        self.assertEqual(self.app.memory.profile('朋友')['history'], [{'kind': 'sent', 'text': '周六中午见吧。'}])

    def test_text_reader_chains_one_generation_without_screenshot_and_normalizes_memory(self):
        self.app.memory.update_profile('项目同事', background='项目交付同步', relationship='同事', goal='确认时间')
        self.app.select_profile('项目同事'); self.select_window()
        self.app.key.set('test-only-key')
        self.app.attach_image(Image.new('RGB', (80, 80), 'white'))
        self.app.consent.set(True)
        snapshot = {'text': '对方：周五下午可以吗？\r\n我：还需要确认。', 'count': 2, 'source': 'uia', 'identity_verified': False}
        result = {'understanding': '正在确认交付时间，具体时刻未知。', 'replies': [{'label': '自然', 'text': '我确认后回复你。'}]}
        with patch('desktop.read_conversation', return_value=snapshot) as reader, patch('desktop.generate', return_value=result) as generate, patch('desktop.capture_window_isolated') as capture, patch('desktop.ImageGrab.grabclipboard') as clipboard:
            self.app.analyze_button.invoke(); self.wait_for_job()

        reader.assert_called_once_with(self.app.windows[0])
        generate.assert_called_once()
        capture.assert_not_called(); clipboard.assert_not_called()
        self.assertIsNone(self.app.screenshot)
        parts = generate.call_args.args[1][1]['content']
        self.assertEqual(len(parts), 1)
        data = json.loads(parts[0]['text'])
        self.assertIn('周五下午可以吗', data['已记录的历史'])
        self.assertNotIn('\r', data['已记录的历史'])
        self.assertNotIn('\r', self.app.memory.history_text('项目同事'))
        self.assertTrue(all(item['kind'] == 'context' for item in self.app.memory.profile('项目同事')['history']))
        self.assertEqual(self.app.understanding.get('1.0', 'end-1c'), result['understanding'])

    def test_unsupported_text_reader_does_not_generate_or_write_memory(self):
        self.app.memory.update_profile('项目同事', background='项目交付同步', relationship='同事', goal='确认时间')
        self.app.memory.add_context('项目同事', '对方：初稿什么时候给？')
        self.app.select_profile('项目同事'); self.select_window()
        self.app.context.insert('1.0', '读取失败时不应提交的旧输入')
        before = self.app.memory.profile('项目同事')
        self.app.key.set('test-only-key'); self.app.consent.set(True)
        with patch('desktop.read_conversation', side_effect=CaptureError('当前微信没有公开可读取的消息列表文字。')) as reader, patch('desktop.generate') as generate, patch('desktop.messagebox.showwarning') as warning:
            self.app.analyze_button.invoke(); self.wait_for_job()
        reader.assert_called_once()
        generate.assert_not_called()
        warning.assert_called_once()
        self.assertIn('没有公开', warning.call_args.args[1])
        self.assertEqual(self.app.memory.profile('项目同事'), before)
        self.assertEqual(self.app.context.get('1.0', 'end-1c'), '读取失败时不应提交的旧输入')
        self.assertIn('没有提交给 AI', self.app.capture_status.get())

    def test_reader_provenance_header_does_not_duplicate_rolling_history(self):
        self.app.select_profile('朋友')
        self.app.key.set('test-only-key'); self.app.consent.set(True)
        header = '【微信可访问消息文字；发言者及会话身份未自动核实，请核对】\n'
        result = {'understanding': '正在讨论约见时间。', 'replies': [{'label': '自然', 'text': '我确认后回复你。'}]}
        with patch('desktop.generate', return_value=result):
            for snapshot in ('对方：周末见吗？\n我：周六可以。', '我：周六可以。\n对方：中午怎么样？'):
                text = header + snapshot
                self.app.context.delete('1.0', 'end'); self.app.context.insert('1.0', text)
                self.app.last_reader_text = text
                self.app.request_generation(); self.wait_for_job()
        history = self.app.memory.history_text('朋友')
        self.assertEqual(history.count('周六可以'), 1)
        self.assertIn('中午怎么样', history)
        self.assertNotIn('可访问消息文字', history)

    def test_style_combinations_remain_separate_across_named_and_temporary_chats(self):
        warm = {**DEFAULT_STYLE, 'expression': '偏男性化', 'personality': '温柔体贴', 'humor': 3, 'meme_level': 0, 'abstraction': 2}
        reserved = {**DEFAULT_STYLE, 'expression': '偏女性化', 'personality': '理性克制', 'humor': 0, 'meme_level': 3, 'abstraction': 0}
        self.app.select_profile('朋友')
        self.app.set_reply_style(warm)
        self.app.select_profile('同事')
        self.app.set_reply_style(reserved)
        self.assertEqual(self.app.memory.profile('朋友')['style'], warm)
        self.assertEqual(self.app.memory.profile('同事')['style'], reserved)
        self.app.select_profile('朋友')
        self.assertEqual(self.app.style, warm)
        self.assertIn('偏男性化', self.app.style_summary.get())
        self.assertIn('温柔体贴', self.app.style_summary.get())
        self.app.select_profile('同事')
        self.assertEqual(self.app.style, reserved)
        self.app.select_profile(TEMP_CONVERSATION)
        self.assertEqual(self.app.style, DEFAULT_STYLE)

    def test_invalid_style_does_not_change_current_or_saved_preferences(self):
        self.app.select_profile('朋友')
        self.app.set_reply_style({**DEFAULT_STYLE, 'personality': '活泼外向', 'humor': 2})
        before = dict(self.app.style)
        summary = self.app.style_summary.get()
        with self.assertRaises(ValueError):
            self.app.set_reply_style({'humor': 4})
        self.assertEqual(self.app.style, before)
        self.assertEqual(self.app.memory.profile('朋友')['style'], before)
        self.assertEqual(self.app.style_summary.get(), summary)

    def test_style_preset_dialog_preserves_independent_expression_choice(self):
        self.app.select_profile('朋友')
        initial = {**DEFAULT_STYLE, 'expression': '偏女性化', 'personality': '温柔体贴'}
        self.app.set_reply_style(initial)
        self.app.show_reply_style(); self.root.update()
        def find_button(widget, text):
            for child in widget.winfo_children():
                if child.winfo_class() == 'TButton' and child.cget('text') == text:
                    return child
                found = find_button(child, text)
                if found is not None:
                    return found
            return None
        preset = find_button(self.app.style_dialog, '抽象搞怪')
        self.assertIsNotNone(preset)
        preset.invoke()
        self.assertEqual(self.app.style, initial, 'dialog edits are not applied until confirmation')
        apply = find_button(self.app.style_dialog, '应用风格')
        self.assertIsNotNone(apply)
        apply.invoke()
        expected = {'expression': '偏女性化', 'personality': '俏皮毒舌', 'humor': 3, 'meme_level': 3, 'abstraction': 3}
        self.assertEqual(self.app.style, expected)
        self.assertEqual(self.app.memory.profile('朋友')['style'], expected)

    def test_style_dialog_rewrites_current_context_without_recapturing_wechat(self):
        self.app.select_profile('朋友'); self.app.key.set('test-only-key')
        self.app.context.insert('1.0', '未知：周五下班一起吃火锅吗？')
        self.app.reply_texts[0].insert('1.0', '原来的草稿')
        self.app.show_reply_style(); self.root.update()
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        widgets = list(descendants(self.app.style_dialog))
        button = lambda text: next(widget for widget in widgets if widget.winfo_class() == 'TButton' and widget.cget('text') == text)
        button('抽象搞怪').invoke()
        tone = next(widget for widget in widgets if widget.winfo_class() == 'TCombobox' and '简洁' in widget.cget('values'))
        tone.set('简洁')
        result = {'understanding': '正在约饭。', 'replies': [{'label': '搞怪', 'text': '收到，火锅雷达已启动。'}]}
        with patch('desktop.generate', return_value=result) as model, patch('desktop_screenshots.capture_visible_isolated') as capture, patch('desktop.read_conversation') as read:
            button('应用并重新生成').invoke()
            self.assertIn('原来的草稿', self.app.reply_texts[0].get('1.0', 'end-1c'), 'keep the current draft while requesting its replacement')
            self.wait_for_job()
        capture.assert_not_called(); read.assert_not_called(); model.assert_called_once()
        data = json.loads(model.call_args.args[1][1]['content'][0]['text'])
        self.assertEqual(data['风格维度']['personality'], '俏皮毒舌')
        self.assertEqual(data['风格维度']['abstraction'], 3)
        self.assertEqual(data['语气'], '简短直接')
        self.assertIn('一起吃火锅', data['已记录的历史'])
        self.assertNotIn('原来的草稿', data['已记录的历史'])
        self.assertEqual(self.app.reply_texts[0].get('1.0', 'end-1c'), result['replies'][0]['text'])

    def test_failed_style_rewrite_keeps_previous_draft(self):
        self.app.key.set('test-only-key')
        self.app.context.insert('1.0', '未知：周五下班一起吃火锅吗？')
        self.app.reply_texts[0].insert('1.0', '请保留这条已编辑草稿')
        with patch('desktop.generate', side_effect=ServiceError('临时请求失败')):
            self.app.regenerate_reply(); self.wait_for_job()
        self.assertEqual(self.app.reply_texts[0].get('1.0', 'end-1c'), '请保留这条已编辑草稿')
        self.assertIn('临时请求失败', self.app.status.get())

    def test_style_remains_available_during_watch_read_and_late_result_is_ignored(self):
        window = Window(123, 456, '微信')
        self.app.watch.start('朋友', window)
        token = self.app.watch.generation
        self.app.set_busy(True, operation='watch_read')
        self.assertTrue(all('disabled' not in button.state() for button in self.app.style_buttons))
        self.app.style_buttons[-1].invoke()
        self.assertIsNotNone(self.app.style_dialog)
        self.assertFalse(self.app.watch.active)
        self.assertFalse(self.app.busy)
        self.app.set_busy(True, operation='generate')
        self.app.reader_events.put((False, {'__watch_token': token, 'error': '旧窗口读取失败'}))
        self.app.poll()
        self.assertTrue(self.app.busy, 'an abandoned window read must not unlock a newer rewrite request')
        self.assertNotIn('旧窗口读取失败', self.app.status.get())

    def import_test_meme(self):
        path = Path(self.temp.name) / '抽象无语.png'
        Image.new('RGB', (60, 60), (60, 120, 180)).save(path)
        return self.library.import_files([path], tags='抽象,无语')[0]

    def test_generation_passes_full_style_and_uses_model_theme_for_local_candidates(self):
        self.assertFalse(self.library.directory.exists(), 'empty library should not create local files')
        item = self.import_test_meme()
        self.app.select_profile('朋友')
        choice = {**DEFAULT_STYLE, 'expression': '中性', 'personality': '俏皮毒舌', 'humor': 2, 'meme_level': 3, 'abstraction': 3}
        self.app.set_reply_style(choice)
        self.app.tone.set('自然')
        self.app.context.insert('1.0', '对方：今天又加班到很晚。')
        self.app.key.set('test-only-key'); self.app.consent.set(True)
        result = {'understanding': '对方在抱怨加班，适合先回应辛苦。', 'replies': [{'label': '轻松', 'text': '辛苦了，先歇一会儿。'}], 'meme_hint': '抽象无语的加班反应', 'meme_tags': ['抽象', '无语']}
        with patch('desktop.generate', return_value=result) as generate:
            self.app.request_generation(); self.wait_for_job()
        generate.assert_called_once()
        prompt = json.loads(generate.call_args.args[1][1]['content'][0]['text'])
        self.assertEqual(prompt['风格维度'], choice)
        self.assertEqual(self.app.meme_hint, result['meme_hint'])
        self.assertEqual(self.app.meme_tags, result['meme_tags'])
        self.assertEqual([candidate['id'] for candidate in self.app.meme_candidates], [item['id']])
        self.assertIn('1 张候选', self.app.meme_note.get())
        self.assertNotIn('加班反应', self.app.memory.history_text('朋友'), 'model image hint is not sent-message history')

    def test_empty_meme_theme_does_not_offer_recent_images_in_serious_context(self):
        self.import_test_meme()
        self.app.update_meme_suggestions({'meme_hint': '抽象无语', 'meme_tags': ['无语']})
        self.assertTrue(self.app.meme_candidates)
        with patch.object(self.library, 'recommend') as recommend:
            self.app.update_meme_suggestions({'meme_hint': '', 'meme_tags': []})
        recommend.assert_not_called()
        self.assertEqual(self.app.meme_hint, '')
        self.assertEqual(self.app.meme_tags, [])
        self.assertEqual(self.app.meme_candidates, [])
        self.assertIn('没有建议配图', self.app.meme_note.get())

    def test_http_settings_need_opt_in_and_probe_can_correct_model_case(self):
        self.app.base.set('http://192.0.2.10/v1\u00a0')
        self.app.model.set('GLM-5.3-Flash')
        self.app.key.set('test-only-key')
        self.app.show_settings()
        self.root.update()
        with patch('desktop.test_connection', return_value=['glm-5.3-flash']) as probe, patch('desktop.generate') as model:
            self.app.api_test_button.invoke()
            probe.assert_not_called()
            self.assertIn('允许此 HTTP 地址', self.app.api_probe_note.get())
            self.app.api_http_checkbox.invoke()
            self.app.api_test_button.invoke()
            deadline = time.monotonic() + 2
            while 'disabled' in self.app.api_test_button.state() and time.monotonic() < deadline:
                self.root.update(); time.sleep(0.01)
            probe.assert_called_once()
            self.assertTrue(probe.call_args.args[0].allow_http)
            self.assertEqual(probe.call_args.args[0].base, 'http://192.0.2.10/v1')
            model.assert_not_called()
        self.assertEqual(self.app.model.get(), 'glm-5.3-flash')
        self.app.base.set('http://other.example/v1')
        self.assertFalse(self.app.allow_http.get(), 'consent must not follow an address change')
        self.app.settings_dialog.destroy()

    def test_http_config_reaches_generation_only_after_opt_in(self):
        self.app.base.set('http://192.0.2.10/v1')
        self.app.model.set('GLM-5.3-Flash')
        self.app.allow_http.set(True)
        self.app.key.set('test-only-key')
        self.app.context.insert('1.0', '对方：周末有空吗？')
        with patch('desktop.generate', return_value=[{'label': '自然', 'text': '我确认一下安排。'}]) as model:
            self.app.generate_from_editor(); self.wait_for_job()
        config = model.call_args.args[0]
        self.assertEqual(config.base, 'http://192.0.2.10/v1')
        self.assertEqual(config.model, 'GLM-5.3-Flash')
        self.assertTrue(config.allow_http)

    def test_probe_can_finish_after_settings_closed_without_touching_tk(self):
        gate, finished = threading.Event(), threading.Event()
        self.app.key.set('test-only-key')
        self.app.show_settings()
        def delayed(_config):
            gate.wait(2); finished.set(); return ['model']
        with patch('desktop.test_connection', side_effect=delayed):
            self.app.api_test_button.invoke()
            self.app.settings_dialog.destroy()
            gate.set()
            self.assertTrue(finished.wait(2))
            self.root.update()

    def test_image_probe_is_explicit_and_does_not_submit_current_chat(self):
        self.app.key.set('test-only-key')
        self.app.context.insert('1.0', '这是一段不应进入接口测试的私聊。')
        self.app.show_settings()
        with patch('desktop.check_vision', return_value=True) as vision, patch('desktop.test_connection') as models, patch('desktop.generate') as generate:
            vision.assert_not_called()
            self.app.api_vision_test_button.invoke()
            deadline = time.monotonic() + 2
            while 'disabled' in self.app.api_vision_test_button.state() and time.monotonic() < deadline:
                self.root.update(); time.sleep(0.01)
        vision.assert_called_once_with(self.app.current_api_config())
        models.assert_not_called(); generate.assert_not_called()
        self.assertIn('图片测试通过', self.app.api_probe_note.get())
        self.assertIn('不应进入接口测试', self.app.context.get('1.0', 'end-1c'))
        self.assertIsNone(self.app.screenshot)

    def test_image_probe_failure_is_distinct_from_a_reachable_model_list(self):
        self.app.key.set('test-only-key')
        self.app.show_settings()
        with patch('desktop.test_connection', return_value=[self.app.model.get()]), patch('desktop.check_vision', side_effect=ServiceError('图片输入被接口拒绝（400）')):
            for button in (self.app.api_test_button, self.app.api_vision_test_button):
                button.invoke()
                deadline = time.monotonic() + 2
                while 'disabled' in button.state() and time.monotonic() < deadline:
                    self.root.update(); time.sleep(0.01)
                if button == self.app.api_test_button:
                    self.assertIn('尚未验证生成或图片识别', self.app.api_probe_note.get())
        self.assertIn('图片输入被接口拒绝', self.app.api_probe_note.get())
        self.assertNotIn('disabled', self.app.api_vision_test_button.state())

    def test_saved_output_limit_reaches_generation_and_format_errors_are_copyable(self):
        self.app.key.set('test-only-key')
        self.app.show_settings()
        self.app.max_output_tokens.set('8192')
        self.app.settings_dialog.destroy()
        self.app.remember_ui_preferences()
        self.assertEqual(self.app.settings.values()['max_output_tokens'], 8192)
        self.app.context.insert('1.0', '未知：周五几点见？')
        upstream = {'choices': [{'finish_reason': 'length', 'message': {'content': '{"replies":', 'reasoning_content': 'private chain'}}]}
        with patch('server.urllib.request.OpenerDirector.open', return_value=io.BytesIO(json.dumps(upstream).encode())) as opened:
            self.app.generate_from_editor(); self.wait_for_job()
        self.assertEqual(json.loads(opened.call_args.args[0].data)['max_tokens'], 8192)
        self.assertIn('被截断', self.app.understanding.get('1.0', 'end-1c'))
        self.assertNotIn('private chain', self.app.understanding.get('1.0', 'end-1c'))

    def test_local_meme_picker_receives_theme_without_an_extra_model_request(self):
        self.app.update_meme_suggestions({'meme_hint': '夸张无语', 'meme_tags': ['无语']})
        with patch('desktop.MemePicker') as picker, patch('desktop.generate') as generate:
            self.app.show_memes()
            picker.assert_called_once_with(self.root, self.library, query='夸张无语', tags=['无语'])
            generate.assert_not_called()
            self.app.show_memes()
            picker.assert_called_once()
            picker.return_value.lift.assert_called_once()
