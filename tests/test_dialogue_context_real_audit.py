import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import dialogue_context_real_audit as audit


class SealTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'pack'
        self.source = Path(self.temp.name) / 'source'
        self.root.mkdir()
        self.source.mkdir()
        (self.source / 'example.py').write_bytes(b'pass\n')
        self.source_hash = audit.digest(b'pass\n')
        self.write('real-v1/state.json', {'status': 'completed', 'real': True, 'main_completed': 128, 'physical_calls': 224})
        self.summary = {'real': True, 'main_completed': 128, 'complete_pairs': 64,
                        'machine_reading_pending': 0, 'frame_outputs': 32, 'frame_machine_read': 32,
                        'user_review_received': 12, 'user_review_complete': True,
                        'receipts': {'reserved_attempts': 224}}
        self.write('real-v1/analysis/summary.json', self.summary)
        self.write('real-v1/freeze.json', {'sources': {'example.py': self.source_hash}})
        with zipfile.ZipFile(self.root / 'source.zip', 'w') as archive:
            archive.write(self.source / 'example.py', 'example.py')
        self.seal()
        self.patched = patch.object(audit, 'ROOT', self.source)
        self.patched.start()
        self.addCleanup(self.patched.stop)

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def seal(self):
        values = {p.relative_to(self.root).as_posix(): audit.digest(p.read_bytes())
                  for p in self.root.rglob('*') if p.is_file() and p.name != 'closure.json'}
        self.write('closure.json', {'stage': 'CR2.1', 'stage_complete': True,
                   'source_archive': 'source.zip', 'source_hashes': {'example.py': self.source_hash},
                   'evidence_hashes': values, 'effect_conclusion': 'development only'})

    def test_verification_is_read_only(self):
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertTrue(audit.verify(self.root, worktree=True)['passed'])
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_changed_evidence_is_rejected(self):
        (self.root / 'real-v1/state.json').write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'sealed_evidence_changed'):
            audit.verify(self.root)

    def test_added_evidence_is_rejected(self):
        (self.root / 'extra.txt').write_text('unexpected', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'evidence_inventory_changed'):
            audit.verify(self.root)

    def test_user_review_cannot_be_skipped(self):
        self.summary['user_review_complete'] = False
        self.write('real-v1/analysis/summary.json', self.summary)
        self.seal()
        with self.assertRaisesRegex(ValueError, 'required_work_incomplete'):
            audit.verify(self.root)

    def test_future_worktree_change_does_not_rewrite_history(self):
        (self.source / 'example.py').write_bytes(b'changed\n')
        self.assertTrue(audit.verify(self.root)['passed'])
        with self.assertRaisesRegex(ValueError, 'worktree_changed'):
            audit.verify(self.root, worktree=True)

    def test_path_escape_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'outside_package'):
            audit.child(self.root, '../other.txt')


if __name__ == '__main__':
    unittest.main()
