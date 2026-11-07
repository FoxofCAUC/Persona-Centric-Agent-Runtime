"""Isolated model import and closed-loop replay with synthetic text only."""
import hashlib
from pathlib import Path
from unittest.mock import patch

from experiments import companion_c2_chat as trial
from experiments import companion_model_replay as replay
from experiments import companion_workbench_trial as workbench
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from tests.test_companion_native_chat import NativeChat
from tests.test_companion_c2_chat import FixtureClient, NetworkFence


class ModelReplay(NativeChat):
    def make_donor_copy(self):
        before = ConfigStore(RuntimePaths(self.source)).file.read_bytes()
        ConfigStore(self.paths).update({'model': {'name': 'deepseek-flash'}})
        manifest = trial.prepare(self.binding, self.source, native_text=True,
                                 approve_current_provider=True, model_configuration_home=self.paths.home)
        owner, plan = trial.verify(manifest)
        self.assertEqual(plan['model_public']['name'], 'deepseek-flash')
        self.assertEqual(ConfigStore(RuntimePaths(self.source)).file.read_bytes(), before)
        self.assertFalse(any(owner['initial_counts'].values()))
        return manifest, owner

    def test_only_explicit_owned_native_model_import_and_drift_guard(self):
        with self.assertRaisesRegex(RuntimeErrorBase, 'model_import_not_authorized'):
            trial.prepare(self.binding, self.source, native_text=True, model_configuration_home=self.paths.home)
        manifest, _ = self.make_donor_copy()
        ConfigStore(self.paths).update({'model': {'name': 'changed-model'}})
        with self.assertRaisesRegex(RuntimeErrorBase, 'model_configuration_changed'):
            trial.verify(manifest)

    def test_replay_preserves_members_and_uses_only_new_assistant_history(self):
        manifest, owner = self.make_donor_copy()
        source = self.paths.home / 'runtime/artificial-replay.json'
        groups = [['Artificial first input.\n'], ['Artificial second input.', 'Artificial correction.']]
        turns = [{'ordinal': n, 'source_turn_id': 'artificial-old-' + str(n),
                  'inputs': [{'text': text, 'text_sha256': hashlib.sha256(text.encode()).hexdigest()}
                             for text in members], 'reply_text': 'Old-model-only answer must not enter B history.'}
                 for n, members in enumerate(groups, 1)]
        atomic_json(source, {'source_home': str(self.paths.home), 'turns': turns})
        model = FixtureClient()
        model.native = True
        original_start = workbench.TrialRuntime.start
        runtime = workbench.TrialRuntime(trial_manifest=manifest, client=model)
        with NetworkFence() as fence:
            async def start(runtime):
                await original_start(runtime)
                fence.port = runtime.port
            with patch.object(workbench.TrialRuntime, 'start', start), \
                    patch.object(replay, 'TrialRuntime', return_value=runtime):
                result = replay.execute(manifest, source, client=model)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(model.calls), 2)
        self.assertTrue(all(row['inputs_exact'] for row in result['positions']))
        self.assertNotIn('Old-model-only answer', str(model.calls[-1][1]))
        self.assertTrue(any(m['role'] == 'assistant' and 'Artificial native reply.' in m['content']
                            for m in model.calls[-1][1]))
        self.assertEqual(result['audit']['pending_turns'], 0)
        self.assertEqual(result['audit']['active_reservations'], 0)
        self.assertFalse(any(result['audit']['auxiliary_calls'].values()))
        with self.assertRaises(FileExistsError):
            replay.execute(manifest, source, client=model)
        self.assertEqual(len(model.calls), 2)
