"""Persona and memory operations, independent of CLI and model providers."""

import json
from pathlib import Path
import sqlite3

from .models import (CATEGORIES, LOCAL_USER_ID, ROLES, PersonaSpec, RuntimeErrorBase,
                     identifier, require_text, timestamp)
from .storage import Store
from .memory_policy import exclude_memory


class RuntimeService:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def _persona(conn, persona_id: str | None = None) -> dict:
        persona_id = Store.current(conn) if persona_id is None else persona_id
        row = conn.execute("SELECT * FROM personas WHERE id=?", (persona_id,)).fetchone()
        if not row:
            raise RuntimeErrorBase("Persona not found")
        result = dict(row)
        result["spec"] = json.loads(result.pop("spec_json"))
        return result

    @staticmethod
    def _owned(conn, table: str, record_id: str, persona_id: str) -> dict:
        # table is internal, never taken from user input.
        row = conn.execute(f"SELECT * FROM {table} WHERE id=? AND persona_id=? AND user_id=?",
                           (record_id, persona_id, LOCAL_USER_ID)).fetchone()
        if not row:
            raise RuntimeErrorBase("Record not found in the selected persona and user scope")
        return dict(row)

    def create_persona(self, spec: PersonaSpec) -> dict:
        data = spec.to_dict()
        with self.store.connection(write=True) as conn:
            persona_id = identifier()
            conn.execute("INSERT INTO personas VALUES (?, ?, ?, ?, ?)",
                         (persona_id, spec.name, spec.format_version,
                          json.dumps(data, ensure_ascii=False), timestamp()))
            from .profile import initialize_profiles
            initialize_profiles(conn,persona_id,actor='create')
            from .character import initialize_characters
            initialize_characters(conn,persona_id,actor='create')
            return self._persona(conn, persona_id)

    def list_personas(self) -> list[dict]:
        with self.store.connection() as conn:
            current = Store.current(conn, required=False)
            return [dict(row) | {"selected": row["id"] == current} for row in conn.execute(
                "SELECT id, name, format_version, created_at FROM personas ORDER BY created_at, id")]

    def show_persona(self, persona_id: str | None = None) -> dict:
        with self.store.connection() as conn:
            return self._persona(conn, persona_id)

    def use_persona(self, persona_id: str) -> dict:
        with self.store.connection(write=True) as conn:
            persona = self._persona(conn, persona_id)
            conn.execute("INSERT INTO settings VALUES ('current_persona', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (persona_id,))
            return persona

    def set_relationship(self, description: str) -> dict:
        require_text(description, "description")
        with self.store.connection(write=True) as conn:
            persona_id = self._persona(conn)["id"]
            from .profile import ProfileService,head
            ProfileService(self.store)._write(conn,persona_id,'relationship',{'description':description},head(conn,persona_id,'relationship')['id'],identifier())
            return dict(conn.execute("SELECT * FROM relationships WHERE persona_id=? AND user_id=?",
                                     (persona_id, LOCAL_USER_ID)).fetchone())

    def show_relationship(self) -> dict | None:
        with self.store.connection() as conn:
            persona_id = self._persona(conn)["id"]
            row = conn.execute("SELECT * FROM relationships WHERE persona_id=? AND user_id=?",
                               (persona_id, LOCAL_USER_ID)).fetchone()
            return dict(row) if row else None

    def create_session(self, scope: str = "private") -> dict:
        if scope != "private":
            raise RuntimeErrorBase("Only local single-user private scope is supported")
        with self.store.connection(write=True) as conn:
            persona_id = self._persona(conn)["id"]
            session_id = identifier()
            conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                         (session_id, persona_id, LOCAL_USER_ID, scope, timestamp()))
            return self._owned(conn, "sessions", session_id, persona_id)

    def list_sessions(self) -> list[dict]:
        with self.store.connection() as conn:
            persona_id = self._persona(conn)["id"]
            return [dict(row) for row in conn.execute(
                "SELECT * FROM sessions WHERE persona_id=? AND user_id=? ORDER BY created_at, id",
                (persona_id, LOCAL_USER_ID))]

    def show_session(self, session_id: str) -> dict:
        with self.store.connection() as conn:
            persona_id = self._persona(conn)["id"]
            result = self._owned(conn, "sessions", session_id, persona_id)
            result["events"] = [dict(row) for row in conn.execute(
                "SELECT * FROM events WHERE session_id=? AND persona_id=? AND user_id=? ORDER BY seq",
                (session_id, persona_id, LOCAL_USER_ID))]
            return result

    def append_event(self, session_id: str, role: str, text: str) -> dict:
        require_text(text, "text")
        if role not in ROLES:
            raise RuntimeErrorBase("Unsupported event role")
        with self.store.connection(write=True) as conn:
            persona_id = self._persona(conn)["id"]
            self._owned(conn, "sessions", session_id, persona_id)
            event_id = identifier()
            conn.execute("INSERT INTO events (id, session_id, persona_id, user_id, role, text, created_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (event_id, session_id, persona_id, LOCAL_USER_ID, role, text, timestamp()))
            return self._owned(conn, "events", event_id, persona_id)

    def add_memory(self, category: str, text: str, source_event_id: str) -> dict:
        require_text(text, "text")
        if category not in CATEGORIES:
            raise RuntimeErrorBase("Unsupported memory category")
        with self.store.connection(write=True) as conn:
            persona_id = self._persona(conn)["id"]
            self._owned(conn, "events", source_event_id, persona_id)
            memory_id = identifier()
            now = timestamp()
            conn.execute("INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (memory_id, persona_id, LOCAL_USER_ID, category, text, source_event_id,
                          memory_id, None, 1, "active", now, now))
            return self._owned(conn, "memories", memory_id, persona_id)

    def search_memories(self, query: str = "", limit: int = 20) -> list[dict]:
        if not isinstance(query, str):
            raise RuntimeErrorBase("query must be a string")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise RuntimeErrorBase("limit must be between 1 and 1000")
        with self.store.connection() as conn:
            persona_id = self._persona(conn)["id"]
            return [dict(row) for row in conn.execute(
                "SELECT * FROM memories WHERE persona_id=? AND user_id=? AND status='active' "
                "AND instr(text, ?) > 0 ORDER BY updated_at DESC, id DESC LIMIT ?",
                (persona_id, LOCAL_USER_ID, query, limit))]

    def show_memory(self, memory_id: str) -> dict:
        with self.store.connection() as conn:
            persona_id = self._persona(conn)["id"]
            result = self._owned(conn, "memories", memory_id, persona_id)
            result["source"] = self._owned(conn, "events", result["source_event_id"], persona_id)
            result["sources"] = [dict(r) for r in conn.execute("SELECT * FROM memory_sources WHERE memory_id=?", (memory_id,))]
            result["history"] = []
            for row in conn.execute(
                "SELECT * FROM memories WHERE root_id=? AND persona_id=? AND user_id=? ORDER BY revision",
                (result["root_id"], persona_id, LOCAL_USER_ID)):
                revision = dict(row)
                revision["source"] = self._owned(conn, "events", row["source_event_id"], persona_id)
                result["history"].append(revision)
            return result

    def revise_memory(self, memory_id: str, text: str, source_event_id: str | None = None) -> dict:
        require_text(text, "text")
        with self.store.connection(write=True) as conn:
            persona_id = self._persona(conn)["id"]
            old = self._owned(conn, "memories", memory_id, persona_id)
            if old["status"] != "active":
                raise RuntimeErrorBase("Only the active memory revision can be revised")
            source = old["source_event_id"] if source_event_id is None else source_event_id
            self._owned(conn, "events", source, persona_id)
            exclude_memory(conn, old, "memory_revised")
            now = timestamp()
            conn.execute("UPDATE memories SET status='superseded', updated_at=? WHERE id=?", (now, memory_id))
            new_id = identifier()
            conn.execute("INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (new_id, persona_id, LOCAL_USER_ID, old["category"], text, source,
                          old["root_id"], old["id"], old["revision"] + 1, "active", now, now))
            if source_event_id is None:
                conn.execute("INSERT INTO memory_sources SELECT ?,event_id,quote FROM memory_sources WHERE memory_id=?", (new_id, old["id"]))
            return self._owned(conn, "memories", new_id, persona_id)

    def retract_memory(self, memory_id: str) -> dict:
        with self.store.connection(write=True) as conn:
            persona_id = self._persona(conn)["id"]
            old = self._owned(conn, "memories", memory_id, persona_id)
            if old["status"] != "active":
                raise RuntimeErrorBase("Only the active memory revision can be retracted")
            exclude_memory(conn, old, "memory_retracted")
            conn.execute("UPDATE memories SET status='retracted', updated_at=? WHERE id=?",
                         (timestamp(), memory_id))
            return self._owned(conn, "memories", memory_id, persona_id)

    def _output_path(self, directory: str, name: str | None, extension: str) -> Path:
        name = name if name is not None else f"{directory}-{identifier()}{extension}"
        if not name or Path(name).name != name or ":" in name or name in (".", ".."):
            raise RuntimeErrorBase("Output name must be a simple file name, without a directory")
        path = self.store.paths.directories()[directory] / name
        if path.exists():
            raise RuntimeErrorBase("Output already exists; choose a new name")
        return path

    def export_persona(self, name: str | None = None) -> dict:
        with self.store.connection() as conn:
            persona = self._persona(conn)
            payload = {"format_version": 4, "exported_at": timestamp(), "persona": persona,
                       "user": dict(conn.execute("SELECT * FROM users WHERE id=?", (LOCAL_USER_ID,)).fetchone())}
            for table in ("relationships", "sessions", "events", "memories"):
                order = "seq" if table == "events" else "rowid"
                payload[table] = [dict(r) for r in conn.execute(
                    f"SELECT * FROM {table} WHERE persona_id=? AND user_id=? ORDER BY {order}",
                    (persona["id"], LOCAL_USER_ID))]
            for table in ("summaries", "candidates", "candidate_reviews", "source_exclusions", "organization_progress", "automation", "context_uses"):
                payload[table] = [dict(r) for r in conn.execute(f"SELECT * FROM {table} WHERE persona_id=? AND user_id=? ORDER BY rowid", (persona["id"], LOCAL_USER_ID))]
            payload["memory_sources"] = [dict(r) for r in conn.execute("SELECT s.* FROM memory_sources s JOIN memories m ON m.id=s.memory_id WHERE m.persona_id=? AND m.user_id=?", (persona["id"], LOCAL_USER_ID))]
            for table in ('profile_versions','relationship_proposals'):
                payload[table]=[dict(r) for r in conn.execute(f'SELECT * FROM {table} WHERE persona_id=? AND user_id=? ORDER BY rowid',(persona['id'],LOCAL_USER_ID))]
            for table in ('character_versions','character_heads'):
                payload[table]=[dict(r) for r in conn.execute(f'SELECT * FROM {table} WHERE persona_id=? AND user_id=? ORDER BY rowid',(persona['id'],LOCAL_USER_ID))]
            payload['profile_heads']=[dict(r) for r in conn.execute('SELECT * FROM profile_heads WHERE persona_id=?',(persona['id'],))]
        path = self._output_path("exports", name, ".json")
        created = False
        try:
            with path.open("x", encoding="utf-8") as output:
                created = True
                json.dump(payload, output, ensure_ascii=False, indent=2)
                output.write("\n")
        except BaseException:
            if created:
                path.unlink(missing_ok=True)
            raise
        return {"path": str(path), "persona_id": persona["id"]}

    def backup(self, name: str | None = None) -> dict:
        path = self._output_path("backups", name, ".sqlite3")
        with path.open("xb"):
            pass
        try:
            with self.store.connection() as source:
                destination = sqlite3.connect(path)
                try:
                    destination.execute("PRAGMA temp_store = MEMORY")
                    source.backup(destination)
                    if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeErrorBase("Backup integrity check failed")
                finally:
                    destination.close()
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return {"path": str(path)}
