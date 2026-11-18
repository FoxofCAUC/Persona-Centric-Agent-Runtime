"""Exercise D3 through normal Tk submissions and loopback service using identifier-only transports."""
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestServer
from experiments.en5_d3_desktop import local_app
from experiments.en5_d3_presentation import D3Workbench, close_local_window, frames
from experiments.en5_decision_tool import MODE
from persona_runtime.archive import fingerprint
from persona_runtime.character import CharacterService
from persona_runtime.chat import ModelCompletion
from persona_runtime.config import ConfigStore
from persona_runtime.models import generic_persona, identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.profile import snapshot
from persona_runtime.service import RuntimeService
from persona_runtime.sessions import select
from persona_runtime.storage import Store
from tests import test_en4_workbench_loopback as workbench
from tests.test_en5_decision_repair import encoded_response


class LocalDecision:
    d3_offline_fixture=True

    def __init__(self):
        self.gate=None
        self.calls=[]
        self.invalid=False

    async def complete(self,model,wire,*,save_raw):
        self.calls.append(wire)
        if self.gate:await self.gate.wait()
        target=json.loads(wire['messages'][-1]['content'])['id']
        choice={'target':target,'action':'参与交流','task_state':'无待办'}
        if wire.get('tools'):
            raw=encoded_response(wire,args='{}' if self.invalid else json.dumps(choice,ensure_ascii=False))
            save_raw(raw,http_status=200,diagnostics={})
            return
        value={'model':'deepseek-flash','usage':{'total_tokens':1},'choices':[{'finish_reason':'stop',
            'message':{'content':'{}' if self.invalid else json.dumps(choice,ensure_ascii=False)}}]}
        save_raw(json.dumps(value).encode(),http_status=200,diagnostics={})


class LocalMain:
    d3_offline_fixture=True

    def __init__(self, reply):
        self.calls=[]
        self.reply=reply
        self.hold=None

    async def complete(self,model,messages):
        self.calls.append(messages)
        if self.hold:await self.hold.wait()
        return ModelCompletion(self.reply,{'total_tokens':1},{'finish_reason':'stop','response_model':model['name']})


class DesktopFlow(unittest.IsolatedAsyncioTestCase):
    record_frame=workbench.WorkbenchLoopback.record_frame
    until=workbench.WorkbenchLoopback.until
    submit=workbench.WorkbenchLoopback.submit

    async def asyncSetUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.paths=RuntimePaths(Path(self.tmp.name))
        self.cfg=ConfigStore(self.paths);self.cfg.initialize()
        self.cfg.update({'model':{'base_url':'https://api.deepseek.com','name':'deepseek-v4-pro','hourly_limit':1000}})
        self.store=Store(self.paths);self.store.initialize()
        self.service=RuntimeService(self.store)
        self.pid=self.service.create_persona(generic_persona())['id'];self.service.use_persona(self.pid)
        with self.store.connection(write=True) as db:
            sid=select(db,self.pid,'desktop',None)
            profile=fingerprint(snapshot(db,self.pid))
        self.sid=sid
        self.binding={'version':'en5-d3-local-v1','mode':'local_fixture','home':str(self.paths.home.resolve()),
            'instance_id':self.cfg.load()['instance_id'],'persona_id':self.pid,'profile_hash':profile,
            'character_version':CharacterService(self.store).current(self.pid)['id'],
            'sessions':{sid:{'branch':'candidate','display':'bubbles','inputs':[[fingerprint(t)] for t in ('U1','U2','U3')]}}}
        if self._testMethodName=='test_strict_tool_passes_one_decision_to_pro_and_rejects_invalid':
            self.binding['decision_mode']=MODE
        self.flash=LocalDecision(); self.provider=LocalMain('A1…B2？C3。')
        self.sealed=set();self.frames=[];self.errors=[];self.window=None
        for method in ('poll','poll_organization','poll_profile','poll_batches','poll_deliveries'):
            self.enterContext(patch.object(D3Workbench,method,lambda *args,**kwargs:None))
        await self.open_server()
        self.addAsyncCleanup(self.close)
        self.open_window()

    async def open_server(self):
        self.app,self.ledger=local_app(self.paths,self.binding,self.provider,self.flash,{'flash':'F-ID','pro':'P-ID'})
        self.server=TestServer(self.app,host='127.0.0.1');await self.server.start_server()
        self.cfg.update({'service_port':self.server.port})

    def open_window(self):
        w=D3Workbench(self.paths,self.ledger);self.window=w
        w.geometry('900x680+20000+20000')
        w.report_callback_exception=lambda *_:self.errors.append('tk_callback_error')
        w.show_page('chat');w.load_chat()

    async def close_window(self):
        if self.window is not None:
            await close_local_window(self.window)
            self.window=None

    async def close(self):
        if self.flash.gate:self.flash.gate.set()
        if self.provider.hold:self.provider.hold.set()
        try:
            await self.close_window()
        finally:
            await self.server.close()

    def decisions(self):
        with self.ledger.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM decisions ORDER BY rowid')]

    def paints(self):
        with self.ledger.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM visible ORDER BY rowid')]

    async def test_first_decision_bubbles_recovery_and_visible_history(self):
        await self.submit('U1')
        await self.until(lambda:len(self.paints())==3)
        self.assertIn('P-ID',self.provider.calls[0][0]['content'])
        self.assertEqual(self.decisions()[0]['reason'],'selected')
        receipts=self.paints()
        self.assertEqual([r['idx'] for r in receipts],[0,1,2])
        with self.store.connection() as db:
            row=db.execute('SELECT manifest_json FROM context_manifests WHERE turn_id=? AND task_type=?',
                           (receipts[0]['turn'],'dialogue')).fetchone()
            self.assertEqual(json.loads(row[0])['input_snapshot_hash'],fingerprint(self.provider.calls[0]))
        await self.close_window();await self.server.close();await self.open_server();self.open_window()
        await self.until(lambda:'C3。' in self.frames[-1])
        self.assertEqual(self.paints(),receipts)
        self.assertEqual(len(self.flash.calls),1)
        self.flash.invalid=True
        await self.submit('U2')
        await self.until(lambda:len(self.paints())==6)
        self.assertNotIn('P-ID',self.provider.calls[-1][0]['content'])
        self.assertIn({'role':'assistant','content':self.provider.reply},self.provider.calls[-1])
        self.assertEqual(self.decisions()[-1]['reason'],'invalid')

    async def test_strict_tool_passes_one_decision_to_pro_and_rejects_invalid(self):
        await self.submit('U1');await self.until(lambda:len(self.paints())==3)
        self.assertTrue(self.flash.calls[0]['tools'][0]['function']['strict'])
        self.assertEqual(len(self.provider.calls),1)
        handoff=json.loads(self.provider.calls[0][0]['content'].rsplit('\n\n',1)[1])
        self.assertEqual(handoff['target'],{'message_index':1,'role':'user','member':1})
        self.assertEqual(self.decisions()[0]['reason'],'selected')
        self.flash.invalid=True
        await self.submit('U2');await self.until(lambda:len(self.paints())==6)
        self.assertEqual(len(self.provider.calls),2)
        self.assertNotIn('P-ID',self.provider.calls[1][0]['content'])
        invalid=self.decisions()[-1]
        self.assertEqual(invalid['reason'],'invalid')
        self.assertIsNotNone(invalid['raw'])

    async def test_real_three_second_deadline_no_second_main_and_cancel(self):
        self.flash.gate=asyncio.Event()
        await self.submit('U1')
        await self.until(lambda:len(self.paints())>=1)
        self.assertNotIn('P-ID',self.provider.calls[0][0]['content'])
        rec=self.decisions()[0]
        self.assertEqual(rec['reason'],'deadline')
        self.assertGreaterEqual(self.paints()[0]['at']-rec['started'],2.99)
        self.flash.gate.set()
        await self.until(lambda:self.decisions()[0]['raw'] is not None)
        self.assertEqual(self.decisions()[0]['reason'],'deadline')
        self.assertEqual(len(self.provider.calls),1)
        self.window.stop_current_reply()
        with self.ledger.db() as db: self.assertEqual(db.execute('SELECT stopped FROM plans').fetchone()[0],1)
        prefix=''.join(x['text'] for x in frames(self.provider.reply,True)[:len(self.paints())])
        self.flash.gate=asyncio.Event()
        await self.submit('U2');await self.until(lambda:len(self.flash.calls)==2)
        await self.until(lambda:any(t['status']=='queued' and t['text']=='U2' for t in self.window.turns))
        self.window.stop_current_reply()
        await self.until(lambda:self.decisions()[-1]['reason']=='cancelled')
        self.flash.gate.set()
        await self.until(lambda:self.decisions()[-1]['raw'] is not None)
        self.assertEqual(len(self.provider.calls),1)
        self.flash.gate=None
        await self.submit('U3')
        await self.until(lambda:len(self.provider.calls)==2)
        self.assertNotIn({'role':'assistant','content':self.provider.reply},self.provider.calls[-1])
        self.assertIn({'role':'assistant','content':prefix},self.provider.calls[-1])

    async def test_role_change_prevents_main_dispatch(self):
        self.flash.gate=asyncio.Event()
        await self.submit('U1');await self.until(lambda:len(self.flash.calls)==1)
        other=self.service.create_persona(generic_persona())['id']
        self.service.use_persona(other)
        self.flash.gate.set()
        await self.until(lambda:not self.app['engine'].tasks)
        self.assertEqual(len(self.provider.calls),0)

    async def test_source_withdrawal_during_flash_blocks_main(self):
        from persona_runtime.memory_policy import exclude_sources
        self.flash.gate=asyncio.Event()
        await self.submit('U1');await self.until(lambda:len(self.flash.calls)==1)
        with self.store.connection(write=True) as db:
            event=db.execute('SELECT user_event_id FROM turns WHERE session_id=?',(self.sid,)).fetchone()[0]
            exclude_sources(db,self.pid,[event],'author_withdrawn')
        self.flash.gate.set()
        await self.until(lambda:not self.app['engine'].tasks)
        self.assertFalse(self.provider.calls)
        self.assertFalse(self.paints())

    async def test_unreviewed_input_is_not_dispatched(self):
        await self.submit('OUTSIDE')
        def failed():
            with self.store.connection() as db:
                row=db.execute("SELECT error_code FROM turns WHERE status='failed'").fetchone()
                return row and row[0]=='en5_decision_desktop_unreviewed_input'
        await self.until(failed)
        self.assertFalse(self.provider.calls)
        self.assertFalse(self.flash.calls)

    async def test_role_switch_while_main_is_in_flight_keeps_body_out_of_history(self):
        self.provider.hold=asyncio.Event()
        await self.submit('U1');await self.until(lambda:len(self.provider.calls)==1)
        other=self.service.create_persona(generic_persona())['id']
        self.service.use_persona(other)
        self.provider.hold.set()
        await self.until(lambda:not self.app['engine'].tasks)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE role='persona'").fetchone()[0],0)
        self.assertFalse(self.paints())

    async def test_restart_preserves_unknown_flash_attempt_without_resending(self):
        await self.close_window();await self.server.close()
        with self.ledger.db() as db:
            db.execute("INSERT INTO decisions(turn,scope,wire,started) VALUES('T','S','{}',?)",(time.time(),))
        await self.open_server();self.open_window()
        decision=self.app['engine'].decision
        def unexpected():self.fail('unexpected dispatch')
        reason,choice=await decision.decide({'id':'T'},'S',[],{},unexpected)
        self.assertEqual((reason,choice),('restart_unknown',None))
        self.assertFalse(self.flash.calls)

    def test_code_formula_and_quotes_keep_original_characters(self):
        for text in ('A1…B2？C3。','前。'+'$A?B$'+'。后','“A？B”！Z','def fn():\n    return 1'):
            parts=frames(text,True)
            self.assertEqual(''.join(p['text'] for p in parts),text)
        self.assertEqual(len(frames('def fn():\n    return 1',True)),1)
