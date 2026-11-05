"""Artificial-only six-pair preparation and execution checks; network blocked."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

from experiments import companion_comparison as comparison
from experiments.companion_trial import configure, configure_policy
from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.ingress import IngressService
from persona_runtime.models import generic_persona, identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import provider_identity
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


class ComparisonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=RuntimePaths.from_environment().home / 'tmp', prefix='comparison-artificial-unit-')
        self.addCleanup(self.temp.cleanup)
        self.network = patch('socket.socket.connect', side_effect=AssertionError('unit_network_blocked'))
        self.network.start(); self.addCleanup(self.network.stop)
        self.home = Path(self.temp.name)
        self.source = self.home / 'tmp/private-acceptance/artificial-A'
        paths = RuntimePaths(self.source)
        store = Store(paths); store.initialize()
        config = ConfigStore(paths); config.initialize()
        config.update({'model': {'name': 'artificial-model', 'base_url': 'http://127.0.0.1:9/v1'}})
        service = RuntimeService(store)
        self.pid = service.create_persona(generic_persona())['id']
        service.use_persona(self.pid)
        configure(paths, config.load(), self.pid)
        configure_policy(store, config, self.pid, 'minimal')
        atomic_json(self.source / 'private-sandbox.json', {'private_id': 'artificial-only',
                    'persona_id': self.pid, 'formal_home': str(self.home.resolve())})
        atomic_json(self.source / 'runtime/private-copy-policy.json', {'persona_id': self.pid,
                    'approved_models': {'main': provider_identity(config.load()['model']),
                                        'selector': provider_identity(config.load()['character_selector'])}})
        engine = ChatEngine(store, config, client=comparison.FakeClient())
        ingress = IngressService(engine)
        receipt = ingress.accept({'channel': 'desktop', 'text': '这是一条独立人工前文。', 'request_id': identifier()})
        batch = ingress.get(receipt['batch_id'])
        ingress.action(batch['id'], 'seal', batch['revision'], identifier(), self.pid)
        await engine.wait()
        self.assertEqual(engine.requests()[0]['status'], 'completed')
        engine.cognitive.characters.close(); engine.cognitive.shadow.close()
        self.trial = self.home / 'tmp/companion-trials/artificial-trial'
        atomic_json(self.trial / 'trial.json', {'version': 1, 'private': True, 'status': 'prepared',
            'source_home': str(self.home), 'review_deadline': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            'arms': {label: {'home': str(self.source), 'persona_id': self.pid, 'mode': mode}
                     for label, mode in [('A', 'minimal'), ('B', 'current')]}})

    def prepare(self):
        with patch.object(comparison, 'copy_weights'):
            return comparison.prepare(self.trial, expected_source_turns=1, development_material=True)

    def read(self, path):
        return json.loads(path.read_text(encoding='utf-8'))

    async def test_prepare_copies_same_complete_prefix_and_registers_all_private_homes(self):
        original = comparison._sha(self.source / 'data/database/runtime.sqlite3')
        root = self.prepare(); plan = comparison.load_plan(root)
        self.assertEqual(len(plan['positions']), 12)
        self.assertEqual(len(set(plan['cleanup_homes'])), 12)
        self.assertEqual(len(plan['pairs']), 6)
        for row in plan['positions']:
            self.assertIn(row['home'], plan['cleanup_homes'])
            home = Path(row['home'])
            self.assertEqual(home.parent, self.home / 'tmp/private-acceptance')
            self.assertTrue((home / 'runtime/private-copy-policy.json').is_file())
            conn = sqlite3.connect(home / 'data/database/runtime.sqlite3')
            try:
                self.assertEqual(conn.execute('SELECT count(*) FROM turns').fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT count(*) FROM turns WHERE status='completed'").fetchone()[0], 1)
            finally: conn.close()
        self.assertEqual(comparison._sha(self.source / 'data/database/runtime.sqlite3'), original)
        comparison.verify_frozen(root, plan)

    async def test_run_creates_one_new_main_and_matches_actual_manifest_without_replaying_prefix(self):
        root = self.prepare()
        result = await comparison.run_pcar(root, 'p01-minimal', fake=True)
        self.assertEqual(result['status'], 'completed', result.get('error_code'))
        self.assertEqual(result['physical_calls'], 0)
        self.assertEqual(result['simulated_calls'], 1)
        self.assertTrue(result['manifest_matches_actual_input'])
        request = self.read(root / 'positions/p01-minimal/request.json')
        self.assertIn('这是一条独立人工前文。', str(request['messages'][1:-1]))
        self.assertTrue(request['wire']['stream'])
        self.assertNotIn('max_tokens', request['wire'])
        with self.assertRaises(FileExistsError):
            await comparison.run_pcar(root, 'p01-minimal', fake=True)
        self.assertEqual(len(list((root / 'positions/p01-minimal').glob('dispatch-*'))), 1)

    async def test_wish_change_members_are_one_complete_ordered_input(self):
        root = self.prepare()
        result = await comparison.run_pcar(root, 'p02-minimal', fake=True)
        self.assertEqual(result['status'], 'completed', result.get('error_code'))
        current = self.read(root / 'positions/p02-minimal/request.json')['messages'][-1]['content']
        first, second = comparison.CONTINUATIONS[1][1]
        self.assertLess(current.index(first), current.index(second))

    async def test_public_capture_recursively_excludes_credentials(self):
        value = {'name': 'artificial', 'encrypted_key': 'private-cipher',
                 'nested': {'api_key': 'private-key', 'admin_token': 'private-token', 'temperature': 0.7}}
        self.assertEqual(comparison.public_model(value), {'name': 'artificial', 'nested': {'temperature': 0.7}})

    async def test_prefix_or_plan_change_rejected_before_dispatch(self):
        root = self.prepare(); plan = comparison.load_plan(root)
        changed = dict(plan); changed['created_at'] = 'tampered'
        atomic_json(root / 'plan.json', changed)
        with self.assertRaisesRegex(Exception, 'comparison_plan_changed'):
            await comparison.run_pcar(root, 'p01-minimal', fake=True)
        atomic_json(root / 'plan.json', plan)
        conn = sqlite3.connect(self.source / 'data/database/runtime.sqlite3')
        try:
            conn.execute('PRAGMA user_version=19'); conn.commit()
        finally: conn.close()
        with self.assertRaisesRegex(Exception, 'comparison_prefix_changed'):
            await comparison.run_pcar(root, 'p01-minimal', fake=True)

    async def test_changed_clone_cannot_use_same_position(self):
        root = self.prepare(); row = comparison.position(comparison.load_plan(root), 'p01-minimal')
        conn = sqlite3.connect(Path(row['home']) / 'data/database/runtime.sqlite3')
        try:
            conn.execute('PRAGMA user_version=19'); conn.commit()
        finally: conn.close()
        result = await comparison.run_pcar(root, 'p01-minimal', fake=True)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error_code'], 'comparison_initial_prefix_changed')
        self.assertTrue((root / 'positions/p01-minimal/terminal.json').is_file())
        with self.assertRaisesRegex(Exception, 'comparison_batch_stopped'):
            await comparison.run_pcar(root, 'p02-minimal', fake=True)

    async def test_fake_package_cannot_dispatch_real_and_partial_report_is_not_real_evidence(self):
        root = self.prepare()
        with self.assertRaisesRegex(Exception, 'comparison_simulation_scope_mismatch'):
            await comparison.run_pcar(root, 'p01-minimal', fake=False)
        await comparison.run_pcar(root, 'p01-minimal', fake=True)
        report = comparison.report(root)
        self.assertNotIn('这是独立人工夹具', report.read_text(encoding='utf-8'))
        self.assertFalse(self.read(root / 'report-status.json')['real_pairs_complete'])
        summary = self.read(root / 'comparison-summary.json')
        self.assertFalse(summary['pairs'][0]['actual_history_equal'])
        self.assertTrue(summary['source_prefix_is_not_effective_history'])

    async def test_interrupted_started_position_blocks_any_new_position(self):
        root = self.prepare()
        atomic_json(root / 'positions/p01-minimal/started.json', {'kind': 'simulated'})
        with self.assertRaisesRegex(Exception, 'comparison_unfinished_position_no_auto_resume'):
            await comparison.run_pcar(root, 'p02-minimal', fake=True)
        self.assertFalse((root / 'positions/p02-minimal/started.json').exists())

    async def test_real_entry_blocks_before_any_client_dispatch_when_local_preparation_is_unavailable(self):
        ready = {'status': 'ready', 'representative_hybrid': True}
        with patch.object(comparison, 'copy_weights'), patch.object(comparison, 'local_retrieval_ready', return_value=ready):
            root = comparison.prepare(self.trial, expected_source_turns=1)
        blocked = comparison.RuntimeErrorBase('comparison_local_preparation_blocked')
        blocked.preparation_reason = 'character_cache_missing'
        with patch.object(comparison, 'local_retrieval_ready', side_effect=blocked), \
             patch.object(comparison.ChatCompletionsClient, 'complete', new=AsyncMock()) as dispatch:
            result = await comparison.run_pcar(root, 'p01-minimal')
        dispatch.assert_not_awaited()
        self.assertEqual(result['status'], 'preparation_blocked')
        self.assertEqual(result['preparation_reason'], 'character_cache_missing')
        self.assertEqual(result['physical_calls'], 0)
        self.assertFalse(list((root / 'positions/p01-minimal').glob('dispatch-*.json')))

    async def test_missing_weights_leave_preparation_blocked_instead_of_a_runnable_frozen_package(self):
        with patch.object(comparison, 'copy_weights', side_effect=comparison.RuntimeErrorBase('embedding_unavailable')):
            with self.assertRaisesRegex(Exception, 'comparison_local_preparation_blocked'):
                comparison.prepare(self.trial, expected_source_turns=1)
        roots = list((self.trial / 'comparison').iterdir())
        self.assertEqual(len(roots), 1)
        self.assertEqual(self.read(roots[0] / 'plan.json')['status'], 'preparation_blocked')
        self.assertFalse((roots[0] / 'freeze.json').exists())


class LocalPreparationTests(unittest.TestCase):
    def runtime(self):
        events = []
        index = SimpleNamespace(provider=SimpleNamespace(diagnose=lambda: {'status': 'ready'}),
                                build=lambda *args: events.append('build'))
        runtime = SimpleNamespace(_scope=lambda *args: ({'id': 'artificial-version'}, {'config': {}}),
                                  index=lambda *args: index,
                                  status=lambda *args: {'cache': {'state': 'ready'}},
                                  local_pool=lambda *args: events.append('query') or {
                                      'model_fingerprint': 'artificial-model', 'index_fingerprint': 'artificial-index'})
        return runtime, events

    def test_existing_index_build_then_readback_and_query_warm_are_synchronous(self):
        runtime, events = self.runtime()
        value = comparison.local_retrieval_ready(runtime, 'artificial-persona', ['人工输入'], build=True)
        self.assertEqual(events, ['build', 'query'])
        self.assertEqual(value['status'], 'ready')
        self.assertTrue(value['representative_hybrid'])

    def test_run_check_does_not_rebuild_missing_cache_or_queue_a_worker(self):
        runtime, events = self.runtime()
        runtime.status = lambda *args: {'cache': {'state': 'unavailable', 'reason': 'character_cache_missing'}}
        with self.assertRaisesRegex(Exception, 'comparison_local_preparation_blocked') as caught:
            comparison.local_retrieval_ready(runtime, 'artificial-persona', ['人工输入'])
        self.assertEqual(caught.exception.preparation_reason, 'character_cache_missing')
        self.assertEqual(events, [])

    def test_fixture_bypass_is_explicit_and_does_not_claim_hybrid_readiness(self):
        runtime = Mock()
        value = comparison.local_retrieval_ready(runtime, 'artificial-persona', ['人工输入'], development_material=True)
        self.assertEqual(value, {'status': 'fixture_bypass', 'representative_hybrid': False})
        runtime.assert_not_called()

    def test_actual_manifest_lexical_fallback_is_rejected_before_delegate_dispatch(self):
        conn = Mock()
        conn.execute.return_value.fetchone.return_value = [json.dumps({'character_retrieval': {
            'requested_mode': 'hybrid', 'effective_mode': 'lexical', 'degrade_reason': 'embedding_busy'}})]
        store = Mock()
        store.connection.return_value.__enter__ = Mock(return_value=conn)
        store.connection.return_value.__exit__ = Mock(return_value=False)
        capture = comparison.CapturingClient(Mock(), None, None, None, store=store, representative=True)
        with self.assertRaisesRegex(Exception, 'comparison_local_preparation_blocked'):
            capture._check_actual_retrieval()
        self.assertEqual(capture.preparation_blocked_reason, 'embedding_busy')
        self.assertEqual(capture.calls, [])


if __name__ == '__main__':
    unittest.main()
