"""Normal desktop/QQ generation, live permissions and pending-result reuse."""

from concurrent.futures import Future
from contextlib import closing
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest.mock import patch

from persona_runtime.archive import fingerprint
from persona_runtime.config import atomic_json
from persona_runtime.delivery import DeliveryService
from persona_runtime.knowledge_package import file_digest, DATA_REVISION, PROGRAM_VERSION, MODEL
from persona_runtime.knowledge_service import KnowledgeService, should_retrieve
from persona_runtime.models import RuntimeErrorBase, generic_persona, identifier
from tests import test_m4 as fixtures


class FakeRetriever:
    def __init__(self, paths, package):
        manifest = json.loads((package / 'manifest.json').read_text(encoding='utf-8'))
        self.identity = manifest['database']['sha256']
        self.cache = {}
        self.lock = threading.RLock()
        self.ready = True
        self.closed = False
        self.calls = []
        self.pending = False
        quote = '月球是地球唯一的天然卫星。'
        self.result = {'state': 'ready', 'package_sha256': self.identity,
            'candidates': [{'article_id': 1, 'title': '月球'}], 'evidence': [{
                'kind': 'encyclopedia', 'package_sha256': self.identity,
                'data_revision': 'fixture', 'article_id': 1, 'article_title': '月球',
                'section_id': 11, 'section_title': '', 'start': 0, 'end': len(quote),
                'quote': quote, 'section_sha256': hashlib.sha256(quote.encode()).hexdigest(),
                'source_snapshot_at': None, 'validity': 'package_snapshot',
                'url': 'https://zh.wikipedia.org/wiki/月球'}]}

    def search(self, query, **kwargs):
        self.calls.append(query)
        if self.pending:
            return {'state': 'pending', 'request_key': 'fixture', 'package_sha256': self.identity}
        result = copy.deepcopy(self.result)
        if not kwargs['allowed'](result['candidates'][0]):
            result.update(candidates=[], evidence=[])
        return result

    def close(self):
        self.closed = True


class CapturingModel:
    def __init__(self):
        self.calls = []
        self.gate = None

    async def complete(self, model, messages):
        self.calls.append(copy.deepcopy(messages))
        if self.gate:
            await self.gate.wait()
        return '月球是地球的天然卫星。', {'total_tokens': 7}


class KnowledgeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.M4Tests.setUp

    async def asyncTearDown(self):
        if getattr(self, 'capture', None) and self.capture.gate:
            self.capture.gate.set()
        await self.chat.wait()
        self.chat.knowledge.close()

    def enable(self):
        package = self.paths.home / 'data/knowledge/packages/fixture'
        package.mkdir(parents=True)
        database = package / 'body.db'
        with closing(sqlite3.connect(database)) as conn, conn:
            conn.executescript("CREATE TABLE sections(id INTEGER PRIMARY KEY,article_id INTEGER,content TEXT);"
                "INSERT INTO sections VALUES(11,1,'月球是地球唯一的天然卫星。');")
        (package / 'wikilite.exe').write_bytes(b'artificial-owned-program')
        atomic_json(package / 'manifest.json', {'state': 'staged', 'database': {
            'sha256': file_digest(database), 'file': database.name,
            'embedded_model': {'sha256': MODEL.sha256}}, 'data_revision': DATA_REVISION,
            'program_version': PROGRAM_VERSION, 'program_sha256': file_digest(package / 'wikilite.exe')})
        self.chat.knowledge.close()
        self.chat.knowledge = KnowledgeService(self.store, FakeRetriever)
        self.knowledge = self.chat.knowledge
        self.package = package
        self.identity = file_digest(database)
        self.program_patch = patch('persona_runtime.knowledge_service.PROGRAM_SHA256', file_digest(package / 'wikilite.exe'))
        self.program_patch.start()
        self.addCleanup(self.program_patch.stop)
        self.knowledge.activate_package('fixture', 0, 'activate')
        self.knowledge.update_policy(self.persona['id'],
            {'enabled': True, 'channels': ['desktop', 'qq']}, 0, 'enable')
        self.capture = CapturingModel()
        self.chat.client = self.capture

    async def ask(self, text='地球的天然卫星叫什么', channel='desktop', **kwargs):
        turn = await self.chat.submit(channel, text, channel + '-' + identifier(), **kwargs)
        await self.chat.wait()
        return self.chat.get(turn['id'])

    async def test_default_off_and_four_chitchat_routes(self):
        fixtures = Path(__file__).parents[1] / 'templates/knowledge-retrieval-cases.json'
        cases = json.loads(fixtures.read_text(encoding='utf-8'))['cases']
        for row in cases:
            if not row['retrieval_required']:
                self.assertFalse(should_retrieve(row['query']))
        self.assertFalse(self.chat.knowledge.current_policy(self.persona['id'])['data']['enabled'])
        self.assertEqual(self.chat.knowledge.status()['preparation']['revision'], 0)
        self.assertIsNone(self.chat.knowledge.status()['p95_target_seconds'])
        self.assertTrue(should_retrieve('最近在看黑洞的资料'))
        self.assertFalse(should_retrieve('你是谁'))
        self.enable()
        for query in ('早上好呀', '我今天有点累，陪我聊会儿', '哈哈你这说法挺逗', '晚安，明天见'):
            result = await self.ask(query)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['context']['knowledge']['state'], 'not_triggered')
        self.assertIsNone(self.knowledge.backend)

    async def test_both_entrypoints_bind_actual_evidence_and_hashes(self):
        self.enable()
        for channel, kwargs in (('desktop', {}), ('qq', {'persona_id': self.persona['id'],
                'origin': {'sender': '123', 'bot': '456'}})):
            result = await self.ask(channel=channel, **kwargs)
            self.assertEqual(result['status'], 'completed', result.get('error_code'))
            self.assertIn('月球是地球唯一的天然卫星。', self.capture.calls[-1][0]['content'])
            with self.store.connection() as conn:
                manifest = json.loads(conn.execute('SELECT manifest_json FROM context_manifests '
                    'WHERE turn_id=? ORDER BY rowid DESC', (result['id'],)).fetchone()[0])
            self.assertEqual(manifest['input_snapshot_hash'], fingerprint(self.capture.calls[-1]))
            self.assertEqual(len(manifest['knowledge']['evidence']), 1)
            self.assertNotIn('quote', manifest['knowledge']['evidence'][0])
            shown = self.knowledge.show_evidence(self.persona['id'], result['id'])
            self.assertEqual(shown['evidence'][0]['quote'], '月球是地球唯一的天然卫星。')

    async def test_time_questions_use_knowledge_in_both_entrypoints(self):
        self.enable()
        for query, channel, kwargs in (
                ('受控验收公告A里，灯塔每天几点开放？', 'desktop', {}),
                ('受控驗收公告裡，燈塔每天幾點開放？', 'qq', {'persona_id': self.persona['id'],
                    'origin': {'sender': '123', 'bot': '456'}})):
            self.assertTrue(should_retrieve(query))
            result = await self.ask(query, channel=channel, **kwargs)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['context']['knowledge']['state'], 'ready')
            self.assertEqual(len(result['context']['knowledge']['evidence']), 1)
            self.assertEqual(self.knowledge.backend.calls[-1], query)

    async def test_cached_result_and_saved_future_filtered_after_revoke(self):
        self.enable()
        result = self.knowledge.search(self.persona['id'], '月球是什么')
        prepared = copy.deepcopy(result)
        self.knowledge.invalidate(self.identity, 1, 'withdrawn', 'fixture retraction', 0, 'withdraw')
        self.assertEqual(self.knowledge.search(self.persona['id'], '月球是什么')['evidence'], [])
        with self.store.connection() as conn:
            checked = self.knowledge.recheck(conn, {'persona_id': self.persona['id'], 'channel': 'desktop'}, prepared)
        self.assertEqual(checked['evidence'], [])
        self.assertEqual(checked['candidates'], [])

    async def test_scope_and_source_permissions(self):
        self.enable()
        other = self.service.create_persona(generic_persona())
        self.assertEqual(self.knowledge.search(other['id'], '月球是什么')['state'], 'disabled')
        policy = {'enabled': True, 'channels': ['desktop']}
        self.knowledge.update_policy(self.persona['id'], policy, 1, 'desktop-only')
        self.assertEqual(self.knowledge.search(self.persona['id'], '月球是什么', 'qq')['state'], 'disabled')
        self.knowledge.update_sources({'wikipedia_zh': {'enabled': False}, 'wikidata': {'enabled': True}}, 0, 'source-off')
        self.assertEqual(self.knowledge.search(self.persona['id'], '月球是什么')['state'], 'disabled')

    async def test_pending_followup_reuses_original_query_in_same_session(self):
        self.enable()
        self.knowledge.search(self.persona['id'], '月球是什么')
        self.knowledge.backend.pending = True
        first = await self.ask()
        self.assertEqual(first['context']['knowledge']['state'], 'pending')
        self.knowledge.backend.pending = False
        result = await self.ask('查到了吗', session_id=first['session_id'])
        self.assertEqual(result['context']['knowledge']['state'], 'ready')
        self.assertEqual(self.knowledge.backend.calls[-1], '地球的天然卫星叫什么')

    async def test_delivery_checks_live_sources_even_for_repeated_claim(self):
        self.enable()
        result = await self.ask(channel='qq', persona_id=self.persona['id'],
                                origin={'sender': '123', 'bot': '456'})
        delivery = DeliveryService(self.store)
        stream = delivery.by_batch(result['reply_batch']['id'])
        part = stream['parts'][0]
        self.knowledge.invalidate(self.identity, 1, 'corrected', 'fixture correction', 0, 'correction')
        answer = delivery.claim(stream['id'], part['id'], stream['revision'], 'claim',
            persona_id=self.persona['id'], capabilities=['reply_batch_v2', 'reply_rhythm_v1', 'reply_bubbles_v3', 'reply_display_messages_v1'])
        self.assertFalse(answer['claimed'])
        self.assertEqual(answer['reason'], 'knowledge_evidence_changed')
        self.assertFalse(delivery.claim(stream['id'], part['id'], stream['revision'], 'claim',
            persona_id=self.persona['id'])['claimed'])
        with self.assertRaisesRegex(RuntimeErrorBase, 'knowledge_evidence_changed'):
            self.knowledge.show_evidence(self.persona['id'], result['id'])

    async def test_generation_retraction_preserves_usage_and_blocks_reply(self):
        import asyncio
        self.enable()
        self.capture.gate = asyncio.Event()
        result = await self.chat.submit('desktop', '月球是什么', 'running-retraction')
        for _ in range(100):
            if self.capture.calls:
                break
            await asyncio.sleep(.01)
        self.assertTrue(self.capture.calls)
        self.knowledge.invalidate(self.identity, 1, 'withdrawn', 'during generation', 0, 'in-flight')
        self.capture.gate.set()
        await self.chat.wait()
        result = self.chat.get(result['id'])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error_code'], 'knowledge_evidence_changed')
        self.assertEqual(result['usage']['total_tokens'], 7)
        self.assertIsNone(result['reply_batch'])

    async def test_policy_idempotency_version_and_package_tampering(self):
        self.enable()
        data = {'enabled': True, 'channels': ['desktop', 'qq']}
        self.assertEqual(self.knowledge.update_policy(self.persona['id'], data, 0, 'enable')['revision'], 1)
        with self.assertRaisesRegex(RuntimeErrorBase, 'version_conflict'):
            self.knowledge.update_policy(self.persona['id'], data, 0, 'stale')
        with self.assertRaisesRegex(RuntimeErrorBase, 'operation_id_conflict'):
            self.knowledge.update_policy(self.persona['id'], {'enabled': False, 'channels': ['desktop']}, 0, 'enable')
        (self.package / 'body.db').write_bytes(b'changed')
        self.assertEqual(self.knowledge.search(self.persona['id'], '月球是什么')['state'], 'quarantined')

    async def test_retraction_removes_completed_reply_from_context_and_desktop_read(self):
        from persona_runtime.archive import read_exact, scope
        self.enable()
        result = await self.ask()
        self.knowledge.invalidate(self.identity, 1, 'withdrawn', 'history invalidation', 0, 'history')
        self.assertIsNone(self.chat.get(result['id'])['reply'])
        with self.store.connection() as conn:
            self.assertEqual(read_exact(conn, scope(self.persona['id'], 'desktop'),
                {'kind': 'turn', 'id': result['id']})['status'], 'unavailable')
            from persona_runtime.organization import eligible_turns
            self.assertEqual(eligible_turns(conn, result['session_id']), [])

    async def test_source_recheck_before_dispatch_and_budget_exclusion(self):
        from persona_runtime.knowledge_context import attach
        self.enable()
        result = await self.ask()
        prepared = self.chat.cognitive.prepare(result['id'])
        self.knowledge.invalidate(self.identity, 1, 'withdrawn', 'before dispatch', 0, 'before')
        with self.store.connection() as conn:
            turn = dict(conn.execute('SELECT * FROM turns WHERE id=?', (result['id'],)).fetchone())
            checked = self.chat.cognitive.finalize(conn, turn, prepared)
        self.assertEqual(checked['details']['knowledge']['evidence'], [])
        messages = [{'role': 'system', 'content': 'required'}, {'role': 'user', 'content': 'question'}]
        original = copy.deepcopy(messages)
        messages, details = attach(messages, {}, self.knowledge.backend.result,
            {'model_snapshot': json.dumps({'max_input_chars': 16})})
        self.assertEqual(messages, original)
        self.assertEqual(details['knowledge']['state'], 'budget_excluded')

    async def test_authority_survives_restart_and_v24_migration_is_additive(self):
        from persona_runtime.storage import Store, SCHEMA_VERSION
        self.enable()
        self.knowledge.invalidate(self.identity, 1, 'withdrawn', 'durable', 0, 'durable')
        restored = KnowledgeService(self.store, FakeRetriever)
        self.addCleanup(restored.close)
        self.assertEqual(restored.current_policy(self.persona['id'])['revision'], 1)
        self.assertEqual(restored.search(self.persona['id'], '月球是什么')['evidence'], [])
        with self.store.connection(write=True) as conn:
            from persona_runtime.schema_m9a3 import TABLES
            for table in reversed(TABLES):conn.execute('DROP TABLE '+table)
            conn.execute('DROP TABLE knowledge_control')
            conn.execute('PRAGMA user_version=24')
        self.store.initialize()
        with self.store.connection() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(conn.execute('SELECT count(*) FROM knowledge_control').fetchone()[0], 0)
            self.assertTrue(conn.execute('SELECT 1 FROM personas WHERE id=?', (self.persona['id'],)).fetchone())
        self.assertTrue(list((self.paths.home / 'backups').glob('before-m9a-*.sqlite3')))

    async def test_package_switch_waits_for_owned_work_and_supports_rollback(self):
        import shutil
        self.enable()
        self.knowledge.search(self.persona['id'], '月球是什么')
        original = self.knowledge.backend
        pending = Future()
        original.cache['in-flight'] = (0, pending)
        second = self.package.parent / 'second'
        shutil.copytree(self.package, second)
        with closing(sqlite3.connect(second / 'body.db')) as conn, conn:
            conn.execute("INSERT INTO sections VALUES(12,2,'地球是行星。')")
        manifest = json.loads((second / 'manifest.json').read_text(encoding='utf-8'))
        manifest['database']['sha256'] = file_digest(second / 'body.db')
        atomic_json(second / 'manifest.json', manifest)
        self.knowledge.activate_package('second', 1, 'switch')
        self.assertEqual(self.knowledge.search(self.persona['id'], '月球是什么')['state'], 'pending')
        self.assertFalse(original.closed)
        pending.set_result(None)
        self.assertEqual(self.knowledge.search(self.persona['id'], '月球是什么')['state'], 'ready')
        self.assertTrue(original.closed)
        self.knowledge.activate_package('fixture', 2, 'rollback')
        self.assertEqual(self.knowledge.search(self.persona['id'], '月球是什么')['package_sha256'], self.identity)

    async def test_admin_management_api_and_bridge_boundary(self):
        from aiohttp import ClientSession
        from aiohttp.test_utils import TestServer
        from persona_runtime.server import create_app
        self.enable()
        app = create_app(self.paths, self.capture)
        app['engine'].knowledge.factory = FakeRetriever
        async with TestServer(app) as server, ClientSession() as client:
            payload = {'action': 'knowledge.search', 'args': {
                'persona_id': self.persona['id'], 'query': '月球是什么'}}
            async with client.post(server.make_url('/v1/manage'), json=payload,
                    headers={'Authorization': 'Bearer ' + self.cfg.token()}) as response:
                self.assertEqual(response.status, 200)
                self.assertTrue((await response.json())['evidence'])
            async with client.post(server.make_url('/v1/manage'), json=payload,
                    headers={'Authorization': 'Bearer ' + self.cfg.token('bridge')}) as response:
                self.assertEqual(response.status, 403)


if __name__ == '__main__':
    unittest.main()
