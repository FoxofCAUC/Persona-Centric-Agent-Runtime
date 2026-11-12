import asyncio,copy,hashlib,json,time,unittest
from types import SimpleNamespace
from unittest.mock import patch
from persona_runtime.chat import ModelCompletion
from persona_runtime.config import atomic_json
from persona_runtime.knowledge_service import read
from persona_runtime.knowledge_store import search
from persona_runtime.knowledge_transfer import restore
from persona_runtime.knowledge_web import document
from persona_runtime.models import RuntimeErrorBase,generic_persona,identifier
from tests import test_m4 as fixtures
from tests import test_knowledge_web as web
from tests.test_knowledge_wikimedia import envelope
from tests import test_knowledge_maintenance as maintenance
from tests import test_knowledge_routing as routing

class CombinedModel:
    def __init__(self,test):self.test=test;self.calls=[];self.background=[];self.current=False
    async def complete(self,model,messages):
        data=json.loads(messages[-1]['content']);self.calls.append(data)
        if 'document' in data:
            self.background.append(dict(model))
            if 'proposals' not in data:
                body=data['document'].get('excerpt') or data['document']['windows'][0]['text'];color='红色' if '红色' in body else '蓝色'
                out={'claims':[{'subject':'测试行星','predicate':'颜色','value':color,'kind':'source_claim','quote':body,'valid_from':None,'valid_until':None}]}
            else:out={'decisions':[{'index':i,'supported':True,'temporal_ok':True,'version_ok':True,'kind':'source_claim','note':'逐字原文支持。'} for i in range(len(data['proposals']['claims']))]}
        elif 'recent_context' in data:out={'queries':['测试行星'],'freshness':'current' if self.current else 'snapshot'}
        elif 'topics' in data:out={'topic_ids':['planet']}
        elif 'sources' in data and 'fetch_ids' in messages[0]['content']:out={'fetch_ids':[data['sources'][0]['id']]}
        elif 'sources' in data:out={'section_ids':[data['sources'][0]['id']]}
        elif 'units' in data:out={'decisions':[{'index':i,'supported':True,'temporal_ok':True,'version_ok':True,'note':'核对支持。'} for i in range(len(data['units']))]}
        else:
            m=data['materials'][0];out={'units':[{'text':m['text'],'kind':'source_claim','refs':[{'material_id':m['id'],'quote':m['text']}]}]}
        return json.dumps({'format_version':1,**out},ensure_ascii=False),{'total_tokens':4}

class BridgeTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.M4Tests.setUp
    asyncTearDown=web.WebTests.asyncTearDown
    def enable(self,learn=True):
        self.pid=self.persona['id'];self.now=time.time();self.color='蓝色';self.revision=1;self.cloud=[];self.source_calls=[]
        self.cfg.update({'model':{'base_url':'https://api.deepseek.com','name':'fake-main','api_key':'artificial-main','timeout':180,'max_tokens':8192}})
        self.chat.knowledge.update_policy(self.pid,{'enabled':True,'channels':['desktop']},0,'read')
        p=self.org.web.policy(self.pid);self.org.web.update_policy(self.pid,p['data']|{'enabled':True,'topics':{'planet':'测试行星'},'domains':['zh.wikipedia.org']},0,'web')
        self.maintenance=self.org.knowledge;self.maintenance.clock=lambda:self.now
        p=self.maintenance.policy(self.pid);self.maintenance.update_policy(self.pid,p['data']|{'enabled':learn,'daily_scan':False},0,'learn')
        self.source=self.maintenance.fetcher=maintenance.Source();self.put()
        self.processor=self.org.client=CombinedModel(self);self.main=self.chat.client=web.Main()
        routing.configure(self);self.choice='web'
        policy=self.org.processing.policy(self.pid)
        self.org.processing.update_policy(self.pid,policy['data']|{'prompt_profile':'question_focused_v6'},policy['revision'],'v6-bridge')
        async def native(stage,args,model):
            self.cloud.append((stage,args))
            assert stage=='search','Wiki learning should use revision-aware API directly'
            return ModelCompletion(json.dumps({'results':[{'title':'测试行星','url':'https://zh.wikipedia.org/wiki/测试行星','summary':'','snippet':'','content':'','date':None,'provider':'wikimedia_api'}]}),{'total_tokens':0},{'request_headers_sent':True})
        async def wiki(stage,args,timeout):
            self.source_calls.append((stage,args));value=json.loads(envelope());first=json.loads(value['responses'][0]['body']);first['parse'].update(title='测试行星',pageid=5,revid=self.revision,text='<p>测试行星的颜色是'+self.color+'。</p>')
            second=json.loads(value['responses'][1]['body']);second['query']['pages'][0]['revisions'][0]['revid']=self.revision
            for row,body in zip(value['responses'],(first,second)):
                row['body']=json.dumps(body,ensure_ascii=False);row['sha256']=hashlib.sha256(row['body'].encode()).hexdigest()
            return ModelCompletion(json.dumps(value),{'total_tokens':0,'source_network_requests':2},{'request_headers_sent':True})
        self.org.web.transport=native;self.org.web.wikimedia_transport=wiki
        self.worker=asyncio.create_task(self.org.worker())
    def put(self):
        self.source.values[('wikipedia_zh','测试行星')]={'upstream_id':'5','revision':self.revision,'title':'测试行星','body':'测试行星的颜色是'+self.color+'。',
            'upstream_at':'2026-11-10T00:00:00+00:00','url':'https://zh.wikipedia.org/wiki/测试行星','metadata':{'parser':'artificial-v1'},'raw':{'artificial':True}}
    async def ask(self,text='测试行星的颜色是什么？'):
        result=await self.chat.submit('desktop',text,identifier());await asyncio.wait_for(self.chat.wait(),15);return self.chat.get(result['id'])
    async def learned(self):
        for _ in range(400):
            with self.store.connection() as db:
                rows=db.execute('SELECT state FROM knowledge_batches').fetchall()
                pending=db.execute("SELECT count(*) FROM knowledge_fetch_jobs WHERE state IN ('queued','fetching')").fetchone()[0]
            if rows and all(r[0] not in ('waiting_extract','extracting','waiting_review','reviewing') for r in rows) and not pending:return
            await asyncio.sleep(.025)
        self.fail('maintenance did not commit')
    async def test_fetched_page_is_tracked_learned_by_main_and_used_without_ann_or_new_source(self):
        self.enable();first=await self.ask();self.assertEqual(first['status'],'completed',first['error_code']);await self.learned()
        self.assertEqual(len(self.processor.background),2)
        with self.store.connection() as db:
            self.assertIsNone(read(db,'registry',{'active':None})['data']['active'])
        self.assertTrue(all(m['name']=='fake-main' and m['encrypted_key']==self.cfg.load()['model']['encrypted_key'] for m in self.processor.background))
        before=len(self.source_calls)
        with patch('persona_runtime.knowledge_processing.search_turn',side_effect=AssertionError('ANN used')):second=await self.ask()
        self.assertEqual(second['status'],'completed',second['error_code']);self.assertEqual(len(self.source_calls),before)
        self.assertTrue(all(e['kind']=='knowledge_claim' for e in second['context']['knowledge']['evidence']))
    async def test_topic_hints_are_owner_scoped_names_without_fact_values(self):
        self.enable();await self.ask();await self.learned()
        other=self.service.create_persona(generic_persona())['id']
        self.chat.knowledge.update_policy(other,{'enabled':True,'channels':['desktop']},0,'hint-other')
        with self.store.connection() as db:
            own=self.org.processing.topic_hints(db,{'persona_id':self.pid,'channel':'desktop'},'测试行星')
            foreign=self.org.processing.topic_hints(db,{'persona_id':other,'channel':'desktop'},'测试行星')
        self.assertTrue(own);self.assertFalse(foreign)
        self.assertEqual(set(own[0]),{'subject','predicate','source_name','source_origin'})
        self.assertNotIn('蓝色',json.dumps(own,ensure_ascii=False))

    async def test_reading_without_learning_does_not_create_maintenance_jobs(self):
        self.enable(learn=False);result=await self.ask();self.assertEqual(result['status'],'completed',result['error_code'])
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM knowledge_fetch_jobs').fetchone()[0],0)
        self.assertEqual(self.processor.background,[])
    async def test_daily_correction_invalidates_old_web_and_new_answer_uses_updated_claims(self):
        self.enable();first=await self.ask();await self.learned();self.color='红色';self.revision=2;self.put();self.now+=86401
        p=self.maintenance.policy(self.pid);self.maintenance.update_policy(self.pid,p['data']|{'daily_scan':True},p['revision'],'daily')
        await self.learned_after_tick()
        self.assertIsNone(self.chat.get(first['id'])['reply'])
        second=await self.ask();self.assertEqual(second['status'],'completed',second['error_code'])
        self.assertTrue(any('红色' in e['quote'] for e in self.chat.knowledge.show_evidence(self.pid,second['id'])['evidence']))
    async def learned_after_tick(self):
        await self.maintenance.tick();await self.learned()
    async def test_new_web_revision_stops_old_claims_even_before_background_review_and_restore(self):
        self.enable();first=await self.ask();await self.learned();self.service.backup('old.sqlite3')
        p=self.maintenance.policy(self.pid);self.maintenance.update_policy(self.pid,p['data']|{'enabled':False},p['revision'],'stop-learning')
        self.color='红色';self.revision=2;self.put();self.processor.current=True
        second=await self.ask('请核对测试行星目前的颜色。');self.assertEqual(second['status'],'completed',second['error_code'])
        self.assertIsNone(self.chat.get(first['id'])['reply'])
        with self.store.connection() as db:self.assertFalse(search(db,self.pid,'desktop','测试行星',current=True))
        restore(self.chat.knowledge,'old.sqlite3',0,'restore-old')
        with self.store.connection() as db:
            page=db.execute('SELECT latest_revision,head_document_id FROM knowledge_pages').fetchone()
            self.assertEqual(page[0],2);self.assertIsNone(page[1]);self.assertFalse(search(db,self.pid,'desktop','测试行星',current=True))
    async def test_source_withdrawal_blocks_web_cache_and_backup_restore(self):
        self.enable();first=await self.ask();await self.learned();self.service.backup('before-withdraw.sqlite3')
        with self.store.connection() as db:target=db.execute('SELECT id FROM knowledge_targets').fetchone()[0]
        self.maintenance.withdraw(self.pid,target,'明确撤回',0,'withdraw-source')
        self.assertIsNone(self.chat.get(first['id'])['reply'])
        restore(self.chat.knowledge,'before-withdraw.sqlite3',0,'restore-withdrawn')
        with self.store.connection() as db:
            self.assertFalse(search(db,self.pid,'desktop','测试行星'))
            item=first['context']['knowledge']['evidence'][0]
            with self.assertRaises(RuntimeErrorBase):document(db,self.pid,'desktop',item)
    async def test_role_specific_document_withdrawal_also_blocks_its_maintained_claims(self):
        self.enable();first=await self.ask();await self.learned()
        item=first['context']['knowledge']['evidence'][0];policy=self.org.web.policy(self.pid)
        with self.store.connection() as db:rev=read(db,'web.invalidations',{})['revision']
        self.org.web.invalidate(self.pid,item['document_id'],'不再使用该版本',rev,'withdraw-web')
        with self.store.connection() as db:self.assertFalse(search(db,self.pid,'desktop','测试行星'))
    async def test_withdrawn_unchanged_head_cannot_reuse_older_extraction(self):
        self.enable();await self.ask();await self.learned()
        self.revision=2;self.put();self.now+=2;self.processor.current=True
        second=await self.ask('请核对测试行星目前的颜色。');await self.learned()
        self.assertEqual(second['status'],'completed',second['error_code'])
        self.assertEqual(len(self.processor.background),2,'unchanged text must reuse extraction')
        with self.store.connection() as db:
            evidence=search(db,self.pid,'desktop','测试行星')
            self.assertTrue(evidence);self.assertEqual(evidence[0]['upstream_revision'],1)
            self.assertEqual(evidence[0]['observed_revision'],2)
            revision=read(db,'web.invalidations',{})['revision']
        self.org.web.invalidate(self.pid,second['context']['knowledge']['evidence'][0]['document_id'],'撤回当前版本',revision,'withdraw-same-body')
        with self.store.connection() as db:self.assertFalse(search(db,self.pid,'desktop','测试行星'))

    async def test_other_role_cannot_read_claims_or_share_pending_fetch_owner(self):
        self.enable();await self.ask();await self.learned()
        other=self.service.create_persona(generic_persona())['id'];self.chat.knowledge.update_policy(other,{'enabled':True,'channels':['desktop']},0,'other-read')
        p=self.maintenance.policy(other);self.maintenance.update_policy(other,p['data']|{'enabled':True,'daily_scan':False},0,'other-learn')
        self.now+=2
        with self.store.connection(write=True) as db:
            self.assertFalse(search(db,other,'desktop','测试行星'))
            a=self.maintenance._enqueue(db,self.pid,{'source_id':'wikipedia_zh','public_id':'测试行星'},True,'current')
            b=self.maintenance._enqueue(db,other,{'source_id':'wikipedia_zh','public_id':'测试行星'},True,'current')
            self.assertNotEqual(a,b)
    async def test_expired_claim_cache_does_not_substitute_for_a_fresh_source(self):
        self.enable();await self.ask();await self.learned();before=len(self.source_calls)
        self.now+=86401;self.color='红色';self.revision=2;self.put()
        clock=SimpleNamespace(time=lambda:self.now)
        with patch('persona_runtime.knowledge_web.time',clock),patch('persona_runtime.knowledge_store.time',clock):result=await self.ask()
        self.assertEqual(result['status'],'completed',result['error_code']);self.assertGreater(len(self.source_calls),before)
        self.assertTrue(all(e['kind']=='web_document' for e in result['context']['knowledge']['evidence']))

    async def test_invalidated_answer_retains_only_permitted_user_question_and_status(self):
        self.enable();first=await self.ask();await self.learned()
        with self.store.connection() as db:target=db.execute('SELECT id FROM knowledge_targets').fetchone()[0]
        self.maintenance.withdraw(self.pid,target,'撤回',0,'withdraw-context')
        self.choice='none';follow=await self.ask('给我刚才的出处。')
        self.assertEqual(follow['status'],'completed',follow['error_code'])
        state=self.route_calls[-1][1]['state'];previous=next(r for r in state['recent_context'] if r['turn_id']==first['id'])
        self.assertEqual(previous['user'],'测试行星的颜色是什么？');self.assertEqual(previous['assistant'],'')
        self.assertEqual(previous['request_state'],'evidence_invalidated')
        self.assertEqual(follow['context']['knowledge']['reference_status'],'previous_evidence_unavailable')
        self.assertIn('不要说用户没有提供上下文',self.main.calls[-1][1][0]['content'])
        from persona_runtime.models import LOCAL_USER_ID,timestamp
        with self.store.connection(write=True) as db:
            event=db.execute('SELECT user_event_id FROM turns WHERE id=?',(first['id'],)).fetchone()[0]
            db.execute('INSERT INTO source_exclusions VALUES(?,?,?,?,?)',(event,self.pid,LOCAL_USER_ID,'explicit removal',timestamp()))
        await self.ask('晚安。')
        self.assertFalse(any(r['turn_id']==first['id'] for r in self.route_calls[-1][1]['state']['recent_context']))

    async def test_new_owner_extraction_does_not_receive_other_owner_claims(self):
        self.enable();await self.ask();await self.learned();self.now+=2
        other=self.service.create_persona(generic_persona())['id'];self.chat.knowledge.update_policy(other,{'enabled':True,'channels':['desktop']},0,'second-read')
        p=self.maintenance.policy(other);self.maintenance.update_policy(other,p['data']|{'enabled':True,'daily_scan':False},0,'second-learn')
        with self.store.connection(write=True) as db:
            jobid=self.maintenance._enqueue(db,other,{'source_id':'wikipedia_zh','public_id':'测试行星'},True,'current')
            job=dict(db.execute('SELECT j.*,t.source_id,t.public_id FROM knowledge_fetch_jobs j JOIN knowledge_targets t ON t.id=j.target_id WHERE j.id=?',(jobid,)).fetchone())
            self.maintenance.ingest(db,job,copy.deepcopy(self.source.values[('wikipedia_zh','测试行星')]))
            batch=dict(db.execute('SELECT * FROM knowledge_batches WHERE owner_persona=?',(other,)).fetchone())
            payload=self.maintenance.payload(db,batch,'extract')['external_knowledge']
            self.assertEqual(payload['existing_claims'],[]);self.assertEqual(payload['retention_candidates'],[])

    async def test_unknown_main_learning_attempt_is_not_replayed_by_daily_discovery(self):
        from persona_runtime.chat import ModelError
        self.enable();original=self.processor.complete
        async def unknown(model,messages):
            if 'document' in json.loads(messages[-1]['content']):raise ModelError('provider_network_error',diagnostics={'request_headers_sent':True})
            return await original(model,messages)
        self.processor.complete=unknown;first=await self.ask();self.assertEqual(first['status'],'completed')
        await self.learned();self.revision=2;self.put();self.now+=86401
        p=self.maintenance.policy(self.pid);self.maintenance.update_policy(self.pid,p['data']|{'daily_scan':True},p['revision'],'scan-unknown')
        await self.learned_after_tick()
        with self.store.connection() as db:
            attempts=db.execute("SELECT a.status FROM organization_tasks t JOIN organization_attempts a ON a.task_id=t.id WHERE json_type(t.payload_json,'$.external_knowledge')='object'").fetchall()
        self.assertEqual(len(attempts),1);self.assertEqual(attempts[0][0],'interrupted')
