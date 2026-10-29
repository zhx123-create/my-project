import base64
import ctypes
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import desktop_settings as settings
from desktop_settings import DesktopSettings


class DesktopSettingsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="jiehua-settings-test-")
        self.path = Path(self.folder.name) / "settings.json"

    def tearDown(self):
        self.folder.cleanup()

    def test_in_memory_defaults_and_copies_have_no_filesystem_effect(self):
        subject = DesktopSettings()
        values = subject.values()
        self.assertEqual(values["base"], "https://api.openai.com/v1")
        self.assertEqual(values["model"], "gpt-4.1-mini")
        self.assertFalse(values["auto_copy"])
        self.assertEqual(values["watch_interval"], 10)
        self.assertEqual(subject.api_key(), "")
        values["auto_copy"] = True
        self.assertFalse(subject.values()["auto_copy"])
        subject.update(auto_copy=True, watch_interval=20)
        self.assertTrue(subject.values()["auto_copy"])
        self.assertEqual(list(Path(self.folder.name).iterdir()), [])

    def test_settings_persist_and_constructor_does_not_create_missing_file(self):
        subject = DesktopSettings(self.path)
        self.assertFalse(self.path.exists())
        subject.update(auto_copy=True, watch_interval=5, last_profile="朋友")
        restored = DesktopSettings(self.path)
        self.assertEqual(restored.values(), subject.values())
        self.assertEqual(restored.api_key(), "")
        if settings.os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_output_limit_persists_and_old_settings_get_a_usable_default(self):
        subject = DesktopSettings(self.path)
        subject.set_api('https://api.example.com/v1', 'model', '', False, max_output_tokens=8192)
        self.assertEqual(DesktopSettings(self.path).values()['max_output_tokens'], 8192)
        raw = self.path.read_bytes()
        for limit in (True, 0, 16385, '4096'):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                subject.update(max_output_tokens=limit)
            self.assertEqual(self.path.read_bytes(), raw)
        legacy = json.loads(raw)
        legacy['values'].pop('max_output_tokens')
        self.path.write_text(json.dumps(legacy), encoding='utf-8')
        self.assertEqual(DesktopSettings(self.path).values()['max_output_tokens'], 4096)

    def test_plaintext_key_is_never_saved_when_remember_is_off(self):
        subject = DesktopSettings(self.path)
        subject.set_api("https://api.example.com/v1/", "custom-model", "private-test-key", False)
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("private-test-key", raw)
        self.assertNotIn("api_key", raw)
        self.assertEqual(subject.api_key(), "")
        self.assertEqual(subject.values()["base"], "https://api.example.com/v1")
        self.assertFalse(subject.values()["remember_key"])

    def test_explicit_remember_uses_dpapi_and_regular_updates_keep_ciphertext(self):
        subject = DesktopSettings(self.path)
        protected = b"opaque-current-user-ciphertext"
        with patch("desktop_settings.supports_secure_storage", return_value=True), patch("desktop_settings._dpapi", side_effect=[protected, b"private-test-key"]) as dpapi:
            subject.set_api("https://api.example.com/v1", "model", "private-test-key", True)
            subject.update(auto_copy=True)
            restored = DesktopSettings(self.path)
            self.assertEqual(restored.api_key(), "private-test-key")
        self.assertEqual(dpapi.call_args_list[0].args, (b"private-test-key",))
        self.assertEqual(dpapi.call_args_list[0].kwargs, {"protect": True})
        self.assertEqual(dpapi.call_args_list[1].args, (protected,))
        self.assertEqual(dpapi.call_args_list[1].kwargs, {"protect": False})
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("private-test-key", raw)
        self.assertNotIn("encrypted_key", restored.values())
        self.assertTrue(restored.values()["remember_key"])
        restored.set_api("https://api.example.com/v1", "model", "", False)
        self.assertFalse(restored.values()["remember_key"])
        self.assertNotIn(base64.b64encode(protected).decode(), self.path.read_text())

    def test_non_windows_never_decrypts_and_cannot_enable_remember(self):
        subject = DesktopSettings(self.path)
        before = subject.values()
        with patch("desktop_settings.supports_secure_storage", return_value=False), patch("desktop_settings._dpapi") as dpapi:
            with self.assertRaisesRegex(ValueError, "Windows"):
                subject.set_api("https://api.example.com/v1", "model", "private-key", True)
            subject._encrypted_key = base64.b64encode(b"ciphertext").decode()
            self.assertEqual(subject.api_key(), "")
            dpapi.assert_not_called()
        subject._encrypted_key = ""
        self.assertEqual(subject.values(), before)
        self.assertFalse(self.path.exists())

    def test_bad_updates_are_rejected_without_changing_current_or_saved_settings(self):
        subject = DesktopSettings(self.path)
        subject.update(auto_copy=True)
        raw, previous = self.path.read_bytes(), subject.values()
        for fields in ({"watch_interval": True}, {"watch_interval": 0}, {"watch_interval": 7}, {"auto_copy": "yes"},
                       {"last_profile": "x" * 101}, {"model": ""}, {"base": "http://api.example.com/v1"},
                       {"base": "https://user:pass@example.com/v1"}, {"base": "https://example.com/v1?key=x"},
                       {"base": "https://example.com:99999/v1"}, {"base": "https://a..example.com/v1"},
                       {"api_key": "never-save-this"}, {"remember_key": True}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                subject.update(**fields)
            self.assertEqual(subject.values(), previous)
            self.assertEqual(self.path.read_bytes(), raw)

    def test_corrupt_oversized_or_unrecognized_files_are_preserved(self):
        for raw in (b"not-json", b"0" * (16 * 1024 + 1), b'{"schema":"other","version":1}',
                    json.dumps({"schema": "jiehua.desktop-settings", "version": True, "values": {}}).encode(),
                    json.dumps({"schema": "jiehua.desktop-settings", "version": 1, "values": {}, "encrypted_key": "not!base64"}).encode()):
            self.path.write_bytes(raw)
            with self.subTest(size=len(raw)), self.assertRaisesRegex(ValueError, "原文件未改动"):
                DesktopSettings(self.path)
            self.assertEqual(self.path.read_bytes(), raw)

    def test_symlinks_are_rejected_before_read_and_write(self):
        destination = self.path.with_name("original.json")
        destination.write_text("original")
        try:
            self.path.symlink_to(destination)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaisesRegex(ValueError, "符号链接"):
            DesktopSettings(self.path)
        self.path.unlink()
        subject = DesktopSettings(self.path)
        self.path.symlink_to(destination)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            subject.update(auto_copy=True)
        self.assertEqual(destination.read_text(), "original")
        self.assertFalse(subject.values()["auto_copy"])

    def test_failed_save_keeps_prior_data_and_does_not_change_runtime(self):
        subject = DesktopSettings(self.path)
        subject.update(watch_interval=20)
        raw = self.path.read_bytes()
        with patch("desktop_settings.os.replace", side_effect=OSError("no permission")):
            with self.assertRaisesRegex(ValueError, "无法保存"):
                subject.update(auto_copy=True)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertFalse(subject.values()["auto_copy"])
        self.assertEqual(list(Path(self.folder.name).iterdir()), [self.path])

    def test_decryption_errors_are_redacted(self):
        subject = DesktopSettings()
        subject._encrypted_key = base64.b64encode(b"opaque").decode()
        with patch("desktop_settings.supports_secure_storage", return_value=True), patch("desktop_settings._dpapi", side_effect=ValueError("private-key")):
            with self.assertRaisesRegex(ValueError, "无法解密") as raised:
                subject.api_key()
        self.assertNotIn("private-key", str(raised.exception))

    def test_dpapi_current_user_flags_and_memory_cleanup(self):
        # Exercise the FFI boundary with a fake Windows library on any test OS.
        encrypted = (ctypes.c_ubyte * 3)(1, 2, 3)
        crypt, kernel = Mock(), Mock()
        def process(*args):
            target = args[-1]._obj
            target.cbData = 3
            target.pbData = ctypes.cast(encrypted, ctypes.POINTER(ctypes.c_ubyte))
            self.assertEqual(args[-2], 1, "must forbid UI without machine-wide storage")
            return True
        crypt.CryptProtectData.side_effect = process
        crypt.CryptUnprotectData.side_effect = process
        with patch("desktop_settings.supports_secure_storage", return_value=True), patch("desktop_settings.ctypes.WinDLL", create=True, side_effect=[crypt, kernel, crypt, kernel]):
            self.assertEqual(settings._dpapi(b"plaintext", protect=True), b"\x01\x02\x03")
            self.assertEqual(settings._dpapi(b"cipher", protect=False), b"\x01\x02\x03")
        self.assertEqual(kernel.LocalFree.call_count, 2)


if __name__ == "__main__":
    unittest.main()
