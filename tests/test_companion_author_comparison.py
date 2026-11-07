"""Two approved author revisions, private ownership and native replay safety."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from experiments import companion_author_comparison as run
from experiments import companion_c2 as replay
from persona_runtime.config import atomic_json
from tests import test_companion_c2 as fixture
from tests import test_companion_boundary_adoption as author_fixture


class AuthorComparison(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.Replay.asyncSetUp

    async def test_sixteen_normal_targets_are_independent_and_only_author_differs(self):
        root = await run.prepare(self.home)
        owner, plan = replay.verify(root)
        self.assertEqual(len(plan['positions']), 16)
        self.assertEqual(len(plan['pair_checks']), 8)
        self.assertTrue(all(r['only_approved_boundary_changed'] for r in plan['pair_checks']))
        self.assertNotEqual(owner['copies']['revision2']['home'], owner['copies']['revision3']['home'])
        self.assertTrue(all(not any(c['initial_counts'].values()) for c in owner['copies'].values()))
        client = replay.base.FixtureClient()
        summary = await replay.execute(root, simulated=True, client=client)
        self.assertEqual((summary['completed'], summary['physical_calls'], summary['simulated_calls']), (16, 0, 16))
        self.assertEqual({p['attribution'] for p in summary['pairs']}, {'simulation_only'})
        self.assertEqual(len(client.calls), 16)
        for position, call in zip(plan['order'], client.calls):
            request = replay.read(replay.data_root(owner) / 'positions' / position / 'request.json')
            self.assertEqual(request['wire'], call['wire'])
        self.assertTrue(replay.read(replay.data_root(owner) / 'smoke.json')['passed'])
        self.assertEqual(len(run.cleanup_targets(root)), 3)
        audit = run.audit_trial(root)
        self.assertEqual(len(audit['rows']), 16)
        self.assertFalse(audit['private_body_included'])
        self.assertTrue(all(r[k] for r in audit['rows'] for k in
                           ('manifest_matches', 'audit_matches', 'wire_matches', 'attempts_terminal')))
        self.assertFalse(any(c['active_reservations'] or c['pending_turns'] for c in audit['copies'].values()))
        with self.assertRaisesRegex(Exception, 'no_auto_resume'):
            await replay.execute(root, simulated=True)

    async def prepare_compat(self, *args, **kwargs):
        return await run.prepare(self.home)

    async def test_retry_recovery_exhaustion_and_success_not_replayed(self):
        with patch.object(replay, 'prepare', self.prepare_compat):
            await fixture.Replay.test_retry_recovery_and_exhaustion_preserve_input(self)

    async def test_terminal_write_failure_preserves_unknown_breakpoint(self):
        with patch.object(replay, 'prepare', self.prepare_compat):
            await fixture.Replay.test_terminal_failure_keeps_breakpoint_and_actual_call_count(self)

    async def test_dispatch_write_failure_has_zero_calls(self):
        with patch.object(replay, 'prepare', self.prepare_compat):
            await fixture.Replay.test_reservation_write_failure_never_dispatches(self)

    async def test_cancel_has_terminal(self):
        with patch.object(replay, 'prepare', self.prepare_compat):
            await fixture.Replay.test_cancellation_has_terminal_and_stops_following_positions(self)

    async def test_tamper_and_real_scope_stop_before_dispatch(self):
        with patch.object(replay, 'prepare', self.prepare_compat):
            await fixture.Replay.test_tamper_and_real_scope_rejected_before_dispatch(self)

    async def test_second_copy_and_cleanup_ownership_are_checked(self):
        root = await run.prepare(self.home)
        owner, _ = replay.verify(root)
        marker = Path(owner['copies']['revision3']['home']) / 'private-sandbox.json'
        value = replay.read(marker)
        value['purpose'] = 'different_owner'
        atomic_json(marker, value)
        with self.assertRaisesRegex(Exception, 'copy_owner_changed'):
            replay.verify(root)
        with self.assertRaisesRegex(Exception, 'cleanup_owner_mismatch'):
            run.cleanup_targets(root)

    async def test_saved_author_pair_enters_both_normal_c2_copies(self):
        from persona_runtime.paths import RuntimePaths
        from persona_runtime.config import ConfigStore
        helper = author_fixture.BoundaryAdoption('test_exact_single_field_save_copy_and_idempotency')
        with patch.object(RuntimePaths, 'from_environment', return_value=RuntimePaths(self.home)):
            helper.setUp()
        self.addCleanup(helper.doCleanups)
        helper.approve()
        binding = helper.save()['binding_path']
        ConfigStore(helper.paths).update({'model': {'base_url': 'http://127.0.0.1:9/v1',
                                                   'name': 'grok-4.7', 'hourly_limit': 0}})
        before = helper.data()
        root = await run.prepare(helper.paths.home, binding_path=binding)
        owner, plan = replay.verify(root)
        self.assertEqual(plan['source_versions']['revision2']['core']['id'], helper.before['core']['id'])
        self.assertNotEqual(plan['source_versions']['revision2']['core'], plan['source_versions']['revision3']['core'])
        self.assertEqual(helper.data(), before)
        for copy in owner['copies'].values():
            self.assertFalse(any(copy['initial_counts'].values()))
        helper.check_originals()

    async def test_retrieval_drift_stops_before_call(self):
        root = await run.prepare(self.home)
        _, plan = replay.verify(root)
        client = replay.base.FixtureClient()
        with patch.object(run, 'validate_dispatch', side_effect=RuntimeError('actual_retrieval_changed')):
            result = await replay.run_position(root, plan['order'][0], simulated=True, client=client)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(client.calls, [])


class HistoricalBinding(unittest.TestCase):
    setUp = author_fixture.BoundaryAdoption.setUp
    approve = author_fixture.BoundaryAdoption.approve
    save = author_fixture.BoundaryAdoption.save
    data = author_fixture.BoundaryAdoption.data
    check_originals = author_fixture.BoundaryAdoption.check_originals

    def test_exact_approved_predecessor_read_keeps_formal_head(self):
        self.approve()
        saved = self.save()
        current = deepcopy(self.data())
        source = run.historical_source(saved['binding_path'], self.paths.home)
        self.assertEqual(source['old_core'], self.before['core']['data'])
        self.assertEqual(source['current']['core']['data'], current['core'])
        self.assertEqual(self.data(), current)
        self.check_originals()

    def test_historical_hash_and_persona_scope_are_validated(self):
        self.approve()
        saved = self.save()
        binding = Path(saved['binding_path'])
        value = replay.read(binding)
        value['source_binding']['versions']['core']['content_sha256'] = '0' * 64
        atomic_json(binding, value)
        with self.assertRaisesRegex(Exception, 'historical_core_changed'):
            run.historical_source(binding, self.paths.home)
        value['source_binding']['versions']['core']['id'] = 'other-persona-version'
        atomic_json(binding, value)
        with self.assertRaisesRegex(Exception, 'historical_core_unavailable'):
            run.historical_source(binding, self.paths.home)


class Attribution(unittest.TestCase):
    def test_backend_missing_changed_incomplete_and_simulation(self):
        row = {'status': 'completed', 'kind': 'real', 'usage': {'generation_retry': {'attempts': [
            {'diagnostics': {'response_model': 'fixture', 'system_fingerprint': 'fp_one'}}]}}}
        self.assertEqual(run.attribution(row, row), 'comparable_author_pair_with_sampling_uncertainty')
        changed = deepcopy(row)
        changed['usage']['generation_retry']['attempts'][0]['diagnostics']['system_fingerprint'] = 'fp_two'
        self.assertEqual(run.attribution(row, changed), 'backend_mismatch')
        changed['usage']['generation_retry']['attempts'][0]['diagnostics'].pop('system_fingerprint')
        self.assertEqual(run.attribution(row, changed), 'backend_unknown')
        self.assertEqual(run.attribution(row, {'status': 'failed'}), 'incomplete_pair')
        self.assertEqual(run.attribution(row, {**row, 'kind': 'simulated'}), 'simulation_only')
