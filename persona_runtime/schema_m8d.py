"""Automatic memory business records; execution remains organization_tasks."""
TABLES=('memory_defaults','memory_operations','memory_imports','memory_scopes','memory_batches','memory_coverage','memory_decisions','memory_facts','memory_fragments')
SCHEMA='''
CREATE TABLE memory_defaults (persona_id TEXT PRIMARY KEY REFERENCES personas(id));
CREATE TABLE memory_operations (id TEXT PRIMARY KEY,persona_id TEXT NOT NULL REFERENCES personas(id),user_id TEXT NOT NULL,fingerprint TEXT NOT NULL,result_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE memory_imports (id TEXT PRIMARY KEY,persona_id TEXT NOT NULL REFERENCES personas(id),user_id TEXT NOT NULL,format_version INTEGER NOT NULL,data_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE memory_scopes (scope_key TEXT PRIMARY KEY,persona_id TEXT NOT NULL REFERENCES personas(id),user_id TEXT NOT NULL,scope_json TEXT NOT NULL,enabled_epoch REAL NOT NULL,backfill_epoch REAL NOT NULL DEFAULT 0);
CREATE TABLE memory_batches (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id), user_id TEXT NOT NULL,
 session_id TEXT NOT NULL REFERENCES sessions(id), scope_json TEXT NOT NULL,
 work_key TEXT NOT NULL UNIQUE, purpose TEXT NOT NULL, state TEXT NOT NULL,
 extract_task_id TEXT REFERENCES organization_tasks(id), verify_task_id TEXT REFERENCES organization_tasks(id),
 data_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE memory_coverage (
 persona_id TEXT NOT NULL REFERENCES personas(id), user_id TEXT NOT NULL,
 turn_id TEXT PRIMARY KEY REFERENCES turns(id), scope_json TEXT NOT NULL, source_hash TEXT NOT NULL,
 batch_id TEXT REFERENCES memory_batches(id), disposition TEXT NOT NULL,
 summary_state TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE memory_decisions (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id), user_id TEXT NOT NULL,
 batch_id TEXT NOT NULL REFERENCES memory_batches(id), item_id TEXT NOT NULL,
 kind TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL, data_json TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(batch_id,item_id)
);
CREATE TABLE memory_facts (
 memory_id TEXT PRIMARY KEY REFERENCES memories(id), persona_id TEXT NOT NULL REFERENCES personas(id),
 user_id TEXT NOT NULL, scope_json TEXT NOT NULL, subject TEXT NOT NULL, attribute TEXT NOT NULL,
 valid_from TEXT, valid_until TEXT, batch_id TEXT REFERENCES memory_batches(id), data_json TEXT NOT NULL
);
CREATE TABLE memory_fragments (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id), user_id TEXT NOT NULL,
 session_id TEXT NOT NULL REFERENCES sessions(id), batch_id TEXT NOT NULL REFERENCES memory_batches(id),
 previous_id TEXT REFERENCES memory_fragments(id), state TEXT NOT NULL,
 data_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX memory_batch_scope ON memory_batches(persona_id,state);
CREATE INDEX memory_fragment_scope ON memory_fragments(persona_id,session_id,state);
'''


def migrate(conn):
    from .storage import execute_schema
    # Future versions may be active records while their disjoint valid-time
    # intervals select exactly one current fact. Publication is serialized and
    # guards interval overlap; legacy writers still supersede their predecessor.
    conn.execute('DROP INDEX IF EXISTS one_active_memory')
    execute_schema(conn,SCHEMA)
