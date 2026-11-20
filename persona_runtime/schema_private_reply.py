"""Structure 34: desktop session consent and an immutable private-reply dispatch boundary."""
from .storage import execute_schema

TABLES = ('session_privacy_events', 'private_reply_instances', 'private_reply_turns',
          'private_reply_decisions', 'private_reply_attempts', 'private_reply_reviews')

SCHEMA = """
CREATE TABLE session_privacy_events (
 instance_id TEXT NOT NULL, session_id TEXT NOT NULL REFERENCES sessions(id),
 persona_id TEXT NOT NULL REFERENCES personas(id), user_id TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision > 0), enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
 protocol TEXT NOT NULL, recipients_json TEXT NOT NULL, operation_id TEXT NOT NULL,
 operation_hash TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(instance_id,session_id,revision), UNIQUE(instance_id,session_id,operation_id)
);
CREATE TABLE private_reply_instances (
 instance_id TEXT PRIMARY KEY, route TEXT NOT NULL
);
CREATE TABLE private_reply_turns (
 turn_id TEXT PRIMARY KEY REFERENCES turns(id), instance_id TEXT NOT NULL, route TEXT NOT NULL
);
CREATE TABLE private_reply_decisions (
 id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES private_reply_turns(turn_id),
 execution_revision INTEGER NOT NULL, phase TEXT NOT NULL, binding TEXT NOT NULL,
 contract_json TEXT NOT NULL CHECK(json_valid(contract_json)), decision_json TEXT NOT NULL CHECK(json_valid(decision_json)),
 created_at TEXT NOT NULL, expires_at REAL NOT NULL, UNIQUE(id,turn_id,phase),
 UNIQUE(turn_id,execution_revision,phase,binding,expires_at)
);
CREATE TABLE private_reply_attempts (
 id TEXT PRIMARY KEY, decision_id TEXT NOT NULL REFERENCES private_reply_decisions(id),
 turn_id TEXT NOT NULL REFERENCES private_reply_turns(turn_id), phase TEXT NOT NULL,
 draft INTEGER NOT NULL CHECK(draft IN (0,1)), state TEXT NOT NULL CHECK(state IN ('sending','unknown','received')),
 body TEXT, detail TEXT, body_sha256 TEXT, usage_json TEXT, started_at TEXT NOT NULL, received_at TEXT,
 UNIQUE(turn_id,phase,draft),
 FOREIGN KEY(decision_id,turn_id,phase) REFERENCES private_reply_decisions(id,turn_id,phase),
 CHECK((state='received' AND body IS NOT NULL AND body_sha256 IS NOT NULL AND received_at IS NOT NULL)
       OR (state IN ('sending','unknown') AND body IS NULL AND detail IS NULL AND received_at IS NULL))
);
CREATE UNIQUE INDEX private_reply_single_repair ON private_reply_attempts(turn_id) WHERE draft=1;
CREATE TABLE private_reply_reviews (
 attempt_id TEXT PRIMARY KEY REFERENCES private_reply_attempts(id),
 approved INTEGER NOT NULL CHECK(approved IN (0,1)), reason TEXT NOT NULL, receipt_id TEXT NOT NULL UNIQUE
);
"""


def migrate(db):
    from .models import RuntimeErrorBase
    existing = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if existing.intersection(TABLES):
        if not set(TABLES).issubset(existing):
            raise RuntimeErrorBase('private_reply_schema_incomplete')
    else:
        execute_schema(db, SCHEMA)
    for table in ('session_privacy_events', 'private_reply_turns', 'private_reply_decisions', 'private_reply_reviews'):
        for action in ('UPDATE', 'DELETE'):
            execute_schema(db, f"""CREATE TRIGGER IF NOT EXISTS {table}_immutable_{action} BEFORE {action} ON {table}
                BEGIN SELECT RAISE(ABORT,'immutable session evidence'); END;""")
    execute_schema(db, """CREATE TRIGGER IF NOT EXISTS private_reply_receipt_sealed BEFORE UPDATE ON private_reply_attempts
        WHEN OLD.state='received' OR NEW.id!=OLD.id OR NEW.decision_id!=OLD.decision_id
          OR NEW.turn_id!=OLD.turn_id OR NEW.phase!=OLD.phase OR NEW.draft!=OLD.draft
          OR NEW.started_at!=OLD.started_at OR (OLD.state='unknown' AND NEW.state='sending')
        BEGIN SELECT RAISE(ABORT,'private reply receipt sealed'); END;
        CREATE TRIGGER IF NOT EXISTS private_reply_attempt_retained BEFORE DELETE ON private_reply_attempts
        BEGIN SELECT RAISE(ABORT,'private reply attempt retained'); END;
    """)


def initialize_nodes(db):
    execute_schema(db, """
    DROP TRIGGER IF EXISTS private_reply_attempt_retained;
    DROP TRIGGER IF EXISTS private_reply_node_retained;
    CREATE TRIGGER IF NOT EXISTS private_reply_attempt_history BEFORE DELETE ON private_reply_attempts
      WHEN OLD.state!='sending' OR NOT EXISTS(SELECT 1 FROM knowledge_control
          WHERE key='private.writer.unsent.'||OLD.id AND json_extract(value,'$.turn')=OLD.turn_id)
      BEGIN SELECT RAISE(ABORT,'private reply attempt retained'); END;
    CREATE TABLE IF NOT EXISTS private_lookup_intents (
      turn_id TEXT PRIMARY KEY REFERENCES private_reply_turns(turn_id), target_json TEXT NOT NULL,
      origin TEXT, choice TEXT NOT NULL, binding TEXT NOT NULL, cached_turn TEXT,
      state TEXT NOT NULL CHECK(state IN ('none','waiting','ready','skipped')),
      query_json TEXT, confirmation_id TEXT, consent_revision INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS private_lookup_calls (
      turn_id TEXT NOT NULL REFERENCES private_lookup_intents(turn_id), step TEXT NOT NULL,
      attempt_id TEXT UNIQUE NOT NULL, binding TEXT NOT NULL, state TEXT NOT NULL
      CHECK(state IN ('sending','received','failed','unknown')), result_json TEXT, usage_json TEXT,
      PRIMARY KEY(turn_id,step)
    );
    CREATE TRIGGER IF NOT EXISTS private_lookup_intent_sealed BEFORE UPDATE ON private_lookup_intents
      WHEN NEW.turn_id!=OLD.turn_id OR NEW.binding!=OLD.binding OR NEW.choice!=OLD.choice OR NEW.target_json!=OLD.target_json
        OR NEW.cached_turn IS NOT OLD.cached_turn OR OLD.state!='waiting'
      BEGIN SELECT RAISE(ABORT,'lookup intent sealed'); END;
    CREATE TRIGGER IF NOT EXISTS private_lookup_receipt_sealed BEFORE UPDATE ON private_lookup_calls
      WHEN OLD.state IN ('received','failed') OR NEW.turn_id!=OLD.turn_id OR NEW.step!=OLD.step
        OR NEW.attempt_id!=OLD.attempt_id OR NEW.binding!=OLD.binding OR NEW.state='sending'
      BEGIN SELECT RAISE(ABORT,'lookup receipt sealed'); END;
    CREATE TABLE IF NOT EXISTS private_lookup_records (
      turn_id TEXT PRIMARY KEY REFERENCES private_lookup_intents(turn_id), persona_id TEXT NOT NULL,
      session_id TEXT NOT NULL, outcome TEXT NOT NULL, notes TEXT NOT NULL,
      sources_json TEXT NOT NULL, source_turn TEXT, created_at TEXT NOT NULL
    );
    CREATE TRIGGER IF NOT EXISTS private_lookup_record_sealed BEFORE UPDATE ON private_lookup_records
      BEGIN SELECT RAISE(ABORT,'lookup record sealed'); END;
    CREATE TRIGGER IF NOT EXISTS private_lookup_record_retained BEFORE DELETE ON private_lookup_records
      BEGIN SELECT RAISE(ABORT,'lookup record retained'); END;
    CREATE TRIGGER IF NOT EXISTS private_lookup_intent_retained BEFORE DELETE ON private_lookup_intents
      BEGIN SELECT RAISE(ABORT,'lookup intent retained'); END;
    CREATE TRIGGER IF NOT EXISTS private_lookup_attempt_retained BEFORE DELETE ON private_lookup_calls
      WHEN OLD.state!='sending' OR NOT EXISTS(SELECT 1 FROM knowledge_control WHERE key='private.lookup.unsent.'||OLD.attempt_id)
      BEGIN SELECT RAISE(ABORT,'lookup attempt retained'); END;
    CREATE TABLE IF NOT EXISTS private_reply_source_sets (
      turn_id TEXT PRIMARY KEY REFERENCES private_reply_turns(turn_id),
      source_json TEXT NOT NULL CHECK(json_valid(source_json)), material_hash TEXT NOT NULL
    );
    CREATE TRIGGER IF NOT EXISTS private_reply_sources_sealed BEFORE UPDATE ON private_reply_source_sets
      BEGIN SELECT RAISE(ABORT,'private chat materials frozen'); END;
    CREATE TRIGGER IF NOT EXISTS private_reply_sources_retained BEFORE DELETE ON private_reply_source_sets
      BEGIN SELECT RAISE(ABORT,'private chat materials frozen'); END;
    CREATE TABLE IF NOT EXISTS private_reply_nodes (
      turn_id TEXT NOT NULL REFERENCES private_reply_turns(turn_id), draft INTEGER NOT NULL CHECK(draft IN (0,1)),
      node TEXT NOT NULL, attempt_id TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL,
      state TEXT NOT NULL CHECK(state IN ('sending','received','unknown')),
      result_json TEXT, usage_json TEXT, PRIMARY KEY(turn_id,draft,node)
    );
    CREATE TRIGGER IF NOT EXISTS private_reply_node_sealed BEFORE UPDATE ON private_reply_nodes
      WHEN OLD.state='received' OR NEW.turn_id!=OLD.turn_id OR NEW.draft!=OLD.draft OR NEW.node!=OLD.node
        OR NEW.attempt_id!=OLD.attempt_id OR NEW.request_hash!=OLD.request_hash OR NEW.state='sending'
      BEGIN SELECT RAISE(ABORT,'private decision receipt sealed'); END;
    CREATE TRIGGER IF NOT EXISTS private_reply_node_history BEFORE DELETE ON private_reply_nodes
      WHEN OLD.state!='sending' OR NOT EXISTS(SELECT 1 FROM knowledge_control
          WHERE key='private.judge.unsent.'||OLD.attempt_id AND json_extract(value,'$.turn')=OLD.turn_id)
      BEGIN SELECT RAISE(ABORT,'private decision receipt retained'); END;
    """)
