import unittest
from scripts.provider_usage_summary import usage_summary


class UsageTests(unittest.TestCase):
    def test_different_gateway_conventions_never_double_count(self):
        rows=[{'usage':{'prompt_tokens':100,'completion_tokens':50,'total_tokens':150,
                       'completion_tokens_details':{'reasoning_tokens':30}}},
              {'usage':{'prompt_tokens':100,'completion_tokens':20,'total_tokens':150,
                       'completion_tokens_details':{'reasoning_tokens':30}}},
              {'usage':None}]
        result=usage_summary(rows)
        self.assertEqual(result['known_usage']['total_tokens'],300)
        self.assertEqual(result['known_usage']['reasoning_tokens'],60)
        self.assertEqual(result['reported_arithmetic'],{
            'reported_total_includes_reasoning_in_completion':1,
            'reported_total_adds_reasoning_separately':1})
        self.assertEqual(result['unknown_usage_calls'],1)

    def test_missing_or_inconsistent_fields_remain_unknown(self):
        result=usage_summary([{'usage':{'prompt_tokens':5}},
            {'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':99,
                      'completion_tokens_details':{'reasoning_tokens':3}}}])
        self.assertEqual(result['known_usage']['total_tokens'],99)
        self.assertEqual(result['reported_arithmetic'],{'unknown':1,'other_or_inconsistent':1})
