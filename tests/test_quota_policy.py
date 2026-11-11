"""User-directed calls are audited but independent of automatic-call limits."""
import time
import unittest
from tests import test_m8d as memory
from persona_runtime.models import identifier
from persona_runtime.quotas import QuotaService


class AutomaticQuotaPolicyTests(unittest.IsolatedAsyncioTestCase):
    setUp=memory.MemoryTests.setUp
    asyncTearDown=memory.MemoryTests.asyncTearDown
    seed=memory.MemoryTests.seed
    enable=memory.MemoryTests.enable
    install_model=memory.MemoryTests.install_model
    finish=memory.MemoryTests.finish

    async def test_automatic_caps_still_wait_and_manual_calls_do_not_use_them(self):
        self.cfg.update({'model':{'hourly_limit':1},'organization':{'hourly_limit':1,'daily_limit':1}})
        turns=await self.seed(3)
        with self.store.connection() as db:orders=[db.execute('SELECT rowid FROM turns WHERE id=?',(t['id'],)).fetchone()[0] for t in turns]
        def submit(index,auto):return self.org.submit(self.org.preview(turns[index]['session_id'],'candidates',start_row=orders[index],end_row=orders[index]),identifier(),automatic=auto)
        manual=submit(0,False);await self.org.run_next();automatic=submit(1,True);await self.org.run_next()
        self.assertEqual(self.org.show(automatic['id'])['status'],'completed')
        for _ in range(3):
            probe=self.org.test_model();await self.org.run_next();self.assertEqual(self.org.show(probe['id'])['status'],'completed')
        waiting=submit(2,True);await self.org.run_next();self.assertEqual(self.org.show(waiting['id'])['status'],'waiting_quota')
        self.assertEqual(QuotaService(self.store,self.cfg).current()['used']['project_hourly'],1)
        self.org.managed.clock=lambda:time.time()+3601;await self.org.run_next();self.assertEqual(self.org.show(waiting['id'])['status'],'waiting_quota')
        self.org.managed.clock=lambda:time.time()+86401;await self.org.run_next();self.assertEqual(self.org.show(waiting['id'])['status'],'completed')

    async def test_development_automatic_work_exceeds_saved_limits_and_keeps_ledger(self):
        self.cfg.update({'model':{'hourly_limit':1},'organization':{'hourly_limit':1,'daily_limit':1}})
        turns=await self.seed(3)
        quotas=QuotaService(self.store,self.cfg);quotas.begin_development()
        # An older driver writing its old settings must not silently restore caps.
        self.cfg.update({'organization':{'hourly_limit':1,'daily_limit':1}})
        with self.store.connection() as db:
            orders=[db.execute('SELECT rowid FROM turns WHERE id=?',(turn['id'],)).fetchone()[0] for turn in turns]
        for turn,order in zip(turns,orders):
            task=self.org.submit(self.org.preview(turn['session_id'],'candidates',start_row=order,end_row=order),identifier(),automatic=True)
            await self.org.run_next()
            self.assertEqual(self.org.show(task['id'])['status'],'completed')
        current=quotas.current()
        self.assertTrue(current['development_unlimited'])
        self.assertTrue(all(value==0 for value in current['limits'].values()))
        self.assertEqual(current['used']['background_daily'],3)
        self.assertEqual(current['used']['project_hourly'],3)
        self.assertEqual(current['restore_limits']['background_daily'],1)
        quotas.end_acceptance()
        self.assertEqual(quotas.current()['limits']['background_daily'],1)

    async def test_manual_memory_extraction_and_review_share_manual_origin(self):
        turns=await self.seed();m=self.install_model();self.enable()
        self.cfg.update({'model':{'hourly_limit':1},'organization':{'hourly_limit':1,'daily_limit':1}})
        self.org.submit(self.org.preview(turns[0]['session_id']),'explicit-manual')
        await self.finish();self.assertEqual(len(m.calls),2);self.assertEqual(len(self.service.search_memories()),1)
        with self.store.connection() as db:self.assertEqual([r[0] for r in db.execute('SELECT automatic FROM organization_tasks')],[0,0])
        self.assertEqual(QuotaService(self.store,self.cfg).current()['used']['background_hourly'],0)

    async def test_manual_rebuild_ignores_exhausted_automatic_budget(self):
        await self.seed();m=self.install_model();self.enable();await self.finish()
        summary=self.org.summaries.list()[0];self.org.summaries.disable(summary['id'])
        self.cfg.update({'model':{'hourly_limit':1},'organization':{'hourly_limit':1,'daily_limit':1}})
        self.org.automatic.rebuild_submit(self.org.automatic.rebuild_preview(summary['session_id'],self.persona['id']),self.persona['id'],'manual-repair')
        await self.finish();self.assertEqual(len(m.calls),4)
        with self.store.connection() as db:self.assertEqual([r[0] for r in db.execute('SELECT automatic FROM organization_tasks')],[1,1,0,0])

    async def test_manual_full_scan_is_bounded_and_keeps_later_sources_automatic(self):
        await self.seed();m=self.install_model();self.enable()
        self.cfg.update({'model':{'hourly_limit':1},'organization':{'hourly_limit':1,'daily_limit':1}})
        self.org.schedule.run_daily_now();await self.finish();self.assertEqual(len(m.calls),2)
        import json
        from pathlib import Path
        portable=json.loads(Path(self.service.export_persona('manual-scan-export')['path']).read_text(encoding='utf-8'))
        self.assertFalse(any(op['id'].startswith('manual-memory-scan:') for op in portable['memory_operations']))
        await self.chat.submit('desktop','新增事实：偏爱散步。','later-auto');await self.chat.wait();await self.finish()
        self.assertEqual(len(m.calls),3)
        with self.store.connection() as db:self.assertEqual([r[0] for r in db.execute('SELECT automatic FROM organization_tasks')],[0,0,1,1])
        self.assertEqual(self.org.automatic.coverage()['batches'][0]['state'],'verifying')
