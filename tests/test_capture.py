import base64
import io
import json
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from windows_capture import CaptureError, Window, WindowsAPI, _capture_cli, capture_window, capture_window_isolated, crop_box, image_payload


class CaptureTests(unittest.TestCase):
    def api(self, window=None, minimized=False):
        return SimpleNamespace(identify=Mock(return_value=window), user=SimpleNamespace(IsIconic=Mock(return_value=minimized)))

    def test_reused_or_foreign_window_is_never_captured(self):
        selected = Window(123, 456, '微信')
        for actual in (None, Window(123, 999, 'another process')):
            grab = Mock()
            with self.assertRaises(CaptureError):
                capture_window(selected, api=self.api(actual), grab=grab)
            grab.assert_not_called()

    def test_minimized_window_is_not_captured(self):
        window, grab = Window(123, 456, '微信'), Mock()
        with self.assertRaises(CaptureError):
            capture_window(window, api=self.api(window, True), grab=grab)
        grab.assert_not_called()

    def test_only_selected_window_handle_is_passed_to_capture(self):
        window = Window(123, 456, '微信')
        image = Image.new('RGB', (300, 300), 'white')
        image.putpixel((10, 10), (1, 2, 3))
        grab = Mock(return_value=image)
        result = capture_window(window, api=self.api(window), grab=grab)
        grab.assert_called_once_with(window=123)
        self.assertEqual(result.size, (300, 300))

    def test_failure_never_falls_back_to_full_desktop(self):
        window = Window(123, 456, '微信')
        grab = Mock(side_effect=OSError('native failure'))
        with self.assertRaises(CaptureError):
            capture_window(window, api=self.api(window), grab=grab)
        grab.assert_called_once_with(window=123)

    def test_blank_capture_is_rejected(self):
        window = Window(123, 456, '微信')
        with self.assertRaises(CaptureError):
            capture_window(window, api=self.api(window), grab=Mock(return_value=Image.new('RGB', (200, 200))))

    def test_crop_reverse_drag_and_boundary_clamping(self):
        self.assertEqual(crop_box((80, 90), (-10, -10), (100, 100), (1000, 1000)), (0, 0, 800, 900))
        with self.assertRaises(CaptureError):
            crop_box((2, 2), (5, 5), (100, 100), (1000, 1000))

    def test_encoded_crop_excludes_other_pixels_and_is_bounded(self):
        image = Image.new('RGB', (4000, 2000), 'red')
        image.paste('blue', (2000, 0, 4000, 2000))
        payload = image_payload(image.crop((2000, 0, 4000, 2000)))
        with Image.open(io.BytesIO(base64.b64decode(payload.split(',')[1]))) as result:
            self.assertEqual(result.size, (2000, 2000))
            self.assertLess(result.getpixel((0, 0))[0], 5)
            self.assertGreater(result.getpixel((0, 0))[2], 250)
        reduced = image_payload(Image.new('RGB', (4000, 2000), 'white'))
        with Image.open(io.BytesIO(base64.b64decode(reduced.split(',')[1]))) as result:
            self.assertEqual(result.size, (2000, 1000))

    @unittest.skipIf(sys.platform == 'win32', 'non-Windows diagnostic only')
    def test_non_windows_reports_limitation(self):
        with self.assertRaisesRegex(CaptureError, 'Windows'):
            WindowsAPI()


class IsolatedCaptureTests(unittest.TestCase):
    def setUp(self):
        self.window = Window(123, 456, 'private conversation title')

    def png(self, size=(320, 240)):
        buffer = io.BytesIO()
        Image.new('RGB', size, (10, 20, 30)).save(buffer, format='PNG')
        return buffer.getvalue()

    def test_success_preserves_image_size_and_passes_only_selected_identity(self):
        result = SimpleNamespace(returncode=0, stdout=self.png(), stderr=b'')
        with patch('windows_capture.subprocess.run', return_value=result) as run:
            image = capture_window_isolated(self.window)
        self.assertEqual(image.size, (320, 240))
        self.assertEqual(image.getpixel((0, 0)), (10, 20, 30))
        args, kwargs = run.call_args
        self.assertEqual(args[0][0], sys.executable)
        self.assertEqual(args[0][-3:], ['--capture-window', '123', '456'])
        self.assertNotIn(self.window.title, args[0])
        self.assertEqual(kwargs['timeout'], 8)
        self.assertTrue(kwargs['capture_output'])
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)

    def test_windows_helper_does_not_open_a_console(self):
        result = SimpleNamespace(returncode=0, stdout=self.png(), stderr=b'')
        # Path creation must use the host platform; only the flag decision is Windows.
        with patch('windows_capture.sys.platform', 'win32'), patch('windows_capture.subprocess.run', return_value=result) as run:
            capture_window_isolated(self.window, timeout=3)
        self.assertEqual(run.call_args.kwargs['creationflags'], 0x08000000)
        self.assertEqual(run.call_args.kwargs['timeout'], 3)

    def test_timeout_is_localized_and_does_not_leak_captured_output(self):
        error = subprocess.TimeoutExpired('private command', 8, output=b'private screenshot', stderr=b'private details')
        with patch('windows_capture.subprocess.run', side_effect=error):
            with self.assertRaises(CaptureError) as raised:
                capture_window_isolated(self.window)
        self.assertIn('超时', str(raised.exception))
        self.assertIn('Win+Shift+S', str(raised.exception))
        self.assertNotIn('private', str(raised.exception))

    def test_native_error_codes_remain_meaningful_without_untrusted_stderr(self):
        for code, expected in [('not_found', '重新查找'), ('minimized', '最小化'), ('blank', '空白')]:
            with self.subTest(code=code):
                stderr = json.dumps({'capture_error': code, 'message': 'private title and secret'}).encode()
                with patch('windows_capture.subprocess.run', return_value=SimpleNamespace(returncode=1, stdout=b'', stderr=stderr)):
                    with self.assertRaises(CaptureError) as raised:
                        capture_window_isolated(self.window)
                self.assertIn(expected, str(raised.exception))
                self.assertIn('Win+Shift+S', str(raised.exception))
                self.assertNotIn('private', str(raised.exception))

    def test_arbitrary_native_stderr_is_not_shown(self):
        result = SimpleNamespace(returncode=1, stdout=b'', stderr=b'private title, API_KEY=not-for-display')
        with patch('windows_capture.subprocess.run', return_value=result):
            with self.assertRaises(CaptureError) as raised:
                capture_window_isolated(self.window)
        self.assertIn('窗口采集失败', str(raised.exception))
        self.assertNotIn('API_KEY', str(raised.exception))

    def test_invalid_or_tiny_image_payload_is_rejected(self):
        for payload in (b'not a PNG: private payload', self.png((1, 1))):
            with patch('windows_capture.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=payload, stderr=b'')):
                with self.assertRaises(CaptureError) as raised:
                    capture_window_isolated(self.window)
            self.assertIn('有效截图', str(raised.exception))
            self.assertNotIn('private', str(raised.exception))

    def test_helper_launch_failure_is_localized(self):
        with patch('windows_capture.subprocess.run', side_effect=OSError('private filesystem path')):
            with self.assertRaises(CaptureError) as raised:
                capture_window_isolated(self.window)
        self.assertIn('无法启动', str(raised.exception))
        self.assertNotIn('private', str(raised.exception))

    def test_cli_error_protocol_works_with_non_utf8_windows_pipe(self):
        raw = io.BytesIO()
        stderr = io.TextIOWrapper(raw, encoding='ascii', write_through=True)
        with patch('windows_capture.sys.stderr', stderr), patch('windows_capture.capture_window', side_effect=OSError('private native details')):
            code = _capture_cli(['--capture-window', '123', '456'])
        payload = json.loads(raw.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload['capture_error'], 'capture_failed')
        self.assertIn('窗口采集失败', payload['message'])
        self.assertNotIn('private', raw.getvalue().decode())

    @unittest.skipIf(sys.platform == 'win32', 'requires no real Windows capture')
    def test_real_cli_subprocess_reports_non_windows_without_tk(self):
        with self.assertRaises(CaptureError) as raised:
            capture_window_isolated(self.window)
        self.assertIn('Windows 10/11', str(raised.exception))
