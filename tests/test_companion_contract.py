"""Artificial protocol/profile checks; no model or production data access."""
from copy import deepcopy
import unittest

from persona_runtime.archive import fingerprint
from persona_runtime.companion import choose, checked, read, freeze_model
from persona_runtime.companion_contract import VERSION, REMOVALS, structural_contract
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.reply_batch import bubble_prompt, expression_snapshot


class Contract(unittest.TestCase):
    def test_full_protocol_frozen_and_hard_contract_retained(self):
        full=bubble_prompt()
        self.assertEqual(fingerprint(full),'c4fca2406f6dd4e30a3475d18a37e75c42c7cf87ca2840ea0872f835268105a7')
        reduced=structural_contract()
        self.assertEqual(reduced,structural_contract(full))
        self.assertEqual(len(REMOVALS),7)
        self.assertEqual(len(full)-len(reduced),sum(len(text) for _,text in REMOVALS))
        for _,text in REMOVALS:self.assertNotIn(text,reduced)
        for text in ('"schema_version":3','messages为1至','总正文最多','behavior_intent仅',
                     '技术、代码、网址、数值与引用保持完整','所有必需答案必须cancel_if_new_input=false',
                     '正确转义正文换行、双引号及反斜杠'):
            self.assertIn(text,reduced)

    def test_only_exact_protocol_is_eligible_not_author_text(self):
        for text in ('',bubble_prompt()+'人物资料',bubble_prompt().replace('JSON','json',1)):
            with self.assertRaisesRegex(RuntimeErrorBase,'contract_language_clause_boundary_changed'):
                structural_contract(text)


class Profiles(unittest.TestCase):
    def turn(self):
        context={'companion':choose('minimal_structural'),'character_retrieval':{'mode':'hybrid'},
                 'disposition_mode':'baseline','situation_observation_mode':'disabled'}
        return {'model_snapshot':freeze_model({'name':'fake'},context),'context_policy_snapshot':context,
                'reply_mode_snapshot':'natural_structured','expression_snapshot':expression_snapshot('natural_structured')}

    def test_old_shapes_and_new_opt_in_are_separate(self):
        for mode in ('current','minimal'):
            self.assertEqual(choose(mode),{'version':'companion-runtime-v1','mode':mode})
        self.assertEqual(choose('minimal_structural'),
            {'version':'companion-runtime-v2','mode':'minimal','format_contract':VERSION})
        original=self.turn();copy=deepcopy(original)
        self.assertEqual(read(original),choose('minimal_structural'));self.assertEqual(original,copy)
        for patch in ({'version':'companion-runtime-v3'},{'mode':'current'},
                      {'format_contract':'unknown'},{'extra':True}):
            with self.assertRaisesRegex(RuntimeErrorBase,'companion_profile_invalid'):
                checked(choose('minimal_structural')|patch)

    def test_binding_and_format_fail_before_dispatch(self):
        for mutation in ('binding','mode','expression','voice'):
            t=self.turn()
            if mutation=='binding':t['context_policy_snapshot']['companion']=choose('minimal')
            elif mutation=='mode':t['reply_mode_snapshot']='context_structured'
            elif mutation=='expression':t['expression_snapshot']=expression_snapshot('natural_structured',version=2)
            else:t['model_snapshot']['_dialogue_profile']={'version':'natural-dialogue-v2'}
            with self.assertRaisesRegex(RuntimeErrorBase,'companion_snapshot_mismatch|companion_format_conflict'):
                read(t)
        with self.assertRaisesRegex(RuntimeErrorBase,'companion_format_conflict'):
            freeze_model({'_dialogue_profile':{'version':'natural-dialogue-v2'}},self.turn()['context_policy_snapshot'])
