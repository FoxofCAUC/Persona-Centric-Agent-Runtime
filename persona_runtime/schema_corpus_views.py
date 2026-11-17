"""Corpus schema 3: immutable local privacy views, fragments and split groups."""
from .schema_corpus_moect import SCHEMA_HASH_INPUT as PRIOR_HASH_INPUT, TABLES as PRIOR_TABLES

SCHEMA_VERSION = 3
NEW_TABLES = ('corpus_dataset_versions','corpus_view_messages','corpus_view_fragments',
              'corpus_view_groups','corpus_view_edges')
TABLES = PRIOR_TABLES + NEW_TABLES
EXTENSION = '''
CREATE TABLE corpus_dataset_versions (
 id TEXT PRIMARY KEY, parse_id TEXT NOT NULL REFERENCES corpus_parses(id),
 dataset_revision INTEGER NOT NULL, source_proof_json TEXT NOT NULL CHECK(json_valid(source_proof_json)),
 source_proof_hash TEXT NOT NULL, recipe_json TEXT NOT NULL CHECK(json_valid(recipe_json)),
 recipe_hash TEXT NOT NULL, implementation_hash TEXT NOT NULL, request_hash TEXT NOT NULL UNIQUE,
 summary_json TEXT NOT NULL CHECK(json_valid(summary_json)), summary_hash TEXT NOT NULL,
 payload_hash TEXT NOT NULL, model_input_approved INTEGER NOT NULL CHECK(model_input_approved=0),
 created_at TEXT NOT NULL
);
CREATE TABLE corpus_view_messages (
 view_id TEXT NOT NULL REFERENCES corpus_dataset_versions(id), message_id TEXT NOT NULL,
 ordinal INTEGER NOT NULL, split TEXT NOT NULL CHECK(split IN ('learn','dev_review','final_holdout','excluded')),
 target_candidate INTEGER NOT NULL CHECK(target_candidate IN (0,1)), text_hash TEXT NOT NULL,
 data_json TEXT NOT NULL CHECK(json_valid(data_json)), PRIMARY KEY(view_id,message_id), UNIQUE(view_id,ordinal)
);
CREATE TABLE corpus_view_fragments (
 view_id TEXT NOT NULL REFERENCES corpus_dataset_versions(id), id TEXT NOT NULL, ordinal INTEGER NOT NULL,
 split TEXT NOT NULL CHECK(split IN ('learn','dev_review','final_holdout','excluded')), group_id TEXT NOT NULL,
 data_json TEXT NOT NULL CHECK(json_valid(data_json)), PRIMARY KEY(view_id,id), UNIQUE(view_id,ordinal)
);
CREATE TABLE corpus_view_groups (
 view_id TEXT NOT NULL REFERENCES corpus_dataset_versions(id), id TEXT NOT NULL,
 split TEXT NOT NULL CHECK(split IN ('learn','dev_review','final_holdout','excluded')),
 data_json TEXT NOT NULL CHECK(json_valid(data_json)), PRIMARY KEY(view_id,id)
);
CREATE TABLE corpus_view_edges (
 view_id TEXT NOT NULL REFERENCES corpus_dataset_versions(id), ordinal INTEGER NOT NULL,
 data_json TEXT NOT NULL CHECK(json_valid(data_json)), PRIMARY KEY(view_id,ordinal)
);
CREATE INDEX corpus_view_split_targets ON corpus_view_messages(view_id,split,target_candidate);
'''
for table in NEW_TABLES:
    for action in ('UPDATE','DELETE'):
        EXTENSION += f'''
CREATE TRIGGER {table}_immutable_{action.lower()} BEFORE {action} ON {table}
BEGIN SELECT RAISE(ABORT,'corpus dataset snapshots are append only'); END;
'''
SCHEMA_HASH_INPUT = PRIOR_HASH_INPUT + '\n' + EXTENSION
