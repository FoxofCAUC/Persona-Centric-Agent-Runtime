"""Streaming recovery guards exercised with artificial files and no HTTP."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,AsyncMock

from scripts import dialogue_stream_recovery as r


def scratch(prefix):
    base=r.HOME/'tmp'
    return tempfile.TemporaryDirectory(prefix=prefix,dir=base if base.is_dir() else None)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=scratch('stream-recovery-test-')
        self.root=Path(self.tmp.name)
        self.prepared={'real':False,'idle_timeout_seconds':90,'positions':[{'id':'pending','conditions':['raw','full']}]}
        self.ledger=r.Ledger(self.root,self.prepared)
        self.model={'name':'grok-4.7','base_url':'https://example.invalid/v1','max_tokens':8192,'timeout':180}
        self.messages=[{'role':'system','content':'synthetic'},{'role':'user','content':'input'}]
        self.contract=r.stream_transport_contract(90)
        self.payload=r.build_messages_request(self.model,self.messages,self.contract)[0]
    def tearDown(self):self.tmp.cleanup()
    def reserve(self,pos='pending',kind='joint',payload=None):
        with patch.object(r,'frozen',return_value=self.prepared):
            return self.ledger.reserve(pos,kind,self.model,self.messages,self.payload if payload is None else payload)
    def terminal(self,row,status='failed',ident='attempt'):
        self.ledger.transport(row,{'status':'returned','raw':'synthetic','real':False,'usage':None,'elapsed_seconds':900})
        self.ledger.audit({'generation_retry':{'attempts':[{'id':ident,'status':status,'request_hash':row['reserve']['request_hash'],
            'started_epoch':1,'finished_epoch':901}]}})

    # --- guards carried over unchanged from the predecessor runners ---
    def test_completed_groups_skipped_and_partial_rejected(self):
        p={'positions':[{'id':'old','conditions':['raw','full']},{'id':'pending','conditions':['raw','full']}]}
        done,pending=r.remaining(p,{'old-raw':{},'old-full':{}})
        self.assertEqual([x['id'] for x in done],['old']);self.assertEqual([x['id'] for x in pending],['pending'])
        with self.assertRaisesRegex(Exception,'partial_group'):r.remaining(p,{'old-raw':{}})
    def test_formal_directory_rejected(self):
        with self.assertRaisesRegex(Exception,'outside'):r.confined(r.HOME)
        with self.assertRaisesRegex(Exception,'outside'):r.confined(r.OUT)
    def test_successful_positions_cannot_be_reserved(self):
        with self.assertRaisesRegex(Exception,'replay_forbidden'):self.reserve('imported')
        self.assertEqual(len(self.ledger.rows),0)
    def test_each_physical_call_rechecks_source(self):
        with patch.object(r,'frozen',side_effect=ValueError('changed')) as guard:
            with self.assertRaisesRegex(ValueError,'changed'):self.ledger.reserve('pending','joint',self.model,self.messages,self.payload)
            self.assertEqual(guard.call_count,1)
        self.assertFalse(self.ledger.rows)
    def test_success_not_replayed_and_unsettled_blocks(self):
        row=self.reserve()
        with self.assertRaisesRegex(Exception,'unsettled'):self.reserve()
        self.terminal(row,'completed')
        with self.assertRaisesRegex(Exception,'replay'):self.reserve()
    def test_first_plus_three_ceiling_and_same_request(self):
        for i in range(4):self.terminal(self.reserve(),ident=str(i))
        with self.assertRaisesRegex(Exception,'retry_or_success'):self.reserve()
    def test_changed_wire_cannot_retry(self):
        self.terminal(self.reserve())
        with self.assertRaisesRegex(Exception,'retry_changed'):self.reserve(payload=self.payload|{'temperature':.2})
    def test_failed_receipt_write_disables_more_dispatch(self):
        with patch.object(r,'frozen',return_value=self.prepared),patch.object(r,'write_once',side_effect=OSError('disk')):
            with self.assertRaises(OSError):self.ledger.reserve('pending','joint',self.model,self.messages,self.payload)
        self.assertTrue(self.ledger.broken)
        with self.assertRaisesRegex(Exception,'unsettled'):self.reserve()
    def test_started_batch_cannot_restart(self):
        (self.root/'run.started.json').write_text('{}')
        with patch.object(r,'frozen',return_value={'real':False,'positions':[]}):
            with self.assertRaisesRegex(Exception,'report_only'):asyncio.run(r.run(self.root,False))

    # --- streaming-specific ---
    def test_receipt_records_streaming_wire_idle_limit_and_no_caps(self):
        row=self.reserve()['reserve']
        self.assertIs(row['wire_request']['stream'],True);self.assertIs(row['stream'],True)
        self.assertEqual(row['request_hash'],r.fingerprint(self.payload))
        self.assertEqual(row['input_hash'],r.fingerprint(self.messages))
        self.assertEqual(row['wire_protocol'],'anthropic-messages-v1')
        self.assertNotIn('max_tokens',row['wire_request'])
        self.assertIsNone(row['effective_timeout_seconds']);self.assertEqual(row['idle_timeout_seconds'],90)
        self.assertEqual(row['wire_request']['system'],[{'type':'text','text':'synthetic'}])
    def test_non_streaming_payload_cannot_be_reserved(self):
        legacy=r.build_messages_request(self.model,self.messages)[0]
        self.assertIs(legacy['stream'],False)
        with self.assertRaisesRegex(Exception,'not_streaming'):self.reserve(payload=legacy)
        with self.assertRaisesRegex(Exception,'not_streaming'):self.reserve(payload=self.payload|{'max_tokens':2048})
        self.assertFalse(self.ledger.rows)
    def test_transport_refuses_legacy_contract_file(self):
        (self.root/'transport-contract.json').write_text(json.dumps({'version':'anthropic-messages-uncapped-v1'}))
        with self.assertRaisesRegex(Exception,'not_streaming'):r.Transport(self.root,self.prepared,{},None,self.ledger,'pending','joint')
        (self.root/'transport-contract.json').write_text(json.dumps(self.contract))
        self.assertEqual(r.Transport(self.root,self.prepared,{},None,self.ledger,'pending','joint').request_payload(self.model,self.messages),self.payload)
    def test_stream_public_keeps_timing_numbers_and_flags_only(self):
        value={'stream':True,'stream_events':812,'first_event_ms':4100.5,'first_thinking_ms':4310,'first_text_ms':155970,
               'timeout_seconds':90,'local_timeout_enabled':True,'elapsed_ms':164152,'stream_downgraded':False,
               'phase':'streaming','text':'SECRET','first_text_ms_bad':'x','reasoning':'private'}
        self.assertEqual(r.stream_public(value),{'stream':True,'stream_events':812,'first_event_ms':4100.5,'first_thinking_ms':4310,
            'first_text_ms':155970,'timeout_seconds':90,'local_timeout_enabled':True,'elapsed_ms':164152,'stream_downgraded':False})
        self.assertEqual(r.stream_public({'first_text_ms':-1,'stream':'yes','stream_events':True}),{})
        self.assertEqual(r.stream_public(None),{})
    def test_manifest_contract_describes_actual_streaming_wire(self):
        original={'messages':self.messages,'manifest':{'provider_request_contract':{'profile':'openai-legacy'}}}
        turn={'model_snapshot':json.dumps(self.model),'reply_mode_snapshot':'natural_structured','expression_snapshot':'{}'}
        pipe=r.StreamViewPipeline.__new__(r.StreamViewPipeline)
        pipe.messages_contract=self.contract;pipe.attachment_json='{}'
        with patch.object(r.old.ViewPipeline,'prepare',new=AsyncMock(return_value=deepcopy(original))),patch.object(r.old,'current',return_value=turn):
            result=asyncio.run(pipe.prepare(None,'synthetic-turn'))
        contract=result['manifest']['provider_request_contract']
        self.assertEqual(contract['profile'],'anthropic-messages-v1');self.assertEqual(contract['version'],r.STREAM_VERSION)
        self.assertIs(contract['stream'],True);self.assertEqual(contract['idle_timeout_seconds'],90)
        self.assertEqual(contract['wire_hash'],r.fingerprint(self.payload))
        self.assertIsNone(contract['effective_max_tokens']);self.assertIsNone(contract['effective_timeout_seconds'])
        self.assertEqual(contract['requested_limits_from_accepted_snapshot'],{'timeout':180,'max_tokens':8192})
        self.assertEqual(contract['isolated_execution_override'],r.VERSION)


class SmokeLimitTests(unittest.TestCase):
    """--max-groups: stop cleanly after N groups; state and run.started say so."""
    def setUp(self):
        self.tmp=scratch('stream-recovery-smoke-');self.root=Path(self.tmp.name)
        self.p={'real':False,'batch':'b','idle_timeout_seconds':90,
                'positions':[{'id':f'G{i}','conditions':['raw']} for i in range(3)]}
        self.calls=[]
    def tearDown(self):self.tmp.cleanup()
    async def fake_group(self,root,p,pos,ledger):self.calls.append(pos['id'])
    def run_with(self,limit):
        with patch.object(r,'frozen',return_value=self.p),patch.object(r,'report',return_value={'reported':True}):
            return asyncio.run(r.run(self.root,False,limit,group_runner=self.fake_group))
    def read(self,name):return json.loads((self.root/name).read_text(encoding='utf-8'))
    def test_limit_one_runs_first_group_only_and_stops_with_reason(self):
        self.assertEqual(self.run_with(1),{'reported':True})
        self.assertEqual(self.calls,['G0'])
        state=self.read('state.json')
        self.assertEqual((state['status'],state['reason'],state['completed_new_groups'],state['new_groups']),('stopped','group_limit_reached',1,3))
        started=self.read('run.started.json')
        self.assertEqual((started['max_groups'],started['planned_groups'],started['transport']),(1,3,r.STREAM_VERSION))
    def test_no_limit_completes_all_groups(self):
        self.run_with(None)
        self.assertEqual(self.calls,['G0','G1','G2']);self.assertEqual(self.read('state.json')['status'],'completed')
    def test_invalid_limits_rejected_before_anything_is_written(self):
        for limit in (0,4,-1,1.5,True):
            with self.subTest(limit=limit),self.assertRaisesRegex(Exception,'group_limit_invalid'):self.run_with(limit)
            self.assertFalse((self.root/'run.started.json').exists())
        self.assertEqual(self.calls,[])
    def test_group_failure_stops_with_failure_reason_not_limit(self):
        async def boom(root,p,pos,ledger):raise ValueError('recovery_main_failed_provider_timeout')
        with patch.object(r,'frozen',return_value=self.p),patch.object(r,'report',return_value={}):
            with self.assertRaises(Exception):asyncio.run(r.run(self.root,False,2,group_runner=boom))
        state=self.read('state.json')
        self.assertEqual((state['status'],state['reason'],state['completed_new_groups']),('stopped','recovery_main_failed_provider_timeout',0))


class PackageHistoryTests(unittest.TestCase):
    """A later root in the same package imports earlier settled real roots; it never replays them."""
    def setUp(self):
        self.tmp=scratch('stream-recovery-pkg-');self.out=Path(self.tmp.name)/'stream-4'
        self.out.mkdir()
        self.patch=patch.object(r,'OUT',self.out);self.patch.start()
    def tearDown(self):self.patch.stop();self.tmp.cleanup()
    def root(self,name,*,real=True,version=r.VERSION,started=True,state='stopped',receipts=(('0001','completed'),),unsettled=()):
        root=self.out/name;(root/'receipts').mkdir(parents=True)
        (root/'prepared.json').write_text(json.dumps({'version':version,'real':real}))
        if started:(root/'run.started.json').write_text('{}')
        if state:(root/'state.json').write_text(json.dumps({'status':state}))
        for ident,status in receipts:
            for ext in ('reserve','transport'):(root/'receipts'/f'{ident}.{ext}.json').write_text('{}')
            (root/'receipts'/f'{ident}.terminal.json').write_text(json.dumps({'status':status}))
        for ident in unsettled:(root/'receipts'/f'{ident}.reserve.json').write_text('{}')
        return root
    def test_only_started_real_roots_of_this_version_are_imported(self):
        smoke=self.root('real-v1',receipts=(('0001','failed'),('0002','completed')))
        self.root('simulation-v1',real=False)
        self.root('real-other',version='cr3-responses-recovery-v1')
        self.root('real-prepared-only',started=False,state=None)
        self.assertEqual(r.package_history(),[smoke])
        self.assertEqual(r.receipt_counts(smoke),{'physical':2,'settled':2,'failed':1})
    def test_new_root_excludes_itself(self):
        smoke=self.root('real-v1')
        self.assertEqual(r.package_history(exclude=smoke),[])
    def test_unsettled_or_running_prior_root_blocks(self):
        self.root('real-v1',unsettled=('0002',))
        with self.assertRaisesRegex(Exception,'in_package_unsettled'):r.package_history()
    def test_running_state_blocks(self):
        self.root('real-v1',state='running')
        with self.assertRaisesRegex(Exception,'in_package_unsettled'):r.package_history()
    def test_imported_group_leaves_pending_and_partial_group_requires_plan(self):
        parent={'positions':[{'id':'G05','conditions':['raw']},{'id':'G11','conditions':['raw','essential','full']},
                             {'id':'G07','conditions':['raw','essential','full']}]}
        base={'G05-raw':{}}
        _,original_pending=r.remaining(parent,base)
        done,pending=r.remaining(parent,base|{'G11-raw':{},'G11-essential':{},'G11-full':{}})
        self.assertEqual([x['id'] for x in done],['G05','G11']);self.assertEqual([x['id'] for x in pending],['G07'])
        self.assertEqual(pending,[x for x in original_pending if x['id'] not in {d['id'] for d in done}])
        with self.assertRaisesRegex(Exception,'partial_group'):r.remaining(parent,base|{'G11-raw':{}})


class ContractTests(unittest.TestCase):
    def test_module_constants_match_conservative_predecessor_ledger(self):
        self.assertEqual((r.PRIOR_PHYSICAL_CALLS,r.PRIOR_FAILED_CALLS,r.PRIOR_UNKNOWN_CALLS),(27,16,1))
        self.assertEqual(r.IDLE_TIMEOUT_SECONDS,90)
        self.assertEqual(r.stream_transport_contract(r.IDLE_TIMEOUT_SECONDS)['version'],r.STREAM_VERSION)
    def test_mock_completion_is_marked_streaming_and_never_real(self):
        item={'position':{'case_id':'G01','role':'process'}}
        with patch.object(r.old,'reply',return_value='模拟回复'):
            value=r.mock_completion(item,'main',[])
        self.assertEqual(value[0],'模拟回复');self.assertEqual(value.diagnostics['response_model'],'mock-messages')
        self.assertTrue(r.stream_public(value.diagnostics)['stream'])


if __name__=='__main__':unittest.main()
