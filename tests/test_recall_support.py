"""Recall .0-.2 contracts, lifecycle, real canonical adapters and bounded search."""
import asyncio,json,math,random,sqlite3,time,unittest
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path
from persona_runtime import recall_policy as p
from persona_runtime.recall_support import RecallSupport
from persona_runtime.recall_sources import epoch
from persona_runtime.archive import scope,fingerprint
from persona_runtime.models import identifier,LOCAL_USER_ID
from tests import test_m4 as fixture

class Policy(unittest.TestCase):
    def test_half_life_and_independent_reinforcement(self):
        for kind in p.KINDS:
            half=p.HALF_LIFE_DAYS[kind]*86400
            self.assertAlmostEqual(p.priority(kind,.8,100,100+half),.4)
            self.assertEqual(p.priority(kind,.8,100,0),.8)
            value,anchor=p.reinforce(kind,.8,100,100+half,.2)
            self.assertAlmostEqual(value,.52);self.assertEqual(anchor,100+half)
            self.assertAlmostEqual(p.priority(kind,.8,100,p.due(kind,.8,100)),p.ACTIVE_THRESHOLD)

    def test_random_restart_equivalence_and_saturation(self):
        rng=random.Random(28);value=.65;anchor=0
        for _ in range(100):
            now=anchor+rng.randint(0,500000)
            value,anchor=p.reinforce('memory',value,anchor,now,.12)
            restored=json.loads(json.dumps([value,anchor]))
            self.assertAlmostEqual(p.priority('memory',*restored,now+86400),p.priority('memory',value,anchor,now+86400))
            self.assertTrue(0<=value<=1)

    def test_clock_and_bad_parameters(self):
        self.assertEqual(p.clock(100,90),(100,'clock_rollback'))
        self.assertEqual(p.clock(100,100+p.MAX_CLOCK_JUMP+1)[0],100)
        self.assertEqual(p.clock(100,100+p.MAX_CLOCK_JUMP+1,confirm_jump=True)[1],None)
        for bad in (float('nan'),float('inf'),True,'1'):
            with self.assertRaises(ValueError):p.finite(bad)

class Recall(unittest.IsolatedAsyncioTestCase):
    setUp=fixture.M4Tests.setUp
    asyncTearDown=fixture.M4Tests.asyncTearDown
    seed=fixture.M4Tests.seed

    def setup_recall(self):
        self.now=time.time()+1;self.recall=RecallSupport(self.store,clock=lambda:self.now)
        self.scope=scope(self.persona['id'],'desktop')

    async def memory(self,text='乌龙茶清香，下午泡一杯',*,source=None):
        if source is None:source=(await self.seed())[0]
        mid=identifier();stamp=source['created_at']
        with self.store.connection(write=True) as db:
            db.execute('INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (mid,self.persona['id'],LOCAL_USER_ID,'preference',text,source['user_event_id'],mid,None,1,'active',stamp,stamp))
        return mid,source

    async def register_memory(self,text='乌龙茶清香，下午泡一杯',source=None):
        mid,turn=await self.memory(text,source=source)
        return self.recall.register('memory',{'id':mid},self.scope),mid,turn

    def advance(self,days,confirm=True):
        self.now+=days*86400
        if confirm:self.recall.confirm_time()

    def raw_root(self,tid):
        with self.store.connection() as db:return dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())

    async def new_turn(self,text='再聊聊乌龙茶'):
        value=await self.chat.submit('desktop',text,identifier())
        await self.chat.wait()
        return self.chat.get(value['id'])

    def evidence(self,turn,unit):
        selection=self.recall.select(self.scope,'',root_id=turn['id'],required_ids=[unit])
        with self.store.connection(write=True) as db:
            row=db.execute('SELECT attempt_id,details_json FROM context_uses WHERE turn_id=? ORDER BY rowid DESC LIMIT 1',(turn['id'],)).fetchone()
            data=json.loads(row['details_json']);data['recall_support']={'selected_ids':[unit],'selection_id':selection['id']}
            db.execute('UPDATE context_uses SET details_json=? WHERE attempt_id=?',(json.dumps(data),row['attempt_id']))
            return {'attempt_id':row['attempt_id']}

    async def test_registration_is_not_consumption_and_restart_does_not_refresh(self):
        self.setup_recall();unit,mid,_=await self.register_memory()
        self.advance(3);before=self.recall.inspect(unit,self.scope)
        again=self.recall.register('memory',{'id':mid},self.scope)
        self.assertEqual(unit,again);self.assertEqual(before,self.recall.inspect(unit,self.scope))
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM recall_support_events').fetchone()[0],0)

    async def test_scope_and_cross_session_filter(self):
        self.setup_recall();unit,_,turn=await self.register_memory()
        other=scope(identifier(),'desktop')
        self.assertEqual(self.recall.inspect(unit,other)['state'],'permission_denied')
        result=self.recall.select(other,'乌龙茶',required_ids=[unit])
        self.assertFalse(result['selected']);self.assertIn('permission_denied',result['excluded'])
        self.assertFalse(self.recall.select(self.scope,'乌龙茶',session_id=identifier())['selected'])
        self.assertTrue(self.recall.select(self.scope,'乌龙茶',session_id=turn['session_id'])['selected'])

    async def test_withdrawal_and_expiry_override_high_priority(self):
        self.setup_recall();unit,mid,turn=await self.register_memory()
        with self.store.connection(write=True) as db:
            db.execute('INSERT INTO source_exclusions VALUES(?,?,?,?,?)',(turn['user_event_id'],self.persona['id'],LOCAL_USER_ID,'withdrawn',turn['created_at']))
        self.assertEqual(self.recall.inspect(unit,self.scope)['state'],'source_withdrawn')
        self.assertFalse(self.recall.select(self.scope,'乌龙茶',deep=True)['selected'])

    async def test_changed_memory_version_requires_new_unit_without_inheriting_heat(self):
        self.setup_recall();unit,mid,_=await self.register_memory()
        with self.store.connection(write=True) as db:db.execute('UPDATE memories SET text=? WHERE id=?',('现在喜欢白茶',mid))
        self.assertEqual(self.recall.inspect(unit,self.scope)['state'],'source_version_changed')
        new=self.recall.register('memory',{'id':mid},self.scope)
        self.assertNotEqual(new,unit);self.assertAlmostEqual(self.recall.inspect(new,self.scope)['priority'],.65)
        self.assertEqual([r['id'] for r in self.recall.select(self.scope,'白茶')['selected']],[new])

    async def test_cold_search_relevance_beats_hot_unrelated_material(self):
        self.setup_recall();cold,_,turn=await self.register_memory('乌龙茶 是我喜欢的饮品')
        self.advance(200)
        hot,_,_=await self.register_memory('红茶 是我喜欢的饮品',source=turn)
        result=self.recall.select(self.scope,'还记得我喜欢的乌龙茶吗',limit=2)
        self.assertEqual(result['selected'][0]['id'],cold)
        self.assertEqual(result['cold_status'],'found');self.assertEqual(result['deep_reason'],'explicit_history')
        self.assertEqual(self.recall.inspect(cold,self.scope)['layer'],'sleeping')

    async def test_active_candidate_generation_excludes_sleeping_pool(self):
        self.setup_recall();_,_,turn=await self.register_memory()
        for i in range(35):await self.register_memory('乌龙茶清香 旧事 '+str(i),source=turn)
        self.advance(200)
        for i in range(5):await self.register_memory('乌龙茶清香 新事 '+str(i),source=turn)
        result=self.recall.select(self.scope,'乌龙茶清香',limit=2,candidate_budget=8)
        self.assertEqual(result['searched_layers'],['active'])
        self.assertLessEqual(result['candidate_rows_materialized'],8)
        self.assertTrue(all(i['layer']=='active' for i in result['selected']))
        with self.store.connection() as db:
            self.assertGreater(db.execute('SELECT count(*) FROM recall_support_units').fetchone()[0],40)

    async def test_explicit_history_reserves_cold_budget_even_with_many_active_hits(self):
        self.setup_recall();cold,_,turn=await self.register_memory('乌龙茶 清香 秋天 九月')
        self.advance(200)
        for i in range(20):await self.register_memory('乌龙茶 当下 '+str(i),source=turn)
        result=self.recall.select(self.scope,'九月秋天乌龙茶',deep=True,limit=2,candidate_budget=8)
        self.assertIn(cold,[i['id'] for i in result['selected']]);self.assertLessEqual(result['candidate_rows_materialized'],8)

    async def test_required_cold_source_bypasses_priority_but_not_validity(self):
        self.setup_recall();unit,_,_=await self.register_memory();self.advance(200)
        result=self.recall.select(self.scope,'完全不相关',required_ids=[unit],limit=1)
        self.assertEqual(result['selected'][0]['id'],unit);self.assertTrue(result['selected'][0]['required'])

    async def test_same_root_upgrade_and_replay_do_not_double_reinforce(self):
        self.setup_recall();unit,_,turn=await self.register_memory('虚构偏好：喜欢乌龙茶')
        ref={'resolved_unit_id':unit,'user_event_id':turn['user_event_id'],'mention_quote':'乌龙茶'}
        first=self.recall.record(unit,turn['id'],self.scope,kind='user_mention',status='completed',occurred_at=epoch(turn['created_at']),evidence=ref)
        self.assertTrue(first['applied'])
        duplicate=self.recall.record(unit,turn['id'],self.scope,kind='user_mention',status='completed',occurred_at=epoch(turn['created_at']),evidence=ref)
        self.assertFalse(duplicate['applied'])
        context=self.recall.record(unit,turn['id'],self.scope,kind='context_adopted',status='completed',occurred_at=epoch(turn['created_at']),evidence=self.evidence(turn,unit))
        self.assertFalse(context['applied']);self.assertEqual(self.recall.inspect(unit,self.scope)['priority'],first['priority'])

    async def test_failed_cancelled_unknown_never_strengthen(self):
        self.setup_recall();unit,_,turn=await self.register_memory()
        for status in ('failed','cancelled','unknown'):
            result=self.recall.record(unit,turn['id'],self.scope,kind='context_adopted',status=status,occurred_at=epoch(turn['created_at']),evidence={})
            self.assertFalse(result['applied'])
        self.assertAlmostEqual(self.recall.inspect(unit,self.scope)['priority'],.65)

    async def test_success_requires_actual_completed_context_receipt(self):
        self.setup_recall();unit,_,turn=await self.register_memory()
        with self.assertRaisesRegex(Exception,'unconfirmed'):
            self.recall.record(unit,turn['id'],self.scope,kind='context_adopted',status='completed',occurred_at=epoch(turn['created_at']),evidence={})
        result=self.recall.record(unit,turn['id'],self.scope,kind='context_adopted',status='completed',occurred_at=epoch(turn['created_at']),evidence=self.evidence(turn,unit))
        self.assertTrue(result['applied'])

    async def test_unknown_then_success_requires_new_confirmed_receipt(self):
        self.setup_recall();unit,_,turn=await self.register_memory()
        self.recall.record(unit,turn['id'],self.scope,kind='context_adopted',status='unknown',occurred_at=epoch(turn['created_at']),evidence={})
        result=self.recall.record(unit,turn['id'],self.scope,kind='context_adopted',status='completed',occurred_at=epoch(turn['created_at']),evidence=self.evidence(turn,unit))
        self.assertTrue(result['applied'])
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM recall_support_events').fetchone()[0],1)

    async def test_late_events_match_chronological_replay(self):
        self.setup_recall();unit,mid,first=await self.register_memory('乌龙茶')
        second=await self.new_turn();self.now=time.time()+2
        self.recall.record(unit,second['id'],self.scope,kind='context_adopted',status='completed',occurred_at=epoch(second['created_at']),evidence=self.evidence(second,unit))
        self.recall.record(unit,first['id'],self.scope,kind='context_adopted',status='completed',occurred_at=epoch(first['created_at']),evidence=self.evidence(first,unit))
        with self.store.connection() as db:
            row=db.execute('SELECT * FROM recall_support_units WHERE id=?',(unit,)).fetchone()
            events=db.execute('SELECT * FROM recall_support_events ORDER BY effective_epoch,root_id').fetchall()
            value=.65;anchor=row['registered_epoch']
            for event in events:value,anchor=p.reinforce('memory',value,anchor,event['effective_epoch'],event['alpha'])
            self.assertAlmostEqual(row['anchor_priority'],value)

    async def test_sync_keyset_resumes_and_chunks_are_not_whole_document_reinforcement(self):
        self.setup_recall();mid,turn=await self.memory('乌龙茶 '*2700)
        first=self.recall.synchronize(self.persona['id'],'memory',limit=1)
        second=self.recall.synchronize(self.persona['id'],'memory',limit=1)
        self.assertFalse(first['completed']);self.assertNotEqual(first['ids'],second['ids'])
        while not second['completed']:second=self.recall.synchronize(self.persona['id'],'memory',limit=1)
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(DISTINCT lineage) FROM recall_support_units').fetchone()[0],3)

    async def test_native_source_reuse_withdrawal_and_correction(self):
        self.setup_recall();turn=(await self.seed())[0];key='knowledge.conversation.record.'+turn['id']
        source={'id':'public-a','title':'乌龙茶','url':'https://example.org/tea','summary':'旧做法','content':'水温九十度'}
        record={'persona_id':self.persona['id'],'channel':'desktop','session_id':turn['session_id'],'root_turn_id':turn['id'],'sources':[source],'organized':{}}
        with self.store.connection(write=True) as db:db.execute('INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps({'revision':0,'data':record})))
        unit=self.recall.register('native',{'key':key,'id':'public-a'},self.scope)
        self.assertIn('九十',self.recall.inspect(unit,self.scope)['text'])
        from persona_runtime.knowledge_native_memory import source_states
        with self.store.connection(write=True) as db:
            states={'public-a':{'state':'corrected','replacement':'更正：八十度','revision':1}}
            db.execute('INSERT INTO knowledge_control VALUES(?,?)',('knowledge.native.source_state.'+self.persona['id'],json.dumps({'revision':1,'data':states})))
        self.assertEqual(self.recall.inspect(unit,self.scope)['state'],'source_version_changed')
        new=self.recall.register('native',{'key':key,'id':'public-a'},self.scope)
        self.assertIn('八十',self.recall.inspect(new,self.scope)['text'])
        with self.store.connection(write=True) as db:
            states['public-a']['state']='withdrawn'
            db.execute('UPDATE knowledge_control SET value=? WHERE key=?',(json.dumps({'revision':2,'data':states}),'knowledge.native.source_state.'+self.persona['id']))
        self.assertFalse(self.recall.select(self.scope,'乌龙茶',deep=True)['selected'])

    async def test_dialogue_adapter_and_source_exclusion(self):
        self.setup_recall();turn=(await self.seed())[0]
        unit=self.recall.register('dialogue',{'id':turn['user_event_id']},self.scope)
        self.assertEqual(self.recall.inspect(unit,self.scope)['source_type'],'user_statement')
        result=self.recall.synchronize(self.persona['id'],'dialogue',limit=1)
        self.assertEqual(result['registered'],1)

    async def test_clock_jump_freezes_decay_until_explicit_confirmation(self):
        self.setup_recall();unit,_,_=await self.register_memory()
        self.advance(300,confirm=False);state=self.recall.inspect(unit,self.scope)
        self.assertEqual(state['layer'],'active');self.assertEqual(state['clock_anomaly'],'clock_forward_requires_confirmation')
        self.recall.confirm_time();self.assertEqual(self.recall.inspect(unit,self.scope)['layer'],'sleeping')
        self.now-=301*86400;self.assertEqual(self.recall.inspect(unit,self.scope)['clock_anomaly'],'clock_rollback')

    async def test_delete_canonical_memory_purges_derived_text_and_events(self):
        self.setup_recall();unit,mid,_=await self.register_memory()
        with self.store.connection(write=True) as db:db.execute('DELETE FROM memories WHERE id=?',(mid,))
        with self.store.connection() as db:
            self.assertFalse(db.execute('SELECT 1 FROM recall_support_units WHERE id=?',(unit,)).fetchone())
            self.assertFalse(db.execute('SELECT 1 FROM recall_support_terms WHERE unit_id=?',(unit,)).fetchone())

    async def test_source_change_before_consumption_rolls_back_event(self):
        self.setup_recall();unit,mid,turn=await self.register_memory()
        ref=self.evidence(turn,unit)
        with self.store.connection(write=True) as db:db.execute('UPDATE memories SET status=\'retracted\' WHERE id=?',(mid,))
        with self.assertRaisesRegex(Exception,'source_withdrawn'):
            self.recall.record(unit,turn['id'],self.scope,kind='context_adopted',status='completed',occurred_at=epoch(turn['created_at']),evidence=ref)
        with self.store.connection() as db:self.assertFalse(db.execute('SELECT 1 FROM recall_support_events').fetchone())

    async def test_metadata_audit_retains_hashes_not_source_prose(self):
        self.setup_recall();unit,_,_=await self.register_memory('私人测试文本 乌龙茶')
        result=self.recall.select(self.scope,'乌龙茶')
        with self.store.connection() as db:raw=db.execute('SELECT result_json FROM recall_support_searches WHERE id=?',(result['id'],)).fetchone()[0]
        self.assertNotIn('私人测试文本',raw);self.assertIn('text_hash',raw)

    async def test_expired_fact_is_distinct_from_cold_no_result(self):
        self.setup_recall();unit,mid,turn=await self.register_memory()
        expired=datetime.fromtimestamp(self.now-1,timezone.utc).isoformat()
        with self.store.connection(write=True) as db:
            db.execute('INSERT INTO memory_facts(memory_id,persona_id,user_id,scope_json,subject,attribute,valid_until,data_json) VALUES(?,?,?,?,?,?,?,?)',
                (mid,self.persona['id'],LOCAL_USER_ID,json.dumps(self.scope),'user','preference',expired,'{}'))
        self.assertEqual(self.recall.inspect(unit,self.scope)['state'],'source_expired')
        result=self.recall.select(self.scope,'乌龙茶',deep=True)
        self.assertEqual(result['cold_status'],'source_expired')

    async def test_native_multiple_locations_keep_scope_and_no_refresh(self):
        self.setup_recall();turn=(await self.seed())[0]
        source={'id':'public-shared','title':'乌龙茶','url':'https://example.org/a','content':'泡茶用水'}
        keys=['knowledge.conversation.record.'+turn['id'],'knowledge.native.imported.test']
        with self.store.connection(write=True) as db:
            for key in keys:
                record={'persona_id':self.persona['id'],'channel':'desktop','root_turn_id':turn['id'],'sources':[source]}
                db.execute('INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps({'revision':0,'data':record})))
        first=self.recall.register('native',{'key':keys[0],'id':source['id']},self.scope)
        self.advance(3)
        before=self.recall.inspect(first,self.scope)['priority']
        second=self.recall.register('native',{'key':keys[1],'id':source['id']},self.scope)
        self.assertEqual(first,second);self.assertEqual(before,self.recall.inspect(first,self.scope)['priority'])
        with self.store.connection(write=True) as db:db.execute('DELETE FROM knowledge_control WHERE key=?',(keys[0],))
        self.assertEqual(self.recall.inspect(first,self.scope)['state'],'available')
        self.assertTrue(self.recall.select(self.scope,'乌龙茶',session_id=turn['session_id'])['selected'])

    async def test_independent_interactions_each_reinforce(self):
        self.setup_recall();unit,_,first=await self.register_memory()
        second=await self.new_turn();self.now=time.time()+3
        before=self.recall.inspect(unit,self.scope)['priority']
        for turn in (first,second):
            self.recall.record(unit,turn['id'],self.scope,kind='context_adopted',status='completed',occurred_at=epoch(turn['created_at']),evidence=self.evidence(turn,unit))
        after=self.recall.inspect(unit,self.scope)['priority']
        self.assertGreater(after,before+.05)
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM recall_support_events WHERE alpha>0').fetchone()[0],2)

    async def test_context_packet_adapter_actual_model_input_and_success_feedback(self):
        self.setup_recall();unit,_,_=await self.register_memory('云雾乌龙茶是保留给周末的话题')
        from persona_runtime.recall_runtime import RecallRuntime
        runtime=RecallRuntime(self.store);runtime.update(self.persona['id'],True,0,identifier())
        from persona_runtime.recall_context import prepare,record_completed
        original=self.chat.cognitive.prepare
        captured=[]
        def candidate_prepare(turn_id,*args):
            turn=self.raw_root(turn_id)
            block,meta,audit=prepare(self.store,turn,'云雾乌龙茶',clock=lambda:self.now)
            prepared=original(turn_id,*args)
            prepared['messages'][0]['content']+='\n'+block
            prepared['details']['recall_support']=meta
            with self.store.connection() as db:
                manifest=self.chat.cognitive._manifest(db,turn,prepared['messages'],prepared['details'])
            prepared['manifest'].update(manifest)
            return prepared
        self.chat.cognitive.prepare=candidate_prepare
        old_complete=self.chat.client.complete
        async def model(model,messages):
            captured.append(messages)
            self.assertIn('云雾乌龙茶',str(messages))
            return await old_complete(model,messages)
        self.chat.client.complete=model
        turn=await self.new_turn('说说云雾乌龙茶')
        self.assertEqual(turn['status'],'completed',turn['error_code'])
        results=record_completed(self.store,turn['id'],clock=lambda:self.now)
        self.assertTrue(captured);self.assertTrue(any(r['applied'] for r in results))
        self.assertTrue(all(not r['applied'] for r in record_completed(self.store,turn['id'],clock=lambda:self.now)))

    async def test_policy_identity_conflict_is_atomic(self):
        self.setup_recall();unit,_,turn=await self.register_memory()
        ref=self.evidence(turn,unit)
        from unittest.mock import patch
        with patch.object(p,'MAX_REPLAY_EVENTS',0):
            with self.assertRaisesRegex(Exception,'policy_version_conflict'):
                self.recall.record(unit,turn['id'],self.scope,kind='context_adopted',status='completed',occurred_at=epoch(turn['created_at']),evidence=ref)
        with self.store.connection() as db:self.assertFalse(db.execute('SELECT 1 FROM recall_support_events').fetchone())

    async def test_forward_clock_anomaly_does_not_extend_fact_validity(self):
        self.setup_recall();unit,mid,_=await self.register_memory()
        until=datetime.fromtimestamp(self.now+86400,timezone.utc).isoformat()
        with self.store.connection(write=True) as db:
            db.execute('INSERT INTO memory_facts(memory_id,persona_id,user_id,scope_json,subject,attribute,valid_until,data_json) VALUES(?,?,?,?,?,?,?,?)',
                (mid,self.persona['id'],LOCAL_USER_ID,json.dumps(self.scope),'user','preference',until,'{}'))
        self.advance(100,confirm=False)
        self.assertEqual(self.recall.inspect(unit,self.scope)['state'],'source_expired')

    async def test_event_budget_rollback_keeps_unit_and_prior_ledger(self):
        self.setup_recall();unit,_,turn=await self.register_memory()
        ref=self.evidence(turn,unit)
        with self.store.connection(write=True) as db:
            registered=db.execute('SELECT registered_epoch FROM recall_support_units WHERE id=?',(unit,)).fetchone()[0]
            db.executemany('INSERT INTO recall_support_events VALUES(?,?,?,?,?,?,?,?,?)',
                [(identifier(),unit,'synthetic-history-'+str(i),'context_adopted','completed',registered,.12,1,'{}') for i in range(p.MAX_REPLAY_EVENTS)])
        with self.assertRaisesRegex(Exception,'replay_budget_exceeded'):
            self.recall.record(unit,turn['id'],self.scope,kind='context_adopted',status='completed',occurred_at=epoch(turn['created_at']),evidence=ref)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM recall_support_events').fetchone()[0],p.MAX_REPLAY_EVENTS)
            self.assertEqual(db.execute('SELECT anchor_priority FROM recall_support_units WHERE id=?',(unit,)).fetchone()[0],.65)

    async def test_query_budget_interruption_is_not_reported_as_no_result(self):
        self.setup_recall();await self.register_memory()
        from unittest.mock import patch
        with patch('persona_runtime.recall_support.time.monotonic',side_effect=[0]+[1000]*1000):
            # Force SQLite's first progress callback on a larger posting set.
            with self.store.connection(write=True) as db:
                unit=db.execute('SELECT id FROM recall_support_units').fetchone()[0]
                db.executemany('INSERT OR IGNORE INTO recall_support_terms VALUES(?,?)',[(str(i),unit) for i in range(2000)])
            with self.assertRaisesRegex(Exception,'query_budget_exceeded'):
                self.recall.select(self.scope,' '.join(str(i) for i in range(32)))

    async def test_historical_versions_do_not_exhaust_current_candidate_budget(self):
        self.setup_recall();old,mid,_=await self.register_memory('乌龙茶旧记录')
        for i in range(12):
            with self.store.connection(write=True) as db:db.execute('UPDATE memories SET text=? WHERE id=?',('乌龙茶第'+str(i)+'版',mid))
            current=self.recall.register('memory',{'id':mid},self.scope)
        result=self.recall.select(self.scope,'乌龙茶',limit=1,candidate_budget=2)
        self.assertEqual(result['selected'][0]['id'],current)
        self.assertEqual(result['candidate_rows_materialized'],1)
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM recall_support_units').fetchone()[0],13)

    async def test_declared_upgrade_checker_keeps_other_schema_and_row_guards(self):
        from scripts.pcar_upgrade_check import compare,FORMAT
        from persona_runtime.schema_recall_support import TABLES
        before={'format':FORMAT,'schema_version':27,'tables':{'events':{'original':'hash'}},'readable_records':{},'attempts_by_root':{}}
        after=dict(before,schema_version=28,tables=dict(before['tables'],**{t:{} for t in TABLES}))
        self.assertFalse(compare(before,after)['passed'])
        self.assertTrue(compare(before,after,allow_schema_upgrade=True)['passed'])
        bad=dict(after,schema_version=29)
        self.assertFalse(compare(before,bad,allow_schema_upgrade=True)['passed'])
        bad=dict(after,tables=dict(after['tables'],events={'original':'changed'}))
        self.assertFalse(compare(before,bad,allow_schema_upgrade=True)['passed'])

    async def test_stronger_evidence_replaces_contribution_instead_of_compounding(self):
        self.setup_recall();unit,_,_=await self.register_memory('乌龙茶清香。')
        async def model(model,messages):return '乌龙茶清香。',{'total_tokens':1}
        self.chat.client.complete=model
        turn=await self.new_turn('乌龙茶怎么样')
        ref=self.evidence(turn,unit);when=epoch(turn['created_at'])
        self.recall.record(unit,turn['id'],self.scope,kind='context_adopted',status='completed',occurred_at=when,evidence=ref)
        stronger=self.recall.record(unit,turn['id'],self.scope,kind='supported_recall',status='completed',occurred_at=when,evidence=ref|{'source_quote':'乌龙茶'})
        self.assertTrue(stronger['applied'])
        with self.store.connection() as db:
            row=db.execute('SELECT * FROM recall_support_units WHERE id=?',(unit,)).fetchone()
            events=db.execute('SELECT * FROM recall_support_events WHERE unit_id=?',(unit,)).fetchall()
            self.assertEqual(len(events),1);self.assertEqual(events[0]['alpha'],.24)
            self.assertAlmostEqual(row['anchor_priority'],.65+.24*(1-.65),places=6)
        with self.assertRaisesRegex(Exception,'output_evidence_required'):
            self.recall.record(unit,turn['id'],self.scope,kind='supported_recall',status='completed',occurred_at=when,evidence=ref|{'source_quote':'不存在的输出'})

    async def test_whitespace_only_fragment_does_not_end_source_sync(self):
        self.setup_recall();await self.memory('起始乌龙茶'+' '*9000+'尾部咖啡线索')
        for _ in range(10):
            result=self.recall.synchronize(self.persona['id'],'memory',limit=1)
            if result['completed']:break
        self.assertTrue(result['completed'])
        found=self.recall.select(self.scope,'咖啡',deep=True)
        self.assertTrue(any('尾部咖啡' in i['text'] for i in found['selected']))
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM recall_support_units').fetchone()[0],2)

    async def test_single_cjk_character_query_is_indexed(self):
        self.setup_recall();unit,_,_=await self.register_memory('周末喜欢喝茶')
        result=self.recall.select(self.scope,'茶')
        self.assertIn(unit,[i['id'] for i in result['selected']])

    async def test_derived_index_rebuild_does_not_refresh_attention(self):
        self.setup_recall();unit,_,_=await self.register_memory('周末喜欢喝茶')
        self.advance(3);before=self.recall.inspect(unit,self.scope)['priority']
        with self.store.connection(write=True) as db:db.execute('DELETE FROM recall_support_terms WHERE unit_id=?',(unit,))
        self.assertFalse(self.recall.select(self.scope,'茶')['selected'])
        result=self.recall.synchronize(self.persona['id'],'memory',restart=True,rebuild_index=True)
        while not result['completed']:result=self.recall.synchronize(self.persona['id'],'memory',rebuild_index=True)
        self.assertEqual(self.recall.inspect(unit,self.scope)['priority'],before)
        self.assertIn(unit,[i['id'] for i in self.recall.select(self.scope,'茶')['selected']])
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM recall_support_events').fetchone()[0],0)

    async def test_27_upgrade_backup_and_old_business_unchanged(self):
        self.setup_recall();_,turn=await self.memory()
        from persona_runtime.schema_recall_support import TABLES
        with closing(sqlite3.connect(self.store.path)) as db,db:
            before=db.execute('SELECT * FROM memories').fetchall()
            for trigger in ('recall_support_memory_delete','recall_support_event_delete','recall_support_native_delete'):db.execute('DROP TRIGGER '+trigger)
            for table in reversed(TABLES):db.execute('DROP TABLE '+table)
            db.execute('PRAGMA user_version=27')
        self.store.initialize()
        with self.store.connection() as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM memories')],before)
            from persona_runtime.storage import SCHEMA_VERSION
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],SCHEMA_VERSION)
        self.assertTrue(list((self.paths.home/'backups').glob('before-recall-support-*.sqlite3')))

if __name__=='__main__':unittest.main()
