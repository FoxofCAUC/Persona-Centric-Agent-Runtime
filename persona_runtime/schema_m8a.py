"""Additive task contracts. Existing organization task IDs remain authoritative."""
TABLES = ('organization_policies','organization_contracts','organization_submissions',
          'organization_reservations','organization_execution')
SCHEMA = """
CREATE TABLE organization_policies (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id), revision INTEGER NOT NULL,
 data_json TEXT NOT NULL, operation_id TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(persona_id,revision)
);
CREATE TABLE organization_contracts (
 task_id TEXT PRIMARY KEY REFERENCES organization_tasks(id), work_key TEXT NOT NULL,
 scope_json TEXT NOT NULL, policy_id TEXT REFERENCES organization_policies(id), data_json TEXT NOT NULL
);
CREATE INDEX organization_work ON organization_contracts(work_key);
CREATE TABLE organization_submissions (
 request_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES organization_tasks(id),
 request_hash TEXT NOT NULL, trigger_kind TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE organization_reservations (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES organization_tasks(id),
 attempt_id TEXT UNIQUE REFERENCES organization_attempts(id), persona_id TEXT NOT NULL REFERENCES personas(id),
 state TEXT NOT NULL CHECK(state IN ('active','consumed','released')),
 created_epoch REAL NOT NULL, expires_epoch REAL NOT NULL
);
CREATE TABLE organization_execution (
 id TEXT PRIMARY KEY CHECK(id='singleton'), task_id TEXT REFERENCES organization_tasks(id),
 owner TEXT, generation INTEGER NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0
);
INSERT INTO organization_execution(id) VALUES ('singleton');
"""


def migrate(conn):
    from .storage import execute_schema
    execute_schema(conn,SCHEMA)
