"""Offline M3 state-machine, timing and stable-submission acceptance."""
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from persona_runtime.chat import ChatEngine, ModelError
from persona_runtime.config import ConfigStore
from persona_runtime.desktop_client import LocalClient, LocalAPIError, TransportUnknown
from persona_runtime.models import RuntimeErrorBase, generic_persona, LOCAL_USER_ID
from persona_runtime.paths import RuntimePaths
from persona_runtime.server import create_app
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store, SCHEMA, SCHEMA2, SCHEMA3, execute_schema
from persona_runtime.submissions import SubmissionJournal
from persona_runtime.qq_bridge import QQBridge
from concurrent.futures import ThreadPoolExecutor


class ControlledModel:
    def __init__(self):
        self.calls = []
        self.gate = None
        self.fail = False

    async def complete(self, model, messages):
        self.calls.append(messages[-1]["content"])
        if self.gate:
            await self.gate.wait()
        if self.fail:
            raise ModelError("provider_network_error")
        await asyncio.sleep(.01)
        return "回复："+messages[-1]["content"], {"total_tokens": 5}


class M3Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        root = RuntimePaths.from_environment().home / "tmp"
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="m3-test-", dir=root)
        self.addCleanup(self.temp.cleanup)
        self.paths = RuntimePaths(Path(self.temp.name))
        self.cfg = ConfigStore(self.paths)
        self.cfg.initialize()
        self.cfg.update({"bot_qq": "123", "owner_qq": "456", "model": {"base_url": "http://127.0.0.1:9999/v1", "name": "fake"}})
        self.store = Store(self.paths)
        self.store.initialize()
        self.service = RuntimeService(self.store)
        self.persona = self.service.create_persona(generic_persona())
        self.service.use_persona(self.persona["id"])
        self.model = ControlledModel()
        self.engine = ChatEngine(self.store, self.cfg, self.model)

    async def asyncTearDown(self):
        if self.model.gate:
            self.model.gate.set()
        await self.engine.wait()

    async def test_three_messages_are_ordered_and_timed(self):
        turns = [await self.engine.submit("desktop", text, "id-"+text) for text in ("第一句", "第二句", "第三句")]
        await self.engine.wait()
        self.assertEqual(self.model.calls, ["第一句", "第二句", "第三句"])
        for turn in turns:
            result = self.engine.get(turn["id"])
            self.assertEqual(result["phase"], "completed")
            self.assertGreaterEqual(result["timings"]["queue_seconds"], 0)
            self.assertGreaterEqual(result["timings"]["provider_seconds"], .005)
            self.assertIsNone(result["timings"]["first_output_seconds"])
            self.assertIsNone(result["timings"]["delivery_seconds"])

    async def test_cancel_queued_never_calls_model(self):
        self.model.gate = asyncio.Event()
        running = await self.engine.submit("desktop", "running", "running")
        async with asyncio.timeout(5):
            while not self.model.calls:await asyncio.sleep(.005)
        queued = await self.engine.submit("desktop", "cancel-me", "cancel-me")
        cancelled = self.engine.cancel_queued(queued["id"])
        self.assertEqual(cancelled["phase"], "cancelled")
        with self.assertRaisesRegex(RuntimeErrorBase, "only_queued_can_cancel"):
            self.engine.cancel_queued(running["id"])
        self.assertEqual(self.engine.cancel_queued(queued["id"])["phase"], "cancelled")
        self.model.gate.set()
        await self.engine.wait()
        self.assertEqual(self.model.calls, ["running"])
        self.assertEqual(self.engine.get(queued["id"])["attempt_count"], 0)
        await self.engine.retry(queued["id"])
        await self.engine.wait()
        self.assertEqual(self.engine.get(queued["id"])["phase"], "completed")

    async def test_lookup_and_conflicting_payload(self):
        turn = await self.engine.submit("desktop", "原文", "fixed")
        found = self.engine.lookup("desktop", "fixed")
        self.assertEqual(found["id"], turn["id"])
        self.assertIsNone(self.engine.lookup("desktop", "missing"))
        again = await self.engine.submit("desktop", "原文", "fixed")
        self.assertEqual(again["id"], turn["id"])
        with self.assertRaisesRegex(RuntimeErrorBase, "submission_conflict"):
            await self.engine.submit("desktop", "另一段文字", "fixed")
        await self.engine.wait()
        self.assertEqual(len(self.model.calls), 1)

    async def test_unchanged_history_does_not_resend_bodies(self):
        turn = await self.engine.submit("desktop", "snapshot", "snapshot")
        initial = self.engine.history_snapshot(turn["session_id"])
        self.assertIsNone(self.engine.history_snapshot(turn["session_id"], initial["version"])["turns"])
        await self.engine.wait()
        changed = self.engine.history_snapshot(turn["session_id"], initial["version"])
        self.assertIsNotNone(changed["turns"])
        self.assertGreater(changed["version"], initial["version"])

    async def test_qq_claim_prevents_out_of_order_and_interleaved_send(self):
        first = await self.engine.submit("qq", "first", "first")
        second = await self.engine.submit("qq", "second", "second")
        await self.engine.wait()
        self.assertFalse(self.engine.delivery(second["id"], "claim")["claimed"])
        self.assertTrue(self.engine.delivery(first["id"], "claim")["claimed"])
        self.assertFalse(self.engine.delivery(second["id"], "claim")["claimed"])
        self.engine.delivery(first["id"], "sent")
        self.assertTrue(self.engine.delivery(second["id"], "claim")["claimed"])
        self.engine.delivery(second["id"], "uncertain")
        self.assertIsNotNone(self.engine.get(first["id"])["timings"]["delivery_seconds"])
        self.assertEqual(self.engine.get(second["id"])["phase"], "delivery_uncertain")

    async def test_failure_retry_has_distinct_metrics_and_no_new_input(self):
        self.model.fail = True
        turn = await self.engine.submit("desktop", "failed", "failed")
        await self.engine.wait()
        failed = self.engine.get(turn["id"])
        self.assertEqual(failed["phase"], "failed")
        self.assertTrue(failed["recovery"])
        self.model.fail = False
        await self.engine.retry(turn["id"])
        await self.engine.wait()
        result = self.engine.get(turn["id"])
        self.assertEqual(result["user_event_id"], turn["user_event_id"])
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(sum(e["event"] == "provider_started" for e in result["events"]), 2)

    async def test_encrypted_journal_survives_reopen(self):
        payload = {"channel": "desktop", "request_id": "fixed", "persona_id": self.persona["id"], "text": "不应明文保存的草稿"}
        journal = SubmissionJournal(self.paths, "desktop")
        journal.remember(payload)
        self.assertNotIn(payload["text"], journal.path.read_text(encoding="utf-8"))
        reopened = SubmissionJournal(self.paths, "desktop")
        self.assertEqual(reopened.pending()[0]["payload"], payload)
        self.assertEqual(reopened.remember(payload | {"request_id": "new"}, match_draft=True)["request_id"], "fixed")

    async def test_local_submit_response_lost_then_recovered_without_second_post(self):
        client = LocalClient(self.cfg)
        payload = {"channel": "desktop", "request_id": "stable", "text": "message", "persona_id": self.persona["id"]}
        accepted = {"id": "accepted", "status": "queued"}
        with patch.object(client, "lookup", return_value=None), patch.object(client, "call", side_effect=TransportUnknown("lost")):
            with self.assertRaises(TransportUnknown):
                client.submit(payload)
        self.assertEqual(len(client.journal.pending()), 1)
        with patch.object(client, "lookup", return_value=accepted), patch.object(client, "call") as call:
            result = client.submit(payload | {"request_id": "new-id"})
            self.assertEqual(result, accepted)
            call.assert_not_called()
        self.assertEqual(client.journal.pending(), [])

    async def test_known_rejection_keeps_no_unknown_journal(self):
        client = LocalClient(self.cfg)
        payload = {"channel": "desktop", "request_id": "full", "text": "message"}
        with patch.object(client, "lookup", return_value=None), patch.object(client, "call", side_effect=LocalAPIError("queue_full", 400)):
            with self.assertRaises(LocalAPIError):
                client.submit(payload)
        self.assertEqual(client.journal.pending(), [])

    async def test_lookup_auth_error_preserves_unconfirmed_input(self):
        client = LocalClient(self.cfg)
        payload = {"channel": "desktop", "request_id": "unknown", "text": "synthetic"}
        with patch.object(client, "lookup", side_effect=LocalAPIError("unauthorized", 401)), patch.object(client, "call") as call:
            with self.assertRaises(LocalAPIError):
                client.submit(payload)
            call.assert_not_called()
        self.assertEqual(client.journal.pending()[0]["payload"], payload)

    async def test_independent_journal_writers_do_not_lose_messages(self):
        def write(index):
            SubmissionJournal(self.paths, "qq").remember({"request_id": str(index), "text": str(index)})
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(write, range(20)))
        self.assertEqual(len(SubmissionJournal(self.paths, "qq").pending()), 20)

    async def test_journal_waits_for_existing_byte_lock_without_creation_probe(self):
        import threading
        import time
        first=SubmissionJournal(self.paths,'qq');second=SubmissionJournal(self.paths,'qq')
        started=threading.Event()
        def read():
            started.set()
            return second.pending()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with first._locked():
                future=pool.submit(read)
                self.assertTrue(started.wait(1))
                time.sleep(.05)
                self.assertFalse(future.done())
            self.assertEqual(future.result(timeout=3),[])

    async def test_qq_reconcile_only_queries_matching_target_never_replays(self):
        bridge = QQBridge(self.paths)
        for request_id, sender in (("accepted", "456"), ("absent", "456"), ("old-target", "999")):
            bridge.journal.remember({"request_id": request_id, "sender": sender, "bot": "123", "text": "synthetic"})
        calls = []
        async def lookup(payload):
            calls.append(payload["request_id"])
            return {"id": "found"} if payload["request_id"] == "accepted" else None
        with patch.object(bridge, "lookup", side_effect=lookup), patch.object(bridge, "request") as request:
            await bridge.reconcile_inbox()
            request.assert_not_called()
        self.assertEqual(calls, ["accepted", "absent"])
        rows = {r["payload"]["request_id"]: r["state"] for r in bridge.journal.pending()}
        self.assertEqual(rows, {"absent": "not_accepted", "old-target": "unconfirmed"})

    async def test_service_notice_default_off_and_durable_once(self):
        self.model.gate = asyncio.Event()
        turn = await self.engine.submit("qq", "notice", "notice")
        await asyncio.sleep(.01)
        self.assertFalse(self.engine.claim_notice(turn["id"], "waiting")["claimed"])
        self.cfg.update({"interaction": {"qq_wait_notice": True}})
        self.assertTrue(self.engine.claim_notice(turn["id"], "waiting")["claimed"])
        self.assertFalse(self.engine.claim_notice(turn["id"], "waiting")["claimed"])
        self.assertEqual(self.store.status()["counts"]["service_notices"], 1)

    async def test_cancel_http_requires_explicit_setting(self):
        app = create_app(self.paths, self.model)
        self.model.gate = asyncio.Event()
        async with TestServer(app) as server, ClientSession() as client:
            admin = {"Authorization": "Bearer " + self.cfg.token()}
            first = await app["engine"].submit("desktop", "running", "first")
            await asyncio.sleep(.01)
            queued = await app["engine"].submit("desktop", "queued", "queued")
            url = server.make_url(f"/v1/requests/{queued['id']}/cancel")
            async with client.post(url, headers=admin, json={}) as response:
                self.assertNotEqual(response.status, 200)
            self.cfg.update({"interaction": {"cancel_queued": True}})
            async with client.post(url, headers=admin, json={}) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())["phase"], "cancelled")
            self.model.gate.set()
            await app["engine"].wait()

    async def test_v3_migration_preserves_legacy_history_without_fake_timings(self):
        legacy = Store(RuntimePaths(self.paths.home / "v3"))
        legacy.paths.initialize()
        conn = sqlite3.connect(legacy.path, isolation_level=None)
        for schema in (SCHEMA, SCHEMA2, SCHEMA3):
            execute_schema(conn, schema)
        conn.execute('INSERT INTO users VALUES (?,?,?)',(LOCAL_USER_ID,'本地用户','2026-01-01T00:00:00Z'))
        conn.execute("INSERT INTO personas VALUES (?,?,?,?,?)", ("legacy-persona", "旧人格", 1, json.dumps(generic_persona().to_dict()), "2026-01-01T00:00:00Z"))
        conn.execute("INSERT INTO settings VALUES ('current_persona','legacy-persona')")
        conn.execute("PRAGMA user_version=3")
        conn.close()
        with patch("persona_runtime.storage.SCHEMA4", "ALTER TABLE turns ADD COLUMN cancelled_at TEXT; CREATE TABLE personas (id TEXT);"):
            with self.assertRaises(sqlite3.Error):
                legacy.initialize()
        with sqlite3.connect(legacy.path) as failed:
            self.assertEqual(failed.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertNotIn("cancelled_at", [r[1] for r in failed.execute("PRAGMA table_info(turns)")])
        failed.close()
        legacy.initialize()
        self.assertEqual(legacy.status()["schema_version"], 12)
        with legacy.connection() as migrated:
            self.assertEqual(migrated.execute("SELECT value FROM settings").fetchone()[0], "legacy-persona")
        self.assertTrue(list((legacy.paths.home / "backups").glob("before-m3-*.sqlite3")))

    async def test_lookup_api_scope_and_missing(self):
        app = create_app(self.paths, self.model)
        async with TestServer(app) as server, ClientSession() as client:
            admin = {"Authorization": "Bearer " + self.cfg.token()}
            bridge = {"Authorization": "Bearer " + self.cfg.token("bridge")}
            async with client.get(server.make_url("/v1/submissions/desktop/missing"), headers=admin) as response:
                self.assertEqual(response.status, 404)
            async with client.get(server.make_url("/v1/submissions/desktop/missing"), headers=bridge) as response:
                self.assertEqual(response.status, 403)
            async with client.post(server.make_url("/v1/chat"), headers=admin, json={"text": "api", "request_id": "same"}) as response:
                turn = await response.json()
            async with client.get(server.make_url("/v1/submissions/desktop/same"), headers=admin) as response:
                self.assertEqual((await response.json())["id"], turn["id"])
            await app["engine"].wait()


if __name__ == "__main__":
    unittest.main()
