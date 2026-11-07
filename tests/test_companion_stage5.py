"""New runner data-flow and failure regressions with fixture responses only."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments import companion_stage5 as run
from persona_runtime.chat import ModelError
from persona_runtime.config import ConfigStore
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths


class Runner(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=DEFAULT_HOME / 'tmp', prefix='stage5-unit-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.out = self.home / 'evidence'
        self.enterContext(patch.object(run, 'TEMP', self.home / 'tmp/companion-stage5'))
        config = ConfigStore(RuntimePaths(self.home))
        config.initialize()
        config.update({'model': {'name': 'deepseek-flash', 'base_url': 'https://offline.invalid/v1', 'hourly_limit': 0}})
        await run.prepare(self.out, self.home, fixture=True)

    async def test_full_44_mock_requests_memory_reopen_and_semantic_errors_preserved(self):
        result = await run.execute(self.out, simulated=True)
        self.assertEqual((result['completed'], result['physical_calls'], result['simulated_calls']), (44, 0, 44))
        rows = {r['id']: r for r in result['positions']}
        self.assertNotEqual(rows['V11-A']['reply'], '9')
        self.assertEqual(rows['V11-A']['physical_calls'], 1)
        self.assertEqual(len(rows['V06-B']['input_event_ids']), 3)
        self.assertEqual(rows['T01']['session_id'], rows['T20']['session_id'])
        self.assertTrue(run.read(self.out / 'simulation/reopen-result.json')['history_equal'])
        memory = run.read(self.out / 'simulation/positions/T15/manual-memory-operation.json')
        self.assertEqual(memory['result']['revision'], 2)
        request = run.read(self.out / 'simulation/positions/T17/request.json')
        self.assertIn('先绘画', str(request['messages']))
        owner, _ = run.verify(self.out)
        self.assertFalse(any(run.idle(RuntimePaths(Path(owner['temporary_root']) / 'simulation')).values()))
        with self.assertRaisesRegex(Exception, 'no_automatic_resume'):
            await run.execute(self.out, simulated=True)
        with self.assertRaisesRegex(Exception, 'fixture_real_execution_forbidden'):
            await run.execute(self.out)

    async def test_changed_model_and_binding_rejected(self):
        owner, _ = run.verify(self.out)
        cfg = ConfigStore(RuntimePaths(Path(owner['temporary_root']) / 'simulation'))
        cfg.update({'model': {'name': 'changed'}})
        with self.assertRaisesRegex(Exception, 'model_changed'):
            run.verify(self.out)

    async def test_changed_persona_or_policy_rejected_before_dispatch(self):
        owner, _ = run.verify(self.out)
        paths = RuntimePaths(Path(owner['temporary_root']) / 'simulation')
        service = run.RuntimeService(run.Store(paths))
        service.use_persona(owner['homes']['simulation']['personas']['A'])
        profiles = run.ProfileService(run.Store(paths))
        current = profiles.current('relationship')
        profiles.update('relationship', {'description': 'changed'}, current['id'], run.identifier())
        with self.assertRaisesRegex(Exception, 'persona_or_policy_changed'):
            await run.execute(self.out, simulated=True)
        self.assertFalse((self.out / 'simulation/started.json').exists())

    async def test_reservation_write_failure_never_dispatches(self):
        original = run.atomic_json
        class Counting(run.SimulationClient):
            count = 0
            async def complete(self, model, messages):
                self.count += 1
                return await super().complete(model, messages)
        client = Counting()
        def fail(path, value):
            if value.get('status') == 'reserved':
                raise OSError('fixture_reservation_write_failure')
            return original(path, value)
        with patch.object(run, 'atomic_json', fail):
            with self.assertRaises(Exception):
                await run.execute(self.out, simulated=True, client=client)
        self.assertEqual(client.count, 0)

    async def test_native_retry_recovers_without_replaying_successes(self):
        class Once(run.SimulationClient):
            count = 0
            async def complete(self, model, messages):
                self.count += 1
                if self.count == 1:
                    raise ModelError('provider_network_error')
                return await super().complete(model, messages)
        async def no_delay(_):
            pass
        client = Once()
        with patch('persona_runtime.generation_retry.retry_delay', no_delay):
            result = await run.execute(self.out, simulated=True, client=client)
        self.assertEqual(result['simulated_calls'], 45)
        self.assertEqual(result['retries'], 1)
        self.assertEqual(client.count, 45)

    async def test_retry_exhaustion_stops_and_preserves_all_attempts(self):
        class Failed:
            async def complete(self, model, messages):
                raise ModelError('provider_network_error')
        async def no_delay(_):
            pass
        with patch('persona_runtime.generation_retry.retry_delay', no_delay):
            with self.assertRaisesRegex(Exception, 'provider_retries_exhausted'):
                await run.execute(self.out, simulated=True, client=Failed())
        folder = self.out / 'simulation/positions/V01-A/calls'
        self.assertEqual(len(list(folder.glob('*.json'))), 4)
        self.assertTrue(all(run.read(p)['status'] == 'failed' for p in folder.glob('*.json')))
        with self.assertRaisesRegex(Exception, 'no_automatic_resume'):
            await run.execute(self.out, simulated=True)

    async def test_result_write_failure_does_not_replay_received_response(self):
        original = run.atomic_json
        def fail(path, value):
            if Path(path).name == 'result.json':
                raise OSError('fixture_terminal_write_failure')
            return original(path, value)
        with patch.object(run, 'atomic_json', fail):
            with self.assertRaisesRegex(OSError, 'fixture_terminal_write_failure'):
                await run.execute(self.out, simulated=True)
        self.assertEqual(len(list((self.out / 'simulation/positions/V01-A/calls').glob('*.json'))), 1)
        with self.assertRaisesRegex(Exception, 'no_automatic_resume'):
            await run.execute(self.out, simulated=True)

    async def test_cancellation_has_terminal_receipt(self):
        entered = asyncio.Event()
        class Waiting:
            async def complete(self, model, messages):
                entered.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(run.execute(self.out, simulated=True, client=Waiting()))
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        calls = list((self.out / 'simulation/positions/V01-A/calls').glob('*.json'))
        self.assertEqual(len(calls), 1)
        self.assertEqual(run.read(calls[0])['status'], 'interrupted')


if __name__ == '__main__':
    unittest.main()
