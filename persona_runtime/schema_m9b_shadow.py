"""Schema-27 migration compatibility only; retired observation logic is absent."""
from .schema_m9b import SCHEMA as PRIORITY_SCHEMA, TABLES as PRIORITY_TABLES

TABLES = PRIORITY_TABLES + ('recall_shadow_refs', 'recall_shadow_runs', 'recall_shadow_operations')
SCHEMA = PRIORITY_SCHEMA.replace('persona_id TEXT NOT NULL,',
    'persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,') + """
CREATE TABLE recall_shadow_refs (
 unit_id TEXT PRIMARY KEY REFERENCES recall_priority_units(id) ON DELETE CASCADE,
 persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
 user_id TEXT NOT NULL REFERENCES users(id),
 kind TEXT NOT NULL CHECK(kind IN ('memory','processed','claim')),
 authority_json TEXT NOT NULL CHECK(json_valid(authority_json)),
 guard_hash TEXT NOT NULL,
 first_seen_epoch REAL NOT NULL,
 last_seen_epoch REAL NOT NULL
);
CREATE INDEX recall_shadow_ref_scope ON recall_shadow_refs(persona_id,user_id);
CREATE TABLE recall_shadow_runs (
 id TEXT PRIMARY KEY,
 persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
 user_id TEXT NOT NULL REFERENCES users(id),
 turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
 execution_revision INTEGER NOT NULL,
 mode TEXT NOT NULL CHECK(mode='shadow'),
 observation_epoch REAL NOT NULL,
 input_hash TEXT NOT NULL,
 audit_json TEXT NOT NULL CHECK(json_valid(audit_json)),
 created_at TEXT NOT NULL
);
CREATE INDEX recall_shadow_run_scope ON recall_shadow_runs(persona_id,turn_id,execution_revision);
CREATE TABLE recall_shadow_operations (
 id TEXT PRIMARY KEY,
 persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
 request_json TEXT NOT NULL CHECK(json_valid(request_json)),
 result_json TEXT NOT NULL CHECK(json_valid(result_json)),
 created_at TEXT NOT NULL
);

"""

def migrate(conn):
    from .storage import execute_schema
    execute_schema(conn, SCHEMA)
