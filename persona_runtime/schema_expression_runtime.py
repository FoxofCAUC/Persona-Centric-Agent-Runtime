"""Schema 31: durable primary receipts, expression tasks and presentation authority."""
TABLES = ('expression_runtime_policies', 'expression_runtime_turns', 'expression_main_receipts',
          'expression_runs', 'expression_publications', 'expression_publication_versions',
          'expression_presentation_items', 'expression_utterances','expression_presentation_receipts')
SCHEMA = '''
CREATE TABLE expression_runtime_policies (
 persona_id TEXT PRIMARY KEY REFERENCES personas(id), revision INTEGER NOT NULL, data_json TEXT NOT NULL
);
CREATE TABLE expression_runtime_turns (
 turn_id TEXT PRIMARY KEY REFERENCES turns(id), policy_json TEXT NOT NULL, cancelled_at TEXT, closed_at TEXT
);
CREATE TABLE expression_main_receipts (
 id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns(id), execution_revision INTEGER NOT NULL,
 logical_attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id), physical_attempt_id TEXT NOT NULL,
 valid INTEGER NOT NULL, raw_text TEXT NOT NULL, raw_hash TEXT NOT NULL, body TEXT, body_hash TEXT,
 batch_json TEXT, context_json TEXT NOT NULL, usage_json TEXT, created_at TEXT NOT NULL,
 UNIQUE(turn_id,execution_revision)
);
CREATE TABLE expression_runs (
 id TEXT PRIMARY KEY, turn_id TEXT NOT NULL UNIQUE REFERENCES turns(id), draft_id TEXT NOT NULL UNIQUE REFERENCES expression_main_receipts(id),
 state TEXT NOT NULL CHECK(state IN ('prepared','dispatched','completed','failed','unknown','stale','cancelled','skipped')),
 policy_json TEXT NOT NULL, input_json TEXT, request_json TEXT, request_hash TEXT, model_json TEXT,
 candidate_snapshot_id TEXT REFERENCES expression_candidate_snapshots(id), attempt_id TEXT UNIQUE, started_epoch REAL,
 raw_response BLOB, raw_response_hash TEXT, transport_json TEXT, usage_json TEXT, result_json TEXT, error_code TEXT
);
CREATE TABLE expression_publications (
 id TEXT PRIMARY KEY, turn_id TEXT NOT NULL UNIQUE REFERENCES turns(id), revision INTEGER NOT NULL,
 producer_kind TEXT NOT NULL, producer_id TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','partial','unknown','complete','closed')),
 reason TEXT, canonical_event_id TEXT REFERENCES events(id), created_at TEXT NOT NULL
);
CREATE TABLE expression_publication_versions (
 publication_id TEXT NOT NULL REFERENCES expression_publications(id), revision INTEGER NOT NULL,
 producer_kind TEXT NOT NULL, producer_id TEXT NOT NULL, plan_json TEXT NOT NULL,
 PRIMARY KEY(publication_id,revision)
);
CREATE TABLE expression_presentation_items (
 id TEXT PRIMARY KEY, publication_id TEXT NOT NULL, revision INTEGER NOT NULL, ordinal INTEGER NOT NULL,
 text TEXT NOT NULL, content_hash TEXT NOT NULL, kind TEXT NOT NULL, required INTEGER NOT NULL, delay_ms INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','offered','presented','not_presented','cancelled','unknown')),
 lease_id TEXT UNIQUE, presenter_id TEXT, offered_at TEXT, receipt_id TEXT UNIQUE, acknowledged_at TEXT,
 UNIQUE(publication_id,revision,ordinal),
 FOREIGN KEY(publication_id,revision) REFERENCES expression_publication_versions(publication_id,revision)
);
CREATE TABLE expression_utterances (
 id TEXT PRIMARY KEY REFERENCES expression_presentation_items(id), turn_id TEXT NOT NULL REFERENCES turns(id),
 publication_id TEXT NOT NULL REFERENCES expression_publications(id), revision INTEGER NOT NULL,
 ordinal INTEGER NOT NULL, text TEXT NOT NULL, receipt_id TEXT NOT NULL UNIQUE, presented_at TEXT NOT NULL
);
CREATE TABLE expression_presentation_receipts (
 id TEXT PRIMARY KEY, utterance_id TEXT NOT NULL REFERENCES expression_presentation_items(id),
 lease_id TEXT NOT NULL, presenter_id TEXT NOT NULL, body_hash TEXT NOT NULL,
 state TEXT NOT NULL, observed_at TEXT NOT NULL
);
CREATE INDEX expression_runs_state ON expression_runs(state);
CREATE INDEX expression_utterances_turn ON expression_utterances(turn_id,ordinal);
'''


def migrate(db):
    from .storage import execute_schema
    from .models import RuntimeErrorBase
    present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if set(TABLES) & present:
        if not set(TABLES).issubset(present):raise RuntimeErrorBase('expression_runtime_schema_incomplete')
    else:execute_schema(db, SCHEMA)
    for table in ('expression_main_receipts', 'expression_publication_versions', 'expression_utterances','expression_presentation_receipts'):
        for action in ('UPDATE', 'DELETE'):
            execute_schema(db, f'''CREATE TRIGGER IF NOT EXISTS {table}_immutable_{action.lower()} BEFORE {action} ON {table}
            BEGIN SELECT RAISE(ABORT,'expression evidence is append only'); END;''')
    execute_schema(db, '''CREATE TRIGGER IF NOT EXISTS expression_acceptance_freeze AFTER INSERT ON turns
    WHEN NEW.persona_id IS NOT NULL AND NEW.channel='desktop' BEGIN
      INSERT INTO expression_runtime_turns(turn_id,policy_json) VALUES(NEW.id,COALESCE(
       (SELECT json_object('revision',revision,'data',json(data_json)) FROM expression_runtime_policies WHERE persona_id=NEW.persona_id),
       '{"revision":0,"data":{"enabled":false}}'));
    END;
    CREATE TRIGGER IF NOT EXISTS expression_execution_refreeze AFTER UPDATE OF execution_revision ON turns
    WHEN NEW.execution_revision!=OLD.execution_revision AND NEW.channel='desktop' BEGIN
      UPDATE expression_runtime_turns SET policy_json=COALESCE(
       (SELECT json_object('revision',revision,'data',json(data_json)) FROM expression_runtime_policies WHERE persona_id=NEW.persona_id),
       '{"revision":0,"data":{"enabled":false}}') WHERE turn_id=NEW.id;
    END;
    CREATE TRIGGER IF NOT EXISTS expression_run_receipt_immutable BEFORE UPDATE ON expression_runs
    WHEN OLD.raw_response IS NOT NULL AND (NEW.raw_response IS NOT OLD.raw_response OR NEW.raw_response_hash IS NOT OLD.raw_response_hash)
    BEGIN SELECT RAISE(ABORT,'expression response is immutable'); END;
    CREATE TRIGGER IF NOT EXISTS expression_run_contract_immutable BEFORE UPDATE ON expression_runs
    WHEN OLD.attempt_id IS NOT NULL AND (NEW.attempt_id IS NOT OLD.attempt_id OR NEW.request_json IS NOT OLD.request_json
      OR NEW.request_hash IS NOT OLD.request_hash OR NEW.input_json IS NOT OLD.input_json OR NEW.model_json IS NOT OLD.model_json
      OR NEW.draft_id IS NOT OLD.draft_id OR NEW.turn_id IS NOT OLD.turn_id)
    BEGIN SELECT RAISE(ABORT,'expression dispatched contract is immutable'); END;
    CREATE TRIGGER IF NOT EXISTS expression_presentation_body_immutable BEFORE UPDATE ON expression_presentation_items
    WHEN NEW.text IS NOT OLD.text OR NEW.content_hash IS NOT OLD.content_hash OR NEW.ordinal IS NOT OLD.ordinal
      OR NEW.publication_id IS NOT OLD.publication_id OR NEW.revision IS NOT OLD.revision
    BEGIN SELECT RAISE(ABORT,'expression presentation body is immutable'); END;
    ''')
