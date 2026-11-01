import unittest
from scripts.screen_m763_r06 import output_flags


class MechanicalScreen(unittest.TestCase):
    def test_missing_output_is_explicit(self):
        self.assertEqual(output_flags({},None),['missing_output'])

    def test_required_literal_is_only_a_review_hint(self):
        case={'required_substrings':['19.90','Q-004']}
        self.assertEqual(output_flags(case,'19.9 Q-004'),['required_literal_missing:0'])
        self.assertEqual(output_flags(case,'19.90 Q-004'),[])

    def test_user_json_is_not_internal_protocol_leak(self):
        self.assertEqual(output_flags({},'{"days":7}'),[])
        self.assertIn('possible_internal_structure_leak',output_flags({},'{"behavior_intent":"respond"}'))

    def test_code_fence_is_not_silently_repaired(self):
        value='```python\nprint(1)'
        self.assertEqual(output_flags({},value),['unbalanced_code_fence'])
        self.assertEqual(output_flags({},value+'\n```'),[])
