"""Exercise the native Workbench against the real loopback server and local provider markers."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestServer

from persona_runtime.chat import ModelCompletion
from persona_runtime.config import atomic_json
from persona_runtime.desktop import Workbench
from persona_runtime.models import generic_persona, identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.server import create_app
from persona_runtime.service import RuntimeService
from tests.test_m9r_native_retrieval import Provider


class LocalV8:
    def __init__(self):
        self.calls = []
        self.notices = []
        self.notice_hold = None
        self.hold = None

    async def complete(self, model, messages):
        if model['name'] != 'deepseek-v4-pro':
            raise AssertionError('wrong provider model')
        if model.get('_search_notice_profile'):
            self.notices.append(messages)
            if self.notice_hold:
                await self.notice_hold.wait()
            return ModelCompletion('N0', {'total_tokens':2}, {'finish_reason':'stop'})
        self.calls.append((model, messages))
        n = len(self.calls)
        if self.hold:
            await self.hold.wait()
        return ModelCompletion(f'B{n}-A\n\nB{n}-Z', {'total_tokens':2}, {'finish_reason':'stop'})


class WorkbenchLoopback(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = RuntimePaths(Path(self.temp.name))
        self.provider = LocalV8()
        self.app = create_app(self.paths,client=self.provider)
        self.server = TestServer(self.app,host='127.0.0.1')
        await self.server.start_server()
        self.cfg = self.app['config']
        self.cfg.update({'service_port':self.server.port,'model':{'base_url':'https://api.deepseek.com',
                         'name':'deepseek-v4-pro','hourly_limit':1000}})
        self.engine = self.app['engine']
        self.service = RuntimeService(self.app['store'])
        self.pid = self.service.create_persona(generic_persona())['id']
        self.service.use_persona(self.pid)
        self.window = None
        self.errors = []
        self.frames = []
        self.sealed = set()
        self.release_route = None
        self.addAsyncCleanup(self.dispose)
        self.open_window()

    def open_window(self):
        # Keep the actual chat widgets mapped offscreen. Only unrelated OS/service panels are idle.
        for method in ('poll','poll_organization','poll_profile','poll_batches','poll_deliveries'):
            self.enterContext(patch.object(Workbench,method,lambda *a,**kw:None))
        window = Workbench(self.paths)
        window.geometry('900x680+20000+20000')
        window.report_callback_exception = lambda *_: self.errors.append('tk_callback_error')
        self.window = window
        window.show_page('chat')
        window.load_chat()

    async def close_window(self):
        window = self.window
        if window is None:
            return
        window.closed = True
        for timer in window.tk.splitlist(window.tk.call('after','info')):
            window.after_cancel(timer)
        await asyncio.to_thread(window.hub.pool.shutdown,wait=True,cancel_futures=True)
        window.hub.drain()
        window.destroy()
        self.window = None

    async def dispose(self):
        if self.release_route:
            self.release_route.set()
        if self.provider.notice_hold:
            self.provider.notice_hold.set()
        if self.provider.hold:
            self.provider.hold.set()
        await self.close_window()
        await self.server.close()
        self.temp.cleanup()

    def record_frame(self):
        self.window.update()
        if self.window.current_page=='chat' and self.window.transcript.winfo_viewable():
            self.frames.append(self.window.transcript.get('1.0','end-1c'))
        self.assertFalse(self.errors)

    async def until(self, predicate, timeout=12):
        try:
            async with asyncio.timeout(timeout):
                while True:
                    self.record_frame()
                    if predicate():
                        return
                    await asyncio.sleep(.02)
        except TimeoutError:
            with self.app['store'].connection() as db:
                rows=[tuple(row) for row in db.execute('SELECT status,error_code FROM turns ORDER BY rowid')]
            self.fail(f'Workbench timeout: {rows}, main calls {len(self.provider.calls)}, '
                      f'{self.window.notice.get()}, {self.frames[-1:]!r}')

    async def submit(self, text, *, public=False):
        w = self.window
        await self.until(lambda:w.session_id is not None and w.chat_persona.current()>=0)
        if public:
            w.chat_public_task={'persona_id':w.persona_id,'session_id':w.session_id,'task':{'question':'P0'}}
        w.chat_input.insert('1.0',text)
        w.send_chat()
        # Explicitly close the rolling input batch through its regular domain action.
        await self.until(lambda:not w.chat_input.get('1.0','end-1c'))
        for batch in self.app['ingress'].list(self.pid,channel='desktop'):
            if batch['session_id']==w.session_id and batch['status']=='collecting' and batch['id'] not in self.sealed:
                self.sealed.add(batch['id'])
                self.app['ingress'].action(batch['id'],'seal',batch['revision'],identifier(),self.pid)

    async def test_b_native_body_followup_and_reopen(self):
        await self.submit('U1')
        await self.until(lambda:'B1-Z' in self.frames[-1])
        self.assertIn('B1-A\n\nB1-Z',self.frames[-1])
        self.assertEqual(self.provider.calls[0][0]['_companion_profile']['version'],'companion-runtime-v8')
        await self.submit('U2')
        await self.until(lambda:'B2-Z' in self.frames[-1])
        self.assertIn({'role':'assistant','content':'B1-A\n\nB1-Z'},self.provider.calls[1][1])
        session = self.window.session_id
        await self.close_window()
        self.open_window()
        await self.until(lambda:self.window.session_id==session and 'B2-Z' in self.frames[-1])
        self.assertEqual(self.frames[-1].count('B1-A'),1)
        self.assertEqual(self.frames[-1].count('B2-A'),1)
        self.assertEqual(len(self.provider.calls),2)
        with self.app['store'].connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE role='persona'").fetchone()[0],2)
            for table in ('expression_runs','dialogue_generation_stages'):
                self.assertEqual(db.execute(f'SELECT count(*) FROM {table}').fetchone()[0],0)

    async def test_cancel_then_new_message_and_persona_switch(self):
        self.provider.hold=asyncio.Event()
        await self.submit('C0')
        await self.until(lambda:bool(self.provider.calls) and any(t['status']=='running' for t in self.window.turns))
        self.window.stop_current_reply()
        await self.until(lambda:any(t.get('cancelled_at') for t in self.window.turns))
        self.provider.hold.set()
        self.assertNotIn('B1-A',self.frames[-1])
        await self.submit('C1')
        await self.until(lambda:'B2-Z' in self.frames[-1])
        first_session=self.window.session_id
        other=self.service.create_persona(generic_persona())['id']
        self.window.use_persona(other)
        await self.until(lambda:self.window.persona_id==other and self.window.session_id not in (None,first_session))
        self.assertNotIn('B2-A',self.frames[-1])
        self.window.use_persona(self.pid)
        await self.until(lambda:self.window.session_id==first_session and 'B2-Z' in self.frames[-1])
        self.assertEqual(self.frames[-1].count('B2-A'),1)
        self.assertEqual(len(self.provider.calls),2)

    def setup_web(self):
        org=self.app['organization']
        reader=self.engine.knowledge.current_policy(self.pid)
        processing=org.processing.policy(self.pid)
        policy=org.web.policy(self.pid)
        org.processing.enable_for_persona(self.pid,True,reader['revision'],processing['revision'],policy['revision'],identifier())
        settings=org.processing.native.policy(self.pid)
        org.processing.native.update(self.pid,True,settings['revision'],identifier())
        atomic_json(self.paths.home/'private/knowledge-router/config.json',{'base_url':'https://decision.fixture.invalid/v1',
                    'name':'jev-test','encrypted_key':'artificial-protected-jev'})
        self.release_route=asyncio.Event()
        self.need_web=True

        async def decision(model, payload):
            await self.release_route.wait()
            return ModelCompletion(json.dumps({'answers':{'route':{'choice':'web' if self.need_web else 'none','confidence':1}}}),
                                   {'total_tokens':1},{'network_route':'local_fixture'})
        org.routing.transport=decision
        seen=[]
        native=Provider()
        async def observe(model, prompt):
            seen.append((self.window.current_page,self.window.transcript.winfo_viewable(),
                         self.window.transcript.get('1.0','end-1c').count('N0')))
            return await native.complete(model,prompt)
        org.client=type('NativeProbe',(),{'complete':staticmethod(observe)})()
        return native,seen

    async def test_native_search_waits_for_paint_and_notice_survives_reopen(self):
        native,seen=self.setup_web()
        await self.submit('W0',public=True)
        self.window.show_page('home')
        self.release_route.set()
        await self.until(lambda:bool(self.provider.notices))
        for _ in range(30):
            self.record_frame()
            await asyncio.sleep(.025)
        self.assertEqual(seen,[],'native transport started while the chat page was hidden')
        self.window.show_page('chat')
        await self.until(lambda:bool(seen) and 'B1-Z' in self.frames[-1],timeout=20)
        self.assertEqual(seen,[('chat',1,1)])
        shown=self.frames[-1]
        self.assertEqual(shown.count('N0'),1)
        self.assertLess(shown.index('N0'),shown.index('B1-A'))
        self.assertEqual(len(self.provider.notices),1)
        await self.close_window()
        self.open_window()
        await self.until(lambda:'B1-Z' in self.frames[-1])
        self.assertEqual(self.frames[-1].count('N0'),1)
        self.assertEqual(len(native.calls),1)
        native_model, public_messages=native.calls[0]
        self.assertEqual(native_model['name'],'deepseek-flash')
        public_wire=json.dumps(public_messages)
        self.assertIn('P0',public_wire)
        self.assertNotIn('W0',public_wire)
        self.assertNotIn('N0',public_wire)
        self.assertIn('N0',json.dumps(self.provider.calls[0][1]))

    async def test_new_input_during_generation_preserves_b_order(self):
        self.provider.hold=asyncio.Event()
        await self.submit('Q0')
        await self.until(lambda:len(self.provider.calls)==1)
        await self.submit('Q1')
        await self.until(lambda:len(self.window.turns)==2)
        self.assertEqual(len(self.provider.calls),1)
        self.assertNotIn('B1-A',self.frames[-1])
        self.provider.hold.set()
        await self.until(lambda:'B2-Z' in self.frames[-1])
        self.assertEqual(len(self.provider.calls),2)
        self.assertIn({'role':'assistant','content':'B1-A\n\nB1-Z'},self.provider.calls[1][1])
        self.assertLess(self.frames[-1].index('B1-Z'),self.frames[-1].index('B2-A'))

    async def test_unseen_notice_reopens_without_a_second_provider_call(self):
        native,seen=self.setup_web()
        await self.submit('W1',public=True)
        self.window.show_page('home')
        self.release_route.set()
        await self.until(lambda:bool(self.provider.notices))
        await self.close_window()
        await asyncio.sleep(.6)
        self.assertFalse(native.calls)
        self.open_window()
        await self.until(lambda:bool(seen) and 'B1-Z' in self.frames[-1],timeout=20)
        self.assertEqual(seen,[('chat',1,1)])
        self.assertEqual(self.frames[-1].count('N0'),1)
        self.assertEqual(len(self.provider.notices),1)

    async def test_switch_before_notice_never_searches_or_leaks_old_preface(self):
        native,seen=self.setup_web()
        await self.submit('W2',public=True)
        first_session=self.window.session_id
        self.window.show_page('home')
        self.release_route.set()
        await self.until(lambda:bool(self.provider.notices))
        other=self.service.create_persona(generic_persona())['id']
        self.window.use_persona(other)
        await self.until(lambda:self.window.persona_id==other and self.window.session_id not in (None,first_session))
        self.window.show_page('chat')
        await self.until(lambda:len(self.frames)>0 and self.window.transcript.winfo_viewable())
        for _ in range(30):
            self.record_frame()
            await asyncio.sleep(.025)
        self.assertNotIn('N0',self.frames[-1])
        self.assertEqual(seen,[])
        self.assertEqual(len(self.provider.notices),1)
        with self.app['store'].connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE role='persona' AND text='N0'").fetchone()[0],0)

    async def test_new_accepted_input_prevents_stale_preface_and_search(self):
        native,seen=self.setup_web()
        self.release_route.set()
        self.provider.notice_hold=asyncio.Event()
        await self.submit('W3',public=True)
        await self.until(lambda:len(self.provider.notices)==1)
        self.need_web=False
        await self.submit('W4')
        self.provider.notice_hold.set()
        await self.until(lambda:'B1-Z' in self.frames[-1] and len(self.window.turns)==2)
        self.assertEqual(seen,[])
        self.assertEqual(len(native.calls),0)
        self.assertNotIn('N0',self.frames[-1])
        self.assertEqual(len(self.provider.calls),1)

    async def test_lost_ack_response_reuses_displayed_notice_and_search(self):
        from persona_runtime.desktop_client import TransportUnknown
        native,seen=self.setup_web()
        self.release_route.set()
        real=self.window.client.manage
        acks=[]
        def lose_first_response(action,**params):
            reply=real(action,**params)
            if action=='chat.notice.presented':
                acks.append(params)
                if len(acks)==1:
                    raise TransportUnknown('ID-ACK-UNKNOWN')
            return reply
        self.window.client.manage=lose_first_response
        await self.submit('W5',public=True)
        await self.until(lambda:'B1-Z' in self.frames[-1] and len(acks)>=1)
        # The caller lost the local response, though the server committed it.
        repeated=await asyncio.to_thread(real,'chat.notice.presented',**acks[0])
        self.assertTrue(repeated['published'])
        self.assertEqual(seen,[('chat',1,1)])
        self.assertEqual(self.frames[-1].count('N0'),1)
        self.assertEqual(len(self.provider.notices),1)
        self.assertEqual(len(native.calls),1)
        with self.app['store'].connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE role='persona' AND text='N0'").fetchone()[0],1)
