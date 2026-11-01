import json
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest

from persona_runtime.evaluation import EvaluationStore
from persona_runtime.paths import RuntimePaths
from scripts.prepare_m763_review import PACKAGE, copy_for_review, validate_reviews, validate_context_replay


class ReviewPublication(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = EvaluationStore(RuntimePaths(root / 'source'))
        self.target = EvaluationStore(RuntimePaths(root / 'target'))
        self.folder = root / 'checks'
        self.folder.mkdir()
        cases = [{'id': str(i), 'scenario': '人工工具夹具', 'author_summary': '工具模拟资料'} for i in range(100)]
        self.source.create(PACKAGE, cases, {'unit_test_only': True}, simulated=False)
        self.rows = []
        with self.source.connect(PACKAGE, True) as db:
            for case in cases:
                for arm in 'ABC':
                    aid = 'synthetic-' + case['id'] + arm
                    output = '模拟输出' + aid
                    db.execute("UPDATE slots SET state='success',attempt_id=?,output=? WHERE case_id=? AND arm=?",
                               (aid, output, case['id'], arm))
                    self.rows.append({'case_id': case['id'], 'review_id': sha256(aid.encode()).hexdigest()[:16],
                                      'output_sha256': sha256(output.encode()).hexdigest(),
                                      'task_check': 'pass', 'reason': '模拟单测标签，不是真实评审'})

    def write_checks(self, rows=None, **overrides):
        data = {'reviewer': 'execution_assistant', 'additional_model_calls': 0,
                'human_blind_reviews_written': 0, 'naturalness_assessed': False,
                'rows': rows if rows is not None else self.rows}
        data.update(overrides)
        (self.folder / 'batch-001.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

    def test_complete_copy_preserves_source_and_blank_anonymous_reviews(self):
        self.write_checks()
        evidence = validate_reviews(self.source, self.folder)
        self.assertEqual(evidence['outputs'], 300)
        source = self.source.path(PACKAGE)
        before = source.read_bytes()
        result = copy_for_review(source, self.target, 'review-v1', {'test': True})
        self.assertEqual(before, source.read_bytes())
        self.assertFalse(result['quality_gate_passed'])
        self.assertFalse(result['live_authorized'])
        for batch in range(1, 5):
            rows = self.target.load('review-v1', batch)['cases']
            self.assertEqual(len(rows), 25)
            for row in rows:
                self.assertEqual(set(row), {'case_id', 'scenario', 'author_summary', 'left', 'right', 'revision', 'review'})
                self.assertEqual(row['revision'], 0)
                self.assertIsNone(row['review'])
        target_before = self.target.path('review-v1').read_bytes()
        with self.assertRaises(FileExistsError):
            copy_for_review(source, self.target, 'review-v1', {})
        self.assertEqual(target_before, self.target.path('review-v1').read_bytes())

    def test_missing_duplicate_stale_and_wrong_scope_reviews_rejected(self):
        self.write_checks(self.rows[:-1])
        with self.assertRaisesRegex(ValueError, 'coverage_incomplete'):
            validate_reviews(self.source, self.folder)
        self.write_checks(self.rows + [self.rows[0]])
        with self.assertRaisesRegex(ValueError, 'output_binding'):
            validate_reviews(self.source, self.folder)
        self.write_checks([self.rows[0] | {'output_sha256': 'changed'}] + self.rows[1:])
        with self.assertRaisesRegex(ValueError, 'output_binding'):
            validate_reviews(self.source, self.folder)
        self.write_checks(naturalness_assessed=True)
        with self.assertRaisesRegex(ValueError, 'wrong_scope'):
            validate_reviews(self.source, self.folder)

    def test_real_task_errors_remain_visible_and_not_replaced_with_pass(self):
        self.write_checks([self.rows[0] | {'task_check': 'task_error'}] + self.rows[1:])
        result = validate_reviews(self.source, self.folder)
        self.assertEqual(result['task_checks'], {'task_error': 1, 'pass': 299})

    def test_incomplete_or_simulated_or_started_package_not_published(self):
        source = self.source.path(PACKAGE)
        for key, value, error in [('simulated', True, 'simulation_rejected'), ('frozen', True, 'already_started')]:
            with self.source.connect(PACKAGE, True) as db:
                db.execute('UPDATE metadata SET value=? WHERE key=?', (json.dumps(value), key))
            with self.assertRaisesRegex(ValueError, error):
                copy_for_review(source, self.target, 'review-v1', {})
            self.assertFalse(self.target.path('review-v1').exists())
            self.assertEqual(list(self.target.root.glob('.review-stage-*')), [])
            with self.source.connect(PACKAGE, True) as db:
                db.execute('UPDATE metadata SET value=? WHERE key=?', ('false', key))
        with self.source.connect(PACKAGE, True) as db:
            db.execute("UPDATE slots SET state='accepted' WHERE case_id='0' AND arm='A'")
        with self.assertRaisesRegex(ValueError, 'outputs_incomplete'):
            copy_for_review(source, self.target, 'review-v1', {})
        self.assertFalse(self.target.path('review-v1').exists())

    def test_target_id_cannot_escape_controlled_directory(self):
        with self.assertRaisesRegex(Exception, 'invalid_id'):
            copy_for_review(self.source.path(PACKAGE), self.target, '../runtime', {})

    def test_context_replay_requires_every_actual_successful_attempt(self):
        with self.source.connect(PACKAGE) as db:
            rows = [{'attempt_id': r['attempt_id'], 'reproduced': True} for r in db.execute('SELECT attempt_id FROM slots')]
        report = {'passed': True, 'model_calls': 0, 'authority_read_only': True, 'successful_contexts': 300, 'rows': rows}
        validate_context_replay(self.source, report)
        for bad in (report | {'rows': rows[:-1]}, report | {'model_calls': 1},
                    report | {'rows': [rows[0]] * 300}, report | {'rows': [rows[0] | {'reproduced': False}] + rows[1:]}):
            with self.assertRaisesRegex(ValueError, 'context_replay_incomplete'):
                validate_context_replay(self.source, bad)


if __name__ == '__main__':
    unittest.main()
