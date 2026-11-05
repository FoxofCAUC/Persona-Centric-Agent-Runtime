"""Local SSE and lifecycle checks; no provider traffic or credentials."""
import asyncio
from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from persona_runtime.archive import fingerprint
from persona_runtime.chat import ChatCompletionsClient, ModelError
from persona_runtime.chat_stream import ChatStream
from persona_runtime.generation_retry import audited_complete, GenerationRetriesExhausted
from persona_runtime.provider_compat import build_request, describe_request


PROFILE={'version':'chat-stream-unlimited-v1'}


def event(delta=None,finish=None,*,usage=None):
    value={'choices':[] if delta is None and finish is None else [
        {'index':0,'delta':delta or {},'finish_reason':finish}]}
    if usage is not None:value['usage']=usage
    return ('data: '+json.dumps(value,ensure_ascii=False)+'\n\n').encode()


def complete(text='你好',*,usage=None):
    return event({'content':text})+event({},'stop')+event(usage=usage or {'completion_tokens':3})+b'data: [DONE]\n\n'


class WireAndParser(unittest.TestCase):
    def setUp(self):
        self.model={'name':'grok-4.7','base_url':'https://example.invalid/v1','max_tokens':8192,
                    'timeout':180,'_reply_mode':'context_full'}
        self.messages=[{'role':'user','content':'人工输入'}]

    def test_legacy_request_and_contract_keep_original_shape_and_order(self):
        wire,contract=build_request(self.model,self.messages)
        self.assertEqual(list(wire),['model','messages','max_tokens','stream'])
        self.assertEqual(wire,{'model':'grok-4.7','messages':self.messages,'max_tokens':8192,'stream':False})
        self.assertEqual(contract['version'],'provider-wire-v1')
        self.assertNotIn('generation_version',contract)

    def test_explicit_profile_omits_limit_preserves_format_and_low_reasoning(self):
        model={**self.model,'_reply_mode':'context_structured','_generation_profile':PROFILE}
        before=build_request({k:v for k,v in model.items() if k!='_generation_profile'},self.messages)[0]
        actual,contract=build_request(model,self.messages)
        expected={k:v for k,v in before.items() if k not in ('max_tokens','stream')}
        self.assertEqual({k:v for k,v in actual.items() if k not in ('stream','stream_options')},expected)
        self.assertTrue(actual['stream']);self.assertTrue(actual['stream_options']['include_usage'])
        self.assertEqual(actual['reasoning_effort'],'low')
        self.assertEqual(contract['timeout_policy'],'none')
        # A new request does not need placeholder numeric limits.
        model.pop('max_tokens');model.pop('timeout')
        self.assertNotIn('max_tokens',build_request(model,self.messages)[0])

    def test_unknown_or_extra_profile_fields_fail_before_transport(self):
        for value in ({'version':'future'}, {'version':PROFILE['version'],'timeout':60},True):
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'generation_profile_invalid'):
                describe_request({**self.model,'_generation_profile':value})

    def test_fragmented_utf8_crlf_comments_and_cumulative_usage(self):
        parser=ChatStream(self.model)
        data=(b': keepalive\n\n'+event({'reasoning_content':'not-visible'})+
              event({'content':'雨后'})+event(usage={'completion_tokens':5})+
              event({'content':'公园'})+event({},'stop')+
              event(usage={'completion_tokens':8,'total_tokens':20})+b'data: [DONE]\n\n')
        data=data.replace(b'\n',b'\r\n')
        for byte in data:parser.feed(bytes((byte,)))
        text,usage,info=parser.finish()
        self.assertEqual(text,'雨后公园');self.assertEqual(usage['completion_tokens'],8)
        self.assertEqual(usage['total_tokens'],20)
        self.assertTrue(info['reasoning_present']);self.assertTrue(info['stream_complete'])
        self.assertLessEqual(info['first_event_ms'],info['first_content_delta_ms'])
        self.assertNotIn('not-visible',json.dumps(info))

    def test_disconnect_never_returns_partial_answer_or_partial_usage(self):
        parser=ChatStream(self.model)
        parser.feed(event({'content':'不可投递的部分'})+event(usage={'completion_tokens':4}))
        with self.assertRaisesRegex(ModelError,'provider_network_error') as caught:parser.finish()
        self.assertIsNone(caught.exception.usage)

    def test_done_requires_finish_and_no_more_content(self):
        for data in (event({'content':'x'})+b'data: [DONE]\n\n',
                     event({'content':'x'})+event({},'stop')+event({'content':'late'})):
            with self.subTest(data=data),self.assertRaisesRegex(ModelError,'provider_invalid_response'):
                ChatStream(self.model).feed(data)

    def test_length_error_retains_final_usage_but_no_success(self):
        parser=ChatStream(self.model)
        parser.feed(event({'content':'partial'})+event({},'length')+
                    event(usage={'completion_tokens':12})+b'data: [DONE]\n\n')
        with self.assertRaisesRegex(ModelError,'provider_output_truncated') as caught:parser.finish()
        self.assertEqual(caught.exception.usage,{'completion_tokens':12})

    def test_tools_filter_empty_and_bad_json_are_not_chat_answers(self):
        cases=[(event({'tool_calls':[{'id':'x'}]}),'provider_tool_call_not_supported'),
               (event({},'content_filter')+b'data: [DONE]\n\n','provider_content_filtered'),
               (event({},'stop')+b'data: [DONE]\n\n','provider_empty_response'),
               (b'data: not json\n\n','provider_invalid_response')]
        for data,code in cases:
            with self.subTest(code=code),self.assertRaisesRegex(ModelError,code):
                parser=ChatStream(self.model);parser.feed(data);parser.finish()


class LocalStreaming(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.delay=0;self.calls=[];self.json_fallback=False;self.hold=False;self.linger=False
        self.started=asyncio.Event();self.release=asyncio.Event()
        async def handler(request):
            self.calls.append(await request.json());self.started.set()
            if self.json_fallback:return web.json_response({'choices':[{'message':{'content':'fallback'}}]})
            response=web.StreamResponse(headers={'Content-Type':'text/event-stream; charset=utf-8'})
            await response.prepare(request)
            try:
                await response.write(event({'reasoning_content':'not-user-text'}))
                if self.hold:await self.release.wait()
                await asyncio.sleep(self.delay)
                await response.write(complete())
                if self.linger:await self.release.wait()
                await response.write_eof()
            except (ConnectionResetError,RuntimeError):pass
            return response
        app=web.Application();app.router.add_post('/chat/completions',handler)
        self.server=TestServer(app);await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.model={'base_url':str(self.server.make_url('')).rstrip('/'),'name':'fake',
                    'encrypted_key':'','timeout':.01,'max_tokens':1,'_generation_profile':PROFILE}
        self.messages=[{'role':'user','content':'private-input-must-not-appear-in-diagnostics'}]

    async def test_no_timeout_at_transport_or_retry_and_actual_wire_hash(self):
        self.delay=.04;captured=[];saved=[];original=aiohttp.ClientSession
        def session(**kw):captured.append(kw['timeout']);return original(**kw)
        with patch('persona_runtime.chat.aiohttp.ClientSession',side_effect=session):
            result=await audited_complete(ChatCompletionsClient(),self.model,self.messages,
                validate=lambda _:None,save=lambda value:saved.append(deepcopy(value)),before_attempt=lambda _:None)
        timeout=captured[0]
        self.assertTrue(all(getattr(timeout,k) is None for k in ('total','connect','sock_connect','sock_read')))
        self.assertEqual(len(self.calls),1);self.assertEqual(result[0],'你好')
        self.assertEqual(result[1]['generation_retry']['attempts'][0]['request_hash'],fingerprint(self.calls[0]))
        self.assertIsNone(saved[-1]['generation_retry']['attempts'][0]['timeout_seconds'])
        self.assertNotIn('private-input',json.dumps(result.diagnostics))
        self.assertNotIn('not-user-text',json.dumps(result.diagnostics))

    async def test_server_json_fallback_rejected_without_second_dispatch(self):
        self.json_fallback=True
        with self.assertRaisesRegex(ModelError,'provider_stream_expected'):
            await ChatCompletionsClient().complete(self.model,self.messages)
        self.assertEqual(len(self.calls),1)

    async def test_done_returns_without_waiting_for_server_connection_close(self):
        self.linger=True
        try:
            result=await asyncio.wait_for(ChatCompletionsClient().complete(self.model,self.messages),1)
            self.assertEqual(result[0],'你好')
        finally:self.release.set()

    async def test_unlimited_request_ignores_stale_numeric_deadline(self):
        self.delay=.03;self.model['_request_deadline']=0
        result=await ChatCompletionsClient().complete(self.model,self.messages)
        self.assertEqual(result[0],'你好')

    async def test_cancel_incomplete_stream_leaves_terminal_without_retry(self):
        self.hold=True;saved=[]
        task=asyncio.create_task(audited_complete(ChatCompletionsClient(),self.model,self.messages,
            validate=lambda _:None,save=lambda value:saved.append(deepcopy(value)),before_attempt=lambda _:None))
        await self.started.wait();await asyncio.sleep(.01)
        self.assertFalse(task.done());task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.release.set()
        self.assertEqual(len(self.calls),1)
        row=saved[-1]['generation_retry']['attempts'][0]
        self.assertEqual(row['status'],'interrupted');self.assertFalse(row['usage_known'])


class UnlimitedRetry(unittest.IsolatedAsyncioTestCase):
    async def test_exhaustion_never_dispatches_fifth_request(self):
        model={'name':'fake','base_url':'https://example.invalid','_generation_profile':PROFILE}
        client=type('Client',(),{})();client.complete=AsyncMock(side_effect=ModelError('provider_network_error'))
        saved=[]
        with self.assertRaises(GenerationRetriesExhausted):
            await audited_complete(client,model,[],validate=lambda _:None,
                save=lambda value:saved.append(deepcopy(value)),before_attempt=lambda _:None,sleep=AsyncMock())
        self.assertEqual(client.complete.await_count,4)
        rows=saved[-1]['generation_retry']['attempts']
        self.assertEqual([r['index'] for r in rows],[0,1,2,3])
        self.assertEqual([r['status'] for r in rows],['failed']*4)

    async def test_four_attempts_one_hash_and_success_not_replayed(self):
        model={'name':'grok-4.7','base_url':'https://example.invalid/v1','_generation_profile':PROFILE}
        client=type('Client',(),{})();client.complete=AsyncMock(side_effect=[ModelError('provider_network_error')]*3+[('完整',None)])
        sleeps=AsyncMock();saved=[]
        result=await audited_complete(client,model,[{'role':'user','content':'人工'}],validate=lambda _:None,
            save=lambda value:saved.append(deepcopy(value)),before_attempt=lambda _:None,sleep=sleeps)
        self.assertEqual(client.complete.await_count,4)
        self.assertEqual([call.args[0] for call in sleeps.await_args_list],[1,2,4])
        rows=result[1]['generation_retry']['attempts']
        self.assertEqual(len({r['request_hash'] for r in rows}),1)
        self.assertTrue(all(r['timeout_seconds'] is None for r in rows))
        self.assertFalse(rows[-1]['usage_known'])

    async def test_cancel_awaits_transport_cleanup(self):
        entered=asyncio.Event();closed=asyncio.Event();saved=[]
        class Client:
            async def complete(self,*_):
                entered.set()
                try:await asyncio.Event().wait()
                finally:await asyncio.sleep(.02);closed.set()
        model={'name':'fake','base_url':'https://example.invalid','_generation_profile':PROFILE}
        task=asyncio.create_task(audited_complete(Client(),model,[],validate=lambda _:None,
            save=lambda value:saved.append(deepcopy(value)),before_attempt=lambda _:None))
        await entered.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(closed.is_set())
        self.assertEqual(saved[-1]['generation_retry']['attempts'][0]['status'],'interrupted')


if __name__=='__main__':unittest.main()
