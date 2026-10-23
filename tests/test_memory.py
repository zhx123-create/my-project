import json
from pathlib import Path
import tempfile
import unittest

from conversation_memory import ConversationMemory, MAX_HISTORY_CHARS


class ConversationMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jiehua-memory-")
        self.path = Path(self.temp.name) / "memory.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_profiles_are_isolated_and_reads_are_copies(self):
        memory = ConversationMemory(self.path)
        memory.update_profile("同事", background="上次说周五交稿", relationship="同事", goal="确认进度")
        memory.add_context("同事", "对方：周五交稿可以吗？")
        memory.add_context("朋友", "对方：周末吃饭吗？")
        self.assertNotIn("交稿", memory.history_text("朋友"))
        self.assertNotIn("吃饭", memory.history_text("同事"))
        profile = memory.profile("同事")
        profile["background"] = "改动副本"
        profile["history"].clear()
        self.assertEqual(memory.profile("同事")["background"], "上次说周五交稿")
        self.assertTrue(memory.profile("同事")["history"])
        self.assertFalse(self.path.exists(), "providing a path must not itself enable saving")

    def test_full_snapshot_and_overlapping_lines_are_not_repeated(self):
        memory = ConversationMemory()
        self.assertTrue(memory.add_context("朋友", "对方：周末有空吗？\n我：周六可以。"))
        self.assertFalse(memory.add_context("朋友", "对方：周末有空吗？\n我：周六可以。"))
        self.assertTrue(memory.add_context("朋友", "对方：周末有空吗？\n我：周六可以。\n对方：一起吃饭吧。"))
        self.assertTrue(memory.add_context("朋友", "我：周六可以。\n对方：一起吃饭吧。\n对方：中午怎么样？"))
        history = memory.history_text("朋友")
        self.assertEqual(history.count("周六可以"), 1)
        self.assertEqual(history.count("一起吃饭"), 1)
        self.assertIn("中午怎么样", history)

    def test_only_explicit_confirmation_records_a_sent_reply(self):
        memory = ConversationMemory()
        memory.add_context("朋友", "对方：周末吃饭吗？")
        self.assertFalse(any(item["kind"] == "sent" for item in memory.profile("朋友")["history"]))
        self.assertTrue(memory.confirm_sent("朋友", "好呀，周六中午吧。"))
        self.assertFalse(memory.confirm_sent("朋友", "好呀，周六中午吧。"))
        self.assertIn("用户确认已发送（我）", memory.history_text("朋友"))
        memory.add_context("朋友", "对方：那我们定个地方？")
        memory.confirm_sent("朋友", "好呀，周六中午吧。")
        self.assertEqual(sum(item["kind"] == "sent" for item in memory.profile("朋友")["history"]), 2)

    def test_overlap_preserves_repeated_lines_within_a_snapshot(self):
        memory = ConversationMemory()
        memory.add_context("工作", "我：收到\n对方：继续\n我：收到")
        memory.add_context("工作", "我：收到\n对方：下一步")
        self.assertEqual(memory.history_text("工作").count("我：收到"), 2)
        self.assertEqual(memory.history_text("工作").count("下一步"), 1)

    def test_history_is_bounded_but_profile_background_survives(self):
        memory = ConversationMemory()
        memory.update_profile("工作", background="沟通交付进度")
        for index in range(70):
            memory.add_context("工作", f"消息{index}：" + "新内容" * 900)
        self.assertLessEqual(len(memory.history_text("工作")), MAX_HISTORY_CHARS)
        self.assertLessEqual(len(memory.profile("工作")["history"]), 60)
        self.assertIn("消息69", memory.history_text("工作"))
        self.assertNotIn("消息0：", memory.history_text("工作"))
        self.assertEqual(memory.profile("工作")["background"], "沟通交付进度")

    def test_persistence_is_opt_in_and_can_be_removed_without_losing_memory(self):
        memory = ConversationMemory(self.path)
        memory.update_profile("朋友", background="正在约周末见面", goal="确认时间")
        memory.add_context("朋友", "对方：周六有空吗？")
        self.assertFalse(self.path.exists())
        memory.set_persistent(True)
        self.assertTrue(self.path.exists())
        saved = json.loads(self.path.read_text())
        self.assertTrue(saved["persistent"])
        self.assertNotIn("key", saved)
        self.assertNotIn("images", saved)
        restored = ConversationMemory(self.path)
        self.assertTrue(restored.persistent)
        self.assertEqual(restored.profile("朋友")["goal"], "确认时间")
        self.assertIn("周六有空", restored.history_text("朋友"))
        restored.set_persistent(False)
        self.assertFalse(self.path.exists())
        self.assertIn("朋友", restored.names())
        self.assertEqual(ConversationMemory(self.path).names(), [])

    def test_delete_and_clear_remove_persisted_content(self):
        memory = ConversationMemory(self.path)
        memory.set_persistent(True)
        memory.update_profile("会话A", background="背景保留")
        memory.add_context("会话A", "需要删除的聊天内容")
        memory.clear_history("会话A")
        self.assertEqual(memory.history_text("会话A"), "")
        self.assertNotIn("需要删除的聊天内容", self.path.read_text())
        self.assertEqual(memory.profile("会话A")["background"], "背景保留")
        memory.delete("会话A")
        self.assertNotIn("会话A", self.path.read_text())
        self.assertEqual(ConversationMemory(self.path).names(), [])

    def test_invalid_inputs_and_corrupt_file_do_not_overwrite_existing_data(self):
        memory = ConversationMemory()
        for name in ("", " " * 5, "名" * 101):
            with self.assertRaises(ValueError):
                memory.profile(name)
        with self.assertRaises(ValueError):
            memory.update_profile("会话", background="字" * 3001)
        with self.assertRaises(ValueError):
            memory.set_persistent(True)
        self.path.write_text("not valid JSON")
        with self.assertRaises(ValueError):
            ConversationMemory(self.path)
        self.assertEqual(self.path.read_text(), "not valid JSON")

    def test_disabled_saved_opt_in_does_not_load_old_profiles(self):
        self.path.write_text(json.dumps({"schema": "jiehua.conversation-memory", "version": 1, "persistent": False, "profiles": [{"name": "不会加载"}]}))
        memory = ConversationMemory(self.path)
        self.assertFalse(memory.persistent)
        self.assertEqual(memory.names(), [])

    def test_oversized_single_snapshot_keeps_recent_end(self):
        memory = ConversationMemory()
        memory.add_context("会话", "旧" * 19000 + "最新一条")
        self.assertLessEqual(len(memory.history_text("会话")), MAX_HISTORY_CHARS)
        self.assertTrue(memory.history_text("会话").endswith("最新一条"))
