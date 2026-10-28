"""Native session controls and draft/stale-result boundaries."""
import gc
import unittest
from unittest.mock import patch
from tests import test_desktop as base


class SessionDesktopTests(unittest.TestCase):
    setUp=base.DesktopTests.setUp
    window=base.DesktopTests.window
    def doCleanups(self):
        for w in self.windows:w.hub.pool.shutdown(wait=True,cancel_futures=True);w.hub.drain()
        result=unittest.TestCase.doCleanups(self);self.windows.clear();gc.collect();return result
    def test_page_and_concrete_controls(self):
        w=self.window();w.show_page('sessions');w.update()
        labels={b.cget('text') for b,_ in w.buttons}
        self.assertTrue({'封存会话','新建接续会话','只读回放','整理预览','提交本批整理','关联主题段'}<=labels)
    def test_reading_does_not_change_chat_target_and_late_response_is_ignored(self):
        w=self.window();w.persona_id='a';w.session_id='active';w.ss_id.set('history');pending=[]
        w.chat_input.insert('1.0','未提交草稿');w.ss_title.set('标题草稿')
        with patch.object(w,'run_task',side_effect=lambda n,r,a,success=None,**k:pending.append((success,k))):w.session_action('show')
        self.assertEqual(w.session_id,'active');w.persona_id='b';w.view_epoch+=1;w.invalidate_sessions()
        pending[0][0]({'revision':1,'secret':'SHOULD_NOT_APPEAR'})
        self.assertNotIn('SHOULD_NOT_APPEAR',w.ss_output.get('1.0','end'));self.assertIsNone(w.ss_revision)
        self.assertEqual(w.chat_input.get('1.0','end-1c'),'未提交草稿');self.assertEqual(w.ss_title.get(),'标题草稿')
    def test_failed_save_keeps_draft_and_operation(self):
        w=self.window();w.persona_id='a';w.ss_title.set('人工主题');calls=[];actions=[]
        with patch.object(w,'run_task',side_effect=lambda n,r,a,*x,**k:actions.append(a)),patch.object(w.client,'manage',side_effect=lambda a,**v:calls.append(v)):
            w.topic_action('thread','create');actions[-1](None);w.topic_action('thread','create');actions[-1](None)
        self.assertEqual(calls[0]['operation_id'],calls[1]['operation_id']);self.assertEqual(w.ss_title.get(),'人工主题')
    def test_target_change_cannot_apply_old_topic_result(self):
        w=self.window();w.persona_id='a';w.ss_id.set('s');w.ss_topic.set('t1');callbacks=[]
        with patch.object(w,'run_task',side_effect=lambda n,r,a,success=None,**k:callbacks.append(success)):w.topic_action('topic','show')
        w.ss_topic.set('t2');callbacks[0]({'id':'t1','revision':3})
        self.assertIsNone(w.ss_topic_row);self.assertNotIn('t1',w.ss_output.get('1.0','end'))

    def test_sealed_clock_and_shanghai_display(self):
        import time
        w=self.window();w.ss_clock_row={'key':w.session_key(),'received':time.monotonic(),'row':{'state':'sealed','idle_seconds':None}}
        w.update_session_clock();self.assertIn('sealed',w.ss_clock_label.cget('text'))
        shown=w.session_display({'sealed_at':'2026-10-28T10:00:00+00:00'})
        self.assertEqual(shown['sealed_at_shanghai'],'2026-10-28T18:00:00+08:00')

    def test_topic_source_navigation_keeps_draft_and_rechecks_archive(self):
        w=self.window();w.ss_turn_ids=['chosen-turn'];w.ss_members.insert('end','chosen-turn');w.ss_members.selection_set(0)
        w.chat_input.insert('1.0','保留草稿');w.chat_reference={'persona_id':'a','target':{'kind':'turn','id':'stale'}}
        with patch.object(w,'archive_read') as read:
            w.open_topic_source();read.assert_called_once_with('get')
        self.assertEqual(w.archive_id.get(),'chosen-turn');self.assertIsNone(w.chat_reference)
        self.assertEqual(w.chat_input.get('1.0','end-1c'),'保留草稿')
