"""E0预检在换行差异、材料篡改和范围漂移下的行为。"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.pcar_expression_preflight import (
    check_core, check_materials, check_upstream, source_digest, validate_material,
)


REPO = Path(__file__).resolve().parents[1]


class ExpressionPreflightTests(unittest.TestCase):
    def test_source_checkout_newlines_are_portable_and_code_drift_is_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            path = repo / 'persona_runtime/chat.py'
            path.parent.mkdir()
            path.write_bytes(b'answer = 1\r\n')
            baseline = {'commit': 'fixed', 'allowed_new_core_files_e0': [],
                        'files': [{'path': 'persona_runtime/chat.py', 'sha256': source_digest(b'answer = 1\n')}]}
            self.assertTrue(check_core(repo, baseline)['passed'])
            path.write_bytes(b'answer = 2\r\n')
            self.assertEqual(check_core(repo, baseline)['errors'][0]['code'], 'frozen_core_changed')

    def test_unregistered_runtime_extension_and_path_escape_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            (repo / 'persona_runtime').mkdir()
            (repo / 'persona_runtime/unplanned.py').write_text('pass\n', encoding='utf-8')
            baseline = {'commit': 'fixed', 'allowed_new_core_files_e0': [], 'files': []}
            self.assertEqual(check_core(repo, baseline)['errors'][0]['code'], 'unregistered_core_extension')
            baseline['files'] = [{'path': '../outside.py', 'sha256': ''}]
            with self.assertRaisesRegex(ValueError, 'path_outside_selected_root'):
                check_core(repo, baseline)

    def test_public_plan_contains_only_bounded_new_expression_positions(self):
        report = check_materials(REPO)
        self.assertTrue(report['passed'], report['errors'])
        self.assertEqual(report['expression_positions'], 32)
        self.assertEqual(report['local_materials_checked'], 0)

    def test_cross_persona_candidate_is_detected_in_fixed_materials(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            target = repo / 'experiments/expression_e0'
            target.mkdir(parents=True)
            for name in ('materials_manifest.json', 'public_fixtures.json', 'call_plan.json'):
                (target / name).write_bytes((REPO / 'experiments/expression_e0' / name).read_bytes())
            path = target / 'public_fixtures.json'
            fixtures = json.loads(path.read_text(encoding='utf-8'))
            fixtures['assets'][0]['persona'] = 'other-persona'
            path.write_text(json.dumps(fixtures, ensure_ascii=False), encoding='utf-8')
            self.assertIn('candidate_persona_mismatch', [e['code'] for e in check_materials(repo)['errors']])

    def test_authored_boundary_draft_is_not_accepted_as_provider_return(self):
        fixtures = json.loads((REPO / 'experiments/expression_e0/public_fixtures.json').read_text(encoding='utf-8'))
        fixture = fixtures['cases'][-1]
        case = {'id': fixture['id'], 'source_kind': 'authored_public_fixture', 'persona': fixture['persona'], 'candidate_ids': fixture['candidate_ids']}
        material = {'version': 'expression-e1-input-snapshot-v1', 'case_id': case['id'],
                    'source_kind': case['source_kind'], 'persona': case['persona'],
                    'draft': fixture['draft'], 'draft_sha256': hashlib.sha256(fixture['draft'].encode()).hexdigest(),
                    'user': fixture['user'], 'role_projection': fixtures['personas'][fixture['persona']],
                    'candidate_ids': fixture['candidate_ids'], 'provider_generated': False, 'protected': []}
        self.assertEqual(validate_material(material, case, fixtures), [])
        wrong = deepcopy(material)
        wrong['provider_generated'] = True
        self.assertIn('material_provenance_mismatch', [e['code'] for e in validate_material(wrong, case, fixtures)])
        wrong = deepcopy(material)
        wrong['draft'] = '好。再额外加一句。'
        self.assertIn('public_fixture_text_changed', [e['code'] for e in validate_material(wrong, case, fixtures)])

    def test_upstream_blob_verification_keeps_original_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = b'upstream\n'
            (root / 'source.py').write_bytes(data)
            blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
            ledger = {'commit': 'fixed', 'verified_files': [{'path': 'source.py', 'blob': blob,
                      'sha256': hashlib.sha256(data).hexdigest()}], 'components': []}
            self.assertTrue(check_upstream(root, ledger)['passed'])
            (root / 'source.py').write_bytes(b'upstream\r\n')
            self.assertEqual(check_upstream(root, ledger)['errors'][0]['code'], 'upstream_hash_mismatch')


if __name__ == '__main__':
    unittest.main()
