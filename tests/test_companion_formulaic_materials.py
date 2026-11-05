"""Artificial materials, fixed review selection, and report-only checks."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from experiments import companion_formulaic_materials as materials


class FormulaicMaterials(unittest.TestCase):
    def complete_results(self):
        return [{'position_id': p['position_id'], 'status': 'completed',
                 'display': '人工输出-' + str(p['order'])} for p in materials.POSITIONS]

    def test_all_authored_quotes_and_explicit_questions_are_checkable(self):
        check = materials.validate_materials()
        self.assertEqual((check['cases'], check['positions'], check['semantic_units']), (3, 12, 23))
        self.assertEqual(check['semantic_equivalence'], 'authored_requires_reading')
        self.assertIsNone(check['provider_sampling_seed'])
        for case in materials.CASES:
            for turn in (1, 2):
                questions = [u for u in case['semantic_units'] if u['turn'] == turn and u['kind'] == 'question']
                self.assertTrue(questions)
                self.assertTrue(all(q['quotes']['fixed'] == q['quotes']['natural'] for q in questions))

    def test_bad_quote_and_question_changes_fail_validation(self):
        cases = deepcopy(materials.CASES)
        cases[0]['semantic_units'][0]['quotes']['fixed'] = 'not present'
        with patch.object(materials, 'CASES', cases), self.assertRaisesRegex(ValueError, 'quote_not_found'):
            materials.validate_materials()
        cases = deepcopy(materials.CASES)
        unit = next(u for u in cases[0]['semantic_units'] if u['kind'] == 'question')
        cases[0]['histories']['natural'][0] += '另一个明确问题？'
        unit['quotes']['natural'] = '另一个明确问题？'
        with patch.object(materials, 'CASES', cases), self.assertRaisesRegex(ValueError, 'question_changed'):
            materials.validate_materials()

    def test_histories_share_users_and_contain_no_evaluation_metadata(self):
        for case in materials.CASES:
            fixed = materials.history_messages(case['id'], 'fixed')
            natural = materials.history_messages(case, 'natural')
            self.assertEqual([m['role'] for m in fixed], ['user', 'assistant', 'user', 'assistant'])
            self.assertEqual(fixed[::2], natural[::2])
            self.assertNotEqual(fixed[1::2], natural[1::2])
            for messages in (fixed, natural):
                self.assertTrue(all(set(m) == {'role', 'content'} for m in messages))
                self.assertNotIn(case['current_input'], [m['content'] for m in messages])
            fixed[0]['content'] = 'outside mutation'
            self.assertNotEqual(materials.history_messages(case, 'fixed')[0]['content'], 'outside mutation')

    def test_pairs_are_adjacent_alternate_first_arm_and_cover_two_samples(self):
        positions = materials.POSITIONS
        self.assertEqual([p['order'] for p in positions], list(range(1, 13)))
        self.assertEqual(len({p['position_id'] for p in positions}), 12)
        for pair_index in range(6):
            first, second = positions[pair_index * 2:pair_index * 2 + 2]
            self.assertEqual(first['pair_id'], second['pair_id'])
            self.assertEqual({first['arm'], second['arm']}, {'fixed', 'natural'})
            self.assertEqual(first['arm'], 'fixed' if pair_index % 2 == 0 else 'natural')
        self.assertEqual({(p['case_id'], p['sample']) for p in positions},
                         {(case['id'], sample) for case in materials.CASES for sample in (1, 2)})

    def test_anonymous_review_uses_frozen_sample_one_without_replacement(self):
        rows = self.complete_results()
        target = f'F01-s1-{materials.ANONYMOUS_MAP["F01"]["A"]}'
        for row in rows:
            if row['position_id'] == target: row.update(status='failed', display='FAILED_BODY_NOT_DISPLAYED')
            if '-s2-' in row['position_id']: row['display'] = 'SAMPLE_TWO_MUST_NOT_REPLACE'
        text = materials.render_anonymous(rows)
        self.assertIn('保留空缺', text)
        self.assertNotIn('FAILED_BODY_NOT_DISPLAYED', text)
        self.assertNotIn('SAMPLE_TWO_MUST_NOT_REPLACE', text)
        for case in materials.CASES:
            self.assertIn(case['current_input'], text)
            for item in case['shared_background']: self.assertIn(item, text)
        self.assertNotIn('fixed', text); self.assertNotIn('natural', text)
        self.assertNotIn('F01-s1-', text)

    def test_machine_report_requires_reading_and_keeps_all_twelve_positions(self):
        rows = self.complete_results()
        rows[0].update(status='failed', display=None)
        rows[1]['display'] = '这个输出看似很自然？我同意。还要再问吗？'
        report = materials.machine_report_skeleton(rows)
        self.assertEqual(len(report['positions']), 12)
        self.assertEqual(len(report['pairs']), 6)
        self.assertEqual(report['pairs'][0]['verdict'], 'cannot_compare')
        self.assertTrue(all(p['verdict'] == 'unassessed' for p in report['pairs'][1:]))
        self.assertTrue(all(p['instant_message_naturalness'] is None for p in report['positions']))
        self.assertEqual(report['outcome'], 'not_assessed')
        self.assertEqual(report['positions'][1]['display'], rows[1]['display'])

    def test_unknown_or_duplicate_results_are_rejected(self):
        row = self.complete_results()[0]
        with self.assertRaisesRegex(ValueError, 'invalid_result_position'):
            materials.render_anonymous([row, row])
        with self.assertRaisesRegex(ValueError, 'invalid_result_position'):
            materials.machine_report_skeleton([{'position_id': 'outside', 'status': 'completed', 'display': '人工'}])


if __name__ == '__main__':
    unittest.main()
