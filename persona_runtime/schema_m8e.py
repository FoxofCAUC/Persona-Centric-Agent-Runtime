"""23→24: scheduled outbound origins and fenced delivery, preserving old rows."""
import re

TABLES=('proactive_policies','proactive_rules','proactive_occurrences','proactive_operations','delivery_late_receipts')
SCHEMA="""
CREATE TABLE proactive_policies (
 id TEXT PRIMARY KEY,persona_id TEXT NOT NULL REFERENCES personas(id),revision INTEGER NOT NULL,
 data_json TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(persona_id,revision)
);
CREATE TABLE proactive_rules (
 id TEXT PRIMARY KEY,rule_id TEXT NOT NULL,persona_id TEXT NOT NULL REFERENCES personas(id),
 revision INTEGER NOT NULL,data_json TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(rule_id,revision)
);
CREATE TABLE proactive_occurrences (
 id TEXT PRIMARY KEY,rule_id TEXT NOT NULL,rule_version TEXT NOT NULL REFERENCES proactive_rules(id),
 persona_id TEXT NOT NULL REFERENCES personas(id),user_id TEXT NOT NULL REFERENCES users(id),
 scope_json TEXT NOT NULL,scheduled_epoch REAL NOT NULL,expires_epoch REAL NOT NULL,
 state TEXT NOT NULL,task_id TEXT REFERENCES organization_tasks(id),text TEXT,
 decision_json TEXT NOT NULL,created_epoch REAL NOT NULL,UNIQUE(rule_id,scheduled_epoch)
);
CREATE TABLE proactive_operations (
 operation_id TEXT PRIMARY KEY,request_hash TEXT NOT NULL,result_json TEXT NOT NULL,created_at TEXT NOT NULL
);
CREATE TABLE delivery_late_receipts (
 id TEXT PRIMARY KEY,part_id TEXT NOT NULL REFERENCES reply_delivery_parts(id),
 claim_token TEXT NOT NULL,platform_message_id TEXT,reason TEXT NOT NULL,created_at TEXT NOT NULL,
 UNIQUE(part_id,claim_token,platform_message_id)
);
"""


def rebuild(db,table,changes,extra=''):
    sql=db.execute("SELECT sql FROM sqlite_master WHERE name=?",(table,)).fetchone()[0]
    indexes=[r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",(table,))]
    columns=[r[1] for r in db.execute('PRAGMA table_info('+table+')')]
    sql=re.sub(r'^CREATE TABLE\s+"?'+table+r'"?', 'CREATE TABLE '+table+'_m8e',sql,count=1)
    for before,after in changes:sql=sql.replace(before,after)
    if extra:
        column,check=extra.split(', CHECK',1)
        boundary=re.search(r'\n\s*(?:UNIQUE\(|FOREIGN KEY\()',sql)
        if not boundary:raise RuntimeError('Missing expected table constraint boundary')
        index=boundary.start()
        sql=sql[:index]+'\n '+column+','+sql[index:]
        end=sql.rfind(')');sql=sql[:end]+',CHECK'+check+sql[end:]
    db.execute(sql)
    db.execute('INSERT INTO '+table+'_m8e ('+','.join(columns)+') SELECT '+','.join(columns)+' FROM '+table)
    db.execute('DROP TABLE '+table)
    db.execute('ALTER TABLE '+table+'_m8e RENAME TO '+table)
    for index in indexes:db.execute(index)


def migrate(db):
    from .storage import execute_schema
    execute_schema(db,SCHEMA)
    # Other tables' triggers can reference these tables, so preserve all affected
    # trigger definitions while replacing the tables inside this one transaction.
    triggers=list(db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND (sql LIKE '%reply_batches%' OR sql LIKE '%reply_delivery_parts%')"))
    for name,_ in triggers:db.execute('DROP TRIGGER '+name)
    db.execute('PRAGMA defer_foreign_keys=ON')
    rebuild(db,'reply_batches',[('turn_id TEXT NOT NULL','turn_id TEXT'),('attempt_id TEXT NOT NULL','attempt_id TEXT')],
        "outbound_id TEXT REFERENCES proactive_occurrences(id), CHECK((turn_id IS NOT NULL AND attempt_id IS NOT NULL AND outbound_id IS NULL) OR (turn_id IS NULL AND attempt_id IS NULL AND outbound_id IS NOT NULL))")
    rebuild(db,'reply_delivery_parts',[('turn_id TEXT NOT NULL','turn_id TEXT')])
    for column in ('sender_json TEXT','claimed_epoch REAL','uncertain_since REAL','isolated INTEGER NOT NULL DEFAULT 0'):
        db.execute('ALTER TABLE reply_delivery_parts ADD COLUMN '+column)
    for name,sql in triggers:
        if name=='reply_batch_scope_insert':
            sql=sql.replace('BEFORE INSERT ON reply_batches','BEFORE INSERT ON reply_batches WHEN NEW.turn_id IS NOT NULL')
        elif name=='reply_delivery_part_scope_insert':
            sql=sql.replace('BEFORE INSERT ON reply_delivery_parts','BEFORE INSERT ON reply_delivery_parts WHEN NEW.turn_id IS NOT NULL')
        db.execute(sql)
    execute_schema(db,"""
CREATE TRIGGER outbound_batch_scope BEFORE INSERT ON reply_batches WHEN NEW.outbound_id IS NOT NULL
BEGIN SELECT CASE WHEN NOT EXISTS (
 SELECT 1 FROM proactive_occurrences o WHERE o.id=NEW.outbound_id AND o.persona_id=NEW.persona_id AND o.user_id=NEW.user_id
) THEN RAISE(ABORT,'outbound batch scope mismatch') END; END;
CREATE TRIGGER outbound_part_scope BEFORE INSERT ON reply_delivery_parts WHEN NEW.turn_id IS NULL
BEGIN SELECT CASE WHEN NOT EXISTS (
 SELECT 1 FROM reply_batches b JOIN proactive_occurrences o ON o.id=b.outbound_id
 JOIN reply_delivery_streams s ON s.id=NEW.stream_id
 WHERE b.id=NEW.reply_batch_id AND b.turn_id IS NULL AND o.persona_id=NEW.persona_id
 AND o.user_id=NEW.user_id AND s.persona_id=NEW.persona_id AND s.user_id=NEW.user_id
 AND s.channel=json_extract(o.scope_json,'$.channel')
 AND json_extract(s.origin_json,'$.bot')=json_extract(o.scope_json,'$.bot')
 AND json_extract(s.origin_json,'$.sender')=json_extract(o.scope_json,'$.peer')
) THEN RAISE(ABORT,'outbound delivery scope mismatch') END; END;
""")
    if db.execute('PRAGMA foreign_key_check').fetchone():raise RuntimeError('M8.E migration foreign key failure')
    db.execute('PRAGMA defer_foreign_keys=OFF')
