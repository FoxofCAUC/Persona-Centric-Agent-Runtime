"""CR2.1 runner contracts. All transports and credentials here are synthetic."""
import asyncio
from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import io
import unittest
from unittest.mock import AsyncMock,MagicMock,patch

from persona_runtime.archive import fingerprint
from persona_runtime.chat import ModelCompletion
from persona_runtime.models import RuntimeErrorBase
from scripts import dialogue_context_real as real


MODEL={'base_url':'https://example.invalid/v1','name':'grok-4.7','timeout':180,
       'max_tokens':8192,'max_input_chars':24000,'encrypted_key':''}
MESSAGES=[{'role':'user','content':'仅限人工测试'}]


class Temporary(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=real.HOME/'tmp',prefix='cr21-unit-')
        self.root=Path(self.temp.name).resolve()
    def tearDown(self):
        self.assertTrue(self.root.is_relative_to((real.HOME/'tmp').resolve()))
        self.temp.cleanup()


def settle(ledger,row,status='failed',ident=None,elapsed=1):
    attempt={'id':ident or f'attempt-{row["reserve"]["id"]}','status':status,
        'request_hash':row['reserve']['request_hash'],'started_epoch':100,'finished_epoch':100+elapsed}
    ledger.transport(row,{'status':'returned' if status=='completed' else 'error','real':ledger.real,
                          'usage':None,'diagnostics':{},'elapsed_seconds':elapsed})
    ledger.audit({'generation_retry':{'attempts':[attempt]}})
    return attempt


class LedgerTests(Temporary):
    def test_initial_plus_three_then_fifth_dispatch_rejected(self):
        ledger=real.Ledger(self.root,False)
        for _ in range(4):settle(ledger,ledger.reserve('position','main',MODEL,MESSAGES))
        with self.assertRaisesRegex(RuntimeErrorBase,'no_success_replay'):
            ledger.reserve('position','main',MODEL,MESSAGES)
        self.assertEqual(len(ledger.rows),4)

    def test_completed_position_never_replayed(self):
        ledger=real.Ledger(self.root,False)
        settle(ledger,ledger.reserve('position','main',MODEL,MESSAGES),'completed')
        with self.assertRaisesRegex(RuntimeErrorBase,'no_success_replay'):
            ledger.reserve('position','main',MODEL,MESSAGES)

    def test_retry_must_preserve_provider_request(self):
        ledger=real.Ledger(self.root,False)
        settle(ledger,ledger.reserve('position','main',MODEL,MESSAGES))
        with self.assertRaisesRegex(RuntimeErrorBase,'retry_changed'):
            ledger.reserve('position','main',MODEL,[{'role':'user','content':'different'}])

    def test_unsettled_reservation_blocks_every_later_slot(self):
        ledger=real.Ledger(self.root,False);ledger.reserve('first','joint',MODEL,MESSAGES)
        with self.assertRaisesRegex(RuntimeErrorBase,'unsettled_receipt'):
            ledger.reserve('second','main',MODEL,MESSAGES)

    def test_repeated_old_audit_does_not_fail_recovered_attempt(self):
        ledger=real.Ledger(self.root,False)
        first=ledger.reserve('position','main',MODEL,MESSAGES);old=settle(ledger,first,ident='old')
        nextrow=ledger.reserve('position','main',MODEL,MESSAGES)
        ledger.audit({'generation_retry':{'attempts':[old,{'id':'new','status':'running'}]}})
        self.assertNotIn('terminal',nextrow)
        settle(ledger,nextrow,'completed',ident='new')
        self.assertEqual(nextrow['terminal']['status'],'completed')

    def test_reserve_writer_failure_permanently_breaks_ledger(self):
        ledger=real.Ledger(self.root,False)
        with patch.object(real,'write_once',side_effect=OSError('injected')):
            with self.assertRaises(OSError):ledger.reserve('one','main',MODEL,MESSAGES)
        self.assertTrue(ledger.broken)
        with self.assertRaisesRegex(RuntimeErrorBase,'ledger_broken'):ledger.reserve('two','main',MODEL,MESSAGES)

    def test_transport_writer_failure_permanently_breaks_ledger(self):
        ledger=real.Ledger(self.root,False);row=ledger.reserve('one','main',MODEL,MESSAGES)
        with patch.object(real,'write_once',side_effect=OSError('injected')):
            with self.assertRaises(OSError):ledger.transport(row,{'status':'returned'})
        self.assertTrue(ledger.broken)
        with self.assertRaisesRegex(RuntimeErrorBase,'ledger_broken'):ledger.reserve('two','main',MODEL,MESSAGES)

    def test_terminal_writer_failure_preserves_unsettled_receipt(self):
        ledger=real.Ledger(self.root,False);row=ledger.reserve('one','main',MODEL,MESSAGES)
        ledger.transport(row,{'status':'returned'})
        attempt={'id':'one','status':'completed','request_hash':row['reserve']['request_hash'],'started_epoch':0,'finished_epoch':1}
        with patch.object(real,'write_once',side_effect=OSError('injected')):
            with self.assertRaises(OSError):ledger.audit({'generation_retry':{'attempts':[attempt]}})
        self.assertTrue(ledger.broken);self.assertNotIn('terminal',row)

    def test_one_slow_physical_failure_stops_but_total_retry_time_does_not(self):
        ledger=real.Ledger(self.root,False)
        for _ in range(3):settle(ledger,ledger.reserve('one','joint',MODEL,MESSAGES),elapsed=240)
        self.assertFalse(ledger.broken)
        with self.assertRaisesRegex(RuntimeErrorBase,'slow_physical_failure'):
            settle(ledger,ledger.reserve('one','joint',MODEL,MESSAGES),elapsed=601)
        self.assertTrue(ledger.broken)

    def test_public_receipt_removes_cipher_and_is_independent_of_input(self):
        ledger=real.Ledger(self.root,False);model=deepcopy(MODEL)|{'encrypted_key':'fake-cipher'}
        row=ledger.reserve('one','main',model,MESSAGES)
        model['name']='changed'
        self.assertNotIn('encrypted_key',row['reserve']['model'])
        self.assertEqual(row['reserve']['model']['name'],'grok-4.7')
        self.assertFalse(row['reserve']['real']);self.assertTrue(row['reserve']['simulation'])


class OrderAndCandidates(unittest.TestCase):
    def test_all_pairs_are_adjacent_balanced_and_smoke_first(self):
        keys={f'S{i:02d}-{role}' for i in range(1,17) for role in ('process','candid')}
        rows=real.position_order(keys)
        self.assertEqual(len(rows),128);self.assertEqual(len({r['id'] for r in rows}),128)
        self.assertEqual({r['case_id'] for r in rows[:16]},{'S01','S11'})
        for n in range(0,128,2):
            a,b=rows[n:n+2]
            self.assertEqual((a['key'],a['replicate']),(b['key'],b['replicate']))
            self.assertEqual({a['arm'],b['arm']},{'legacy','automatic'})
        self.assertEqual(sum(r['arm']=='legacy' for r in rows),64)

    def test_candidate_pool_is_copy_and_wrong_version_is_rejected(self):
        pool={'version_id':'accepted','payload':{'roots':{'example':['e1']}}}
        item={'key':'case','pool':pool,'attachment':{'pool_hash':fingerprint(pool)},
              'public_snapshots':{'character_snapshot':json.dumps({'id':'accepted','persona_id':'pid'}),
                                  'context_policy_snapshot':json.dumps({'character_retrieval':{'mode':'lexical'}})}}
        proxy=real.FrozenCandidates(object(),item)
        preview={'shared_preview':{'sources':[{'kind':'current','text':'hello'}]}}
        # Adapt to the scoped checks while keeping fixture values fully synthetic.
        with patch.object(real,'read',return_value=preview):
            with self.assertRaises(RuntimeErrorBase):proxy.local_pool({'id':'wrong','persona_id':'pid'},{'mode':'lexical'},['hello'])

    def test_candidate_pool_copy_and_scope_policy_scenario_checks(self):
        from persona_runtime.behavior import resolve_behavior
        accepted={'id':'accepted','persona_id':'pid'};policy={'mode':'lexical'}
        pool={'version_id':'accepted','payload':{'roots':{'example':['e1']}}}
        item={'key':'case','pool':pool,'attachment':{'persona_id':'pid','pool_hash':fingerprint(pool)},
              'public_snapshots':{'character_snapshot':json.dumps(accepted),
                                  'context_policy_snapshot':json.dumps({'character_retrieval':policy})}}
        proxy=real.FrozenCandidates(object(),item)
        preview={'shared_preview':{'sources':[{'kind':'current','text':'hello'}]}}
        scenario=[s['act'] for s in resolve_behavior('hello')['signals']]
        with patch.object(real,'read',return_value=preview):
            result=proxy.local_pool(accepted,policy,['hello'],scenario)
            result['payload']['roots']['example'].clear()
            self.assertEqual(pool['payload']['roots']['example'],['e1'])
            for version,changed_policy,members,cues in [
                (accepted|{'persona_id':'other'},policy,['hello'],scenario),
                (accepted,{'mode':'hybrid'},['hello'],scenario),
                (accepted,policy,['changed'],scenario),
                (accepted,policy,['hello'],['invented-scenario'])]:
                with self.assertRaises(RuntimeErrorBase):proxy.local_pool(version,changed_policy,members,cues)
            item['pool']['payload']['roots']['example'].append('changed')
            with self.assertRaisesRegex(RuntimeErrorBase,'candidate_changed'):proxy.local_pool(accepted,policy,['hello'],scenario)


class VerificationTests(Temporary):
    def test_completed_state_without_expected_results_is_not_enough(self):
        (self.root/'state.json').write_text(json.dumps({'status':'completed','real':False,'main_completed':128,'physical_calls':0}),encoding='utf-8')
        prepared={'real':False,'positions':[{'id':f'p{i}'} for i in range(128)]}
        with patch.object(real,'frozen',return_value=prepared):
            with self.assertRaisesRegex(RuntimeErrorBase,'result_inventory'):real.verify(self.root)

    def test_orphan_terminal_is_rejected(self):
        (self.root/'state.json').write_text(json.dumps({'status':'completed','real':False,'main_completed':128,'physical_calls':0}),encoding='utf-8')
        (self.root/'receipts').mkdir();(self.root/'receipts/0001.terminal.json').write_text('{}',encoding='utf-8')
        with patch.object(real,'frozen',return_value={'real':False,'positions':[]}):
            with self.assertRaisesRegex(RuntimeErrorBase,'orphan_receipt'):real.verify(self.root)

    def test_non_execution_cli_actions_cannot_construct_transport(self):
        import sys
        fake_report=MagicMock(return_value={'status':'only-report'})
        module=SimpleNamespace(report=fake_report)
        for action in ('report','verify'):
            with patch.object(sys,'argv',['dialogue_context_real.py',action,'--root',str(self.root),'--real']), \
                 patch.object(real,'confined',return_value=self.root),patch.object(real,'verify',return_value={'passed':True}), \
                 patch.object(real,'ChatCompletionsClient',side_effect=AssertionError('network transport')), \
                 patch.dict(sys.modules,{'scripts.dialogue_context_real_analysis':module}),patch('sys.stdout',new=io.StringIO()):
                real.main()


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=real.HOME/'tmp',prefix='cr21-unit-')
        self.root=Path(self.temp.name).resolve()
    async def asyncTearDown(self):
        self.assertTrue(self.root.is_relative_to((real.HOME/'tmp').resolve()))
        self.temp.cleanup()

    def transport(self,real_mode=False,simulation_client=None):
        engine=SimpleNamespace(store=SimpleNamespace(connection=lambda:nullcontext(None)))
        prepared={'real':real_mode,'batch':'unit','runtime_root':str(self.root)}
        pos={'id':'unit-position','arm':'automatic'}
        item={'attachment':{'turn_id':'turn'},'shared_guard':'guard'}
        collected={'archive_evidence':{},'guard_details':{}}
        ledger=real.Ledger(self.root/'receipts',real_mode)
        obj=real.Transport(self.root,prepared,pos,item,engine,ledger,collected,simulation_client)
        return obj,ledger

    async def test_simulation_cannot_use_encrypted_credentials(self):
        fake=SimpleNamespace(complete=AsyncMock())
        obj,ledger=self.transport(simulation_client=fake)
        with patch.object(real,'current',return_value={}),patch.object(real,'source_guard',return_value='guard'):
            with self.assertRaisesRegex(RuntimeErrorBase,'simulated_credentials'):
                await obj.complete(MODEL|{'encrypted_key':'fake-cipher'},MESSAGES)
        fake.complete.assert_not_awaited();self.assertEqual(ledger.rows,[])

    async def test_simulation_never_constructs_real_client(self):
        fake=SimpleNamespace(complete=AsyncMock(return_value=ModelCompletion('fake',{'total_tokens':1},{})))
        obj,ledger=self.transport(simulation_client=fake)
        with patch.object(real,'current',return_value={}),patch.object(real,'source_guard',return_value='guard'),patch.object(real,'ChatCompletionsClient',side_effect=AssertionError('real client')):
            await obj.complete(MODEL,MESSAGES)
        fake.complete.assert_awaited_once();self.assertEqual(len(ledger.rows),1)

    async def test_auth_changes_only_cipher_not_provider_body_or_messages(self):
        approved=deepcopy(MODEL)|{'encrypted_key':'synthetic-dpapi-cipher'}
        (self.root/'run.started.json').write_text(json.dumps({'batch':'unit','real':True}),encoding='utf-8')
        (self.root/'owner.json').write_text(json.dumps({'batch':'unit','artifact_root':str(self.root)}),encoding='utf-8')
        (self.root/'credentials.dpapi.json').write_text(json.dumps({'model':approved}),encoding='utf-8')
        obj,ledger=self.transport(real_mode=True)
        native=SimpleNamespace(complete=AsyncMock(return_value=ModelCompletion('fake',{'total_tokens':1},{})))
        turn={'persona_id':'artificial','model_snapshot':json.dumps(MODEL),
              'reply_mode_snapshot':None,'expression_snapshot':None}
        with patch.object(real,'current',return_value=turn),patch.object(real,'source_guard',return_value='guard'), \
             patch.object(real,'ConfigStore') as config,patch.object(real,'guard_cloud_dispatch'),patch.object(real,'ChatCompletionsClient',return_value=native):
            config.return_value.load.return_value={'model':approved}
            await obj.complete(MODEL,MESSAGES)
        actual_model,actual_messages=native.complete.await_args.args
        self.assertEqual(actual_messages,MESSAGES)
        self.assertEqual({k:v for k,v in actual_model.items() if k!='encrypted_key'},real.public(MODEL))
        self.assertEqual(actual_model['encrypted_key'],'synthetic-dpapi-cipher')
        self.assertNotIn('encrypted_key',ledger.rows[0]['reserve']['model'])

    async def test_sampling_change_rejected_before_real_transport(self):
        from persona_runtime.dialogue_voice import freeze
        expected=deepcopy(MODEL)|{'_dialogue_profile':freeze('cr21-unit-seed',['人工闲聊'],version='natural-dialogue-v2')}
        approved=deepcopy(expected)|{'encrypted_key':'synthetic-dpapi-cipher'}
        changed=deepcopy(expected)
        changed['_dialogue_profile']['temperature']=0.3 if expected['_dialogue_profile']['temperature']!=0.3 else 1.1
        (self.root/'run.started.json').write_text(json.dumps({'batch':'unit','real':True}),encoding='utf-8')
        (self.root/'owner.json').write_text(json.dumps({'batch':'unit','artifact_root':str(self.root)}),encoding='utf-8')
        (self.root/'credentials.dpapi.json').write_text(json.dumps({'model':approved}),encoding='utf-8')
        obj,ledger=self.transport(real_mode=True)
        native=SimpleNamespace(complete=AsyncMock())
        turn={'persona_id':'artificial','model_snapshot':json.dumps(expected),
              'reply_mode_snapshot':None,'expression_snapshot':None}
        with patch.object(real,'current',return_value=turn),patch.object(real,'source_guard',return_value='guard'), \
             patch.object(real,'ConfigStore') as config,patch.object(real,'guard_cloud_dispatch'), \
             patch.object(real,'ChatCompletionsClient',return_value=native) as constructor:
            config.return_value.load.return_value={'model':approved}
            with self.assertRaisesRegex(RuntimeErrorBase,'cr21_public_wire_changed'):
                await obj.complete(changed,MESSAGES)
        constructor.assert_not_called();native.complete.assert_not_awaited();self.assertEqual(ledger.rows,[])

    async def test_simulation_mode_mismatch_stops_before_position(self):
        p={'real':True}
        with patch.object(real,'frozen',return_value=p),patch.object(real,'run_position',new=AsyncMock()) as run_position:
            with self.assertRaisesRegex(RuntimeErrorBase,'execution_mode_mismatch'):await real.run(self.root,False)
        run_position.assert_not_awaited()

    async def test_existing_batch_is_report_only(self):
        (self.root/'run.started.json').write_text('{}',encoding='utf-8')
        with patch.object(real,'frozen',return_value={'real':False}),patch.object(real,'run_position',new=AsyncMock()) as run_position:
            with self.assertRaisesRegex(RuntimeErrorBase,'existing_batch_report_only'):await real.run(self.root,False)
        run_position.assert_not_awaited()


if __name__=='__main__':unittest.main()
