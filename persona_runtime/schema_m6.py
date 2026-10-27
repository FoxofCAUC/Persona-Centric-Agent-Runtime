"""Additive M6.0 character canon; existing identity/profile rows remain authoritative."""
TABLES7 = ('character_versions', 'character_heads', 'character_operations')
SCHEMA7 = """
ALTER TABLE turns ADD COLUMN character_snapshot TEXT;
CREATE TABLE character_versions (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id),
 user_id TEXT NOT NULL REFERENCES users(id), revision INTEGER NOT NULL,
 data_json TEXT NOT NULL, actor TEXT NOT NULL,
 previous_id TEXT, restored_from TEXT, created_at TEXT NOT NULL,
 UNIQUE(id,persona_id,user_id), UNIQUE(persona_id,revision),
 FOREIGN KEY(previous_id,persona_id,user_id) REFERENCES character_versions(id,persona_id,user_id),
 FOREIGN KEY(restored_from,persona_id,user_id) REFERENCES character_versions(id,persona_id,user_id)
);
CREATE TABLE character_heads (
 persona_id TEXT PRIMARY KEY REFERENCES personas(id), user_id TEXT NOT NULL REFERENCES users(id),
 version_id TEXT NOT NULL,
 FOREIGN KEY(version_id,persona_id,user_id) REFERENCES character_versions(id,persona_id,user_id)
);
CREATE TABLE character_operations (
 operation_id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id),
 user_id TEXT NOT NULL REFERENCES users(id), fingerprint TEXT NOT NULL, version_id TEXT NOT NULL,
 FOREIGN KEY(version_id,persona_id,user_id) REFERENCES character_versions(id,persona_id,user_id)
);
"""
