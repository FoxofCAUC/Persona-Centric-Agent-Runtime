"""Structure 34 boundaries exercised with local numeric fixtures only."""
import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from persona_runtime import session_privacy
from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore
from persona_runtime.ingress import IngressService
from persona_runtime.models import PersonaSpec, RuntimeErrorBase, identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


class StageOne(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.paths = RuntimePaths(Path(temp.name))
        self.config = ConfigStore(self.paths)
        self.config.initialize()
        self.config.update({'model':{'name':'deepseek-v4-pro','base_url':'https://api.deepseek.com','api_key':'111'}})
        self.store = Store(self.paths)
        self.store.initialize()
        service = RuntimeService(self.store)
        self.pid = service.create_persona(PersonaSpec(1,'101','102',['103'],'104',['105'],'106'))['id']
        service.use_persona(self.pid)
        self.calls = []
        self.legacy = Mock()
        self.engine = ChatEngine(self.store,self.config,self.legacy,private_reply_transport=self.reply)
        self.sid = self.engine.session()['session_id']

    async def reply(self, attempt, decision, contract):
        self.calls.append((attempt,decision,contract))
        return '201', {'total_tokens':1}

    def grant(self, sid=None):
        return session_privacy.status(self.store,self.config,self.pid,sid or self.sid)

    def consent(self, enable, state, operation=None, sid=None):
        return session_privacy.update(self.store,self.config,self.pid,sid or self.sid,enable,state['revision'],
                                      state['recipient_hash'],operation or identifier(),state['protocol'])

    def decide(self, tid):
        contract = self.engine.private_reply.contract(tid)
        decision = {'targets':contract['targets'], 'action':'respond', 'stance':'1', 'style':'2',
                    'facts':[], 'mode':'chat', 'ending':'stop'}
        return self.engine.private_reply.accept_decision(tid,decision,contract['binding'],expires_at=time.time()+60)

    async def submit(self):
        result = await self.engine.submit('desktop','301',identifier(),self.sid,persona_id=self.pid)
        await self.engine.wait()
        return result['id']

    async def test_missing_decision_then_valid_and_concurrent_retry(self):
        tid = await self.submit()
        self.assertEqual(self.engine.get(tid)['error_code'],'reply_decision_required')
        self.assertFalse(self.calls)
        self.assertEqual(self.decide(tid), self.decide(tid))
        await asyncio.gather(*(self.engine.retry(tid) for _ in range(3)))
        restarted = ChatEngine(self.store,self.config,self.legacy)
        restarted.recover()
        await restarted.retry(tid)
        another = await restarted.submit('desktop','302',identifier(),self.sid,persona_id=self.pid)
        await restarted.wait()
        self.assertEqual(restarted.get(another['id'])['error_code'],'reply_decision_required')
        self.assertEqual(len(self.calls),1)
        self.assertEqual(restarted.get(tid)['error_code'],'reply_review_pending')
        self.assertIsNone(restarted.get(tid)['reply'])
        self.legacy.complete.assert_not_called()

    async def test_revoke_invalidates_frozen_decision_and_compat_routes(self):
        before = self.grant()
        enabled = self.consent(True,before)
        tid = await self.submit()
        self.decide(tid)
        self.consent(False,enabled)
        await self.engine.retry(tid)
        self.assertEqual(self.engine.get(tid)['error_code'],'reply_decision_required')
        for fn in (self.engine.resume_direct,self.engine.resume_expression):
            with self.assertRaisesRegex(RuntimeErrorBase,'private_reply_route_required'):
                await fn(tid)
        with self.assertRaisesRegex(RuntimeErrorBase,'private_reply_route_required'):
            self.engine.resume_scene(tid)
        self.assertFalse(self.calls)
        self.legacy.complete.assert_not_called()

    async def test_invalid_target_or_stale_model(self):
        tid=await self.submit()
        contract=self.engine.private_reply.contract(tid)
        with self.assertRaisesRegex(RuntimeErrorBase,'reply_decision_required'):
            self.engine.private_reply.accept_decision(tid,{'targets':['invalid']},contract['binding'],expires_at=time.time()+60)
        self.decide(tid)
        self.config.update({'model':{'name':'deepseek-flash'}})
        await self.engine.retry(tid)
        self.assertEqual(self.engine.get(tid)['error_code'],'private_reply_model_changed')
        self.assertFalse(self.calls)

    async def test_unknown_transport_is_not_replayed(self):
        tid=await self.submit()
        self.decide(tid)
        async def interrupted(attempt, decision, contract):
            self.calls.append(attempt)
            raise ConnectionError()
        self.engine.private_reply.transport=interrupted
        with self.assertRaises(ConnectionError):
            await self.engine.retry(tid)
        self.engine.recover()
        await self.engine.retry(tid)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.engine.get(tid)['error_code'],'reply_dispatch_unknown')

    async def test_expired_decision_can_be_renewed_without_replaying(self):
        tid=await self.submit()
        old=self.decide(tid)
        with patch('persona_runtime.private_reply_entry.time.time',return_value=time.time()+120):
            await self.engine.retry(tid)
            self.assertFalse(self.calls)
            fresh=self.decide(tid)
            await self.engine.retry(tid)
        self.assertNotEqual(old,fresh)
        self.assertEqual(len(self.calls),1)

    async def test_received_decision_survives_exit_before_dispatch(self):
        tid=await self.submit()
        self.decide(tid)
        self.assertEqual(self.engine.get(tid)['status'],'queued')
        restarted=ChatEngine(self.store,self.config,self.legacy,private_reply_transport=self.reply)
        restarted.recover()
        IngressService(restarted).schedule_committed()
        await restarted.wait()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(restarted.get(tid)['error_code'],'reply_review_pending')

    async def test_cancellation_keeps_late_receipt_private(self):
        tid=await self.submit()
        self.decide(tid)
        started,release=asyncio.Event(),asyncio.Event()
        async def delayed(*args):
            started.set()
            await release.wait()
            return '505',{}
        self.engine.private_reply.transport=delayed
        task=asyncio.create_task(self.engine.retry(tid))
        await asyncio.wait_for(started.wait(),2)
        await self.engine.cancel(tid)
        release.set()
        await task
        value=self.engine.get(tid)
        self.assertIsNone(value['reply'])
        self.assertEqual(value['error_code'],'cancelled_by_user')
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT state FROM private_reply_attempts').fetchone()[0],'received')

    def test_new_endpoint_requires_its_own_credential_and_stale_preview_is_rejected(self):
        args=(self.store,self.config,'jev','https://example.invalid/v1','jev-1.14')
        with self.assertRaisesRegex(RuntimeErrorBase,'credential_required_for_new_endpoint'):
            session_privacy.connection(*args,key='')
        self.consent(True,self.grant())
        preview=session_privacy.connection(*args,key='611')
        self.consent(False,self.grant())
        with self.assertRaisesRegex(RuntimeErrorBase,'session_privacy_recipients_changed'):
            session_privacy.connection(*args,key='611',confirm=preview['confirmation'])
        self.assertNotIn('jev',self.grant()['recipients'])

    async def test_crash_after_durable_claim_does_not_replay(self):
        tid=await self.submit()
        choice=self.decide(tid)
        with self.store.connection(write=True) as db:
            db.execute("""INSERT INTO private_reply_attempts
                (id,decision_id,turn_id,phase,draft,state,started_at) VALUES(?,?,?,'main',0,'sending','0')""",
                ('711',choice,tid))
            db.execute("UPDATE turns SET status='running' WHERE id=?",(tid,))
        reopened=ChatEngine(self.store,self.config,self.legacy,private_reply_transport=self.reply)
        reopened.recover()
        await reopened.retry(tid)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT state FROM private_reply_attempts').fetchone()[0],'unknown')
        self.assertFalse(self.calls)
        self.legacy.complete.assert_not_called()

    async def test_unlimited_buffer_and_duplicate_confirmation(self):
        ingress=IngressService(self.engine)
        policy=ingress.current_policy(self.pid,'desktop')
        ingress.update_policy(self.pid,'desktop','natural_batch',policy['id'],identifier(),manual_confirmation=True)
        one={'persona_id':self.pid,'channel':'desktop','session_id':self.sid,'text':'401','request_id':'411'}
        two=one|{'text':'402','request_id':'412'}
        held=ingress.accept(one)
        self.assertEqual(held['receipt_id'],ingress.accept(one)['receipt_id'])
        ingress.accept(two)
        with self.store.connection() as db:
            self.assertFalse(db.execute('SELECT 1 FROM private_reply_turns').fetchone())
            batch=db.execute('SELECT id,revision FROM input_batches WHERE id=?',(held['batch_id'],)).fetchone()
        args=(batch['id'],'confirm',batch['revision'],identifier(),self.pid,self.sid)
        sent=ingress.action(*args)
        self.assertEqual(ingress.action(*args),sent)
        await self.engine.wait()
        tid=ingress.lookup('desktop','411')['turn_id']
        self.decide(tid)
        await self.engine.retry(tid)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(len(self.calls[0][1]['targets']),2)
        self.legacy.complete.assert_not_called()

    def test_consent_cas_replay_and_session_isolation(self):
        state=self.grant()
        with ThreadPoolExecutor(2) as executor:
            jobs=[executor.submit(self.consent,True,state,op) for op in ('501','502')]
        failed=[job.exception() for job in jobs if job.exception()]
        self.assertEqual([str(err) for err in failed],['version_conflict'])
        winning='501' if jobs[0].exception() is None else '502'
        self.assertTrue(self.consent(True,state,winning)['enabled'])
        with self.assertRaisesRegex(RuntimeErrorBase,'operation_conflict'):
            self.consent(False,state,winning)
        second=self.engine.session(fresh=True)['session_id']
        self.assertFalse(self.grant(second)['enabled'])
        self.assertTrue(self.grant()['enabled'])

    def test_connection_preview_save_and_out_of_band_change(self):
        self.consent(True,self.grant())
        args=(self.store,self.config,'jev','https://example.invalid/v1','jev-1.14','601')
        preview=session_privacy.connection(*args)
        self.assertEqual(preview['affected'],[self.sid])
        self.assertNotIn('601',json.dumps(preview))
        session_privacy.connection(*args,confirm=preview['confirmation'])
        self.assertTrue(self.grant()['enabled'])
        self.assertEqual(self.grant()['revision'],2)
        self.config.update({'model':{'name':'deepseek-flash'}})
        self.assertTrue(self.grant()['enabled'])
        self.config.update({'model':{'api_key':'602'}})
        self.assertFalse(self.grant()['enabled'])

    def test_organization_and_selector_use_the_reviewed_receiver_set(self):
        self.config.update({'character_selector':{'base_url':'https://api.deepseek.com','name':'601',
                                                 'api_key':'600','enabled':True}})
        self.consent(True,self.grant())
        for role in ('organization','selector'):
            args=(self.store,self.config,role,'https://api.deepseek.com','deepseek-flash','605')
            preview=session_privacy.connection(*args)
            self.assertEqual(preview['affected'],[self.sid])
            session_privacy.connection(*args,confirm=preview['confirmation'])
            self.assertTrue(self.grant()['enabled'])
        self.assertEqual(self.grant()['revision'],3)
        self.assertEqual(self.grant()['recipients']['search_fetch'],'https://api.deepseek.com/v1/tools')

    def test_detaching_memory_model_preserves_the_reviewed_account(self):
        self.config.update({'organization':{'reuse_chat':True}})
        self.consent(True,self.grant())
        args=(self.store,self.config,'organization','https://api.deepseek.com','deepseek-flash')
        preview=session_privacy.connection(*args)
        self.assertEqual(preview['affected'],[])
        session_privacy.connection(*args,confirm=preview['confirmation'])
        self.assertFalse(self.config.load()['organization']['reuse_chat'])
        self.assertTrue(self.grant()['enabled'])

    def test_old_database_cannot_restore_revoked_consent(self):
        allowed=self.consent(True,self.grant())
        backup=self.paths.home/'snapshot.sqlite'
        with self.store.connection() as db, closing(sqlite3.connect(backup)) as copy:
            db.backup(copy)
        self.consent(False,allowed)
        with closing(sqlite3.connect(backup)) as copy,closing(sqlite3.connect(self.store.path)) as db:
            copy.backup(db)
        restored=self.grant()
        self.assertFalse(restored['enabled'])
        self.assertEqual(restored['revision'],2)
        self.assertEqual(self.consent(True,restored)['revision'],3)
