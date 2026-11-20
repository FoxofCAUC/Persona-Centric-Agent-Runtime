"""Crash boundaries for desktop ingress, main receipts and the encrypted outbox."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from persona_runtime import desktop_recovery as recovery
from persona_runtime.chat import ChatEngine, ModelCompletion
from persona_runtime.config import ConfigStore
from persona_runtime.desktop_client import LocalClient, TransportUnknown
from persona_runtime.ingress import IngressService
from persona_runtime.models import PersonaSpec, RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from persona_runtime.workbench_submissions import WorkbenchSubmissions


class Completion:
    def __init__(self):
        self.calls = 0

    async def complete(self, model, messages):
        self.calls += 1
        return ModelCompletion("902", {"total_tokens": 1}, {"finish_reason": "stop", "network_route": "local_fixture"})


class DesktopRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        root = RuntimePaths.from_environment().home / "tmp"
        root.mkdir(parents=True, exist_ok=True)
        temp = TemporaryDirectory(dir=root)
        self.addCleanup(temp.cleanup)
        self.paths = RuntimePaths(Path(temp.name))
        self.config = ConfigStore(self.paths)
        self.config.initialize()
        self.config.update({"model": {"base_url": "http://127.0.0.1:9", "name": "900"}})
        self.store = Store(self.paths)
        self.store.initialize()
        service = RuntimeService(self.store)
        self.pid = service.create_persona(PersonaSpec(1,"901","901",["901"],"901",["901"],"901"))["id"]
        service.use_persona(self.pid)
        self.model = Completion()
        self.now = 1000.
        self.reopen()
        self.sid = self.chat.session()["session_id"]
        self.payload = {"channel": "desktop", "text": "903", "persona_id": self.pid, "session_id": self.sid, "request_id": "904"}

    def reopen(self):
        self.chat = ChatEngine(self.store, self.config, self.model)
        self.ingress = IngressService(self.chat, lambda:self.now, lambda:self.now)

    async def asyncTearDown(self):
        await self.chat.wait()

    async def queued_v1(self):
        with patch.object(self.chat, "_schedule"):
            value = await self.chat.submit("desktop","907","908",self.sid,persona_id=self.pid)
        return value["id"]

    async def test_two_restarts_one_batch_and_one_reply(self):
        first = self.ingress.accept(self.payload)
        self.now += 2
        for _ in range(2):
            self.reopen()
            self.chat.recover()
            self.ingress.recover()
            self.ingress.tick()
            self.assertEqual(self.ingress.accept(self.payload)["receipt_id"],first["receipt_id"])
            self.assertEqual(self.model.calls,0)
        self.now += 3.01
        self.ingress.tick()
        await self.chat.wait()
        turn = self.ingress.lookup("desktop","904")["turn_id"]
        self.assertEqual((self.chat.get(turn)["status"], self.model.calls), ("completed", 1))

    async def test_legacy_interruption_does_not_block_next_batch(self):
        first = self.ingress.accept(self.payload)
        with self.store.connection(write=True) as db:
            db.execute("UPDATE input_batches SET policy_json=json_remove(policy_json,'$.desktop_binding') WHERE id=?", (first["batch_id"],))
        self.reopen()
        self.ingress.recover()
        self.assertEqual(self.ingress.unresolved_count(),0)
        next_item = self.ingress.accept(self.payload | {"request_id":"905"})
        self.now += 6
        self.ingress.tick()
        await self.chat.wait()
        self.assertEqual(self.ingress.get(first["batch_id"])["state"],"interrupted_buffer")
        self.assertEqual(self.ingress.get(next_item["batch_id"])["status"],"completed")

    async def test_sealed_batch_reuses_snapshot(self):
        first = self.ingress.accept(self.payload)
        with patch.object(self.ingress,"_enqueue"):
            self.now += 6
            self.ingress.tick()
        self.config.update({"model":{"name":"906"}})
        self.reopen()
        self.ingress.recover()
        self.ingress.tick()
        await self.chat.wait()
        with self.store.connection() as db:
            frozen = db.execute("SELECT model_snapshot FROM turns WHERE input_batch_id=?", (first["batch_id"],)).fetchone()[0]
        self.assertEqual((json.loads(frozen)["name"], self.model.calls), ("900",1))

    async def test_v1_claimed_before_preparation(self):
        tid = await self.queued_v1()
        with self.store.connection(write=True) as db:
            turn = db.execute("SELECT * FROM turns WHERE id=?", (tid,)).fetchone()
            self.assertTrue(recovery.claim(db,turn,self.config.load(),"909"))
            self.assertFalse(recovery.claim(db,turn,self.config.load(),"910"))
        self.reopen()
        with patch("persona_runtime.instance.process_identity_alive",return_value=False):
            self.chat.recover()
        self.ingress.tick()
        self.ingress.tick()
        await self.chat.wait()
        self.assertEqual((self.chat.get(tid)["status"],self.model.calls),("completed",1))

    def test_actual_process_exit_before_dispatch_and_after_receipt(self):
        child = """
import asyncio, os, sys
from pathlib import Path
from unittest.mock import patch
from persona_runtime.paths import RuntimePaths
from persona_runtime.config import ConfigStore
from persona_runtime.storage import Store
from persona_runtime.chat import ChatEngine, ModelCompletion
from persona_runtime.ingress import IngressService

paths = RuntimePaths(Path(sys.argv[1]))
phase, pid, sid = sys.argv[2:]
counter = paths.home / "tmp/transport-count"
class LocalTransport:
    async def complete(self, model, messages):
        with counter.open("a", encoding="ascii") as output:
            output.write("1")
        return ModelCompletion("936", {"total_tokens":1}, {"finish_reason":"stop"})
async def main():
    chat = ChatEngine(Store(paths), ConfigStore(paths), LocalTransport())
    ingress = IngressService(chat)
    if phase == "receive":
        with patch.object(chat, "_schedule"):
            await chat.submit("desktop","934","935",sid,persona_id=pid)
    else:
        chat.recover()
        ingress.recover()
        if phase == "crash":
            chat._finish_reply = lambda *args, **kwargs: os._exit(71)
        ingress.tick()
        await chat.wait()
asyncio.run(main())
"""
        for phase, expected in (("receive",0),("crash",71),("recover",0),("recover",0)):
            run = subprocess.run([sys.executable,"-B","-c",child,str(self.paths.home),phase,self.pid,self.sid],
                                 capture_output=True,timeout=45)
            self.assertEqual(run.returncode,expected,run.stderr.decode(errors="replace"))
        self.assertEqual((self.paths.home/"tmp/transport-count").read_text(encoding="ascii"),"1")
        self.assertEqual(self.chat.lookup("desktop","935")["reply"],"936")

    async def test_jev_unknown_leaves_following_turn_usable(self):
        tid = await self.queued_v1()
        with self.store.connection(write=True) as db:
            recovery.save(db,"scene.route."+tid,{"started":1000,"scene":None,"reason":None})
        self.chat.recover()
        self.ingress.tick()
        self.assertEqual((self.chat.get(tid)["status"],self.model.calls),("interrupted",0))
        await self.chat.submit("desktop","911","912",self.sid,persona_id=self.pid)
        await self.chat.wait()
        self.assertEqual(self.model.calls,1)

    async def test_cancel_prevents_recovery(self):
        tid = await self.queued_v1()
        self.chat.cancel_queued(tid)
        self.chat.recover()
        self.ingress.tick()
        await self.chat.wait()
        self.assertEqual(self.model.calls,0)
        self.assertTrue(self.chat.get(tid)["cancelled_at"])

    async def test_plain_receipt_before_publication(self):
        tid = await self.queued_v1()
        with patch.object(self.chat,"_finish_reply"):
            self.chat._schedule(tid)
            await self.chat.wait()
        self.assertEqual(self.chat.get(tid)["status"],"running")
        for _ in range(2):
            self.reopen()
            with patch("persona_runtime.instance.process_identity_alive",return_value=False):
                self.chat.recover()
        self.assertEqual((self.chat.get(tid)["reply"],self.model.calls),("902",1))

    async def test_local_settlement_error_reuses_success_without_restart(self):
        tid = await self.queued_v1()
        with patch.object(self.chat,"_finish_reply",side_effect=sqlite3.OperationalError("937")):
            self.chat._schedule(tid)
            await self.chat.wait()
        self.assertEqual(self.chat.get(tid)["error_code"],"desktop_settlement_pending")
        self.ingress.tick()
        self.assertEqual((self.chat.get(tid)["reply"],self.model.calls),("902",1))

    def test_database_restore_invalidates_ticket(self):
        from contextlib import closing
        ticket = recovery.issue_intent(self.store,self.config,self.payload)
        snapshot = self.paths.home/"tmp/before.sqlite"
        with closing(sqlite3.connect(self.store.path)) as source, closing(sqlite3.connect(snapshot)) as dest:
            source.backup(dest)
        self.ingress.accept(self.payload,desktop_ticket=ticket)
        with closing(sqlite3.connect(snapshot)) as source, closing(sqlite3.connect(self.store.path)) as dest:
            source.backup(dest)
        with self.assertRaisesRegex(RuntimeErrorBase,"desktop_continuity_unconfirmed"):
            self.ingress.accept(self.payload,desktop_ticket=ticket)
        self.assertIsNone(self.ingress.lookup("desktop","904"))

    async def test_unfinished_main_dispatch_and_exhausted_budget_stay_isolated(self):
        for index in range(2):
            with patch.object(self.chat,"_schedule"):
                turn = await self.chat.submit("desktop","927",str(928+index),self.sid,persona_id=self.pid)
            tid = turn["id"]
            attempt = "930"+str(index)
            entries = ([{"status":"running","index":0}] if index == 0 else [
                {"status":"failed","index":rank,"error_code":"provider_timeout","diagnostics":{"request_headers_sent":False}}
                for rank in range(4)])
            with self.store.connection(write=True) as db:
                stored = db.execute("SELECT * FROM turns WHERE id=?",(tid,)).fetchone()
                recovery.claim(db,stored,self.config.load(),"931")
                recovery.transition(db,stored,"931","main_pending",attempt=attempt,wire="932")
                db.execute("INSERT INTO attempts VALUES (?,?,?,'running',NULL,?,?)",
                           (attempt,tid,self.now,json.dumps({"generation_retry":{"attempts":entries}}),stored["model_snapshot"]))
                db.execute("UPDATE turns SET status='running' WHERE id=?",(tid,))
            self.reopen()
            with patch("persona_runtime.instance.process_identity_alive",return_value=False):
                self.chat.recover()
            self.ingress.tick()
            self.assertEqual((self.chat.get(tid)["status"],self.model.calls),("interrupted",0))

    async def test_before_first_transport_reuses_logical_attempt(self):
        tid = await self.queued_v1()
        with patch("persona_runtime.generation_retry.audited_complete",side_effect=RuntimeErrorBase("933")):
            await self.chat._run(tid)
        with self.store.connection(write=True) as db:
            db.execute("UPDATE turns SET status='running',error_code=NULL WHERE id=?",(tid,))
            attempt=db.execute("SELECT id FROM attempts WHERE turn_id=?",(tid,)).fetchone()[0]
        self.reopen()
        with patch("persona_runtime.instance.process_identity_alive",return_value=False):
            self.chat.recover()
        self.ingress.tick()
        await self.chat.wait()
        self.assertEqual(self.chat.get(tid)["status"],"completed")
        with self.store.connection() as db:
            self.assertEqual([r[0] for r in db.execute("SELECT id FROM attempts WHERE turn_id=?",(tid,))],[attempt])
        self.assertEqual(self.model.calls,1)

    def test_lost_response_and_identical_independent_sends(self):
        client = LocalClient(self.config)
        outbox = WorkbenchSubmissions(self.config,client)
        posts = []
        def call(method,path,value=None):
            if path=="/v1/health":
                return {"instance_id":self.config.load()["instance_id"],"capabilities":["desktop_submission_recovery_v1"]}
            if path=="/v1/manage":
                with self.store.connection() as db:
                    return recovery.lookup_intent(db,value["args"])
            self.assertEqual(path,"/v2/ingress")
            values = dict(value)
            ticket = values.pop("desktop_ticket")
            posts.append(values["request_id"])
            result = self.ingress.accept(values,desktop_ticket=ticket)
            if len(posts)==1:
                raise TransportUnknown("913")
            return result
        with patch.object(client,"call",side_effect=call):
            self.assertEqual(outbox.submit(self.payload,"914")["state"],"unconfirmed")
            restarted = WorkbenchSubmissions(self.config,client)
            self.assertEqual(restarted.reconcile(self.pid,"915"),[])
            restarted.journal.update("904",next_check=0)
            self.assertEqual(restarted.reconcile(self.pid,self.sid)[0]["state"],"accepted")
            self.assertEqual(restarted.submit(self.payload,"914")["state"],"accepted")
            self.assertEqual(restarted.submit(self.payload|{"request_id":"916"},"917")["state"],"accepted")
            self.assertEqual(posts,["904","916"])
        first = {"previous":None,"revision":"918","reference":None,"text":"903"}
        outbox.draft(self.pid,self.sid,first)
        updated = outbox.draft(self.pid,self.sid,first|{"previous":"918","revision":"919"})
        self.assertEqual(outbox.draft(self.pid,self.sid,first|{"previous":"918","text":""}),updated)

    def test_changed_recipient_and_conflicting_payload(self):
        ticket = recovery.issue_intent(self.store,self.config,self.payload)
        self.config.update({"model":{"name":"920"}})
        with self.assertRaisesRegex(RuntimeErrorBase,"desktop_intent_changed"):
            self.ingress.accept(self.payload,desktop_ticket=ticket)
        self.ingress.accept(self.payload)
        with self.store.connection() as db:
            with self.assertRaisesRegex(RuntimeErrorBase,"submission_conflict"):
                recovery.lookup_intent(db,self.payload|{"text":"921"})
        self.assertEqual(self.model.calls,0)

    def test_unknown_lookup_wrong_instance_and_legacy_server_never_post(self):
        client = LocalClient(self.config)
        outbox = WorkbenchSubmissions(self.config,client)
        posted = []
        def route(method,path,value=None):
            if path == "/v1/health":
                return health
            if path == "/v1/manage":
                from persona_runtime.desktop_client import LocalAPIError
                raise LocalAPIError("local_api_error",404)
            posted.append(path)
            raise AssertionError(path)
        for number, health in enumerate((
                {"instance_id":"938","capabilities":["desktop_submission_recovery_v1"]},
                {"instance_id":self.config.load()["instance_id"],"capabilities":[]},
                {"instance_id":self.config.load()["instance_id"],"capabilities":["desktop_submission_recovery_v1"]})):
            with patch.object(client,"call",side_effect=route):
                item = outbox.submit(self.payload|{"request_id":str(939+number)},str(number))
                self.assertEqual(item["state"],"blocked")
        self.assertEqual(posted,[])

    async def test_live_owner_and_late_cancelled_task(self):
        tid = await self.queued_v1()
        with self.store.connection(write=True) as db:
            turn = db.execute("SELECT * FROM turns WHERE id=?", (tid,)).fetchone()
            recovery.claim(db,turn,self.config.load(),"944")
            recovery.transition(db,turn,"944","preparing")
        other = ChatEngine(self.store,self.config,self.model)
        other.recover()
        self.assertEqual(other.get(tid)["status"],"queued")
        await other._run(tid)
        self.assertEqual(self.model.calls,0)
        self.chat.cancel_queued(tid)
        await other._run(tid)
        self.assertEqual(self.model.calls,0)
