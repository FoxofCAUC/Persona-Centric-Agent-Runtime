import asyncio
from copy import deepcopy
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
from scripts import r4_expression_realization as run
from scripts.r4_expression_report import attribution
from persona_runtime import generation_retry as retry
from persona_runtime.dialogue_voice import freeze,render
from persona_runtime.provider_compat import build_request
from tests.r4_expression_fixture import fixture,rubric


class ContractTests(unittest.TestCase):
    def test_artificial_materials_same_facts_and_separate_rubric(self):
        data=fixture();a=deepcopy(data['characters']['detail']);b=deepcopy(data['characters']['stance'])
        self.assertEqual(len(data['cases']),12);self.assertEqual(len(a.pop('examples')),6);b.pop('examples');self.assertEqual(a,b)
        run.PersonaSpec.from_dict(data['spec']);self.assertNotIn('reading_rules',str(data));self.assertEqual(rubric()['primary_denominator_per_arm'],18)
    def test_only_voice_removed_facts_history_and_parameters_still_compared(self):
        def payload(version):
            v=freeze('same',['人工'],version=version);s='人物资料：人工。'+render(v)[0];m={'prompt_assembly':{'blocks':[{'name':'persona_expression','start_character':0,'characters':len(s)}]}}
            return {'messages':[{'role':'system','content':s+'材料'},{'role':'user','content':'人工'}],'temperature':1.1},m,v
        a,ma,va=payload(run.ARMS['control']);b,mb,vb=payload(run.ARMS['experiment'])
        original=deepcopy(a);self.assertEqual(run.voice_removed(a,ma,va),run.voice_removed(b,mb,vb));self.assertEqual(a,original)
        b['messages'][1]['content']='变更';self.assertNotEqual(run.voice_removed(a,ma,va),run.voice_removed(b,mb,vb))
        b['messages'][1]['content']='人工';b['temperature']=.3;self.assertNotEqual(run.voice_removed(a,ma,va),run.voice_removed(b,mb,vb))
        a['messages'][0]['content']=a['messages'][0]['content'].replace('人工','变更',1)
        self.assertNotEqual(run.voice_removed(a,ma,va),run.voice_removed(b,mb,vb))
    def test_attribution_requires_pair_backend_and_only_voice_change(self):
        m=[{'response_model':'grok-4.7','system_fingerprint':'x'} for _ in range(2)]
        self.assertTrue(attribution(True,False,True,m)['eligible_for_directional_analysis'])
        for values in ((False,False,True,m),(True,True,True,m),(True,False,False,m),(True,False,True,[m[0],{}]),(True,False,True,[m[0],dict(m[1],system_fingerprint='y')])):
            self.assertFalse(attribution(*values)['eligible_for_directional_analysis'])
    def test_offline_blocks_provider_and_external_dns(self):
        with run.offline():
            with self.assertRaises(run.StopRun):socket.getaddrinfo('example.com',443)
            with self.assertRaises(run.StopRun):asyncio.run(run.ChatCompletionsClient().complete({},[]))
    def test_public_data_strips_only_credentials(self):
        self.assertEqual(run.public({'encrypted_key':'secret','timeout':180,'nested':[{'encrypted_key':'secret','name':'grok'}]}),{'timeout':180,'nested':[{'name':'grok'}]})


class LedgerTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name);self.ledger=run.Ledger(self.root/'receipts')
    def reserve(self,slot='a',request=None):return self.ledger.reserve_attempt(slot,{'request':request or {'messages':['人工']}})
    def test_192_stage_total_across_batches(self):
        self.ledger=run.Ledger(self.root/'receipts',limit=192)
        for i in range(192):self.ledger.finish(self.reserve(f'batch-{i//48}/'+str(i)),'completed')
        with self.assertRaises(run.StopRun):self.reserve('new-batch/new')
    def test_unlimited_total_keeps_numeric_receipt_order_after_999(self):
        self.assertIsNone(run.LIMIT)
        for ordinal in range(1,1002):
            slot='artificial-'+str(ordinal)
            row={'ordinal':ordinal,'key':slot+'/0','slot':slot,'attempt_index':0,'status':'reserved','request':{}}
            run.write_once(self.ledger.root/(str(ordinal).zfill(3)+'.reserve.json'),row)
            self.ledger.finish(row,'completed')
        rows=self.ledger.check();self.assertEqual(len(rows),1001)
        self.assertEqual([r['ordinal'] for r in rows],list(range(1,1002)))
        extra=self.reserve('beyond-old-ceiling');self.assertEqual(extra['ordinal'],1002)
        self.ledger.finish(extra,'completed')
        with self.assertRaisesRegex(run.StopRun,'successful_slot_never_regenerate'):self.reserve('beyond-old-ceiling')
    def test_four_failures_stop_entire_stage(self):
        for _ in range(4):self.ledger.finish(self.reserve(),'failed',seconds=1)
        with self.assertRaisesRegex(run.StopRun,'retries_exhausted'):self.reserve('other')
    def test_success_no_replay_and_request_stays_identical(self):
        self.ledger.finish(self.reserve(),'failed')
        with self.assertRaises(run.StopRun):self.reserve(request={'messages':['changed']})
        self.ledger.finish(self.reserve(),'completed')
        with self.assertRaises(run.StopRun):self.reserve()
    def test_pending_interruption_long_failed_call_stop(self):
        for status in ('reserved','interrupted','failed'):
            self.ledger=run.Ledger(self.root/status);r=self.reserve()
            if status!='reserved':self.ledger.finish(r,status,seconds=601)
            with self.assertRaises(run.StopRun):self.reserve('other')
    def test_existing_execution_never_resumes(self):
        run.write_once(self.root/'run-started.json',{})
        with patch.object(run,'protected'),patch.object(run,'verify_freeze',return_value={'real':False}):
            with self.assertRaisesRegex(run.StopRun,'never_resume'):asyncio.run(run.execute(self.root,False))


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name);self.ledger=run.Ledger(self.root/'receipts')
        self.file=self.root/'config.json';self.file.write_text('{}');self.messages=[{'role':'user','content':'人工'}]
        self.model={'name':'grok-4.7','base_url':'https://e-flowcode.cc/v1','max_tokens':8192,'timeout':180,'_reply_mode':'natural_structured','_expression_snapshot':None}
        self.item={'key':'artificial','configuration_hash':run.sha(self.file)};self.engine=SimpleNamespace(config=SimpleNamespace(file=self.file))
        self.client=AsyncMock();self.capture=run.Capture(self.root,self.item,self.engine,self.ledger,self.client,False)
        for name in ('check_item','main_manifest','validate_request','validate_reply'):
            p=patch.object(run,name);p.start();self.addCleanup(p.stop)
    def success(self):return run.ModelCompletion('人工回复',{'total_tokens':2},{'finish_reason':'stop'})
    async def dispatch(self):self.capture.preflight(self.model,self.messages);return await self.capture.complete(self.model,self.messages)
    async def test_quality_does_not_trigger_retry(self):
        self.client.complete.return_value=self.success();await self.dispatch();self.assertEqual(self.client.complete.await_count,1)
    async def test_timeout_record_then_recovery(self):
        self.client.complete.side_effect=[asyncio.CancelledError('generation_attempt_timeout'),self.success()]
        with self.assertRaises(asyncio.CancelledError):await self.dispatch()
        self.assertEqual(self.ledger.check()[0]['error'],'provider_timeout');await self.dispatch();self.assertEqual(len(self.ledger.rows()),2)
    async def test_cancel_stops_next_attempt(self):
        self.client.complete.side_effect=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):await self.dispatch()
        with self.assertRaises(run.StopRun):await self.dispatch()
        self.assertEqual(self.client.complete.await_count,1)
    async def test_reserve_failure_prevents_network(self):
        with patch('scripts.grok47_r1_retry.write_once',side_effect=OSError('disk')):
            with self.assertRaises(run.StopRun):await self.dispatch()
        self.client.complete.assert_not_awaited()
    async def test_terminal_failure_leaves_visible_gap(self):
        self.client.complete.return_value=self.success()
        with patch('scripts.retry_expression_selection_r1.write_once',side_effect=OSError('disk')):
            with self.assertRaises(run.StopRun):await self.dispatch()
        self.assertEqual(self.ledger.rows()[0]['status'],'reserved')
        with self.assertRaises(run.StopRun):await self.dispatch()
    async def test_config_source_or_frozen_request_change_stops_before_network(self):
        self.capture.preflight(self.model,self.messages);self.file.write_text('{"changed":true}')
        with self.assertRaises(run.StopRun):await self.capture.complete(self.model,self.messages)
        self.client.complete.assert_not_awaited()
    async def test_runtime_owns_retries_and_does_not_replay_success(self):
        self.client.complete.side_effect=[run.ModelError('provider_timeout'),self.success()];audits=[]
        with patch.object(retry.asyncio,'sleep',new=AsyncMock()):
            await run.guarded_complete(self.capture,self.model,self.messages,validate=lambda t:t,save=lambda s:audits.append(deepcopy(s)),before_attempt=lambda s:None)
        self.assertEqual(self.client.complete.await_count,2);self.assertEqual([r['attempt_index'] for r in self.ledger.rows()],[0,1])
    async def test_runtime_four_failures_and_cancellation_terminal(self):
        self.client.complete.side_effect=run.ModelError('provider_timeout');audits=[]
        with patch.object(retry.asyncio,'sleep',new=AsyncMock()):
            with self.assertRaises(Exception):
                await run.guarded_complete(self.capture,self.model,self.messages,validate=lambda t:t,save=lambda s:audits.append(deepcopy(s)),before_attempt=lambda s:None)
        self.assertEqual(self.client.complete.await_count,4);self.assertEqual(len(self.ledger.rows()),4)
        with self.assertRaises(run.StopRun):self.ledger.check()
