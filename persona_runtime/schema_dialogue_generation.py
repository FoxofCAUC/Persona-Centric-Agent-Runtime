"""Structure 33 freezes each desktop turn's generation policy and first stage receipts."""
import json

from .models import RuntimeErrorBase, timestamp
from .storage import execute_schema


TABLES = ('dialogue_generation_policies', 'dialogue_generation_turns',
          'dialogue_generation_units', 'dialogue_generation_stages')

SCHEMA = """
CREATE TABLE dialogue_generation_policies (
 persona_id TEXT PRIMARY KEY REFERENCES personas(id), revision INTEGER NOT NULL, data_json TEXT NOT NULL
);
CREATE TABLE dialogue_generation_turns (
 turn_id TEXT PRIMARY KEY REFERENCES turns(id), policy_json TEXT NOT NULL, closed_at TEXT, reason TEXT
);
CREATE TABLE dialogue_generation_units (
 id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES dialogue_generation_turns(turn_id),
 phase TEXT NOT NULL CHECK(phase IN ('main','search_notice')), material_json TEXT NOT NULL,
 context_json TEXT NOT NULL, execution_revision INTEGER NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(turn_id,phase)
);
CREATE TABLE dialogue_generation_stages (
 id TEXT PRIMARY KEY, unit_id TEXT NOT NULL REFERENCES dialogue_generation_units(id),
 stage TEXT NOT NULL CHECK(stage IN ('understanding','selection','reply')),
 state TEXT NOT NULL CHECK(state IN ('sending','completed','unknown','failed')),
 model_json TEXT NOT NULL, request_json TEXT NOT NULL, request_hash TEXT NOT NULL,
 dependencies_json TEXT NOT NULL, attempt_id TEXT NOT NULL UNIQUE, started_at TEXT NOT NULL,
 raw_response BLOB, raw_hash TEXT, http_status INTEGER, transport_json TEXT, result_json TEXT, error_code TEXT,
 artifact_json TEXT, validation_error TEXT,
 UNIQUE(unit_id,stage)
);
CREATE INDEX dialogue_stage_state ON dialogue_generation_stages(state);
"""


def migrate(db):
    present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if set(TABLES) & present:
        if not set(TABLES).issubset(present):
            raise RuntimeErrorBase('dialogue_generation_schema_incomplete')
    else:
        execute_schema(db, SCHEMA)
    execute_schema(db, """
    CREATE TRIGGER IF NOT EXISTS dialogue_generation_turn_freeze AFTER INSERT ON turns
    WHEN NEW.persona_id IS NOT NULL AND NEW.channel='desktop' BEGIN
      INSERT INTO dialogue_generation_turns(turn_id,policy_json) VALUES(NEW.id, COALESCE(
        (SELECT json_object('revision',revision,'data',json(data_json)) FROM dialogue_generation_policies
          WHERE persona_id=NEW.persona_id), '{"revision":0,"data":{"enabled":false}}'));
    END;
    CREATE TRIGGER IF NOT EXISTS dialogue_generation_turn_contract BEFORE UPDATE ON dialogue_generation_turns
    WHEN NEW.turn_id IS NOT OLD.turn_id OR NEW.policy_json IS NOT OLD.policy_json
      OR (OLD.closed_at IS NOT NULL AND (NEW.closed_at IS NOT OLD.closed_at OR NEW.reason IS NOT OLD.reason))
    BEGIN SELECT RAISE(ABORT,'generation turn frozen'); END;
    CREATE TRIGGER IF NOT EXISTS dialogue_generation_unit_contract BEFORE UPDATE ON dialogue_generation_units
    BEGIN SELECT RAISE(ABORT,'generation material frozen'); END;
    CREATE TRIGGER IF NOT EXISTS dialogue_generation_call_contract BEFORE UPDATE ON dialogue_generation_stages
    WHEN NEW.id IS NOT OLD.id OR NEW.unit_id IS NOT OLD.unit_id OR NEW.stage IS NOT OLD.stage
      OR NEW.request_json IS NOT OLD.request_json OR NEW.request_hash IS NOT OLD.request_hash
      OR NEW.model_json IS NOT OLD.model_json OR NEW.dependencies_json IS NOT OLD.dependencies_json
      OR NEW.attempt_id IS NOT OLD.attempt_id OR NEW.started_at IS NOT OLD.started_at
      OR (OLD.raw_response IS NOT NULL AND (NEW.raw_response IS NOT OLD.raw_response
          OR NEW.raw_hash IS NOT OLD.raw_hash OR NEW.http_status IS NOT OLD.http_status))
      OR (OLD.state!='sending' AND NEW.state IS NOT OLD.state)
      OR (OLD.result_json IS NOT NULL AND NEW.result_json IS NOT OLD.result_json)
      OR ((OLD.artifact_json IS NOT NULL OR OLD.validation_error IS NOT NULL) AND
          (NEW.artifact_json IS NOT OLD.artifact_json OR NEW.validation_error IS NOT OLD.validation_error))
    BEGIN SELECT RAISE(ABORT,'generation first receipt frozen'); END;
    """)
    for table in TABLES[1:]:
        execute_schema(db, f"""CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table}
                     BEGIN SELECT RAISE(ABORT,'generation evidence immutable'); END;""")


def pause(db):
    for pid, revision, raw in db.execute('SELECT * FROM dialogue_generation_policies').fetchall():
        value = json.loads(raw)
        if value.get('enabled'):
            value['enabled'] = False
            db.execute('UPDATE dialogue_generation_policies SET revision=?,data_json=? WHERE persona_id=?',
                       (revision + 1, json.dumps(value), pid))
    db.execute("""UPDATE dialogue_generation_turns SET closed_at=?,reason='migration_requires_review'
        WHERE closed_at IS NULL AND json_extract(policy_json,'$.data.enabled')=1""", (timestamp(),))
    unknown = db.execute("""UPDATE dialogue_generation_stages SET state='unknown',error_code='migration_requires_review'
        WHERE state='sending' AND raw_response IS NULL""").rowcount
    return {'dialogue_generation_paused': True, 'dialogue_generation_unknown': unknown}
