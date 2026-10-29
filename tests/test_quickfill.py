from contextlib import nullcontext
import ctypes
import json
import queue
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from desktop_quickfill import QuickFillJob
from quickfill_native import (Input, QuickFillError, _live, call_native, fill_focused,
                              prepare_text, read_context, text_digest, unicode_inputs,
                              validate_snapshot, verify_focus)
from server import Config, ServiceError
from wechat_bot import OperationCancellation
from wechat_reader import ReaderFailure
from wechat_hotkey import WeChatHotkey
from windows_capture import Window


def snapshot():
    context = '对方：下午好。'
    return {'window': {'handle': 123, 'pid': 456, 'title': '小林'},
            'focus': {'handle': 123, 'input_time': 789}, 'context': context,
            'digest': text_digest(context), 'source': 'windows_ocr'}


def result():
    return {'understanding': '日常问候。', 'replies': [{'label': '自然', 'text': '下午好呀。'}], 'meme_hint': '', 'meme_tags': []}


class NativeQuickFillTests(unittest.TestCase):
    def setUp(self):
        self.window = Window(123, 456, '小林')
        self.focus = snapshot()['focus']
        self.api = SimpleNamespace(identify=Mock(return_value=self.window), focus_token=Mock(return_value=self.focus),
                                   keys_up=Mock(return_value=True), type_unicode=Mock(),
                                   user=SimpleNamespace(GetForegroundWindow=Mock(return_value=123), IsIconic=Mock(return_value=False)))

    def test_unicode_events_cannot_press_enter_and_preserve_chinese_and_surrogates(self):
        text = '你好\n收到\r\n😎'
        events = unicode_inputs(text)
        self.assertEqual(prepare_text(text), '你好 收到 😎')
        self.assertEqual(ctypes.sizeof(Input), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
        self.assertTrue(all(event.kind == 1 and event.keyboard.virtual_key == 0 and event.keyboard.flags in (4, 6) for event in events))
        self.assertEqual([event.keyboard.flags for event in events], [4, 6] * (len(events) // 2))
        units = b''.join(int(event.keyboard.scan).to_bytes(2, 'little') for event in events[::2])
        self.assertEqual(units.decode('utf-16-le'), '你好 收到 😎')
        self.assertNotIn(13, [event.keyboard.scan for event in events]); self.assertNotIn(10, [event.keyboard.scan for event in events])
        for invalid in ('', ' \n', 'x\ttext', 'x\x00', 'x\x7f', '\ud800', 'x' * 3001):
            with self.subTest(value=repr(invalid)[:30]), self.assertRaises(QuickFillError):
                prepare_text(invalid)

    def test_changed_window_focus_or_input_cancels_before_typing(self):
        for reason in ('window', 'focus', 'keys', 'minimized'):
            self.setUp()
            if reason == 'window':
                self.api.identify.return_value = Window(123, 456, '另一个聊天')
            elif reason == 'focus':
                self.api.focus_token.return_value = {**self.focus, 'input_time': 790}
            elif reason == 'keys':
                self.api.keys_up.return_value = False
            else:
                self.api.user.IsIconic.return_value = True
            with self.subTest(reason=reason), self.assertRaises(QuickFillError):
                fill_focused(self.api, self.window, self.focus, '下午好。')
            self.api.type_unicode.assert_not_called()

    def test_value_pattern_fill_does_not_need_message_list_or_send_button(self):
        pattern = SimpleNamespace(Value='', IsReadOnly=False, SetValue=Mock())
        pattern.SetValue.side_effect = lambda text: setattr(pattern, 'Value', text)
        control = SimpleNamespace(ProcessId=456)
        receipt = fill_focused(self.api, self.window, self.focus, '下午好。', (control, pattern))
        self.assertEqual(receipt, {'filled': True, 'confirmed': True, 'method': 'uia'})
        pattern.SetValue.assert_called_once_with('下午好。')
        self.api.type_unicode.assert_not_called()

    def test_existing_draft_and_failed_set_value_are_never_overwritten_or_retried(self):
        pattern = SimpleNamespace(Value='用户草稿', IsReadOnly=False, SetValue=Mock())
        control = SimpleNamespace(ProcessId=456)
        with self.assertRaisesRegex(QuickFillError, '已有文字'):
            fill_focused(self.api, self.window, self.focus, '回复', (control, pattern))
        pattern.SetValue.assert_not_called()
        pattern.Value = ''
        pattern.SetValue.side_effect = RuntimeError('private COM error')
        with self.assertRaisesRegex(QuickFillError, '不会.*重复') as raised:
            fill_focused(self.api, self.window, self.focus, '回复', (control, pattern))
        pattern.SetValue.assert_called_once()
        self.api.type_unicode.assert_not_called()
        self.assertNotIn('private COM', str(raised.exception))

    def test_missing_value_pattern_types_only_into_the_unchanged_chosen_focus(self):
        receipt = fill_focused(self.api, self.window, self.focus, '下午好。')
        self.api.type_unicode.assert_called_once_with('下午好。')
        self.assertIs(receipt['confirmed'], False)
        self.assertEqual(receipt['method'], 'unicode')

    def test_unreadable_uia_automatically_uses_only_selected_window_and_local_ocr(self):
        auto = SimpleNamespace(Logger=SimpleNamespace(SetLogFile=Mock()), UIAutomationInitializerInThread=nullcontext, ControlFromHandle=Mock())
        with patch.dict('sys.modules', {'uiautomation': auto}), patch('quickfill_native.extract_messages', side_effect=ReaderFailure('unavailable')), patch('quickfill_native.capture_visible', return_value='selected-image') as capture, patch('quickfill_native.recognize_image', return_value={'text': '对方：下午好。'}) as ocr:
            text, source = read_context(self.window, self.api)
        capture.assert_called_once_with(self.window, api=self.api, preview=True)
        ocr.assert_called_once_with('selected-image')
        self.assertEqual((text, source), ('对方：下午好。', 'windows_ocr'))

    def test_live_read_captures_window_and_focus_then_rechecks_before_returning(self):
        with patch('quickfill_native.sys.platform', 'win32'), patch('quickfill_native.read_context', return_value=('对方：下午好。', 'windows_ocr')):
            value = _live({'action': 'read_current'}, self.api)
        self.assertEqual(value, snapshot())
        validate_snapshot(value)
        self.assertGreaterEqual(self.api.focus_token.call_count, 3)
        self.api.type_unicode.assert_not_called()

    def test_ambiguous_message_regions_never_fall_back_to_reading_multiple_chats(self):
        auto = SimpleNamespace(Logger=SimpleNamespace(SetLogFile=Mock()), UIAutomationInitializerInThread=nullcontext, ControlFromHandle=Mock())
        with patch.dict('sys.modules', {'uiautomation': auto}), patch('quickfill_native.extract_messages', side_effect=ReaderFailure('ambiguous')), patch('quickfill_native.capture_visible') as capture:
            with self.assertRaisesRegex(QuickFillError, '多个消息区域'):
                read_context(self.window, self.api)
        capture.assert_not_called()

    def test_new_chat_content_after_generation_does_not_fill_old_reply(self):
        with patch('quickfill_native.sys.platform', 'win32'), patch('quickfill_native.read_context', return_value=('对方：又补了一句。', 'windows_ocr')):
            with self.assertRaisesRegex(QuickFillError, '新内容'):
                _live({**snapshot(), 'action': 'fill', 'text': '旧回复'}, self.api)
        self.api.type_unicode.assert_not_called()

    def test_floating_entry_never_reads_a_different_window_after_the_click(self):
        expected = {'handle': 999, 'pid': 456, 'title': '之前的聊天'}
        with patch('quickfill_native.sys.platform', 'win32'), patch('quickfill_native.read_context') as read:
            with self.assertRaisesRegex(QuickFillError, '未读取其他聊天'):
                _live({'action': 'read_current', 'expected_window': expected}, self.api)
        read.assert_not_called()
        self.api.type_unicode.assert_not_called()

    def test_snapshot_protocol_rejects_bad_scope_or_digest(self):
        for update in ({'window': {'handle': True, 'pid': 456, 'title': 'x'}}, {'focus': {'handle': 123, 'input_time': -1}}, {'digest': 'wrong'}, {'source': 'desktop'}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                validate_snapshot({**snapshot(), **update})

    def test_native_payload_stays_on_pipe_and_cancelled_job_starts_no_process(self):
        process = Mock(returncode=0)
        process.communicate.return_value = (json.dumps({'ok': True, 'result': snapshot()}).encode(), b'')
        with patch('quickfill_native.subprocess.Popen', return_value=process) as opened:
            self.assertEqual(call_native({'action': 'read_current'}), snapshot())
        self.assertNotIn('下午好', str(opened.call_args.args[0]))
        cancel = OperationCancellation(); cancel.set()
        with patch('quickfill_native.subprocess.Popen') as opened:
            with self.assertRaises(QuickFillError):
                call_native({'action': 'fill'}, cancel)
        opened.assert_not_called()


class QuickFillJobTests(unittest.TestCase):
    def setUp(self):
        self.events = queue.Queue()
        self.job = QuickFillJob(Config('test-only-key'), {'style': {'personality': '自然'}, 'context': '应由当前窗口替换'}, self.events)

    def test_one_gesture_reads_generates_and_fills_without_send_action_or_image_upload(self):
        with patch('desktop_quickfill.call_native', side_effect=[snapshot(), {'filled': True, 'confirmed': False, 'method': 'unicode'}]) as native, patch('desktop_quickfill.generate', return_value=result()) as model:
            self.job.run()
        self.assertEqual([call.args[0]['action'] for call in native.call_args_list], ['read_current', 'fill'])
        self.assertEqual(native.call_args.args[0]['focus'], snapshot()['focus'])
        self.assertNotIn('test-only-key', json.dumps(native.call_args.args[0]))
        payload = model.call_args.args[1][1]['content']
        self.assertEqual(len(payload), 1)
        self.assertEqual(json.loads(payload[0]['text'])['聊天上下文'], snapshot()['context'])
        self.assertEqual([event[1] for event in list(self.events.queue)], ['reading', 'context', 'generating', 'generated', 'filled', 'done'])

    def test_pause_while_waiting_for_model_prevents_fill_and_late_draft(self):
        def delayed(*args, **kwargs):
            self.job.cancel.set(); return result()
        with patch('desktop_quickfill.call_native', return_value=snapshot()) as native, patch('desktop_quickfill.generate', side_effect=delayed):
            self.job.run()
        native.assert_called_once()
        self.assertNotIn('generated', [event[1] for event in list(self.events.queue)])

    def test_generation_failure_leaves_context_but_never_attempts_to_type(self):
        with patch('desktop_quickfill.call_native', return_value=snapshot()) as native, patch('desktop_quickfill.generate', side_effect=ServiceError('看图请求被拒绝（403）')):
            self.job.run()
        native.assert_called_once()
        kinds = [event[1] for event in list(self.events.queue)]
        self.assertIn('context', kinds); self.assertIn('error', kinds); self.assertNotIn('generated', kinds)


class HotkeyTests(unittest.TestCase):
    def test_registers_only_explicit_shortcut_dispatches_to_queue_and_unregisters(self):
        events = queue.Queue()
        listener = WeChatHotkey(events)
        listener.stop_event = SimpleNamespace(is_set=Mock(side_effect=[False, True]), wait=Mock())
        user = SimpleNamespace(RegisterHotKey=Mock(return_value=True), UnregisterHotKey=Mock(), PeekMessageW=Mock())
        count = 0
        def message(pointer, *args):
            nonlocal count
            count += 1
            if count == 1:
                pointer._obj.wParam = 0x4A48
                return True
            return False
        user.PeekMessageW.side_effect = message
        with patch('wechat_hotkey.ctypes.WinDLL', return_value=user, create=True):
            listener.run()
        user.RegisterHotKey.assert_called_once_with(None, 0x4A48, 0x4003, ord('R'))
        user.UnregisterHotKey.assert_called_once_with(None, 0x4A48)
        self.assertEqual(events.get_nowait(), ('hotkey', None))

    def test_occupied_shortcut_reports_button_fallback_without_keyboard_hook(self):
        events = queue.Queue()
        listener = WeChatHotkey(events)
        user = SimpleNamespace(RegisterHotKey=Mock(return_value=False), UnregisterHotKey=Mock(), PeekMessageW=Mock())
        with patch('wechat_hotkey.ctypes.WinDLL', return_value=user, create=True):
            listener.run()
        self.assertEqual(events.get_nowait()[0], 'hotkey_error')
        user.PeekMessageW.assert_not_called(); user.UnregisterHotKey.assert_not_called()


if __name__ == '__main__':
    unittest.main()
