"""Additive M4 schema. JSON payloads are business data, never log records."""
TABLES5 = ("organization_tasks", "organization_attempts", "summaries", "candidates",
           "memory_sources", "candidate_reviews", "source_exclusions", "organization_progress",
           "automation", "context_uses", "daily_updates")
SCHEMA5 = """
CREATE TABLE organization_tasks (
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, persona_id TEXT NOT NULL REFERENCES personas(id),
 user_id TEXT NOT NULL REFERENCES users(id), session_id TEXT REFERENCES sessions(id), channel TEXT,
 mode TEXT NOT NULL, automatic INTEGER NOT NULL DEFAULT 0, fingerprint TEXT NOT NULL,
 payload_json TEXT NOT NULL, model_snapshot TEXT NOT NULL, status TEXT NOT NULL,
 error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX organization_queue ON organization_tasks(status,created_at);
CREATE TABLE organization_attempts (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES organization_tasks(id),
 started_epoch REAL NOT NULL, status TEXT NOT NULL, error_code TEXT, usage_json TEXT,
 model_snapshot TEXT NOT NULL
);
CREATE INDEX organization_quota ON organization_attempts(started_epoch);
CREATE TABLE summaries (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id), user_id TEXT NOT NULL REFERENCES users(id),
 session_id TEXT NOT NULL REFERENCES sessions(id), channel TEXT NOT NULL,
 task_id TEXT NOT NULL REFERENCES organization_tasks(id), previous_id TEXT REFERENCES summaries(id),
 revision INTEGER NOT NULL, start_row INTEGER NOT NULL, end_row INTEGER NOT NULL,
 items_json TEXT NOT NULL, sources_json TEXT NOT NULL, covered_json TEXT NOT NULL, status TEXT NOT NULL, reason TEXT,
 created_at TEXT NOT NULL
);
CREATE TABLE candidates (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id), user_id TEXT NOT NULL REFERENCES users(id),
 task_id TEXT NOT NULL REFERENCES organization_tasks(id), category TEXT NOT NULL,
 text TEXT NOT NULL, sources_json TEXT NOT NULL, conflicts_json TEXT NOT NULL, fingerprint TEXT NOT NULL,
 status TEXT NOT NULL, memory_id TEXT REFERENCES memories(id), created_at TEXT NOT NULL
);
CREATE INDEX candidate_scope ON candidates(persona_id,status);
CREATE TABLE memory_sources (
 memory_id TEXT NOT NULL REFERENCES memories(id), event_id TEXT NOT NULL REFERENCES events(id),
 quote TEXT NOT NULL, PRIMARY KEY(memory_id,event_id)
);
CREATE TABLE candidate_reviews (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id), user_id TEXT NOT NULL REFERENCES users(id),
 candidate_id TEXT NOT NULL UNIQUE REFERENCES candidates(id), action TEXT NOT NULL,
 memory_id TEXT REFERENCES memories(id), text TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE source_exclusions (
 event_id TEXT PRIMARY KEY REFERENCES events(id), persona_id TEXT NOT NULL REFERENCES personas(id),
 user_id TEXT NOT NULL REFERENCES users(id), reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE organization_progress (
 session_id TEXT PRIMARY KEY REFERENCES sessions(id), persona_id TEXT NOT NULL REFERENCES personas(id),
 user_id TEXT NOT NULL REFERENCES users(id), boundary INTEGER NOT NULL, cursor INTEGER NOT NULL,
 blocked_task TEXT REFERENCES organization_tasks(id)
);
CREATE TABLE automation (
 persona_id TEXT PRIMARY KEY REFERENCES personas(id), user_id TEXT NOT NULL REFERENCES users(id),
 enabled INTEGER NOT NULL, model_fingerprint TEXT NOT NULL, enabled_at TEXT NOT NULL
);
CREATE TABLE context_uses (
 attempt_id TEXT PRIMARY KEY REFERENCES attempts(id), turn_id TEXT NOT NULL REFERENCES turns(id),
 persona_id TEXT REFERENCES personas(id), user_id TEXT NOT NULL REFERENCES users(id),
 details_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE daily_updates (
 id TEXT PRIMARY KEY, persona_id TEXT REFERENCES personas(id), user_id TEXT NOT NULL REFERENCES users(id),
 status TEXT NOT NULL, next_epoch REAL NOT NULL, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""
