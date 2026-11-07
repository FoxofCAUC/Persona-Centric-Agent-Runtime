"""Continuation isolation, preserved receipts and native cancellation; artificial fixtures only."""
import asyncio
from pathlib import Path
import tempfile
import shutil
import unittest
from unittest.mock import patch

from experiments import companion_stage5 as base
from experiments import companion_stage5_continue as run


class Continue(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=base.DEFAULT_HOME / 'tmp', prefix='stage5-continue-unit-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.out = self.home / 'evidence'
        self.enterContext(patch.object(base, 'TEMP', self.home / 'tmp/companion-stage5'))
        config = base.ConfigStore(base.RuntimePaths(self.home))
        config.initialize()
        config.update({'model': {'name': 'deepseek-flash', 'base_url': 'https://offline.invalid/v1', 'hourly_limit': 0}})
        await base.prepare(self.out, self.home, fixture=True)
        items = base.positions()
        for item in items[:4]:
            dest = self.out / 'real/positions' / item['id']
            dest.mkdir(parents=True)
            base.atomic_json(dest / 'result.json', {'fixture': True, 'physical_calls': 1,
                'retries': 0, 'inputs': item['scene']['inputs'], 'reply': 'original fixture'})
        dest = self.out / 'real/positions/V03-A/calls'
        dest.mkdir(parents=True)
        base.atomic_json(dest / 'unknown.json', {'status': 'reserved', 'fixture': True})
        base.atomic_json(self.out / 'real/pause-checkpoint.json', {
            'status': 'paused_by_user_waiting_provider_recovery',
            'completed_positions': [x['id'] for x in items[:4]],
            'interrupted_unknown': [{'position': 'V03-A', 'usage': 'unknown'}],
            'never_dispatched': [x['id'] for x in items[5:]]})

    async def test_full_continuation_preserves_originals_memory_reopen_and_no_replay(self):
        value = await run.prepare(self.out, supplement_unknown=True)
        self.assertEqual(value['positions'], 40)
        owner, original, plan = run.verify(self.out, True)
        self.assertEqual(plan['positions'][0]['id'], 'V03-A-supplement-1')
        result = await run.execute(self.out, simulated=True)
        self.assertEqual(result['completed_new'], 40)
        self.assertEqual(result['physical_calls'], 40)
        self.assertEqual(len(list((run.folder(self.out) / 'simulation/progress-events').glob('*.json'))), 41)
        self.assertFalse((run.folder(self.out) / 'simulation/progress.json').exists())
        self.assertEqual(base.read(self.out / 'real/positions/V03-A/calls/unknown.json')['status'], 'reserved')
        reopen = base.read(run.folder(self.out) / 'simulation/reopen-result.json')
        self.assertEqual(reopen['new_generation_calls'], 0)
        memory = base.read(self.out / run.NAME / 'positions/T15/manual-memory-operation.json')
        self.assertEqual(memory['result']['revision'], 2)
        run.verify(self.out, True)
        with self.assertRaisesRegex(Exception, 'no_automatic_resume'):
            await run.execute(self.out, simulated=True)
        with self.assertRaisesRegex(Exception, 'fixture_real_execution_forbidden'):
            await run.execute(self.out)

    async def test_without_explicit_supplement_original_unknown_is_not_scheduled(self):
        await run.prepare(self.out)
        _, _, plan = run.verify(self.out, True)
        self.assertEqual(len(plan['positions']), 39)
        self.assertEqual(plan['positions'][0]['id'], 'V03-B')
        self.assertNotIn('V03-A', [x['canonical_id'] for x in plan['positions']])

    async def test_explicit_resumption_skips_saved_success_and_keeps_source_lineage(self):
        await run.prepare(self.out, supplement_unknown=True)
        plan = base.read(run.folder(self.out) / 'freeze.json')
        for key in plan['code_hashes']:
            target = run.folder(self.out) / 'executed-source' / key
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(base.ROOT / key, target)
        (run.folder(self.out) / 'real').mkdir()
        base.atomic_json(run.folder(self.out) / 'real/started.json', {'fixture': True})
        saved = self.out / 'real/positions/V03-A-supplement-1/result.json'
        saved.parent.mkdir()
        base.atomic_json(saved, {'fixture': True, 'reply': 'saved response'})
        result = await run.prepare_resumption(self.out)
        self.assertEqual(result['remaining'], 39)
        owner, _, resume = run.verify(self.out, True, True)
        self.assertEqual(resume['positions'][0]['id'], 'V03-B')
        simulated = await run.execute(self.out, simulated=True, resuming=True)
        self.assertEqual((simulated['completed_new'], simulated['physical_calls']), (39, 39))
        self.assertEqual(base.read(saved)['reply'], 'saved response')

    async def test_changed_old_receipt_rejected_before_dispatch(self):
        await run.prepare(self.out, supplement_unknown=True)
        base.atomic_json(self.out / 'real/positions/V01-A/result.json', {'tampered': True})
        with self.assertRaisesRegex(Exception, 'old_receipt_changed'):
            await run.execute(self.out, simulated=True)
        self.assertFalse((run.folder(self.out) / 'simulation/started.json').exists())

    async def test_native_retry_recovers_without_outer_replay(self):
        await run.prepare(self.out, supplement_unknown=True)
        class Once(base.SimulationClient):
            count = 0
            async def complete(self, model, messages):
                self.count += 1
                if self.count == 1:
                    raise base.ModelError('provider_network_error')
                return await super().complete(model, messages)
        from persona_runtime.chat import ModelError
        async def fast(_):
            pass
        client = Once()
        # Keep the fixture error type local without changing runtime behavior.
        self.enterContext(patch.object(base, 'ModelError', ModelError, create=True))
        with patch('persona_runtime.generation_retry.retry_delay', fast):
            result = await run.execute(self.out, simulated=True, client=client)
        self.assertEqual((result['completed_new'], result['physical_calls'], client.count), (40, 41, 41))

    async def test_cooperative_stop_settles_inflight_attempt_and_does_not_start_next(self):
        await run.prepare(self.out, supplement_unknown=True)
        entered = asyncio.Event()
        class Waiting:
            async def complete(self, model, messages):
                entered.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(run.execute(self.out, simulated=True, client=Waiting()))
        await asyncio.wait_for(entered.wait(), 10)
        base.atomic_json(run.folder(self.out) / 'stop-request.json', {'reason': 'fixture_user_stop'})
        with self.assertRaises(asyncio.CancelledError):
            await task
        calls = list((self.out / run.NAME / 'positions').glob('*/calls/*.json'))
        self.assertEqual(len(calls), 1)
        self.assertEqual(base.read(calls[0])['status'], 'interrupted')
        owner, _, _ = run.verify(self.out, True)
        self.assertFalse(any(base.idle(base.RuntimePaths(Path(owner['temporary_root']) / run.NAME)).values()))


if __name__ == '__main__':
    unittest.main()
