"""M9R.0 contract, protocol evidence and no-replay boundaries; no public HTTP."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from persona_runtime.chat import ChatCompletionsClient, ModelCompletion, ModelError
from persona_runtime.deepseek_native_contract import (
    VERSION, ReplayScope, branch, official_endpoint, protocol_packet, replay,
    supplement, tool_accounting)
from persona_runtime.generation_retry import audited_complete
from persona_runtime.paths import RuntimePaths
from persona_runtime.provider_compat import build_request, usage_fields
from experiments import m9r_native_probe as probe


def local_temp():
    root = RuntimePaths.from_environment().home / 'tmp'
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix='m9r0-unit-', dir=root)


def model(**updates):
    return {'base_url': 'https://api.deepseek.com/v1', 'name': 'deepseek-flash',
        'max_tokens': 2048, 'encrypted_key': '', 'timeout': 10,
        '_native_web_profile': {'version': VERSION, 'mode': 'ordinary', 'tools': 'search', 'stream': False},
        **updates}


def response(*, finish='stop', content='公开资料摘要', tools=True, usage=True):
    message = {'role': 'assistant', 'content': content, 'reasoning_content': 'synthetic-reasoning'}
    if tools:
        message['server_tool_calls'] = [{'index': 0, 'reasoning_content': 'synthetic-step', 'calls': [
            {'index': 0, 'id': 'test-call', 'type': 'deepseek_read_url', 'kind': 'read_link',
             'action': {'type': 'read_link', 'url': 'https://example.org/'}, 'status': 'completed',
             'page': {'url': 'https://example.org/', 'title': 'public'}, 'signature': 'synthetic-opaque-signature',
             'future_opaque_field': {'keep': True}}]}]
        message['annotations'] = [{'type': 'url_citation', 'url_citation': {
            'url': 'https://example.org/', 'title': 'public', 'start_index': 0, 'end_index': 2}}]
    value = {'model': 'deepseek-flash', 'choices': [{'index': 0, 'message': message, 'finish_reason': finish}]}
    if usage:
        value['usage'] = {'prompt_tokens': 10, 'completion_tokens': 7, 'total_tokens': 17,
            'completion_tokens_details': {'reasoning_tokens': 3},
            'server_tool_use': {'tool_search': 2, 'tool_fetch': 1, 'tool_image_search': 0,
                                'web_search_requests': 1, 'web_fetch_requests': 1}}
    return value


def sse(*, finish='stop', done=True, internal_error=False):
    base = response(finish=finish)
    chunks = [
        {'choices': [{'index': 0, 'delta': {}, 'server_tool_event': {'type': 'call_started', 'step': 0}}]},
        {'choices': [{'index': 0, 'delta': {'reasoning_content': 'synthetic-step'}}]},
        {'choices': [{'index': 0, 'delta': {}, 'server_tool_event': {'type': 'step_reasoning_done', 'step': 0}}]},
        {'choices': [{'index': 0, 'delta': {'content': '中文😀'}}]},
    ]
    if internal_error:
        chunks.append({'error': {'message': 'synthetic provider error'}, 'choices': [], 'usage': base['usage']})
    else:
        chunks.extend([
            {'choices': [{'index': 0, 'delta': {key: value for key, value in base['choices'][0]['message'].items()
                if key in ('server_tool_calls', 'annotations')}, 'finish_reason': finish}]},
            {'choices': [], 'usage': base['usage']},
        ])
    return ''.join('data: ' + json.dumps(chunk, ensure_ascii=False) + '\r\n\r\n' for chunk in chunks) + (
        'data: [DONE]\r\n\r\n' if done else '')


class RequestContract(unittest.TestCase):
    def test_exact_official_endpoint_and_flash_only(self):
        for base in ('https://api.deepseek.com', 'https://api.deepseek.com/v1',
                     'https://api.deepseek.com/v1/chat/completions/'):
            self.assertTrue(official_endpoint(model(base_url=base)).endswith('/chat/completions'))
        for base in ('http://api.deepseek.com', 'https://api.deepseek.com.attacker.org',
                     'https://user:pass@api.deepseek.com', 'https://api.deepseek.com/v1?key=hidden',
                     'https://api.deepseek.com/anthropic', 'https://api.deepseek.com/beta',
                     'https://api.deepseek.com\n', 'https://api.deepseek.com:123/v1'):
            with self.subTest(base=base), self.assertRaises(ValueError):
                official_endpoint(model(base_url=base))
        with self.assertRaises(ValueError): official_endpoint(model(name='deepseek-v4-pro'))

    def test_ordinary_deep_and_read_only_profiles_do_not_inherit_json_or_sampling(self):
        for mode in ('ordinary', 'deep'):
            snapshot = model(_native_web_profile={'version': VERSION, 'mode': mode, 'tools': 'search', 'stream': True})
            payload, contract = build_request(snapshot, [{'role': 'user', 'content': 'public'}])
            self.assertEqual(payload['thinking']['type'], 'enabled' if mode == 'deep' else 'disabled')
            self.assertEqual(payload['stream_options'], {'include_usage': True})
            self.assertEqual(len(payload['tools']), 2)
            self.assertNotIn('response_format', payload); self.assertNotIn('temperature', payload)
            self.assertNotIn('tool_choice', payload)
            self.assertEqual(contract['output_limit_scope'], 'per_step_not_request_cost')
        snapshot = model(_native_web_profile={'version': VERSION, 'mode': 'ordinary', 'tools': 'read_link', 'stream': False})
        self.assertEqual(build_request(snapshot, [{'role': 'user', 'content': 'public'}])[0]['tools'], [{'type': 'deepseek_read_url'}])

    def test_conflicting_purposes_fail_before_dispatch(self):
        for key in ('_json_output', '_disposition_compile', '_generation_profile', '_companion_profile', '_reasoning_effort'):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'conflict'):
                build_request(model(**{key: True}), [{'role': 'user', 'content': 'public'}])
        for bad in ({}, {'version': VERSION, 'mode': [], 'tools': 'search', 'stream': False}):
            with self.assertRaises(ValueError):
                build_request(model(_native_web_profile=bad), [{'role': 'user', 'content': 'public'}])

    def test_arbitrary_private_context_and_assistant_prefix_are_not_inputs(self):
        for messages in ([{'role': 'user', 'content': {'persona': 'secret'}}],
                         [{'role': 'user', 'content': 'public', 'hidden_persona': 'secret'}],
                         [{'role': 'assistant', 'content': 'partial', 'prefix': True}], []):
            with self.assertRaises(ValueError): build_request(model(), messages)

    def test_native_followup_omits_tools_and_preserves_opaque_fields_without_mutation(self):
        message = response()['choices'][0]['message']
        scope = ReplayScope('p', 'desktop', 's', 'account')
        packet = protocol_packet(message, scope)
        history = [replay(packet, scope), {'role': 'user', 'content': 'followup'}]
        before = deepcopy(history)
        payload, _ = build_request(model(_native_web_profile={'version': VERSION, 'mode': 'ordinary', 'tools': 'none', 'stream': False}), history)
        self.assertNotIn('tools', payload)
        self.assertEqual(payload['messages'][0], message)
        payload['messages'][0]['server_tool_calls'][0]['calls'][0]['signature'] = 'changed'
        self.assertEqual(history, before)
        self.assertEqual(packet['assistant'], message)

    def test_protocol_packet_never_crosses_scope_account_model_or_version(self):
        scope = ReplayScope('p', 'desktop', 's', 'account')
        packet = protocol_packet(response()['choices'][0]['message'], scope)
        for other in (ReplayScope('q', 'desktop', 's', 'account'), ReplayScope('p', 'qq', 's', 'account'),
                      ReplayScope('p', 'desktop', 'new', 'account'), ReplayScope('p', 'desktop', 's', 'other')):
            with self.assertRaises(ValueError): replay(packet, other)
        with self.assertRaises(ValueError): replay(packet | {'version': 'future'}, scope)

    def test_jev_truth_table_includes_disabled_and_existing_fallback(self):
        for enabled in (False, True):
            for decision in ('none', 'web', 'unavailable'):
                value = branch(enabled=enabled, decision=decision)
                self.assertEqual(value['call_jev'], enabled)
                self.assertEqual(value['allow_native_tools'], enabled and decision != 'none')
        self.assertEqual(branch(enabled=True, decision='unavailable')['decision_source'], 'policy_fallback')

    def test_finite_supplement_is_a_new_action_only_for_completed_tool_free_explicit_tasks(self):
        args = dict(enabled=True, decision='web', outcome='completed', tool_calls=0, used=0,
                    intent='explicit_search', public_query='approved public query')
        self.assertFalse(supplement(**args)['replay'])
        for change in ({'enabled': False}, {'decision': 'none'}, {'outcome': 'unknown'}, {'outcome': 'failed'},
                       {'used': 1}, {'tool_calls': 1}, {'tool_calls': None}, {'intent': 'general'}, {'public_query': None}):
            self.assertIsNone(supplement(**(args | change)))
        self.assertEqual(supplement(**(args | {'intent': 'explicit_link', 'url': 'https://example.org/a'}))['tool'], 'fetch')
        with self.assertRaises(Exception): supplement(**(args | {'intent': 'explicit_link', 'url': 'http://127.0.0.1/private'}))


class Receipts(unittest.TestCase):
    def test_numeric_tool_usage_whitelist_separates_display_and_billable(self):
        value = response()['usage']
        value['server_tool_use'].update(secret='synthetic secret', tool_fetch=True, tool_image_search=-1)
        usage = usage_fields(value, deepseek=True)
        self.assertEqual(usage['server_tool_use'], {'tool_search': 2, 'web_search_requests': 1, 'web_fetch_requests': 1})
        self.assertNotIn('synthetic secret', json.dumps(usage))
        account = tool_accounting(response()['usage'], terminal=True)
        self.assertEqual(account['estimated_tool_amount'], '0.015')
        self.assertIsNone(account['actual_amount']); self.assertEqual(account['currency'], 'CNY')

    def test_unknown_receipts_never_turn_into_zero_cost(self):
        for usage, terminal in ((None, True), ({}, True), (response()['usage'], False),
                                ({'total_tokens': 3, 'server_tool_use': None}, True)):
            value = tool_accounting(usage, terminal=terminal)
            self.assertIsNone(value['estimated_tool_amount'])
        value = tool_accounting({'total_tokens': 3}, terminal=True)
        self.assertEqual(value['estimated_tool_amount'], '0.000')
        self.assertEqual(value['basis'], 'documented_terminal_omission')

    def test_json_preserves_final_tool_packet_and_citations(self):
        raw = response(); observed = probe.inspect_response(raw)
        self.assertEqual(observed['outcome'], 'completed')
        self.assertTrue(observed['material_acquired']); self.assertEqual(observed['tool_calls'], 1)
        self.assertEqual(observed['message'], raw['choices'][0]['message'])
        self.assertEqual(observed['usage']['total_tokens'], 17)

    def test_json_internal_error_keeps_usage_even_without_choices(self):
        result = probe.inspect_response({'error': {'message': 'synthetic'}, 'usage': response()['usage']})
        self.assertEqual(result['outcome'], 'failed'); self.assertEqual(result['usage']['total_tokens'], 17)
        self.assertEqual(result['accounting']['billable']['tool_search'], 2)

    def test_truncation_refusal_and_missing_terminal_are_distinct(self):
        for finish in ('length', 'content_filter', 'aborted', 'insufficient_system_resource'):
            result = probe.inspect_response(response(finish=finish, content=''))
            self.assertEqual(result['outcome'], 'failed'); self.assertEqual(result['reason'], finish)
            self.assertEqual(result['usage']['total_tokens'], 17)
        result = probe.inspect_response(response(finish=None))
        self.assertEqual(result['outcome'], 'unknown'); self.assertIsNone(result['usage'])

    def test_no_tool_execution_is_not_a_second_web_decision(self):
        result = probe.inspect_response(response(tools=False))
        self.assertEqual(result['outcome'], 'completed'); self.assertEqual(result['tool_calls'], 0)
        self.assertFalse(result['material_acquired'])

    def test_stream_uses_final_fields_not_progress_and_unicode_survives(self):
        result = probe.inspect_response(sse(), stream=True)
        self.assertEqual(result['outcome'], 'completed'); self.assertEqual(result['message']['content'], '中文😀')
        self.assertEqual(result['tool_calls'], 1); self.assertEqual(len(result['tool_events']), 2)
        self.assertTrue(result['message']['server_tool_calls'][0]['calls'][0]['future_opaque_field']['keep'])
        self.assertNotIn('synthetic-step', result['message'].get('reasoning_content', ''))

    def test_stream_error_before_done_is_known_failure_with_tool_usage(self):
        result = probe.inspect_response(sse(done=False, internal_error=True), stream=True)
        self.assertEqual(result['outcome'], 'failed'); self.assertFalse(result['stream_done'])
        self.assertEqual(result['usage']['total_tokens'], 17)

    def test_stream_disconnect_keeps_last_snapshot_but_not_final_usage(self):
        result = probe.inspect_response(sse(done=False), stream=True)
        self.assertEqual(result['outcome'], 'unknown'); self.assertIsNone(result['usage'])
        self.assertEqual(result['last_observed_usage']['total_tokens'], 17)
        self.assertIsNone(result['accounting']['estimated_tool_amount'])

    def test_progress_only_and_malformed_events_cannot_complete(self):
        for raw in ('data: {"choices": [{"delta": {}, "server_tool_event": {"type": "call_completed"}}]}\n\n',
                    'data: invalid\n\ndata: [DONE]\n\n', sse() + 'data: [DONE]\n\n'):
            self.assertEqual(probe.inspect_response(raw, stream=True)['outcome'], 'unknown')


class AuditBoundary(unittest.IsolatedAsyncioTestCase):
    async def test_native_profile_is_never_sent_through_the_old_text_only_path(self):
        # M9R.1 now supplies the dedicated transport; retain M9R.0's isolation
        # invariant without requiring the historical temporary rejection.
        with patch('persona_runtime.chat.unprotect', side_effect=AssertionError('old credential path')), \
                patch('persona_runtime.deepseek_native_transport.DeepSeekNativeClient.complete',
                      new_callable=AsyncMock, return_value=ModelCompletion('public', None, {}, native={'test': True})) as dispatch:
            value = await ChatCompletionsClient(allow_loopback_fixture=True, ).complete(model(), [{'role': 'user', 'content': 'public'}])
        dispatch.assert_awaited_once()
        self.assertEqual(value.native, {'test': True})

    async def test_probe_audits_once_and_keeps_tool_usage_with_existing_attempt_identity(self):
        with local_temp() as folder:
            paths = RuntimePaths(Path(folder)); paths.initialize()
            saved = []; calls = []
            class Client:
                async def complete(self, snapshot, messages):
                    calls.append(1)
                    return ModelCompletion('public', response()['usage'], {'request_headers_sent': True})
            result = await audited_complete(Client(), model(), [{'role': 'user', 'content': 'public'}],
                validate=lambda text: None, save=lambda value: saved.append(deepcopy(value)),
                before_attempt=lambda value: None, paths=paths)
            self.assertEqual(len(calls), 1)
            self.assertEqual(result[1]['server_tool_use']['tool_search'], 2)
            self.assertEqual(result[1]['generation_retry']['attempts'][0]['id'], saved[0]['generation_retry']['attempts'][0]['id'])

    async def test_unknown_probe_error_does_not_enter_ordinary_retry_loop(self):
        with local_temp() as folder:
            paths = RuntimePaths(Path(folder)); paths.initialize()
            calls = []
            class Client:
                async def complete(self, snapshot, messages):
                    calls.append(1); raise ModelError('native_probe_transport_unknown', diagnostics={'request_headers_sent': True})
            with self.assertRaisesRegex(ModelError, 'native_probe_transport_unknown'):
                await audited_complete(Client(), model(), [{'role': 'user', 'content': 'public'}],
                    validate=lambda text: None, save=lambda value: None,
                    before_attempt=lambda value: None, paths=paths)
            self.assertEqual(len(calls), 1)

    async def test_reserved_or_changed_probe_plan_never_dispatches(self):
        with local_temp() as folder:
            root = Path(folder)
            with self.assertRaises(ValueError): probe.evidence_root(probe.REPO / 'tmp')
            plan = {'version': probe.PROBE_VERSION, 'positions': probe.POSITIONS, 'source_hashes': probe.source_hashes()}
            from persona_runtime.config import atomic_json
            from persona_runtime.archive import fingerprint
            atomic_json(root / 'plan.json', plan)
            atomic_json(root / 'plan-fingerprint.json', {'sha256': fingerprint(plan)})
            plan['positions'] = []
            atomic_json(root / 'plan.json', plan)
            with self.assertRaisesRegex(ValueError, 'frozen_plan_changed'): probe.load_plan(root)
            reserved = root / probe.POSITIONS[0]['id']
            reserved.mkdir()
            frozen = {'source_home': str(root), 'positions': probe.POSITIONS,
                      'protected_before': {}, 'stop_after_observed_tokens': 100000,
                      'stop_after_observed_billable_tools': 60}
            with patch.object(probe, 'load_plan', return_value=frozen), \
                    patch.object(probe.ConfigStore, 'load', return_value={'model': model()}), \
                    patch.object(probe, 'protected_snapshot', return_value={}), \
                    patch.object(probe.ProbeClient, 'complete', new_callable=AsyncMock) as dispatch:
                with self.assertRaisesRegex(ValueError, 'position_already_reserved'):
                    await probe.run(root)
                dispatch.assert_not_called()
            self.assertTrue(reserved.is_dir())
            self.assertFalse((root / 'run.lock').exists())


if __name__ == '__main__':
    unittest.main()
