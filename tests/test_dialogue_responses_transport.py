"""Responses fixtures use no supplier network or real credentials."""
import asyncio
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from persona_runtime.archive import fingerprint
from persona_runtime.chat import ModelError
from persona_runtime.generation_retry import retryable
from scripts import dialogue_responses_transport as transport
from tests.test_dialogue_anthropic_transport import FakeContent, FakeResponse, FakeSession, MODEL, MESSAGES


def response(text='回答'):
    return {'id': 'resp_artificial', 'object': 'response', 'status': 'completed',
            'model': 'grok-4.7-build', 'error': None, 'incomplete_details': None,
            'output': [{'type': 'message', 'status': 'completed', 'role': 'assistant',
                        'content': [{'type': 'output_text', 'text': text}]}],
            'usage': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 130,
                      'input_tokens_details': {'cached_tokens': 80, 'cache_write_tokens': 10},
                      'output_tokens_details': {'reasoning_tokens': 12}}}


class ResponsesWireTests(unittest.TestCase):
    def test_original_input_order_roles_content_and_store_false(self):
        messages = deepcopy(MESSAGES) + [{'role': 'assistant', 'content': '旧答\n'},
            {'role': 'system', 'content': '后置系统块'}, {'role': 'user', 'content': '更正'}]
        before = deepcopy(messages)
        body, descriptor = transport.build_responses_request(MODEL, messages)
        self.assertEqual(body['input'], before)
        self.assertEqual(messages, before)
        self.assertEqual(set(body), {'model', 'input', 'stream', 'store'})
        self.assertIs(body['store'], False); self.assertIs(body['stream'], False)
        self.assertEqual(descriptor['wire_hash'], fingerprint(body))
        self.assertEqual(descriptor['logical_input_hash'], fingerprint(messages))
        self.assertEqual(descriptor['profile'], 'openai-responses-v1')

    def test_caps_deadline_and_old_json_flags_do_not_enter_payload(self):
        body, _ = transport.build_responses_request(MODEL | {'_json_output': True, '_disposition_compile': True}, MESSAGES)
        for key in ('max_tokens', 'max_output_tokens', 'timeout', 'reasoning', 'text', 'response_format', 'encrypted_key', '_request_deadline'):
            self.assertNotIn(key, body)
        for contract in ({'max_output_tokens': 0}, {'max_tokens': 100}, {'store': True}, {'timeout_policy': '240'}):
            with self.subTest(contract=contract), self.assertRaises(ValueError):
                transport.build_responses_request(MODEL, MESSAGES, contract)

    def test_endpoint_no_duplicate_v1_and_no_other_protocol_reuse(self):
        for base in ('https://gateway.invalid', 'https://gateway.invalid/v1/', 'https://gateway.invalid/v1/responses'):
            self.assertEqual(transport.responses_endpoint(MODEL | {'base_url': base}), 'https://gateway.invalid/v1/responses')
        for base in ('http://gateway.invalid/v1', 'https://key@gateway.invalid/v1', 'https://gateway.invalid/v1/messages'):
            with self.assertRaises(ValueError): transport.responses_endpoint(MODEL | {'base_url': base})

    def test_temperature_preserved_and_model_not_migrated(self):
        model = MODEL | {'_dialogue_profile': {'version': 'natural-dialogue-v2', 'seed': 'artificial', 'temperature': 1.1}}
        body, _ = transport.build_responses_request(model, MESSAGES)
        self.assertEqual(body['temperature'], 1.1)
        self.assertEqual(body['model'], 'grok-4.7')

    def test_unsupported_input_not_silently_dropped(self):
        for message in ({'role': 'tool', 'content': 'x'}, {'role': 'user', 'content': [{'type': 'image'}]},
                        {'role': 'user', 'content': 'x', 'name': 'not silently discarded'}):
            with self.assertRaisesRegex(ValueError, 'message_invalid'):
                transport.build_responses_request(MODEL, [message])


class ResponsesParseTests(unittest.TestCase):
    def test_typed_output_order_no_reasoning_or_invented_separator(self):
        data = response('甲\n')
        data['output'].insert(0, {'type': 'reasoning', 'summary': [{'text': 'HIDDEN_REASONING'}]})
        data['output'][1]['content'].append({'type': 'output_text', 'text': '乙'})
        data['output'].append({'type': 'message', 'role': 'assistant', 'status': 'completed',
                               'content': [{'type': 'output_text', 'text': '\n丙'}]})
        value = transport.parse_response(data)
        self.assertEqual(value[0], '甲\n乙\n丙')
        self.assertTrue(value.diagnostics['reasoning_present'])
        self.assertNotIn('HIDDEN_REASONING', json.dumps(value.diagnostics))

    def test_sdk_convenience_text_alone_is_not_raw_canonical_response(self):
        data = response(); data.pop('output'); data['output_text'] = '不得冒充实际output'
        with self.assertRaisesRegex(ModelError, 'invalid_response'): transport.parse_response(data)
        data['output'] = []
        with self.assertRaisesRegex(ModelError, 'empty_response'): transport.parse_response(data)

    def test_usage_subdivisions_not_added_to_reported_totals_or_reconciled(self):
        usage = transport.parse_response(response())[1]
        self.assertEqual(usage['prompt_tokens'], 100)
        self.assertEqual(usage['completion_tokens'], 20)
        self.assertEqual(usage['total_tokens'], 130)
        self.assertEqual(usage['responses_usage']['input_tokens_details']['cached_tokens'], 80)
        self.assertEqual(usage['completion_tokens_details']['reasoning_tokens'], 12)
        del_reported = transport.responses_usage({'input_tokens': 5, 'output_tokens': 3})
        self.assertNotIn('total_tokens', del_reported)
        self.assertIsNone(transport.responses_usage({'output_tokens': True}))

    def test_incomplete_truncated_content_filter_and_failed_are_not_success(self):
        for status, details, code in (
                ('incomplete', {'reason': 'max_output_tokens'}, 'provider_output_truncated'),
                ('incomplete', {'reason': 'content_filter'}, 'provider_content_filtered'),
                ('failed', None, 'provider_aborted'),
                ('in_progress', None, 'provider_invalid_response')):
            data = response(); data.update(status=status, incomplete_details=details)
            with self.subTest(status=status, details=details), self.assertRaisesRegex(ModelError, code) as raised:
                transport.parse_response(data)
            self.assertEqual(raised.exception.usage['total_tokens'], 130)

    def test_explicit_failed_authorization_parameter_or_quota_code_not_retried(self):
        for code, classification in (('invalid_api_key', 'provider_http_401'),
                ('permission_denied', 'provider_http_403'), ('model_not_found', 'provider_http_404'),
                ('invalid_prompt', 'provider_http_400'), ('insufficient_quota', 'provider_quota_exhausted')):
            data = response(); data.update(status='failed', error={'code': code, 'message': 'structured rejection'})
            with self.subTest(code=code), self.assertRaisesRegex(ModelError, classification) as raised:
                transport.parse_response(data, {'http_status': 200})
            self.assertFalse(retryable(raised.exception))
            self.assertEqual(raised.exception.diagnostics['http_status'], 200)
            self.assertEqual(raised.exception.provider_error['code'], code)

    def test_only_explicit_transient_failure_code_retried_no_message_guess(self):
        for code, classification in (('server_error', 'provider_http_500'),
                                      ('rate_limit_exceeded', 'provider_http_429'),
                                      ('unknown', 'provider_aborted')):
            data = response(); data.update(status='failed', error={'code': code, 'message': 'invalid_api_key in prose only'})
            with self.subTest(code=code), self.assertRaisesRegex(ModelError, classification) as raised:
                transport.parse_response(data)
            self.assertTrue(retryable(raised.exception))

    def test_refusal_tools_wrong_role_and_incomplete_output_item_rejected(self):
        for output, code in (
                ([{'type': 'message', 'role': 'assistant', 'content': [{'type': 'refusal', 'refusal': 'no'}]}], 'content_filtered'),
                ([{'type': 'function_call', 'name': 'tool'}], 'tool_call_not_supported'),
                ([{'type': 'message', 'role': 'user', 'content': []}], 'invalid_response'),
                ([{'type': 'message', 'role': 'assistant', 'status': 'incomplete', 'content': []}], 'invalid_response')):
            data = response(); data['output'] = output
            with self.subTest(output=output), self.assertRaisesRegex(ModelError, code): transport.parse_response(data)

    def test_blank_invalid_object_and_wrong_content_type_rejected(self):
        with self.assertRaisesRegex(ModelError, 'empty_response'): transport.parse_response(response('   '))
        data = response(); data['object'] = 'chat.completion'
        with self.assertRaisesRegex(ModelError, 'invalid_response'): transport.parse_response(data)
        data = response(); data['output'][0]['content'] = [{'type': 'text', 'text': 'Messages shape'}]
        with self.assertRaisesRegex(ModelError, 'invalid_response'): transport.parse_response(data)

    def test_raw_public_metadata_not_limited_to_requested_grok_prefix(self):
        data = response(); data.update(model='other-gateway-backend', system_fingerprint='opaque-backend-17')
        value = transport.parse_response(data)
        self.assertEqual(value.response_metadata, {'model': 'other-gateway-backend', 'status': 'completed',
            'system_fingerprint': 'opaque-backend-17', 'response_id': 'resp_artificial'})
        self.assertEqual(value.diagnostics['response_model'], 'other-gateway-backend')
        data.pop('system_fingerprint')
        self.assertNotIn('system_fingerprint', transport.parse_response(data).response_metadata)

    def test_echoed_credentials_not_kept_as_metadata(self):
        data = response(); data['model'] = 'ARTIFICIAL_SECRET'
        value = transport.parse_response(data, secret_values=('ARTIFICIAL_SECRET',))
        self.assertNotIn('model', value.response_metadata)
        self.assertNotIn('ARTIFICIAL_SECRET', json.dumps(value.diagnostics))


class ResponsesClientTests(unittest.IsolatedAsyncioTestCase):
    def client(self, result):
        session = FakeSession(result); self.options = []
        def factory(**kwargs): self.options.append(kwargs); return session
        client = transport.ResponsesClient(session_factory=factory, connector_factory=lambda **_: object(), ssl_context=object())
        return client, session

    async def test_success_no_local_timeout_no_anthropic_headers_and_no_redirect(self):
        client, session = self.client(FakeResponse(value=response()))
        with patch.object(transport, 'unprotect', return_value='ARTIFICIAL_SECRET'):
            value = await client.complete(MODEL, MESSAGES)
        self.assertEqual(value[0], '回答')
        self.assertEqual(value.response_metadata['response_id'], 'resp_artificial')
        timeout = self.options[0]['timeout']
        self.assertTrue(all(getattr(timeout, k) is None for k in ('total', 'connect', 'sock_connect', 'sock_read')))
        call = session.calls[0]
        self.assertEqual(call['url'], 'https://gateway.invalid/v1/responses')
        self.assertFalse(call['allow_redirects'])
        self.assertEqual(set(call['headers']), {'Content-Type', 'Authorization'})
        self.assertNotIn('ARTIFICIAL_SECRET', json.dumps(value.diagnostics))
        self.assertTrue(session.closed and session.response.closed)

    async def test_cancel_settles_response_and_session(self):
        result = FakeResponse(value=response(), wait=True); client, session = self.client(result)
        with patch.object(transport, 'unprotect', return_value='ARTIFICIAL_SECRET'):
            task = asyncio.create_task(client.complete(MODEL, MESSAGES))
            await result.content.entered.wait(); task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(session.closed and result.closed)

    async def test_http400_sanitized_error_preserved_no_retry_or_fallback_cap(self):
        data = {'error': {'type': 'invalid_request_error', 'code': 'unsupported_model',
                         'message': 'unsupported grok route; Authorization: Bearer ARTIFICIAL_SECRET'}}
        client, session = self.client(FakeResponse(status=400, value=data))
        with patch.object(transport, 'unprotect', return_value='ARTIFICIAL_SECRET'):
            with self.assertRaisesRegex(ModelError, 'provider_http_400') as raised:
                await client.complete(MODEL, MESSAGES)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(raised.exception.provider_error['code'], 'unsupported_model')
        self.assertNotIn('ARTIFICIAL_SECRET', json.dumps(raised.exception.provider_error))
        self.assertNotIn('max_output_tokens', session.calls[0]['json'])
        self.assertNotIn('max_tokens', session.calls[0]['json'])

    async def test_html_failure_no_invented_provider_reason(self):
        fake = FakeResponse(status=500, value=response()); fake.content = FakeContent(b'<html>failed</html>')
        client, _ = self.client(fake)
        with patch.object(transport, 'unprotect', return_value='ARTIFICIAL_SECRET'):
            with self.assertRaisesRegex(ModelError, 'provider_http_500') as raised:
                await client.complete(MODEL, MESSAGES)
        self.assertIsNone(raised.exception.provider_error)

    async def test_no_inherited_two_mib_response_limit(self):
        text = 'x' * (2 * 1024 * 1024 + 1); client, _ = self.client(FakeResponse(value=response(text)))
        with patch.object(transport, 'unprotect', return_value='ARTIFICIAL_SECRET'):
            value = await client.complete(MODEL, MESSAGES)
        self.assertEqual(value[0], text)


if __name__ == '__main__': unittest.main()
