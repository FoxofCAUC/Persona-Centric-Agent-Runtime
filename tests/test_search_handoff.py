"""Check post-search assembly order, provenance and old/cached turn compatibility."""
import json
import unittest
from persona_runtime.archive import fingerprint
from persona_runtime.knowledge_general import attach
from persona_runtime.knowledge_handoff import VERSION
from persona_runtime.prompt_assembly import PromptBlock,assemble


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.turn={'id':'11','channel':'desktop','model_snapshot':json.dumps({'max_input_chars':20000})}
        self.messages,assembly=assemble([PromptBlock('scene_voice','901','1','1'),PromptBlock('search_notice','902','2','2'),
            PromptBlock('history_recorded_at','903','3','3')],[{'role':'assistant','content':'904'}],{'role':'user','content':'905'})
        self.details={'scene_voice':{'version':'1'},'prompt_assembly':assembly}
        self.result={'record_id':'11','organized':{'notes':'910','task_id':'12','source_ids':['13']},
            'retrieval':{'executor':'deepseek-native-web-v1','search_outcome':'completed','material_acquired':True,'task_ids':['12']},
            'sources':[{'id':'13','title':'911','url':'https://example.invalid','kind':'native_search_summary','summary':'913'*1000}]}

    def test_verified_spans_and_budget_after_compacting_sources(self):
        messages,details=attach(self.messages,self.details,self.result,self.turn)
        layout=details['prompt_assembly']
        self.assertEqual(layout['input_snapshot_hash'],fingerprint(messages))
        self.assertEqual(messages[1:],self.messages[1:])
        self.assertTrue(messages[0]['content'].endswith('903'))
        self.assertEqual(layout['input_characters'],sum(len(m['content']) for m in messages))
        self.assertLess(layout['input_characters'],1100)
        self.assertEqual([b['name'] for b in layout['blocks']],['scene_voice','retrieved_material','history_recorded_at','history','current_input'])
        for b in layout['blocks']:
            start=b['start_character']
            piece=messages[b['message_index']]['content'][start:start+b['characters']]
            self.assertEqual(b['content_hash'],fingerprint(piece))
        self.assertEqual(details['knowledge']['translation']['handoff_version'],VERSION)
        self.assertIn('902',self.messages[0]['content'])

    def test_other_turns_unconfirmed_empty_and_stale_material_keep_original_contract(self):
        for change in ({'record_id':'14'},{'organized':{}},{'organized':dict(self.result['organized'],task_id='15')},
                       {'retrieval':dict(self.result['retrieval'],search_outcome='unconfirmed')},
                       {'retrieval':dict(self.result['retrieval'],material_acquired=False)}):
            with self.subTest(change=change):
                messages,details=attach(self.messages,self.details,self.result|change,self.turn)
                self.assertIn('902',messages[0]['content'])
                self.assertNotIn('handoff_version',details['knowledge']['translation'])
