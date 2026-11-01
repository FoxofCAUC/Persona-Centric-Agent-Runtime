"""Supplement accounting and no-cherry-picking contract; no external calls."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts.retry_expression_selection_r1 import ReceiptLedger,RetryClient,StopRun,lenient_labels
from persona_runtime.chat import ModelError
from persona_runtime.reply_batch import expression_snapshot
from tests.expression_selection_fixture import fixture


class RetryEvidence(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.ledger=ReceiptLedger(self.root/'receipts')
    def reserve(self,slot='one',request=None):
        return self.ledger.reserve(slot,{'request':request or {'messages':[]}})
    def test_missing_receipt_never_replays(self):
        self.reserve()
        with self.assertRaisesRegex(StopRun,'receipt_gap'):ReceiptLedger(self.ledger.root).reserve('two',{'request':{}})
    def test_success_never_replaced(self):
        row=self.reserve();self.ledger.finish(row,'completed',output='bad but complete')
        with self.assertRaisesRegex(StopRun,'successful_slot'):self.reserve()
    def test_two_attempts_with_exact_request(self):
        row=self.reserve();self.ledger.finish(row,'failed')
        with self.assertRaisesRegex(StopRun,'request_changed'):self.reserve(request={'messages':['different']})
        row=self.reserve();self.ledger.finish(row,'failed')
        with self.assertRaisesRegex(StopRun,'retry_limit'):self.reserve()
    def test_global_limit_across_arms(self):
        for i in range(34):
            row=self.reserve(str(i));self.ledger.finish(row,'completed')
        with self.assertRaisesRegex(StopRun,'retry_limit'):self.reserve('extra')
    def test_fifth_failure_stops(self):
        for i in range(5):
            row=self.reserve(str(i));self.ledger.finish(row,'failed',seconds=1)
        with self.assertRaisesRegex(StopRun,'failure_stop'):self.ledger.check()
    def test_long_failed_call_stops(self):
        row=self.reserve();self.ledger.finish(row,'failed',seconds=601)
        with self.assertRaisesRegex(StopRun,'failure_stop'):self.ledger.check()
    def test_receipt_tampering_stops(self):
        row=self.reserve();self.ledger.finish(row,'completed')
        path=self.ledger.root/'001.reserve.json';path.write_text(path.read_text().replace('one','two'),encoding='utf-8')
        with self.assertRaisesRegex(StopRun,'reservation_mismatch'):self.ledger.check()
    def test_write_failure_never_dispatches(self):
        calls=[]
        class Fake:
            async def complete(self,*args):calls.append(args);return ('{}',None)
        client=RetryClient(self.root,{'key':'test'},self.ledger,lambda:None,True);client.client=Fake()
        with patch('scripts.retry_expression_selection_r1.write_once',side_effect=OSError('denied')):
            with self.assertRaisesRegex(StopRun,'reservation_write_failed'):asyncio.run(client.complete(self.model(),[{'role':'system','content':'输出JSON'}]))
        self.assertEqual(calls,[])
    def test_partial_receipt_blocks_resume(self):
        (self.ledger.root/'001.reserve.json').write_text('{',encoding='utf-8')
        with self.assertRaisesRegex(StopRun,'receipt_unreadable'):self.reserve()
    def test_result_write_failure_never_retries_paid_call(self):
        from scripts.retry_expression_selection_r1 import write_once
        calls=[]
        class Fake:
            async def complete(self,*args):
                calls.append(args)
                return ('{"schema_version":3,"behavior_intent":"respond","messages":[{"content":"完整回复","content_kind":"chat","cancel_if_new_input":false}]}',None)
        def fail_result(path,value):
            if path.name.endswith('.result.json'):raise OSError('result unavailable')
            return write_once(path,value)
        client=RetryClient(self.root,{'key':'test'},self.ledger,lambda:None,True);client.client=Fake()
        with patch('scripts.retry_expression_selection_r1.write_once',side_effect=fail_result):
            with self.assertRaisesRegex(StopRun,'result_write_failed'):
                asyncio.run(client.complete(self.model(),[{'role':'user','content':'JSON'}]))
        self.assertEqual(len(calls),1)
        with self.assertRaisesRegex(StopRun,'receipt_gap'):self.ledger.check()
    @staticmethod
    def model():
        return {'base_url':'https://api.deepseek.com','name':'deepseek-flash','max_tokens':8192,
                'temperature':1,'timeout':180,'encrypted_key':'','_reply_mode':'natural_structured',
                '_expression_snapshot':expression_snapshot('natural_structured',version=3)}
    def test_retry_preserves_parameters_and_counts_failure(self):
        calls=[]
        class Fake:
            async def complete(self,model,messages):
                calls.append((dict(model),list(messages)))
                if len(calls)==1:raise ModelError('provider_empty_response')
                return ('{"schema_version":3,"behavior_intent":"respond","messages":[{"content":"完整但不一定好","content_kind":"chat","cancel_if_new_input":false}]}',{'total_tokens':3})
        client=RetryClient(self.root,{'key':'test'},self.ledger,lambda:None,True);client.client=Fake()
        result=asyncio.run(client.complete(self.model(),[{'role':'user','content':'人工输入，请输出JSON'}]))
        self.assertIn('完整',result[0]);self.assertEqual(calls[0],calls[1])
        self.assertEqual([r['status'] for r in self.ledger.rows()],['failed','completed'])
    def test_lenient_labels_do_not_rewrite_frozen_or_facts(self):
        data=fixture();old=fixture();labels=lenient_labels(data)
        self.assertEqual(data,old)
        self.assertEqual(labels['allowed']['R05'],[]);self.assertEqual(labels['allowed']['R06'],[])
        self.assertIn('opinion',labels['allowed']['R07']);self.assertNotIn('small_win',labels['allowed']['R03'])


if __name__=='__main__':unittest.main()
