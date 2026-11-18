"""Verify the live study boundary and reopening a finished continuous study."""
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from experiments.en5_scene_continuous import ContinuousEngine, reviewed, run
from experiments.en5_scene_continuous_review import report
from experiments.en5_decision_packet import file_hash
from persona_runtime.models import RuntimeErrorBase


class SceneBoundary(unittest.TestCase):
    def test_transport_markers_do_not_grant_real_use(self):
        ledger=SimpleNamespace(binding={'mode':'scene_reviewed_live'})
        with self.assertRaises(RuntimeErrorBase):
            ContinuousEngine.check_transports(ledger,SimpleNamespace(d3_offline_fixture=True),
                                             SimpleNamespace(offline_fixture=True))


@unittest.skipUnless(os.environ.get('PCAR_SCENE_CONTINUOUS_COMPLETED'),'private completed study not supplied')
class CompletedStudy(unittest.IsolatedAsyncioTestCase):
    async def test_reopen_preserves_four_sessions_and_first_receipts(self):
        root=Path(os.environ['PCAR_SCENE_CONTINUOUS_COMPLETED'])
        _,_,rev,_,_=reviewed(root)
        folder=rev/'continuous'
        first=report(root)
        files=[folder/p for p in ('results.private.json','blind-key.private.json','review/index.html',
                                  'review/evidence.html','candidate/scene-ledger/decisions.sqlite3',
                                  'candidate/home/private/en5-d3/desktop.sqlite3')]
        before=[file_hash(p) for p in files]
        with patch('experiments.en5_scene_continuous.JevTransport.complete',new_callable=AsyncMock) as jev, \
             patch('experiments.en5_scene_continuous.MainTransport.complete',new_callable=AsyncMock) as pro:
            await run(root)
            jev.assert_not_awaited()
            pro.assert_not_awaited()
        self.assertEqual(first,report(root))
        self.assertEqual(before,[file_hash(p) for p in files])
        self.assertEqual(first['summaries']['baseline']['main'],30)
        self.assertEqual(first['summaries']['candidate']['main'],30)
