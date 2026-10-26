"""M1 acceptance checks. All generated files live under the runtime data root."""

from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.models import LOCAL_USER_ID, PersonaSpec, RuntimeErrorBase, generic_persona
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        base = RuntimePaths.from_environment().directories()["temp"]
        base.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="m1-test-", dir=base)
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.paths = RuntimePaths(self.home)
        self.store = Store(self.paths)
        self.store.initialize()
        self.service = RuntimeService(self.store)

    def seed(self):
        persona = self.service.create_persona(generic_persona())
        self.service.use_persona(persona["id"])
        session = self.service.create_session()
        event = self.service.append_event(session["id"], "user", "我喜欢喝茶。")
        memory = self.service.add_memory("preference", "用户喜欢喝茶", event["id"])
        return persona, session, event, memory

    def cli(self, *args, success=True):
        env = os.environ.copy()
        env.update(PERSONA_RUNTIME_HOME=str(self.home), PYTHONIOENCODING="utf-8",
                   PYTHONDONTWRITEBYTECODE="1", TEMP=str(self.home / "tmp"), TMP=str(self.home / "tmp"))
        result = subprocess.run([sys.executable, "-B", "-m", "persona_runtime", "--json", *args],
                                cwd=Path(__file__).resolve().parents[1], env=env,
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("error:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def test_idempotent_init_and_restart(self):
        persona, session, _, memory = self.seed()
        self.service.set_relationship("刚认识，按真实互动记录关系。")
        before = self.store.status()
        self.store.initialize()
        self.assertEqual(self.store.status(), before)
        fresh = RuntimeService(Store(self.paths))
        self.assertEqual(fresh.show_persona()["id"], persona["id"])
        self.assertEqual(len(fresh.show_session(session["id"])["events"]), 1)
        self.assertEqual(fresh.show_memory(memory["id"])["revision"], 1)
        self.assertIn("刚认识", fresh.show_relationship()["description"])

    def test_formal_persona_does_not_inherit(self):
        original, session, event, memory = self.seed()
        self.service.set_relationship("测试关系")
        formal = self.service.create_persona(replace(generic_persona(), name="正式人格"))
        self.assertNotEqual(formal["id"], original["id"])
        self.assertEqual(self.service.show_persona()["id"], original["id"])
        self.service.use_persona(formal["id"])
        self.assertEqual(self.service.search_memories(), [])
        self.assertEqual(self.service.list_sessions(), [])
        self.assertIsNone(self.service.show_relationship())
        operations = [
            lambda: self.service.show_session(session["id"]),
            lambda: self.service.append_event(session["id"], "user", "不允许"),
            lambda: self.service.add_memory("preference", "不允许", event["id"]),
            lambda: self.service.show_memory(memory["id"]),
            lambda: self.service.revise_memory(memory["id"], "不允许"),
            lambda: self.service.retract_memory(memory["id"]),
        ]
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(RuntimeErrorBase):
                operation()
        own_session = self.service.create_session()
        own_event = self.service.append_event(own_session["id"], "user", "新来源")
        own_memory = self.service.add_memory("user_fact", "新记忆", own_event["id"])
        with self.assertRaises(RuntimeErrorBase):
            self.service.revise_memory(own_memory["id"], "错误引用", event["id"])
        self.service.use_persona(original["id"])
        self.assertEqual(self.service.search_memories()[0]["id"], memory["id"])
        self.assertEqual(self.service.show_relationship()["description"], "测试关系")
        self.assertEqual(self.service.show_session(session["id"])["persona_id"], original["id"])

    def test_revision_and_retraction_keep_sources(self):
        _, session, event, memory = self.seed()
        correction = self.service.append_event(session["id"], "user", "现在更喜欢咖啡。")
        updated = self.service.revise_memory(memory["id"], "用户更喜欢咖啡", correction["id"])
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(self.service.search_memories("茶"), [])
        self.assertEqual(self.service.search_memories("咖啡")[0]["id"], updated["id"])
        shown = self.service.show_memory(memory["id"])
        self.assertEqual(shown["status"], "superseded")
        self.assertEqual([r["source"]["id"] for r in shown["history"]], [event["id"], correction["id"]])
        with self.assertRaises(RuntimeErrorBase):
            self.service.revise_memory(memory["id"], "不能修订旧版本")
        self.service.retract_memory(updated["id"])
        self.assertEqual(self.service.search_memories(), [])
        self.assertEqual(len(self.service.show_memory(updated["id"])["history"]), 2)
        with self.assertRaises(RuntimeErrorBase):
            self.service.retract_memory(updated["id"])

    def test_unicode_and_literal_search(self):
        _, session, _, _ = self.seed()
        text = "中文🙂\n引号'\"；%_；'); DROP TABLE memories; --"
        event = self.service.append_event(session["id"], "note", text)
        memory = self.service.add_memory("world_knowledge", text, event["id"])
        self.assertEqual(self.service.show_memory(memory["id"])["source"]["text"], text)
        for query in ("中文", "🙂", "\n", "%_", "'); DROP TABLE"):
            self.assertEqual(self.service.search_memories(query)[0]["text"], text)
        self.assertEqual(self.service.search_memories("%NOT_A_WILDCARD%"), [])
        events = self.service.show_session(session["id"])["events"]
        self.assertEqual([e["seq"] for e in events], sorted(e["seq"] for e in events))

    def test_search_limit_and_order(self):
        _, _, event, _ = self.seed()
        for i in range(22):
            self.service.add_memory("user_fact", f"事实{i}", event["id"])
        results = self.service.search_memories()
        self.assertEqual(len(results), 20)
        self.assertEqual(results[0]["text"], "事实21")
        self.assertEqual(len(self.service.search_memories(limit=1)), 1)
        for limit in (0, -1, 1001, True):
            with self.assertRaises(RuntimeErrorBase):
                self.service.search_memories(limit=limit)

    def test_validation_no_partial_writes(self):
        _, session, _, _ = self.seed()
        counts = self.store.status()["counts"]
        bad_specs = [None, {}, generic_persona().to_dict() | {"format_version": True},
                     generic_persona().to_dict() | {"core_traits": "不是列表"},
                     generic_persona().to_dict() | {"name": "  "}]
        for data in bad_specs:
            with self.assertRaises(RuntimeErrorBase):
                PersonaSpec.from_dict(data)
        with self.assertRaises(RuntimeErrorBase):
            self.service.create_persona(replace(generic_persona(), name=""))
        for operation in (
            lambda: self.service.create_session("group"),
            lambda: self.service.append_event(session["id"], "system", "不允许"),
            lambda: self.service.append_event(session["id"], "user", " "),
            lambda: self.service.add_memory("unknown", "不允许", "missing"),
            lambda: self.service.add_memory("user_fact", "没有来源", "missing"),
            lambda: self.service.use_persona("missing"),
        ):
            with self.assertRaises(RuntimeErrorBase):
                operation()
        self.assertEqual(self.store.status()["counts"], counts)

    def test_paths_and_template_contract(self):
        with self.assertRaises(ValueError):
            RuntimePaths(Path("relative-data"))
        with patch.dict(os.environ, {"PERSONA_RUNTIME_HOME": "relative-data"}):
            with self.assertRaises(ValueError):
                RuntimePaths.from_environment()
        template = Path(__file__).resolve().parents[1] / "templates" / "persona.generic.json"
        self.assertEqual(PersonaSpec.from_dict(json.loads(template.read_text(encoding="utf-8"))),
                         generic_persona())

    def test_other_user_sources_are_rejected(self):
        persona, _, _, memory = self.seed()
        other_user = "00000000-0000-4000-8000-000000000002"
        with self.store.connection(write=True) as conn:
            conn.execute("INSERT INTO users VALUES (?, 'other', '2026-10-26T00:00:00+00:00')", (other_user,))
            conn.execute("INSERT INTO sessions VALUES ('foreign-session', ?, ?, 'private', '2026-10-26T00:00:00+00:00')",
                         (persona["id"], other_user))
            conn.execute("INSERT INTO events (id, session_id, persona_id, user_id, role, text, created_at) "
                         "VALUES ('foreign-event', 'foreign-session', ?, ?, 'user', 'foreign', '2026-10-26T00:00:00+00:00')",
                         (persona["id"], other_user))
        for operation in (
            lambda: self.service.show_session("foreign-session"),
            lambda: self.service.add_memory("preference", "不允许", "foreign-event"),
            lambda: self.service.revise_memory(memory["id"], "不允许", "foreign-event"),
            lambda: self.service.revise_memory(memory["id"], "不允许", ""),
        ):
            with self.assertRaises(RuntimeErrorBase):
                operation()
        self.assertEqual(self.service.show_memory(memory["id"])["status"], "active")

    def test_revision_transaction_rolls_back_after_insert_failure(self):
        _, _, _, memory = self.seed()
        with self.store.connection(write=True) as conn:
            conn.execute("CREATE TRIGGER reject_revision BEFORE INSERT ON memories "
                         "WHEN NEW.revision > 1 BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.revise_memory(memory["id"], "不会留下半条更新")
        self.assertEqual(self.service.show_memory(memory["id"])["status"], "active")
        self.assertEqual(len(self.service.show_memory(memory["id"])["history"]), 1)

    def test_transaction_interruption_rolls_back(self):
        self.seed()
        before = self.store.status()["counts"]
        with self.assertRaises(KeyboardInterrupt):
            with self.store.connection(write=True) as conn:
                conn.execute("DELETE FROM settings")
                raise KeyboardInterrupt()
        self.assertEqual(self.store.status()["counts"], before)

    def test_lock_wait_and_no_partial_writes(self):
        self.seed()
        with self.store.connection() as conn:
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        locker = sqlite3.connect(self.store.path)
        try:
            locker.execute("BEGIN IMMEDIATE")
            short_wait_service = RuntimeService(Store(self.paths, timeout=0.05))
            with self.assertRaises(sqlite3.OperationalError):
                short_wait_service.create_persona(generic_persona())
        finally:
            locker.rollback()
            locker.close()
        self.assertEqual(self.store.status()["counts"]["personas"], 1)

    def test_schema_version_never_overwritten(self):
        self.seed()
        conn = sqlite3.connect(self.store.path)
        conn.execute("PRAGMA user_version=99")
        conn.close()
        before = self.store.path.read_bytes()
        for operation in (self.store.initialize, self.store.status,
                          lambda: self.service.create_persona(generic_persona())):
            with self.assertRaises(RuntimeErrorBase):
                operation()
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_unknown_unversioned_database_not_adopted(self):
        other = Store(RuntimePaths(self.home / "other"))
        other.paths.initialize()
        conn = sqlite3.connect(other.path)
        conn.execute("CREATE TABLE existing_data (value TEXT)")
        conn.commit()
        conn.close()
        before = other.path.read_bytes()
        with self.assertRaises(RuntimeErrorBase):
            other.initialize()
        self.assertEqual(other.path.read_bytes(), before)

    def test_failed_initialization_is_atomic(self):
        other = Store(RuntimePaths(self.home / "other"))
        with patch("persona_runtime.storage.SCHEMA", "CREATE TABLE first_step (id TEXT);\nINVALID SQL;\n"):
            with self.assertRaises(sqlite3.OperationalError):
                other.initialize()
        conn = sqlite3.connect(other.path)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])
        finally:
            conn.close()
        other.initialize()

    def test_foreign_key_rejects_cross_persona_source(self):
        _, _, event, memory = self.seed()
        second = self.service.create_persona(generic_persona())
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connection(write=True) as conn:
                conn.execute("UPDATE memories SET persona_id=? WHERE id=?", (second["id"], memory["id"]))
        self.assertEqual(self.service.show_memory(memory["id"])["source"]["id"], event["id"])

    def test_export_and_backup_independent_readable_snapshot(self):
        persona, _, _, memory = self.seed()
        self.service.set_relationship("备份关系")
        revised = self.service.revise_memory(memory["id"], "修订后的茶偏好")
        self.service.retract_memory(revised["id"])
        self.service.create_persona(replace(generic_persona(), name="另一个人格"))
        export_path = Path(self.service.export_persona("snapshot.json")["path"])
        payload = json.loads(export_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["persona"]["id"], persona["id"])
        self.assertEqual(len(payload["memories"]), 2)
        self.assertEqual(payload["user"]["id"], LOCAL_USER_ID)
        self.assertEqual(payload["relationships"][0]["description"], "备份关系")
        before_export = export_path.read_bytes()
        with self.assertRaises(RuntimeErrorBase):
            self.service.export_persona("snapshot.json")
        self.assertEqual(export_path.read_bytes(), before_export)
        backup_path = Path(self.service.backup("snapshot.sqlite3")["path"])
        backup_before = backup_path.read_bytes()
        with self.assertRaises(RuntimeErrorBase):
            self.service.backup("snapshot.sqlite3")
        self.assertEqual(backup_path.read_bytes(), backup_before)
        conn = sqlite3.connect(backup_path)
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            for table, count in self.store.status()["counts"].items():
                self.assertEqual(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], count)
        finally:
            conn.close()
        self.service.create_persona(generic_persona())
        conn = sqlite3.connect(backup_path)
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM personas").fetchone()[0], 2)
        finally:
            conn.close()
        for name in ("../escape.json", "D:\\escape.json", "..", ""):
            with self.assertRaises(RuntimeErrorBase):
                self.service.export_persona(name)

    def test_unavailable_or_unwritable_storage_no_fallback(self):
        with patch("pathlib.Path.exists", return_value=False):
            with self.assertRaises(OSError):
                RuntimePaths(self.home / "unavailable").initialize()
        with patch("pathlib.Path.mkdir", side_effect=PermissionError("injected unwritable storage")):
            with self.assertRaises(PermissionError):
                RuntimePaths(self.home / "unwritable").initialize()
        self.assertFalse((self.home / "unavailable").exists())
        self.assertFalse((self.home / "unwritable").exists())

    def test_read_only_database_write_failure(self):
        self.seed()
        conn = sqlite3.connect(self.store.path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
        conn.row_factory = sqlite3.Row
        with patch.object(self.store, "_connect", return_value=conn):
            with self.assertRaises(sqlite3.OperationalError):
                self.service.set_relationship("不可写")
        self.assertIsNone(self.service.show_relationship())

    def test_cli_end_to_end_acceptance_with_process_restarts(self):
        self.cli("db", "init")
        persona = self.cli("persona", "create")
        self.cli("persona", "show", success=False)
        self.cli("persona", "use", persona["id"])
        self.cli("relationship", "set", "--text", "验收关系")
        session = self.cli("session", "create")
        event = self.cli("event", "append", "--session", session["id"], "--role", "user", "--text", "我喜欢茶🙂")
        memory = self.cli("memory", "add", "--category", "preference", "--text", "用户喜欢茶🙂", "--source", event["id"])
        self.assertEqual(self.cli("memory", "search", "茶")[0]["id"], memory["id"])
        self.assertEqual(self.cli("relationship", "show")["description"], "验收关系")
        self.assertEqual(self.cli("session", "show", session["id"])["events"][0]["text"], "我喜欢茶🙂")
        revised = self.cli("memory", "revise", memory["id"], "--text", "用户偏好绿茶")
        self.assertEqual(len(self.cli("memory", "show", revised["id"])["history"]), 2)
        spec_file = self.home / "config" / "formal.json"
        spec_file.write_text(json.dumps(replace(generic_persona(), name="正式人格").to_dict(), ensure_ascii=False), encoding="utf-8-sig")
        formal = self.cli("persona", "create", "--file", str(spec_file))
        self.cli("persona", "use", formal["id"])
        self.assertEqual(self.cli("memory", "search"), [])
        self.assertEqual(self.cli("session", "list"), [])
        self.cli("memory", "show", memory["id"], success=False)
        self.cli("persona", "use", persona["id"])
        self.assertEqual(self.cli("memory", "search", "绿茶")[0]["id"], revised["id"])
        export = self.cli("data", "export")
        backup = self.cli("data", "backup")
        self.assertEqual(len(json.loads(Path(export["path"]).read_text(encoding="utf-8"))["memories"]), 2)
        conn = sqlite3.connect(backup["path"])
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM personas").fetchone()[0], 2)
        finally:
            conn.close()
        self.cli("memory", "retract", revised["id"])
        self.assertEqual(self.cli("memory", "search"), [])
        log = (self.home / "logs" / "operations.jsonl").read_text(encoding="utf-8")
        for secret in ("我喜欢茶", "用户喜欢茶", "验收关系", "用户偏好绿茶"):
            self.assertNotIn(secret, log)
        self.assertTrue(any(json.loads(line)["error_type"] for line in log.splitlines()))

    def test_cli_input_errors_and_repository_clean_of_data(self):
        malformed = self.home / "config" / "invalid.json"
        malformed.write_text('{"secret text":', encoding="utf-8")
        self.cli("persona", "create", "--file", str(malformed), success=False)
        self.assertEqual(self.store.status()["counts"]["personas"], 0)
        self.cli("persona", "create", "--file", str(self.home / "missing.json"), success=False)
        self.cli("memory", "search", "--limit", "0", success=False)
        repo = Path(__file__).resolve().parents[1]
        for pattern in ("*.sqlite3", "*.sqlite3-journal", "operations.jsonl", "__pycache__"):
            self.assertEqual(list(repo.rglob(pattern)), [], pattern)


if __name__ == "__main__":
    unittest.main()
