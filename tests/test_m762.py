"""Assist is process admitted, source bound, and audited before any generation."""
import json
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_m761 as fixtures
from persona_runtime.archive import fingerprint
from persona_runtime.decision_capsule import AssistAdmission, capsule, VERSION
from persona_runtime.models import identifier, RuntimeErrorBase


class M762(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.M761.asyncSetUp
    asyncTearDown=fixtures.M761.asyncTearDown
    source=fixtures.M761.source
    document=fixtures.M761.document
    publish=fixtures.M761.publish
    shadow_on=fixtures.M761.shadow_on

    def admit(self):
        self.engine.assist_admission=AssistAdmission(self.paths.home,self.config.load()['instance_id'],
                                                   frozenset((self.pid,)),self.paths.home/'absent-formal')

    def mode(self,mode):
        p=self.ingress.current_policy(self.pid,'desktop')
        return self.ingress.update_policy(self.pid,'desktop',p['mode'],p['id'],identifier(),disposition_mode=mode)

    async def queued(self,text='我想找人倾诉'):
        with patch.object(self.engine,'_schedule'):
            row=await self.engine.submit('desktop',text,identifier(),persona_id=self.pid)
        return row['id']

    async def test_no_grant_and_wrong_scope_rejected(self):
        with self.assertRaisesRegex(RuntimeErrorBase,'assist_not_admitted'):self.mode('assist')
        self.admit();self.engine.assist_admission=AssistAdmission(self.paths.home,'other',frozenset((self.pid,)),self.paths.home/'absent')
        with self.assertRaisesRegex(RuntimeErrorBase,'assist_not_admitted'):self.mode('assist')
        self.admit();self.engine.assist_admission=AssistAdmission(self.paths.home,self.config.load()['instance_id'],frozenset((self.pid,)),self.paths.home)
        with self.assertRaisesRegex(RuntimeErrorBase,'assist_not_admitted'):self.mode('assist')

    async def test_assist_real_call_payload_and_body_free_audit(self):
        self.publish();self.admit();self.mode('assist')
        row=await self.engine.submit('desktop','我想找人倾诉',identifier(),persona_id=self.pid);await self.engine.wait()
        self.assertEqual(self.engine.get(row['id'])['status'],'completed')
        self.assertEqual(len(self.fake.messages),1)
        self.assertIn(VERSION,self.fake.messages[0][0]['content'])
        with self.store.connection() as db:
            manifest=json.loads(db.execute('SELECT manifest_json FROM context_manifests WHERE turn_id=?',(row['id'],)).fetchone()[0])
            record=db.execute('SELECT record_json FROM disposition_decisions WHERE turn_id=?',(row['id'],)).fetchone()[0]
        self.assertEqual(manifest['input_snapshot_hash'],fingerprint(self.fake.messages[0]))
        self.assertTrue(manifest['shadow']['applied_to_prompt'])
        self.assertNotIn('先简短回应用户',record)
        self.assertNotIn('我想找人倾诉',record)

    async def test_withdraw_after_prepare_removes_capsule(self):
        h=self.publish();self.admit();self.mode('assist');tid=await self.queued()
        prepared=self.engine.cognitive.prepare(tid);self.assertIn(VERSION,prepared['messages'][0]['content'])
        self.dispositions.withdraw(self.pid,h['active_id'],h['revision'],identifier())
        with self.store.connection(write=True) as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
            final=self.engine.cognitive.finalize(db,turn,prepared)
        self.assertNotIn(VERSION,final['messages'][0]['content'])
        self.assertFalse(final['manifest']['decision_capsule']['applied_to_prompt'])
        self.assertIsNone(final['manifest']['decision_capsule']['capsule_hash'])

    async def test_record_failure_blocks_assist_call_and_rolls_back_attempt(self):
        self.publish();self.admit();self.mode('assist')
        with self.store.connection(write=True) as db:
            db.execute("CREATE TRIGGER injected_failure BEFORE INSERT ON disposition_decisions BEGIN SELECT RAISE(ABORT,'injected'); END")
        row=await self.engine.submit('desktop','我想找人倾诉',identifier(),persona_id=self.pid);await self.engine.wait()
        self.assertEqual(self.fake.calls,[])
        self.assertEqual(self.engine.get(row['id'])['error_code'],'decision_record_storage_failed')

    async def test_baseline_shadow_messages_identical(self):
        self.publish();tid=await self.queued();base=self.engine.cognitive.prepare(tid)['messages']
        self.shadow_on();tid=await self.queued();shadow=self.engine.cognitive.prepare(tid)['messages']
        self.assertEqual(base,shadow)

    async def test_budget_fallback_preserves_complete_input(self):
        self.publish();self.admit();self.mode('assist');tid=await self.queued()
        prepared=self.engine.cognitive.prepare(tid)
        with self.store.connection(write=True) as db:
            row=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
            model=json.loads(row['model_snapshot']);model['max_input_chars']=sum(len(m['content']) for m in prepared['messages'])-1
            db.execute('UPDATE turns SET model_snapshot=? WHERE id=?',(json.dumps(model),tid))
        final=self.engine.cognitive.prepare(tid)
        self.assertNotIn(VERSION,final['messages'][0]['content'])
        self.assertEqual(final['manifest']['decision_capsule']['degrade_reason'],'capsule_budget_unavailable')
        self.assertIn('我想找人倾诉',final['messages'][-1]['content'])

    async def test_snapshot_renderer_corruption_and_admission_loss_fail(self):
        self.publish();self.admit();self.mode('assist');tid=await self.queued()
        with self.store.connection(write=True) as db:
            snap=json.loads(db.execute('SELECT context_policy_snapshot FROM turns WHERE id=?',(tid,)).fetchone()[0])
            snap['disposition']['renderer']='unsupported'
            db.execute('UPDATE turns SET context_policy_snapshot=? WHERE id=?',(json.dumps(snap),tid))
        with self.assertRaisesRegex(RuntimeErrorBase,'assist_snapshot_invalid'):self.engine.cognitive.prepare(tid)
        self.engine.assist_admission=None
        with self.assertRaisesRegex(RuntimeErrorBase,'assist_not_admitted'):self.engine.cognitive.prepare(tid)

    async def test_preview_readonly_and_unknown_no_capsule(self):
        self.publish();self.admit()
        result=self.engine.cognitive.shadow.preview(self.pid,['我想找人倾诉'],mode='assist')
        self.assertIn(VERSION,result['capsule_preview']);self.assertFalse(result['persisted'])
        result=self.engine.cognitive.shadow.preview(self.pid,['窗边飘着一片云'],mode='assist')
        self.assertEqual(result['capsule_preview'],'')
        with self.store.connection() as db:
            for table in ('turns','attempts','context_manifests','disposition_decisions'):
                self.assertEqual(db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)

    def test_fixed_templates_all_actions_and_limits(self):
        from persona_runtime.decision_capsule import TEMPLATES
        for action in TEMPLATES:
            value=capsule({'mode':'assist','status':'observed','record':{'activated':['a'],'chosen_action':action,'status':'selected'}})
            self.assertLessEqual(len(value),600)
            self.assertNotIn('silence',value)

    async def test_nonfirst_member_and_snapshot_frozen(self):
        self.publish();self.admit();p=self.mode('assist')
        p=self.ingress.update_policy(self.pid,'desktop','natural_batch',p['id'],identifier())
        a=self.ingress.accept({'text':'只是一条补充','request_id':identifier(),'persona_id':self.pid})
        b=self.ingress.accept({'text':'我想找人倾诉','request_id':identifier(),'persona_id':self.pid})
        self.assertEqual(a['batch_id'],b['batch_id'])
        batch=self.ingress.get(a['batch_id'])
        # Administrator seals atomically; no automatic model scheduling in this test.
        with patch.object(self.engine,'_schedule'):
            value=self.ingress.action(a['batch_id'],'seal',batch['revision'],identifier(),self.pid)
        tid=value['turn_id'];prepared=self.engine.cognitive.prepare(tid)
        self.assertEqual(prepared['manifest']['_shadow_prepared']['record']['chosen_action'],'support')
        self.assertIn(VERSION,prepared['messages'][0]['content'])
        self.mode('baseline')
        self.assertIn(VERSION,self.engine.cognitive.prepare(tid)['messages'][0]['content'])

    async def test_final_rebuild_uses_no_worker_and_keeps_capsule(self):
        self.publish();self.admit();self.mode('assist');tid=await self.queued()
        prepared=self.engine.cognitive.prepare(tid)
        # A changed optional-state guard must rebuild, not call the decision worker.
        prepared['guard']='stale'
        with patch.object(self.engine.cognitive.shadow,'prepare',side_effect=AssertionError('worker in write')):
            with self.store.connection(write=True) as db:
                turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
                value=self.engine.cognitive.finalize(db,turn,prepared)
        self.assertIn(VERSION,value['messages'][0]['content'])
        self.assertEqual(value['manifest']['input_snapshot_hash'],fingerprint(value['messages']))

    async def test_busy_falls_back_without_adding_call(self):
        from concurrent.futures import Future
        self.publish();self.admit();self.mode('assist');tid=await self.queued()
        self.engine.cognitive.shadow.future=Future()
        prepared=self.engine.cognitive.prepare(tid)
        self.assertNotIn(VERSION,prepared['messages'][0]['content'])
        self.assertEqual(prepared['manifest']['decision_capsule']['degrade_reason'],'busy')

    async def test_slow_admission_counts_in_deadline_and_late_result_unused(self):
        import time
        from persona_runtime.decision_capsule import require_admission
        self.publish();self.admit();self.mode('assist');tid=await self.queued()
        def delayed(engine,pid):
            time.sleep(.16);require_admission(engine,pid)
        with patch('persona_runtime.decision_capsule.require_admission',side_effect=delayed):
            prepared=self.engine.cognitive.prepare(tid)
        self.assertEqual(prepared['manifest']['decision_capsule']['degrade_reason'],'timeout')
        self.assertNotIn(VERSION,prepared['messages'][0]['content'])
        time.sleep(.03)
        self.assertNotIn(VERSION,prepared['messages'][0]['content'])

    async def test_manifest_failure_zero_calls(self):
        self.publish();self.admit();self.mode('assist')
        with self.store.connection(write=True) as db:
            db.execute("CREATE TRIGGER injected_manifest BEFORE INSERT ON context_manifests BEGIN SELECT RAISE(ABORT,'injected'); END")
        await self.engine.submit('desktop','我想找人倾诉',identifier(),persona_id=self.pid);await self.engine.wait()
        self.assertEqual(self.fake.calls,[])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM disposition_decisions').fetchone()[0],0)

    async def test_preview_does_not_admit_formal_or_create_grant(self):
        self.publish()
        with self.assertRaisesRegex(RuntimeErrorBase,'assist_not_admitted'):
            self.engine.cognitive.shadow.preview(self.pid,['我想找人倾诉'],mode='assist')
        with self.assertRaisesRegex(RuntimeErrorBase,'invalid_disposition_mode'):self.mode('live')

    async def test_known_fact_and_complete_code_not_rewritten(self):
        self.publish();self.admit();self.mode('assist')
        text='请解释这段完整代码，不要修改：def add(a, b): return a + b。数字12.50，网址https://example.org/a?b=1'
        tid=await self.queued(text);value=self.engine.cognitive.prepare(tid)
        self.assertIn(text,value['messages'][-1]['content'])
        self.assertNotIn('support',json.dumps(value['manifest'].get('decision_capsule',{})))

    async def test_character_envelope_trims_example_preserves_whole_dependency_and_anchor(self):
        from persona_runtime.character_context import select_context,render_parts
        self.publish();self.admit();self.mode('assist');tid=await self.queued()
        with self.store.connection() as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
            selected=select_context(json.loads(turn['character_snapshot']),'我想找人倾诉')
            selected['parts']=[{'kind':'anchor','id':'identity','text':'身'*700,'required':False},
                {'kind':'lore_group','id':'root','text':json.dumps({'data':[{'id':'root','text':'背'*700},{'id':'dep','text':'景'*700}]},ensure_ascii=False),'required':False},
                {'kind':'example','id':'sample','text':'例'*570,'required':False}]
            text=capsule({'mode':'assist','status':'observed','record':{'activated':['a'],'chosen_action':'support','status':'selected'}})
            messages,details=self.engine.cognitive.dialogue.render(db,turn,None,selected,capsule=text)
        self.assertTrue(details['capsule_budget']['applied'])
        self.assertEqual(details['character']['example_ids'],[])
        self.assertEqual(details['character']['lore_ids'],['root','dep'])
        self.assertIn('身'*700,messages[0]['content'])
        self.assertLessEqual(details['character']['input_characters']+len(text),3000)

    async def test_trusted_fixture_gaps_render_four_restricted_actions_only_in_mock(self):
        from persona_runtime.disposition_core.contracts import validate_input
        from persona_runtime.disposition_core.engine import interpret,select_dispositions,decide
        fixture=json.loads((Path(__file__).resolve().parents[1]/'experiments/persona_decision/fixtures.json').read_text(encoding='utf-8'))['development'][0]['input']
        for action,flag in (('ask','information_gap'),('clarify','reference_gap'),('defer','resource_unavailable'),('set_boundary','boundary_source_id')):
            raw=deepcopy(fixture);raw['members']=[{'id':'m1','text':'给些建议'}]
            raw['context'][flag]=raw['sources'][0]['id'] if action=='set_boundary' else True
            raw['definitions'][0]['triggers']=[];raw['definitions'][0]['preferences']={action:2}
            checked=validate_input(raw);policy=self.engine.cognitive.shadow.policy
            frame=interpret(checked.members,checked.context,policy)
            record=decide(frame,select_dispositions(checked,frame),policy).to_dict()
            self.assertEqual(record['chosen_action'],action)
            text=capsule({'mode':'assist','status':'observed','record':record})
            await self.fake.complete({},[{'role':'system','content':text}])
            self.assertIn(action,self.fake.messages[-1][0]['content'])
