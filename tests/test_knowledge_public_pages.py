import asyncio,copy,hashlib,json,unittest
from unittest.mock import patch
from persona_runtime.chat import ModelCompletion
from persona_runtime.knowledge_processing_output import parse
from persona_runtime.knowledge_public_pages import PLAN,REVIEW,title_url,check_fetch
from persona_runtime.models import RuntimeErrorBase
from tests import test_knowledge_wikimedia as wiki
from tests import test_knowledge_web as web
from tests import test_m4 as fixtures

class PublicModel(wiki.Models):
    def __init__(self):self.private=False;self.empty=False;self.gate=None;self.review_started=asyncio.Event()
    async def complete(self,model,messages):
        value=json.loads(messages[-1]['content'])
        if messages[0]['content'].endswith(PLAN):out={'candidates':[] if self.empty else [{'title':'水循环','kind':'science'}]}
        elif messages[0]['content'].endswith(REVIEW):
            self.review_started.set()
            if self.gate:await self.gate.wait()
            out={'decisions':[{'id':r['id'],'public':not self.private,'relevant':True,'private_detail':self.private,'note':'人工复核夹具。'} for r in value['candidates']]}
        else:return await super().complete(model,messages)
        return json.dumps({'format_version':1,**out},ensure_ascii=False),{'total_tokens':3}

class PublicPageTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.M4Tests.setUp
    async def asyncTearDown(self):
        if getattr(self,'model',None) and getattr(self.model,'gate',None):self.model.gate.set()
        await web.WebTests.asyncTearDown(self)
    def enable(self):
        wiki.WikiTests.enable(self)
        policy=self.org.web.policy(self.persona['id'])
        self.org.web.update_policy(self.persona['id'],policy['data']|{'topics':{},'discover_public_pages':True},policy['revision'],'public-discovery')
        self.model=self.org.client=PublicModel()
    ask=wiki.WikiTests.ask
    async def test_unregistered_topic_has_independent_review_and_no_original_query_on_wire(self):
        self.enable();result=await self.ask()
        self.assertEqual(result['status'],'completed',result['error_code']);self.assertEqual(self.cloud,[])
        self.assertEqual(len(self.source),1);self.assertEqual(self.source[0][0],'wiki_fetch')
        self.assertEqual(self.source[0][1],{'url':'https://zh.wikipedia.org/wiki/%E6%B0%B4%E5%BE%AA%E7%8E%AF'})
        self.assertTrue(result['context']['knowledge']['evidence'])
        with self.store.connection() as db:
            rows=db.execute("SELECT json_extract(payload_json,'$.knowledge_processing.stage'),automatic FROM organization_tasks").fetchall()
            self.assertTrue(self.org.routing.capabilities(db,self.persona['id'])['web'])
        self.assertEqual([r[0] for r in rows[:4]],['goal','public_page_plan','public_page_review','wiki_fetch'])
        self.assertTrue(all(r[1]==1 for r in rows))
    async def test_private_rejection_and_no_candidate_never_reach_source(self):
        self.enable();self.model.private=True;result=await self.ask()
        self.assertEqual(result['status'],'completed');self.assertEqual(self.source,[]);self.assertFalse(result['context']['knowledge']['evidence'])
        self.model.private=False;self.model.empty=True
        turn=await self.chat.submit('desktop','另一个私人问题是什么？','empty-candidate');await self.chat.wait()
        self.assertEqual(self.source,[])
    async def test_turn_cancellation_during_review_does_not_fetch(self):
        self.enable();self.model.gate=asyncio.Event()
        turn=await self.chat.submit('desktop','水循环是什么？','cancel-public-review')
        await asyncio.wait_for(self.model.review_started.wait(),10)
        await self.chat.cancel(turn['id']);self.model.gate.set();await self.chat.wait()
        self.assertEqual(self.source,[]);self.assertEqual(self.main.calls,[])
    async def test_discovery_withdrawal_while_reviewing_blocks_external_request(self):
        self.enable();self.model.gate=asyncio.Event()
        turn=await self.chat.submit('desktop','水循环是什么？','withdraw-discovery');await asyncio.wait_for(self.model.review_started.wait(),10)
        policy=self.org.web.policy(self.persona['id'])
        self.org.web.update_policy(self.persona['id'],policy['data']|{'enabled':False,'discover_public_pages':False},policy['revision'],'withdraw-public-discovery')
        self.model.gate.set();await self.chat.wait()
        self.assertEqual(self.source,[]);self.assertEqual(self.main.calls,[])
    async def test_page_missing_is_known_no_match_not_fabricated_document(self):
        self.enable()
        async def absent(stage,args,timeout):
            self.source.append((stage,args,timeout));value=json.loads(wiki.envelope());row=value['responses'][0]
            row['body']=json.dumps({'error':{'code':'missingtitle'}});row['sha256']=hashlib.sha256(row['body'].encode()).hexdigest();value['responses']=[row]
            return ModelCompletion(json.dumps(value),{'total_tokens':0,'source_network_requests':1},{'request_headers_sent':True,'http_status':200})
        self.org.web.wikimedia_transport=absent;result=await self.ask()
        self.assertEqual(result['status'],'completed',result['error_code']);self.assertFalse(result['context']['knowledge']['evidence'])
        self.assertIn('没有取得可用的参考资料',self.main.calls[0][0]['content'])
    async def test_disabled_source_blocks_discovery_before_model_proposal(self):
        self.enable();sources=self.chat.knowledge.sources();value=copy.deepcopy(sources['data']);value['wikipedia_zh']['enabled']=False
        self.chat.knowledge.update_sources(value,sources['revision'],'stop-wiki')
        result=await self.ask()
        self.assertEqual(result['error_code'],'web_discovery_not_authorized');self.assertEqual(self.source,[]);self.assertEqual(self.main.calls,[])

    async def test_proof_binds_url_owner_and_reviewed_result(self):
        self.enable();result=await self.ask();self.assertEqual(result['status'],'completed',result['error_code'])
        policy=self.org.web.policy(self.persona['id'])
        with self.store.connection() as db:
            task=db.execute("SELECT payload_json FROM organization_tasks WHERE json_extract(payload_json,'$.knowledge_processing.stage')='wiki_fetch'").fetchone()
            inputs=json.loads(task[0])['knowledge_processing']['input'];turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(result['id'],)).fetchone())
            check_fetch(db,turn,policy,inputs)
            with self.assertRaises(RuntimeErrorBase):check_fetch(db,turn,policy,dict(inputs,url='https://zh.wikipedia.org/wiki/Other'))
            with self.assertRaises(RuntimeErrorBase):check_fetch(db,dict(turn,persona_id='other-role'),policy,inputs)
            proof=inputs['public_discovery'];bad=dict(inputs,public_discovery=proof|{'review_result_hash':'changed'})
            with self.assertRaises(RuntimeErrorBase):check_fetch(db,turn,policy,bad)

class PublicOutputTests(unittest.TestCase):
    def payload(self,stage,inputs=None):return {'knowledge_processing':{'stage':stage,'input':inputs or {}}}
    def test_sensitive_or_nonarticle_targets_rejected(self):
        for title in ('User:Private','用户:私人','abc@example.com','我的电话13800000000','https://example.com/','private\naddress','abc?secret=123'):
            with self.assertRaises(RuntimeErrorBase):title_url(title)
        self.assertTrue(title_url('量子纠缠').startswith('https://zh.wikipedia.org/wiki/'))
    def test_reviewer_must_cover_all_candidates_with_real_booleans(self):
        p=self.payload('public_page_review',{'candidates':[{'id':'p1'}]})
        for decisions in ([],[{'id':'p1','public':'true','relevant':True,'private_detail':False,'note':'x'}],
                          [{'id':'p2','public':True,'relevant':True,'private_detail':False,'note':'x'}]):
            with self.assertRaises(RuntimeErrorBase):parse(p,json.dumps({'format_version':1,'decisions':decisions}))

    def test_new_route_instruction_is_versioned_and_legacy_request_unchanged(self):
        from persona_runtime.knowledge_routing_contract import request
        values={'options':['none','web'],'state':{'current':'查询私人住址','recent_context':[],'reusable_evidence':{'available':False},'clarified_goal':{'queries':[]}}}
        old=request({'name':'mock'},values);new=request({'name':'mock'},dict(values,routing_prompt_revision=2))
        self.assertNotIn('没有可外发',old['questions']['route']['instructions'])
        self.assertIn('没有可外发',new['questions']['route']['instructions'])
        self.assertIn('私人',new['questions']['route']['criteria']['none'])
