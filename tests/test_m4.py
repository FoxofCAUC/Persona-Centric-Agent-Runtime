"""M4 offline contracts. All fixtures and reports live under the D data home."""
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from persona_runtime.chat import ChatEngine, ModelError
from persona_runtime.config import ConfigStore
from persona_runtime.models import generic_persona, RuntimeErrorBase
from persona_runtime.organization import OrganizationEngine, encoded
from persona_runtime.paths import RuntimePaths
from persona_runtime.server import create_app
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store, execute_schema, SCHEMA, SCHEMA2, SCHEMA3, SCHEMA4


class ChatModel:
    async def complete(self, model, messages):
        return '模拟回复', {'total_tokens': 1}


class OrganizerModel:
    def __init__(self):
        self.calls = []
        self.gate = None
        self.failure = None
        self.transform = lambda data: data

    async def complete(self, model, messages):
        self.calls.append((model, messages))
        if self.gate:
            await self.gate.wait()
        if self.failure:
            raise ModelError(self.failure)
        if len(messages) == 1:
            return 'OK', {'total_tokens': 1}
        payload = json.loads(messages[-1]['content'])
        user = next(e for e in payload['events'] if e['role'] == 'user')
        source = {'event_id': user['id'], 'quote': user['text']}
        item = {'text': user['text'], 'sources': [source]}
        summary = payload['previous_summary']+[item]
        result = {'format_version': 1, 'summary': summary if payload['mode'] != 'candidates' else [],
                  'candidates': [{'category': 'preference', 'text': user['text'], 'sources': [source], 'conflicts': []}] if payload['mode'] != 'summary' else []}
        return encoded(self.transform(result)), {'total_tokens': 10}


class M4Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        root = RuntimePaths.from_environment().home/'tmp'
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='m4-test-', dir=root)
        self.addCleanup(self.temp.cleanup)
        self.paths = RuntimePaths(Path(self.temp.name))
        self.cfg = ConfigStore(self.paths)
        self.cfg.initialize()
        self.cfg.update({'model': {'base_url': 'http://127.0.0.1:1/v1', 'name': 'fake', 'hourly_limit': 1000},
                         'organization': {'model': {'base_url': 'http://127.0.0.1:1/v1', 'name': 'fake-organizer'}}})
        self.store = Store(self.paths)
        self.store.initialize()
        self.service = RuntimeService(self.store)
        self.persona = self.service.create_persona(generic_persona())
        self.service.use_persona(self.persona['id'])
        self.chat = ChatEngine(self.store, self.cfg, ChatModel())
        self.model = OrganizerModel()
        self.org = OrganizationEngine(self.store, self.cfg, self.model, self.chat)

    async def asyncTearDown(self):
        if self.model.gate:
            self.model.gate.set()
        await self.chat.wait()

    async def seed(self, count=1, channel='desktop'):
        turns = []
        for i in range(count):
            turn = await self.chat.submit(channel, '虚构偏好：喜欢乌龙茶 '+str(i), f'{channel}-{i}')
            await self.chat.wait()
            turns.append(self.chat.get(turn['id']))
        return turns

    async def organize(self, session_id, mode='both', **kwargs):
        preview = self.org.preview(session_id, mode, **kwargs)
        task = self.org.submit(preview, 'manual-'+str(time.time_ns()))
        await self.org.run_next()
        return self.org.show(task['id'])

    async def test_preview_submit_idempotent_and_atomic_publication(self):
        turn = (await self.seed())[0]
        preview = self.org.preview(turn['session_id'])
        task = self.org.submit(preview, 'stable')
        self.assertEqual(self.org.submit(preview, 'stable')['id'], task['id'])
        self.assertEqual(len(self.model.calls), 0)
        await self.org.run_next()
        self.assertEqual(self.org.show(task['id'])['status'], 'completed')
        self.assertEqual(len(self.org.summaries.list()), 1)
        self.assertEqual(self.service.search_memories(), [])
        self.assertEqual(len(self.org.candidates.list('pending')), 1)

    async def test_review_idempotence_multisource_and_new_session_recall(self):
        turn = (await self.seed(2))[0]
        def multiple(data):
            data['candidates'][0]['sources'].append(dict(data['summary'][0]['sources'][0]))
            return data
        self.model.transform = multiple
        await self.organize(turn['session_id'])
        candidate = self.org.candidates.list()[0]
        result = self.org.candidates.accept(candidate['id'], 'review')
        self.assertEqual(self.org.candidates.accept(candidate['id'], 'another')['memory_id'], result['memory_id'])
        self.assertEqual(len(self.service.search_memories()), 1)
        fresh = self.chat.session(fresh=True)
        next_turn = await self.chat.submit('desktop', '喜欢什么茶', 'fresh', fresh['session_id'])
        await self.chat.wait()
        explanation = self.org.explain(next_turn['id'])[0]['details']
        self.assertEqual(explanation['memories'][0]['id'], result['memory_id'])
        self.assertIsNone(explanation['summary_id'])

    async def test_reject_excludes_whole_turn_and_invalidates_summary(self):
        turns = await self.seed(2)
        await self.organize(turns[0]['session_id'])
        candidate = self.org.candidates.list()[0]
        self.org.candidates.reject(candidate['id'], 'reject')
        self.assertEqual(self.org.summaries.list()[0]['status'], 'stale')
        preview = self.org.preview(turns[0]['session_id'])
        self.assertNotIn(turns[0]['user_event_id'], [e['id'] for e in preview['events']])
        with self.store.connection() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM source_exclusions').fetchone()[0], 2)
        self.assertEqual(len(self.service.show_session(turns[0]['session_id'])['events']), 4)

    async def test_cli_service_revision_filters_old_raw_and_stales_summary(self):
        turn = (await self.seed())[0]
        await self.organize(turn['session_id'])
        candidate = self.org.candidates.list()[0]
        memory = self.org.candidates.accept(candidate['id'], 'accept')['memory_id']
        new = self.service.revise_memory(memory, '虚构偏好：喜欢咖啡')
        self.assertEqual(new['revision'], 2)
        self.assertEqual(self.org.summaries.list()[0]['status'], 'stale')
        next_turn = await self.chat.submit('desktop', '喜欢什么', 'after-correction')
        await self.chat.wait()
        details = self.org.explain(next_turn['id'])[0]['details']
        self.assertNotIn(turn['id'], details['history_rows'])
        self.assertEqual([m['id'] for m in details['memories']], [new['id']])
        self.service.retract_memory(new['id'])
        self.assertEqual(self.service.search_memories(), [])

    async def test_invalid_quote_or_source_rolls_back_all_outputs(self):
        turn = (await self.seed())[0]
        for bad in ('not-original', 'foreign'):
            def mutate(data):
                if bad == 'foreign':
                    data['candidates'][0]['sources'][0]['event_id'] = 'not-a-source'
                else:
                    data['candidates'][0]['sources'][0]['quote'] = 'invented quote'
                return data
            self.model.transform = mutate
            result = await self.organize(turn['session_id'])
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['error_code'], 'organization_invalid_output')
            self.assertEqual(self.org.summaries.list(), [])
            self.assertEqual(self.org.candidates.list(), [])

    async def test_source_change_during_model_wait_prevents_publication(self):
        turn = (await self.seed())[0]
        memory = self.service.add_memory('preference', '旧偏好', turn['user_event_id'])
        task = self.org.submit(self.org.preview(turn['session_id']), 'race')
        self.model.gate = asyncio.Event()
        running = asyncio.create_task(self.org.run_next())
        await asyncio.sleep(.01)
        self.service.retract_memory(memory['id'])
        self.model.gate.set()
        await running
        self.assertEqual(self.org.show(task['id'])['status'], 'stale')
        self.assertEqual(self.org.summaries.list(), [])

    async def test_long_dialogue_summary_coverage_and_input_bound(self):
        # Preserve the M4 near-ten/summary compatibility contract explicitly.
        from persona_runtime.ingress import IngressService
        ingress=IngressService(self.chat);policy=ingress.current_policy(self.persona['id'],'desktop')
        ingress.update_policy(self.persona['id'],'desktop',policy['mode'],policy['id'],'m4-legacy',context_mode='legacy_recent')
        turns = await self.seed(32)
        session = turns[0]['session_id']
        for _ in range(3):
            result = await self.organize(session, 'summary')
            self.assertEqual(result['status'], 'completed')
        latest = self.org.summaries.list()[0]
        next_turn = await self.chat.submit('desktop', '早先的乌龙茶偏好是什么', 'long-follow-up')
        await self.chat.wait()
        details = self.org.explain(next_turn['id'])[0]['details']
        self.assertEqual(details['summary_id'], latest['id'])
        self.assertEqual(details['coverage_gap_turns'], 0)
        self.assertLessEqual(details['input_characters'], 24000)
        self.assertEqual(len(details['history_rows']), 2)

    async def test_persona_switch_does_not_redirect_task_or_allow_cross_review(self):
        turn = (await self.seed())[0]
        task = self.org.submit(self.org.preview(turn['session_id']), 'pinned')
        other = self.service.create_persona(generic_persona())
        self.service.use_persona(other['id'])
        await self.org.run_next()
        self.assertEqual(self.org.list(), [])
        with self.assertRaises(RuntimeErrorBase):
            self.org.show(task['id'])
        self.service.use_persona(self.persona['id'])
        self.assertEqual(self.org.show(task['id'])['status'], 'completed')

    async def test_background_quota_counts_test_and_retry_and_no_auto_retry(self):
        self.cfg.update({'organization': {'hourly_limit': 1}})
        turn = (await self.seed())[0]
        self.model.failure = 'provider_timeout'
        task = self.org.submit(self.org.preview(turn['session_id']), 'timeout')
        await self.org.run_next()
        self.assertEqual(self.org.show(task['id'])['status'], 'failed')
        await self.org.run_next()
        self.assertEqual(len(self.model.calls), 1)
        self.org.retry(task['id'])
        await self.org.run_next()
        self.assertEqual(self.org.show(task['id'])['status'], 'waiting_quota')
        self.assertEqual(len(self.model.calls), 1)

    async def test_shared_project_quota_includes_background(self):
        turn = (await self.seed())[0]
        task = self.org.submit(self.org.preview(turn['session_id']), 'budget')
        await self.org.run_next()
        self.cfg.update({'model': {'hourly_limit': 2}})
        next_turn = await self.chat.submit('desktop', '额度应该用完', 'limit')
        await self.chat.wait()
        self.assertEqual(self.chat.get(next_turn['id'])['error_code'], 'hourly_limit_reached')
        self.assertEqual(self.org.show(task['id'])['status'], 'completed')

    async def test_queue_cancel_recover_and_encrypted_model_not_public(self):
        turn = (await self.seed())[0]
        task = self.org.submit(self.org.preview(turn['session_id']), 'cancel')
        self.org.cancel(task['id'])
        await self.org.run_next()
        self.assertEqual(len(self.model.calls), 0)
        self.org.retry(task['id'])
        self.org.recover()
        self.assertEqual(self.org.show(task['id'])['status'], 'interrupted')
        self.assertNotIn('model_snapshot', self.org.show(task['id']))
        self.assertNotIn('encrypted_key', encoded(self.cfg.public()))

    async def test_automatic_boundary_excludes_old_history(self):
        turns = await self.seed(2)
        self.org.schedule.set_automatic(True)
        self.org.schedule.incremental_tick()
        self.assertEqual(self.org.list(), [])
        with self.store.connection() as conn:
            progress = conn.execute('SELECT * FROM organization_progress').fetchone()
            max_row = conn.execute('SELECT max(rowid) FROM turns').fetchone()[0]
            self.assertEqual(progress['boundary'], max_row)

    async def test_daily_next_local_time_and_full_scan_auto_accept(self):
        turn = (await self.seed())[0]
        status = self.org.schedule.status()
        self.assertTrue(status['enabled'])
        self.assertGreater(status['next_epoch'], time.time()+86300)
        self.assertIn('T', status['next_local_time'])
        self.org.schedule.daily_tick(status['next_epoch']+1)
        task = self.org.list()[0]
        self.assertEqual(task['automatic'], 2)
        await self.org.run_next()
        self.org.schedule.daily_tick(status['next_epoch']+2)
        self.assertEqual(len(self.service.search_memories()), 1)
        self.assertEqual(self.org.candidates.list()[0]['status'], 'accepted')
        self.assertEqual(self.org.schedule.status()['status'], 'scheduled')

    async def test_daily_conflict_waits_for_review_without_overwriting(self):
        turn = (await self.seed())[0]
        old = self.service.add_memory('preference', '虚构偏好：喜欢乌龙茶但不喝咖啡', turn['user_event_id'])
        def conflict(data):
            data['candidates'][0]['conflicts'] = [old['id']]
            return data
        self.model.transform = conflict
        due = self.org.schedule.status()['next_epoch']
        self.org.schedule.daily_tick(due+1)
        await self.org.run_next()
        self.org.schedule.daily_tick(due+2)
        self.assertEqual(self.org.schedule.status()['status'], 'awaiting_review')
        self.assertEqual(self.org.candidates.list()[0]['status'], 'pending')
        self.assertEqual([m['id'] for m in self.service.search_memories()], [old['id']])
        self.assertEqual(self.org.summaries.list(), [])

    async def test_daily_freezes_persona_and_resumes_after_quota(self):
        turn = (await self.seed(12))[0]
        due = self.org.schedule.status()['next_epoch']
        self.org.schedule.daily_tick(due+1)
        other = self.service.create_persona(generic_persona())
        self.service.use_persona(other['id'])
        await self.org.run_next()
        self.org.schedule.daily_tick(due+2)
        self.cfg.update({'organization': {'hourly_limit': 1}})
        await self.org.run_next()
        self.org.schedule.daily_tick(due+3)
        self.assertEqual(self.org.schedule.status()['status'], 'waiting_quota')
        self.assertEqual(self.org.schedule.status()['remaining_turns'],2)
        self.assertEqual(self.org.schedule.status()['total_turns'],12)
        self.assertEqual(self.org.schedule.status()['persona_id'], self.persona['id'])
        self.cfg.update({'organization': {'hourly_limit': 6}})
        await self.org.run_next()
        self.org.schedule.daily_tick(due+4)
        self.service.use_persona(self.persona['id'])
        self.assertEqual(len(self.org.list()), 2)

    async def test_v4_upgrade_backup_and_rollback(self):
        legacy = Store(RuntimePaths(self.paths.home/'legacy'))
        legacy.paths.initialize()
        db = sqlite3.connect(legacy.path, isolation_level=None)
        for schema in (SCHEMA, SCHEMA2, SCHEMA3, SCHEMA4):
            execute_schema(db, schema)
        db.execute('PRAGMA user_version=4')
        db.close()
        with patch('persona_runtime.storage.SCHEMA5', 'CREATE TABLE partially_created (id TEXT);\nCREATE TABLE turns(id TEXT);\n'):
            with self.assertRaises(sqlite3.Error):
                legacy.initialize()
        db = sqlite3.connect(legacy.path)
        try:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 4)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='partially_created'").fetchone())
        finally:
            db.close()
        legacy.initialize()
        self.assertEqual(legacy.status()['schema_version'],19)
        self.assertTrue(list((legacy.paths.home/'backups').glob('before-m4-*.sqlite3')))

    async def test_management_is_admin_only_and_public_config_masks_both_keys(self):
        app = create_app(self.paths, self.model)
        async with TestServer(app) as server, ClientSession() as client:
            bridge = {'Authorization': 'Bearer '+self.cfg.token('bridge')}
            admin = {'Authorization': 'Bearer '+self.cfg.token()}
            async with client.post(server.make_url('/v1/manage'), headers=bridge, json={'action': 'organization.list'}) as response:
                self.assertEqual(response.status, 403)
            async with client.post(server.make_url('/v1/manage'), headers=admin, json={'action': 'organization.status'}) as response:
                data = await response.json()
                self.assertEqual(response.status, 200)
                self.assertIn('next_local_time', data)

    async def test_daily_explicit_run_now_resets_next_time_without_replaying_missed_days(self):
        await self.seed()
        started = time.time()
        status = self.org.schedule.run_daily_now()
        self.assertGreaterEqual(status['next_epoch'], started+86400)
        self.assertEqual(len(self.org.list()), 1)
        await self.org.run_next()
        self.org.schedule.daily_tick()
        count = len(self.model.calls)
        self.org.schedule.daily_tick(status['next_epoch']+3*86400)
        self.assertLessEqual(len(self.org.list()), 2)
        self.assertEqual(len(self.model.calls), count)

    async def test_model_change_pauses_daily_and_cancels_unstarted_work(self):
        await self.seed()
        self.org.schedule.run_daily_now()
        self.cfg.update({'organization': {'model': {'name': 'changed'}}})
        self.org.schedule.pause_changed_models()
        self.assertEqual(self.org.schedule.status()['status'], 'paused_model')
        self.assertEqual(self.org.list()[0]['status'], 'cancelled')
        await self.org.run_next()
        self.assertEqual(len(self.model.calls), 0)
        self.org.schedule.resume_daily()
        self.org.schedule.daily_tick()
        await self.org.run_next()
        self.assertEqual(self.model.calls[0][0]['name'], 'changed')

    async def test_disabling_daily_prevents_queued_call_and_requires_resume(self):
        await self.seed()
        self.org.schedule.run_daily_now()
        self.cfg.update({'organization': {'daily_enabled': False}})
        self.org.schedule.pause_changed_models()
        await self.org.run_next()
        self.assertEqual(len(self.model.calls), 0)
        self.assertEqual(self.org.schedule.status()['status'], 'paused')
        with self.assertRaisesRegex(RuntimeErrorBase,'daily_update_disabled'):
            self.org.schedule.resume_daily()

    async def test_daily_recover_blocks_instead_of_reissuing_paid_request(self):
        await self.seed()
        self.org.schedule.run_daily_now()
        self.org.recover()
        self.org.schedule.daily_tick()
        self.assertEqual(self.org.schedule.status()['status'], 'blocked')
        self.org.schedule.daily_tick(time.time()+90000)
        self.assertEqual(len(self.org.list()), 1)
        self.assertEqual(len(self.model.calls), 0)

    async def test_daily_old_scan_does_not_rewind_idle_boundary(self):
        await self.seed(12)
        self.org.schedule.set_automatic(True)
        self.org.schedule.run_daily_now()
        await self.org.run_next()
        with self.store.connection() as conn:
            row = conn.execute('SELECT * FROM organization_progress').fetchone()
            self.assertGreaterEqual(row['cursor'],row['boundary'])

    async def test_empty_candidates_are_valid_and_cancelled_failures_excluded(self):
        turn = (await self.seed())[0]
        self.model.transform = lambda data: data | {'candidates': []}
        result = await self.organize(turn['session_id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.org.candidates.list(), [])
        with self.store.connection(write=True) as conn:
            conn.execute("UPDATE turns SET status='failed' WHERE id=?", (turn['id'],))
        with self.assertRaisesRegex(RuntimeErrorBase,'no_eligible_turns'):
            self.org.preview(turn['session_id'], 'candidates')

    async def test_frontend_has_priority_and_background_never_holds_write_lock(self):
        turn = (await self.seed())[0]
        self.org.submit(self.org.preview(turn['session_id']), 'queued')
        with self.store.connection(write=True) as conn:
            conn.execute("UPDATE turns SET status='queued' WHERE id=?", (turn['id'],))
        self.assertFalse(await self.org.run_next())
        self.assertEqual(len(self.model.calls), 0)
        with self.store.connection(write=True) as conn:
            conn.execute("UPDATE turns SET status='completed' WHERE id=?", (turn['id'],))
        self.model.gate = asyncio.Event()
        running = asyncio.create_task(self.org.run_next())
        await asyncio.sleep(.01)
        self.service.set_relationship('模拟后台网络等待期间可以保存')
        self.model.gate.set()
        await running

    async def test_candidate_revision_checks_target_version_and_allows_explicit_resolution(self):
        turn = (await self.seed())[0]
        old = self.service.add_memory('preference', '虚构旧偏好', turn['user_event_id'])
        await self.organize(turn['session_id'])
        candidate = self.org.candidates.list()[0]
        with self.assertRaisesRegex(RuntimeErrorBase,'memory_version_conflict'):
            self.org.candidates.accept(candidate['id'],'bad',target_memory_id=old['id'],expected_revision=9)
        result = self.org.candidates.accept(candidate['id'],'good',target_memory_id=old['id'],expected_revision=1)
        self.assertEqual(self.service.show_memory(result['memory_id'])['revision'],2)
        self.assertEqual(len(self.service.search_memories()),1)

    async def test_excessive_input_and_invalid_output_lengths_never_publish_partial_rows(self):
        turn = (await self.seed())[0]
        self.cfg.update({'organization': {'model': {'max_input_chars':1000}}})
        with self.store.connection(write=True) as conn:
            conn.execute('UPDATE events SET text=? WHERE id=?',('虚构'*2000,turn['user_event_id']))
        with self.assertRaisesRegex(RuntimeErrorBase,'organization_input_too_long'):
            self.org.preview(turn['session_id'])
        self.cfg.update({'organization': {'model': {'max_input_chars':24000}}})
        def long(data):
            data['candidates'][0]['text']='长'*501
            return data
        self.model.transform=long
        result=await self.organize(turn['session_id'])
        self.assertEqual(result['status'],'failed')
        self.assertEqual(self.org.summaries.list(),[])

    async def test_export_has_evidence_without_credentials_and_backup_restores_schedule(self):
        turn=(await self.seed())[0]
        await self.organize(turn['session_id'])
        candidate=self.org.candidates.list()[0]
        self.org.candidates.accept(candidate['id'],'accept')
        data=json.loads(Path(self.service.export_persona()['path']).read_text(encoding='utf-8'))
        self.assertEqual(data['format_version'],16)
        self.assertEqual(len(data['candidate_reviews']),1)
        self.assertNotIn('encrypted_key',encoded(data))
        destination=Path(self.service.backup()['path'])
        db=sqlite3.connect(destination)
        try:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('SELECT next_epoch FROM daily_updates').fetchone()[0],self.org.schedule.status()['next_epoch'])
        finally:
            db.close()

    async def test_daily_can_approve_previously_manual_pending_candidate(self):
        turn=(await self.seed())[0]
        await self.organize(turn['session_id'],'candidates')
        candidate=self.org.candidates.list()[0]
        self.org.schedule.run_daily_now()
        await self.org.run_next()
        self.org.schedule.daily_tick()
        self.assertEqual(self.org.candidates.show(candidate['id'])['status'],'accepted')
        self.assertEqual(len(self.service.search_memories()),1)

    async def test_daily_existing_conflict_stays_pending_across_regeneration(self):
        turn=(await self.seed())[0]
        old=self.service.add_memory('preference','虚构偏好：不再喜欢乌龙茶',turn['user_event_id'])
        def conflict(data):
            data['candidates'][0]['conflicts']=[old['id']]
            return data
        self.model.transform=conflict
        await self.organize(turn['session_id'],'candidates')
        self.org.schedule.run_daily_now()
        await self.org.run_next()
        self.org.schedule.daily_tick()
        self.assertEqual(len(self.org.candidates.list()),1)
        self.assertEqual(self.org.schedule.status()['status'],'awaiting_review')

    async def test_reuse_chat_identity_preserves_background_request_limits(self):
        self.cfg.update({'model': {'max_tokens':100,'timeout':5},
                         'organization': {'reuse_chat':True,'model': {'max_tokens':2048,'timeout':60}}})
        model=self.org.model()
        self.assertEqual(model['name'],'fake')
        self.assertEqual(model['max_tokens'],2048)
        self.assertEqual(model['timeout'],60)

    async def test_disable_old_summary_keeps_independent_rebuild_active(self):
        turn=(await self.seed())[0]
        await self.organize(turn['session_id'],'summary')
        old=self.org.summaries.list()[0]
        await self.organize(turn['session_id'],'summary',start_row=0,rebuild=True)
        fresh=self.org.summaries.list()[0]
        self.org.summaries.disable(old['id'])
        self.assertEqual(self.org.summaries.show(fresh['id'])['status'],'active')


if __name__ == '__main__':
    unittest.main()
