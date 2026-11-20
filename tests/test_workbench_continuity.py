"""Read boundaries and continuation of the normal Windows workbench."""
import json
from unittest.mock import patch
import unittest

from persona_runtime import workbench_timeline as timeline
from persona_runtime.models import RuntimeErrorBase, LOCAL_USER_ID
from persona_runtime.progress import record
from persona_runtime.workbench_import import WorkbenchData
from tests import test_desktop_recovery as fixture


class ContinuityTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixture.DesktopRecoveryTests.setUp
    reopen=fixture.DesktopRecoveryTests.reopen
    asyncTearDown=fixture.DesktopRecoveryTests.asyncTearDown

    def bridge(self):
        return WorkbenchData(self.paths,lambda:None)

    def test_browsing_another_role_has_no_business_writes(self):
        person=self.chat.service.show_persona(self.pid)
        from persona_runtime.models import PersonaSpec
        other=self.chat.service.create_persona(PersonaSpec.from_dict(person["spec"]))["id"]
        sid=self.chat.session(persona_id=other)["session_id"]
        selection=timeline.selection(self.store,other)
        self.assertEqual(selection["active"],sid)
        self.assertEqual(self.chat.service.show_persona()["id"],self.pid)
        with self.assertRaises(RuntimeErrorBase):timeline.page(self.store,self.pid,sid)
        self.assertEqual(timeline.page(self.store,other,sid)["turns"],[])
        self.assertEqual(self.chat.service.show_persona()["id"],self.pid)

    def test_blank_role_is_readable_before_first_session(self):
        from persona_runtime.models import PersonaSpec
        other=self.chat.service.create_persona(PersonaSpec(1,"801","801",["801"],"801",["801"],"801"))["id"]
        self.assertEqual(timeline.selection(self.store,other)["sessions"],[])
        self.assertEqual(self.bridge().live_draft(other,"")["text"],"")
        self.assertEqual(timeline.selection(self.store,other)["sessions"],[])

    def seed(self,count):
        with self.store.connection(write=True) as db:
            for n in range(count):
                mark=str(80000+n)
                event="event-"+mark
                db.execute("INSERT INTO events(id,session_id,persona_id,user_id,role,text,created_at) VALUES(?,?,?,?,'user',?,?)",
                           (event,self.sid,self.pid,LOCAL_USER_ID,mark,"2026-01-01"))
                db.execute("""INSERT INTO turns(id,source_key,persona_id,session_id,channel,user_event_id,status,model_snapshot,created_at,updated_at)
                           VALUES(?,?,?,?,'desktop',?,'completed','{}',?,?)""",
                           (mark,mark,self.pid,self.sid,event,mark,mark))
                record(db,mark,"persisted")

    def test_bidirectional_pages_and_incremental_old_turn(self):
        self.seed(150)
        first=timeline.page(self.store,self.pid,self.sid)
        self.assertEqual((len(first["turns"]),first["newer"]),(60,None))
        before=timeline.page(self.store,self.pid,self.sid,before=first["older"])
        after=timeline.page(self.store,self.pid,self.sid,after=before["newer"])
        self.assertEqual(after["turns"],first["turns"])
        self.assertIsNone(timeline.page(self.store,self.pid,self.sid,first["version"])["turns"])
        with self.store.connection(write=True) as db:
            db.execute("UPDATE turns SET status='failed',error_code='provider_http_401' WHERE id='80005'")
            record(db,"80005","failed")
        changed=timeline.page(self.store,self.pid,self.sid,first["version"])
        self.assertFalse(changed["reset"])
        self.assertEqual([(t["id"],t["error_code"]) for t in changed["turns"]],[("80005","provider_http_401")])
        restored=timeline.page(self.store,self.pid,self.sid,anchor=before["turns"][15]["position"])
        self.assertIn(before["turns"][15]["id"],[card["id"] for card in restored["turns"]])

    def test_gap_recovers_a_bounded_page(self):
        self.seed(90)
        lost=timeline.page(self.store,self.pid,self.sid,version=99999)
        backlog=timeline.page(self.store,self.pid,self.sid,version=0)
        self.assertEqual(lost["turns"],backlog["turns"])
        self.assertTrue(backlog["reset"])
        self.assertEqual(len(backlog["turns"]),60)

    def test_reference_draft_and_anchor_reopen_after_exclusion(self):
        self.seed(2)
        bridge=self.bridge()
        draft={"previous":None,"revision":"810","text":"811","reference":{"id":"event-80000","text":"812"}}
        saved=bridge.live_draft(self.pid,self.sid,draft)
        self.assertEqual(saved["reference"],{"id":"event-80000","text":"80000"})
        mark={"kind":"native","id":"event-80000","position":1,"offset":-14}
        bridge.live_location(self.pid,self.sid)
        bridge.live_location(self.pid,self.sid,mark)
        reopened=self.bridge()
        self.assertEqual(reopened.live_location(),{"persona_id":self.pid,"session_id":self.sid})
        self.assertEqual(reopened.live_location(self.pid,self.sid),mark)
        with self.store.connection(write=True) as db:
            db.execute("INSERT INTO source_exclusions VALUES(?,?,?,?,?)",("event-80000",self.pid,LOCAL_USER_ID,"813","2026-01-01"))
        updated=reopened.live_draft(self.pid,self.sid)
        self.assertEqual((updated["text"],updated["reference"]),("811",None))
        self.assertEqual(reopened.live_draft(self.pid,self.sid,draft)["sequence"],saved["sequence"])

    def test_wrong_instance_is_not_a_successful_probe(self):
        from persona_runtime.config import atomic_json
        from persona_runtime.desktop_client import LocalAPIError
        bridge=self.bridge()
        with patch.object(bridge._client,"call",return_value={"service":"persona-runtime","instance_id":"814"}):
            self.assertEqual(bridge.live_health(),{"state":"wrong_instance"})
        bridge.live_location(self.pid,self.sid)
        composer=bridge.live_draft(self.pid,self.sid)
        atomic_json(self.config.file,self.config.load()|{"instance_id":"893"})
        with self.assertRaises(LocalAPIError):
            bridge.live_location(self.pid,self.sid,{"kind":"tail"})
        with self.assertRaises(LocalAPIError):
            bridge.live_draft(self.pid,self.sid,{"text":"895","previous":composer["revision"],"revision":"896","reference":None})

    def test_reading_reports_model_configuration_independently_of_service(self):
        bridge=self.bridge()
        info={"service":"persona-runtime","instance_id":self.config.load()["instance_id"],"service_generation":"822"}
        with patch.object(bridge._client,"call",return_value=info):
            self.assertFalse(bridge.live_health()["configured"])
            self.config.update({"model":{"api_key":"823"}})
            status=bridge.live_health()
        self.assertEqual((status["generation"],status["configured"]),("822",True))
        self.assertEqual(set(status),{"state","generation","configured","model","configVersion","draining"})

    def test_latest_authentication_action_survives_paging_and_clears_after_repair(self):
        self.seed(72)
        self.config.update({"model":{"api_key":"831"}})
        with self.store.connection(write=True) as db:
            db.execute("UPDATE turns SET status='failed',error_code='provider_http_401',model_snapshot=? WHERE id='80071'",
                       (json.dumps(self.config.load()["model"]),))
        first=timeline.page(self.store,self.pid,self.sid)
        older=timeline.page(self.store,self.pid,self.sid,before=first["older"])
        self.assertNotIn("80071",[turn["id"] for turn in older["turns"]])
        self.assertEqual(older["latest"],{"id":"80071","error_code":"provider_http_401","needs_connection":True})
        self.config.update({"model":{"api_key":"832"}})
        refreshed=timeline.page(self.store,self.pid,self.sid,version=first["version"])
        self.assertIsNone(refreshed["turns"])
        self.assertFalse(refreshed["latest"]["needs_connection"])
        self.assertEqual(timeline.page(self.store,self.pid,self.sid,before=0)["turns"],[])
        self.assertEqual(timeline.page(self.store,self.pid,self.sid,after=0)["turns"][0]["id"],"80000")

    def test_sealed_and_qq_sessions_keep_separate_authority(self):
        from persona_runtime.models import PersonaSpec
        from persona_runtime.sessions import select
        second=self.chat.service.create_persona(PersonaSpec(1,"824","824",["824"],"824",["824"],"824"))["id"]
        desktop=self.chat.session(persona_id=second)["session_id"]
        other=self.chat.session(persona_id=second,fresh=True)["session_id"]
        self.assertEqual(self.chat.service.show_persona()["id"],second)
        self.chat.service.use_persona(self.pid)
        with self.store.connection(write=True) as db:
            qq=select(db,second,"qq",origin={"bot":"825","sender":"826"})
        selection=timeline.selection(self.store,second)
        self.assertEqual(selection["active"],other)
        self.assertEqual({row["id"] for row in selection["sessions"]},{desktop,other})
        self.assertTrue(timeline.page(self.store,second,desktop)["sealed"])
        with self.assertRaises(RuntimeErrorBase):timeline.page(self.store,second,qq)
        self.assertEqual(self.chat.service.show_persona()["id"],self.pid)

    def test_unstructured_error_detail_does_not_reach_window(self):
        self.seed(1)
        with self.store.connection(write=True) as db:
            db.execute("UPDATE turns SET error_code=?,status='failed' WHERE id='80000'",("815 816",))
        card=timeline.page(self.store,self.pid,self.sid)["turns"][0]
        self.assertEqual(card["error_code"],"request_failed")
        self.assertNotIn("815 816",json.dumps(card))
        self.assertNotIn("model_snapshot",card)
