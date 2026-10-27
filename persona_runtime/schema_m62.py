"""M6.2 response policy snapshot and validated semantic reply batches."""
TABLES9 = ("reply_batches", "reply_segments")
SCHEMA9 = """
ALTER TABLE interaction_policies ADD COLUMN reply_mode TEXT NOT NULL DEFAULT 'legacy_full'
 CHECK(reply_mode IN ('legacy_full','context_full','context_structured'));
ALTER TABLE turns ADD COLUMN reply_mode_snapshot TEXT;
CREATE TABLE reply_batches (
 id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns(id),
 attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id),
 persona_id TEXT NOT NULL REFERENCES personas(id), user_id TEXT NOT NULL REFERENCES users(id),
 schema_version INTEGER NOT NULL CHECK(schema_version=1),
 mode TEXT NOT NULL CHECK(mode IN ('legacy_full','context_full','context_structured')),
 raw_fingerprint TEXT NOT NULL, segment_count INTEGER NOT NULL CHECK(segment_count BETWEEN 1 AND 3),
 total_chars INTEGER NOT NULL CHECK(total_chars > 0), created_at TEXT NOT NULL,
 UNIQUE(id,persona_id,user_id),
 FOREIGN KEY(turn_id) REFERENCES turns(id)
);
CREATE INDEX reply_batches_turn ON reply_batches(turn_id, created_at);
CREATE TRIGGER reply_batch_scope_insert BEFORE INSERT ON reply_batches
BEGIN
 SELECT CASE WHEN NOT EXISTS (
   SELECT 1 FROM turns t JOIN attempts a ON a.turn_id=t.id
   WHERE t.id=NEW.turn_id AND t.persona_id=NEW.persona_id
     AND a.id=NEW.attempt_id AND a.turn_id=NEW.turn_id
 ) THEN RAISE(ABORT,'reply batch scope mismatch') END;
END;
CREATE TABLE reply_segments (
 batch_id TEXT NOT NULL, persona_id TEXT NOT NULL, user_id TEXT NOT NULL,
 segment_index INTEGER NOT NULL CHECK(segment_index BETWEEN 1 AND 3),
 text TEXT NOT NULL, format TEXT NOT NULL CHECK(format IN ('plain','markdown')),
 PRIMARY KEY(batch_id,segment_index),
 FOREIGN KEY(batch_id,persona_id,user_id) REFERENCES reply_batches(id,persona_id,user_id)
);
"""
