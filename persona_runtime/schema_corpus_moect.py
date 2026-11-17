"""Corpus schema 2 adds local Moect parsing and author data-purpose receipts."""
from .schema_corpus import SCHEMA as FOUNDATION_SCHEMA, TABLES as FOUNDATION_TABLES

SCHEMA_VERSION = 2
NEW_TABLES = ('corpus_ingest_meta', 'corpus_material_grants', 'corpus_parses',
              'corpus_messages', 'corpus_relations')
TABLES = FOUNDATION_TABLES + NEW_TABLES
EXTENSION = '''
CREATE TABLE corpus_ingest_meta (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), identity_key_hash TEXT NOT NULL
);
CREATE TABLE corpus_material_grants (
 id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES corpus_datasets(id),
 receipt_hash TEXT NOT NULL, receipt_json TEXT NOT NULL CHECK(json_valid(receipt_json)),
 speaker_scope TEXT NOT NULL CHECK(speaker_scope='both'),
 training_input_authorized INTEGER NOT NULL CHECK(training_input_authorized=1),
 model_input_approved INTEGER NOT NULL DEFAULT 0 CHECK(model_input_approved=0),
 created_at TEXT NOT NULL, UNIQUE(dataset_id,receipt_hash)
);
CREATE TABLE corpus_parses (
 id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES corpus_datasets(id),
 dataset_revision INTEGER NOT NULL,
 json_import_id TEXT NOT NULL REFERENCES corpus_imports(id),
 md_import_id TEXT NOT NULL REFERENCES corpus_imports(id),
 grant_id TEXT NOT NULL REFERENCES corpus_material_grants(id),
 parser_version TEXT NOT NULL, implementation_hash TEXT NOT NULL,
 summary_json TEXT NOT NULL CHECK(json_valid(summary_json)), summary_hash TEXT NOT NULL,
 rows_hash TEXT NOT NULL, relations_hash TEXT NOT NULL, identity_map_hash TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(dataset_id,json_import_id,md_import_id,grant_id,parser_version,implementation_hash)
);
CREATE TABLE corpus_messages (
 parse_id TEXT NOT NULL REFERENCES corpus_parses(id), id TEXT NOT NULL, ordinal INTEGER NOT NULL,
 platform_message_id TEXT NOT NULL, source_locator TEXT NOT NULL,
 local_id INTEGER NOT NULL, shard INTEGER NOT NULL, speaker_token TEXT,
 role TEXT NOT NULL CHECK(role IN ('self','peer','system')), kind TEXT NOT NULL,
 created_epoch INTEGER NOT NULL, time_str TEXT NOT NULL, text TEXT NOT NULL,
 text_hash TEXT NOT NULL, record_hash TEXT NOT NULL,
 target_candidate INTEGER NOT NULL CHECK(target_candidate IN (0,1)), exclusion_code TEXT,
 md_text_start INTEGER NOT NULL, md_text_end INTEGER NOT NULL,
 PRIMARY KEY(parse_id,id), UNIQUE(parse_id,ordinal), UNIQUE(parse_id,platform_message_id)
);
CREATE TABLE corpus_relations (
 parse_id TEXT NOT NULL, source_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('reply','forward')), target_id TEXT,
 external_message_id TEXT, state TEXT NOT NULL CHECK(state IN ('resolved','unresolved','embedded')),
 evidence_locator TEXT NOT NULL,
 PRIMARY KEY(parse_id,source_id,kind),
 FOREIGN KEY(parse_id,source_id) REFERENCES corpus_messages(parse_id,id),
 FOREIGN KEY(parse_id,target_id) REFERENCES corpus_messages(parse_id,id)
);
CREATE INDEX corpus_messages_order ON corpus_messages(parse_id,ordinal);
CREATE INDEX corpus_messages_targets ON corpus_messages(parse_id,target_candidate,role);
'''
SCHEMA_HASH_INPUT = FOUNDATION_SCHEMA + '\n' + EXTENSION
