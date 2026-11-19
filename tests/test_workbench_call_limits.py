import asyncio
import unittest
from unittest.mock import patch

from persona_runtime import call_limits
from persona_runtime.config import atomic_json
from persona_runtime.models import identifier
from persona_runtime.organization import OrganizationEngine
from persona_runtime.quotas import QuotaService,FIELDS
from tests import test_desktop_recovery as desktop
from tests import test_m9r_native_retrieval as native, test_m8d as memory


async def until(predicate):
    async with asyncio.timeout(12):
        while not predicate():await asyncio.sleep(.04)


class Limits(unittest.IsolatedAsyncioTestCase):
    setUp=desktop.DesktopRecoveryTests.setUp
    reopen=desktop.DesktopRecoveryTests.reopen

    async def asyncTearDown(self):
        for task in tuple(self.chat.tasks.values()):task.cancel()
        await self.chat.wait()

    def waits(self):
        with self.store.connection() as db:return call_limits.pending(db)

    def limits(self,enabled=True,hourly=1,daily=0):
        return QuotaService(self.store,self.config).configure(enabled,hourly,daily,dict.fromkeys(FIELDS,0))

    async def send(self):
        return (await self.chat.submit('desktop','904',identifier(),self.sid))['id']

    async def test_consent_releases_chat_and_background_but_next_message_waits(self):
        self.limits()
        await self.send();await self.chat.wait()
        tid=await self.send()
        await until(self.waits)
        self.config.update({'organization':{'reuse_chat':True}})
        org=OrganizationEngine(self.store,self.config,self.model,self.chat)
        job=org.test_model()['id']
        await org.managed.run_next('background')
        self.assertEqual(org.show(job)['status'],'waiting_quota')
        ids=[r['id'] for r in self.waits()]
        self.assertEqual(len(ids),2)
        call_limits.decide(self.store,ids,False)
        self.assertEqual({r['state'] for r in self.waits()},{'deferred'})
        self.assertEqual(self.model.calls,1)
        call_limits.decide(self.store,ids,True)
        await org.managed.run_next('background')
        await asyncio.wait_for(self.chat.wait(),8)
        self.assertEqual(self.chat.get(tid)['status'],'completed')
        self.assertEqual(org.show(job)['status'],'completed')
        self.assertEqual(self.model.calls,3)
        next_id=await self.send();await until(self.waits)
        call_limits.decide(self.store,ids,True)
        self.assertEqual(self.model.calls,3)
        self.limits(False)
        await asyncio.wait_for(self.chat.wait(),8)
        self.assertEqual(self.chat.get(next_id)['status'],'completed')
        self.assertEqual(self.model.calls,4)

    async def test_atomic_last_position_natural_window_and_new_defaults(self):
        service=QuotaService(self.store,self.config)
        self.assertFalse(service.desktop_settings()['enabled'])
        self.assertEqual(set(service.current()['limits'].values()),{0})
        self.limits(daily=1)
        def compete(scope):
            with self.store.connection(write=True) as db:
                try:call_limits.take(db,self.config,scope,'main',90000)
                except call_limits.CallLimitPending:return False
                return True
        self.assertEqual(sum(await asyncio.gather(*(asyncio.to_thread(compete,str(i)) for i in range(8)))),1)
        with self.store.connection(write=True) as db:
            with self.assertRaises(call_limits.CallLimitPending) as stopped:
                call_limits.take(db,self.config,'931','background',93601)
            self.assertEqual(stopped.exception.reason,'daily')
            call_limits.take(db,self.config,'932','background',176401)
        set_old=self.config.load()
        del set_old['call_limits']
        set_old['organization'].update(hourly_limit=60,daily_limit=240)
        set_old['model']['hourly_limit']=300
        set_old['character_selector'].update(hourly_limit=3,daily_limit=4)
        atomic_json(self.config.file,set_old)
        self.assertEqual(service._limits(),dict(project_hourly=0,background_hourly=0,background_daily=0,selector_hourly=3,selector_daily=4))

    async def test_restarting_before_dispatch_preserves_original_wait_and_receipt(self):
        self.limits()
        await self.send();await self.chat.wait()
        self.ingress.accept(self.payload);self.now+=6;self.ingress.tick()
        await until(self.waits)
        wait_id=self.waits()[0]['id']
        with self.store.connection() as db:
            turn=db.execute("SELECT id FROM turns WHERE input_batch_id IS NOT NULL").fetchone()[0]
            self.assertFalse(db.execute("SELECT 1 FROM attempts WHERE turn_id=?",(turn,)).fetchone())
        call_limits.draining(self.store,True)
        await asyncio.wait_for(self.chat.wait(),4)
        self.reopen();self.chat.recover();self.ingress.recover()
        call_limits.draining(self.store,False)
        self.ingress.tick()
        self.assertEqual(self.waits()[0]['id'],wait_id)
        call_limits.decide(self.store,[wait_id],True)
        await asyncio.wait_for(self.chat.wait(),8)
        self.assertEqual(self.chat.get(turn)['status'],'completed')
        self.assertEqual(self.model.calls,2)

    def test_tk_dialog_submits_only_the_displayed_waits(self):
        from types import SimpleNamespace
        from persona_runtime.desktop_quotas import QuotaUI
        with self.store.connection(write=True) as db:call_limits.park(db,'935','hourly',1)
        def manage(action,*,ids,allow):
            self.assertEqual(action,'quota.workflow.decide')
            return call_limits.decide(self.store,ids,allow)
        def run_task(label,resources,work,done,**opts):
            done(work(None));return True
        host=SimpleNamespace(closed=False,shutdown_started=False,tray_hidden=False,quota_waits=self.waits(),
                client=SimpleNamespace(manage=manage),run_task=run_task,refresh_quotas=lambda:None)
        with patch('tkinter.messagebox.askyesno',side_effect=[False,True]) as ask:
            QuotaUI.prompt_quota_waits(host)
            host.quota_waits=self.waits()
            self.assertEqual(host.quota_waits[0]['state'],'deferred')
            QuotaUI.prompt_quota_waits(host)
            QuotaUI.prompt_quota_waits(host,True)
            self.assertEqual(ask.call_count,2)
        self.assertFalse(self.waits())


class Scheduling(unittest.IsolatedAsyncioTestCase):
    setUp=native.Integration.setUp
    asyncTearDown=native.Integration.asyncTearDown
    enable=native.Integration.enable
    ask=native.Integration.ask
    decision_transport=native.Integration.decision_transport
    transport=native.Integration.transport

    async def test_one_grant_finishes_retrieval_and_main_after_router_exhausts_limit(self):
        self.enable()
        QuotaService(self.store,self.cfg).configure(True,1,0,dict.fromkeys(FIELDS,0))
        prompt=asyncio.create_task(self.ask())
        def waits():
            with self.store.connection() as db:return call_limits.pending(db)
        await until(waits)
        self.assertEqual(len(self.decisions),1)
        self.assertFalse(self.main.calls)
        call_limits.decide(self.store,[r['id'] for r in waits()],True)
        result=await asyncio.wait_for(prompt,15)
        self.assertEqual(result['status'],'completed')
        self.assertEqual((len(self.decisions),len(self.provider.calls),len(self.main.calls)),(1,1,1))
        self.assertFalse(waits())

    async def test_full_background_queue_and_inflight_call_leave_chat_a_lease(self):
        self.enable()
        held=asyncio.Event()
        entered=asyncio.Event()
        provider=self.provider.complete
        async def transport(model,messages):
            if model.get('_native_web_profile'):return await provider(model,messages)
            entered.set();await held.wait()
            from persona_runtime.chat import ModelCompletion
            return ModelCompletion('908',{}, {})
        self.provider.complete=transport
        self.org.foreground_only=False
        self.worker.cancel();await asyncio.gather(self.worker,return_exceptions=True)
        first=self.org.test_model()['id']
        running=asyncio.create_task(self.org.managed.run_next('background'))
        try:
            await asyncio.wait_for(entered.wait(),5)
            for _ in range(8):self.org.test_model()
            with self.assertRaisesRegex(Exception,'queue_full'):self.org.test_model()
            self.org.foreground_only=True
            self.worker=asyncio.create_task(self.org.worker())
            turn=await self.ask()
            self.assertEqual(turn['status'],'completed')
            self.assertEqual(self.org.show(first)['status'],'running')
            self.assertTrue(self.decisions and self.provider.calls)
        finally:
            held.set();await running

    async def test_broken_maintenance_components_do_not_stop_healthy_workers(self):
        self.enable()
        self.worker.cancel();await asyncio.gather(self.worker,return_exceptions=True)
        self.org.foreground_only=False
        with patch.object(self.org.knowledge,'tick',side_effect=RuntimeError()), \
                patch.object(self.org.automatic,'tick',side_effect=RuntimeError()), \
                patch('persona_runtime.recall_runtime.RecallRuntime.maintain',side_effect=RuntimeError()), \
                patch('logging.Logger.warning',side_effect=OSError()):
            self.worker=asyncio.create_task(self.org.worker())
            turn=await self.ask()
            self.assertEqual(turn['status'],'completed')
            for name in ('knowledge','memory','index'):
                self.assertEqual(self.org.maintenance_status[name]['state'],'retrying')
                self.assertLess(self.org.maintenance_status[name]['failures'],5)
            self.assertEqual(self.org.maintenance_status['daily']['state'],'ready')

    async def test_single_execution_slot_holds_foreground_until_existing_call_returns(self):
        from persona_runtime.chat import ModelCompletion
        self.cfg.update({'organization_parallel_tasks':1})
        self.enable()
        self.worker.cancel();await asyncio.gather(self.worker,return_exceptions=True)
        started=asyncio.Event();release=asyncio.Event()
        normal=self.provider.complete
        async def transport(model,messages):
            if model.get('_native_web_profile'):return await normal(model,messages)
            started.set();await release.wait();return ModelCompletion('916',{}, {})
        self.provider.complete=transport
        self.org.test_model()
        job=asyncio.create_task(self.org.managed.run_next('background'))
        try:
            await asyncio.wait_for(started.wait(),5)
            self.org.foreground_only=True
            self.worker=asyncio.create_task(self.org.worker())
            chat=asyncio.create_task(self.ask())
            from persona_runtime.knowledge_wait import required_tasks
            def waiting():
                with self.store.connection() as db:return required_tasks(db)
            await until(waiting)
            await asyncio.sleep(.35)
            self.assertFalse(self.decisions)
        finally:
            release.set();await job
        result=await asyncio.wait_for(chat,15)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(len(self.decisions),1)


class MemoryContinuation(unittest.IsolatedAsyncioTestCase):
    setUp=memory.MemoryTests.setUp
    asyncTearDown=memory.MemoryTests.asyncTearDown

    async def test_waived_extraction_includes_review_of_same_batch(self):
        await memory.MemoryTests.seed(self)
        model=memory.MemoryTests.install_model(self)
        memory.MemoryTests.enable(self)
        QuotaService(self.store,self.cfg).configure(True,1,0,dict.fromkeys(FIELDS,0))
        self.org.automatic.tick(force=True)
        await self.org.run_next()
        with self.store.connection() as db:waits=call_limits.pending(db)
        self.assertEqual(len(waits),1)
        call_limits.decide(self.store,[waits[0]['id']],True)
        await memory.MemoryTests.finish(self)
        self.assertEqual(len(model.calls),2)
        self.assertEqual(len(self.service.search_memories()),1)
        with self.store.connection() as db:self.assertFalse(db.execute("SELECT 1 FROM model_call_waits").fetchone())
