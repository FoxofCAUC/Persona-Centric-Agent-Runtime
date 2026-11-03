from collections import Counter
import json
from pathlib import Path
import unittest

from tests.dialogue_context_challenges import challenges, evidence_for, run_diagnostics


PROTOCOL = Path(__file__).parent/'fixtures/dialogue-context-real-protocol-v1.json'


class CR21Protocol(unittest.TestCase):
    def test_planned_counts_and_complete_case_labels(self):
        p=json.loads(PROTOCOL.read_text('utf-8'))
        self.assertEqual(p['generation']['normal_main_positions'],16*2*2*2)
        self.assertEqual(p['generation']['normal_generation_positions'],224)
        self.assertEqual(p['generation']['aggregate_cap'],None)
        self.assertEqual(set(p['cases']),{f'S{i:02d}' for i in range(1,17)})
        for ident,row in p['cases'].items():
            self.assertTrue(row['required_content'],ident)
            self.assertTrue(row['allowed'],ident)
            self.assertTrue(row['forbidden'],ident)
            self.assertEqual(bool(row['frame_key_items']),ident>='S09')
        self.assertFalse(p['acceptance']['naturalness_is_hard_gate'])
        self.assertTrue(p['acceptance']['all_candidate_explicit_tasks_and_boundaries'])

    def test_anonymous_cases_balance_without_score_selection(self):
        p=json.loads(PROTOCOL.read_text('utf-8'));r=p['anonymous_review'];rows=r['entries']
        self.assertEqual(len(rows),12)
        self.assertEqual(len({x['scene'] for x in rows}),12)
        self.assertEqual(Counter(x['role'] for x in rows),{'process':6,'candid':6})
        self.assertEqual(Counter(x['route'] for x in rows),{'direct':6,'joint':6})
        self.assertEqual(Counter(x['candidate_side'] for x in rows),{'A':6,'B':6})
        self.assertEqual(r['orientation_seed'],'cr21-review-v1')
        self.assertEqual(r['selection'],'fixed_before_outputs_not_score_selected')
        for x in rows:self.assertIn(x['replicate'],(1,2))

    def test_challenges_are_complete_and_labels_not_in_evidence(self):
        rows=challenges()
        self.assertEqual(len(rows),16);self.assertEqual(len({r['id'] for r in rows}),16)
        self.assertEqual(Counter(x['expected_path'] for x in rows),{'direct':8,'joint':8})
        for case in rows:
            value=evidence_for(case).document()
            self.assertNotIn('expected_path',value);self.assertNotIn('label_basis',value)
            self.assertTrue(case['label_basis']);self.assertTrue(value['current'])

    def test_challenges_execute_without_forcing_all_matches(self):
        before=challenges();report=run_diagnostics()
        self.assertEqual(report['count'],16);self.assertEqual(report['real_model_calls'],0)
        self.assertEqual(before,challenges())
        self.assertFalse(report['rules_modified']);self.assertFalse(report['independent_holdout_claim'])
        self.assertTrue(all(x['actual_path'] in ('direct','joint') for x in report['rows']))
        # A mismatch is a reported development observation, not a fixture failure.
        self.assertEqual(set(report['misses']),{r['id'] for r in report['rows'] if r['expected_path']=='joint' and r['actual_path']=='direct'})


if __name__=='__main__':unittest.main()
