"""Official DeepSeek Chat Completions contract; loopback HTTP only, no API key."""
import asyncio
from copy import deepcopy
import json
import time
import unittest

from aiohttp import web
from aiohttp.test_utils import TestServer
from persona_runtime.archive import fingerprint
from persona_runtime.chat import ChatCompletionsClient,ModelError,model_tls_context
from persona_runtime.provider_compat import build_request,describe_request,endpoint,is_deepseek,usage_fields
from persona_runtime.reply_batch import expression_snapshot,prompt_contract,validate_reply


class Contracts(unittest.TestCase):
    def model(self,**kwargs):return {'name':'deepseek-flash','base_url':'https://api.deepseek.com/v1','max_tokens':512,**kwargs}

    def test_roots_v1_and_pasted_full_endpoint(self):
        for base in ('https://api.deepseek.com','https://api.deepseek.com/v1','https://api.deepseek.com/v1/chat/completions/'):
            model=self.model(base_url=base)
            self.assertTrue(endpoint(model).endswith('/chat/completions'))
            self.assertNotIn('/chat/completions/chat/completions',endpoint(model))
            self.assertTrue(endpoint(model,'models').endswith('/models'))
            self.assertNotIn('/chat/completions/models',endpoint(model,'models'))
        for path in ('/anthropic','/v1/responses'):
            with self.assertRaisesRegex(ValueError,'provider_endpoint_incompatible'):endpoint(self.model(base_url='https://api.deepseek.com'+path))

    def test_known_model_or_exact_host_not_hidden_gateway_guess(self):
        self.assertTrue(is_deepseek(self.model(base_url='http://127.0.0.1:1234/v1')))
        self.assertTrue(is_deepseek(self.model(name='future-official-name')))
        self.assertFalse(is_deepseek(self.model(base_url='https://api.deepseek.com.example.org/v1',name='grok-4.7')))
        self.assertFalse(is_deepseek(self.model(base_url='https://example.org/v1',name='custom-deepseek-alias')))

    def test_structured_versions_use_json_object_without_prompt_or_budget_mutation(self):
        for mode in ('context_structured','natural_structured'):
            for version in (1,3):
                model=self.model(_reply_mode=mode,_expression_snapshot=expression_snapshot(mode,version=version))
                messages=[{'role':'system','content':prompt_contract(mode,model['_expression_snapshot'])},{'role':'user','content':'人工材料'}]
                before=deepcopy(messages);payload,contract=build_request(model,messages)
                self.assertEqual(payload['response_format'],{'type':'json_object'})
                self.assertEqual(payload['thinking'],{'type':'disabled'});self.assertEqual(payload['max_tokens'],512)
                self.assertFalse(payload['stream']);self.assertEqual(messages,before)
                self.assertNotIn('json_schema',payload);self.assertEqual(contract['response_format'],'json_object')

    def test_generic_schema_and_compile_transport_are_unchanged(self):
        model=self.model(name='grok-4.7',base_url='https://example.org/v1',_reply_mode='natural_structured')
        payload,_=build_request(model,[{'role':'user','content':'generic'}])
        self.assertEqual(payload['response_format']['type'],'json_schema');self.assertNotIn('thinking',payload)
        payload,_=build_request(model|{'_disposition_compile':True},[{'role':'user','content':'JSON'}])
        self.assertNotIn('response_format',payload)

    def test_legacy_alias_does_not_falsely_claim_non_thinking(self):
        payload,contract=build_request(self.model(name='deepseek-reasoner',_json_output=True),[{'role':'system','content':'Return JSON'}])
        self.assertNotIn('thinking',payload);self.assertEqual(contract['thinking'],'provider_default')

    def test_numeric_usage_whitelist_drops_content_and_invalid_values(self):
        usage=usage_fields({'prompt_tokens':9,'total_tokens':True,'prompt_cache_hit_tokens':3,
            'prompt_cache_miss_tokens':-1,'prompt_tokens_details':{'cached_tokens':3,'secret':'sensitive'},
            'completion_tokens_details':{'reasoning_tokens':2,'reasoning_content':'sensitive'},'other':'sensitive'},deepseek=True)
        self.assertEqual(usage,{'prompt_tokens':9,'prompt_cache_hit_tokens':3,'prompt_tokens_details':{'cached_tokens':3},'completion_tokens_details':{'reasoning_tokens':2}})


class HTTP(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests=[];self.status=200;self.finish='stop';self.delay=0;self.heartbeat=False
        self.raw='{"schema_version":2,"behavior_intent":"respond","messages":[{"content":"收到","content_kind":"chat","cancel_if_new_input":false}]}'
        async def provider(request):
            self.requests.append(await request.json())
            value={'model':'deepseek-flash','system_fingerprint':'a'*32,'choices':[{'message':{'content':self.raw,'reasoning_content':'synthetic-private-thought'},'finish_reason':self.finish}],
                   'usage':{'prompt_tokens':9,'completion_tokens':5,'total_tokens':14,'prompt_cache_hit_tokens':6,'prompt_cache_miss_tokens':3,
                            'completion_tokens_details':{'reasoning_tokens':2,'not_saved':'private'},'prompt_tokens_details':{'cached_tokens':6}}}
            if self.heartbeat:
                response=web.StreamResponse(headers={'Content-Type':'application/json'});await response.prepare(request)
                try:
                    await response.write(b' \n \n');await asyncio.sleep(self.delay)
                    await response.write(json.dumps(value).encode());await response.write_eof()
                except (ConnectionResetError,OSError):pass
                return response
            return web.json_response(value,status=self.status)
        app=web.Application();app.router.add_post('/v1/chat/completions',provider)
        self.server=TestServer(app);await self.server.start_server();self.addAsyncCleanup(self.server.close)
        self.model={'base_url':str(self.server.make_url('/v1')),'name':'deepseek-flash','encrypted_key':'','timeout':1,'max_tokens':512,'_reply_mode':'natural_structured'}
        self.messages=[{'role':'system','content':prompt_contract('natural_structured')},{'role':'user','content':'人工输入'}]

    async def test_wire_content_usage_and_body_free_metadata(self):
        before=fingerprint(self.messages);reply=await ChatCompletionsClient().complete(self.model,self.messages)
        self.assertEqual(validate_reply(reply[0],'natural_structured').text,'收到')
        self.assertEqual(self.requests[0]['response_format'],{'type':'json_object'});self.assertEqual(self.requests[0]['thinking'],{'type':'disabled'})
        self.assertEqual(reply[1]['prompt_cache_hit_tokens'],6);self.assertEqual(reply[1]['completion_tokens_details']['reasoning_tokens'],2)
        self.assertEqual(reply.diagnostics['response_model'],'deepseek-flash');self.assertTrue(reply.diagnostics['reasoning_present'])
        self.assertNotIn('synthetic-private-thought',json.dumps(reply.diagnostics));self.assertNotIn('private',json.dumps(reply[1]))
        self.assertEqual(fingerprint(self.messages),before);self.assertEqual(len(self.requests),1)

    async def test_pasted_endpoint_and_auxiliary_markers(self):
        from persona_runtime.disposition_compile import PROMPT
        from persona_runtime.situation_observation import PROMPT as OBSERVATION
        from persona_runtime.character_selector_protocol import SYSTEM_V2
        for flags,prompt in (({'_json_output':True},SYSTEM_V2),({'_disposition_compile':True},PROMPT),({'_disposition_compile':True,'_situation_observation':True},OBSERVATION)):
            model=self.model|{'base_url':str(self.server.make_url('/v1/chat/completions')),'_reply_mode':None}|flags
            await ChatCompletionsClient().complete(model,[{'role':'system','content':prompt},{'role':'user','content':'人工资料'}])
            self.assertEqual(self.requests[-1]['response_format'],{'type':'json_object'})
            self.assertEqual(self.requests[-1]['thinking'],{'type':'disabled'})
        self.assertEqual(len(self.requests),3)

    async def test_plain_probe_has_no_json_mode(self):
        await ChatCompletionsClient().complete(self.model|{'_reply_mode':'legacy_full'},[{'role':'user','content':'Reply OK'}])
        self.assertNotIn('response_format',self.requests[0]);self.assertEqual(self.requests[0]['thinking'],{'type':'disabled'})

    async def test_missing_json_instruction_is_zero_requests(self):
        with self.assertRaisesRegex(ModelError,'provider_json_instruction_missing'):
            await ChatCompletionsClient().complete(self.model,[{'role':'user','content':'没有格式约定'}])
        self.assertEqual(self.requests,[])

    async def test_blank_and_reasoning_only_does_not_become_reply_or_retry(self):
        self.raw='   \n'
        with self.assertRaisesRegex(ModelError,'provider_empty_response') as caught:await ChatCompletionsClient().complete(self.model,self.messages)
        self.assertEqual(caught.exception.usage['total_tokens'],14);self.assertEqual(len(self.requests),1)
        self.assertNotIn('synthetic-private-thought',str(caught.exception));self.assertTrue(caught.exception.diagnostics['reasoning_present'])

    async def test_incomplete_and_aborted_answers_fail_without_second_request(self):
        for finish,code in (('length','provider_output_truncated'),('aborted','provider_aborted'),
                            ('insufficient_system_resource','provider_insufficient_system_resource'),('content_filter','provider_content_filtered'),('tool_calls','provider_tool_call_not_supported')):
            self.finish=finish;before=len(self.requests)
            with self.assertRaisesRegex(ModelError,code) as caught:await ChatCompletionsClient().complete(self.model,self.messages)
            self.assertEqual(caught.exception.usage['total_tokens'],14);self.assertEqual(len(self.requests),before+1)

    async def test_http_error_is_not_schema_downgrade_retry(self):
        for status in (400,401,402,422,429,500,503):
            self.status=status;before=len(self.requests)
            with self.assertRaisesRegex(ModelError,'provider_http_'+str(status)):await ChatCompletionsClient().complete(self.model,self.messages)
            self.assertEqual(len(self.requests),before+1)

    async def test_keepalive_whitespace_and_one_total_deadline(self):
        self.heartbeat=True;self.delay=.005
        client=ChatCompletionsClient(ssl_context=model_tls_context())
        reply=await client.complete(self.model,self.messages);self.assertTrue(reply[0])
        self.delay=.6;before=len(self.requests)
        with self.assertRaisesRegex(ModelError,'provider_timeout'):
            await client.complete(self.model|{'timeout':.35,'_request_deadline':time.monotonic()+.4},self.messages)
        self.assertEqual(len(self.requests),before+1)


from tests.test_reply_protocol import ProtocolIntegrationTests as PipelineBase


class Pipeline(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=PipelineBase.asyncSetUp
    cleanup_engine=PipelineBase.cleanup_engine
    submit=PipelineBase.submit
    manifest=PipelineBase.manifest

    async def test_deepseek_declared_before_dispatch_and_strict_local_validation(self):
        self.config.update({'model':{'name':'deepseek-flash'}})
        turn=await self.submit();self.assertEqual(turn['status'],'completed')
        m=self.manifest(turn['id']);wire=m['provider_request_contract']
        self.assertEqual(wire['response_format'],'json_object');self.assertEqual(wire['thinking'],'disabled')
        self.assertEqual(self.requests[0]['response_format'],{'type':'json_object'})
        self.assertEqual(m['input_snapshot_hash'],fingerprint(self.requests[0]['messages']))
        self.raw='{"schema_version":2,"schema_version":2,"messages":[]}'
        failed=await self.submit();self.assertEqual(failed['error_code'],'reply_schema_invalid')
        self.assertEqual(failed['context']['reply_validation']['reason'],'duplicate_json_key')
        self.assertIsNone(failed['reply_event_id']);self.assertEqual(len(self.requests),2)


# unittest discovers imported TestCase objects too; only the concrete local
# subclass belongs to this file. Generic wire regression runs in its own suite.
del PipelineBase


class AuxiliaryManifests(unittest.IsolatedAsyncioTestCase):
    async def check_fixture(self,fixture,method,field,task_type,*,selector=False):
        case=fixture(method)
        case.setUp()
        await case.asyncSetUp()
        try:
            case.config.update({field:{'base_url':'https://api.deepseek.com/v1','name':'deepseek-flash'}})
            if selector:
                # The existing fake routes by name. Exact official host selects
                # the same wire profile without any network request.
                original=case.enable
                def enable():
                    original();case.config.update({field:{'base_url':'https://api.deepseek.com/v1'}})
                case.enable=enable
            await getattr(case,method)()
            with case.store.connection() as db:
                rows=db.execute('SELECT manifest_json FROM context_manifests WHERE task_type=?',(task_type,)).fetchall()
            self.assertTrue(rows)
            for row in rows:
                manifest=json.loads(row[0]);wire=manifest['provider_request_contract']
                self.assertEqual(wire['profile'],'deepseek-chat-v1')
                self.assertEqual(wire['thinking'],'disabled')
                self.assertEqual(wire['response_format'],'text' if manifest.get('purpose')=='provider_probe' else 'json_object')
        finally:
            await case.asyncTearDown()
            case.doCleanups()

    async def test_selector_manifest_declares_json_object(self):
        from tests.test_m754r2 import SelectorTests
        await self.check_fixture(SelectorTests,'test_success_separate_attempts_and_manifest','character_selector','character_selection',selector=True)

    async def test_compiler_manifest_declares_json_object(self):
        from tests.test_m761 import M761
        await self.check_fixture(M761,'test_compile_once_manifest_quota_and_no_auto_confirm','character_selector','disposition_compilation')

    async def test_observation_manifest_declares_json_object(self):
        from tests.test_m763_repair import Repair
        await self.check_fixture(Repair,'test_observation_real_path_atomic_manifest_and_no_replay','character_selector','situation_observation')

    async def test_reflection_and_probe_declare_different_wire_formats(self):
        from tests.test_m72 import ContextTests
        await self.check_fixture(ContextTests,'test_reflection_calls_and_probes_persist_exact_manifest_before_send','model','reflection')
