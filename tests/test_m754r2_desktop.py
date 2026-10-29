import unittest
from unittest.mock import patch
from tests import test_m72_desktop as base

class SelectorDesktopTests(unittest.TestCase):
    setUp=base.ContextDesktopTests.setUp
    window=base.ContextDesktopTests.window
    doCleanups=base.ContextDesktopTests.doCleanups

    def test_controls_and_save_failure_preserves_draft(self):
        w=self.window();w.show_page('model');w.update();w.selector_vars['name'].set('unsaved')
        labels={b.cget('text') for b,_ in w.buttons}
        self.assertTrue({'复制主模型配置','保存选择配置','测试选择模型（计费一次）','最近选择调用'}<=labels)
        calls=[]
        with patch.object(w,'run_task',side_effect=lambda n,r,a,done=None,**kw:calls.append((a,done,kw))),patch.object(w.config,'update',side_effect=OSError('disk')):
            w.save_selector()
            with self.assertRaises(OSError):calls[-1][0](None)
            calls[-1][2]['failure'](OSError('disk'))
        self.assertEqual(w.selector_vars['name'].get(),'unsaved')

    def test_late_test_and_records_cannot_cross_persona(self):
        w=self.window();w.persona_id='a';callbacks=[]
        with patch.object(w,'run_task',side_effect=lambda n,r,a,done=None,**kw:callbacks.append(done)),patch.object(w,'show_json') as show:
            w.test_selector();w.selector_records();w.persona_id='b';w.view_epoch+=1
            for callback in callbacks:callback({'data':'old'})
            show.assert_not_called()

    def test_three_mode_selection_preserved(self):
        w=self.window();w.persona_id='a';w.ctx_policy={'id':'p','persona_id':'a','channel':'desktop','mode':'legacy_per_message'}
        w.ctx_character.set('本地召回＋云端选择');calls=[]
        with patch.object(w,'run_task',side_effect=lambda n,r,a,done=None,**kw:calls.append(a)),patch.object(w.client,'manage',return_value={}) as manage:
            w.save_context_policy();calls[-1](None)
        self.assertEqual(manage.call_args.kwargs['character_retrieval_mode'],'hybrid_llm')
