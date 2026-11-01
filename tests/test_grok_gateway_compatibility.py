"""Local HTTP evidence for routed model IDs, deadlines and response formats."""
import asyncio
import json
import time
import unittest

from aiohttp import web
from aiohttp.test_utils import TestServer
from persona_runtime.chat import ChatCompletionsClient,ModelError


class GrokGatewayCompatibility(unittest.IsolatedAsyncioTestCase):
    async def test_shared_tls_verifies_certificates_and_hostname(self):
        import ssl
        from persona_runtime.chat import model_tls_context
        context=model_tls_context()
        self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreater(context.cert_store_stats()['x509_ca'],0)
    async def asyncSetUp(self):
        self.payloads=[];self.delay=0
        self.response={'model':'grok-4.7-build','choices':[{'message':{'content':'{"status":"sufficient"}',
                        'reasoning_content':'synthetic-internal-not-an-answer'},'finish_reason':'stop'}],
                       'usage':{'prompt_tokens':12,'completion_tokens':8,'total_tokens':20}}
        async def complete(request):
            self.payloads.append(await request.json());await asyncio.sleep(self.delay)
            return web.json_response(self.response)
        app=web.Application();app.router.add_post('/chat/completions',complete)
        self.server=TestServer(app);await self.server.start_server();self.addAsyncCleanup(self.server.close)
        self.model={'name':'grok-4.7','base_url':str(self.server.make_url('')).rstrip('/'),
                    'encrypted_key':'','timeout':1,'max_tokens':512,'_situation_observation':True,'_disposition_compile':True}
        self.messages=[{'role':'system','content':'Return JSON only.'},{'role':'user','content':'Synthetic task'}]

    async def test_gateway_response_model_alias_is_accepted_without_rewriting_request(self):
        result=await ChatCompletionsClient().complete(self.model,self.messages)
        self.assertEqual(result[0],'{"status":"sufficient"}')
        self.assertEqual(self.payloads[0],{'model':'grok-4.7','messages':self.messages,'max_tokens':512,'stream':False})
        self.assertEqual(result[1]['total_tokens'],20)
        self.assertNotIn('synthetic-internal',result[0])

    async def test_response_after_deadline_is_rejected_but_same_shape_within_budget_succeeds(self):
        # Compressed local timing, not an external latency measurement.
        self.delay=.6
        with self.assertRaises(ModelError) as caught:
            await ChatCompletionsClient().complete(self.model|{'timeout':.5,'_request_deadline':time.monotonic()+.5},self.messages)
        self.assertEqual(str(caught.exception),'provider_timeout')
        self.assertEqual(caught.exception.diagnostics['phase'],'awaiting_headers')
        self.assertTrue(caught.exception.diagnostics['request_headers_sent'])
        result=await ChatCompletionsClient().complete(self.model,self.messages)
        self.assertEqual(result[0],'{"status":"sufficient"}');self.assertEqual(len(self.payloads),2)

    async def test_raw_responses_api_shape_requires_an_adapter(self):
        self.response={'model':'grok-4.7-build','output':[{'type':'message','content':[{'type':'output_text','text':'ok'}]}]}
        with self.assertRaises(ModelError) as caught:await ChatCompletionsClient().complete(self.model,self.messages)
        self.assertEqual(str(caught.exception),'provider_invalid_response');self.assertEqual(len(self.payloads),1)

    async def test_reasoning_without_final_content_is_not_shown_as_answer(self):
        self.response['choices'][0]['message']['content']=None
        with self.assertRaises(ModelError) as caught:await ChatCompletionsClient().complete(self.model,self.messages)
        self.assertEqual(str(caught.exception),'provider_empty_response')
        self.assertEqual(len(self.payloads),1)
