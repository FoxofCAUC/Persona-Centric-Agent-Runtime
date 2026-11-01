from copy import deepcopy
import unittest
from scripts.r4_expression_assessment import calculate
from tests.r4_expression_fixture import rubric


class AssessmentTests(unittest.TestCase):
    def setUp(self):
        self.items=[{'key':f'A{n:02}-{role}-{arm}','pair':f'A{n:02}-{role}','case':f'A{n:02}','arm':arm}
            for n in range(1,13) for role in ('detail','stance') for arm in ('control','experiment')]
        rows=[{'key':i['key'],'issues':[],'specific_engagement':True,'boundary_pass':True,'task_pass':True,'sample_usage':'natural_transfer'} for i in self.items]
        rows[0]['issues']=['redundant_restatement'];rows[2]['issues']=['strategy_narration']
        pairs=[{'pair':p,'verdict':'win'} for p in dict.fromkeys(i['pair'] for i in self.items)]
        self.review={'requests':rows,'pairs':pairs};self.metrics={'real':True,'complete_requests':48,'all_complete_differences_explained':True}
        self.diffs=[{'pair':p['pair'],'primary':int(p['pair'][1:3])<=9,'eligible_for_directional_analysis':True} for p in pairs]
    def result(self):return calculate(self.review,{'items':self.items},self.metrics,self.diffs,rubric())
    def test_simulation_cannot_claim_real_effect(self):
        self.assertTrue(self.result()['effect_acceptance_passed']);self.metrics['real']=False
        self.assertFalse(self.result()['effect_acceptance_passed'])
    def test_two_issue_types_in_one_request_do_not_count_as_two_improvements(self):
        self.review['requests'][0]['issues']+=['strategy_narration'];self.review['requests'][2]['issues']=[]
        self.assertFalse(self.result()['checks']['issues_reduced_at_least_two'])
    def test_new_boundary_failure_cannot_be_offset_by_fixing_another(self):
        self.review['requests'][0]['boundary_pass']=False
        self.review['requests'][3]['boundary_pass']=False
        self.assertEqual(self.result()['arm_statistics']['control']['hard_boundary_failures'],1)
        self.assertFalse(self.result()['checks']['no_hard_boundary_regression'])
    def test_generic_short_replies_or_missing_task_content_do_not_pass(self):
        self.review['requests'][1]['specific_engagement']=False
        self.assertFalse(self.result()['checks']['specific_engagement_not_decreased'])
        self.review['requests'][-1]['task_pass']=False
        self.assertFalse(self.result()['checks']['all_task_controls_pass'])
    def test_backend_ineligible_preferences_do_not_count(self):
        for d in self.diffs:d['eligible_for_directional_analysis']=False
        self.assertFalse(self.result()['checks']['eligible_primary_pairs_at_least_four'])
        self.assertFalse(self.result()['checks']['eligible_primary_wins_more_than_losses'])
