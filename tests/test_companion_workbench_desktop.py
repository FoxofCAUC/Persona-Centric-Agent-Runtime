"""Real Tk controls with artificial policies; no services or provider calls."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from persona_runtime.companion import choose, capture_restore
from persona_runtime.desktop_client import LocalClient
from persona_runtime.models import RuntimeErrorBase
from tests import test_desktop as base
from tests import test_m72_desktop as context_fixture


def policy(candidate=False, *, channel='desktop', restore=True):
    value={'id':'old-policy','persona_id':'artificial','channel':channel,'revision':1,
           'mode':'legacy_per_message','reply_mode':'legacy_full','data_json':'{}',
           'context_mode':'working_set','cross_session':False,'character_retrieval':{'mode':'lexical'},
           'disposition_mode':'shadow','situation_observation_mode':'disabled',
           'expression_policy':{'version':1}}
    if candidate:
        data={'companion':choose('minimal_structural')}
        if restore:data['companion_restore']=capture_restore(value)
        value.update(id='new-policy',revision=2,mode='natural_batch',reply_mode='natural_structured',
                     companion=data['companion'],data_json=json.dumps(data),
                     character_retrieval={'mode':'hybrid'},disposition_mode='baseline',
                     expression_policy={'version':3})
    return value


class CompanionDesktopTests(unittest.TestCase):
    setUp=base.DesktopTests.setUp
    window=base.DesktopTests.window
    doCleanups=context_fixture.ContextDesktopTests.doCleanups

    def test_rules_candidate_controls_show_actual_version(self):
        value=policy(True);value['companion']=choose('minimal_context_rules_off')
        data=json.loads(value['data_json']);data['companion']=value['companion'];value['data_json']=json.dumps(data)
        w=self.ready(value)
        w.remember_companion_policy(value)
        self.assertEqual(w.ctx_companion_enable.cget('text'),'升级伴侣聊天')
        self.assertEqual(str(w.ctx_companion_enable.cget('state')),'normal')
        self.assertEqual(str(w.ctx_character_combo.cget('state')),'disabled')
        self.assertIn('情境规则',w.chat_mode_text.get())

    def test_purpose_candidate_is_recognized_and_can_upgrade(self):
        value=policy(True);value['companion']=choose('minimal_persona')
        data=json.loads(value['data_json']);data['companion']=value['companion'];value['data_json']=json.dumps(data)
        w=self.ready(value)
        w.remember_companion_policy(value)
        self.assertEqual(w.ctx_companion_enable.cget('text'),'升级伴侣聊天')
        self.assertEqual(str(w.ctx_companion_enable.cget('state')),'normal')
        self.assertIn('人物资料',w.chat_mode_text.get())

    def ready(self, value=None):
        w=self.window();w.persona_id='artificial';w.ctx_policy=value or policy()
        w._context_companion_controls(w.ctx_policy)
        return w

    def test_enable_uses_complete_policy_and_reuses_failed_intent(self):
        w=self.ready();jobs=[];calls=[]
        def run(name,resources,action,success=None,**kwargs):jobs.append((action,success,kwargs))
        def save(**args):calls.append(deepcopy(args));raise OSError('lost local response')
        with patch.object(w,'run_task',side_effect=run),patch.object(w.client,'update_companion_policy',side_effect=save):
            for _ in range(2):
                w.change_companion_mode(True)
                with self.assertRaises(OSError):jobs[-1][0](None)
        self.assertEqual(calls[0],calls[1])
        expected={'companion_mode':'minimal_im','mode':'natural_batch',
                  'conversation_policy_version':2,'expression_policy_version':3,
                  'disposition_mode':'baseline','situation_observation_mode':'disabled',
                  'reply_mode':'context_full','character_retrieval_mode':'hybrid',
                  'persona_id':'artificial','channel':'desktop','expected_version':'old-policy','pending_action':None}
        self.assertEqual({k:v for k,v in calls[0].items() if k!='operation_id'},expected)
        jobs[-1][1](policy(True))
        self.assertIn('伴侣候选',w.chat_mode_text.get())
        self.assertEqual(w.ctx_character.get(),'本地召回（伴侣聊天）')

    def test_restore_sends_only_restore_intent_qq_and_legacy_have_no_automatic_restore(self):
        w=self.ready(policy(True));jobs=[]
        with patch.object(w,'run_task',side_effect=lambda n,r,a,success=None,**kw:jobs.append(a)), \
                patch('persona_runtime.desktop_context.messagebox.askokcancel',return_value=True), \
                patch.object(w.client,'update_companion_policy',return_value=policy()) as save:
            w.change_companion_mode(False);jobs[-1](None)
        self.assertEqual(set(save.call_args.kwargs),{'persona_id','channel','mode','expected_version',
                                                    'operation_id','pending_action','companion_mode'})
        self.assertEqual(save.call_args.kwargs['companion_mode'],'disabled')
        self.assertEqual(save.call_args.kwargs['mode'],'natural_batch')
        w.ctx_policy=policy(True,restore=False);w._context_companion_controls(w.ctx_policy)
        self.assertEqual(str(w.ctx_companion_restore.cget('state')),'disabled')
        with patch.object(w,'run_task') as run:w.change_companion_mode(False);run.assert_not_called()
        w.ctx_channel.set('qq');w.ctx_policy=policy(channel='qq');w._context_companion_controls(w.ctx_policy)
        self.assertEqual(str(w.ctx_companion_enable.cget('state')),'normal')
        with patch.object(w,'run_task') as run:w.change_companion_mode(True);run.assert_called_once()

    def test_restore_scope_is_shown_and_cancel_writes_nothing(self):
        w=self.ready(policy(True))
        with patch('persona_runtime.desktop_context.messagebox.askokcancel',return_value=False) as ask, \
                patch.object(w,'run_task') as run:
            w.change_companion_mode(False);run.assert_not_called()
        text=ask.call_args.args[1]
        for phrase in ('逐条回复','兼容全文','词法选材','辅助理解','工作集','跨会话','启用期间修改'):
            self.assertIn(phrase,text)
        self.assertNotIn('data_json',text)
        self.assertIn('最初',text)

    def test_upgrade_requires_original_restore_point(self):
        w=self.ready(policy(True))
        self.assertEqual(w.ctx_companion_enable.cget('text'),'升级伴侣聊天')
        self.assertEqual(str(w.ctx_companion_enable.cget('state')),'normal')
        w.ctx_policy=policy(True,restore=False);w._context_companion_controls(w.ctx_policy)
        self.assertEqual(str(w.ctx_companion_enable.cget('state')),'disabled')
        with patch.object(w,'run_task') as run:
            w.change_companion_mode(True);run.assert_not_called()

    def test_pending_resolution_and_late_results_preserve_current_scope_and_draft(self):
        w=self.ready();w.batch_rows=[{'persona_id':'artificial','channel':'desktop','state':'collecting'}]
        with patch('persona_runtime.desktop_context.messagebox.askyesnocancel',return_value=None),patch.object(w,'run_task') as run:
            w.change_companion_mode(True);run.assert_not_called()
        for answer,expected in ((True,'seal'),(False,'cancel')):
            jobs=[]
            with patch('persona_runtime.desktop_context.messagebox.askyesnocancel',return_value=answer), \
                    patch.object(w,'run_task',side_effect=lambda n,r,a,success=None,**kw:jobs.append((a,success,kw))), \
                    patch.object(w.client,'update_companion_policy') as save:
                w.change_companion_mode(True);jobs[-1][0](None)
            self.assertEqual(save.call_args.kwargs['pending_action'],expected)
        w.ctx_enabled.set(False);w.ctx_cross.set(True)
        jobs[-1][1](policy(True))
        self.assertFalse(w.ctx_enabled.get());self.assertTrue(w.ctx_cross.get())
        w.persona_id='new-persona';w.view_epoch+=1;w.invalidate_context()
        jobs[-1][1](policy(True))
        self.assertIsNone(w.ctx_policy)
        self.assertFalse(jobs[-1][2]['error_current']())

    def test_candidate_conflict_controls_stay_disabled_after_tick(self):
        w=self.ready(policy(True));w.remember_companion_policy(w.ctx_policy)
        w.batch_policies={'desktop':policy(True)};w.dispo_state={'revision':1}
        w.tick()
        for widget in (w.ctx_character_combo,w.reply_mode_buttons['desktop'],w.dispo_mode_combo,
                       w.dispo_observation_combo,w.dispo_mode_save):
            self.assertEqual(str(widget.cget('state')),'disabled')
        self.assertEqual(w.dispo_mode.get(),'baseline');self.assertEqual(w.dispo_observation.get(),'disabled')
        with patch.object(w,'run_task') as run:
            w.change_reply_mode('desktop');w.save_disposition_mode();run.assert_not_called()

    def test_c2_actual_version_and_conflicting_controls(self):
        from persona_runtime.desktop_context import companion_candidate, companion_description
        value=policy(True)
        value['companion']=choose('minimal_chat')
        data=json.loads(value['data_json']);data['companion']=value['companion']
        value['data_json']=json.dumps(data)
        w=self.ready(value);w.remember_companion_policy(value)
        w.batch_policies={'desktop':value};w.dispo_state={'revision':1}
        w.tick()
        self.assertTrue(companion_candidate(value))
        self.assertIn('C2',companion_description(value))
        self.assertIn('C2',w.chat_mode_text.get())
        for widget in (w.ctx_character_combo,w.reply_mode_buttons['desktop'],w.dispo_mode_combo,
                       w.dispo_observation_combo,w.dispo_mode_save):
            self.assertEqual(str(widget.cget('state')),'disabled')
        self.assertEqual(str(w.ctx_companion_enable.cget('state')),'normal')
        wrong=deepcopy(value);wrong['companion']['format_contract']='schema3-structural-v1'
        self.assertFalse(companion_candidate(wrong))
        # An older background read cannot re-enable controls after activation.
        w.remember_companion_policy(policy())
        self.assertEqual(str(w.reply_mode_buttons['desktop'].cget('state')),'disabled')
        restored=policy();restored.update(id='restored',revision=3)
        w.ctx_policy=restored;w.remember_companion_policy(restored);w.tick()
        for widget in (w.ctx_character_combo,w.dispo_mode_combo,w.dispo_observation_combo):
            self.assertEqual(str(widget.cget('state')),'readonly')

    def test_late_policy_read_cannot_replace_successful_enable(self):
        w=self.ready();jobs=[]
        with patch.object(w,'run_task',side_effect=lambda n,r,a,success=None,**kw:jobs.append((success,kw))):
            w.refresh_context_policy();w.change_companion_mode(True)
        jobs[1][0](policy(True));jobs[0][0](policy())
        self.assertEqual(w.ctx_policy['id'],'new-policy')
        self.assertFalse(jobs[0][1]['error_current']())

    def test_reply_summary_distinguishes_structural_contract_without_fixed_punctuation(self):
        w=self.ready(policy(True));jobs=[]
        with patch.object(w,'run_task',side_effect=lambda n,r,a,success=None,**kw:jobs.append(success)):
            w.refresh_batches()
        jobs[-1](([],{'desktop':policy(True),'qq':policy(channel='qq')}))
        text=w.reply_policy_text.get()
        self.assertIn('伴侣候选结构合同',text);self.assertNotIn('通常省略',text)
        self.assertNotIn('companion',w.companion_policies['qq'])

    def test_chat_usage_is_compact_and_original_audit_is_preserved(self):
        w=self.window()
        usage={'prompt_tokens':5,'completion_tokens':8,'total_tokens':13,
               'generation_retry':{'version':'generation-retry-v1','attempts':[
                   {'index':0,'id':'private-audit-id','request_hash':'private-request-hash','started_epoch':1234,
                    'status':'failed','usage_known':False},
                   {'index':1,'id':'second-audit-id','status':'completed','usage_known':True}]}}
        before=deepcopy(usage)
        w.turns=[{'status':'completed','text':'人工输入','reply':'人工回复','usage':usage}]
        w.render_chat();shown=w.transcript.get('1.0','end')
        self.assertIn('用量：已知 13 token（部分调用用量未知） · 自动重试 1 次',shown)
        for forbidden in ('generation_retry','private-audit-id','private-request-hash','started_epoch','1234'):
            self.assertNotIn(forbidden,shown)
        self.assertEqual(usage,before);self.assertEqual(w.turns[0]['reply'],'人工回复')
        w.turns[0]['usage']={'generation_retry':{'attempts':[]}}
        w.render_chat();self.assertIn('用量：未知',w.transcript.get('1.0','end'))

    def test_inflight_candidate_cancellation_does_not_claim_generation_never_started(self):
        w=self.window();old_note='已取消排队：不会开始主回复。'
        turn={'status':'interrupted','text':'人工输入','reply':None,'usage':None,
              'error_code':'cancelled_by_user','attempt_count':1,'recovery':old_note,
              'context_policy_snapshot':json.dumps({'companion':choose('minimal_structural')})}
        original=deepcopy(turn);w.turns=[turn];w.render_chat();shown=w.transcript.get('1.0','end')
        self.assertIn('本地不再发布本次回复',shown);self.assertIn('用量以已收到回执为准',shown)
        self.assertNotIn('不会开始主回复',shown);self.assertEqual(turn,original)
        w.turns=[turn|{'attempt_count':0,'context_policy_snapshot':'{}'}]
        w.render_chat();self.assertIn(old_note,w.transcript.get('1.0','end'))


class CompanionClientTests(unittest.TestCase):
    def test_rules_candidate_requires_its_own_capability(self):
        client=self.client()
        with patch.object(client,'call',return_value={'capabilities':['companion_c2_workbench_v1']}) as call:
            with self.assertRaisesRegex(RuntimeErrorBase,'runtime_upgrade_required'):
                client.update_companion_policy(companion_mode='minimal_context_rules_off')
            call.assert_called_once_with('GET','/v1/health')
        with patch.object(client,'call',side_effect=[{'capabilities':['companion_context_rules_workbench_v1']},{'id':'v5'}]):
            self.assertEqual(client.update_companion_policy(companion_mode='minimal_context_rules_off'),{'id':'v5'})

    def client(self):
        client=object.__new__(LocalClient)
        return client

    def test_old_service_refuses_policy_write_before_post(self):
        client=self.client()
        with patch.object(client,'call',return_value={'capabilities':[]}) as call:
            with self.assertRaisesRegex(RuntimeErrorBase,'runtime_upgrade_required'):
                client.update_companion_policy(companion_mode='minimal_structural')
        call.assert_called_once_with('GET','/v1/health')

    def test_c2_needs_c2_capability_before_write(self):
        client=self.client()
        with patch.object(client,'call',return_value={'capabilities':['companion_workbench_v1']}) as call:
            with self.assertRaisesRegex(RuntimeErrorBase,'runtime_upgrade_required'):
                client.update_companion_policy(companion_mode='minimal_chat')
            call.assert_called_once_with('GET','/v1/health')
        with patch.object(client,'call',side_effect=[{'capabilities':['companion_c2_workbench_v1']},{'id':'c2'}]) as call:
            self.assertEqual(client.update_companion_policy(companion_mode='minimal_chat'),{'id':'c2'})
            self.assertEqual(call.call_args.args[0:2],('POST','/v1/manage'))

    def test_persona_candidate_requires_its_own_service_capability(self):
        client=self.client()
        with patch.object(client,'call',return_value={'capabilities':['companion_c2_workbench_v1']}) as call:
            with self.assertRaisesRegex(RuntimeErrorBase,'runtime_upgrade_required'):
                client.update_companion_policy(companion_mode='minimal_persona')
            call.assert_called_once_with('GET','/v1/health')
        with patch.object(client,'call',side_effect=[{'capabilities':['companion_persona_workbench_v1']},{'id':'v4'}]):
            self.assertEqual(client.update_companion_policy(companion_mode='minimal_persona'),{'id':'v4'})

    def test_new_service_policy_write_uses_existing_api(self):
        client=self.client()
        with patch.object(client,'call',side_effect=[{'capabilities':['companion_workbench_v1']},{'id':'new'}]) as call:
            self.assertEqual(client.update_companion_policy(companion_mode='disabled'),{'id':'new'})
        self.assertEqual(call.call_args.args,('POST','/v1/manage',{'action':'interaction.update','args':{'companion_mode':'disabled'}}))

    def test_running_cancel_uses_accepted_snapshot_and_capability_old_queued_unchanged(self):
        client=self.client()
        accepted={'id':'accepted','status':'running','context_policy_snapshot':json.dumps({'companion':choose('minimal_structural')})}
        with patch.object(client,'call',return_value={'capabilities':[]}) as call:
            with self.assertRaisesRegex(RuntimeErrorBase,'runtime_upgrade_required'):client.cancel_request(accepted)
            call.assert_called_once_with('GET','/v1/health')
        with patch.object(client,'call',side_effect=[{'capabilities':['companion_cancel_v1']},{'cancelled_at':'now'}]) as call:
            self.assertEqual(client.cancel_request(accepted),{'cancelled_at':'now'})
            self.assertEqual(call.call_args.args,('POST','/v1/requests/accepted/cancel',{}))
        with patch.object(client,'call') as call:
            with self.assertRaisesRegex(RuntimeErrorBase,'only_queued_can_cancel'):
                client.cancel_request({'id':'old','status':'running','context_policy_snapshot':'{}'})
            call.assert_not_called()
            client.cancel_request({'id':'old','status':'queued','context_policy_snapshot':'{}'})
            call.assert_called_once_with('POST','/v1/requests/old/cancel',{})


if __name__=='__main__':unittest.main()
