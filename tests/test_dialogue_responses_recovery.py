"""Responses recovery guards exercised with artificial files and no HTTP."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,AsyncMock

from scripts import dialogue_responses_recovery as r


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='recovery-test-',dir=r.HOME/'tmp')
        self.root=Path(self.tmp.name)
        self.prepared={'real':False,'positions':[{'id':'pending','conditions':['raw','full']}]}
        self.ledger=r.Ledger(self.root,self.prepared)
        self.model={'name':'grok-4.7','base_url':'https://example.invalid/v1','max_tokens':8192,'timeout':180}
        self.messages=[{'role':'system','content':'synthetic'},{'role':'user','content':'input'}]
        self.payload=r.build_responses_request(self.model,self.messages)[0]
    def tearDown(self):self.tmp.cleanup()
    def reserve(self,pos='pending',kind='joint',payload=None):
        with patch.object(r,'frozen',return_value=self.prepared):
            return self.ledger.reserve(pos,kind,self.model,self.messages,self.payload if payload is None else payload)
    def terminal(self,row,status='failed',ident='attempt'):
        self.ledger.transport(row,{'status':'returned','raw':'synthetic','real':False,'usage':None,'elapsed_seconds':900})
        self.ledger.audit({'generation_retry':{'attempts':[{'id':ident,'status':status,'request_hash':row['reserve']['request_hash'],
            'started_epoch':1,'finished_epoch':901}]}})
    def test_completed_groups_skipped_and_partial_rejected(self):
        p={'positions':[{'id':'old','conditions':['raw','full']},{'id':'pending','conditions':['raw','full']}]}
        done,pending=r.remaining(p,{'old-raw':{},'old-full':{}})
        self.assertEqual([x['id'] for x in done],['old']);self.assertEqual([x['id'] for x in pending],['pending'])
        with self.assertRaisesRegex(Exception,'partial_group'):r.remaining(p,{'old-raw':{}})
    def test_formal_directory_rejected(self):
        with self.assertRaisesRegex(Exception,'outside'):r.confined(r.HOME)
    def test_actual_messages_wire_fingerprint_and_no_output_field(self):
        row=self.reserve()['reserve']
        self.assertEqual(row['request_hash'],r.fingerprint(self.payload))
        self.assertEqual(row['input_hash'],r.fingerprint(self.messages))
        self.assertEqual(row['wire_protocol'],'openai-responses-v1')
        self.assertNotIn('max_tokens',row['wire_request']);self.assertIsNone(row['effective_timeout_seconds'])
        self.assertEqual(row['wire_request']['input'],self.messages)
        self.assertIs(row['wire_request']['store'],False)
        self.assertNotIn('max_output_tokens',row['wire_request'])
    def test_public_unknown_backend_id_preserved_without_private_metadata(self):
        value=type('Response',(),{})()
        value.provider_response_metadata={'model':'different-routed-model-v9','system_fingerprint':'fp_abc',
            'status':'completed','response_id':'resp_123','headers':'secret','reasoning':'private',
            'unsafe':'not allowed'}
        self.assertEqual(r.public_response_metadata(value),{'model':'different-routed-model-v9','system_fingerprint':'fp_abc','status':'completed','response_id':'resp_123'})
        value.provider_response_metadata={'model':'bad\nheader','system_fingerprint':'x'*129}
        self.assertEqual(r.public_response_metadata(value),{})
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
    def test_slow_remote_failure_has_no_local_600_second_stop(self):
        row=self.reserve();self.terminal(row)
        self.assertFalse(self.ledger.broken);self.assertEqual(row['terminal']['status'],'failed')
    def test_failed_receipt_write_disables_more_dispatch(self):
        with patch.object(r,'frozen',return_value=self.prepared),patch.object(r,'write_once',side_effect=OSError('disk')):
            with self.assertRaises(OSError):self.ledger.reserve('pending','joint',self.model,self.messages,self.payload)
        self.assertTrue(self.ledger.broken)
        with self.assertRaisesRegex(Exception,'unsettled'):self.reserve()
    def test_started_batch_cannot_restart(self):
        (self.root/'run.started.json').write_text('{}')
        with patch.object(r,'frozen',return_value={'real':False}):
            with self.assertRaisesRegex(Exception,'report_only'):asyncio.run(r.run(self.root,False))
    def test_manifest_contract_describes_actual_messages_wire(self):
        original={'messages':self.messages,'manifest':{'provider_request_contract':{'profile':'openai-legacy'}}}
        turn={'model_snapshot':json.dumps(self.model),'reply_mode_snapshot':'natural_structured','expression_snapshot':'{}'}
        pipe=r.ResponsesViewPipeline.__new__(r.ResponsesViewPipeline)
        pipe.responses_contract=r.transport_contract();pipe.attachment_json='{}'
        with patch.object(r.old.ViewPipeline,'prepare',new=AsyncMock(return_value=deepcopy(original))),patch.object(r.old,'current',return_value=turn):
            result=asyncio.run(pipe.prepare(None,'synthetic-turn'))
        contract=result['manifest']['provider_request_contract']
        self.assertEqual(contract['profile'],'openai-responses-v1');self.assertEqual(contract['wire_hash'],r.fingerprint(self.payload))
        self.assertIsNone(contract['effective_max_tokens']);self.assertIsNone(contract['effective_timeout_seconds'])
        self.assertEqual(contract['requested_limits_from_accepted_snapshot'],{'timeout':180,'max_tokens':8192})


if __name__=='__main__':unittest.main()
