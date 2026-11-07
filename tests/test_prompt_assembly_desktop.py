"""Read-only context presentation using synthetic manifests and isolated Tk windows."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from tests import test_m72_desktop as fixture


def manifest():
    return {'context_id':'artificial-call','persisted':True,'prompt_assembly':{
        'assembler_version':'prompt-assembly-r0-v1','input_characters':12,'input_snapshot_hash':'fixed-hash',
        'requested':{'dialogue_voice':'natural-dialogue-v2'},
        'effective':{'expression':'natural-dialogue-v2','format':'segments-schema1-v1',
                     'wire':{'expression_version':1},'capsule':{'requested_mode':'shadow','applied':False,'reason':'shadow_observation_only'}},
        'blocks':[{'name':'persona_expression','owner':'context_m4','version':'natural-dialogue-v2',
                   'present':True,'characters':12,'content_hash':'block-hash'}],
        'truncation_info':[]}}


class AssemblyDesktop(unittest.TestCase):
    setUp=fixture.ContextDesktopTests.setUp
    window=fixture.ContextDesktopTests.window
    doCleanups=fixture.ContextDesktopTests.doCleanups

    def test_persona_purpose_history_summary_does_not_invent_old_fields(self):
        w=self.window();value=manifest()
        value['prompt_assembly']['persona_purpose']={'version':'companion-persona-v2','characters':180,
            'sections':[{'id':'identity'}], 'fields':[{'field_id':'identity.gender'}]}
        w.show_context_result('inspect',{'manifests':[value]})
        self.assertIn('companion-persona-v2',w.ctx_summary.get('1.0','end'))
        self.assertIn('S8尚需结合语境审阅',w.ctx_summary.get('1.0','end'))
        w.show_context_result('inspect',{'manifests':[manifest()]})
        self.assertIn('未采集人物资料用途信息',w.ctx_summary.get('1.0','end'))

    def test_preview_is_labeled_readonly_with_component_versions(self):
        w=self.window();w.show_page('context');w.show_context_result('preview',{'manifest':manifest()});w.update()
        text=w.ctx_summary.get('1.0','end')
        self.assertIn('按当前可用来源重建，未发起模型调用',text)
        self.assertIn('natural-dialogue-v2',text);self.assertIn('segments-schema1-v1',text)
        self.assertIn('未应用',text);self.assertEqual(str(w.ctx_summary.cget('state')),'disabled')
        self.assertEqual(len(w.ctx_blocks.get_children()),1)
        self.assertTrue(w.ctx_blocks.winfo_ismapped())
        self.assertLessEqual(w.ctx_blocks.winfo_rooty()+w.ctx_blocks.winfo_height(),w.winfo_rooty()+w.winfo_height())

    def test_history_selection_old_record_and_omission(self):
        w=self.window();old={'context_id':'old-call','persisted':True}
        omitted=deepcopy(manifest());omitted['context_id']='omitted';omitted['prompt_assembly'].pop('blocks');omitted['prompt_assembly']['details_omitted']=True
        value={'manifests':[manifest(),old,omitted]};before=deepcopy(value)
        w.show_context_result('inspect',value)
        self.assertIn('仅展示该次保存',w.ctx_summary.get('1.0','end'))
        w.ctx_record_picker.current(1);w.show_context_record()
        self.assertIn('该历史记录未采集分块信息',w.ctx_summary.get('1.0','end'))
        self.assertFalse(w.ctx_blocks.get_children())
        w.ctx_record_picker.current(2);w.show_context_record()
        self.assertIn('明细因诊断大小上限被省略',w.ctx_summary.get('1.0','end'))
        self.assertEqual(value,before)

    def test_late_preview_does_not_replace_new_target_or_new_history_request(self):
        w=self.window();w.persona_id='a';w.ctx_turn.set('turn-a');pending=[]
        with patch.object(w,'run_task',side_effect=lambda *a,**kw:pending.append((a[3],kw))):
            w.context_request('preview');w.context_request('inspect')
        pending[1][0]({'manifests':[manifest()]})
        pending[0][0]({'manifest':{'context_id':'late'}})
        self.assertIn('历史',w.ctx_summary.get('1.0','end'));self.assertNotIn('late',w.ctx_output.get('1.0','end'))
        self.assertFalse(pending[0][1]['error_current']())
        w.ctx_turn.set('turn-b');pending[1][0]({'manifests':[manifest()]})
        self.assertFalse(w.ctx_blocks.get_children());self.assertEqual(w.ctx_records,[])
        self.assertNotIn('artificial-call',w.ctx_output.get('1.0','end'))

    def test_empty_history_and_unsupported_preview_never_invent_details(self):
        w=self.window();w.show_context_result('inspect',{'manifests':[]})
        self.assertIn('未找到已保存',w.ctx_summary.get('1.0','end'))
        w.show_context_result('preview',{'manifest':{'task_type':'reflection'}})
        self.assertIn('此预览未提供分块信息',w.ctx_summary.get('1.0','end'))
        self.assertFalse(w.ctx_blocks.get_children())

    def test_selector_returned_vs_included_and_fallback_not_v3_applied(self):
        w=self.window();value=manifest();value['prompt_assembly']['material_selection']={
            'requested_prompt':'character-selector-v3','effective_mode':'lexical','applied_prompt':None,
            'returned_example_count':2,'included_example_count':0,'fallback_reason':'character_preparation_invalidated'}
        w.show_context_result('inspect',{'manifests':[value]})
        text=w.ctx_summary.get('1.0','end')
        self.assertIn('请求 character-selector-v3',text);self.assertIn('实际 lexical',text)
        self.assertIn('生效版本 未应用云端结果',text);self.assertIn('选择器返回 2；实际入模 0',text)
        w.show_context_result('inspect',{'manifests':[manifest()]})
        self.assertIn('材料选择版本及返回明细未采集',w.ctx_summary.get('1.0','end'))
