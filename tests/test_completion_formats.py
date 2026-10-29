import io
import json
import unittest
from unittest.mock import patch

from server import Config, ServiceError, generate


class CompletionFormatTests(unittest.TestCase):
    def setUp(self):
        self.config = Config('test-only-key')
        self.content = {'understanding': '正在确认时间。', 'replies': [{'label': '自然', 'text': '我确认一下安排。'}], 'screenshot_text': '未知：周五有空吗？'}
        self.json = json.dumps(self.content, ensure_ascii=False)

    def run_message(self, message, finish='stop', transcribe=True):
        response = {'choices': [{'finish_reason': finish, 'message': message}]}
        with patch('server.urllib.request.OpenerDirector.open', return_value=io.BytesIO(json.dumps(response).encode())) as opened:
            result = generate(self.config, [], reply_count=1, include_analysis=True, transcribe_images=transcribe)
        opened.assert_called_once()
        return result

    def test_common_json_wrappers_and_text_blocks_are_accepted(self):
        forms = [self.json, '```json\n' + self.json + '\n```', '```JSON\r\n' + self.json + '\r\n```', '```\n' + self.json + '\n```', '\ufeff  ' + self.json, self.content,
                 [{'type': 'text', 'text': self.json[:13]}, {'type': 'thinking', 'text': 'private reasoning'}, {'type': 'text', 'text': self.json[13:]}]]
        for content in forms:
            with self.subTest(form=type(content).__name__):
                result = self.run_message({'content': content})
            self.assertEqual(result['screenshot_text'], self.content['screenshot_text'])
            self.assertEqual(result['replies'], self.content['replies'])
            self.assertNotIn('private reasoning', str(result))
        result = self.run_message({'content': None, 'parsed': self.content})
        self.assertEqual(result['replies'], self.content['replies'])

    def test_truncation_is_distinct_from_a_syntax_error(self):
        for content in ('{"replies":', '', None, {'replies': []}):
            with self.subTest(content=content), self.assertRaisesRegex(ServiceError, '被截断') as raised:
                self.run_message({'content': content, 'reasoning_content': 'private reasoning'}, finish='length')
            self.assertNotIn('private reasoning', str(raised.exception))
        self.assertEqual(self.run_message({'content': self.json}, finish='length')['replies'], self.content['replies'])

    def test_thinking_only_response_is_never_used_as_a_reply(self):
        for content in ('', None):
            with self.subTest(content=content), self.assertRaisesRegex(ServiceError, '没有最终回复') as raised:
                self.run_message({'content': content, 'reasoning_content': 'private reasoning'})
            self.assertNotIn('private reasoning', str(raised.exception))

    def test_schema_errors_are_specific_without_echoing_model_text(self):
        for content, expected in [('private model text', '有效的 JSON'), ('[]', '顶层'), ('{"replies":[]}', 'replies'), ('{"replies":[{"label":"自然","text":"private reply"}]}', 'understanding')]:
            with self.subTest(expected=expected), self.assertRaises(ServiceError) as raised:
                self.run_message({'content': content}, transcribe=False)
            self.assertIn(expected, str(raised.exception))
            self.assertNotIn('private', str(raised.exception))

    def test_outer_http_format_and_missing_choices_are_distinct(self):
        for raw, expected in [(b'data: private streaming response\n\n', '非流式'), (b'{"error":"private provider response"}', 'choices')]:
            with self.subTest(expected=expected), patch('server.urllib.request.OpenerDirector.open', return_value=io.BytesIO(raw)):
                with self.assertRaises(ServiceError) as raised:
                    generate(self.config, [], reply_count=1)
            self.assertIn(expected, str(raised.exception))
            self.assertNotIn('private', str(raised.exception))

    def test_configured_budget_is_sent_and_invalid_values_never_make_requests(self):
        self.config = Config('test-only-key', max_output_tokens=8192)
        response = {'choices': [{'message': {'content': self.json}}]}
        with patch('server.urllib.request.OpenerDirector.open', return_value=io.BytesIO(json.dumps(response).encode())) as opened:
            generate(self.config, [], reply_count=1, include_analysis=True)
        payload = json.loads(opened.call_args.args[0].data)
        self.assertEqual(payload['max_tokens'], 8192)
        self.assertFalse(payload['stream'])
        for limit in (True, 0, 16385, '4096'):
            with self.subTest(limit=limit), patch('server.urllib.request.build_opener') as opener:
                with self.assertRaisesRegex(ServiceError, '输出上限'):
                    generate(Config('test-only-key', max_output_tokens=limit), [], reply_count=1)
            opener.assert_not_called()


if __name__ == '__main__':
    unittest.main()
