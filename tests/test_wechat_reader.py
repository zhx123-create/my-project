import io
import json
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from wechat_reader import MAX_TEXT, ReaderFailure, _read_live, _reader_cli, extract_messages, read_conversation
from windows_capture import Window


class Control:
    def __init__(self, kind='PaneControl', name='', children=(), offscreen=False):
        self.ControlTypeName, self._name = kind, name
        self.children, self.IsOffscreen = list(children), offscreen
        self.name_reads, self.children_reads = 0, 0

    @property
    def Name(self):
        self.name_reads += 1
        return self._name

    def GetChildren(self):
        self.children_reads += 1
        return self.children


def message(text, **kwargs):
    return Control('ListItemControl', text, **kwargs)


def root_with_messages(items):
    return Control('WindowControl', 'private contact title', [Control('PaneControl', children=[Control('ListControl', '消息', items)])])


class ScopedReaderTests(unittest.TestCase):
    def test_reads_message_entries_without_speaker_or_contact_guessing(self):
        root = root_with_messages([message('张三：周末见吗？'), message('最近有点忙')])
        result = extract_messages(root)
        self.assertIn('张三：周末见吗？', result['text'])
        self.assertIn('最近有点忙', result['text'])
        self.assertNotIn('private contact title', result['text'])
        self.assertFalse(result['identity_verified'])
        self.assertEqual(result['count'], 2)
        self.assertEqual(root.name_reads, 0)

    def test_contact_lists_are_never_traversed_or_extracted(self):
        contact = Control('ListItemControl', 'private contact data')
        contacts = Control('ListControl', '联系人', [contact])
        root = root_with_messages([message('allowed message')])
        root.children.insert(0, contacts)
        result = extract_messages(root)
        self.assertNotIn('private contact', result['text'])
        self.assertEqual(contacts.children_reads, 0)
        self.assertEqual(contact.name_reads, 0)

    def test_no_fallback_to_window_text_if_message_list_missing(self):
        root = Control('WindowControl', children=[Control('TextControl', 'private arbitrary text')])
        with self.assertRaisesRegex(ReaderFailure, '没有公开'):
            extract_messages(root)
        self.assertEqual(root.children[0].name_reads, 0)

    def test_hidden_messages_and_control_buttons_are_not_read(self):
        hidden = message('hidden private message', offscreen=True)
        button = Control('ButtonControl', 'not a message')
        result = extract_messages(root_with_messages([hidden, button, message('visible')]))
        self.assertNotIn('hidden', result['text'])
        self.assertNotIn('not a message', result['text'])
        self.assertEqual(hidden.name_reads, 0)
        self.assertEqual(result['count'], 1)

    def test_text_children_are_used_when_item_name_is_not_the_content(self):
        item = message('张三', children=[Control('TextControl', '实际内容'), Control('ButtonControl', '发送')])
        result = extract_messages(root_with_messages([item]))
        self.assertIn('张三\n实际内容', result['text'])
        self.assertNotIn('发送', result['text'])

    def test_multiple_visible_message_lists_are_ambiguous(self):
        root = Control('WindowControl', children=[Control('ListControl', '消息', [message('a')]), Control('ListControl', '聊天消息', [message('b')])])
        with self.assertRaisesRegex(ReaderFailure, '多个'):
            extract_messages(root)

    def test_latest_entries_and_total_character_budget_are_bounded(self):
        result = extract_messages(root_with_messages([message(f'条目{i}: ' + '字' * 500) for i in range(150)]))
        self.assertLessEqual(result['count'], 100)
        self.assertLessEqual(len(result['text']), MAX_TEXT)
        self.assertIn('条目149', result['text'])
        self.assertNotIn('条目0:', result['text'])

    def test_empty_list_is_not_reported_as_success(self):
        with self.assertRaisesRegex(ReaderFailure, '没有可读取'):
            extract_messages(root_with_messages([]))

    def test_truncated_tree_search_does_not_pick_a_partial_candidate(self):
        root = root_with_messages([message('candidate')])
        root.children.extend(Control('PaneControl') for _ in range(600))
        with self.assertRaisesRegex(ReaderFailure, '没有公开'):
            extract_messages(root)


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.window = Window(123, 456, 'private title')

    def test_bounded_process_passes_numeric_identity_and_parses_payload(self):
        payload = {'text': '对方：你好', 'count': 1, 'source': 'uia', 'identity_verified': False}
        response = SimpleNamespace(returncode=0, stdout=json.dumps(payload).encode(), stderr=b'')
        with patch('wechat_reader.subprocess.run', return_value=response) as run:
            self.assertEqual(read_conversation(self.window), payload)
        self.assertEqual(run.call_args.args[0][-3:], ['--read-window', '123', '456'])
        self.assertNotIn('private title', run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs['timeout'], 8)
        self.assertTrue(run.call_args.kwargs['capture_output'])

    def test_timeout_and_native_errors_are_redacted(self):
        with patch('wechat_reader.subprocess.run', side_effect=subprocess.TimeoutExpired('secret command', 8, stderr=b'secret')):
            with self.assertRaisesRegex(ReaderFailure, '超时') as raised:
                read_conversation(self.window)
            self.assertNotIn('secret', str(raised.exception))
        response = SimpleNamespace(returncode=1, stdout=b'', stderr=b'private title and API_KEY=secret')
        with patch('wechat_reader.subprocess.run', return_value=response):
            with self.assertRaises(ReaderFailure) as raised:
                read_conversation(self.window)
            self.assertNotIn('secret', str(raised.exception))

    def test_unknown_identity_or_invalid_text_is_rejected(self):
        for payload in ({'text': 'hi', 'count': 1, 'source': 'uia', 'identity_verified': True}, {'text': 'x' * (MAX_TEXT + 1), 'count': 1, 'source': 'uia', 'identity_verified': False}, {'text': 'hi', 'count': True, 'source': 'uia', 'identity_verified': False}):
            response = SimpleNamespace(returncode=0, stdout=json.dumps(payload).encode(), stderr=b'')
            with patch('wechat_reader.subprocess.run', return_value=response):
                with self.assertRaisesRegex(ReaderFailure, '有效'):
                    read_conversation(self.window)

    def test_windows_process_has_no_console(self):
        payload = {'text': 'hi', 'count': 1, 'source': 'uia', 'identity_verified': False}
        response = SimpleNamespace(returncode=0, stdout=json.dumps(payload).encode(), stderr=b'')
        with patch('wechat_reader.sys.platform', 'win32'), patch('wechat_reader.subprocess.run', return_value=response) as run:
            read_conversation(self.window)
        self.assertEqual(run.call_args.kwargs['creationflags'], 0x08000000)

    def test_reused_window_is_rejected_before_com_import(self):
        api = SimpleNamespace(identify=Mock(return_value=Window(123, 999, 'wrong process')), user=SimpleNamespace(IsIconic=Mock(return_value=False)))
        with patch('wechat_reader.sys.platform', 'win32'), patch('wechat_reader.WindowsAPI', return_value=api):
            with self.assertRaisesRegex(ReaderFailure, '发生变化'):
                _read_live(self.window)
        api.user.IsIconic.assert_not_called()

    def test_minimized_window_is_rejected_before_com_import(self):
        api = SimpleNamespace(identify=Mock(return_value=self.window), user=SimpleNamespace(IsIconic=Mock(return_value=True)))
        with patch('wechat_reader.sys.platform', 'win32'), patch('wechat_reader.WindowsAPI', return_value=api):
            with self.assertRaisesRegex(ReaderFailure, '还原'):
                _read_live(self.window)

    def test_cli_suppresses_arbitrary_diagnostics_and_uses_safe_error_protocol(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        def fail(window):
            print('private third-party diagnostic')
            raise OSError('private native detail')
        with patch('wechat_reader.sys.stdout', stdout), patch('wechat_reader.sys.stderr', stderr), patch('wechat_reader._read_live', side_effect=fail):
            self.assertEqual(_reader_cli(['--read-window', '123', '456']), 1)
        self.assertEqual(stdout.getvalue(), '')
        self.assertEqual(json.loads(stderr.getvalue()), {'reader_error': 'failed'})

    @unittest.skipIf(sys.platform == 'win32', 'non-Windows diagnostic')
    def test_real_helper_reports_unsupported_os_without_loading_uia(self):
        with self.assertRaisesRegex(ReaderFailure, 'Windows 10/11'):
            read_conversation(self.window)
