from copy import deepcopy
import unittest
from scripts import r4_expression_realization as common
from scripts import r4_default_candidate as runner
from tests.r4_default_candidate_fixture import fixture,rubric,MATERIAL_FORMS
from persona_runtime.dialogue_voice import VERSION,render

class Contracts(unittest.TestCase):
    def test_roles_have_identical_facts_and_six_distinct_expression_texts(self):
        d=fixture();a=deepcopy(d['characters']['detail']);b=deepcopy(d['characters']['stance'])
        ae=a.pop('examples');be=b.pop('examples');self.assertEqual(a,b);self.assertEqual(len(ae),6)
        for x,y in zip(ae,be):
            self.assertNotEqual(x['text'],y['text']);x.pop('text');y.pop('text');self.assertEqual(x,y)
        common.PersonaSpec.from_dict(d['spec'])
    def test_fresh_scenarios_and_sample_texts_do_not_replay_six_old_cohorts(self):
        d=fixture();old_inputs=set();old_text=set()
        for n in range(1,7):
            old=common.read(common.OUT/f'real-batch-{n:03}/fixture.json')
            old_inputs.update(m for c in old['cases'] for m in c['members'])
            old_text.update(e['text'] for char in old['characters'].values() for e in char['examples'])
        self.assertEqual(len({c['id'] for c in d['cases']}),12)
        self.assertFalse({m for c in d['cases'] for m in c['members']}&old_inputs)
        self.assertFalse({e['text'] for char in d['characters'].values() for e in char['examples']}&old_text)
    def test_mixed_material_form_labels_are_report_only(self):
        self.assertEqual(list(MATERIAL_FORMS.values()).count('spoken_example'),3)
        self.assertEqual(list(MATERIAL_FORMS.values()).count('habit_description'),3)
        self.assertNotIn('material_forms',str(fixture()));self.assertEqual(rubric()['material_forms'],MATERIAL_FORMS)
    def test_default_and_candidate_are_existing_frozen_versions(self):
        self.assertEqual(VERSION,'natural-dialogue-v2');self.assertEqual(runner.ARMS,{'control':VERSION,'experiment':'natural-dialogue-v4'})
        for n,arm in ((2,'control'),(6,'control')):
            p=common.read(common.OUT/f'real-batch-{n:03}/prepared.json')
            for i in p['items']:
                if i['arm']==arm:
                    request=common.read(common.OUT/f'real-batch-{n:03}/planned'/(i['key']+'.json'))['request']
                    self.assertIn(render(i['voice'])[0],request['messages'][0]['content'])
    def test_quality_gate_unchanged_and_current_intent_explicit(self):
        v=rubric();self.assertEqual(v['acceptance'],common.read(common.OUT/'real-batch-006/rubric.json')['acceptance'])
        self.assertEqual(v['primary_denominator_per_arm'],18)
        cases={c['id']:c for c in fixture()['cases']}
        self.assertIn('才撕到三月',cases['H07']['members'][-1]);self.assertIn('我也说好了参加',cases['H08']['members'][-1])
        self.assertTrue(cases['H09']['reference']);self.assertEqual([k for k,c in cases.items() if c['reference']],['H09'])
    def test_history_is_controlled_without_claiming_six_turn_input(self):
        d=fixture();self.assertEqual([c['id'] for c in d['cases'] if c.get('history')],['H04','H05','H06','H08'])
        for c in d['cases']:
            if c.get('history'):self.assertEqual(len(c['history']),2)
