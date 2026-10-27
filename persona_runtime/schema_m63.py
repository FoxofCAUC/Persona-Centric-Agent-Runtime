"""M6.3 durable platform delivery ledger."""

TABLES10 = ("reply_delivery_streams", "reply_delivery_parts", "reply_delivery_attempts", "reply_delivery_operations")

SCHEMA10 = """
CREATE TABLE reply_delivery_streams (
 id TEXT PRIMARY KEY,
 persona_id TEXT NOT NULL REFERENCES personas(id),
 user_id TEXT NOT NULL REFERENCES users(id),
 channel TEXT NOT NULL CHECK(channel IN ('desktop','qq')),
 origin_json TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('open','blocked','completed','skipped')),
 revision INTEGER NOT NULL CHECK(revision >= 1),
 blocked_reason TEXT,
 blocked_part_id TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(id, persona_id, user_id),
 UNIQUE(persona_id,user_id,channel,origin_json)
);
CREATE INDEX reply_delivery_streams_scope ON reply_delivery_streams(persona_id,user_id,channel,state,updated_at);
CREATE TABLE reply_delivery_parts (
 id TEXT PRIMARY KEY,
 stream_id TEXT NOT NULL REFERENCES reply_delivery_streams(id),
 reply_batch_id TEXT NOT NULL REFERENCES reply_batches(id),
 turn_id TEXT NOT NULL REFERENCES turns(id),
 persona_id TEXT NOT NULL REFERENCES personas(id),
 user_id TEXT NOT NULL REFERENCES users(id),
 semantic_index INTEGER NOT NULL CHECK(semantic_index >= 1),
 part_index INTEGER NOT NULL CHECK(part_index >= 1),
 stream_seq INTEGER NOT NULL CHECK(stream_seq >= 1),
 text TEXT NOT NULL,
 text_hash TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','sending','sent','failed','uncertain','skipped','resolved')),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
 claim_token TEXT,
 attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0),
 platform_message_id TEXT,
 sent_at TEXT,
 error TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(reply_batch_id,part_index),
 UNIQUE(stream_id,stream_seq),
 UNIQUE(id,persona_id,user_id),
 FOREIGN KEY(stream_id,persona_id,user_id) REFERENCES reply_delivery_streams(id,persona_id,user_id),
 FOREIGN KEY(reply_batch_id) REFERENCES reply_batches(id)
);
CREATE INDEX reply_delivery_parts_ready ON reply_delivery_parts(stream_id,state,part_index);
CREATE TRIGGER reply_delivery_part_scope_insert BEFORE INSERT ON reply_delivery_parts
BEGIN
 SELECT CASE WHEN NOT EXISTS (
   SELECT 1 FROM reply_delivery_streams s JOIN reply_batches b ON b.id=NEW.reply_batch_id
   JOIN turns t ON t.id=NEW.turn_id
   WHERE s.id=NEW.stream_id AND s.persona_id=NEW.persona_id AND s.user_id=NEW.user_id
     AND b.persona_id=NEW.persona_id AND b.user_id=NEW.user_id AND b.turn_id=NEW.turn_id
     AND t.persona_id=NEW.persona_id AND t.channel=s.channel
     AND json_extract(t.origin_json,'$.sender')=json_extract(s.origin_json,'$.sender')
     AND json_extract(t.origin_json,'$.bot')=json_extract(s.origin_json,'$.bot')
 ) THEN RAISE(ABORT,'reply delivery scope mismatch') END;
END;
CREATE TABLE reply_delivery_attempts (
 id TEXT PRIMARY KEY,
 part_id TEXT NOT NULL REFERENCES reply_delivery_parts(id),
 attempt_no INTEGER NOT NULL CHECK(attempt_no >= 1),
 state TEXT NOT NULL CHECK(state IN ('sending','sent','failed','uncertain')),
 platform_message_id TEXT,
 error TEXT,
 started_at TEXT NOT NULL,
 ended_at TEXT,
 UNIQUE(part_id,attempt_no)
);
CREATE INDEX reply_delivery_attempts_part ON reply_delivery_attempts(part_id,attempt_no);
CREATE TABLE reply_delivery_operations (
 operation_id TEXT PRIMARY KEY,
 stream_id TEXT NOT NULL REFERENCES reply_delivery_streams(id),
 part_id TEXT,
 action TEXT NOT NULL,
 actor TEXT NOT NULL,
 reason TEXT,
 allow_continue INTEGER NOT NULL CHECK(allow_continue IN (0,1)),
 request_hash TEXT NOT NULL,
 result_json TEXT NOT NULL,
 created_at TEXT NOT NULL,
 FOREIGN KEY(part_id) REFERENCES reply_delivery_parts(id)
);
"""


def refresh_development_scope_trigger(conn):
    """Repair the unreleased schema-10 trigger without rewriting historical rows."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='reply_delivery_part_scope_insert'").fetchone()
    if row and 't.origin_json=s.origin_json' in row[0]:
        start = SCHEMA10.index('CREATE TRIGGER reply_delivery_part_scope_insert')
        end = SCHEMA10.index('CREATE TABLE reply_delivery_attempts', start)
        conn.execute('DROP TRIGGER reply_delivery_part_scope_insert')
        conn.execute(SCHEMA10[start:end].strip())
