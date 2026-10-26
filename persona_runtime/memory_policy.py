"""Shared correction policy used by CLI, review, and conversation services."""
import json
from .models import LOCAL_USER_ID, timestamp


def exclude_sources(conn, persona_id, event_ids, reason):
    expanded = set(event_ids)
    for event_id in list(expanded):
        for row in conn.execute("SELECT user_event_id,reply_event_id FROM turns WHERE persona_id=? AND "
                                "(user_event_id=? OR reply_event_id=?)", (persona_id, event_id, event_id)):
            expanded.update(value for value in row if value)
    for event_id in expanded:
        conn.execute("INSERT OR IGNORE INTO source_exclusions VALUES (?,?,?,?,?)",
                     (event_id, persona_id, LOCAL_USER_ID, reason, timestamp()))
    # Cumulative summaries carry all their evidence, including parent evidence.
    for row in conn.execute("SELECT id,sources_json FROM summaries WHERE persona_id=? AND status='active'", (persona_id,)).fetchall():
        if expanded.intersection(json.loads(row["sources_json"])):
            conn.execute("UPDATE summaries SET status='stale',reason=? WHERE id=?", (reason, row["id"]))
    for row in conn.execute("SELECT id,sources_json FROM candidates WHERE persona_id=? AND status='pending'", (persona_id,)).fetchall():
        if expanded.intersection(s["event_id"] for s in json.loads(row["sources_json"])):
            conn.execute("UPDATE candidates SET status='stale' WHERE id=?", (row["id"],))


def exclude_memory(conn, memory, reason):
    ids = {memory["source_event_id"]}
    ids.update(r[0] for r in conn.execute("SELECT event_id FROM memory_sources WHERE memory_id=?", (memory["id"],)))
    exclude_sources(conn, memory["persona_id"], ids, reason)


def project_calls(conn, since):
    return (conn.execute("SELECT count(*) FROM attempts WHERE started_epoch>?", (since,)).fetchone()[0]
            + conn.execute("SELECT count(*) FROM organization_attempts WHERE started_epoch>?", (since,)).fetchone()[0])
