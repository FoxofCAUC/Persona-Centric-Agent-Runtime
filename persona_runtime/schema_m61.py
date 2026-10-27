"""M6.1 durable receipt and batch contracts; no fabricated legacy ingress."""
TABLES8 = ('interaction_policies','interaction_heads','raw_events','input_batches','ingress_receipts','input_batch_members','batch_operations')
SCHEMA8 = """
CREATE TABLE interaction_policies (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id), user_id TEXT NOT NULL REFERENCES users(id),
 channel TEXT NOT NULL CHECK(channel IN ('desktop','qq')), revision INTEGER NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('legacy_per_message','natural_batch')),
 data_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(id,persona_id,user_id), UNIQUE(persona_id,channel,revision)
);
CREATE TABLE interaction_heads (
 persona_id TEXT NOT NULL, user_id TEXT NOT NULL, channel TEXT NOT NULL, version_id TEXT NOT NULL,
 PRIMARY KEY(persona_id,channel), FOREIGN KEY(version_id,persona_id,user_id) REFERENCES interaction_policies(id,persona_id,user_id)
);
CREATE TABLE raw_events (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL, user_id TEXT NOT NULL, event_id TEXT NOT NULL UNIQUE,
 channel TEXT NOT NULL, bot TEXT NOT NULL, peer TEXT NOT NULL, platform_message_id TEXT,
 platform_time TEXT, received_at TEXT NOT NULL, representation TEXT NOT NULL CHECK(representation='normalized_payload'),
 parser_version INTEGER NOT NULL, payload_json TEXT NOT NULL, fingerprint TEXT NOT NULL,
 UNIQUE(id,persona_id,user_id), FOREIGN KEY(event_id,persona_id,user_id) REFERENCES events(id,persona_id,user_id)
);
CREATE TABLE input_batches (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL, user_id TEXT NOT NULL, session_id TEXT NOT NULL,
 channel TEXT NOT NULL, bot TEXT NOT NULL, peer TEXT NOT NULL, scope_key TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('collecting','sealed_waiting_capacity','linked','interrupted_buffer','cancelled')),
 revision INTEGER NOT NULL, first_seq INTEGER NOT NULL, first_epoch REAL NOT NULL, last_epoch REAL NOT NULL,
 deadline_epoch REAL NOT NULL, max_deadline_epoch REAL NOT NULL, sealed_at TEXT, seal_reason TEXT,
 policy_json TEXT NOT NULL, profile_snapshot TEXT NOT NULL, character_snapshot TEXT NOT NULL, model_snapshot TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, buffer_ms REAL NOT NULL DEFAULT 0, UNIQUE(id,persona_id,user_id),
 FOREIGN KEY(session_id,persona_id,user_id) REFERENCES sessions(id,persona_id,user_id)
);
CREATE UNIQUE INDEX one_collecting_scope ON input_batches(scope_key) WHERE state='collecting';
CREATE TABLE ingress_receipts (
 accepted_seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, source_key TEXT NOT NULL UNIQUE,
 request_id TEXT NOT NULL, persona_id TEXT NOT NULL, user_id TEXT NOT NULL, channel TEXT NOT NULL,
 bot TEXT NOT NULL, peer TEXT NOT NULL, session_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
 raw_event_id TEXT NOT NULL UNIQUE, event_id TEXT NOT NULL UNIQUE, batch_id TEXT NOT NULL, received_at TEXT NOT NULL, ingress_ms REAL NOT NULL DEFAULT 0,
 UNIQUE(id,persona_id,user_id), UNIQUE(channel,request_id),
 FOREIGN KEY(raw_event_id,persona_id,user_id) REFERENCES raw_events(id,persona_id,user_id),
 FOREIGN KEY(event_id,persona_id,user_id) REFERENCES events(id,persona_id,user_id),
 FOREIGN KEY(batch_id,persona_id,user_id) REFERENCES input_batches(id,persona_id,user_id)
);
CREATE TABLE input_batch_members (
 batch_id TEXT NOT NULL, persona_id TEXT NOT NULL, user_id TEXT NOT NULL, receipt_id TEXT NOT NULL UNIQUE,
 event_id TEXT NOT NULL UNIQUE, member_order INTEGER NOT NULL, PRIMARY KEY(batch_id,member_order),
 FOREIGN KEY(batch_id,persona_id,user_id) REFERENCES input_batches(id,persona_id,user_id),
 FOREIGN KEY(receipt_id,persona_id,user_id) REFERENCES ingress_receipts(id,persona_id,user_id),
 FOREIGN KEY(event_id,persona_id,user_id) REFERENCES events(id,persona_id,user_id)
);
CREATE TABLE batch_operations (
 operation_id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id), user_id TEXT NOT NULL REFERENCES users(id),
 fingerprint TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL
);
ALTER TABLE turns ADD COLUMN input_batch_id TEXT REFERENCES input_batches(id);
CREATE TRIGGER turn_batch_scope_insert BEFORE INSERT ON turns WHEN NEW.input_batch_id IS NOT NULL
BEGIN
 SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM input_batches b WHERE b.id=NEW.input_batch_id AND b.persona_id=NEW.persona_id AND b.user_id='00000000-0000-4000-8000-000000000001' AND b.session_id=NEW.session_id AND b.channel=NEW.channel) THEN RAISE(ABORT,'turn batch scope mismatch') END;
END;
CREATE UNIQUE INDEX one_turn_per_batch ON turns(input_batch_id) WHERE input_batch_id IS NOT NULL;
"""
