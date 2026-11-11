"""Knowledge preparation has no completion deadline; UI polls the same task."""

import asyncio
import copy
import math
import unittest
from unittest.mock import patch

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from persona_runtime.models import RuntimeErrorBase
from persona_runtime.server import create_app
from tests import test_knowledge_integration as integration


class KnowledgeWaitTests(unittest.IsolatedAsyncioTestCase):
    setUp = integration.KnowledgeIntegrationTests.setUp
    asyncTearDown = integration.KnowledgeIntegrationTests.asyncTearDown
    enable = integration.KnowledgeIntegrationTests.enable
    ask = integration.KnowledgeIntegrationTests.ask

    def pending(self):
        self.enable()
        self.knowledge.search(self.persona['id'], '月球是什么')
        self.backend = self.knowledge.backend
        self.backend.pending = True
        self.addCleanup(setattr, self.backend, 'pending', False)

    async def until_waiting(self, turn):
        for _ in range(200):
            current = self.chat.get(turn['id'])
            if current['phase'] == 'knowledge_search':
                return current
            await asyncio.sleep(.01)
        self.fail('knowledge stage did not start')

    async def test_more_than_five_seconds_waits_and_injects_into_original_turn(self):
        self.pending()
        turn = await self.chat.submit('desktop', '月球是什么', 'slow-original')
        waiting = await self.until_waiting(turn)
        await asyncio.sleep(5.1)
        self.assertEqual(self.capture.calls, [])
        self.assertEqual(self.chat.lookup('desktop', 'slow-original')['id'], waiting['id'])
        self.backend.pending = False
        await self.chat.wait()
        done = self.chat.get(turn['id'])
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(len(self.capture.calls), 1)
        self.assertIn('月球是地球唯一的天然卫星。', self.capture.calls[0][0]['content'])
        self.assertGreater(done['timings']['knowledge_seconds'], 5)

    async def test_cancel_stops_waiter_and_never_dispatches_main(self):
        self.pending()
        turn = await self.chat.submit('desktop', '月球是什么', 'cancel-search')
        await self.until_waiting(turn)
        await self.chat.cancel(turn['id'])
        self.backend.pending = False
        await asyncio.sleep(.3)
        done = self.chat.get(turn['id'])
        self.assertEqual(done['phase'], 'cancelled')
        self.assertEqual(done['error_code'], 'cancelled_by_user')
        self.assertEqual(self.capture.calls, [])
        self.assertEqual(done['attempt_count'], 0)

    async def test_api_poll_disconnect_and_cancel_without_queued_cancel_setting(self):
        self.pending()
        app = create_app(self.paths, self.capture)
        app['engine'].knowledge.close()
        app['engine'].knowledge = self.knowledge
        self.chat = app['engine']
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        headers = {'Authorization': 'Bearer ' + self.cfg.token()}
        turn = await self.chat.submit('desktop', '月球是什么', 'disconnected-client')
        await self.until_waiting(turn)
        async with ClientSession(headers=headers) as client:
            async with client.post(server.make_url('/v1/manage'), json={
                    'action': 'knowledge.search', 'args': {'persona_id': self.persona['id'], 'query': '月球是什么'}}) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())['state'], 'pending')
        # A new transport observes the same persisted request, with no submit.
        async with ClientSession(headers=headers) as client:
            async with client.get(server.make_url('/v1/submissions/desktop/disconnected-client')) as response:
                found = await response.json()
                self.assertEqual(found['id'], turn['id'])
                self.assertEqual(found['phase'], 'knowledge_search')
            async with client.post(server.make_url(f"/v1/requests/{turn['id']}/cancel"), json={}) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())['phase'], 'cancelled')
        self.assertEqual(self.capture.calls, [])

    async def test_source_revocation_while_waiting_never_injects_old_evidence(self):
        self.pending()
        turn = await self.chat.submit('desktop', '月球是什么', 'revoke-search')
        await self.until_waiting(turn)
        self.knowledge.update_policy(self.persona['id'], {'enabled': False, 'channels': ['desktop', 'qq']}, 1, 'stop-reader')
        self.backend.pending = False
        await self.chat.wait()
        done = self.chat.get(turn['id'])
        self.assertEqual(done['status'], 'completed')
        self.assertNotIn('月球是地球唯一的天然卫星。', self.capture.calls[0][0]['content'])

    async def test_close_interrupts_and_explicit_retry_preserves_original_turn(self):
        self.pending()
        turn = await self.chat.submit('desktop', '月球是什么', 'shutdown-search')
        await self.until_waiting(turn)
        self.knowledge.close()
        await self.chat.wait()
        done = self.chat.get(turn['id'])
        self.assertEqual(done['status'], 'interrupted')
        self.assertEqual(done['error_code'], 'knowledge_stopped')
        self.assertEqual(done['attempt_count'], 0)
        from persona_runtime.knowledge_service import KnowledgeService
        from tests.test_knowledge_integration import FakeRetriever
        self.chat.knowledge = self.knowledge = KnowledgeService(self.store, FakeRetriever)
        resumed = await self.chat.retry(turn['id'])
        await self.chat.wait()
        self.assertEqual(resumed['id'], turn['id'])
        self.assertEqual(self.chat.get(turn['id'])['status'], 'completed')
        self.assertEqual(len(self.capture.calls), 1)

    async def test_no_match_and_worker_failure_are_distinct(self):
        self.enable()
        self.knowledge.search(self.persona['id'], '月球是什么')
        self.knowledge.backend.result.update(candidates=[], evidence=[])
        with patch.object(self.knowledge.backend, 'search', return_value={'state': 'ready', 'evidence': []}):
            empty = await self.ask()
        self.assertEqual(empty['status'], 'completed')
        self.assertIn('knowledge_no_matches', [e['event'] for e in empty['events']])
        with patch.object(self.knowledge.backend, 'search', return_value={'state': 'failed', 'reason': 'fixture'}):
            failed = await self.ask()
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['error_code'], 'knowledge_retrieval_failed')
        self.assertEqual(len(self.capture.calls), 1)

    async def test_inflight_request_keeps_old_package_after_activation(self):
        import json
        import shutil
        import sqlite3
        from contextlib import closing
        from persona_runtime.config import atomic_json
        from persona_runtime.knowledge_package import file_digest
        self.pending()
        turn = await self.chat.submit('desktop', '月球是什么', 'pinned-package')
        await self.until_waiting(turn)
        # Wait until the first pending result has bound its package identity.
        await asyncio.sleep(.1)
        second = self.package.parent / 'replacement'
        shutil.copytree(self.package, second)
        with closing(sqlite3.connect(second / 'body.db')) as db, db:
            db.execute("INSERT INTO sections VALUES(12,2,'新的包版本。')")
        manifest = json.loads((second / 'manifest.json').read_text(encoding='utf-8'))
        manifest['database']['sha256'] = file_digest(second / 'body.db')
        atomic_json(second / 'manifest.json', manifest)
        self.knowledge.activate_package('replacement', 1, 'new-active')
        self.backend.pending = False
        await self.chat.wait()
        done = self.chat.get(turn['id'])
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['context']['knowledge']['package_sha256'], self.identity)

    async def test_poll_timeout_validation_keeps_default_unbounded(self):
        self.enable()
        for timeout in (True, -1, 61, math.nan, math.inf, '5'):
            with self.assertRaisesRegex(RuntimeErrorBase, 'knowledge_timeout_invalid'):
                self.knowledge.search(self.persona['id'], '月球是什么', timeout=timeout)
        self.assertEqual(self.knowledge.search(self.persona['id'], '月球是什么')['state'], 'ready')



class KnowledgeDependencyWaitTests(unittest.IsolatedAsyncioTestCase):
    from tests import test_knowledge_maintenance as fixtures
    setUp = fixtures.MaintenanceTests.setUp
    asyncTearDown = fixtures.MaintenanceTests.asyncTearDown
    enable = fixtures.MaintenanceTests.enable
    submit = fixtures.MaintenanceTests.submit
    process = fixtures.MaintenanceTests.process

    async def start_correction(self):
        self.enable(); self.source.put(); self.submit(); await self.process()
        self.source.put(value='红色', revision=2); self.now += 2
        turn = await self.chat.submit('desktop', '测试行星最新颜色是什么', 'dependent-current')
        for _ in range(200):
            if self.chat.get(turn['id'])['phase'] == 'knowledge_refresh': return turn
            await asyncio.sleep(.01)
        self.fail('refresh did not start')

    async def test_review_dependency_respects_quota_and_resumes(self):
        turn = await self.start_correction()
        self.cfg.update({'organization': {'hourly_limit': 1, 'daily_limit': 1}})
        await self.process()
        for _ in range(200):
            if self.chat.get(turn['id'])['phase'] == 'knowledge_waiting_quota': break
            await asyncio.sleep(.01)
        self.assertEqual(self.chat.get(turn['id'])['phase'], 'knowledge_waiting_quota')
        self.assertEqual(len(self.processor.calls), 3)
        self.cfg.update({'organization': {'hourly_limit': 1000, 'daily_limit': 1000}})
        await self.process(); await self.chat.wait()
        done = self.chat.get(turn['id'])
        self.assertEqual(done['status'], 'completed')
        self.assertIn('红色', done['context']['knowledge']['evidence'][0]['claim_text'])
        self.assertEqual(len(self.processor.calls), 4)

    async def test_stopping_learning_unblocks_wait_with_explicit_reason(self):
        turn = await self.start_correction()
        policy = self.maintenance.policy(self.pid)
        self.maintenance.update_policy(self.pid, dict(policy['data'], enabled=False), policy['revision'], 'stop-learning')
        await self.chat.wait()
        done = self.chat.get(turn['id'])
        self.assertEqual(done['status'], 'failed')
        self.assertEqual(done['error_code'], 'knowledge_refresh_disabled')
        self.assertEqual(done['attempt_count'], 0)

    async def test_cancelled_dependency_cannot_bypass_foreground_priority(self):
        from persona_runtime.knowledge_wait import required_tasks
        turn = await self.start_correction()
        await self.maintenance.tick()
        with self.store.connection() as db:
            self.assertTrue(required_tasks(db))
        await self.chat.cancel(turn['id'])
        with self.store.connection() as db:
            self.assertEqual(required_tasks(db), set())


class KnowledgePreviewTests(unittest.TestCase):
    def test_preview_poll_keeps_query_and_stops_for_new_role(self):
        from types import SimpleNamespace
        from persona_runtime.desktop_knowledge import KnowledgeUI
        pending, scheduled, received = [], [], []
        state = {'role': 'first', 'query': '月球是什么'}
        w = SimpleNamespace(knowledge_preview_version=1,
            knowledge_key=lambda: (state['role'], 0),
            knowledge_query=SimpleNamespace(get=lambda: state['query']),
            knowledge_output=object(), set_text=lambda *args: received.append(args[-1]),
            after=lambda ms, callback: scheduled.append(callback),
            run_task=lambda *args, **kwargs: pending.append((args, kwargs)),
            client=SimpleNamespace(manage=lambda *args, **kwargs: kwargs))
        w.poll_knowledge_search = lambda *args: KnowledgeUI.poll_knowledge_search(w, *args)
        w.poll_knowledge_search(('first', 0), state['query'], 1)
        self.assertEqual(pending[0][0][2](None)['timeout'], 0)
        pending[0][0][3]({'state': 'pending'})
        self.assertEqual(len(scheduled), 1)
        state['role'] = 'second'
        scheduled[0]()
        self.assertEqual(len(pending), 1)
        self.assertFalse(pending[0][1]['error_current']())


if __name__ == '__main__':
    unittest.main()
