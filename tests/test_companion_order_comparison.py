"""Artificial-only prompt-order comparison checks; never real providers."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from experiments import companion_order_comparison as comparison
from persona_runtime.archive import fingerprint
from persona_runtime.chat import ModelCompletion
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.storage import Store


class CountingClient:
    def __init__(self, *, error=None, text='这句普通接话也应原样保留。'):
        self.calls = []
        self.error = error
        self.text = text

    async def complete(self, model, messages):
        self.calls.append((deepcopy(model), deepcopy(messages)))
        if self.error is not None:
            raise self.error
        body = json.dumps({'schema_version': 3, 'behavior_intent': 'respond', 'messages': [
            {'content': self.text, 'content_kind': 'chat', 'cancel_if_new_input': False}
        ]}, ensure_ascii=False)
        return ModelCompletion(body, {'total_tokens': 3}, {'finish_reason': 'stop'})


class OrderComparisonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=DEFAULT_HOME / 'tmp', prefix='order-artificial-unit-')
        self.addCleanup(self.temp.cleanup)
        self.source_home = Path(self.temp.name)
        self.paths = RuntimePaths(self.source_home)
        Store(self.paths).initialize()
        config = ConfigStore(self.paths)
        config.initialize()
        config.update({'model': {'name': 'grok-4.7', 'base_url': 'http://127.0.0.1:9/v1',
                                 'hourly_limit': 0}})
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('order_unit_network_blocked')))

    async def prepared(self):
        return await comparison.prepare(self.source_home, development_material=True)

    @staticmethod
    def read(path):
        return json.loads(Path(path).read_text(encoding='utf-8'))

    @staticmethod
    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    async def test_prepare_freezes_only_order_differences_with_no_real_positions_dispatched(self):
        root = await self.prepared()
        plan = comparison.load_plan(root)
        self.assertEqual(len(plan['cases']), 3)
        self.assertEqual(len(plan['positions']), 6)
        self.assertTrue(plan['development_material'])
        self.assertFalse(list((root / 'positions').glob('*/started.json')))
        for case in plan['cases']:
            pair = [row for row in plan['positions'] if row['case_id'] == case['id']]
            by_condition = {row['condition']: self.read(root / row['request_file']) for row in pair}
            control, variant = by_condition['control'], by_condition['variant']
            self.assertEqual(control['messages'][1:], variant['messages'][1:])
            self.assertEqual(control['model'], variant['model'])
            self.assertEqual([m['role'] for m in control['messages']], [m['role'] for m in variant['messages']])
            self.assertEqual(Counter(control['messages'][0]['content']), Counter(variant['messages'][0]['content']))
            self.assertNotEqual(control['messages'][0]['content'], variant['messages'][0]['content'])
            self.assertEqual({k: v for k, v in control['wire'].items() if k != 'messages'},
                             {k: v for k, v in variant['wire'].items() if k != 'messages'})
            self.assertTrue(control['wire']['stream'])
            self.assertNotIn('max_tokens', control['wire'])
            self.assertNotIn('encrypted_key', control['model'])
            conn = sqlite3.connect(Path(case['home']) / 'data/database/runtime.sqlite3')
            try:
                self.assertEqual(conn.execute("SELECT count(*) FROM turns WHERE status IN ('queued','running')").fetchone()[0], 0)
                completed = conn.execute("SELECT count(*) FROM turns WHERE status='completed'").fetchone()[0]
                self.assertEqual(completed, 2 if case['id'] == 'O03' else 0)
                self.assertEqual(case['simulated_source_history'], case['id'] == 'O03')
            finally:
                conn.close()
        comparison.verify(root)

    async def test_fake_position_uses_one_frozen_request_and_never_replays_success(self):
        root = await self.prepared(); plan = comparison.load_plan(root)
        row = next(c for c in plan['cases'] if c['id'] == 'O01')
        source_db = Path(row['home']) / 'data/database/runtime.sqlite3'
        before = self.digest(source_db)
        client = CountingClient(text='就这样吧')
        result = await comparison.run_position(root, 'O01-control', fake=True, client=client)
        self.assertEqual(result['status'], 'completed', result.get('error_code'))
        self.assertEqual(result['physical_calls'], 0)
        self.assertEqual(result['simulated_calls'], 1)
        self.assertEqual(result['display'], '就这样吧')
        self.assertEqual(len(client.calls), 1)
        expected = self.read(root / next(p['request_file'] for p in plan['positions'] if p['id'] == 'O01-control'))
        self.assertEqual(client.calls[0][1], expected['messages'])
        self.assertEqual(self.digest(source_db), before)
        with self.assertRaises(FileExistsError):
            await comparison.run_position(root, 'O01-control', fake=True, client=client)
        self.assertEqual(len(client.calls), 1)

    async def test_development_package_cannot_dispatch_as_real(self):
        root = await self.prepared(); client = CountingClient()
        with self.assertRaisesRegex(Exception, 'scope|simulation|fixture'):
            await comparison.run_position(root, 'O01-control', fake=False, client=client)
        self.assertEqual(client.calls, [])

    async def test_source_database_change_is_rejected_before_any_dispatch(self):
        root = await self.prepared(); plan = comparison.load_plan(root)
        conn = sqlite3.connect(Path(plan['cases'][0]['home']) / 'data/database/runtime.sqlite3')
        try:
            conn.execute('PRAGMA user_version=19'); conn.commit()
        finally:
            conn.close()
        client = CountingClient()
        with self.assertRaises(Exception):
            await comparison.run_position(root, 'O01-control', fake=True, client=client)
        self.assertEqual(client.calls, [])

    async def test_source_permission_change_is_rejected_before_any_dispatch(self):
        root = await self.prepared(); plan = comparison.load_plan(root)
        policy = Path(plan['cases'][0]['home']) / 'runtime/private-copy-policy.json'
        data = self.read(policy); data['approval_scope'] = 'changed-since-freeze'; atomic_json(policy, data)
        client = CountingClient()
        with self.assertRaises(Exception):
            await comparison.run_position(root, 'O01-control', fake=True, client=client)
        self.assertEqual(client.calls, [])

    async def test_tampered_request_is_rejected_before_any_dispatch(self):
        root = await self.prepared(); plan = comparison.load_plan(root)
        request_path = root / plan['positions'][0]['request_file']
        data = self.read(request_path); data['messages'][-1]['content'] = 'changed after freeze'; atomic_json(request_path, data)
        client = CountingClient()
        with self.assertRaises(Exception):
            await comparison.run_position(root, 'O01-control', fake=True, client=client)
        self.assertEqual(client.calls, [])

    async def test_unfinished_position_blocks_next_position_without_autoresume(self):
        root = await self.prepared()
        atomic_json(root / 'positions/O01-control/started.json', {'kind': 'simulated'})
        client = CountingClient()
        with self.assertRaisesRegex(Exception, 'unfinished|incomplete|resume'):
            await comparison.run_position(root, 'O02-control', fake=True, client=client)
        self.assertEqual(client.calls, [])
        self.assertFalse((root / 'positions/O02-control/started.json').exists())

    async def test_nonretryable_failure_preserves_terminal_and_stops_batch(self):
        root = await self.prepared(); client = CountingClient(error=RuntimeErrorBase('provider_http_401'))
        result = await comparison.run_position(root, 'O01-control', fake=True, client=client)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(client.calls), 1)
        terminal = self.read(root / 'positions/O01-control/terminal.json')
        self.assertEqual(terminal['status'], 'failed')
        self.assertEqual(self.read(root / 'state.json')['status'], 'stopped')
        with self.assertRaisesRegex(Exception, 'stopped|unfinished'):
            await comparison.run_position(root, 'O02-control', fake=True, client=client)
        self.assertEqual(len(client.calls), 1)

    async def test_protocol_legal_low_quality_reply_is_not_regenerated_or_reported_as_real(self):
        root = await self.prepared(); client = CountingClient(text='嗯')
        result = await comparison.run_position(root, 'O01-control', fake=True, client=client)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['display'], '嗯')
        self.assertEqual(len(client.calls), 1)
        report = comparison.report(root)
        self.assertNotIn('\n\n嗯\n', report.read_text(encoding='utf-8'))
        self.assertFalse(self.read(root / 'report-status.json')['real_pairs_complete'])

    async def test_wrong_execution_order_is_rejected_before_dispatch(self):
        root = await self.prepared(); client = CountingClient()
        with self.assertRaisesRegex(Exception, 'order_comparison_execution_order_mismatch'):
            await comparison.run_position(root, 'O01-variant', fake=True, client=client)
        self.assertEqual(client.calls, [])
        self.assertFalse((root / 'positions/O01-variant/started.json').exists())

    async def test_relabelled_fake_package_with_unowned_source_home_is_rejected_before_lock(self):
        root = await self.prepared(); plan = comparison.load_plan(root)
        plan.update(development_material=False, private=True,
                    source_home=str(self.source_home / 'unowned-source-home'))
        atomic_json(root / 'plan.json', plan)
        freeze = self.read(root / 'freeze.json'); freeze['plan_hash'] = fingerprint(plan)
        atomic_json(root / 'freeze.json', freeze)
        client = CountingClient()
        with self.assertRaisesRegex(Exception, 'order_comparison_formal_source_scope'):
            await comparison.run_position(root, 'O01-control', fake=False, client=client)
        self.assertEqual(client.calls, [])
        self.assertFalse((root / 'execution.lock').exists())
        self.assertFalse((root / 'positions/O01-control/started.json').exists())

    async def test_legacy_order_plan_without_kind_keeps_old_display_behavior(self):
        root = await self.prepared(); plan = comparison.load_plan(root)
        self.assertEqual(plan.pop('experiment_kind'), 'block_order')
        atomic_json(root / 'plan.json', plan)
        freeze = self.read(root / 'freeze.json'); freeze['plan_hash'] = fingerprint(plan)
        atomic_json(root / 'freeze.json', freeze)
        self.assertEqual(comparison.load_plan(root)['version'], comparison.VERSION)
        comparison.verify(root)
        client = CountingClient(text='原来的展示句号。')
        result = await comparison.run_position(root, 'O01-control', fake=True, client=client)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['reply'], '原来的展示句号。')
        self.assertEqual(result['display'], '原来的展示句号。')
        self.assertNotIn('display_projection', result)
        self.assertEqual(len(client.calls), 1)


if __name__ == '__main__':
    unittest.main()
