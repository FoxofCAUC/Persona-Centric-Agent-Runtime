"""Seal tamper checks use artificial files and cannot generate replies."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import dialogue_communication_continuation_audit as audit


class ContinuationSealTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='cr31-cont-seal-', dir='D:/Persona-Centric Agent Runtime/tmp')
        self.root = Path(self.tmp.name)
        self.origin = self.root / 'origin'
        self.origin.mkdir()
        (self.origin / 'closure.json').write_text('{}', encoding='utf-8')
        (self.root / 'real-v1').mkdir()
        self.write('real-v1/state.json', {'status': 'completed'})
        with zipfile.ZipFile(self.root / 'source.zip', 'w') as archive:
            archive.writestr('artificial.py', b'pass\n')
        self.seal = {'stage': 'CR3-A.1-continuation', 'stage_complete': False,
                     'user_review': 'pending', 'source_archive': 'source.zip',
                     'source_hashes': {'artificial.py': audit.digest(b'pass\n')},
                     'origin_closure_sha256': audit.digest(b'{}'),
                     'evidence_hashes': {name: audit.digest((self.root / name).read_bytes())
                                         for name in ('real-v1/state.json', 'source.zip')}}
        self.write('checkpoint.json', self.seal)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value), encoding='utf-8')

    def verify(self):
        with patch.object(audit, 'ORIGIN', self.origin), patch('scripts.dialogue_communication_real_audit.verify', return_value={'passed': True}):
            return audit.verify(self.root)

    def test_checkpoint_is_read_only_and_not_stage_completion(self):
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = self.verify()
        self.assertTrue(result['passed'])
        self.assertFalse(result['stage_complete'])
        self.assertEqual(result['generation_calls'], 0)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_changed_evidence_rejected(self):
        self.write('real-v1/state.json', {'status': 'stopped'})
        with self.assertRaisesRegex(ValueError, 'evidence_changed'):
            self.verify()

    def test_changed_origin_rejected(self):
        (self.origin / 'closure.json').write_text('{"changed":true}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'origin_seal_changed'):
            self.verify()

    def test_user_review_cannot_be_assumed(self):
        self.seal['stage_complete'] = True
        self.write('checkpoint.json', self.seal)
        with self.assertRaisesRegex(ValueError, 'premature_completion'):
            self.verify()

    def test_outside_evidence_path_rejected(self):
        self.seal['evidence_hashes']['../outside'] = 'not-a-hash'
        self.write('checkpoint.json', self.seal)
        with self.assertRaisesRegex(ValueError, 'outside_path'):
            self.verify()


if __name__ == '__main__':
    unittest.main()
