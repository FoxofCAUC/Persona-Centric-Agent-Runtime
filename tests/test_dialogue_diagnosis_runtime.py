"""CR1 isolated admission and physical receipts; no model inference needed."""
import asyncio
from copy import deepcopy
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from persona_runtime.archive import encoded
from persona_runtime.chat import ModelError
from persona_runtime.config import ConfigStore
from persona_runtime.generation_retry import audited_complete, GenerationRetriesExhausted
from persona_runtime.models import generic_persona
from persona_runtime.paths import RuntimePaths
from persona_runtime.reply_batch import validate_reply, expression_snapshot
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from scripts import dialogue_diagnosis_runtime as runtime
from scripts.dialogue_refactor import offline, NoDispatch, close
from tests.dialogue_diagnosis_fixture import fixture
from tests.dialogue_diagnosis_responses import reply


class Receipts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=runtime.HOME / 'tmp', prefix='cr1-receipts-')
        self.root = Path(self.temp.name)
        self.ledger = runtime.Ledger(self.root / 'receipts')
        self.model = {'name': 'grok-4.7', 'base_url': 'https://example.invalid/v1', 'max_tokens': 512, 'timeout': 1}
        self.messages = [{'role': 'user', 'content': '人工输入'}]
        self.saved = []
        self.delays = AsyncMock()

    def tearDown(self):
        self.temp.cleanup()

    async def complete(self, client, validate=lambda raw: None):
        with offline():
            return await audited_complete(client, self.model, self.messages, validate=validate,
                        save=lambda value: self.saved.append(deepcopy(value)), before_attempt=lambda _: None,
                        sleep=self.delays)

    async def test_three_retries_same_payload_and_success_never_replayed(self):
        client = runtime.MockClient(self.ledger, 's/main', '完成', failures=[ModelError('provider_timeout')] * 3)
        await self.complete(client)
        rows = self.ledger.rows()
        self.assertEqual([r['status'] for r in rows], ['failed'] * 3 + ['completed'])
        self.assertEqual([r['attempt_index'] for r in rows], [0, 1, 2, 3])
        self.assertEqual(len(client.received), 4)
        self.assertEqual([c.args[0] for c in self.delays.await_args_list], [1, 2, 4])
        with self.assertRaisesRegex(ValueError, 'cr1_retry_limit|cr1_success_never_replay'):
            self.ledger.reserve('s/main', self.model, self.messages, 'main')

    async def test_retry_exhaustion_has_four_failed_receipts(self):
        client = runtime.MockClient(self.ledger, 's/main', '', failures=[ModelError('provider_timeout')] * 4)
        with self.assertRaises(GenerationRetriesExhausted):
            await self.complete(client)
        self.assertEqual(len(client.received), 4)
        self.assertTrue(all(r['status'] == 'failed' and r['usage'] is None for r in self.ledger.rows()))

    async def test_recovery_does_not_add_runner_retry_layer(self):
        client = runtime.MockClient(self.ledger, 's/main', '完成', failures=[ModelError('provider_timeout')])
        await self.complete(client)
        self.assertEqual(len(client.received), 2)
        self.assertEqual(len(self.saved[-1]['generation_retry']['attempts']), 2)
        self.assertTrue(self.ledger.rows()[-1]['status'] == 'completed')

    async def test_cancellation_is_terminal_and_no_next_dispatch(self):
        client = runtime.MockClient(self.ledger, 's/main', '', failures=[asyncio.CancelledError()])
        with self.assertRaises(asyncio.CancelledError):
            await self.complete(client)
        self.assertEqual([r['status'] for r in self.ledger.rows()], ['interrupted'])
        with self.assertRaisesRegex(ValueError, 'cr1_unsettled_receipt'):
            self.ledger.reserve('s/next', self.model, self.messages, 'main')

    async def test_reservation_or_terminal_write_failure_cannot_be_success(self):
        client = runtime.MockClient(self.ledger, 's/main', '完成')
        with patch.object(self.ledger, 'reserve', side_effect=OSError('disk_failure')):
            with self.assertRaises(OSError):
                await self.complete(client)
        self.assertEqual(client.received, [])
        client = runtime.MockClient(self.ledger, 's/main', '完成')
        with patch.object(self.ledger, 'finish', side_effect=OSError('disk_failure')):
            with self.assertRaises(OSError):
                await self.complete(client)
        self.assertEqual(len(client.received), 1)
        self.assertEqual(self.ledger.rows()[0]['status'], 'reserved')
        with self.assertRaisesRegex(ValueError, 'cr1_unsettled_receipt'):
            self.ledger.reserve('s/next', self.model, self.messages, 'main')

    async def test_valid_but_bad_content_not_retried(self):
        # A deliberately wrong answer to a precise arithmetic task is legal JSON.
        # Content quality belongs to analysis, not protocol retry.
        raw = reply('D18')
        raw['messages'][0]['content'] = '999'
        client = runtime.MockClient(self.ledger, 's/main', encoded(raw))
        await self.complete(client, validate=lambda text: validate_reply(text, 'natural_structured', expression_snapshot('natural_structured')))
        self.assertEqual(len(client.received), 1)

    def test_request_mutation_and_orphan_terminal_are_rejected(self):
        row = self.ledger.reserve('s/main', self.model, self.messages, 'main')
        self.ledger.finish(row, 'failed')
        with self.assertRaisesRegex(ValueError, 'cr1_retry_request_changed'):
            self.ledger.reserve('s/main', self.model, [{'role': 'user', 'content': '变化'}], 'main')
        runtime.write_once(self.ledger.root / '9999.terminal.json', {'status': 'completed'})
        with self.assertRaisesRegex(ValueError, 'cr1_orphan_terminal'):
            self.ledger.rows()


class Admission(unittest.IsolatedAsyncioTestCase):
    async def test_real_admission_preserves_history_correction_and_empty_credentials(self):
        with tempfile.TemporaryDirectory(dir=runtime.HOME / 'tmp', prefix='cr1-admission-') as temporary:
            root = Path(temporary)
            store = Store(RuntimePaths(root / 'runtime'))
            store.initialize()
            case = fixture()['cases'][21]
            models = runtime.public_models()
            with offline():
                cfg = runtime.configure(store.paths, models, case)
                service = RuntimeService(store)
                pid = service.create_persona(generic_persona())['id']
                item = await runtime.admit(store, cfg, {'persona_id': pid}, case, 'weigh')
                copied = runtime.clone(str(store.paths.home), root / 'cloned-from-json-path')
                with copied.connection() as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM turns WHERE id=?', (item['turn_id'],)).fetchone()[0], 1)
                engine = runtime.ChatEngine(store, cfg, NoDispatch())
                try:
                    turn = runtime.turn_for(engine, item)
                    self.assertEqual(turn['status'], 'queued')
                    with store.connection() as db:
                        self.assertEqual([m['text'] for m in runtime.load_turn_input(db, turn)], case['members'])
                    self.assertEqual(cfg.load()['model']['encrypted_key'], '')
                    self.assertEqual(cfg.load()['character_selector']['encrypted_key'], '')
                finally:
                    close(engine)

    async def test_full_cohort_through_real_admission_and_selector_with_fake_recall(self):
        """Actual recall gets its separate local-model run; this test isolates wiring."""
        from persona_runtime.character import CharacterService
        from persona_runtime.character_semantic_index import sources
        from persona_runtime.character_policy import choose
        from persona_runtime.archive import fingerprint
        class Provider:
            def __init__(self, *args): pass
            def diagnose(self): return {'status': 'ready', 'test_only_fake_recall': True}
        class Index:
            def __init__(self, store, provider): self.store = store
            def build(self, *args): pass
            def query(self, pid, vid, members, **kw):
                version = CharacterService(self.store).show(pid, vid)
                rows = [row | {'score': .8, 'member_index': 0, 'chunk_index': 0} for row in sources(version)]
                return {'version_id': vid, 'model_fingerprint': choose('hybrid_llm')['config']['model_fingerprint'],
                        'index_fingerprint': fingerprint(['fake-recall-test-only', pid, vid]),
                        'by_kind': {kind: [r for r in rows if r['kind'] == kind] for kind in ('lore', 'example')}}
        with tempfile.TemporaryDirectory(dir=runtime.HOME / 'tmp', prefix='cr1-full-cohort-') as temporary:
            root = Path(temporary) / 'cohort'
            with offline(), patch.object(runtime, 'IsolatedEmbeddingProvider', Provider), \
                    patch.object(runtime, 'CharacterSemanticIndex', Index), redirect_stdout(io.StringIO()):
                prepared = await runtime.prepare(root)
                self.assertEqual(prepared['main_positions'], 144)
                result = await runtime.simulate(root)
                self.assertEqual(result['physical_simulations'], 168)
                self.assertEqual(result['real_calls'], 0)
                self.assertEqual(result['expression_effect'], 'unverified')


if __name__ == '__main__':
    unittest.main()
