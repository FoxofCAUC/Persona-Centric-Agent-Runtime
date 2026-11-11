import unittest
from scripts.m9a_paired_report import dispatched,percentile,summarize,dispatch_counts

class ComparisonReportTests(unittest.TestCase):
    def test_nondispatch_and_fixed_input_are_not_paid_network_calls(self):
        self.assertFalse(dispatched({'kind':'web','physical':True,'state':'failed_or_unknown','diagnostics':{'request_headers_sent':False}}))
        self.assertFalse(dispatched({'kind':'fixed_source_input','physical':False,'state':'received'}))
        self.assertTrue(dispatched({'kind':'main','physical':True,'state':'received','diagnostics':{'request_headers_sent':True}}))
    def test_source_execution_is_separate_from_paid_dispatches_and_http_counts(self):
        rows=[{'kind':'main','state':'received'},{'kind':'web','state':'received'},
              {'kind':'wikimedia','state':'received','usage':{'source_network_requests':2}},
              {'kind':'fixed_source_input','physical':False,'state':'received'}]
        counts=dispatch_counts(rows)
        self.assertEqual(counts['physical_dispatches'],2)
        self.assertEqual(counts['external_execution_records'],3)
        self.assertEqual(counts['source_api_executions'],1)
        self.assertEqual(counts['source_api_completed_http_responses'],2)
        self.assertIsNone(counts['source_api_actual_http_attempts'])

    def test_unknown_dispatch_stays_unknown(self):
        self.assertIsNone(dispatched({'kind':'web','physical':True,'state':'failed_or_unknown','diagnostics':{}}))
    def test_latency_keeps_failure_and_recovery_values(self):
        self.assertEqual(percentile([1,2,100],.95),90.2)
    def test_completed_without_evidence_is_not_knowledge_success(self):
        base={'arm':'A','phase':'paired','expected_knowledge':True,'status':'completed','evidence_injection_pass':False,
              'direct_chitchat_pass':None,'seconds':3,'wall_seconds_with_operator_recovery':30,'error':None}
        report=summarize([base,base|{'evidence_injection_pass':True}])['A']
        self.assertEqual(report['completed'],2);self.assertEqual(report['evidence_injection_pass'],1)
        self.assertEqual(report['completed_without_required_evidence'],1)
