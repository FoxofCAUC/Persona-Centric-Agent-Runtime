import json,unittest
from unittest.mock import patch
from persona_runtime.knowledge_processing_output import messages,parse
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.chat import ModelCompletion
from tests import test_knowledge_routing as routing
from tests import test_m4 as fixtures

class ModelViewTests(unittest.TestCase):
    def data(self,stage='translate'):
        return {'knowledge_processing':{'stage':stage,'policy':{'data':{'prompt_profile':'question_focused_v4'}},
            'input':{'question':'何时开放？','materials':[{'id':'long-authority-id','text':'每天09:00开放。','evidence':{'claim_kind':'source_claim','source_snapshot_at':None,'document_sha256':'private-execution-hash','article_title':'公告'}}]}}}
    def test_model_sees_short_ids_and_semantics_but_authority_remains_intact(self):
        payload=self.data();view=json.loads(messages(payload)[1]['content'])
        self.assertEqual(view['materials'][0]['id'],'M1');self.assertEqual(view['materials'][0]['source']['claim_kind'],'source_claim')
        self.assertNotIn('document_sha256',view['materials'][0]['source']);self.assertEqual(list(view)[-1],'question')
        self.assertEqual(payload['knowledge_processing']['input']['materials'][0]['id'],'long-authority-id')
    def test_alias_restoration_preserves_strict_quotes_numbers_and_claims(self):
        unit={'text':'每天09:00开放。','kind':'source_claim','refs':[{'material_id':'M1','quote':'每天09:00开放。'}]}
        def check(u):return parse(self.data(),json.dumps({'format_version':1,'units':[u]}))
        self.assertEqual(check(unit)['units'][0]['refs'][0]['material_id'],'long-authority-id')
        for bad in (dict(unit,text='每天10:00开放。'),dict(unit,kind='fact'),dict(unit,refs=[{'material_id':'M2','quote':'每天09:00开放。'}]),dict(unit,refs=[{'material_id':'M1','quote':'每天9点开放。'}])):
            with self.assertRaises(RuntimeErrorBase):check(bad)
    def test_empty_goal_only_new_profile_and_still_no_new_fields(self):
        data=self.data('goal');raw='{"format_version":1,"queries":[],"freshness":"snapshot"}'
        self.assertEqual(parse(data,raw)['queries'],[])
        data['knowledge_processing']['policy']['data']['prompt_profile']='question_focused_v3'
        with self.assertRaises(RuntimeErrorBase):parse(data,raw)

    def test_v6_source_ids_fill_exact_quote_without_model_copying(self):
        data=self.data();data['knowledge_processing']['policy']['data']['prompt_profile']='question_focused_v6'
        def result(ident='M1',text='每天09:00开放。'):
            return parse(data,json.dumps({'format_version':1,'units':[{'text':text,'kind':'source_claim','refs':[{'material_id':ident}]}]}))
        out=result();self.assertEqual(out['units'][0]['refs'][0],{'material_id':'long-authority-id','quote':'每天09:00开放。'})
        self.assertEqual(out['_normalizations'][0]['kind'],'registered_source_window')
        with self.assertRaises(RuntimeErrorBase):result('made-up')
        with self.assertRaises(RuntimeErrorBase):result(text='每天10:00开放。')
        data['knowledge_processing']['input']['materials'][0]['text']='长'*501
        with self.assertRaises(RuntimeErrorBase):result()

class TargetedRouteTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.M4Tests.setUp
    asyncTearDown=routing.cleanup
    ask=routing.RoutingTests.ask
    def enable(self):
        routing.RoutingTests.enable(self)
        policy=self.org.processing.policy(self.persona['id'])
        self.org.processing.update_policy(self.persona['id'],policy['data']|{'prompt_profile':'question_focused_v4'},policy['revision'],'focused-v4')
    def ambiguous_reuse(self,decide=None):
        original=self.org.routing.transport
        async def transport(model,body):
            if decide:decide(body)
            value=await original(model,body);out=json.loads(value[0]);out['answers']['reuse_evidence']['noul']=.5 if self.choice!='none' else 0
            return ModelCompletion(json.dumps(out),value[1],{'request_headers_sent':True})
        self.org.routing.transport=transport
    async def test_fresh_search_ignores_irrelevant_reuse_uncertainty(self):
        self.enable();self.ambiguous_reuse()
        result=await self.ask('那它是什么？')
        self.assertEqual(result['status'],'completed',result['error_code']);self.assertEqual(len(self.route_calls),1)
        self.assertFalse(result['context']['knowledge']['routing']['reuse']);self.assertEqual(len(self.processor.calls),4)
        self.assertEqual(self.knowledge.show_evidence(self.persona['id'],result['id'])['evidence'][0]['quote'],self.body)
    async def test_goal_route_disagreement_redecides_once_without_source_calls(self):
        self.enable()
        self.ambiguous_reuse(lambda body:setattr(self,'choice','none' if 'clarified_goal' in body['state'] else 'local'))
        async def empty(model,messages):return '{"format_version":1,"queries":[],"freshness":"snapshot"}',{'total_tokens':1}
        with patch.object(self.processor,'complete',side_effect=empty),patch('persona_runtime.knowledge_processing.search_turn',side_effect=AssertionError('chitchat searched')):
            result=await self.ask('小时候我觉得植物是在做饭。')
        self.assertEqual(result['status'],'completed',result['error_code']);self.assertEqual(len(self.route_calls),2)
        self.assertEqual(result['context']['knowledge']['routing']['choice'],'none');self.assertFalse(result['context']['knowledge']['evidence'])
    async def test_unresolved_goal_conflict_does_not_invent_query_or_loop(self):
        self.enable();self.ambiguous_reuse()
        async def empty(model,messages):return '{"format_version":1,"queries":[],"freshness":"snapshot"}',{'total_tokens':1}
        with patch.object(self.processor,'complete',side_effect=empty):result=await self.ask('晚安。')
        self.assertEqual(result['error_code'],'knowledge_route_goal_conflict');self.assertEqual(len(self.route_calls),2);self.assertEqual(self.capture.calls,[])

    async def test_v5_conversation_confirmation_ignores_irrelevant_reuse_probability(self):
        self.enable()
        policy=self.org.processing.policy(self.persona['id'])
        self.org.processing.update_policy(self.persona['id'],policy['data']|{'prompt_profile':'question_focused_v5'},policy['revision'],'v5-conversation')
        original=self.org.routing.transport
        async def route(model,body):
            self.choice='none';value=await original(model,body);out=json.loads(value[0]);out['answers']['reuse_evidence']['noul']=.5
            return ModelCompletion(json.dumps(out),value[1],{'request_headers_sent':True})
        self.org.routing.transport=route
        async def empty(model,messages):return '{"format_version":1,"queries":[],"freshness":"snapshot"}',{'total_tokens':1}
        with patch.object(self.processor,'complete',side_effect=empty):result=await self.ask('晚安，下次聊。')
        self.assertEqual(result['status'],'completed',result['error_code'])
        self.assertEqual(len(self.route_calls),2);self.assertFalse(result['context']['knowledge']['routing']['reuse'])
        self.assertFalse(result['context']['knowledge']['evidence'])

    async def test_routing_update_preserves_existing_processing_profile(self):
        self.enable();before=self.org.processing.policy(self.persona['id'])['data']['prompt_profile']
        policy=self.org.routing.policy(self.persona['id'])
        self.org.routing.update_policy(self.persona['id'],policy['data'],policy['revision'],'route-settings-edit')
        self.assertEqual(self.org.processing.policy(self.persona['id'])['data']['prompt_profile'],before)
