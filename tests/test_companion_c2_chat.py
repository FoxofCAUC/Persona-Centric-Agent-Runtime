"""Owned C2 normal-window flow with artificial author data and local clients."""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from experiments import companion_c2_chat as trial
from experiments import companion_workbench_trial as workbench
from experiments.companion_workbench_check import NetworkFence
from persona_runtime.character_embedding import SemanticFailure
from persona_runtime.character_runtime import CharacterRuntime
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.desktop_client import LocalClient
from persona_runtime.models import RuntimeErrorBase, identifier
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.storage import Store
from tests import test_companion_expression_adoption as adoption_fixture


class FixtureClient:
    def __init__(self, failures=0, waiting=False):
        self.calls = []
        self.failures, self.waiting = failures, waiting
        self.entered = threading.Event()
        self.native = False

    async def complete(self, model, messages):
        self.calls.append((model, messages))
        self.entered.set()
        if len(self.calls) <= self.failures:
            raise RuntimeErrorBase('provider_network_error')
        if self.waiting:
            await asyncio.Event().wait()
        if self.native:
            return 'Artificial native reply.\n\n```python\nprint(168)\n```', {'total_tokens': 3}
        return json.dumps({'schema_version': 3, 'behavior_intent': 'respond', 'messages': [
            {'content': 'Artificial normal-window response.', 'content_kind': 'chat',
             'cancel_if_new_input': False}]}), {'total_tokens': 3}


class C2ChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=DEFAULT_HOME / 'tmp', prefix='c2-chat-artificial-')
        self.addCleanup(self.temp.cleanup)
        with patch.object(RuntimePaths, 'from_environment', return_value=RuntimePaths(Path(self.temp.name))):
            fixture = adoption_fixture.AdoptionTests()
            fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.approve()
        self.binding = Path(fixture.save()['binding_path'])
        fixture.network.stop()
        self.source = fixture.paths.home
        cfg = ConfigStore(fixture.paths)
        cfg.update({'model': {'base_url': 'https://offline.invalid/v1', 'name': getattr(self, 'model_name', 'grok-4.7'), 'hourly_limit': 0}})
        self.hybrid = patch.object(CharacterRuntime, '_hybrid', side_effect=SemanticFailure('embedding_unavailable'))
        self.hybrid.start()
        self.addCleanup(self.hybrid.stop)
        self.local = patch.object(workbench, 'configure_local', return_value=None)
        self.local.start()
        self.addCleanup(self.local.stop)
        self.manifest = trial.prepare(self.binding, self.source, integration=getattr(self, 'integration', False), persona_purpose=getattr(self, 'persona_purpose', False), context_rules_off=getattr(self, 'context_rules_off', False), prompt_light=getattr(self, 'prompt_light', False), native_text=getattr(self, 'native_text', False), im_style=getattr(self, 'im_style', False))
        self.companion_mode = ('minimal_im' if getattr(self, 'im_style', False)
                               else 'minimal_native' if getattr(self, 'native_text', False)
                               else 'minimal_experience' if getattr(self, 'prompt_light', False)
                               else 'minimal_context_rules_off' if getattr(self, 'context_rules_off', False)
                               else 'minimal_persona' if getattr(self, 'persona_purpose', False) else 'minimal_chat')
        self.owner, self.plan = trial.verify(self.manifest)
        self.paths = RuntimePaths(Path(self.owner['home']))

    def runtime(self, model):
        return workbench.TrialRuntime(trial_manifest=self.manifest, client=model)

    def submit(self, client, text):
        receipt = client.submit_input({'channel': 'desktop', 'persona_id': self.owner['persona_id'],
                                      'text': text, 'request_id': identifier()})
        batch = client.call('GET', '/v2/batches/' + receipt['batch_id'])
        client.call('POST', '/v2/batches/' + batch['id'] + '/seal', {
            'expected_revision': batch['revision'], 'operation_id': identifier(),
            'persona_id': self.owner['persona_id']})
        return batch['id']

    def wait_turn(self, client, batch_id):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            batch = client.call('GET', '/v2/batches/' + batch_id)
            if batch.get('turn_id'):
                with Store(self.paths).connection() as db:
                    row = dict(db.execute('SELECT * FROM turns WHERE id=?', (batch['turn_id'],)).fetchone())
                if row['status'] not in ('queued', 'running'):
                    return row
            time.sleep(.03)
        self.fail('artificial request failed to terminate')

    def test_fresh_receipt_preserves_closed_adoption_and_empty_history(self):
        old = self.binding.with_suffix('.copy.json')
        atomic_json(old, {'status': 'closed', 'home': str(self.source / 'tmp/private-acceptance/removed')})
        before = old.read_bytes()
        second = trial.prepare(self.binding, self.source)
        owner, _ = trial.verify(second)
        self.assertNotEqual(owner['home'], self.owner['home'])
        self.assertEqual(old.read_bytes(), before)
        with self.assertRaisesRegex(RuntimeErrorBase, 'copy_not_prepared'):
            workbench.prepared_copy(self.binding, self.source)
        self.assertFalse(any(owner['initial_counts'].values()))
        self.assertEqual(self.plan['profile'], trial.choose(self.companion_mode))

    def test_current_provider_authorization_is_explicit_and_copy_scoped(self):
        registry = self.source / 'private/personas/registry.json'
        before = registry.read_bytes()
        with patch.object(trial, 'DEFAULT_HOME', self.source), \
                patch.object(trial, 'guard_cloud_dispatch', side_effect=RuntimeErrorBase('private_cloud_provider_not_authorized')) as guard, \
                patch.object(trial, 'copy_weights'), patch.object(trial, 'local_retrieval_ready'):
            with self.assertRaisesRegex(RuntimeErrorBase, 'private_cloud_provider_not_authorized'):
                trial.prepare(self.binding, self.source)
            manifest = trial.prepare(self.binding, self.source, approve_current_provider=True)
            owner, _ = trial.verify(manifest)
            self.assertEqual(owner['provider_approval'], 'explicit_current_stage')
            self.assertEqual(guard.call_count, 1)
        self.assertEqual(registry.read_bytes(), before)

    def test_actual_loopback_two_turns_manifest_wire_and_no_startup_generation(self):
        model = FixtureClient()
        runtime = self.runtime(model)
        with NetworkFence() as fence, runtime:
            fence.port = runtime.port
            client = LocalClient(ConfigStore(self.paths))
            self.assertEqual(model.calls, [])
            for text in ('Artificial first share.', 'Artificial follow-up correction.'):
                row = self.wait_turn(client, self.submit(client, text))
                self.assertEqual(row['status'], 'completed', row['error_code'])
            self.assertEqual(len(model.calls), 2)
            self.assertIn('Artificial first share.', str(model.calls[-1][1]))
            self.assertEqual(model.calls[0][0]['_companion_profile'], trial.choose(self.companion_mode))
        summary = trial.audit(self.manifest)
        self.assertEqual(summary['simulated_calls'], 2)
        self.assertTrue(all(r['manifest_matches'] and r['wire_matches'] for r in summary['rows']))
        self.assertTrue(all(r['persisted_manifest_matches'] and r['runtime_audit_matches'] for r in summary['rows']))
        self.assertEqual(summary['pending_turns'], 0)
        self.assertFalse(any(summary['auxiliary_calls'].values()))
        with self.assertRaisesRegex(RuntimeErrorBase, 'started_requires_review'):
            with self.runtime(model):
                self.fail('successful chat reopened')
        self.assertEqual(len(model.calls), 2)

    def test_duplicate_window_cannot_overwrite_running_ownership(self):
        model = FixtureClient()
        first = self.runtime(model)
        second = self.runtime(model)
        with NetworkFence() as fence, first:
            fence.port = first.port
            before = first.state_file.read_bytes()
            with self.assertRaises(RuntimeErrorBase):
                with second: self.fail('duplicate instance started')
            self.assertEqual(first.state_file.read_bytes(), before)
            self.assertTrue(first.running)
        self.assertEqual(model.calls, [])

    def test_retry_recovery_uses_identical_input(self):
        model = FixtureClient(failures=1)
        runtime = self.runtime(model)
        async def no_delay(seconds):
            self.assertEqual(seconds, 1)
        with patch('persona_runtime.generation_retry.retry_delay', no_delay), NetworkFence() as fence, runtime:
            fence.port = runtime.port
            client = LocalClient(ConfigStore(self.paths))
            row = self.wait_turn(client, self.submit(client, 'Artificial retry input.'))
            self.assertEqual(row['status'], 'completed', row['error_code'])
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(model.calls[0][1], model.calls[1][1])
        self.assertEqual(trial.audit(self.manifest)['simulated_calls'], 2)

    def test_retry_exhaustion_stops_acceptance_and_never_replays(self):
        model = FixtureClient(failures=4)
        runtime = self.runtime(model)
        delays = []
        async def no_delay(seconds): delays.append(seconds)
        with patch('persona_runtime.generation_retry.retry_delay', no_delay), NetworkFence() as fence, runtime:
            fence.port = runtime.port
            client = LocalClient(ConfigStore(self.paths))
            row = self.wait_turn(client, self.submit(client, 'Artificial exhausted input.'))
            self.assertEqual(row['status'], 'failed')
            self.assertTrue(runtime.app['engine'].draining)
            with self.assertRaises(Exception): self.submit(client, 'Forbidden extra input.')
        self.assertEqual(delays, [1, 2, 4])
        self.assertEqual(len(model.calls), 4)
        self.assertEqual(trial.read(trial.body(self.owner) / 'state.json')['status'], 'stopped')

    def test_owned_shutdown_cancels_generation_and_releases_reservations(self):
        model = FixtureClient(waiting=True)
        runtime = self.runtime(model)
        with NetworkFence() as fence, runtime:
            fence.port = runtime.port
            client = LocalClient(ConfigStore(self.paths))
            self.submit(client, 'Artificial cancelled input.')
            self.assertTrue(model.entered.wait(10))
        self.assertFalse(runtime.thread.is_alive())
        summary = trial.audit(self.manifest)
        self.assertEqual(summary['pending_turns'], 0)
        self.assertEqual(summary['active_reservations'], 0)
        self.assertEqual(len(model.calls), 1)

    def test_changed_model_stops_before_dispatch(self):
        model = FixtureClient()
        runtime = self.runtime(model)
        with NetworkFence() as fence, runtime:
            fence.port = runtime.port
            ConfigStore(self.paths).update({'model': {'name': 'changed-model'}})
            client = LocalClient(ConfigStore(self.paths))
            row = self.wait_turn(client, self.submit(client, 'Artificial drift input.'))
            self.assertEqual(row['status'], 'failed')
        self.assertEqual(model.calls, [])

    def test_user_cancel_allows_a_new_request_without_replaying_old_one(self):
        model = FixtureClient(waiting=True)
        runtime = self.runtime(model)
        with NetworkFence() as fence, runtime:
            fence.port = runtime.port
            client = LocalClient(ConfigStore(self.paths))
            batch_id = self.submit(client, 'Artificial user cancellation.')
            self.assertTrue(model.entered.wait(10))
            batch = client.call('GET', '/v2/batches/' + batch_id)
            with Store(self.paths).connection() as db:
                active = dict(db.execute('SELECT * FROM turns WHERE id=?', (batch['turn_id'],)).fetchone())
            client.cancel_request(active)
            cancelled = self.wait_turn(client, batch_id)
            self.assertEqual(cancelled['error_code'], 'cancelled_by_user')
            model.waiting = False
            following = self.wait_turn(client, self.submit(client, 'Artificial new input after cancellation.'))
            self.assertEqual(following['status'], 'completed', following['error_code'])
        self.assertEqual(len(model.calls), 2)

    def test_response_write_failure_does_not_repeat_successful_transport(self):
        model = FixtureClient()
        runtime = self.runtime(model)
        original = trial.atomic_json
        def write(path, value):
            if Path(path).name.endswith('.response.json'):
                raise OSError('artificial_receipt_write_failure')
            return original(path, value)
        with patch.object(trial, 'atomic_json', write), NetworkFence() as fence, runtime:
            fence.port = runtime.port
            client = LocalClient(ConfigStore(self.paths))
            row = self.wait_turn(client, self.submit(client, 'Artificial receipt input.'))
            self.assertEqual(row['status'], 'failed')
            self.assertTrue(runtime.app['engine'].draining)
        self.assertEqual(len(model.calls), 1)


if __name__ == '__main__':
    unittest.main()
