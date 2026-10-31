"""Static author projection and optional Shadow integration. No live providers."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.archive import fingerprint
from persona_runtime.character import CharacterService
from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore
from persona_runtime.disposition import DispositionService,accepted_snapshot,source_catalog,status_for,version,projection_head
from persona_runtime.disposition_compile import DispositionCompiler,parse_output
from persona_runtime.disposition_shadow import persist_shadow
from persona_runtime.ingress import IngressService
from persona_runtime.models import RuntimeErrorBase,generic_persona,identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.profile import ProfileService
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


class Fake:
    def __init__(self):self.calls=[];self.output=None;self.messages=[]
    async def complete(self,model,messages):
        self.calls.append(model);self.messages.append(deepcopy(messages))
        if model.get('_disposition_compile'):return self.output,{'total_tokens':10}
        return '模拟回复',{'total_tokens':10}


class M761(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.paths=RuntimePaths(Path(self.tmp.name));self.store=Store(self.paths);self.store.initialize()
        self.config=ConfigStore(self.paths);self.config.initialize()
        self.config.update({'model':{'base_url':'https://example.invalid','name':'fake'},
            'character_selector':{'enabled':True,'base_url':'https://example.invalid','name':'fake'}})
        self.service=RuntimeService(self.store);self.pid=self.service.create_persona(generic_persona())['id'];self.service.use_persona(self.pid)
        self.fake=Fake();self.engine=ChatEngine(self.store,self.config,self.fake);self.ingress=IngressService(self.engine)
        self.dispositions=DispositionService(self.store,self.ingress);self.compiler=DispositionCompiler(self.dispositions,self.config,self.fake)
        self.policy=self.ingress.current_policy(self.pid,'desktop')
        self.policy=self.ingress.update_policy(self.pid,'desktop','legacy_per_message',self.policy['id'],identifier(),reply_mode='legacy_full',character_retrieval_mode='lexical')

    async def asyncTearDown(self):
        await self.compiler.drain();await self.engine.wait()
        self.engine.cognitive.shadow.close();self.engine.cognitive.characters.close();self.tmp.cleanup()

    def source(self):
        return next(r for r in self.dispositions.sources(self.pid)['sources'] if r['kind']=='expression')

    def document(self):
        r=self.source();return parse_output(json.dumps({'schema_version':1,'definitions':[{'id':'supportive','label':'人工倾听偏好',
            'source_refs':[{'source_id':r['source_id'],'quote':r['text']}],'triggers':[],
            'preferences':{'support':2},'priority':10,'enabled':True}]}),[r])

    def publish(self):
        h=self.dispositions.current(self.pid)
        h=self.dispositions.draft_update(self.pid,self.document(),h['revision'],identifier())
        return self.dispositions.confirm(self.pid,h['draft_id'],h['active_id'],h['revision'],identifier())

    def shadow_on(self):
        p=self.ingress.current_policy(self.pid,'desktop')
        return self.ingress.update_policy(self.pid,'desktop',p['mode'],p['id'],identifier(),disposition_mode='shadow')

    async def test_initialize_repeat_and_immutable_versions(self):
        self.store.initialize();h=self.publish()
        with self.store.connection(write=True) as db:
            with self.assertRaises(Exception):db.execute("UPDATE disposition_versions SET kind='draft' WHERE id=?",(h['active_id'],))
        with self.store.connection() as db:self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])

    async def test_sources_filter_exact_facts_private_disabled_and_experiential_relationship(self):
        chars=CharacterService(self.store);v=chars.current(self.pid);data=v['data'];data['appearance']['height_cm']=172
        data['social']['humor']='略带幽默';data['examples']=[{'id':'off','scenario':'x','keywords':[],'text':'secret','enabled':False}]
        data['lorebook']=[{'id':'private','title':'private','category':'person','keywords':[],'text':'secret','priority':1,'enabled':True,'visibility':'creator_only','dependencies':[]}]
        chars.update(self.pid,data,v['id'],identifier())
        rows=self.dispositions.sources(self.pid)['sources']
        self.assertFalse(any(r['entry_id'] in ('off','private') or r['path'].startswith('appearance') for r in rows))
        self.assertTrue(any(r['path']=='social.humor' for r in rows))
        from persona_runtime.profile import append_version
        with self.store.connection(write=True) as db:append_version(db,self.pid,'relationship',{'description':'经历'},source_ids=('fiction-event',))
        self.assertFalse(any(r['kind']=='relationship' for r in self.dispositions.sources(self.pid)['sources']))

    async def test_confirm_idempotency_conflict_and_body_free_preview(self):
        h=self.dispositions.current(self.pid);op=identifier();doc=self.document()
        a=self.dispositions.draft_update(self.pid,doc,h['revision'],op)
        self.assertTrue(self.dispositions.draft_update(self.pid,doc,h['revision'],op)['already_applied'])
        with self.assertRaisesRegex(RuntimeErrorBase,'submission_conflict'):self.dispositions.draft_update(self.pid,doc,9,op)
        with self.assertRaisesRegex(RuntimeErrorBase,'revision_conflict'):self.dispositions.confirm(self.pid,a['draft_id'],None,0,identifier())
        h=self.dispositions.confirm(self.pid,a['draft_id'],None,a['revision'],identifier())
        result=self.engine.cognitive.shadow.preview(self.pid,['我想找人倾诉'])
        self.assertEqual(result['record']['chosen_action'],'support')
        self.assertNotIn('我想找人倾诉',json.dumps(result,ensure_ascii=False))
        self.assertFalse(result['persisted'])

    async def test_source_update_stale_but_frozen_old_version_legal(self):
        h=self.publish()
        with self.store.connection() as db:snapshot=accepted_snapshot(db,self.pid,'shadow')
        old_document=self.dispositions.show(self.pid,h['active_id'])['document']
        p=ProfileService(self.store);old=p.current('expression')
        if p.current('expression')['id']==old['id']:
            with self.store.connection(write=True) as db:
                from persona_runtime.profile import append_version
                append_version(db,self.pid,'expression',{'style':'更新后的表达'})
        with self.store.connection() as db:
            self.assertEqual(status_for(db,self.pid,h['active_id']),'needs_review')
            self.assertEqual(status_for(db,self.pid,h['active_id'],current=False),'ready')
        with self.assertRaisesRegex(RuntimeErrorBase,'source_changed'):
            self.dispositions.draft_update(self.pid,old_document,h['revision'],identifier())

    async def test_shadow_frozen_request_and_prompt_identity(self):
        self.publish()
        self.shadow_on()
        b=await self.engine.submit('desktop','我想找人倾诉',identifier(),persona_id=self.pid)
        tid=b['id']
        with self.store.connection(write=True) as db:
            snapshot=db.execute('SELECT context_policy_snapshot FROM turns WHERE id=?',(tid,)).fetchone()[0]
            context=json.loads(snapshot);context['disposition']['mode']='baseline'
            db.execute('UPDATE turns SET context_policy_snapshot=? WHERE id=?',(json.dumps(context),tid))
        baseline=self.engine.cognitive.prepare(tid)['messages']
        with self.store.connection(write=True) as db:db.execute('UPDATE turns SET context_policy_snapshot=? WHERE id=?',(snapshot,tid))
        prepared=self.engine.cognitive.prepare(tid)
        self.assertEqual(fingerprint(baseline),fingerprint(prepared['messages']))
        await self.engine.wait()
        self.assertEqual(fingerprint(baseline),fingerprint(self.fake.messages[-1]))
        with self.store.connection() as db:
            records=db.execute('SELECT * FROM disposition_decisions WHERE turn_id=?',(b['id'],)).fetchall()
            self.assertEqual(len(records),1)
            data=json.loads(records[0]['record_json']);self.assertEqual(data['record']['chosen_action'],'support')
            self.assertFalse(data['applied_to_prompt'])
            manifest=json.loads(db.execute('SELECT manifest_json FROM context_manifests WHERE turn_id=? AND task_type=\'dialogue\'',(b['id'],)).fetchone()[0])
            self.assertFalse(manifest['shadow']['applied_to_prompt'])
        self.assertNotIn('chosen_action',json.dumps(self.fake.messages[-1]))

    async def test_compile_once_manifest_quota_and_no_auto_confirm(self):
        r=self.source();self.fake.output=json.dumps({'schema_version':1,'definitions':[]})
        op=identifier();receipt=await self.compiler.compile(self.pid,[r['source_id']],0,op)
        await self.compiler.task
        again=await self.compiler.compile(self.pid,[r['source_id']],0,op)
        self.assertEqual(receipt['attempt_id'],again['attempt_id']);self.assertEqual(len(self.fake.calls),1)
        self.assertIsNone(self.dispositions.current(self.pid)['active_id'])
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM context_manifests WHERE task_type='disposition_compilation'").fetchone()[0],1)
            from persona_runtime.memory_policy import project_calls
            self.assertEqual(project_calls(db,0),1)

    async def test_compile_invalid_output_and_source_race_never_publish(self):
        r=self.source();self.fake.output='{"schema_version":1,"definitions":[],"extra":1}'
        receipt=await self.compiler.compile(self.pid,[r['source_id']],0,identifier());await self.compiler.task
        self.assertEqual(self.compiler.status(self.pid,receipt['attempt_id'])[0]['status'],'failed')
        self.assertIsNone(self.dispositions.current(self.pid)['draft_id'])

    async def test_withdraw_blocks_frozen_projection(self):
        h=self.publish();self.dispositions.withdraw(self.pid,h['active_id'],h['revision'],identifier())
        with self.store.connection() as db:self.assertEqual(status_for(db,self.pid,h['active_id'],current=False),'withdrawn')

    async def test_old_client_preserves_mode_and_operation_conflicts(self):
        a=self.shadow_on();p=self.ingress.update_policy(self.pid,'desktop',a['mode'],a['id'],identifier(),reply_mode='legacy_full')
        self.assertEqual(p['disposition_mode'],'shadow')
        op=identifier();self.ingress.update_policy(self.pid,'desktop',p['mode'],p['id'],op,disposition_mode='shadow')
        with self.assertRaisesRegex(RuntimeErrorBase,'submission_conflict'):
            self.ingress.update_policy(self.pid,'desktop',p['mode'],p['id'],op,disposition_mode='baseline')
        with self.assertRaisesRegex(RuntimeErrorBase,'invalid_disposition_mode'):
            self.ingress.update_policy(self.pid,'desktop',p['mode'],p['id'],identifier(),disposition_mode='live')

    async def test_foreign_source_and_missing_manifest_fail_before_provider(self):
        r=self.source();doc=self.document();doc['definitions'][0]['source_refs'][0]['content_hash']='a'*64
        with self.assertRaisesRegex(RuntimeErrorBase,'source_mismatch'):self.dispositions.draft_update(self.pid,doc,0,identifier())
        # A trigger rejecting the compilation manifest must roll back attempt and prevent dispatch.
        with self.store.connection(write=True) as db:
            db.execute("CREATE TRIGGER fail_compiler_manifest BEFORE INSERT ON context_manifests WHEN NEW.task_type='disposition_compilation' BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(Exception):await self.compiler.compile(self.pid,[r['source_id']],0,identifier())
        self.assertEqual(self.fake.calls,[])
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM disposition_compile_attempts').fetchone()[0],0)

    async def test_pending_batch_requires_explicit_resolution(self):
        p=self.ingress.current_policy(self.pid,'desktop')
        self.ingress.update_policy(self.pid,'desktop','natural_batch',p['id'],identifier())
        receipt=self.ingress.accept({'channel':'desktop','text':'人工待处理','request_id':identifier()})
        h=self.dispositions.draft_update(self.pid,self.document(),0,identifier())
        with self.assertRaisesRegex(RuntimeErrorBase,'pending_batch_resolution_required'):
            self.dispositions.confirm(self.pid,h['draft_id'],None,h['revision'],identifier())
        self.dispositions.confirm(self.pid,h['draft_id'],None,h['revision'],identifier(),pending_action='cancel')
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT state FROM input_batches WHERE id=?',(receipt['batch_id'],)).fetchone()[0],'cancelled')

    async def test_record_failure_does_not_block_m7(self):
        self.publish();self.shadow_on()
        with self.store.connection(write=True) as db:
            db.execute("CREATE TRIGGER reject_shadow BEFORE INSERT ON disposition_decisions BEGIN SELECT RAISE(ABORT,'injected'); END")
        r=await self.engine.submit('desktop','人工聊天',identifier(),persona_id=self.pid);await self.engine.wait()
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT status FROM turns WHERE id=?',(r['id'],)).fetchone()[0],'completed')
            manifest=json.loads(db.execute("SELECT manifest_json FROM context_manifests WHERE turn_id=? AND task_type='dialogue'",(r['id'],)).fetchone()[0])
            self.assertEqual(manifest['shadow']['status'],'record_failed')
            self.assertFalse(manifest['shadow']['persisted'])
        self.assertEqual(len(self.fake.calls),1)

    async def test_manifest_failure_keeps_zero_main_calls(self):
        self.publish();self.shadow_on()
        with self.store.connection(write=True) as db:
            db.execute("CREATE TRIGGER reject_main BEFORE INSERT ON context_manifests WHEN NEW.task_type='dialogue' BEGIN SELECT RAISE(ABORT,'injected'); END")
        r=await self.engine.submit('desktop','人工聊天',identifier(),persona_id=self.pid);await self.engine.wait()
        self.assertEqual(self.fake.calls,[])
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM disposition_decisions').fetchone()[0],0)

    async def test_batch_snapshot_keeps_first_projection_and_retry_uses_current(self):
        h=self.publish();self.shadow_on()
        p=self.ingress.current_policy(self.pid,'desktop')
        self.ingress.update_policy(self.pid,'desktop','natural_batch',p['id'],identifier())
        r=self.ingress.accept({'channel':'desktop','text':'首成员','request_id':identifier()})
        with self.store.connection() as db:
            policy=json.loads(db.execute('SELECT policy_json FROM input_batches WHERE id=?',(r['batch_id'],)).fetchone()[0])
            self.assertEqual(policy['context']['disposition']['version_id'],h['active_id'])
        self.ingress.action(r['batch_id'],'cancel',r['batch_revision'],identifier(),self.pid)

    async def test_compiler_shares_selector_pool_and_project_reservations(self):
        self.config.update({'character_selector':{'hourly_limit':1}})
        self.fake.output='{"schema_version":1,"definitions":[]}'
        r=self.source()
        await self.compiler.compile(self.pid,[r['source_id']],0,identifier());await self.compiler.task
        with self.assertRaisesRegex(RuntimeErrorBase,'selector_limit_reached'):
            await self.compiler.compile(self.pid,[r['source_id']],1,identifier())
        self.assertEqual(len(self.fake.calls),1)

    async def test_compiler_busy_and_recovery_never_replays(self):
        event=asyncio.Event();original=self.fake.complete
        async def delayed(model,messages):await event.wait();return await original(model,messages)
        self.fake.complete=delayed;self.fake.output='{"schema_version":1,"definitions":[]}'
        r=self.source();a=await self.compiler.compile(self.pid,[r['source_id']],0,identifier())
        with self.assertRaisesRegex(RuntimeErrorBase,'compile_busy'):
            await self.compiler.compile(self.pid,[r['source_id']],0,identifier())
        event.set();await self.compiler.task
        self.compiler.recover();self.assertEqual(len(self.fake.calls),1)
        self.assertEqual(self.compiler.status(self.pid,a['attempt_id'])[0]['status'],'completed')

    async def test_compile_source_changed_while_provider_running(self):
        event=asyncio.Event();r=self.source()
        self.fake.output=json.dumps({'schema_version':1,'definitions':[{'id':'d','label':'人工','source_refs':[{'source_id':r['source_id'],'quote':r['text']}],'triggers':[],'preferences':{'support':2},'priority':1,'enabled':True}]})
        original=self.fake.complete
        async def delayed(model,messages):await event.wait();return await original(model,messages)
        self.fake.complete=delayed
        receipt=await self.compiler.compile(self.pid,[r['source_id']],0,identifier())
        with self.store.connection(write=True) as db:
            from persona_runtime.profile import append_version
            append_version(db,self.pid,'expression',{'style':'改变后'})
        event.set();await self.compiler.task
        self.assertEqual(self.compiler.status(self.pid,receipt['attempt_id'])[0]['error_code'],'disposition_source_changed')
        self.assertIsNone(self.dispositions.current(self.pid)['draft_id'])

    async def test_copy_only_confirmed_sources_not_runtime_records(self):
        self.publish()
        from persona_runtime.persona_management import PersonaManagement
        manager=PersonaManagement(self.store);view=manager.inspect(self.pid)
        result=manager.clone(self.pid,view['versions'],self.pid,identifier())
        copied=self.dispositions.current(result['id']);self.assertEqual(copied['status'],'ready')
        with self.store.connection() as db:
            self.assertNotEqual(copied['active_id'],projection_head(db,self.pid)['active_id'])
            self.assertEqual(db.execute('SELECT count(*) FROM disposition_decisions WHERE persona_id=?',(result['id'],)).fetchone()[0],0)

    async def test_preview_no_persistent_effect_and_export_16(self):
        self.publish()
        with self.store.connection() as db:before={t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('events','turns','attempts','context_manifests','disposition_decisions')}
        self.engine.cognitive.shadow.preview(self.pid,['只解释原因'])
        with self.store.connection() as db:after={t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in before}
        self.assertEqual(before,after)
        result=self.service.export_persona()
        payload=json.loads(Path(result['path']).read_text(encoding='utf-8'))
        self.assertEqual(payload['format_version'],16);self.assertIn('disposition_versions',payload)

    async def test_timeout_busy_late_result_and_retry_records(self):
        import time
        self.publish();self.shadow_on()
        turn=await self.engine.submit('desktop','人工倾诉',identifier(),persona_id=self.pid)
        await self.engine.wait()
        with self.store.connection() as db:raw=dict(db.execute('SELECT * FROM turns WHERE id=?',(turn['id'],)).fetchone())
        shadow=self.engine.cognitive.shadow;original=shadow._turn
        def slow(*args):time.sleep(.19);return original(*args)
        with patch.object(shadow,'_turn',side_effect=slow):
            late=await asyncio.to_thread(shadow.prepare,raw)
            self.assertEqual(late['status'],'timeout')
            self.assertEqual(shadow.prepare(raw)['status'],'busy')
            await asyncio.to_thread(shadow.future.result)
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM disposition_decisions').fetchone()[0],1)
        # Retry a failed revision using the current projection, retaining its prior record.
        r=await self.engine.submit('desktop','人工失败后重试',identifier(),persona_id=self.pid)
        await self.engine.wait()
        with self.store.connection(write=True) as db:db.execute("UPDATE turns SET status='failed' WHERE id=?",(r['id'],))
        old=self.dispositions.current(self.pid);self.dispositions.withdraw(self.pid,old['active_id'],old['revision'],identifier())
        await self.engine.retry(r['id']);await self.engine.wait()
        with self.store.connection() as db:
            rows=db.execute('SELECT execution_revision,status FROM disposition_decisions WHERE turn_id=? ORDER BY execution_revision',(r['id'],)).fetchall()
            self.assertEqual([x['status'] for x in rows],['observed','withdrawn'])
        self.assertEqual(len(self.fake.messages),3)

    async def test_cancelled_compile_cancels_provider_no_late_draft(self):
        entered=asyncio.Event();exited=asyncio.Event()
        async def provider(*args):
            entered.set()
            try:await asyncio.Event().wait()
            finally:exited.set()
        self.fake.complete=provider;r=self.source()
        receipt=await self.compiler.compile(self.pid,[r['source_id']],0,identifier())
        await entered.wait();self.compiler.task.cancel();await self.compiler.task
        await exited.wait()
        self.assertEqual(self.compiler.status(self.pid,receipt['attempt_id'])[0]['status'],'interrupted')
        self.assertIsNone(self.dispositions.current(self.pid)['draft_id'])

    async def test_historical_source_read_cannot_reveal_now_private_lore(self):
        chars=CharacterService(self.store);old=chars.current(self.pid);data=old['data']
        data['lorebook']=[{'id':'public','title':'人工','category':'person','keywords':[],'text':'人工角色材料','priority':1,'enabled':True,'visibility':'conversation','dependencies':[]}]
        public=chars.update(self.pid,data,old['id'],identifier());versions={'character':public['id']}
        self.assertTrue(any(r['entry_id']=='public' for r in self.dispositions.sources(self.pid,versions)['sources']))
        data['lorebook'][0]['visibility']='creator_only';chars.update(self.pid,data,public['id'],identifier())
        self.assertFalse(any(r['entry_id']=='public' for r in self.dispositions.sources(self.pid,versions)['sources']))

    async def test_compiler_provider_error_keeps_body_free_diagnostics(self):
        from persona_runtime.chat import ModelError
        async def failed(*args):raise ModelError('provider_network_error',diagnostics={'phase':'connecting','request_headers_sent':False,'private_body':'never-store-this'})
        self.fake.complete=failed;r=self.source()
        receipt=await self.compiler.compile(self.pid,[r['source_id']],0,identifier());await self.compiler.task
        row=self.compiler.status(self.pid,receipt['attempt_id'])[0]
        self.assertEqual(row['error_code'],'provider_network_error')
        self.assertEqual(json.loads(row['diagnostics_json'])['phase'],'connecting')
        self.assertNotIn('never-store-this',row['diagnostics_json'])

    async def test_reserved_foreground_capacity_cannot_be_used_by_compilation(self):
        import time
        from persona_runtime.models import LOCAL_USER_ID
        from tests.m71_fixtures import seed_turn
        self.config.update({'model':{'hourly_limit':1}})
        sid=self.service.create_session()['id']
        with self.store.connection(write=True) as db:
            seed_turn(db,self.pid,sid,'reserved',['人工前台请求'])
            revision=db.execute("SELECT execution_revision FROM turns WHERE id='reserved'").fetchone()[0]
            db.execute('INSERT INTO quota_reservations VALUES(?,?,?,?,?,?,?,?)',(identifier(),'reserved',revision,self.pid,LOCAL_USER_ID,'active',time.time(),time.time()+120))
        r=self.source()
        with self.assertRaisesRegex(RuntimeErrorBase,'hourly_limit_reached'):await self.compiler.compile(self.pid,[r['source_id']],0,identifier())
        self.assertEqual(self.fake.calls,[])

    async def test_malformed_optional_shadow_snapshot_is_reported_not_applied(self):
        self.publish();self.shadow_on();r=await self.engine.submit('desktop','人工消息',identifier(),persona_id=self.pid);await self.engine.wait()
        with self.store.connection() as db:turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(r['id'],)).fetchone())
        for value in ({'mode':'live'},[],{'mode':'shadow','extra':'not-copied'}):
            turn['context_policy_snapshot']=json.dumps({'disposition':value})
            result=self.engine.cognitive.shadow.prepare(turn)
            self.assertEqual(result['status'],'snapshot_invalid');self.assertFalse(result['applied_to_prompt'])
            self.assertNotIn('not-copied',json.dumps(result))

    async def test_decision_manifest_scope_is_deferred_and_exact(self):
        import sqlite3
        a=await self.engine.submit('desktop','人工一',identifier(),persona_id=self.pid);await self.engine.wait()
        b=await self.engine.submit('desktop','人工二',identifier(),persona_id=self.pid);await self.engine.wait()
        with self.store.connection() as db:
            context=db.execute("SELECT id FROM context_manifests WHERE turn_id=? AND task_type='dialogue'",(a['id'],)).fetchone()[0]
            revision=db.execute('SELECT execution_revision FROM turns WHERE id=?',(b['id'],)).fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connection(write=True) as db:
                db.execute('INSERT INTO disposition_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?)',(identifier(),self.pid,'00000000-0000-4000-8000-000000000001',b['id'],revision,None,context,None,'observed','{}','2026-10-31T00:00:00+00:00'))
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM disposition_decisions').fetchone()[0],0)

    async def test_kernel_matches_frozen_prototype_cases(self):
        from experiments.persona_decision import engine as experiment
        from persona_runtime.disposition_core import engine as production
        directory=Path(__file__).resolve().parents[1]/'experiments/persona_decision'
        corpus=experiment.read_json(directory/'fixtures.json');a=experiment.load_policy(directory/'rules.json');b=production.load_policy(directory/'rules.json')
        for case in corpus['development']:
            for mode in ('text','neutral','fixture'):
                self.assertEqual(experiment.run_case(case,mode,a).to_dict(),production.run_case(case,mode,b).to_dict())


if __name__=='__main__':unittest.main()
