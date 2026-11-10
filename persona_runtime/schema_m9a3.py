"""Versioned public documents and knowledge stages owned by the existing executor."""

TABLES = ('knowledge_sources', 'knowledge_targets', 'knowledge_pages',
          'knowledge_documents', 'knowledge_fetch_jobs', 'knowledge_batches',
          'knowledge_claims', 'knowledge_evidence')
SCHEMA = """
CREATE TABLE knowledge_sources (
 id TEXT PRIMARY KEY,adapter TEXT NOT NULL,endpoint TEXT NOT NULL,
 license_url TEXT NOT NULL,source_group TEXT NOT NULL
);
INSERT INTO knowledge_sources VALUES
 ('wikipedia_zh','mediawiki','https://zh.wikipedia.org/w/api.php',
  'https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use','wikimedia'),
 ('wikidata','wikibase','https://www.wikidata.org/w/api.php',
  'https://www.wikidata.org/wiki/Wikidata:Licensing','wikimedia');
CREATE TABLE knowledge_targets (
 id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES knowledge_sources(id),
 public_id TEXT NOT NULL,page_id TEXT REFERENCES knowledge_pages(id),
 bindings_json TEXT NOT NULL,last_used REAL NOT NULL,last_checked REAL,
 state TEXT NOT NULL,UNIQUE(source_id,public_id)
);
CREATE TABLE knowledge_pages (
 id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES knowledge_sources(id),
 upstream_id TEXT NOT NULL,title TEXT NOT NULL,url TEXT NOT NULL,
 head_document_id TEXT REFERENCES knowledge_documents(id),latest_revision INTEGER NOT NULL,
 state TEXT NOT NULL,last_checked REAL NOT NULL,UNIQUE(source_id,upstream_id)
);
CREATE TABLE knowledge_documents (
 id TEXT PRIMARY KEY,page_id TEXT NOT NULL REFERENCES knowledge_pages(id),
 upstream_revision INTEGER NOT NULL,sha256 TEXT NOT NULL,body TEXT NOT NULL,
 raw_json TEXT NOT NULL,metadata_json TEXT NOT NULL,upstream_at TEXT,
 fetched_epoch REAL NOT NULL,UNIQUE(page_id,upstream_revision)
);
CREATE INDEX knowledge_document_content ON knowledge_documents(page_id,sha256);
CREATE TABLE knowledge_fetch_jobs (
 id TEXT PRIMARY KEY,target_id TEXT NOT NULL REFERENCES knowledge_targets(id),
 owner_persona TEXT NOT NULL REFERENCES personas(id),automatic INTEGER NOT NULL,
 state TEXT NOT NULL,reason TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
 ready_after REAL NOT NULL,created_epoch REAL NOT NULL,lease_json TEXT,
 lease_until REAL NOT NULL DEFAULT 0,error TEXT,document_id TEXT REFERENCES knowledge_documents(id)
);
CREATE INDEX knowledge_fetch_ready ON knowledge_fetch_jobs(state,ready_after,created_epoch);
CREATE TABLE knowledge_batches (
 id TEXT PRIMARY KEY,document_id TEXT NOT NULL REFERENCES knowledge_documents(id),
 owner_persona TEXT NOT NULL REFERENCES personas(id),work_key TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL,extract_task_id TEXT REFERENCES organization_tasks(id),
 review_task_id TEXT REFERENCES organization_tasks(id),data_json TEXT NOT NULL,created_epoch REAL NOT NULL
);
CREATE TABLE knowledge_claims (
 id TEXT PRIMARY KEY,page_id TEXT NOT NULL REFERENCES knowledge_pages(id),
 document_id TEXT NOT NULL REFERENCES knowledge_documents(id),
 batch_id TEXT NOT NULL REFERENCES knowledge_batches(id),claim_key TEXT NOT NULL,
 subject TEXT NOT NULL,predicate TEXT NOT NULL,value TEXT NOT NULL,kind TEXT NOT NULL,
 state TEXT NOT NULL,valid_from TEXT,valid_until TEXT,previous_id TEXT REFERENCES knowledge_claims(id),
 created_epoch REAL NOT NULL
);
CREATE INDEX knowledge_claim_attribute ON knowledge_claims(claim_key,state);
CREATE TABLE knowledge_evidence (
 claim_id TEXT PRIMARY KEY REFERENCES knowledge_claims(id),
 document_id TEXT NOT NULL REFERENCES knowledge_documents(id),
 start_character INTEGER NOT NULL,end_character INTEGER NOT NULL,quote TEXT NOT NULL
);
"""


def migrate(conn):
    from .storage import execute_schema
    execute_schema(conn, SCHEMA)
