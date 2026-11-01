"""Schema 20: separately audited, bounded task-gap observations."""
TABLES20=('situation_observation_attempts',)


def migrate(conn):
    from .storage import execute_schema
    execute_schema(conn, '''
ALTER TABLE turns ADD COLUMN observation_model_snapshot TEXT;
ALTER TABLE input_batches ADD COLUMN observation_model_snapshot TEXT;
CREATE TABLE situation_observation_attempts (
 id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns(id), execution_revision INTEGER NOT NULL,
 persona_id TEXT NOT NULL REFERENCES personas(id), user_id TEXT NOT NULL REFERENCES users(id),
 status TEXT NOT NULL CHECK(status IN ('running','completed','failed','interrupted')),
 started_epoch REAL NOT NULL, finished_epoch REAL, model_snapshot TEXT NOT NULL,
 contract_version TEXT NOT NULL, input_hash TEXT NOT NULL, scope_json TEXT NOT NULL,
 evidence_json TEXT NOT NULL, result_json TEXT, error_code TEXT, usage_json TEXT, created_at TEXT NOT NULL,
 UNIQUE(turn_id,execution_revision)
);
CREATE INDEX observation_calls_time ON situation_observation_attempts(started_epoch);
CREATE TRIGGER observation_scope BEFORE INSERT ON situation_observation_attempts BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM turns t WHERE t.id=NEW.turn_id AND t.persona_id=NEW.persona_id
 AND NEW.user_id='00000000-0000-4000-8000-000000000001' AND t.execution_revision=NEW.execution_revision)
 THEN RAISE(ABORT,'observation scope mismatch') END;
END;
''')
    sql=conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='context_manifests'").fetchone()[0]
    auxiliary=list(conn.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name='context_manifests' AND type IN ('index','trigger') AND sql IS NOT NULL"))
    for kind,name,_ in auxiliary:conn.execute(f'DROP {kind} "{name}"')
    new=sql.replace('CREATE TABLE context_manifests','CREATE TABLE context_manifests_new',1)
    # SQLite may quote the table identifier after an earlier rebuild.
    new=new.replace('CREATE TABLE "context_manifests"','CREATE TABLE context_manifests_new',1)
    new=new.replace("'disposition_compilation'","'disposition_compilation','situation_observation'",1)
    new=new.replace('manifest_json TEXT NOT NULL','observation_attempt_id TEXT UNIQUE REFERENCES situation_observation_attempts(id), manifest_json TEXT NOT NULL',1)
    marker="OR (task_type='disposition_compilation'"
    idx=new.index(marker)
    new=new[:idx]+"OR (task_type='situation_observation' AND observation_attempt_id IS NOT NULL AND turn_id IS NOT NULL AND attempt_id IS NULL AND organization_task_id IS NULL AND organization_attempt_id IS NULL AND selection_attempt_id IS NULL AND compile_attempt_id IS NULL)\n "+new[idx:]
    conn.execute(new)
    columns=','.join(r[1] for r in conn.execute('PRAGMA table_info(context_manifests)'))
    conn.execute(f'INSERT INTO context_manifests_new ({columns}) SELECT {columns} FROM context_manifests')
    conn.execute('DROP TABLE context_manifests')
    conn.execute('ALTER TABLE context_manifests_new RENAME TO context_manifests')
    for _,_,statement in auxiliary:conn.execute(statement)
    execute_schema(conn, '''
CREATE TRIGGER observation_manifest_scope BEFORE INSERT ON context_manifests BEGIN
 SELECT CASE WHEN NEW.task_type='situation_observation' AND NOT EXISTS(
 SELECT 1 FROM situation_observation_attempts a WHERE a.id=NEW.observation_attempt_id AND a.persona_id=NEW.persona_id AND a.user_id=NEW.user_id AND a.turn_id=NEW.turn_id)
 THEN RAISE(ABORT,'observation manifest mismatch') END;
 SELECT CASE WHEN NEW.observation_attempt_id IS NOT NULL AND NEW.task_type!='situation_observation'
 THEN RAISE(ABORT,'observation manifest purpose') END;
END;
''')
