import tempfile
import unittest
from pathlib import Path
from persona_runtime.evaluation import EvaluationStore
from persona_runtime.paths import RuntimePaths


class Ledger(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=EvaluationStore(RuntimePaths(Path(self.temp.name)))
        self.cases=[{'id':f'contract-{i}','scenario':f'模拟工具场景{i}','author_summary':'人工测试作者资料'} for i in range(100)]
        self.store.create('synthetic',self.cases,{'fixture':'simulation-only'},simulated=True)

    def complete(self,case,arm):
        self.store.transition('synthetic',case,arm,'not_called','accepted',receipt_id='receipt-'+case+arm)
        self.store.transition('synthetic',case,arm,'accepted','running')
        self.store.transition('synthetic',case,arm,'running','success',attempt_id='attempt-'+case+arm,output='同一段模拟正文')

    def review(self):
        return {'left_task':'pass','right_task':'pass','left_action':'respond','right_action':'respond','natural':'tie','reason':'仅自动验证审阅合同，非真实用户偏好'}

    def test_300_slots_100_reviews_freeze_and_blind_contract(self):
        self.assertEqual(self.store.status('synthetic')['slots'],{'not_called':300})
        for c in self.cases:
            for arm in 'ABC':self.complete(c['id'],arm)
        for batch in range(1,5):
            rows=self.store.load('synthetic',batch)['cases'];self.assertEqual(len(rows),25)
            for row in rows:
                self.assertEqual(set(row),{'case_id','scenario','author_summary','left','right','revision','review'})
                self.assertIsNone(row['review'])
                self.store.save('synthetic',row['case_id'],self.review(),0,row['case_id'])
        result=self.store.export('synthetic');self.assertEqual(len(result['reviews']),100)
        self.assertTrue(self.store.status('synthetic')['frozen'])
        with self.assertRaisesRegex(Exception,'review_frozen'):
            self.store.save('synthetic',self.cases[0]['id'],self.review(),1,'new')

    def test_recovery_unknown_and_no_success_replay(self):
        case=self.cases[0]['id'];self.complete(case,'A')
        with self.assertRaisesRegex(Exception,'slot_conflict'):self.store.transition('synthetic',case,'A','not_called','accepted')
        self.store.transition('synthetic',case,'B','not_called','accepted')
        self.store.transition('synthetic',case,'B','accepted','running')
        rows=self.store.recover('synthetic');self.assertEqual(rows[0]['state'],'fee_unknown')
        with self.assertRaisesRegex(Exception,'invalid_transition'):self.store.transition('synthetic',case,'B','fee_unknown','running')

    def test_operation_idempotency_conflict_and_incomplete_export(self):
        case=self.cases[0]['id']
        for arm in 'BC':self.complete(case,arm)
        result=self.store.save('synthetic',case,self.review(),0,'same')
        self.assertEqual(result,self.store.save('synthetic',case,self.review(),0,'same'))
        with self.assertRaisesRegex(Exception,'operation_conflict'):self.store.save('synthetic',case,self.review()|{'natural':'left'},0,'same')
        with self.assertRaisesRegex(Exception,'revision_conflict'):self.store.save('synthetic',case,self.review(),0,'other')
        with self.assertRaisesRegex(Exception,'review_incomplete'):self.store.export('synthetic')

    def test_path_and_empty_answers_rejected(self):
        for value in ('../secrets','C:\\secrets','a/b','',None):
            with self.assertRaisesRegex(Exception,'invalid_id'):self.store.status(value)
        with self.assertRaisesRegex(Exception,'review_contract'):self.store.save('synthetic','contract-0',{},0,'op')
        with self.assertRaisesRegex(Exception,'outputs_incomplete'):self.store.load('synthetic')
        with self.assertRaises(FileExistsError):self.store.create('synthetic',self.cases,{},simulated=True)

    def test_supplement_history_and_no_unknown_or_success_replay(self):
        case=self.cases[0]['id']
        self.store.transition('synthetic',case,'A','not_called','accepted')
        self.store.transition('synthetic',case,'A','accepted','failed',error='provider_network_error')
        result=self.store.supplement('synthetic',case,'A','连接恢复后的明确补缺','operation')
        self.assertEqual(result,self.store.supplement('synthetic',case,'A','连接恢复后的明确补缺','operation'))
        self.assertNotEqual(result['request_id'],result['parent_request_id'])
        self.complete(case,'A')
        with self.assertRaisesRegex(Exception,'supplement_not_eligible'):self.store.supplement('synthetic',case,'A','不能替换成功输出','other')
        with self.store.connect('synthetic') as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE kind='supplement'").fetchone()[0],1)

    def test_two_workers_cannot_both_advance_the_same_slot(self):
        from concurrent.futures import ThreadPoolExecutor
        def accept(_):
            try:self.store.transition('synthetic','contract-0','A','not_called','accepted');return True
            except Exception:return False
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(accept,range(2)))
        self.assertEqual(sum(results),1)

    def test_single_long_failure_persistently_stops_new_slots(self):
        self.store.transition('synthetic','contract-0','A','not_called','accepted')
        self.store.transition('synthetic','contract-0','A','accepted','failed',elapsed_seconds=600.001)
        restored=EvaluationStore(RuntimePaths(Path(self.temp.name)))
        with self.assertRaisesRegex(Exception,'model_tests_stopped'):restored.transition('synthetic','contract-1','B','not_called','accepted')
        with self.assertRaisesRegex(Exception,'model_tests_stopped'):restored.supplement('synthetic','contract-0','A','不能绕过停测','reason')

    def test_schedule_is_stable_randomized_and_keeps_unknown(self):
        before=self.store.schedule('synthetic');self.assertEqual(len(before),300)
        self.assertNotEqual([r['arm'] for r in before[:3]],['A','B','C'])
        self.store.transition('synthetic','contract-0','A','not_called','accepted')
        restored=EvaluationStore(RuntimePaths(Path(self.temp.name))).schedule('synthetic')
        self.assertEqual([r['request_id'] for r in before],[r['request_id'] for r in restored])
        self.assertEqual(next(r for r in restored if r['case_id']=='contract-0' and r['arm']=='A')['state'],'accepted')


from tests.test_m761_api import API as APIBase

class API(APIBase):
    async def test_evaluation_admin_only_and_no_runtime_writes(self):
        store=EvaluationStore(self.paths)
        cases=[{'id':str(i),'scenario':'模拟','author_summary':'人工'} for i in range(100)]
        store.create('synthetic',cases,{},simulated=True)
        with self.app['store'].connection() as db:before=db.execute('SELECT count(*) FROM events').fetchone()[0]
        for action in ('evaluation.status','evaluation.review.load','evaluation.review.save','evaluation.review.export',
                       'evaluation.ai.status','evaluation.ai.load','evaluation.ai.approve','evaluation.ai.save-modified'):
            code,_=await self.post(action,{'package_id':'synthetic'},'bridge');self.assertEqual(code,403)
        code,result=await self.post('evaluation.status',{'package_id':'synthetic'})
        self.assertEqual(code,200);self.assertEqual(result['slots'],{'not_called':300})
        code,_=await self.post('evaluation.status',{'package_id':'../outside'});self.assertEqual(code,400)
        with self.app['store'].connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],before)
        self.assertEqual(self.fake.calls,[])


from tests.test_m761_desktop import Desktop as DesktopBase

class Desktop(unittest.TestCase):
    setUp=DesktopBase.setUp;window=DesktopBase.window;doCleanups=DesktopBase.doCleanups

    def test_review_options_are_chinese_and_existing_scores_round_trip(self):
        from unittest.mock import patch
        from persona_runtime.desktop_evaluation import open_review
        from persona_runtime.disposition_core.contracts import ACTIONS
        from tests.test_m763_desktop import descendants
        w=self.window();w.persona_id='artificial';dialog=open_review(w);w.update()
        widgets=list(descendants(dialog));combos=[x for x in widgets if x.winfo_class()=='TCombobox']
        self.assertEqual([x.get() for x in combos[1:]],['']*5)
        self.assertEqual(tuple(combos[1]['values']),('通过','关键错误','无法判断'))
        self.assertEqual(tuple(combos[2]['values']),tuple(combos[1]['values']))
        self.assertEqual(len(combos[3]['values']),len(ACTIONS)+1)
        self.assertEqual(tuple(combos[4]['values']),tuple(combos[3]['values']))
        self.assertEqual(tuple(combos[5]['values']),('左侧更自然','右侧更自然','平局'))
        for combo in combos[1:]:
            self.assertTrue(all(any('\u4e00'<=c<='\u9fff' for c in choice) for choice in combo['values']))
        old={'left_task':'pass','right_task':'critical_error','left_action':'acknowledge',
             'right_action':'set_boundary','natural':'right','reason':'人工夹具评分'}
        rows=[{'case_id':str(i),'scenario':'人工夹具输入','author_summary':'人工资料',
               'left':'模拟左回复','right':'模拟右回复','revision':1 if i==0 else 0,
               'review':dict(old) if i==0 else None} for i in range(25)]
        entry=next(x for x in widgets if x.winfo_class()=='TEntry');entry.insert(0,'synthetic')
        def run(name,resources,work,success=None,**kw):
            value=work(None)
            if success:success(value)
        buttons={x.cget('text'):x for x in widgets if x.winfo_class()=='TButton'}
        with patch.object(w,'run_task',side_effect=run),patch.object(w.client,'manage') as manage,\
             patch('persona_runtime.desktop_evaluation.messagebox.showwarning') as warning:
            ai={'cases':[{'case_id':str(i),'state':'unavailable','suggestion':None,'suggestion_hash':None,'error':None} for i in range(25)]}
            manage.side_effect=[{'cases':rows},ai,{'revision':2}];buttons['载入'].invoke();w.update()
            self.assertEqual([x.get() for x in combos[1:]],['通过','关键错误','确认或肯定','说明边界','右侧更自然'])
            self.assertEqual(manage.call_count,2)
            combos[3].set('询问必要信息');combos[5].set('左侧更自然')
            buttons['保存'].invoke()
            self.assertEqual(manage.call_args.args,('evaluation.review.save',))
            self.assertEqual(manage.call_args.kwargs['review'],old|{'left_action':'ask','natural':'left'})
            self.assertEqual(manage.call_args.kwargs['expected_revision'],1)
            buttons['下一组'].invoke();warning.assert_not_called()
            self.assertEqual([x.get() for x in combos[1:]],['']*5)
        dialog.destroy()

    def test_review_widgets_have_no_default_answers_and_reject_late_scope(self):
        from unittest.mock import patch
        from persona_runtime.desktop_evaluation import open_review
        from tests.test_m763_desktop import descendants
        w=self.window();w.persona_id='artificial';dialog=open_review(w);w.update()
        widgets=list(descendants(dialog));combos=[x for x in widgets if x.winfo_class()=='TCombobox']
        self.assertEqual([x.get() for x in combos[1:]],['']*5)
        callbacks=[]
        entry=next(x for x in widgets if x.winfo_class()=='TEntry');entry.insert(0,'synthetic')
        def pending(n,r,a,success=None,**kw):
            callbacks.append((success,kw));return object()
        with patch.object(w,'run_task',side_effect=pending):
            next(x for x in widgets if x.winfo_class()=='TButton' and x.cget('text')=='载入').invoke()
        w.persona_id='another';callbacks[0][0]({'invalid':'stale'})
        self.assertFalse(callbacks[0][1]['error_current']())
        dialog.destroy()
