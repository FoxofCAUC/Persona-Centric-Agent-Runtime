"""Shared correction policy used by CLI, review, and conversation services."""
import json
from .models import LOCAL_USER_ID, timestamp


def exclude_sources(conn, persona_id, event_ids, reason):
    expanded = set(event_ids)
    from .turn_input import load_turn_sources
    for row in conn.execute("SELECT * FROM turns WHERE persona_id=?",(persona_id,)):
        ids=set(load_turn_sources(conn,dict(row)))
        if expanded.intersection(ids):expanded.update(ids)
    for event_id in expanded:
        conn.execute("INSERT OR IGNORE INTO source_exclusions VALUES (?,?,?,?,?)",
                     (event_id, persona_id, LOCAL_USER_ID, reason, timestamp()))
        if reason!='memory_revised':
            # A later withdrawal must supersede the narrower raw-source
            # suppression caused by a human correction.
            conn.execute("UPDATE source_exclusions SET reason=?,created_at=? WHERE event_id=? AND reason='memory_revised'",
                         (reason,timestamp(),event_id))
        conn.execute("UPDATE relationship_proposals SET status='stale' WHERE source_event_id=? AND status='pending'", (event_id,))
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
    compiler = conn.execute("SELECT count(*) FROM disposition_compile_attempts WHERE started_epoch>?",(since,)).fetchone()[0] if conn.execute("SELECT 1 FROM sqlite_master WHERE name='disposition_compile_attempts'").fetchone() else 0
    return (compiler + conn.execute("SELECT count(*) FROM attempts WHERE started_epoch>?", (since,)).fetchone()[0]
            + conn.execute("SELECT count(*) FROM organization_attempts WHERE started_epoch>?", (since,)).fetchone()[0]
            + conn.execute("SELECT count(*) FROM character_selection_attempts WHERE started_epoch>?", (since,)).fetchone()[0])


def project_capacity_used(conn, since, exclude_turn=None):
    import time
    return project_calls(conn,since)+conn.execute(
        "SELECT count(*) FROM quota_reservations WHERE state='active' AND expires_epoch>? AND (? IS NULL OR turn_id!=?)",
        (time.time(),exclude_turn,exclude_turn)).fetchone()[0]
