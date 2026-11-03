"""Offline checks for the explicit Messages executor, including native chat use."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from persona_runtime.archive import fingerprint
from persona_runtime.chat import ModelCompletion, ModelError
from persona_runtime.generation_retry import GenerationRetriesExhausted
from persona_runtime.models import RuntimeErrorBase
from scripts import dialogue_messages_retry as retry
from tests import test_m754r2 as fixture


class MessagesRetry(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.model = {'name': 'grok-4.7', 'base_url': 'https://synthetic.invalid/v1',
                      'max_tokens': 8192, 'timeout': 180}
        self.messages = [{'role': 'user', 'content': '全新人工材料'}]
        self.client = SimpleNamespace()
        self.client.request_payload = lambda model, messages: {
            'model': model['name'], 'messages': deepcopy(messages), 'stream': False}
        self.client.complete = AsyncMock(return_value=ModelCompletion('有效回复', {'total_tokens': 7}, {}))
        self.saved = []
        self.sleep = AsyncMock()

    async def run_case(self, **kw):
        options = {'validate': lambda _: None,
                   'save': lambda v: self.saved.append(deepcopy(v)),
                   'before_attempt': lambda _: None, 'sleep': self.sleep}
        options.update(kw)
        return await retry.audited_messages_complete(self.client, self.model, self.messages, **options)

    async def test_no_local_deadline_even_after_virtual_ten_thousand_seconds(self):
        async def complete(model, messages):
            self.assertNotIn('_request_deadline', model)
            await asyncio.sleep(0)
            return '晚于旧时限的合法回复', {'total_tokens': 7}
        self.client.complete.side_effect = complete
        clock = SimpleNamespace(time=Mock(side_effect=[0, 10000]))
        with patch.object(retry, 'time', clock), patch.object(asyncio, 'wait', side_effect=AssertionError('no local timer')):
            result = await self.run_case()
        row = result[1]['generation_retry']['attempts'][0]
        self.assertIsNone(row['timeout_seconds'])
        self.assertEqual(row['finished_epoch'] - row['started_epoch'], 10000)
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(self.model['timeout'], 180)
        self.assertEqual(self.model['max_tokens'], 8192)

    async def test_first_plus_three_success_last_identical_wire_and_intervals(self):
        self.client.complete.side_effect = [ModelError('provider_http_524')] * 3 + [('成功', {'total_tokens': 5})]
        result = await self.run_case()
        rows = result[1]['generation_retry']['attempts']
        self.assertEqual(self.client.complete.await_count, 4)
        self.assertEqual([r['status'] for r in rows], ['failed', 'failed', 'failed', 'completed'])
        self.assertEqual([x.args[0] for x in self.sleep.await_args_list], [1, 2, 4])
        self.assertEqual(len({r['request_hash'] for r in rows}), 1)
        self.assertEqual(rows[0]['request_hash'], fingerprint(self.client.request_payload(self.model, self.messages)))
        self.assertEqual(result[1]['total_tokens'], 5)

    async def test_four_failures_stop_without_fifth(self):
        self.client.complete.side_effect = ModelError('provider_http_524')
        with self.assertRaises(GenerationRetriesExhausted) as caught:
            await self.run_case()
        self.assertEqual(self.client.complete.await_count, 4)
        self.assertEqual(len(caught.exception.usage['generation_retry']['attempts']), 4)
        self.assertEqual(self.saved[-1]['generation_retry']['attempts'][-1]['status'], 'failed')

    async def test_required_max_tokens_rejection_and_permissions_never_retry(self):
        for code in ('provider_http_400', 'provider_http_401', 'provider_content_filtered', 'character_source_changed'):
            self.client.complete.reset_mock()
            self.client.complete.side_effect = ModelError(code)
            with self.assertRaisesRegex(ModelError, code):
                await self.run_case()
            self.assertEqual(self.client.complete.await_count, 1)

    async def test_semantically_poor_but_legal_output_is_not_regenerated(self):
        self.client.complete.return_value = ('{"legally_formed_but_unhelpful":true}', {})
        result = await self.run_case(validate=json.loads)
        self.assertIn('unhelpful', result[0])
        self.assertEqual(self.client.complete.await_count, 1)

    async def test_protocol_failure_retries_same_request(self):
        self.client.complete.side_effect = [('invalid', {}), ('valid', {})]
        def validate(value):
            if value != 'valid':
                raise RuntimeErrorBase('reply_schema_invalid')
        result = await self.run_case(validate=validate)
        self.assertEqual(result[0], 'valid')
        self.assertEqual(self.client.complete.await_count, 2)
        self.assertEqual(len({r['request_hash'] for r in result[1]['generation_retry']['attempts']}), 1)

    async def test_before_dispatch_source_failure_does_not_call_client(self):
        def before(_):
            raise RuntimeErrorBase('source_withdrawn')
        with self.assertRaisesRegex(RuntimeErrorBase, 'source_withdrawn'):
            await self.run_case(before_attempt=before)
        self.client.complete.assert_not_awaited()
        self.assertFalse(self.saved)

    async def test_initial_audit_failure_prevents_dispatch(self):
        def fail(_):
            raise OSError('synthetic write failure')
        with self.assertRaises(OSError):
            await self.run_case(save=fail)
        self.client.complete.assert_not_awaited()

    async def test_terminal_write_failure_does_not_replay_success(self):
        def save(value):
            if value['generation_retry']['attempts'][-1]['status'] == 'completed':
                raise OSError('synthetic terminal write failure')
        with self.assertRaises(OSError):
            await self.run_case(save=save)
        self.assertEqual(self.client.complete.await_count, 1)

    async def test_cancel_settles_client_before_interrupted_terminal_and_no_retry(self):
        entered, cleaned = asyncio.Event(), asyncio.Event()
        async def complete(*_):
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        self.client.complete.side_effect = complete
        def save(value):
            if value['generation_retry']['attempts'][-1]['status'] == 'interrupted':
                self.assertTrue(cleaned.is_set())
            self.saved.append(deepcopy(value))
        task = asyncio.create_task(self.run_case(save=save))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cleaned.is_set())
        self.assertEqual(self.client.complete.await_count, 1)
        self.sleep.assert_not_awaited()
        self.assertEqual(self.saved[-1]['generation_retry']['attempts'][0]['status'], 'interrupted')

    async def test_wire_change_before_retry_refuses_second_dispatch(self):
        self.client.complete.side_effect = ModelError('provider_network_error')
        async def change(_):
            self.messages[0]['content'] = 'changed synthetic input'
        self.sleep.side_effect = change
        with self.assertRaisesRegex(RuntimeErrorBase, 'messages_retry_request_changed'):
            await self.run_case()
        self.assertEqual(self.client.complete.await_count, 1)

    async def test_missing_wire_contract_fails_before_dispatch(self):
        del self.client.request_payload
        with self.assertRaisesRegex(RuntimeErrorBase, 'wire_contract_missing'):
            await self.run_case()
        self.client.complete.assert_not_awaited()

    async def test_prior_failure_count_and_invalid_state(self):
        self.client.complete.side_effect = ModelError('provider_http_524')
        with self.assertRaises(GenerationRetriesExhausted):
            await self.run_case(prior_failures=1)
        self.assertEqual(self.client.complete.await_count, 3)
        self.client.complete.reset_mock()
        with self.assertRaisesRegex(RuntimeErrorBase, 'generation_retry_state_invalid'):
            await self.run_case(prior_failures=True)
        self.client.complete.assert_not_awaited()


class NativeMessagesInjection(fixture.SelectorTests):
    async def test_injected_executor_runs_through_native_chat_and_manifest(self):
        self.engine.generation_executor = retry.audited_messages_complete
        turn = await self.turn('人工原生主回复检查')
        payload = lambda model, messages: {'model': model['name'], 'messages': messages}
        with patch.object(self.model, 'request_payload', side_effect=payload, create=True), patch.object(self.model, 'complete', new=AsyncMock(return_value=('主回复', {'total_tokens': 5}))) as call:
            await self.engine._run(turn['id'])
        self.assertEqual(self.engine.get(turn['id'])['status'], 'completed')
        self.assertEqual(call.await_count, 1)
        self.assertNotIn('_request_deadline', call.await_args.args[0])
        with self.store.connection() as db:
            usage = json.loads(db.execute('SELECT usage_json FROM attempts WHERE turn_id=?', (turn['id'],)).fetchone()[0])
            self.assertEqual(usage['generation_retry']['version'], retry.VERSION)
            self.assertIsNone(usage['generation_retry']['attempts'][0]['timeout_seconds'])
            self.assertEqual(db.execute('SELECT count(*) FROM context_manifests WHERE turn_id=?', (turn['id'],)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM quota_reservations WHERE state='active'").fetchone()[0], 0)

    async def test_default_executor_still_uses_existing_retry_policy(self):
        self.assertIsNone(self.engine.generation_executor)
        turn = await self.turn('人工旧默认兼容检查')
        with patch.object(self.model, 'request_payload', side_effect=AssertionError('default must not use Messages contract'), create=True), patch.object(self.model, 'complete', new=AsyncMock(return_value=('主回复', {'total_tokens': 5}))):
            await self.engine._run(turn['id'])
        self.assertEqual(self.engine.get(turn['id'])['status'], 'completed')
        with self.store.connection() as db:
            usage = json.loads(db.execute('SELECT usage_json FROM attempts WHERE turn_id=?', (turn['id'],)).fetchone()[0])
        self.assertEqual(usage['generation_retry']['version'], 'generation-retry-v1')
        self.assertIsNotNone(usage['generation_retry']['attempts'][0]['timeout_seconds'])


for name in dir(fixture.SelectorTests):
    if name.startswith('test_') and name not in NativeMessagesInjection.__dict__:
        setattr(NativeMessagesInjection, name, None)


if __name__ == '__main__':
    unittest.main()
