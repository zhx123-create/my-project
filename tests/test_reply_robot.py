import copy
import io
import json
import queue
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from conversation_memory import ConversationMemory
from reply_persona import validate_persona
from reply_robot import RobotRunner, RobotSession
from server import Config, InputError, ServiceError, generate, make_messages
from wechat_bot import BotAdapterError, digest_snapshot
from windows_capture import Window


def snapshot(*messages):
    result = {'contact': '小林', 'messages': [{'direction': direction, 'text': text} for direction, text in messages]}
    result['digest'] = digest_snapshot(result)
    return result


def decision(reply=True):
    return {'should_reply': reply, 'understanding': '日常问候，可简短回应。' if reply else '无需继续接话。',
            'replies': [{'label': '自然', 'text': '收到，今天也一起加油。'}] if reply else [], 'meme_hint': '', 'meme_tags': []}


class PersonaTests(unittest.TestCase):
    def test_persona_is_separate_per_conversation_and_copies_do_not_mutate_memory(self):
        memory = ConversationMemory()
        memory.update_profile('A', persona={'name': '虚拟管家', 'description': '简洁', 'examples': '明白，交给我整理。'}, bot_rules='只回复日常问候')
        first = memory.profile('A')
        first['persona']['name'] = 'changed'
        self.assertEqual(memory.profile('A')['persona']['name'], '虚拟管家')
        self.assertEqual(memory.profile('B')['persona'], validate_persona())
        self.assertEqual(memory.profile('B')['bot_rules'], '')
        data = json.loads(make_messages({'context': '你好', 'persona': memory.profile('A')['persona']})[1]['content'][0]['text'])
        self.assertEqual(data['自定义人设']['description'], '简洁')

    def test_persona_validation_rejects_bad_types_and_limits(self):
        for value in ([], {'extra': 'x'}, {'name': 'x' * 61}, {'description': None}, {'examples': 'x' * 3001}):
            with self.subTest(value=str(value)[:60]), self.assertRaises(ValueError):
                validate_persona(value)

    def test_persona_persistence_is_opt_in_and_legacy_files_receive_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'memory.json'
            memory = ConversationMemory(path)
            persona = {'name': '虚拟管家', 'description': '冷静', 'examples': '明白。'}
            memory.update_profile('角色', persona=persona, bot_rules='只回复问候')
            self.assertFalse(path.exists())
            memory.set_persistent(True)
            restored = ConversationMemory(path).profile('角色')
            self.assertEqual(restored['persona'], persona)
            self.assertEqual(restored['bot_rules'], '只回复问候')
            memory.confirm_sent('角色', '已收到。', confirmed_by_robot=True)
            confirmed = ConversationMemory(path)
            self.assertEqual(confirmed.profile('角色')['history'][-1]['source'], 'robot')
            self.assertIn('机器人核对已发送', confirmed.history_text('角色'))
            raw = json.loads(path.read_text())
            raw['profiles'][0].pop('persona'); raw['profiles'][0].pop('bot_rules')
            path.write_text(json.dumps(raw))
            legacy = ConversationMemory(path).profile('角色')
            self.assertEqual(legacy['persona'], validate_persona())
            self.assertEqual(legacy['bot_rules'], '')

    def test_robot_contract_requires_explicit_boolean_and_bounded_single_reply(self):
        for result in (decision(True), decision(False)):
            body = {'choices': [{'message': {'content': json.dumps(result)}}]}
            with patch('server.urllib.request.OpenerDirector.open', return_value=io.BytesIO(json.dumps(body).encode())):
                self.assertEqual(generate(Config('test'), [], reply_count=1, include_analysis=True, automated=True), result)
        invalid = [decision(True), decision(False), decision(True), decision(True)]
        invalid[0].pop('should_reply')
        invalid[1]['should_reply'] = 'false'
        invalid[2]['replies'][0]['text'] = '字' * 201
        invalid[3]['should_reply'] = False
        for result in invalid:
            body = {'choices': [{'message': {'content': json.dumps(result)}}]}
            with self.subTest(result=str(result)[:40]), patch('server.urllib.request.OpenerDirector.open', return_value=io.BytesIO(json.dumps(body).encode())):
                with self.assertRaises(ServiceError):
                    generate(Config('test'), [], reply_count=1, include_analysis=True, automated=True)
        with self.assertRaises(InputError):
            make_messages({'context': '你好', 'automated': True})


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.session = RobotSession('小林')
        self.first = snapshot(('other', '旧消息'))

    def test_start_is_baseline_and_continuous_messages_are_merged(self):
        self.assertEqual(self.session.observe(self.first, 0), 'baseline')
        self.assertEqual(self.session.observe(self.first, 10), 'same')
        second = snapshot(('other', '旧消息'), ('other', '新消息1'))
        third = snapshot(('other', '旧消息'), ('other', '新消息1'), ('other', '新消息2'))
        self.assertEqual(self.session.observe(second, 11), 'pending')
        self.assertEqual(self.session.observe(third, 12), 'pending')
        self.assertEqual(self.session.observe(third, 13), 'same')
        self.assertEqual(self.session.observe(third, 14), 'ready')
        self.assertEqual(self.session.pending, third)
        self.session.complete(third)
        self.assertEqual(self.session.observe(third, 20), 'same')

    def test_own_and_unknown_messages_cancel_pending_replies(self):
        for direction, expected in (('self', 'own'), ('unknown', 'unknown')):
            self.setUp()
            self.session.observe(self.first, 0)
            self.session.observe(snapshot(('other', '旧消息'), ('other', '新消息')), 1)
            changed = snapshot(('other', '旧消息'), ('other', '新消息'), (direction, '最后一句'))
            self.assertEqual(self.session.observe(changed, 2), expected)
            self.assertIsNone(self.session.pending)

    def test_history_expansion_and_missing_overlap_do_not_trigger_reply(self):
        self.session.observe(self.first, 0)
        self.assertEqual(self.session.observe(snapshot(('self', '更早的消息'), ('other', '旧消息')), 1), 'same')
        with self.assertRaisesRegex(BotAdapterError, '不连续'):
            self.session.observe(snapshot(('other', '另一个聊天')), 2)

    def test_cooldown_applies_after_confirmed_send(self):
        self.session.observe(self.first, 0)
        self.session.complete(self.first, sent=True, now=5)
        second = snapshot(('other', '旧消息'), ('other', '新消息'))
        self.session.observe(second, 6)
        self.assertEqual(self.session.observe(second, 10), 'same')
        self.assertEqual(self.session.observe(second, 15), 'ready')


class RunnerTests(unittest.TestCase):
    def setUp(self):
        profile = ConversationMemory().profile('当前会话')
        self.events = queue.Queue()
        self.runner = RobotRunner(Window(1, 2, '聊天'), '小林', profile, Config('test'), self.events, interval=.001)
        self.runner.session.settle_seconds = 0
        self.first = snapshot(('other', '旧消息'))
        self.second = snapshot(('other', '旧消息'), ('other', '你好呀'))

    def run_until_decision(self, result, dry_run=True, changing=False, fail_send=False):
        self.runner.dry_run = dry_run
        sent = snapshot(('other', '旧消息'), ('other', '你好呀'), ('self', result['replies'][0]['text'] if result['replies'] else '已发出'))
        calls = []
        def adapter(window, contact, **kwargs):
            calls.append(kwargs)
            if kwargs.get('action') == 'send':
                if fail_send:
                    raise BotAdapterError('发送结果不确定，不会重试。')
                self.runner.cancel.set()
                return {'sent': True, 'snapshot': sent}
            if len(calls) == 1:
                return self.first
            if len(calls) <= 3:
                return self.second
            if changing:
                self.runner.cancel.set()
                return snapshot(('other', '旧消息'), ('other', '你好呀'), ('self', '我已手动回复'))
            return self.second
        original = self.runner.emit
        def emit(kind, **values):
            original(kind, **values)
            if kind in ('draft', 'skipped', 'error'):
                self.runner.stop()
        with patch('reply_robot.call_adapter', side_effect=adapter) as native, patch('reply_robot.generate', return_value=copy.deepcopy(result)) as model, patch.object(self.runner, 'emit', side_effect=emit):
            self.runner.run()
        return native, model, list(self.events.queue)

    def test_dry_run_generates_but_never_fills_or_sends(self):
        native, model, events = self.run_until_decision(decision(True))
        model.assert_called_once()
        self.assertFalse(any(call.kwargs.get('action') == 'send' for call in native.call_args_list))
        self.assertIn('draft', [event[1] for event in events])

    def test_skip_decision_never_attempts_send(self):
        native, model, events = self.run_until_decision(decision(False), dry_run=False)
        self.assertEqual(native.call_count, 3)
        self.assertIn('skipped', [event[1] for event in events])

    def test_send_happens_once_only_after_fresh_snapshot_matches(self):
        native, model, events = self.run_until_decision(decision(True), dry_run=False)
        sends = [call for call in native.call_args_list if call.kwargs.get('action') == 'send']
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0].kwargs['expected_digest'], self.second['digest'])
        self.assertIn('sent', [event[1] for event in events])

    def test_pause_during_ai_request_prevents_sending(self):
        def generate_and_pause(*args, **kwargs):
            self.runner.stop(); return decision(True)
        with patch('reply_robot.call_adapter', side_effect=[self.first, self.second, self.second]) as native, patch('reply_robot.generate', side_effect=generate_and_pause):
            self.runner.run()
        self.assertEqual(native.call_count, 3)
        self.assertNotIn('sent', [event[1] for event in list(self.events.queue)])

    def test_new_messages_during_generation_discard_old_reply_without_sending(self):
        changed = snapshot(('other', '旧消息'), ('other', '你好呀'), ('other', '又补了一句'))
        def emit(kind, **values):
            self.events.put((self.runner, kind, values))
            if kind == 'stale':
                self.runner.stop()
        with patch('reply_robot.call_adapter', side_effect=[self.first, self.second, self.second, changed]) as native, patch('reply_robot.generate', return_value=decision(True)), patch.object(self.runner, 'emit', side_effect=emit):
            self.runner.run()
        self.assertEqual(native.call_count, 4)
        self.assertFalse(any(call.kwargs.get('action') == 'send' for call in native.call_args_list))
        self.assertEqual(self.runner.session.pending, changed)

    def test_unknown_send_result_pauses_without_retry_or_sent_event(self):
        native, model, events = self.run_until_decision(decision(True), dry_run=False, fail_send=True)
        self.assertEqual(sum(call.kwargs.get('action') == 'send' for call in native.call_args_list), 1)
        self.assertIn('error', [event[1] for event in events])
        self.assertNotIn('sent', [event[1] for event in events])
