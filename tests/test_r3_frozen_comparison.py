import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock,patch

from scripts import r3_frozen_comparison as run
from scripts.r3_frozen_report import classification,report
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
    def test_total_384_and_four_per_position(self):
        for position in range(384):self.ledger.finish(self.reserve(str(position)),'completed')
        self.assertEqual(len(self.ledger.rows()),384)
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
        with patch('scripts.r3_frozen_report.verify_freeze',return_value={'real':True,'items':[]}):
            with self.assertRaisesRegex(run.StopRun,'mixed_real_simulation'):report(self.root)
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
        self.client=AsyncMock();self.capture=run.Capture(self.root,self.item,object(),self.ledger,self.client,False)
        p=patch.object(run,'check_item');p.start();self.addCleanup(p.stop)
    def success(self):return run.ModelCompletion('{"schema_version":1,"lore_ids":[],"example_ids":["valid_but_inapplicable"]}',{'total_tokens':2},{'finish_reason':'stop'})
    async def test_valid_but_inapplicable_selection_kept_without_retry(self):
        self.client.complete.return_value=self.success()
        result=await self.capture.complete(self.model,self.messages)
        self.assertIn('valid_but_inapplicable',result[0]);self.assertEqual(self.client.complete.await_count,1)
        self.assertEqual(self.ledger.rows()[0]['status'],'completed')
    async def test_timeout_terminal_then_runtime_recovers_without_collector_retry(self):
        self.client.complete.side_effect=[asyncio.CancelledError('generation_attempt_timeout'),self.success()]
        with self.assertRaises(asyncio.CancelledError):await self.capture.complete(self.model,self.messages)
        self.assertEqual(self.ledger.check()[0]['error'],'selector_timeout')
        await self.capture.complete(self.model,self.messages)
        self.assertEqual([r['attempt_index'] for r in self.ledger.rows()],[0,1])
    async def test_user_cancellation_terminal_prevents_new_call(self):
        self.client.complete.side_effect=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):await self.capture.complete(self.model,self.messages)
        with self.assertRaises(run.StopRun):await self.capture.complete(self.model,self.messages)
        self.assertEqual(self.client.complete.await_count,1)
    async def test_reservation_failure_prevents_network(self):
        with patch('scripts.grok47_r1_retry.write_once',side_effect=OSError('disk')):
            with self.assertRaises(run.StopRun):await self.capture.complete(self.model,self.messages)
        self.client.complete.assert_not_awaited()
    async def test_terminal_failure_leaves_reservation_and_prevents_replay(self):
        self.client.complete.return_value=self.success()
        with patch('scripts.retry_expression_selection_r1.write_once',side_effect=OSError('disk')):
            with self.assertRaises(run.StopRun):await self.capture.complete(self.model,self.messages)
        with self.assertRaises(run.StopRun):await self.capture.complete(self.model,self.messages)
        self.assertEqual(self.client.complete.await_count,1)
    async def test_runtime_four_attempts_and_one_two_four_delays(self):
        self.client.complete.side_effect=run.ModelError('provider_empty_response');saved=[];sleep=AsyncMock()
        with self.assertRaises(retry.GenerationRetriesExhausted):
            await retry.audited_complete(self.capture,self.model,self.messages,validate=lambda _:None,save=lambda s:saved.append(deepcopy(s)),before_attempt=lambda _:None,sleep=sleep)
        self.assertEqual(self.client.complete.await_count,4)
        self.assertEqual([c.args[0] for c in sleep.await_args_list],[1,2,4])
        self.assertEqual(len(self.ledger.rows()),4)
    async def test_runtime_success_does_not_replay_selector_when_main_retries(self):
        self.client.complete.return_value=self.success()
        await retry.audited_complete(self.capture,self.model,self.messages,validate=lambda _:None,save=lambda _:None,before_attempt=lambda _:None)
        main=AsyncMock();main.complete.side_effect=[run.ModelError('provider_timeout'),run.ModelCompletion('内容质量差但有效',{'total_tokens':1},{})]
        await retry.audited_complete(main,self.model,self.messages,validate=lambda _:None,save=lambda _:None,before_attempt=lambda _:None,sleep=AsyncMock())
        self.assertEqual(self.client.complete.await_count,1);self.assertEqual(main.complete.await_count,2)
    async def test_offline_context_refuses_provider(self):
        with run.offline():
            with self.assertRaises(run.StopRun):await run.ChatCompletionsClient().complete(self.model,self.messages)


if __name__=='__main__':unittest.main()
