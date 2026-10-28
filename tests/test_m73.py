"""Lifecycle, authoritative packages and explicit consolidation contracts."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from persona_runtime import storage
from persona_runtime.archive import fingerprint
from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore
from persona_runtime.ingress import IngressService
from persona_runtime.models import LOCAL_USER_ID,RuntimeErrorBase,generic_persona,identifier,timestamp
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.sessions import SessionService,OrganizationDecisionProvider
from persona_runtime.topics import TopicService
from persona_runtime.organization import OrganizationEngine
from persona_runtime.session_organization import SessionOrganization
from persona_runtime.memory_policy import exclude_sources
from tests.m71_fixtures import seed_turn


class Model:
    def __init__(self):self.calls=[]
    async def complete(self,model,messages):
        self.calls.append(messages)
        if '"format_version":2' in messages[0]['content']:
            source=json.loads(messages[-1]['content']);events=source['events'];e=events[0]
            summary=[{'text':'人工资料摘要','sources':[{'source_id':x['id'],'quote':x['text']}]} for x in events[:3]]
            candidates=[{'category':'preference','text':'用户偏好蓝色','sources':[{'source_id':e['id'],'quote':e['text']}],'conflicts':[]}]
            return json.dumps({'format_version':2,'summary':summary if source['mode']!='candidates' else [],'candidates':candidates if source['mode']!='summary' else []},ensure_ascii=False),{'total_tokens':2}
        return '离线纯文本确认',{'total_tokens':1}


class SessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='m73-',dir=RuntimePaths.from_environment().home/'tmp');self.addCleanup(self.tmp.cleanup)
        self.paths=RuntimePaths(Path(self.tmp.name));self.store=storage.Store(self.paths);self.store.initialize()
        self.config=ConfigStore(self.paths);self.config.initialize();self.config.update({'model':{'base_url':'http://127.0.0.1:1/v1','name':'fake','hourly_limit':1000},'bot_qq':'12345','owner_qq':'67890'})
        self.model=Model();self.engine=ChatEngine(self.store,self.config,self.model);self.service=self.engine.service
        self.pid=self.service.create_persona(generic_persona())['id'];self.service.use_persona(self.pid)
        self.sid=self.engine.session()['session_id'];self.now=0.;self.sessions=SessionService(self.store,lambda:self.now)
        self.ingress=IngressService(self.engine);self.topics=TopicService(self.store)
        self.org=OrganizationEngine(self.store,self.config,self.model,self.engine);self.org.model=lambda:self.config.load()['model']
        self.organize=SessionOrganization(self.org)
    async def asyncTearDown(self):await self.engine.wait()
    def show(self,sid=None,**scope):return self.sessions.show(sid or self.sid,self.pid,**scope)
    def seal(self,sid=None,**scope):
        sid=sid or self.sid;r=self.show(sid,**scope)
        return self.sessions.seal(sid,self.pid,r['revision'],identifier(),**scope)
    async def turn(self,text='人工设定：书店预算三百元，偏好蓝色',**extra):
        value={'text':text,'request_id':identifier(),'persona_id':self.pid,**extra};r=self.ingress.accept(value);await self.engine.wait();return r
    def seed(self,key='seed',members=None,sid=None,**extra):
        with self.store.connection(write=True) as db:seed_turn(db,self.pid,sid or self.sid,key,members or ['人工信息甲','预算三百元','偏好蓝色'],**extra)
        return key

    async def test_idle_boundary_duplicate_reads_and_restart(self):
        await self.turn();self.sessions.tick();self.now=1799
        self.assertEqual(self.sessions.tick(),[]);self.show();self.now=1800
        self.assertEqual(self.sessions.tick()[0]['state'],'sealed')
        next_sid=self.engine.session()['session_id'];self.sessions.tick();self.now+=1799;self.sessions.restart_observation();self.sessions.tick();self.now+=1
        self.assertEqual(self.sessions.tick(),[]);self.now+=1799;self.assertEqual(self.sessions.tick()[0]['session_id'],next_sid)

    def test_scoped_simulated_clock_records_provenance_without_other_session_changes(self):
        self.seed();other=self.service.create_persona(generic_persona())['id'];self.service.use_persona(other)
        other_sid=self.engine.session()['session_id'];self.service.use_persona(self.pid)
        before=self.sessions.show(other_sid,other)
        self.sessions.tick(_session_ids={self.sid});self.now=1799
        self.assertEqual(self.sessions.tick(_session_ids={self.sid}),[])
        self.now=1800;marker={'mode':'simulated_deadline','threshold_seconds':1800}
        self.assertEqual(self.sessions.tick(_session_ids={self.sid},_clock_evidence=marker)[0]['state'],'sealed')
        self.assertEqual(self.sessions.package(self.sid,self.pid)['manifest']['clock_evidence'],marker)
        after=self.sessions.show(other_sid,other)
        self.assertEqual(after['state'],'active');self.assertEqual(before['revision'],after['revision'])

    async def test_duplicate_receipt_after_seal_and_explicit_old_session_rejected(self):
        value={'text':'原文空白  ','request_id':identifier(),'persona_id':self.pid,'session_id':self.sid}
        first=self.ingress.accept(value);await self.engine.wait();self.seal()
        self.assertEqual(self.ingress.accept(value)['receipt_id'],first['receipt_id'])
        with self.assertRaisesRegex(RuntimeErrorBase,'session_sealed'):self.ingress.accept(dict(value,request_id=identifier()))
        self.assertEqual(len(self.model.calls),1)

    async def test_reopen_keeps_sources_and_does_not_expand_context(self):
        r=await self.turn();sealed=self.seal();before=self.sessions.package(self.sid,self.pid)
        new=self.sessions.reopen(self.sid,self.pid,sealed['revision'],'reopen-op')
        self.assertEqual(new,self.sessions.reopen(self.sid,self.pid,sealed['revision'],'reopen-op'))
        r2=await self.turn('普通问候')
        self.assertEqual(r2['session_id'],new['session_id']);self.assertNotEqual(r2['session_id'],self.sid)
        self.assertEqual(before,self.sessions.package(self.sid,self.pid));self.assertNotIn('三百元',json.dumps(self.model.calls[-1],ensure_ascii=False))

    def test_package_has_all_members_hashes_but_no_raw_text_or_credentials(self):
        self.seed();s=self.seal();p=self.sessions.package(self.sid,self.pid);t=p['manifest']['turns'][0]
        self.assertEqual(len(t['member_ids']),3);self.assertNotIn('三百元',json.dumps(p,ensure_ascii=False))
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM session_triggers').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],0)
        replay=self.sessions.replay(self.sid,self.pid);self.assertEqual(len(replay['groups'][0]['current']['members']),3)
        self.assertEqual(OrganizationDecisionProvider().evaluate(p)['decision'],'DEFER')

    def test_empty_session_no_action_triggers(self):
        self.seal();r=self.sessions.triggers(self.sid,self.pid)
        self.assertEqual({t['state'] for t in r['triggers']},{'no_action'})

    def test_atomic_package_write_failure(self):
        self.seed()
        with patch('persona_runtime.sessions.event',side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaises(sqlite3.Error):self.seal()
        self.assertEqual(self.show()['state'],'active')
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM session_packages').fetchone()[0],0)

    def test_seal_racing_new_event_rejects_old_snapshot(self):
        self.seed();from persona_runtime.sessions import build_manifest
        def race(db,row):
            value=build_manifest(db,row)
            # The read transaction must close before competing commit (SQLite rollback journal).
            return value
        expected=self.show()['revision'];self.service.append_event(self.sid,'note','晚到笔记')
        with self.assertRaisesRegex(RuntimeErrorBase,'version_conflict'):self.sessions.seal(self.sid,self.pid,expected,'old')
        self.assertEqual(self.show()['state'],'active')

    async def test_collecting_and_queued_block_then_cancel(self):
        p=self.ingress.current_policy(self.pid,'desktop');self.ingress.update_policy(self.pid,'desktop','natural_batch',p['id'],'policy')
        r=self.ingress.accept({'text':'未完成输入','request_id':'pending','persona_id':self.pid})
        self.assertEqual(self.seal()['state'],'blocked');b=self.ingress.get(r['batch_id'])
        self.ingress.action(b['id'],'cancel',b['revision'],'cancel',self.pid)
        self.assertEqual(self.seal()['state'],'sealed');self.assertEqual(self.model.calls,[])

    def test_sealed_notes_and_retry_are_refused(self):
        self.seed(status='failed');self.seal()
        with self.assertRaisesRegex(RuntimeErrorBase,'session_sealed'):self.service.append_event(self.sid,'note','must not append')

    async def test_retry_sealed_failed_request_is_zero_call(self):
        self.seed(status='failed');self.seal()
        with self.assertRaisesRegex(RuntimeErrorBase,'session_sealed'):await self.engine.retry('seed')
        self.assertEqual(self.model.calls,[])

    def test_scope_and_selection_conflicts(self):
        with self.assertRaisesRegex(RuntimeErrorBase,'unavailable'):self.sessions.show(self.sid,self.pid,'qq','12345','67890')
        other=self.service.create_persona(generic_persona())['id'];self.service.use_persona(other)
        with self.assertRaisesRegex(RuntimeErrorBase,'selection_conflict'):self.seal()
        with self.assertRaisesRegex(RuntimeErrorBase,'unavailable'):self.sessions.show(self.sid,other)

    async def test_qq_identity_change_gets_new_session(self):
        with patch.object(self.engine,'_schedule'):
            a=self.ingress.accept({'channel':'qq','text':'甲','bot':'12345','sender':'67890','platform_message_id':'a','request_id':'a'})
            self.config.update({'bot_qq':'99999'})
            b=self.ingress.accept({'channel':'qq','text':'乙','bot':'99999','sender':'67890','platform_message_id':'b','request_id':'b'})
        self.assertNotEqual(a['session_id'],b['session_id'])
        with self.assertRaisesRegex(RuntimeErrorBase,'unavailable'):self.sessions.show(a['session_id'],self.pid,'qq','99999','67890')

    def test_seal_operation_conflict_and_immutable_package(self):
        row=self.show();a=self.sessions.seal(self.sid,self.pid,row['revision'],'stable')
        self.assertEqual(a,self.sessions.seal(self.sid,self.pid,row['revision'],'stable'))
        with self.assertRaisesRegex(RuntimeErrorBase,'operation_conflict'):self.sessions.seal(self.sid,self.pid,row['revision'],'stable',reason='idle')
        with self.assertRaises(sqlite3.Error),self.store.connection(write=True) as db:db.execute('DELETE FROM session_packages')

    def test_topics_contiguous_whole_scope_version_and_unlink(self):
        for i in range(3):self.seed('t'+str(i))
        self.seal();topic=self.topics.create(self.pid,'书店','人工主题','topic',session_id=self.sid,turn_ids=['t0','t1'])
        thread=self.topics.create(self.pid,'计划','人工组织','thread',kind='thread')
        r=self.topics.link(thread['id'],topic['id'],self.pid,1,1,'link')
        self.assertTrue(r['linked']);self.assertFalse(self.topics.show(thread['id'],self.pid,kind='thread')['affects_recall'])
        with self.assertRaisesRegex(RuntimeErrorBase,'version_conflict'):self.topics.link(thread['id'],topic['id'],self.pid,1,1,'stale')
        with self.assertRaisesRegex(RuntimeErrorBase,'contiguous'):self.topics.create(self.pid,'错段','','bad',session_id=self.sid,turn_ids=['t0','t2'])
        self.assertFalse(self.topics.link(thread['id'],topic['id'],self.pid,2,2,'unlink',False)['linked'])

    def test_nonfirst_withdrawal_invalidates_topic_package_and_preview(self):
        self.seed();self.seal();topic=self.topics.create(self.pid,'主题','','topic',session_id=self.sid,turn_ids=['seed'])
        p=self.sessions.package(self.sid,self.pid)
        with self.store.connection(write=True) as db:exclude_sources(db,self.pid,['seed-e2'],'manual_withdrawal')
        self.assertFalse(self.sessions.replay(self.sid,self.pid)['groups'][0]['usable'])
        self.assertFalse(self.topics.show(topic['id'],self.pid)['usable'])
        self.assertEqual(self.organize.preview(self.sid,self.pid)['status'],'no_action')
        self.assertEqual(p,self.sessions.package(self.sid,self.pid))

    async def test_manual_organization_manifest_candidates_and_coverage(self):
        self.seed();self.seal();preview=self.organize.preview(self.sid,self.pid)
        t=self.organize.submit(preview,self.pid,preview['lifecycle_revision'],'manual')
        self.assertEqual(t,self.organize.submit(preview,self.pid,preview['lifecycle_revision'],'manual'))
        await self.org.run_next();self.assertEqual(self.org.show(t['id'])['status'],'completed')
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM context_manifests').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT status FROM candidates').fetchone()[0],'pending')
            self.assertEqual(db.execute('SELECT count(*) FROM memories').fetchone()[0],0)
            self.assertIsNotNone(db.execute('SELECT source_refs_json FROM summaries').fetchone()[0])
        self.assertEqual(self.organize.preview(self.sid,self.pid)['status'],'no_action')
        self.assertEqual(len(self.model.calls),1)

    async def test_legacy_desktop_coverage_reused_and_previous_revalidated(self):
        from tests.test_m4 import OrganizerModel
        from persona_runtime.session_organization import valid
        self.seed();legacy=OrganizerModel();self.org.client=legacy
        task=self.org.submit(self.org.preview(self.sid),'before-seal')
        await self.org.run_next();self.assertEqual(self.org.show(task['id'])['status'],'completed')
        self.seal();preview=self.organize.preview(self.sid,self.pid)
        self.assertEqual(preview['status'],'no_action');self.assertTrue(preview['covered_rows'])
        self.assertTrue(preview['previous']['source_refs_json'])
        before=len(legacy.calls)
        result=self.organize.submit(preview,self.pid,preview['lifecycle_revision'],'reuse-old')
        self.assertEqual(result['status'],'no_action');self.assertEqual(len(legacy.calls),before)
        with self.store.connection(write=True) as db:exclude_sources(db,self.pid,['seed-e2'],'withdraw-nonfirst')
        with self.store.connection() as db:self.assertFalse(valid(db,preview))
        self.assertIsNone(self.organize.preview(self.sid,self.pid)['previous'])

    async def test_organization_rechecks_withdrawal_before_call(self):
        self.seed();self.seal();preview=self.organize.preview(self.sid,self.pid)
        t=self.organize.submit(preview,self.pid,preview['lifecycle_revision'],'manual')
        with self.store.connection(write=True) as db:exclude_sources(db,self.pid,['seed-e3'],'withdrawn')
        await self.org.run_next();self.assertEqual(self.org.show(t['id'])['status'],'stale');self.assertEqual(self.model.calls,[])

    async def test_restart_interrupts_manual_task_no_replay(self):
        self.seed();self.seal();p=self.organize.preview(self.sid,self.pid);t=self.organize.submit(p,self.pid,p['lifecycle_revision'],'manual')
        self.org.recover();await self.org.run_next()
        self.assertEqual(self.org.show(t['id'])['status'],'interrupted');self.assertEqual(self.model.calls,[])

    def test_organization_overlap_and_budget(self):
        self.seed();self.seal();p=self.organize.preview(self.sid,self.pid);self.organize.submit(p,self.pid,p['lifecycle_revision'],'manual')
        with self.assertRaisesRegex(RuntimeErrorBase,'busy'):self.organize.submit(p,self.pid,p['lifecycle_revision'],'other')
        with patch.object(self.org,'model',return_value={'max_input_chars':30}):
            with self.assertRaisesRegex(RuntimeErrorBase,'too_long'):self.organize.preview(self.sid,self.pid)

    async def test_qq_actual_sent_source_and_legacy_disposition(self):
        from persona_runtime.delivery import DeliveryService
        r=await self.turn(channel='qq',bot='12345',sender='67890',platform_message_id='p1');sid=r['session_id'];selected={'channel':'qq','bot':'12345','peer':'67890'}
        turn=self.engine.get(r['turn_id']);delivery=DeliveryService(self.store);stream=delivery.by_batch(turn['reply_batch']['id'])
        self.assertEqual(self.seal(sid,**selected)['state'],'blocked')
        for part in stream['parts']:
            stream=delivery.get(stream['id']);claim=delivery.claim(stream['id'],part['id'],stream['revision'],identifier())
            delivery.result(stream['id'],part['id'],claim['claim_token'],'sent',claim['part_revision'],identifier(),platform_message_id='out-'+part['id'])
        self.seal(sid,**selected);p=self.organize.preview(sid,self.pid,**selected)
        self.assertIn('part',{e['kind'] for e in p['events']});t=self.organize.submit(p,self.pid,p['lifecycle_revision'],'qq-organize',**selected)
        await self.org.run_next();self.assertEqual(self.org.show(t['id'])['status'],'completed')
        # A legacy send cannot claim after explicit abandonment; original state stays intact.
        q=self.engine.session(channel='qq',fresh=True)['session_id']
        with self.store.connection(write=True) as db:
            seed_turn(db,self.pid,q,'old',['旧入站'],channel='qq',bot='12345',peer='67890')
            db.execute("INSERT INTO deliveries VALUES('old','uncertain',?)",(timestamp(),))
        before=self.show(q,**selected)
        self.sessions.abandon_delivery(q,'old',self.pid,before['revision'],'abandon','人工放弃',**selected)
        self.assertFalse(self.engine.delivery('old','claim')['claimed']);self.assertEqual(self.seal(q,**selected)['state'],'sealed')
        with self.store.connection() as db:self.assertEqual(db.execute("SELECT state FROM deliveries WHERE turn_id='old'").fetchone()[0],'uncertain')

    def test_export10_has_lifecycle_no_credentials(self):
        self.seed();self.seal();path=Path(self.service.export_persona()['path']);value=json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(value['format_version'],10);self.assertEqual(len(value['session_packages']),1)
        self.assertNotIn('encrypted_key',path.read_text(encoding='utf-8'))

    async def test_manifest_failure_prevents_organization_call(self):
        self.seed();self.seal();p=self.organize.preview(self.sid,self.pid)
        task=self.organize.submit(p,self.pid,p['lifecycle_revision'],'manifest-failure')
        with patch.object(self.engine.cognitive,'reflection',side_effect=RuntimeErrorBase('context_manifest_storage_failed')):
            await self.org.run_next()
        self.assertEqual(self.model.calls,[]);self.assertEqual(self.org.show(task['id'])['status'],'failed')
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM organization_attempts').fetchone()[0],0)

    async def test_organization_publish_rechecks_nonfirst_source(self):
        self.seed();self.seal();p=self.organize.preview(self.sid,self.pid)
        task=self.organize.submit(p,self.pid,p['lifecycle_revision'],'withdraw-at-response')
        original=self.model.complete
        async def response(*args):
            result=await original(*args)
            with self.store.connection(write=True) as db:exclude_sources(db,self.pid,['seed-e2'],'withdrawn during call')
            return result
        with patch.object(self.model,'complete',side_effect=response):await self.org.run_next()
        self.assertEqual(self.org.show(task['id'])['status'],'stale')
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM candidates').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM summaries').fetchone()[0],0)
        self.assertEqual(len(self.model.calls),1)

    def test_service_session_creation_uses_same_authority(self):
        self.seed();old=self.sid;new=self.service.create_session()['id']
        self.assertNotEqual(new,old);self.assertEqual(self.show()['state'],'sealed')
        self.assertEqual(self.engine.session()['session_id'],new)

    def test_source_mutation_cannot_win_seal_cas(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        from persona_runtime.sessions import build_manifest
        from contextlib import contextmanager
        original=self.store.connection;ready=threading.Event();written=threading.Event()
        def writer():
            ready.wait(5)
            with original(write=True) as db:exclude_sources(db,self.pid,['seed-e2'],'race')
            written.set()
        @contextmanager
        def connection(*,write=False):
            with original(write=write) as db:yield db
            if not write and threading.current_thread() is threading.main_thread():
                ready.set();self.assertTrue(written.wait(5))
        self.seed();revision=self.show()['revision']
        with ThreadPoolExecutor(max_workers=1) as pool:
            job=pool.submit(writer)
            with patch.object(self.store,'connection',connection),self.assertRaisesRegex(RuntimeErrorBase,'version_conflict'):
                self.sessions.seal(self.sid,self.pid,revision,'race')
            job.result()
        self.assertEqual(self.show()['state'],'active')

    async def test_http_management_scope_permissions_and_zero_side_effect_preview(self):
        from aiohttp.test_utils import TestServer
        from aiohttp import ClientSession
        from persona_runtime.server import create_app
        self.seed();self.seal();app=create_app(self.paths,self.model)
        admin={'Authorization':'Bearer '+self.config.token()};bridge={'Authorization':'Bearer '+self.config.token('bridge')}
        def counts():
            with self.store.connection() as db:return [db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('events','attempts','organization_tasks','context_manifests')]
        before=counts()
        async with TestServer(app) as server,ClientSession() as client:
            for action in ('session.show','session.replay','session.organization.preview','session.triggers'):
                body={'action':action,'args':{'persona_id':self.pid,'session_id':self.sid,'channel':'desktop'}}
                async with client.post(server.make_url('/v1/manage'),headers=bridge,json=body) as r:self.assertEqual(r.status,403)
                async with client.post(server.make_url('/v1/manage'),headers=admin,json=body) as r:
                    result=await r.json();self.assertEqual(r.status,200,result)
            body={'action':'session.show','args':{'persona_id':self.pid,'session_id':self.sid,'channel':'qq','bot':'12345','peer':'67890'}}
            async with client.post(server.make_url('/v1/manage'),headers=admin,json=body) as r:self.assertEqual(r.status,400)
        self.assertEqual(counts(),before);self.assertEqual(self.model.calls,[])


class MigrationTests(unittest.TestCase):
    def test_all_versions_to13_and_repeat(self):
        schemas=[storage.SCHEMA,storage.SCHEMA2,storage.SCHEMA3,storage.SCHEMA4,storage.SCHEMA5,storage.SCHEMA6,storage.SCHEMA7,storage.SCHEMA8,storage.SCHEMA9,storage.SCHEMA10,storage.SCHEMA11,storage.SCHEMA12]
        for v in range(13):
            with self.subTest(version=v),tempfile.TemporaryDirectory(prefix='m73-migrate-',dir=RuntimePaths.from_environment().home/'tmp') as d:
                paths=RuntimePaths(Path(d));paths.initialize();store=storage.Store(paths)
                if v:
                    with closing(sqlite3.connect(store.path)) as db,db:
                        for schema in schemas[:v]:storage.execute_schema(db,schema)
                        db.execute('INSERT INTO users VALUES(?,?,?)',(LOCAL_USER_ID,'人工',timestamp()));db.execute(f'PRAGMA user_version={v}')
                store.initialize();store.initialize()
                with store.connection() as db:
                    self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],13)
                    self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok');self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())

    def test_schema12_failure_rolls_back(self):
        with tempfile.TemporaryDirectory(prefix='m73-rollback-',dir=RuntimePaths.from_environment().home/'tmp') as d:
            paths=RuntimePaths(Path(d));paths.initialize();store=storage.Store(paths)
            with closing(sqlite3.connect(store.path)) as db,db:
                for schema in (storage.SCHEMA,storage.SCHEMA2,storage.SCHEMA3,storage.SCHEMA4,storage.SCHEMA5,storage.SCHEMA6,storage.SCHEMA7,storage.SCHEMA8,storage.SCHEMA9,storage.SCHEMA10,storage.SCHEMA11,storage.SCHEMA12):storage.execute_schema(db,schema)
                db.execute('INSERT INTO users VALUES(?,?,?)',(LOCAL_USER_ID,'人工',timestamp()));db.execute('PRAGMA user_version=12')
            real=storage.execute_schema
            def fail(conn,schema):
                real(conn,schema)
                if schema==storage.SCHEMA13:raise sqlite3.OperationalError('transaction fault')
            with patch('persona_runtime.storage.execute_schema',side_effect=fail),self.assertRaises(sqlite3.Error):store.initialize()
            with closing(sqlite3.connect(store.path)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],12)
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='session_lifecycle'").fetchone())
            store.initialize()
