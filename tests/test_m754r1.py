"""Layered evaluation and frozen retrieval compatibility; no live models."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from scripts.character_evaluation import ranking_metrics, selection_check, choose_threshold, quality_gate, rejection_margins
import test_m752 as previous
from persona_runtime.character_retrieval import rank_candidates, assemble
from persona_runtime.character_embedding import digest
from persona_runtime.character_policy import checked


class EvaluationTests(unittest.TestCase):
    def case(self, **overrides):
        return dict(required_ids=['a'],allowed_ids=['a'],forbidden_ids=['private'],**overrides)

    def test_extra_candidate_fails_even_if_required_present(self):
        result=selection_check(self.case(),['a','unlabelled'])
        self.assertFalse(result['passed']); self.assertEqual(result['unexpected_ids'],['unlabelled'])

    def test_allowed_optional_and_missing(self):
        c=self.case();c['allowed_ids'].append('b')
        self.assertTrue(selection_check(c,['a','b'])['passed'])
        self.assertFalse(selection_check(c,['b'])['passed'])

    def test_private_and_hard_negative_are_hard_failures(self):
        self.assertTrue(selection_check(self.case(),['a','private'])['hard_violation'])
        c=dict(required_ids=[],allowed_ids=[],forbidden_ids=[],hard_negative=True)
        self.assertTrue(selection_check(c,['x'])['hard_violation'])

    def test_degraded_is_not_hybrid_success(self):
        self.assertFalse(selection_check(self.case(),['a'],degraded=True)['passed'])

    def test_ranking_layers_and_top5(self):
        self.assertEqual(ranking_metrics(['a'],['a']),{'recall5':1,'mrr5':1})
        self.assertEqual(ranking_metrics([],['a']),{'recall5':0,'mrr5':0})
        self.assertEqual(ranking_metrics(['x','y','z','u','v','a'],['a'])['recall5'],0)

    def test_margin_tie_break_not_highest_threshold(self):
        rows=[dict(threshold=.5,hits=14,margin=.1,violations=0),dict(threshold=.6,hits=14,margin=.01,violations=0),dict(threshold=.4,hits=16,margin=.2,violations=1)]
        self.assertEqual(choose_threshold(rows)['threshold'],.5)
        self.assertIsNone(choose_threshold([rows[-1]]))

    def test_budget_and_ambiguity_not_threshold_margin(self):
        rows=[dict(item_id='budget',score=.9),dict(item_id='alias',score=.8),dict(item_id='other',score=.4)]
        self.assertAlmostEqual(rejection_margins(rows,.5,set(),{'budget','alias'})[0],.1)
        self.assertEqual(rejection_margins(rows,.5,{'other'},{'budget','alias'}),[])

    def test_hard_violation_cannot_hide_in_negative_allowance(self):
        rows=[dict(group='positive',filtered={'recall5':1},lexical_candidates={'recall5':0},passed=True,hard_violation=False,degraded=False,selected=['a']) for _ in range(20)]
        rows += [dict(group='negative',passed=True,hard_violation=False,degraded=False,selected=[]) for _ in range(20)]
        rows += [dict(group='contract',passed=True,hard_violation=False,degraded=False,selected=[]) for _ in range(8)]
        self.assertTrue(quality_gate(rows))
        rows[20].update(selected=['wrong'],hard_violation=True)
        self.assertFalse(quality_gate(rows))

    def test_old_and_new_configuration_snapshots(self):
        c=previous.config();c['model_fingerprint']='a'*64
        original=checked(dict(mode='hybrid',config=c,config_fingerprint=digest(c)))
        new=deepcopy(c);new.update(calibration_version='r1');new['thresholds']['lore']=.5
        changed=checked(dict(mode='hybrid',config=new,config_fingerprint=digest(new)))
        self.assertEqual(original['config']['thresholds']['lore'],.6)
        self.assertNotEqual(original['config_fingerprint'],changed['config_fingerprint'])


class DiagnosticTests(unittest.TestCase):
    setUp=previous.RetrievalTests.setUp
    save=previous.RetrievalTests.save
    retriever=previous.RetrievalTests.retriever

    def test_raw_top1_survives_rejection_without_text_or_terms(self):
        v=self.version;row=v['data']['lorebook'][0]
        raw={'version_id':v['id'],'model_fingerprint':'test-space-v1','index_fingerprint':'cache',
             'by_kind':{'lore':[previous.candidate(row,score=.5)],'example':[]}}
        with patch.object(self.index,'query',return_value=raw):
            result=self.retriever().retrieve(self.pid,v['id'],['换个话题'])
        self.assertEqual(result['raw_candidates']['lore'][0]['item_id'],row['id'])
        self.assertEqual(result['candidates']['lore'],[])
        self.assertEqual(result['lore_ids'],[])
        self.assertEqual(set(result['raw_candidates']['lore'][0]),{'item_id','score','member_index'})

    def test_budget_removal_is_distinct_from_retrieval_miss(self):
        v=deepcopy(self.version);v['data']['lorebook'][0]['text']='长段资料'*390
        row=v['data']['lorebook'][0]
        ranked,rejected=rank_candidates(v,['换个话题'],{'lore':[previous.candidate(row,score=.8)]},previous.config())
        selected=assemble(v,['换个话题'],ranked,2)
        self.assertEqual(rejected,[]);self.assertTrue(ranked['lore'])
        self.assertFalse(selected['lore_ids']);self.assertTrue(selected['removed'])


if __name__=='__main__':unittest.main()
