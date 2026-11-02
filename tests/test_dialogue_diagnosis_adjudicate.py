from copy import deepcopy
import unittest

from scripts import dialogue_diagnosis_adjudicate as final


class UserReview(unittest.TestCase):
    def test_complete_actual_choices_required(self):
        rows = [{'number': i, 'choice': 'B', 'reason': None} for i in range(1, 13)]
        self.assertEqual(len(final.validate_review(rows)), 12)
        with self.assertRaisesRegex(ValueError, 'cr1_user_review_coverage'):
            final.validate_review(rows[:-1])

    def test_duplicate_or_invalid_choice_rejected(self):
        rows = [{'number': i, 'choice': 'B'} for i in range(1, 13)]
        duplicate = deepcopy(rows); duplicate[-1]['number'] = 1
        with self.assertRaisesRegex(ValueError, 'cr1_user_review_coverage'):
            final.validate_review(duplicate)
        rows[-1]['choice'] = 'machine_guess'
        with self.assertRaisesRegex(ValueError, 'cr1_user_review_choice'):
            final.validate_review(rows)

    def test_same_letter_can_select_candidate_or_control(self):
        review = [{'number': i, 'choice': 'B'} for i in range(1, 13)]
        pairs, mapping = [], []
        for i in range(1, 13):
            a, b = f'D{i}/B1/r1', f'D{i}/B0/r1'
            pair = {'id': str(i), 'candidate': a, 'control': b, 'comparison': 'B1-B0',
                    'verdict': 'tie', 'scene': f'D{i}', 'role': 'role'}
            pairs.append(pair)
            mapping.append({'number': i, 'pair': str(i), 'A': a if i % 2 else b, 'B': b if i % 2 else a})
        resolved = final.resolve_review(review, mapping, pairs, pairs)
        self.assertEqual([r['user_candidate_verdict'] for r in resolved[:2]], ['loss', 'win'])
        self.assertTrue(all(r['user_reason'] is None for r in resolved))


class PolicyOverlay(unittest.TestCase):
    def setUp(self):
        self.rows = [{'position_id': 'D10-lean/B1/r1', 'output_hash': 'unchanged',
                      'dimensions': {'im_naturalness': {'score': 4, 'reason': 'original'}},
                      'obligations': [], 'sample_use': {'category': 'not_observable'},
                      'boundaries': [{'status': 'violated', 'excerpt': '下回请你喝饮料', 'reason': 'strict'}]}]
        self.decision = {'kind': 'ordinary_courtesy_in_unsent_draft', 'choice': 'allow',
                         'affected_positions': ['D10-lean/B1/r1']}

    def test_overlay_preserves_original_scores_outputs_and_mapping_inputs(self):
        original = deepcopy(self.rows)
        revised, changes = final.apply_draft_policy(self.rows, self.decision)
        self.assertEqual(self.rows, original)
        self.assertEqual(revised[0]['dimensions'], original[0]['dimensions'])
        self.assertEqual(revised[0]['output_hash'], original[0]['output_hash'])
        self.assertEqual(revised[0]['boundaries'][0]['status'], 'respected')
        self.assertEqual(len(changes), 1)

    def test_cannot_override_another_kind_of_boundary(self):
        self.decision['affected_positions'] = ['D18-lean/B0/r1']
        with self.assertRaisesRegex(ValueError, 'cr1_adjudication_target'):
            final.apply_draft_policy(self.rows, self.decision)

    def test_disallow_does_not_change_strict_view(self):
        self.decision['choice'] = 'disallow'
        revised, changes = final.apply_draft_policy(self.rows, self.decision)
        self.assertEqual(revised, self.rows)
        self.assertEqual(changes, [])


if __name__ == '__main__': unittest.main()
