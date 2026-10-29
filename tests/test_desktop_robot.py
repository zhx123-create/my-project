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
from windows_capture import Window


@unittest.skipUnless(os.environ.get('DISPLAY') or os.name == 'nt', 'requires a desktop display')
class DesktopRobotTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='jiehua-robot-ui-')
        self.root = tk.Tk()
        self.app = DesktopApp(self.root, memory=ConversationMemory(), settings=DesktopSettings(), meme_library=MemeLibrary(Path(self.folder.name) / 'memes'))
        self.app.select_profile('角色聊天')
        self.app.key.set('test-only-key')
        self.window = Window(123, 456, '微信')
        self.app.windows = [self.window]
        self.app.window_box.configure(values=['微信']); self.app.window_box.current(0)
        self.root.update()

    def tearDown(self):
        self.app.close()
        del self.app, self.root
        gc.collect()
        self.folder.cleanup()

    def configure(self, auto=False):
        self.app.show_robot(); self.root.update()
        fields = self.app.robot_fields
        fields['name'].set('虚拟管家')
        fields['description'].insert('1.0', '简短、冷静、讲道理')
        fields['examples'].insert('1.0', '明白，我帮你理一理。')
        fields['bot_rules'].insert('1.0', '只回复日常闲聊')
        fields['contact'].set('小林'); fields['layout'].set(True)
        if auto:
            fields['mode'].set('自动发送文字')
        return fields

    def start(self, auto=False):
        fields = self.configure(auto)
        with patch('desktop_robot.RobotRunner.start') as started:
            fields['start']()
        started.assert_called_once()
        return self.app.robot

    def test_saved_persona_is_used_by_regular_drafts_without_arming_robot(self):
        fields = self.configure()
        self.assertTrue(fields['save']())
        self.app.robot_dialog.destroy()
        self.assertIsNone(self.app.robot)
        self.app.context.insert('1.0', '对方：下午好')
        response = {'understanding': '日常问候', 'replies': [{'label': '自然', 'text': '下午好。'}], 'meme_hint': '', 'meme_tags': []}
        with patch('desktop.generate', return_value=response) as model:
            self.app.generate_from_editor()
            deadline = time.monotonic() + 3
            while self.app.busy and time.monotonic() < deadline:
                self.root.update(); time.sleep(.01)
        import json
        data = json.loads(model.call_args.args[1][1]['content'][0]['text'])
        self.assertEqual(data['自定义人设']['name'], '虚拟管家')
        self.assertIn('讲道理', data['自定义人设']['description'])

    def test_start_defaults_to_dry_run_and_freezes_exact_window_and_profile(self):
        runner = self.start()
        self.assertTrue(runner.dry_run)
        self.assertEqual(runner.window, self.window)
        self.assertEqual(runner.contact, '小林')
        self.assertEqual(runner.profile['name'], '角色聊天')
        self.assertTrue(self.app.busy)
        self.assertNotIn('disabled', self.app.robot_button.state())
        self.assertIn('disabled', self.app.profile_box.state())

    def test_sending_mode_is_explicit_and_pause_does_not_arm_another_session(self):
        runner = self.start(auto=True)
        self.assertFalse(runner.dry_run)
        self.app.robot_button.invoke()
        self.assertTrue(runner.cancel.is_set())
        self.assertFalse(self.app.busy)
        self.app.robot_events.put((runner, 'stopped', {}))
        self.app.poll_robot()
        self.assertIsNone(self.app.robot)
        self.assertEqual(self.app.robot_button.cget('text'), '聊天机器人')

    def test_adapter_failure_restores_controls_and_keeps_explanation(self):
        runner = self.start()
        self.app.robot_events.put((runner, 'error', {'message': '当前微信未公开发送控件。'}))
        self.app.robot_events.put((runner, 'stopped', {}))
        self.app.poll_robot()
        self.assertFalse(self.app.busy)
        self.assertIsNone(self.app.robot)
        self.assertIn('未公开发送控件', self.app.status.get())
        self.assertNotIn('disabled', self.app.profile_box.state())

    def test_only_confirmed_sent_event_is_recorded_and_robot_results_remain_visible(self):
        runner = self.start(auto=True)
        result = {'understanding': '日常问候', 'should_reply': True, 'replies': [{'label': '自然', 'text': '下午好。'}], 'meme_hint': '', 'meme_tags': []}
        self.app.robot_events.put((runner, 'draft', {'result': result}))
        self.app.poll_robot()
        self.assertEqual(self.app.memory.profile('角色聊天')['history'], [])
        self.app.robot_events.put((runner, 'sent', {'result': result, 'text': '下午好。'}))
        self.app.poll_robot()
        self.assertEqual(self.app.memory.profile('角色聊天')['history'], [{'kind': 'sent', 'text': '下午好。', 'source': 'robot'}])
        self.assertEqual(self.app.reply_texts[0].get('1.0', 'end-1c'), '下午好。')
        self.assertEqual(self.app.understanding.get('1.0', 'end-1c'), '日常问候')
        self.assertEqual(self.app.reply_labels[0].cget('text'), '已自动发送')

    def test_target_name_and_layout_are_required_before_starting(self):
        self.app.show_robot()
        with patch('desktop_robot.RobotRunner.start') as start:
            self.app.robot_fields['start']()
        start.assert_not_called()
        self.assertIn('聊天名称', self.app.robot_fields['note'].get())


if __name__ == '__main__':
    unittest.main()
