"""E3主稿／表达／呈现故障矩阵；呈现回执为隔离模拟，不代表桌面验收。"""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.archive import scope
from persona_runtime.chat import ChatEngine, ModelError
from persona_runtime.config import ConfigStore
from persona_runtime.expression_assets import ExpressionAssets, ExpressionAssetError
from persona_runtime.expression_runtime import ExpressionRuntime
from persona_runtime.expression_publication import ExpressionPublication
from persona_runtime.models import generic_persona,identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


class MainClient:
    def __init__(self):self.calls=[];self.gate=None;self.started=asyncio.Event();self.cancel_receipt=False
    async def complete(self,model,messages):
        self.calls.append(messages);self.started.set()
        if self.gate:
            try:await self.gate.wait()
            except asyncio.CancelledError:
                if not self.cancel_receipt:raise
        return '隐藏主稿。',{'prompt_tokens':20,'completion_tokens':5,'total_tokens':25}


class GateClient:
    def __init__(self):
        self.calls=[];self.started=asyncio.Event();self.gate=None;self.failure=None
        self.body='已呈现前半句。尚未呈现后半句。';self.parts=['已呈现前半句。','尚未呈现后半句。']
        self.raw_override=None;self.callback=None;self.after_receipt=False
    def raw(self):
        content=self.raw_override if self.raw_override is not None else json.dumps({'body':self.body,'segments':self.parts,'selected_expression_ids':[]},ensure_ascii=False)
        return json.dumps({'choices':[{'message':{'content':content},'finish_reason':'stop'}],
                           'usage':{'prompt_tokens':100,'completion_tokens':50}},ensure_ascii=False).encode('utf-8')
    async def complete(self,model,messages,*,save_raw):
        self.calls.append(messages);self.callback=save_raw;self.started.set()
        if self.gate:await self.gate.wait()
        if self.failure:
            from persona_runtime.expression_transport import ExpressionCallFailure
            raise ExpressionCallFailure(self.failure,{'request_headers_sent':True,'response_headers_received':False})
        save_raw(self.raw())
        if self.after_receipt:raise RuntimeError('local processing stopped')
        return {'diagnostics':{'request_headers_sent':True,'response_headers_received':True}}


class Runtime(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='expression-runtime-',dir=RuntimePaths.from_environment().home/'tmp');self.addCleanup(self.temp.cleanup)
        self.paths=RuntimePaths(Path(self.temp.name));self.cfg=ConfigStore(self.paths);self.cfg.initialize()
        self.cfg.update({'model':{'base_url':'https://api.deepseek.com','name':'deepseek-flash'}})
        self.store=Store(self.paths);self.store.initialize();self.service=RuntimeService(self.store)
        self.pid=self.service.create_persona(generic_persona())['id'];self.service.use_persona(self.pid)
        self.main=MainClient();self.gate=GateClient();self.runtime=ExpressionRuntime(self.store,self.gate)
        self.runtime.configure(self.pid,{'enabled':True},0,identifier())
        self.chat=ChatEngine(self.store,self.cfg,self.main,expression_runtime=self.runtime);self.publisher=ExpressionPublication(self.runtime)

    async def asyncTearDown(self):
        if self.main.gate:self.main.gate.set()
        if self.gate.gate:self.gate.gate.set()
        for task in list(self.chat.tasks.values()):
            if not task.done():task.cancel()
        await self.chat.wait()

    async def submit(self,text='今天先随便聊聊。'):
        turn=await self.chat.submit('desktop',text,identifier());await self.chat.wait();return self.chat.get(turn['id'])

    def acknowledge(self,turn,ordinal,*,state='presented'):
        plan=self.publisher.plan(self.pid,turn['session_id'],turn['id'])
        offered=self.publisher.offer(self.pid,turn['session_id'],turn['id'],plan['id'],plan['revision'],ordinal,'fixture-presenter')
        receipt=identifier()
        result=self.publisher.acknowledge(self.pid,turn['session_id'],turn['id'],offered['id'],offered['lease_id'],'fixture-presenter',receipt,offered['body_hash'],state)
        return offered,receipt,result

    def all_visible(self,turn):
        plan=self.publisher.plan(self.pid,turn['session_id'],turn['id'])
        for item in plan['items']:self.acknowledge(turn,item['ordinal'])
        return self.chat.get(turn['id'])

    async def test_main_is_durable_before_expression_dispatch_and_never_in_chat_history(self):
        self.gate.gate=asyncio.Event();turn=await self.chat.submit('desktop','先聊一句。',identifier());await asyncio.wait_for(self.gate.started.wait(),10)
        with self.store.connection() as db:
            row=db.execute('SELECT * FROM expression_main_receipts WHERE turn_id=?',(turn['id'],)).fetchone()
            self.assertEqual(row['body'],'隐藏主稿。')
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE role='persona'").fetchone()[0],0)
            attempt=db.execute('SELECT * FROM attempts WHERE id=?',(row['logical_attempt_id'],)).fetchone()
            self.assertEqual(json.loads(attempt['usage_json'])['generation_retry']['attempts'][0]['status'],'completed')
        self.gate.gate.set();await self.chat.wait();turn=self.chat.get(turn['id'])
        self.assertIsNone(turn['reply']);self.assertNotIn('body',turn['expression']['draft'])
        visible=self.all_visible(turn);self.assertEqual(visible['reply'],self.gate.body)
        self.assertEqual(len(self.main.calls),1);self.assertEqual(len(self.gate.calls),1)

    async def test_partial_receipt_does_not_promote_hidden_tail_and_explicit_stop_keeps_prefix(self):
        turn=await self.submit();self.acknowledge(turn,0)
        self.assertIsNone(self.chat.get(turn['id'])['reply'])
        self.assertEqual([r['text'] for r in self.publisher.visible(self.pid,turn['session_id'],turn['id'])],[self.gate.parts[0]])
        self.publisher.cancel(turn['id'])
        self.assertEqual(self.chat.get(turn['id'])['reply'],self.gate.parts[0])

    async def test_new_main_waits_until_old_actual_prefix_is_closed(self):
        turn=await self.submit();self.acknowledge(turn,0)
        next_turn=await self.chat.submit('desktop','接着刚才说。',identifier())
        await asyncio.sleep(.12);self.assertEqual(len(self.main.calls),1)
        self.publisher.cancel(turn['id']);await self.chat.wait()
        latest=json.dumps(self.main.calls[-1],ensure_ascii=False)
        self.assertIn(self.gate.parts[0],latest);self.assertNotIn(self.gate.parts[1],latest);self.assertNotIn('隐藏主稿',latest)
        self.publisher.cancel(next_turn['id'])

    async def test_webview_new_input_closes_prior_and_only_reads_confirmed_prefix(self):
        from persona_runtime.ingress import IngressService
        from persona_runtime.tk_notice import CONTRACT
        prior=await self.submit()
        self.acknowledge(prior,0)
        ingress=IngressService(self.chat)
        received=ingress.accept({"persona_id":self.pid,"channel":"desktop","session_id":prior["session_id"],
                                 "request_id":identifier(),"text":"811","desktop_presentation":CONTRACT})
        if not received["turn_id"]:
            received=ingress.action(received["batch_id"],"seal",received["batch_revision"],identifier(),self.pid)
        await asyncio.wait_for(self.chat.wait(),10)
        self.assertEqual(self.chat.get(prior["id"])["reply"],self.gate.parts[0])
        history=json.dumps(self.main.calls[-1],ensure_ascii=False)
        self.assertIn(self.gate.parts[0],history)
        self.assertNotIn(self.gate.parts[1],history)
        self.assertEqual(len(self.main.calls),2)

    async def test_legacy_webview_known_not_presented_reuses_only_same_fragment(self):
        turn=await self.submit()
        key=dict(persona_id=self.pid,session_id=turn["session_id"],turn_id=turn["id"])
        plan=self.publisher.plan(**key)
        position=dict(publication_id=plan["id"],revision=plan["revision"],ordinal=0)
        slot=dict(key,**position,presenter_id="webview-v2.815")
        first=self.publisher.offer(**slot)
        decision=dict(slot,utterance_id=first["id"],receipt_id="815",lease_id=first["lease_id"],
                      body_hash=first["body_hash"],webview=True,state="not_presented")
        self.publisher.acknowledge(**decision)
        self.assertTrue(self.publisher.acknowledge(**decision)["already_applied"])
        second=self.publisher.offer(**(slot|{"presenter_id":"webview-v2.816"}))
        self.assertNotEqual(first["lease_id"],second["lease_id"])
        self.publisher.acknowledge(**(decision|{"receipt_id":"816","presenter_id":"webview-v2.816",
                                               "lease_id":second["lease_id"],"state":"presented"}))
        self.assertEqual(self.publisher.visible(**key)[0]["text"],first["body"])
        self.publisher.cancel(turn["id"])

    async def test_webview_new_input_settles_prior_with_only_saved_draft(self):
        from persona_runtime.ingress import IngressService
        from persona_runtime.tk_notice import CONTRACT
        async def interrupted(*args):raise RuntimeError("812")
        with patch.object(self.runtime,"execute",side_effect=interrupted):
            prior=await self.submit()
        self.assertIsNotNone(prior['expression']['draft'])
        ingress=IngressService(self.chat)
        received=ingress.accept({"persona_id":self.pid,"channel":"desktop","session_id":prior["session_id"],
                                 "request_id":identifier(),"text":"813","desktop_presentation":CONTRACT})
        if not received["turn_id"]:
            ingress.action(received["batch_id"],"seal",received["batch_revision"],identifier(),self.pid)
        await asyncio.wait_for(self.chat.wait(),10)
        self.assertEqual(self.chat.get(prior["id"])["error_code"],"desktop_reply_superseded")
        self.assertIsNone(self.chat.get(prior["id"])["reply"])
        self.assertEqual(len(self.main.calls),2)

    async def test_reopen_can_present_saved_webview_reply_without_new_model_call(self):
        from persona_runtime.ingress import IngressService
        from persona_runtime.tk_notice import CONTRACT
        ingress=IngressService(self.chat)
        async def interrupted(*args):raise RuntimeError("814")
        with patch.object(self.runtime,"execute",side_effect=interrupted):
            receipt=ingress.accept({"persona_id":self.pid,"channel":"desktop","text":"817",
                                    "request_id":identifier(),"desktop_presentation":CONTRACT})
            if not receipt["turn_id"]:
                receipt=ingress.action(receipt["batch_id"],"seal",receipt["batch_revision"],identifier(),self.pid)
            await self.chat.wait()
        tid=receipt["turn_id"]
        self.chat.recover()
        self.chat.recover()
        current=self.chat.get(tid)
        self.assertEqual(current["expression"]["publication"]["producer_kind"],"draft")
        self.assertEqual(current["desktop_publications"][0]["state"],"pending")
        self.assertIsNone(current["reply"])
        with self.store.connection() as db:
            expected=db.execute("SELECT body FROM expression_main_receipts WHERE turn_id=?",(tid,)).fetchone()[0]
        self.assertEqual(self.all_visible(current)["reply"],expected)
        self.assertEqual((len(self.main.calls),len(self.gate.calls)),(1,0))

    async def test_invalid_gate_transport_uses_saved_main_without_second_main(self):
        self.gate.raw_override=' ' * 50;turn=await self.submit()
        self.assertEqual(turn['expression']['publication']['producer_kind'],'draft')
        result=self.all_visible(turn);self.assertEqual(result['reply'],'隐藏主稿。')
        self.assertEqual(len(self.main.calls),1);self.assertEqual(len(self.gate.calls),1)
        with self.store.connection() as db:self.assertIsNotNone(db.execute('SELECT raw_response FROM expression_runs').fetchone()[0])

    async def test_bad_segmentation_retains_valid_expression_not_main(self):
        self.gate.raw_override=json.dumps({'body':'有效表达稿。','segments':['不同的片段。'],'selected_expression_ids':[]},ensure_ascii=False)
        turn=await self.submit();self.assertEqual(turn['expression']['publication']['producer_kind'],'artifact')
        self.assertEqual(self.all_visible(turn)['reply'],'有效表达稿。')

    async def test_unknown_fallback_seals_main_and_late_result_is_evidence_only(self):
        self.gate.failure='provider_transport_interrupted';turn=await self.submit();self.assertEqual(turn['expression']['run']['state'],'unknown')
        self.assertIsNone(turn['expression']['run']['usage_json'])
        self.all_visible(turn);self.gate.callback(self.gate.raw())
        value=self.runtime.finalize(turn['expression']['run']['id']);self.assertTrue(value['result']['late_evidence_only'])
        self.assertEqual(self.chat.get(turn['id'])['reply'],'隐藏主稿。')
        await self.chat.resume_expression(turn['id']);self.assertEqual(len(self.main.calls),1);self.assertEqual(len(self.gate.calls),1)

    async def test_duplicate_ack_and_conflicting_ack_do_not_duplicate_events(self):
        turn=await self.submit();offered,receipt,_=self.acknowledge(turn,0)
        again=self.publisher.acknowledge(self.pid,turn['session_id'],turn['id'],offered['id'],offered['lease_id'],'fixture-presenter',receipt,offered['body_hash'])
        self.assertTrue(again['already_applied'])
        with self.assertRaises(ExpressionAssetError):self.publisher.acknowledge(self.pid,turn['session_id'],turn['id'],offered['id'],offered['lease_id'],'other',identifier(),offered['body_hash'])
        self.acknowledge(turn,1)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM expression_utterances').fetchone()[0],2)
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE role='persona'").fetchone()[0],1)

    async def test_unknown_presentation_cannot_reoffer_or_fallback_whole_draft(self):
        turn=await self.submit();offered,receipt,_=self.acknowledge(turn,0,state='unknown')
        plan=self.publisher.plan(self.pid,turn['session_id'],turn['id'])
        result=self.publisher.offer(self.pid,turn['session_id'],turn['id'],plan['id'],plan['revision'],0,'fixture-presenter')
        self.assertNotIn('body',result)
        self.assertEqual(self.publisher.fallback(turn['id'],'operator-request')['state'],'blocked')
        self.publisher.acknowledge(self.pid,turn['session_id'],turn['id'],offered['id'],offered['lease_id'],'fixture-presenter',identifier(),offered['body_hash'],'presented')
        self.acknowledge(turn,1)
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM expression_presentation_receipts').fetchone()[0],3)

    async def test_role_switch_and_order_gap_block_text_release(self):
        turn=await self.submit();plan=self.publisher.plan(self.pid,turn['session_id'],turn['id'])
        with self.assertRaisesRegex(ExpressionAssetError,'order_gap'):self.publisher.offer(self.pid,turn['session_id'],turn['id'],plan['id'],1,1,'fixture')
        other=self.service.create_persona(generic_persona())['id'];self.service.use_persona(other)
        with self.assertRaisesRegex(ExpressionAssetError,'not_selected'):self.publisher.offer(self.pid,turn['session_id'],turn['id'],plan['id'],1,0,'fixture')
        self.service.use_persona(self.pid);self.publisher.cancel(turn['id'])

    async def test_restart_preserves_pending_plan_and_no_model_call_replay(self):
        turn=await self.submit();self.runtime.recover();await self.chat.resume_expression(turn['id'])
        self.assertEqual(len(self.main.calls),1);self.assertEqual(len(self.gate.calls),1)
        self.assertEqual(self.all_visible(turn)['reply'],self.gate.body)

    async def test_restarted_presenter_can_reconcile_original_lease_without_receiving_body_twice(self):
        turn=await self.submit();plan=self.publisher.plan(self.pid,turn['session_id'],turn['id'])
        offer=self.publisher.offer(self.pid,turn['session_id'],turn['id'],plan['id'],1,0,'durable-presenter')
        self.runtime.recover();reopened=ExpressionPublication(self.runtime)
        pending=reopened.pending_receipts(self.pid,turn['session_id'],turn['id'],'durable-presenter')
        self.assertEqual(len(pending),1);self.assertEqual(pending[0]['lease_id'],offer['lease_id'])
        self.assertNotIn('body',pending[0]);self.assertNotIn('text',pending[0])
        self.assertEqual(reopened.pending_receipts(self.pid,turn['session_id'],turn['id'],'other-presenter'),[])
        reopened.acknowledge(self.pid,turn['session_id'],turn['id'],pending[0]['id'],pending[0]['lease_id'],'durable-presenter',identifier(),pending[0]['body_hash'],'not_presented')
        self.acknowledge(turn,1);self.assertEqual(self.chat.get(turn['id'])['reply'],self.gate.parts[1])
        self.assertEqual(len(self.main.calls),1);self.assertEqual(len(self.gate.calls),1)

    async def test_resume_after_main_local_handler_crash_does_not_regenerate_main(self):
        original=self.runtime.execute
        async def stopped(*args):raise RuntimeError('local stage stopped')
        self.runtime.execute=stopped;turn=await self.submit();self.assertEqual(turn['status'],'failed')
        self.runtime.execute=original;self.chat.recover();await self.chat.resume_expression(turn['id'])
        self.assertEqual(len(self.main.calls),1);self.assertEqual(len(self.gate.calls),1)
        self.assertEqual(self.all_visible(turn)['reply'],self.gate.body)

    async def test_persistence_failure_does_not_retry_paid_main_or_call_expression(self):
        with patch.object(self.runtime,'save_main',side_effect=ExpressionAssetError('main_draft_persistence_failed')):
            turn=await self.submit()
        self.assertEqual(turn['status'],'failed');self.assertEqual(len(self.main.calls),1);self.assertEqual(len(self.gate.calls),0)
        with self.assertRaises(Exception):await self.chat.retry(turn['id'])

    async def test_complete_main_wins_cancel_race_and_is_retained_without_expression(self):
        self.main.gate=asyncio.Event();self.main.cancel_receipt=True
        turn=await self.chat.submit('desktop','先等一下。',identifier());await self.main.started.wait();await self.chat.cancel(turn['id'])
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT body FROM expression_main_receipts').fetchone()[0],'隐藏主稿。')
        self.assertEqual(len(self.main.calls),1);self.assertEqual(len(self.gate.calls),0)
        with self.assertRaises(Exception):await self.chat.retry(turn['id'])

    async def test_cancel_expression_records_unknown_and_never_publishes_main(self):
        self.gate.gate=asyncio.Event();turn=await self.chat.submit('desktop','先说一句。',identifier());await self.gate.started.wait();await self.chat.cancel(turn['id'])
        result=self.chat.get(turn['id']);self.assertIsNone(result['reply']);self.assertEqual(result['status'],'interrupted')
        await self.chat.resume_expression(turn['id']);self.assertEqual(len(self.main.calls),1);self.assertEqual(len(self.gate.calls),1)

    async def test_new_purpose_denial_degrades_main_without_disclosing_expression_input(self):
        from persona_runtime.private_personas import guard_cloud_dispatch as actual
        def guarded(store,pid,kind,model):
            if kind=='expression':raise ExpressionAssetError('private_cloud_provider_not_authorized')
            return actual(store,pid,kind,model)
        with patch('persona_runtime.private_personas.guard_cloud_dispatch',side_effect=guarded):turn=await self.submit()
        self.assertEqual(len(self.gate.calls),0);self.assertEqual(self.all_visible(turn)['reply'],'隐藏主稿。')
        await self.chat.resume_expression(turn['id']);self.assertEqual(len(self.gate.calls),0)

    def seed_asset(self):
        assets=ExpressionAssets(self.store);assets.configure(self.pid,{'enabled':True},0,identifier());selected=scope(self.pid,'desktop')
        source=assets.register_source(self.pid,selected,kind='authored_examples',parts=[{'kind':'text','text':'对方累了，用轻轻的语气接话。'}],attribution='synthetic fixture',operation_id=identifier())
        assets.put(self.pid,selected,situation='很累想休息',style='轻轻接话',keywords=['累'],source_refs=[{'source_id':source['id'],'revision':1}],operation_id=identifier())
        return assets,selected,source

    async def test_asset_revoked_in_flight_falls_back_only_to_authorized_main(self):
        assets,selected,source=self.seed_asset();self.gate.gate=asyncio.Event()
        turn=await self.chat.submit('desktop','很累想休息。',identifier());await self.gate.started.wait()
        assets.revise_source(self.pid,selected,source['id'],expected_revision=1,permissions=['use'],revoke=True,operation_id=identifier())
        self.gate.gate.set();await self.chat.wait();value=self.chat.get(turn['id']);self.assertEqual(value['expression']['publication']['producer_kind'],'draft')
        self.assertEqual(self.all_visible(value)['reply'],'隐藏主稿。')

    async def test_asset_revoked_before_offer_swaps_unoffered_plan_once(self):
        assets,selected,source=self.seed_asset();turn=await self.submit('很累想休息。');old=self.publisher.plan(self.pid,turn['session_id'],turn['id'])
        assets.revise_source(self.pid,selected,source['id'],expected_revision=1,permissions=['use'],revoke=True,operation_id=identifier())
        offered=self.publisher.offer(self.pid,turn['session_id'],turn['id'],old['id'],old['revision'],0,'fixture')
        self.assertEqual(offered['state'],'plan_changed');new=self.publisher.plan(self.pid,turn['session_id'],turn['id']);self.assertEqual(new['revision'],2)
        self.assertEqual(self.all_visible(turn)['reply'],'隐藏主稿。')

    async def test_disabled_at_acceptance_does_not_add_expression_after_enable(self):
        current=self.runtime.policy(self.pid);self.runtime.configure(self.pid,{'enabled':False},current['revision'],identifier())
        turn=await self.chat.submit('desktop','旧方式。',identifier());current=self.runtime.policy(self.pid);self.runtime.configure(self.pid,{'enabled':True},current['revision'],identifier())
        await self.chat.wait();self.assertEqual(self.chat.get(turn['id'])['reply'],'隐藏主稿。');self.assertEqual(len(self.gate.calls),0)

    async def test_protected_code_loss_is_unusable_and_saved_main_stays_exact(self):
        async def code(model,messages):return '```python\nprint("好")\n```',{'prompt_tokens':10,'completion_tokens':10}
        self.main.complete=code;turn=await self.submit('给出完整代码。')
        self.assertEqual(turn['expression']['publication']['producer_kind'],'draft')
        self.assertEqual(self.all_visible(turn)['reply'],'```python\nprint("好")\n```')

    async def test_received_result_can_recover_after_local_expression_processing_failure(self):
        self.gate.after_receipt=True;turn=await self.submit()
        self.assertEqual(turn['expression']['run']['state'],'completed');self.assertEqual(len(self.gate.calls),1)
        self.all_visible(turn)

    async def seed_memory(self):
        from persona_runtime.models import LOCAL_USER_ID
        source=self.all_visible(await self.submit('我喜欢山岚乌龙茶。'))
        mid=identifier()
        with self.store.connection(write=True) as db:
            db.execute('INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (mid,self.pid,LOCAL_USER_ID,'preference','我喜欢山岚乌龙茶。',source['user_event_id'],mid,None,1,'active',source['created_at'],source['created_at']))
        from persona_runtime.recall_runtime import RecallRuntime
        recall=RecallRuntime(self.store);recall.update(self.pid,True,0,identifier())
        for _ in range(20):
            recall.maintain(self.pid,pages=3)
            if not recall.status(self.pid)['dirty_jobs']:break
        return mid,recall

    async def test_main_source_withdrawal_during_expression_blocks_both_outputs(self):
        mid,recall=await self.seed_memory();self.gate.started.clear();self.gate.gate=asyncio.Event()
        turn=await self.chat.submit('desktop','山岚乌龙茶，我喜欢哪种？',identifier());await asyncio.wait_for(self.gate.started.wait(),10)
        self.service.retract_memory(mid);self.gate.gate.set();await self.chat.wait()
        result=self.chat.get(turn['id']);self.assertIsNone(result['reply']);self.assertEqual(result['status'],'failed')
        self.assertIsNone(self.publisher.plan(self.pid,turn['session_id'],turn['id']))
        self.assertEqual(self.publisher.fallback(turn['id'],'source_withdrawn')['state'],'blocked')
        await self.chat.resume_expression(turn['id']);self.assertEqual(len(self.main.calls),2);self.assertEqual(len(self.gate.calls),2)
        recall.feedback()
        with self.store.connection() as db:self.assertFalse(db.execute('SELECT 1 FROM recall_support_events WHERE root_id=?',(turn['id'],)).fetchone())

    async def test_recall_consumption_is_once_for_main_root_after_visible_close(self):
        _,recall=await self.seed_memory();turn=await self.submit('山岚乌龙茶，我喜欢什么茶？')
        with self.store.connection() as db:
            details=json.loads(db.execute('SELECT details_json FROM context_uses WHERE turn_id=?',(turn['id'],)).fetchone()[0])
            self.assertTrue(details['recall_support']['selected_ids'])
            self.assertFalse(db.execute('SELECT 1 FROM recall_runtime_feedback WHERE turn_id=?',(turn['id'],)).fetchone())
        self.acknowledge(turn,0);recall.feedback()
        with self.store.connection() as db:self.assertFalse(db.execute('SELECT 1 FROM recall_support_events WHERE root_id=?',(turn['id'],)).fetchone())
        self.acknowledge(turn,1);recall.feedback()
        with self.store.connection() as db:
            rows=[tuple(r) for r in db.execute('SELECT unit_id,root_id,alpha FROM recall_support_events WHERE root_id=?',(turn['id'],))]
            self.assertTrue(rows);self.assertEqual(len(rows),len({r[0] for r in rows}))
            self.assertEqual(db.execute('SELECT count(*) FROM recall_runtime_feedback WHERE turn_id=?',(turn['id'],)).fetchone()[0],1)
        self.runtime.recover();recall.feedback()
        with self.store.connection() as db:self.assertEqual([tuple(r) for r in db.execute('SELECT unit_id,root_id,alpha FROM recall_support_events WHERE root_id=?',(turn['id'],))],rows)

    async def test_disable_during_expression_keeps_raw_receipt_and_saved_main(self):
        self.gate.gate=asyncio.Event();turn=await self.chat.submit('desktop','继续。',identifier());await self.gate.started.wait()
        current=self.runtime.policy(self.pid);self.runtime.configure(self.pid,{'enabled':False},current['revision'],identifier())
        self.gate.gate.set();await self.chat.wait();value=self.chat.get(turn['id'])
        self.assertEqual(value['expression']['publication']['producer_kind'],'draft')
        self.assertEqual(self.all_visible(value)['reply'],'隐藏主稿。')
        with self.store.connection() as db:self.assertIsNotNone(db.execute('SELECT raw_response FROM expression_runs').fetchone()[0])

    async def test_disable_after_partial_does_not_restart_whole_draft(self):
        turn=await self.submit();self.acknowledge(turn,0);current=self.runtime.policy(self.pid)
        self.runtime.configure(self.pid,{'enabled':False},current['revision'],identifier())
        plan=self.publisher.plan(self.pid,turn['session_id'],turn['id'])
        stopped=self.publisher.offer(self.pid,turn['session_id'],turn['id'],plan['id'],plan['revision'],1,'fixture')
        self.assertEqual(stopped['state'],'stopped');self.assertNotIn('body',stopped)
        self.assertEqual(self.chat.get(turn['id'])['reply'],self.gate.parts[0])
        self.assertEqual(self.publisher.fallback(turn['id'],'disabled')['state'],'blocked')

    async def test_permission_change_after_offer_keeps_actual_ack_and_cancels_tail(self):
        turn=await self.submit();plan=self.publisher.plan(self.pid,turn['session_id'],turn['id'])
        offered=self.publisher.offer(self.pid,turn['session_id'],turn['id'],plan['id'],1,0,'fixture')
        with patch.object(self.runtime,'_validate_main',side_effect=ExpressionAssetError('permission_changed')):
            stopped=self.publisher.offer(self.pid,turn['session_id'],turn['id'],plan['id'],1,1,'fixture')
        self.assertEqual(stopped['state'],'stopped')
        self.publisher.acknowledge(self.pid,turn['session_id'],turn['id'],offered['id'],offered['lease_id'],'fixture',identifier(),offered['body_hash'])
        self.assertEqual(self.chat.get(turn['id'])['reply'],self.gate.parts[0])
        self.assertEqual(self.publisher.plan(self.pid,turn['session_id'],turn['id'])['state'],'closed')

    async def test_migration_pause_projects_only_confirmed_prefix_and_closes_unknown(self):
        from persona_runtime.expression_compat import pause
        turn=await self.submit();self.acknowledge(turn,0);plan=self.publisher.plan(self.pid,turn['session_id'],turn['id'])
        last=self.publisher.offer(self.pid,turn['session_id'],turn['id'],plan['id'],1,1,'fixture')
        with self.store.connection(write=True) as db:
            before=[tuple(r) for r in db.execute('SELECT raw_text,body,usage_json FROM expression_main_receipts')]
            raw=bytes(db.execute('SELECT raw_response FROM expression_runs').fetchone()[0]);pause(db)
            self.assertEqual([tuple(r) for r in db.execute('SELECT raw_text,body,usage_json FROM expression_main_receipts')],before)
            self.assertEqual(bytes(db.execute('SELECT raw_response FROM expression_runs').fetchone()[0]),raw)
        self.assertEqual(self.chat.get(turn['id'])['reply'],self.gate.parts[0]);self.assertFalse(self.runtime.policy(self.pid)['data']['enabled'])
        with self.assertRaisesRegex(ExpressionAssetError,'closed'):
            self.publisher.acknowledge(self.pid,turn['session_id'],turn['id'],last['id'],last['lease_id'],'fixture',identifier(),last['body_hash'])
        self.runtime.recover();await self.chat.resume_expression(turn['id'])
        self.assertEqual(len(self.main.calls),1);self.assertEqual(len(self.gate.calls),1)

    async def test_compatible_snapshot_refuses_unsettled_presentation(self):
        from persona_runtime.recall_rollback import prepare_snapshot
        turn=await self.submit()
        with self.assertRaisesRegex(ValueError,'pending_presentation'):prepare_snapshot(self.paths,target_schema=30)
        self.all_visible(turn);result=prepare_snapshot(self.paths,target_schema=30)
        self.assertEqual(result['target_schema'],30);self.assertFalse(result['original_database_replaced'])

    async def test_management_api_is_admin_only_and_never_releases_draft_as_status(self):
        from aiohttp import ClientSession
        from aiohttp.test_utils import TestServer
        from persona_runtime.server import create_app
        turn=await self.submit();app=create_app(self.paths,self.main);app['engine'].expression=self.runtime
        async with TestServer(app) as server,ClientSession() as client:
            async def manage(action,args,role='admin',status=200):
                headers={'Authorization':'Bearer '+self.cfg.token('bridge' if role=='bridge' else 'admin')}
                async with client.post(server.make_url('/v1/manage'),headers=headers,json={'action':action,'args':args}) as response:
                    data=await response.json();self.assertEqual(response.status,status,data);return data
            await manage('expression.status',{'turn_id':turn['id']},'bridge',403)
            result=await manage('expression.status',{'turn_id':turn['id']})
            self.assertNotIn('隐藏主稿',json.dumps(result,ensure_ascii=False))
            args={'persona_id':self.pid,'session_id':turn['session_id'],'turn_id':turn['id']}
            plan=await manage('expression.presentation.plan',args)
            self.assertNotIn('text',json.dumps(plan));self.assertNotIn('body',json.dumps(plan))
            await manage('expression.presentation.plan',dict(args,session_id=identifier()),status=400)
            offered=await manage('expression.presentation.offer',args|{'publication_id':plan['id'],'revision':plan['revision'],'ordinal':0,'presenter_id':'api-fixture'})
            pending=await manage('expression.presentation.pending_receipts',args|{'presenter_id':'api-fixture'})
            self.assertEqual(pending[0]['lease_id'],offered['lease_id']);self.assertNotIn('body',pending[0])
            await manage('expression.presentation.ack',args|{'utterance_id':offered['id'],'lease_id':offered['lease_id'],'presenter_id':'api-fixture','receipt_id':identifier(),'body_hash':offered['body_hash']})
            self.assertEqual(len(await manage('expression.presentation.visible',args)),1)
            await manage('expression.cancel',{'turn_id':turn['id']})
        self.assertEqual(self.chat.get(turn['id'])['reply'],self.gate.parts[0])


class PromotionParity(unittest.TestCase):
    def test_frozen_e1_materials_have_identical_prompts_selection_and_decoding(self):
        from tests.test_expression_e1 import FIXTURES,material
        from experiments.expression_e1.repair_protocol import build_messages as before,decode_output_for as decode_before,JSON_PROTOCOL
        from persona_runtime.expression_realizer import build_messages as after,decode_output_for as decode_after
        for index in range(len(FIXTURES['cases'])):
            item=material(index)
            with self.subTest(case=index):
                old=before(item,FIXTURES,JSON_PROTOCOL);new=after(item,FIXTURES,JSON_PROTOCOL);self.assertEqual(old,new)
                raw=json.dumps({'body':item['draft'],'segments':'no_split','selected_expression_ids':[]},ensure_ascii=False)
                self.assertEqual(decode_before(JSON_PROTOCOL,raw,item,old[1]),decode_after(JSON_PROTOCOL,raw,item,new[1]))


if __name__=='__main__':unittest.main()
