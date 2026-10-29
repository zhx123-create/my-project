import os
import ctypes
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from windows_capture import CaptureError, Window, WindowsAPI, pick_foreground_window, selected_window_state


class WindowPickerTests(unittest.TestCase):
    def test_explicit_pick_uses_only_foreground_handle_without_app_discovery(self):
        window = Window(123, os.getpid() + 1, '单独聊天')
        api = SimpleNamespace(user=SimpleNamespace(GetForegroundWindow=Mock(return_value=123)), describe=Mock(return_value=window))
        selected = pick_foreground_window(api)
        api.describe.assert_called_once_with(123)
        self.assertEqual(selected, Window(123, window.pid, window.title, True))

    def test_assistant_or_missing_foreground_is_not_accepted(self):
        for window in (None, Window(123, os.getpid(), '助手')):
            api = SimpleNamespace(user=SimpleNamespace(GetForegroundWindow=Mock(return_value=123)), describe=Mock(return_value=window))
            with self.subTest(window=window), self.assertRaises(CaptureError):
                pick_foreground_window(api)

    def test_selected_handle_is_revalidated_and_reused_pid_is_rejected(self):
        window = Window(123, 456, '聊天', True)
        api = SimpleNamespace(describe=Mock(return_value=Window(123, 456, '聊天')))
        self.assertEqual(selected_window_state(window, api), window)
        api.describe.assert_called_once_with(123)
        api.describe.return_value = Window(123, 999, '聊天')
        self.assertIsNone(selected_window_state(window, api))
        api.describe.return_value = None
        self.assertIsNone(selected_window_state(window, api))
        with self.assertRaises(CaptureError):
            selected_window_state(Window(123, 456, '聊天'), api)

    def test_automatic_discovery_does_not_read_titles_of_unrecognized_apps(self):
        from ctypes import wintypes as w
        api = object.__new__(WindowsAPI)
        def pid(handle, pointer):
            ctypes.cast(pointer, ctypes.POINTER(w.DWORD)).contents.value = 456
            return 1
        def path(process, flags, buffer, length):
            buffer.value = 'C:\\Apps\\Other.exe'
            return True
        api.user = SimpleNamespace(IsWindow=Mock(return_value=True), IsWindowVisible=Mock(return_value=True), GetWindowThreadProcessId=pid, GetWindowTextLengthW=Mock(), GetWindowTextW=Mock())
        api.kernel = SimpleNamespace(OpenProcess=Mock(return_value=7), QueryFullProcessImageNameW=path, CloseHandle=Mock())
        self.assertIsNone(api.identify(123))
        api.user.GetWindowTextLengthW.assert_not_called(); api.user.GetWindowTextW.assert_not_called()
        api.kernel.CloseHandle.assert_called_once_with(7)


if __name__ == '__main__':
    unittest.main()
