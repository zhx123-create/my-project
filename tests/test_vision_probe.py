import base64
import io
import json
import unittest
from unittest.mock import patch
import urllib.error

from PIL import Image

from server import Config, ServiceError, test_connection as fetch_models
from vision_probe import check_text, check_vision


class VisionProbeTests(unittest.TestCase):
    def setUp(self):
        self.config = Config('test-only-key', 'http://192.0.2.10/v1', 'custom-model', True)
        self.code = 'A3B5C7'

    def response(self, code):
        result = {'understanding': '识字测试', 'replies': [{'label': '确认', 'text': '识别完成。'}], 'screenshot_text': code}
        return io.BytesIO(json.dumps({'choices': [{'message': {'content': json.dumps(result)}}]}).encode())

    def test_text_probe_uses_actual_generation_contract_without_images_or_history(self):
        with patch('server.urllib.request.OpenerDirector.open', return_value=self.response('')) as opened:
            self.assertTrue(check_text(self.config))
        opened.assert_called_once()
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, self.config.base + '/chat/completions')
        self.assertEqual(request.get_method(), 'POST')
        payload = json.loads(request.data)
        self.assertEqual(payload['model'], self.config.model)
        self.assertEqual(payload['response_format'], {'type': 'json_object'})
        self.assertIs(payload['stream'], False)
        content = payload['messages'][1]['content']
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]['type'], 'text')
        data = json.loads(content[0]['text'])
        self.assertIn('虚构', data['我的回复意图'])
        self.assertEqual(data['已记录的历史'], '')
        self.assertEqual(data['用户填写的背景'], '')

    def test_text_probe_does_not_pass_for_models_list_or_invalid_reply_structure(self):
        for body in ({'data': [{'id': self.config.model}]}, {'choices': [{'message': {'content': '{"replies": []}'}}]}):
            with self.subTest(body=body), patch('server.urllib.request.OpenerDirector.open', return_value=io.BytesIO(json.dumps(body).encode())):
                with self.assertRaises(ServiceError):
                    check_text(self.config)

    def test_text_probe_propagates_access_failure_without_retry(self):
        error = urllib.error.HTTPError(self.config.base, 403, 'private provider response', {}, None)
        with patch('server.urllib.request.OpenerDirector.open', side_effect=error) as opened:
            with self.assertRaisesRegex(ServiceError, '文字生成.*HTTP 403') as raised:
                check_text(self.config)
        opened.assert_called_once()
        self.assertNotIn('private provider', str(raised.exception))

    def test_actual_image_request_contains_no_answer_or_private_chat_in_text(self):
        with patch('vision_probe.secrets.choice', side_effect=self.code), patch('server.urllib.request.OpenerDirector.open', return_value=self.response(self.code)) as opened:
            self.assertTrue(check_vision(self.config))
        opened.assert_called_once()
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, self.config.base + '/chat/completions')
        payload = json.loads(request.data)
        self.assertEqual(payload['model'], self.config.model)
        messages = payload['messages']
        text = messages[0]['content'] + messages[1]['content'][0]['text']
        self.assertNotIn(self.code, text, 'the answer must be visible only inside the image')
        self.assertEqual(len(messages[1]['content']), 2)
        url = messages[1]['content'][1]['image_url']['url']
        with Image.open(io.BytesIO(base64.b64decode(url.split(',', 1)[1]))) as image:
            self.assertEqual(image.size, (360, 140))
        self.assertEqual(json.loads(messages[1]['content'][0]['text'])['聊天上下文'], '')

    def test_wrong_reading_does_not_pass_even_when_generation_succeeds(self):
        with patch('vision_probe.secrets.choice', side_effect=self.code), patch('server.urllib.request.OpenerDirector.open', return_value=self.response('ZZZZZZ')):
            with self.assertRaisesRegex(ServiceError, '没有正确读出'):
                check_vision(self.config)

    def test_models_can_pass_while_image_request_is_rejected(self):
        def respond(request, timeout):
            if request.get_method() == 'GET':
                return io.BytesIO(json.dumps({'data': [{'id': self.config.model}]}).encode())
            raise urllib.error.HTTPError(request.full_url, 400, 'private provider response', {}, None)
        with patch('server.urllib.request.OpenerDirector.open', side_effect=respond):
            self.assertEqual(fetch_models(self.config), [self.config.model])
            with self.assertRaises(ServiceError) as raised:
                check_vision(self.config)
        self.assertIn('image_url', str(raised.exception))
        self.assertNotIn('private provider', str(raised.exception))

    def test_picture_test_keeps_explicit_http_opt_in(self):
        config = Config('test-only-key', self.config.base, self.config.model)
        with patch('server.urllib.request.build_opener') as opener:
            with self.assertRaisesRegex(ServiceError, '允许此 HTTP 地址'):
                check_vision(config)
        opener.assert_not_called()


if __name__ == '__main__':
    unittest.main()
