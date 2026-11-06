"""Artificial, retained fixtures for the normal private-copy Workbench API."""
import asyncio
import json
from pathlib import Path
import threading
import tempfile
import time
import unittest
from unittest.mock import patch

from experiments import companion_expression_adoption as adoption
from experiments import companion_workbench_trial as trial
from experiments.companion_trial import configure, configure_policy
from experiments.companion_workbench_check import NetworkFence
from persona_runtime.character_embedding import SemanticFailure
from persona_runtime.character_runtime import CharacterRuntime
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.desktop_client import LocalClient
from persona_runtime.models import RuntimeErrorBase, identifier
from persona_runtime.paths import RuntimePaths, DEFAULT_HOME
from persona_runtime.private_personas import provider_identity
from persona_runtime.storage import Store
from tests import test_companion_expression_adoption as adoption_fixture


class ArtificialModel:
    def __init__(self, waiting=False):
        self.calls = []; self.waiting = waiting; self.entered = threading.Event()
    async def complete(self, model, messages):
        self.calls.append((model, messages)); self.entered.set()
        if self.waiting: await asyncio.Event().wait()
        return json.dumps({'schema_version': 3, 'behavior_intent': 'respond', 'messages': [
            {'content': 'Artificial Workbench reply.', 'content_kind': 'chat', 'cancel_if_new_input': False}]}), {'total_tokens': 1}


class WorkbenchTrialTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=DEFAULT_HOME / 'tmp', prefix='workbench-trial-artificial-')
        self.addCleanup(temporary.cleanup)
        with patch.object(RuntimePaths, 'from_environment', return_value=RuntimePaths(Path(temporary.name))):
            fixture = adoption_fixture.AdoptionTests(); fixture.setUp()
        fixture.network.stop()
        self.addCleanup(fixture.doCleanups)
        fixture.approve(); self.binding = Path(fixture.save()['binding_path'])
        copy = adoption.prepare_copy(self.binding, fixture.paths.home)
        self.source_home = fixture.paths.home; self.paths = RuntimePaths(Path(copy['home'])); self.pid = copy['persona_id']
        source = ConfigStore(fixture.paths).load()
        source['model'].update(base_url='https://offline.invalid/v1', name='grok-4.7')
        cfg = configure(self.paths, source, self.pid)
        self.notify = patch.object(CharacterRuntime, 'notify', return_value=None); self.notify.start(); self.addCleanup(self.notify.stop)
        self.hybrid = patch.object(CharacterRuntime, '_hybrid', side_effect=SemanticFailure('embedding_unavailable'))
        self.hybrid.start(); self.addCleanup(self.hybrid.stop)
        configure_policy(Store(self.paths), cfg, self.pid, 'minimal_structural', background_prepare=False)
        atomic_json(self.paths.home / 'runtime/private-copy-policy.json', {
            'persona_id': self.pid, 'approved_models': {'main': provider_identity(cfg.load()['model'])}})
        self.ready_path = self.paths.home / 'runtime/adoption-workbench-ready.json'
        atomic_json(self.ready_path, {'status': 'ready', 'binding_path': str(self.binding), 'persona_id': self.pid,
            'adoption_sha256': json.loads(self.binding.read_text(encoding='utf-8'))['draft_sha256'],
            'source_main_provider_fingerprint': provider_identity(cfg.load()['model']),
            'real_calls_at_prepare': 0, 'qq_enabled': False, 'background_organization_enabled': False})
        self.local = patch.object(trial, 'configure_local', return_value=None); self.local.start(); self.addCleanup(self.local.stop)

    def runtime(self, model): return trial.TrialRuntime(self.binding, self.source_home, client=model)

    def test_zero_startup_generation_real_loopback_and_clean_shutdown(self):
        model = ArtificialModel()
        runtime = self.runtime(model)
        with NetworkFence() as fence:
            with runtime:
                fence.port = runtime.port
                client = LocalClient(ConfigStore(self.paths))
                health = client.call('GET', '/v1/health')
                self.assertEqual(health['service'], 'persona-runtime')
                self.assertEqual(model.calls, [])
                self.assertTrue(runtime.thread.is_alive())
            self.assertFalse(runtime.thread.is_alive()); self.assertEqual(model.calls, [])
        state = json.loads(runtime.state_file.read_text(encoding='utf-8'))
        self.assertEqual(state['pending_turns'], 0); self.assertEqual(state['active_reservations'], 0)

    def test_running_mock_is_cancelled_on_owned_window_shutdown(self):
        model = ArtificialModel(waiting=True)
        runtime = self.runtime(model)
        with NetworkFence() as fence:
            with runtime:
                fence.port = runtime.port
                client = LocalClient(ConfigStore(self.paths))
                receipt = client.submit_input({'channel': 'desktop', 'persona_id': self.pid,
                    'text': 'Artificial held response.', 'request_id': identifier()})
                batch = client.call('GET', '/v2/batches/' + receipt['batch_id'])
                client.call('POST', '/v2/batches/' + batch['id'] + '/seal', {
                    'expected_revision': batch['revision'], 'operation_id': identifier(), 'persona_id': self.pid})
                self.assertTrue(model.entered.wait(10), 'mock generation did not start')
            self.assertFalse(runtime.thread.is_alive())
        with Store(self.paths).connection() as db:
            self.assertEqual(db.execute('SELECT status FROM turns').fetchone()[0], 'interrupted')
            self.assertEqual(db.execute("SELECT count(*) FROM quota_reservations WHERE state='active'").fetchone()[0], 0)
        self.assertEqual(len(model.calls), 1)

    def test_unfinished_input_refused_before_recovery_or_generation(self):
        from persona_runtime.chat import ChatEngine
        from persona_runtime.ingress import IngressService
        engine = ChatEngine(Store(self.paths), ConfigStore(self.paths), ArtificialModel())
        try:
            IngressService(engine).accept({'channel': 'desktop', 'persona_id': self.pid,
                'text': 'Artificial unfinished collection.', 'request_id': identifier()})
        finally:
            engine.cognitive.characters.close(); engine.cognitive.shadow.close()
        model = ArtificialModel()
        with self.assertRaisesRegex(RuntimeErrorBase, 'unfinished_requires_review'):
            with self.runtime(model): self.fail('unexpected start')
        self.assertEqual(model.calls, [])

    def test_ready_route_and_background_drift_are_refused(self):
        ready = json.loads(self.ready_path.read_text(encoding='utf-8'))
        atomic_json(self.ready_path, ready | {'source_main_provider_fingerprint': 'changed'})
        with self.assertRaisesRegex(RuntimeErrorBase, 'ready_binding_changed'):
            trial.check_start(self.paths, self.pid, self.binding)
        atomic_json(self.ready_path, ready)
        with patch.object(trial, 'expression_version', return_value=2):
            with self.assertRaisesRegex(RuntimeErrorBase, 'companion_policy_changed'):
                trial.check_start(self.paths, self.pid, self.binding)
        ConfigStore(self.paths).update({'bot_qq': '12345'})
        with self.assertRaisesRegex(RuntimeErrorBase, 'background_forbidden'):
            trial.check_start(self.paths, self.pid, self.binding)

    def test_controller_never_delegates_components_or_global_stop(self):
        from types import SimpleNamespace
        controller = trial.OwnedController(SimpleNamespace(running=True))
        self.assertTrue(controller.owned('service')); self.assertFalse(controller.owned('napcat'))
        for name in ('start', 'stop', 'start_all', 'stop_all', 'force_stop_all', 'force_stop_qq', 'management_url'):
            with self.assertRaises(RuntimeErrorBase): getattr(controller, name)('service')
        self.assertNotIn('离线验收', json.dumps(controller.snapshot(), ensure_ascii=False))


if __name__ == '__main__': unittest.main()
