"""Reply protocol repair: local HTTP only, artificial identities, no paid calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web,ClientSession
from aiohttp.test_utils import TestServer
from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore
from persona_runtime.ingress import IngressService
from persona_runtime.models import generic_persona,identifier,RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.reply_protocol import descriptor,response_format,validation_failure
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


def natural():
    return json.dumps({'schema_version':2,'behavior_intent':'respond','messages':[
        {'content':'晚上好','content_kind':'chat','cancel_if_new_input':False},
        {'content':'今天过得怎么样','content_kind':'chat','cancel_if_new_input':False}]},ensure_ascii=False)


class ProtocolDiagnosticsTests(unittest.TestCase):
    def test_schema_names_hashes_and_no_constraints_for_unstructured_tasks(self):
        for mode,name in [('natural_structured','pcar_reply_v2'),('context_structured','pcar_reply_v1')]:
            fmt=response_format(mode);self.assertTrue(fmt['json_schema']['strict'])
            self.assertEqual(fmt['json_schema']['name'],name)
            schema=fmt['json_schema']['schema'];self.assertFalse(schema['additionalProperties'])
            self.assertEqual(set(schema['required']),set(schema['properties']))
            self.assertEqual(descriptor(mode),descriptor(mode))
        self.assertIsNone(response_format('legacy_full'));self.assertIsNone(response_format(None))
        self.assertNotEqual(descriptor('natural_structured')['schema_sha256'],descriptor('context_structured')['schema_sha256'])

    def test_precise_body_free_diagnostics(self):
        secret='never-save-this-body'
        cases=[('```json\n'+natural()+'\n```','outer_code_fence'),
               ('{"schema_version":2,','invalid_json'),
               ('{"schema_version":2,"schema_version":2}','duplicate_json_key'),
               ('[]','envelope_type'),('{"schema_version":9}','schema_version'),
               (json.dumps({'schema_version':2,'secret':secret}),'envelope_fields'),
               (json.dumps({'schema_version':2,'behavior_intent':'respond','messages':[]}),'message_count')]
        for raw,expected in cases:
            with self.subTest(reason=expected):
                diagnostic=validation_failure(raw,'natural_structured')
                self.assertEqual(diagnostic['reason'],expected)
                self.assertNotIn(secret,json.dumps(diagnostic));self.assertLess(len(json.dumps(diagnostic)),512)


class ProtocolIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root=RuntimePaths.from_environment().home/'tmp';root.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(prefix='reply-wire-',dir=root);self.addCleanup(self.tmp.cleanup)
        self.paths=RuntimePaths(Path(self.tmp.name));self.store=Store(self.paths);self.store.initialize()
        self.config=ConfigStore(self.paths);self.config.initialize();self.requests=[]
        self.raw=natural();self.finish='stop';self.http_status=200;self.require_manifest=True
        async def complete(request):
            body=await request.json();self.requests.append(body)
            if self.require_manifest:
                with self.store.connection() as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM attempts WHERE status='running'").fetchone()[0],1)
                    self.assertTrue(db.execute('SELECT 1 FROM context_manifests WHERE attempt_id IS NOT NULL').fetchone())
            return web.json_response({'choices':[{'message':{'content':self.raw},'finish_reason':self.finish}],
                'usage':{'prompt_tokens':5,'completion_tokens':7,'total_tokens':12}},status=self.http_status)
        app=web.Application();app.router.add_post('/v1/chat/completions',complete)
        self.provider=TestServer(app);await self.provider.start_server();self.addAsyncCleanup(self.provider.close)
        self.config.update({'model':{'base_url':str(self.provider.make_url('/v1')),'name':'mock'}})
        self.service=RuntimeService(self.store);self.pid=self.service.create_persona(generic_persona())['id'];self.service.use_persona(self.pid)
        self.engine=ChatEngine(self.store,self.config);self.ingress=IngressService(self.engine)
        # This suite verifies the frozen pre-bubble wire contract.
        from persona_runtime.reply_batch import expression_snapshot
        with self.store.connection(write=True) as conn:
            for r in conn.execute('SELECT id,reply_mode,data_json FROM interaction_policies').fetchall():
                data=json.loads(r['data_json']);data['expression']=expression_snapshot(r['reply_mode'],r['id'],version=1)
                conn.execute('UPDATE interaction_policies SET data_json=? WHERE id=?',(json.dumps(data),r['id']))
        self.addAsyncCleanup(self.cleanup_engine)

    async def cleanup_engine(self):
        await self.engine.wait();self.engine.cognitive.characters.close()

    async def submit(self,mode='natural_structured'):
        p=self.ingress.current_policy(self.pid,'desktop')
        self.ingress.update_policy(self.pid,'desktop','legacy_per_message',p['id'],identifier(),
                                  reply_mode=mode,character_retrieval_mode='lexical')
        receipt=self.ingress.accept({'text':'晚上好，能看到我的消息吗','request_id':identifier(),'channel':'desktop','persona_id':self.pid})
        await self.engine.wait();return self.engine.get(receipt['turn_id'])

    def manifest(self,tid):
        with self.store.connection() as db:
            return json.loads(db.execute("SELECT manifest_json FROM context_manifests WHERE turn_id=? AND task_type='dialogue'",(tid,)).fetchone()[0])

    async def test_wire_schema_manifest_and_two_messages_one_call(self):
        turn=await self.submit();self.assertEqual(turn['status'],'completed')
        self.assertEqual(len(self.requests),1);self.assertEqual(self.requests[0]['response_format'],response_format('natural_structured'))
        self.assertEqual(turn['reply_batch']['segment_count'],2)
        self.assertEqual(self.manifest(turn['id'])['reply_output_contract'],descriptor('natural_structured'))
        system=self.requests[0]['messages'][0]['content']
        self.assertTrue(system.endswith('不加任何对象外文字。'))
        self.assertEqual(turn['context']['reply_provider']['finish_reason'],'stop')

    async def test_v1_strict_schema_and_legacy_without_response_format(self):
        self.raw='{"schema_version":1,"segments":[{"text":"收到","format":"plain"}]}'
        t=await self.submit('context_structured');self.assertEqual(t['status'],'completed')
        self.assertEqual(self.requests[0]['response_format'],response_format('context_structured'))
        self.raw='普通回复';t=await self.submit('legacy_full');self.assertEqual(t['status'],'completed')
        self.assertNotIn('response_format',self.requests[-1])

    async def test_bad_output_has_diagnostic_usage_and_zero_reply_or_retry(self):
        self.raw='{"schema_version":2,"never-save-this-body":1}'
        t=await self.submit();self.assertEqual(t['error_code'],'reply_schema_invalid')
        self.assertEqual(t['context']['reply_validation']['reason'],'envelope_fields')
        self.assertEqual(t['usage']['total_tokens'],12);self.assertEqual(len(self.requests),1)
        with self.store.connection() as db:
            self.assertFalse(db.execute('SELECT 1 FROM reply_batches WHERE turn_id=?',(t['id'],)).fetchone())
            self.assertFalse(db.execute('SELECT 1 FROM reply_delivery_parts WHERE turn_id=?',(t['id'],)).fetchone())
            diagnostic=db.execute('SELECT details_json FROM context_uses WHERE turn_id=?',(t['id'],)).fetchone()[0]
            self.assertNotIn('never-save-this-body',diagnostic)

    async def test_length_finish_does_not_lose_usage_or_accept_partial_answer(self):
        self.finish='length';t=await self.submit()
        self.assertEqual(t['error_code'],'reply_output_truncated')
        self.assertEqual(t['context']['reply_validation']['reason'],'provider_length_limit')
        self.assertEqual(t['usage']['total_tokens'],12);self.assertEqual(len(self.requests),1)

    async def test_empty_filtered_response_keeps_usage_and_diagnostic(self):
        self.finish='content_filter';self.raw=None;t=await self.submit()
        self.assertEqual(t['error_code'],'provider_content_filtered')
        self.assertEqual(t['context']['reply_provider']['finish_reason'],'content_filter')
        self.assertEqual(t['usage']['total_tokens'],12)

    async def test_filtered_nonempty_partial_output_is_not_published(self):
        self.finish='content_filter';t=await self.submit()
        self.assertEqual(t['error_code'],'provider_content_filtered')
        self.assertEqual(t['context']['reply_validation']['reason'],'provider_content_filtered')
        self.assertIsNone(t['reply_event_id']);self.assertEqual(len(self.requests),1)

    async def test_unsupported_schema_fails_once_without_automatic_second_call(self):
        self.http_status=400;t=await self.submit()
        self.assertEqual(t['error_code'],'provider_http_400');self.assertEqual(len(self.requests),1)

    async def test_manifest_failure_zero_cloud_call(self):
        with patch.object(self.engine.cognitive,'persist',side_effect=OSError('disk unavailable')):
            t=await self.submit()
        self.assertEqual(t['status'],'failed');self.assertEqual(self.requests,[])
        with self.store.connection() as db:self.assertFalse(db.execute('SELECT 1 FROM attempts').fetchone())

    async def test_connection_diagnostics_persist_without_reply(self):
        from persona_runtime.chat import ModelError
        diagnostic={'phase':'connecting','request_headers_sent':False,
                    'response_headers_received':False,'timeout_seconds':60,'elapsed_ms':60001}
        with patch.object(self.engine.client,'complete',side_effect=ModelError('provider_connection_timeout',diagnostics=diagnostic)) as call:
            t=await self.submit()
        self.assertEqual(call.call_count,1)
        self.assertEqual(t['error_code'],'provider_connection_timeout')
        self.assertEqual(t['context']['reply_provider'],diagnostic)
        self.assertIsNone(t['reply_event_id']);self.assertIsNone(t['usage'])

    async def test_generic_probe_is_idempotent_and_has_no_persona_or_events(self):
        rid=identifier();a=await self.engine.submit('test','Reply with OK.',rid,test=True,test_reply_mode='natural_structured')
        await self.engine.wait();b=await self.engine.submit('test','Reply with OK.',rid,test=True,test_reply_mode='natural_structured')
        self.assertEqual(a['id'],b['id']);self.assertEqual(b['status'],'completed')
        self.assertEqual(len(self.requests),1);self.assertIsNone(b['persona_id'])
        self.assertEqual(len(self.requests[0]['messages']),2)
        self.assertEqual(self.requests[0]['messages'][-1]['content'],'晚上好，能看到我的消息吗')
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM character_selection_attempts').fetchone()[0],0)
        with self.assertRaisesRegex(RuntimeErrorBase,'submission_conflict'):
            await self.engine.submit('test','Reply with OK.',rid,test=True,test_reply_mode='context_structured')

    async def test_admin_probe_http_is_idempotent_and_bridge_is_denied(self):
        from persona_runtime.server import create_app
        app=create_app(self.paths)
        async with TestServer(app) as server,ClientSession() as client:
            endpoint=server.make_url('/v1/model-test')
            payload={'reply_mode':'natural_structured','request_id':identifier()}
            async with client.post(endpoint,json=payload,headers={'Authorization':'Bearer '+self.config.token('bridge')}) as response:
                self.assertEqual(response.status,403)
            for _ in range(2):
                async with client.post(endpoint,json=payload,headers={'Authorization':'Bearer '+self.config.token()}) as response:
                    self.assertEqual(response.status,200);body=await response.json()
            await app['engine'].wait()
            self.assertEqual(app['engine'].get(body['id'])['status'],'completed')
            self.assertEqual(len(self.requests),1)
            with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],0)


if __name__=='__main__':unittest.main()
