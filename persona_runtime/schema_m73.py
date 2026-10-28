"""Additive session lifecycle metadata; canonical events never move."""
TABLES13 = ('session_lifecycle', 'session_heads', 'session_events', 'session_packages',
            'session_triggers', 'session_operations', 'legacy_delivery_dispositions',
            'topic_segments', 'conversation_threads', 'thread_segments', 'topic_versions')
SCHEMA13 = """
CREATE TABLE session_lifecycle (
 session_id TEXT PRIMARY KEY REFERENCES sessions(id), persona_id TEXT NOT NULL REFERENCES personas(id),
 user_id TEXT NOT NULL REFERENCES users(id), channel TEXT, bot TEXT NOT NULL DEFAULT '', peer TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL CHECK(state IN ('active','sealed','legacy_unmanaged')), revision INTEGER NOT NULL,
 activity_revision INTEGER NOT NULL DEFAULT 1,
 last_activity_at TEXT, observation_started_at TEXT, sealed_at TEXT, seal_reason TEXT,
 continued_from_session_id TEXT REFERENCES sessions(id), created_at TEXT NOT NULL
);
CREATE TABLE session_heads (
 persona_id TEXT NOT NULL REFERENCES personas(id), user_id TEXT NOT NULL REFERENCES users(id),
 channel TEXT NOT NULL, bot TEXT NOT NULL, peer TEXT NOT NULL,
 session_id TEXT NOT NULL UNIQUE REFERENCES session_lifecycle(session_id),
 PRIMARY KEY(persona_id,user_id,channel,bot,peer)
);
CREATE TABLE session_events (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL, user_id TEXT NOT NULL,
 session_id TEXT NOT NULL REFERENCES session_lifecycle(session_id), kind TEXT NOT NULL,
 revision INTEGER NOT NULL, data_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE session_packages (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL, user_id TEXT NOT NULL,
 session_id TEXT NOT NULL UNIQUE REFERENCES session_lifecycle(session_id),
 manifest_json TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE session_triggers (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL, user_id TEXT NOT NULL,
 package_id TEXT NOT NULL REFERENCES session_packages(id), kind TEXT NOT NULL,
 pipeline_version TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(package_id,kind,pipeline_version)
);
CREATE TABLE session_operations (
 operation_id TEXT PRIMARY KEY, persona_id TEXT NOT NULL, user_id TEXT NOT NULL,
 action TEXT NOT NULL, request_hash TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE legacy_delivery_dispositions (
 turn_id TEXT PRIMARY KEY REFERENCES turns(id), persona_id TEXT NOT NULL, user_id TEXT NOT NULL,
 reason TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE topic_segments (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL, user_id TEXT NOT NULL,
 session_id TEXT NOT NULL REFERENCES session_lifecycle(session_id), package_id TEXT NOT NULL REFERENCES session_packages(id),
 scope_json TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, turn_ids_json TEXT NOT NULL,
 revision INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('active','archived')), created_at TEXT NOT NULL
);
CREATE TABLE conversation_threads (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL, user_id TEXT NOT NULL, scope_json TEXT NOT NULL,
 title TEXT NOT NULL, description TEXT NOT NULL, revision INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('active','archived')), created_at TEXT NOT NULL
);
CREATE TABLE thread_segments (
 thread_id TEXT NOT NULL REFERENCES conversation_threads(id), segment_id TEXT NOT NULL UNIQUE REFERENCES topic_segments(id),
 persona_id TEXT NOT NULL, user_id TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(thread_id,segment_id)
);
CREATE TABLE topic_versions (
 id TEXT PRIMARY KEY, persona_id TEXT NOT NULL, user_id TEXT NOT NULL, object_kind TEXT NOT NULL,
 object_id TEXT NOT NULL, revision INTEGER NOT NULL, data_json TEXT NOT NULL, actor TEXT NOT NULL,
 operation_id TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(object_kind,object_id,revision)
);
ALTER TABLE summaries ADD COLUMN source_refs_json TEXT;
CREATE INDEX session_activity ON session_lifecycle(state,persona_id);
CREATE INDEX session_event_order ON session_events(session_id,revision);
CREATE INDEX topic_session ON topic_segments(session_id,state);
CREATE TRIGGER session_event_no_append BEFORE INSERT ON events WHEN EXISTS(SELECT 1 FROM session_lifecycle WHERE session_id=NEW.session_id AND state='sealed') BEGIN SELECT RAISE(ABORT,'session_sealed'); END;
CREATE TRIGGER session_no_retry BEFORE UPDATE OF status ON turns WHEN NEW.status IN ('queued','running') AND EXISTS(SELECT 1 FROM session_lifecycle WHERE session_id=NEW.session_id AND state='sealed') BEGIN SELECT RAISE(ABORT,'session_sealed'); END;
CREATE TRIGGER session_input_activity AFTER INSERT ON events BEGIN
 UPDATE session_lifecycle SET revision=revision+1,activity_revision=activity_revision+CASE WHEN NEW.role='user' THEN 1 ELSE 0 END,last_activity_at=CASE WHEN NEW.role='user' THEN NEW.created_at ELSE last_activity_at END WHERE session_id=NEW.session_id AND state='active'; END;
CREATE TRIGGER session_turn_activity AFTER UPDATE OF status ON turns WHEN OLD.status!=NEW.status AND NEW.status IN ('completed','failed','interrupted','cancelled') BEGIN
 UPDATE session_lifecycle SET revision=revision+1,activity_revision=activity_revision+1,last_activity_at=NEW.updated_at WHERE session_id=NEW.session_id AND state='active'; END;
CREATE TRIGGER session_part_activity AFTER UPDATE OF state ON reply_delivery_parts WHEN OLD.state!=NEW.state BEGIN
 UPDATE session_lifecycle SET revision=revision+1,activity_revision=activity_revision+1,last_activity_at=NEW.updated_at WHERE session_id=(SELECT session_id FROM turns WHERE id=NEW.turn_id) AND state='active'; END;
CREATE TRIGGER session_legacy_activity AFTER UPDATE OF state ON deliveries WHEN OLD.state!=NEW.state BEGIN
 UPDATE session_lifecycle SET revision=revision+1,activity_revision=activity_revision+1,last_activity_at=NEW.updated_at WHERE session_id=(SELECT session_id FROM turns WHERE id=NEW.turn_id) AND state='active'; END;
CREATE TRIGGER session_exclusion_revision AFTER INSERT ON source_exclusions BEGIN
 UPDATE session_lifecycle SET revision=revision+1 WHERE session_id=(SELECT session_id FROM events WHERE id=NEW.event_id) AND state='active'; END;
CREATE TRIGGER session_event_revision AFTER UPDATE ON events BEGIN
 UPDATE session_lifecycle SET revision=revision+1 WHERE session_id=NEW.session_id AND state='active'; END;
CREATE TRIGGER session_part_revision AFTER INSERT ON reply_delivery_parts BEGIN
 UPDATE session_lifecycle SET revision=revision+1 WHERE session_id=(SELECT session_id FROM turns WHERE id=NEW.turn_id) AND state='active'; END;
CREATE TRIGGER session_turn_revision AFTER UPDATE ON turns BEGIN
 UPDATE session_lifecycle SET revision=revision+1 WHERE session_id=NEW.session_id AND state='active'; END;
CREATE TRIGGER session_part_content_revision AFTER UPDATE OF text,text_hash ON reply_delivery_parts BEGIN
 UPDATE session_lifecycle SET revision=revision+1 WHERE session_id=(SELECT session_id FROM turns WHERE id=NEW.turn_id) AND state='active'; END;
CREATE TRIGGER session_exclusion_update AFTER UPDATE ON source_exclusions BEGIN
 UPDATE session_lifecycle SET revision=revision+1 WHERE session_id=(SELECT session_id FROM events WHERE id=NEW.event_id) AND state='active'; END;
CREATE TRIGGER session_exclusion_delete AFTER DELETE ON source_exclusions BEGIN
 UPDATE session_lifecycle SET revision=revision+1 WHERE session_id=(SELECT session_id FROM events WHERE id=OLD.event_id) AND state='active'; END;
CREATE TRIGGER session_turn_insert AFTER INSERT ON turns BEGIN
 UPDATE session_lifecycle SET revision=revision+1 WHERE session_id=NEW.session_id AND state='active'; END;
CREATE TRIGGER session_lifecycle_owner BEFORE INSERT ON session_lifecycle WHEN NOT EXISTS(SELECT 1 FROM sessions WHERE id=NEW.session_id AND persona_id=NEW.persona_id AND user_id=NEW.user_id) BEGIN SELECT RAISE(ABORT,'session_owner_mismatch'); END;
CREATE TRIGGER session_head_owner BEFORE INSERT ON session_heads WHEN NOT EXISTS(SELECT 1 FROM session_lifecycle WHERE session_id=NEW.session_id AND persona_id=NEW.persona_id AND user_id=NEW.user_id AND channel=NEW.channel AND bot=NEW.bot AND peer=NEW.peer AND state='active') BEGIN SELECT RAISE(ABORT,'session_scope_mismatch'); END;
CREATE TRIGGER session_head_update BEFORE UPDATE ON session_heads WHEN NOT EXISTS(SELECT 1 FROM session_lifecycle WHERE session_id=NEW.session_id AND persona_id=NEW.persona_id AND user_id=NEW.user_id AND channel=NEW.channel AND bot=NEW.bot AND peer=NEW.peer AND state='active') BEGIN SELECT RAISE(ABORT,'session_scope_mismatch'); END;
CREATE TRIGGER session_no_unseal BEFORE UPDATE OF state ON session_lifecycle WHEN OLD.state='sealed' AND NEW.state!='sealed' BEGIN SELECT RAISE(ABORT,'sealed_session_immutable'); END;
CREATE TRIGGER thread_scope_guard BEFORE INSERT ON thread_segments WHEN NOT EXISTS(SELECT 1 FROM conversation_threads t JOIN topic_segments s ON t.scope_json=s.scope_json WHERE t.id=NEW.thread_id AND s.id=NEW.segment_id AND t.persona_id=NEW.persona_id AND t.user_id=NEW.user_id) BEGIN SELECT RAISE(ABORT,'topic_scope_mismatch'); END;
"""
for table, key, parent, parent_key in (('session_packages','session_id','session_lifecycle','session_id'),
                                    ('session_events','session_id','session_lifecycle','session_id'),
                                    ('session_triggers','package_id','session_packages','id')):
    SCHEMA13 += f"\nCREATE TRIGGER {table}_owner BEFORE INSERT ON {table} WHEN NOT EXISTS(SELECT 1 FROM {parent} WHERE {parent_key}=NEW.{key} AND persona_id=NEW.persona_id AND user_id=NEW.user_id) BEGIN SELECT RAISE(ABORT,'session_owner_mismatch'); END;\n"
for table in ('session_packages', 'session_events', 'session_operations', 'topic_versions', 'legacy_delivery_dispositions'):
    for action in ('UPDATE', 'DELETE'):
        SCHEMA13 += f"\nCREATE TRIGGER {table}_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'session audit is append only'); END;\n"


def initialize(conn):
    from .sessions import infer_scope, register
    for row in conn.execute('SELECT * FROM sessions').fetchall():
        selected = infer_scope(conn, dict(row))
        active = selected and conn.execute('SELECT 1 FROM channel_sessions WHERE session_id=? AND channel=?',
                                          (row['id'], selected['channel'])).fetchone()
        register(conn, dict(row), selected, reason='migration', managed=bool(active))
