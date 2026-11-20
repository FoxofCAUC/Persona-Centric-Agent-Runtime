"""SQLite storage, schema checks and atomic transactions."""

from contextlib import contextmanager
import sqlite3
from uuid import uuid4

from .models import LOCAL_USER_ID, RuntimeErrorBase, timestamp
from .paths import RuntimePaths
from .schema_m4 import SCHEMA5, TABLES5
from .schema_m5 import SCHEMA6, TABLES6

from .schema_m6 import SCHEMA7, TABLES7
from .schema_m61 import SCHEMA8, TABLES8
from .schema_m62 import SCHEMA9, TABLES9
from .schema_m63 import SCHEMA10, TABLES10, refresh_development_scope_trigger

from .schema_m70 import SCHEMA11, TABLES11
from .schema_m72 import SCHEMA12, TABLES12
from .schema_m73 import SCHEMA13, TABLES13
from .schema_disposition import TABLES19
from .schema_observation import TABLES20

SCHEMA_VERSION = 36
M1_TABLES = ("users", "personas", "relationships", "sessions", "events", "memories", "settings")
M2_TABLES = M1_TABLES + ("channel_sessions", "turns", "attempts", "deliveries")
M3_TABLES = M2_TABLES + ("request_activity", "service_notices")
M4_TABLES = M3_TABLES + TABLES5
M5_TABLES = M4_TABLES + TABLES6
M6_TABLES = M5_TABLES + TABLES7
TABLES = M6_TABLES + TABLES8 + TABLES9 + TABLES10 + TABLES11 + TABLES12 + TABLES13

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
        if SCHEMA_VERSION >= 36:
            from .schema_local_knowledge import STUDY_TABLES
            if not set(STUDY_TABLES).issubset(tables):
                raise RuntimeErrorBase('knowledge_study_schema_incomplete')
        if SCHEMA_VERSION >= 35:
            from .schema_local_knowledge import TABLES as local_tables, INDEX
            if not set((*local_tables, INDEX)).issubset(tables):
                raise RuntimeErrorBase('local_knowledge_schema_incomplete')
        if SCHEMA_VERSION >= 34:
            from .schema_private_reply import TABLES as private_reply_tables
            if not set(private_reply_tables).issubset(tables):
                raise RuntimeErrorBase('private_reply_schema_incomplete')
        if SCHEMA_VERSION >= 33:
            from .schema_dialogue import check
            check(tables)
        if SCHEMA_VERSION>=32:
            from .schema_expression_utterance import TABLES as speech_tables
            tables32={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not set(speech_tables).issubset(tables32):raise RuntimeErrorBase('expression_speech_schema_incomplete')
        if SCHEMA_VERSION>=31:
            from .schema_expression_runtime import TABLES as expression_runtime_tables
            if not set(expression_runtime_tables).issubset(tables):raise RuntimeErrorBase('Expression runtime schema is incomplete')
        if SCHEMA_VERSION>=30:
            from .schema_expression_assets import TABLES as expression_asset_tables
            if not set(expression_asset_tables).issubset(tables):raise RuntimeErrorBase('Expression asset schema is incomplete')
        if SCHEMA_VERSION>=29:
            from .schema_recall_runtime import TABLES as runtime_recall_tables
            if not set(runtime_recall_tables).issubset(tables):raise RuntimeErrorBase('Recall runtime schema is incomplete')
        if SCHEMA_VERSION>=28:
            from .schema_recall_support import TABLES as recall_support_tables
            if not set(recall_support_tables).issubset(tables):raise RuntimeErrorBase('Recall support schema is incomplete')
        if SCHEMA_VERSION>=27:
            from .schema_m9b_shadow import TABLES as recall_tables
            if not set(recall_tables).issubset(tables):raise RuntimeErrorBase('Recall shadow schema is incomplete')
        if SCHEMA_VERSION>=26:
            from .schema_m9a3 import TABLES as public_knowledge_tables
            if not set(public_knowledge_tables).issubset(tables):raise RuntimeErrorBase('Public knowledge schema is incomplete')
        if SCHEMA_VERSION>=25:
            from .schema_m9a import TABLES as knowledge_tables
            if not set(knowledge_tables).issubset(tables):raise RuntimeErrorBase('Knowledge schema is incomplete')
        if SCHEMA_VERSION>=24:
            from .schema_m8e import TABLES as application_tables
            if not set(application_tables).issubset(tables):raise RuntimeErrorBase('Application runtime schema is incomplete')
        if SCHEMA_VERSION>=23:
            from .schema_m8d import TABLES as automatic_tables
            if not set(automatic_tables).issubset(tables):
                raise RuntimeErrorBase('Automatic memory schema is incomplete')
        if SCHEMA_VERSION>=22:
            from .schema_m8a import TABLES as managed_tables
            if not set(managed_tables).issubset(tables):
                raise RuntimeErrorBase('Managed task schema is incomplete')
        if not set(TABLES + (('character_selection_attempts','quota_reservations') if SCHEMA_VERSION>=15 else ()) + (('persona_lifecycle','persona_operations') if SCHEMA_VERSION>=16 else ()) + (('reply_display_messages',) if SCHEMA_VERSION>=17 else ()) + (TABLES19 if SCHEMA_VERSION>=19 else ()) + (TABLES20 if SCHEMA_VERSION>=20 else ()) + (('organization_receipts',) if SCHEMA_VERSION>=21 else ())).issubset(tables):
            raise RuntimeErrorBase("Database schema is incomplete; restore a verified backup")

    @contextmanager
    def connection(self, *, write: bool = False):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            self._check(conn)
            yield conn
            conn.commit()
        except BaseException as error:
            conn.rollback()
            if isinstance(error,sqlite3.Error) and getattr(error,'sqlite_errorcode',0)&255 in (8,10,13):
                from .maintenance import mark_write_fault
                mark_write_fault(self.paths,self.path.parent)
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        self.paths.initialize()
        conn = self._connect(create=True)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if 1 <= version < SCHEMA_VERSION:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not set(M1_TABLES).issubset(tables):
                    raise RuntimeErrorBase("M1 database schema is incomplete; migration refused")
                prefix = "before-m72" if version == 11 else "before-m70" if version == 10 else "before-m63" if version == 9 else "before-m62" if version == 8 else "before-m61" if version == 7 else "before-m6" if version == 6 else ("before-m5" if version == 5 else ("before-m4" if version == 4 else ("before-m3" if version == 3 else "before-m2")))
                prefix = 'before-m73' if version == 12 else prefix
                prefix = 'before-m74' if version == 13 else prefix
                prefix = 'before-m754r2' if version == 14 else prefix
                prefix = 'before-persona-management' if version == 15 else prefix
                prefix = 'before-rolling-chat' if version == 16 else prefix
                prefix = 'before-observation' if version == 19 else prefix
                prefix = 'before-memory-repair' if version == 20 else prefix
                prefix = 'before-m8a' if version == 21 else prefix
                prefix = 'before-m8d' if version == 22 else prefix
                prefix = 'before-m8e' if version == 23 else prefix
                prefix = 'before-m9a' if version == 24 else prefix
                prefix = 'before-m9a3' if version == 25 else prefix
                prefix = 'before-m9b-shadow' if version == 26 else prefix
                prefix = 'before-recall-support' if version == 27 else prefix
                prefix = 'before-recall-runtime' if version == 28 else prefix
                prefix = 'before-expression-assets' if version == 29 else prefix
                prefix = 'before-expression-runtime' if version == 30 else prefix
                prefix = 'before-expression-utterance' if version == 31 else prefix
                prefix = 'before-dialogue-generation' if version == 32 else prefix
                prefix = 'before-private-reply' if version == 33 else prefix
                prefix = 'before-local-knowledge' if version == 34 else prefix
                prefix = 'before-knowledge-study' if version == 35 else prefix
                prefix = 'before-disposition' if version in (17,18) else prefix
                if version >= 23:
                    from .character_embedding import private_directory
                    private_directory(self.paths.home,self.paths.home/'backups')
                backup = self.paths.home / "backups" / f"{prefix}-{uuid4()}.sqlite3"
                target = sqlite3.connect(backup)
                try:
                    conn.backup(target)
                finally:
                    target.close()
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("PRAGMA legacy_alter_table = ON")
            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if version == 0 and not tables:
                execute_schema(conn, SCHEMA)
                execute_schema(conn, SCHEMA2)
                execute_schema(conn, SCHEMA3)
                execute_schema(conn, SCHEMA4)
                execute_schema(conn, SCHEMA5)
                conn.execute("INSERT INTO users VALUES (?, ?, ?)",
                             (LOCAL_USER_ID, "本地用户", timestamp()))
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version == 1 and set(M1_TABLES).issubset(tables):
                execute_schema(conn, SCHEMA2)
                execute_schema(conn, SCHEMA3)
                execute_schema(conn, SCHEMA4)
                execute_schema(conn, SCHEMA5)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version == 2 and set(M2_TABLES).issubset(tables):
                execute_schema(conn, SCHEMA3)
                execute_schema(conn, SCHEMA4)
                execute_schema(conn, SCHEMA5)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version == 3 and set(M2_TABLES).issubset(tables):
                execute_schema(conn, SCHEMA4)
                execute_schema(conn, SCHEMA5)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version == 4 and set(M3_TABLES).issubset(tables):
                execute_schema(conn, SCHEMA5)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version == 5 and set(M4_TABLES).issubset(tables):
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version == 6 and set(M5_TABLES).issubset(tables):
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version == 7 and set(M6_TABLES).issubset(tables):
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version == 8 and set(M6_TABLES + TABLES8).issubset(tables):
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version == 9 and set(M6_TABLES + TABLES8 + TABLES9).issubset(tables):
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version == 10 and set(M6_TABLES + TABLES8 + TABLES9 + TABLES10).issubset(tables):
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version == 11 and set(TABLES[:-len(TABLES12)-len(TABLES13)]).issubset(tables):
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version == 12 and set(TABLES[:-len(TABLES13)]).issubset(tables):
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif 13 <= version < SCHEMA_VERSION and set(TABLES).issubset(tables):
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            else:
                self._check(conn)
            if version < 6:
                execute_schema(conn, SCHEMA6)
                from .profile import initialize_profiles
                initialize_profiles(conn)
            if version < 7:
                execute_schema(conn, SCHEMA7)
                from .character import initialize_characters
                initialize_characters(conn)
            if version < 8:
                execute_schema(conn, SCHEMA8)
            if version < 9:
                execute_schema(conn, SCHEMA9)
            if version < 10:
                execute_schema(conn, SCHEMA10)
            if version < 11:
                execute_schema(conn, SCHEMA11)
            if version < 12:
                execute_schema(conn, SCHEMA12)
                from .context_policy import initialize
                initialize(conn)
            if version < 13:
                execute_schema(conn, SCHEMA13)
                from .schema_m73 import initialize as initialize_sessions
                initialize_sessions(conn)
            if version < 14:
                from .schema_m74 import migrate
                migrate(conn)
            if version < 15 and SCHEMA_VERSION>=15:
                from .schema_m754r2 import migrate as migrate_selector
                migrate_selector(conn)
            if version < 16 and SCHEMA_VERSION>=16:
                from .persona_management import SCHEMA as management_schema
                execute_schema(conn,management_schema)
            if version < 17 and SCHEMA_VERSION>=17:
                from .schema_rolling_chat import SCHEMA as rolling_schema
                execute_schema(conn,rolling_schema)
            if version < 18 and SCHEMA_VERSION>=18:
                from .schema_bubbles import migrate as migrate_bubbles
                migrate_bubbles(conn)
            if version < 19 and SCHEMA_VERSION>=19:
                from .schema_disposition import migrate as migrate_disposition
                migrate_disposition(conn)
            if version < 20 and SCHEMA_VERSION>=20:
                from .schema_observation import migrate as migrate_observation
                migrate_observation(conn)
            refresh_development_scope_trigger(conn)
            if version < 21 and SCHEMA_VERSION >= 21:
                from .organization_output import migrate as migrate_organization_receipts
                migrate_organization_receipts(conn)
            if version < 22 and SCHEMA_VERSION >= 22:
                from .schema_m8a import migrate as migrate_managed
                migrate_managed(conn)
            if version < 23 and SCHEMA_VERSION >= 23:
                from .schema_m8d import migrate as migrate_automatic
                migrate_automatic(conn)
            if version < 24 and SCHEMA_VERSION >= 24:
                from .schema_m8e import migrate as migrate_application
                migrate_application(conn)
            if version < 25 and SCHEMA_VERSION >= 25:
                from .schema_m9a import migrate as migrate_knowledge
                migrate_knowledge(conn)
            if version < 26 and SCHEMA_VERSION >= 26:
                from .schema_m9a3 import migrate as migrate_public_knowledge
                migrate_public_knowledge(conn)
            if version < 27 and SCHEMA_VERSION >= 27:
                from .schema_m9b_shadow import migrate as migrate_recall_shadow
                migrate_recall_shadow(conn)
            if version < 28 and SCHEMA_VERSION >= 28:
                from .schema_recall_support import migrate as migrate_recall_support
                migrate_recall_support(conn)
            if version < 29 and SCHEMA_VERSION >= 29:
                from .schema_recall_runtime import migrate as migrate_recall_runtime
                migrate_recall_runtime(conn)
            if version < 30 and SCHEMA_VERSION >= 30:
                from .schema_expression_assets import migrate as migrate_expression_assets
                migrate_expression_assets(conn)
            if version < 31 and SCHEMA_VERSION >= 31:
                from .schema_expression_runtime import migrate as migrate_expression_runtime
                migrate_expression_runtime(conn)
            if version < 32 and SCHEMA_VERSION >= 32:
                from .schema_expression_utterance import migrate as migrate_expression_speech
                migrate_expression_speech(conn)
            if version < 33 and SCHEMA_VERSION >= 33:
                from .schema_dialogue import migrate as migrate_dialogue
                migrate_dialogue(conn)
            if version < 34 and SCHEMA_VERSION >= 34:
                from .schema_private_reply import migrate as migrate_private_reply
                migrate_private_reply(conn)
            if SCHEMA_VERSION >= 34:
                from .schema_private_reply import initialize_nodes
                initialize_nodes(conn)
            if version < 35 and SCHEMA_VERSION >= 35:
                from .schema_local_knowledge import migrate as migrate_local_knowledge
                migrate_local_knowledge(conn)
            if version < 36 and SCHEMA_VERSION >= 36:
                from .schema_local_knowledge import migrate_study
                migrate_study(conn)
            if SCHEMA_VERSION >= 36:
                conn.execute("CREATE INDEX IF NOT EXISTS local_knowledge_predecessor ON local_knowledge_facts(previous_id) WHERE previous_id IS NOT NULL")
                conn.execute("CREATE INDEX IF NOT EXISTS local_knowledge_document_facts ON local_knowledge_facts(document_id)")
            if SCHEMA_VERSION >= 33:
                conn.execute("""CREATE TABLE IF NOT EXISTS organization_foreground_execution (
                    id TEXT PRIMARY KEY CHECK(id='singleton'), task_id TEXT REFERENCES organization_tasks(id),
                    owner TEXT, generation INTEGER NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0)""")
                conn.execute("INSERT OR IGNORE INTO organization_foreground_execution(id) VALUES ('singleton')")
                from .call_limits import initialize as initialize_call_limits
                initialize_call_limits(conn)
                conn.execute("CREATE TABLE IF NOT EXISTS memory_scan_positions(name TEXT PRIMARY KEY,position INTEGER NOT NULL)")
                conn.execute("""CREATE INDEX IF NOT EXISTS desktop_paused_backlog ON turns(persona_id,session_id)
                    WHERE channel='desktop' AND error_code IN ('desktop_network_wait','desktop_storage_wait') AND status IN ('queued','running') AND cancelled_at IS NULL""")
                conn.execute("""CREATE INDEX IF NOT EXISTS organization_network_backlog ON organization_tasks(id)
                    WHERE status='queued' AND error_code='organization_network_wait'""")
                conn.execute("CREATE INDEX IF NOT EXISTS memory_scan_session ON sessions(persona_id)")
                conn.execute("""CREATE INDEX IF NOT EXISTS memory_scan_native ON turns(session_id)
                    WHERE status='completed' AND source_key NOT GLOB 'imported.*'""")
                conn.execute("""CREATE INDEX IF NOT EXISTS memory_scan_imported ON turns(session_id)
                    WHERE status='completed' AND source_key GLOB 'imported.*'""")
                conn.execute("CREATE INDEX IF NOT EXISTS memory_scan_pending ON memory_coverage(persona_id,disposition,turn_id)")
                conn.execute("""CREATE INDEX IF NOT EXISTS desktop_native_turn_page ON turns(persona_id,session_id)
                             WHERE channel='desktop' AND source_key NOT GLOB 'imported.*'""")
                conn.execute("CREATE INDEX IF NOT EXISTS non_desktop_session ON turns(session_id) WHERE channel!='desktop'")
                conn.execute("""CREATE INDEX IF NOT EXISTS structured_turns_by_session ON turns(persona_id,session_id,channel)
                                WHERE reply_mode_snapshot='natural_structured'""")
                conn.execute("CREATE INDEX IF NOT EXISTS context_uses_turn ON context_uses(turn_id)")
                from .portable_memory import protect_history_remaps
                protect_history_remaps(conn)
            if conn.execute("PRAGMA foreign_key_check").fetchone():
                raise RuntimeErrorBase("Migration foreign key verification failed")
            self._check(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def status(self) -> dict:
        from .schema_recall_support import TABLES as recall_support_tables
        with self.connection() as conn:
            return {
                "database": str(self.path), "schema_version": SCHEMA_VERSION,
                "current_persona": self.current(conn, required=False),
                "counts": {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES + (TABLES19 if SCHEMA_VERSION>=19 else ()) + (TABLES20 if SCHEMA_VERSION>=20 else ()) + (recall_support_tables if SCHEMA_VERSION>=28 else ()) + ('character_selection_attempts','quota_reservations','persona_lifecycle','persona_operations','reply_display_messages')},
            }

    @staticmethod
    def current(conn: sqlite3.Connection, *, required: bool = True) -> str | None:
        row = conn.execute("SELECT value FROM settings WHERE key='current_persona'").fetchone()
        if not row and required:
            raise RuntimeErrorBase("No persona selected; run persona use ID")
        return row[0] if row else None
