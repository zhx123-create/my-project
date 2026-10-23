import base64
import io
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from PIL import Image
from windows_capture import CaptureError, Window, WindowsAPI, capture_window, crop_box, image_payload


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
