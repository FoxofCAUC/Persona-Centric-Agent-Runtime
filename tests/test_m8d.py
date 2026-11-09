"""Artificial staged memory tests; never formal sources or paid providers."""
import json
import unittest
from tests import test_m4 as fixtures
from persona_runtime.models import identifier


class Model:
    def __init__(self):self.calls=[];self.review_action=None
    async def complete(self,model,messages):
        self.calls.append(messages);p=json.loads(messages[-1]['content'])
        if 'proposals' not in p:
            users=[e for e in p['events'] if e['role']=='user'];summary=[];candidates=[]
            for n,e in enumerate(users):
                source=[{'source_id':e['id'],'quote':e['text']}]
                summary.append({'id':'s'+str(n),'text':e['text'],'sources':source})
                if p['purpose']!='repair':candidates.append({'id':'c'+str(n),'category':'preference','text':e['text'],'subject':'用户','attribute':'pref-'+e['id'],'sources':source})
            return json.dumps({'format_version':3,'summary':summary,'candidates':candidates},ensure_ascii=False),{'total_tokens':2}
        decisions=[]
        for kind in ('summary','candidates'):
            for i in p['proposals'][kind]:
                events={e['id']:e['text'] for e in p['events']}
                sources=[{'source_id':s['source_id'],'quote':events[s['source_id']][s['start']:s['end']]} for s in i['sources']]
                d={'id':i['id'],'action':'summary' if kind=='summary' else 'publish','reason':'人工复核通过','subject':i.get('subject','用户'),'attribute':i.get('attribute','summary'),'sources':sources,'target_memory_id':None,'effective_at':None,'valid_until':None}
                if self.review_action:self.review_action(kind,i,d,p)
                decisions.append(d)
        return json.dumps({'format_version':3,'decisions':decisions},ensure_ascii=False),{'total_tokens':3}


class MemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.M4Tests.setUp(self)
        with self.store.connection(write=True) as db:db.execute('INSERT OR IGNORE INTO memory_defaults VALUES(?)',(self.persona['id'],))
    asyncTearDown=fixtures.M4Tests.asyncTearDown
    seed=fixtures.M4Tests.seed

    def enable(self):
        p=self.org.managed.current_policy()
        return self.org.managed.update_policy(self.persona['id'],dict(p['data'],memory_automation=True),p['id'],self.persona['id'],identifier())

    def install_model(self):
        m=Model();self.org.client=m;return m

    async def finish(self,limit=20):
        for _ in range(limit):
            self.org.automatic.tick(force=True)
            if not await self.org.run_next():break

    async def test_new_persona_default_off_and_no_dispatch(self):
        await self.seed();m=self.install_model();await self.finish()
        self.assertEqual(m.calls,[]);self.assertFalse(self.org.automatic.coverage()['enabled'])

    async def test_eleven_sources_two_stages_and_automatic_continuation(self):
        await self.seed(11);m=self.install_model();self.enable();await self.finish()
        self.assertEqual(len(m.calls),4)
        self.assertEqual(len(self.service.search_memories()),11)
        status=self.org.automatic.coverage();self.assertEqual(status['processing'],[{'disposition':'processed','count':11}])
        self.assertTrue(all(b['state']=='completed' for b in status['batches']))
        await self.finish();self.org.schedule.daily_tick();await self.finish();self.assertEqual(len(m.calls),4)

    async def test_extraction_never_publishes_before_independent_review(self):
        await self.seed();self.install_model();self.enable();self.org.automatic.tick(force=True)
        await self.org.run_next();self.assertFalse(self.service.search_memories())
        self.org.automatic.tick(force=True);await self.org.run_next();self.assertEqual(len(self.service.search_memories()),1)

    async def test_one_quarantined_candidate_does_not_block_other_sources(self):
        await self.seed(2);m=self.install_model();self.enable()
        def review(kind,item,d,p):
            if kind=='candidates' and item['id']=='c0':d['action']='quarantine';d['reason']='人工不确定'
        m.review_action=review;await self.finish()
        self.assertEqual(len(self.service.search_memories()),1)
        self.assertEqual(len(self.org.automatic.decisions()),4)
        await self.finish();self.assertEqual(len(m.calls),2)

    async def test_revocation_invalidates_published_fact_and_rebuilds_remaining_fragment(self):
        from persona_runtime.memory_policy import exclude_sources
        turns=await self.seed(2);m=self.install_model();self.enable();await self.finish()
        with self.store.connection(write=True) as db:
            exclude_sources(db,self.persona['id'],[turns[0]['user_event_id']],'withdrawn')
        await self.finish()
        self.assertEqual(len(m.calls),4)
        self.assertEqual(len(self.service.search_memories()),1)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM memory_fragments WHERE state='active'").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT disposition FROM memory_coverage WHERE turn_id=?",(turns[0]['id'],)).fetchone()[0],'revoked')

    async def test_supported_revision_keeps_other_fact_and_never_excludes_original_round(self):
        await self.seed(2);m=self.install_model();self.enable();await self.finish()
        with self.store.connection() as db:
            old=dict(db.execute("SELECT * FROM memories ORDER BY rowid LIMIT 1").fetchone());f=dict(db.execute('SELECT * FROM memory_facts WHERE memory_id=?',(old['id'],)).fetchone())
        t=await self.chat.submit('desktop','更正：现在喜欢红茶，以这次为准。','correction');await self.chat.wait()
        def review(kind,item,d,p):
            if kind=='candidates':d.update(action='revise',subject=f['subject'],attribute=f['attribute'],target_memory_id=old['id'])
        m.review_action=review;await self.finish()
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT status FROM memories WHERE id=?',(old['id'],)).fetchone()[0],'superseded',
                {'tasks':[tuple(r) for r in db.execute('SELECT status,error_code FROM organization_tasks')],'batches':[tuple(r) for r in db.execute('SELECT state FROM memory_batches')],'turns':[tuple(r) for r in db.execute('SELECT status,error_code FROM turns')]})
            self.assertEqual(db.execute('SELECT count(*) FROM source_exclusions').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM memories WHERE status='active'").fetchone()[0],2)
            self.assertEqual(db.execute('SELECT count(*) FROM memory_fragments WHERE state=\'active\'').fetchone()[0],2)
        self.assertEqual(len(m.calls),6)

    async def test_hourly_quota_waits_between_extraction_and_review_without_duplicate_charge(self):
        import time
        await self.seed();m=self.install_model();self.enable();self.cfg.update({'organization':{'hourly_limit':1}})
        await self.finish();self.assertFalse(self.service.search_memories());self.assertEqual(len(m.calls),1)
        self.org.managed.clock=lambda:time.time()+3601
        await self.finish();self.assertEqual(len(m.calls),2);self.assertEqual(len(self.service.search_memories()),1)

    async def test_policy_pause_and_resume_rebinds_saved_receipt_without_clicking_continue(self):
        await self.seed();m=self.install_model();self.enable();self.org.automatic.tick(force=True)
        p=self.org.managed.current_policy();self.org.managed.update_policy(self.persona['id'],dict(p['data'],enabled=False),p['id'],self.persona['id'],identifier())
        await self.finish();self.assertEqual(len(m.calls),0)
        p=self.org.managed.current_policy();self.org.managed.update_policy(self.persona['id'],dict(p['data'],enabled=True),p['id'],self.persona['id'],identifier())
        await self.finish();self.assertEqual(len(m.calls),2)

    async def test_unknown_extraction_not_replayed_by_new_trigger_but_other_scope_can_progress(self):
        from persona_runtime.chat import ModelError
        await self.seed();self.enable();calls=[]
        async def unknown(*args):calls.append(1);raise ModelError('provider_timeout')
        self.org.client.complete=unknown;await self.finish();self.org.recover();await self.finish()
        self.assertEqual(len(calls),1)
        self.assertEqual(self.org.automatic.coverage()['batches'][0]['state'],'blocked_unknown')

    async def test_expired_memory_not_in_current_search_context_but_history_retained(self):
        from persona_runtime.automatic_memory import usable_fact
        await self.seed();m=self.install_model();self.enable()
        def review(kind,item,d,p):
            if kind=='candidates':d.update(effective_at='2025-01-01T00:00:00+00:00',valid_until='2025-01-02T00:00:00+00:00')
        m.review_action=review;await self.finish()
        with self.store.connection() as db:
            memory=dict(db.execute('SELECT * FROM memories').fetchone());self.assertFalse(usable_fact(db,memory))
            self.assertEqual(memory['status'],'active')

    async def test_export18_import17_and18_preserve_history_without_live_authority(self):
        from pathlib import Path
        from persona_runtime.portable_memory import import_export
        await self.seed();self.install_model();self.enable();await self.finish()
        exported=self.service.export_persona('portable')
        package=json.loads(Path(exported['path']).read_text(encoding='utf-8'));self.assertEqual(package['format_version'],19)
        self.assertNotIn('organization_policies',package);self.assertNotIn('organization_execution',package)
        for version in (17,18,19):
            package['format_version']=version
            result=import_export(self.store,package,self.persona['id'],'import-'+str(version))
            self.assertFalse(result['automation_enabled']);self.assertFalse(result['permissions_imported'])
            self.assertEqual(import_export(self.store,package,self.persona['id'],'import-'+str(version)),result)
            with self.store.connection() as db:
                count=db.execute('SELECT count(*) FROM memories WHERE persona_id=?',(result['id'],)).fetchone()[0]
                self.assertEqual(count,1)
                self.assertIsNotNone(db.execute('SELECT 1 FROM memory_defaults WHERE persona_id=?',(result['id'],)).fetchone())
                self.assertFalse(db.execute('SELECT 1 FROM organization_tasks WHERE persona_id=?',(result['id'],)).fetchone())

    async def test_future_temporal_version_has_one_current_value_at_each_time(self):
        from datetime import datetime,timezone,timedelta
        from persona_runtime.automatic_memory import usable_fact
        await self.seed();m=self.install_model();self.enable();await self.finish()
        old=self.service.search_memories()[0]
        with self.store.connection() as db:f=dict(db.execute('SELECT * FROM memory_facts WHERE memory_id=?',(old['id'],)).fetchone())
        cutoff=datetime.now(timezone.utc)+timedelta(days=10)
        await self.chat.submit('desktop','从明确的未来日期开始改为红茶。','future');await self.chat.wait()
        def review(kind,item,d,p):
            if kind=='candidates':d.update(action='temporal',subject=f['subject'],attribute=f['attribute'],target_memory_id=old['id'],effective_at=cutoff.isoformat())
        m.review_action=review;await self.finish()
        with self.store.connection() as db:
            rows=[dict(r) for r in db.execute("SELECT * FROM memories WHERE status='active'")]
            self.assertEqual(len(rows),2,{'batches':self.org.automatic.coverage(),'tasks':[tuple(r) for r in db.execute('SELECT status,error_code FROM organization_tasks')],'decisions':self.org.automatic.decisions(),'turns':[tuple(r) for r in db.execute('SELECT status,error_code FROM turns')]})
            self.assertEqual([r['id'] for r in rows if usable_fact(db,r)], [old['id']])
            self.assertEqual(len([r for r in rows if usable_fact(db,r,cutoff+timedelta(seconds=1))]),1)
            self.assertFalse(usable_fact(db,next(r for r in rows if r['id']==old['id']),cutoff))

    async def test_overlapping_scheduled_version_is_quarantined(self):
        from datetime import datetime,timezone,timedelta
        self.cfg.update({'organization':{'hourly_limit':100,'daily_limit':100}})
        await self.seed();m=self.install_model();self.enable();await self.finish()
        old=self.service.search_memories()[0]
        with self.store.connection() as db:f=dict(db.execute('SELECT * FROM memory_facts WHERE memory_id=?',(old['id'],)).fetchone())
        cutoff=(datetime.now(timezone.utc)+timedelta(days=10)).isoformat()
        def review(kind,item,d,p):
            if kind=='candidates':d.update(action='temporal',subject=f['subject'],attribute=f['attribute'],target_memory_id=old['id'],effective_at=cutoff)
        m.review_action=review
        await self.chat.submit('desktop','十天后改为红茶。','scheduled');await self.chat.wait();await self.finish()
        def review2(kind,item,d,p):
            if kind=='candidates':d.update(action='revise',subject=f['subject'],attribute=f['attribute'],target_memory_id=old['id'])
        m.review_action=review2
        await self.chat.submit('desktop','现在改为咖啡，未说明未来计划。','ambiguous');await self.chat.wait();await self.finish()
        self.assertTrue(any(r['reason']=='overlapping_fact_versions' for r in self.org.automatic.decisions()),self.org.automatic.decisions())

    async def test_verification_budget_change_resumes_without_new_extraction(self):
        await self.seed();m=self.install_model();self.enable();self.org.automatic.tick(force=True);await self.org.run_next()
        self.cfg.update({'organization':{'model':{'max_input_chars':1000}}})
        self.org.automatic.tick(force=True)
        self.assertEqual(self.org.automatic.coverage()['batches'][0]['state'],'blocked_input')
        self.cfg.update({'organization':{'model':{'max_input_chars':12000}}})
        await self.finish();self.assertEqual(len(m.calls),2)

    async def test_unrelated_memory_change_does_not_replay_saved_review(self):
        await self.seed();m=self.install_model();self.enable();self.org.automatic.tick(force=True);await self.org.run_next()
        self.org.automatic.tick(force=True)
        with self.store.connection(write=True) as db:
            now=__import__('persona_runtime.models',fromlist=['timestamp']).timestamp();mid=identifier()
            event=db.execute('SELECT id FROM events WHERE role=\'user\' LIMIT 1').fetchone()[0]
            from persona_runtime.models import LOCAL_USER_ID
            db.execute('INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(mid,self.persona['id'],LOCAL_USER_ID,'user_fact','完全无关的人工审核内容',event,mid,None,1,'active',now,now))
        await self.finish();self.assertEqual(len(m.calls),2)

    async def test_changed_content_same_import_operation_rejected_atomically(self):
        from pathlib import Path
        from persona_runtime.portable_memory import import_export
        from persona_runtime.models import RuntimeErrorBase
        await self.seed();package=json.loads(Path(self.service.export_persona('portable-conflict')['path']).read_text(encoding='utf-8'))
        import_export(self.store,package,self.persona['id'],'one')
        package['exported_at']='different'
        with self.assertRaisesRegex(RuntimeErrorBase,'submission_conflict'):import_export(self.store,package,self.persona['id'],'one')

    async def test_human_revision_uses_fact_level_dependencies_and_preserves_other_fact(self):
        await self.seed(2);self.install_model();self.enable();await self.finish()
        old=self.service.search_memories()[0];self.service.revise_memory(old['id'],'人工明确修订后的事实')
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM source_exclusions').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM memories WHERE status='active'").fetchone()[0],2)

    async def test_human_can_override_quarantine_with_zero_model_calls(self):
        await self.seed();m=self.install_model();self.enable()
        def review(kind,item,d,p):
            if kind=='candidates':d.update(action='quarantine',reason='人工不确定')
        m.review_action=review;await self.finish()
        row=next(r for r in self.org.automatic.decisions() if r['state']=='quarantined')
        result=self.org.automatic.override(row['id'],'publish',self.persona['id'],'manual-one')
        self.assertEqual(result['model_calls'],0);self.assertEqual(len(m.calls),2)
        self.assertEqual(len(self.service.search_memories()),1)
        self.assertEqual(self.org.automatic.override(row['id'],'publish',self.persona['id'],'manual-one'),result)

    async def test_full_queue_rolls_back_partial_memory_batch_admission(self):
        await self.seed();self.install_model();self.enable()
        for i in range(10):self.org.test_model('full-'+str(i))
        self.org.automatic.tick(force=True)
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM memory_batches').fetchone()[0],0)
        self.org.cancel(self.org.list()[0]['id']);await self.finish()
        self.assertTrue(self.org.automatic.coverage()['batches'])

    async def test_saved_verification_receipt_resumes_without_second_model_call(self):
        from unittest.mock import patch
        from persona_runtime.models import RuntimeErrorBase
        await self.seed();m=self.install_model();self.enable();self.org.automatic.tick(force=True);await self.org.run_next();self.org.automatic.tick(force=True)
        with patch.object(self.org.automatic,'commit',side_effect=RuntimeErrorBase('injected_commit_failure')):await self.org.run_next()
        with self.store.connection(write=True) as db:
            tid=db.execute('SELECT verify_task_id FROM memory_batches').fetchone()[0]
            # Offline crash image at the durable receipt boundary, not a plugin
            # intervention into a running product or a claim of real recovery.
            db.execute("UPDATE organization_tasks SET status='running' WHERE id=?",(tid,))
        self.org.recover();await self.finish()
        self.assertEqual(len(m.calls),2);self.assertEqual(len(self.service.search_memories()),1)

    async def test_same_scope_two_workers_share_one_batch_and_execution_owner(self):
        from persona_runtime.organization import OrganizationEngine
        await self.seed(11);m=self.install_model();self.enable()
        second=OrganizationEngine(self.store,self.cfg,m,self.chat)
        self.org.automatic.tick(force=True);second.automatic.tick(force=True)
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM memory_batches').fetchone()[0],1)
        await self.finish();second.automatic.tick(force=True)
        self.assertEqual(len(m.calls),4)

    async def test_paused_old_client_preserves_automatic_memory_setting(self):
        self.enable();p=self.org.managed.current_policy();data=dict(p['data']);data.pop('memory_automation');data['enabled']=False
        new=self.org.managed.update_policy(self.persona['id'],data,p['id'],self.persona['id'],identifier())
        self.assertTrue(new['data']['memory_automation']);self.assertFalse(new['data']['enabled'])

    async def test_source_withdrawal_between_extraction_and_review_never_publishes(self):
        from persona_runtime.memory_policy import exclude_sources
        turns=await self.seed(2);m=self.install_model();self.enable();self.org.automatic.tick(force=True);await self.org.run_next()
        with self.store.connection(write=True) as db:exclude_sources(db,self.persona['id'],[turns[0]['user_event_id']],'withdrawn_before_review')
        await self.finish();await self.finish()
        self.assertTrue(all('虚构偏好：喜欢乌龙茶 0'!=r['text'] for r in self.service.search_memories()))

    async def test_semantic_rejection_is_not_regenerated_or_counted_as_published(self):
        await self.seed();m=self.install_model();self.enable()
        def review(kind,item,d,p):
            if kind=='candidates':d.update(action='omit',reason='question_not_asserted_fact',sources=[])
        m.review_action=review;await self.finish();await self.finish()
        self.assertFalse(self.service.search_memories());self.assertEqual(len(m.calls),2)

    async def test_fixed_mixed_review_decisions_publish_positive_cases_and_isolate_ambiguity(self):
        # Hand-authored independent-review decisions verify transaction behavior,
        # not a claim that the mock model learned semantic classification.
        cases=[('我偏爱柚子茶。','publish','用户','饮品'),
               ('我不喝含糖饮料。','publish','用户','饮料限制'),
               ('我喜欢可乐吗？','omit','用户','疑问'),
               ('假如搬去海边，我会养一只猫。','omit','用户','假设'),
               ('开玩笑的，我有一千只猫。','omit','用户','玩笑'),
               ('同事林川住在北城。','publish','同事林川','住所'),
               ('表弟林川住在南城。','publish','表弟林川','住所'),
               ('我的背包添了一个铜铃。','publish','用户背包','配件'),
               ('寄存柜可能是蓝柜，也可能是绿柜，无法确认。','quarantine','用户','寄存柜'),
               ('我记不清寄存柜了，有人说是红柜。','quarantine','用户','寄存柜')]
        for n,(text,*_) in enumerate(cases):await self.chat.submit('desktop',text,'semantic-mix-'+str(n));await self.chat.wait()
        expected={text:(action,subject,attribute) for text,action,subject,attribute in cases}
        m=self.install_model();self.enable()
        def review(kind,item,d,p):
            event=next(e for e in p['events'] if e['id']==item['sources'][0]['source_id'])
            action,subject,attribute=expected[event['text']]
            if kind=='summary':d.update(action='summary' if action=='publish' else 'omit',reason='fixed_review_fixture')
            else:d.update(action=action,subject=subject,attribute=attribute,reason='fixed_review_fixture')
            if d['action'] in ('omit','quarantine'):d['sources']=[]
        m.review_action=review;await self.finish();await self.finish()
        current=self.service.search_memories();self.assertEqual(len(current),5)
        self.assertTrue(all(any(c['text']==text for c in current) for text,action,*_ in cases if action=='publish'))
        decisions=self.org.automatic.decisions();self.assertEqual(sum(d['state']=='quarantined' for d in decisions),2)
        self.assertEqual(len(m.calls),2)

    async def test_legacy_manual_entry_reuses_automatic_work_instead_of_paying_again(self):
        turns=await self.seed();m=self.install_model();self.enable();await self.finish()
        preview=self.org.preview(turns[0]['session_id'],'candidates',start_row=1,end_row=1)
        result=self.org.submit(preview,'manual-after-auto')
        self.assertTrue(result['shared_coverage']);await self.finish();self.assertEqual(len(m.calls),2)

    async def test_relative_plan_retains_source_date_in_clean_session_context(self):
        await self.chat.submit('desktop','我明天去图书馆。','dated-fact');await self.chat.wait()
        m=self.install_model();self.enable();await self.finish();memory=self.service.search_memories()[0]
        self.service.create_session();captured=[]
        async def reply(model,messages):captured.extend(messages);return '离线日期绑定检查。',{'total_tokens':1}
        self.chat.client.complete=reply
        turn=await self.chat.submit('desktop','我之前说哪天去图书馆？','dated-recall');await self.chat.wait()
        with self.store.connection() as db:
            original=db.execute('SELECT created_at FROM events WHERE id=?',(memory['source_event_id'],)).fetchone()[0]
        self.assertTrue(any(original in item['content'] and '记忆时间' in item['content'] for item in captured))
        uses=self.org.explain(turn['id']);self.assertTrue(any(t['id']==memory['id'] for u in uses for t in u['details'].get('memory_time_bindings',[])))

    async def test_disabled_summary_stays_disabled_until_explicit_rebuild_preview(self):
        await self.seed();m=self.install_model();self.enable();await self.finish()
        summary=self.org.summaries.list()[0];self.org.summaries.disable(summary['id'])
        await self.finish();self.assertEqual(len(m.calls),2)
        preview=self.org.automatic.rebuild_preview(summary['session_id'],self.persona['id'])
        result=self.org.automatic.rebuild_submit(preview,self.persona['id'],'rebuild-once')
        await self.finish();self.assertEqual(len(m.calls),4)
        self.assertEqual(self.org.automatic.rebuild_submit(preview,self.persona['id'],'rebuild-once'),result)

    async def test_legacy_unknown_effect_cannot_be_bypassed_by_enabling_automatic_memory(self):
        turns=await self.seed();p=self.org.preview(turns[0]['session_id']);self.model.failure='provider_timeout';self.org.submit(p,'legacy-unknown');await self.org.run_next()
        m=self.install_model();self.enable();await self.finish();self.assertFalse(m.calls)

    async def test_backfill_can_be_paused_without_pausing_new_sources(self):
        await self.seed();m=self.install_model();p=self.org.managed.current_policy()
        self.org.managed.update_policy(self.persona['id'],dict(p['data'],memory_automation=True,memory_backfill=False),p['id'],self.persona['id'],identifier())
        await self.finish();self.assertFalse(m.calls)
        await self.chat.submit('desktop','新增事实：喜欢山间散步。','new-source');await self.chat.wait();await self.finish()
        self.assertEqual(len(m.calls),2)
        self.assertEqual(self.org.automatic.coverage()['processed_turns'],1)


class MigrationTests(unittest.TestCase):
    def test_artificial22_migration_rollback_repeat_restore_and_export18(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from persona_runtime.paths import RuntimePaths
        from persona_runtime.storage import Store
        from persona_runtime.service import RuntimeService
        from persona_runtime.models import generic_persona
        from scripts.verify_m8d_migration import verify
        root=RuntimePaths.from_environment().home/'exports/acceptance'
        with tempfile.TemporaryDirectory(prefix='m8d-migration-',dir=root) as folder:
            paths=RuntimePaths(Path(folder)/'source');store=Store(paths)
            with patch('persona_runtime.storage.SCHEMA_VERSION',22):
                store.initialize();service=RuntimeService(store);p=service.create_persona(generic_persona());service.use_persona(p['id'])
            with patch('persona_runtime.storage.SCHEMA_VERSION',23):
                self.assertTrue(verify(store.path,Path(folder)/'proof')['source_unchanged'])
