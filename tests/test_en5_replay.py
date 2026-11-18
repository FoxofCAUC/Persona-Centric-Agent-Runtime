"""Study-only replay guards with local marker input and an isolated ingress ledger."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from experiments.corpus_reply_study import save_once
from experiments.en5_experience_replay import completed, drive, intent, plan_for, sha, snapshot
from experiments.en5_experience_setup import bind_sessions
from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore
from persona_runtime.ingress import IngressService
from persona_runtime.models import RuntimeErrorBase, generic_persona
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from tests import test_en5_workbench_flow as flow


def markers():
    parts=[]
    for part in (1,2):
        turns=[]
        for n in range(1,16):
            words=[f'{part}-{n}-A']
            if n==1:words+=['M-B']
            turns.append({'n':n,'text':'\n'.join(words),'bubbles':[{'text':s} for s in words],
                          'public_task':{'question':'ID-P'} if n==1 else None})
        parts.append({'part':part,'turns':turns})
    return {'sessions':parts}


class Replay(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.paths=RuntimePaths(self.root/'home')
        self.cfg=ConfigStore(self.paths)
        self.cfg.initialize()
        self.store=Store(self.paths)
        self.store.initialize()
        svc=RuntimeService(self.store)
        pid=svc.create_persona(generic_persona())['id']
        svc.use_persona(pid)
        self.binding={'persona_id':pid,'study_id':'TEST','sessions':bind_sessions(self.paths,pid),
                      'instance_id':self.cfg.load()['instance_id'],'mode':'author_material_replay'}
        self.engine=ChatEngine(self.store,self.cfg,Mock())
        self.addCleanup(self.engine.knowledge.close)
        self.ingress=IngressService(self.engine)
        self.material=markers()
        self.plan=plan_for(self.material,self.binding,1,'F')
        self.sid=self.binding['sessions'][0]

    def accept(self,item,**changes):
        payload={'text':item['text'],'persona_id':self.binding['persona_id'],'session_id':self.sid,
                 'channel':'desktop','desktop_presentation':'tk-visible-v1','request_id':item['request_id']}
        if item['public_task'] is not None:payload['public_task']=item['public_task']
        return self.ingress.accept(payload|changes)

    def test_identity_scope_and_human_bubble_order(self):
        one,two=self.plan[0]
        self.assertNotEqual(one['request_id'],two['request_id'])
        self.assertNotEqual(one['request_id'],plan_for(self.material,self.binding,2,'F')[0][0]['request_id'])
        self.assertNotEqual(one['request_id'],plan_for(self.material,self.binding,1,'G')[0][0]['request_id'])
        self.assertIsNone(one['public_task'])
        self.assertEqual(two['public_task']['question'],'ID-P')
        self.accept(one)
        self.accept(two)
        self.accept(two)
        rows,batches,turns=snapshot(self.paths,self.binding,self.sid,self.plan)
        self.assertEqual(list(rows),[one['request_id'],two['request_id']])
        self.assertEqual(len(batches),1)
        self.assertFalse(turns)
        self.accept(self.plan[1][0],text='ID-MISMATCH')
        with self.assertRaisesRegex(RuntimeErrorBase,'input_prefix_mismatch'):
            snapshot(self.paths,self.binding,self.sid,self.plan)

    async def test_unconfirmed_click_blocks_new_dispatch(self):
        item=self.plan[0][0]
        path,fields=intent(self.root,self.binding,self.sid,1,item,'F')
        save_once(path,fields)
        w=Mock(paths=self.paths,current_page='chat',closed=False,tray_hidden=False,session_id=self.sid,
               persona_id=self.binding['persona_id'],chat_reference=None,chat_public_task=None)
        w.transcript.winfo_viewable.return_value=True
        w.state.return_value='normal'
        w.chat_persona.current.return_value=0
        w.hub.active={}
        w.chat_input.get.return_value=''
        with patch('experiments.en5_experience_replay.read_receipts',return_value={}):
            with self.assertRaisesRegex(RuntimeErrorBase,'submitted_position_unknown'):
                await drive(self.root,self.material,'F',lambda:None,w,None,self.binding,1)
        w.send_chat.assert_not_called()

    def test_only_exact_visible_main_advances(self):
        turn={'status':'completed','id':'T1','reply_event_id':'E1','text':'V','cancelled_at':None}
        self.assertFalse(completed({},turn))
        self.assertTrue(completed({'T1':{'main':{'source_id':'E1','body_sha256':sha(b'V')}}},turn))
        with self.assertRaisesRegex(RuntimeErrorBase,'main_display_changed'):
            completed({'T1':{'main':{'source_id':'E9','body_sha256':sha(b'V')}}},turn)
        with self.assertRaisesRegex(RuntimeErrorBase,'prior_turn_stopped'):
            completed({},turn|{'status':'interrupted'})

    def test_incomplete_or_unapproved_structure_rejected(self):
        self.material['sessions'][1]['turns'].pop()
        with self.assertRaisesRegex(RuntimeErrorBase,'turn_count'):
            plan_for(self.material,self.binding,2,'F')
        with self.assertRaisesRegex(RuntimeErrorBase,'mode'):
            plan_for(self.material,self.binding|{'mode':'author_desktop'},1,'F')


class NativeReplayEntry(unittest.IsolatedAsyncioTestCase):
    open_window=flow.ExperienceFlow.open_window
    close_window=flow.ExperienceFlow.close_window
    dispose=flow.ExperienceFlow.dispose
    record_frame=flow.ExperienceFlow.record_frame
    until=flow.ExperienceFlow.until
    asyncSetUp=flow.ExperienceFlow.asyncSetUp

    async def test_two_bubbles_use_normal_tk_submission_and_natural_seal(self):
        source=markers()
        binding=dict(self.binding,mode='author_material_replay')
        sid=binding['sessions'][0]
        await self.until(lambda:self.window.session_id is not None and self.window.chat_persona.current()>=0)
        if self.window.session_id!=sid:
            self.window.load_chat(session_id=sid)
            await self.until(lambda:self.window.session_id==sid)
        def stop_after_first():
            if self.provider.calls and read_receipts(self.paths,binding['study_id'],self.pid,sid):
                raise RuntimeErrorBase('local_test_segment_end')
        from persona_runtime.en5_display import read_receipts
        task=asyncio.create_task(drive(self.paths.home/'test-replay-evidence',source,'F',stop_after_first,self.window,
                                       self.app,binding,1))
        try:
            await self.until(task.done,timeout=25)
            with self.assertRaisesRegex(RuntimeErrorBase,'local_test_segment_end'):
                await task
            plan=plan_for(source,binding,1,'F')
            accepted,batches,turns=snapshot(self.paths,binding,sid,plan)
            self.assertEqual(len(accepted),2)
            self.assertEqual(len(batches),1)
            self.assertEqual(len(turns),1)
            self.assertEqual(len(self.provider.calls),1)
            self.assertEqual(set(accepted),{b['request_id'] for b in plan[0]})
        finally:
            if not task.done():task.cancel()
            await asyncio.gather(task,return_exceptions=True)
