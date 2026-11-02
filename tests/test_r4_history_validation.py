from copy import deepcopy
import unittest
from scripts import r4_expression_realization as common
from scripts import r4_history_validation as runner
from tests.r4_history_fixture import fixture,rubric
from persona_runtime.dialogue_voice import render,VERSION

class Contracts(unittest.TestCase):
    def test_two_roles_only_differ_in_six_expression_examples(self):
        data=fixture();a=deepcopy(data['characters']['detail']);b=deepcopy(data['characters']['stance'])
        self.assertEqual(len(a.pop('examples')),6);self.assertEqual(len(b.pop('examples')),6);self.assertEqual(a,b)
        common.PersonaSpec.from_dict(data['spec']);self.assertNotIn('history_groups',str(data))

    def test_all_cases_have_six_complete_history_pairs_and_new_inputs(self):
        data=fixture();self.assertEqual(len({x['id'] for x in data['cases']}),12)
        old=set()
        for n in range(1,6):
            old.update(m for x in common.read(common.OUT/f'real-batch-{n:03}/fixture.json')['cases'] for m in x['members'])
        for case in data['cases']:
            self.assertEqual(len(case['history']),6)
            self.assertTrue(all(h['text'] and h['reply'] for h in case['history']))
            self.assertFalse(set(case['members']) & old)

    def test_frozen_v4_v7_renderings_remain_in_actual_fifth_requests(self):
        old=common.read(common.OUT/'real-batch-005/prepared.json')
        for item in old['items']:
            request=common.read(common.OUT/'real-batch-005/planned'/(item['key']+'.json'))['request']
            self.assertIn(render(item['voice'])[0],request['messages'][0]['content'])
        self.assertEqual(VERSION,'natural-dialogue-v2');self.assertEqual(runner.ARMS,old['arms'])

    def test_gate_unchanged_and_history_groups_cover_primary_population(self):
        rules=rubric();self.assertEqual(rules['acceptance'],common.read(common.OUT/'real-batch-005/rubric.json')['acceptance'])
        groups=rules['history_groups'];self.assertEqual(set(groups['natural'])|set(groups['procedural_history']),set(rules['primary_cases']))
        self.assertFalse(set(groups['natural'])&set(groups['procedural_history']))
        self.assertEqual(len(groups['task_switch']),3)

    def test_latest_intent_and_reference_evidence_explicitly_present(self):
        cases={c['id']:c for c in fixture()['cases']}
        self.assertIn('整体没完成',cases['G08']['members'][-1]);self.assertIn('已经聊完',cases['G09']['members'][-1])
        self.assertIn('侧面还没缝',cases['G07']['members'][-1]);self.assertIn('老火车',cases['G07']['reference']['reply'])
        self.assertEqual([c['id'] for c in cases.values() if c['reference']],['G07'])
