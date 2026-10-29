from contextlib import nullcontext
import json
import subprocess
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from wechat_bot import BotAdapterError, OperationCancellation, _live, call_adapter, inspect_chat
from windows_capture import Window


class Control:
    def __init__(self, kind='PaneControl', name='', box=(0, 0, 600, 600), children=()):
        self.ControlTypeName, self.Name = kind, name
        self.BoundingRectangle = SimpleNamespace(**dict(zip(('left', 'top', 'right', 'bottom'), box)))
        self.children, self.IsOffscreen, self.ProcessId = list(children), False, 456
        self.value = SimpleNamespace(Value='', IsReadOnly=False, SetValue=Mock())
        self.value.SetValue.side_effect = lambda text: setattr(self.value, 'Value', text)
        self.invoke = SimpleNamespace(Invoke=Mock())

    def GetChildren(self):
        return self.children

    def GetValuePattern(self):
        return self.value

    def GetInvokePattern(self):
        return self.invoke


def message(text, box):
    return Control('ListItemControl', text, children=[Control('TextControl', text, box)])


class NativeBotTests(unittest.TestCase):
    def setUp(self):
        self.header = Control('TextControl', '小林', (20, 20, 100, 50))
        self.messages = Control('ListControl', '消息', (0, 100, 600, 420), [message('你好', (20, 120, 130, 150))])
        self.editor = Control('EditControl', '输入', (20, 450, 580, 550))
        self.send = Control('ButtonControl', '发送', (500, 560, 580, 590))
        self.root = Control('WindowControl', children=[self.header, self.messages, self.editor, self.send])
        self.window = Window(123, 456, '微信')
        self.api = SimpleNamespace(identify=Mock(return_value=self.window), uncovered=Mock(), user=SimpleNamespace(IsIconic=Mock(return_value=False)))
        self.auto = SimpleNamespace(Logger=SimpleNamespace(SetLogFile=Mock()), UIAutomationInitializerInThread=nullcontext, ControlFromHandle=Mock(return_value=self.root))
        self.payload = {'window': {'handle': 123, 'pid': 456, 'title': '微信'}, 'contact': '小林', 'action': 'read'}

    def live(self):
        with patch('wechat_bot.sys.platform', 'win32'), patch('wechat_bot.VisibleWindowsAPI', return_value=self.api), patch.dict('sys.modules', {'uiautomation': self.auto}):
            return _live(self.payload)

    def prepare_send(self):
        snapshot, _ = inspect_chat(self.root, '小林')
        self.payload.update(action='send', text='收到。', expected_digest=snapshot['digest'])

    def test_sender_direction_uses_confirmed_layout_and_ambiguous_wide_text_stays_unknown(self):
        self.messages.children += [message('我发的', (400, 160, 570, 190)), message('跨越两侧的长文本', (20, 210, 570, 240))]
        snapshot = self.live()
        self.assertEqual([item['direction'] for item in snapshot['messages']], ['other', 'self', 'unknown'])
        self.editor.value.SetValue.assert_not_called(); self.send.invoke.Invoke.assert_not_called()

    def test_read_never_descends_into_contacts_list(self):
        contacts = Control('ListControl', '联系人', children=[Control('TextControl', '私人联系人')])
        contacts.GetChildren = Mock(side_effect=AssertionError('must not read contacts'))
        self.root.children.insert(0, contacts)
        snapshot = self.live()
        self.assertNotIn('私人联系人', json.dumps(snapshot, ensure_ascii=False))

    def test_missing_or_ambiguous_contact_header_prevents_any_mutation(self):
        self.prepare_send()
        for mode in ('missing', 'duplicate'):
            self.root.children = [self.header, self.messages, self.editor, self.send]
            self.header.Name = '另一个聊天' if mode == 'missing' else '小林'
            if mode == 'duplicate':
                self.root.children.append(Control('TextControl', '小林', (130, 20, 210, 50)))
            with self.subTest(mode=mode), self.assertRaisesRegex(BotAdapterError, '聊天标题'):
                self.live()
        self.editor.value.SetValue.assert_not_called(); self.send.invoke.Invoke.assert_not_called()

    def test_send_requires_empty_input_unchanged_snapshot_and_owned_controls(self):
        for mode in ('input', 'changed', 'process', 'covered', 'readonly', 'identity'):
            self.setUp(); self.prepare_send()
            if mode == 'input':
                self.editor.value.Value = '用户自己写的草稿'
            elif mode == 'changed':
                self.messages.children.append(message('又来一条', (20, 160, 130, 190)))
            elif mode == 'process':
                self.send.ProcessId = 999
            elif mode == 'covered':
                self.api.uncovered.side_effect = BotAdapterError('消息区被遮挡')
            elif mode == 'readonly':
                self.editor.value.IsReadOnly = True
            elif mode == 'identity':
                self.api.identify.return_value = Window(123, 999, '微信')
            with self.subTest(mode=mode), self.assertRaises(BotAdapterError):
                self.live()
            self.editor.value.SetValue.assert_not_called(); self.send.invoke.Invoke.assert_not_called()

    def test_confirmed_send_uses_value_and_invoke_once_then_requires_visible_outgoing_message(self):
        self.prepare_send()
        def invoke():
            self.messages.children.append(message('收到。', (430, 200, 570, 230)))
            self.editor.value.Value = ''
        self.send.invoke.Invoke.side_effect = invoke
        receipt = self.live()
        self.assertTrue(receipt['sent'])
        self.editor.value.SetValue.assert_called_once_with('收到。')
        self.send.invoke.Invoke.assert_called_once()
        self.assertEqual(receipt['snapshot']['messages'][-1], {'direction': 'self', 'text': '收到。'})

    def test_identity_change_after_filling_never_invokes_send(self):
        self.prepare_send()
        self.api.identify.side_effect = [self.window, self.window, None]
        with self.assertRaises(BotAdapterError):
            self.live()
        self.editor.value.SetValue.assert_called_once()
        self.send.invoke.Invoke.assert_not_called()

    def test_invocation_failure_is_not_retried(self):
        self.prepare_send()
        self.send.invoke.Invoke.side_effect = RuntimeError('private COM message')
        with self.assertRaisesRegex(BotAdapterError, '未能确认') as raised:
            self.live()
        self.send.invoke.Invoke.assert_called_once()
        self.assertNotIn('private COM', str(raised.exception))

    def test_button_invocation_without_visible_outgoing_message_is_not_success(self):
        self.prepare_send()
        with patch('wechat_bot.time.monotonic', side_effect=[0, 3]):
            with self.assertRaisesRegex(BotAdapterError, '未能确认.*不会重发'):
                self.live()
        self.send.invoke.Invoke.assert_called_once()
        self.assertEqual(self.editor.value.Value, '收到。')

    def test_cancellation_before_adapter_start_creates_no_process(self):
        cancel = threading.Event(); cancel.set()
        with patch('wechat_bot.subprocess.Popen') as process:
            with self.assertRaises(BotAdapterError):
                call_adapter(self.window, '小林', action='send', text='test', cancel=cancel)
        process.assert_not_called()

    def test_subprocess_uses_private_pipe_and_validates_the_snapshot_protocol(self):
        snapshot, _ = inspect_chat(self.root, '小林')
        process = Mock(returncode=0)
        process.communicate.return_value = (json.dumps({'ok': True, 'result': snapshot}).encode(), b'')
        with patch('wechat_bot.subprocess.Popen', return_value=process) as opened:
            self.assertEqual(call_adapter(self.window, '小林'), snapshot)
        command = opened.call_args.args[0]
        self.assertNotIn('小林', str(command)); self.assertNotIn(self.window.title, str(command))
        payload = json.loads(process.communicate.call_args.args[0])
        self.assertEqual(payload['contact'], '小林')
        self.assertEqual(payload['window']['handle'], 123)
        self.assertEqual(opened.call_args.kwargs['stdin'], subprocess.PIPE)

    def test_adapter_timeout_kills_child_without_retrying(self):
        process = Mock(returncode=-1)
        process.poll.return_value = None
        process.communicate.return_value = (b'', b'private details')
        with patch('wechat_bot.subprocess.Popen', return_value=process) as opened:
            with self.assertRaisesRegex(BotAdapterError, '不会重发') as raised:
                call_adapter(self.window, '小林', action='send', text='私密回复', timeout=0)
        opened.assert_called_once()
        self.assertGreaterEqual(process.kill.call_count, 1)
        self.assertNotIn('private', str(raised.exception))

    def test_stop_synchronously_terminates_registered_native_child(self):
        cancel = OperationCancellation()
        process = Mock(); process.poll.return_value = None
        cancel.attach(process)
        cancel.set()
        process.kill.assert_called_once()
        self.assertTrue(cancel.is_set())
        cancel.detach(process)
        cancel.set()
        process.kill.assert_called_once()

    def test_process_created_just_after_pause_is_killed_before_input_is_sent(self):
        cancel = OperationCancellation(); cancel.set()
        process = Mock(); process.poll.return_value = None
        cancel.attach(process)
        process.kill.assert_called_once()
        process.communicate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
