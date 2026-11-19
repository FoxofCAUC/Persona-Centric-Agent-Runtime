"""Portable character copies preserve local history while awaiting fresh source approval."""
import json
import tempfile
import time
import unittest
from pathlib import Path

from persona_runtime.archive import scope
from persona_runtime.models import LOCAL_USER_ID, generic_persona, timestamp
from persona_runtime.paths import RuntimePaths
from persona_runtime.portable_memory import HISTORY_REVIEW, REVIEW_MARK, import_export
from persona_runtime.recall_sources import material
from persona_runtime.recall_support import RecallSupport
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


class PortableHistoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(RuntimePaths(Path(temp.name)))
        self.store.initialize()
        service = RuntimeService(self.store)
        self.pid = service.create_persona(generic_persona())["id"]
        service.use_persona(self.pid)
        now = timestamp()
        row = {"persona_id":"91","user_id":LOCAL_USER_ID,"created_at":now}
        self.package = {"format_version":17, "persona":{"id":"91","spec":generic_persona().to_dict()},
            "sessions":[row | {"id":"1"}], "memories":[], "events":[
               row | {"id":eid,"role":role,"text":str(index),"session_id":"1"}
               for index,(eid,role) in enumerate((("imported.2.1","user"),("imported.2.2","persona"),
                   ("imported.2.3","note"),("4","user"),("5","system")))],
            "source_exclusions":[row | {"event_id":"imported.2.2","reason":"history_withdrawn"}]}

    def mapped_copy(self):
        copy = import_export(self.store,self.package,self.pid,"92")
        self.assertEqual(copy,import_export(self.store,self.package,self.pid,"92"))
        with self.store.connection() as db:
            archive = db.execute("SELECT id,data_json FROM memory_imports WHERE persona_id=?",(copy["id"],)).fetchone()
        return copy["id"],archive[0],json.loads(archive[1])["identity_map"]

    def test_portable_history_remains_browsable_but_cannot_be_recalled_without_review(self):
        pid,_,mapping = self.mapped_copy()
        with self.store.connection() as db:
            excluded = dict(db.execute("SELECT event_id,reason FROM source_exclusions WHERE persona_id=?",(pid,)).fetchall())
            self.assertEqual(excluded,{mapping["imported.2.1"]:HISTORY_REVIEW,
                mapping["imported.2.2"]:"history_withdrawn",mapping["imported.2.3"]:HISTORY_REVIEW})
            rows = db.execute("SELECT id,role,text FROM events WHERE persona_id=? ORDER BY seq",(pid,)).fetchall()
            self.assertEqual([r["role"] for r in rows],["user","persona","note","user","note"])
            for row in rows[:3]:
                self.assertIsNone(material(db,"dialogue",{"id":row["id"]},now=time.time()))
            self.assertEqual(material(db,"dialogue",{"id":mapping["4"]},now=time.time())["scope"],scope(pid,"desktop"))
        support = RecallSupport(self.store)
        sync = support.synchronize(pid,"dialogue",limit=64)
        self.assertEqual(sync["registered"],1)

    def test_import_of_older_second_generation_copy_follows_original_identity_maps(self):
        import copy
        grandparent=self.package
        parent=copy.deepcopy(grandparent)
        parent_ids={event["id"]:str(100+i) for i,event in enumerate(parent["events"])}
        for event in parent["events"]:
            event["id"]=parent_ids[event["id"]]
        parent["source_exclusions"]=[]
        parent["memory_imports"]=[{"data_json":json.dumps({"package":grandparent,"identity_map":parent_ids})}]
        self.package=parent
        pid,_,mapping=self.mapped_copy()
        with self.store.connection() as db:
            excluded=dict(db.execute("SELECT event_id,reason FROM source_exclusions WHERE persona_id=?",(pid,)).fetchall())
            self.assertEqual(excluded,{mapping[parent_ids[eid]]:HISTORY_REVIEW for eid in parent_ids if eid.startswith("imported.")})
            self.assertIsNone(material(db,"dialogue",{"id":mapping[parent_ids["imported.2.1"]]},now=time.time()))
            self.assertIsNotNone(material(db,"dialogue",{"id":mapping[parent_ids["4"]]},now=time.time()))

    def test_existing_portable_copy_recovers_policy_once_from_archived_map(self):
        pid,archive_id,mapping = self.mapped_copy()
        with self.store.connection(write=True) as db:
            db.execute("DELETE FROM knowledge_control WHERE key=?", (REVIEW_MARK+archive_id,))
            db.execute("DELETE FROM source_exclusions WHERE event_id=?", (mapping["imported.2.1"],))
        self.store.initialize()
        with self.store.connection() as db:
            reason,at = db.execute("SELECT reason,created_at FROM source_exclusions WHERE event_id=?", (mapping["imported.2.1"],)).fetchone()
            self.assertEqual(reason,HISTORY_REVIEW)
            self.assertIsNone(material(db,"dialogue",{"id":mapping["imported.2.1"]},now=time.time()))
        self.store.initialize()
        with self.store.connection() as db:
            self.assertEqual(tuple(db.execute("SELECT reason,created_at FROM source_exclusions WHERE event_id=?", (mapping["imported.2.1"],)).fetchone()),(reason,at))
            self.assertEqual(db.execute("SELECT count(*) FROM knowledge_control WHERE key=?", (REVIEW_MARK+archive_id,)).fetchone()[0],1)


if __name__ == "__main__":
    unittest.main()
