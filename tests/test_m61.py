"""M6.1 durable input, timing, snapshots, evidence and API contracts."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from persona_runtime.config import ConfigStore
from persona_runtime.chat import ChatEngine
from persona_runtime.service import RuntimeService
from persona_runtime.models import generic_persona,identifier,RuntimeErrorBase
from persona_runtime.ingress import IngressService
from persona_runtime.character import CharacterService,empty_character
from persona_runtime.turn_input import load_turn_input
from persona_runtime.organization import eligible_turns,OrganizationEngine
from persona_runtime.memory_policy import exclude_sources
from persona_runtime.server import create_app

class Fake:
    def __init__(self):self.messages=[];self.gate=None
    async def complete(self,model,messages):
        self.messages.append(messages)
        if self.gate:await self.gate.wait()
        return '模拟完整回复',{'total_tokens':1}

class IngressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root=RuntimePaths.from_environment().home/'tmp';root.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(prefix='m61-',dir=root);self.addCleanup(self.tmp.cleanup)
        self.paths=RuntimePaths(Path(self.tmp.name));self.store=Store(self.paths);self.store.initialize()
        self.config=ConfigStore(self.paths);self.config.initialize();self.config.update({'model':{'base_url':'http://localhost:9/v1','name':'mock'}})
        self.service=RuntimeService(self.store);self.pid=self.service.create_persona(generic_persona())['id'];self.service.use_persona(self.pid)
        self.fake=Fake();self.engine=ChatEngine(self.store,self.config,self.fake);self.now=0.;self.epoch=100000.
        self.ingress=IngressService(self.engine,lambda:self.now,lambda:self.epoch+self.now)
        self.addAsyncCleanup(self.finish)
    async def finish(self):
        if self.fake.gate:self.fake.gate.set()
        await self.engine.wait()
    def natural(self):
        p=self.ingress.current_policy(self.pid,'desktop')
        return self.ingress.update_policy(self.pid,'desktop','natural_batch',p['id'],identifier())
    def accept(self,text='第一条',rid=None,**kw):
        return self.ingress.accept({'text':text,'request_id':rid or identifier(),'persona_id':self.pid,**kw})
    def batch(self,r):return self.ingress.get(r['batch_id'])
    def seal(self,r):
        b=self.batch(r);return self.ingress.action(b['id'],'seal',b['revision'],identifier(),self.pid)
    async def test_legacy_single_and_old_v1(self):
        r=self.accept('  A\n😀  ');await self.engine.wait()
        self.assertIsNotNone(r['turn_id']);self.assertEqual(self.engine.get(r['turn_id'])['text'],'  A\n😀  ')
        old=await self.engine.submit('desktop','旧API',identifier());await self.engine.wait()
        self.assertIsNone(self.engine.get(old['id'])['input_batch_id'])
        with self.store.connection() as c:self.assertEqual(c.execute('SELECT count(*) FROM ingress_receipts').fetchone()[0],1)
    async def test_three_members_one_call_deadline(self):
        self.natural();a=self.accept('甲');self.now=1;b=self.accept('乙');self.now=3;c=self.accept('丙')
        self.assertEqual(a['batch_id'],c['batch_id']);self.now=7.99;self.ingress.tick();self.assertEqual(self.fake.messages,[])
        self.now=8.;self.ingress.tick();await self.engine.wait();r=self.ingress.lookup('desktop',a.get('request_id','none'))
        batch=self.batch(a);turn=self.engine.get(batch['turn_id'])
        self.assertEqual([x['text'] for x in turn['input_members']],['甲','乙','丙']);self.assertEqual(len(self.fake.messages),1)
        for text in ('甲','乙','丙'):self.assertIn(text,self.fake.messages[0][-1]['content'])
        self.assertEqual(len(turn['context']['input_event_ids']),3)
    async def test_max_wait_and_duplicate_no_extension(self):
        self.natural();a=self.accept(rid='stable');self.now=4;self.accept('2');self.now=8;self.accept('3');self.now=12;self.accept('4')
        before=self.batch(a)['revision'];self.now=14;duplicate=self.accept(rid='stable');self.assertEqual(duplicate['receipt_id'],a['receipt_id']);self.assertEqual(self.batch(a)['revision'],before)
        self.now=15;self.ingress.tick();await self.engine.wait();self.assertIsNotNone(self.batch(a)['turn_id'])
    async def test_conflict_no_mutation(self):
        self.natural();r=self.accept('original','id')
        with self.assertRaisesRegex(RuntimeErrorBase,'submission_conflict'):self.accept('different','id')
        self.assertEqual([m['text'] for m in self.batch(r)['members']],['original'])
    async def test_seal_cas_idempotent(self):
        self.natural();r=self.accept();b=self.batch(r);op=identifier()
        a=self.ingress.action(b['id'],'seal',b['revision'],op,self.pid)
        self.assertEqual(self.ingress.action(b['id'],'seal',b['revision'],op,self.pid),a)
        with self.assertRaisesRegex(RuntimeErrorBase,'version_conflict'):self.ingress.action(b['id'],'seal',b['revision'],identifier(),self.pid)
        await self.engine.wait();self.assertEqual(len(self.fake.messages),1)
    async def test_deadline_vs_append(self):
        self.natural();a=self.accept('before');self.now=5;b=self.accept('after')
        self.assertNotEqual(a['batch_id'],b['batch_id']);self.seal(b);await self.engine.wait();self.assertEqual(len(self.fake.messages),2)
    async def test_recover_no_calls_resume(self):
        self.natural();r=self.accept();self.ingress.recover();self.now=99;self.ingress.tick();await self.engine.wait()
        self.assertEqual(self.fake.messages,[]);b=self.batch(r);self.assertEqual(b['state'],'interrupted_buffer')
        self.ingress.action(b['id'],'resume',b['revision'],identifier(),self.pid);await self.engine.wait();self.assertEqual(len(self.fake.messages),1)
    async def test_cancel_preserves_and_excludes(self):
        self.natural();r=self.accept();b=self.batch(r);self.ingress.action(b['id'],'cancel',b['revision'],identifier(),self.pid)
        with self.store.connection() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM events').fetchone()[0],1);self.assertEqual(eligible_turns(c,r['session_id']),[])
        self.assertEqual(self.fake.messages,[])
    async def test_capacity_and_long_single(self):
        self.natural();a=self.accept('a'*11999);b=self.accept('bb');self.assertNotEqual(a['batch_id'],b['batch_id']);self.seal(b)
        c=self.accept('x'*24000);self.assertIsNotNone(c['turn_id']);await self.engine.wait()
        self.assertEqual(len(self.batch(c)['members'][0]['text']),24000)
        self.assertEqual(self.engine.get(c['turn_id'])['error_code'],'context_limit_exceeded')
    async def test_twenty_capacity(self):
        self.natural();rows=[self.accept(str(i)) for i in range(20)]
        self.assertEqual(len({r['batch_id'] for r in rows}),1);self.assertIsNotNone(self.batch(rows[0])['turn_id']);await self.engine.wait()
    async def test_frozen_character_and_model_at_seal(self):
        self.natural();a=self.accept('你多高');ch=CharacterService(self.store);v=ch.current(self.pid);d=deepcopy(v['data']);d['appearance']['height_cm']=162;ch.update(self.pid,d,v['id'],identifier())
        b=self.accept('你多高');self.assertNotEqual(a['batch_id'],b['batch_id'])
        self.config.update({'model':{'name':'second'}});self.seal(b);await self.engine.wait()
        with self.store.connection() as c:
            turns=[dict(r) for r in c.execute('SELECT * FROM turns ORDER BY rowid')]
        self.assertNotEqual(json.loads(turns[0]['character_snapshot'])['id'],json.loads(turns[1]['character_snapshot'])['id']);self.assertEqual(json.loads(turns[1]['model_snapshot'])['name'],'second')
    async def test_input_during_generation_next_batch(self):
        self.natural();self.fake.gate=asyncio.Event();a=self.accept('first');self.seal(a);await asyncio.sleep(.03)
        b=self.accept('second');self.assertNotEqual(a['batch_id'],b['batch_id']);self.seal(b);self.fake.gate.set();await self.engine.wait();self.assertEqual(len(self.fake.messages),2)
    async def test_policy_resolution_and_selection(self):
        p=self.natural();a=self.accept()
        with self.assertRaisesRegex(RuntimeErrorBase,'pending_batch_resolution_required'):self.ingress.update_policy(self.pid,'desktop','legacy_per_message',p['id'],identifier())
        self.ingress.update_policy(self.pid,'desktop','legacy_per_message',p['id'],identifier(),'cancel')
        self.assertEqual(self.batch(a)['state'],'cancelled');self.assertIsNotNone(self.accept()['turn_id'])
    async def test_nonfirst_sources_organization_and_retraction(self):
        self.natural();a=self.accept('首条');self.accept('非首条特殊事实');self.seal(a);await self.engine.wait();b=self.batch(a)
        org=OrganizationEngine(self.store,self.config,self.fake,self.engine)
        preview=org.preview(a['session_id']);self.assertIn('非首条特殊事实',[e['text'] for e in preview['events']])
        with self.store.connection(write=True) as c:exclude_sources(c,self.pid,[b['members'][1]['event_id']],'test')
        with self.store.connection() as c:
            self.assertEqual(eligible_turns(c,a['session_id']),[])
            for m in b['members']:self.assertIsNotNone(c.execute('SELECT 1 FROM source_exclusions WHERE event_id=?',(m['event_id'],)).fetchone())
    async def test_atomic_storage_failure(self):
        self.natural()
        with self.store.connection(write=True) as c:c.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON ingress_receipts BEGIN SELECT RAISE(ABORT,'disk simulation'); END")
        with self.assertRaises(sqlite3.Error):self.accept()
        with self.store.connection() as c:
            for t in ('events','raw_events','input_batches','ingress_receipts'):self.assertEqual(c.execute('SELECT count(*) FROM '+t).fetchone()[0],0)
        self.assertEqual(self.fake.messages,[])
    async def test_export_native_character_separation(self):
        self.natural();self.accept();full=json.loads(Path(self.service.export_persona()['path']).read_text(encoding='utf-8'))
        self.assertEqual(full['format_version'],7);self.assertEqual(len(full['ingress_receipts']),1);self.assertNotIn('model_snapshot',full['input_batches'][0])
    async def test_v1_v2_no_replay(self):
        self.accept(rid='v2')
        with self.assertRaisesRegex(RuntimeErrorBase,'v2_submission_exists'):await self.engine.submit('desktop','第一条','v2')
        await self.engine.submit('desktop','old','v1')
        with self.assertRaisesRegex(RuntimeErrorBase,'legacy_submission_exists'):self.accept('old','v1')
    async def test_limits_and_unknown_metadata(self):
        with self.assertRaises(RuntimeErrorBase):self.accept('x'*24001)
        with self.assertRaises(RuntimeErrorBase):self.accept(secret='credential')
        self.engine.draining=True
        with self.assertRaisesRegex(RuntimeErrorBase,'service_draining'):self.accept()
    async def test_qq_source_identity(self):
        self.config.update({'bot_qq':'10001','owner_qq':'10002'})
        p={'channel':'qq','bot':'10001','sender':'10002','platform_message_id':'55','text':'qq','request_id':'qq55'}
        a=self.ingress.accept(p);self.assertEqual(self.ingress.accept(p)['receipt_id'],a['receipt_id'])
        with self.assertRaisesRegex(RuntimeErrorBase,'qq_identity_conflict'):self.ingress.accept(p|{'sender':'20002'})
        with self.store.connection() as c:self.assertIn('55',c.execute('SELECT source_key FROM ingress_receipts').fetchone()[0])
    async def test_api_permissions_and_capabilities(self):
        app=create_app(self.paths,self.fake)
        async with TestServer(app) as server,ClientSession() as session:
            admin={'Authorization':'Bearer '+self.config.token()};bridge={'Authorization':'Bearer '+self.config.token('bridge')}
            url=lambda path:str(server.make_url(path))
            async with session.get(url('/v1/health'),headers=admin) as r:self.assertIn('durable_ingress_v2',(await r.json())['capabilities'])
            async with session.post(url('/v2/ingress'),headers=admin,json={'text':'api','request_id':'api'}) as r:receipt=await r.json();self.assertEqual(r.status,200,receipt)
            async with session.get(url('/v2/batches/'+receipt['batch_id']),headers=bridge) as r:self.assertEqual(r.status,403)
            async with session.post(url('/v2/batches/'+receipt['batch_id']+'/cancel'),headers=bridge,json={}) as r:self.assertEqual(r.status,403)

    async def test_queue_capacity_wait_and_drain_guard(self):
        self.fake.gate=asyncio.Event()
        await self.engine.submit('desktop','running',identifier());await asyncio.sleep(.02)
        for i in range(10):await self.engine.submit('desktop',str(i),identifier())
        self.natural();a=self.accept('buffer');b=self.seal(a)
        self.assertEqual(b['state'],'sealed_waiting_capacity');self.assertIsNone(b['turn_id'])
        self.fake.gate.set();await self.engine.wait();self.ingress.tick();await self.engine.wait()
        self.assertIsNotNone(self.batch(a)['turn_id']);self.assertEqual(len(self.fake.messages),12)

    async def test_pending_count_and_character_limits(self):
        self.natural()
        for i in range(10):self.seal(self.accept(str(i)))
        with self.assertRaisesRegex(RuntimeErrorBase,'buffer_full'):self.accept('eleventh')
        await self.engine.wait()
        for i in range(5):self.accept('x'*24000)
        with self.assertRaisesRegex(RuntimeErrorBase,'buffer_full'):self.accept('x')

    async def test_retry_keeps_members_current_version_and_prior_context(self):
        from persona_runtime.chat import ModelError
        self.natural();a=self.accept('first');self.accept('second');b=self.seal(a)
        async def fail(*args):raise ModelError('synthetic_failure')
        with patch.object(self.fake,'complete',side_effect=fail):await self.engine.wait()
        tid=b['turn_id'];prior=self.engine.get(tid)['context'];v=CharacterService(self.store).current(self.pid)
        d=deepcopy(v['data']);d['appearance']['height_cm']=180;CharacterService(self.store).update(self.pid,d,v['id'],identifier())
        await self.engine.retry(tid);await self.engine.wait();current=self.engine.get(tid)
        self.assertEqual([m['text'] for m in current['input_members']],['first','second'])
        with self.store.connection() as c:self.assertEqual(c.execute('SELECT count(*) FROM context_uses WHERE turn_id=?',(tid,)).fetchone()[0],2)
        self.assertEqual(prior['input_event_ids'],current['context']['input_event_ids'])

    async def test_schema7_upgrade_rollback_and_preservation(self):
        from persona_runtime.storage import SCHEMA,SCHEMA2,SCHEMA3,SCHEMA4,SCHEMA5,SCHEMA6,SCHEMA7,execute_schema,SCHEMA8
        paths=RuntimePaths(self.paths.home/'v7');paths.initialize();store=Store(paths)
        c=sqlite3.connect(store.path)
        for sql in (SCHEMA,SCHEMA2,SCHEMA3,SCHEMA4,SCHEMA5,SCHEMA6,SCHEMA7):execute_schema(c,sql)
        c.execute('PRAGMA user_version=7');c.commit();c.close()
        def injected(conn,sql):
            execute_schema(conn,sql)
            if sql==SCHEMA8:raise RuntimeError('injected post-DDL failure')
        with patch('persona_runtime.storage.execute_schema',side_effect=injected):
            with self.assertRaises(RuntimeError):store.initialize()
        c=sqlite3.connect(store.path);self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],7)
        self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='ingress_receipts'").fetchone());c.close()
        store.initialize();store.initialize()
        with store.connection() as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],10);self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(c.execute('SELECT count(*) FROM ingress_receipts').fetchone()[0],0)

    async def test_server_stop_rejects_unresolved_and_no_bridge_management(self):
        app=create_app(self.paths,self.fake);p=app['ingress'].current_policy(self.pid,'desktop')
        app['ingress'].update_policy(self.pid,'desktop','natural_batch',p['id'],identifier())
        async with TestServer(app) as server,ClientSession() as session:
            admin={'Authorization':'Bearer '+self.config.token()};bridge={'Authorization':'Bearer '+self.config.token('bridge')}
            async with session.post(server.make_url('/v2/ingress'),headers=admin,json={'text':'pending','request_id':'stop'}) as r:self.assertEqual(r.status,200)
            async with session.post(server.make_url('/v1/control/stop'),headers=admin,json={}) as r:self.assertEqual((await r.json())['error'],'unresolved_input_batches')
            self.assertFalse(app['engine'].draining)
            async with session.post(server.make_url('/v1/manage'),headers=bridge,json={'action':'interaction.current','args':{'persona_id':self.pid,'channel':'desktop'}}) as r:self.assertEqual(r.status,403)
