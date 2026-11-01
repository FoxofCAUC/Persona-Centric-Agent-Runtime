import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock,patch

from scripts import r2_frozen_comparison as run
from scripts.r2_frozen_report import classification,report
from persona_runtime import generation_retry as retry


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


class LedgerTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name);self.ledger=run.Ledger(self.root/'receipts')
    def reserve(self,slot='a',request=None):return self.ledger.reserve_attempt(slot,{'request':request or {'messages':['人工']}})
    def test_total_256_and_four_per_position(self):
        for position in range(256):self.ledger.finish(self.reserve(str(position)),'completed')
        self.assertEqual(len(self.ledger.rows()),256)
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
        with patch('scripts.r2_frozen_report.verify_freeze',return_value={'real':True,'items':[]}):
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
