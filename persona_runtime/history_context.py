"""Revalidate the imported evidence actually used by an in-flight reply."""
import json

from .archive import binding, read_exact, turn_scope
from .models import RuntimeErrorBase


def capture(db, turn, details):
    if turn["channel"] != "desktop":
        return {}
    turn_ids = {g["turn_id"] for g in details.get("history_groups", [])}
    turn_ids.update((details.get("references") or {}).get("turn_ids", []))
    if not details.get("history_groups"):
        for ordinal in details.get("history_rows", []):
            row = db.execute("SELECT id FROM turns WHERE rowid=? AND session_id=?", (ordinal,turn["session_id"])).fetchone()
            if row:
                turn_ids.add(row[0])
    event_ids = set()
    memory_ids = {m["id"] for m in details.get("memories", [])}
    for unit in (details.get("recall_support") or {}).get("selected_ids", []):
        row = db.execute("SELECT kind,source_id FROM recall_support_units WHERE id=?", (unit,)).fetchone()
        if row and row[0] == "dialogue":
            event_ids.add(row[1])
        if row and row[0] == "memory":
            memory_ids.add(row[1])
    for mid in memory_ids:
        event_ids.update(row[0] for row in db.execute(
            "SELECT source_event_id FROM memories WHERE id=? UNION SELECT event_id FROM memory_sources WHERE memory_id=?", (mid,mid)))
    if details.get("summary_id"):
        row = db.execute("SELECT sources_json FROM summaries WHERE id=?", (details["summary_id"],)).fetchone()
        if row:
            event_ids.update(json.loads(row[0]))
    turn_ids.update(e.split(".")[1] for e in event_ids if isinstance(e,str) and e.startswith("imported.") and e.count(".")==2)
    if not turn_ids:
        return {}
    result = {}
    scope = turn_scope(db,turn)
    for tid in turn_ids:
        source = db.execute("SELECT session_id FROM turns WHERE id=? AND source_key LIKE 'imported.%'", (tid,)).fetchone()
        if source:
            group = read_exact(db,scope,{"kind":"turn","id":tid})
            if not group.get("usable"):
                raise RuntimeErrorBase("历史原话的使用范围已变化。")
            result[tid] = binding(group)["hash"]
    return result


def validate(db, turn, proof, *, recipient=None):
    if not proof:
        return
    from .import_history import history_allowed
    scope = turn_scope(db,turn)
    model = json.loads(turn["model_snapshot"]) if recipient is None else recipient
    for tid, expected in proof.items():
        group = read_exact(db,scope,{"kind":"turn","id":tid})
        if (not group.get("usable") or binding(group)["hash"] != expected
                or not history_allowed(db,group["session_id"],"dialogue",model)):
            raise RuntimeErrorBase("历史原话的使用范围已变化。")

def references_allowed(db,turn,recipient):
    from .references import prepare
    _, references = prepare(db,turn)
    proof = capture(db,turn,{"references":references})
    try:
        validate(db,turn,proof,recipient=recipient)
    except RuntimeErrorBase:
        return False
    return True


def time_block(history_items):
    """Index only retained imported bubbles. System occupies message position zero."""
    from .archive import encoded
    from .prompt_assembly import PromptBlock
    position, dates = 1, []
    for item in history_items:
        for offset, values in enumerate(item.get("source_times", [])):
            for recorded in values:
                if recorded:
                    dates.append({"message_index":position + offset, "recorded_at":recorded})
        position += len(item["pair"])
    if dates:
        return PromptBlock("history_recorded_at", "\n" + encoded({"history_recorded_at":dates}),
                           "import_history", "history-time-v1")
    return None

def project_speech(rows, native_bursts):
    """Render retained bubbles in order and map every original time to its message."""
    from itertools import groupby
    from .turn_input import render_input
    groups = groupby(rows, key=lambda e: e["role"]) if native_bursts else ((r["role"], [r]) for r in rows)
    pair, dates = [], []
    for speaker, events in groups:
        events = list(events)
        if speaker == "user":
            body = render_input(events)
        else:
            body = "\n\n".join(e["text"] for e in events)
        pair.append({"role":"user" if speaker=="user" else "assistant", "content":body})
        dates.append([e["created_at"] for e in events])
    return pair, dates


def fit_speech(rows, native_bursts, remaining):
    """Keep the most recent complete utterances that fit, including native wrappers."""
    retained, omitted = list(rows), []
    pair, dates = project_speech(retained,native_bursts)
    if remaining is not None and sum(len(m["content"]) for m in pair) > remaining:
        retained = []
        for event in reversed(rows):
            candidate = [event, *retained]
            rendered, _ = project_speech(candidate,native_bursts)
            if sum(len(m["content"]) for m in rendered) <= remaining:
                retained = candidate
            else:
                omitted.append(event["id"])
        pair, dates = project_speech(retained,native_bursts)
    return retained, pair, dates, omitted
