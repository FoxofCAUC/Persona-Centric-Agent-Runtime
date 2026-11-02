from pathlib import Path
import unittest
from unittest.mock import patch
from scripts import r4_expression_realization as common
from scripts import r4_expression_assessment as assessment
from scripts.r4_expression_assessment import calculate
from tests import test_r4_expression_assessment as previous_tests
from tests.r4_expression_fixture import rubric

class ProspectiveTaskPolicy(unittest.TestCase):
    setUp=previous_tests.AssessmentTests.setUp
    def result(self,policy='candidate-all-tasks-v1'):
        return calculate(self.review,{'items':self.items},self.metrics,self.diffs,rubric(),task_control_policy=policy)

    def test_legacy_failure_retained_but_repaired_candidate_can_pass(self):
        self.review['requests'][-2]['task_pass']=False
        self.assertFalse(self.result('both-arms-v1')['effect_acceptance_passed'])
        result=self.result();self.assertTrue(result['effect_acceptance_passed'])
        self.assertEqual(result['arm_statistics']['control']['task_control_failures'],1)

    def test_any_candidate_task_failure_still_fails(self):
        self.review['requests'][-1]['task_pass']=False
        self.assertFalse(self.result()['checks']['all_task_controls_pass'])
        self.review['requests'][-2]['task_pass']=False
        self.assertFalse(self.result()['effect_acceptance_passed'])

    def test_boundary_regression_cannot_be_hidden_by_task_pass(self):
        self.review['requests'][-1]['boundary_pass']=False
        r=self.result();self.assertTrue(r['checks']['all_task_controls_pass'])
        self.assertFalse(r['checks']['no_hard_boundary_regression']);self.assertFalse(r['effect_acceptance_passed'])

    def test_real_effect_and_attribution_gates_unchanged(self):
        self.metrics['real']=False;self.assertFalse(self.result()['effect_acceptance_passed'])
        self.metrics['real']=True
        for d in self.diffs:d['eligible_for_directional_analysis']=False
        self.assertFalse(self.result()['effect_acceptance_passed'])
        for row in self.review['requests']:row['issues']=[]
        self.assertFalse(self.result()['checks']['issues_reduced_at_least_two'])

    def test_old_seventh_assessment_stays_failed_without_rewriting(self):
        root=common.OUT/'real-batch-007';old=common.read(root/'analysis/final-assessment.json')
        args=[common.read(root/p) for p in ('analysis/machine-reading.json','prepared.json','analysis/metrics.json','analysis/request-differences.json','rubric.json')]
        legacy=calculate(*args);self.assertEqual(legacy['checks'],old['checks'])
        future=calculate(*args,task_control_policy='candidate-all-tasks-v1')
        self.assertTrue(future['checks']['all_task_controls_pass']);self.assertFalse(future['effect_acceptance_passed'])
        self.assertEqual({k:v for k,v in future['checks'].items() if k!='all_task_controls_pass'},
                         {k:v for k,v in legacy['checks'].items() if k!='all_task_controls_pass'})

    def test_unknown_policy_not_silently_treated_as_approval(self):
        with self.assertRaisesRegex(common.StopRun,'unknown_task_control_policy'):self.result('unapproved')

    def authorized_assessment(self,*,frozen_hash='authhash',created='2026-11-02T06:00:00+00:00',real=True):
        inputs={'prepared.json':{'items':[i|{'voice':{'version':'natural-dialogue-v8' if i['arm']=='experiment' else 'natural-dialogue-v2'}} for i in self.items],
                                 'real':real,'created_at':created},
          'fixture.json':{},'rubric.json':rubric(),'machine-reading.json':self.review|{'policy_version':'liveliness-priority-v1'},
          'metrics.json':self.metrics,'request-differences.json':self.diffs,'evaluation-policy-amendment.json':{'version':'liveliness-priority-v1'},
          'authorization.json':{'task_control_policy':'candidate-all-tasks-v1','task_control_authorization_hash':'approvalhash'},
          'task-gate-amendment-20261102.json':{'version':'candidate-all-tasks-v1','historical_regrading':False,'at':'2026-11-02T05:00:00+00:00'},
          'freeze.json':{'files':{'authorization.json':frozen_hash}}}
        with patch.object(assessment,'read',side_effect=lambda p:inputs[p.name]),patch.object(Path,'exists',return_value=True),\
             patch.object(assessment,'validate_reading'),patch.object(assessment,'atomic_json') as output,\
             patch.object(common,'sha',side_effect=lambda p:'approvalhash' if p.name.startswith('task-gate-amendment') else 'authhash'),\
             patch.object(common,'verify_freeze') as verify:
            result=assessment.assess(Path('nonexistent-artificial-assessment'))
            verify.assert_called_once();output.assert_called_once()
            return result

    def test_only_explicit_frozen_future_real_authorization_activates_new_policy(self):
        self.assertEqual(self.authorized_assessment()['task_control_policy'],'candidate-all-tasks-v1')
        for kwargs,error in (({'frozen_hash':'wrong'},'task_control_policy_not_frozen'),
                             ({'created':'2026-11-01T06:00:00+00:00'},'task_control_policy_predates_authorization'),
                             ({'real':False},'task_control_amendment_real_only')):
            with self.subTest(kwargs=kwargs),self.assertRaisesRegex(common.StopRun,error):self.authorized_assessment(**kwargs)
