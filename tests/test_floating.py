import gc
import ctypes
import os
import queue
from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from floating_window import FloatingCard, FloatingProgress, NonActivatingWindow, card_position
from wechat_presence import WeChatPresence
from windows_capture import Window


class ProgressTests(unittest.TestCase):
    def test_wait_time_includes_read_generation_and_fill_and_freezes_at_completion(self):
        subject = FloatingProgress()
        self.assertEqual(subject.view(100)['seconds'], 0)
        subject.begin(100)
        self.assertEqual(subject.view(103)['title'], '正在读取聊天')
        subject.advance('generating', 104)
        self.assertEqual(subject.view(112)['seconds'], 12)
        self.assertEqual(subject.view(112)['action'], '取消')
        subject.advance('filling', 113)
        subject.advance('filled', 115)
        self.assertEqual(subject.view(300)['seconds'], 15)
        self.assertEqual(subject.view(300)['title'], '回复已填入')
        subject.reset()
        self.assertEqual(subject.view(301)['seconds'], 0)
        self.assertEqual(subject.view(301)['action'], '生成')

    def test_error_and_cancellation_keep_elapsed_time_and_support_retry(self):
        for phase in ('error', 'cancelled', 'submitted'):
            subject = FloatingProgress()
            subject.begin(10)
            subject.advance(phase, 17)
            self.assertEqual(subject.view(100)['seconds'], 7)
            self.assertFalse(subject.view(100)['active'])
        self.assertEqual(FloatingProgress('error').view(10)['action'], '再试')

    def test_position_prefers_outside_chat_and_stays_on_negative_coordinate_monitor(self):
        self.assertEqual(card_position((100, 100, 700, 900), (0, 0, 1920, 1040)), (712, 136))
        x, y = card_position((900, 50, 1920, 1040), (0, 0, 1920, 1040))
        self.assertLessEqual(x + 272, 900)
        x, y = card_position((0, 0, 1920, 1040), (0, 0, 1920, 1040))
        self.assertEqual((x, y), (1632, 12))
        x, y = card_position((-1800, 100, -900, 900), (-1920, 0, 0, 1040))
        self.assertTrue(-1920 <= x < x + 272 <= 0)
        self.assertTrue(0 <= y < y + 100 <= 1040)
        self.assertEqual(card_position((100, 100, 700, 900), (0, 0, 1920, 1040), offset=(-500, 5000)), (0, 940))


class NativeOverlayTests(unittest.TestCase):
    def test_window_styles_are_applied_only_to_our_own_nonactivating_tool_window(self):
        user = SimpleNamespace(GetWindowLongPtrW=Mock(side_effect=lambda handle, index: 0x40000 if index == -20 else 1111),
                               SetWindowLongPtrW=Mock(return_value=1111), GetAncestor=Mock(return_value=123),
                               ShowWindow=Mock(), SetWindowPos=Mock(), CallWindowProcW=Mock(return_value=0),
                               IsWindow=Mock(return_value=True), GetWindowThreadProcessId=Mock())
        user.GetWindowThreadProcessId.side_effect = lambda handle, pointer: setattr(pointer._obj, 'value', os.getpid()) or 1
        with patch('floating_window.ctypes.WinDLL', return_value=user, create=True), patch('floating_window.ctypes.WINFUNCTYPE', side_effect=ctypes.CFUNCTYPE, create=True):
            subject = NonActivatingWindow(SimpleNamespace(winfo_id=lambda: 88))
        styles = next(call.args[2] for call in user.SetWindowLongPtrW.call_args_list if call.args[1] == -20)
        self.assertEqual(styles & 0x08000080, 0x08000080)
        self.assertEqual(styles & 0x40000, 0)
        self.assertEqual(subject.callback(123, 0x21, 0, 0), 3)
        subject.close()
        user.SetWindowLongPtrW.reset_mock()
        user.GetWindowThreadProcessId.side_effect = lambda handle, pointer: setattr(pointer._obj, 'value', os.getpid() + 1) or 1
        with patch('floating_window.ctypes.WinDLL', return_value=user, create=True), patch('floating_window.ctypes.WINFUNCTYPE', side_effect=ctypes.CFUNCTYPE, create=True):
            with self.assertRaises(OSError):
                NonActivatingWindow(SimpleNamespace(winfo_id=lambda: 88))
        user.SetWindowLongPtrW.assert_not_called()

    def test_mouse_click_never_activates_and_other_messages_are_delegated(self):
        subject = object.__new__(NonActivatingWindow)
        subject.previous = 1234
        subject.user = SimpleNamespace(CallWindowProcW=Mock(return_value=99))
        self.assertEqual(subject.dispatch(12, 0x21, 1, 2), 3)
        subject.user.CallWindowProcW.assert_not_called()
        self.assertEqual(subject.dispatch(12, 0x0F, 1, 2), 99)
        subject.user.CallWindowProcW.assert_called_once_with(1234, 12, 0x0F, 1, 2)

    def test_show_uses_nonactivating_native_flags_without_tk_deiconify(self):
        subject = object.__new__(NonActivatingWindow)
        subject.handle = 123
        subject.user = SimpleNamespace(SetWindowPos=Mock(), ShowWindow=Mock(), IsWindow=Mock(return_value=True))
        subject.set_long = Mock(); subject.previous = 567
        subject.show(10, 20, 272, 100)
        subject.user.SetWindowPos.assert_called_once_with(123, -1, 10, 20, 272, 100, 0x50)
        subject.user.ShowWindow.assert_called_once_with(123, 4)
        subject.close()
        subject.set_long.assert_called_once_with(123, -4, 567)

    def test_presence_only_reads_foreground_metadata_and_never_enumerates_or_reads_chats(self):
        events = queue.Queue(maxsize=1)
        subject = WeChatPresence(events)
        subject.stop_event = SimpleNamespace(is_set=Mock(side_effect=[False, True]), wait=Mock())
        window = Window(123, 456, '小林')
        api = SimpleNamespace(user=SimpleNamespace(GetForegroundWindow=Mock(return_value=123), IsIconic=Mock(return_value=False)), identify=Mock(return_value=window), bounds=Mock(return_value=(100, 100, 900, 900)))
        with patch('wechat_presence.VisibleWindowsAPI', return_value=api), patch('wechat_presence.work_area', return_value=(0, 0, 1920, 1040)):
            subject.run()
        self.assertEqual(events.get_nowait(), (window, (100, 100, 900, 900), (0, 0, 1920, 1040)))
        api.identify.assert_called_once_with(123)

    def test_presence_queue_keeps_only_latest_window_and_pause_does_not_query_any_app(self):
        events = queue.Queue(maxsize=1)
        subject = WeChatPresence(events)
        subject.put_latest('old'); subject.put_latest('new')
        self.assertEqual(events.get_nowait(), 'new')
        subject.set_enabled(False)
        subject.stop_event = SimpleNamespace(is_set=Mock(side_effect=[False, True]), wait=Mock())
        api = SimpleNamespace(user=SimpleNamespace(GetForegroundWindow=Mock()), identify=Mock())
        with patch('wechat_presence.VisibleWindowsAPI', return_value=api):
            subject.run()
        api.user.GetForegroundWindow.assert_not_called(); api.identify.assert_not_called()

    def test_presence_recovers_when_window_closes_between_metadata_calls(self):
        events = queue.Queue(maxsize=1)
        subject = WeChatPresence(events)
        subject.stop_event = SimpleNamespace(is_set=Mock(side_effect=[False, False, True]), wait=Mock())
        window = Window(123, 456, '小林')
        api = SimpleNamespace(user=SimpleNamespace(GetForegroundWindow=Mock(return_value=123), IsIconic=Mock(return_value=False)),
                              identify=Mock(return_value=window), bounds=Mock(side_effect=[OSError('closed'), (100, 100, 700, 900)]))
        with patch('wechat_presence.VisibleWindowsAPI', return_value=api), patch('wechat_presence.work_area', return_value=(0, 0, 1920, 1040)):
            subject.run()
        self.assertEqual(events.get_nowait()[0], window)
        self.assertEqual(api.identify.call_count, 2)


@unittest.skipUnless(os.environ.get('DISPLAY') or os.name == 'nt', 'requires a desktop display')
class FloatingCardTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.action, self.dismiss, self.details = Mock(), Mock(), Mock()
        self.card = FloatingCard(self.root, self.action, self.dismiss, self.details)

    def tearDown(self):
        self.card.close(); self.root.destroy()
        del self.card, self.root
        gc.collect()

    def click(self, x, y):
        x, y = round(x * self.card.scale), round(y * self.card.scale)
        event = SimpleNamespace(x=x, y=y, x_root=x + 100, y_root=y + 100)
        self.assertEqual(self.card.pointer(self.card.press, event), 'break')
        self.assertEqual(self.card.pointer(self.card.release, event), 'break')

    def test_canvas_actions_do_not_use_focus_or_clipboard_and_error_details_are_available(self):
        self.root.clipboard_clear(); self.root.clipboard_append('keep-original')
        self.card.present((123, 456, '小林'), (100, 100, 700, 900), (0, 0, 1920, 1040), FloatingProgress().view(0), 0)
        self.root.update()
        self.click(220, 72)
        self.action.assert_called_once()
        subject = FloatingProgress(); subject.begin(0); subject.advance('error', 9)
        self.card.render(subject.view(100), 100)
        self.click(50, 74)
        self.details.assert_called_once()
        self.click(256, 17)
        self.dismiss.assert_called_once()
        self.assertEqual(self.root.clipboard_get(), 'keep-original')

    def test_same_target_timer_redraw_does_not_remap_or_reactivate_the_window(self):
        native = Mock()
        original = self.card.native
        self.card.native = native
        status = FloatingProgress(); status.begin(100)
        scope = ((123, 456, '小林'), (100, 100, 700, 900), (0, 0, 1920, 1040))
        self.card.present(*scope, status.view(101), 101)
        self.card.present(*scope, status.view(112), 112)
        native.show.assert_called_once()
        self.assertEqual(self.card.view['seconds'], 12)
        self.card.native = original


if __name__ == '__main__':
    unittest.main()
