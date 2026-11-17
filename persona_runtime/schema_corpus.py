"""Independent corpus schema 1: provenance and original files before chat parsing."""

SCHEMA_VERSION = 1
APPLICATION_ID = 0x50434350
TABLES = ('corpus_meta', 'corpus_datasets', 'corpus_previews', 'corpus_files',
          'corpus_imports', 'corpus_source_locations', 'corpus_operations')

SCHEMA = '''
CREATE TABLE corpus_meta (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), corpus_id TEXT NOT NULL UNIQUE,
 schema_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE corpus_datasets (
 id TEXT PRIMARY KEY, platform TEXT NOT NULL CHECK(platform='wechat'),
 label TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('active','revoked')),
 revision INTEGER NOT NULL CHECK(revision>=1),
 local_storage INTEGER NOT NULL CHECK(local_storage=1),
 cloud_learning INTEGER NOT NULL DEFAULT 0 CHECK(cloud_learning=0),
 expression_use INTEGER NOT NULL DEFAULT 0 CHECK(expression_use=0),
 created_at TEXT NOT NULL
);
CREATE TABLE corpus_previews (
 id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES corpus_datasets(id),
 dataset_revision INTEGER NOT NULL, source_path TEXT NOT NULL,
 manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),
 manifest_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE corpus_files (
 sha256 TEXT PRIMARY KEY, byte_count INTEGER NOT NULL CHECK(byte_count>0),
 created_at TEXT NOT NULL
);
CREATE TABLE corpus_imports (
 id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES corpus_datasets(id),
 file_hash TEXT NOT NULL REFERENCES corpus_files(sha256),
 state TEXT NOT NULL CHECK(state='awaiting_export_format'), created_at TEXT NOT NULL,
 UNIQUE(dataset_id,file_hash)
);
CREATE TABLE corpus_source_locations (
 preview_id TEXT PRIMARY KEY REFERENCES corpus_previews(id),
 import_id TEXT NOT NULL REFERENCES corpus_imports(id)
);
CREATE TABLE corpus_operations (
 operation_id TEXT PRIMARY KEY, action TEXT NOT NULL, request_hash TEXT NOT NULL,
 result_json TEXT NOT NULL CHECK(json_valid(result_json)), created_at TEXT NOT NULL
);
CREATE INDEX corpus_preview_dataset ON corpus_previews(dataset_id);
CREATE INDEX corpus_import_dataset ON corpus_imports(dataset_id);
'''
