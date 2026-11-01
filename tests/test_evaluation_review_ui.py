"""Save acknowledgement and draft protection with artificial review data only."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from tests import test_m761_desktop as desktop_base
from tests.test_m763_desktop import descendants
from persona_runtime.desktop_evaluation import open_review


class ReviewUI(unittest.TestCase):
    setUp=desktop_base.Desktop.setUp
    window=desktop_base.Desktop.window
    doCleanups=desktop_base.Desktop.doCleanups

    def review(self):
        w=self.window();w.persona_id='artificial';dialog=open_review(w);w.update()
        widgets=list(descendants(dialog))
        combos=[x for x in widgets if x.winfo_class()=='TCombobox'][1:]
        reason=[x for x in widgets if x.winfo_class()=='Text'][-1]
        buttons={x.cget('text'):x for x in widgets if x.winfo_class()=='TButton'}
        entry=next(x for x in widgets if x.winfo_class()=='TEntry');entry.insert(0,'synthetic')
        status=next(x for x in widgets if x.winfo_class()=='TLabel' and x.cget('textvariable'))
        rows=[{'case_id':str(i),'scenario':'人工工具夹具'+str(i),'author_summary':'人工资料',
               'left':'模拟左回复','right':'模拟右回复','revision':0,'review':None} for i in range(25)]
        saved=[]
        def manage(action,**body):
            if action=='evaluation.review.load':return {'cases':deepcopy(rows)}
            if action=='evaluation.ai.load':return {'cases':[{'case_id':str(i),'state':'not_called','suggestion':None,'suggestion_hash':None,'error':None} for i in range(25)]}
            self.assertIn(action,('evaluation.review.save','evaluation.ai.approve','evaluation.ai.save-modified'))
            saved.append(deepcopy(body));return {'revision':body['expected_revision']+1}
        def run(name,resources,work,success=None,**kwargs):
            try:value=work(None)
            except Exception as error:
                kwargs['failure'](error)
            else:success(value)
            return object()
        self.enterContext(patch.object(w.client,'manage',side_effect=manage))
        self.enterContext(patch.object(w,'run_task',side_effect=run))
        warning=self.enterContext(patch('persona_runtime.desktop_evaluation.messagebox.showwarning'))
        error=self.enterContext(patch('persona_runtime.desktop_evaluation.messagebox.showerror'))
        buttons['载入'].invoke();w.update()
        return w,dialog,combos,reason,buttons,status,saved,warning,error

    def fill(self,combos,reason,text='仅人工夹具理由'):
        for combo,label in zip(combos,['通过','通过','支持或倾听','支持或倾听','平局']):combo.set(label)
        if text:reason.insert('1.0',text)

    def test_empty_reason_is_explained_before_dispatch_and_can_be_corrected(self):
        w,d,combos,reason,buttons,status,saved,warning,error=self.review()
        self.fill(combos,reason,'');buttons['保存'].invoke()
        self.assertEqual(saved,[]);self.assertIn('评分理由',warning.call_args.args[1])
        self.assertIn('未保存',status.cget('text'))
        self.assertEqual(combos[0].get(),'通过')
        reason.insert('1.0','人工理由');buttons['保存'].invoke()
        self.assertEqual(saved[0]['review']['reason'],'人工理由')
        self.assertIn('已保存（修订1）',status.cget('text'))
        warning.reset_mock();buttons['下一组'].invoke();warning.assert_not_called();error.assert_not_called()
        self.assertIn('第2/25组',status.cget('text'));d.destroy()

    def test_unselected_answer_is_not_silently_submitted(self):
        w,d,combos,reason,buttons,status,saved,warning,error=self.review()
        self.fill(combos,reason);combos[-1].set('');buttons['保存'].invoke()
        self.assertEqual(saved,[]);self.assertIn('自然度',warning.call_args.args[1]);d.destroy()

    def test_nonediting_key_and_reverted_text_do_not_make_saved_score_dirty(self):
        w,d,combos,reason,buttons,status,saved,warning,error=self.review()
        self.fill(combos,reason);buttons['保存'].invoke()
        reason.insert('end','临时');reason.delete('end-3c','end-1c')
        reason.event_generate('<KeyRelease>',keysym='Tab');w.update()
        buttons['下一组'].invoke();warning.assert_not_called()
        self.assertIn('第2/25组',status.cget('text'));d.destroy()

    def test_next_while_saving_waits_for_acknowledgement_and_duplicate_save_is_ignored(self):
        w,d,combos,reason,buttons,status,saved,warning,error=self.review()
        self.fill(combos,reason);pending=[]
        def run(name,resources,work,success=None,**kwargs):
            pending.append((work,success,kwargs));return object()
        with patch.object(w,'run_task',side_effect=run):
            buttons['保存'].invoke();buttons['保存'].invoke();buttons['下一组'].invoke()
            self.assertEqual(len(pending),1);self.assertEqual(saved,[])
            self.assertIn('正在保存',status.cget('text'));warning.assert_not_called()
            work,done,_=pending[0];done(work(None))
        self.assertEqual(len(saved),1);self.assertIn('第2/25组',status.cget('text'))
        warning.assert_not_called();error.assert_not_called();d.destroy()

    def test_new_edits_during_save_stay_as_draft_and_prevent_automatic_next(self):
        w,d,combos,reason,buttons,status,saved,warning,error=self.review()
        self.fill(combos,reason);pending=[]
        def run(name,resources,work,success=None,**kwargs):
            pending.append((work,success,kwargs));return object()
        with patch.object(w,'run_task',side_effect=run):
            buttons['保存'].invoke();buttons['下一组'].invoke();reason.insert('end','新编辑')
            work,done,_=pending[0];done(work(None))
        self.assertIn('新编辑的草稿仍保留',status.cget('text'))
        self.assertNotIn('新编辑',saved[0]['review']['reason']);self.assertIn('新编辑',reason.get('1.0','end'))
        buttons['下一组'].invoke();warning.assert_called_once();d.destroy()

    def test_save_error_is_visible_and_retry_reuses_operation_and_keeps_draft(self):
        w,d,combos,reason,buttons,status,saved,warning,error=self.review()
        self.fill(combos,reason);pending=[]
        def run(name,resources,work,success=None,**kwargs):
            pending.append((work,success,kwargs));return object()
        with patch.object(w,'run_task',side_effect=run):
            buttons['保存'].invoke();buttons['下一组'].invoke()
            first_work,_,callbacks=pending[0]
            with patch.object(w.client,'manage',side_effect=OSError('connection lost')) as failed:
                with self.assertRaises(OSError):first_work(None)
                first_body=deepcopy(failed.call_args.kwargs)
            callbacks['failure'](OSError('connection lost'))
            error.assert_called_once();self.assertIs(error.call_args.kwargs['parent'],d)
            self.assertIn('未保存',status.cget('text'));self.assertEqual(reason.get('1.0','end-1c'),'仅人工夹具理由')
            buttons['保存'].invoke();work,done,_=pending[1];done(work(None))
        self.assertEqual(first_body,saved[0]);self.assertIn('已保存',status.cget('text'))
        self.assertNotIn('第2/25组',status.cget('text'))
        buttons['下一组'].invoke();warning.assert_not_called();d.destroy()

    def test_busy_operation_reports_not_submitted_and_allows_later_save(self):
        w,d,combos,reason,buttons,status,saved,warning,error=self.review()
        self.fill(combos,reason)
        with patch.object(w,'run_task',return_value=None):buttons['保存'].invoke()
        self.assertEqual(saved,[]);self.assertIn('尚未提交',error.call_args.args[1])
        buttons['保存'].invoke();self.assertEqual(len(saved),1);d.destroy()

    def test_score_is_persisted_in_isolated_ledger_before_next_group(self):
        from persona_runtime.evaluation import EvaluationStore
        w,d,combos,reason,buttons,status,saved,warning,error=self.review()
        store=EvaluationStore(self.paths)
        cases=[{'id':str(i),'scenario':'人工工具夹具'+str(i),'author_summary':'人工资料'} for i in range(100)]
        store.create('synthetic',cases,{'purpose':'UI contract only'},simulated=True)
        for case in cases[:25]:
            for arm in 'BC':
                store.transition('synthetic',case['id'],arm,'not_called','accepted')
                store.transition('synthetic',case['id'],arm,'accepted','running')
                store.transition('synthetic',case['id'],arm,'running','success',
                                 attempt_id='artificial-'+case['id']+arm,output='模拟回复')
        def manage(action,**body):
            if action=='evaluation.review.load':return store.load(**body)
            if action=='evaluation.ai.load':return {'cases':[{'case_id':str(i),'state':'not_called','suggestion':None,'suggestion_hash':None,'error':None} for i in range(25)]}
            self.assertEqual(action,'evaluation.review.save');return store.save(**body)
        with patch.object(w.client,'manage',side_effect=manage):
            buttons['载入'].invoke();self.fill(combos,reason,'人工工具夹具评分，不代表用户偏好')
            buttons['保存'].invoke()
            self.assertEqual(store.status('synthetic')['reviewed'],1)
            persisted=store.load('synthetic')['cases'][0]
            self.assertEqual(persisted['revision'],1)
            self.assertEqual(persisted['review']['left_action'],'support')
            self.assertIn('已保存（修订1）',status.cget('text'))
            buttons['下一组'].invoke();self.assertIn('第2/25组',status.cget('text'))
            warning.assert_not_called();error.assert_not_called()
        d.destroy()

    def test_ai_approval_saves_exact_suggestion_and_advances(self):
        w,d,combos,reason,buttons,status,saved,warning,error=self.review()
        rows=[{'case_id':str(i),'scenario':'AI辅助人工夹具'+str(i),'author_summary':'人工资料',
               'left':'匿名左回复','right':'匿名右回复','revision':0,'review':None} for i in range(25)]
        suggestion={'left_task':'pass','right_task':'pass','left_action':'support','right_action':'ask',
                    'natural':'left','reason':'左侧先回应并倾听，右侧提出了不必要的追问。'}
        calls=[]
        def manage(action,**body):
            if action=='evaluation.review.load':return {'cases':deepcopy(rows)}
            if action=='evaluation.ai.load':
                return {'cases':[{'case_id':str(i),'state':'success','suggestion':deepcopy(suggestion),
                                  'suggestion_hash':'hash-'+str(i),'error':None} for i in range(25)]}
            calls.append((action,deepcopy(body)));return {'revision':1}
        with patch.object(w.client,'manage',side_effect=manage):
            buttons['载入'].invoke();w.update()
            self.assertEqual(combos[0].get(),'通过');self.assertEqual(combos[2].get(),'支持或倾听')
            self.assertEqual(str(combos[0].cget('state')),'disabled')
            self.assertIn('DeepSeek建议待核查',status.cget('text'))
            buttons['通过'].invoke()
        self.assertEqual(calls[0][0],'evaluation.ai.approve')
        self.assertEqual(calls[0][1]['review'],suggestion)
        self.assertEqual(calls[0][1]['suggestion_hash'],'hash-0')
        self.assertIn('第2/25组',status.cget('text'));warning.assert_not_called();error.assert_not_called();d.destroy()

    def test_ai_modify_requires_manual_save_then_manual_next(self):
        w,d,combos,reason,buttons,status,saved,warning,error=self.review()
        rows=[{'case_id':str(i),'scenario':'AI辅助人工夹具'+str(i),'author_summary':'人工资料',
               'left':'匿名左回复','right':'匿名右回复','revision':0,'review':None} for i in range(25)]
        suggestion={'left_task':'pass','right_task':'pass','left_action':'support','right_action':'ask',
                    'natural':'left','reason':'AI建议理由。'}
        calls=[]
        def manage(action,**body):
            if action=='evaluation.review.load':return {'cases':deepcopy(rows)}
            if action=='evaluation.ai.load':return {'cases':[{'case_id':str(i),'state':'success','suggestion':deepcopy(suggestion),
                'suggestion_hash':'hash-'+str(i),'error':None} for i in range(25)]}
            calls.append((action,deepcopy(body)));return {'revision':1}
        with patch.object(w.client,'manage',side_effect=manage):
            buttons['载入'].invoke();w.update();buttons['修改'].invoke()
            self.assertEqual(str(combos[0].cget('state')),'readonly')
            combos[-1].set('平局');reason.configure(state='normal');reason.delete('1.0','end');reason.insert('1.0','人工修改理由。')
            buttons['下一组'].invoke();warning.assert_called_once()
            buttons['保存'].invoke()
            self.assertIn('已保存（修订1）',status.cget('text'))
            buttons['下一组'].invoke()
        self.assertEqual(calls[0][0],'evaluation.ai.save-modified')
        self.assertEqual(calls[0][1]['review']['natural'],'tie')
        self.assertEqual(calls[0][1]['review']['reason'],'人工修改理由。')
        self.assertIn('第2/25组',status.cget('text'));error.assert_not_called();d.destroy()
