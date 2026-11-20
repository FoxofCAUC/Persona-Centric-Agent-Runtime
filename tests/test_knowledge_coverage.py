"""Structured source receipts through the desktop chat entry; all providers are local doubles."""
import json
import unittest
from unittest.mock import patch

from persona_runtime import knowledge_coverage as coverage, knowledge_study, local_knowledge
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.private_reply_output import FACT_REVIEW_VERSION
from tests import test_knowledge_study as study_tests


def at(field, text, occurrence=0):
    return dict(field=field, text=text, occurrence=occurrence)


def fact(source, value, index, quote, *, qualifiers=(), state='fact', predicate=None):
    return dict(source_id=source['id'], subject=at('title',source['title']), predicate=predicate or f'P{index}',
                value=value, quote=at('text',quote), state=state, supports=[index], qualifiers=list(qualifiers))


class Coverage(unittest.IsolatedAsyncioTestCase):
    reopen=study_tests.Study.reopen
    submit=study_tests.Study.submit
    show=study_tests.Study.show
    paint=study_tests.Study.paint
    continue_turn=study_tests.Study.continue_turn
    cold=study_tests.Study.cold

    def setUp(self):
        study_tests.Study.setUp(self)
        self.providers.structured_study=True
        self.providers.knowledge_revisions=True
        self.providers.output_contract_version=FACT_REVIEW_VERSION
        self.sources[0].update(content='A1 701。B2 702。C3 703。',kind='page_content',title='K44')
        self.needed=['A1','B2','C3']
        self.question='帮我查一下K44 A1 B2 C3'
        self.records=lambda s:[fact(s, str(701+i), i, text) for i,text in enumerate(s['text'].replace('。','。|').split('|')[:3])]
        async def organize(aid,packet,model):
            self.organizations.append((aid,packet))
            return {'raw':json.dumps({'requirements':self.needed,'facts':self.records(packet['sources'][0])},ensure_ascii=False)},{}
        self.providers.organize=organize

    async def test_three_slots_cold_then_reordered_warm_then_withdraw(self):
        first=await self.cold(self.question)
        self.show(first,'main')
        research=self.providers.bodies[-1][1]['research']
        self.assertEqual([f['value'] for f in research['facts']],['701','702','703'])
        with self.store.connection() as db:
            plan=coverage.saved_plan(db,first)
            self.assertEqual(plan['requirements'],self.needed)
            self.assertTrue(coverage.complete(plan,research['facts']))
        self.reopen()
        second=await self.submit('K44 C3 B2 A1？')
        selected=self.providers.bodies[-1][1]['research']['facts']
        self.assertEqual({f['value'] for f in selected},{'701','702','703'})
        self.assertEqual((len(self.organizations),len(self.providers.lookups)),(1,1))
        unit=next(u for u in self.out.list(self.pid,self.sid,second) if u['phase']=='main')
        plan=self.out.plan(self.pid,self.sid,unit['id'])
        with self.store.connection(write=True) as db:local_knowledge.withdraw(db,selected[-1]['id'])
        self.assertEqual(self.out.offer(self.pid,self.sid,unit['id'],plan['id'],plan['revision'],0,'902')['state'],'stopped')

    async def test_missing_slot_and_bad_location_keep_partial_facts_out_of_reply(self):
        self.records=lambda s:[fact(s,'701',0,'A1 701。'),fact(s,'703',2,'C3 703。')|{'subject':at('text','A1')}]
        first=await self.cold(self.question)
        self.show(first,'main')
        self.assertFalse(self.providers.bodies[-1][1]['research']['facts'])
        with self.store.connection() as db:
            self.assertEqual(tuple(db.execute('SELECT status,rejected FROM knowledge_study_commits').fetchone()),('committed',1))
            self.assertEqual(db.execute('SELECT outcome FROM knowledge_study_reads').fetchone()[0],'insufficient')
        self.assertFalse(any(node=='J5' for node,_,_ in self.providers.calls))

    async def test_typed_uncertainty_and_attribution_are_not_query_requirements(self):
        self.needed=['Q9'];self.sources[0]['content']='K44 P8 据L45推算，或为705。'
        self.records=lambda s:[fact(s,'705',0,s['text'],state='rumor',qualifiers=[
            dict(kind='attribution',at=at('text','L45')),dict(kind='certainty',at=at('text','或为'))])]
        tid=await self.cold('帮我查一下K44 Q9')
        self.show(tid,'main')
        chosen=self.providers.bodies[-1][1]['research']['facts'][0]
        self.assertEqual(chosen['qualifications'],[])
        self.assertEqual((chosen['kind'],chosen['value']),('rumor','705'))
        for q in chosen['witnesses']['qualifiers']:
            start,end=q['span']
            self.assertEqual(self.sources[0]['content'][start:end],q['text'])

    async def test_selection_can_drop_wrong_event_and_resume_after_commit(self):
        import asyncio
        self.needed=['A1']
        self.records=lambda s:[fact(s,'701',0,'A1 701。',predicate='P1'),fact(s,'702',0,'B2 702。',predicate='P2')]
        saved=self.providers.choose
        async def choose(node,aid,state,options):
            if node=='J5' and 'drop/0' in options:return 'drop/0',{}
            return await saved(node,aid,state,options)
        self.providers.choose=choose
        tid=await self.submit('帮我查一下K44 A1');self.show(tid,'search_notice')
        with patch.object(knowledge_study,'reread',side_effect=asyncio.CancelledError),self.assertRaises(asyncio.CancelledError):
            await self.continue_turn(tid)
        self.reopen();self.chat.recover()
        await self.continue_turn(tid);self.show(tid,'main')
        self.assertEqual([f['value'] for f in self.providers.bodies[-1][1]['research']['facts']],['702'])
        self.assertEqual(len(self.organizations),1)

    async def test_shared_attribute_does_not_merge_distinct_claims(self):
        self.needed=['A1']
        self.records=lambda s:[fact(s,'701',0,'A1 701。',predicate='A1-x'),fact(s,'702',0,'B2 702。',predicate='A1')]
        saved=self.providers.choose
        seen=[]
        async def choose(node,aid,state,options):
            if node=='J5':
                seen.append({f['value'] for f in state['facts'].values()})
                for key,f in state['facts'].items():
                    if f['value']=='701':return 'drop/'+key,{}
            return await saved(node,aid,state,options)
        self.providers.choose=choose
        tid=await self.cold('帮我查一下K44 A1')
        self.show(tid,'main')
        self.assertEqual(seen,[{'701','702'},{'702'}])

    async def test_exact_repeat_cannot_complete_after_one_requirement_is_withdrawn(self):
        tid=await self.cold(self.question);self.show(tid,'main')
        selected=self.providers.bodies[-1][1]['research']['facts']
        with self.store.connection(write=True) as db:local_knowledge.withdraw(db,selected[0]['id'])
        again=await self.submit(self.question)
        self.show(again,'search_notice')
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT outcome FROM local_knowledge_reads WHERE turn_id=?',(again,)).fetchone()[0],'insufficient')

    async def test_same_scope_contradiction_rejects_both_receipts(self):
        self.needed=['A1']
        self.records=lambda s:[fact(s,'701',0,'A1 701。'),fact(s,'702',0,'B2 702。')]
        tid=await self.cold('帮我查一下K44 A1');self.show(tid,'main')
        self.assertFalse(self.providers.bodies[-1][1]['research']['facts'])

    async def test_crop_cannot_hide_the_preceding_negation(self):
        self.needed=['A1']
        self.sources[0]['content']='K44 A1 不是701。'
        self.records=lambda s:[fact(s,'701',0,'701。')]
        tid=await self.cold('帮我查一下K44 A1');self.show(tid,'main')
        self.assertFalse(self.providers.bodies[-1][1]['research']['facts'])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT rejected FROM knowledge_study_commits').fetchone()[0],1)

    async def test_literal_shorthand_is_resolved_against_the_offered_excerpt(self):
        self.needed=['A1']
        self.sources[0].update(content='第1项。11月3日 K44 A1 为701，B1为702。')
        def records(s):
            row=fact(s,'701',0,'11月3日 K44 A1 为701。',state='observed')
            return [row|{'subject':'K44','quote':'11月3日 K44 A1 为701','qualifiers':[
                {'kind':'occurrence_date','at':at('text','11月3日')}]}]
        self.records=records
        tid=await self.cold('帮我查一下K44 A1');self.show(tid,'main')
        with self.store.connection() as db:
            spec=json.loads(db.execute('SELECT spec_json FROM local_knowledge_facts').fetchone()[0])
        self.assertEqual(spec['quote'],'11月3日 K44 A1 为701，B1为702。')
        self.assertIsNotNone(spec['expires_at'])
        start,end=spec['witnesses']['subject']['span']
        self.assertEqual(self.sources[0]['content'][start:end],'K44')

    def test_shorthand_fails_closed_on_ambiguous_or_absent_text(self):
        source={'title':'X X','text':'K1。K1。X X。','start':25}
        for value in ('K1。','absent'):
            with self.assertRaises(RuntimeErrorBase):coverage.quote_location(source,value)
        quote=coverage.quote_location(source,'X X。')
        with self.assertRaises(RuntimeErrorBase):coverage.subject_location(source,'X',quote)

    async def test_completed_event_date_is_durable_but_plan_is_not_an_observation(self):
        self.needed=['A1']
        self.sources[0]['content']='K44 2001-01-02 701。'
        def records(s):
            date=[dict(kind='occurrence_date',at=at('text','2001-01-02'))]
            return [fact(s,'701',0,s['text'],state=state,qualifiers=date) for state in ('announced','observed')]
        self.records=records
        tid=await self.cold('帮我查一下K44 A1');self.show(tid,'main')
        with self.store.connection() as db:
            spec=json.loads(db.execute('SELECT spec_json FROM local_knowledge_facts').fetchone()[0])
            self.assertEqual(spec['state'],'observed')
            self.assertIsNone(spec['expires_at'])
            self.assertEqual(db.execute('SELECT rejected FROM knowledge_study_commits').fetchone()[0],1)

    def test_malformed_question_parts_and_unoffered_occurrences(self):
        for req in ([],['wrong'],['A1','A1'],[False]):
            with self.assertRaises(RuntimeErrorBase):coverage.requirements({'requirements':req,'facts':[]},'A1')
        self.assertEqual(coverage.witness('AB AB','AB',1),(3,5))
        for occurrence in (True,2,-1):
            with self.assertRaises(RuntimeErrorBase):coverage.witness('AB AB','AB',occurrence)

    def test_split_question_anchors_use_only_distinguishing_original_words(self):
        raw={'requirements':['甲册的装订方式','甲册的页数'],'facts':[]}
        parts,_=coverage.requirements(raw,'甲册的装订方式和页数？')
        self.assertEqual(parts,['甲册的装订方式','页数'])
        with self.assertRaises(RuntimeErrorBase):
            coverage.requirements(dict(raw,requirements=['甲册的价格']), '甲册的装订方式和页数？')
        self.assertEqual(coverage.aligned_runs('ABX ABY','ABY'),[])

    def test_contracted_value_can_restore_only_an_unambiguous_literal_passage(self):
        self.assertEqual(coverage.literal_value('甲乙与丙丁','甲乙丙丁'),'甲乙与丙丁')
        for quote in ('甲乙。丙丁','甲乙可能是丙丁','甲乙甲乙与丙丁','甲乙和戊己'):
            with self.assertRaises(RuntimeErrorBase):coverage.literal_value(quote,'甲乙丙丁')

    def test_parent_is_explicit_and_siblings_conflict(self):
        def place(value, parent=None):
            return dict(subject='S',predicate='P',kind='observed',qualifications=[],value=value,
                        witnesses={'qualifiers':[{'kind':'location_parent','text':parent}] if parent else []})
        self.assertTrue(coverage.compatible_places([place('H'),place('East', 'H')]))
        self.assertFalse(coverage.compatible_places([place('East', 'H'),place('West', 'H')]))
        self.assertFalse(coverage.compatible_places([place('H'),place('H-West')]))

    async def test_reviewed_premise_correction_requires_all_independent_facts(self):
        self.providers.conditional_coverage=True
        async def organize(aid,packet,model):
            s=packet['sources'][0]
            items=[fact(s,'701',0,'A1 701。'),fact(s,'703',2,'C3 703。')]
            return {'raw':json.dumps({'requirements':self.needed,'dependencies':[{'item':1,'premise':0}],
                                      'facts':items})},{}
        self.providers.organize=organize
        saved=self.providers.choose
        async def choose(node,aid,state,options):
            if node=='J5':
                self.assertNotIn('complete',options)
                self.assertIn('correct/0',options)
                self.assertEqual(state['dependencies'],[{'item':1,'premise':0}])
                return 'correct/0',{}
            return await saved(node,aid,state,options)
        self.providers.choose=choose
        tid=await self.cold(self.question)
        self.show(tid,'main')
        selected=self.providers.bodies[-1][1]['research']['facts']
        self.assertEqual([f['value'] for f in selected],['701','703'])
        with self.store.connection() as db:
            plan=coverage.saved_plan(db,tid)
            self.assertFalse(coverage.corrections(plan,selected[:1]))
            self.assertFalse(coverage.corrections(dict(plan,dependencies=[]),selected))

    def test_dependency_edges_only_bind_distinct_ordered_requirements(self):
        for edges in ([{'item':0,'premise':1}],[{'item':1,'premise':1}],
                      [{'item':1,'premise':False}],[{'item':2,'premise':0},{'item':2,'premise':1}]):
            with self.assertRaises(RuntimeErrorBase):coverage.premise_dependencies(edges,['A1','B2','C3'])

    async def test_complete_explanation_is_checked_before_a_shorter_premise_correction(self):
        self.providers.conditional_coverage=self.providers.relation_judge=True
        self.needed=['A1','B2']
        self.records=lambda s:[fact(s,'701',0,'A1 701。'),fact(s,'702',1,'B2 702。')]
        saved=self.providers.choose
        checks=[]
        async def choose(node,aid,state,options):
            if node=='J5.relation':return 'premise/0',{}
            if node=='J5':
                checks.append(tuple(options))
                if 'complete' in options:
                    self.assertNotIn('correct/0',options)
                    return 'insufficient',{}
                return 'correct/0',{}
            return await saved(node,aid,state,options)
        self.providers.choose=choose
        tid=await self.cold('帮我查一下K44 A1 B2')
        self.show(tid,'main')
        self.assertEqual(len(checks),2)
        self.assertEqual([f['value'] for f in self.providers.bodies[-1][1]['research']['facts']],['701'])
        with self.store.connection() as db:
            chosen=db.execute("""SELECT n.node FROM knowledge_study_reads r JOIN private_reply_nodes n
                ON n.attempt_id=r.selection_id WHERE r.turn_id=?""",(tid,)).fetchone()[0]
            self.assertIn('.correction.',chosen)

    async def test_compact_candidate_needs_an_independent_factual_acceptance(self):
        self.providers.conditional_coverage=self.providers.relation_judge=True
        self.needed=['A1','B2']
        self.records=lambda s:[fact(s,'701',0,'A1 701。',predicate='P0'),fact(s,'702',1,'B2 702。'),
                               fact(s,'703',0,'C3 703。',predicate='P3')]
        saved=self.providers.choose
        checked=[]
        async def choices(node,aid,state,options):
            if node=='J5.relation':return 'independent',{}
            if node=='J5':
                checked.append(len(state['facts']))
                return ('insufficient' if len(checked)==1 else 'complete'),{}
            return await saved(node,aid,state,options)
        self.providers.choose=choices
        tid=await self.cold('帮我查一下K44 A1 B2');self.show(tid,'main')
        self.assertEqual(checked,[2,3])
        self.assertEqual(len(self.providers.bodies[-1][1]['research']['facts']),3)

    async def test_omitted_relation_gets_a_separate_choice_and_is_reused_on_exact_repeat(self):
        self.needed=['A1','B2']
        self.providers.conditional_coverage=self.providers.relation_judge=True
        self.records=lambda s:[fact(s,'701',0,'A1 701。')]
        saved=self.providers.choose
        relations=[]
        async def choose(node,aid,state,options):
            if node=='J5.relation':
                self.assertEqual(set(state),{'question','requirements','item'})
                self.assertEqual(tuple(options),('independent','premise/0'))
                relations.append(aid)
                return 'premise/0',{}
            if node=='J5':
                self.assertEqual(state['dependencies'],[{'item':1,'premise':0}])
                return 'correct/0',{}
            return await saved(node,aid,state,options)
        self.providers.choose=choose
        question='帮我查一下K44 A1 B2'
        tid=await self.cold(question);self.show(tid,'main')
        self.reopen();second=await self.submit(question);self.show(second,'main')
        self.assertEqual((len(relations),len(self.providers.lookups)),(1,1))
        with self.store.connection() as db:
            self.assertEqual(coverage.saved_plan(db,tid)['dependencies'],[{'item':1,'premise':0}])

    async def test_adjacent_source_date_is_context_only(self):
        self.needed=['A1']
        self.sources[0]['content']='K44 2001-02-03。K44 A1 701。'
        self.records=lambda s:[fact(s,'701',0,'K44 A1 701。',state='observed',qualifiers=[
            dict(kind='occurrence_date',at=at('text','2001-02-03'))])]
        tid=await self.cold('帮我查一下K44 A1');self.show(tid,'main')
        with self.store.connection() as db:
            spec=json.loads(db.execute('SELECT spec_json FROM local_knowledge_facts').fetchone()[0])
        self.assertEqual(spec['quote'],self.sources[0]['content'])
        self.assertTrue(spec['witnesses']['qualifiers'][0]['context_only'])
        self.assertIsNotNone(spec['expires_at'])

    async def test_extra_pointer_is_accepted_only_when_it_repeats_a_verified_field(self):
        self.needed=['A1']
        def records(s):
            row=fact(s,'701',0,'A1 701。')|{'quote':'A1 701。','subject':'K44'}
            return [row|{'position':at('text','A1 701。')},row|{'pointer':at('text','C3 703。')},
                    row|{'polarity':False}]
        self.records=records
        tid=await self.cold('帮我查一下K44 A1');self.show(tid,'main')
        with self.store.connection() as db:
            self.assertEqual(tuple(db.execute('SELECT rejected,status FROM knowledge_study_commits').fetchone()),(2,'committed'))

    async def test_relative_clock_does_not_displace_a_dated_observation(self):
        self.needed=['A1']
        self.sources[0]['content']='K44 A1 2002年3月4日15时12分。K44 A1 今天15时12分。'
        self.records=lambda s:[fact(s,x,0,'K44 A1 '+x+'。',predicate='日期时间',state='observed')
                               for x in ('2002年3月4日15时12分','今天15时12分')]
        tid=await self.cold('帮我查一下K44 A1');self.show(tid,'main')
        self.assertEqual([f['value'] for f in self.providers.bodies[-1][1]['research']['facts']],['2002年3月4日15时12分'])

    async def test_same_clock_cannot_merge_two_explicitly_different_days(self):
        self.needed=['A1']
        values=('2002年3月4日15时12分','2002年3月5日15时12分','今天15时12分')
        self.sources[0]['content']=''.join('K44 A1 '+x+'。' for x in values)
        self.records=lambda s:[fact(s,x,0,'K44 A1 '+x+'。',predicate='日期时间',state='observed') for x in values]
        tid=await self.cold('帮我查一下K44 A1');self.show(tid,'main')
        self.assertFalse(self.providers.bodies[-1][1]['research']['facts'])

    async def test_confirmed_http_failure_preserves_the_successful_search_sources(self):
        from persona_runtime.chat import ModelError
        self.sources[0]['summary']=self.sources[0].pop('content')
        self.sources[0]['kind']='native_search_summary'
        search=self.providers.retrieve
        async def retrieve(step,aid,query,model):
            if step=='fetch':
                raise ModelError('http_error',diagnostics={'http_status':404,'read_only_tool':True,'request_headers_sent':True})
            return await search(step,aid,query,model)
        self.providers.retrieve=retrieve
        tid=await self.cold(self.question);self.show(tid,'main')
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT state FROM private_lookup_calls WHERE turn_id=? AND step='fetch'",(tid,)).fetchone()[0],'failed')
        self.assertEqual(len(self.providers.bodies[-1][1]['research']['facts']),3)

    async def test_cold_and_warm_give_the_same_order_for_the_same_eligible_facts(self):
        self.providers.conditional_coverage=self.providers.relation_judge=True
        saved=self.providers.choose
        async def choose(node,aid,state,options):
            if node=='J5.relation':return 'independent',{}
            return await saved(node,aid,state,options)
        self.providers.choose=choose
        # A source's extraction order is independent of query relevance.
        records=self.records
        self.records=lambda s:list(reversed(records(s)))
        first=await self.cold(self.question);self.show(first,'main')
        cold=self.providers.bodies[-1][1]['research']['facts']
        self.reopen();second=await self.submit(self.question);self.show(second,'main')
        self.assertEqual([f['id'] for f in cold],[f['id'] for f in self.providers.bodies[-1][1]['research']['facts']])
        self.assertEqual(len(self.organizations),1)

    async def test_terminal_length_reuses_frozen_sources_and_second_receipt_after_restart(self):
        import asyncio
        from persona_runtime.chat import ModelError
        original=self.providers.organize
        calls=[]
        async def organize(aid, packet, model):
            calls.append((packet,model.get('_study_length_retry',False)))
            if len(calls)==1:
                raise ModelError('provider_output_truncated',usage={'completion_tokens':1024},
                                 diagnostics={'finish_reason':'length','response_headers_received':True})
            return await original(aid,packet,model)
        self.providers.organize=organize
        tid=await self.submit(self.question);self.show(tid,'search_notice')
        with patch.object(knowledge_study,'commit',side_effect=asyncio.CancelledError),self.assertRaises(asyncio.CancelledError):
            await self.continue_turn(tid)
        self.reopen();self.chat.recover()
        await self.continue_turn(tid);self.show(tid,'main')
        self.assertEqual(calls,[(calls[0][0],False),(calls[0][0],True)])
        self.assertEqual(len(self.providers.lookups),1)
        with self.store.connection() as db:
            steps=dict(db.execute('SELECT step,state FROM private_lookup_calls WHERE turn_id=?',(tid,)).fetchall())
            self.assertEqual(steps,{'native':'received','organize':'failed','organize_retry':'received'})
            self.assertEqual(db.execute("SELECT c.attempt_id=a.attempt_id FROM knowledge_study_commits c JOIN private_lookup_calls a ON a.turn_id=c.turn_id AND a.step='organize_retry'").fetchone()[0],1)

    async def test_unknown_delivery_and_other_terminal_reasons_do_not_resend(self):
        from persona_runtime.chat import ModelError
        for diagnostics in ({'request_headers_sent':True},{'finish_reason':'content_filter','response_headers_received':True}):
            async def organize(aid,packet,model):
                raise ModelError('provider_response_missing',diagnostics=diagnostics)
            self.providers.organize=organize
            tid=await self.cold(self.question)
            with self.store.connection() as db:
                self.assertFalse(db.execute("SELECT 1 FROM private_lookup_calls WHERE turn_id=? AND step='organize_retry'",(tid,)).fetchone())
