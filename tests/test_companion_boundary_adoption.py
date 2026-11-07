"""Exact one-field author approval, with artificial source and automatic cleanup."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from experiments import companion_expression_adoption as adoption
from persona_runtime.config import atomic_json
from persona_runtime.models import RuntimeErrorBase, timestamp
from persona_runtime.profile import ProfileService
from tests import test_companion_expression_adoption as fixture


class BoundaryAdoption(unittest.TestCase):
    save = fixture.AdoptionTests.save
    data = fixture.AdoptionTests.data
    check_originals = fixture.AdoptionTests.check_originals

    def setUp(self):
        fixture.AdoptionTests.setUp(self)
        self.draft['version'] = adoption.BOUNDARY_REVIEW_VERSION
        self.draft['replacements'] = [{
            'path': adoption.BOUNDARY_PATHS[0],
            'before': self.before['core']['data']['values_and_boundaries'][0],
            'after': 'Artificial unknown memories remain uncertain.', 'reason': 'Artificial behaviour edit.'}]
        atomic_json(self.draft_path, self.draft)
        marker_path = self.review_home / 'private-sandbox.json'
        marker = adoption._json(marker_path)
        marker['purpose'] = 'stage4E_persona_review_only'
        atomic_json(marker_path, marker)
        self.review_file = self.draft_path.parent / adoption.BOUNDARY_REVIEW_NAME
        self.review_file.write_text('Artificial boundary exact text.', encoding='utf-8')

    def approve(self):
        atomic_json(self.draft_path.parent / 'approval.json', {
            'draft_sha256': adoption.sha(self.draft_path.read_bytes()),
            'review_sha256': adoption.sha(self.review_file.read_bytes()),
            'approved_at': timestamp(), 'user_answer': 'Approve artificial boundary.',
            'scope': adoption.BOUNDARY_SCOPE})

    def test_exact_single_field_save_copy_and_idempotency(self):
        self.approve()
        result = self.save()
        expected = {k: deepcopy(v['data']) for k,v in self.before.items()}
        expected['core']['values_and_boundaries'][0] = self.draft['replacements'][0]['after']
        self.assertEqual(self.data(), expected)
        versions = adoption._versions(self.store,self.pid)
        for kind in adoption.KINDS:
            if kind != 'core':
                self.assertEqual(versions[kind],self.before[kind])
        self.assertTrue(self.save()['already_saved'])
        self.assertEqual(adoption._selected(self.store),self.seed)
        copied = adoption.prepare_copy(result['binding_path'],self.paths.home)
        self.assertTrue(Path(copied['home']).is_dir())
        self.check_originals()

    def test_saved_boundary_version_enters_normal_c2_request(self):
        from experiments import companion_c2_chat as trial
        from experiments import companion_workbench_trial as workbench
        from experiments.companion_workbench_check import NetworkFence
        from persona_runtime.character_embedding import SemanticFailure
        from persona_runtime.character_runtime import CharacterRuntime
        from persona_runtime.companion import MINIMAL_INSTRUCTION, choose
        from persona_runtime.companion_chat_contract import chat_contract
        from persona_runtime.config import ConfigStore
        from persona_runtime.desktop_client import LocalClient
        from persona_runtime.paths import RuntimePaths
        from persona_runtime.storage import Store
        from tests import test_companion_c2_chat as chat_fixture

        self.approve()
        binding = Path(self.save()['binding_path'])
        source_home = self.paths.home
        expected = adoption._visible_data(adoption._versions(self.store, self.pid))
        ConfigStore(self.paths).update({'model': {
            'base_url': 'https://offline.invalid/v1', 'name': 'grok-4.7', 'hourly_limit': 0}})
        self.network.stop()
        with patch.object(CharacterRuntime, '_hybrid', side_effect=SemanticFailure('embedding_unavailable')), \
                patch.object(workbench, 'configure_local', return_value=None):
            manifest = trial.prepare(binding, source_home, integration=False, persona_purpose=False)
            self.owner, plan = trial.verify(manifest)
            self.paths = RuntimePaths(Path(self.owner['home']))
            copied = adoption._versions(Store(self.paths), self.owner['persona_id'])
            self.assertEqual({kind: version['data'] for kind, version in copied.items()}, expected)
            self.assertFalse(any(self.owner['initial_counts'].values()))
            self.assertEqual(copied['state']['data'], {'text': '', 'expires_at': None})
            self.assertEqual(plan['profile'], choose('minimal_chat'))
            model = chat_fixture.FixtureClient()
            runtime = workbench.TrialRuntime(trial_manifest=manifest, client=model)
            with NetworkFence() as fence, runtime:
                fence.port = runtime.port
                client = LocalClient(ConfigStore(self.paths))
                self.assertEqual(model.calls, [])
                batch = chat_fixture.C2ChatTests.submit(self, client, 'Artificial current preference question.')
                row = chat_fixture.C2ChatTests.wait_turn(self, client, batch)
                self.assertEqual(row['status'], 'completed', row['error_code'])
                self.assertEqual(len(model.calls), 1)
                model_config, messages = model.calls[0]
                self.assertEqual(model_config['_companion_profile'], choose('minimal_chat'))
                spec = {'format_version': 1, **copied['core']['data'],
                        'expression_style': copied['expression']['data']['style']}
                self.assertTrue(messages[0]['content'].startswith(
                    MINIMAL_INSTRUCTION + json.dumps(spec, ensure_ascii=False)))
                self.assertTrue(messages[0]['content'].endswith(chat_contract()))
                self.assertIn(self.draft['replacements'][0]['after'], messages[0]['content'])
            self.assertFalse(runtime.thread.is_alive())
            summary = trial.audit(manifest)
            self.assertEqual(summary['simulated_calls'], 1)
            self.assertEqual(summary['physical_calls'], 0)
            self.assertEqual(summary['pending_turns'], 0)
            self.assertEqual(summary['active_reservations'], 0)
            self.assertFalse(any(summary['auxiliary_calls'].values()))
            self.assertTrue(all(item[key] for item in summary['rows'] for key in (
                'manifest_matches', 'wire_matches', 'persisted_manifest_matches', 'runtime_audit_matches')))
            with self.assertRaisesRegex(RuntimeErrorBase, 'started_requires_review'):
                with workbench.TrialRuntime(trial_manifest=manifest, client=model):
                    self.fail('Completed trial reopened')
            self.assertEqual(len(model.calls), 1)
        self.check_originals()

    def test_approval_and_paths_are_exact(self):
        with self.assertRaisesRegex(RuntimeErrorBase,'approval_missing'):
            self.save()
        self.approve()
        self.review_file.write_text('Different artificial draft.',encoding='utf-8')
        with self.assertRaisesRegex(RuntimeErrorBase,'approval_mismatch'):
            self.save()
        self.draft['replacements'][0]['path']='core.core_traits[0]'
        atomic_json(self.draft_path,self.draft)
        self.approve()
        with self.assertRaisesRegex(RuntimeErrorBase,'replacement_paths_invalid'):
            self.save()

    def test_drift_and_failed_write_stop_without_replay(self):
        self.approve()
        with patch.object(ProfileService,'_write',side_effect=OSError('artificial save failure')):
            with self.assertRaises(OSError):
                self.save()
        self.assertEqual(self.data(),{k:v['data'] for k,v in self.before.items()})
        self.assertEqual(adoption._selected(self.store),self.seed)
        with self.assertRaisesRegex(RuntimeErrorBase,'save_requires_inspection'):
            self.save()

    def test_new_contract_rejects_source_revision_drift(self):
        self.approve()
        with self.store.connection(write=True) as db:
            data=deepcopy(self.before['core']['data'])
            data['introduction']='Another artificial edit.'
            ProfileService(self.store)._write(db,self.pid,'core',data,self.before['core']['id'],'outside')
        with self.assertRaisesRegex(RuntimeErrorBase,'source_versions_changed'):
            self.save()
