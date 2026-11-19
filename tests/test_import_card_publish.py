"""Publication of approved card revisions. Opaque fixtures and local databases only."""
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from persona_runtime import character, context_policy, scene_policy, tk_notice
from persona_runtime.chat import ChatEngine, ModelCompletion
from persona_runtime.ingress import IngressService
from persona_runtime.models import identifier
from persona_runtime.service import RuntimeService
from persona_runtime.archive import fingerprint
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.import_card_publish import CardPublisher
from persona_runtime.import_library import ImportLibrary, require
from persona_runtime.import_style import REVOKED
from persona_runtime.paths import RuntimePaths
from persona_runtime.prompt_assembly import PromptBlock, assemble
from persona_runtime.scene_voice import SCENES, decision, jev_request, project
from persona_runtime.storage import Store


class Materials:
    def __init__(self, document, plan):
        self.document = document
        self.plan = plan
        self.cards = {}
        for ordinal, role in enumerate(("A","B"),1):
            chars = character.empty_character()
            chars["interests"]["likes"] = [str(200 + ordinal)]
            self.cards[role] = {
                "spec":{"format_version":1,"name":role,"introduction":"101","expression_style":"102",
                        "core_traits":["103"],"initial_background":"104","values_and_boundaries":["105"]},
                "character":chars, "relationship":"106", "common":str(500 + ordinal),
                "scenes":{s:str(600 + i + ordinal * 10) for i,s in enumerate(SCENES)},
                "jev":{"instruction":"701","criteria":{s:str(710+i) for i,s in enumerate(SCENES)}},
                "sources":{"spec.introduction":[{"id":"10"}]}, "issues":[]}

    def material(self, ident, *, revision, plan_id, card_revision, db):
        row = db.execute("SELECT revision,status FROM documents WHERE id=?", (ident,)).fetchone()
        require(row and row["revision"] == revision and row["status"] != "revoked" and
                ident == self.document and plan_id == self.plan and card_revision == 3, "stale")
        return self.cards


class CardPublicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        root = TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.paths = RuntimePaths(Path(root.name))
        self.config = ConfigStore(self.paths)
        self.config.initialize()
        self.config.update({"model":{"base_url":"https://api.deepseek.com/v1","name":scene_policy.PRO,"api_key":"10"}})
        self.store = Store(self.paths)
        self.store.initialize()
        self.library = ImportLibrary(self.paths)
        self.document = self.library.add(b"## X\n101\n## Y\n201")["id"]
        self.plan = fingerprint(["100", self.document])
        with self.library.db() as db:
            db.execute("UPDATE documents SET status='ready',revision=2 WHERE id=?", (self.document,))
            db.execute("INSERT INTO profile_current VALUES(?,?)", (self.document,self.plan))
        self.jobs = Materials(self.document,self.plan)
        with self.library.db() as db:
            db.execute("INSERT INTO profile_card_drafts VALUES(?,?,?,?,?)",
                       (self.document,self.plan,2,3,json.dumps(self.jobs.cards)))
        self.publisher = CardPublisher(self.store,self.library,self.jobs)

    def publish(self, roles=None, **overrides):
        args = {"ident":self.document, "revision":2, "plan_id":self.plan, "card_revision":3,
                "roles":roles or ["A","B"], "operation_id":"100", "confirmed":True}
        return self.publisher.create(**(args | overrides))

    def configure_jev(self, endpoint="https://jev.fixture.invalid"):
        atomic_json(self.paths.home/"private/knowledge-router/config.json", {
            "base_url":endpoint,"name":scene_policy.JEV,"encrypted_key":"100"})

    def enrolled(self, pid):
        with self.store.connection() as db:
            return context_policy.current(db,pid,"desktop",self.store).get(scene_policy.KEY)

    def test_two_cards_with_history_only_for_counterparty(self):
        from persona_runtime.import_history_service import HistoryService
        from persona_runtime.import_history import page
        workflow=HistoryService(self.store,self.library,self.publisher)
        creation={"roles":["A","B"],"card_revision":3,"plan_id":self.plan,"enable_scene":False}
        preview=workflow.preview(self.document,2,"A",{"X":"A","Y":"B"},creation=creation)
        from unittest.mock import patch
        with patch("persona_runtime.import_history_service.restore",side_effect=RuntimeError("101")):
            with self.assertRaises(RuntimeError):
                workflow.commit(preview,"110",True,creation)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM personas WHERE name IN ('A','B')").fetchone()[0],0)
        result=workflow.commit(preview,"110",True,creation)
        self.assertEqual(workflow.commit(preview,"110",True,creation),result)
        self.assertEqual(result["persona_id"],result["people"][1])
        self.assertEqual([r["role"] for r in page(self.store,result["persona_id"],result["session_id"])["rows"]],["user","persona"])
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE persona_id=?",(result["people"][0],)).fetchone()[0],0)
        with self.assertRaises(ValueError):
            workflow.preview(self.document,2,"B",{"X":"A","Y":"B"},creation=creation)

    def test_ordinary_created_atomically_and_single_role_retry_is_idempotent(self):
        self.jobs.cards["B"]["spec"]["initial_background"] = ""
        with self.assertRaises(ValueError):
            self.publish()
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM personas WHERE name IN ('A','B')").fetchone()[0],0)
        ids = self.publish(["A"],enable_scene=True)
        self.assertEqual(ids,self.publish(["A"],enable_scene=True))
        with self.assertRaises(ValueError):
            self.publish(["A"],operation_id="200")
        pid, = ids
        self.assertIsNone(self.enrolled(pid))
        info = self.publisher.info(pid)
        self.assertFalse(info["enabled"])
        self.assertFalse(info["available"])
        with self.store.connection() as db:
            self.assertEqual(character.head(db,pid)["data"]["interests"]["likes"],["201"])
            self.assertIsNone(character.head(db,pid)["data"]["appearance"]["height_cm"])
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE persona_id=?",(pid,)).fetchone()[0],0)
            self.assertEqual(db.execute("SELECT description FROM relationships WHERE persona_id=?", (pid,)).fetchone()[0],"106")

    def test_unresolved_conflict_blocks_selected_roles_atomically(self):
        self.jobs.cards["B"]["issues"] = [{"field":"common","severity":"conflict","reason":"101"}]
        with self.assertRaisesRegex(ValueError, "B 的角色卡还有重大冲突"):
            self.publish()
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM personas WHERE name IN ('A','B')").fetchone()[0], 0)
        self.assertEqual(len(self.publish(["A"])), 1)

    def test_confirmation_is_bound_to_role_and_current_providers(self):
        self.configure_jev()
        aid,bid = self.publish()
        a = self.publisher.info(aid)
        b = self.publisher.info(bid)
        self.assertTrue(a["available"])
        self.assertFalse(b["enabled"])
        with self.assertRaises(ValueError):
            self.publisher.enable(bid,True,True,a["revision"],a["version"])
        self.configure_jev("https://second.fixture.invalid")
        with self.assertRaises(ValueError):
            self.publisher.enable(aid,True,True,a["revision"],a["version"])
        reviewed = self.publisher.info(aid)
        enabled = self.publisher.enable(aid,True,True,reviewed["revision"],reviewed["version"])
        self.assertTrue(enabled["enabled"])
        policy = self.enrolled(aid)
        with self.store.connection() as db:
            self.assertIsNone(scene_policy.read(db,scene_policy.PREFIX+aid))
            self.assertFalse(scene_policy.active(db,aid))
        self.assertIsNone(self.enrolled(bid))
        catalog,_ = scene_policy.material(self.paths,policy)
        packet = jev_request(scene_policy.JEV, [], catalog["instruction"], catalog["criteria"])
        self.assertNotIn(self.jobs.cards["A"]["common"], json.dumps(packet))
        for s in SCENES:
            raw = json.dumps({"answers":{"scene":{"type":"choice","choice":s,
                "probabilities":{k:float(k==s) for k in SCENES}}}})
            self.assertEqual(decision(raw),(s,"selected"))
            msg,assembly = assemble([PromptBlock("persona_expression","900","companion_im","991")],[],
                                    {"role":"user","content":"901"})
            chosen = scene_policy.card(self.paths,policy,s)
            messages,_ = project(msg,assembly,chosen)
            self.assertEqual(messages[0]["content"],enabled["common"]+"\n\n"+enabled["scenes"][s])
        self.assertEqual(decision("{}")[0],"fallback")
        paused = self.publisher.enable(aid,False,True,enabled["revision"],enabled["version"])
        self.assertFalse(paused["enabled"])
        self.assertIsNone(self.enrolled(aid))

    def test_provider_head_and_withdrawal_guard_both_turn_and_presentation(self):
        self.configure_jev()
        pid, = self.publish(["A"],enable_scene=True)
        self.assertIsNotNone(self.enrolled(pid))
        self.configure_jev("https://another.fixture.invalid")
        self.assertIsNone(self.enrolled(pid))
        self.configure_jev()
        self.assertIsNotNone(self.enrolled(pid))
        with self.store.connection(write=True) as db:
            scene_policy.save(db,REVOKED+self.document,True)
        self.assertIsNone(self.enrolled(pid))
        self.assertFalse(self.publisher.info(pid)["available"])
        self.assertEqual(self.publisher.info(pid)["affected_fields"],["spec.introduction"])
        with self.store.connection(write=True) as db:
            scene_policy.save(db,REVOKED+self.document,False)
            character.append(db,pid,character.empty_character())
        self.assertIsNone(self.enrolled(pid))
        self.assertFalse(self.publisher.info(pid)["available"])

    def test_editing_one_role_pauses_only_its_confirmed_route(self):
        self.configure_jev()
        a, b = self.publish(enable_scene=True)
        self.jobs.cards["B"]["common"] = "801"
        with self.library.db() as db:
            db.execute("UPDATE profile_card_drafts SET revision=4,cards=? WHERE document=?",
                       (json.dumps(self.jobs.cards),self.document))
        self.assertIsNotNone(self.enrolled(a))
        self.assertIsNone(self.enrolled(b))
        refreshed = self.publisher.info(b)
        self.assertTrue(refreshed["available"])
        self.assertTrue(refreshed["updated"])
        self.assertEqual(refreshed["common"],"801")
        self.publisher.enable(b,True,True,refreshed["revision"],refreshed["version"])
        self.assertIsNotNone(self.enrolled(b))
        self.assertEqual(scene_policy.card(self.paths,self.enrolled(b),"daily").splitlines()[0],"801")

    def test_incomplete_scenes_leave_ordinary_role(self):
        self.configure_jev()
        self.jobs.cards["A"]["scenes"]["help"] = ""
        pid, = self.publish(["A"],enable_scene=True)
        self.assertIsNone(self.enrolled(pid))
        self.assertFalse(self.publisher.info(pid)["available"])
        self.jobs.cards["A"]["scenes"]["help"] = "804"
        with self.library.db() as db:
            db.execute("UPDATE profile_card_drafts SET revision=4,cards=? WHERE document=?",
                       (json.dumps(self.jobs.cards),self.document))
        review = self.publisher.info(pid)
        self.assertTrue(review["available"])
        self.assertEqual(review["scenes"]["help"],"804")
        self.publisher.enable(pid,True,True,review["revision"],review["version"])
        self.assertIsNotNone(self.enrolled(pid))

    async def test_normal_ingress_uses_confirmed_scene_and_rechecks_source_before_display(self):
        self.configure_jev()
        pid, = self.publish(["A"])
        RuntimeService(self.store).use_persona(pid)

        class Main:
            def __init__(self):
                self.messages = []

            async def complete(self, model, messages):
                self.messages.append(messages)
                return ModelCompletion("901", {"total_tokens":1}, {"finish_reason":"stop"})

        class Choice:
            def __init__(self, model):
                self.model = model
                self.wires = []

            async def complete(self, wire, *, on_dispatch, on_receipt, diagnostics):
                self.wires.append(wire)
                now = asyncio.get_running_loop().time()
                on_dispatch(now)
                on_receipt(now + .1)
                return 200, json.dumps({"answers":{"scene":{"type":"choice","choice":"help",
                    "probabilities":{s:float(s=="help") for s in SCENES}}}})

        main = Main()
        engine = ChatEngine(self.store,self.config,main)
        try:
            ingress = IngressService(engine)
            with self.store.connection() as db:
                prev = ingress.policy(db,pid,"desktop")
            ingress.update_policy(pid,"desktop","legacy_per_message",prev["id"],identifier(),
                                  companion_mode="minimal_im",reply_mode="context_full",
                                  context_mode="working_set",expression_policy_version=3)
            review = self.publisher.info(pid)
            self.publisher.enable(pid,True,True,review["revision"],review["version"])
            choice = Choice(scene_policy.configured_jev(self.paths))
            engine.scene.transport = choice
            sid = engine.session("desktop")["session_id"]
            result = ingress.accept({"channel":"desktop","persona_id":pid,"session_id":sid,"text":"901",
                                     "request_id":identifier(),"desktop_presentation":tk_notice.CONTRACT})
            await asyncio.wait_for(engine.wait(),20)
            turn = engine.get(result["turn_id"])
            self.assertTrue(turn.get("scene_speech"),turn.get("error_code"))
            self.assertEqual(len(choice.wires),1)
            self.assertEqual(len(main.messages),1)
            self.assertEqual(main.messages[0][0]["content"],review["common"]+"\n\n"+review["scenes"]["help"])
            with self.store.connection(write=True) as db:
                scene_policy.save(db,REVOKED+self.document,True)
            speech = turn["scene_speech"]
            offer = engine.scene_publication.publisher.offer(pid,sid,speech["utterance_id"],
                       speech["publication_id"],speech["revision"],0,"103")
            self.assertEqual(offer["state"],"stopped")
            self.assertIsNone(engine.get(turn["id"])["reply_event_id"])
        finally:
            await engine.scene.close()
            engine.cognitive.characters.close()
