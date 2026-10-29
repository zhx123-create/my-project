import base64
import ctypes
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image, PngImagePlugin
from desktop_screenshots import ScreenshotBinding, ScreenshotReader
from visible_capture import VisibleCaptureError, VisibleWindowsAPI, _cli, capture_visible, capture_visible_isolated
from windows_capture import CaptureError, Window
from windows_ocr import OCR_HEADER, recognize_image


def picture(size=(200, 140)):
    image = Image.new('RGB', size, 'white')
    image.putpixel((10, 10), (0, 0, 0))
    return image


class VisibleRegionTests(unittest.TestCase):
    def setUp(self):
        self.window = Window(123, 456, '聊天')
        self.api = SimpleNamespace(identify=Mock(return_value=self.window), user=SimpleNamespace(IsIconic=Mock(return_value=False)), bounds=Mock(return_value=(100, 200, 500, 500)), uncovered=Mock())
        self.region = (20, 40, 220, 180)

    def test_only_bound_region_pixels_are_requested_in_screen_coordinates(self):
        grab = Mock(return_value=picture())
        result = capture_visible(self.window, self.region, (400, 300), self.api, grab)
        grab.assert_called_once_with(bbox=(120, 240, 320, 380), all_screens=True)
        self.assertEqual(result.size, (200, 140))
        self.assertEqual(self.api.uncovered.call_count, 2)

    def test_move_preserves_relative_region_but_resize_stops_before_grab(self):
        self.api.bounds.return_value = (-400, 0, 0, 300)
        grab = Mock(return_value=picture())
        capture_visible(self.window, self.region, (400, 300), self.api, grab)
        grab.assert_called_once_with(bbox=(-380, 40, -180, 180), all_screens=True)
        with self.assertRaisesRegex(CaptureError, '尺寸'):
            capture_visible(self.window, self.region, (500, 300), self.api, Mock())

    def test_minimized_closed_covered_or_bad_region_never_captures_pixels(self):
        for mode in ('minimized', 'closed', 'covered', 'region'):
            with self.subTest(mode=mode):
                self.setUp()
                grab = Mock()
                if mode == 'minimized':
                    self.api.user.IsIconic.return_value = True
                elif mode == 'closed':
                    self.api.identify.return_value = None
                elif mode == 'covered':
                    self.api.uncovered.side_effect = VisibleCaptureError('covered')
                region = (-1, 0, 100, 100) if mode == 'region' else self.region
                with self.assertRaises(CaptureError):
                    capture_visible(self.window, region, (400, 300), self.api, grab)
                grab.assert_not_called()

    def test_window_change_during_capture_discards_image(self):
        self.api.identify.side_effect = [self.window, Window(123, 999, 'replacement')]
        with self.assertRaisesRegex(CaptureError, '变化'):
            capture_visible(self.window, self.region, (400, 300), self.api, Mock(return_value=picture()))

    def test_occluding_window_rectangles_are_checked_in_z_order(self):
        api = object.__new__(VisibleWindowsAPI)
        api.user = SimpleNamespace(GetTopWindow=Mock(return_value=9), GetWindow=Mock(return_value=123), IsWindowVisible=Mock(return_value=True), IsIconic=Mock(return_value=False))
        api.bounds = Mock(return_value=(0, 0, 90, 90))
        api.uncovered(123, (100, 100, 200, 200))
        api.bounds.return_value = (150, 150, 250, 250)
        with self.assertRaisesRegex(CaptureError, '遮挡'):
            api.uncovered(123, (100, 100, 200, 200))
        api.user.GetSystemMetrics = lambda index: {76: 0, 77: 0, 78: 1000, 79: 800}[index]
        with self.assertRaisesRegex(CaptureError, '屏幕'):
            api.uncovered(123, (-10, 100, 200, 200))

    def test_maximized_window_uses_visible_frame_not_offscreen_resize_borders(self):
        from ctypes import wintypes as w
        api = object.__new__(VisibleWindowsAPI)
        def frame(handle, attribute, pointer, length):
            self.assertEqual(attribute, 9)
            rect = ctypes.cast(pointer, ctypes.POINTER(w.RECT)).contents
            rect.left, rect.top, rect.right, rect.bottom = 0, 0, 1920, 1080
            return 0
        api._cloaked = frame
        api.identify = Mock(return_value=self.window)
        api.user = SimpleNamespace(IsIconic=Mock(return_value=False), GetWindowRect=Mock(), GetTopWindow=Mock(return_value=123), GetSystemMetrics=lambda index: {76: 0, 77: 0, 78: 1920, 79: 1080}[index])
        grab = Mock(return_value=picture((1920, 1080)))
        capture_visible(self.window, api=api, grab=grab)
        grab.assert_called_once_with(bbox=(0, 0, 1920, 1080), all_screens=True)
        api.user.GetWindowRect.assert_not_called()

    def test_frame_query_failure_falls_back_to_window_rect(self):
        from ctypes import wintypes as w
        api = object.__new__(VisibleWindowsAPI)
        api._cloaked = Mock(return_value=1)
        def window_rect(handle, pointer):
            rect = ctypes.cast(pointer, ctypes.POINTER(w.RECT)).contents
            rect.left, rect.top, rect.right, rect.bottom = 100, 200, 500, 500
            return True
        api.user = SimpleNamespace(GetWindowRect=window_rect)
        self.assertEqual(api.bounds(123), (100, 200, 500, 500))

    def test_only_explicitly_selected_windows_can_use_generic_metadata_checks(self):
        selected = Window(123, 456, '手动聊天', True)
        self.api.describe = Mock(return_value=selected)
        self.api.identify.return_value = None
        grab = Mock(return_value=picture())
        with self.assertRaises(CaptureError):
            capture_visible(self.window, self.region, (400, 300), self.api, grab)
        self.api.describe.assert_not_called(); grab.assert_not_called()
        capture_visible(selected, self.region, (400, 300), self.api, grab)
        self.assertEqual(self.api.describe.call_count, 2)
        grab.assert_called_once_with(bbox=(120, 240, 320, 380), all_screens=True)

    def test_preview_masks_input_toolbar_outside_message_region_before_return(self):
        self.api.covering_rectangles = Mock(return_value=[(350, 450, 490, 490)])
        self.api.uncovered.side_effect = VisibleCaptureError('covered')
        original = picture((400, 300))
        result = capture_visible(self.window, api=self.api, grab=Mock(return_value=original), preview=True)
        self.assertEqual(result.info['capture_blocks'], [(250, 250, 390, 290)])
        self.assertEqual(result.getpixel((300, 270)), (218, 222, 230))
        self.assertEqual(result.getpixel((10, 10)), (0, 0, 0))
        self.assertEqual(original.getpixel((300, 270)), (255, 255, 255), 'mask a copy without modifying the source')
        self.api.uncovered.assert_not_called()
        self.assertEqual(self.api.covering_rectangles.call_count, 2)

    def test_preview_masks_both_positions_of_a_moving_overlay(self):
        self.api.covering_rectangles = Mock(side_effect=[[(100, 400, 200, 500)], [(400, 400, 500, 500)]])
        result = capture_visible(self.window, api=self.api, grab=Mock(return_value=picture((400, 300))), preview=True)
        self.assertEqual(result.info['capture_blocks'], [(0, 200, 100, 300), (300, 200, 400, 300)])
        for point in ((50, 250), (350, 250)):
            self.assertEqual(result.getpixel(point), (218, 222, 230))

    def test_fully_covered_preview_is_rejected(self):
        self.api.covering_rectangles = Mock(return_value=[(100, 200, 500, 500)])
        with self.assertRaisesRegex(CaptureError, '遮挡'):
            capture_visible(self.window, api=self.api, grab=Mock(return_value=picture((400, 300))), preview=True)

    def test_zero_alpha_layer_is_ignored_but_visible_layer_still_blocks(self):
        from ctypes import wintypes as w
        api = object.__new__(VisibleWindowsAPI)
        api.user = SimpleNamespace(GetTopWindow=Mock(return_value=9), GetWindow=Mock(return_value=123), IsWindowVisible=Mock(return_value=True), IsIconic=Mock(return_value=False))
        api.bounds = Mock(return_value=(0, 0, 1000, 800))
        api._window_style = Mock(return_value=0x80000)
        opacity = [0]
        def attributes(handle, color, alpha, flags):
            ctypes.cast(alpha, ctypes.POINTER(w.BYTE)).contents.value = opacity[0]
            ctypes.cast(flags, ctypes.POINTER(w.DWORD)).contents.value = 2
            return True
        api._layered_attributes = attributes
        api.uncovered(123, (100, 100, 200, 200))
        api.bounds.assert_not_called()
        opacity[0] = 128
        with self.assertRaisesRegex(CaptureError, '遮挡'):
            api.uncovered(123, (100, 100, 200, 200))

    def test_masked_preview_scope_survives_child_png_protocol(self):
        preview = picture((400, 300)); preview.info['capture_blocks'] = [(250, 250, 390, 290)]
        output = io.BytesIO()
        with patch('visible_capture.capture_visible', return_value=preview) as capture, patch('visible_capture.sys.stdout', SimpleNamespace(buffer=output)):
            self.assertEqual(_cli(['--preview-selected', '123', '456']), 0)
        self.assertTrue(capture.call_args.args[0].user_selected)
        self.assertTrue(capture.call_args.kwargs['preview'])
        response = SimpleNamespace(returncode=0, stdout=output.getvalue(), stderr=b'')
        with patch('visible_capture.subprocess.run', return_value=response) as run:
            result = capture_visible_isolated(Window(123, 456, '聊天', True), preview=True)
        self.assertIn('--preview-selected', run.call_args.args[0])
        self.assertEqual(result.info['capture_blocks'], [[250, 250, 390, 290]])

    def test_preview_without_valid_scope_metadata_is_not_accepted(self):
        for blocks in (None, '[[-1,0,20,20]]', '[[0,0,900,900]]', 'private non-json metadata'):
            stream = io.BytesIO(); metadata = PngImagePlugin.PngInfo()
            if blocks is not None:
                metadata.add_text('capture_blocks', blocks)
            picture().save(stream, format='PNG', pnginfo=metadata)
            response = SimpleNamespace(returncode=0, stdout=stream.getvalue(), stderr=b'')
            with self.subTest(blocks=blocks), patch('visible_capture.subprocess.run', return_value=response):
                with self.assertRaises(CaptureError) as raised:
                    capture_visible_isolated(self.window, preview=True)
            self.assertNotIn('private', str(raised.exception))

    def test_isolated_capture_passes_only_numeric_scope_and_bounds_time(self):
        stream = io.BytesIO(); picture().save(stream, format='PNG')
        response = SimpleNamespace(returncode=0, stdout=stream.getvalue(), stderr=b'')
        with patch('visible_capture.subprocess.run', return_value=response) as run:
            capture_visible_isolated(self.window, self.region, (400, 300))
        self.assertEqual(run.call_args.args[0][-8:], ['123', '456', '20', '40', '220', '180', '400', '300'])
        self.assertNotIn('聊天', run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs['timeout'], 8)
        with patch('visible_capture.subprocess.run', return_value=response) as selected_run:
            capture_visible_isolated(Window(123, 456, '手动聊天', True), self.region, (400, 300))
        self.assertIn('--visible-selected', selected_run.call_args.args[0])
        self.assertNotIn('手动聊天', selected_run.call_args.args[0])
        with patch('visible_capture.subprocess.run', side_effect=subprocess.TimeoutExpired('secret', 8)):
            with self.assertRaisesRegex(CaptureError, '超时'):
                capture_visible_isolated(self.window)

    @unittest.skipIf(sys.platform == 'win32', 'non-Windows protocol diagnostic')
    def test_live_helper_reports_os_limitation_without_capturing_desktop(self):
        with self.assertRaisesRegex(CaptureError, 'Windows'):
            capture_visible_isolated(self.window)


class WindowsOcrTests(unittest.TestCase):
    def test_ocr_gets_image_over_stdin_and_returns_only_bounded_text(self):
        response = SimpleNamespace(returncode=0, stdout=json.dumps({'text': '对方：周五能交稿吗？', 'count': 1}).encode(), stderr=b'')
        original = picture((4000, 2000))
        with patch('windows_ocr.sys.platform', 'win32'), patch('windows_ocr.subprocess.run', return_value=response) as run:
            result = recognize_image(original)
        self.assertEqual(result['text'], OCR_HEADER + '对方：周五能交稿吗？')
        self.assertEqual(result['source'], 'windows_ocr')
        self.assertFalse(result['identity_verified'])
        command = run.call_args.args[0]
        self.assertNotIn('-ExecutionPolicy', command)
        self.assertIn('-EncodedCommand', command)
        with Image.open(io.BytesIO(base64.b64decode(run.call_args.kwargs['input']))) as supplied:
            self.assertEqual(supplied.size, (2000, 1000))
        self.assertEqual(original.size, (4000, 2000))
        self.assertEqual(run.call_args.kwargs['timeout'], 15)

    def test_missing_language_and_private_native_errors_are_readable_and_redacted(self):
        for stderr, expected in ((b'{"ocr_error":"language"}', '语言'), (b'{"ocr_error":"runtime"}', '组件'), (b'{"ocr_error":"decode"}', '解码'), (b'secret text from a private screenshot', '识字失败')):
            response = SimpleNamespace(returncode=1, stdout=b'', stderr=stderr)
            with patch('windows_ocr.sys.platform', 'win32'), patch('windows_ocr.subprocess.run', return_value=response):
                with self.assertRaises(CaptureError) as raised:
                    recognize_image(picture())
                self.assertIn(expected, str(raised.exception))
                self.assertNotIn('secret', str(raised.exception))

    def test_no_text_timeout_and_missing_powershell_are_distinct(self):
        cases = [(SimpleNamespace(returncode=0, stdout=b'{"text":""}', stderr=b''), None, '没有识别到文字'),
                 (None, subprocess.TimeoutExpired('private command', 15), '超时'),
                 (None, OSError('private filesystem'), 'PowerShell')]
        for response, error, expected in cases:
            with self.subTest(expected=expected), patch('windows_ocr.sys.platform', 'win32'), patch('windows_ocr.subprocess.run', return_value=response, side_effect=error):
                with self.assertRaisesRegex(CaptureError, expected):
                    recognize_image(picture())

    @unittest.skipIf(sys.platform == 'win32', 'non-Windows diagnostic')
    def test_local_ocr_reports_platform_requirement(self):
        with self.assertRaisesRegex(CaptureError, 'Windows'):
            recognize_image(picture())


class ScreenshotReaderTests(unittest.TestCase):
    def test_identical_pixels_reuse_local_ocr_and_changed_pixels_are_recognized(self):
        window = Window(123, 456, '聊天')
        reader = ScreenshotReader(ScreenshotBinding(window, '会话', (0, 0, 200, 140), (400, 300)))
        first, second = picture(), picture()
        second.putpixel((20, 20), (0, 0, 0))
        result = {'text': OCR_HEADER + '对方：你好', 'count': 1, 'source': 'windows_ocr', 'identity_verified': False}
        with patch('desktop_screenshots.capture_visible_isolated', side_effect=[first, first, second]) as capture, patch('desktop_screenshots.list_windows', return_value=[window]), patch('desktop_screenshots.recognize_image', return_value=result) as ocr:
            self.assertEqual(reader.read(window), result)
            self.assertEqual(reader.read(window), result)
            self.assertEqual(reader.read(window), result)
        self.assertEqual(capture.call_count, 3)
        self.assertEqual(ocr.call_count, 2)

    def test_changed_title_or_profile_is_not_accepted_as_the_bound_chat(self):
        window = Window(123, 456, '聊天')
        reader = ScreenshotReader(ScreenshotBinding(window, '会话', (0, 0, 200, 140), (400, 300)))
        self.assertFalse(reader.matches('另一个人', window))
        with patch('desktop_screenshots.capture_visible_isolated', return_value=picture()), patch('desktop_screenshots.list_windows', return_value=[Window(123, 456, '另一聊天')]), patch('desktop_screenshots.recognize_image') as ocr:
            with self.assertRaises(CaptureError):
                reader.read(window)
            ocr.assert_not_called()


if __name__ == '__main__':
    unittest.main()
