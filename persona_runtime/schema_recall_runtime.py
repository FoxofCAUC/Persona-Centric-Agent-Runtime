"""Schema 29: runtime policy, accepted-turn snapshots and bounded dirty work."""
TABLES=('recall_runtime_policies','recall_runtime_turns','recall_runtime_dirty','recall_runtime_feedback')
SCHEMA='''
CREATE TABLE IF NOT EXISTS recall_runtime_policies (
 persona_id TEXT PRIMARY KEY REFERENCES personas(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL, data_json TEXT NOT NULL CHECK(json_valid(data_json))
);
CREATE TABLE IF NOT EXISTS recall_runtime_turns (
 turn_id TEXT PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
 policy_json TEXT NOT NULL CHECK(json_valid(policy_json)), selection_id TEXT
);
CREATE TABLE IF NOT EXISTS recall_runtime_dirty (
 persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, source_id TEXT NOT NULL, cursor_json TEXT NOT NULL DEFAULT '[0,0]',
 revision INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(persona_id,kind,source_id)
);
CREATE TABLE IF NOT EXISTS recall_runtime_feedback (
 consumer_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
 stage TEXT NOT NULL, state TEXT NOT NULL, error_code TEXT
);
CREATE TRIGGER IF NOT EXISTS recall_runtime_freeze AFTER INSERT ON turns
WHEN NEW.persona_id IS NOT NULL AND NEW.channel='desktop' BEGIN
 INSERT INTO recall_runtime_turns(turn_id,policy_json)
 VALUES(NEW.id,COALESCE((SELECT json_object('revision',revision,'data',json(data_json)) FROM recall_runtime_policies WHERE persona_id=NEW.persona_id),
 '{"revision":0,"data":{"enabled":false,"version":"recall-runtime-v1"}}'));
END;
'''

def migrate(db):
    from .storage import execute_schema
    execute_schema(db,SCHEMA)
    for table,kind,key,pid in (('memories','memory','id','persona_id'),('events','dialogue','id','persona_id')):
        for event in ('INSERT','UPDATE'):
            execute_schema(db,f'''CREATE TRIGGER IF NOT EXISTS recall_runtime_{table}_{event.lower()} AFTER {event} ON {table}
            WHEN EXISTS(SELECT 1 FROM recall_runtime_policies p WHERE p.persona_id=NEW.{pid} AND json_extract(p.data_json,'$.enabled')=1) BEGIN
              INSERT INTO recall_runtime_dirty(persona_id,kind,source_id) VALUES(NEW.{pid},'{kind}',NEW.{key})
              ON CONFLICT(persona_id,kind,source_id) DO UPDATE SET cursor_json='[0,0]',revision=revision+1;
            END;''')
    for event in ('INSERT','UPDATE'):
        execute_schema(db,f'''CREATE TRIGGER IF NOT EXISTS recall_runtime_native_{event.lower()} AFTER {event} ON knowledge_control
        WHEN (NEW.key LIKE 'knowledge.conversation.record.%' OR NEW.key LIKE 'knowledge.native.imported.%')
          AND EXISTS(SELECT 1 FROM recall_runtime_policies p WHERE p.persona_id=json_extract(NEW.value,'$.data.persona_id') AND json_extract(p.data_json,'$.enabled')=1) BEGIN
          INSERT INTO recall_runtime_dirty(persona_id,kind,source_id) VALUES(json_extract(NEW.value,'$.data.persona_id'),'native',NEW.key)
          ON CONFLICT(persona_id,kind,source_id) DO UPDATE SET cursor_json='[0,0]',revision=revision+1;
        END;''')
    execute_schema(db,'''CREATE TRIGGER IF NOT EXISTS recall_runtime_turn_complete AFTER UPDATE OF status ON turns
    WHEN NEW.status='completed' AND OLD.status!='completed'
      AND EXISTS(SELECT 1 FROM recall_runtime_policies p WHERE p.persona_id=NEW.persona_id AND json_extract(p.data_json,'$.enabled')=1) BEGIN
      INSERT INTO recall_runtime_dirty(persona_id,kind,source_id)
      SELECT NEW.persona_id,'dialogue',event_id FROM input_batch_members WHERE batch_id=NEW.input_batch_id
      ON CONFLICT(persona_id,kind,source_id) DO UPDATE SET cursor_json='[0,0]',revision=revision+1;
      INSERT INTO recall_runtime_dirty(persona_id,kind,source_id) VALUES(NEW.persona_id,'dialogue',NEW.user_event_id)
      ON CONFLICT(persona_id,kind,source_id) DO UPDATE SET cursor_json='[0,0]',revision=revision+1;
    END;''')
