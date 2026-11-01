import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock,patch

from scripts import r3_provider240 as run
from scripts.r3_provider240_report import classification,report
from persona_runtime import generation_retry as retry
from tests.r3_prerequisite_fixture import fixture
from tests.r3_prerequisite_annotations import labels
from tests.r3_real_evaluation import evaluation
from scripts.r3_frozen_report import boundary_statistics,selection_direction
from scripts.r3_prerequisite_analysis import material_row
import json


class AttributionTests(unittest.TestCase):
    def assess(self,**changes):
        values=dict(complete=True,same=False,explained=True,changed=True,versions_valid=True,
                    metadata={k:[{'response_model':'grok-4.7','system_fingerprint':'backend'} for _ in range(2)] for k in ('selector','main')})
        values.update(changes);return classification(**values)
    def test_examples_only_all_metadata_known_can_be_analyzed(self):
        result=self.assess();self.assertTrue(result['eligible_for_directional_analysis']);self.assertFalse(result['effect_proven'])
    def test_identical_request_is_not_selector_benefit(self):
        self.assertFalse(self.assess(same=True,changed=False)['eligible_for_directional_analysis'])
    def test_missing_arm_wrong_version_and_confounds_excluded(self):
        for change in ({'complete':False},{'versions_valid':False},{'explained':False},{'changed':False}):
            with self.subTest(change=change):self.assertFalse(self.assess(**change)['eligible_for_directional_analysis'])
    def test_both_stages_model_and_backend_changes_and_missing_are_excluded(self):
        for stage in ('selector','main'):
            for field in ('response_model','system_fingerprint'):
                for value in (None,'other'):
                    metadata={k:[{'response_model':'grok-4.7','system_fingerprint':'backend'} for _ in range(2)] for k in ('selector','main')}
                    metadata[stage][1][field]=value
                    self.assertFalse(self.assess(metadata=metadata)['eligible_for_directional_analysis'])
    def test_only_credentials_removed_from_public_snapshots(self):
        value={'encrypted_key':'secret','name':'grok-4.7','timeout':180,'nested':[{'encrypted_key':'secret','max_tokens':8}]}
        self.assertEqual(run.public(value),{'name':'grok-4.7','timeout':180,'nested':[{'max_tokens':8}]})
        self.assertEqual(value['encrypted_key'],'secret')

    def test_legal_lore_difference_kept_but_not_expression_causal_evidence(self):
        def request(lore,anchor='人类'):
            body=json.dumps({'kind':'anchor','id':'identity.species','data':anchor},ensure_ascii=False)+'\n'+json.dumps({'kind':'lore_group','id':'catalogue','data':lore},ensure_ascii=False)
            manifest={'prompt_assembly':{'blocks':[{'name':'character','start_character':0,'characters':len(body)}]}}
            return {'messages':[{'role':'system','content':body}],'temperature':.5},manifest
        a,ma=request([]);b,mb=request([{'id':'catalogue','title':'目录','text':'角色资料'}])
        self.assertTrue(run.compare_common(a,ma,b,mb))
        self.assertFalse(self.assess(explained=False)['eligible_for_directional_analysis'])
        c,mc=request([],anchor='变更事实');self.assertFalse(run.compare_common(a,ma,c,mc))
        b['temperature']=.6;self.assertFalse(run.compare_common(a,ma,b,mb))


class EvaluationTests(unittest.TestCase):
    def test_frozen_strata_reference_original_labels_only(self):
        original=labels();before=deepcopy(original);e=evaluation(fixture(),original)
        self.assertEqual(original,before);self.assertFalse(e['labels_changed'])
        self.assertEqual(e['denominators_per_arm'],{'requests':24,'applicable_candidates':22,'candidate_missing_only_applicable':1,'no_applicable_material':1})
        self.assertTrue(e['premise_conflicts']);self.assertEqual(len(e['case_boundaries']),12)

    def row(self,case,role,arm,included):
        data=fixture();pool={'payload':{'roots':{'example':['e1','e2','e3','e4','e5','e6']},
          'sources':[r|{'kind':'example'} for r in data['characters'][role]['examples']]}}
        item={'key':case+'-'+role+'-'+arm,'case':case,'role':role,'arm':arm}
        observed={'returned_example_ids':included,'included_example_ids':included,'lore_ids':[],
          'applied_prompt':run.ARMS[arm],'effective_mode':'hybrid_llm'}
        return material_row(item,pool,observed,labels())

    def test_empty_everything_cannot_pass_by_eliminating_errors(self):
        e=evaluation(fixture(),labels())
        rows=[self.row('P01','detail','control',['e1','e3']),self.row('P01','detail','experiment',[])]
        result=selection_direction(rows,e,True)
        self.assertTrue(result['checks']['premise_conflict_reduced'])
        self.assertFalse(result['checks']['applicable_not_decreased']);self.assertFalse(result['checks']['no_preservation_case_loss'])
        self.assertFalse(result['numerical_conditions_met'])

    def test_whole_completion_and_local_completion_have_distinct_strata(self):
        e=evaluation(fixture(),labels())
        rows=[self.row('P07','detail','control',['e2']),self.row('P07','stance','experiment',['e2'])]
        value=boundary_statistics(rows,e)
        self.assertEqual(value['control']['premise_conflict_included_requests'],0)
        self.assertEqual(value['experiment']['premise_conflict_included_requests'],1)

    def test_no_observed_old_error_is_not_an_improvement(self):
        e=evaluation(fixture(),labels());rows=[self.row('P01','detail',arm,['e1']) for arm in run.ARMS]
        value=selection_direction(rows,e,True)
        self.assertFalse(value['checks']['premise_conflict_reduced']);self.assertFalse(value['numerical_conditions_met'])

    def test_incomplete_pairs_cannot_pass(self):
        e=evaluation(fixture(),labels());rows=[self.row('P01','detail','control',['e1','e3']),self.row('P01','detail','experiment',['e1'])]
        self.assertFalse(selection_direction(rows,e,False)['numerical_conditions_met'])


class LedgerTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name);self.ledger=run.Ledger(self.root/'receipts')
    def reserve(self,slot='a',request=None):return self.ledger.reserve_attempt(slot,{'request':request or {'messages':['人工']}})
    def test_total_349_plus_35_and_four_per_position(self):
        for position in range(349):self.ledger.finish(self.reserve(str(position)),'completed')
        self.assertEqual(len(self.ledger.rows()),349)
        with self.assertRaises(run.StopRun):self.reserve('new')
    def test_exhaustion_blocks_other_positions_too(self):
        for _ in range(4):self.ledger.finish(self.reserve(),'failed')
        with self.assertRaises(run.StopRun):self.reserve('new')
    def test_success_no_replay_and_same_retry_request(self):
        self.ledger.finish(self.reserve(),'failed')
        with self.assertRaises(run.StopRun):self.reserve(request={'messages':['更改']})
        self.ledger.finish(self.reserve(),'completed')
        with self.assertRaises(run.StopRun):self.reserve()
    def test_reserved_interrupted_or_long_failed_call_blocks_next_position(self):
        for status in ('reserved','interrupted','long'):
            self.ledger=run.Ledger(self.root/status);r=self.reserve()
            if status!='reserved':self.ledger.finish(r,'failed' if status=='long' else status,seconds=601 if status=='long' else 0)
            with self.assertRaises(run.StopRun):self.reserve('next')
    def test_mixed_real_simulated_receipts_rejected(self):
        row=self.ledger.reserve_attempt('a',{'request':{},'real':False});self.ledger.finish(row,'completed')
        with patch('scripts.r3_provider240_report.verify_freeze',return_value={'real':True,'items':[]}):
            with self.assertRaisesRegex(run.StopRun,'mixed_real_simulation'):report(self.root,combined=False)
    def test_existing_receipts_never_resume(self):
        with patch.object(run,'protected'),patch.object(run,'verify_freeze',return_value={'real':False}):
            with self.assertRaisesRegex(run.StopRun,'existing_receipts'):asyncio.run(run.execute(self.root,False))


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.ledger=run.Ledger(self.root/'receipts');self.messages=[{'role':'user','content':'人工测试'}]
        self.model={'name':'grok-4.7','base_url':'https://api.uniapi.io/v1','max_tokens':512,'timeout':60,'_character_selection':True}
        self.pool={'payload':{'roots':{'lore':[],'example':['valid_but_inapplicable']}}}
        self.path=self.root/'candidate.json';run.write_once(self.path,self.pool)
        self.item={'key':'artificial','selector_messages':self.messages,'candidate_file':str(self.path)}
        self.configfile=self.root/'config.json';self.configfile.write_text('{}')
        self.item['configuration_hash']=run.sha(self.configfile)
        from types import SimpleNamespace
        self.engine=SimpleNamespace(config=SimpleNamespace(file=self.configfile))
        self.client=AsyncMock();self.capture=run.Capture(self.root,self.item,self.engine,self.ledger,self.client,False)
        p=patch.object(run,'check_item');p.start();self.addCleanup(p.stop)
    async def dispatch(self):
        self.capture.preflight(self.model,self.messages)
        return await self.capture.complete(self.model,self.messages)
    def success(self):return run.ModelCompletion('{"schema_version":1,"lore_ids":[],"example_ids":["valid_but_inapplicable"]}',{'total_tokens':2},{'finish_reason':'stop'})
    async def test_valid_but_inapplicable_selection_kept_without_retry(self):
        self.client.complete.return_value=self.success()
        result=await self.dispatch()
        self.assertIn('valid_but_inapplicable',result[0]);self.assertEqual(self.client.complete.await_count,1)
        self.assertEqual(self.ledger.rows()[0]['status'],'completed')
    async def test_timeout_terminal_then_runtime_recovers_without_collector_retry(self):
        self.client.complete.side_effect=[asyncio.CancelledError('generation_attempt_timeout'),self.success()]
        with self.assertRaises(asyncio.CancelledError):await self.dispatch()
        self.assertEqual(self.ledger.check()[0]['error'],'selector_timeout')
        await self.dispatch()
        self.assertEqual([r['attempt_index'] for r in self.ledger.rows()],[0,1])
    async def test_user_cancellation_terminal_prevents_new_call(self):
        self.client.complete.side_effect=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):await self.dispatch()
        with self.assertRaises(run.StopRun):await self.dispatch()
        self.assertEqual(self.client.complete.await_count,1)
    async def test_reservation_failure_prevents_network(self):
        with patch('scripts.grok47_r1_retry.write_once',side_effect=OSError('disk')):
            with self.assertRaises(run.StopRun):await self.dispatch()
        self.client.complete.assert_not_awaited()
    async def test_terminal_failure_leaves_reservation_and_prevents_replay(self):
        self.client.complete.return_value=self.success()
        with patch('scripts.retry_expression_selection_r1.write_once',side_effect=OSError('disk')):
            with self.assertRaises(run.StopRun):await self.dispatch()
        with self.assertRaises(run.StopRun):await self.dispatch()
        self.assertEqual(self.client.complete.await_count,1)
    async def test_runtime_four_attempts_and_one_two_four_delays(self):
        self.client.complete.side_effect=run.ModelError('provider_empty_response');saved=[];sleep=AsyncMock()
        with self.assertRaises(retry.GenerationRetriesExhausted):
            await run.guarded_complete(self.capture,self.model,self.messages,validate=lambda _:None,save=lambda s:saved.append(deepcopy(s)),before_attempt=lambda _:None,sleep=sleep)
        self.assertEqual(self.client.complete.await_count,4)
        self.assertEqual([c.args[0] for c in sleep.await_args_list],[1,2,4])
        self.assertEqual(len(self.ledger.rows()),4)
    async def test_runtime_success_does_not_replay_selector_when_main_retries(self):
        self.client.complete.return_value=self.success()
        await run.guarded_complete(self.capture,self.model,self.messages,validate=lambda _:None,save=lambda _:None,before_attempt=lambda _:None)
        main=AsyncMock();main.complete.side_effect=[run.ModelError('provider_timeout'),run.ModelCompletion('内容质量差但有效',{'total_tokens':1},{})]
        await retry.audited_complete(main,self.model,self.messages,validate=lambda _:None,save=lambda _:None,before_attempt=lambda _:None,sleep=AsyncMock())
        self.assertEqual(self.client.complete.await_count,1);self.assertEqual(main.complete.await_count,2)
    async def test_offline_context_refuses_provider(self):
        with run.offline():
            with self.assertRaises(run.StopRun):await run.ChatCompletionsClient().complete(self.model,self.messages)




from tests import test_m754r2 as selector_fixture
from persona_runtime.config import ConfigStore,DEFAULTS,SELECTOR_TIMEOUT_MAX
from persona_runtime.models import RuntimeErrorBase
import time

class TimeoutConfigurationTests(unittest.TestCase):
    def test_240_accepted_241_rejected_and_legacy_60_kept(self):
        value=deepcopy(DEFAULTS)
        self.assertEqual(value['character_selector']['timeout'],60)
        self.assertEqual(SELECTOR_TIMEOUT_MAX,240)
        value['character_selector']['timeout']=240;ConfigStore.validate(value)
        for bad in (241,0,True,240.0):
            value['character_selector']['timeout']=bad
            with self.assertRaises(RuntimeErrorBase):ConfigStore.validate(value)
    def test_remaining_order_has_34_and_no_old_success_replay(self):
        keys=run.remaining_keys()
        self.assertEqual(len(keys),34);self.assertEqual(len(set(keys)),34)
        self.assertEqual(keys[0],'P04-stance-experiment')
        old=run.read(run.OLD/'real/prepared.json')['items']
        self.assertEqual(keys,[i['key'] for i in old if i['key'] not in {p.stem for p in (run.OLD/'real/results').glob('*.json') if run.read(p)['turn']['status']=='completed'}])
        self.assertEqual(run.PRIOR_CALLS+run.LIMIT,384)
    def test_only_authorized_snapshot_fields_change(self):
        old={'base_url':'https://old.test/v1','name':'grok-4.7','timeout':60,'max_tokens':512,'encrypted_key':'old'}
        new=old|{'base_url':'https://new.test/v1','timeout':240,'encrypted_key':'new'}
        with patch.object(run,'read',return_value={'new_public_provider':{'character_selector':new}}):
            self.assertTrue(run.compare_public_snapshot('selector_model_snapshot',json.dumps(new),json.dumps(old)))
            self.assertTrue(run.compare_public_snapshot('observation_model_snapshot',json.dumps(new),json.dumps(old)))
            self.assertFalse(run.compare_public_snapshot('selector_model_snapshot',json.dumps(new|{'max_tokens':513}),json.dumps(old)))
        self.assertFalse(run.compare_public_snapshot('expression_snapshot','new prompt','old prompt'))

class RuntimeTimeoutTests(selector_fixture.SelectorTests):
    async def check_timeout(self,accepted,current,transient=False):
        self.enable();self.config.update({'character_selector':{'timeout':accepted}})
        turn=await self.turn('书店');self.config.update({'character_selector':{'timeout':current}})
        seen=[]
        async def provider(model,messages):
            if model.get('_character_selection'):
                with self.store.connection() as db:
                    reservation=db.execute('SELECT expires_epoch FROM quota_reservations WHERE turn_id=?',(turn['id'],)).fetchone()[0]
                seen.append((model['timeout'],reservation-time.time()))
                if transient and len(seen)==1:raise run.ModelError('selector_timeout')
                return '{"schema_version":1,"lore_ids":[],"example_ids":[]}',{'total_tokens':1}
            return '主回复',{'total_tokens':1}
        with patch.object(self.rt,'local_pool',side_effect=self.pool),patch.object(self.model,'complete',side_effect=provider),patch.object(retry,'retry_delay',new=AsyncMock()):
            await self.engine._run(turn['id'])
        self.assertEqual(self.engine.get(turn['id'])['status'],'completed')
        self.assertEqual(len(seen),2 if transient else 1)
        for timeout,reserved in seen:
            self.assertEqual(timeout,accepted);self.assertGreater(reserved,accepted+55)
        return seen
    async def test_new_240_and_retry_reservation_extended(self):await self.check_timeout(240,240,True)
    async def test_frozen_60_not_rewritten_by_current_240(self):await self.check_timeout(60,240)

for name in dir(selector_fixture.SelectorTests):
    if name.startswith('test_') and name not in RuntimeTimeoutTests.__dict__:setattr(RuntimeTimeoutTests,name,None)

class PreflightTests(CaptureTests):
    async def test_slow_protection_outside_provider_deadline(self):
        self.model['timeout']=.2
        def slow(*_):time.sleep(.25)
        async def delayed(*_):await asyncio.sleep(.06);return self.success()
        self.client.complete.side_effect=delayed
        with patch.object(run,'check_item',side_effect=slow):
            await run.guarded_complete(self.capture,self.model,self.messages,validate=lambda _:None,save=lambda _:None,before_attempt=lambda _:None)
        row=self.ledger.rows()[0]
        self.assertGreater(row['preflight_seconds'],.24)
        self.assertGreater(row['deadline_remaining_before_client'],.15)
        self.assertEqual(row['status'],'completed')
    async def test_missing_preflight_prevents_dispatch(self):
        with self.assertRaisesRegex(run.StopRun,'preflight_required'):await self.capture.complete(self.model,self.messages)
        self.client.complete.assert_not_awaited()
    async def test_configuration_changed_after_guard_prevents_dispatch(self):
        self.capture.preflight(self.model,self.messages);self.configfile.write_text('{"changed":true}')
        with self.assertRaisesRegex(run.StopRun,'configuration_changed'):await self.capture.complete(self.model,self.messages)
        self.client.complete.assert_not_awaited()

for name in dir(CaptureTests):
    if name.startswith('test_') and name not in PreflightTests.__dict__:setattr(PreflightTests,name,None)
