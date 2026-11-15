"""Additive schema 28: versioned attention overlay, never a fact authority."""
TABLES=('recall_support_policies','recall_support_clock','recall_support_units',
        'recall_support_locations','recall_support_terms','recall_support_events','recall_support_searches','recall_support_sync')
SCHEMA='''
CREATE TABLE recall_support_policies (
 version TEXT PRIMARY KEY, data_json TEXT NOT NULL CHECK(json_valid(data_json))
);
CREATE TABLE recall_support_clock (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), effective_epoch REAL NOT NULL,
 observed_epoch REAL NOT NULL, anomaly TEXT
);
CREATE TABLE recall_support_units (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
 scope_key TEXT NOT NULL, scope_json TEXT NOT NULL CHECK(json_valid(scope_json)),
 kind TEXT NOT NULL, source_id TEXT NOT NULL, source_version TEXT NOT NULL, fragment_offset INTEGER NOT NULL,
 lineage TEXT NOT NULL, session_id TEXT, reference_json TEXT NOT NULL CHECK(json_valid(reference_json)),
 registered_epoch REAL NOT NULL, origin_epoch REAL,
 policy_version TEXT NOT NULL REFERENCES recall_support_policies(version),
 initial_priority REAL NOT NULL, anchor_priority REAL NOT NULL, anchor_epoch REAL NOT NULL,
 active_until REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 indexed_text TEXT NOT NULL, metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)),
 UNIQUE(scope_key,kind,source_id,source_version,fragment_offset)
);
CREATE INDEX recall_support_active ON recall_support_units(scope_key,kind,active_until,id);
CREATE INDEX recall_support_lineage ON recall_support_units(scope_key,lineage,id);
CREATE TABLE recall_support_locations (
 unit_id TEXT NOT NULL REFERENCES recall_support_units(id) ON DELETE CASCADE,
 location_id TEXT NOT NULL, reference_json TEXT NOT NULL CHECK(json_valid(reference_json)),
 session_id TEXT, observed_epoch REAL NOT NULL, PRIMARY KEY(unit_id,location_id)
);
CREATE INDEX recall_support_location_session ON recall_support_locations(session_id,unit_id);
CREATE TABLE recall_support_terms (
 term TEXT NOT NULL, unit_id TEXT NOT NULL REFERENCES recall_support_units(id) ON DELETE CASCADE,
 PRIMARY KEY(term,unit_id)
);
CREATE INDEX recall_support_term_unit ON recall_support_terms(unit_id,term);
CREATE TABLE recall_support_events (
 id TEXT PRIMARY KEY, unit_id TEXT NOT NULL REFERENCES recall_support_units(id) ON DELETE CASCADE,
 root_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
 effective_epoch REAL NOT NULL, alpha REAL NOT NULL, revision INTEGER NOT NULL,
 evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)),
 UNIQUE(unit_id,root_id)
);
CREATE TABLE recall_support_searches (
 id TEXT PRIMARY KEY, root_id TEXT, scope_key TEXT NOT NULL,
 request_hash TEXT NOT NULL, query_hash TEXT NOT NULL, effective_epoch REAL NOT NULL,
 result_json TEXT NOT NULL CHECK(json_valid(result_json))
);
CREATE TABLE recall_support_sync (
 persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE, kind TEXT NOT NULL,
 cursor TEXT NOT NULL, completed INTEGER NOT NULL, PRIMARY KEY(persona_id,kind)
);
CREATE TRIGGER recall_support_memory_delete AFTER DELETE ON memories BEGIN
 DELETE FROM recall_support_units WHERE kind='memory' AND source_id=OLD.id;
END;
CREATE TRIGGER recall_support_event_delete AFTER DELETE ON events BEGIN
 DELETE FROM recall_support_units WHERE kind='dialogue' AND source_id=OLD.id;
END;
CREATE TRIGGER recall_support_native_delete AFTER DELETE ON knowledge_control
WHEN OLD.key LIKE 'knowledge.conversation.record.%' OR OLD.key LIKE 'knowledge.native.imported.%' BEGIN
 DELETE FROM recall_support_locations WHERE json_extract(reference_json,'$.key')=OLD.key;
 DELETE FROM recall_support_units WHERE kind='native' AND NOT EXISTS(
  SELECT 1 FROM recall_support_locations l WHERE l.unit_id=recall_support_units.id);
END;
'''

def migrate(db):
    from .storage import execute_schema
    execute_schema(db,SCHEMA)
