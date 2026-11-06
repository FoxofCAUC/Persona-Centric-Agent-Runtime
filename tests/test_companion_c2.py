"""Artificial C2 normal-runtime replay, failure and privacy boundaries."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments import companion_c2 as run
from persona_runtime.chat import ModelError
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.storage import Store


class Replay(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=DEFAULT_HOME / 'tmp', prefix='c2-artificial-')
        self.home = Path(self.temp.name).resolve()
        self.assertEqual(self.home.parent, (DEFAULT_HOME / 'tmp').resolve())
        self.addCleanup(self.temp.cleanup)
        paths = RuntimePaths(self.home)
        Store(paths).initialize()
        config = ConfigStore(paths)
        config.initialize()
        config.update({'model': {'base_url': 'http://127.0.0.1:9/v1', 'name': 'grok-4.7', 'hourly_limit': 0}})
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('network_forbidden')))

    async def test_sixteen_runtime_targets_only_contract_diff_and_no_success_replay(self):
        root = await run.prepare(self.home)
        owner, plan = run.verify(root)
        self.assertEqual(len(plan['positions']), 16)
        self.assertTrue(all(r['only_contract_changed'] for r in plan['pair_checks']))
        client = run.base.FixtureClient()
        result = await run.execute(root, simulated=True, client=client)
        self.assertEqual(result['completed'], 16)
        self.assertEqual(result['physical_calls'], 0)
        self.assertEqual(result['simulated_calls'], 16)
        self.assertEqual(len(client.calls), 16)
        for position_id, call in zip(plan['order'], client.calls):
            record = run.read(run.data_root(owner) / 'positions' / position_id / 'request.json')
            self.assertEqual(record['wire'], call['wire'])
        with self.assertRaisesRegex(Exception, 'no_auto_resume'):
            await run.execute(root, simulated=True, client=client)

    async def test_retry_recovery_and_exhaustion_preserve_input(self):
        root = await run.prepare(self.home)
        _, plan = run.verify(root)
        fixture = run.base.FixtureClient()
        calls = []

        class Client:
            async def complete(self, model, messages):
                calls.append(messages)
                if len(calls) == 1:
                    raise ModelError('provider_network_error')
                return await fixture.complete(model, messages)

        async def no_delay(_):
            pass

        with patch('persona_runtime.generation_retry.retry_delay', no_delay):
            result = await run.run_position(root, plan['order'][0], simulated=True, client=Client())
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['simulated_calls'], 2)
        self.assertEqual(calls[0], calls[1])
        with self.assertRaisesRegex(Exception, 'already_completed'):
            await run.run_position(root, plan['order'][0], simulated=True)

        class Broken:
            async def complete(self, model, messages):
                raise ModelError('provider_network_error')

        with patch('persona_runtime.generation_retry.retry_delay', no_delay):
            result = await run.run_position(root, plan['order'][1], simulated=True, client=Broken())
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['simulated_calls'], 4)
        with self.assertRaisesRegex(Exception, 'batch_stopped'):
            await run.run_position(root, plan['order'][2], simulated=True)

    async def test_terminal_failure_keeps_breakpoint_and_actual_call_count(self):
        root = await run.prepare(self.home)
        owner, plan = run.verify(root)
        original = run.atomic_json

        def fail(path, value):
            if Path(path).name == 'terminal.json':
                raise OSError('fixture_write_failure')
            return original(path, value)

        client = run.base.FixtureClient()
        with patch.object(run, 'atomic_json', side_effect=fail):
            with self.assertRaises(OSError):
                await run.run_position(root, plan['order'][0], simulated=True, client=client)
        self.assertEqual(len(client.calls), 1)
        report = run.report(root)
        self.assertEqual(report['unfinished_positions'], 1)
        self.assertEqual(report['simulated_calls'], 1)
        with self.assertRaisesRegex(Exception, 'unfinished_no_auto_resume'):
            await run.run_position(root, plan['order'][0], simulated=True)

    async def test_cancellation_has_terminal_and_stops_following_positions(self):
        root = await run.prepare(self.home)
        owner, plan = run.verify(root)
        entered = asyncio.Event()

        class Waiting:
            async def complete(self, model, messages):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(run.run_position(root, plan['order'][0], simulated=True, client=Waiting()))
        await asyncio.wait_for(entered.wait(), 30)
        atomic_json(run.data_root(owner) / 'cancel.json', {'reason': 'user'})
        result = await asyncio.wait_for(task, 10)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['usage']['generation_retry']['attempts'][0]['status'], 'interrupted')

    async def test_tamper_and_real_scope_rejected_before_dispatch(self):
        root = await run.prepare(self.home)
        owner, plan = run.verify(root)
        with self.assertRaisesRegex(Exception, 'simulation_scope_mismatch'):
            await run.run_position(root, plan['order'][0])
        path = run.data_root(owner) / 'preflight' / plan['order'][0] / 'request.json'
        value = run.read(path)
        value['messages'][-1]['content'] = 'modified'
        atomic_json(path, value)
        with self.assertRaisesRegex(Exception, 'frozen_request_changed'):
            await run.run_position(root, plan['order'][0], simulated=True)

    async def test_reservation_write_failure_never_dispatches(self):
        root = await run.prepare(self.home)
        _, plan = run.verify(root)
        original = run.atomic_json

        def fail(path, value):
            if Path(path).name.endswith('.dispatch.json'):
                raise OSError('fixture_write_failure')
            return original(path, value)

        client = run.base.FixtureClient()
        with patch.object(run, 'atomic_json', side_effect=fail):
            result = await run.run_position(root, plan['order'][0], simulated=True, client=client)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['simulated_calls'], 0)
        self.assertEqual(client.calls, [])


class ApprovedCopy(unittest.TestCase):
    def test_current_author_data_copied_without_reopening_deleted_receipt(self):
        from tests.test_companion_expression_adoption import AdoptionTests
        from persona_runtime.models import identifier
        from experiments import companion_expression_adoption as adoption
        temp = tempfile.TemporaryDirectory(dir=DEFAULT_HOME / 'tmp', prefix='c2-approved-artificial-')
        home = Path(temp.name).resolve()
        self.assertEqual(home.parent, (DEFAULT_HOME / 'tmp').resolve())
        self.addCleanup(temp.cleanup)
        fixture = AdoptionTests('test_review_is_read_only_and_exact_approval_is_required')
        with patch.object(RuntimePaths, 'from_environment', return_value=RuntimePaths(home)):
            fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.approve()
        saved = fixture.save()
        binding = Path(saved['binding_path'])
        receipt = binding.with_suffix('.copy.json')
        atomic_json(receipt, {'status': 'deleted_after_user_review', 'reopen_allowed': False})
        before = receipt.read_bytes()
        root = fixture.paths.home / 'tmp' / run.NAMESPACE / identifier()
        root.mkdir(parents=True)
        owner = {'version': run.VERSION, 'status': 'preparing', 'source_home': str(fixture.paths.home)}
        paths, pid = run.create_copy(fixture.paths.home, binding, owner, root)
        expected = adoption._visible_data(adoption._versions(fixture.store, fixture.pid))
        actual = adoption._versions(Store(paths), pid)
        self.assertEqual({k: v['data'] for k, v in actual.items()}, expected)
        self.assertEqual(receipt.read_bytes(), before)
        self.assertFalse(any(owner['initial_counts'].values()))
        self.assertEqual([r['id'] for r in expected['character']['lorebook']], ['public'])
        fixture.check_originals()
