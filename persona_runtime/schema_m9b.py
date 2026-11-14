"""Legacy database layout required by schema-27 compatibility; no ranking logic."""

TABLES = (
    "recall_priority_policies",
    "recall_priority_units",
    "recall_events",
    "recall_search_explanations",
)

SCHEMA = """
CREATE TABLE recall_priority_policies (
    version TEXT PRIMARY KEY,
    data_json TEXT NOT NULL CHECK(json_valid(data_json)),
    created_at TEXT NOT NULL
);

CREATE TABLE recall_priority_units (
    id TEXT PRIMARY KEY,
    persona_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    domain TEXT NOT NULL CHECK(domain IN ('personal', 'public', 'imported')),
    category TEXT NOT NULL,
    scope_json TEXT NOT NULL CHECK(json_valid(scope_json)),
    evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)),
    importance REAL NOT NULL CHECK(importance >= 0 AND importance <= 1),
    anchor_priority REAL NOT NULL CHECK(anchor_priority >= 0 AND anchor_priority <= 1),
    anchor_epoch REAL NOT NULL,
    policy_version TEXT NOT NULL REFERENCES recall_priority_policies(version),
    active_layer TEXT NOT NULL CHECK(active_layer IN ('active', 'sleeping', 'cold')),
    last_recalled_epoch REAL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX recall_units_scope
    ON recall_priority_units(persona_id, domain, category, active_layer);

CREATE TABLE recall_events (
    id TEXT PRIMARY KEY,
    persona_id TEXT NOT NULL,
    unit_id TEXT NOT NULL REFERENCES recall_priority_units(id),
    logical_key TEXT NOT NULL,
    root_task_id TEXT,
    event_kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('completed', 'failed', 'cancelled', 'unknown')),
    effective_epoch REAL NOT NULL,
    applied_epoch REAL NOT NULL,
    pre_priority REAL NOT NULL CHECK(pre_priority >= 0 AND pre_priority <= 1),
    post_priority REAL NOT NULL CHECK(post_priority >= 0 AND post_priority <= 1),
    policy_version TEXT NOT NULL REFERENCES recall_priority_policies(version),
    evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)),
    reinforced INTEGER NOT NULL CHECK(reinforced IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE(persona_id, unit_id, logical_key)
);

CREATE INDEX recall_events_unit ON recall_events(unit_id, effective_epoch);

CREATE TABLE recall_search_explanations (
    id TEXT PRIMARY KEY,
    persona_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    root_task_id TEXT,
    policy_version TEXT NOT NULL REFERENCES recall_priority_policies(version),
    query_json TEXT NOT NULL CHECK(json_valid(query_json)),
    candidate_budget INTEGER NOT NULL CHECK(candidate_budget > 0),
    requested_limit INTEGER NOT NULL CHECK(requested_limit > 0),
    searched_layers_json TEXT NOT NULL CHECK(json_valid(searched_layers_json)),
    deep_search_reason TEXT NOT NULL,
    cold_status TEXT NOT NULL CHECK(cold_status IN (
        'not_searched', 'no_result', 'permission_denied', 'source_withdrawn', 'found', 'unknown'
    )),
    selected_ids_json TEXT NOT NULL CHECK(json_valid(selected_ids_json)),
    explanation_json TEXT NOT NULL CHECK(json_valid(explanation_json)),
    created_at TEXT NOT NULL
);

CREATE INDEX recall_search_scope
    ON recall_search_explanations(persona_id, user_id, created_at);
"""


def migrate(conn):
    """Create the package schema in an explicitly isolated database only."""
    from .storage import execute_schema

    execute_schema(conn, SCHEMA)
