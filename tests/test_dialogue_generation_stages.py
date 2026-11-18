"""Offline first-receipt, cancellation, private grants and restart tests using opaque tokens."""
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from persona_runtime.config import ConfigStore
from persona_runtime.dialogue_generation import DialogueGeneration, STAGE_MODELS
from persona_runtime.dialogue_receipt import parse_receipt
from persona_runtime.models import generic_persona, identifier, RuntimeErrorBase, timestamp
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import PrivatePersonas, provider_identity
from persona_runtime.provider_compat import build_request
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


BUNDLE = 'a' * 64
BASE = 'https://api.deepseek.com'


class BoundAssembly:
    def guard(self, db, turn, material):
        if material['bundle_id'] != BUNDLE:
            raise ValueError('bundle_scope')

    def selectable(self, material):
        return material['assets']

    def request(self, stage, turn, material, thought, ids):
        public = {'base_url': BASE, 'name': STAGE_MODELS.get(stage, 'deepseek-v4-pro'),
                  'max_tokens': 1000, 'timeout': 60, 'max_input_chars': 60000}
        token = {'understanding': 'ID-U', 'selection': 'json-ID-S', 'reply': 'ID-R', 'search_notice': 'ID-N'}[stage]
        if stage == 'selection':
            public['_json_output'] = True
        return public, build_request(public, [{'role': 'user', 'content': token}])[0]

    def parse_selection(self, body, material):
        return json.loads(body)

    def projection(self, body, material):
        return {'display_body': body, 'removed_spans': []}


class Client:
    def __init__(self):
        self.wires = []
        self.failure = False
        self.start = asyncio.Event()
        self.block = None
        self.saved = None
        self.selection = '["1"]'

    async def complete(self, model, wire, *, save_raw):
        self.wires.append(wire)
        self.start.set()
        if self.block:
            await self.block.wait()
        content = self.selection if wire.get('response_format') else 'ID-OUTPUT-' + str(len(self.wires))
        raw = json.dumps({'model': model['name'], 'choices': [{'finish_reason': 'stop', 'message': {'content': content}}],
                          'usage': {'total_tokens': 4}}).encode()
        self.saved = lambda: save_raw(raw, http_status=200, diagnostics={'elapsed_ms': 10})
        if self.failure:
            raise TimeoutError()
        self.saved()


class DirectGeneration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        paths = RuntimePaths(Path(temp.name))
        config = ConfigStore(paths)
        config.initialize()
        config.update({'model': {'base_url': BASE, 'name': 'deepseek-flash'}})
        self.store = Store(paths)
        self.store.initialize()
        service = RuntimeService(self.store)
        self.pid = service.create_persona(generic_persona())['id']
        service.use_persona(self.pid)
        self.session = service.create_session()['id']
        self.client = Client()
        self.g = DialogueGeneration(self.store, self.client, lambda db, turn, context: None)
        self.a = BoundAssembly()
        self.credentials = {'base_url': BASE, 'encrypted_key': 'KEY-ID'}
        self.g.load_assembly = lambda db, pid, bundle: self.a
        self.g.configure(self.pid, {'enabled': True, 'bundle_id': BUNDLE}, 0, identifier())
        self.turn = identifier()
        with self.store.connection(write=True) as db:
            at = timestamp()
            db.execute("""INSERT INTO turns(id,source_key,persona_id,session_id,channel,status,
                          model_snapshot,created_at,updated_at) VALUES(?,?,?,?,'desktop','running','{}',?,?)""",
                       (self.turn, self.turn, self.pid, self.session, at, at))

    def register(self, phase='main', assets=True):
        with self.store.connection(write=True) as db:
            turn = dict(db.execute('SELECT * FROM turns WHERE id=?', (self.turn,)).fetchone())
            return self.g.register(db, turn, phase, {'bundle_id': BUNDLE, 'assets': assets}, {})

    async def run_unit(self, uid):
        return await self.g.execute(uid, self.credentials, self.a)

    async def test_main_and_notice_have_distinct_single_dispatches(self):
        main, notice = self.register(), self.register('search_notice')
        self.assertEqual((await self.run_unit(notice))['state'], 'completed')
        first = await self.run_unit(main)
        self.assertEqual((await self.run_unit(main)), first)
        status = self.g.status(self.turn)
        self.assertNotIn('ID-OUTPUT', json.dumps(status))
        self.assertNotIn('json-ID-S', json.dumps(status))
        self.assertEqual([w['messages'][0]['content'] for w in self.client.wires],
                         ['ID-N', 'ID-U', 'json-ID-S', 'ID-R'])
        self.assertEqual(len({r['attempt_id'] for r in status['stages']}), 4)
        self.assertTrue(all(r['usage']['total_tokens'] == 4 and r['actual_charge'] is None for r in status['stages']))
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM dialogue_generation_stages WHERE result_json IS NOT NULL").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT artifact_json FROM dialogue_generation_stages WHERE stage='selection'").fetchone()[0], '["1"]')

    async def test_no_assets_skips_selector(self):
        self.assertEqual((await self.run_unit(self.register(assets=False)))['state'], 'completed')
        self.assertEqual(len(self.client.wires), 2)

    async def test_local_preflight_is_recoverable_before_physical_dispatch(self):
        uid = self.register(assets=False)
        def blocked(model, wire):
            raise ValueError('ID-PREFLIGHT')
        self.client.preflight = blocked
        with self.assertRaisesRegex(RuntimeErrorBase, 'preflight_blocked'):
            await self.run_unit(uid)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM dialogue_generation_stages').fetchone()[0], 0)
        self.client.preflight = lambda model, wire: None
        self.assertEqual((await self.run_unit(uid))['state'], 'completed')
        self.assertEqual(len(self.client.wires), 2)

    async def test_unknown_and_late_first_receipt_never_replay(self):
        uid = self.register()
        self.client.failure = True
        result = await self.run_unit(uid)
        self.assertEqual(result['state'], 'unknown')
        self.assertIsNone(self.g.status(self.turn)['stages'][0]['usage'])
        self.g.recover()
        self.client.saved()
        self.g.recover()
        self.assertEqual((await self.run_unit(uid))['state'], 'unknown')
        self.assertEqual(len(self.client.wires), 1)
        self.assertEqual(self.g.status(self.turn)['stages'][0]['usage'], {'total_tokens': 4})

    async def test_concurrent_execution_and_restart_during_active_stage(self):
        uid = self.register(assets=False)
        self.client.block = asyncio.Event()
        live = asyncio.create_task(self.run_unit(uid))
        await self.client.start.wait()
        self.g.recover()
        duplicate = await self.run_unit(uid)
        self.assertEqual(duplicate['state'], 'sending')
        self.client.block.set()
        self.assertEqual((await live)['state'], 'completed')
        self.assertEqual(len(self.client.wires), 2)

    async def test_restart_after_raw_callback_resumes_only_outstanding_stage(self):
        uid = self.register(assets=False)
        original = self.client.complete
        async def interrupted(*args, **kwargs):
            await original(*args, **kwargs)
            raise asyncio.CancelledError()
        self.client.complete = interrupted
        with self.assertRaises(asyncio.CancelledError):
            await self.run_unit(uid)
        with self.store.connection() as db:
            row = db.execute('SELECT state,raw_response,result_json FROM dialogue_generation_stages').fetchone()
            self.assertEqual(row['state'], 'sending')
            self.assertIsNotNone(row['raw_response'])
            self.assertIsNone(row['result_json'])
        self.client.complete = original
        restarted = DialogueGeneration(self.store, self.client, lambda db,turn,context: None)
        restarted.recover()
        self.assertEqual(len(self.client.wires), 1)
        result = await restarted.execute(uid,self.credentials,self.a)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual([w['messages'][0]['content'] for w in self.client.wires], ['ID-U','ID-R'])

    async def test_cancel_between_dispatch_and_receipt_keeps_bytes_only(self):
        uid = self.register(assets=False)
        self.client.block = asyncio.Event()
        live = asyncio.create_task(self.run_unit(uid))
        await self.client.start.wait()
        self.g.cancel_root(self.turn)
        self.client.block.set()
        with self.assertRaisesRegex(RuntimeErrorBase, 'turn_closed'):
            await live
        self.assertEqual(len(self.client.wires), 1)
        self.assertTrue(self.g.status(self.turn)['stages'][0]['raw_hash'])
        self.assertTrue(self.g.status(self.turn)['closed'])

    async def test_invalid_selection_cannot_advance_to_reply(self):
        uid = self.register()
        self.client.selection = '{}'
        for _ in range(2):
            self.assertEqual((await self.run_unit(uid))['error_code'], 'dialogue_selection_artifact_invalid')
        self.assertEqual(len(self.client.wires), 2)

    async def test_bundle_policy_cannot_change_frozen_turn(self):
        self.g.configure(self.pid, {'enabled': False}, 1, identifier())
        with self.assertRaisesRegex(RuntimeErrorBase, 'policy_changed'):
            await self.run_unit(self.register())
        self.assertEqual(self.client.wires, [])

    async def test_role_grants_are_separate_and_completed_understanding_is_reused(self):
        uid = self.register()
        ConfigStore(self.store.paths).update({'model':{'base_url':BASE + '/v1'}})
        self.credentials['base_url'] = BASE + '/v1'
        def grant(stage):
            model = {'base_url': BASE, 'name': STAGE_MODELS[stage]}
            self.g.authorize(self.pid, stage, provider_identity(model), identifier())
        with patch.object(PrivatePersonas, 'record_for_persona', return_value={'id': 'PRIVATE-ID'}):
            with self.assertRaisesRegex(RuntimeErrorBase, 'provider_not_authorized'):
                await self.run_unit(uid)
            self.assertFalse(self.client.wires)
            grant('understanding')
            with self.assertRaisesRegex(RuntimeErrorBase, 'provider_not_authorized'):
                await self.run_unit(uid)
            self.assertEqual(len(self.client.wires), 1)
            grant('selection')
            with self.assertRaisesRegex(RuntimeErrorBase, 'provider_not_authorized'):
                await self.run_unit(uid)
            # This fixture's Pro endpoint is bare. The grant for a /v1 Pro
            # endpoint is distinct even though its credential is managed together.
            self.g.authorize(self.pid, 'reply', provider_identity(
                {'base_url':BASE + '/v1','name':STAGE_MODELS['reply']}), identifier())
            with self.assertRaisesRegex(RuntimeErrorBase, 'provider_not_authorized'):
                await self.run_unit(uid)
            ConfigStore(self.store.paths).update({'model':{'base_url':BASE}})
            grant('reply')
            self.assertEqual((await self.run_unit(uid))['state'], 'completed')
            self.assertEqual(len(self.client.wires), 3)

    def test_sse_preserves_body_and_requires_first_terminal_receipt(self):
        packet = {'model': 'deepseek-v4-pro', 'choices': [{'index': 0, 'delta': {'content': ' 1 '}, 'finish_reason': 'stop'}]}
        prefix = b'data: ' + json.dumps(packet).encode() + b'\n\n'
        parsed = parse_receipt(prefix + b'data: [DONE]\n\n', True)
        self.assertEqual(parsed['body'], ' 1 ')
        self.assertIsNone(parsed['usage'])
        self.assertTrue(parse_receipt(prefix, True)['incomplete_stream'])
        malformed = {'model':'deepseek-v4-pro','choices':[{'finish_reason':'stop',
                     'message':{'content':chr(0xd800)}}]}
        self.assertFalse(parse_receipt(json.dumps(malformed).encode(),False)['valid'])
