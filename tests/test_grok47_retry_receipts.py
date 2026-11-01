import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,AsyncMock
from scripts.grok47_r1_retry import RetryReceipts,Capture,StopRun


class RetryReceiptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.ledger=RetryReceipts(self.root/'receipts',limit=23)
    def reserve(self,slot='test',prior=0):return self.ledger.reserve_attempt(slot,{'request':{'messages':['人工']}},prior)
    def test_initial_plus_three_limit_and_global_limit(self):
        for _ in range(4):self.ledger.finish(self.reserve(),'failed')
        with self.assertRaises(StopRun):self.reserve()
        for n in range(19):self.ledger.finish(self.reserve(str(n)),'completed')
        with self.assertRaises(StopRun):self.reserve('24th')
    def test_prior_failure_success_and_exact_payload_guards(self):
        self.ledger.finish(self.reserve(prior=1),'failed')
        with self.assertRaises(StopRun):self.ledger.reserve_attempt('test',{'request':{}},1)
        self.ledger.finish(self.reserve(prior=1),'failed')
        self.ledger.finish(self.reserve(prior=1),'completed')
        with self.assertRaises(StopRun):self.reserve(prior=1)
    async def test_runtime_timeout_cancellation_persists_terminal(self):
        item={'key':'artificial','prior_selector_failures':0}
        client=Capture(self.root,item,object(),self.ledger,True)
        client.client.complete=AsyncMock(side_effect=asyncio.CancelledError('generation_attempt_timeout'))
        model={'name':'grok-4.7','base_url':'https://api.uniapi.io/v1','max_tokens':8192}
        with patch('scripts.grok47_r1_retry.check'):
            with self.assertRaises(asyncio.CancelledError):await client.complete(model,[{'role':'user','content':'人工'}])
        rows=self.ledger.check();self.assertEqual(len(rows),1);self.assertEqual(rows[0]['status'],'failed')
        self.assertEqual(rows[0]['error'],'provider_timeout')
    async def test_manual_cancel_is_terminal_and_prevents_new_dispatch(self):
        client=Capture(self.root,{'key':'artificial','prior_selector_failures':0},object(),self.ledger,True)
        client.client.complete=AsyncMock(side_effect=asyncio.CancelledError())
        with patch('scripts.grok47_r1_retry.check'):
            with self.assertRaises(asyncio.CancelledError):await client.complete({'name':'grok-4.7','base_url':'https://api.uniapi.io/v1','max_tokens':8},[{'role':'user','content':'人工'}])
        self.assertEqual(self.ledger.rows()[0]['status'],'interrupted')
        with self.assertRaises(StopRun):self.reserve('other')


if __name__=='__main__':unittest.main()
