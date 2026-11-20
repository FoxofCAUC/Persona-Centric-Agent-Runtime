"""Exercise the lookup chain with local opaque inputs and controlled tool receipts."""
import json
import unittest

from persona_runtime import private_lookup
from persona_runtime.models import RuntimeErrorBase,identifier
from persona_runtime.ingress import IngressService
from tests import test_private_reply_stage2 as ordinary

UNUSED_TOOL_NOTE='fixture-unused-note-908'

class ResearchProviders(ordinary.LocalProviders):
    research=True
    version='3'
    def __init__(self):
        super().__init__()
        self.route='web'
        self.lookups=[]
        self.long=False
        self.failing=False
        self.zero_tools=False
        self.body='801。'
        self.notice_body='800。'
        self.consume=True

    async def choose(self,node,attempt,state,options):
        if node in ('J4','J5','J6.mode'):
            self.calls.append((node,attempt,state))
            choice={'J4':self.route,'J5':options[0],'J6.mode':'long_detail' if self.long and 'long_detail' in options else options[0]}[node]
            return choice,{'total_tokens':1}
        return await super().choose(node,attempt,state,options)

    async def retrieve(self,step,attempt,query,model):
        self.lookups.append((step,attempt,query))
        if self.failing:raise TimeoutError()
        if self.zero_tools and step=='native':return {'notes':UNUSED_TOOL_NOTE,'sources':[],'tool_calls':0},{}
        return {'notes':'901。902。','sources':[{'url':'https://example.org/801','title':'801',
                'summary':'903','date':'2026-11-20','qualification':'904','kind':'native_search_summary'}],
                'tool_calls':1},{'total_tokens':1}

    async def reply(self,attempt,packet):
        self.bodies.append((attempt,packet))
        if packet['phase']=='search_notice':return self.notice_body,{}
        if self.consume and packet['mode']=='short_answer' and packet['research']['facts']:
            fact=packet['research']['facts'][0]
            parts=[fact['text'].rstrip('。'),fact.get('date'),*fact.get('qualifications',[])]
            return ' '.join(p for p in parts if p)+'。',{}
        return ({'body':'810。','detail':'811。'*75} if packet['mode']=='long_detail' else self.body),{}


class ResearchTests(unittest.IsolatedAsyncioTestCase):
    reopen=ordinary.OrdinaryTests.reopen
    submit=ordinary.OrdinaryTests.submit
    paint=ordinary.OrdinaryTests.paint
    consent=ordinary.OrdinaryTests.consent

    def setUp(self):
        ordinary.OrdinaryTests.setUp(self)
        self.providers=ResearchProviders();self.reopen()
        with self.store.connection(write=True) as db:
            for key,data in [('policy.',{'enabled':True,'channels':['desktop']}),('web.policy.',{'enabled':True})]:
                db.execute("INSERT OR REPLACE INTO knowledge_control VALUES(?,?)",
                           ('knowledge.'+key+self.pid,json.dumps({'revision':1,'data':data})))

    def show(self,tid,phase):
        units=[u for u in self.out.list(self.pid,self.sid,tid) if u['phase']==phase]
        self.assertEqual(len(units),1,self.chat.get(tid)['error_code'])
        uid=units[0]['id'];plan=self.out.plan(self.pid,self.sid,uid)
        return ''.join(self.paint(uid,plan,i) for i in range(len(plan['items'])))

    async def continue_turn(self,tid):
        await self.chat.retry(tid)
        await self.chat.wait()

    async def test_release_question_retains_public_subject_and_temporal_clause(self):
        self.providers.grounded_search=True
        question='最近《701》是不是快要更新了？帮我查一下703'
        tid=await self.submit(question)
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        query=self.providers.lookups[0][2]
        self.assertEqual(query['question'],'最近《701》是不是快要更新了？703')
        self.assertRegex(query['as_of'],r'^\d{4}-\d{2}-\d{2}$')
        self.assertEqual(self.providers.bodies[-1][1]['research']['lookup_outcome'],'acquired')
        self.show(tid,'main')

    async def test_unrelated_personal_preamble_stays_local(self):
        self.providers.grounded_search=True
        tid=await self.submit('我今天把《701》送人了。帮我查一下703')
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        self.assertEqual(self.providers.lookups[0][2]['question'],'703')

    def test_synthesis_citation_binds_to_the_right_occurrence(self):
        import sqlite3
        source={'id':'ref','url':'https://example.org/1','title':'701','date':None}
        note='701'+'0'*650+'。701。702。'
        data={'sources':[source],'origin':'stored','notes':note}
        with sqlite3.connect(':memory:') as db:
            db.execute('create table private_lookup_calls(turn_id,step,state,result_json)')
            receipt={'notes':note,'annotations':[{'url_citation':{'start_index':a,'end_index':b,'url':source['url']+'#1'}}
                                                  for a,b in ((0,4),(len(note)-4,len(note)))]}
            db.execute("insert into private_lookup_calls values('stored','native','received',?)",(json.dumps(receipt),))
            citations=private_lookup.research_citations(db,data)
            self.assertEqual(private_lookup.research_citations(db,{**data,'sources':[]}),[])
            self.assertEqual(private_lookup.research_citations(db,{**data,'notes':'703'}),[])
        facts=private_lookup.ranked(data,'701',grounded=True,citations=citations)
        cites={f['text']:f['citations'] for f in facts if f.get('record_id')}
        self.assertNotIn('701。',cites)
        self.assertEqual(cites['702。'][0]['id'],'ref')

    async def test_elliptical_followup_shows_actual_evidence_to_router(self):
        self.providers.grounded_search=True
        choose=self.providers.choose
        first=await self.submit('帮我查一下703')
        self.show(first,'search_notice')
        await self.continue_turn(first)
        first_body=self.show(first,'main')
        async def use_existing(node,attempt,state,options):
            if node=='J4':
                self.assertIn('903',state['available_evidence'])
                return 'none',{}
            if node=='J5':
                self.assertEqual(state['previous_reply'],first_body)
            return await choose(node,attempt,state,options)
        self.providers.choose=use_existing
        second=await self.submit('那具体是几点？')
        self.show(second,'main')
        self.assertEqual(self.providers.bodies[-1][1]['research']['origin'],first)
        self.assertEqual(len(self.providers.lookups),1)

    async def test_private_query_receipt_then_evidence_and_reuse(self):
        self.consent(True)
        tid=await self.submit('13800000000')
        self.assertFalse(self.providers.lookups)
        await self.continue_turn(tid)
        self.assertFalse(self.providers.lookups)
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        self.assertEqual(self.providers.lookups[0][2]['question'],'13800000000')
        packet=self.providers.bodies[-1][1]
        self.assertEqual(packet['mode'],'short_answer')
        self.assertTrue(packet['research']['facts'])
        self.assertNotIn('901。902。',json.dumps(packet,ensure_ascii=False))
        displayed=self.show(tid,'main')
        for required in ('903','904','2026-11-20'):
            self.assertIn(required,displayed)
        self.assertEqual(private_lookup.details(self.store,self.pid,self.sid,tid,'research')['notes'],'901。902。')
        self.providers.route='none'
        second=await self.submit('13800000000')
        self.show(second,'main')
        self.assertEqual(len(self.providers.lookups),1)
        self.assertEqual(self.providers.bodies[-1][1]['research']['origin'],tid)
        self.consent(False)
        self.assertEqual(private_lookup.details(self.store,self.pid,self.sid,second,'research')['notes'],'901。902。')
        standard=await self.submit('13800000000');self.show(standard,'main')
        self.assertFalse(self.providers.bodies[-1][1]['research']['facts'])

    async def test_confirm_or_skip_same_turn(self):
        tid=await self.submit('13800000000 821')
        with self.store.connection() as db:it=private_lookup.intent(db,tid)
        self.assertEqual(it['state'],'waiting');self.assertFalse(self.providers.bodies)
        with self.assertRaises(RuntimeErrorBase):
            private_lookup.confirm(self.store,self.pid,self.sid,tid,it['binding'],'81','822')
        for _ in range(2):
            private_lookup.confirm(self.store,self.pid,self.sid,tid,it['binding'],'81','821')
        await self.continue_turn(tid);self.show(tid,'search_notice')
        self.reopen();self.chat.recover();await self.continue_turn(tid);self.show(tid,'main')
        self.assertEqual([call[2]['question'] for call in self.providers.lookups],['821'])
        next_turn=await self.submit('822')
        with self.store.connection() as db:it=private_lookup.intent(db,next_turn)
        private_lookup.confirm(self.store,self.pid,self.sid,next_turn,it['binding'],'82')
        await self.continue_turn(next_turn);self.show(next_turn,'main')
        self.assertEqual(len(self.providers.lookups),1)
        self.assertEqual(self.providers.bodies[-1][1]['research']['status'],'skipped')

    async def test_full_consent_wakes_existing_scope_request(self):
        tid=await self.submit('13800000000')
        self.consent(True)
        IngressService(self.chat).schedule_committed();await self.chat.wait()
        self.show(tid,'search_notice');await self.continue_turn(tid);self.show(tid,'main')
        self.assertEqual(sum(node=='J4' for node,*_ in self.providers.calls),1)

    async def test_tool_free_completion_supplements_only_once(self):
        self.consent(True);self.providers.zero_tools=True
        tid=await self.submit();self.show(tid,'search_notice');await self.continue_turn(tid)
        self.reopen();self.chat.recover();await self.continue_turn(tid);self.show(tid,'main')
        self.assertEqual([s for s,*_ in self.providers.lookups],['native','search'])
        self.assertNotIn(UNUSED_TOOL_NOTE,json.dumps(self.providers.bodies[-1][1]))

    async def test_unknown_search_is_not_replayed(self):
        self.consent(True);self.providers.failing=True
        tid=await self.submit();self.show(tid,'search_notice');await self.continue_turn(tid)
        await self.continue_turn(tid);self.show(tid,'main')
        self.assertEqual(len(self.providers.lookups),1)
        self.assertEqual(self.providers.bodies[-1][1]['research']['status'],'unconfirmed')
        self.assertFalse(self.providers.bodies[-1][1]['research']['facts'])

    async def test_long_body_has_independent_detail_and_review(self):
        self.providers.route='none';self.providers.long=True
        ingress=IngressService(self.chat)
        payload={'persona_id':self.pid,'session_id':self.sid,'channel':'desktop','text':'828',
                 'long_requested':True,'request_id':identifier()}
        receipt=ingress.accept(payload)
        with self.store.connection() as db:batch=db.execute('SELECT revision,state FROM input_batches WHERE id=?',(receipt['batch_id'],)).fetchone()
        if batch['state']=='collecting':
            ingress.action(receipt['batch_id'],'seal',batch['revision'],identifier(),self.pid,self.sid)
        await self.chat.wait()
        tid=ingress.lookup('desktop',payload['request_id'])['turn_id']
        self.assertEqual(self.show(tid,'main'),'810。')
        self.assertEqual(private_lookup.details(self.store,self.pid,self.sid,tid,'long')['body'],'811。'*75)
        self.assertNotIn('J7',[node for node,*_ in self.providers.calls])
        next_turn=await self.submit('822');self.show(next_turn,'main')
        with self.assertRaises(RuntimeErrorBase):private_lookup.details(self.store,self.pid,self.sid,next_turn,'long')

    async def test_retracted_evidence_blocks_pending_main_and_private_cache_is_scoped(self):
        from persona_runtime.knowledge_service import KnowledgeService
        self.consent(True)
        tid=await self.submit('13800000000')
        self.show(tid,'search_notice');await self.continue_turn(tid)
        with self.store.connection() as db:
            data=private_lookup.evidence(db,self.pid,self.sid,tid,active_turn=tid)
        source=data['sources'][0]['id']
        service=KnowledgeService(self.store)
        private_lookup.edit_source(service,self.pid,source,'withdrawn',0,'841',None)
        unit=next(x for x in self.out.list(self.pid,self.sid,tid) if x['phase']=='main')
        plan=self.out.plan(self.pid,self.sid,unit['id'])
        offer=self.out.offer(self.pid,self.sid,unit['id'],plan['id'],plan['revision'],0,'842')
        self.assertEqual(offer['state'],'stopped')
        self.assertIsNone(self.chat.get(tid)['reply_event_id'])

    async def test_corrected_source_invalidates_old_detail_and_next_turn_uses_new_version(self):
        from persona_runtime.knowledge_service import KnowledgeService
        self.consent(True)
        tid=await self.submit('13800000000')
        self.show(tid,'search_notice');await self.continue_turn(tid);self.show(tid,'main')
        old=private_lookup.details(self.store,self.pid,self.sid,tid,'research')
        service=KnowledgeService(self.store)
        private_lookup.edit_source(service,self.pid,old['sources'][0]['id'],'corrected',0,'843','844')
        with self.assertRaises(RuntimeErrorBase):private_lookup.details(self.store,self.pid,self.sid,tid,'research')
        self.providers.route='none'
        new=await self.submit('13800000000')
        self.show(new,'main')
        updated=private_lookup.details(self.store,self.pid,self.sid,new,'research')
        self.assertEqual(updated['notes'],'')
        self.assertEqual(updated['sources'][0]['summary'],'844')
        self.assertEqual(self.providers.bodies[-1][1]['research']['facts'][0]['kind'],'user_correction')
        self.sid=self.chat.session(fresh=True)['session_id']
        other=await self.submit('13800000000');self.show(other,'main')
        self.assertFalse(self.providers.bodies[-1][1]['research']['facts'])

    async def test_unknown_notice_never_starts_search(self):
        self.consent(True);tid=await self.submit()
        unit=self.out.list(self.pid,self.sid,tid)[0];plan=self.out.plan(self.pid,self.sid,unit['id'])
        self.paint(unit['id'],plan,0,'unknown')
        self.reopen();self.chat.recover();await self.continue_turn(tid)
        self.assertFalse(self.providers.lookups)
        self.assertEqual(self.chat.get(tid)['error_code'],'search_notice_not_fully_presented')

    async def test_preflight_offline_recovers_without_repeating_notice(self):
        from persona_runtime.chat import ModelError
        self.consent(True);tid=await self.submit();self.show(tid,'search_notice')
        original=self.providers.retrieve
        async def offline(*args):
            raise ModelError('provider_network_error',diagnostics={'request_headers_sent':False})
        self.providers.retrieve=offline
        await self.continue_turn(tid)
        self.assertEqual(self.chat.get(tid)['error_code'],'desktop_network_wait')
        self.providers.retrieve=original
        self.reopen();self.chat.recover();await self.continue_turn(tid);self.show(tid,'main')
        self.assertEqual(len(self.providers.lookups),1)
        self.assertEqual(len(self.providers.bodies),2)

    async def test_crash_after_tool_receipt_before_record_recovers_once(self):
        self.consent(True);tid=await self.submit();self.show(tid,'search_notice')
        entry=self.chat.private_reply.ordinary
        with self.store.connection() as db:turn,base,_=entry._snapshot(db,tid)
        state,_=await private_lookup._call(entry,turn,base,'native')
        self.assertEqual(state,'received')
        self.reopen();self.chat.recover();await self.continue_turn(tid);self.show(tid,'main')
        self.assertEqual(len(self.providers.lookups),1)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM private_lookup_records").fetchone()[0],1)

    async def test_quota_waiver_continues_lookup_and_reply_until_visible(self):
        import asyncio
        from persona_runtime import call_limits
        self.consent(True);tid=await self.submit();self.show(tid,'search_notice')
        self.config.update({'call_limits':{'enabled':True,'hourly':1,'daily':0}})
        pending=asyncio.create_task(self.continue_turn(tid))
        try:
            async with asyncio.timeout(5):
                while True:
                    with self.store.connection() as db:waits=call_limits.pending(db)
                    if waits:break
                    await asyncio.sleep(.01)
            self.assertFalse(self.providers.lookups)
            call_limits.decide(self.store,[waits[0]['id']],True)
            await asyncio.wait_for(pending,8)
            self.show(tid,'main')
            with self.store.connection() as db:self.assertFalse(db.execute("SELECT 1 FROM model_call_waits").fetchone())
            self.assertEqual(len(self.providers.lookups),1)
        finally:
            if not pending.done():pending.cancel()

    async def test_one_repair_shared_between_notice_and_main(self):
        self.consent(True)
        self.providers.consume=False
        original=self.providers.reply
        async def output(aid,packet):
            self.providers.body='801。802。'
            self.providers.notice_body='8'*76 if not packet['repair_reason'] else '801。'
            return await original(aid,packet)
        self.providers.reply=output
        tid=await self.submit()
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        self.assertEqual(self.chat.get(tid)['error_code'],'private_reply_review_rejected')
        self.assertEqual([u['phase'] for u in self.out.list(self.pid,self.sid,tid)],['search_notice'])
        self.assertEqual(len(self.providers.bodies),3)

    async def test_approved_short_notice_may_contain_more_than_one_sentence(self):
        self.consent(True)
        self.providers.notice_body = '801。802。803。'
        tid = await self.submit()
        self.assertFalse(self.providers.lookups)
        self.assertEqual(self.show(tid, 'search_notice'), self.providers.notice_body)
        self.assertEqual(len(self.providers.bodies), 1)
        await self.continue_turn(tid)
        self.show(tid, 'main')
        self.assertEqual(len(self.providers.lookups), 1)

    async def test_notice_search_boundary_uses_actual_presentation(self):
        self.consent(True)
        tid=await self.submit()
        self.assertFalse(self.providers.lookups)
        self.assertNotIn('J7',[node for node,*_ in self.providers.calls])
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        self.assertEqual(len(self.providers.lookups),1)
        self.show(tid,'main')

    async def test_composer_span_idempotent_and_never_widens_to_private_prefix(self):
        self.consent(True)
        args=('desktop','13800000000 831','851',self.sid)
        result=await self.chat.submit(*args,persona_id=self.pid,lookup_span='831')
        await self.chat.wait()
        self.assertEqual((await self.chat.submit(*args,persona_id=self.pid,lookup_span='831'))['id'],result['id'])
        with self.assertRaisesRegex(RuntimeErrorBase,'submission_conflict'):
            await self.chat.submit(*args,persona_id=self.pid,lookup_span='13800000000')
        self.show(result['id'],'search_notice');await self.continue_turn(result['id']);self.show(result['id'],'main')
        self.assertEqual(self.providers.lookups[0][2]['question'],'831')

    async def test_echo_of_visible_notice_is_not_published_again(self):
        self.consent(True)
        self.providers.consume=False
        self.providers.body=self.providers.notice_body
        tid=await self.submit();self.show(tid,'search_notice');await self.continue_turn(tid)
        self.assertEqual(self.chat.get(tid)['error_code'],'private_reply_review_rejected')
        self.assertEqual([u['phase'] for u in self.out.list(self.pid,self.sid,tid)],['search_notice'])
        self.assertEqual(len(self.providers.lookups),1)

    async def test_legacy_composer_field_obeys_the_current_session_mode(self):
        task={'question':'13800000000','intent':'search','request_search':True}
        tid=(await self.chat.submit('desktop','13800000000',identifier(),self.sid,persona_id=self.pid,public_task=task))['id']
        await self.chat.wait()
        self.assertEqual(self.chat.get(tid)['error_code'],'private_lookup_confirmation_required')
        self.assertFalse(self.providers.lookups)
        self.consent(True)
        await self.continue_turn(tid);self.show(tid,'search_notice');await self.continue_turn(tid);self.show(tid,'main')
        self.assertEqual(self.providers.lookups[0][2]['question'],'13800000000')

    async def test_scope_changes_before_notice_do_not_dispatch(self):
        self.consent(True)
        tid=await self.submit('13800000000')
        self.consent(False)
        unit=self.out.list(self.pid,self.sid,tid)[0];plan=self.out.plan(self.pid,self.sid,unit['id'])
        offer=self.out.offer(self.pid,self.sid,unit['id'],plan['id'],plan['revision'],0,'881')
        self.assertEqual(offer['state'],'stopped')
        await self.continue_turn(tid)
        self.assertFalse(self.providers.lookups)

    async def test_requested_readable_page_gets_only_one_scoped_fetch(self):
        original=self.providers.retrieve
        async def tool(step,aid,args,model):
            result,usage=await original(step,aid,args,model)
            if step=='fetch':
                result['sources']=[{'url':args['url'],'title':'821','content':'822','kind':'page_content'}]
            return result,usage
        self.providers.retrieve=tool
        url='https://example.org/822'
        item=await self.chat.submit('desktop','822',identifier(),self.sid,persona_id=self.pid,
                    public_task={'question':'822','intent':'read_link','url':url,'retain_page':True})
        await self.chat.wait();self.show(item['id'],'search_notice')
        await self.continue_turn(item['id']);self.show(item['id'],'main')
        self.assertEqual([s for s,*_ in self.providers.lookups],['native','fetch'])
        self.assertTrue(self.providers.bodies[-1][1]['research']['requested_page']['readable'])
        data=private_lookup.details(self.store,self.pid,self.sid,item['id'],'research')
        self.assertIn('822',[s['content'] for s in data['sources']])

    async def test_post_search_publishes_first_legal_body_with_selected_evidence(self):
        self.consent(True)
        tid=await self.submit()
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        self.assertIn('903',self.show(tid,'main'))
        self.assertEqual(sum(n=='J5' for n,*_ in self.providers.calls),1)
        self.assertNotIn('J7',[node for node,*_ in self.providers.calls])
        self.assertEqual(len(self.providers.lookups),1)

    async def test_native_adapter_uses_approved_search_only_and_distinct_long_slots(self):
        import hashlib
        from unittest.mock import patch
        from persona_runtime.ordinary_providers import ReviewedPrivateChatProviders
        from persona_runtime.chat import ModelCompletion
        from persona_runtime.deepseek_native_contract import build_request
        nodes=('J1','J2.action','J2.task','J2.stance','J3','J4','J5','J6.mode','J6.coverage','J6.ending','J7')
        bundle={'protocol':'maibot-private-v3','scope':{'instance':self.config.load()['instance_id'],'personas':[self.pid]},
                'main':{'base_url':'https://api.deepseek.com','name':'deepseek-v4-pro'},
                'search':{'base_url':'https://api.deepseek.com','name':'deepseek-flash'},
                'jev':{'base_url':'https://example.invalid/v1','name':'jev-1.14'},
                'reply_system':'801','notice_system':'802','search_system':'803','long_system':'804',
                'nodes':{n:{'instructions':'805','criteria':{},'source_criterion':'806'} for n in nodes}}
        path=self.paths.home/'807.json';path.write_text(json.dumps(bundle),encoding='utf8')
        class Writer:
            async def complete(inner,model,messages):
                self.assertEqual(messages[0]['content'],'804')
                return '{"body":"808","detail":"809"}',{}
        provider=ReviewedPrivateChatProviders(self.config,path,hashlib.sha256(path.read_bytes()).hexdigest(),writer=Writer())
        model=self.config.load()['model']|{'name':'deepseek-flash'}
        query={'question':'13800000000'}
        async def native(inner,selected,messages):
            self.assertEqual(messages,[{'role':'system','content':'803'},{'role':'user','content':'13800000000'}])
            wire,_=build_request(selected,messages)
            self.assertEqual((wire['thinking']['type'],wire['reasoning_effort']),('enabled','high'))
            self.assertTrue(wire['tools'])
            packet={'outcome':'completed','assistant':{'server_tool_calls':[{'calls':[{'status':'completed',
                    'sources':[{'url':'https://example.org/91','snippet':'91','published_at':'2026-11-20'}]}]}]}}
            return ModelCompletion('811',{}, {}, native=packet)
        with patch('persona_runtime.deepseek_native_transport.DeepSeekNativeClient.complete',native):
            result,audit=await provider.retrieve('native','812',query,model)
        self.assertEqual((result['notes'],result['sources'][0]['summary'],result['tool_calls']),('811','91',1))
        self.assertEqual(audit['native']['outcome'],'completed')
        doc,_=await provider.reply('813',{'mode':'long_detail'})
        self.assertEqual(doc,{'body':'808','detail':'809'})

    async def test_malformed_long_slots_repair_before_exposure(self):
        self.providers.route='none';self.providers.long=True
        original=self.providers.reply
        async def writer(aid,packet):
            if not packet['repair_reason']:
                self.providers.bodies.append((aid,packet))
                return '813',{}
            return await original(aid,packet)
        self.providers.reply=writer
        item=await self.chat.submit('desktop','831',identifier(),self.sid,persona_id=self.pid,long_requested=True)
        await self.chat.wait()
        self.assertEqual(len(self.providers.bodies),2)
        self.show(item['id'],'main')
        self.assertEqual(private_lookup.details(self.store,self.pid,self.sid,item['id'],'long')['body'],'811。'*75)
