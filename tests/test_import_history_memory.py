"""Memory grants and chronology on isolated, numbered historical events."""
import asyncio
import json
import unittest

from persona_runtime import import_history
from persona_runtime import scene_policy
from persona_runtime.archive import scope
from persona_runtime.import_markdown import assign
from persona_runtime.import_plan import parse_source
from persona_runtime.migration import _sanitize_database
from persona_runtime.models import LOCAL_USER_ID, identifier
from persona_runtime.session_organization import SessionOrganization
from persona_runtime.sessions import SessionService
from tests import test_m4
from tests.test_m8d import Model


class HistoryMemoryTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_m4.M4Tests.setUp
    asyncTearDown = test_m4.M4Tests.asyncTearDown

    def restore(self, organize=True, *, undated=False):
        self.pid = self.persona["id"]
        self.cfg.update({'organization':{'model':{'api_key':'111'}}})
        settings=self.cfg.load()
        raw=(b"## 1 [2018-05-01T00:00:00]\n101\n\n> 901\n## 1 [2018-05-01T00:00:01]\n102\n"
             b"## 2 [2018-05-01T00:01:00]\n201\n## 2 [2018-05-01T00:02:00]\n202\n")
        if undated:
            import re
            raw=re.sub(rb" \[[^\]]+\]", b"", raw)
        rows=assign(parse_source(raw),{"1":"A","2":"B"},{})
        meta={"version":2,"organize":organize,"self_role":"A","revision":1,"withdrawn":False,
              "timezone_offset":480, "providers":{"memory":import_history.provider(self.org.model()),
              "dialogue":import_history.provider(settings["model"])}}
        with self.store.connection(write=True) as db:
            sid=import_history.restore(db,self.pid,"0"*64,"B",rows,meta=meta)
        policy=self.org.managed.current_policy()
        self.org.managed.update_policy(self.pid,dict(policy["data"],memory_automation=True,memory_backfill=True),
                                       policy["id"],self.pid,identifier())
        self.org.client=Model()
        return sid

    def change(self, sid, **values):
        with self.store.connection(write=True) as db:
            key=import_history.META+sid
            grant=scene_policy.read(db,key)
            scene_policy.save(db,key,dict(grant,revision=grant["revision"]+1,**values))

    async def finish(self):
        for _ in range(12):
            self.org.automatic.tick(force=True)
            if not await self.org.run_next():
                break

    async def test_no_organization_or_sealed_preview_when_source_not_opted_in(self):
        sid=self.restore(False)
        await self.finish()
        self.assertEqual(self.org.client.calls,[])
        self.assertRaisesRegex(ValueError,"no_eligible_turns",self.org.preview,sid)
        sessions=SessionService(self.store)
        current=sessions.current(self.pid)
        sessions.seal(sid,self.pid,current["revision"],identifier())
        result=SessionOrganization(self.org).preview(sid,self.pid)
        self.assertEqual(result["status"],"no_action")

    async def test_memory_receipt_uses_owned_text_in_native_order_and_original_date(self):
        sid=self.restore()
        await self.finish()
        self.assertEqual(len(self.org.client.calls),2)
        material=json.loads(self.org.client.calls[0][-1]["content"])
        self.assertEqual([e["text"] for e in material["events"]],["101","102","201","202"])
        self.assertEqual(list(material["recorded_times"].values()),["2018-05-01T00:00:00+08:00"])
        self.assertTrue({m["text"] for m in self.service.search_memories()}=={"101","102"})
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM memory_sources").fetchone()[0],2)
        self.change(sid,organize=False)
        await self.finish()
        self.assertEqual(len(self.org.client.calls),2)
        self.assertEqual(len(self.service.search_memories()),2)

    async def test_pause_during_call_preserves_receipt_then_reuses_it_on_resume(self):
        sid=self.restore()
        entered,gate=asyncio.Event(),asyncio.Event()
        transport=self.org.client
        complete=transport.complete
        async def delay(model,messages):
            entered.set()
            await gate.wait()
            return await complete(model,messages)
        transport.complete=delay
        self.org.automatic.tick(force=True)
        task=asyncio.create_task(self.org.run_next())
        await asyncio.wait_for(entered.wait(),10)
        self.change(sid,organize=False)
        gate.set()
        await task
        await self.finish()
        self.assertEqual(len(transport.calls),1)
        self.assertFalse(self.service.search_memories())
        self.change(sid,organize=True)
        await self.finish()
        self.assertEqual(len(transport.calls),2)
        self.assertEqual(len(self.service.search_memories()),2)

    async def test_wrong_provider_and_no_time_legacy_input_are_handled(self):
        sid=self.restore(undated=True)
        with self.store.connection() as db:
            stored=scene_policy.read(db,import_history.META+sid)["providers"]
        self.change(sid,providers=dict(stored,memory={"name":"999","base_url":"999"}))
        await self.finish()
        self.assertEqual(self.org.client.calls,[])
        self.change(sid,providers=stored)
        await self.finish()
        evidence=json.loads(self.org.client.calls[0][-1]["content"])
        self.assertEqual(list(evidence["recorded_times"].values()),[None])

    async def test_existing_newer_native_value_survives_imported_revision(self):
        self.pid=self.persona["id"]
        turn=await self.chat.submit("desktop","345","111")
        await self.chat.wait()
        current=self.chat.get(turn["id"])
        sid=self.restore()
        old=self.service.add_memory("preference","345",current["user_event_id"])
        with self.store.connection(write=True) as db:
            from persona_runtime.archive import encoded
            db.execute("INSERT INTO memory_facts VALUES(?,?,?,?,?,?,NULL,NULL,NULL,?)",
                       (old['id'],self.pid,LOCAL_USER_ID,encoded(scope(self.pid,"desktop")),"1","2","{}"))
        def change_review(kind,item,decision,payload):
            if kind=="candidates" and item["text"] in ("101","102"):
                decision.update(action="revise",subject="1",attribute="2",target_memory_id=old["id"])
        self.org.client.review_action=change_review
        await self.finish()
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT status FROM memories WHERE id=?",(old['id'],)).fetchone()[0],"active")
            opinions=db.execute("SELECT data_json FROM memory_decisions WHERE reason='historical_source_not_newer'").fetchall()
            self.assertTrue(opinions)
            self.assertTrue(all(not json.loads(row[0])['blocked_memory_ids'] for row in opinions))
        self.assertIn("345",[m["text"] for m in self.service.search_memories()])

    async def test_native_followup_is_processed_with_transcript_paused(self):
        sid=self.restore(False)
        turn=await self.chat.submit("desktop","333","303",session_id=sid)
        await self.chat.wait()
        await self.finish()
        payload=json.loads(self.org.client.calls[0][-1]["content"])
        self.assertEqual([e["id"] for e in payload["events"] if e["role"]=="user"],[turn["user_event_id"]])
        self.assertTrue(all(not e["id"].startswith("imported.") for e in payload["events"]))
        self.assertEqual([m["text"] for m in self.service.search_memories()],["333"])

    async def test_new_recipient_requires_reconfirmation_then_rebinds_only_unsent_request(self):
        from persona_runtime.private_personas import provider_identity
        sid=self.restore()
        self.org.automatic.tick(force=True)
        self.cfg.update({"organization":{"model":{"name":"707"}}})
        policy=self.org.managed.current_policy()
        self.org.managed.update_policy(self.pid,dict(policy["data"],provider=provider_identity(self.org.model())),
                                       policy["id"],self.pid,identifier())
        await self.finish()
        self.assertFalse(self.org.client.calls)
        with self.store.connection() as db:
            current=scene_policy.read(db,import_history.META+sid)
        self.change(sid,providers=dict(current["providers"],memory=import_history.provider(self.org.model())))
        await self.finish()
        self.assertEqual(len(self.org.client.calls),2)
        self.assertEqual(len(self.service.search_memories()),2)
        with self.store.connection() as db:
            self.assertEqual({json.loads(r[0])["name"] for r in db.execute("SELECT model_snapshot FROM organization_attempts")},{"707"})

    async def test_pause_between_stages_does_not_hold_a_later_native_turn(self):
        sid=self.restore()
        self.org.automatic.tick(force=True)
        await self.org.run_next()
        self.change(sid,organize=False)
        turn=await self.chat.submit("desktop","303","333",session_id=sid)
        await self.chat.wait()
        await self.finish()
        self.assertEqual(len(self.org.client.calls),3)
        emitted=json.loads(self.org.client.calls[1][-1]["content"])
        self.assertEqual(emitted["events"][0]["id"],turn["user_event_id"])
        self.change(sid,organize=True)
        await self.finish()
        self.assertEqual(len(self.org.client.calls),4)

    async def test_saved_extraction_may_be_published_locally_under_renewed_grant(self):
        from persona_runtime.private_personas import provider_identity
        sid=self.restore()
        entered,gate=asyncio.Event(),asyncio.Event()
        complete=self.org.client.complete
        async def delay(model,messages):
            entered.set()
            await gate.wait()
            return await complete(model,messages)
        self.org.client.complete=delay
        self.org.automatic.tick(force=True)
        task=asyncio.create_task(self.org.run_next())
        await asyncio.wait_for(entered.wait(),10)
        self.cfg.update({"organization":{"model":{"name":"717"}}})
        p=self.org.managed.current_policy()
        self.org.managed.update_policy(self.pid,dict(p["data"],provider=provider_identity(self.org.model())),
                                       p["id"],self.pid,identifier())
        gate.set()
        await task
        await self.finish()
        self.assertEqual(len(self.org.client.calls),1)
        with self.store.connection() as db:
            current=scene_policy.read(db,import_history.META+sid)
        self.change(sid,providers=dict(current["providers"],memory=import_history.provider(self.org.model())))
        await self.finish()
        self.assertEqual(len(self.org.client.calls),2)
        self.assertEqual(len(self.service.search_memories()),2)
        with self.store.connection() as db:
            providers=[json.loads(row[0])["name"] for row in db.execute("SELECT model_snapshot FROM organization_attempts ORDER BY rowid")]
            self.assertEqual(providers,["fake-organizer","717"])

    async def test_withdrawal_invalidates_only_imported_material_and_keeps_local_bubbles(self):
        from persona_runtime.import_history_service import HistoryService
        from persona_runtime.recall_sources import material
        import time
        sid=self.restore()
        await self.finish()
        await self.chat.submit("desktop","888","808",session_id=sid)
        await self.chat.wait()
        await self.finish()
        with self.store.connection() as db:
            old=db.execute("SELECT source_event_id FROM memories WHERE text='101'").fetchone()[0]
            self.assertEqual(material(db,"dialogue",{"id":old},now=time.time())["text"],"101")
        manager=HistoryService(self.store,None,None)
        manager.update(self.pid,sid,1,"withdraw",confirmed=True)
        with self.store.connection() as db:
            self.assertIsNone(material(db,"dialogue",{"id":old},now=time.time()))
        self.assertIn("101",import_history.page(self.store,self.pid,sid)["rows"][0]["text"])
        self.assertEqual([m["text"] for m in self.service.search_memories()],["888"])
        self.assertTrue(all(not row["text"] in ("101","102") for row in self.service.search_memories()))

    async def test_unknown_extraction_keeps_original_request_after_reauthorization(self):
        from persona_runtime.chat import ModelError
        sid=self.restore()
        calls=[]
        async def unknown(model,messages):
            calls.append(1)
            raise ModelError("provider_timeout")
        self.org.client.complete=unknown
        await self.finish()
        self.org.recover()
        self.change(sid,organize=False)
        self.change(sid,organize=True)
        await self.finish()
        self.assertEqual(calls,[1])
        self.assertEqual(self.org.automatic.coverage()["batches"][0]["state"],"blocked_unknown")

    async def test_large_fragment_backlog_uses_bounded_reads_and_preserves_recent_order(self):
        from unittest.mock import patch
        from persona_runtime import automatic_memory
        sid=self.restore()
        await self.finish()
        with self.store.connection(write=True) as db:
            original=dict(db.execute("SELECT * FROM memory_fragments LIMIT 1").fetchone())
            initial=json.loads(original["data_json"])
            for i in range(32):
                row=dict(original,id=identifier())
                data=dict(initial,items=[dict(item,text=str(600+2*i+j)) for j,item in enumerate(initial["items"])])
                row["data_json"]=json.dumps(data)
                db.execute("INSERT INTO memory_fragments VALUES(?,?,?,?,?,?,?,?,?)",tuple(row.values()))
            task=db.execute("SELECT verify_task_id FROM memory_batches LIMIT 1").fetchone()[0]
            with patch.object(automatic_memory,"validate_sources",wraps=automatic_memory.validate_sources) as verify:
                self.org.automatic.reconcile(db)
                self.assertLessEqual(verify.call_count,16)
            with patch.object(automatic_memory,"validate_sources",wraps=automatic_memory.validate_sources) as verify:
                self.org.automatic.compose_summary(db,sid,scope(self.pid,"desktop"),task)
                self.assertLessEqual(verify.call_count,15)
            result=json.loads(db.execute("SELECT items_json FROM summaries WHERE session_id=? AND status='active'",(sid,)).fetchone()[0])
            self.assertEqual([item["text"] for item in result],[str(i) for i in range(634,664)])

    async def test_migration_clears_imported_history_model_grants_on_copy(self):
        sid=self.restore()
        destination=self.paths.home/"copy.sqlite3"
        import sqlite3
        from contextlib import closing
        with self.store.connection() as db, closing(sqlite3.connect(destination)) as copy:
            db.backup(copy)
        changes,_,_=_sanitize_database(destination)
        self.assertEqual(changes["imported_history_grants_paused"],1)
        with closing(sqlite3.connect(destination)) as copy:
            copy.row_factory=sqlite3.Row
            self.assertFalse(import_history.history_allowed(copy,sid,"memory",providers=self.org.model()))
            self.assertEqual(copy.execute("SELECT count(*) FROM events WHERE session_id=?",(sid,)).fetchone()[0],4)
        with self.store.connection() as db:
            self.assertTrue(import_history.history_allowed(db,sid,"memory",providers=self.org.model()))
