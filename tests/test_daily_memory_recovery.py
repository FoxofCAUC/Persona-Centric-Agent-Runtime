import asyncio
from datetime import datetime,timezone
import json
import threading
import time
import unittest
from unittest.mock import patch

from persona_runtime.automatic_memory import AutomaticMemory
from persona_runtime.character_runtime import PreparationCoordinator
from persona_runtime.chat import ModelError
from persona_runtime.import_history_service import HistoryService
from persona_runtime.models import identifier
from tests import test_m8d as memory,test_import_history_memory as history,test_desktop_recovery as desktop


class NetworkRecovery(unittest.IsolatedAsyncioTestCase):
    setUp=desktop.DesktopRecoveryTests.setUp
    reopen=desktop.DesktopRecoveryTests.reopen
    asyncTearDown=desktop.DesktopRecoveryTests.asyncTearDown

    async def test_fifo_after_repeated_unsent_and_credential_change(self):
        from persona_runtime.scene_policy import read,save
        from persona_runtime.generation_retry import call_count
        self.config.update({'model':{'api_key':'702'}})
        async def offline(*args):
            raise ModelError('provider_network_error',diagnostics={'request_headers_sent':False,'response_headers_received':False})
        with patch.object(self.model,'complete',side_effect=offline):
            first=(await self.chat.submit('desktop','712',identifier(),self.sid))['id']
            await self.chat.wait()
            second=(await self.chat.submit('desktop','713',identifier(),self.sid))['id']
            await self.chat.wait()
            for _ in range(5):
                with self.store.connection(write=True) as db:
                    wait=read(db,'desktop.network.'+first)
                    save(db,'desktop.network.'+first,dict(wait,due=0))
                self.ingress.schedule_committed()
                await self.chat.wait()
        with self.store.connection() as db:
            attempts=db.execute("SELECT usage_json FROM attempts WHERE turn_id=?",(first,)).fetchall()
            self.assertEqual(len(attempts),1)
            self.assertEqual(json.loads(attempts[0][0])['generation_retry']['connection_wait']['count'],6)
            self.assertEqual(call_count(db,'attempts',0),0)
            self.assertFalse(db.execute("SELECT 1 FROM attempts WHERE turn_id=?",(second,)).fetchone())
            self.assertFalse(db.execute('SELECT 1 FROM model_call_meter').fetchone())
        self.config.update({'model':{'api_key':'703'}})
        with self.store.connection(write=True) as db:
            wait=read(db,'desktop.network.'+first)
            save(db,'desktop.network.'+first,dict(wait,due=0))
        self.ingress.schedule_committed();await self.chat.wait()
        self.assertEqual(self.chat.get(first)['error_code'],'desktop_connection_changed')
        self.assertEqual(self.model.calls,0)


class DailyMemory(unittest.IsolatedAsyncioTestCase):
    setUp=memory.MemoryTests.setUp
    asyncTearDown=memory.MemoryTests.asyncTearDown
    seed=memory.MemoryTests.seed
    enable=memory.MemoryTests.enable
    model_for_memory=memory.MemoryTests.install_model

    async def test_next_day_scanner_resumes_at_prior_page(self):
        from persona_runtime.automatic_memory_schedule import scan_sessions,advance
        from persona_runtime.models import LOCAL_USER_ID
        self.enable()
        sid=self.chat.session(persona_id=self.persona['id'])['session_id']
        with self.store.connection(write=True) as db:
            for i in range(72):
                key=str(700000+i)
                db.execute("""INSERT INTO events(id,session_id,persona_id,user_id,role,text,created_at)
                    VALUES(?,?,?,?,'user',?,'2026-11-18')""",('e'+key,sid,self.persona['id'],LOCAL_USER_ID,key))
                db.execute("""INSERT INTO turns(id,source_key,persona_id,session_id,channel,user_event_id,status,
                    model_snapshot,created_at,updated_at) VALUES(?,?,?,?,'desktop',?,'completed','{}','2026-11-18','2026-11-18')""",
                    (key,key,self.persona['id'],sid,'e'+key))
            policy=self.org.managed.policy(db,self.persona['id'])['data']
            first=scan_sessions(db,self.persona['id'],policy,self.org.model())[0]
            for key,number in first[2].items():advance(db,key,number)
        from persona_runtime.storage import Store
        with Store(self.paths).connection() as db:following=scan_sessions(db,self.persona['id'],policy,self.org.model())[0]
        self.assertEqual(len(first[1]),16);self.assertEqual(len(following[1]),16)
        self.assertFalse(first[1] & following[1])

    async def test_paused_legacy_cycle_does_not_block_another_persona(self):
        from persona_runtime.models import PersonaSpec
        first=self.persona['id'];other=self.service.create_persona(PersonaSpec(1,'912','912',['912'],'912',['912'],'912'))['id']
        with self.store.connection(write=True) as db:
            db.execute("UPDATE daily_updates SET persona_id=?,status='paused_model',payload_json=? WHERE id='daily'",
                       (first,json.dumps({'last_task':None,'index':1})))
        self.service.use_persona(other)
        self.org.schedule.daily_tick(time.time()+86401)
        self.assertEqual(self.org.schedule.status()['persona_id'],other)
        self.assertNotEqual(self.org.schedule.status()['status'],'paused_model')
        self.service.use_persona(first)
        self.org.schedule.daily_tick(time.time()+86402)
        self.assertEqual(self.org.schedule.status()['status'],'paused_model')
        self.assertEqual(self.org.schedule.status()['progress']['index'],1)

    async def test_continuous_foreground_and_restart_do_not_starve_aged_source(self):
        self.enable()
        model=self.model_for_memory();rows=await self.seed(3)
        with patch.object(self.chat,'_schedule'):
            active=await self.chat.submit('desktop','7781',identifier())
        self.org.automatic.tick()
        self.assertFalse(self.org.automatic.coverage()['batches'])
        now=time.time()+601
        self.org.managed.clock=lambda:now
        with self.store.connection(write=True) as db:
            db.execute("UPDATE turns SET updated_at=? WHERE id=?",(datetime.fromtimestamp(now,timezone.utc).isoformat(),active['id']))
        self.org.automatic=AutomaticMemory(self.org)
        self.org.automatic.tick()
        await self.org.managed.run_next('background')
        self.org.automatic=AutomaticMemory(self.org)
        self.org.automatic.tick()
        await self.org.managed.run_next('background')
        self.assertEqual(len(model.calls),2)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM memory_coverage WHERE disposition='processed'").fetchone()[0],len(rows))
            self.assertEqual(db.execute("SELECT status FROM turns WHERE id=?",(active['id'],)).fetchone()[0],'queued')

    async def test_settings_change_rebinds_unsent_task_but_not_different_account(self):
        self.enable();self.model_for_memory();await self.seed()
        self.cfg.update({'organization':{'model':{'api_key':'711'}}})
        self.org.schedule.set_automatic(True)
        self.org.automatic.tick(force=True)
        self.cfg.update({'organization':{'model':{'timeout':19,'api_key':'711'}}})
        self.org.schedule.pause_changed_models()
        self.assertTrue(self.org.schedule.status()['automatic']['enabled'])
        await self.org.run_next()
        self.assertEqual(len(self.org.client.calls),1)
        with self.store.connection() as db:
            self.assertEqual(json.loads(db.execute('SELECT model_snapshot FROM organization_attempts').fetchone()[0])['timeout'],19)
        self.org.automatic.tick(force=True)
        self.cfg.update({'organization':{'model':{'api_key':'712'}}})
        await self.org.run_next()
        self.assertEqual(len(self.org.client.calls),1)
        with self.store.connection() as db:
            self.assertTrue(db.execute("SELECT 1 FROM organization_tasks WHERE error_code='organization_account_changed'").fetchone())
        self.cfg.update({'organization':{'model':{'api_key':'711'}}})
        await memory.MemoryTests.finish(self)
        self.assertEqual(len(self.org.client.calls),2)

    async def test_background_network_recovery_retains_one_paid_extraction_and_review(self):
        self.enable();model=self.model_for_memory();await self.seed()
        self.org.automatic.tick(force=True)
        now=time.time();self.org.managed.clock=lambda:now
        async def offline(*args):
            raise ModelError('provider_network_error',diagnostics={'request_headers_sent':False,'response_headers_received':False})
        with patch.object(model,'complete',side_effect=offline):
            for _ in range(5):
                await self.org.run_next();now+=61
                self.org.recover()
        await memory.MemoryTests.finish(self)
        self.assertEqual(len(model.calls),2)
        from persona_runtime.generation_retry import call_count
        with self.store.connection() as db:
            self.assertEqual(call_count(db,'organization_attempts',0),2)
            self.assertEqual(db.execute("SELECT count(*) FROM organization_attempts WHERE error_code='organization_network_wait'").fetchone()[0],5)

    async def test_background_probe_cannot_consume_an_attempt_while_route_is_down(self):
        from unittest.mock import AsyncMock
        self.enable();model=self.model_for_memory();await self.seed()
        model.route_reachable=AsyncMock(side_effect=[False,True])
        self.org.automatic.tick(force=True)
        now=time.time();self.org.managed.clock=lambda:now
        with patch.object(model,'complete',side_effect=ModelError('provider_network_error',diagnostics={'request_headers_sent':False})):
            await self.org.run_next()
        now+=65;self.org.recover()
        await self.org.managed.check_network()
        self.assertFalse(await self.org.run_next())
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM organization_attempts").fetchone()[0],1)
        now+=65
        await self.org.managed.check_network()
        await memory.MemoryTests.finish(self)
        self.assertEqual(model.route_reachable.await_count,2)
        self.assertEqual(len(model.calls),2)


class SourceState(unittest.IsolatedAsyncioTestCase):
    setUp=history.HistoryMemoryTests.setUp
    asyncTearDown=history.HistoryMemoryTests.asyncTearDown

    async def test_failure_updates_imported_source_and_does_not_dispatch_again(self):
        sid=history.HistoryMemoryTests.restore(self)
        self.org.automatic.tick(force=True)
        with self.store.connection(write=True) as db:
            db.execute("UPDATE organization_tasks SET status='failed' WHERE status='queued'")
        self.org.automatic.tick(force=True)
        with self.store.connection() as db:
            self.assertEqual(HistoryService._memory_state(db,sid),'failed')
            self.assertEqual({r[0] for r in db.execute('SELECT disposition FROM memory_coverage')},{'failed'})
        self.assertFalse(await self.org.run_next())
        self.assertFalse(self.org.client.calls)


class IndexRecovery(unittest.TestCase):
    def test_transient_lock_and_full_queue_recover_without_manual_prepare(self):
        now=[0.0];entered=threading.Event();release=threading.Event();attempts=[]
        def build(value):
            attempts.append(value)
            if value=='1' and attempts.count('1')==1:
                entered.set();release.wait(5);raise PermissionError()
        c=PreparationCoordinator(build,clock=lambda:now[0])
        self.addCleanup(c.close);self.addCleanup(release.set)
        c.submit('1',('1',));self.assertTrue(entered.wait(2))
        for i in range(32):c.submit('k'+str(i),(str(20+i),))
        self.assertEqual(c.submit('2',('2',))['state'],'queue_full')
        release.set()
        with c.condition:self.assertTrue(c.condition.wait_for(lambda:c.running is None and not c.pending,4))
        self.assertEqual(c.submit('1',('1',))['state'],'failed')
        now[0]=3
        c.submit('1',('1',));c.submit('2',('2',))
        with c.condition:self.assertTrue(c.condition.wait_for(lambda:c.running is None and not c.pending,4))
        self.assertEqual((c.status('1')['state'],c.status('2')['state']),('ready','ready'))
        self.assertEqual((attempts.count('1'),attempts.count('2')),(2,1))
