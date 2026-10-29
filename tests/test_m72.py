"""M7.2 scope-safe context, policy snapshots and pre-call audit contracts."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import closing

from persona_runtime import storage
from persona_runtime.archive import fingerprint
from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore
from persona_runtime.ingress import IngressService,PARAMS
from persona_runtime.models import LOCAL_USER_ID,generic_persona,timestamp,RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from tests.m71_fixtures import seed_turn,insert


class CaptureModel:
    def __init__(self):self.calls=[]
    async def complete(self,model,messages):
        self.calls.append(messages)
        return '离线模拟回复',{'total_tokens':1}


def old_database(path,version):
    with closing(sqlite3.connect(path)) as db,db:
        for schema in (storage.SCHEMA,storage.SCHEMA2,storage.SCHEMA3,storage.SCHEMA4,storage.SCHEMA5,
                       storage.SCHEMA6,storage.SCHEMA7,storage.SCHEMA8,storage.SCHEMA9,storage.SCHEMA10,storage.SCHEMA11)[:version]:
            storage.execute_schema(db,schema)
        db.execute('INSERT INTO users VALUES(?,?,?)',(LOCAL_USER_ID,'人工测试',timestamp()))
        db.execute(f'PRAGMA user_version={version}')


class MigrationTests(unittest.TestCase):
    def test_all_supported_versions_new_and_repeat_initialize(self):
        for version in range(12):
            with self.subTest(version=version),tempfile.TemporaryDirectory(prefix='m72-migrate-',dir=RuntimePaths.from_environment().home/'tmp') as d:
                paths=RuntimePaths(Path(d));paths.initialize();store=storage.Store(paths)
                if version:old_database(store.path,version)
                store.initialize();store.initialize()
                with store.connection() as db:
                    self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],14)
                    self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                    self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())
                    self.assertEqual(db.execute('SELECT count(*) FROM context_manifests').fetchone()[0],0)

    def test_version11_failure_rolls_back_then_preserves_legacy_policy(self):
        with tempfile.TemporaryDirectory(prefix='m72-rollback-',dir=RuntimePaths.from_environment().home/'tmp') as d:
            paths=RuntimePaths(Path(d));paths.initialize();store=storage.Store(paths);old_database(store.path,11)
            spec=generic_persona();original=json.dumps(spec.to_dict(),ensure_ascii=False)
            with closing(sqlite3.connect(store.path)) as db,db:
                db.execute('INSERT INTO personas VALUES(?,?,?,?,?)',('old-persona',spec.name,1,original,timestamp()))
                db.execute('INSERT INTO interaction_policies VALUES(?,?,?,?,?,?,?,?,?)',('old-policy','old-persona',LOCAL_USER_ID,'qq',1,'natural_batch',json.dumps(PARAMS),timestamp(),'context_full'))
                db.execute('INSERT INTO interaction_heads VALUES(?,?,?,?)',('old-persona',LOCAL_USER_ID,'qq','old-policy'))
            real=storage.execute_schema
            def broken(conn,schema):
                real(conn,schema)
                if schema==storage.SCHEMA12:raise sqlite3.OperationalError('injected migration failure')
            with patch('persona_runtime.storage.execute_schema',side_effect=broken),self.assertRaises(sqlite3.Error):store.initialize()
            with closing(sqlite3.connect(store.path)) as db,db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],11)
                self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='context_manifests'").fetchone())
            store.initialize()
            with store.connection() as db:
                self.assertEqual(db.execute('SELECT spec_json FROM personas').fetchone()[0],original)
                self.assertEqual(db.execute("SELECT data_json FROM interaction_policies WHERE id='old-policy'").fetchone()[0],json.dumps(PARAMS))
                head=db.execute("SELECT p.* FROM interaction_policies p JOIN interaction_heads h ON h.version_id=p.id WHERE h.channel='qq'").fetchone()
                self.assertEqual(head['mode'],'natural_batch');self.assertEqual(head['reply_mode'],'context_full')
                self.assertEqual(json.loads(head['data_json'])['context'],{'context_mode':'working_set','cross_session':False})
                self.assertEqual(head['revision'],2)


class ContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='m72-context-',dir=RuntimePaths.from_environment().home/'tmp');self.addCleanup(self.temp.cleanup)
        self.paths=RuntimePaths(Path(self.temp.name));self.store=storage.Store(self.paths);self.store.initialize()
        self.config=ConfigStore(self.paths);self.config.initialize()
        self.config.update({'model':{'base_url':'http://127.0.0.1:1/v1','name':'fake','hourly_limit':1000}})
        self.model=CaptureModel();self.engine=ChatEngine(self.store,self.config,self.model);self.service=self.engine.service
        self.pid=self.service.create_persona(generic_persona())['id'];self.service.use_persona(self.pid)
        self.sid=self.engine.session()['session_id'];self.ingress=IngressService(self.engine)

    async def turn(self,text='青禾书店的预算和颜色是什么？'):
        with patch.object(self.engine,'_schedule'):
            return await self.engine.submit('desktop',text,str(id(text))+timestamp())

    def raw(self,tid):
        with self.store.connection() as db:return dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())

    def seed(self,count=24):
        with self.store.connection(write=True) as db:
            seed_turn(db,self.pid,self.sid,'book',['青禾书店需要阅读角','预算三百元，偏好蓝色','请给两个建议'])
            for i in range(count-1):seed_turn(db,self.pid,self.sid,f'noise-{i}',['无关人工日常 number_'+str(i)])

    def policy(self,**values):
        old=self.ingress.current_policy(self.pid,'desktop')
        return self.ingress.update_policy(self.pid,'desktop',old['mode'],old['id'],timestamp()+str(values),**values)

    async def test_default_enabled_recalls_nonfirst_member_without_uploading_fifty(self):
        self.seed();t=await self.turn();p=self.engine.cognitive.prepare(t['id'])
        text=json.dumps(p['messages'],ensure_ascii=False)
        self.assertIn('预算三百元',text);self.assertIn('book-e3',text)
        self.assertEqual(p['details']['retrieval']['window_count'],24)
        self.assertLessEqual(len(p['details']['history_groups']),5)
        self.assertNotIn('number_10',text)
        self.assertEqual(json.loads(t['context_policy_snapshot'])['context_mode'],'working_set')

    async def test_every_actual_call_has_exact_hash_and_append_only_manifest(self):
        self.seed();t=await self.turn();await self.engine._run(t['id'])
        self.assertEqual(len(self.model.calls),1);self.assertEqual(self.engine.get(t['id'])['status'],'completed')
        with self.store.connection() as db:
            manifest=json.loads(db.execute('SELECT manifest_json FROM context_manifests').fetchone()[0])
            self.assertEqual(manifest['input_snapshot_hash'],fingerprint(self.model.calls[0]))
            self.assertNotIn('预算三百元',json.dumps(manifest,ensure_ascii=False))
            self.assertTrue(manifest['persisted'])
        for statement in ('UPDATE context_manifests SET created_at=created_at','DELETE FROM context_manifests'):
            with self.assertRaises(sqlite3.IntegrityError),self.store.connection(write=True) as db:db.execute(statement)

    async def test_disabled_uses_ten_and_old_null_snapshot_stays_legacy(self):
        self.seed();self.policy(context_mode='legacy_recent');t=await self.turn()
        p=self.engine.cognitive.prepare(t['id']);self.assertEqual(len(p['details']['history_groups']),10)
        self.assertNotIn('预算三百元',json.dumps(p['messages'],ensure_ascii=False))
        with self.store.connection(write=True) as db:db.execute('UPDATE turns SET context_policy_snapshot=NULL WHERE id=?',(t['id'],))
        self.assertEqual(self.engine.cognitive.prepare(t['id'])['details']['retrieval']['policy']['origin'],'legacy_without_context_snapshot')

    async def test_future_turns_other_personas_and_sessions_never_leak(self):
        self.seed();t=await self.turn()
        other=self.service.create_persona(generic_persona())['id']
        with self.store.connection(write=True) as db:
            seed_turn(db,self.pid,self.sid,'future',['未来秘密 应当排除'])
        self.assertNotIn('未来秘密',json.dumps(self.engine.cognitive.prepare(t['id'])['messages'],ensure_ascii=False))
        self.assertEqual(self.engine.cognitive.preview(other,'desktop',task_type='affect')['status'],'not_ready')
        with self.assertRaisesRegex(RuntimeErrorBase,'context_unavailable'):self.engine.cognitive.preview(other,'desktop',turn_id=t['id'])

    async def test_cross_session_requires_explicit_policy_and_snapshot_is_frozen(self):
        self.seed();new=self.engine.session(fresh=True)['session_id'];t=await self.turn()
        self.assertNotIn('预算三百元',json.dumps(self.engine.cognitive.prepare(t['id'])['messages'],ensure_ascii=False))
        self.policy(cross_session=True)
        self.assertNotIn('预算三百元',json.dumps(self.engine.cognitive.prepare(t['id'])['messages'],ensure_ascii=False))
        other=await self.turn('请回忆青禾书店预算');self.assertIn('预算三百元',json.dumps(self.engine.cognitive.prepare(other['id'])['messages'],ensure_ascii=False))
        self.assertNotEqual(new,self.sid)

    async def test_cache_busy_and_corrupt_degrade_without_stale_retrieval(self):
        self.seed();t=await self.turn()
        with self.engine.cognitive.dialogue.index.locked():
            p=self.engine.cognitive.prepare(t['id'])
        self.assertEqual(p['details']['retrieval']['degraded_reason'],'cache_busy')
        self.assertNotIn('预算三百元',json.dumps(p['messages'],ensure_ascii=False))
        self.engine.cognitive.prepare(t['id']);self.engine.cognitive.dialogue.index.path.write_bytes(b'broken cache')
        p=self.engine.cognitive.prepare(t['id']);self.assertTrue(p['details']['retrieval']['degraded_reason'])
        self.assertIn('青禾书店的预算',p['messages'][-1]['content'])

    async def test_retraction_between_prepare_and_final_check_drops_whole_turn(self):
        self.seed();t=await self.turn();p=self.engine.cognitive.prepare(t['id'])
        with self.store.connection(write=True) as db:
            insert(db,'source_exclusions',dict(event_id='book-e2',persona_id=self.pid,user_id=LOCAL_USER_ID,reason='test',created_at=timestamp()))
        with self.store.connection(write=True) as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(t['id'],)).fetchone());p=self.engine.cognitive.finalize(db,turn,p)
        self.assertNotIn('预算三百元',json.dumps(p['messages'],ensure_ascii=False))
        self.assertEqual(p['details']['retrieval']['degraded_reason'],'source_changed')

    async def test_invalid_explicit_reference_records_failure_without_attempt(self):
        with patch.object(self.ingress,'schedule_committed'):
            receipt=self.ingress.accept({'channel':'desktop','request_id':'missing-ref','text':'请说明引用','reference':{'kind':'turn','id':'not-found'}})
        await self.engine._run(receipt['turn_id'])
        result=self.engine.get(receipt['turn_id']);self.assertEqual(result['error_code'],'reference_not_found');self.assertEqual(result['attempt_count'],0)
        self.assertFalse(self.model.calls)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM reference_checks').fetchone()[0],1)
            self.assertEqual(json.loads(db.execute('SELECT manifest_json FROM context_manifests').fetchone()[0])['status'],'blocked_before_call')

    async def test_policy_idempotency_and_collecting_batch_resolution(self):
        p=self.ingress.current_policy(self.pid,'desktop')
        self.ingress.update_policy(self.pid,'desktop','natural_batch',p['id'],'start-buffer')
        with patch.object(self.ingress,'schedule_committed'):
            first=self.ingress.accept({'channel':'desktop','request_id':'first','text':'第一条'})
            p=self.ingress.current_policy(self.pid,'desktop')
            with self.assertRaisesRegex(RuntimeErrorBase,'pending_batch_resolution_required'):
                self.ingress.update_policy(self.pid,'desktop','natural_batch',p['id'],'change',context_mode='legacy_recent')
            saved=self.ingress.update_policy(self.pid,'desktop','natural_batch',p['id'],'change',pending_action='seal',context_mode='legacy_recent')
            again=self.ingress.update_policy(self.pid,'desktop','natural_batch',p['id'],'change',pending_action='seal',context_mode='legacy_recent')
        self.assertEqual(saved,again)
        batch=self.ingress.get(first['batch_id']);turn=self.engine.get(batch['turn_id'])
        self.assertEqual(json.loads(turn['context_policy_snapshot'])['context_mode'],'working_set')

    async def test_probe_has_manifest_and_future_builders_are_side_effect_free(self):
        with self.store.connection() as db:before=list(db.iterdump())
        for kind in ('affect','director','planning'):
            result=self.engine.cognitive.preview(self.pid,'desktop',task_type=kind)
            self.assertEqual(result['status'],'not_ready');self.assertFalse(result['manifest']['persisted'])
        with self.store.connection() as db:self.assertEqual(before,list(db.iterdump()))
        with patch.object(self.engine,'_schedule'):t=await self.engine.submit('test','Reply with OK.','probe',test=True)
        await self.engine._run(t['id'])
        with self.store.connection() as db:self.assertEqual(json.loads(db.execute('SELECT manifest_json FROM context_manifests').fetchone()[0])['purpose'],'provider_probe')
        probe=self.engine.cognitive.inspect(self.pid,'desktop',turn_id=t['id'])['manifests'][0]
        self.assertEqual(probe['purpose'],'provider_probe');self.assertIsNone(probe['scope'])
        with self.assertRaisesRegex(RuntimeErrorBase,'context_unavailable'):
            self.engine.cognitive.inspect(self.pid,'qq',bot='10',peer='20',turn_id=t['id'])

    async def test_export9_includes_policy_and_manifest_without_credentials(self):
        t=await self.turn('你好');await self.engine._run(t['id'])
        result=self.service.export_persona();payload=json.loads(Path(result['path']).read_text(encoding='utf-8'))
        self.assertEqual(payload['format_version'],11);self.assertEqual(len(payload['context_manifests']),1)
        self.assertNotIn('encrypted_key',json.dumps(payload['context_manifests']))

    async def test_manifest_write_failure_rolls_back_attempt_and_never_calls_provider(self):
        t=await self.turn('本次禁止无清单调用')
        with self.store.connection(write=True) as db:
            db.execute("CREATE TRIGGER reject_manifest BEFORE INSERT ON context_manifests BEGIN SELECT RAISE(ABORT,'injected disk failure'); END")
        await self.engine._run(t['id'])
        result=self.engine.get(t['id'])
        self.assertEqual(result['error_code'],'context_manifest_storage_failed')
        self.assertEqual(result['attempt_count'],0);self.assertFalse(self.model.calls)

    async def test_retraction_after_retrieval_before_render_is_not_pinned_as_valid(self):
        self.seed();t=await self.turn();original=self.engine.cognitive.dialogue.evidence
        def raced(turn):
            value=original(turn)
            with self.store.connection(write=True) as db:
                insert(db,'source_exclusions',dict(event_id='book-e2',persona_id=self.pid,user_id=LOCAL_USER_ID,reason='withdrawn',created_at=timestamp()))
            return value
        with patch.object(self.engine.cognitive.dialogue,'evidence',side_effect=raced):p=self.engine.cognitive.prepare(t['id'])
        self.assertNotIn('预算三百元',json.dumps(p['messages'],ensure_ascii=False))
        self.assertEqual(p['details']['retrieval']['degraded_reason'],'source_changed')

    async def test_state_expires_between_render_and_final_verification(self):
        from datetime import datetime,timedelta,timezone
        from persona_runtime.profile import append_version
        expiry=datetime.now(timezone.utc)+timedelta(minutes=1)
        with self.store.connection(write=True) as db:append_version(db,self.pid,'state',{'text':'临时状态仅用于过期检查','expires_at':expiry.isoformat()})
        t=await self.turn();p=self.engine.cognitive.prepare(t['id'])
        self.assertIn('临时状态仅用于过期检查',p['messages'][0]['content'])
        with patch('persona_runtime.profile.datetime',wraps=datetime) as clock:
            clock.now.return_value=expiry+timedelta(seconds=1)
            with self.store.connection(write=True) as db:p=self.engine.cognitive.finalize(db,self.raw(t['id']),p)
        self.assertNotIn('临时状态仅用于过期检查',p['messages'][0]['content']);self.assertIsNone(p['details']['state_version'])
        self.assertIn('preparation_ms',p['manifest'])

    async def test_memory_human_revision_survives_raw_suppression_but_withdrawal_does_not(self):
        from persona_runtime.memory_policy import exclude_sources
        self.seed();memory=self.service.add_memory('preference','人工偏好：青禾书店喜欢红色','book-e2')
        corrected=self.service.revise_memory(memory['id'],'人工纠正：青禾书店喜欢蓝色')
        t=await self.turn();p=self.engine.cognitive.prepare(t['id'])
        self.assertIn(corrected['id'],[m['id'] for m in p['details']['memories']])
        with self.store.connection(write=True) as db:exclude_sources(db,self.pid,['book-e3'],'source_withdrawn')
        with self.store.connection(write=True) as db:p=self.engine.cognitive.finalize(db,self.raw(t['id']),p)
        self.assertNotIn(corrected['id'],[m['id'] for m in p['details']['memories']])

    async def test_zero_one_49_50_51_windows_and_twenty_members_are_whole(self):
        for n in (0,1,49,50,51):
            self.engine.session(fresh=True)
            sid=self.engine.session()['session_id']
            with self.store.connection(write=True) as db:
                for i in range(n):seed_turn(db,self.pid,sid,f'w{n}-{i}',[f'人工序列 {i}'])
            t=await self.turn('没有相关词项 zzzzz');p=self.engine.cognitive.prepare(t['id'])
            self.assertEqual(p['details']['retrieval']['window_count'],min(n,50))
            self.assertEqual(len(p['details']['history_groups']),min(n,2))
            with self.store.connection(write=True) as db:db.execute("UPDATE turns SET status='interrupted' WHERE id=?",(t['id'],))
        self.engine.session(fresh=True);sid=self.engine.session()['session_id']
        texts=[f'原始完整成员 {i}' for i in range(20)]
        with self.store.connection(write=True) as db:seed_turn(db,self.pid,sid,'twenty',texts)
        t=await self.turn('请结合成员');p=self.engine.cognitive.prepare(t['id'])
        self.assertEqual(p['details']['retrieval']['window_count'],1)
        for text in texts:self.assertIn(text,json.dumps(p['messages'],ensure_ascii=False))

    async def test_explicit_reference_outside_fifty_deduplicates_and_exceeds_budget_atomically(self):
        self.seed(52)
        with patch.object(self.ingress,'schedule_committed'):
            receipt=self.ingress.accept({'channel':'desktop','request_id':'outside50','text':'引用青禾书店','reference':{'kind':'event','id':'book-e3'}})
        p=self.engine.cognitive.prepare(receipt['turn_id'])
        self.assertNotIn('book',p['details']['retrieval']['window_turn_ids'])
        self.assertIn('预算三百元',p['messages'][0]['content'])
        self.assertNotIn('book',[g['turn_id'] for g in p['details']['history_groups']])
        with self.store.connection(write=True) as db:
            model=json.loads(self.raw(receipt['turn_id'])['model_snapshot']);model['max_input_chars']=10
            db.execute('UPDATE turns SET model_snapshot=? WHERE id=?',(json.dumps(model),receipt['turn_id']))
        await self.engine._run(receipt['turn_id'])
        self.assertEqual(self.engine.get(receipt['turn_id'])['error_code'],'reference_budget_exceeded')
        self.assertFalse(self.model.calls)

    async def test_scope_qq_bot_peer_actual_sent_and_desktop_memory_isolation(self):
        self.seed();self.service.add_memory('preference','青禾书店 私人桌面记忆','book-e2')
        self.config.update({'bot_qq':'10','owner_qq':'20'})
        with self.store.connection(write=True) as db:
            sid=self.sid
            seed_turn(db,self.pid,sid,'qq-good',['允许当前范围的原文'],channel='qq',bot='10',peer='20',reply='未确认语义秘密')
            seed_turn(db,self.pid,sid,'qq-other',['不允许其他Bot'],channel='qq',bot='11',peer='20')
            seed_turn(db,self.pid,sid,'qq-peer',['不允许其他联系人'],channel='qq',bot='10',peer='21')
        with patch.object(self.ingress,'schedule_committed'):
            r=self.ingress.accept({'channel':'qq','request_id':'qq-context','text':'青禾书店','bot':'10','sender':'20','platform_message_id':'qq-context'})
        p=self.engine.cognitive.prepare(r['turn_id']);text=json.dumps(p['messages'],ensure_ascii=False)
        self.assertNotIn('私人桌面记忆',text);self.assertNotIn('未确认语义秘密',text)
        self.assertNotIn('不允许其他',text)

    async def test_round_robin_terms_and_internal_any_do_not_change_public_all(self):
        from persona_runtime.context_builders import query_terms
        terms,_,info=query_terms([{'id':'first','text':' '.join('a'+str(i) for i in range(50))},
                                  {'id':'last','text':'uniquelastmember'}])
        self.assertEqual(len(terms),32);self.assertIn('uniquelastmember',terms);self.assertGreater(info['omitted_terms'],0)
        self.seed();t=await self.turn('青禾书店 impossible_nohit');p=self.engine.cognitive.prepare(t['id'])
        self.assertIn('book',[g['turn_id'] for g in p['details']['history_groups']])
        from persona_runtime.archive_search import ArchiveSearchService
        public=ArchiveSearchService(self.store).search(persona_id=self.pid,channel='desktop',query='青禾书店 impossible_nohit')
        self.assertFalse(public.get('groups'))

    async def test_natural_chinese_punctuation_still_extracts_recall_terms(self):
        self.seed();t=await self.turn('工程验收：根据本会话人工档案，虚构青禾书店阅读角的预算和偏好颜色是什么？没有资料不要猜。')
        p=self.engine.cognitive.prepare(t['id'])
        self.assertGreater(p['details']['retrieval']['query']['term_count'],0)
        self.assertIn('预算三百元',json.dumps(p['messages'],ensure_ascii=False))

    async def test_slow_cache_work_does_not_hold_main_writer_or_loop(self):
        import threading
        entered=threading.Event();release=threading.Event();original=self.engine.cognitive.dialogue.index.synchronize
        def slow(*args,**kwargs):entered.set();release.wait(3);return original(*args,**kwargs)
        self.seed();t=await self.turn()
        with patch.object(self.engine.cognitive.dialogue.index,'synchronize',side_effect=slow):
            pending=asyncio.create_task(self.engine._run(t['id']))
            async with asyncio.timeout(5):
                while not entered.is_set():await asyncio.sleep(.005)
            try:
                with self.store.connection(write=True) as db:db.execute("UPDATE settings SET value=value WHERE key='current_persona'")
                with patch.object(self.engine,'_schedule'):new=await self.engine.submit('desktop','并发入站','concurrent')
                self.assertEqual(new['status'],'queued')
            finally:release.set();await pending
        self.assertEqual(len(self.model.calls),1)

    async def test_reflection_calls_and_probes_persist_exact_manifest_before_send(self):
        from persona_runtime.organization import OrganizationEngine
        from tests.test_m4 import OrganizerModel
        model=OrganizerModel();org=OrganizationEngine(self.store,self.config,model,self.engine)
        self.config.update({'organization':{'reuse_chat':True}})
        self.seed(1);task=org.submit(org.preview(self.sid),'reflection')
        original=model.complete
        async def checked(config,messages):
            with self.store.connection() as db:
                row=db.execute('SELECT manifest_json FROM context_manifests WHERE organization_task_id=?',(task['id'],)).fetchone()
                self.assertIsNotNone(row);self.assertEqual(json.loads(row[0])['input_snapshot_hash'],fingerprint(messages))
            return await original(config,messages)
        model.complete=checked
        await org.run_next();self.assertEqual(org.show(task['id'])['status'],'completed')
        self.assertEqual(len(model.calls),1)
        shown=self.engine.cognitive.inspect(self.pid,'desktop',task_id=task['id'])
        self.assertEqual(shown['manifests'][0]['purpose'],'memory_organization')
        task=org.test_model();await org.run_next()
        with self.store.connection() as db:
            self.assertEqual(json.loads(db.execute('SELECT manifest_json FROM context_manifests WHERE organization_task_id=?',(task['id'],)).fetchone()[0])['purpose'],'provider_probe')

    async def test_reflection_manifest_failure_has_no_attempt_or_provider_call(self):
        from persona_runtime.organization import OrganizationEngine
        from tests.test_m4 import OrganizerModel
        model=OrganizerModel();org=OrganizationEngine(self.store,self.config,model,self.engine)
        self.config.update({'organization':{'reuse_chat':True}});self.seed(1)
        task=org.submit(org.preview(self.sid),'reflection-disk-failure')
        with self.store.connection(write=True) as db:db.execute("CREATE TRIGGER reject_manifest BEFORE INSERT ON context_manifests BEGIN SELECT RAISE(ABORT,'injected'); END")
        await org.run_next();self.assertFalse(model.calls)
        self.assertEqual(org.show(task['id'])['status'],'failed')
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM organization_attempts').fetchone()[0],0)

    async def test_admin_api_preview_inspection_permissions_and_no_business_writes(self):
        from aiohttp import ClientSession
        from aiohttp.test_utils import TestServer
        from persona_runtime.server import create_app
        async with TestServer(create_app(self.paths,self.model)) as server,ClientSession() as client:
            headers={'Authorization':'Bearer '+self.config.token()};url=str(server.make_url('/v1/manage'))
            async with client.get(str(server.make_url('/v1/health')),headers=headers) as r:
                self.assertIn('cognitive_context_v1',(await r.json())['capabilities'])
            with self.store.connection() as db:before=list(db.iterdump())
            for kind in ('affect','director','planning'):
                payload={'action':'context.preview','args':{'persona_id':self.pid,'channel':'desktop','task_type':kind}}
                async with client.post(url,headers=headers,json=payload) as r:self.assertEqual((await r.json())['status'],'not_ready')
                async with client.post(url,headers={'Authorization':'Bearer '+self.config.token('bridge')},json=payload) as r:self.assertEqual(r.status,403)
            with self.store.connection() as db:self.assertEqual(before,list(db.iterdump()))
