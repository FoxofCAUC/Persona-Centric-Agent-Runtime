"""Artificial wire/HTTP fixtures; no supplier connection or secret access."""
import asyncio
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from persona_runtime.archive import fingerprint
from persona_runtime.chat import ModelError
from scripts import dialogue_anthropic_transport as transport


MODEL = {'base_url': 'https://gateway.invalid/v1', 'name': 'grok-4.7',
         'encrypted_key': 'fake-dpapi', 'timeout': 1, 'max_tokens': 1,
         '_request_deadline': 0}
MESSAGES = [{'role': 'system', 'content': '第一段\n'},
            {'role': 'system', 'content': '\n第二段'},
            {'role': 'user', 'content': '输入\n原样'}]


def response(text='回答'):
    return {'type': 'message', 'role': 'assistant', 'model': 'grok-4.7-build',
            'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': text}],
            'usage': {'input_tokens': 10, 'output_tokens': 4,
                      'cache_creation_input_tokens': 3, 'cache_read_input_tokens': 7}}


class WireTests(unittest.TestCase):
    def test_system_blocks_text_order_and_logical_vs_wire_hash(self):
        original = deepcopy(MESSAGES)
        body, wire = transport.build_messages_request(MODEL, MESSAGES)
        self.assertEqual(body['system'], [{'type': 'text', 'text': '第一段\n'},
                                         {'type': 'text', 'text': '\n第二段'}])
        self.assertEqual(body['messages'], [MESSAGES[2]])
        self.assertEqual(MESSAGES, original)
        self.assertEqual(wire['logical_input_hash'], fingerprint(MESSAGES))
        self.assertEqual(wire['wire_hash'], fingerprint(body))
        self.assertNotEqual(wire['logical_input_hash'], wire['wire_hash'])

    def test_legacy_caps_deadline_and_json_flags_not_sent(self):
        body, _ = transport.build_messages_request(MODEL | {'_json_output': True, '_disposition_compile': True}, MESSAGES)
        self.assertEqual(set(body), {'model', 'messages', 'system', 'stream'})
        self.assertNotIn('max_tokens', body)
        self.assertNotIn('timeout', body)
        self.assertNotIn('_request_deadline', body)
        self.assertNotIn('encrypted_key', body)

    def test_explicit_zero_positive_and_unknown_limit_mode_rejected(self):
        for value in ({'max_tokens': 0}, {'max_tokens': 999999}, {'max_tokens_mode': 'explicit'}, {'timeout_policy': '240'}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'limit_contract_invalid'):
                transport.build_messages_request(MODEL, MESSAGES, value)

    def test_endpoint_no_duplicate_v1_and_incompatible_paths_rejected(self):
        for base in ('https://gateway.invalid', 'https://gateway.invalid/v1/', 'https://gateway.invalid/v1/messages'):
            self.assertEqual(transport.messages_endpoint(MODEL | {'base_url': base}), 'https://gateway.invalid/v1/messages')
        for base in ('http://gateway.invalid/v1', 'https://gateway.invalid/v1/chat/completions', 'https://key@gateway.invalid/v1'):
            with self.subTest(base=base), self.assertRaises(ValueError):
                transport.messages_endpoint(MODEL | {'base_url': base})

    def test_interleaved_system_not_silently_moved(self):
        with self.assertRaisesRegex(ValueError, 'interleaved_system'):
            transport.build_messages_request(MODEL, [MESSAGES[2], MESSAGES[0]])

    def test_temperature_preserved_and_consecutive_turns_not_rewritten(self):
        model = MODEL | {'_dialogue_profile': {'version': 'natural-dialogue-v2', 'seed': 'artificial', 'temperature': 1.1}}
        messages = [MESSAGES[2], MESSAGES[2], {'role': 'assistant', 'content': '原答案'}, MESSAGES[2]]
        body, _ = transport.build_messages_request(model, messages)
        self.assertEqual(body['messages'], messages)
        self.assertEqual(body['temperature'], 1.1)

    def test_text_blocks_concatenated_without_reasoning_or_added_separator(self):
        value = response()
        value['content'] = [{'type': 'thinking', 'thinking': 'DO_NOT_EXPORT'},
                            {'type': 'text', 'text': '甲\n'}, {'type': 'text', 'text': '乙'}]
        parsed = transport.parse_message(value)
        self.assertEqual(parsed[0], '甲\n乙')
        self.assertTrue(parsed.diagnostics['reasoning_present'])
        self.assertNotIn('DO_NOT_EXPORT', json.dumps(parsed.diagnostics))

    def test_usage_cache_components_added_once_and_original_counts_retained(self):
        parsed = transport.parse_message(response())
        self.assertEqual(parsed[1]['prompt_tokens'], 20)
        self.assertEqual(parsed[1]['completion_tokens'], 4)
        self.assertEqual(parsed[1]['total_tokens'], 24)
        self.assertEqual(parsed[1]['anthropic_usage']['input_tokens'], 10)
        self.assertIsNone(transport.messages_usage({'output_tokens': 4}))

    def test_truncation_refusal_tool_and_empty_not_success(self):
        for reason, code in (('max_tokens', 'provider_output_truncated'), ('refusal', 'provider_content_filtered'),
                             ('tool_use', 'provider_tool_call_not_supported'), ('pause_turn', 'provider_invalid_response')):
            value = response(); value['stop_reason'] = reason
            with self.subTest(reason=reason), self.assertRaisesRegex(ModelError, code):
                transport.parse_message(value)
        with self.assertRaisesRegex(ModelError, 'empty_response'):
            transport.parse_message(response('  '))

    def test_missing_backend_is_not_fabricated(self):
        parsed = transport.parse_message(response())
        self.assertEqual(parsed.diagnostics['response_model'], 'grok-4.7-build')
        self.assertNotIn('system_fingerprint', parsed.diagnostics)


class FakeContent:
    def __init__(self, body, wait=False):
        self.body = body
        self.wait = wait
        self.entered = asyncio.Event()

    async def iter_chunked(self, size):
        self.entered.set()
        if self.wait:
            await asyncio.Future()
        yield self.body[:7]
        yield self.body[7:]


class FakeResponse:
    def __init__(self, status=200, value=None, wait=False):
        self.status = status
        self.content = FakeContent(json.dumps(value or response(), ensure_ascii=False).encode(), wait)
        self.closed = False

    async def __aenter__(self): return self
    async def __aexit__(self, *args): self.closed = True


class FakeSession:
    def __init__(self, response): self.response, self.closed, self.calls = response, False, []
    async def __aenter__(self): return self
    async def __aexit__(self, *args): self.closed = True
    def post(self, url, **kwargs):
        self.calls.append({'url': url, **kwargs})
        return self.response


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def client(self, result, contract=None):
        session = FakeSession(result)
        self.settings = []
        def factory(**kwargs): self.settings.append(kwargs); return session
        client = transport.AnthropicMessagesClient(contract, session_factory=factory,
                    connector_factory=lambda **_: object(), ssl_context=object())
        return client, session

    async def test_all_timeouts_disabled_headers_private_and_redirects_disabled(self):
        client, session = self.client(FakeResponse())
        with patch.object(transport, 'unprotect', return_value='ARTIFICIAL_SECRET'):
            value = await client.complete(MODEL, MESSAGES)
        self.assertEqual(value[0], '回答')
        timeout = self.settings[0]['timeout']
        self.assertTrue(all(getattr(timeout, name) is None for name in ('total', 'connect', 'sock_connect', 'sock_read')))
        self.assertFalse(self.settings[0]['trust_env'])
        self.assertFalse(session.calls[0]['allow_redirects'])
        self.assertEqual(session.calls[0]['headers']['Authorization'], 'Bearer ARTIFICIAL_SECRET')
        self.assertNotIn('ARTIFICIAL_SECRET', json.dumps(value.diagnostics))
        self.assertTrue(session.closed and session.response.closed)

    async def test_cancellation_closes_session_and_response(self):
        result = FakeResponse(wait=True)
        client, session = self.client(result)
        with patch.object(transport, 'unprotect', return_value='ARTIFICIAL_SECRET'):
            task = asyncio.create_task(client.complete(MODEL, MESSAGES))
            await result.content.entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(session.closed and result.closed)

    async def test_http400_no_retry_or_added_limit_or_fallback(self):
        rejection = {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'max_tokens: Field required'}}
        client, session = self.client(FakeResponse(status=400, value=rejection))
        with patch.object(transport, 'unprotect', return_value='ARTIFICIAL_SECRET'):
            with self.assertRaisesRegex(ModelError, 'provider_http_400') as raised:
                await client.complete(MODEL, MESSAGES)
        self.assertEqual(len(session.calls), 1)
        self.assertNotIn('max_tokens', session.calls[0]['json'])
        self.assertEqual(session.calls[0]['url'], 'https://gateway.invalid/v1/messages')
        self.assertEqual(raised.exception.diagnostics['http_status'], 400)
        self.assertEqual(raised.exception.provider_error,
                         {'type': 'invalid_request_error', 'message': 'max_tokens: Field required', 'message_truncated': False})

    async def test_rejection_message_secret_redacted_and_diagnostic_length_only(self):
        rejection = {'error': {'type': 'invalid_request_error', 'code': 'missing_field',
            'message': 'Authorization: Bearer ARTIFICIAL_SECRET; key ARTIFICIAL_SECRET\n' + 'detail' * 200}}
        client, session = self.client(FakeResponse(status=400, value=rejection))
        with patch.object(transport, 'unprotect', return_value='ARTIFICIAL_SECRET'):
            with self.assertRaisesRegex(ModelError, 'provider_http_400') as raised:
                await client.complete(MODEL, MESSAGES)
        public = raised.exception.provider_error
        self.assertNotIn('ARTIFICIAL_SECRET', json.dumps(public))
        self.assertTrue(public['message_truncated'])
        self.assertLessEqual(len(public['message']), 512)
        self.assertEqual(public['code'], 'missing_field')
        self.assertEqual(len(session.calls), 1)

    async def test_html_rejection_keeps_http_status_without_inventing_cause(self):
        result = FakeResponse(status=524)
        result.content = FakeContent(b'<html>upstream failure</html>')
        client, _ = self.client(result)
        with patch.object(transport, 'unprotect', return_value='ARTIFICIAL_SECRET'):
            with self.assertRaisesRegex(ModelError, 'provider_http_524') as raised:
                await client.complete(MODEL, MESSAGES)
        self.assertIsNone(raised.exception.provider_error)

    async def test_no_inherited_two_mib_output_cap(self):
        text = 'x' * (2 * 1024 * 1024 + 1)
        client, _ = self.client(FakeResponse(value=response(text)))
        with patch.object(transport, 'unprotect', return_value='ARTIFICIAL_SECRET'):
            value = await client.complete(MODEL, MESSAGES)
        self.assertEqual(value[0], text)

    async def test_x_api_key_header_is_explicit_contract(self):
        client, session = self.client(FakeResponse(), {'auth_mode': 'x-api-key'})
        with patch.object(transport, 'unprotect', return_value='ARTIFICIAL_SECRET'):
            await client.complete(MODEL, MESSAGES)
        self.assertEqual(session.calls[0]['headers']['x-api-key'], 'ARTIFICIAL_SECRET')
        self.assertNotIn('Authorization', session.calls[0]['headers'])


if __name__ == '__main__':
    unittest.main()
