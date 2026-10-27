"""Additive persona, relationship and explicit-state version history."""
TABLES6 = ('profile_versions', 'profile_heads', 'profile_operations', 'relationship_proposals')
SCHEMA6 = """
ALTER TABLE turns ADD COLUMN profile_snapshot TEXT;
CREATE TABLE profile_versions (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id), user_id TEXT NOT NULL REFERENCES users(id),
 kind TEXT NOT NULL CHECK(kind IN ('core','expression','relationship','state')), revision INTEGER NOT NULL,
 data_json TEXT NOT NULL, source_ids_json TEXT NOT NULL, actor TEXT NOT NULL, previous_id TEXT REFERENCES profile_versions(id),
 restored_from TEXT REFERENCES profile_versions(id), created_at TEXT NOT NULL,
 UNIQUE(persona_id,kind,revision)
);
CREATE TABLE profile_heads (
 persona_id TEXT NOT NULL REFERENCES personas(id), kind TEXT NOT NULL, version_id TEXT NOT NULL REFERENCES profile_versions(id),
 PRIMARY KEY(persona_id,kind)
);
CREATE TABLE profile_operations (
 operation_id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id), fingerprint TEXT NOT NULL,
 version_id TEXT NOT NULL REFERENCES profile_versions(id)
);
CREATE TABLE relationship_proposals (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id), user_id TEXT NOT NULL REFERENCES users(id),
 text TEXT NOT NULL, source_event_id TEXT NOT NULL REFERENCES events(id), expected_version TEXT NOT NULL REFERENCES profile_versions(id),
 status TEXT NOT NULL, version_id TEXT REFERENCES profile_versions(id), created_at TEXT NOT NULL
);
"""
