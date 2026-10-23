import base64
import io
import json
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import server


class PromptTests(unittest.TestCase):
    def test_context_intent_and_screenshot_are_sent(self):
        image = 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\nimage').decode()
        messages = server.make_messages({'context': '对方：周末见吗？', 'intent': '委婉拒绝', 'images': [image]})
        data = json.loads(messages[1]['content'][0]['text'])
        self.assertEqual(data['我的回复意图'], '委婉拒绝')
        self.assertEqual(messages[1]['content'][1]['image_url']['url'], image)

    def test_invalid_or_empty_inputs(self):
        cases = [[], {}, {'context': 1}, {'context': 'x' * 20001}, {'context': 'hi', 'tone': 'unknown'}, {'images': ['data:image/png;base64,AAAA']}, {'images': ['bad'] * 4}]
        for data in cases:
            with self.subTest(data=str(data)[:40]):
                with self.assertRaises(server.InputError):
                    server.make_messages(data)

    def test_history_only_conversation_uses_background_and_current_goal(self):
        messages = server.make_messages({"history": "对方：周五交稿？\n我：正在整理。", "background": "这是每周项目同步。", "conversation_name": "项目同事", "intent": "确认周五下午交付", "reply_count": 1, "include_analysis": True})
        prompt = messages[0]['content']
        data = json.loads(messages[1]['content'][0]['text'])
        self.assertEqual(data['聊天上下文'], '')
        self.assertIn('周五交稿', data['已记录的历史'])
        self.assertEqual(data['用户填写的背景'], '这是每周项目同步。')
        self.assertIn('1条', prompt)
        self.assertIn('understanding', prompt)
        self.assertIn('候选草稿', prompt)

    def test_memory_and_generation_options_are_bounded(self):
        invalid = [{'background': 'x' * 3001}, {'history': 'x' * 12001}, {'conversation_name': 'x' * 101}, {'reply_count': 2}, {'reply_count': True}, {'include_analysis': 'yes'}]
        for extra in invalid:
            with self.subTest(keys=list(extra)):
                with self.assertRaises(server.InputError):
                    server.make_messages({'context': '你好', **extra})


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.config = server.Config(key='test-only-key')

    def test_upstream_request_and_reply_parsing(self):
        replies = [{'label': '自然', 'text': f'回复 {i}'} for i in range(3)]
        upstream = {'choices': [{'message': {'content': json.dumps({'replies': replies})}}]}
        class Opener:
            def open(inner, request, timeout):
                self.assertEqual(request.full_url, 'https://api.openai.com/v1/chat/completions')
                self.assertEqual(request.get_header('Authorization'), 'Bearer test-only-key')
                body = json.loads(request.data)
                self.assertEqual(body['model'], 'gpt-4.1-mini')
                self.assertEqual(body['response_format']['type'], 'json_object')
                self.assertIn('聊天上下文', body['messages'][1]['content'][0]['text'])
                return io.BytesIO(json.dumps(upstream).encode())
        with patch('server.urllib.request.build_opener', return_value=Opener()):
            self.assertEqual(server.generate(self.config, server.make_messages({'context': '你好'})), replies)

    def test_missing_key_and_unsafe_api_base(self):
        for config in [server.Config(), server.Config(key='x', base='http://example.com'), server.Config(key='x', base='https://name:secret@example.com')]:
            with self.assertRaises(server.ServiceError) as error:
                server.generate(config, [])
            self.assertEqual(error.exception.status, 503)

    def test_upstream_failures_are_redacted(self):
        errors = [urllib.error.HTTPError('https://api.openai.com', 401, 'secret-body', {}, None), urllib.error.URLError('secret-body')]
        for error in errors:
            with patch('server.urllib.request.OpenerDirector.open', side_effect=error):
                with self.assertRaises(server.ServiceError) as raised:
                    server.generate(self.config, [])
                self.assertNotIn('secret-body', str(raised.exception))

    def test_invalid_model_output_is_rejected(self):
        for payload in [{'choices': []}, {'choices': [{'message': {'content': '{"replies": []}'}}]}]:
            with patch('server.urllib.request.OpenerDirector.open', return_value=io.BytesIO(json.dumps(payload).encode())):
                with self.assertRaises(server.ServiceError):
                    server.generate(self.config, [])

    def test_one_reply_with_understanding_and_bounded_output(self):
        expected = {'understanding': '正在确认项目交付时间，具体时刻尚未确定。', 'replies': [{'label': '自然', 'text': '我会在周五下午发给你。'}]}
        upstream = {'choices': [{'message': {'content': json.dumps(expected)}}]}
        with patch('server.urllib.request.OpenerDirector.open', return_value=io.BytesIO(json.dumps(upstream).encode())) as opened:
            result = server.generate(self.config, server.make_messages({'context': '周五能交吗？', 'reply_count': 1, 'include_analysis': True}), reply_count=1, include_analysis=True)
        self.assertEqual(result, expected)
        payload = json.loads(opened.call_args.args[0].data)
        self.assertLessEqual(payload['max_tokens'], 400)

    def test_one_reply_default_return_remains_a_list(self):
        replies = [{'label': '自然', 'text': '好的，我们周五确认。'}]
        upstream = {'choices': [{'message': {'content': json.dumps({'replies': replies})}}]}
        with patch('server.urllib.request.OpenerDirector.open', return_value=io.BytesIO(json.dumps(upstream).encode())):
            result = server.generate(self.config, [], reply_count=1)
        self.assertEqual(result, replies)

    def test_wrong_reply_count_or_missing_analysis_is_rejected(self):
        cases = [{'replies': []}, {'replies': [{'label': '自然', 'text': '你好'}]}, {'understanding': '字' * 201, 'replies': [{'label': '自然', 'text': '你好'}]}]
        for content in cases:
            with self.subTest(content=str(content)[:40]):
                upstream = {'choices': [{'message': {'content': json.dumps(content)}}]}
                with patch('server.urllib.request.OpenerDirector.open', return_value=io.BytesIO(json.dumps(upstream).encode())):
                    with self.assertRaises(server.ServiceError):
                        server.generate(self.config, [], reply_count=1, include_analysis=True)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = server.make_server(port=0, config=server.Config())
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.http.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join()

    def request(self, path, body=None, **headers):
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, headers={'Content-Type': 'application/json', **headers})
        try:
            return urllib.request.urlopen(request)
        except urllib.error.HTTPError as error:
            return error

    def test_mobile_page_and_assets(self):
        for path, marker in [('/', 'viewport-fit=cover'), ('/app.js', 'navigator.clipboard'), ('/style.css', 'safe-area-inset-bottom'), ('/manifest.webmanifest', 'standalone')]:
            with self.request(path) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(marker, response.read().decode())
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertIn("img-src 'self' data: blob:", response.headers['Content-Security-Policy'])

    def test_status_does_not_contain_credentials(self):
        with self.request('/api/status') as response:
            self.assertEqual(json.load(response), {'configured': False, 'model': 'gpt-4.1-mini'})

    def test_generation_without_key_is_not_fake_success(self):
        with self.request('/api/generate', {'context': '你好'}) as response:
            self.assertEqual(response.status, 503)
            self.assertIn('CHAT_API_KEY', json.load(response)['error'])

    def test_http_generation_round_trip(self):
        replies = [{'label': '友好', 'text': f'你好 {i}'} for i in range(3)]
        with patch('server.generate', return_value=replies) as generate:
            with self.request('/api/generate', {'context': '对方：你好', 'intent': '自然接话'}) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response)['replies'], replies)
            self.assertIn('自然接话', generate.call_args.args[1][1]['content'][0]['text'])

    def test_http_analysis_has_flat_result_and_preserves_history(self):
        result = {'understanding': '双方正在确认见面时间。', 'replies': [{'label': '自然', 'text': '周六中午怎么样？'}]}
        with patch('server.generate', return_value=result) as generate:
            with self.request('/api/generate', {'history': '对方：周末见吗？', 'reply_count': 1, 'include_analysis': True}) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response), result)
            self.assertTrue(generate.call_args.kwargs['include_analysis'])
            self.assertEqual(generate.call_args.kwargs['reply_count'], 1)

    def test_invalid_and_cross_origin_requests(self):
        for body, headers, expected in [({}, {}, 400), ({'context': 'hi'}, {'Origin': 'https://other.example'}, 403)]:
            with self.request('/api/generate', body, **headers) as response:
                self.assertEqual(response.status, expected)

    def test_repository_and_secrets_are_not_served(self):
        for path in ('/server.py', '/.env', '/.git/config', '/tests/test_server.py'):
            with self.request(path) as response:
                self.assertEqual(response.status, 404)


if __name__ == '__main__':
    unittest.main()
