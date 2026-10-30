"""Local transport only: bounded waits, phase evidence, no external calls."""
import asyncio
import json
import unittest
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from persona_runtime.chat import ChatCompletionsClient, ModelError


class DeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls=0;self.delay=0;self.body_delay=0
        async def handler(request):
            self.calls+=1
            await request.json()
            await asyncio.sleep(self.delay)
            result={'choices':[{'message':{'content':'received'},'finish_reason':'stop'}]}
            if self.body_delay:
                response=web.StreamResponse(headers={'Content-Type':'application/json'})
                await response.prepare(request)
                await asyncio.sleep(self.body_delay)
                try:await response.write(json.dumps(result).encode())
                except ConnectionResetError:pass
                return response
            return web.json_response(result)
        app=web.Application();app.router.add_post('/chat/completions',handler)
        self.server=TestServer(app);await self.server.start_server();self.addAsyncCleanup(self.server.close)
        self.model={'base_url':str(self.server.make_url('')).rstrip('/'),'name':'fake',
                    'encrypted_key':'','timeout':.15,'max_tokens':50}
        self.messages=[{'role':'user','content':'private-input-never-log'}]

    async def test_configured_sixty_has_no_hidden_five_second_cap(self):
        original=aiohttp.ClientSession;captured=[]
        def session(**kwargs):
            captured.append(kwargs['timeout']);return original(**kwargs)
        self.model['timeout']=60
        with patch('persona_runtime.chat.aiohttp.ClientSession',side_effect=session):
            result=await ChatCompletionsClient().complete(self.model,self.messages)
        self.assertEqual(captured[0].total,60);self.assertEqual(captured[0].connect,60)
        self.assertTrue(result.diagnostics['request_headers_sent'])
        self.assertEqual(self.calls,1)

    async def test_connect_failure_is_distinct_and_body_free(self):
        async def delayed(*args,**kwargs):await asyncio.sleep(.4)
        with patch.object(aiohttp.TCPConnector,'_create_connection',delayed):
            with self.assertRaises(ModelError) as caught:
                await ChatCompletionsClient().complete(self.model,self.messages)
        self.assertEqual(str(caught.exception),'provider_connection_timeout')
        d=caught.exception.diagnostics
        self.assertEqual(d['phase'],'connecting');self.assertFalse(d['request_headers_sent'])
        self.assertLess(d['elapsed_ms'],500);self.assertEqual(self.calls,0)
        self.assertNotIn('private-input',json.dumps(d));self.assertNotIn('127.0.0.1',json.dumps(d))

    async def test_headers_timeout_once_without_retry(self):
        self.delay=.35
        with self.assertRaises(ModelError) as caught:
            await ChatCompletionsClient().complete(self.model,self.messages)
        self.assertEqual(str(caught.exception),'provider_timeout')
        d=caught.exception.diagnostics
        self.assertEqual(d['phase'],'awaiting_headers');self.assertTrue(d['request_headers_sent'])
        self.assertFalse(d['response_headers_received']);self.assertEqual(self.calls,1)

    async def test_body_timeout_is_not_connection_failure(self):
        self.body_delay=.35
        with self.assertRaises(ModelError) as caught:
            await ChatCompletionsClient().complete(self.model,self.messages)
        d=caught.exception.diagnostics
        self.assertEqual(d['phase'],'reading_body');self.assertTrue(d['response_headers_received'])
        self.assertEqual(str(caught.exception),'provider_timeout');self.assertEqual(self.calls,1)

    async def test_slow_connect_and_response_share_one_budget(self):
        original=aiohttp.TCPConnector._create_connection
        async def delayed(*args,**kwargs):
            await asyncio.sleep(.1);return await original(*args,**kwargs)
        self.delay=.1
        with patch.object(aiohttp.TCPConnector,'_create_connection',delayed):
            with self.assertRaises(ModelError) as caught:
                await ChatCompletionsClient().complete(self.model,self.messages)
        self.assertLess(caught.exception.diagnostics['elapsed_ms'],350)
        self.assertEqual(self.calls,1)

    async def test_cancellation_propagates_without_retry(self):
        self.delay=.5;self.model['timeout']=60
        task=asyncio.create_task(ChatCompletionsClient().complete(self.model,self.messages))
        for _ in range(100):
            if self.calls:break
            await asyncio.sleep(.005)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(self.calls,1)
