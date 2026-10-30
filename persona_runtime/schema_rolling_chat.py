"""Additive display ledger. No historical presentation or delivery is rewritten."""
SCHEMA = """
CREATE TABLE reply_display_messages (
 id TEXT PRIMARY KEY,
 batch_id TEXT NOT NULL, persona_id TEXT NOT NULL, user_id TEXT NOT NULL,
 semantic_index INTEGER NOT NULL, display_index INTEGER NOT NULL CHECK(display_index>0),
 text TEXT NOT NULL, text_hash TEXT NOT NULL, content_kind TEXT NOT NULL,
 transform_version TEXT NOT NULL, source_json TEXT NOT NULL, diagnostics_json TEXT NOT NULL,
 wait_ms INTEGER NOT NULL CHECK(wait_ms>=0),
 UNIQUE(batch_id,display_index),
 UNIQUE(id,batch_id,persona_id,user_id,semantic_index),
 FOREIGN KEY(batch_id,semantic_index) REFERENCES reply_segments(batch_id,segment_index),
 FOREIGN KEY(batch_id,persona_id,user_id) REFERENCES reply_batches(id,persona_id,user_id)
);
ALTER TABLE reply_delivery_parts ADD COLUMN display_message_id TEXT REFERENCES reply_display_messages(id);
CREATE INDEX delivery_display_message ON reply_delivery_parts(display_message_id,part_index);
CREATE TRIGGER delivery_display_scope BEFORE INSERT ON reply_delivery_parts
WHEN NEW.display_message_id IS NOT NULL
BEGIN
 SELECT CASE WHEN NOT EXISTS (
  SELECT 1 FROM reply_display_messages d WHERE d.id=NEW.display_message_id
  AND d.batch_id=NEW.reply_batch_id AND d.persona_id=NEW.persona_id
  AND d.user_id=NEW.user_id AND d.semantic_index=NEW.semantic_index
 ) THEN RAISE(ABORT,'display message scope mismatch') END;
END;
"""
