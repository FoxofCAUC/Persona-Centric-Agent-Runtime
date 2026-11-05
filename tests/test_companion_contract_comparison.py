"""Artificial-only contract-language integration checks; no real providers."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments import companion_contract_comparison as comparison
from experiments import companion_order_comparison as order
from persona_runtime.archive import fingerprint
from persona_runtime.bubble_contract import V3
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.provider_compat import build_request
from persona_runtime.reply_batch import prompt_contract
from persona_runtime.storage import Store
from tests.test_companion_order_comparison import CountingClient


class ContractComparisonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=DEFAULT_HOME / 'tmp', prefix='contract-artificial-unit-')
        self.addCleanup(self.temp.cleanup)
        self.source_home = Path(self.temp.name)
        self.paths = RuntimePaths(self.source_home)
        Store(self.paths).initialize()
        config = ConfigStore(self.paths); config.initialize()
        config.update({'model': {'name': 'grok-4.7', 'base_url': 'http://127.0.0.1:9/v1', 'hourly_limit': 0}})
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('contract_unit_network_blocked')))

    async def prepared(self):
        return await comparison.prepare(self.source_home, development_material=True)

    @staticmethod
    def read(path):
        return json.loads(Path(path).read_text(encoding='utf-8'))

    def resign(self, root, plan):
        atomic_json(root / 'plan.json', plan)
        freeze = self.read(root / 'freeze.json'); freeze['plan_hash'] = fingerprint(plan)
        atomic_json(root / 'freeze.json', freeze)

    async def test_prepare_contract_namespace_and_only_contract_tail_changes(self):
        root = await self.prepared(); plan = comparison.load_plan(root)
        self.assertEqual(root.parent.name, 'companion-contract')
        self.assertEqual(plan['version'], comparison.VERSION)
        self.assertEqual(plan['experiment_kind'], 'contract_language')
        self.assertEqual(len(plan['cases']), 3)
        self.assertEqual(len(plan['positions']), 6)
        self.assertFalse(list((root / 'positions').glob('*/started.json')))
        for case in plan['cases']:
            rows = [row for row in plan['positions'] if row['case_id'] == case['id']]
            values = {row['condition']: self.read(root / row['request_file']) for row in rows}
            control, variant = values['control'], values['variant']
            contract = prompt_contract(control['model']['_reply_mode'], control['model']['_expression_snapshot'])
            self.assertTrue(control['messages'][0]['content'].endswith(contract))
            material_prefix = control['messages'][0]['content'][:-len(contract)]
            self.assertTrue(variant['messages'][0]['content'].startswith(material_prefix))
            self.assertNotEqual(control['messages'][0]['content'], variant['messages'][0]['content'])
            self.assertEqual(control['messages'][1:], variant['messages'][1:])
            self.assertEqual(control['model'], variant['model'])
            self.assertEqual({key: value for key, value in control['wire'].items() if key != 'messages'},
                             {key: value for key, value in variant['wire'].items() if key != 'messages'})
            self.assertNotIn('encrypted_key', variant['model'])
        comparison.verify(root)

    async def test_all_six_fake_positions_complete_with_exact_frozen_inputs_and_parameters(self):
        root = await self.prepared(); plan = comparison.load_plan(root)
        client = CountingClient(text='人工协议回包，真实人物效果未验证。')
        for position_id in plan['order']:
            result = await comparison.run_position(root, position_id, fake=True, client=client)
            self.assertEqual(result['status'], 'completed', result.get('error_code'))
            self.assertEqual(result['physical_calls'], 0)
            self.assertEqual(result['simulated_calls'], 1)
            row = next(row for row in plan['positions'] if row['id'] == position_id)
            request = self.read(root / row['request_file'])
            model, messages = client.calls[-1]
            self.assertEqual(messages, request['messages'])
            self.assertEqual(build_request(model, messages)[0], request['wire'])
        self.assertEqual(len(client.calls), 6)
        comparison.verify(root)
        comparison.report(root)
        self.assertFalse(self.read(root / 'report-status.json')['real_pairs_complete'])

    async def test_wrappers_and_order_cli_reject_the_other_experiment_kind(self):
        old_root = await order.prepare(self.source_home, development_material=True)
        client = CountingClient(); old_plan = order.load_plan(old_root)
        with self.assertRaisesRegex(Exception, 'kind_mismatch'):
            await comparison.run_position(old_root, old_plan['order'][0], fake=True, client=client)
        self.assertEqual(client.calls, [])
        self.assertFalse((old_root / 'execution.lock').exists())
        self.assertFalse(list((old_root / 'positions').glob('*/started.json')))
        root = await self.prepared(); console = io.StringIO()
        with patch('sys.argv', ['companion_order_comparison.py', 'verify', str(root)]), redirect_stdout(console):
            with self.assertRaises(SystemExit) as exit_info:
                order.main()
        self.assertEqual(exit_info.exception.code, 1)
        self.assertIn('companion_comparison_kind_mismatch', console.getvalue())
        self.assertFalse((root / 'execution.lock').exists())

    async def test_kind_version_namespace_mismatch_and_unknown_kind_never_dispatch(self):
        root = await self.prepared(); original = comparison.load_plan(root); client = CountingClient()
        mutations = [
            {'version': order.VERSION},
            {'experiment_kind': 'block_order', 'version': order.VERSION},
            {'experiment_kind': 'unregistered_experiment'},
        ]
        for mutation in mutations:
            changed = deepcopy(original); changed.update(mutation); self.resign(root, changed)
            with self.subTest(mutation=mutation):
                with self.assertRaisesRegex(Exception, 'kind|scope|version|outside'):
                    await comparison.run_position(root, original['order'][0], fake=True, client=client)
                self.assertFalse((root / 'execution.lock').exists())
                self.assertFalse(list((root / 'positions').glob('*/started.json')))
        self.resign(root, original)
        with self.assertRaisesRegex(Exception, 'simulation_scope_mismatch'):
            await comparison.run_position(root, original['order'][0], fake=False, client=client)
        self.assertEqual(client.calls, [])

    async def test_successful_contract_position_is_not_replayed(self):
        root = await self.prepared(); plan = comparison.load_plan(root); client = CountingClient()
        first = plan['order'][0]
        result = await comparison.run_position(root, first, fake=True, client=client)
        self.assertEqual(result['status'], 'completed')
        with self.assertRaises(FileExistsError):
            await comparison.run_position(root, first, fake=True, client=client)
        self.assertEqual(len(client.calls), 1)

    async def test_unknown_started_position_blocks_contract_batch_without_resume(self):
        root = await self.prepared(); plan = comparison.load_plan(root); client = CountingClient()
        first, second = plan['order'][:2]
        atomic_json(root / 'positions' / first / 'started.json', {'kind': 'simulated'})
        with self.assertRaisesRegex(Exception, 'unfinished_no_auto_resume'):
            await comparison.run_position(root, second, fake=True, client=client)
        self.assertEqual(client.calls, [])
        self.assertFalse((root / 'positions' / second / 'started.json').exists())

    async def test_common_display_projection_keeps_raw_reply_and_one_native_generation(self):
        root = await self.prepared(); plan = comparison.load_plan(root)
        client = CountingClient(text='有个想法。')
        position = plan['order'][0]
        result = await comparison.run_position(root, position, fake=True, client=client)
        self.assertEqual(result['status'], 'completed', result.get('error_code'))
        self.assertEqual(result['reply'], '有个想法。')
        self.assertEqual(result['decoded_segments'][0]['text'], '有个想法。')
        self.assertEqual(json.loads(result['raw'])['messages'][0]['content'], '有个想法。')
        self.assertEqual(result['display'], '有个想法')
        self.assertEqual(result['display_projection'], V3.presentation)
        self.assertTrue(result['display_projection_diagnostics'][0]['terminal_period_removed'])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result['simulated_calls'], 1)

    async def test_projection_failure_preserves_completed_generation_and_does_not_retry(self):
        root = await self.prepared(); plan = comparison.load_plan(root)
        client = CountingClient(text='保留原始回包。'); position = plan['order'][0]
        with patch('persona_runtime.chat_display.bubble_plan', side_effect=RuntimeErrorBase('artificial_projection_failed')):
            result = await comparison.run_position(root, position, fake=True, client=client)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error_code'], 'artificial_projection_failed')
        self.assertEqual(len(client.calls), 1)
        output = root / 'positions' / position
        generation = self.read(output / 'generation-result.json')
        self.assertEqual(generation['reply'], '保留原始回包。')
        self.assertEqual(json.loads(generation['raw'])['messages'][0]['content'], '保留原始回包。')
        attempts = self.read(output / 'audit.json')['generation_retry']['attempts']
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]['status'], 'completed')
        self.assertEqual(self.read(root / 'state.json')['status'], 'stopped')


if __name__ == '__main__':
    unittest.main()
