"""SQLite storage, schema checks and atomic transactions."""

from contextlib import contextmanager
import sqlite3
from uuid import uuid4

from .models import LOCAL_USER_ID, RuntimeErrorBase, timestamp
from .paths import RuntimePaths

SCHEMA_VERSION = 4
M1_TABLES = ("users", "personas", "relationships", "sessions", "events", "memories", "settings")
M2_TABLES = M1_TABLES + ("channel_sessions", "turns", "attempts", "deliveries")
TABLES = M2_TABLES + ("request_activity", "service_notices")

SCHEMA = """
CREATE TABLE users (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE personas (
    id TEXT PRIMARY KEY, name TEXT NOT NULL,
    format_version INTEGER NOT NULL CHECK(format_version = 1),
    spec_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE settings (
    key TEXT PRIMARY KEY CHECK(key = 'current_persona'),
    value TEXT NOT NULL REFERENCES personas(id)
);
CREATE TABLE relationships (
    persona_id TEXT NOT NULL REFERENCES personas(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    description TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(persona_id, user_id)
);
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    scope TEXT NOT NULL CHECK(scope = 'private'), created_at TEXT NOT NULL,
    UNIQUE(id, persona_id, user_id)
);
CREATE TABLE events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE, session_id TEXT NOT NULL,
    persona_id TEXT NOT NULL, user_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user', 'persona', 'note')),
    text TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(id, persona_id, user_id),
    FOREIGN KEY(session_id, persona_id, user_id) REFERENCES sessions(id, persona_id, user_id)
);
CREATE TABLE memories (
    id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    category TEXT NOT NULL CHECK(category IN
        ('user_fact', 'preference', 'shared_experience', 'relationship', 'world_knowledge')),
    text TEXT NOT NULL, source_event_id TEXT NOT NULL, root_id TEXT NOT NULL,
    previous_id TEXT UNIQUE, revision INTEGER NOT NULL CHECK(revision >= 1),
    status TEXT NOT NULL CHECK(status IN ('active', 'superseded', 'retracted')),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(id, persona_id, user_id), UNIQUE(root_id, revision),
    FOREIGN KEY(source_event_id, persona_id, user_id) REFERENCES events(id, persona_id, user_id),
    FOREIGN KEY(previous_id, persona_id, user_id) REFERENCES memories(id, persona_id, user_id),
    FOREIGN KEY(root_id, persona_id, user_id) REFERENCES memories(id, persona_id, user_id)
);
CREATE UNIQUE INDEX one_active_memory ON memories(root_id) WHERE status = 'active';
CREATE INDEX events_session_order ON events(session_id, seq);
CREATE INDEX memories_scope ON memories(persona_id, user_id, status, updated_at);
"""

SCHEMA2 = """
CREATE TABLE channel_sessions (
    persona_id TEXT NOT NULL REFERENCES personas(id),
    channel TEXT NOT NULL CHECK(channel IN ('desktop','qq')),
    user_id TEXT NOT NULL REFERENCES users(id), session_id TEXT NOT NULL,
    PRIMARY KEY(persona_id, channel),
    FOREIGN KEY(session_id, persona_id, user_id) REFERENCES sessions(id, persona_id, user_id)
);
CREATE TABLE turns (
    id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE,
    persona_id TEXT REFERENCES personas(id), session_id TEXT REFERENCES sessions(id),
    channel TEXT NOT NULL CHECK(channel IN ('desktop','qq','test')),
    user_event_id TEXT REFERENCES events(id), reply_event_id TEXT REFERENCES events(id),
    status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','interrupted')),
    error_code TEXT, model_snapshot TEXT NOT NULL, usage_json TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE attempts (
    id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns(id),
    started_epoch REAL NOT NULL, status TEXT NOT NULL,
    error_code TEXT, usage_json TEXT, model_snapshot TEXT NOT NULL
);
CREATE TABLE deliveries (
    turn_id TEXT PRIMARY KEY REFERENCES turns(id),
    state TEXT NOT NULL CHECK(state IN ('pending','sending','sent','uncertain','failed')),
    updated_at TEXT NOT NULL
);
CREATE INDEX turns_session ON turns(session_id, created_at);
CREATE INDEX attempts_quota ON attempts(started_epoch);
"""

SCHEMA3 = "ALTER TABLE turns ADD COLUMN origin_json TEXT NOT NULL DEFAULT '{}';\n"

SCHEMA4 = """
ALTER TABLE turns ADD COLUMN cancelled_at TEXT;
CREATE TABLE request_activity (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id TEXT NOT NULL REFERENCES turns(id),
    attempt_id TEXT REFERENCES attempts(id),
    event TEXT NOT NULL,
    at_epoch REAL NOT NULL
);
CREATE INDEX activity_turn ON request_activity(turn_id,seq);
CREATE TABLE service_notices (
    turn_id TEXT NOT NULL REFERENCES turns(id),
    kind TEXT NOT NULL CHECK(kind IN ('waiting','failure')),
    claimed_at TEXT NOT NULL,
    PRIMARY KEY(turn_id,kind)
);
"""


def execute_schema(conn, schema):
    statement = ""
    for line in schema.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""


class Store:
    def __init__(self, paths: RuntimePaths, timeout: float = 5.0):
        self.paths = paths
        self.path = paths.directories()["database"] / "runtime.sqlite3"
        self.timeout = timeout

    def _connect(self, *, create: bool = False) -> sqlite3.Connection:
        if create:
            conn = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
        else:
            if not self.path.is_file():
                raise RuntimeErrorBase("Database is not initialized; run db init")
            conn = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                                   timeout=self.timeout, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA temp_store = MEMORY")
        return conn

    @staticmethod
    def _check(conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise RuntimeErrorBase(f"Incompatible database schema version {version}; expected {SCHEMA_VERSION}")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not set(TABLES).issubset(tables):
            raise RuntimeErrorBase("Database schema is incomplete; restore a verified backup")

    @contextmanager
    def connection(self, *, write: bool = False):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            self._check(conn)
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        self.paths.initialize()
        conn = self._connect(create=True)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version in (1, 2, 3):
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not set(M1_TABLES).issubset(tables):
                    raise RuntimeErrorBase("M1 database schema is incomplete; migration refused")
                prefix = "before-m3" if version == 3 else "before-m2"
                backup = self.paths.home / "backups" / f"{prefix}-{uuid4()}.sqlite3"
                target = sqlite3.connect(backup)
                try:
                    conn.backup(target)
                finally:
                    target.close()
            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if version == 0 and not tables:
                execute_schema(conn, SCHEMA)
                execute_schema(conn, SCHEMA2)
                execute_schema(conn, SCHEMA3)
                execute_schema(conn, SCHEMA4)
                conn.execute("INSERT INTO users VALUES (?, ?, ?)",
                             (LOCAL_USER_ID, "本地用户", timestamp()))
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version == 1 and set(M1_TABLES).issubset(tables):
                execute_schema(conn, SCHEMA2)
                execute_schema(conn, SCHEMA3)
                execute_schema(conn, SCHEMA4)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version == 2 and set(M2_TABLES).issubset(tables):
                execute_schema(conn, SCHEMA3)
                execute_schema(conn, SCHEMA4)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version == 3 and set(M2_TABLES).issubset(tables):
                execute_schema(conn, SCHEMA4)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            else:
                self._check(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def status(self) -> dict:
        with self.connection() as conn:
            return {
                "database": str(self.path), "schema_version": SCHEMA_VERSION,
                "current_persona": self.current(conn, required=False),
                "counts": {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES},
            }

    @staticmethod
    def current(conn: sqlite3.Connection, *, required: bool = True) -> str | None:
        row = conn.execute("SELECT value FROM settings WHERE key='current_persona'").fetchone()
        if not row and required:
            raise RuntimeErrorBase("No persona selected; run persona use ID")
        return row[0] if row else None
