import io
import errno
import http.client
import json
import os
from pathlib import Path
import ssl
import socket
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from desktop_settings import DesktopSettings
import server


class ApiTransportTests(unittest.TestCase):
    def setUp(self):
        self.base = 'http://192.0.2.10/v1'
        self.config = server.Config('test-only-key', self.base, 'GLM-5.3-Flash', True)

    def test_http_requires_explicit_opt_in_before_any_request(self):
        with patch('server.urllib.request.build_opener') as opener:
            for action in (lambda config: server.generate(config, [], reply_count=1), server.test_connection):
                with self.subTest(action=action), self.assertRaisesRegex(server.ServiceError, '允许此 HTTP 地址'):
                    action(server.Config('test-only-key', self.base, 'GLM-5.3-Flash'))
            opener.assert_not_called()

    def test_opted_in_http_preserves_model_id_and_exact_request_destination(self):
        replies = [{'label': '自然', 'text': '好的，我确认一下。'}]
        result = {'choices': [{'message': {'content': json.dumps({'replies': replies})}}]}
        with patch('server.urllib.request.OpenerDirector.open', return_value=io.BytesIO(json.dumps(result).encode())) as opened:
            self.assertEqual(server.generate(self.config, [], reply_count=1), replies)
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, self.base + '/chat/completions')
        self.assertEqual(request.get_header('Authorization'), 'Bearer test-only-key')
        self.assertEqual(json.loads(request.data)['model'], 'GLM-5.3-Flash')

    def test_invalid_addresses_and_full_endpoint_suffix_are_rejected(self):
        for base in ('ftp://example.com/v1', 'http://user:secret@example.com/v1',
                     'http://example.com/v1?api_key=secret', 'http://example.com/v1#x',
                     'http://example.com:99999/v1', 'http://a..example.com/v1',
                     'http://example.com/v1/chat/completions', 'http://example.com/v1/models',
                     'http://exam\nple.com/v1'):
            with self.subTest(base=base), self.assertRaises(server.InputError):
                server.normalize_api_base(base, True)
        for flag in ('yes', 1, None):
            with self.subTest(flag=flag), self.assertRaises(server.InputError):
                server.normalize_api_base(self.base, flag)
        self.assertEqual(server.normalize_api_base(self.base + '/\u00a0', True), self.base)

    def test_https_verification_is_not_disabled_and_redirects_never_carry_key(self):
        opener = server._api_opener()
        handler = next(handler for handler in opener.handlers if isinstance(handler, urllib.request.HTTPRedirectHandler))
        request = urllib.request.Request(self.base + '/models', headers={'Authorization': 'Bearer test-only-key'})
        for target in (self.base + '/other', 'https://other.example/models', 'http://other.example/models'):
            self.assertIsNone(handler.redirect_request(request, None, 302, '', {}, target))
        config = server.Config('test-only-key')
        with patch('server.urllib.request.OpenerDirector.open', side_effect=urllib.error.URLError(ssl.SSLCertVerificationError('certificate rejected'))):
            with self.assertRaisesRegex(server.ServiceError, '证书'):
                server.test_connection(config)

    def test_connection_probe_sends_only_get_models_not_chat(self):
        body = {'data': [{'id': 'glm-5.3-flash'}, {'id': 'glm-5.3-flash'}, {'id': 'other-model'}, {'id': 'bad\nmodel'}]}
        with patch('server.urllib.request.OpenerDirector.open', return_value=io.BytesIO(json.dumps(body).encode())) as opened:
            result = server.test_connection(self.config)
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, self.base + '/models')
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)
        self.assertEqual(result, ['glm-5.3-flash', 'other-model'])

    def test_probe_failures_do_not_expose_key_or_response_content(self):
        errors = [urllib.error.HTTPError(self.base, 401, 'secret-key-in-body', {}, None),
                  urllib.error.HTTPError(self.base, 404, 'secret-key-in-body', {}, None),
                  urllib.error.URLError('secret-key-in-body')]
        for error in errors:
            with self.subTest(error=type(error)), patch('server.urllib.request.OpenerDirector.open', side_effect=error):
                with self.assertRaises(server.ServiceError) as raised:
                    server.test_connection(self.config)
                self.assertNotIn('secret-key', str(raised.exception))
        with patch('server.urllib.request.OpenerDirector.open', return_value=io.BytesIO(b'not-json-secret')):
            with self.assertRaisesRegex(server.ServiceError, '模型列表'):
                server.test_connection(self.config)

    def test_network_reasons_are_distinct_and_do_not_echo_private_error_details(self):
        cases = [
            (TimeoutError('private-details'), '超时'),
            (socket.gaierror(-2, 'private-details'), 'DNS'),
            (ConnectionRefusedError(errno.ECONNREFUSED, 'private-details'), '拒绝'),
            (ConnectionResetError(errno.ECONNRESET, 'private-details'), '断开'),
            (ssl.SSLCertVerificationError('private-details'), '证书'),
            (ssl.SSLError('private-details'), '安全连接'),
            (OSError(errno.ENETUNREACH, 'private-details'), '无法到达'),
            (http.client.RemoteDisconnected('private-details'), '断开'),
            (http.client.IncompleteRead(b'private-details', 42), '未完整接收'),
            (urllib.error.URLError('private-details'), '暂时无法连接'),
        ]
        for reason, expected in cases:
            for wrapped in (reason, urllib.error.URLError(reason)):
                for action in (server.test_connection, lambda config: server.generate(config, [], reply_count=1)):
                    with self.subTest(reason=type(reason).__name__, wrapped=isinstance(wrapped, urllib.error.URLError), action=action), patch('server.urllib.request.OpenerDirector.open', side_effect=wrapped) as opened:
                        with self.assertRaises(server.ServiceError) as raised:
                            action(self.config)
                    self.assertIn(expected, str(raised.exception))
                    self.assertNotIn('private-details', str(raised.exception))
                    self.assertNotIn('test-only-key', str(raised.exception))
                    opened.assert_called_once()

    def test_windows_socket_codes_are_classified_without_localized_error_strings(self):
        for code, expected in ((10060, '超时'), (10061, '拒绝'), (10054, '断开'), (10065, '无法到达')):
            reason = OSError('private-details')
            reason.winerror = code
            with self.subTest(code=code), patch('server.urllib.request.OpenerDirector.open', side_effect=urllib.error.URLError(reason)):
                with self.assertRaises(server.ServiceError) as raised:
                    server.generate(self.config, [], reply_count=1)
            self.assertIn(expected, str(raised.exception))
            self.assertNotIn('private-details', str(raised.exception))

    def test_gateway_error_includes_status_and_is_never_automatically_retried(self):
        for code in (408, 413, 500, 502, 503, 504):
            error = urllib.error.HTTPError(self.base, code, 'private-details', {}, None)
            with self.subTest(code=code), patch('server.urllib.request.OpenerDirector.open', side_effect=error) as opened:
                with self.assertRaises(server.ServiceError) as raised:
                    server.generate(self.config, [], reply_count=1)
            self.assertIn(str(code), str(raised.exception))
            self.assertNotIn('private-details', str(raised.exception))
            opened.assert_called_once()

    def test_forbidden_errors_identify_request_type_without_claiming_a_cause(self):
        image_messages = [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'private-image'}}]}]
        cases = [
            (lambda: server.generate(self.config, [], reply_count=1), '文字生成'),
            (lambda: server.generate(self.config, [], reply_count=1, include_analysis=True, transcribe_images=True), '看图'),
            (lambda: server.generate(self.config, image_messages, reply_count=1), '看图'),
            (lambda: server.test_connection(self.config), '模型列表'),
        ]
        for action, request_type in cases:
            error = urllib.error.HTTPError(self.base, 403, 'private-provider-details', {'X-Debug': 'test-only-key'}, io.BytesIO(b'private-chat'))
            with self.subTest(request_type=request_type), patch('server.urllib.request.OpenerDirector.open', side_effect=error) as opened:
                with self.assertRaises(server.ServiceError) as raised:
                    action()
            message = str(raised.exception)
            self.assertIn(request_type, message)
            self.assertIn('HTTP 403', message)
            self.assertIn('中间代理', message)
            for private in ('private-', 'test-only-key', self.base):
                self.assertNotIn(private, message)
            if request_type == '看图':
                self.assertIn('本机识字并分析', message)
                self.assertIn('image_url', message)
            opened.assert_called_once()

    def test_an_intermittent_403_does_not_disable_later_manual_generation(self):
        replies = [{'label': '确认', 'text': '收到。'}]
        response = json.dumps({'choices': [{'message': {'content': json.dumps({'replies': replies})}}]}).encode()
        error = urllib.error.HTTPError(self.base, 403, 'private-provider-details', {}, None)
        with patch('server.urllib.request.OpenerDirector.open', side_effect=[io.BytesIO(response), error, io.BytesIO(response)]) as opened:
            self.assertEqual(server.generate(self.config, [], reply_count=1), replies)
            with self.assertRaisesRegex(server.ServiceError, 'HTTP 403'):
                server.generate(self.config, [], reply_count=1)
            self.assertEqual(server.generate(self.config, [], reply_count=1), replies)
        self.assertEqual(opened.call_count, 3, 'each explicit action sends exactly one request')

    def test_settings_http_opt_in_is_paired_with_its_address(self):
        settings = DesktopSettings()
        with self.assertRaises(ValueError):
            settings.set_api(self.base, 'GLM-5.3-Flash', 'test-only-key', False)
        settings.set_api(self.base, 'GLM-5.3-Flash', 'test-only-key', False, allow_http=True)
        self.assertTrue(settings.values()['allow_http'])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            saved = DesktopSettings(path)
            saved.set_api(self.base, 'GLM-5.3-Flash', 'test-only-key', False, allow_http=True)
            self.assertTrue(DesktopSettings(path).values()['allow_http'])
            self.assertNotIn('test-only-key', path.read_text())
        previous = settings.values()
        with self.assertRaises(ValueError):
            settings.update(base='http://other.example/v1')
        self.assertEqual(settings.values(), previous)
        settings.update(base='https://secure.example/v1')
        self.assertFalse(settings.values()['allow_http'])

    def test_environment_http_opt_in_is_explicit(self):
        for flag, expected in (('0', False), ('true', False), ('1', True)):
            with patch.dict(os.environ, {'CHAT_API_BASE': self.base, 'CHAT_ALLOW_HTTP': flag}):
                self.assertEqual(server.Config.from_env().allow_http, expected)


if __name__ == '__main__':
    unittest.main()
