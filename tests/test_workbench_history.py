"""Exercise the read boundary with local numeric markers and no model transport."""

from dataclasses import replace
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.models import LOCAL_USER_ID, RuntimeErrorBase, generic_persona, identifier, timestamp
from persona_runtime.paths import RuntimePaths
from persona_runtime.scene_publication import SOURCE
from persona_runtime.service import RuntimeService
from persona_runtime.sessions import select
from persona_runtime.storage import Store
from persona_runtime.workbench_history import WorkbenchHistory, open_history


def insert_turn(db, persona, session, number, channel="desktop", prefix="desktop:", status="completed"):
    tid = identifier()
    now = timestamp()
    events = []
    for role, text in (("user", str(number)), ("persona", str(number + 10000))):
        eid = identifier()
        db.execute("INSERT INTO events(id, session_id, persona_id, user_id, role, text, created_at) "
                   "VALUES(?,?,?,?,?,?,?)", (eid, session, persona, LOCAL_USER_ID, role, text, now))
        events.append(eid)
    db.execute("INSERT INTO turns(id,source_key,persona_id,session_id,channel,user_event_id,"
               "reply_event_id,status,model_snapshot,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               (tid, prefix + tid, persona, session, channel, *events, status, "{}", now, now))
    return tid


class WorkbenchHistoryTests(unittest.TestCase):
    def setUp(self):
        parent = RuntimePaths.from_environment().home / "tmp"
        parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temporary.cleanup)
        self.paths = RuntimePaths(Path(temporary.name))
        self.store = Store(self.paths)
        self.history = WorkbenchHistory(self.paths)

    def create_roles(self):
        self.store.initialize()
        service = RuntimeService(self.store)
        first = service.create_persona(replace(generic_persona(), name="1001"))["id"]
        second = service.create_persona(replace(generic_persona(), name="1002"))["id"]
        service.use_persona(first)
        return first, second

    def test_uninitialized_database_stays_uninitialized(self):
        self.assertTrue(self.history.people()["waiting"])
        self.assertFalse(self.store.path.exists())

    def test_reads_are_scoped_and_leave_the_current_persona_and_database_intact(self):
        first, second = self.create_roles()
        with self.store.connection(write=True) as db:
            sid = select(db, first, "desktop")
            other = select(db, second, "desktop")
            qq = select(db, first, "qq", origin={"bot": "10", "sender": "11"})
            insert_turn(db, first, sid, 1)
            insert_turn(db, second, other, 2)
            insert_turn(db, first, qq, 3, "qq", "qq:")
            insert_turn(db, first, sid, 4, prefix="imported.")
        before = self.store.path.read_bytes()
        self.assertEqual(self.history.people()["selected"], first)
        self.assertEqual([row["id"] for row in self.history.sessions(first)], [sid])
        self.assertEqual([row["id"] for row in self.history.sessions(second)], [other])
        self.assertEqual([m["text"] for m in self.history.page(second, other)["messages"]], ["2", "10002"])
        self.assertEqual([m["text"] for m in self.history.page(first, sid)["messages"]], ["1", "10001"])
        for invalid in (other, qq):
            with self.assertRaises(RuntimeErrorBase):
                self.history.page(first, invalid)
        with open_history(self.store) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM settings")
            self.assertEqual(Store.current(db), first)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_pages_keep_chronological_order_and_check_cursor_scope(self):
        first, second = self.create_roles()
        with self.store.connection(write=True) as db:
            sid = select(db, first, "desktop")
            other = select(db, second, "desktop")
            foreign = insert_turn(db, second, other, 20)
            for number in (1, 2, 3):
                insert_turn(db, first, sid, number)
        with patch("persona_runtime.workbench_history.PAGE_SIZE", 2):
            recent = self.history.page(first, sid)
            older = self.history.page(first, sid, recent["before"])
        self.assertEqual([m["text"] for m in recent["messages"]], ["2", "10002", "3", "10003"])
        self.assertEqual([m["text"] for m in older["messages"]], ["1", "10001"])
        self.assertIsNone(older["before"])
        with self.assertRaises(RuntimeErrorBase):
            self.history.page(first, sid, foreign)

    def test_scene_history_only_displays_the_confirmed_prefix(self):
        first, _ = self.create_roles()
        with self.store.connection(write=True) as db:
            sid = select(db, first, "desktop")
            tid = insert_turn(db, first, sid, 5, status="interrupted")
            now = timestamp()
            db.execute("INSERT INTO expression_speech_units(id,turn_id,phase,source_kind,source_id,source_hash,"
                       "persona_id,session_id,body,body_hash,policy_json,voice_json,context_json,created_at) "
                       "VALUES('u',?,'main',?,'s','0',?,?,'507 508','0','{}','{}','{}',?)",
                       (tid, SOURCE, first, sid, now))
            db.execute("INSERT INTO expression_speech_publications(id,utterance_id,revision,producer_kind,"
                       "body,body_hash,state,created_at) VALUES('p','u',1,'scene','507 508','0','partial',?)", (now,))
            db.execute("INSERT INTO expression_speech_versions VALUES('p',1,'{}')")
            db.execute("INSERT INTO expression_speech_items(id,publication_id,revision,ordinal,text,body_hash,"
                       "kind,required,delay_ms,state) VALUES('f','p',1,0,'507','0','bubble',1,0,'presented')")
            db.execute("INSERT INTO expression_speech_visible VALUES('f','u','p',0,'507','1',?)", (now,))
        self.assertEqual([m["text"] for m in self.history.page(first, sid)["messages"]], ["5", "507"])


if __name__ == "__main__":
    unittest.main()
