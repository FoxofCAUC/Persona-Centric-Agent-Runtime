"""Schema 15: separately audited selector calls and reply quota reservations."""
TABLES15 = ('character_selection_attempts','quota_reservations')


def migrate(conn):
    from .storage import execute_schema
    from .schema_m72 import SCHEMA12
    execute_schema(conn, '''
ALTER TABLE turns ADD COLUMN execution_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE turns ADD COLUMN selector_model_snapshot TEXT;
ALTER TABLE input_batches ADD COLUMN selector_model_snapshot TEXT;
CREATE TABLE character_selection_attempts (
 id TEXT PRIMARY KEY, turn_id TEXT REFERENCES turns(id), execution_revision INTEGER,
 persona_id TEXT NOT NULL REFERENCES personas(id), user_id TEXT NOT NULL REFERENCES users(id),
 purpose TEXT NOT NULL CHECK(purpose IN ('conversation','provider_probe')), operation_id TEXT UNIQUE,
 status TEXT NOT NULL CHECK(status IN ('running','completed','failed','interrupted')),
 started_epoch REAL NOT NULL, finished_epoch REAL, model_snapshot TEXT NOT NULL,
 input_hash TEXT NOT NULL, candidate_hash TEXT NOT NULL, scope_json TEXT NOT NULL, selected_json TEXT,
 error_code TEXT, usage_json TEXT, created_at TEXT NOT NULL,
 UNIQUE(turn_id,execution_revision),
 CHECK((purpose='conversation' AND turn_id IS NOT NULL AND execution_revision IS NOT NULL)
 OR (purpose='provider_probe' AND turn_id IS NULL AND operation_id IS NOT NULL))
);
CREATE TABLE quota_reservations (
 id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns(id), execution_revision INTEGER NOT NULL,
 persona_id TEXT NOT NULL REFERENCES personas(id), user_id TEXT NOT NULL REFERENCES users(id),
 state TEXT NOT NULL CHECK(state IN ('active','consumed','released','expired')),
 created_epoch REAL NOT NULL, expires_epoch REAL NOT NULL, UNIQUE(turn_id,execution_revision)
);
CREATE INDEX selector_calls_time ON character_selection_attempts(started_epoch);
CREATE TRIGGER selector_attempt_scope BEFORE INSERT ON character_selection_attempts BEGIN
 SELECT CASE WHEN NEW.user_id!='00000000-0000-4000-8000-000000000001'
 OR (NEW.turn_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM turns t WHERE t.id=NEW.turn_id AND t.persona_id=NEW.persona_id AND t.execution_revision=NEW.execution_revision))
 THEN RAISE(ABORT,'selector scope mismatch') END;
END;
CREATE TRIGGER quota_reservation_scope BEFORE INSERT ON quota_reservations BEGIN
 SELECT CASE WHEN NEW.user_id!='00000000-0000-4000-8000-000000000001'
 OR NOT EXISTS(SELECT 1 FROM turns t WHERE t.id=NEW.turn_id AND t.persona_id=NEW.persona_id AND t.execution_revision=NEW.execution_revision)
 THEN RAISE(ABORT,'reservation scope mismatch') END;
END;
DROP TRIGGER context_manifest_scope;
DROP TRIGGER context_manifest_immutable_update;
DROP TRIGGER context_manifest_immutable_delete;
DROP INDEX context_manifests_turn;
DROP INDEX context_manifests_task;
ALTER TABLE context_manifests RENAME TO context_manifests_v14;
''')
    schema=SCHEMA12[SCHEMA12.index('CREATE TABLE context_manifests'):]
    schema=schema.replace("'director','planning'", "'director','planning','character_selection'")
    schema=schema.replace('manifest_json TEXT NOT NULL', 'selection_attempt_id TEXT UNIQUE REFERENCES character_selection_attempts(id), manifest_json TEXT NOT NULL')
    schema=schema.replace("AND task_type='dialogue')", "AND task_type='dialogue' AND selection_attempt_id IS NULL)")
    schema=schema.replace("AND task_type='reflection'))", "AND task_type='reflection' AND selection_attempt_id IS NULL)\n OR (task_type='character_selection' AND selection_attempt_id IS NOT NULL AND attempt_id IS NULL AND organization_task_id IS NULL AND organization_attempt_id IS NULL))")
    marker=" SELECT CASE WHEN NEW.organization_task_id IS NOT NULL"
    check=" SELECT CASE WHEN NEW.selection_attempt_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM character_selection_attempts a WHERE a.id=NEW.selection_attempt_id AND a.persona_id=NEW.persona_id AND a.user_id=NEW.user_id AND a.turn_id IS NEW.turn_id) THEN RAISE(ABORT,'selector manifest mismatch') END;\n"
    schema=schema.replace(marker,check+marker)
    execute_schema(conn,schema)
    cols='id,persona_id,user_id,task_type,turn_id,organization_task_id,attempt_id,organization_attempt_id,manifest_json,created_at'
    conn.execute(f'INSERT INTO context_manifests ({cols}) SELECT {cols} FROM context_manifests_v14')
    conn.execute('DROP TABLE context_manifests_v14')
