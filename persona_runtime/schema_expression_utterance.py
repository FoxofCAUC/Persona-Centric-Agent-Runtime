"""结构32：人物发言单元；结构31主回复回执保持原合同。"""
TABLES=('expression_speech_contexts','expression_speech_units','expression_speech_runs',
        'expression_speech_publications','expression_speech_versions','expression_speech_items',
        'expression_speech_visible','expression_speech_receipts')
SCHEMA='''
CREATE TABLE expression_speech_contexts (
 task_id TEXT PRIMARY KEY REFERENCES organization_tasks(id), turn_id TEXT NOT NULL REFERENCES turns(id),
 phase TEXT NOT NULL, policy_json TEXT NOT NULL, voice_json TEXT NOT NULL, context_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE expression_speech_units (
 id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns(id), phase TEXT NOT NULL,
 source_kind TEXT NOT NULL, source_id TEXT NOT NULL, source_hash TEXT NOT NULL,
 persona_id TEXT NOT NULL REFERENCES personas(id), session_id TEXT NOT NULL REFERENCES sessions(id),
 task_id TEXT REFERENCES organization_tasks(id), body TEXT NOT NULL, body_hash TEXT NOT NULL,
 policy_json TEXT NOT NULL, voice_json TEXT NOT NULL, context_json TEXT NOT NULL,
 cancelled_at TEXT, closed_at TEXT, created_at TEXT NOT NULL,
 UNIQUE(turn_id,phase), UNIQUE(source_kind,source_id)
);
CREATE TABLE expression_speech_runs (
 id TEXT PRIMARY KEY, utterance_id TEXT NOT NULL UNIQUE REFERENCES expression_speech_units(id),
 state TEXT NOT NULL, request_json TEXT NOT NULL, request_hash TEXT NOT NULL, model_json TEXT NOT NULL,
 attempt_id TEXT UNIQUE, raw_response BLOB, raw_hash TEXT, usage_json TEXT, transport_json TEXT,
 result_json TEXT, error_code TEXT, started_epoch REAL
);
CREATE TABLE expression_speech_publications (
 id TEXT PRIMARY KEY, utterance_id TEXT NOT NULL UNIQUE REFERENCES expression_speech_units(id),
 revision INTEGER NOT NULL, producer_kind TEXT NOT NULL, body TEXT NOT NULL, body_hash TEXT NOT NULL,
 state TEXT NOT NULL, reason TEXT, canonical_event_id TEXT REFERENCES events(id), created_at TEXT NOT NULL
);
CREATE TABLE expression_speech_versions (
 publication_id TEXT NOT NULL REFERENCES expression_speech_publications(id), revision INTEGER NOT NULL,
 plan_json TEXT NOT NULL, PRIMARY KEY(publication_id,revision)
);
CREATE TABLE expression_speech_items (
 id TEXT PRIMARY KEY, publication_id TEXT NOT NULL, revision INTEGER NOT NULL, ordinal INTEGER NOT NULL,
 text TEXT NOT NULL, body_hash TEXT NOT NULL, kind TEXT NOT NULL, required INTEGER NOT NULL,
 delay_ms INTEGER NOT NULL, state TEXT NOT NULL, lease_id TEXT UNIQUE, presenter_id TEXT,
 receipt_id TEXT, offered_at TEXT, UNIQUE(publication_id,revision,ordinal),
 FOREIGN KEY(publication_id,revision) REFERENCES expression_speech_versions(publication_id,revision)
);
CREATE TABLE expression_speech_visible (
 fragment_id TEXT PRIMARY KEY REFERENCES expression_speech_items(id), utterance_id TEXT NOT NULL REFERENCES expression_speech_units(id),
 publication_id TEXT NOT NULL REFERENCES expression_speech_publications(id), ordinal INTEGER NOT NULL,
 text TEXT NOT NULL, receipt_id TEXT NOT NULL UNIQUE, presented_at TEXT NOT NULL
);
CREATE TABLE expression_speech_receipts (
 id TEXT PRIMARY KEY, fragment_id TEXT NOT NULL REFERENCES expression_speech_items(id),
 lease_id TEXT NOT NULL, presenter_id TEXT NOT NULL, body_hash TEXT NOT NULL, state TEXT NOT NULL, observed_at TEXT NOT NULL
);
CREATE INDEX expression_speech_turn ON expression_speech_units(turn_id,phase);
CREATE INDEX expression_speech_run_state ON expression_speech_runs(state);
'''


def migrate(db):
    from .storage import execute_schema
    from .models import RuntimeErrorBase
    present={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if set(TABLES)&present:
        if not set(TABLES).issubset(present):raise RuntimeErrorBase('expression_speech_schema_incomplete')
    else:execute_schema(db,SCHEMA)
    for table in ('expression_speech_contexts','expression_speech_versions','expression_speech_visible','expression_speech_receipts'):
        for action in ('UPDATE','DELETE'):
            execute_schema(db,f'''CREATE TRIGGER IF NOT EXISTS {table}_immutable_{action.lower()} BEFORE {action} ON {table}
              BEGIN SELECT RAISE(ABORT,'speech evidence is append only'); END;''')
    for table in ('expression_speech_units','expression_speech_runs','expression_speech_publications','expression_speech_items'):
        execute_schema(db,f'''CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table}
          BEGIN SELECT RAISE(ABORT,'speech evidence is append only'); END;''')
    execute_schema(db,'''CREATE TRIGGER IF NOT EXISTS expression_speech_unit_contract BEFORE UPDATE ON expression_speech_units
      WHEN NEW.id IS NOT OLD.id OR NEW.turn_id IS NOT OLD.turn_id OR NEW.phase IS NOT OLD.phase
       OR NEW.source_kind IS NOT OLD.source_kind OR NEW.source_id IS NOT OLD.source_id OR NEW.source_hash IS NOT OLD.source_hash
       OR NEW.persona_id IS NOT OLD.persona_id OR NEW.session_id IS NOT OLD.session_id OR NEW.task_id IS NOT OLD.task_id
       OR NEW.body IS NOT OLD.body OR NEW.body_hash IS NOT OLD.body_hash OR NEW.policy_json IS NOT OLD.policy_json
       OR NEW.voice_json IS NOT OLD.voice_json OR NEW.context_json IS NOT OLD.context_json
      BEGIN SELECT RAISE(ABORT,'speech source contract is immutable'); END;
    CREATE TRIGGER IF NOT EXISTS expression_speech_run_contract BEFORE UPDATE ON expression_speech_runs
      WHEN NEW.id IS NOT OLD.id OR NEW.utterance_id IS NOT OLD.utterance_id OR NEW.request_json IS NOT OLD.request_json
       OR NEW.request_hash IS NOT OLD.request_hash OR NEW.model_json IS NOT OLD.model_json
       OR (OLD.attempt_id IS NOT NULL AND NEW.attempt_id IS NOT OLD.attempt_id)
       OR (OLD.raw_response IS NOT NULL AND (NEW.raw_response IS NOT OLD.raw_response OR NEW.raw_hash IS NOT OLD.raw_hash))
      BEGIN SELECT RAISE(ABORT,'speech dispatched contract is immutable'); END;
    CREATE TRIGGER IF NOT EXISTS expression_speech_item_body BEFORE UPDATE ON expression_speech_items
      WHEN NEW.id IS NOT OLD.id OR NEW.publication_id IS NOT OLD.publication_id OR NEW.revision IS NOT OLD.revision
       OR NEW.ordinal IS NOT OLD.ordinal OR NEW.text IS NOT OLD.text OR NEW.body_hash IS NOT OLD.body_hash
      BEGIN SELECT RAISE(ABORT,'speech presentation body is immutable'); END;
    CREATE TRIGGER IF NOT EXISTS expression_speech_publication_body BEFORE UPDATE ON expression_speech_publications
      WHEN (NEW.body IS NOT OLD.body OR NEW.body_hash IS NOT OLD.body_hash OR NEW.revision IS NOT OLD.revision OR NEW.producer_kind IS NOT OLD.producer_kind)
       AND (OLD.state IN ('closed','complete') OR EXISTS(SELECT 1 FROM expression_speech_items WHERE publication_id=OLD.id AND lease_id IS NOT NULL))
      BEGIN SELECT RAISE(ABORT,'speech publication is sealed'); END;
    ''')
