"""Main-configured read-only tools, public arguments and durable evidence."""
import asyncio
import copy
import json
import unittest
from unittest.mock import patch

from persona_runtime.chat import ModelCompletion,ModelError
from persona_runtime.knowledge_service import read
from persona_runtime.knowledge_web import document,public_url
from persona_runtime.models import RuntimeErrorBase,generic_persona
from persona_runtime.portable_memory import import_export
from persona_runtime.knowledge_transfer import restore
from tests import test_m4 as fixtures


class Models:
    def __init__(self):self.calls=[]
    async def complete(self,model,messages):
        self.calls.append(copy.deepcopy((model,messages)))
        data=json.loads(messages[-1]['content'])
        if 'recent_context' in data:out={'queries':['公开灯塔'],'freshness':'current'}
        elif 'topics' in data:out={'topic_ids':['lighthouse']}
        elif 'sources' in data and 'web_policy' in data:out={'fetch_ids':[data['sources'][0]['id']]}
        elif 'sources' in data:out={'section_ids':[data['sources'][0]['id']]}
        elif 'units' in data:out={'decisions':[{'index':i,'supported':True,'temporal_ok':True,'version_ok':True,'note':'按实际网页正文核对。'} for i in range(len(data['units']))]}
        else:
            m=data['materials'][0]
            out={'units':[{'text':'来源公告称，公开验收灯塔09:00开放。','kind':'source_claim','refs':[{'material_id':m['id'],'quote':m['text']}]}]}
        return json.dumps({'format_version':1,**out},ensure_ascii=False),{'total_tokens':9}


class Main:
    def __init__(self):self.calls=[]
    async def complete(self,model,messages):
        self.calls.append(copy.deepcopy((model,messages)))
        return '公告说公开验收灯塔09:00开放。',{'total_tokens':5}


class WebTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.M4Tests.setUp

    async def asyncTearDown(self):
        self.chat.knowledge.close();self.org.draining=True
        with self.store.connection() as db:
            pending=[row[0] for row in db.execute("SELECT id FROM turns WHERE status IN ('queued','running')")]
        for ident in pending:await self.chat.cancel(ident)
        await self.chat.wait()
        await asyncio.wait_for(self.org.drain(),10)
        if getattr(self,'worker',None):
            self.worker.cancel();await asyncio.wait_for(asyncio.gather(self.worker,return_exceptions=True),10)

    def enable(self):
        self.cfg.update({'model':{'base_url':'https://api.deepseek.com','name':'deepseek-flash','api_key':'artificial-main-only','timeout':180,'max_tokens':8192}})
        self.chat.knowledge.update_policy(self.persona['id'],{'enabled':True,'channels':['desktop']},0,'read-web')
        policy=self.org.web.policy(self.persona['id'])
        self.data=policy['data']|{'enabled':True,'domains':['example.com'],'topics':{'lighthouse':'公开验收灯塔 开放时间'}}
        self.org.web.update_policy(self.persona['id'],self.data,0,'enable-web')
        policy=self.org.processing.policy(self.persona['id'])
        self.org.processing.update_policy(self.persona['id'],policy['data']|{'prompt_profile':'question_focused_v3'},policy['revision'],'legacy-web-fixture')
        self.models=self.org.client=Models();self.main=self.chat.client=Main()
        self.tool_calls=[];self.content='公开验收灯塔每天09:00开放。'
        self.org.web.transport=self.transport
        self.worker=asyncio.create_task(self.org.worker())

    async def transport(self,stage,args,model):
        self.tool_calls.append((stage,copy.deepcopy(args),copy.deepcopy(model)))
        if stage=='search':
            value={'results':[{'title':'公开公告','url':'https://example.com/news','summary':'摘要不是事实证据','snippet':'摘要','content':'',
                'date':None,'site_name':'Example','site_icon':None,'authority':-1,'rerank_score':2}]}
        else:value={'title':'公开公告','content':self.content}
        return ModelCompletion(json.dumps(value,ensure_ascii=False),{'tool':stage,'http_status':200,'billable':True,'amount_usd':'0.00075'}, {})

    async def ask(self,text='我昨晚和朋友聊到这里，公开验收灯塔几点开放？'):
        turn=await self.chat.submit('desktop',text,'web-'+__import__('uuid').uuid4().hex)
        await asyncio.wait_for(self.chat.wait(),30);return self.chat.get(turn['id'])

    async def test_source_policy_edit_preserves_processing_profile(self):
        self.enable();before=self.org.processing.policy(self.persona['id'])['data']['prompt_profile']
        policy=self.org.web.policy(self.persona['id'])
        self.org.web.update_policy(self.persona['id'],policy['data'],policy['revision'],'source-policy-edit')
        self.assertEqual(self.org.processing.policy(self.persona['id'])['data']['prompt_profile'],before)

    async def test_full_flow_uses_main_config_and_only_registered_query_leaves(self):
        self.enable();result=await self.ask()
        self.assertEqual(result['status'],'completed',result['error_code'])
        self.assertEqual(len(self.models.calls),6);self.assertEqual(len(self.tool_calls),2)
        for _,_,model in self.tool_calls:
            self.assertEqual(model['encrypted_key'],self.cfg.load()['model']['encrypted_key'])
            self.assertEqual(model['timeout'],180);self.assertEqual(model['max_tokens'],8192)
            self.assertEqual(model['name'],'deepseek-flash')
        self.assertEqual(self.tool_calls[0][1]['query'],'公开验收灯塔 开放时间')
        self.assertNotIn('ori_question',self.tool_calls[0][1])
        self.assertNotIn('我昨晚',json.dumps(self.tool_calls[0][1],ensure_ascii=False))
        self.assertIn('来源公告称',self.main.calls[-1][1][0]['content'])
        self.assertNotIn('摘要不是事实证据',self.main.calls[-1][1][0]['content'])
        evidence=self.chat.knowledge.show_evidence(self.persona['id'],result['id'])['evidence']
        self.assertEqual(evidence[0]['quote'],self.content)
        self.assertEqual(evidence[0]['source_snapshot_at'],None)
        from persona_runtime.knowledge_admin import overview
        cost=overview(self.chat.knowledge,self.persona['id'])
        self.assertEqual(cost['tool_cost']['calls'],2)
        self.assertEqual(cost['tool_cost']['billable_calls'],2)
        self.assertEqual(cost['tool_cost']['known_amount_usd'],'0.00150')
        self.assertEqual(cost['cost']['calls'],6)

    async def test_organizer_configuration_is_not_used_by_network_module(self):
        self.enable();self.cfg.update({'organization':{'model':{'name':'unrelated-organizer','timeout':45}}})
        result=await self.ask()
        self.assertEqual(result['status'],'completed',result['error_code'])
        self.assertTrue(all(model['name']=='deepseek-flash' and model['timeout']==180 for _,_,model in self.tool_calls))
        self.assertTrue(all(model['name']=='deepseek-flash' for model,_ in self.models.calls))

    async def test_changed_automatic_organizer_does_not_cancel_main_knowledge_task(self):
        self.enable();self.worker.cancel()
        await asyncio.gather(self.worker,return_exceptions=True)
        self.org.schedule.set_automatic(True)
        turn=await self.chat.submit('desktop','公开验收灯塔几点开放？','queued-main-config')
        for _ in range(200):
            with self.store.connection() as db:
                task=db.execute("SELECT id FROM organization_tasks WHERE status='queued'").fetchone()
            if task:break
            await asyncio.sleep(.01)
        self.assertIsNotNone(task)
        self.cfg.update({'organization':{'model':{'name':'changed-organizer'}}})
        self.org.schedule.pause_changed_models()
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT status FROM organization_tasks WHERE id=?',(task['id'],)).fetchone()[0],'queued')
        self.worker=asyncio.create_task(self.org.worker())
        await asyncio.wait_for(self.chat.wait(),30)
        result=self.chat.get(turn['id'])
        self.assertEqual(result['status'],'completed',result['error_code'])
        self.assertTrue(all(model['name']=='deepseek-flash' for model,_ in self.models.calls))

    async def test_disabling_processor_never_falls_back_to_local_raw_path(self):
        self.enable()
        policy=self.org.processing.policy(self.persona['id'])
        self.org.processing.update_policy(self.persona['id'],policy['data']|{'enabled':False},policy['revision'],'stop-web-processing')
        result=await self.ask()
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['error_code'],'web_processing_required')
        self.assertEqual(self.models.calls,[]);self.assertEqual(self.tool_calls,[])

    async def test_main_configuration_change_blocks_instead_of_using_organizer(self):
        self.enable();self.cfg.update({'model':{'max_tokens':7000}})
        result=await self.ask()
        self.assertEqual(result['status'],'failed')
        self.assertEqual(self.models.calls,[]);self.assertEqual(self.tool_calls,[])

    async def test_unregistered_topic_and_bad_urls_never_dispatch(self):
        self.enable()
        policy=self.org.web.policy(self.persona['id'])
        with self.assertRaisesRegex(RuntimeErrorBase,'web_public_topic_required'):
            self.org.web.update_policy(self.persona['id'],self.data|{'topics':{}},policy['revision'],'empty')
        for url in ('http://127.0.0.1/x','https://user:secret@example.com/x','https://example.com:999/x','https://example.com.evil.test/x'):
            with self.assertRaises(RuntimeErrorBase):public_url(url,['example.com'])
        self.assertEqual(self.tool_calls,[])

    async def test_empty_search_is_known_nonbillable_and_never_uses_summary(self):
        self.enable()
        async def empty(stage,args,model):
            self.tool_calls.append((stage,args,model))
            return ModelCompletion('{"results":[]}',{'tool':stage,'http_status':200,'billable':False,'amount_usd':'0'}, {})
        self.org.web.transport=empty
        result=await self.ask()
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['context']['knowledge']['evidence'],[])
        self.assertEqual([r[0] for r in self.tool_calls],['search'])

    async def test_received_tool_http_response_is_reused_after_interruption(self):
        self.enable();publish=self.org.web.accept;stopped=False
        def interrupted(db,payload,out):
            nonlocal stopped
            if payload['knowledge_processing']['stage']=='search' and not stopped:
                stopped=True;raise RuntimeErrorBase('organization_lease_lost')
            return publish(db,payload,out)
        with patch.object(self.org.web,'accept',side_effect=interrupted):first=await self.ask()
        self.assertEqual(first['status'],'failed');self.assertEqual(len(self.tool_calls),1)
        await self.chat.retry(first['id']);await self.chat.wait()
        self.assertEqual(self.chat.get(first['id'])['status'],'completed')
        self.assertEqual(len(self.tool_calls),2)

    async def test_unknown_tool_effect_is_preserved_without_replay(self):
        self.enable()
        async def unknown(stage,args,model):
            self.tool_calls.append((stage,args,model))
            raise ModelError('web_transport_unconfirmed',diagnostics={'request_headers_sent':True})
        self.org.web.transport=unknown
        first=await self.ask();await self.chat.retry(first['id']);await self.chat.wait()
        self.assertEqual(len(self.tool_calls),1)
        self.assertEqual(self.main.calls,[])

    async def test_corrected_and_withdrawn_web_documents_cannot_reenter(self):
        self.enable();first=await self.ask()
        evidence=first['context']['knowledge']['evidence'][0]
        receipt=self.org.web.invalidate(self.persona['id'],evidence['document_id'],'withdrawn',1,'withdraw-web')
        self.assertEqual(self.org.web.invalidate(self.persona['id'],evidence['document_id'],'withdrawn',1,'withdraw-web'),receipt)
        from persona_runtime.knowledge_admin import overview
        view=overview(self.chat.knowledge,self.persona['id'])
        self.assertEqual(view['web_documents'][0]['validity'],'withdrawn')
        self.assertNotIn('body',view['web_documents'][0])
        self.assertEqual(view['web_invalidations']['revision'],receipt['revision'])
        self.assertIsNone(self.chat.get(first['id'])['reply'])
        with self.store.connection() as db:
            with self.assertRaises(RuntimeErrorBase):document(db,self.persona['id'],'desktop',evidence)
        # Restore cannot remove a current withdrawal.
        from persona_runtime.knowledge_web import snapshot,merge_snapshot
        with self.store.connection() as db:saved=snapshot(db,self.persona['id'])
        saved['invalidations']={}
        with self.store.connection(write=True) as db:merge_snapshot(db,saved)
        self.assertIsNone(self.chat.get(first['id'])['reply'])

    async def test_stored_body_integrity_is_checked_before_evidence_reuse(self):
        self.enable();first=await self.ask()
        evidence=first['context']['knowledge']['evidence'][0]
        with self.store.connection(write=True) as db:
            row=read(db,'web.document.'+evidence['document_id'],{})
            row['data']['body']='different source body'
            db.execute('UPDATE knowledge_control SET value=? WHERE key=?',(json.dumps(row),'knowledge.web.document.'+evidence['document_id']))
        with self.assertRaises(RuntimeErrorBase):self.chat.knowledge.show_evidence(self.persona['id'],first['id'])
        self.assertIsNone(self.chat.get(first['id'])['reply'])

    async def test_changed_publication_anchor_invalidates_same_body_version(self):
        self.enable();first=await self.ask()
        evidence=first['context']['knowledge']['evidence'][0]
        payload={'persona_id':self.persona['id'],'knowledge_processing':{'stage':'fetch',
            'input':{'url':'https://example.com/news','published_at':'2026-11-11T00:00:00+00:00','web_policy':self.org.web.policy(self.persona['id'])}}}
        with self.store.connection(write=True) as db:
            new=self.org.web.accept(db,payload,{'title':'公开公告','content':self.content})
        self.assertNotEqual(new['document_id'],evidence['document_id'])
        self.assertIsNone(self.chat.get(first['id'])['reply'])

    async def test_tool_http_429_retries_are_finite_and_separately_billed(self):
        self.enable();original=self.transport;failed=False
        async def limited(stage,args,model):
            nonlocal failed
            if not failed:
                failed=True;self.tool_calls.append((stage,args,model))
                error=ModelError('web_http_429',usage={'tool':stage,'http_status':429,'billable':False,'amount_usd':'0'},diagnostics={'request_headers_sent':True,'http_status':429})
                error.tool_response='{"error":{"code":"rate_limit"}}';raise error
            return await original(stage,args,model)
        self.org.web.transport=limited
        result=await self.ask()
        self.assertEqual(result['status'],'completed',result['error_code'])
        self.assertEqual(len(self.tool_calls),3)
        from persona_runtime.knowledge_admin import overview
        cost=overview(self.chat.knowledge,self.persona['id'])['tool_cost']
        self.assertEqual(cost['calls'],3);self.assertEqual(cost['billable_calls'],2)

    async def test_format21_import_keeps_permissions_off_and_owner_separate(self):
        self.enable();first=await self.ask()
        package=json.loads(__import__('pathlib').Path(self.service.export_persona()['path']).read_text(encoding='utf-8'))
        self.assertEqual(package['format_version'],21)
        self.assertEqual(len(package['knowledge']['web']['documents']),1)
        legacy=copy.deepcopy(package);legacy['format_version']=20
        with self.assertRaises(RuntimeErrorBase):import_export(self.store,legacy,self.persona['id'],'web-false-version')
        from persona_runtime.knowledge_web import validate_snapshot
        broken=copy.deepcopy(package['knowledge']['web']);broken['documents'][0]['fetched_epoch']=float('nan')
        with self.assertRaises(RuntimeErrorBase):validate_snapshot(broken)
        broken=copy.deepcopy(package['knowledge']['web']);broken['invalidations']['foreign-document']={'state':'withdrawn','reason':'foreign'}
        with self.assertRaises(RuntimeErrorBase):validate_snapshot(broken)
        imported=import_export(self.store,package,self.persona['id'],'web-import')
        self.assertFalse(self.org.web.policy(imported['id'])['data']['enabled'])
        old=first['context']['knowledge']['evidence'][0]
        with self.store.connection() as db:
            with self.assertRaises(RuntimeErrorBase):document(db,imported['id'],'desktop',old)


class PolicyAndParserTests(unittest.TestCase):
    def test_planner_cannot_generate_url_or_free_text_query(self):
        from persona_runtime.knowledge_processing_output import parse
        payload={'knowledge_processing':{'stage':'web_plan','input':{'topics':{'known':'公开查询'}}}}
        with self.assertRaises(RuntimeErrorBase):parse(payload,'{"format_version":1,"topic_ids":["私密资料"]}')
        with self.assertRaises(RuntimeErrorBase):parse(payload,'{"format_version":1,"topic_ids":["known"],"query":"原始私人问句"}')
