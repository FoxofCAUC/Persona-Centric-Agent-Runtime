"""Regression checks for rendered history and the authorized D4 replay."""
from contextlib import closing,contextmanager
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch

from persona_runtime.models import RuntimeErrorBase
from experiments.en5_d3_live import replay_plans
from experiments.en5_scene_desktop import SceneEngine
from experiments.en5_scene_d4 import materials,run
from experiments.en5_scene_d4_report import collect
from experiments.en5_decision_packet import file_hash


class VisibleHistory(unittest.TestCase):
    def test_rendered_history_has_exact_displayed_prefix(self):
        with closing(sqlite3.connect(':memory:')) as db:
            db.row_factory=sqlite3.Row
            db.executescript('CREATE TABLE plans(turn,session,event,frames);CREATE TABLE visible(turn,idx);')
            frames=[{'text':'FIRST。'},{'text':'SECOND。'}]
            db.execute('INSERT INTO plans VALUES(?,?,?,?)',('T','S','E',json.dumps(frames)))
            db.execute('INSERT INTO visible VALUES(?,0)',('T',))
            @contextmanager
            def side():
                yield db
            engine=SimpleNamespace(ledger=SimpleNamespace(db=side))
            row={'id':'T','session_id':'S'}
            event={'kind':'generated_reply','id':'E','text':'**FIRST**。SECOND。'}
            self.assertEqual(SceneEngine.shown(engine,None,row,event),'FIRST。')
            with self.assertRaises(RuntimeErrorBase):
                SceneEngine.shown(engine,None,row,event|{'text':'FIRST。OTHER。'})


class TargetedScope(unittest.TestCase):
    def test_replay_slice_keeps_the_original_public_slot_in_its_session(self):
        script={'sessions':[{'part':part,'turns':[
            {'n':n,'text':f'ID-{part}-{n}','bubbles':[{'text':f'ID-{part}-{n}'}],
             'public_task':{'question':'Q0'} if n==5 else None}
            for n in range(1,16)]} for part in (1,2)]}
        scope={'mode':'author_material_replay','sessions':['A','B'],'study_id':'V','instance_id':'I'}
        full=replay_plans(script,scope,'M')
        only=replay_plans(script,scope,'M',{'part':2,'through':6})
        self.assertEqual(only,{'B':full['B'][:6]})
        self.assertEqual([g[0]['n'] for g in only['B'] if g[-1]['public_task']],[5])
        explicit=replay_plans(script,scope,'M',{'part':2,'from':5,'through':6})
        self.assertEqual(explicit,{'B':full['B'][4:6]})


@unittest.skipUnless(os.getenv('PCAR_D4_COMPLETED_ROOT') and os.getenv('PCAR_D4_STYLE_FEEDBACK'),
                     'private D4 receipts not supplied')
class CompletedD4(unittest.IsolatedAsyncioTestCase):
    async def check_reopen(self,targeted,author_input=None):
        root=Path(os.environ['PCAR_D4_COMPLETED_ROOT']);feedback=Path(os.environ['PCAR_D4_STYLE_FEEDBACK'])
        *_,folder,_=materials(root,feedback,targeted=targeted,author_input=author_input)
        result=collect(root,feedback,targeted=targeted,author_input=author_input)
        files=[folder/name for name in ('verified/results.private.json','verified/review/index.html','candidate/scene-ledger/decisions.sqlite3',
                                        'candidate/home/private/en5-d3/desktop.sqlite3')]
        hashes=[file_hash(p) for p in files]
        with patch('experiments.en5_scene_d4.D4Speech.complete',new_callable=AsyncMock) as main, \
             patch('experiments.en5_scene_d4.JevTransport.complete',new_callable=AsyncMock) as router, \
             patch('experiments.en5_scene_d4.DeepSeekNativeClient.complete',new_callable=AsyncMock) as native, \
             patch('experiments.en5_scene_d4.web_tool',new_callable=AsyncMock) as tool:
            await run(root,feedback,targeted=targeted,author_input=author_input)
            for client in (main,router,native,tool):client.assert_not_awaited()
        self.assertEqual(result,collect(root,feedback,targeted=targeted,author_input=author_input))
        self.assertEqual(hashes,[file_hash(p) for p in files])
        self.assertEqual(result['turns'],2 if author_input else 6 if targeted else 30)

    async def test_reopen_keeps_first_receipts_and_actual_web_decisions(self):
        await self.check_reopen(False)

    @unittest.skipUnless(os.getenv('PCAR_D4_TARGETED_COMPLETED'),'private targeted receipts not supplied')
    async def test_targeted_resume_keeps_earlier_d4_evidence(self):
        await self.check_reopen(True)

    @unittest.skipUnless(os.getenv('PCAR_D4_AUTHOR_INPUT_COMPLETED'),'private author-requested lookup not supplied')
    async def test_author_query_reopen_keeps_first_replies(self):
        await self.check_reopen(False,Path(os.environ['PCAR_D4_AUTHOR_INPUT_COMPLETED']))
