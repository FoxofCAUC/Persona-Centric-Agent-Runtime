import copy,json,unittest
from unittest.mock import patch
from persona_runtime.knowledge_processing_context import attach
from tests import test_desktop as desktop

class AcquisitionTests(unittest.TestCase):
    def test_new_read_and_cached_source_have_distinct_program_supplied_status(self):
        result={'state':'ready','processing':{'record_id':'checked'},'translation':[{'text':'月相来自观察几何变化。','kind':'source_claim','evidence_indexes':[0]}],
            'evidence':[{'article_title':'月相','section_title':'导语','url':'https://zh.wikipedia.org/wiki/phase','source_snapshot_at':'2025-01-01T00:00:00Z','fetched_epoch':1767225602}]}
        turn={'created_at':'2026-01-01T00:00:00+00:00','model_snapshot':json.dumps({'max_input_chars':24000})}
        messages=[{'role':'system','content':'设定'},{'role':'user','content':'请现在查证'}]
        fresh,_=attach(messages,{},result,turn)
        self.assertIn('本次请求已完成在线读取，读取时间：2026-01-01',fresh[0]['content'])
        self.assertIn('来源快照：2025-01-01',fresh[0]['content'])
        turn['created_at']='2026-01-02T00:00:00+00:00'
        cached,_=attach(messages,{},result,turn)
        self.assertIn('本轮使用已有资料',cached[0]['content'])
        self.assertNotIn('本次请求已完成在线读取，读取时间：',cached[0]['content'])

class QueueTests(unittest.TestCase):
    setUp=desktop.DesktopTests.setUp
    window=desktop.DesktopTests.window
    widgets=staticmethod(desktop.DesktopTests.widgets)
    pump=desktop.DesktopTests.pump
    dialog=desktop.DesktopTests.dialog
    def doCleanups(self):
        import gc
        for window in self.windows:
            window.hub.pool.shutdown(wait=True,cancel_futures=True);window.hub.drain()
        result=super().doCleanups();self.windows.clear();gc.collect();return result
    def test_queue_refreshes_status_preserves_selection_and_stops_after_close(self):
        window=self.window();window.persona_id='one';window.view_epoch=1
        queued={'id':'test-row','status':'queued','phase':'knowledge_translating','channel':'desktop'}
        rows=[queued];calls=[]
        def run(name,resources,action,success=None,**kwargs):
            if name in ('读取请求队列','刷新请求队列'):
                calls.append(name);success(copy.deepcopy(rows))
        with patch.object(window,'run_task',side_effect=run):
            window.request_queue();dialog=self.dialog(window,'请求队列与详情')
            listing=next(w for w in self.widgets(dialog) if w.winfo_class()=='Listbox');listing.selection_set(0)
            rows[0]=queued|{'status':'completed','phase':'completed'}
            self.pump(window,lambda:'已完成' in listing.get(0))
            self.assertEqual(listing.curselection(),(0,));self.assertEqual(len(calls),2)
            dialog.destroy();window.update();self.assertEqual(len(calls),2)
