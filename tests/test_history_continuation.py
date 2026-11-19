"""Exercise the ordinary desktop reply path using opaque, offline model doubles."""
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from persona_runtime.archive import read_exact, scope
from persona_runtime.chat import ChatEngine, ModelCompletion
from persona_runtime.config import ConfigStore
from persona_runtime.import_personas import PersonaImports
from persona_runtime.ingress import IngressService
from persona_runtime.models import generic_persona, identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from persona_runtime.tk_notice import CONTRACT


class ReplyDouble:
    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.resume = asyncio.Event()
        self.resume.set()

    async def complete(self,model,messages):
        self.calls.append(messages)
        self.entered.set()
        await self.resume.wait()
        return ModelCompletion(str(int(messages[-1]["content"])+1000), {"total_tokens":1}, {"finish_reason":"stop"})


class DesktopHistory(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.paths = RuntimePaths(Path(tmp.name))
        self.config = ConfigStore(self.paths)
        self.config.initialize()
        self.config.update({"model":{"name":"deepseek-v4-pro","base_url":"https://api.deepseek.com","api_key":"101"}})
        self.store = Store(self.paths)
        self.store.initialize()
        self.service = RuntimeService(self.store)
        self.imports = PersonaImports(self.store,self.config)
        self.client = ReplyDouble()
        self.engine = ChatEngine(self.store,self.config,self.client)
        self.ingress = IngressService(self.engine)
        self.addCleanup(self.engine.cognitive.characters.close)
        self.addAsyncCleanup(self.engine.scene.close)

    def new_role(self):
        pid = self.service.create_persona(generic_persona())["id"]
        self.service.use_persona(pid)
        with self.store.connection() as db:
            policy = self.ingress.policy(db,pid,"desktop")
        self.ingress.update_policy(pid,"desktop","legacy_per_message",policy["id"],identifier(),
                                   companion_mode="minimal_im",reply_mode="context_full",
                                   context_mode="working_set",expression_policy_version=3)
        return pid

    def restore(self, pid, raw):
        imported = self.imports.library.add(raw)
        preview = self.imports.history.preview(ident=imported["id"],revision=imported["revision"],
            self_role="A",mapping=imported["mapping"],persona_id=pid,timezone_offset=480)
        return self.imports.history.commit(preview,identifier(),True)["session_id"]

    def send(self,pid,sid,number):
        return self.ingress.accept({"channel":"desktop","persona_id":pid,"session_id":sid,
            "desktop_presentation":CONTRACT,"request_id":identifier(),"text":str(number)})["turn_id"]

    async def exchange(self,pid,sid,number):
        tid = self.send(pid,sid,number)
        await asyncio.wait_for(self.engine.wait(),15)
        turn = self.engine.get(tid)
        speech = turn.get("scene_speech")
        if speech:
            for item in speech["items"]:
                publisher = self.engine.scene_publication.publisher
                offer = publisher.offer(pid,sid,speech["utterance_id"],speech["publication_id"],
                                        speech["revision"],item["ordinal"],"101")
                self.assertEqual(offer["state"],"offered")
                publisher.acknowledge(pid,sid,speech["utterance_id"],offer["id"],offer["lease_id"],
                                      "101",identifier(),offer["body_hash"])
            turn = self.engine.get(tid)
        self.assertEqual(turn["status"],"completed", turn["error_code"])
        return [(m["role"],m["content"]) for m in self.client.calls[-1][1:]]

    async def test_native_and_import_match_for_six_successive_desktop_turns(self):
        native = self.new_role()
        sid = self.engine.session("desktop")["session_id"]
        await self.exchange(native,sid,101)
        await self.exchange(native,sid,102)
        expected = [await self.exchange(native,sid,i) for i in range(103,109)]
        imported = self.new_role()
        restored = self.restore(imported,b"## X\n101\n## Y\n1101\n## X\n102\n## Y\n1102\n")
        actual = [await self.exchange(imported,restored,i) for i in range(103,109)]
        self.assertEqual(actual,expected)
        self.assertEqual(len(self.client.calls),14)

    async def test_reference_and_late_completion_after_withdrawal(self):
        pid = self.new_role()
        sid = self.restore(pid,b"## X\n301\n## Y\n401\n## Y\n402\n> 909\n## X\n302\n")
        with self.store.connection() as db:
            eid = db.execute("SELECT id FROM events WHERE session_id=? AND role='persona' ORDER BY seq DESC LIMIT 1",(sid,)).fetchone()[0]
            group = read_exact(db,scope(pid,"desktop"),{"kind":"event","id":eid})
            self.assertTrue(group["usable"])
        self.client.resume.clear()
        tid = self.ingress.accept({"channel":"desktop","persona_id":pid,"session_id":sid,"text":"701",
            "desktop_presentation":CONTRACT,"request_id":identifier(),"reference":{"kind":"event","id":eid}})["turn_id"]
        await asyncio.wait_for(self.client.entered.wait(),8)
        wire = self.client.calls[-1]
        self.assertTrue(any("402" in m["content"] for m in wire))
        self.assertFalse(any("909" in m["content"] for m in wire))
        status = self.imports.history.status(pid,sid)
        self.imports.history.update(pid,sid,status["revision"],"withdraw",True)
        self.client.resume.set()
        await asyncio.wait_for(self.engine.wait(),15)
        self.assertIsNone(self.engine.get(tid)["reply_event_id"])
        with self.store.connection() as db:
            self.assertNotEqual(read_exact(db,scope(pid,"desktop"),{"kind":"event","id":eid})["status"],"found")
        next_wire = await self.exchange(pid,sid,702)
        self.assertFalse(any("402" in body for _,body in next_wire))

    async def test_unpresented_history_based_reply_is_stopped_after_withdrawal(self):
        pid = self.new_role()
        sid = self.restore(pid,b"## X\n301\n## X\n302\n## Y\n401\n")
        tid = self.send(pid,sid,701)
        await asyncio.wait_for(self.engine.wait(),15)
        turn = self.engine.get(tid)
        self.assertIsNone(turn["reply_event_id"])
        speech = turn["scene_speech"]
        self.assertEqual(speech["state"], "pending")
        self.imports.history.update(pid,sid,1,"withdraw",True)
        offer = self.engine.scene_publication.publisher.offer(pid,sid,speech["utterance_id"],
            speech["publication_id"],speech["revision"],0,"101")
        self.assertEqual(offer["state"],"stopped")
        self.assertIsNone(self.engine.get(tid)["reply_event_id"])


    async def test_main_time_index_points_only_to_retained_original_bubbles(self):
        pid = self.new_role()
        raw = (b"## 1 [2017-03-04 19:11]\n" + b"5"*7800
               + b"\n## 2 [2017-03-04 19:12]\n88821\n## 2 [2017-03-04T19:13+03:00]\n88822")
        sid = self.restore(pid,raw)
        await self.exchange(pid,sid,903)
        wire = self.client.calls[-1]
        times = json.loads(wire[0]["content"].splitlines()[-1])["history_recorded_at"]
        retained = [(wire[t["message_index"]]["content"],t["recorded_at"]) for t in times]
        self.assertEqual(retained,[("88821\n\n88822","2017-03-04T19:12:00+08:00"),("88821\n\n88822","2017-03-04T19:13:00+03:00")])
        self.assertEqual(wire[-1]["content"],"903")

    async def test_same_speaker_bursts_match_reviewed_native_history_format(self):
        from persona_runtime.turn_input import render_input
        pid = self.new_role()
        sid = self.restore(pid,b"## X [2018-02-01 01:02]\n701\n## X [2018-02-01 01:03]\n702\n"
                               b"## Y [2018-02-01 01:04]\n801\n## Y [2018-02-01 01:05]\n802")
        await self.exchange(pid,sid,901)
        with self.store.connection() as db:
            members = [dict(r) for r in db.execute("SELECT id,text FROM events WHERE session_id=? AND role='user' AND id LIKE 'imported.%' ORDER BY seq",(sid,))]
        wire = self.client.calls[0]
        self.assertEqual(wire[1:],[{"role":"user","content":render_input(members)},
                                   {"role":"assistant","content":"801\n\n802"},{"role":"user","content":"901"}])
        dates = json.loads(wire[0]["content"].splitlines()[-1])["history_recorded_at"]
        self.assertEqual([d["message_index"] for d in dates],[1,1,2,2])
