"""Native JSON/SSE, terminal accounting and existing-attempt recovery contracts."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from persona_runtime.chat import ChatCompletionsClient, ModelError
from persona_runtime.deepseek_native_response import NativeResponse
from persona_runtime.knowledge_native_store import save_receipt, load_receipt
from persona_runtime.models import RuntimeErrorBase
from tests.test_m9r0_native_contract import model, response, sse


class Folding(unittest.TestCase):
    def test_json_extensions_remain_separate_from_text_diagnostics_and_usage(self):
        parser = NativeResponse(); raw = response(); parser.consume(raw); value = parser.complete()
        self.assertEqual(len(value), 2); self.assertEqual(value[0], '公开资料摘要')
        self.assertEqual(value.native['assistant'], raw['choices'][0]['message'])
        self.assertEqual(value[1]['server_tool_use']['tool_search'], 2)
        self.assertNotIn('synthetic', json.dumps(value.diagnostics))
        self.assertNotIn('synthetic', json.dumps(value[1]))

    def test_sse_every_byte_boundary_keeps_unicode_steps_citations_and_final_usage(self):
        raw = sse().encode('utf-8')
        for size in (1, 2, 7, 65536):
            statuses = []; parser = NativeResponse(stream=True, progress=statuses.append)
            for start in range(0, len(raw), size): parser.feed(raw[start:start+size])
            value = parser.complete()
            self.assertEqual(value[0], '中文😀'); self.assertEqual(value[1]['total_tokens'], 17)
            self.assertEqual(value.native['assistant']['server_tool_calls'], response()['choices'][0]['message']['server_tool_calls'])
            self.assertNotIn('synthetic-step', value.native['assistant'].get('reasoning_content', ''))
            self.assertTrue(statuses)
            self.assertNotIn('synthetic', json.dumps(statuses))

    def test_error_usage_is_read_before_failure_without_done(self):
        for stream in (False, True):
            parser = NativeResponse(stream=stream)
            with self.assertRaisesRegex(ModelError, 'native_internal_error') as caught:
                if stream: parser.feed(sse(done=False, internal_error=True).encode())
                else: parser.consume({'error': {'message': 'synthetic'}, 'usage': response()['usage']})
            error = caught.exception
            self.assertEqual(error.usage['total_tokens'], 17)
            self.assertEqual(error.usage['server_tool_use']['tool_fetch'], 1)
            self.assertEqual(error.diagnostics['native_terminal'], 'failed')
            self.assertNotIn('synthetic', json.dumps(error.diagnostics))

    def test_unknown_stream_keeps_observed_usage_only_in_protocol(self):
        parser = NativeResponse(stream=True); parser.feed(sse(done=False).encode())
        with self.assertRaisesRegex(ModelError, 'native_unknown') as caught: parser.complete()
        self.assertIsNone(caught.exception.usage)
        self.assertEqual(caught.exception.native['last_observed_usage']['total_tokens'], 17)
        self.assertEqual(caught.exception.diagnostics['native_terminal'], 'unknown')

    def test_truncation_refusal_and_aborted_are_known_with_usage(self):
        for finish in ('length', 'content_filter', 'aborted', 'insufficient_system_resource'):
            parser = NativeResponse(); parser.consume(response(finish=finish, content=''))
            with self.assertRaises(ModelError) as caught: parser.complete()
            self.assertEqual(caught.exception.diagnostics['native_terminal'], 'failed')
            self.assertEqual(caught.exception.usage['total_tokens'], 17)

    def test_progress_and_done_without_finish_cannot_complete(self):
        for raw in ('data: {"choices":[{"delta":{},"server_tool_event":{"type":"call_completed"}}]}\n\n',
                    'data: [DONE]\n\n'):
            parser = NativeResponse(stream=True)
            with self.assertRaises(ModelError):
                parser.feed(raw.encode()); parser.complete()

    def test_malformed_fields_and_byte_offsets_do_not_become_completed_answers(self):
        for change in ({'content': 7}, {'server_tool_calls': None}, {'annotations': None},
                       {'annotations': [{'type': 'url_citation', 'url_citation': {
                           'url': 'https://example.org', 'start_index': 0, 'end_index': 9999}}]}):
            parser = NativeResponse(); raw = response(); raw['choices'][0]['message'].update(change)
            parser.consume(raw)
            with self.assertRaises(ModelError): parser.complete()

    def test_explicit_invalid_tool_usage_never_looks_like_documented_omission(self):
        parser = NativeResponse(); raw = response(); raw['usage']['server_tool_use'] = None
        parser.consume(raw); value = parser.complete()
        from persona_runtime.deepseek_native_contract import tool_accounting
        self.assertIsNone(tool_accounting(value[1], terminal=True)['estimated_tool_amount'])

    def test_receipt_scope_attempt_and_corruption_checks_share_existing_tables(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE organization_attempts(id TEXT,task_id TEXT)')
        db.execute('CREATE TABLE knowledge_control(key TEXT PRIMARY KEY,value TEXT)')
        db.execute("INSERT INTO organization_attempts VALUES('a','t')")
        task = {'id': 't', 'persona_id': 'p', 'channel': 'desktop', 'session_id': 's',
                'model_snapshot': json.dumps(model())}
        parser = NativeResponse(); parser.consume(response()); packet = parser.complete().native
        audit = save_receipt(db, task, 'a', packet)
        self.assertEqual(load_receipt(db, task, 'a', expected_hash=audit['native_receipt_sha256']), packet)
        for change in ({'persona_id': 'q'}, {'session_id': 'other'}, {'channel': 'qq'}):
            with self.assertRaises(RuntimeErrorBase): load_receipt(db, task | change, 'a')
        with self.assertRaises(RuntimeErrorBase): load_receipt(db, task, 'a', model=model(encrypted_key='different-account'))
        with self.assertRaises(RuntimeErrorBase): load_receipt(db, task, 'a', expected_hash='corrupt')
        with self.assertRaises(RuntimeErrorBase): save_receipt(db, task, 'unknown-attempt', packet)
        changed = deepcopy(packet); changed['assistant']['content'] = 'changed'
        with self.assertRaisesRegex(RuntimeErrorBase, 'conflict'): save_receipt(db, task, 'a', changed)


class Wire(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []; self.status = 200; self.stream = False; self.body = response()
        self.raw = sse(); self.content_type = 'text/event-stream'
        async def provider(request):
            self.requests.append(await request.json())
            if self.stream:
                return web.Response(text=self.raw, content_type=self.content_type, status=self.status)
            return web.json_response(self.body, status=self.status)
        app = web.Application(); app.router.add_post('/chat/completions', provider)
        self.server = TestServer(app); await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.endpoint_patch = patch('persona_runtime.deepseek_native_transport.official_endpoint',
            return_value=str(self.server.make_url('/chat/completions')))
        self.endpoint_patch.start(); self.addCleanup(self.endpoint_patch.stop)

    async def ask(self):
        snapshot = model()
        snapshot['_native_web_profile']['stream'] = self.stream
        return await ChatCompletionsClient(allow_loopback_fixture=True, ).complete(snapshot, [{'role': 'user', 'content': 'public'}])

    async def test_json_and_sse_delegate_to_dedicated_native_transport(self):
        first = await self.ask(); self.stream = True; second = await self.ask()
        self.assertEqual(first.native['outcome'], 'completed')
        self.assertEqual(second.native['outcome'], 'completed')
        self.assertEqual(second[0], '中文😀'); self.assertEqual(len(self.requests), 2)
        self.assertNotIn('response_format', self.requests[0])

    async def test_http_200_error_preserves_billable_tools_without_retry(self):
        self.body = {'error': {'message': 'synthetic'}, 'usage': response()['usage']}
        with self.assertRaisesRegex(ModelError, 'native_internal_error') as caught: await self.ask()
        self.assertEqual(caught.exception.usage['server_tool_use']['tool_search'], 2)
        self.assertEqual(caught.exception.diagnostics['native_terminal'], 'failed')
        self.assertEqual(len(self.requests), 1)


    async def test_stream_error_without_done_is_known_failure(self):
        self.stream = True; self.raw = sse(done=False, internal_error=True)
        with self.assertRaisesRegex(ModelError, 'native_internal_error') as caught: await self.ask()
        self.assertEqual(caught.exception.usage['total_tokens'], 17)
        self.assertEqual(len(self.requests), 1)

    async def test_disconnect_after_final_choice_is_unknown(self):
        self.stream = True; self.raw = sse(done=False)
        with self.assertRaisesRegex(ModelError, 'native_unknown') as caught: await self.ask()
        self.assertIsNone(caught.exception.usage)
        self.assertEqual(caught.exception.native['last_observed_usage']['total_tokens'], 17)

    async def test_malformed_json_and_wrong_content_type_do_not_trigger_fallback(self):
        self.body = {'unexpected': True}
        with self.assertRaises(ModelError): await self.ask()
        self.stream = True; self.content_type = 'application/json'
        with self.assertRaisesRegex(ModelError, 'native_stream_expected'): await self.ask()
        self.assertEqual(len(self.requests), 2)

    async def test_http_failure_is_known_and_keeps_any_returned_usage(self):
        self.status = 503; self.body = {'error': {'message': 'busy'}, 'usage': response()['usage']}
        with self.assertRaisesRegex(ModelError, 'native_http_503') as caught: await self.ask()
        self.assertEqual(caught.exception.diagnostics['native_terminal'], 'failed')
        self.assertEqual(caught.exception.usage['total_tokens'], 17)
        self.assertEqual(len(self.requests), 1)


class UnknownMainGuard(unittest.IsolatedAsyncioTestCase):
    async def check_unknown(self,timeout=False):
        import asyncio
        from persona_runtime.generation_retry import audited_complete
        from persona_runtime.paths import RuntimePaths
        from tests.test_m9r0_native_contract import local_temp
        calls=[];saved=[]
        class Client:
            async def complete(self,snapshot,messages):
                calls.append(1)
                if timeout:
                    try:await asyncio.Event().wait()
                    except asyncio.CancelledError as error:
                        error.diagnostics={'request_headers_sent':True};raise
                raise ModelError('provider_network_error',diagnostics={'request_headers_sent':True,'response_headers_received':True,
                    'stream':True,'stream_complete':False})
        with local_temp() as folder:
            paths=RuntimePaths(Path(folder));paths.initialize()
            plain={key:value for key,value in model().items() if key!='_native_web_profile'}
            if timeout:plain['timeout']=.01
            with self.assertRaisesRegex(ModelError,'provider_effect_unknown'):
                await audited_complete(Client(),plain,[{'role':'user','content':'public'}],validate=lambda text:None,
                    save=lambda value:saved.append(deepcopy(value)),before_attempt=lambda state:None,paths=paths)
        self.assertEqual(len(calls),1)
        row=saved[-1]['generation_retry']['attempts'][0]
        self.assertTrue(row['diagnostics']['effect_unknown']);self.assertFalse(row['usage_known'])

    async def test_sent_main_network_failure_is_not_replayed_as_an_ordinary_retry(self):
        await self.check_unknown()

    async def test_inflight_deadline_is_unknown_and_not_retried(self):
        await self.check_unknown(timeout=True)

    def test_plain_stream_terminal_error_consumes_same_frame_usage_without_done(self):
        from persona_runtime.chat_stream import ChatStream
        parser=ChatStream({'name':'deepseek-flash','base_url':'https://api.deepseek.com/v1'})
        raw={'error':{'message':'synthetic failure'},'choices':[],'usage':response()['usage']}
        with self.assertRaisesRegex(ModelError,'provider_aborted') as caught:
            parser.feed(('data: '+json.dumps(raw)+'\n\n').encode())
        self.assertEqual(caught.exception.usage['total_tokens'],17)
        self.assertTrue(caught.exception.diagnostics['provider_terminal_error'])
        from persona_runtime.generation_retry import effect_unconfirmed
        caught.exception.diagnostics['request_headers_sent']=True
        self.assertFalse(effect_unconfirmed(caught.exception))


if __name__ == '__main__':
    unittest.main()
