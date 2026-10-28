"""Native Tk search draft, scope and response-generation protection."""
import unittest
import gc
from unittest.mock import patch
from tests import test_desktop as desktop_tests


class SearchDesktopTests(unittest.TestCase):
    setUp=desktop_tests.DesktopTests.setUp
    window=desktop_tests.DesktopTests.window
    def doCleanups(self):
        for window in self.windows:
            window.hub.pool.shutdown(wait=True,cancel_futures=True)
            window.hub.drain()
        result=unittest.TestCase.doCleanups(self)
        self.windows.clear()
        if 'window' in locals():del window
        gc.collect()
        return result

    def test_search_action_bar_is_part_of_page_layout(self):
        w=self.window()
        actions={'查询','缓存状态','重建派生缓存','工作集预览','查看完整回合'}
        buttons=[button for button,_ in w.buttons
                 if button.cget('text') in actions and button.master.master is w.search_tree.master]
        self.assertEqual({button.cget('text') for button in buttons},actions)
        self.assertTrue(all(button.master.winfo_manager()=='pack' for button in buttons))

    def test_query_failure_and_stale_response_preserve_draft(self):
        w=self.window();w.persona_id='persona-a';w.view_epoch=1
        w.search_vars['query'].set('未提交检索草稿');calls=[]
        def run(name,resources,action,success=None,**kwargs):calls.append((action,success,kwargs))
        with patch.object(w,'run_task',side_effect=run):w.archive_search_request('search')
        self.assertTrue(calls[0][2]['error_current']())
        w.search_vars['query'].set('新的草稿')
        self.assertFalse(calls[0][2]['error_current']())
        calls[0][1]({'status':'ok','groups':[{'turn_id':'wrong'}]})
        self.assertFalse(w.search_rows);self.assertEqual(w.search_vars['query'].get(),'新的草稿')

    def test_scope_change_clears_results_but_not_query_or_chat_draft(self):
        w=self.window();w.persona_id='persona-a';w.search_vars['query'].set('青禾书店')
        w.search_rows=[{'turn_id':'old'}];w.search_display_scope=w.search_partition_args()
        w.archive_peer.set('different-peer')
        self.assertFalse(w.search_rows);self.assertEqual(w.search_vars['query'].get(),'青禾书店')
        w.archive_channel.set('qq');w.search_target.set('event:source')
        w.search_display_scope=w.search_partition_args()
        with patch.object(w,'choose_archive_reference') as choose:
            w.choose_search_reference();choose.assert_not_called()

    def test_result_selection_change_invalidates_pending_details(self):
        w=self.window();w.persona_id='persona-a';w.search_vars['query'].set('书店')
        w.search_rows=[{'turn_id':'a','matched_targets':[{'kind':'event','id':'ea'}]},
                       {'turn_id':'b','matched_targets':[{'kind':'event','id':'eb'}]}]
        w.search_display_scope=w.search_partition_args()
        for i in range(2):w.search_tree.insert('','end',iid=str(i),values=(str(i),1,''))
        w.search_tree.selection_set('0');w.pick_search_target();calls=[]
        def run(name,resources,action,success=None,**kwargs):calls.append(kwargs)
        with patch.object(w,'run_task',side_effect=run):w.archive_search_request('get')
        self.assertTrue(calls[0]['error_current']())
        w.search_tree.selection_set('1');w.pick_search_target()
        self.assertFalse(calls[0]['error_current']())

    def test_rebuild_transport_failure_reuses_operation_payload(self):
        from persona_runtime.desktop_client import TransportUnknown
        w=self.window();w.persona_id='persona-a';requests=[];pending=[]
        def manage(action,**args):
            if action=='archive.search-status':return {'status':'ok','cache_instance_id':'cache','generation':2}
            requests.append(dict(args));raise TransportUnknown('simulated lost result')
        def run(name,resources,action,success=None,**kwargs):pending.append(action)
        with patch.object(w,'run_task',side_effect=run),patch.object(w.client,'manage',side_effect=manage):
            for _ in range(2):
                w.archive_search_request('search-rebuild')
                with self.assertRaises(TransportUnknown):pending[-1](None)
        self.assertEqual(requests[0],requests[1])
