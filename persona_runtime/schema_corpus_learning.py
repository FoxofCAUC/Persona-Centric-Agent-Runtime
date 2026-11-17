"""Independent learning ledger schema 1; historical corpus snapshots stay immutable."""

SCHEMA_VERSION = 1
APPLICATION_ID = 0x50434C31
TABLES = ('learning_meta','learning_approvals','learning_jobs','learning_proposals','learning_operations')
SCHEMA = '''
CREATE TABLE learning_meta(singleton INTEGER PRIMARY KEY CHECK(singleton=1),id TEXT NOT NULL,schema_hash TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE learning_approvals(
 id TEXT PRIMARY KEY,approval_hash TEXT NOT NULL UNIQUE,approval_json TEXT NOT NULL CHECK(json_valid(approval_json)),
 binding_json TEXT NOT NULL CHECK(json_valid(binding_json)),binding_hash TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('active','revoked')),max_dispatches INTEGER NOT NULL CHECK(max_dispatches=2),created_at TEXT NOT NULL);
CREATE TABLE learning_jobs(
 id TEXT PRIMARY KEY,approval_id TEXT NOT NULL REFERENCES learning_approvals(id),position INTEGER NOT NULL CHECK(position IN (1,2)),
 position_key TEXT NOT NULL UNIQUE,state TEXT NOT NULL CHECK(state IN ('prepared','dispatched','completed','failed','unknown','stale','cancelled')),
 request_json TEXT NOT NULL CHECK(json_valid(request_json)),request_hash TEXT NOT NULL,
 model_json TEXT NOT NULL CHECK(json_valid(model_json)),sources_json TEXT NOT NULL CHECK(json_valid(sources_json)),
 sources_hash TEXT NOT NULL,attempt_id TEXT UNIQUE,started_epoch REAL,cancelled_at TEXT,
 raw_response BLOB,raw_response_hash TEXT,http_status INTEGER,transport_json TEXT,usage_json TEXT,result_json TEXT,error_code TEXT,
 created_at TEXT NOT NULL,UNIQUE(approval_id,position));
CREATE TABLE learning_proposals(
 id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES learning_jobs(id),ordinal INTEGER NOT NULL,
 data_json TEXT NOT NULL CHECK(json_valid(data_json)),data_hash TEXT NOT NULL,state TEXT NOT NULL CHECK(state='pending'),UNIQUE(job_id,ordinal));
CREATE TABLE learning_operations(operation_id TEXT PRIMARY KEY,action TEXT NOT NULL,request_hash TEXT NOT NULL,result_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TRIGGER learning_approval_contract_immutable BEFORE UPDATE ON learning_approvals
 WHEN NEW.id IS NOT OLD.id OR NEW.approval_hash IS NOT OLD.approval_hash OR NEW.approval_json IS NOT OLD.approval_json
 OR NEW.binding_json IS NOT OLD.binding_json OR NEW.binding_hash IS NOT OLD.binding_hash OR NEW.max_dispatches IS NOT OLD.max_dispatches
 OR (OLD.state='revoked' AND NEW.state!='revoked')
 BEGIN SELECT RAISE(ABORT,'learning approval is immutable'); END;
CREATE TRIGGER learning_job_contract_immutable BEFORE UPDATE ON learning_jobs
 WHEN NEW.id IS NOT OLD.id OR NEW.approval_id IS NOT OLD.approval_id OR NEW.position IS NOT OLD.position
 OR NEW.position_key IS NOT OLD.position_key OR NEW.request_json IS NOT OLD.request_json OR NEW.request_hash IS NOT OLD.request_hash
 OR NEW.model_json IS NOT OLD.model_json OR NEW.sources_json IS NOT OLD.sources_json OR NEW.sources_hash IS NOT OLD.sources_hash
 OR (OLD.attempt_id IS NOT NULL AND (NEW.attempt_id IS NOT OLD.attempt_id OR NEW.started_epoch IS NOT OLD.started_epoch))
 OR (OLD.raw_response IS NOT NULL AND (NEW.raw_response IS NOT OLD.raw_response OR NEW.raw_response_hash IS NOT OLD.raw_response_hash OR NEW.http_status IS NOT OLD.http_status))
 BEGIN SELECT RAISE(ABORT,'learning job contract or first receipt is immutable'); END;
'''
for table in ('learning_approvals','learning_jobs','learning_proposals'):
    SCHEMA += f"\nCREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT,'learning history is append only'); END;\n"
SCHEMA += "\nCREATE TRIGGER learning_proposals_no_update BEFORE UPDATE ON learning_proposals BEGIN SELECT RAISE(ABORT,'learning proposal is immutable'); END;\n"
