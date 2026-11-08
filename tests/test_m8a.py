"""Artificial M8.A fault contracts; no real provider or formal database."""
import asyncio
import json
import sqlite3
import time
import unittest
from unittest.mock import patch
from tests import test_m4 as fixtures
from persona_runtime.models import identifier,RuntimeErrorBase
from persona_runtime.organization import OrganizationEngine,encoded
from persona_runtime.private_personas import PrivatePersonas,provider_identity


class ManagedTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.M4Tests.setUp
    asyncTearDown=fixtures.M4Tests.asyncTearDown
    seed=fixtures.M4Tests.seed

    async def auto_tasks(self,count=2):
        turns=await self.seed(count)
        with self.store.connection() as db:orders=[db.execute('SELECT rowid FROM turns WHERE id=?',(t['id'],)).fetchone()[0] for t in turns]
        return [self.org.submit(self.org.preview(turns[i]['session_id'],'candidates',start_row=n,end_row=n),'auto-'+str(n),automatic=1) for i,n in enumerate(orders)]

    async def test_cross_trigger_dedupe_and_operation_conflict(self):
        turn=(await self.seed())[0];p=self.org.preview(turn['session_id'])
        a=self.org.submit(p,'manual');b=self.org.submit(p,'daily',automatic=2)
        self.assertEqual(a['id'],b['id']);await self.org.run_next()
        self.assertEqual(self.org.submit(p,'idle',automatic=1)['id'],a['id'])
        self.assertEqual(len(self.model.calls),1)
        with self.assertRaisesRegex(RuntimeErrorBase,'submission_conflict'):
            self.org.submit(dict(p,fingerprint='changed'),'manual')
        with self.assertRaisesRegex(RuntimeErrorBase,'submission_conflict'):
            self.org.submit(dict(p,events=[]),'manual')

    async def test_parallel_admission_and_queue_limit(self):
        turn=(await self.seed())[0];p=self.org.preview(turn['session_id'])
        rows=await asyncio.gather(*(asyncio.to_thread(self.org.submit,p,'parallel-'+str(n),automatic=n%3) for n in range(6)))
        self.assertEqual(len({r['id'] for r in rows}),1)
        for i in range(9):self.org.test_model('queued-'+str(i))
        with self.assertRaisesRegex(RuntimeErrorBase,'queue_full'):self.org.test_model('eleventh')
        self.assertFalse(self.model.calls)

    async def test_overlapping_work_serializes_without_second_charge_for_covered_sources(self):
        turns=await self.seed(3);sid=turns[0]['session_id']
        a=self.org.submit(self.org.preview(sid,end_row=2),'first')
        b=self.org.submit(self.org.preview(sid,start_row=1,end_row=3),'overlap')
        await self.org.run_next();await self.org.run_next()
        self.assertEqual(self.org.show(a['id'])['status'],'completed')
        self.assertEqual(self.org.show(b['id'])['status'],'completed')
        data=json.loads(self.model.calls[1][1][-1]['content'])
        self.assertEqual({e['turn'] for e in data['events']},{3})

    async def test_missing_manifest_has_zero_attempt_and_no_reservation(self):
        turn=(await self.seed())[0];t=self.org.submit(self.org.preview(turn['session_id']),'manifest')
        with patch.object(self.chat.cognitive,'reflection',side_effect=RuntimeErrorBase('injected')):await self.org.run_next()
        self.assertEqual(self.org.show(t['id'])['error_code'],'context_manifest_storage_failed')
        self.assertFalse(self.model.calls)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM organization_attempts').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM organization_reservations WHERE state='active'").fetchone()[0],0)

    async def test_unknown_call_blocks_retry_and_restart(self):
        turn=(await self.seed())[0];t=self.org.submit(self.org.preview(turn['session_id']),'unknown')
        self.model.failure='provider_timeout';await self.org.run_next()
        self.org.recover();await self.org.run_next()
        self.assertEqual(self.org.show(t['id'])['status'],'blocked_unknown')
        with self.assertRaisesRegex(RuntimeErrorBase,'effect_unknown'):self.org.retry(t['id'])
        self.assertEqual(len(self.model.calls),1)

    async def test_safe_retries_count_four_total_across_restart_and_quota(self):
        from persona_runtime.chat import ModelError
        turn=(await self.seed())[0];t=self.org.submit(self.org.preview(turn['session_id']),'retry')
        calls=[]
        async def fail(*args):
            calls.append(1);error=ModelError('provider_http_429');error.diagnostics={'http_status':429,'request_headers_sent':True};raise error
        self.model.complete=fail;clock=[time.time()];self.org.managed.clock=lambda:clock[0]
        for i in range(4):
            await self.org.run_next();clock[0]+=10;self.org.recover()
        self.assertEqual(len(calls),4)
        await self.org.run_next();self.assertEqual(len(calls),4)
        self.assertEqual(len(self.org.show(t['id'])['attempts']),4)

    async def test_quota_naturally_resumes_and_recovery_before_dispatch_is_free(self):
        self.cfg.update({'organization':{'hourly_limit':1,'daily_limit':24}})
        one,two=await self.auto_tasks();await self.org.run_next();await self.org.run_next()
        self.assertEqual(self.org.show(two['id'])['status'],'waiting_quota')
        self.org.recover();self.org.managed.clock=lambda:time.time()+3601
        await self.org.run_next()
        self.assertEqual(self.org.show(two['id'])['status'],'completed')
        self.assertEqual(len(self.model.calls),2)

    async def test_policy_pause_blocks_dispatch_and_publish_without_erasing_usage(self):
        turn=(await self.seed())[0];t=self.org.submit(self.org.preview(turn['session_id']),'pause')
        self.model.gate=asyncio.Event();running=asyncio.create_task(self.org.run_next())
        while not self.model.calls:await asyncio.sleep(.01)
        policy=self.org.managed.current_policy();data=dict(policy['data'],enabled=False)
        self.org.managed.update_policy(self.persona['id'],data,policy['id'],self.persona['id'],'disable')
        self.model.gate.set();await running
        result=self.org.show(t['id']);self.assertEqual(result['status'],'blocked_authorization')
        self.assertIsNotNone(result['attempts'][0]['usage_json']);self.assertFalse(self.org.summaries.list())

    async def test_receipt_recovery_without_new_call(self):
        turn=(await self.seed())[0];t=self.org.submit(self.org.preview(turn['session_id']),'receipt')
        with patch.object(self.org,'_publish',side_effect=RuntimeErrorBase('simulated_commit_failure')):await self.org.run_next()
        with self.store.connection(write=True) as db:
            # Disk snapshot at process death: received durable, publication rolled back.
            db.execute("UPDATE organization_tasks SET status='running' WHERE id=?",(t['id'],))
        self.org.recover();await self.org.run_next()
        self.assertEqual(self.org.show(t['id'])['status'],'completed')
        self.assertEqual(len(self.model.calls),1)
        self.assertEqual(len(self.org.summaries.list()),1)

    async def test_concurrent_worker_and_running_cancel(self):
        turn=(await self.seed())[0];preview=self.org.preview(turn['session_id']);t=self.org.submit(preview,'cancel-running')
        self.model.gate=asyncio.Event();runner=asyncio.create_task(self.org.run_next())
        while not self.model.calls:await asyncio.sleep(.01)
        second=OrganizationEngine(self.store,self.cfg,self.model,self.chat)
        self.assertFalse(await second.run_next())
        self.org.cancel(t['id']);await runner
        self.assertEqual(self.org.show(t['id'])['status'],'cancelled')
        self.assertFalse(self.org.summaries.list());self.assertEqual(len(self.model.calls),1)
        with self.assertRaisesRegex(RuntimeErrorBase,'effect_unknown'):self.org.retry(t['id'])
        self.assertEqual(self.org.submit(preview,'do-not-bypass-cancel')['id'],t['id'])
        self.assertFalse(await self.org.run_next());self.assertEqual(len(self.model.calls),1)

    async def test_private_main_does_not_authorize_organization(self):
        private=PrivatePersonas(self.paths)
        private.save_record({'id':'fixture','persona_id':self.persona['id'],'approved_models':{'main':provider_identity(self.cfg.load()['model'])}})
        turn=(await self.seed())[0];t=self.org.submit(self.org.preview(turn['session_id']),'private')
        await self.org.run_next();self.assertFalse(self.model.calls)
        self.assertEqual(self.org.show(t['id'])['error_code'],'private_cloud_provider_not_authorized')
        private.authorize_model(self.store,self.persona['id'],'organization',provider_identity(self.org.model()),'explicit-consent')
        self.org.retry(t['id']);await self.org.run_next()
        self.assertEqual(self.org.show(t['id'])['status'],'completed')

    async def test_source_withdrawal_after_dispatch_blocks_publication(self):
        from persona_runtime.memory_policy import exclude_sources
        turn=(await self.seed())[0];p=self.org.preview(turn['session_id']);t=self.org.submit(p,'withdraw')
        self.model.gate=asyncio.Event();running=asyncio.create_task(self.org.run_next())
        while not self.model.calls:await asyncio.sleep(.01)
        with self.store.connection(write=True) as db:exclude_sources(db,self.persona['id'],[p['events'][0]['id']],'fixture')
        self.model.gate.set();await running
        result=self.org.show(t['id']);self.assertEqual(result['status'],'stale')
        self.assertIsNotNone(result['attempts'][0]['usage_json']);self.assertFalse(self.org.summaries.list())

    async def test_foreground_queued_and_collecting_prevent_dispatch(self):
        from persona_runtime.ingress import IngressService
        turn=(await self.seed())[0];t=self.org.submit(self.org.preview(turn['session_id']),'foreground')
        ingress=IngressService(self.chat)
        receipt=ingress.accept({'channel':'desktop','persona_id':self.persona['id'],'text':'人工新输入','request_id':'collecting'})
        self.assertFalse(await self.org.run_next());self.assertFalse(self.model.calls)

    async def test_aging_auto_can_run_before_new_manual_and_unknown_does_not_block(self):
        turn=(await self.seed())[0];p=self.org.preview(turn['session_id'])
        automatic=self.org.submit(p,'auto',automatic=1)
        manual=self.org.test_model('new-manual')
        with self.store.connection(write=True) as db:
            c=self.org.managed.contract(db,automatic['id']);c['data']['created_epoch']-=601;self.org.managed.save(db,c)
        self.model.failure='provider_timeout';await self.org.run_next()
        self.assertEqual(self.org.show(automatic['id'])['status'],'blocked_unknown')
        self.model.failure=None;await self.org.run_next()
        self.assertEqual(self.org.show(manual['id'])['status'],'completed')

    async def test_daily_cap_recovers_only_after_full_window(self):
        self.cfg.update({'organization':{'hourly_limit':0,'daily_limit':1}})
        one,t=await self.auto_tasks();await self.org.run_next()
        self.org.managed.clock=lambda:time.time()+3601;await self.org.run_next()
        self.assertEqual(self.org.show(t['id'])['status'],'waiting_quota')
        self.org.managed.clock=lambda:time.time()+86401;await self.org.run_next()
        self.assertEqual(self.org.show(t['id'])['status'],'completed');self.assertEqual(len(self.model.calls),2)

    async def test_expired_live_lease_cannot_dispatch_second_worker(self):
        t=self.org.test_model('lease');self.model.gate=asyncio.Event();running=asyncio.create_task(self.org.run_next())
        while not self.model.calls:await asyncio.sleep(.01)
        with self.store.connection(write=True) as db:db.execute('UPDATE organization_execution SET lease_until=0')
        second=OrganizationEngine(self.store,self.cfg,self.model,self.chat)
        second.test_model('second');self.assertFalse(await second.run_next())
        self.model.gate.set();await running
        self.assertNotEqual(self.org.show(t['id'])['status'],'completed')
        # The late durable receipt is recovered without another call for that task.
        await second.run_next();self.assertEqual(second.show(t['id'])['status'],'completed')
        self.assertEqual(len(self.model.calls),1)

    async def test_legacy_running_without_dispatch_evidence_needs_audit(self):
        t=self.org.test_model('legacy')
        with self.store.connection(write=True) as db:
            db.execute('DELETE FROM organization_submissions');db.execute('DELETE FROM organization_contracts')
            db.execute("UPDATE organization_tasks SET status='running' WHERE id=?",(t['id'],))
        self.org.recover();await self.org.run_next()
        self.assertEqual(self.org.show(t['id'])['status'],'blocked_unknown');self.assertFalse(self.model.calls)

    async def test_corrupt_receipt_never_regenerates(self):
        t=self.org.test_model('corrupt');await self.org.run_next()
        with self.store.connection(write=True) as db:
            db.execute("UPDATE organization_tasks SET status='running' WHERE id=?",(t['id'],))
            db.execute("UPDATE organization_receipts SET response_text='tampered'")
        self.org.recover();await self.org.run_next()
        self.assertEqual(self.org.show(t['id'])['error_code'],'organization_receipt_corrupt')
        self.assertEqual(len(self.model.calls),1)

    async def test_empty_redundant_conflict_field_is_narrow_and_raw_receipt_preserved(self):
        turn=(await self.seed())[0];p=self.org.preview(turn['session_id'],mode='candidates');t=self.org.submit(p,'empty')
        self.model.transform=lambda _:dict(format_version=1,summary=[],candidates=[],conflicts=[])
        await self.org.run_next();r=self.org.show(t['id']);self.assertEqual(r['status'],'completed')
        with self.store.connection() as db:
            raw=db.execute('SELECT response_text FROM organization_receipts').fetchone()[0]
            self.assertIn('conflicts',json.loads(raw))
            for extra in ([{'id':'untrusted'}],None,{},False):
                with self.assertRaisesRegex(RuntimeErrorBase,'invalid_output'):
                    self.org.validate_output(db,p,json.dumps(dict(format_version=1,summary=[],candidates=[],conflicts=extra)))
            with self.assertRaisesRegex(RuntimeErrorBase,'invalid_output'):
                self.org.validate_output(db,p,json.dumps(dict(format_version=1,summary=[],candidates=[],unrecognized=[])))
        self.assertEqual(r['attempts'][0]['output_diagnostic']['normalizations'][0]['path'],'$.conflicts')

    async def test_explicit_receipt_repair_uses_same_attempt_and_retains_failure_audit(self):
        turn=(await self.seed())[0];t=self.org.submit(self.org.preview(turn['session_id']),'repair')
        with patch.object(self.org,'validate_output',side_effect=RuntimeErrorBase('organization_invalid_output')):await self.org.run_next()
        before=self.org.show(t['id']);self.assertEqual(before['status'],'failed')
        self.org.retry(t['id']);await self.org.run_next();r=self.org.show(t['id'])
        self.assertEqual(r['status'],'completed');self.assertEqual(len(self.model.calls),1)
        self.assertIsNone(r['attempts'][0]['error_code'])
        self.assertEqual(r['attempts'][0]['output_diagnostic']['previous_validation'][0]['error_code'],'organization_invalid_output')

    async def test_policy_cas_idempotency_and_queue_cancellation(self):
        t=self.org.test_model('cancel-queue');p=self.org.managed.current_policy();data=dict(p['data'],enabled=False)
        result=self.org.managed.update_policy(self.persona['id'],data,p['id'],self.persona['id'],'op')
        self.assertEqual(self.org.managed.update_policy(self.persona['id'],data,p['id'],self.persona['id'],'op')['id'],result['id'])
        with self.assertRaisesRegex(RuntimeErrorBase,'submission_conflict'):
            self.org.managed.update_policy(self.persona['id'],dict(data,enabled=True),p['id'],self.persona['id'],'op')
        with self.assertRaisesRegex(RuntimeErrorBase,'version_conflict'):
            self.org.managed.update_policy(self.persona['id'],data,p['id'],self.persona['id'],'old')
        self.org.cancel(t['id']);await self.org.run_next();self.assertFalse(self.model.calls)

    async def test_private_ancestor_organization_approval_is_not_a_grant(self):
        private=PrivatePersonas(self.paths)
        private.save_record({'id':'fixture','persona_id':self.persona['id'],'approved_models':{'organization':provider_identity(self.org.model())}})
        from persona_runtime.private_personas import guard_cloud_dispatch
        with self.assertRaisesRegex(RuntimeErrorBase,'not_authorized'):guard_cloud_dispatch(self.store,self.persona['id'],'organization',self.org.model())


class MigrationTests(unittest.TestCase):
    def test_21_upgrade_rollback_restore_and_export(self):
        from pathlib import Path
        import tempfile
        from persona_runtime.paths import RuntimePaths
        from persona_runtime.storage import Store
        from persona_runtime.service import RuntimeService
        from persona_runtime.models import generic_persona
        from scripts.verify_m8a_migration import verify
        root=RuntimePaths.from_environment().home/'exports/acceptance'
        with tempfile.TemporaryDirectory(prefix='m8a-migration-test-',dir=root) as temp:
            paths=RuntimePaths(Path(temp)/'source');store=Store(paths)
            with patch('persona_runtime.storage.SCHEMA_VERSION',21):
                store.initialize();service=RuntimeService(store);p=service.create_persona(generic_persona());service.use_persona(p['id'])
            with patch('persona_runtime.storage.SCHEMA_VERSION',22):
                self.assertTrue(verify(store.path,Path(temp)/'proof')['source_unchanged'])


if __name__=='__main__':unittest.main()
