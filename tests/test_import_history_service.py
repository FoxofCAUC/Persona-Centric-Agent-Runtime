"""Continuity, permissions and lifecycle commits with opaque local records."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from persona_runtime.archive import read_exact,scope
from persona_runtime.config import ConfigStore
from persona_runtime.import_history import chronological,page,history_allowed,source_time
from persona_runtime.import_personas import PersonaImports
from persona_runtime.models import generic_persona
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.sessions import select
from persona_runtime.storage import Store


class HistoryTests(unittest.TestCase):
    def setUp(self):
        temp=TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        paths=RuntimePaths(Path(temp.name))
        self.config=ConfigStore(paths)
        self.config.initialize()
        self.config.update({"model":{"base_url":"https://api.deepseek.com","name":"deepseek-v4-pro","api_key":"1"},
                            "organization":{"reuse_chat":True}})
        self.store=Store(paths)
        self.store.initialize()
        imports=PersonaImports(self.store,self.config)
        self.history=imports.history
        self.data=imports.library.add(b"## X [2026-01-02 01:02+03:00]\n101\n> 991\n\n102\n## Y\n201\n## Y\n202\n## system\n301\n## X [2026-02-31 01:02]\n103")
        service=RuntimeService(self.store)
        self.pid=service.create_persona(generic_persona())["id"]
        service.use_persona(self.pid)
        with self.store.connection(write=True) as db:
            self.first=select(db,self.pid,"desktop")

    def preview(self, **opts):
        args=dict(ident=self.data["id"],revision=1,self_role="A",mapping=self.data["mapping"],persona_id=self.pid)
        return self.history.preview(**(args|opts))

    def test_repeat_and_original_evidence(self):
        p=self.preview()
        self.assertEqual(p["counts"],{"A":2,"B":2,"note":1})
        self.assertEqual(p["session_id"],self.first)
        result=self.history.commit(p,"11",True)
        sid=result["session_id"]
        self.assertNotEqual(sid,self.first)
        self.assertEqual(self.history.commit(p,"11",True),result)
        self.assertEqual(self.history.commit(self.preview(),"12",True)["session_id"],sid)
        rows=page(self.store,self.pid,sid)["rows"]
        self.assertEqual([e["text"] for e in rows],["101\n> 991\n\n102","201","202","301","103"])
        self.assertEqual(rows[0]["created_at"],"2026-01-02T01:02:00+03:00")
        self.assertIsNone(rows[-1]["created_at"])
        self.assertEqual(page(self.store,self.pid,sid,query="991")["rows"][0]["seq"],rows[0]["seq"])
        with self.store.connection() as db:
            self.assertFalse(history_allowed(db,sid,"memory"))
            self.assertTrue(history_allowed(db,sid,"dialogue"))
            self.assertIsNone(source_time(db,rows[-1]["id"]))
            turns=db.execute("SELECT id FROM turns WHERE session_id=? ORDER BY rowid",(sid,))
            texts=[e["text"] for t in turns for proj in [read_exact(db,scope(self.pid,"desktop"),{"kind":"turn","id":t[0]})]
                   if proj["status"]=="found" for e in chronological(proj)]
            self.assertEqual(texts,["101\n102","201","202","103"])
            self.assertEqual(db.execute("SELECT count(*) FROM attempts").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT state FROM session_lifecycle WHERE session_id=?",(self.first,)).fetchone()[0],"sealed")

    def test_policy_change_and_withdrawal(self):
        sid=self.history.commit(self.preview(organize=True),"21",True)["session_id"]
        eid=page(self.store,self.pid,sid)["rows"][0]["id"]
        with self.store.connection() as db:
            self.assertTrue(history_allowed(db,sid,"memory"))
        self.history.update(self.pid,sid,1,"pause")
        with self.store.connection() as db:
            self.assertFalse(history_allowed(db,sid,"memory"))
        self.config.update({"model":{"base_url":"https://api.deepseek.com/v1"}})
        self.assertEqual(self.history.status(self.pid,sid)["usage_state"],"provider_changed")
        with self.store.connection() as db:
            self.assertEqual(read_exact(db,scope(self.pid,"desktop"),{"kind":"event","id":eid})["status"],"unavailable")
        s=self.history.status(self.pid,sid)
        self.history.update(self.pid,sid,2,"authorize",True,s["current_providers"])
        self.history.update(self.pid,sid,3,"withdraw",True)
        self.assertEqual(page(self.store,self.pid,sid,query="101")["rows"][0]["text"],"101\n> 991\n\n102")
        with self.store.connection() as db:
            for purpose in ("dialogue","memory","evidence","jev"):
                self.assertFalse(history_allowed(db,sid,purpose))
            self.assertEqual(read_exact(db,scope(self.pid,"desktop"),{"kind":"event","id":eid})["status"],"unavailable")
        with self.assertRaises(ValueError):
            self.history.update(self.pid,sid,4,"authorize",True,s["current_providers"])

    def test_pending_turn_and_foreign_scope_do_not_seal_the_old_session(self):
        from persona_runtime.models import identifier,timestamp
        p=self.preview()
        with self.store.connection(write=True) as db:
            tid=identifier()
            db.execute("""INSERT INTO turns(id,source_key,persona_id,session_id,channel,status,model_snapshot,created_at,updated_at)
                       VALUES(?,?,?,?,'desktop','queued','{}',?,?)""",(tid,"desktop:"+tid,self.pid,self.first,timestamp(),timestamp()))
        with self.assertRaisesRegex(ValueError,"重新预览"):
            self.history.commit(p,"34",True)
        with self.assertRaisesRegex(ValueError,"待处理"):
            self.history.commit(self.preview(),"34",True)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT state FROM session_lifecycle WHERE session_id=?",(self.first,)).fetchone()[0],"active")
        other=RuntimeService(self.store).create_persona(generic_persona())["id"]
        with self.assertRaises(ValueError):
            page(self.store,other,self.first)

    def test_explicit_app_roles_fix_the_user_side(self):
        other=self.history.library.add(b"## user\n102\n## assistant\n202")
        args=dict(ident=other["id"],revision=1,mapping=other["mapping"],persona_id=self.pid)
        self.assertEqual(self.history.preview(**args,self_role="A")["participant_kinds"],{"A":"user","B":"assistant"})
        with self.assertRaisesRegex(ValueError,"用户"):
            self.history.preview(**args,self_role="B")

    def test_partial_scope_and_rollback(self):
        p=self.preview(first=1,last=4,excluded=[2])
        self.assertEqual(p["total"],3)
        with patch("persona_runtime.import_history_service.restore",side_effect=RuntimeError("stop")):
            with self.assertRaises(RuntimeError):
                self.history.commit(p,"31",True)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT session_id FROM session_heads WHERE persona_id=?",(self.pid,)).fetchone()[0],self.first)
            self.assertEqual(db.execute("SELECT count(*) FROM session_packages").fetchone()[0],0)
        with self.store.connection(write=True) as db:
            db.execute("UPDATE session_lifecycle SET revision=revision+1 WHERE session_id=?",(self.first,))
        with self.assertRaises(ValueError):
            self.history.commit(p,"31",True)
        sid=self.history.commit(self.preview(first=1,last=4,excluded=[2]),"31",True)["session_id"]
        self.assertEqual([r["text"] for r in page(self.store,self.pid,sid)["rows"]],["101\n> 991\n\n102","202","301"])
        with self.assertRaises(ValueError):
            self.preview(self_role="B")
