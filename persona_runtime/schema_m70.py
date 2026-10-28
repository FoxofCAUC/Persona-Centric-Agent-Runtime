"""M7.0 reference choices and pre-provider checks; never fabricate history."""
TABLES11 = ('input_references', 'reference_checks')
SCHEMA11 = """
CREATE TABLE input_references (
 event_id TEXT PRIMARY KEY, persona_id TEXT NOT NULL, user_id TEXT NOT NULL,
 channel TEXT NOT NULL CHECK(channel IN ('desktop','qq')), bot TEXT NOT NULL, peer TEXT NOT NULL,
 target_json TEXT NOT NULL, binding_json TEXT NOT NULL, parser_version INTEGER NOT NULL, created_at TEXT NOT NULL,
 FOREIGN KEY(event_id,persona_id,user_id) REFERENCES events(id,persona_id,user_id)
);
CREATE TABLE reference_checks (
 id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns(id), persona_id TEXT NOT NULL REFERENCES personas(id),
 user_id TEXT NOT NULL REFERENCES users(id), trigger_kind TEXT NOT NULL,
 attempt_id TEXT REFERENCES attempts(id), manifest_json TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL
);
CREATE INDEX raw_platform_reference ON raw_events(channel,bot,peer,platform_message_id);
CREATE INDEX sent_platform_reference ON reply_delivery_parts(platform_message_id);
"""
