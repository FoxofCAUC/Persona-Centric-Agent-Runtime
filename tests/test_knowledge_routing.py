"""Opt-in semantic paths, native decisions and durable recovery (artificial gates)."""
import asyncio
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from persona_runtime.chat import ModelCompletion,ModelError
from persona_runtime.config import atomic_json
from persona_runtime.knowledge_routing_contract import parse,usage
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.security import protect
from tests import test_knowledge_processing as processing
from tests import test_knowledge_web as web
from tests import test_m4 as fixtures

def configure(test):
    # Local synthetic proof, never presented as a real ten-round result.
    encrypted=protect('artificial-jeV-test-only')
    test.router_file=test.paths.home/'private/knowledge-router/config.json'
    atomic_json(test.router_file,{'base_url':'https://example.com/v1','name':'jev-1.13','encrypted_key':encrypted,
        'gate':{'base_url':'https://example.com/v1','name':'jev-1.13','completed_rounds':10,'successes':10,
                'credential_sha256':hashlib.sha256(encrypted.encode()).hexdigest(),'artificial_fixture':True}})
    test.route_calls=[];test.choice='local';test.reuse=False;test.low=False
    async def transport(model,body):
        test.route_calls.append(copy.deepcopy((model,body)))
        options=list(body['questions']['route']['criteria'])
        values={k:(.4 if k==test.choice else .6/(len(options)-1)) if test.low else float(k==test.choice) for k in options}
        out={'model':'artificial/jev-1.13','answers':{'route':{'type':'choice','choice':test.choice,'probabilities':values,'confidence':.1 if test.low else 1},
            'reuse_evidence':{'type':'noul','noul':float(test.reuse)}},'usage':{'input_tokens':0,'prompt_tokens':12,'completion_tokens':0,'output_tokens':0}}
        return ModelCompletion(json.dumps(out),{'input_tokens':12,'output_tokens':0,'total_tokens':12},{'request_headers_sent':True,'http_status':200})
    test.org.routing.transport=transport
    policy=test.org.routing.policy(test.persona['id'])
    test.org.routing.update_policy(test.persona['id'],policy['data']|{'enabled':True,'variant':'local_web'},policy['revision'],'enable-jev')
    policy=test.org.processing.policy(test.persona['id'])
    test.org.processing.update_policy(test.persona['id'],policy['data']|{'prompt_profile':'question_focused_v3'},policy['revision'],'legacy-routing-fixture')

async def cleanup(test):
    test.chat.knowledge.close();test.org.draining=True
    if getattr(test,'processor',None) and test.processor.gate:test.processor.gate.set()
    with test.store.connection() as db:
        pending=[row[0] for row in db.execute("SELECT id FROM turns WHERE status IN ('queued','running')")]
    for ident in pending:await test.chat.cancel(ident)
    await test.chat.wait()
    # Let publication/heartbeat cleanup finish before cancelling an idle worker.
    # Cancelling in its receipt-finally path made the old fixture hang silently.
    await asyncio.wait_for(test.org.drain(),10)
    if getattr(test,'worker',None):
        test.worker.cancel();await asyncio.wait_for(asyncio.gather(test.worker,return_exceptions=True),10)

class RoutingTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.M4Tests.setUp
    asyncTearDown=cleanup
    ask=processing.ProcessingTests.ask

    def enable(self):
        processing.ProcessingTests.enable(self);configure(self)

    async def test_semantic_path_bypasses_regex_and_main_receives_translation(self):
        self.enable()
        with patch('persona_runtime.knowledge_service.should_retrieve',side_effect=AssertionError('legacy semantic rule used')):
            result=await self.ask('这个现象呢？')
        self.assertEqual(result['status'],'completed',result['error_code'])
        self.assertIn('整理结论',self.capture.calls[0][0]['content'])
        route=result['context']['knowledge']['routing']
        self.assertEqual(route['choice'],'local');self.assertEqual(len(self.route_calls),1)
        model,body=self.route_calls[0]
        self.assertEqual(model['name'],'jev-1.13')
        self.assertEqual(list(body['questions']['route']['criteria']),['none','local','web'])
        from persona_runtime.archive import fingerprint
        with self.store.connection() as db:
            manifests=[json.loads(r[0]) for r in db.execute('SELECT manifest_json FROM context_manifests WHERE organization_task_id=?',(route['task_id'],))]
        self.assertEqual(manifests[-1]['native_request_hash'],fingerprint(body))

    async def test_source_followup_reuses_but_greeting_has_no_old_knowledge(self):
        self.enable();first=await self.ask('月球是什么？')
        count=len(self.processor.calls)
        self.choice='none';self.reuse=True
        follow=await self.ask('这段信息能给个引用链接吗？',session_id=first['session_id'])
        self.assertEqual(follow['status'],'completed',follow['error_code'])
        self.assertEqual(len(self.processor.calls),count)
        self.assertTrue(follow['context']['knowledge']['routing']['reuse'])
        self.reuse=False
        greeting=await self.ask('晚上好。',session_id=first['session_id'])
        self.assertEqual(greeting['context']['knowledge']['evidence'],[])
        self.assertEqual(len(self.processor.calls),count)
        self.assertNotIn('本轮公共知识',self.capture.calls[-1][0]['content'])

    async def test_low_probability_blocks_instead_of_regular_expression_fallback(self):
        self.enable();self.low=True
        result=await self.ask('月球是什么？')
        self.assertEqual(result['status'],'failed');self.assertEqual(self.capture.calls,[])
        self.assertEqual(len(self.processor.calls),1);self.assertEqual(len(self.route_calls),2)
        self.assertEqual(result['error_code'],'knowledge_route_uncertain')
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM organization_receipts').fetchone()[0],3)

    async def test_uncertain_route_clarifies_once_and_reuses_the_goal(self):
        self.enable();self.low=True;original=self.org.routing.transport
        async def recover(model,body):
            result=await original(model,body);self.low=False;return result
        self.org.routing.transport=recover
        result=await self.ask('这个现象呢？')
        self.assertEqual(result['status'],'completed',result['error_code'])
        self.assertEqual(len(self.route_calls),2);self.assertEqual(len(self.processor.calls),4)
        self.assertIn('clarified_goal',self.route_calls[1][1]['state'])
        self.assertEqual(len(result['context']['knowledge']['routing']['route_task_ids']),2)

    async def test_legacy_low_probability_keeps_its_original_contract(self):
        self.enable();policy=self.org.processing.policy(self.persona['id']);data=dict(policy['data']);data.pop('prompt_profile')
        self.org.processing.update_policy(self.persona['id'],data,policy['revision'],'legacy-route-contract')
        self.low=True;result=await self.ask('月球是什么？')
        self.assertEqual(result['status'],'failed');self.assertEqual(len(self.route_calls),1)

    async def test_current_goal_cannot_be_answered_as_local_snapshot(self):
        self.enable()
        async def current(model,messages):
            return json.dumps({'format_version':1,'queries':['月球'],'freshness':'current'}),{'total_tokens':3}
        with patch.object(self.processor,'complete',side_effect=current),patch('persona_runtime.knowledge_processing.search_turn',side_effect=AssertionError('stale local path used')):
            result=await self.ask('需要查证的月球资料')
        self.assertEqual(result['error_code'],'knowledge_route_freshness_conflict')
        self.assertEqual(self.capture.calls,[])

    async def test_unavailable_path_does_not_silently_switch_to_local(self):
        self.enable();self.choice='web'
        result=await self.ask('最近有什么新资料？')
        self.assertEqual(result['status'],'failed');self.assertEqual(self.capture.calls,[])
        self.assertEqual(self.processor.calls,[])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT error_code FROM organization_tasks').fetchone()[0],'knowledge_route_path_unavailable')

    async def test_successful_native_receipt_recovers_without_second_call(self):
        self.enable();original=self.org.processing.published;interrupted=False
        def publish(db,task,payload,response,audit):
            nonlocal interrupted
            if payload['knowledge_processing']['stage']=='route' and not interrupted:
                interrupted=True;raise RuntimeErrorBase('knowledge_processing_request_inactive')
            return original(db,task,payload,response,audit)
        with patch.object(self.org.processing,'published',side_effect=publish):first=await self.ask('月球是什么？')
        self.assertEqual(first['status'],'failed');self.assertEqual(len(self.route_calls),1)
        await self.chat.retry(first['id']);await self.chat.wait()
        self.assertEqual(self.chat.get(first['id'])['status'],'completed')
        self.assertEqual(len(self.route_calls),1)

    async def test_unknown_native_request_cannot_be_replayed_by_retry(self):
        self.enable()
        async def unknown(model,body):
            self.route_calls.append((model,body))
            raise ModelError('jev_transport_unconfirmed',diagnostics={'request_headers_sent':True})
        self.org.routing.transport=unknown
        first=await self.ask('月球是什么？')
        await self.chat.retry(first['id']);await self.chat.wait()
        self.assertEqual(len(self.route_calls),1);self.assertEqual(self.capture.calls,[])

    async def test_current_gate_and_model_identity_are_required(self):
        self.enable()
        saved=json.loads(self.router_file.read_text(encoding='utf-8'));saved['name']='other-model';atomic_json(self.router_file,saved)
        result=await self.ask('月球是什么？')
        self.assertEqual(result['status'],'failed');self.assertEqual(self.route_calls,[])
        self.assertEqual(self.capture.calls,[])

    async def test_withdrawal_between_decision_and_reuse_cannot_revive_evidence(self):
        self.enable();first=await self.ask('月球是什么？');self.choice='none';self.reuse=True
        original=self.org.routing.transport
        async def withdraw(model,body):
            self.knowledge.invalidate(self.identity,1,'withdrawn','controlled',0,'withdraw-during-routing')
            return await original(model,body)
        self.org.routing.transport=withdraw
        next_turn=await self.ask('能给这段信息的来源吗？',session_id=first['session_id'])
        self.assertEqual(next_turn['status'],'failed')
        self.assertEqual(len(self.capture.calls),1)

    async def test_private_main_authorization_does_not_authorize_jev_disclosure(self):
        self.enable()
        atomic_json(self.paths.home/'runtime/private-copy-policy.json',{'persona_id':self.persona['id'],'approved_models':{'main':'other-provider'}})
        result=await self.ask('月球是什么？')
        self.assertEqual(result['status'],'failed');self.assertEqual(self.route_calls,[])

    async def test_native_http_429_has_finite_retry_and_saved_error_receipt(self):
        self.enable();original=self.org.routing.transport;failed=False
        async def limited(model,body):
            nonlocal failed
            if not failed:
                failed=True;self.route_calls.append((model,body))
                error=ModelError('jev_http_429',diagnostics={'request_headers_sent':True,'http_status':429})
                error.native_response='{"error":{"code":"rate_limit"}}';raise error
            return await original(model,body)
        self.org.routing.transport=limited
        result=await self.ask('月球是什么？')
        self.assertEqual(result['status'],'completed',result['error_code']);self.assertEqual(len(self.route_calls),2)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM knowledge_control WHERE key LIKE 'knowledge.routing.http_receipt.%'").fetchone()[0],1)

    async def test_existing_channel_grant_cannot_be_widened_by_router(self):
        self.enable();policy=self.org.managed.current_policy(self.persona['id'])
        self.org.managed.update_policy(self.persona['id'],policy['data']|{'channels':['desktop']},policy['id'],self.persona['id'],'desktop-only-route')
        result=await self.ask('月球是什么？',channel='qq',persona_id=self.persona['id'],origin={'bot':'456','sender':'123'})
        self.assertEqual(result['status'],'failed');self.assertEqual(self.route_calls,[])

    async def test_missing_gate_never_enables_network_decisions(self):
        policy=self.org.routing.policy(self.persona['id'])
        with self.assertRaises(RuntimeErrorBase):self.org.routing.update_policy(self.persona['id'],policy['data']|{'enabled':True},0,'without-proof')
        self.assertFalse(self.org.routing.policy(self.persona['id'])['data']['enabled'])

    async def test_policy_withdrawal_after_dispatch_blocks_main(self):
        self.enable();original=self.org.routing.transport
        async def stop(model,body):
            policy=self.org.routing.policy(self.persona['id'])
            self.org.routing.update_policy(self.persona['id'],policy['data']|{'enabled':False},policy['revision'],'stop-during-route')
            return await original(model,body)
        self.org.routing.transport=stop
        result=await self.ask('月球是什么？')
        self.assertEqual(result['status'],'failed');self.assertEqual(self.capture.calls,[])

class WebRoutingTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.M4Tests.setUp
    asyncTearDown=cleanup
    transport=web.WebTests.transport

    async def test_explicit_latest_uses_new_tool_positions_for_each_new_turn(self):
        web.WebTests.enable(self);configure(self);self.choice='web'
        first=await web.WebTests.ask(self,'公开灯塔现在几点开放？')
        second=await web.WebTests.ask(self,'公开灯塔现在几点开放？')
        self.assertEqual(first['status'],'completed',first['error_code'])
        self.assertEqual(second['status'],'completed',second['error_code'])
        self.assertEqual(len(self.tool_calls),4)

    async def test_web_only_uses_two_choices_and_never_local_search(self):
        web.WebTests.enable(self);configure(self)
        policy=self.org.routing.policy(self.persona['id'])
        self.org.routing.update_policy(self.persona['id'],policy['data']|{'variant':'web_only'},policy['revision'],'web-only')
        self.choice='web'
        with patch('persona_runtime.knowledge_processing.search_turn',side_effect=AssertionError('local search used')):
            result=await web.WebTests.ask(self,'这是什么概念？')
        self.assertEqual(result['status'],'completed',result['error_code'])
        self.assertEqual(list(self.route_calls[0][1]['questions']['route']['criteria']),['none','web'])
        self.assertFalse(self.route_calls[0][1]['state']['capabilities']['local'])
        self.assertIsNone(self.route_calls[0][1]['state']['capabilities']['package_sha256'])
        self.assertEqual(result['context']['knowledge']['routing']['choice'],'web')
        self.assertTrue(all(e['kind']=='web_document' for e in result['context']['knowledge']['evidence']))

class ContractTests(unittest.TestCase):
    def test_invalid_native_choices_or_probabilities_are_rejected(self):
        base={'answers':{'route':{'type':'choice','choice':'none','probabilities':{'none':1,'local':0,'web':0},'confidence':1},'reuse_evidence':{'type':'noul','noul':0}}}
        for mutator in (lambda d:d['answers']['route'].update(choice='invented'),lambda d:d['answers']['route']['probabilities'].update(none=float('nan')),lambda d:d['answers']['reuse_evidence'].update(noul=2)):
            value=copy.deepcopy(base);mutator(value)
            with self.assertRaises(RuntimeErrorBase):parse(json.dumps(value),['none','local','web'])

    def test_native_token_aliases_are_not_double_counted(self):
        self.assertEqual(usage({'usage':{'input_tokens':0,'prompt_tokens':12,'output_tokens':0,'completion_tokens':0}})['total_tokens'],12)
