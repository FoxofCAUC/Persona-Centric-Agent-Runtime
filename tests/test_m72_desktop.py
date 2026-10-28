"""Native cognitive context controls, drafts and stale response boundaries."""
import gc
import unittest
from unittest.mock import patch
from tests import test_desktop as desktop_tests


class ContextDesktopTests(unittest.TestCase):
    setUp=desktop_tests.DesktopTests.setUp
    window=desktop_tests.DesktopTests.window

    def doCleanups(self):
        for window in self.windows:
            window.hub.pool.shutdown(wait=True,cancel_futures=True);window.hub.drain()
        result=unittest.TestCase.doCleanups(self)
        self.windows.clear();gc.collect();return result

    def test_controls_are_packed_and_future_builders_are_available(self):
        w=self.window();w.show_page('context');w.update()
        labels={'读取入口政策','保存开关','载入最近请求','预览任务上下文','查看调用清单历史'}
        buttons=[button for button,_ in w.buttons if button.cget('text') in labels]
        self.assertEqual({b.cget('text') for b in buttons},labels)
        self.assertTrue(all(b.master.winfo_manager()=='pack' for b in buttons))
        self.assertTrue(w.ctx_enabled.get());self.assertFalse(w.ctx_cross.get())

    def test_late_context_result_cannot_overwrite_different_persona_or_draft(self):
        w=self.window();w.persona_id='a';w.ctx_turn.set('turn-a');pending=[]
        def run(name,resources,action,success=None,**kwargs):pending.append((success,kwargs))
        with patch.object(w,'run_task',side_effect=run):w.context_request('preview')
        self.assertTrue(pending[0][1]['error_current']())
        w.persona_id='b';w.view_epoch+=1;w.invalidate_context()
        w.ctx_turn.set('turn-b');pending[0][0]({'status':'ok','wrong-persona':'SHOULD_NOT_APPEAR'})
        self.assertNotIn('SHOULD_NOT_APPEAR',w.ctx_output.get('1.0','end'));self.assertEqual(w.ctx_turn.get(),'turn-b')
        self.assertFalse(pending[0][1]['error_current']())

    def test_policy_failure_preserves_toggle_and_reuses_operation(self):
        w=self.window();w.persona_id='a';w.ctx_policy={'id':'p','persona_id':'a','channel':'desktop','mode':'legacy_per_message'}
        w.ctx_enabled.set(False);w.ctx_cross.set(True);pending=[];calls=[]
        def run(name,resources,action,success=None,**kwargs):pending.append(action)
        def manage(action,**values):calls.append(values);raise OSError('lost response')
        with patch.object(w,'run_task',side_effect=run),patch.object(w.client,'manage',side_effect=manage):
            for _ in range(2):
                w.save_context_policy()
                with self.assertRaises(OSError):pending[-1](None)
        self.assertEqual(calls[0],calls[1]);self.assertFalse(w.ctx_enabled.get());self.assertTrue(w.ctx_cross.get())

    def test_scope_or_target_change_requires_new_policy_and_preserves_target_draft(self):
        w=self.window();w.persona_id='a';w.ctx_policy={'id':'old'};w.ctx_turn.set('current-draft')
        self.assertIsNone(w.ctx_policy);w.ctx_peer.set('another-peer')
        self.assertEqual(w.ctx_turn.get(),'current-draft')
        with patch.object(w,'run_task') as run:w.save_context_policy();run.assert_not_called()
