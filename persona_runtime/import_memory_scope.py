"""Resolve imported evidence for organization, including earlier facts and summaries."""
import json
from datetime import datetime

from .archive import find_turn, read_exact, turn_scope
from .models import RuntimeErrorBase


def imported(turn):
    return turn["source_key"].startswith("imported.")


def source_group(db, selected, turn, *, model=None, purpose="memory"):
    target = {"kind": "turn", "id": turn["id"]}
    if not imported(turn):
        return read_exact(db, selected, target)
    if turn_scope(db, turn) != selected:
        return {'status': 'not_found'}
    from .import_history import group
    return group(db, turn, selected, target, purpose=purpose, providers=model)


def accepted_turns(db, turns, model, memory_enabled):
    from .import_history import history_allowed
    decisions = {}
    for turn in turns:
        if imported(turn):
            sid = turn["session_id"]
            if sid not in decisions:
                decisions[sid] = memory_enabled and bool(model.get('encrypted_key')) and history_allowed(db, sid, "memory", providers=model)
            if not decisions[sid]:
                continue
        yield turn


def allowed_events(db, ids, model, enabled=True):
    from .import_history import history_allowed
    sessions = set()
    for eid in ids:
        if not isinstance(eid, str) or not eid.startswith("imported."):
            continue
        if not enabled or not model.get('encrypted_key'):
            return False
        source = find_turn(db, "event", eid)
        if not source:
            return False
        sessions.add(source["session_id"])
    return all(history_allowed(db, sid, "memory", providers=model) for sid in sessions)


def memory_source_ids(db, mid):
    return [row[0] for row in db.execute("""SELECT source_event_id FROM memories WHERE id=?
               UNION SELECT event_id FROM memory_sources WHERE memory_id=?""", (mid, mid))]


def allowed_memory(db, mid, model, enabled=True):
    return allowed_events(db, memory_source_ids(db, mid), model, enabled)


def allowed_summary(db, summary, model, enabled=True):
    return not summary or allowed_events(db, json.loads(summary["sources_json"]), model, enabled)


def has_history(db, payload):
    if any(event["id"].startswith("imported.") for event in payload.get("events", ())):
        return True
    previous = payload.get('previous')
    if previous and any(eid.startswith('imported.') for eid in json.loads(previous['sources_json'])):
        return True
    return any(eid.startswith("imported.") for key in ("existing_memories","outdated_claims")
               for m in payload.get(key, ()) for eid in memory_source_ids(db, m["id"]))


def check_payload(db, payload, model, memory_enabled):
    """Recheck the submitted material against its intended recipient at claim and publication."""
    from .import_history import chronological

    imported_events = {}
    for event in payload.get("events", ()):
        eid = event["id"]
        if eid.startswith("imported."):
            if not payload.get('automatic_memory'):
                raise RuntimeErrorBase('source_expired')
            source = find_turn(db, "event", eid)
            if not memory_enabled or not model.get('encrypted_key') or not source or source["persona_id"] != payload.get("persona_id"):
                raise RuntimeErrorBase("history_memory_authorization_required")
            if source["id"] not in imported_events:
                projected = source_group(db, turn_scope(db, source), source, model=model)
                if projected["status"] != "found":
                    raise RuntimeErrorBase("history_memory_authorization_required")
                imported_events[source["id"]] = {row["id"]: row for row in chronological(projected)}
            original = imported_events[source["id"]].get(eid)
            if not original or original["text"] != event["text"] or (
                    (original["role"] == "user") != (event["role"] == "user")):
                raise RuntimeErrorBase("source_expired")
    memory_ids = {m["id"] for key in ("existing_memories", "outdated_claims") for m in payload.get(key, ())}
    if any(not allowed_memory(db, mid, model, memory_enabled) for mid in memory_ids) or not allowed_summary(db, payload.get("previous"), model, memory_enabled):
        raise RuntimeErrorBase("history_memory_authorization_required")


def historical_conflict(db, sources, memories):
    """An old or undated import cannot displace a newer supported value."""
    ids = [s["source_id"] for s in sources]
    if not memories or not ids or not any(i.startswith("imported.") for i in ids):
        return False
    from .import_history import source_time

    def latest(event_ids):
        times = [source_time(db, i) for i in event_ids]
        if not times or not all(times):
            return None
        try:
            parsed = [datetime.fromisoformat(t) for t in times]
            return max(parsed) if all(t.tzinfo is not None for t in parsed) else None
        except (TypeError, ValueError, OverflowError):
            return None

    correction = latest(ids)
    if correction is None:
        return True
    for memory in memories:
        previous = latest(memory_source_ids(db, memory["id"]))
        if previous is None or correction <= previous:
            return True
        if memory.get("valid_from") and correction <= datetime.fromisoformat(memory["valid_from"]):
            return True
        manual = db.execute("""SELECT m.updated_at FROM memories m JOIN memory_facts f ON f.memory_id=m.id
                             WHERE m.id=? AND json_extract(f.data_json,'$.manual_authority')=1""", (memory["id"],)).fetchone()
        if manual and correction <= datetime.fromisoformat(manual[0]):
            return True
    return False
