"""Cold acquisition, source validation, committed reads and replay under the normal private chat route."""
import asyncio
import json
import unittest
from unittest.mock import patch

from persona_runtime import knowledge_study as study, local_knowledge as knowledge, private_lookup
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.private_reply_output import FACT_REVIEW_VERSION, SEMANTIC_REVIEW_VERSION, VERSION
from tests import test_private_reply_stage3 as fixture


class Study(unittest.IsolatedAsyncioTestCase):
    reopen=fixture.ResearchTests.reopen
    submit=fixture.ResearchTests.submit
    consent=fixture.ResearchTests.consent
    paint=fixture.ResearchTests.paint
    show=fixture.ResearchTests.show
    continue_turn=fixture.ResearchTests.continue_turn

    def setUp(self):
        fixture.ResearchTests.setUp(self)
        self.providers.study_enabled=True
        self.providers.local_first=True
        self.providers.semantic_lookup=False  # Historical extraction and delivery receipts.
        self.providers.grounded_search=True
        self.providers.output_contract_version=SEMANTIC_REVIEW_VERSION
        self.providers.route='retrieve'
        self.organizations=[]
        self.sources=[{'url':'https://example.org/701','title':'测试甲',
                       'summary':'701 测试甲 属性乙 903。 790','kind':'native_search_summary','date':'2026-01-01'}]
        self.extract=lambda s:[{'source_id':s[0]['id'],'subject':'测试甲','predicate':'属性乙',
                                'value':'903','quote':'测试甲 属性乙 903。','state':'fact'}]
        async def retrieve(step,aid,query,model):
            self.providers.lookups.append((step,aid,query))
            return {'notes':'SYNTHESIS_ONLY_823','sources':self.sources,'tool_calls':1},{}
        async def organize(aid,packet,model):
            self.organizations.append((aid,packet))
            return {'raw':json.dumps({'facts':self.extract(packet['sources'])},ensure_ascii=False)},{}
        self.providers.retrieve=retrieve
        self.providers.organize=organize

    async def cold(self,question='帮我查一下测试甲的属性乙'):
        tid=await self.submit(question)
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        return tid

    async def test_cold_commit_reread_warm_and_details(self):
        tid=await self.cold()
        self.assertIn('903',self.show(tid,'main'))
        packet=self.providers.bodies[-1][1]
        self.assertEqual(packet['research']['status'],'local_hit')
        self.assertNotIn('SYNTHESIS_ONLY',json.dumps(packet))
        self.assertNotIn('790',packet['research']['facts'][0]['text'])
        self.assertEqual(set(self.organizations[0][1]),{'question','as_of','sources'})
        self.assertNotIn('SYNTHESIS_ONLY',json.dumps(self.organizations[0][1]))
        with self.store.connection() as db:
            commit=db.execute('select * from knowledge_study_commits').fetchone()
            read=db.execute('select * from knowledge_study_reads').fetchone()
            self.assertEqual((commit['status'],read['outcome']),('committed','hit'))
            self.assertEqual(commit['fact_ids'],read['fact_ids'])
            self.assertEqual(db.execute('select outcome from local_knowledge_reads where turn_id=?',(tid,)).fetchone()[0],'miss')
        self.assertIn('903',private_lookup.details(self.store,self.pid,self.sid,tid,'research')['notes'])
        self.reopen()
        again=await self.submit('测试甲的属性乙是什么？')
        self.assertIn('903',self.show(again,'main'))
        self.assertEqual(len(self.organizations),1)
        self.assertEqual(len(self.providers.lookups),1)

    async def test_catalogue_selection_cold_warm_and_revocation(self):
        values=['第5章 705','第3章 703','第4章 704']
        self.sources[0]['summary']='。'.join(values)
        self.extract=lambda s:[dict(source_id=s[0]['id'],subject=s[0]['title'],predicate='章节目录',
                                    value=value,quote=value,state='fact') for value in values]
        question='帮我查一下测试甲的目录'
        first=await self.cold(question)
        self.show(first,'main')
        selected=self.providers.bodies[-1][1]['research']['facts']
        self.assertEqual([f['value'] for f in selected],[values[1],values[2],values[0]])
        self.reopen()
        second=await self.submit(question)
        self.assertEqual(self.providers.bodies[-1][1]['research']['facts'],selected)
        self.assertEqual((len(self.organizations),len(self.providers.lookups)),(1,1))
        unit=next(u for u in self.out.list(self.pid,self.sid,second) if u['phase']=='main')
        plan=self.out.plan(self.pid,self.sid,unit['id'])
        with self.store.connection(write=True) as db:knowledge.withdraw(db,selected[1]['id'])
        self.assertEqual(self.out.offer(self.pid,self.sid,unit['id'],plan['id'],plan['revision'],0,'902')['state'],'stopped')

    def test_outline_options_are_ordered_within_one_document(self):
        base=dict(subject='701',predicate='目录',qualifications=[],kind='fact')
        rows=[base|dict(id=f'{doc}-{n}',value=f'{n}. {700+n}',source_ids=[doc])
              for doc in ('a','b') for n in (10,3)]
        groups=study.selection_groups(rows,'701目录')
        self.assertEqual([[r['id'] for r in group] for group in groups.values()],
                         [['a-3','a-10'],['b-3','b-10'],[]])

    async def test_invalid_evidence_and_metadata_do_not_enter_reply(self):
        self.extract=lambda s:[{'source_id':s[0]['id'],'subject':'测试甲','predicate':'属性乙',
                                'value':'UNSUPPORTED','quote':'903','state':'fact'}]
        tid=await self.cold()
        self.show(tid,'main')
        self.assertFalse(self.providers.bodies[-1][1]['research']['facts'])
        self.assertIn('SYNTHESIS_ONLY',private_lookup.details(self.store,self.pid,self.sid,tid,'research')['notes'])
        with self.store.connection() as db:
            self.assertEqual(tuple(db.execute('select status,rejected from knowledge_study_commits').fetchone()),('invalid_evidence',1))
            self.assertEqual(db.execute('select count(*) from local_knowledge_documents').fetchone()[0],0)
        self.sources[0]['summary']=''
        tid=await self.cold()
        self.show(tid,'main')
        self.assertEqual(len(self.organizations),1)

    async def test_crash_after_organizer_receipt_then_after_commit(self):
        tid=await self.submit('帮我查一下测试甲属性乙');self.show(tid,'search_notice')
        with patch.object(study,'commit',side_effect=asyncio.CancelledError),self.assertRaises(asyncio.CancelledError):
            await self.continue_turn(tid)
        with self.store.connection() as db:
            self.assertFalse(db.execute('select 1 from knowledge_study_commits').fetchone())
            self.assertEqual(db.execute("select state from private_lookup_calls where step='organize'").fetchone()[0],'received')
        self.reopen();self.chat.recover()
        with patch('persona_runtime.knowledge_translation.readable_excerpt',side_effect=AssertionError('re-ranked sent material')), \
                patch.object(study,'reread',side_effect=asyncio.CancelledError),self.assertRaises(asyncio.CancelledError):
            await self.continue_turn(tid)
        self.reopen();self.chat.recover()
        await self.continue_turn(tid)
        self.assertIn('903',self.show(tid,'main'))
        self.assertEqual((len(self.organizations),len(self.providers.lookups)),(1,1))
        with self.store.connection() as db:
            self.assertEqual(db.execute('select count(*) from local_knowledge_facts').fetchone()[0],1)

    async def test_retired_j7_stays_absent_on_nonvector_research_and_chat(self):
        self.providers.output_contract_version=FACT_REVIEW_VERSION
        tid=await self.cold()
        self.assertIn('903',self.show(tid,'main'))
        self.providers.route='none'
        other=await self.submit('703')
        self.show(other,'main')
        self.assertNotIn('J7',[node for node,*_ in self.providers.calls])
        with self.store.connection() as db:
            reasons=[r[0] for r in db.execute('select reason from private_reply_reviews order by rowid')]
            self.assertEqual(reasons,['local_publication_ready']*3)

    async def test_insufficient_evidence_reaches_writer_without_j7_or_retry(self):
        self.providers.output_contract_version=FACT_REVIEW_VERSION
        self.extract=lambda s:[]
        tid=await self.cold()
        self.assertEqual(self.providers.bodies[-1][1]['research']['facts'],[])
        self.show(tid,'main')
        self.assertNotIn('J7',[node for node,*_ in self.providers.calls])
        self.assertEqual((len(self.providers.bodies),len(self.organizations),len(self.providers.lookups)),(2,1,1))

    def test_event_date_requires_the_same_event_in_one_clause(self):
        def extract(body):
            return [s[1] for s in study._dated_excerpt({'summary':body},'A21','发射',(4,24))]
        self.assertEqual(extract('2025年4月24日17时17分，搭载A21的火箭点火发射。'),['2025年4月24日'])
        self.assertEqual(extract('A21于2025年4月24日成功发射。'),['2025年4月24日'])
        for text in ('2025年4月24日。A21发射。','2025年4月24日，B20发射。A21在4月24日发射。',
                     '2025年4月24日，A21计划发射。','2025年4月24日，A21返回。','2025年4月23日，A21发射。',
                     '2025年4月24日，A210发射。','2025年4月24日，A21观看B20发射。'):
            self.assertEqual(extract(text),[])

    async def test_cached_partial_date_is_completed_from_the_original_acquisition(self):
        q='帮我查一下A21的发射日期'
        self.sources[0].update(title='A21',summary='A21于4月24日发射。')
        self.sources.append(self.sources[0]|{'url':'https://example.org/702','summary':'A21于2025年4月24日发射。'})
        def extracted(sources):
            first=next(s for s in sources if s['url']==self.sources[0]['url'])
            return [dict(source_id=first['id'],subject='A21',predicate='发射日期',quote=first['text'],value='4月24日',state='fact')]
        self.extract=extracted
        with patch.object(study,'_complete_date',side_effect=lambda db,turn,base,ident,**kw:ident):
            old=await self.cold(q)
            self.show(old,'main')
        self.assertEqual(self.providers.bodies[-1][1]['research']['facts'][0]['value'],'4月24日')
        self.reopen()
        for _ in range(2):
            tid=await self.submit(q)
            self.show(tid,'main')
            facts=self.providers.bodies[-1][1]['research']['facts']
            self.assertEqual([f['value'] for f in facts],['2025年4月24日'])
            self.assertEqual(facts[0]['url'],self.sources[1]['url'])
        self.assertEqual((len(self.organizations),len(self.providers.lookups)),(1,1))

    async def test_conflicting_years_do_not_supply_a_missing_year(self):
        self.sources[0].update(title='A21',summary='A21于4月24日发射。')
        for i,year in enumerate((2024,2025)):
            self.sources.append(self.sources[0]|{'url':f'https://example.org/{702+i}','summary':f'A21于{year}年4月24日发射。'})
        def extracted(sources):
            first=next(s for s in sources if s['url']==self.sources[0]['url'])
            return [dict(source_id=first['id'],subject='A21',predicate='发射日期',quote=first['text'],value='4月24日',state='fact')]
        self.extract=extracted
        tid=await self.cold('帮我查一下A21的发射日期')
        self.show(tid,'main')
        self.assertEqual(self.providers.bodies[-1][1]['research']['facts'][0]['value'],'4月24日')

    async def test_ambiguous_external_outcome_is_not_dispatched_again(self):
        original=self.providers.organize
        async def unknown(aid,packet,model):
            await original(aid,packet,model)
            raise TimeoutError()
        self.providers.organize=unknown
        tid=await self.cold()
        self.assertEqual(self.chat.get(tid)['error_code'],'knowledge_study_unknown')
        self.assertTrue(private_lookup.details(self.store,self.pid,self.sid,tid,'research')['sources'])
        self.reopen();self.chat.recover();await self.continue_turn(tid)
        self.assertEqual((len(self.organizations),len(self.providers.lookups)),(1,1))
        self.assertFalse([p for _,p in self.providers.bodies if p['phase']=='main'])

    async def test_before_send_failure_wakes_on_same_scope(self):
        from persona_runtime.chat import ModelError
        from persona_runtime.ingress import IngressService
        original=self.providers.organize
        async def offline(*args):
            raise ModelError('provider_network_error',diagnostics={'request_headers_sent':False})
        self.providers.organize=offline
        tid=await self.cold()
        self.assertEqual(self.chat.get(tid)['error_code'],'desktop_network_wait')
        self.providers.organize=original
        self.reopen();self.chat.recover()
        ingress=IngressService(self.chat);ingress.network_wake();ingress.schedule_committed()
        await self.chat.wait()
        self.assertIn('903',self.show(tid,'main'))
        self.assertEqual((len(self.organizations),len(self.providers.lookups)),(1,1))

    async def test_pending_notice_cannot_switch_to_another_study_contract(self):
        tid=await self.submit('帮我查一下测试甲属性乙')
        self.show(tid,'search_notice')
        self.providers.version='changed-test-bundle'
        await self.continue_turn(tid)
        self.assertEqual(self.chat.get(tid)['error_code'],'private_chat_review_version_changed')
        self.assertFalse(self.organizations)
        self.assertFalse(self.providers.lookups)

    async def test_omitted_uncertainty_in_excerpt_is_rejected(self):
        self.sources[0]['summary']='尚未确认测试甲 属性乙 903。'
        tid=await self.cold();self.show(tid,'main')
        self.assertFalse(self.providers.bodies[-1][1]['research']['facts'])
        with self.store.connection() as db:self.assertEqual(db.execute('select rejected from knowledge_study_commits').fetchone()[0],1)

    async def test_chinese_publication_date_is_distinct_from_verification_time(self):
        self.sources[0]['date']='2025年7月1日'
        tid=await self.cold();self.show(tid,'main')
        fact=self.providers.bodies[-1][1]['research']['facts'][0]
        self.assertEqual(fact['date'],'2025-07-01T00:00:00+00:00')
        self.assertGreater(fact['verified_at'],fact['date'])

    async def test_realistic_release_label_and_versioned_entity(self):
        self.sources[0].update(title='[RELEASE] Tool 3.13.0 final',summary='Tool 3.13.0 final released 2024年10月7日。')
        self.extract=lambda s:[{'source_id':s[0]['id'],'subject':s[0]['title'],'predicate':'正式版发布日期',
            'value':'2024年10月7日','quote':s[0]['text'],'state':'fact','aliases':['unattested']}]
        first=await self.cold('帮我查一下Tool 3.13.0是哪一天正式发布的？');self.show(first,'main')
        fact=self.providers.bodies[-1][1]['research']['facts'][0]
        self.assertEqual(fact['subject'],'Tool 3.13.0')
        with self.store.connection() as db:
            spec=json.loads(db.execute('select spec_json from local_knowledge_facts').fetchone()[0])
            self.assertNotIn('unattested',spec['aliases'])
        again=await self.submit('说回Tool 3.13.0，你刚才说它正式发布是哪一天来着？');self.show(again,'main')
        self.assertEqual(len(self.providers.lookups),1)
        self.assertEqual(self.providers.bodies[-1][1]['research']['facts'][0]['id'],fact['id'])

    async def test_adjacent_provenance_includes_subject_without_joining_disjoint_snippets(self):
        self.sources[0].update(title='701',summary='701 ... Subject A, an item from Group B.')
        self.extract=lambda s:[{'source_id':s[0]['id'],'subject':'Subject A','predicate':'origin',
            'value':'Group B','quote':'an item from Group B','state':'fact'}]
        first=await self.cold('帮我查一下Subject A的origin');self.show(first,'main')
        self.assertIn('Subject A',self.providers.bodies[-1][1]['research']['facts'][0]['text'])
        self.sources[0]['summary']='Subject A ... an item from Group B.'
        second=await self.cold('帮我查一下Subject A的origin，请联网核实');self.show(second,'main')
        with self.store.connection() as db:
            result=db.execute('select value from knowledge_control where key=?',('private.study.rejections.'+second,)).fetchone()
            self.assertEqual(json.loads(result[0])[0]['code'],'knowledge_study_ungrounded_subject')

    def test_calendar_equivalence_does_not_hide_disagreement(self):
        base={'subject':'701','predicate':'正式版发布日期','qualifications':[],'kind':'fact'}
        dates=[base|{'value':v} for v in ('10月7日','2024年10月7日','Monday, October 7, 2024','2024-10-07')]
        self.assertTrue(knowledge.compatible_dates(dates))
        self.assertFalse(knowledge.compatible_dates(dates+[base|{'value':'2025年10月7日'}]))
        self.assertFalse(knowledge.compatible_dates(dates+[base|{'value':'2024年10月8日'}]))
        launch=base|{'predicate':'发射时间'}
        times=[launch|{'value':v} for v in ('北京时间2025年4月24日17时17分','2025年4月24日17时17分','4月24日17点17分')]
        self.assertTrue(knowledge.compatible_dates(times))
        self.assertFalse(knowledge.compatible_dates(times+[launch|{'value':'2025年4月24日17时18分'}]))
        self.assertFalse(knowledge.compatible_dates(times+[launch|{'value':'2026年4月24日17时17分'}]))

    def test_launch_site_accepts_the_same_centres_named_subfacility(self):
        base={'subject':'701','predicate':'发射地点','qualifications':[],'kind':'fact'}
        sites=[base|{'value':v} for v in ('甲发射中心','甲发射中心载人航天发射场')]
        self.assertTrue(knowledge.compatible_launch_sites(sites))
        self.assertFalse(knowledge.compatible_launch_sites(sites+[base|{'value':'乙发射中心'}]))

    def test_upcoming_answer_uses_forward_evidence_and_a_traceable_source(self):
        q='下次更新 701'
        sources=[{'title':'ABC', 'summary':'701','url':'https://example.org/701'},
                 {'title':'XYZ', 'summary':'703', 'url':'https://example.org/703'}]
        self.assertEqual(study.forward_fetch_target(q,'下次更新 **ABC**',sources),'https://example.org/701')
        self.assertIsNone(study.forward_fetch_target(q,'下次更新 **ABC**',[sources[0]|{'summary':'ABC'}]))
        before={'kind':'fact','subject':'703','text':'已上线 703','predicate':'704'}
        after={'kind':'announced','subject':'706','text':'将于 2026-12-01 上线','predicate':'705'}
        self.assertEqual(study.selection_groups([before,after],q),{'fact/0':[after],'insufficient':[]})
        self.assertEqual(study.selection_groups([before],q),{'insufficient':[]})

    async def test_insufficient_reread_remains_bound_after_restart(self):
        from persona_runtime.ordinary_reply import OrdinaryReply
        self.extract=lambda sources:[]
        tid=await self.submit('帮我查一下测试甲属性乙');self.show(tid,'search_notice')
        with patch.object(OrdinaryReply,'publish',side_effect=asyncio.CancelledError),self.assertRaises(asyncio.CancelledError):
            await self.continue_turn(tid)
        self.reopen();self.chat.recover();await self.continue_turn(tid)
        self.show(tid,'main')
        self.assertFalse(self.providers.bodies[-1][1]['research']['facts'])
        self.assertEqual((len(self.providers.lookups),len(self.organizations)),(1,1))

    async def test_revocation_during_organization_prevents_commit(self):
        self.consent(True)
        original=self.providers.organize
        async def revoked(aid,packet,model):
            result=await original(aid,packet,model)
            self.consent(False)
            return result
        self.providers.organize=revoked
        tid=await self.cold()
        self.assertEqual(self.chat.get(tid)['error_code'],'private_lookup_scope_changed')
        with self.store.connection() as db:
            self.assertFalse(db.execute('select 1 from local_knowledge_documents').fetchone())
            self.assertFalse(db.execute('select 1 from knowledge_study_commits').fetchone())

    async def test_explicit_refresh_reads_new_receipt_and_reuses_document(self):
        first=await self.cold();self.show(first,'main')
        tid=await self.cold('帮我查一下测试甲属性乙，请联网核实')
        self.assertIn('903',self.show(tid,'main'))
        with self.store.connection() as db:
            self.assertEqual(db.execute('select count(*) from local_knowledge_documents').fetchone()[0],1)
            self.assertEqual(db.execute('select count(*) from knowledge_study_commits').fetchone()[0],2)
            self.assertEqual(db.execute('select outcome from knowledge_study_reads where turn_id=?',(tid,)).fetchone()[0],'hit')

    async def test_withdrawal_before_offer_blocks_publication(self):
        tid=await self.cold()
        unit=next(u for u in self.out.list(self.pid,self.sid,tid) if u['phase']=='main')
        plan=self.out.plan(self.pid,self.sid,unit['id'])
        with self.store.connection(write=True) as db:
            knowledge.withdraw(db,json.loads(db.execute('select fact_ids from knowledge_study_commits').fetchone()[0])[0])
        self.assertEqual(self.out.offer(self.pid,self.sid,unit['id'],plan['id'],plan['revision'],0,'902')['state'],'stopped')

    async def test_compound_query_reads_both_committed_facts_then_reuses(self):
        self.sources[0]['summary']='测试甲 属性乙 903。测试甲 属性丙 904。'
        self.extract=lambda s:[{'source_id':s[0]['id'],'subject':'测试甲','predicate':key,'value':v,
                                'quote':f'测试甲 {key} {v}。','state':'fact'}
                               for key,v in [('属性乙','903'),('属性丙','904')]]
        choose=self.providers.choose
        async def select(node,aid,state,options):
            if node=='J5' and 'first_pair' in options:return 'first_pair',{}
            return await choose(node,aid,state,options)
        self.providers.choose=select
        question='帮我查一下测试甲的属性乙和属性丙'
        first=await self.cold(question)
        self.show(first,'main')
        first_facts=self.providers.bodies[-1][1]['research']['facts']
        self.assertEqual([f['value'] for f in first_facts],['903','904'])
        again=await self.submit(question);self.show(again,'main')
        self.assertEqual(self.providers.bodies[-1][1]['research']['facts'],first_facts)
        followup=await self.submit('那它的属性丙呢？')
        self.assertIn('904',self.show(followup,'main'))
        self.assertEqual((len(self.organizations),len(self.providers.lookups)),(1,1))

    async def test_api_result_is_validated_atomically_and_cross_topic_returns_to_origin(self):
        initial=self.extract
        self.extract=lambda s:initial(s)+[initial(s)[0]|{'quote':'799'}]
        first=await self.cold()
        self.show(first,'main')
        self.sources=[{'url':'https://example.org/702','title':'测试丁','summary':'测试丁 属性戊 904。','kind':'native_search_summary'}]
        self.extract=lambda s:[{'source_id':s[0]['id'],'subject':'测试丁','predicate':'属性戊',
                                'value':'904','quote':'测试丁 属性戊 904。','state':'fact'}]
        other=await self.cold('帮我查一下测试丁属性戊');self.assertIn('904',self.show(other,'main'))
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT rejected FROM knowledge_study_commits WHERE turn_id=?',(first,)).fetchone()[0],1)
        back=await self.submit('再说回测试甲，我刚才问的属性乙叫什么？')
        self.assertIn('903',self.show(back,'main'))
        self.assertEqual((len(self.organizations),len(self.providers.lookups)),(2,2))

    async def test_confirmed_elliptical_query_carries_its_public_referent(self):
        first=await self.cold()
        self.show(first,'main')
        question='那它的属性丙呢？'
        self.sources[0]['summary']='测试甲 属性丙 904。'
        self.extract=lambda s:[dict(source_id=s[0]['id'],subject='测试甲',predicate='属性丙',
                                     value='904',quote='测试甲 属性丙 904。',state='fact')]
        tid=await self.submit(question)
        with self.store.connection() as db:it=private_lookup.intent(db,tid)
        self.assertEqual(it['state'],'waiting')
        private_lookup.confirm(self.store,self.pid,self.sid,tid,it['binding'],'701',question)
        await self.continue_turn(tid);self.show(tid,'search_notice')
        await self.continue_turn(tid)
        self.assertIn('904',self.show(tid,'main'))
        wire=self.providers.lookups[-1][2]
        self.assertEqual(wire['question'],'测试甲 属性丙呢？')
        self.assertNotIn('confirmed_span',wire)

    async def test_preexisting_physical_receipt_survives_writer_rejection(self):
        self.providers.output_contract_version=VERSION
        original=self.providers.reply
        async def too_long(aid,packet):
            if packet['phase']=='main':return '8'*76+'。',{}
            return await original(aid,packet)
        self.providers.reply=too_long
        first=await self.cold()
        self.assertEqual(self.chat.get(first)['error_code'],'private_reply_review_rejected')
        self.providers.reply=original
        next_turn=await self.submit('测试甲属性乙')
        self.assertIn('903',self.show(next_turn,'main'))
        self.assertEqual((len(self.organizations),len(self.providers.lookups)),(1,1))
