"""No network: exercise paid-cohort stop and durable accounting boundaries."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from scripts import grok47_r1_cohort as cohort
from persona_runtime.chat import ModelError
from persona_runtime.reply_batch import expression_snapshot


class CohortGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = cohort.ReceiptLedger(self.root/'receipts', limit=64)
        self.item = {'key':'synthetic-case'}
        self.client = cohort.Client(self.root,self.item,object(),self.ledger,True)
        self.model = {'name':'grok-4.7','base_url':'https://api.uniapi.io/v1',
            'max_tokens':8192,'_reply_mode':'natural_structured',
            '_expression_snapshot':expression_snapshot('natural_structured')}
        self.messages = [{'role':'user','content':'人工测试'}]
        self.check = patch.object(cohort,'check')
        self.check.start(); self.addCleanup(self.check.stop)

    async def test_remote_failure_is_recorded_once_and_not_retried(self):
        self.client.client.complete = AsyncMock(side_effect=ModelError('provider_empty_response'))
        with self.assertRaises(cohort.StopRun):
            await self.client.complete(self.model,self.messages)
        self.client.client.complete.assert_awaited_once()
        rows=self.ledger.rows()
        self.assertEqual(len(rows),1); self.assertEqual(rows[0]['status'],'failed')
        self.assertIn('provider_empty_response',rows[0]['error'])

    async def test_invalid_protocol_keeps_original_output_and_blocks(self):
        self.client.client.complete = AsyncMock(return_value=('这不是约定的JSON',{'total_tokens':8}))
        with self.assertRaises(cohort.StopRun):
            await self.client.complete(self.model,self.messages)
        row=self.ledger.rows()[0]
        self.assertEqual(row['status'],'failed'); self.assertEqual(row['output'],'这不是约定的JSON')
        self.assertEqual(row['usage']['total_tokens'],8)
        self.client.client.complete.assert_awaited_once()

    async def test_write_failure_prevents_dispatch(self):
        self.client.client.complete = AsyncMock()
        with patch.object(cohort.ReceiptLedger,'reserve',side_effect=cohort.StopRun('reservation_write_failed')):
            with self.assertRaises(cohort.StopRun): await self.client.complete(self.model,self.messages)
        self.client.client.complete.assert_not_awaited()

    async def test_existing_unfinished_receipt_prevents_dispatch(self):
        self.ledger.reserve('earlier',{'request':{},'kind':'selector'})
        self.client.client.complete=AsyncMock()
        with self.assertRaises(cohort.StopRun): await self.client.complete(self.model,self.messages)
        self.client.client.complete.assert_not_awaited()

    async def test_different_provider_rejected_before_reservation(self):
        self.client.client.complete=AsyncMock()
        with self.assertRaises(cohort.StopRun): await self.client.complete(self.model|{'name':'other'},self.messages)
        self.assertEqual(self.ledger.rows(),[]); self.client.client.complete.assert_not_awaited()

    async def test_65th_call_never_dispatched(self):
        for index in range(64):
            row=self.ledger.reserve(str(index),{'request':{},'kind':'main'})
            self.ledger.finish(row,'completed')
        self.client.client.complete=AsyncMock()
        with self.assertRaises(cohort.StopRun): await self.client.complete(self.model,self.messages)
        self.client.client.complete.assert_not_awaited()

    def test_blocked_gate_never_reopens(self):
        (self.root/'blocked.json').write_text(json.dumps({'status':'blocked'}))
        with patch.object(cohort,'EVIDENCE',self.root):
            with self.assertRaises(cohort.StopRun):cohort.gate_guard()


if __name__=='__main__':unittest.main()
