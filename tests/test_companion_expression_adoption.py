"""Artificial-only adoption checks. No real private source, model, or server."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import tempfile
from unittest.mock import patch

from experiments import companion_expression_adoption as adoption
from persona_runtime.character import CharacterService, empty_character
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.models import RuntimeErrorBase, generic_persona, identifier, timestamp
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import PrivatePersonas
from persona_runtime.profile import ProfileService
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


class AdoptionTests(unittest.TestCase):
    def setUp(self):
        temporary_root = RuntimePaths.from_environment().home/'tmp'
        temporary_root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='adoption-unit-', dir=temporary_root)
        self.addCleanup(temporary.cleanup)
        fixture = Path(temporary.name)
        atomic_json(fixture / 'fixture-owner.json', {'version': 'stage4B-adoption-unit-fixture-v1',
                    'artificial_only': True, 'retained_for_evidence': False, 'created_at': timestamp()})
        self.temp = SimpleNamespace(name=str(fixture))
        self.paths = RuntimePaths(Path(self.temp.name) / 'home')
        self.store = Store(self.paths); self.store.initialize(); ConfigStore(self.paths).initialize()
        self.runtime = RuntimeService(self.store)
        self.seed = self.runtime.create_persona(generic_persona())['id']; self.runtime.use_persona(self.seed)
        package = {'format': 'pcar_character_v1', 'persona_spec': generic_persona().to_dict(), 'character': empty_character()}
        package['character']['social']['public_image'] = 'Artificial original approach.'
        package['character']['lorebook'] = [
            {'id': 'public', 'title': 'Artificial public', 'category': 'fixture', 'keywords': [],
             'text': 'Artificial visible detail.', 'priority': 0, 'enabled': True, 'visibility': 'conversation', 'dependencies': []},
            {'id': 'hidden', 'title': 'Artificial hidden', 'category': 'fixture', 'keywords': [],
             'text': 'Artificial creator-only detail.', 'priority': 0, 'enabled': True, 'visibility': 'creator_only', 'dependencies': []}]
        source = Path(self.temp.name) / 'artificial-package.json'; atomic_json(source, package)
        archive = Path(self.temp.name) / 'artificial-archive.md'; archive.write_text('Artificial creator-only archive.', encoding='utf-8')
        self.private = PrivatePersonas(self.paths)
        self.record = self.private.register(source, archive, 'Artificial original relationship.')
        self.record = self.private.import_original(self.store, self.record['id']); self.pid = self.record['persona_id']
        self.before = adoption._versions(self.store, self.pid)
        self.review_home = self.paths.home / 'tmp/private-acceptance' / identifier()
        self.draft_path = self.review_home / 'review/draft.json'
        atomic_json(self.review_home / 'private-sandbox.json', {
            'formal_home': str(self.paths.home), 'private_id': self.record['id'], 'persona_id': self.pid,
            'purpose': 'stage4B_expression_review_only'})
        self.draft = {'version': adoption.REVIEW_VERSION, 'status': 'awaiting_user_review', 'approval': None,
            'source_binding': {'persona_id': self.pid, 'private_id': self.record['id'],
                'versions': adoption._descriptors(self.before), 'original_package_sha256': self.record['package_sha256'],
                'original_archive_sha256': self.record['archive_sha256'],
                'initial_relationship_sha256': self.record['relationship_sha256']},
            'replacements': [
                {'path': adoption.PATHS[0], 'before': self.before['core']['data']['core_traits'][0],
                 'after': 'Artificial focus depends on this particular conversation.', 'reason': 'Artificial check.'},
                {'path': adoption.PATHS[1], 'before': self.before['expression']['data']['style'],
                 'after': 'Artificial response length follows the actual content.', 'reason': 'Artificial check.'},
                {'path': adoption.PATHS[2], 'before': self.before['character']['data']['social']['public_image'],
                 'after': 'Artificial particular opinions without a fixed response sequence.', 'reason': 'Artificial check.'}]}
        atomic_json(self.draft_path, self.draft)
        (self.draft_path.parent / adoption.REVIEW_NAME).write_text('Artificial exact review text.', encoding='utf-8')
        self.original_bytes = {name: (self.private.root / self.record['id'] / name).read_bytes()
                               for name in ('source-package.json', 'source-archive.md', 'initial-relationship.txt')}
        self.network = patch('socket.socket.connect', side_effect=AssertionError('network forbidden')); self.network.start()
        self.addCleanup(self.network.stop)

    def approve(self):
        atomic_json(self.draft_path.parent / 'approval.json', {
            'draft_sha256': adoption.sha(self.draft_path.read_bytes()),
            'review_sha256': adoption.sha((self.draft_path.parent / adoption.REVIEW_NAME).read_bytes()),
            'approved_at': timestamp(), 'user_answer': 'Approve this artificial exact draft.', 'scope': adoption.SCOPE})

    def save(self):
        return adoption.save(self.draft_path, self.paths.home)

    def data(self):
        return {k: v['data'] for k, v in adoption._versions(self.store, self.pid).items()}

    def check_originals(self):
        for name, raw in self.original_bytes.items():
            self.assertEqual((self.private.root / self.record['id'] / name).read_bytes(), raw)

    def test_review_is_read_only_and_exact_approval_is_required(self):
        self.assertEqual(adoption.verify(self.draft_path, self.paths.home)['status'], 'awaiting_user_review')
        with self.assertRaisesRegex(RuntimeErrorBase, 'approval_missing'): self.save()
        self.approve()
        with (self.draft_path.parent / adoption.REVIEW_NAME).open('a', encoding='utf-8') as f: f.write(' changed')
        with self.assertRaisesRegex(RuntimeErrorBase, 'approval_mismatch'): self.save()
        self.assertEqual(adoption._versions(self.store, self.pid), self.before)
        self.assertEqual(adoption._selected(self.store), self.seed)

    def test_save_is_exact_idempotent_and_restores_previous_selection(self):
        self.approve(); result = self.save()
        expected = {k: deepcopy(v['data']) for k, v in self.before.items()}
        expected['core']['core_traits'][0] = self.draft['replacements'][0]['after']
        expected['expression']['style'] = self.draft['replacements'][1]['after']
        expected['character']['social']['public_image'] = self.draft['replacements'][2]['after']
        self.assertEqual(self.data(), expected)
        self.assertEqual(adoption._selected(self.store), self.seed)
        versions = adoption._versions(self.store, self.pid)
        self.assertTrue(self.save()['already_saved']); self.assertEqual(adoption._versions(self.store, self.pid), versions)
        metadata = json.loads(Path(result['binding_path']).read_text(encoding='utf-8'))
        self.assertNotIn('replacements', metadata); self.assertNotIn('user_answer', metadata)
        self.assertNotIn(self.draft['replacements'][0]['after'], json.dumps(metadata))
        with self.assertRaisesRegex(RuntimeErrorBase, 'private_literary_content_changed'):
            self.private.check(self.store, self.record['id'])
        self.check_originals()

    def test_unknown_progress_and_changed_original_are_not_automatically_resumed(self):
        self.approve()
        atomic_json(self.draft_path.parent / 'save-progress.json', {'status': 'saving'})
        with self.assertRaisesRegex(RuntimeErrorBase, 'save_requires_inspection'): self.save()
        self.assertEqual(adoption._versions(self.store, self.pid), self.before)
        source = self.private.root / self.record['id'] / 'source-package.json'
        source.write_bytes(source.read_bytes() + b'\n')
        with self.assertRaisesRegex(RuntimeErrorBase, 'original_bytes_changed'):
            adoption.verify(self.draft_path, self.paths.home)

    def test_source_drift_and_invalid_schema_are_rejected_before_write(self):
        self.draft['replacements'][2]['after'] = 'X' * 2001
        atomic_json(self.draft_path, self.draft); self.approve()
        with self.assertRaisesRegex(RuntimeErrorBase, 'invalid_character_text'): self.save()
        self.assertEqual(adoption._versions(self.store, self.pid), self.before)
        self.draft['replacements'][2]['after'] = 'Valid artificial new image.'
        atomic_json(self.draft_path, self.draft); self.approve()
        with self.store.connection(write=True) as db:
            new = deepcopy(self.before['core']['data']); new['introduction'] = 'Unrelated artificial new author edit.'
            ProfileService(self.store)._write(db, self.pid, 'core', new, self.before['core']['id'], 'outside-edit')
        current = adoption._versions(self.store, self.pid)
        with self.assertRaisesRegex(RuntimeErrorBase, 'source_versions_changed'): self.save()
        self.assertEqual(adoption._versions(self.store, self.pid), current)

    def test_third_write_failure_compensates_and_never_replays(self):
        self.approve()
        with patch.object(CharacterService, '_write', side_effect=RuntimeErrorBase('artificial_third_failure')):
            with self.assertRaisesRegex(RuntimeErrorBase, 'artificial_third_failure'): self.save()
        self.assertEqual(self.data(), {k: v['data'] for k, v in self.before.items()})
        self.assertEqual(adoption._selected(self.store), self.seed)
        result = json.loads((self.draft_path.parent / 'save-progress.json').read_text(encoding='utf-8'))
        self.assertEqual(result['status'], 'failed_rolled_back')
        versions = adoption._versions(self.store, self.pid)
        with self.assertRaisesRegex(RuntimeErrorBase, 'save_requires_inspection'): self.save()
        self.assertEqual(adoption._versions(self.store, self.pid), versions)

    def test_metadata_write_failure_compensates_all_three_and_selection(self):
        self.approve(); real = adoption.atomic_json
        def fail_metadata(path, value):
            if Path(path).parent.name == 'author-adoptions': raise OSError('artificial metadata persistence failure')
            return real(path, value)
        with patch.object(adoption, 'atomic_json', side_effect=fail_metadata):
            with self.assertRaisesRegex(OSError, 'artificial metadata'): self.save()
        self.assertEqual(self.data(), {k: v['data'] for k, v in self.before.items()})
        self.assertEqual(adoption._selected(self.store), self.seed)
        result = json.loads((self.draft_path.parent / 'save-progress.json').read_text(encoding='utf-8'))
        self.assertEqual(result['status'], 'failed_rolled_back')
        self.assertEqual(set(result['compensation']), {'core', 'expression', 'character'})

    def test_external_selection_is_preserved_and_partial_state_is_explicit(self):
        self.approve(); other = self.runtime.create_persona(generic_persona())['id']
        untouched = self.runtime.show_persona(other); real = adoption.atomic_json; injected = []
        def change_selection(path, value):
            result = real(path, value)
            if Path(path).name == 'save-progress.json' and value.get('completed_kinds') == ['core'] and not injected:
                injected.append(True); self.runtime.use_persona(other)
            return result
        with patch.object(adoption, 'atomic_json', side_effect=change_selection):
            with self.assertRaisesRegex(RuntimeErrorBase, 'selection_changed'): self.save()
        self.assertEqual(adoption._selected(self.store), other)
        self.assertEqual(self.runtime.show_persona(other), untouched)
        result = json.loads((self.draft_path.parent / 'save-progress.json').read_text(encoding='utf-8'))
        self.assertEqual(result['status'], 'failed_requires_inspection')
        self.assertEqual(result['compensation']['core'], 'selection_conflict')

    def test_copy_uses_approved_current_visible_content_and_no_history(self):
        self.approve(); binding = self.save()['binding_path']
        result = adoption.prepare_copy(binding, self.paths.home)
        self.assertEqual(adoption._selected(self.store), self.seed)
        copy_store = Store(RuntimePaths(Path(result['home'])))
        copied = adoption._versions(copy_store, result['persona_id'])
        self.assertEqual(copied['core']['data'], self.data()['core'])
        self.assertEqual(copied['expression']['data'], self.data()['expression'])
        self.assertEqual(copied['relationship']['data'], self.data()['relationship'])
        self.assertEqual([r['id'] for r in copied['character']['data']['lorebook']], ['public'])
        self.assertFalse((Path(result['home']) / 'source-archive.md').exists())
        self.assertFalse(any(result['empty_history_counts'].values()))
        again = adoption.prepare_copy(binding, self.paths.home)
        self.assertTrue(again['already_prepared']); self.assertEqual(again['home'], result['home'])
        # Changing the copy and its self-reported marker cannot authorize new content.
        service = ProfileService(copy_store); current = service.current('expression')
        service.update('expression', {'style': 'Artificial unapproved copy edit.'}, current['id'], 'copy-outside-edit')
        marker_path = Path(result['home']) / 'private-sandbox.json'
        marker = json.loads(marker_path.read_text(encoding='utf-8'))
        marker['copy_versions'] = adoption._descriptors(adoption._versions(copy_store, result['persona_id']))
        atomic_json(marker_path, marker)
        with self.assertRaisesRegex(RuntimeErrorBase, 'copy_approved_content_changed'):
            adoption.prepare_copy(binding, self.paths.home)
        self.check_originals()


if __name__ == '__main__': unittest.main()
