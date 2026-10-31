"""Schema 19: author projections and optional, non-prompt Shadow audit."""
TABLES19=('disposition_versions','disposition_heads','disposition_operations',
          'disposition_compile_attempts','disposition_revocations','disposition_decisions')


def migrate(conn):
    from .storage import execute_schema
    execute_schema(conn, '''
CREATE TABLE disposition_compile_attempts (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id), user_id TEXT NOT NULL REFERENCES users(id),
 operation_id TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('running','completed','failed','interrupted')),
 started_epoch REAL NOT NULL, finished_epoch REAL, model_snapshot TEXT NOT NULL,
 sources_json TEXT NOT NULL, input_hash TEXT NOT NULL, result_version_id TEXT,
 error_code TEXT, usage_json TEXT, diagnostics_json TEXT, created_at TEXT NOT NULL
);
CREATE INDEX disposition_compile_time ON disposition_compile_attempts(started_epoch);
CREATE TABLE disposition_versions (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id), user_id TEXT NOT NULL REFERENCES users(id),
 revision INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('draft','confirmed')),
 document_json TEXT NOT NULL, parent_id TEXT REFERENCES disposition_versions(id),
 restored_from TEXT REFERENCES disposition_versions(id), compile_attempt_id TEXT REFERENCES disposition_compile_attempts(id),
 created_at TEXT NOT NULL, UNIQUE(persona_id,revision)
);
CREATE TABLE disposition_heads (
 persona_id TEXT PRIMARY KEY REFERENCES personas(id), user_id TEXT NOT NULL REFERENCES users(id),
 draft_id TEXT REFERENCES disposition_versions(id), active_id TEXT REFERENCES disposition_versions(id), revision INTEGER NOT NULL
);
CREATE TABLE disposition_operations (
 operation_id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id), fingerprint TEXT NOT NULL,
 result_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE disposition_revocations (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id), user_id TEXT NOT NULL REFERENCES users(id),
 version_id TEXT NOT NULL REFERENCES disposition_versions(id), operation_id TEXT NOT NULL UNIQUE,
 created_at TEXT NOT NULL
);
CREATE TABLE disposition_decisions (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id), user_id TEXT NOT NULL REFERENCES users(id),
 turn_id TEXT NOT NULL REFERENCES turns(id), execution_revision INTEGER NOT NULL,
 attempt_id TEXT REFERENCES attempts(id), context_id TEXT REFERENCES context_manifests(id) DEFERRABLE INITIALLY DEFERRED,
 version_id TEXT REFERENCES disposition_versions(id), status TEXT NOT NULL, record_json TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(turn_id,execution_revision),
 FOREIGN KEY(context_id,persona_id,user_id,turn_id) REFERENCES context_manifests(id,persona_id,user_id,turn_id) DEFERRABLE INITIALLY DEFERRED
);
''')
    # Preserve existing manifest rows/IDs and all old CHECK/trigger semantics.
    sql=conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='context_manifests'").fetchone()[0]
    auxiliaries=list(conn.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name='context_manifests' AND type IN ('index','trigger') AND sql IS NOT NULL"))
    for kind,name,_ in auxiliaries:
        conn.execute(f'DROP {kind} "{name}"')
    # Decisions have no rows during migration; detach their empty FK target
    # by creating the new manifest before renaming the old one via a temporary name.
    new=sql.replace('CREATE TABLE context_manifests','CREATE TABLE context_manifests_new',1)
    new=new.replace("'character_selection'","'character_selection','disposition_compilation'",1)
    new=new.replace('manifest_json TEXT NOT NULL','compile_attempt_id TEXT UNIQUE REFERENCES disposition_compile_attempts(id), manifest_json TEXT NOT NULL',1)
    # Existing top-level CHECK is retained; allow a compilation-specific branch.
    marker="OR (task_type='character_selection'"
    idx=new.index(marker)
    new=new[:idx]+"OR (task_type='disposition_compilation' AND compile_attempt_id IS NOT NULL AND turn_id IS NULL AND attempt_id IS NULL AND organization_task_id IS NULL AND organization_attempt_id IS NULL AND selection_attempt_id IS NULL)\n "+new[idx:]
    conn.execute(new)
    cols=[r[1] for r in conn.execute('PRAGMA table_info(context_manifests)')]
    columns=','.join(cols)
    conn.execute(f'INSERT INTO context_manifests_new ({columns}) SELECT {columns} FROM context_manifests')
    conn.execute('DROP TABLE context_manifests')
    conn.execute('ALTER TABLE context_manifests_new RENAME TO context_manifests')
    for _,_,sql in auxiliaries:conn.execute(sql)
    execute_schema(conn, '''
CREATE UNIQUE INDEX disposition_manifest_owner ON context_manifests(id,persona_id,user_id,turn_id);
CREATE TRIGGER disposition_compile_scope BEFORE INSERT ON disposition_compile_attempts BEGIN
 SELECT CASE WHEN NEW.user_id!='00000000-0000-4000-8000-000000000001'
 THEN RAISE(ABORT,'disposition compile scope') END;
END;
CREATE TRIGGER disposition_manifest_scope BEFORE INSERT ON context_manifests BEGIN
 SELECT CASE WHEN NEW.task_type='disposition_compilation' AND NOT EXISTS(
 SELECT 1 FROM disposition_compile_attempts a WHERE a.id=NEW.compile_attempt_id AND a.persona_id=NEW.persona_id AND a.user_id=NEW.user_id)
 THEN RAISE(ABORT,'disposition manifest scope') END;
 SELECT CASE WHEN NEW.compile_attempt_id IS NOT NULL AND NEW.task_type!='disposition_compilation'
 THEN RAISE(ABORT,'disposition manifest purpose') END;
END;
CREATE TRIGGER disposition_decision_scope BEFORE INSERT ON disposition_decisions BEGIN
 SELECT CASE WHEN NEW.user_id!='00000000-0000-4000-8000-000000000001' OR NOT EXISTS(SELECT 1 FROM turns t WHERE t.id=NEW.turn_id AND t.persona_id=NEW.persona_id AND t.execution_revision=NEW.execution_revision)
 OR (NEW.version_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM disposition_versions v WHERE v.id=NEW.version_id AND v.persona_id=NEW.persona_id AND v.user_id=NEW.user_id))
 OR (NEW.attempt_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM attempts a WHERE a.id=NEW.attempt_id AND a.turn_id=NEW.turn_id))
 THEN RAISE(ABORT,'disposition decision scope') END;
END;
CREATE TRIGGER disposition_version_scope BEFORE INSERT ON disposition_versions BEGIN
 SELECT CASE WHEN NEW.user_id!='00000000-0000-4000-8000-000000000001'
 OR (NEW.parent_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM disposition_versions v WHERE v.id=NEW.parent_id AND v.persona_id=NEW.persona_id))
 OR (NEW.restored_from IS NOT NULL AND NOT EXISTS(SELECT 1 FROM disposition_versions v WHERE v.id=NEW.restored_from AND v.persona_id=NEW.persona_id))
 OR (NEW.compile_attempt_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM disposition_compile_attempts a WHERE a.id=NEW.compile_attempt_id AND a.persona_id=NEW.persona_id))
 THEN RAISE(ABORT,'disposition version scope') END;
END;
CREATE TRIGGER disposition_revoke_scope BEFORE INSERT ON disposition_revocations BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM disposition_versions v WHERE v.id=NEW.version_id AND v.persona_id=NEW.persona_id AND v.user_id=NEW.user_id)
 THEN RAISE(ABORT,'disposition revocation scope') END;
END;
''')
    for verb in ('INSERT','UPDATE'):
        conn.execute(f'''CREATE TRIGGER disposition_head_{verb.lower()} BEFORE {verb} ON disposition_heads BEGIN
 SELECT CASE WHEN NEW.user_id!='00000000-0000-4000-8000-000000000001'
 OR (NEW.active_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM disposition_versions v WHERE v.id=NEW.active_id AND v.persona_id=NEW.persona_id AND v.kind='confirmed'))
 OR (NEW.draft_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM disposition_versions v WHERE v.id=NEW.draft_id AND v.persona_id=NEW.persona_id AND v.kind='draft'))
 THEN RAISE(ABORT,'disposition head scope') END; END''')
    for table in ('disposition_versions','disposition_revocations','disposition_decisions'):
        for verb in ('UPDATE','DELETE'):
            conn.execute(f"CREATE TRIGGER {table}_{verb.lower()} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'disposition append only'); END")
