"""M2 offline acceptance; all fixtures and HTTP servers stay local."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from persona_runtime.chat import ChatCompletionsClient, ChatEngine, ModelError, recall
from persona_runtime.config import ConfigStore
from persona_runtime.models import RuntimeErrorBase, generic_persona
from persona_runtime.paths import RuntimePaths
from persona_runtime.qq_bridge import QQBridge, allowed_message
from persona_runtime.security import protect, unprotect
from persona_runtime.server import create_app
from persona_runtime.service import RuntimeService
from persona_runtime.storage import SCHEMA, M1_TABLES, Store, execute_schema


class FakeModel:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.gate = None

    async def complete(self, model, messages):
        self.calls.append((dict(model), messages))
        if self.gate:
            await self.gate.wait()
        if self.failure:
            raise ModelError(self.failure)
        return "模拟回复：" + messages[-1]["content"], {"total_tokens": 10}


class M2Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        base = RuntimePaths.from_environment().home / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="m2-test-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.paths = RuntimePaths(Path(self.temp.name))
        self.cfg = ConfigStore(self.paths)
        self.cfg.initialize()
        self.cfg.update({"model": {"base_url": "http://127.0.0.1:9999/v1", "name": "offline-model", "api_key": "test-secret-only"},
                         "bot_qq": "123456", "owner_qq": "654321"})
        self.store = Store(self.paths)
        self.store.initialize()
        self.service = RuntimeService(self.store)
        self.persona = self.service.create_persona(generic_persona())
        self.service.use_persona(self.persona["id"])
        self.model = FakeModel()
        self.engine = ChatEngine(self.store, self.cfg, self.model)

    async def asyncTearDown(self):
        await self.engine.wait()

    async def chat(self, text="我喜欢什么茶？", request_id="first", channel="desktop"):
        turn = await self.engine.submit(channel, text, request_id)
        await self.engine.wait()
        return self.engine.get(turn["id"])

    async def test_context_shared_memory_separate_channel_and_restart(self):
        self.service.set_relationship("相互尊重的朋友")
        session = self.service.create_session()
        event = self.service.append_event(session["id"], "user", "我喜欢乌龙茶")
        self.service.add_memory("preference", "用户喜欢乌龙茶", event["id"])
        first = await self.chat("我喜欢什么茶？")
        self.assertEqual(first["status"], "completed")
        await self.chat("继续窗口对话", "second")
        await self.chat("聊聊乌龙茶", "qq-first", "qq")
        desktop = self.model.calls[1][1]
        qq = self.model.calls[2][1]
        self.assertIn("相互尊重的朋友", qq[0]["content"])
        self.assertIn("乌龙茶", qq[0]["content"])
        self.assertEqual(len(qq), 2)
        self.assertEqual(len(desktop), 4)
        fresh = ChatEngine(Store(self.paths), self.cfg, self.model)
        fresh.recover()
        self.assertEqual(fresh.get(first["id"])["status"], "completed")
        self.assertEqual(fresh.session()["session_id"], first["session_id"])
        self.assertNotEqual(fresh.session("qq")["session_id"], first["session_id"])

    async def test_inflight_persona_and_model_snapshot_and_dedup(self):
        self.model.gate = asyncio.Event()
        original = await self.engine.submit("desktop", "原人格消息", "stable")
        await asyncio.sleep(.01)
        other = self.service.create_persona(replace(generic_persona(), name="新人格"))
        self.service.use_persona(other["id"])
        self.cfg.update({"model": {"name": "new-model"}})
        repeated = await self.engine.submit("desktop", "重复推送", "stable")
        self.assertEqual(repeated["id"], original["id"])
        self.model.gate.set()
        await self.engine.wait()
        result = self.engine.get(original["id"])
        self.assertEqual(result["persona_id"], self.persona["id"])
        self.assertEqual(self.model.calls[0][0]["name"], "offline-model")
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.service.list_sessions(), [])
        with self.store.connection() as conn:
            reply = conn.execute("SELECT persona_id FROM events WHERE id=?", (result["reply_event_id"],)).fetchone()
            self.assertEqual(reply[0], self.persona["id"])

    async def test_fail_retry_atomicity_and_exclude_failed_context(self):
        self.model.failure = "provider_timeout"
        failed = await self.chat("失败的敏感问题", "failure")
        self.assertEqual(failed["status"], "failed")
        self.assertIsNone(failed["reply_event_id"])
        self.model.failure = None
        await self.chat("另一轮消息", "next")
        self.assertFalse(any("失败的敏感问题" in m["content"] for m in self.model.calls[-1][1]))
        await self.engine.retry(failed["id"])
        await self.engine.wait()
        revised = self.engine.get(failed["id"])
        self.assertEqual(revised["user_event_id"], failed["user_event_id"])
        self.assertEqual(revised["attempt_count"], 2)
        self.assertEqual(revised["status"], "completed")
        await self.engine.retry(failed["id"])
        self.assertEqual(len(self.model.calls), 3)

    async def test_quota_persistent_and_test_call_counted(self):
        self.cfg.update({"model": {"hourly_limit": 1}})
        test = await self.engine.submit("test", "OK", "probe", test=True)
        await self.engine.wait()
        self.assertEqual(self.engine.get(test["id"])["status"], "completed")
        self.engine = ChatEngine(Store(self.paths), self.cfg, self.model)
        turn = await self.chat(request_id="over-limit")
        self.assertEqual(turn["error_code"], "hourly_limit_reached")
        self.assertEqual(len(self.model.calls), 1)

    async def test_queue_bound_and_serial_execution(self):
        self.model.gate = asyncio.Event()
        await self.engine.submit("desktop", "运行中", "active")
        await asyncio.sleep(.01)
        for i in range(10):
            await self.engine.submit("desktop", "等待中", f"queued-{i}")
        with self.assertRaisesRegex(RuntimeErrorBase, "queue_full"):
            await self.engine.submit("desktop", "超限", "overflow")
        self.assertEqual(len(self.model.calls), 1)
        self.model.gate.set()
        await self.engine.wait()
        self.assertEqual(len(self.model.calls), 11)

    async def test_context_limit_and_unrelated_memory(self):
        self.service.set_relationship("关系"*600)
        self.cfg.update({"model": {"max_input_chars": 1000}})
        failed = await self.chat(request_id="context-limit")
        self.assertEqual(failed["error_code"], "context_limit_exceeded")
        self.assertEqual(self.model.calls, [])
        self.assertEqual(recall("乌龙茶", [{"id": "x", "text": "偏爱篮球", "updated_at": "now"}]), [])

    async def test_recovery_and_uncertain_delivery(self):
        turn = await self.chat(channel="qq")
        self.assertTrue(self.engine.delivery(turn["id"], "claim")["claimed"])
        self.assertFalse(self.engine.delivery(turn["id"], "claim")["claimed"])
        with self.store.connection(write=True) as conn:
            conn.execute("UPDATE turns SET status='running' WHERE id=?", (turn["id"],))
        self.engine.recover()
        state = self.engine.get(turn["id"])
        self.assertEqual(state["status"], "interrupted")
        self.assertEqual(state["delivery"], "uncertain")
        self.assertEqual(len(self.model.calls), 1)

    async def test_service_auth_management_and_bridge_restrictions(self):
        app = create_app(self.paths, self.model)
        async with TestServer(app) as server, ClientSession() as client:
            async with client.get(server.make_url("/v1/health")) as response:
                self.assertEqual(response.status, 401)
            admin = {"Authorization": "Bearer " + self.cfg.token()}
            bridge = {"Authorization": "Bearer " + self.cfg.token("bridge")}
            async with client.get(server.make_url("/v1/config"), headers=admin) as response:
                public = await response.json()
                self.assertNotIn("admin_token", public)
                self.assertNotIn("encrypted_key", public["model"])
            async with client.get(server.make_url("/v1/config"), headers=bridge) as response:
                self.assertEqual(response.status, 403)
            async with client.post(server.make_url("/v1/chat"), headers=bridge,
                                   json={"channel": "qq", "sender": "intruder", "bot": "123456", "text": "x", "request_id": "qq-x"}) as response:
                self.assertEqual(response.status, 403)
            async with client.post(server.make_url("/v1/chat"), headers=admin,
                                   json={"text": "窗口验证", "request_id": "http-desktop"}) as response:
                turn = await response.json()
            await app["engine"].wait()
            async with client.get(server.make_url("/v1/requests/" + turn["id"]), headers=bridge) as response:
                self.assertEqual(response.status, 403)
            async with client.post(server.make_url("/v1/manage"), headers=admin,
                                   json={"action": "persona.list"}) as response:
                self.assertEqual(len(await response.json()), 1)
            async with client.get(server.make_url("/v1/health"), headers=admin | {"Origin": "https://example.invalid"}) as response:
                self.assertEqual(response.status, 403)

    async def test_chat_completions_http_contract_and_errors(self):
        seen = []
        status = [200]
        async def provider(request):
            seen.append(await request.json())
            return web.json_response({"choices": [{"message": {"content": "本地模拟"}}], "usage": {"total_tokens": 5}}, status=status[0])
        app = web.Application()
        app.router.add_post("/v1/chat/completions", provider)
        async with TestServer(app) as server:
            model = self.cfg.load()["model"] | {"base_url": str(server.make_url("/v1"))}
            text, usage = await ChatCompletionsClient().complete(model, [{"role": "user", "content": "test"}])
            self.assertEqual(text, "本地模拟")
            self.assertEqual(usage["total_tokens"], 5)
            self.assertEqual(seen[0]["max_tokens"], 1024)
            self.assertFalse(seen[0]["stream"])
            for code in (401, 429, 500):
                status[0] = code
                with self.assertRaisesRegex(ModelError, f"provider_http_{code}"):
                    await ChatCompletionsClient().complete(model, [])

    async def test_dpapi_and_configuration_no_plaintext_secret(self):
        self.assertEqual(unprotect(protect("机密🙂")), "机密🙂")
        self.assertNotIn("test-secret-only", self.cfg.file.read_text(encoding="utf-8"))
        self.assertNotIn("test-secret-only", json.dumps(self.cfg.public()))
        before = self.cfg.file.read_bytes()
        with self.assertRaises(RuntimeErrorBase):
            self.cfg.update({"service_port": 6399})
        self.assertEqual(before, self.cfg.file.read_bytes())

    async def test_m1_migration_backup_and_failure_rollback(self):
        paths = RuntimePaths(self.paths.home / "legacy")
        paths.initialize()
        store = Store(paths)
        conn = sqlite3.connect(store.path, isolation_level=None)
        execute_schema(conn, SCHEMA)
        conn.execute("INSERT INTO users VALUES ('legacy-user','旧用户','before')")
        conn.execute("PRAGMA user_version=1")
        conn.close()
        before = store.path.read_bytes()
        with patch("persona_runtime.storage.SCHEMA2", "INVALID SQL;\n"):
            with self.assertRaises(sqlite3.Error):
                store.initialize()
        self.assertEqual(before, store.path.read_bytes())
        store.initialize()
        with store.connection() as conn:
            self.assertEqual(conn.execute("SELECT name FROM users WHERE id='legacy-user'").fetchone()[0], "旧用户")
        backups = list((paths.home / "backups").glob("before-m2-*.sqlite3"))
        self.assertTrue(backups)
        conn = sqlite3.connect(backups[0])
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            conn.close()

    def fake_event(self):
        raw = {"message_type": "private", "sub_type": "friend", "user_id": "654321", "self_id": "123456",
               "message_id": "42", "message": [{"type": "text", "data": {"text": "你好"}}]}
        return SimpleNamespace(platform_meta=SimpleNamespace(name="aiocqhttp", id="persona-onebot"),
            is_private_chat=lambda: True, get_self_id=lambda: "123456", get_sender_id=lambda: "654321",
            message_obj=SimpleNamespace(raw_message=raw, message_id="42"))

    async def test_qq_gate_and_delivery_idempotency(self):
        event = self.fake_event()
        cfg = self.cfg.load()
        self.assertIsNotNone(allowed_message(event, cfg))
        for field, value in (("sub_type", "group"), ("message_type", "group"), ("user_id", "intruder"),
                             ("message_id", ""), ("self_id", "other"),
                             ("message", [{"type": "image", "data": {}}])):
            sample = self.fake_event()
            sample.message_obj.raw_message[field] = value
            self.assertIsNone(allowed_message(sample, cfg), field)
        app = create_app(self.paths, self.model)
        async with TestServer(app) as server:
            # Bridge has a configured fixed port; intercept only its HTTP location for this fixture.
            bridge = QQBridge(self.paths)
            async def request(method, path, value=None):
                async with ClientSession() as client:
                    async with client.request(method, server.make_url(path), json=value,
                        headers={"Authorization": "Bearer " + self.cfg.token("bridge")}) as response:
                        self.assertEqual(response.status, 200)
                        return await response.json()
            bridge.request = request
            sent = []
            async def send(text):
                sent.append(text)
            self.assertTrue(await bridge.handle(event, send))
            self.assertFalse(await bridge.handle(event, send))
            self.assertEqual(len(sent), 1)
            self.assertEqual(len(self.model.calls), 1)


if __name__ == "__main__":
    unittest.main()
