"""Regression for control-invariant reporting; no models or independent data."""
import unittest

from scripts.finalize_m763_review import control_invariant_result


class ControlInvariants(unittest.TestCase):
    def evaluate(self, *, b_task='pass', c_task='pass', output='27', required=None):
        return control_invariant_result(
            {'id':'artificial-control','required_substrings':['27'] if required is None else required},
            b_task,c_task,'respond','explain',output)

    def test_style_label_difference_is_not_answer_change(self):
        result=self.evaluate()
        self.assertTrue(result['action_label_difference'])
        self.assertFalse(result['invariant_violation'])

    def test_task_failure_or_missing_required_answer_still_fails(self):
        for verdict in ('uncertain','critical_error'):
            with self.subTest(verdict=verdict):
                self.assertTrue(self.evaluate(c_task=verdict)['invariant_violation'])
        self.assertTrue(self.evaluate(output='28')['invariant_violation'])

    def test_improved_baseline_failure_is_not_unnecessary_change(self):
        self.assertFalse(self.evaluate(b_task='critical_error')['invariant_violation'])

    def test_missing_output_cannot_be_treated_as_pass(self):
        with self.assertRaisesRegex(ValueError,'quality_control_evidence_missing'):
            self.evaluate(output=None)
