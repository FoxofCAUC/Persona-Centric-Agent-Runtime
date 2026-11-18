"""Local markers only. Exercise the isolated scene choice and both actual speech entry points."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestServer
from experiments.en5_scene_desktop import SceneDisplayLedger, SceneEngine, SceneWorkbench
from experiments.en5_scene_route import SceneRouter, SceneSet
from experiments.en5_scene_voice import SCENES, VERSION, context, decision
from persona_runtime.archive import fingerprint
from persona_runtime.config import atomic_json
from persona_runtime.models import RuntimeErrorBase, generic_persona, identifier
from persona_runtime.memory_policy import exclude_sources
from persona_runtime.server import create_app
from tests import test_en4_workbench_loopback as workbench
from tests import test_en5_d3_desktop as desktop


def assets(folder, persona='PID', **more):
    bodies={k:'ID-'+k for k in SCENES}
    catalog={'version':VERSION,'persona':persona,'instruction':'J0','criteria':{k:'J'+str(n) for n,k in enumerate(SCENES)},
             'cards':{k:fingerprint(v) for k,v in bodies.items()},**more}
    atomic_json(folder/'catalog.json',catalog)
    for key,body in bodies.items():
        atomic_json(folder/(key+'.json'),{'body':body})
    return SceneSet(folder,fingerprint(catalog),persona)


def response(scene='help',p=.88):
    probs={k:p if k==scene else (1-p)/4 for k in SCENES}
    return json.dumps({'answers':{'scene':{'type':'choice','choice':scene,'probabilities':probs}},
                       'usage':{'total_tokens':1}})


class LocalScene:
    offline_fixture=True

    def __init__(self):
        self.wires=[]
        self.hold=None
        self.chosen='help'

    async def __call__(self,wire):
        self.wires.append(wire)
        if self.hold:
            await self.hold.wait()
        return 200,response(self.chosen)


class Protocol(unittest.IsolatedAsyncioTestCase):
    async def test_restart_after_unknown_dispatch_does_not_send_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cards=assets(root/'cards');wire={'model':'J0'}
            router=SceneRouter(root/'db',cards)
            with router.db() as db:
                db.execute("INSERT INTO routes(turn,scope,revision,started,request_hash) VALUES('T','S',?,1,?)",
                           (cards.revision,fingerprint(wire)))
            await router.close()
            router=SceneRouter(root/'db',cards)
            try:
                transport=LocalScene()
                self.assertEqual(await router.resolve('T','S',wire,transport,lambda:None),('fallback','ID-fallback'))
                self.assertEqual(transport.wires,[])
                with router.db() as db:
                    self.assertEqual(db.execute('SELECT reason FROM routes').fetchone()[0],'restart_unknown')
            finally:
                await router.close()

    def test_probability_and_budget(self):
        self.assertEqual(decision(response(p=.60)),('help','selected'))
        self.assertEqual(decision(response(p=.59)),('fallback','uncertain'))
        self.assertEqual(decision('{}'),('fallback','invalid'))
        def target(role,source,slot,text):
            return {'role':role,'origins':source,'pro_target':{'message_index':slot},'text':text}
        targets=[target('user',['reference','recent'],1,'REF'),target('assistant',['recent'],2,'A'*3910),
                 target('user',['recent'],3,'U2'),target('assistant',['recent'],4,'A2'),
                 target('user',['current'],5,'U3'),target('user',['current'],5,'U4')]
        rows=context(targets)
        self.assertEqual([r['text'] for r in rows],['REF','U2','A2','U3','U4'])
        self.assertEqual(rows[0]['source'],['reference'])
        self.assertIsNone(context(targets+[target('user',['current'],5,'U'*4001)]))

    async def test_timeout_late_cancel_reopen_and_withdraw(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); cards=assets(root/'cards'); transport=LocalScene()
            transport.hold=asyncio.Event()
            router=SceneRouter(root/'db',cards,timeout=.04)
            try:
                self.assertEqual(await router.resolve('t','s',{},transport,lambda:None),('fallback','ID-fallback'))
                task=asyncio.create_task(router.resolve('u','s',{},transport,lambda:None))
                while len(transport.wires)<2:
                    await asyncio.sleep(.001)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                transport.hold.set()
                await router.close()
            reopened=SceneRouter(root/'db',cards)
            try:
                with reopened.db() as db:
                    r=db.execute('SELECT reason,http,raw FROM routes WHERE turn=?',('t',)).fetchone()
                    self.assertEqual((r['reason'],r['http'],r['raw']),('timeout',200,response()))
                self.assertEqual(await reopened.resolve('t','s',{},transport,lambda:None),('fallback','ID-fallback'))
                with self.assertRaises(RuntimeErrorBase):reopened.locked('u','s')
                with self.assertRaises(RuntimeErrorBase):reopened.locked('t','other')
                (cards.directory/'fallback.withdrawn').touch()
                with self.assertRaises(RuntimeErrorBase):reopened.locked('t','s')
                self.assertEqual(len(transport.wires),2)
            finally:
                await reopened.close()


class Desktop(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=desktop.DesktopFlow.asyncSetUp
    close=desktop.DesktopFlow.close
    close_window=desktop.DesktopFlow.close_window
    record_frame=desktop.DesktopFlow.record_frame
    until=desktop.DesktopFlow.until
    submit=desktop.DesktopFlow.submit
    paints=desktop.DesktopFlow.paints
    setup_web=workbench.WorkbenchLoopback.setup_web

    def open_window(self):
        self.window=SceneWorkbench(self.paths,self.ledger)
        self.window.geometry('900x680+20000+20000')
        self.window.report_callback_exception=lambda *exc:self.errors.append(str(exc[1]))
        self.window.show_page('chat')
        self.window.load_chat()

    async def open_server(self):
        self.provider=workbench.LocalV8()
        self.provider.d3_offline_fixture=True
        self.transport=LocalScene()
        self.cards=assets(self.paths.home/'private/cards',self.pid,character_version=self.binding['character_version'],
                          profile_sha256=self.binding['profile_hash'])
        self.binding.update(mode='scene_fixture',scene_revision=self.cards.revision,jev_model='jev-test')
        if self._testMethodName=='test_grouped_input':
            self.binding['sessions'][self.sid]['inputs'][0].append(fingerprint('U1b'))
        self.ledger=SceneDisplayLedger(self.paths,self.binding)
        router=SceneRouter(self.paths.home/'private/scene-decisions',self.cards)
        self.addAsyncCleanup(router.close)
        def factory(store,config,client,**kw):
            return SceneEngine(store,config,client,ledger=self.ledger,router=router,scene_transport=self.transport,**kw)
        self.app=create_app(self.paths,client=self.provider,engine_factory=factory)
        self.engine=self.app['engine']
        self.server=TestServer(self.app,host='127.0.0.1')
        await self.server.start_server()
        self.cfg.update({'service_port':self.server.port})

    async def test_workbench_single_scene_and_visible_history(self):
        await self.submit('U1')
        await self.until(lambda:len(self.paints())==2)
        self.assertEqual(self.provider.calls[0][1][0]['content'],'ID-help')
        self.transport.chosen='daily'
        await self.submit('U2')
        await self.until(lambda:len(self.paints())==4)
        self.assertEqual(self.provider.calls[-1][1][0]['content'],'ID-daily')
        self.assertEqual([s['text'] for s in self.transport.wires[-1]['state']],['U1','B1-A\n\nB1-Z','U2'])
        self.assertNotIn('ID-help',json.dumps(self.transport.wires))
        with self.store.connection() as db:
            t=dict(db.execute('SELECT * FROM turns ORDER BY rowid DESC LIMIT 1').fetchone())
        self.assertTrue(self.ledger.can_present(t['id']))
        other=self.service.create_persona(generic_persona())['id']
        self.service.use_persona(other)
        self.assertFalse(self.ledger.can_present(t['id']))
        self.service.use_persona(self.pid)
        with self.store.connection(write=True) as db:
            exclude_sources(db,self.pid,[t['user_event_id']],'withdrawn')
        self.assertFalse(self.ledger.can_present(t['id']))

    async def test_explicit_reference_with_recent_event(self):
        await self.submit('U1')
        await self.until(lambda:len(self.paints())==2)
        with self.store.connection() as db:
            tid=db.execute('SELECT id FROM turns').fetchone()[0]
        self.window.chat_reference={'persona_id':self.pid,'target':{'kind':'turn','id':tid}}
        await self.submit('U2')
        await self.until(lambda:len(self.paints())==4)
        rows=self.transport.wires[-1]['state']
        self.assertEqual([s['text'] for s in rows],['U1','B1-A\n\nB1-Z','U2'])
        self.assertEqual(set(rows[0]['source']),{'recent','reference'})
        self.assertIn('B1-A',self.provider.calls[-1][1][0]['content'])

    async def test_grouped_input(self):
        w=self.window
        await self.until(lambda:w.session_id==self.sid and w.chat_persona.current()>=0)
        for body in ('U1','U1b'):
            w.chat_input.insert('1.0',body)
            w.send_chat()
            await self.until(lambda:not w.chat_input.get('1.0','end-1c'))
        batch=next(b for b in self.app['ingress'].list(self.pid,channel='desktop') if b['status']=='collecting')
        self.app['ingress'].action(batch['id'],'seal',batch['revision'],identifier(),self.pid)
        await self.until(lambda:len(self.paints())==2)
        self.assertEqual(len(self.transport.wires),1)
        self.assertEqual([r['text'] for r in self.transport.wires[0]['state']],['U1','U1b'])

    async def test_web_notice_and_reply_share_one_scene(self):
        native,seen=self.setup_web()
        await self.submit('U1',public=True)
        self.window.show_page('home')
        self.release_route.set()
        await self.until(lambda:bool(self.provider.notices))
        self.assertEqual(seen,[])
        self.window.show_page('chat')
        await self.until(lambda:bool(seen) and len(self.paints())==2,timeout=20)
        self.assertEqual(seen,[('chat',1,1)])
        self.assertEqual(len(self.transport.wires),1)
        for messages in (self.provider.notices[0],self.provider.calls[0][1]):
            self.assertTrue(messages[0]['content'].startswith('ID-help'))
            self.assertEqual(messages[0]['content'].count('ID-help'),1)
            self.assertNotIn('ID-daily',str(messages))
        from persona_runtime.knowledge_translation import GUIDANCE
        self.assertNotIn(GUIDANCE,self.provider.calls[0][1][0]['content'])
        self.assertNotIn('ID-help',str(native.calls))
