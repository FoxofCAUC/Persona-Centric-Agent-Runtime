"""Offline wire and pre-dispatch checks for the native text reference profile."""
import asyncio
from copy import deepcopy
import unittest
from unittest.mock import patch

from persona_runtime.archive import fingerprint
from persona_runtime.chat import ChatCompletionsClient, ModelCompletion, ModelError
from persona_runtime.generation_retry import audited_complete
from persona_runtime.provider_compat import build_request, describe_request, request_timeout
from persona_runtime.reply_batch import expression_snapshot


MESSAGES = [{'role': 'system', 'content': '人工人物资料'},
            {'role': 'user', 'content': '今天想随便聊聊'}]


def base_model():
    return {'name': 'grok-4.7', 'base_url': 'https://example.invalid/v1',
            'encrypted_key': '', 'max_tokens': 8192, 'timeout': 240,
            'max_input_chars': 24000,
            '_generation_profile': {'version': 'chat-stream-unlimited-v1'}}


def native_model():
    return base_model() | {'_native_text_profile': {'version': 'native-text-low-v1'}}


class NativeTextWire(unittest.TestCase):
    def test_only_format_changes_from_current_grok_structured_wire(self):
        structured = base_model() | {'_reply_mode': 'natural_structured',
            '_expression_snapshot': expression_snapshot('natural_structured', version=3)}
        original, _ = build_request(structured, MESSAGES)
        actual, contract = build_request(native_model(), MESSAGES)
        self.assertIn('response_format', original)
        self.assertEqual(actual, {k: v for k, v in original.items() if k != 'response_format'})
        self.assertEqual(actual['reasoning_effort'], 'low')
        self.assertNotIn('temperature', actual)
        self.assertNotIn('max_tokens', actual)
        self.assertTrue(actual['stream'])
        self.assertIsNone(request_timeout(native_model()))
        self.assertEqual(contract['response_format'], 'text')
        self.assertFalse(contract['application_validation_required'])
        self.assertEqual(contract['native_text_profile'], 'native-text-low-v1')
        self.assertEqual(contract['version'], 'provider-wire-native-text-low-v1')

    def test_plain_default_does_not_acquire_low_or_new_profile(self):
        model = base_model()
        actual, contract = build_request(model, MESSAGES)
        self.assertEqual(actual, {'model': 'grok-4.7', 'messages': MESSAGES,
            'stream': True, 'stream_options': {'include_usage': True}})
        self.assertEqual(contract['version'], 'provider-wire-v1')
        self.assertNotIn('native_text_profile', contract)
        del model['_generation_profile']
        actual, _ = build_request(model, MESSAGES)
        self.assertEqual(actual, {'model': 'grok-4.7', 'messages': MESSAGES,
                                 'max_tokens': 8192, 'stream': False})
        self.assertEqual(request_timeout(model), 240)

    def test_explicit_legacy_full_is_plain_without_mutating_inputs(self):
        model = native_model() | {'_reply_mode': 'legacy_full'}
        messages = deepcopy(MESSAGES)
        before = deepcopy(model), deepcopy(messages)
        actual, _ = build_request(model, messages)
        self.assertNotIn('response_format', actual)
        self.assertEqual((model, messages), before)

    def test_unknown_or_malformed_native_version_is_rejected(self):
        for value in (None, 'native-text-low-v1', {}, {'version': 'future'},
                      {'version': 'native-text-low-v1', 'reasoning_effort': 'high'}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, '^native_text_profile_invalid$'):
                    build_request(base_model() | {'_native_text_profile': value}, MESSAGES)

    def test_mixed_structured_and_companion_contracts_are_rejected(self):
        cases = [{'_reply_mode': mode} for mode in
                 ('natural_structured', 'context_structured', 'unknown')]
        cases += [{key: value} for key, value in (
            ('_expression_snapshot', None), ('_character_selection', False),
            ('_json_output', True), ('_disposition_compile', True),
            ('_dialogue_profile', {'version': 'anything'}),
            ('_companion_profile', {'version': 'companion-runtime-v1', 'mode': 'minimal'}))]
        for extra in cases:
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(ValueError, '^native_text_profile_conflict$'):
                    build_request(native_model() | extra, MESSAGES)

    def test_other_models_and_deepseek_routes_are_not_silently_adapted(self):
        for extra in ({'name': 'another-model'}, {'name': 'grok-4.7-build'},
                      {'name': 'deepseek-flash'}, {'base_url': 'https://api.deepseek.com/v1'}):
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(ValueError, '^native_text_model_unsupported$'):
                    describe_request(native_model() | extra)

    def test_generation_contract_is_still_separately_validated(self):
        with self.assertRaisesRegex(ValueError, '^generation_profile_invalid$'):
            build_request(native_model() | {'_generation_profile': {'version': 'future'}}, MESSAGES)


class NativeTextDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_contract_never_creates_http_session(self):
        client = ChatCompletionsClient()
        with patch('persona_runtime.chat.aiohttp.ClientSession',
                   side_effect=AssertionError('must not connect')) as session:
            with self.assertRaisesRegex(ModelError, '^native_text_profile_conflict$'):
                await client.complete(native_model() | {'_reply_mode': 'natural_structured'}, MESSAGES)
        session.assert_not_called()

    async def test_native_audit_hash_matches_actual_wire_and_keeps_plain_answer(self):
        model = native_model()
        saved = []
        seen = []
        class FakeClient:
            async def complete(self, actual_model, actual_messages):
                seen.append(build_request(actual_model, actual_messages)[0])
                self_row = saved[-1]['generation_retry']['attempts'][-1]
                self_status = self_row['status']
                if self_status != 'running':
                    raise AssertionError('not reserved before dispatch')
                return ModelCompletion('那我先说一个突然想到的事。', {'total_tokens': 7},
                                       {'finish_reason': 'stop'})
        result = await audited_complete(FakeClient(), model, MESSAGES,
            validate=lambda body: None, save=lambda value: saved.append(deepcopy(value)),
            before_attempt=lambda state: None)
        self.assertEqual(result[0], '那我先说一个突然想到的事。')
        self.assertEqual(len(seen), 1)
        attempts = saved[-1]['generation_retry']['attempts']
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]['status'], 'completed')
        self.assertEqual(attempts[0]['request_hash'], fingerprint(seen[0]))
        self.assertNotIn('response_format', seen[0])
        self.assertEqual(seen[0]['reasoning_effort'], 'low')


if __name__ == '__main__':
    unittest.main()
