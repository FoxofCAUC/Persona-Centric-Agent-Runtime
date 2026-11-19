"""Normal ingress, managed retrieval, and scene utterance receipts with local stubs."""
import asyncio
import hashlib
import json
import unittest
from unittest.mock import patch

from persona_runtime import scene_policy, tk_notice
from persona_runtime.archive import fingerprint
from persona_runtime.chat import ModelCompletion
from persona_runtime.config import atomic_json
from persona_runtime.ingress import IngressService
from persona_runtime.models import RuntimeErrorBase, identifier
from persona_runtime.scene_voice import SCENES, VERSION
from tests import test_m9r_native_retrieval as native


class Choice:
    def __init__(self, model):
        self.model = model
        self.wires = []

    async def complete(self, wire, *, on_dispatch, on_receipt, diagnostics):
        self.wires.append(wire)
        tick = asyncio.get_running_loop().time()
        on_dispatch(tick)
        on_receipt(tick+.13)
        diagnostics({'wire_ms':130})
        probabilities = {s:float(s=='daily') for s in SCENES}
        return 200,json.dumps({'answers':{'scene':{'type':'choice','choice':'daily','probabilities':probabilities}}})

    async def close(self):
        pass


class Main:
    output = 'ID-PRESENTATION-FIRST ALPHA TAG。ID-PRESENTATION-SECOND BETA TAG。'
    def __init__(self):
        self.calls = []
        self.notices = []
        self.failure = None

    async def complete(self, model, messages):
        if '_search_notice_profile' in model:
            self.notices.append(messages)
            return ModelCompletion('ID-NOTICE',{'total_tokens':1},{'finish_reason':'stop'})
        self.calls.append(messages)
        if self.failure:
            raise self.failure
        return ModelCompletion(self.output,{'total_tokens':1},{'finish_reason':'stop'})


class Delivery(unittest.IsolatedAsyncioTestCase):
    setUp = native.Integration.setUp
    decision_transport = native.Integration.decision_transport
    transport = native.Integration.transport

    async def asyncSetUp(self):
        self._debug = asyncio.get_running_loop().get_debug()
        asyncio.get_running_loop().set_debug(False)
        native.Integration.enable(self)
        self.need_web = False
        self.pid = self.persona['id']
        self.ingress = IngressService(self.chat)
        with self.store.connection() as db:
            revision = self.ingress.policy(db,self.pid,'desktop')['id']
            char, profile = scene_policy.head(db,self.pid)
        self.ingress.update_policy(self.pid,'desktop','legacy_per_message',revision,identifier(),
                                   companion_mode='minimal_im',reply_mode='context_full',
                                   context_mode='working_set',expression_policy_version=3)
        self.cfg.update({'model':{'name':scene_policy.PRO}})
        jev_path = self.paths.home/'private/knowledge-router/config.json'
        jev = json.loads(jev_path.read_bytes()) | {'name':scene_policy.JEV}
        atomic_json(jev_path,jev)
        self.choice = Choice(scene_policy.configured_jev(self.paths))
        self.chat.scene.transport = self.choice
        self.main = self.chat.client = Main()
        self.pub = self.chat.scene_publication.publisher
        self.root = self.paths.home/'approved-test-material'
        self.root.mkdir()
        self.cards = {s:'ID-CARD-'+s for s in SCENES}
        catalog = {'version':VERSION,'persona':self.pid,'character_version':char,'profile_sha256':profile,
                   'instruction':'ID-CHOOSE','criteria':{s:'ID-CRITERION-'+s for s in SCENES},
                   'cards':{s:fingerprint(body) for s,body in self.cards.items()}}
        self.rev = fingerprint(catalog)
        atomic_json(self.root/'catalog.json',catalog)
        for s, body in self.cards.items():
            atomic_json(self.root/(s+'.json'),{'body':body})
        approval = self.root/'author-approval.private.json'
        atomic_json(approval,{'version':'en6-scene-approval-v1','decision':'approved','catalog_sha256':self.rev,
                            'd4_evidence_sha256':'0'*64,'main':scene_policy.provider_identity(self.cfg.load()['model']),
                            'jev':scene_policy.provider_identity(jev)})
        self.approved = hashlib.sha256(approval.read_bytes()).hexdigest()

    async def asyncTearDown(self):
        await native.Integration.asyncTearDown(self)
        await self.chat.scene.close()
        asyncio.get_running_loop().set_debug(self._debug)

    def enable(self):
        return scene_policy.install(self.store,self.pid,self.root,self.rev,self.approved)

    def send(self, uid='ID-USER', *, session_id=None, public=None):
        info = {'channel':'desktop','text':uid,'persona_id':self.pid,'request_id':identifier(),
                'desktop_presentation':tk_notice.CONTRACT}
        if session_id:
            info['session_id']=session_id
        if public:
            info['public_task']={'question':public}
        receipt = self.ingress.accept(info)
        return self.chat.get(receipt['turn_id'])

    async def wait_for(self, turn, selector):
        async with asyncio.timeout(12):
            while True:
                view = self.chat.get(turn['id'])
                result = selector(view)
                if result:
                    return result
                if view['status'] in ('failed','interrupted'):
                    self.fail(str(view['error_code']))
                await asyncio.sleep(.025)

    def offer(self, turn, ordinal):
        args = (self.pid,turn['session_id'],turn['scene_speech']['utterance_id'])
        plan = self.pub.plan(*args)
        return args,self.pub.offer(*args,plan['id'],plan['revision'],ordinal,'ID-PRESENTER')

    def acknowledge(self, args, part, receipt=None):
        return self.pub.acknowledge(*args,part['id'],part['lease_id'],'ID-PRESENTER',receipt or identifier(),part['body_hash'])

    async def test_single_scene_and_only_confirmed_prefix_in_next_turn(self):
        self.enable()
        turn = self.send()
        await self.wait_for(turn,lambda v:v.get('scene_speech'))
        self.assertEqual(self.main.calls[0][0]['content'],self.cards['daily'])
        self.assertNotIn(self.cards['daily'],json.dumps(self.choice.wires))
        self.assertEqual(self.choice.wires[0]['state'],[{'role':'user','source':['current'],'text':'ID-USER'}])
        current = self.chat.get(turn['id'])
        self.assertIsNone(current['reply'])
        args, part = self.offer(current,0)
        receipt = identifier()
        self.acknowledge(args,part,receipt)
        self.assertTrue(self.acknowledge(args,part,receipt)['already_applied'])
        self.assertIsNone(self.chat.get(turn['id'])['reply'])
        later = self.send('ID-NEXT',session_id=turn['session_id'])
        await self.wait_for(later,lambda v:v.get('scene_speech'))
        self.assertEqual(self.chat.get(turn['id'])['reply'],part['body'])
        self.assertIn(part['body'],json.dumps(self.main.calls[1],ensure_ascii=False))
        self.assertNotIn('ID-PRESENTATION-SECOND',json.dumps(self.main.calls[1],ensure_ascii=False))
        self.assertIn(part['body'],json.dumps(self.choice.wires[1],ensure_ascii=False))

    async def test_web_preface_and_main_share_card_and_native_receives_only_public_question(self):
        self.enable()
        self.need_web = True
        turn = self.send('ID-USER ID-PUBLIC',public='ID-PUBLIC')
        item = await self.wait_for(turn,lambda _:self.chat.history_snapshot(turn['session_id']).get('pending_notices'))
        notice_args = item[0] | {'persona_id':self.pid,'session_id':turn['session_id']}
        preview = tk_notice.display(self.org.processing.search_notice,**notice_args)
        self.assertEqual(preview['text'],'ID-NOTICE')
        self.assertFalse(self.provider.calls)
        self.assertIn(self.cards['daily'],self.main.notices[0][0]['content'])
        self.assertTrue(tk_notice.display(self.org.processing.search_notice,**notice_args,acknowledge=True)['published'])
        await self.wait_for(turn,lambda v:v.get('scene_speech'))
        self.assertEqual(len(self.choice.wires),1)
        self.assertEqual(len(self.main.calls),1)
        self.assertEqual(len(self.provider.calls),1)
        wire = json.dumps(self.provider.calls[0][1],ensure_ascii=False)
        self.assertIn('ID-PUBLIC',wire)
        self.assertNotIn('ID-USER',wire)
        self.assertNotIn('ID-CARD-',wire)
        self.assertIn(self.cards['daily'],self.main.calls[0][0]['content'])
        self.assertIn('ID-NOTICE',self.main.calls[0][0]['content'])

    async def test_imported_history_reaches_jev_preface_and_main_only(self):
        from persona_runtime.import_personas import PersonaImports
        imports = PersonaImports(self.store,self.cfg)
        d = imports.library.add(b"## 1\n88101\n## 1\n88102\n## 2 [2018-12-12 09:45]\n88201\n## 2 [2018-12-12T10:30:00+02:00]\n88202\n> 88909\n")
        preview = imports.history.preview(d["id"],d["revision"],"A",d["mapping"],persona_id=self.pid)
        sid = imports.history.commit(preview,identifier(),True)["session_id"]
        self.enable()
        self.need_web = True
        turn = self.send("ID-USER ID-PUBLIC",session_id=sid,public="ID-PUBLIC")
        notices = await self.wait_for(turn,lambda _:self.chat.history_snapshot(sid).get("pending_notices"))
        notice = notices[0] | {"persona_id":self.pid,"session_id":sid}
        self.assertFalse(self.provider.calls)
        self.assertEqual([r["text"] for r in self.choice.wires[0]["state"] if "recent" in r["source"]],
                         ["88101","88102","88201","88202"])
        self.assertIn("88202",json.dumps(self.main.notices[0]))
        self.assertEqual(self.choice.wires[0]["state"][2]["recorded_at"], "2018-12-12T09:45:00+08:00")
        self.assertNotIn("recorded_at", self.choice.wires[0]["state"][1])
        self.assertNotIn("recorded_at", self.choice.wires[0]["state"][-1])
        tk_notice.display(self.org.processing.search_notice,**notice,acknowledge=True)
        await self.wait_for(turn,lambda v:v.get("scene_speech"))
        self.assertEqual(len(self.choice.wires),1)
        self.assertEqual(len(self.main.calls),1)
        self.assertIn("88202",json.dumps(self.main.calls[0]))
        self.assertNotIn("88909",json.dumps(self.main.calls[0]))
        self.assertNotIn("88202",json.dumps(self.provider.calls[0][1]))
        for wire in (self.main.calls[0],self.main.notices[0]):
            dates = json.loads(wire[0]["content"].splitlines()[-1])["history_recorded_at"]
            self.assertEqual([(wire[row["message_index"]]["content"],row["recorded_at"]) for row in dates],
                              [("88201\n\n88202","2018-12-12T09:45:00+08:00"),("88201\n\n88202","2018-12-12T10:30:00+02:00")])

    async def test_unknown_display_reopen_retains_receipt_and_does_not_replay(self):
        self.enable()
        turn = self.send()
        await self.wait_for(turn,lambda v:v.get('scene_speech'))
        await self.chat.wait()
        current = self.chat.get(turn['id'])
        args, first = self.offer(current,0)
        self.acknowledge(args,first)
        _, second = self.offer(current,1)
        self.assertTrue(second['lease_id'])
        self.chat.recover()
        final = self.chat.get(turn['id'])
        self.assertEqual(final['reply'],first['body'])
        self.assertEqual(final['scene_speech']['items'][1]['state'],'unknown')
        self.assertEqual(final['scene_speech']['state'],'closed')
        self.assertEqual(len(self.main.calls),1)
        self.assertEqual(len(self.choice.wires),1)

    async def test_late_receipt_keeps_fallback_and_selected_role(self):
        self.enable()
        complete = self.choice.complete
        release = asyncio.Event()
        async def late(*args,**kwargs):
            dispatch = kwargs['on_dispatch']
            dispatch(asyncio.get_running_loop().time())
            await release.wait()
            return await complete(*args,**(kwargs|{'on_dispatch':lambda _:None}))
        self.choice.complete = late
        try:
            with patch('persona_runtime.scene_runtime.WAIT_SECONDS',.04):
                turn = self.send()
                await self.wait_for(turn,lambda v:v.get('scene_speech'))
                self.assertIn(self.cards['fallback'],self.main.calls[0][0]['content'])
                release.set()
                await asyncio.gather(*self.chat.scene.jobs)
        finally:
            release.set()
        with self.store.connection() as db:
            route = scene_policy.read(db,'scene.route.'+turn['id'])
        self.assertEqual((route['scene'],route['reason']),('fallback','timeout'))
        self.assertIsNotNone(route['raw'])
        self.assertEqual(len(self.main.calls),1)

    async def test_paused_policy_uses_existing_v8_for_new_turn_only(self):
        self.enable()
        with self.store.connection(write=True) as db:
            scene_policy.pause(db,self.pid)
        turn = self.send()
        await self.chat.wait()
        finished = self.chat.get(turn['id'])
        self.assertEqual(finished['status'],'completed',finished['error_code'])
        self.assertNotIn('scene_speech',finished)
        self.assertFalse(self.choice.wires)
        self.assertNotIn('ID-CARD-',self.main.calls[0][0]['content'])

    async def test_unreviewed_card_and_missing_workbench_contract_are_rejected(self):
        policy = self.enable()
        bad = self.paths.home/'private/scene-voice'/policy['catalog_sha256']/'daily.json'
        atomic_json(bad,{'body':'ID-CHANGED'})
        with self.assertRaisesRegex(RuntimeErrorBase,'scene_workbench_required'):
            self.ingress.accept({'channel':'desktop','persona_id':self.pid,'request_id':identifier(),'text':'ID-USER'})
        turn = self.send()
        await self.chat.wait()
        row = self.chat.get(turn['id'])
        self.assertEqual(row['error_code'],'scene_card_changed')
        self.assertFalse(self.main.calls)

    async def test_unknown_main_never_runs_again(self):
        from persona_runtime.chat import ModelError
        self.enable()
        self.main.failure = ModelError('provider_timeout',diagnostics={'effect_unknown':True})
        turn = self.send()
        await self.chat.wait()
        self.assertEqual(self.chat.get(turn['id'])['error_code'],'provider_effect_unknown')
        with self.assertRaisesRegex(RuntimeErrorBase,'scene_first_position_preserved'):
            await self.chat.retry(turn['id'])
        self.assertEqual(len(self.choice.wires),1)
        self.assertEqual(len(self.main.calls),1)

    async def test_normal_workbench_renders_each_bubble_once(self):
        from aiohttp.test_utils import TestServer
        from persona_runtime.desktop import Workbench
        from persona_runtime.server import create_app
        self.enable()
        self.worker.cancel()
        await asyncio.gather(self.worker,return_exceptions=True)
        app=create_app(self.paths,self.main,foreground_only=True)
        app['engine'].scene.transport=self.choice
        app['organization'].routing.transport=self.decision_transport
        server=TestServer(app,host='127.0.0.1')
        await server.start_server()
        self.cfg.update({'service_port':server.port})
        window = Workbench(self.paths)
        window.geometry('900x680+20000+20000')
        errors=[]
        window.report_callback_exception=lambda *exc:errors.append(exc)
        window.current_page='chat'
        window.show_page('chat')
        window.load_chat()

        async def wait_for(check):
            async with asyncio.timeout(15):
                while True:
                    window.update()
                    if errors:
                        raise errors[0][1]
                    result=check()
                    if result:
                        return result
                    await asyncio.sleep(.025)
        try:
            await wait_for(lambda:window.session_id and window.persona_id==self.pid)
            window.chat_input.insert('1.0','ID-WINDOW')
            window.send_chat()
            def completed():
                turns=app['engine'].history(window.session_id)
                return next((t for t in turns if t['status']=='completed' and t.get('scene_speech')),None)
            result=await wait_for(completed)
            self.assertGreaterEqual(len(result['scene_speech']['visible']),2)
            self.assertEqual(result['reply'],''.join(t['text'] for t in result['scene_speech']['visible']))
            for part in result['scene_speech']['visible']:
                self.assertEqual(window.transcript.get('1.0','end').count(part['text']),1)
            self.assertEqual((len(self.choice.wires),len(self.main.calls)),(1,1))
        finally:
            window.closed=True
            for timer in window.tk.splitlist(window.tk.call('after','info')):
                window.tk.call('after','cancel',timer)
            await asyncio.to_thread(window.hub.pool.shutdown,wait=True,cancel_futures=True)
            window.hub.drain()
            window.destroy()
            await server.close()
