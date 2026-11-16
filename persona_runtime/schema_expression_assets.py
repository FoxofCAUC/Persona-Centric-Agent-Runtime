"""Schema 30: scoped expression assets, immutable revisions and bounded learning."""
TABLES = ('expression_asset_policies', 'expression_asset_operations', 'expression_asset_sources',
          'expression_asset_source_versions', 'expression_assets', 'expression_asset_versions',
          'expression_asset_supports', 'expression_asset_terms', 'expression_asset_vectors',
          'expression_candidate_snapshots', 'expression_learning_jobs', 'expression_asset_proposals',
          'expression_asset_observations', 'expression_learning_scan')

SCHEMA = '''
CREATE TABLE expression_asset_policies (
 persona_id TEXT PRIMARY KEY REFERENCES personas(id), revision INTEGER NOT NULL,
 data_json TEXT NOT NULL CHECK(json_valid(data_json))
);
CREATE TABLE expression_asset_operations (
 operation_id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id),
 action TEXT NOT NULL, request_hash TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE expression_asset_sources (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id), scope_key TEXT NOT NULL,
 scope_json TEXT NOT NULL, revision INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('active','revoked')),
 UNIQUE(id,persona_id,scope_key)
);
CREATE TABLE expression_asset_source_versions (
 source_id TEXT NOT NULL REFERENCES expression_asset_sources(id), revision INTEGER NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('authored_examples','public_human','user_event')),
 ref_json TEXT NOT NULL, body TEXT NOT NULL, content_hash TEXT NOT NULL,
 permissions_json TEXT NOT NULL, attribution TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(source_id,revision)
);
CREATE TABLE expression_assets (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id), scope_key TEXT NOT NULL,
 revision INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('active','disabled')),
 UNIQUE(id,persona_id,scope_key)
);
CREATE TABLE expression_asset_versions (
 asset_id TEXT NOT NULL REFERENCES expression_assets(id), revision INTEGER NOT NULL,
 situation TEXT NOT NULL, style TEXT NOT NULL, keywords_json TEXT NOT NULL,
 content_hash TEXT NOT NULL, restored_from INTEGER, created_at TEXT NOT NULL,
 PRIMARY KEY(asset_id,revision)
);
CREATE TABLE expression_asset_supports (
 asset_id TEXT NOT NULL, asset_revision INTEGER NOT NULL, source_id TEXT NOT NULL,
 source_revision INTEGER NOT NULL,
 PRIMARY KEY(asset_id,asset_revision,source_id),
 FOREIGN KEY(asset_id,asset_revision) REFERENCES expression_asset_versions(asset_id,revision),
 FOREIGN KEY(source_id,source_revision) REFERENCES expression_asset_source_versions(source_id,revision)
);
CREATE TABLE expression_asset_terms (
 term TEXT NOT NULL, asset_id TEXT NOT NULL REFERENCES expression_assets(id), revision INTEGER NOT NULL,
 PRIMARY KEY(term,asset_id,revision)
);
CREATE TABLE expression_asset_vectors (
 asset_id TEXT NOT NULL, revision INTEGER NOT NULL, model_fingerprint TEXT NOT NULL,
 content_hash TEXT NOT NULL, vector_json TEXT NOT NULL,
 PRIMARY KEY(asset_id,revision,model_fingerprint),
 FOREIGN KEY(asset_id,revision) REFERENCES expression_asset_versions(asset_id,revision)
);
CREATE TABLE expression_candidate_snapshots (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id), scope_key TEXT NOT NULL,
 data_json TEXT NOT NULL, data_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE expression_learning_jobs (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id), scope_key TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('prepared','dispatched','completed','failed','unknown','stale','cancelled')),
 source_manifest_json TEXT NOT NULL, request_json TEXT NOT NULL, request_hash TEXT NOT NULL,
 model_json TEXT NOT NULL, attempt_id TEXT UNIQUE, started_epoch REAL,
 raw_response BLOB, raw_response_hash TEXT, transport_json TEXT, usage_json TEXT,
 result_json TEXT, error_code TEXT, created_at TEXT NOT NULL
);
CREATE TABLE expression_asset_proposals (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES expression_learning_jobs(id),
 persona_id TEXT NOT NULL REFERENCES personas(id), scope_key TEXT NOT NULL,
 data_json TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','accepted','rejected')),
 UNIQUE(job_id,data_json)
);
CREATE TABLE expression_asset_observations (
 asset_id TEXT NOT NULL REFERENCES expression_assets(id), origin_id TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(asset_id,origin_id)
);
CREATE TABLE expression_learning_scan (
 persona_id TEXT NOT NULL REFERENCES personas(id), session_id TEXT NOT NULL REFERENCES sessions(id),
 last_seq INTEGER NOT NULL, PRIMARY KEY(persona_id,session_id)
);
CREATE INDEX expression_assets_scope ON expression_assets(persona_id,scope_key,state);
CREATE INDEX expression_learning_due ON expression_learning_jobs(state,created_at);
CREATE INDEX expression_support_source ON expression_asset_supports(source_id,source_revision);
'''


def migrate(db):
    from .storage import execute_schema
    from .models import RuntimeErrorBase
    present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if set(TABLES) & present:
        if not set(TABLES).issubset(present):raise RuntimeErrorBase('expression_schema_incomplete')
    else:
        execute_schema(db, SCHEMA)
    for table in ('expression_asset_source_versions', 'expression_asset_versions', 'expression_asset_supports'):
        for action in ('UPDATE', 'DELETE'):
            execute_schema(db, f'''CREATE TRIGGER IF NOT EXISTS {table}_immutable_{action.lower()} BEFORE {action} ON {table}
            BEGIN SELECT RAISE(ABORT,'expression revisions are append only'); END;''')
    execute_schema(db, '''CREATE TRIGGER IF NOT EXISTS expression_support_scope BEFORE INSERT ON expression_asset_supports BEGIN
      SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM expression_assets a JOIN expression_asset_sources s
        ON a.persona_id=s.persona_id AND a.scope_key=s.scope_key
        WHERE a.id=NEW.asset_id AND s.id=NEW.source_id AND s.revision=NEW.source_revision AND s.state='active')
        THEN RAISE(ABORT,'expression support scope mismatch') END;
    END;''')
    execute_schema(db, '''CREATE TRIGGER IF NOT EXISTS expression_learning_contract_immutable BEFORE UPDATE ON expression_learning_jobs
    WHEN OLD.attempt_id IS NOT NULL AND (NEW.attempt_id IS NOT OLD.attempt_id OR NEW.persona_id IS NOT OLD.persona_id
      OR NEW.scope_key IS NOT OLD.scope_key OR NEW.request_json IS NOT OLD.request_json OR NEW.request_hash IS NOT OLD.request_hash
      OR NEW.source_manifest_json IS NOT OLD.source_manifest_json OR NEW.model_json IS NOT OLD.model_json)
    BEGIN SELECT RAISE(ABORT,'expression learning contract is immutable'); END;
    CREATE TRIGGER IF NOT EXISTS expression_learning_receipt_immutable BEFORE UPDATE ON expression_learning_jobs
    WHEN OLD.raw_response IS NOT NULL AND (NEW.raw_response IS NOT OLD.raw_response OR NEW.raw_response_hash IS NOT OLD.raw_response_hash)
    BEGIN SELECT RAISE(ABORT,'expression learning receipt is immutable'); END;
    CREATE TRIGGER IF NOT EXISTS expression_learning_immutable_delete BEFORE DELETE ON expression_learning_jobs
    BEGIN SELECT RAISE(ABORT,'expression learning history is append only'); END;
    CREATE TRIGGER IF NOT EXISTS expression_proposal_immutable BEFORE UPDATE ON expression_asset_proposals
    WHEN NEW.data_json IS NOT OLD.data_json OR NEW.job_id IS NOT OLD.job_id OR NEW.persona_id IS NOT OLD.persona_id OR NEW.scope_key IS NOT OLD.scope_key
    BEGIN SELECT RAISE(ABORT,'expression proposal evidence is immutable'); END;
    ''')
    for table in ('expression_assets', 'expression_asset_sources'):
        predicate = ('asset_id=NEW.id' if table == 'expression_assets' else
                     'asset_id IN (SELECT asset_id FROM expression_asset_supports WHERE source_id=NEW.id)')
        execute_schema(db, f'''CREATE TRIGGER IF NOT EXISTS {table}_invalidate AFTER UPDATE ON {table} BEGIN
          DELETE FROM expression_asset_terms WHERE {predicate};
          DELETE FROM expression_asset_vectors WHERE {predicate}; END;''')
    execute_schema(db, '''CREATE TRIGGER IF NOT EXISTS expression_event_withdrawal AFTER INSERT ON source_exclusions BEGIN
      DELETE FROM expression_asset_terms WHERE asset_id IN (
        SELECT s.asset_id FROM expression_asset_supports s JOIN expression_asset_source_versions v
        ON v.source_id=s.source_id AND v.revision=s.source_revision
        WHERE v.kind='user_event' AND json_extract(v.ref_json,'$.event_id')=NEW.event_id);
      DELETE FROM expression_asset_vectors WHERE asset_id IN (
        SELECT s.asset_id FROM expression_asset_supports s JOIN expression_asset_source_versions v
        ON v.source_id=s.source_id AND v.revision=s.source_revision
        WHERE v.kind='user_event' AND json_extract(v.ref_json,'$.event_id')=NEW.event_id);
    END;''')
