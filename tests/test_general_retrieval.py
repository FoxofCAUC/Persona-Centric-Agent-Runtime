"""General retrieval helps conversation; source imperfection is not a veto."""
import asyncio,copy,json,unittest
from unittest.mock import patch
from persona_runtime.chat import ModelCompletion,ModelError
from persona_runtime.knowledge_general import PROFILE,public_url,rank
from tests import test_m4 as fixtures

class Planner:
    def __init__(self):self.calls=[];self.search=True;self.reuse=False;self.broken=False;self.queries=['游戏攻略 社区经验']
    async def complete(self,model,messages):
        self.calls.append(copy.deepcopy(messages))
        from persona_runtime.knowledge_translation import ORGANIZE
        if messages[0]['content']==ORGANIZE:
            return json.dumps({'notes':'玩家试了不同装备，推荐轻松路线。尚未在简答中说出的细节：先练走位，再换装备。'},ensure_ascii=False),{'total_tokens':1}
        if self.broken:return '先自然聊就好。',{'total_tokens':1}
        return json.dumps({'search':self.search,'queries':self.queries,'read_urls':[],
            'reuse_previous':self.reuse,'official_domains':['official.example'],'community_domains':['community.example']}),{'total_tokens':1}
class Main:
    def __init__(self):self.calls=[]
    async def complete(self,model,messages):self.calls.append(copy.deepcopy(messages));return '我会先试社区里这套打法，操作压力小；等你熟悉了再换更激进的配装。',{'total_tokens':1}

class GeneralTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.M4Tests.setUp
    async def asyncTearDown(self):
        self.chat.knowledge.close();self.org.draining=True;await self.chat.wait();await self.org.drain()
        if getattr(self,'worker',None):self.worker.cancel();await asyncio.gather(self.worker,return_exceptions=True)
    def enable(self):
        pid=self.persona['id'];reader=self.chat.knowledge.current_policy(pid);processing=self.org.processing.policy(pid);web=self.org.web.policy(pid)
        self.org.processing.enable_for_persona(pid,True,reader['revision'],processing['revision'],web['revision'],'general-enable')
        self.planner=self.org.client=Planner();self.main=self.chat.client=Main();self.tools=[]
        from persona_runtime.config import atomic_json
        atomic_json(self.paths.home/'private/knowledge-router/config.json',{'base_url':'https://decision.fixture.invalid/v1','name':'jev-test','encrypted_key':'artificial-protected-jev'})
        self.need_web=True;self.decisions=[];self.org.routing.transport=self.decision_transport
        self.org.processing.general.transport=self.transport;self.worker=asyncio.create_task(self.org.worker())
    async def decision_transport(self,model,body):
        self.decisions.append((copy.deepcopy(model),copy.deepcopy(body)))
        return ModelCompletion(json.dumps({'answers':{'route':{'choice':'web' if self.need_web else 'none','confidence':0.01}}}),{'total_tokens':1},{'network_route':'local_fixture'})
    async def transport(self,stage,args,model):
        self.tools.append((stage,copy.deepcopy(args)))
        data={'results':[{'title':'玩家分享：轻松打法','url':'https://community.example/guide','summary':'社区经验可以有不同看法','content':'点赞 1200，玩家试了不同装备，推荐一套操作轻松的路线。'+('普通玩家的经验分享。'*35),'date':'不知道'}]} if stage=='search' else {'title':'帖子','content':'一次玩家实测，说法并不完整。'}
        return ModelCompletion(json.dumps(data,ensure_ascii=False),{'tool':stage,'billable':False,'amount_usd':'0'},{'network_route':'local_fixture'})
    async def ask(self,text='帮我找找游戏配装，社区玩家怎么打？'):
        from persona_runtime.models import identifier
        turn=await self.chat.submit('desktop',text,identifier());await asyncio.wait_for(self.chat.wait(),30);return self.chat.get(turn['id'])
    async def test_community_unknown_date_and_informal_numbers_do_not_block_reply(self):
        self.enable();result=await self.ask();self.assertEqual(result['status'],'completed',result['error_code'])
        self.assertEqual(result['context']['knowledge']['mode'],PROFILE);self.assertTrue(result['context']['knowledge']['sources'])
        self.assertIn('玩家试了不同装备',self.main.calls[-1][0]['content'])
        self.assertNotIn('assertable',self.main.calls[-1][0]['content']);self.assertEqual(len(self.planner.calls),2)
    async def test_ordinary_chat_has_no_search_or_audit_paragraph(self):
        self.enable();self.need_web=False;result=await self.ask('今天就想随便聊聊。')
        self.assertEqual(result['status'],'completed',result['error_code']);self.assertFalse(self.tools);self.assertFalse(self.planner.calls);self.assertEqual(len(self.decisions),1)
        self.assertNotIn('这轮检索',self.main.calls[-1][0]['content']);self.assertNotIn('暂时不能认定',self.main.calls[-1][0]['content'])
    async def test_fetch_failure_keeps_search_summary_and_the_conversation(self):
        self.enable()
        async def unavailable(stage,args,model):
            self.tools.append((stage,args))
            if stage=='fetch':raise ModelError('web_http_403',usage={'tool':stage,'billable':False,'amount_usd':'0'},diagnostics={'http_status':403})
            return ModelCompletion(json.dumps({'results':[{'title':'社区帖子','url':'https://any-public.example/post','summary':'玩家说先练走位再堆输出','content':''}]}),{'tool':stage,'billable':False,'amount_usd':'0'}, {})
        self.org.processing.general.transport=unavailable;result=await self.ask()
        self.assertEqual(result['status'],'completed',result['error_code']);self.assertIn('玩家说先练走位',self.main.calls[-1][0]['content'])
        self.assertEqual(result['context']['knowledge']['sources'][0]['kind'],'search_summary')
    async def test_planner_format_failure_is_not_a_factual_reply_veto(self):
        self.enable();self.planner.broken=True;result=await self.ask()
        self.assertEqual(result['status'],'completed',result['error_code']);self.assertEqual(len(self.main.calls),1)
    async def test_previously_provided_material_can_be_reused_without_new_search(self):
        self.enable();await self.ask();before=len(self.tools);self.need_web=False;self.planner.reuse=True
        result=await self.ask('就用刚才那篇帖子，给我一个更省事的建议。')
        self.assertEqual(result['status'],'completed',result['error_code']);self.assertEqual(len(self.tools),before)
        self.assertTrue(result['context']['knowledge']['retrieval']['used_previous'])
        self.assertIn('尚未在简答中说出的细节',self.main.calls[-1][0]['content'])
        from persona_runtime.knowledge_translation import ORGANIZE
        self.assertEqual(sum(c[0]['content']==ORGANIZE for c in self.planner.calls),1)
    async def test_jev_is_the_only_web_switch_and_high_cannot_veto(self):
        self.enable();self.planner.search=False;result=await self.ask()
        self.assertEqual(result['status'],'completed',result['error_code'])
        self.assertEqual(len(self.decisions),1);self.assertTrue(self.tools)
        self.assertTrue(result['context']['knowledge']['retrieval']['web_decision']['need_web'])
        self.assertEqual(result['context']['knowledge']['retrieval']['web_decision']['decision_source'],'jev')

    async def test_jev_invalid_reply_falls_back_to_web_without_confidence_gate(self):
        self.enable()
        async def invalid(model,body):
            self.decisions.append((model,body));return ModelCompletion('{"answers":{"route":{"choice":"local"}}}',{'total_tokens':1},{})
        self.org.routing.transport=invalid;result=await self.ask()
        self.assertEqual(result['status'],'completed',result['error_code']);self.assertTrue(self.tools)
        decision=result['context']['knowledge']['retrieval']['web_decision']
        self.assertEqual(decision['decision_source'],'policy_fallback');self.assertEqual(decision['fallback_reason'],'jev_web_decision_invalid')
        self.assertEqual(len(self.decisions),1)

    async def test_jev_unknown_is_not_replayed_on_primary_retry(self):
        self.enable();complete=self.main.complete
        async def unknown(model,body):
            self.decisions.append((model,body));raise ModelError('jev_transport_unconfirmed',diagnostics={'request_headers_sent':True})
        async def fail(model,messages):raise ModelError('provider_http_400')
        self.org.routing.transport=unknown;self.main.complete=fail;failed=await self.ask()
        self.assertEqual(failed['status'],'failed');self.assertEqual(len(self.decisions),1)
        self.main.complete=complete;await self.chat.retry(failed['id']);await asyncio.wait_for(self.chat.wait(),30)
        result=self.chat.get(failed['id']);self.assertEqual(result['status'],'completed',result['error_code'])
        self.assertEqual(len(self.decisions),1)
        self.assertEqual(result['context']['knowledge']['retrieval']['web_decision']['decision_source'],'policy_fallback')

    async def test_unavailable_jev_credential_is_not_retried_or_marked_unknown(self):
        self.enable()
        async def denied(model,body):
            self.decisions.append((model,body));raise ModelError('jev_credential_unavailable',diagnostics={'request_headers_sent':False})
        self.org.routing.transport=denied;result=await self.ask()
        self.assertEqual(result['status'],'completed',result['error_code']);self.assertEqual(len(self.decisions),1)
        self.assertEqual(result['context']['knowledge']['retrieval']['web_decision']['decision_source'],'policy_fallback')
        with self.store.connection() as db:
            status=db.execute("SELECT status FROM organization_tasks WHERE json_extract(payload_json,'$.knowledge_processing.stage')='conversation_web_decision'").fetchone()[0]
        self.assertEqual(status,'failed')
    async def test_evidence_button_returns_the_sources_actually_provided(self):
        self.enable();result=await self.ask();evidence=self.chat.knowledge.show_evidence(self.persona['id'],result['id'])
        self.assertEqual(evidence['evidence'][0]['url'],'https://community.example/guide')
    async def test_a_requested_query_angle_is_not_erased_by_other_high_ranked_results(self):
        self.enable();self.planner.queries=['官网活动','米游社玩家经验']
        async def diverse(stage,args,model):
            self.tools.append((stage,args))
            if '米游社' in args.get('query',''):
                rows=[{'title':'米游社实战','url':'https://www.miyoushe.com/guide','rerank_score':.3,'content':'玩家给了自己的攻略。'*45}]
            else:rows=[{'title':'官方动态','url':'https://official.example/'+str(i),'rerank_score':3,'content':'官方网站活动内容。'*50} for i in range(8)]
            return ModelCompletion(json.dumps({'results':rows},ensure_ascii=False),{'tool':stage,'billable':False,'amount_usd':'0'}, {})
        self.org.processing.general.transport=diverse;result=await self.ask('官网和米游社都帮我看看。')
        self.assertEqual(result['status'],'completed',result['error_code'])
        self.assertIn('https://www.miyoushe.com/guide',{r['url'] for r in result['context']['knowledge']['sources']})

    async def test_native_tools_are_recorded_as_tools_without_retired_review_rules(self):
        self.enable();result=await self.ask()
        self.assertEqual(result['status'],'completed',result['error_code'])
        with self.store.connection() as db:
            rows=db.execute('SELECT m.manifest_json,t.payload_json FROM context_manifests m JOIN organization_tasks t ON t.id=m.organization_task_id').fetchall()
        tools=[]
        for row in rows:
            stage=json.loads(row['payload_json']).get('knowledge_processing',{}).get('stage')
            if stage not in ('conversation_search','conversation_fetch'):continue
            manifest=json.loads(row['manifest_json']);tools.append(stage)
            self.assertEqual(manifest['provider_request_contract']['kind'],stage.removeprefix('conversation_'))
            self.assertEqual(manifest['provider_request_contract']['configuration_source'],'main')
            self.assertNotIn('independent_translation_review',manifest['hard_constraints'])
        self.assertIn('conversation_search',tools)

    async def test_disabling_retrieval_keeps_chat_available(self):
        self.enable();pid=self.persona['id'];r=self.chat.knowledge.current_policy(pid);p=self.org.processing.policy(pid);w=self.org.web.policy(pid)
        self.org.processing.enable_for_persona(pid,False,r['revision'],p['revision'],w['revision'],'general-disable')
        result=await self.ask('陪我聊两句。');self.assertEqual(result['status'],'completed',result['error_code']);self.assertFalse(self.tools);self.assertFalse(self.decisions);self.assertFalse(self.planner.calls)

    async def test_retrieval_stays_high_and_translation_reuses_main_model_configuration(self):
        from persona_runtime.provider_compat import build_request
        self.enable();planner_models=[];reply_models=[]
        plan_complete=self.planner.complete;reply_complete=self.main.complete
        async def plan(model,messages):
            planner_models.append(copy.deepcopy(model));return await plan_complete(model,messages)
        async def reply(model,messages):
            reply_models.append(copy.deepcopy(model));return await reply_complete(model,messages)
        self.planner.complete=plan;self.main.complete=reply
        result=await self.ask();self.assertEqual(result['status'],'completed',result['error_code'])
        self.need_web=False;self.planner.reuse=False
        result=await self.ask('今天不查资料，陪我闲聊。');self.assertEqual(result['status'],'completed',result['error_code'])
        self.assertTrue(all(m.get('_reasoning_effort')=='high' for m in planner_models))
        self.assertTrue(all('_reasoning_effort' not in m for m in reply_models))
        for key in ('base_url','name','encrypted_key','timeout','max_tokens','max_input_chars'):
            self.assertEqual(reply_models[0][key],self.cfg.load()['model'][key])
        for model,messages in zip(planner_models,self.planner.calls):
            current=model|{'base_url':'https://api.deepseek.com','name':'deepseek-flash'}
            before={k:v for k,v in current.items() if k!='_reasoning_effort'}
            old,_=build_request(before,messages);new,contract=build_request(current,messages)
            self.assertEqual(new,old|{'thinking':{'type':'enabled'},'reasoning_effort':'high'})
            self.assertEqual(contract['thinking'],'enabled');self.assertEqual(contract['reasoning_effort'],'high')
        self.assertNotIn('_reasoning_effort',self.cfg.load()['model'])

    async def test_translation_chooses_detail_from_context_and_keeps_plain_text_guidance_last(self):
        from persona_runtime.knowledge_translation import GUIDANCE
        self.enable();result=await self.ask('先简单说说这套打法。')
        self.assertEqual(result['status'],'completed',result['error_code'])
        self.assertTrue(self.main.calls[-1][0]['content'].endswith(GUIDANCE))
        self.assertEqual(result['context']['knowledge']['translation']['model_source'],'main')
        self.assertIn('追问某一处',self.main.calls[-1][0]['content'])
        self.assertIn('完整攻略',self.main.calls[-1][0]['content'])
        self.assertNotIn(GUIDANCE,self.planner.calls[0][0]['content'])

    async def test_organizer_failure_still_translates_original_sources_without_reply_veto(self):
        from persona_runtime.knowledge_translation import ORGANIZE,GUIDANCE
        self.enable();complete=self.planner.complete
        async def failing(model,messages):
            if messages[0]['content']==ORGANIZE:raise ModelError('provider_http_400')
            return await complete(model,messages)
        self.planner.complete=failing;result=await self.ask()
        self.assertEqual(result['status'],'completed',result['error_code'])
        self.assertTrue(self.main.calls[-1][0]['content'].endswith(GUIDANCE))
        self.assertIn('玩家试了不同装备',self.main.calls[-1][0]['content'])

    async def test_primary_retry_reuses_successful_retrieval_and_never_replays_unknown_organizer(self):
        self.enable();complete=self.main.complete
        async def fail(model,messages):raise ModelError('provider_http_400')
        self.main.complete=fail;failed=await self.ask()
        self.assertEqual(failed['status'],'failed')
        planner_calls=len(self.planner.calls);tool_calls=len(self.tools)
        with self.store.connection(write=True) as db:
            row=db.execute("SELECT id FROM organization_tasks WHERE json_extract(payload_json,'$.knowledge_processing.stage')='conversation_organize'").fetchone()
            aid=db.execute('SELECT id FROM organization_attempts WHERE task_id=?',(row['id'],)).fetchone()[0]
            db.execute('DELETE FROM organization_receipts WHERE attempt_id=?',(aid,))
            db.execute("UPDATE organization_attempts SET status='interrupted',usage_json=NULL WHERE id=?",(aid,))
            db.execute("UPDATE organization_tasks SET status='blocked_unknown',error_code='organization_effect_unknown' WHERE id=?",(row['id'],))
            attempts=db.execute('SELECT count(*) FROM organization_attempts').fetchone()[0]
        self.main.complete=complete;await self.chat.retry(failed['id']);await asyncio.wait_for(self.chat.wait(),30)
        result=self.chat.get(failed['id']);self.assertEqual(result['status'],'completed',result['error_code'])
        self.assertEqual(len(self.planner.calls),planner_calls);self.assertEqual(len(self.tools),tool_calls)
        self.assertIn('organization_effect_unknown',result['context']['knowledge']['retrieval']['errors'])
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM organization_attempts').fetchone()[0],attempts)

    def test_translation_input_budget_does_not_overwrite_detailed_source_material(self):
        from persona_runtime.knowledge_translation import organize_input,context,ORGANIZE
        sources=[{'id':'s','url':'https://community.example/post','title':'标题','content':'详细材料\\\"。'*9000}]
        original=copy.deepcopy(sources);model={'max_input_chars':2000}
        value=organize_input('问题',[],sources,model)
        self.assertLessEqual(len(ORGANIZE)+len(json.dumps(value,ensure_ascii=False)),model['max_input_chars'])
        body,_,_=context({'sources':sources,'organized':{'notes':'后台细节。'*1000,'source_ids':['s']}},2000)
        self.assertLessEqual(len(body),2000);self.assertEqual(sources,original)

class OrderingAndTransportTests(unittest.TestCase):
    def test_official_status_is_a_preference_not_a_truth_or_relevance_veto(self):
        plan={'official_domains':['official.example'],'community_domains':['community.example']}
        official={'url':'https://official.example/a','rerank_score':.3};community={'url':'https://community.example/b','rerank_score':2.8,'content':'点赞1200'}
        self.assertLess(rank(community,plan),rank(official,plan))
    def test_public_sites_are_not_restricted_to_a_topic_domain_whitelist(self):
        self.assertEqual(public_url('https://www.miyoushe.com/ys/article/123'),'https://www.miyoushe.com/ys/article/123')
        self.assertEqual(public_url('https://df.qq.com/'),'https://df.qq.com/')
    def test_public_retrieval_does_not_expose_local_services(self):
        from persona_runtime.models import RuntimeErrorBase
        for url in ('file:///C:/secret','http://127.0.0.1/','http://localhost/','http://192.168.1.1/'):
            with self.subTest(url=url),self.assertRaises(RuntimeErrorBase):public_url(url)

class AdminApiTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.M4Tests.setUp
    asyncTearDown=fixtures.M4Tests.asyncTearDown
    async def test_original_management_endpoint_shows_and_enables_general_mode(self):
        from aiohttp import ClientSession
        from aiohttp.test_utils import TestServer
        from persona_runtime.server import create_app
        app=create_app(self.paths)
        async with TestServer(app) as server,ClientSession(headers={'Authorization':'Bearer '+self.cfg.token()}) as client:
            async def manage(action,args):
                async with client.post(server.make_url('/v1/manage'),json={'action':action,'args':args}) as response:
                    value=await response.json();self.assertEqual(response.status,200,value);return value
            current=await manage('knowledge.manage.show',{'persona_id':self.persona['id']})
            self.assertEqual(current['mode'],PROFILE)
            await manage('knowledge.general.update',{'persona_id':self.persona['id'],'enabled':True,
                'expected_reader_revision':current['policy']['revision'],'expected_processing_revision':current['processing_policy']['revision'],
                'expected_web_revision':current['web_policy']['revision'],'operation_id':'general-api-enable'})
            result=await manage('knowledge.manage.show',{'persona_id':self.persona['id']})
            self.assertTrue(result['policy']['data']['enabled']);self.assertEqual(result['processing_policy']['data']['prompt_profile'],PROFILE)

if __name__=='__main__':unittest.main()
