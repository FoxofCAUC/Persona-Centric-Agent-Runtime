"""Artificial data only; no private registry, retrieval weights, or model calls."""
import asyncio
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments import companion_expression_candidate as candidate
from experiments import companion_trial as trial
from persona_runtime.archive import fingerprint
from persona_runtime.character import CharacterService
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.models import RuntimeErrorBase, generic_persona, identifier, timestamp
from persona_runtime.paths import RuntimePaths
from persona_runtime.profile import ProfileService
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


class ExpressionCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=RuntimePaths.from_environment().home / 'tmp',
                                                prefix='expression-candidate-test-')
        self.addCleanup(self.temp.cleanup)
        self.source = RuntimePaths(Path(self.temp.name))
        ConfigStore(self.source).initialize()
        self.original_config = ConfigStore(self.source).file.read_bytes()
        self.copies = []
        self.old = self.artificial_copy('artificial')
        self.old_store = Store(RuntimePaths(Path(self.old['home'])))
        with self.old_store.connection() as conn:
            self.original = candidate._versions(conn, self.old['persona_id'])
        self.approved = {
            'version': candidate.DRAFT_VERSION, 'status': 'approved_not_applied',
            'approval': {'at': timestamp(), 'user_answer': '采用人工改稿', 'scope': '仅本次新副本'},
            'source_binding': {'profile_hash': 'old-random-id-bound-hash'},
            'source_content_hashes': {k: fingerprint(self.original[k]['data']) for k in candidate.KINDS},
            'candidate_version': '0.7.6.dev22', 'paid_calls_for_draft': 0,
            'replacements': [
                {'path': candidate.PATHS[0], 'before': self.original['core']['data']['core_traits'][0],
                 'after': '会认真权衡，也愿意表达个人取舍。', 'reason': '人工测试'},
                {'path': candidate.PATHS[1], 'before': self.original['expression']['data']['style'],
                 'after': '按话题自然交流，允许具体而坦率的表达。', 'reason': '人工测试'},
                {'path': candidate.PATHS[2], 'before': self.original['character']['data']['social']['public_image'],
                 'after': '愿意把自己的判断说清楚。', 'reason': '人工测试'},
            ]}
        self.patch_path = Path(self.old['home']) / 'runtime/review/approved.json'
        atomic_json(self.patch_path, self.approved)
        self.ready_calls = []

    def artificial_copy(self, private_id):
        self.assertEqual(private_id, 'artificial')
        paths = RuntimePaths(self.source.home / 'tmp/private-acceptance' / identifier())
        store = Store(paths)
        store.initialize()
        service = RuntimeService(store)
        pid = service.create_persona(generic_persona())['id']
        service.use_persona(pid)
        characters = CharacterService(store)
        current = characters.current(pid)
        data = deepcopy(current['data'])
        data['identity']['species'] = '人类'
        data['social']['public_image'] = '原有人工表达说明。'
        data['interests']['likes'] = ['纸质地图']
        characters.update(pid, data, current['id'], identifier())
        atomic_json(paths.home / 'private-sandbox.json', {'private_id': private_id, 'persona_id': pid,
                    'formal_home': str(self.source.home), 'created_at': timestamp()})
        atomic_json(paths.home / 'runtime/private-copy-policy.json', {'persona_id': pid, 'approved_models': {}})
        result = {'home': str(paths.home), 'persona_id': pid}
        self.copies.append(result)
        return result

    def local_ready(self, store, config, pid):
        with store.connection() as conn:
            value = candidate._versions(conn, pid)
            candidate._empty_history(conn)
        self.ready_calls.append(value)
        return {'status': 'ready', 'model_fingerprint': 'artificial-local-model',
                'index_fingerprint': fingerprint(value['character']['data'])}

    def prepare(self, *, rebuild=None, create=None):
        with ExitStack() as stack:
            stack.enter_context(patch.object(trial.PrivatePersonas, 'load', return_value={'records': [
                {'id': 'artificial', 'state': 'ready', 'approved_models': {}}]}))
            stack.enter_context(patch.object(trial.PrivatePersonas, 'create_sandbox',
                                           side_effect=create or self.artificial_copy))
            stack.enter_context(patch.object(trial, 'copy_weights'))
            stack.enter_context(patch.object(trial, 'prepare_candidate_retrieval',
                                           side_effect=rebuild or self.local_ready))
            return candidate.prepare(self.patch_path, self.source.home, approve_current_provider=True)

    def test_exact_three_fields_versions_projection_and_normal_admission(self):
        root = self.prepare()
        summary = candidate.verify(root)
        home = Path(summary['home'])
        store = Store(RuntimePaths(home))
        receipt = json.loads((home / candidate.RUNTIME_DIRECTORY / 'application.json').read_text(encoding='utf-8'))
        with store.connection() as conn:
            actual = candidate._versions(conn, summary['persona_id'])
            candidate._empty_history(conn)
        expected = candidate.replaced_data(self.original, self.approved)
        self.assertEqual({k: v['data'] for k, v in actual.items()}, expected)
        for kind in ('core', 'expression', 'character'):
            self.assertNotEqual(receipt['before_versions'][kind], receipt['after_versions'][kind])
            self.assertEqual(self.ready_calls[1][kind]['revision'], self.ready_calls[0][kind]['revision'] + 1)
        for kind in ('relationship', 'state'):
            self.assertEqual(receipt['before_versions'][kind], receipt['after_versions'][kind])
        spec = RuntimeService(store).show_persona()['spec']
        self.assertEqual(spec['expression_style'], expected['expression']['style'])
        self.assertEqual(spec['core_traits'], expected['core']['core_traits'])
        self.assertEqual(len(self.ready_calls), 2)
        self.assertEqual((home / candidate.RUNTIME_DIRECTORY / 'approved-patch.json').read_bytes(), self.patch_path.read_bytes())
        manifest_text = (root / 'trial.json').read_text(encoding='utf-8')
        for row in self.approved['replacements']:
            self.assertNotIn(row['after'], manifest_text)
        with self.old_store.connection() as conn:
            self.assertEqual(candidate._versions(conn, self.old['persona_id']), self.original)
        self.assertEqual(ConfigStore(self.source).file.read_bytes(), self.original_config)
        # No generation: ordinary acceptance freezes the newly published versions.
        from persona_runtime.chat import ChatEngine
        from persona_runtime.ingress import IngressService
        engine = ChatEngine(store, ConfigStore(store.paths))
        try:
            ingress = IngressService(engine)
            accepted = ingress.accept({'channel': 'desktop', 'text': '人工未发送任务', 'request_id': identifier()})
            with store.connection() as conn:
                batch = conn.execute('SELECT profile_snapshot,character_snapshot FROM input_batches WHERE id=?',
                                     (accepted['batch_id'],)).fetchone()
                profile = json.loads(batch['profile_snapshot'])
                character = json.loads(batch['character_snapshot'])
                self.assertEqual(profile['expression']['id'], actual['expression']['id'])
                self.assertEqual(profile['core']['id'], actual['core']['id'])
                self.assertEqual(character['id'], actual['character']['id'])
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 0)
        finally:
            engine.cognitive.characters.close()
            engine.cognitive.shadow.close()

    def test_unapproved_extra_path_and_before_mismatch_rejected(self):
        value = deepcopy(self.approved)
        value['status'] = 'awaiting_user_approval_not_applied'
        atomic_json(self.patch_path, value)
        with patch.object(trial, 'prepare') as create:
            with self.assertRaisesRegex(RuntimeErrorBase, 'not_approved'):
                candidate.prepare(self.patch_path, self.source.home)
            create.assert_not_called()
        value = deepcopy(self.approved)
        value['replacements'][0]['path'] = 'core.name'
        with self.assertRaisesRegex(RuntimeErrorBase, 'paths_invalid'):
            candidate.validate_approval(value)
        value = deepcopy(self.approved)
        value['replacements'][0]['before'] = '并非当前原文'
        atomic_json(self.patch_path, value)
        with self.assertRaisesRegex(RuntimeErrorBase, 'before_mismatch') as caught:
            self.prepare()
        root = Path(caught.exception.preparation_trial_root)
        manifest = json.loads((root / 'trial.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['status'], 'preparation_blocked')
        self.assertEqual(manifest['cleanup_homes'], [self.copies[-1]['home']])

    def test_source_hash_and_existing_history_rejected_before_write(self):
        value = deepcopy(self.approved)
        value['source_content_hashes']['relationship'] = 'changed'
        atomic_json(self.patch_path, value)
        with self.assertRaisesRegex(RuntimeErrorBase, 'source_content_changed'):
            self.prepare()
        atomic_json(self.patch_path, self.approved)
        def with_history(private_id):
            copied = self.artificial_copy(private_id)
            service = RuntimeService(Store(RuntimePaths(Path(copied['home']))))
            session = service.create_session()
            service.append_event(session['id'], 'user', '此前人工记录')
            return copied
        with self.assertRaisesRegex(RuntimeErrorBase, 'fresh_history'):
            self.prepare(create=with_history)
        store = Store(RuntimePaths(Path(self.copies[-1]['home'])))
        with store.connection() as conn:
            actual = candidate._versions(conn, self.copies[-1]['persona_id'])
        self.assertEqual({k: v['data'] for k, v in actual.items()}, {k: v['data'] for k, v in self.original.items()})

    def test_late_write_failure_rolls_back_all_three_fields(self):
        original_write = CharacterService._write
        def fail_candidate(service, conn, pid, data, expected, operation, *args, **kwargs):
            if operation.startswith('expression-candidate-'):
                raise RuntimeErrorBase('artificial_write_failure')
            return original_write(service, conn, pid, data, expected, operation, *args, **kwargs)
        with patch.object(CharacterService, '_write', new=fail_candidate):
            with self.assertRaisesRegex(RuntimeErrorBase, 'artificial_write_failure'):
                self.prepare()
        store = Store(RuntimePaths(Path(self.copies[-1]['home'])))
        with store.connection() as conn:
            actual = candidate._versions(conn, self.copies[-1]['persona_id'])
        self.assertEqual(actual, self.ready_calls[0])

    def test_index_failure_blocks_open_and_keeps_owned_copy(self):
        def fail_second(store, config, pid):
            if self.ready_calls:
                raise RuntimeErrorBase('artificial_index_failure')
            return self.local_ready(store, config, pid)
        with self.assertRaisesRegex(RuntimeErrorBase, 'artificial_index_failure') as caught:
            self.prepare(rebuild=fail_second)
        with self.assertRaisesRegex(RuntimeErrorBase, 'trial_not_prepared'):
            candidate.verify(caught.exception.preparation_trial_root)

    def test_verify_binds_data_and_open_reuses_existing_window(self):
        root = self.prepare()
        with patch.object(trial, 'open_window') as open_window:
            candidate.open_window(root)
            open_window.assert_called_once_with(root, 'C')
        copied = self.copies[-1]
        store = Store(RuntimePaths(Path(copied['home'])))
        service = ProfileService(store)
        current = service.current('expression')
        service.update('expression', {'style': '未经本次批准的额外改动'}, current['id'], identifier())
        with self.assertRaisesRegex(RuntimeErrorBase, 'candidate_changed'):
            candidate.verify(root)

    def test_preparation_blocks_async_network_and_generation(self):
        from persona_runtime.chat import ChatCompletionsClient
        async def attempts():
            # Windows creates a local socket pair while constructing the loop;
            # enter the outbound guard only after that internal setup completes.
            with candidate._offline_preparation():
                with self.assertRaisesRegex(RuntimeErrorBase, 'network_blocked'):
                    await asyncio.get_running_loop().create_connection(asyncio.Protocol, '127.0.0.1', 9)
                with self.assertRaisesRegex(RuntimeErrorBase, 'generation_blocked'):
                    await ChatCompletionsClient().complete({}, [])
        asyncio.run(attempts())


if __name__ == '__main__':
    unittest.main()
