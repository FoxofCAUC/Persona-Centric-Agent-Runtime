import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock,patch

from persona_runtime.config import atomic_json
from scripts.m763_provider_gate import DirectoryGate
from scripts.m763_r04 import model_identity


class Lease(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.approved={'model':{'name':'synthetic-primary'},'character_selector':{'name':'synthetic-auxiliary'}}
        self.path=self.root/'initial.json'
        self.rows=[{'kind':kind,'normal':True,'checked_epoch':100,'model_fingerprint':model_identity(self.approved[key])}
                   for kind,key in (('main','model'),('auxiliary','character_selector'))]
        atomic_json(self.path,{'normal':True,'checks':self.rows});self.original=self.path.read_bytes()
        self.gate=DirectoryGate(self.path,self.approved,self.root)

    async def test_fresh_proof_has_zero_network_operations(self):
        with patch('scripts.m763_provider_gate.time.time',return_value=101),patch.object(self.gate,'_check',new_callable=AsyncMock) as check:
            self.assertEqual(await self.gate.ensure(),self.path);check.assert_not_called()
        self.assertEqual(self.path.read_bytes(),self.original)

    async def test_expiry_renews_once_and_preserves_original(self):
        rows=[dict(r,checked_epoch=700) for r in self.rows]
        with patch('scripts.m763_provider_gate.time.time',return_value=700),patch.object(self.gate,'_check',new_callable=AsyncMock,side_effect=rows) as check:
            renewed=await self.gate.ensure();self.assertEqual(check.await_count,2)
            self.assertNotEqual(renewed,self.path)
            self.assertEqual(await self.gate.ensure(),renewed);self.assertEqual(check.await_count,2)
        self.assertEqual(self.path.read_bytes(),self.original)
        self.assertEqual(json.loads(renewed.read_text())['model_calls'],0)

    async def test_failed_check_is_preserved_and_latched_without_retry(self):
        rows=[dict(self.rows[0],checked_epoch=700),dict(self.rows[1],normal=False,checked_epoch=700,failure_type='TimeoutError')]
        with patch('scripts.m763_provider_gate.time.time',return_value=700),patch.object(self.gate,'_check',new_callable=AsyncMock,side_effect=rows) as check:
            for _ in range(2):
                with self.assertRaisesRegex(ValueError,'provider_wait_after_failed_renewal'):await self.gate.ensure()
            self.assertEqual(check.await_count,2)
        self.assertFalse(json.loads(self.gate.path.read_text())['normal'])
        self.assertEqual(self.path.read_bytes(),self.original)

    async def test_mismatched_connection_does_not_refresh_into_permission(self):
        self.gate.identities['model']='changed'
        with patch.object(self.gate,'_check',new_callable=AsyncMock) as check:
            with self.assertRaisesRegex(ValueError,'provider_check_changed'):await self.gate.ensure()
            check.assert_not_called()

    async def test_short_remaining_lease_renews_before_registering_model_attempt(self):
        rows=[dict(r,checked_epoch=680) for r in self.rows]
        with patch('scripts.m763_provider_gate.time.time',return_value=680),patch.object(self.gate,'_check',new_callable=AsyncMock,side_effect=rows) as check:
            await self.gate.ensure();self.assertEqual(check.await_count,2)
