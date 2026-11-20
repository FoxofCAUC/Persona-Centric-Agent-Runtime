"""Structure 35. Scoped source revisions, indexed facts and per-turn local reads."""
from .storage import execute_schema

TABLES = ('local_knowledge_documents', 'local_knowledge_facts', 'local_knowledge_reads')
INDEX = 'local_knowledge_fts'
STUDY_TABLES = ('knowledge_study_commits','knowledge_study_reads')


def migrate_study(db):
    from .models import RuntimeErrorBase
    existing={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if existing.intersection(STUDY_TABLES) and not set(STUDY_TABLES).issubset(existing):
        raise RuntimeErrorBase('knowledge_study_schema_incomplete')
    execute_schema(db, """
    CREATE INDEX IF NOT EXISTS local_knowledge_same_document ON local_knowledge_documents(scope,url,sha256);
    CREATE TABLE IF NOT EXISTS knowledge_study_commits (
        turn_id TEXT PRIMARY KEY REFERENCES private_lookup_intents(turn_id),
        attempt_id TEXT UNIQUE REFERENCES private_lookup_calls(attempt_id), binding TEXT NOT NULL,
        status TEXT NOT NULL, fact_ids TEXT NOT NULL CHECK(json_valid(fact_ids)),
        rejected INTEGER NOT NULL, committed_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS knowledge_study_reads (
        turn_id TEXT PRIMARY KEY REFERENCES knowledge_study_commits(turn_id),
        query_hash TEXT NOT NULL, outcome TEXT NOT NULL, fact_ids TEXT NOT NULL CHECK(json_valid(fact_ids)),
        checked_at TEXT NOT NULL, selection_id TEXT REFERENCES private_reply_nodes(attempt_id),
        query_bound INTEGER NOT NULL CHECK(query_bound IN (0,1))
    );
    CREATE INDEX IF NOT EXISTS study_reads_lookup ON knowledge_study_reads(query_hash,outcome);
    """)
    for table in STUDY_TABLES:
        for operation in ('UPDATE','DELETE'):
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_{operation} BEFORE {operation} ON {table}
                           BEGIN SELECT RAISE(ABORT,'immutable knowledge study receipt'); END""")


def migrate(db):
    from .models import RuntimeErrorBase
    existing={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expected=set((*TABLES, INDEX))
    if existing & expected and not expected.issubset(existing):
        raise RuntimeErrorBase('local_knowledge_schema_incomplete')
    execute_schema(db, """
    CREATE TABLE IF NOT EXISTS local_knowledge_documents (
        id TEXT PRIMARY KEY, scope TEXT NOT NULL, persona_id TEXT REFERENCES personas(id),
        session_id TEXT REFERENCES sessions(id), consent_revision INTEGER NOT NULL, grant_hash TEXT,
        url TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, sha256 TEXT NOT NULL,
        source_kind TEXT NOT NULL, published_at TEXT, acquired_at TEXT NOT NULL, receipt TEXT NOT NULL,
        CHECK((consent_revision=0 AND grant_hash IS NULL) OR (consent_revision>0 AND grant_hash IS NOT NULL)),
        CHECK((scope='instance' AND persona_id IS NULL AND session_id IS NULL AND consent_revision=0)
           OR (scope='persona' AND persona_id IS NOT NULL AND session_id IS NULL AND consent_revision=0)
           OR (scope='session' AND persona_id IS NOT NULL AND session_id IS NOT NULL AND consent_revision>=0))
    );
    CREATE TABLE IF NOT EXISTS local_knowledge_facts (
        row_id INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL REFERENCES local_knowledge_documents(id),
        previous_id TEXT REFERENCES local_knowledge_facts(id), spec_json TEXT NOT NULL, entities TEXT NOT NULL, attributes TEXT NOT NULL,
        terms TEXT NOT NULL, withdrawn INTEGER NOT NULL DEFAULT 0 CHECK(withdrawn IN (0,1))
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS local_knowledge_fts USING fts5(terms,content='local_knowledge_facts');
    CREATE TRIGGER IF NOT EXISTS local_knowledge_add AFTER INSERT ON local_knowledge_facts BEGIN
        INSERT INTO local_knowledge_fts(rowid,terms) VALUES(new.rowid,new.terms);
    END;
    CREATE TRIGGER IF NOT EXISTS local_knowledge_doc_immutable BEFORE UPDATE ON local_knowledge_documents BEGIN
        SELECT RAISE(ABORT,'immutable knowledge source revision');
    END;
    CREATE TRIGGER IF NOT EXISTS local_knowledge_fact_immutable BEFORE UPDATE ON local_knowledge_facts
        WHEN new.rowid!=old.rowid OR new.id!=old.id OR new.document_id!=old.document_id OR new.previous_id IS NOT old.previous_id
        OR new.spec_json!=old.spec_json OR new.terms!=old.terms OR new.entities!=old.entities
        OR new.attributes!=old.attributes OR new.withdrawn<old.withdrawn BEGIN
        SELECT RAISE(ABORT,'immutable knowledge fact revision');
    END;
    CREATE TRIGGER IF NOT EXISTS local_knowledge_remove AFTER DELETE ON local_knowledge_facts BEGIN
        INSERT INTO local_knowledge_fts(local_knowledge_fts,rowid,terms) VALUES('delete',old.rowid,old.terms);
    END;
    CREATE TABLE IF NOT EXISTS local_knowledge_reads (
        turn_id TEXT PRIMARY KEY REFERENCES private_lookup_intents(turn_id),
        query_hash TEXT NOT NULL, outcome TEXT NOT NULL, fact_ids TEXT NOT NULL, checked_at TEXT NOT NULL,
        selection_id TEXT REFERENCES private_reply_nodes(attempt_id)
    );
    CREATE TRIGGER IF NOT EXISTS local_knowledge_read_immutable BEFORE UPDATE ON local_knowledge_reads BEGIN
        SELECT RAISE(ABORT,'immutable local read');
    END;
    CREATE TRIGGER IF NOT EXISTS local_knowledge_read_retained BEFORE DELETE ON local_knowledge_reads BEGIN
        SELECT RAISE(ABORT,'retained local read');
    END;
    """)
