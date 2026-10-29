"""Transactional v14 migration. Caller disables FK enforcement before BEGIN only."""
import re


def migrate(conn):
    # Rebuild CHECK-constrained parents using SQLite's documented create/copy/drop/rename
    # procedure. No writable_schema edits, no history rewriting, no invented evidence.
    triggers = list(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
    for name, _ in triggers:
        conn.execute('DROP TRIGGER "' + name + '"')
    for table in ("interaction_policies", "reply_batches"):
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
        indexes = [r[0] for r in conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,))]
        if "'natural_structured'" not in sql:
            sql = sql.replace("'context_structured'", "'context_structured','natural_structured'")
        if table == "reply_batches":
            sql = sql.replace("schema_version=1", "schema_version IN (1,2)")
        sql = re.sub(r'CREATE TABLE\s+"?' + table + r'"?', "CREATE TABLE m74_new_" + table, sql, count=1, flags=re.I)
        conn.execute(sql)
        conn.execute(f"INSERT INTO m74_new_{table} SELECT * FROM {table}")
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE m74_new_{table} RENAME TO {table}")
        for sql in indexes:
            conn.execute(sql)
    for _, sql in triggers:
        conn.execute(sql)
    for table, columns in {
        "turns": ("expression_snapshot TEXT", "new_input_event_id TEXT REFERENCES events(id)"),
        "reply_batches": ("behavior_intent TEXT", "fallback_reason TEXT"),
        "reply_segments": ("content_kind TEXT", "cancel_if_new_input INTEGER", "wait_ms INTEGER"),
        "reply_delivery_parts": ("wait_started_at TEXT", "wait_ms INTEGER", "cancel_reason TEXT", "trigger_event_id TEXT REFERENCES events(id)"),
    }.items():
        for column in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
