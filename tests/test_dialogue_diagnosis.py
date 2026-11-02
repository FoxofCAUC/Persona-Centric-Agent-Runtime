"""CR1 material checks. These do not score simulated or real expression."""
from copy import deepcopy
from dataclasses import replace
import unittest

from persona_runtime.archive import encoded
from persona_runtime.dialogue_frame import Statement
from scripts import dialogue_diagnosis as cr1
from tests.dialogue_diagnosis_fixture import fixture


class Materials(unittest.TestCase):
    def setUp(self):
        self.data = fixture()

    def test_complete_materials_and_eight_categories(self):
        rows = cr1.validate_materials(self.data)
        self.assertEqual(len(rows), 48)
        self.assertEqual(len({row['material_hash'] for row in rows}), 48)
        self.assertEqual(len({row['id'] for row in rows}), 48)
        self.assertEqual(cr1.check_materials(self.data)['scene_count'], 24)

    def test_selected_twelve_cover_all_categories_and_fixed_workload(self):
        subset = self.data['real_subset']
        self.assertEqual(len(subset), 12)
        self.assertEqual(set(c['category'] for c in self.data['cases'] if c['id'] in subset), set(self.data['categories']))
        self.assertEqual(len(subset) * len(self.data['roles']), 24)
        self.assertEqual(len(subset) * len(self.data['roles']) * len(cr1.CONDITIONS) * 2, 144)

    def test_identity_knowledge_and_base_prompt_are_equal(self):
        roles = list(self.data['roles'].values())
        for key in cr1.COMMON_FIELDS:
            self.assertEqual(roles[0][key], roles[1][key])
        self.assertNotEqual(roles[0]['disposition'], roles[1]['disposition'])
        self.assertIsNot(roles[0]['facts'], roles[1]['facts'])

    def test_evaluator_reason_never_enters_generation_material(self):
        for case in self.data['cases']:
            for role_id in self.data['roles']:
                before = cr1.generation_materials(self.data, case['id'], role_id)
                changed = deepcopy(self.data)
                changed['evaluation'][case['id']] = {'secret_canary': 'EVALUATION_ONLY_DO_NOT_SEND'}
                after = cr1.generation_materials(changed, case['id'], role_id)
                self.assertEqual(before, after)
                self.assertNotIn('EVALUATION_ONLY_DO_NOT_SEND', encoded(after))
                self.assertNotIn('evaluation', after)

    def test_changed_source_invalidates_both_derived_objects(self):
        case = self.data['cases'][21]
        evidence, aliases = cr1.material_evidence(case, 'weigh', self.data['roles']['weigh'])
        frame, spec, _ = cr1.bind_annotations(case, 'weigh', evidence, aliases)
        changed = replace(evidence, current=evidence.current[:-1] + (replace(evidence.current[-1], text='只问能否晚到'),))
        with self.assertRaisesRegex(ValueError, 'dialogue_frame_stale'):
            frame.validate(changed)
        with self.assertRaisesRegex(ValueError, 'dialogue_frame_stale'):
            spec.validate(changed, frame)

    def test_cross_role_source_is_rejected(self):
        case = self.data['cases'][0]
        ev_a, aliases_a = cr1.material_evidence(case, 'weigh', self.data['roles']['weigh'])
        ev_b, aliases_b = cr1.material_evidence(case, 'lean', self.data['roles']['lean'])
        frame, _, _ = cr1.bind_annotations(case, 'weigh', ev_a, aliases_a)
        changed = replace(frame, evidence_hash=ev_b.digest,
                          focus=(Statement('跨角色错误绑定', (aliases_a['C0'],)),))
        self.assertNotEqual(aliases_a['C0'], aliases_b['C0'])
        with self.assertRaisesRegex(ValueError, 'dialogue_source_out_of_scope'):
            changed.validate(ev_b)

    def test_missing_alias_is_not_silently_removed(self):
        broken = deepcopy(self.data)
        broken['cases'][0]['frame']['focus'][0]['sources'] = ['H99U']
        with self.assertRaisesRegex(ValueError, 'cr1_annotation_alias_missing'):
            cr1.validate_materials(broken)

    def test_reference_has_original_text_and_separate_binding(self):
        case = self.data['cases'][22]
        evidence, aliases = cr1.material_evidence(case, 'weigh', self.data['roles']['weigh'])
        frame, spec, audit = cr1.bind_annotations(case, 'weigh', evidence, aliases)
        self.assertEqual(evidence.references[0].text, case['history'][0]['user'])
        self.assertNotEqual(aliases['REF'], aliases['H0U'])
        self.assertIn(aliases['REF'], spec.obligations[0].source_ids)
        self.assertTrue(any(item['source_id'] == aliases['REF'] for row in audit for item in row['evidence']))

    def test_every_explicit_annotation_has_inspectable_excerpts(self):
        for record in cr1.validate_materials(self.data):
            for row in record['annotation_audit']:
                if row['certainty'] == 'explicit':
                    self.assertTrue(row['evidence'])
                    self.assertTrue(all(item['text'] and item['source_hash'] for item in row['evidence']))

    def test_unknown_remains_unknown_and_optional_imagination_unbound(self):
        case = self.data['cases'][8]
        evidence, aliases = cr1.material_evidence(case, 'weigh', self.data['roles']['weigh'])
        frame, spec, _ = cr1.bind_annotations(case, 'weigh', evidence, aliases)
        self.assertEqual(frame.uncertainty[0].certainty, 'unknown')
        self.assertEqual(frame.uncertainty[0].source_ids, ())
        replace(spec, optional_moves=('想象朋友纠结时的小表情也可以。',)).validate(evidence, frame)

    def test_category_controls_and_role_identity_change_fail(self):
        changed = deepcopy(self.data)
        changed['roles']['lean']['facts'].append('额外职业知识')
        with self.assertRaisesRegex(ValueError, 'cr1_common_role_content'):
            cr1.validate_materials(changed)
        changed = deepcopy(self.data)
        changed['evaluation']['D03']['control'] = 'failure_opportunity'
        with self.assertRaisesRegex(ValueError, 'cr1_category_controls'):
            cr1.validate_materials(changed)

    def test_unexpected_annotations_cannot_leak_through_role_or_spec(self):
        changed = deepcopy(self.data)
        changed['roles']['weigh']['evaluation_reason'] = '不要把评分理由发给模型'
        with self.assertRaisesRegex(ValueError, 'cr1_role_schema'):
            cr1.validate_materials(changed)
        changed = deepcopy(self.data)
        changed['cases'][0]['spec']['roles']['weigh']['evaluation_reason'] = '不得进入规格'
        with self.assertRaisesRegex(ValueError, 'cr1_spec_role_schema'):
            cr1.validate_materials(changed)

    def test_preparation_does_not_claim_runtime_or_effect_completion(self):
        result = cr1.check_materials(self.data)
        self.assertEqual(result['runtime_admission'], 'not_performed')
        self.assertEqual(result['candidate_recall'], 'not_performed')
        self.assertEqual(result['main_request_simulation'], 'not_performed')
        self.assertEqual(result['true_expression_effect'], 'unverified')
        self.assertEqual(result['numeric_protocol'], 'pending_user_choice')
        self.assertEqual(result['real_calls'], 0)


if __name__ == '__main__':
    unittest.main()
