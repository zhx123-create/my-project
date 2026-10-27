import math
from types import SimpleNamespace
import unittest

from conversation_watch import ConversationWatch, MAX_SNAPSHOT_CHARS, normalize_snapshot


HEADER = "【微信可访问消息文字；发言者及会话身份未自动核实，请核对】\n"


class ConversationWatchTests(unittest.TestCase):
    def setUp(self):
        self.window = SimpleNamespace(handle=123, pid=456, title="微信")
        self.watch = ConversationWatch()

    def start(self, baseline=""):
        self.watch.start("朋友", self.window, baseline, now=100)

    def test_scheduling_is_serialized_and_uses_minimum_interval(self):
        self.assertFalse(self.watch.due(100))
        self.start()
        self.assertTrue(self.watch.due(100))
        self.assertTrue(self.watch.due(100), "due must not consume a scheduled check")
        self.assertFalse(self.watch.due(100, busy=True))
        self.watch.mark_check(100)
        self.assertFalse(self.watch.due(109.99))
        self.assertTrue(self.watch.due(110))
        self.watch.mark_check(145)
        self.assertFalse(self.watch.due(150))
        self.assertTrue(self.watch.due(155))

    def test_start_stop_tokens_bind_window_values_and_discard_old_identity(self):
        self.start()
        original_generation = self.watch.generation
        self.window.title = "另一个窗口"
        self.assertEqual(self.watch.window.title, "微信")
        self.assertFalse(self.watch.matches_window(self.window))
        self.assertFalse(self.watch.matches_window(SimpleNamespace(handle=123, pid=999, title="微信")))
        self.assertTrue(self.watch.matches_window(SimpleNamespace(handle=123, pid=456, title="微信")))
        self.watch.stop("已暂停")
        self.assertGreater(self.watch.generation, original_generation)
        self.assertFalse(self.watch.active)
        self.assertEqual(self.watch.reason, "已暂停")
        self.assertFalse(self.watch.due(1000))
        paused_generation = self.watch.generation
        self.watch.start("同事", self.window)
        self.assertGreater(self.watch.generation, paused_generation)
        self.assertEqual(self.watch.profile, "同事")

    def test_prefix_and_blank_lines_are_normalized_without_inventing_speakers(self):
        self.start()
        text = HEADER + "  这个周末见吗？ \r\n\r\n我还没确定。  "
        self.assertEqual(self.watch.accept_snapshot(text, now=100), "changed")
        self.assertEqual(self.watch.pending_text, "这个周末见吗？\n我还没确定。")
        self.assertNotIn("对方", self.watch.pending_text)
        self.assertEqual(self.watch.accept_snapshot("这个周末见吗？\n我还没确定。", now=110), "same")

    def test_identical_snapshots_do_not_regenerate_or_clear_pending(self):
        self.start()
        self.assertEqual(self.watch.accept_snapshot("对方：晚上吃什么？"), "changed")
        self.assertEqual(self.watch.accept_snapshot("对方：晚上吃什么？"), "same")
        self.assertEqual(self.watch.pending_text, "对方：晚上吃什么？")
        self.assertEqual(self.watch.acknowledged_text, "")
        self.watch.acknowledge("对方：晚上吃什么？")
        self.assertEqual(self.watch.pending_text, "")
        self.assertEqual(self.watch.accept_snapshot("对方：晚上吃什么？"), "same")

    def test_baseline_is_not_new_but_new_appended_content_is_pending(self):
        self.start("对方：周末见吗？\n我：周六可以。")
        self.assertEqual(self.watch.accept_snapshot(HEADER + "对方：周末见吗？\n我：周六可以。"), "same")
        self.assertEqual(self.watch.accept_snapshot("我：周六可以。\n对方：中午怎么样？"), "changed")
        self.assertEqual(self.watch.pending_text, "我：周六可以。\n对方：中午怎么样？")

    def test_older_or_smaller_viewport_does_not_generate(self):
        self.start("对方：周末见吗？\n我：周六可以。")
        self.assertEqual(self.watch.accept_snapshot("我：周六可以。"), "same")
        self.assertEqual(self.watch.accept_snapshot("对方：周末见吗？\n我：周六可以。"), "same")
        self.assertEqual(self.watch.accept_snapshot("对方：上周的项目怎么样？\n对方：周末见吗？\n我：周六可以。"), "same")
        self.assertEqual(self.watch.pending_text, "")
        self.assertTrue(self.watch.active)

    def test_repeated_real_message_lines_are_not_globally_deduplicated(self):
        self.start("我：明天十点见。\n对方：好的。")
        self.assertEqual(self.watch.accept_snapshot("我：明天十点见。\n对方：好的。\n对方：好的。"), "changed")

    def test_discontinuous_messages_pause_before_replacing_pending(self):
        self.start()
        self.watch.accept_snapshot("对方：周末见吗？")
        original_generation = self.watch.generation
        self.assertEqual(self.watch.accept_snapshot("另一段完全不同的项目聊天。"), "discontinuous")
        self.assertFalse(self.watch.active)
        self.assertGreater(self.watch.generation, original_generation)
        self.assertIn("不连续", self.watch.reason)
        self.assertEqual(self.watch.pending_text, "对方：周末见吗？")

    def test_shared_timestamps_do_not_establish_continuity(self):
        self.start("昨天\n12:30\n对方：周末见吗？")
        self.assertEqual(self.watch.accept_snapshot("昨天\n12:30\n另一段项目聊天。"), "discontinuous")

    def test_acknowledging_older_result_keeps_newer_pending_snapshot(self):
        self.start()
        self.watch.accept_snapshot("对方：周末见吗？")
        newer = "对方：周末见吗？\n对方：中午怎么样？"
        self.watch.accept_snapshot(newer)
        self.watch.acknowledge("对方：周末见吗？")
        self.assertEqual(self.watch.pending_text, newer)
        self.watch.acknowledge(HEADER + newer)
        self.assertEqual(self.watch.pending_text, "")

    def test_restart_resets_old_deduplication_and_pending_text(self):
        self.start()
        self.watch.accept_snapshot("对方：周末见吗？")
        self.watch.stop()
        self.watch.start("朋友", self.window)
        self.assertEqual(self.watch.pending_text, "")
        self.assertEqual(self.watch.accept_snapshot("对方：周末见吗？"), "changed")

    def test_invalid_configuration_is_rejected_without_changing_active_binding(self):
        self.start("正常聊天内容")
        generation = self.watch.generation
        for profile, window, baseline, now in (("", self.window, "", 100), ("朋友", SimpleNamespace(handle=0, pid=456, title="微信"), "", 100), ("朋友", self.window, "x" * (MAX_SNAPSHOT_CHARS + 1), 100), ("朋友", self.window, "", math.nan)):
            with self.subTest(profile=profile, baseline_length=len(baseline)), self.assertRaises(ValueError):
                self.watch.start(profile, window, baseline, now=now)
        self.assertTrue(self.watch.active)
        self.assertEqual(self.watch.profile, "朋友")
        self.assertEqual(self.watch.generation, generation)
        for interval in (True, 0, 4, 60, "10", None):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                self.watch.interval = interval
        self.assertEqual(self.watch.interval, 10)

    def test_empty_invalid_and_oversized_snapshots_are_rejected_without_mutation(self):
        self.start("对方：周末见吗？")
        for text in (None, "", HEADER, "x" * (MAX_SNAPSHOT_CHARS + 1)):
            with self.subTest(kind=type(text)), self.assertRaises(ValueError):
                self.watch.accept_snapshot(text)
        self.assertTrue(self.watch.active)
        self.assertEqual(self.watch.pending_text, "")
        self.assertEqual(self.watch.accept_snapshot("对方：周末见吗？"), "same")

    def test_invalid_clock_and_inactive_operations_do_not_schedule_work(self):
        for value in (True, None, math.inf, math.nan, "100"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.watch.due(value)
        self.watch.mark_check(100)
        self.assertIsNone(self.watch.next_check_at)
        self.assertEqual(self.watch.accept_snapshot("新内容"), "same")
        self.assertEqual(normalize_snapshot("普通说明\n【引用文字】"), "普通说明\n【引用文字】")


if __name__ == "__main__":
    unittest.main()
