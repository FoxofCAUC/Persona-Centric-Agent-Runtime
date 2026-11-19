"""Scoped history projections. Local bubbles retain the original source separately from admissible speech."""
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
from pathlib import Path

from .archive import encoded
from .config import ConfigStore
from .models import LOCAL_USER_ID, identifier, timestamp
from .paths import RuntimePaths
from .scene_policy import read, save
from .sessions import select

PREFIX = "imported."
META = "persona.import_history."


def provider(model):
    return {k: model.get(k) or "" for k in ("base_url", "name")}


def configured_providers(paths):
    from .knowledge_routing import configured_model
    cfg = ConfigStore(paths).load()
    org = cfg["organization"]
    try:
        jev = configured_model(paths)
    except (OSError, ValueError):
        jev = {}
    return {"dialogue": provider(cfg["model"]), "memory": provider(cfg["model"] if org["reuse_chat"] else org["model"]),
            "jev": provider(jev)}


@lru_cache(maxsize=32)
def _configured_snapshot(home, config_stamp, jev_stamp):
    return configured_providers(RuntimePaths(home))


def _stamp(path):
    try:
        value = path.stat()
        return value.st_ino, value.st_mtime_ns, value.st_size
    except FileNotFoundError:
        return None


def live_provider(db, purpose):
    path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    if path.name != "runtime.sqlite3" or path.parent.name != "database":
        return None
    home = path.parents[2]
    model_file = home/"config/workbench.json"
    jev_file = home/"private/knowledge-router/config.json"
    return _configured_snapshot(home, _stamp(model_file), _stamp(jev_file))[purpose]


def history_allowed(db, session_id, purpose, providers=None):
    meta = read(db, META + session_id)
    if not meta:
        return not db.execute("SELECT 1 FROM events WHERE session_id=? AND id LIKE 'imported.%' LIMIT 1",
                              (session_id,)).fetchone()
    if (not isinstance(meta,dict) or meta.get("version") != 2 or meta.get("withdrawn") is not False
            or meta.get("migration_requires_review") or {meta.get("self_role"), meta.get("role")} != {"A","B"}
            or not isinstance(meta.get("providers"),dict)):
        return False
    if purpose == "evidence":
        return True
    if purpose not in ("dialogue", "memory", "jev") or purpose == "memory" and not meta.get("organize"):
        return False
    approved = meta["providers"].get(purpose)
    if not approved or not all(approved.values()):
        return False
    try:
        current = live_provider(db, purpose)
        return current == approved and (providers is None or provider(providers) == approved)
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        return False


def recorded_time(raw, offset):
    if raw:
        try:
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone(timedelta(minutes=offset)))
            return parsed.isoformat()
        except (ValueError, OverflowError):
            pass
    return None


def source_detail(row, target, offset):
    speaker_role = "note" if row["role"] == "-" else "persona" if row["role"] == target else "user"
    detail = {"number": int(row["id"]), "speaker": row["speaker"], "line": row["line"], "start": row["start"],
              "end": row["end"], "sections": row.get("sections", []), "role": speaker_role,
              "time": recorded_time(row["date"], offset), "raw_time": row.get("raw_date", row["date"])}
    if row["original"] != row["text"]:
        detail["original"] = row["original"]
    return detail


def restore(db, pid, document, target, rows, *, meta=None):
    sid = select(db, pid, "desktop")
    at = timestamp()
    offset = meta["timezone_offset"] if meta else 480
    tid = identifier()
    pending = []
    chars = 0
    last_speaker = None

    def flush():
        if not pending:
            return
        users = [r for r in pending if r[1] == "user"]
        replies = [r for r in pending if r[1] == "persona"]
        origin = {"import_document":document,"target_role":target, "first_seq":pending[0][2],
                  "last_seq":pending[-1][2],"imported_at":at,
                  "sources":{r[0]:r[3] for r in pending}}
        db.execute("""INSERT INTO turns(id,source_key,persona_id,session_id,channel,user_event_id,reply_event_id,
            status,model_snapshot,created_at,updated_at,origin_json)
            VALUES(?,?,?,?,'desktop',?,?,'completed','{}',?,?,?)""",
            (tid, PREFIX+tid, pid, sid, users[0][0] if users else None, replies[-1][0] if replies else None,
             pending[0][3]["time"] or at, at, encoded(origin)))

    for row in rows:
        if row["role"] == "-" and row["speaker"].casefold() not in ("系统","系统消息","system","通知"):
            continue
        detail = source_detail(row,target,offset)
        body = row["text"]
        role = detail["role"] if body.strip() else "note"
        day = (detail["time"] or "")[:10]
        if pending and ((role == "user" and last_speaker == "persona") or day != (pending[-1][3]["time"] or "")[:10]
                        or len(pending) >= 32 or chars + len(body) > 3000):
            flush()
            pending, tid, chars, last_speaker = [], identifier(), 0, None
        eid = PREFIX + tid + "." + row["id"]
        seq = db.execute("INSERT INTO events(id,session_id,persona_id,user_id,role,text,created_at) VALUES(?,?,?,?,?,?,?)",
                         (eid,sid,pid,LOCAL_USER_ID,role,body if meta is not None and role!="note" else row["original"],detail["time"] or at)).lastrowid
        pending.append((eid,role,seq,detail))
        chars += len(body)
        if role != "note":
            last_speaker = role
    flush()
    save(db, META + sid, dict(meta or {},document=document,role=target,imported_at=at))
    if meta is not None:
        from .recall_runtime import enqueue_imported_session
        enqueue_imported_session(db,pid,sid)
    return sid


def source_time(db, event_id):
    row = db.execute("SELECT created_at FROM events WHERE id=?", (event_id,)).fetchone()
    if not row or not event_id.startswith(PREFIX):
        return row[0] if row else None
    parts = event_id.split(".")
    turn = db.execute("SELECT origin_json FROM turns WHERE id=?", (parts[1],)).fetchone()
    if turn:
        origin = json.loads(turn[0])
        detail = origin.get("sources", {}).get(event_id)
        if detail:
            return detail["time"]
        if row[0] != origin.get("imported_at"):
            return row[0]
    return None


def group(db, turn, selected, target, audit=False, *, purpose="dialogue", providers=None):
    origin = json.loads(turn["origin_json"])
    meta = read(db, META + turn["session_id"])
    permitted = bool(meta) and history_allowed(db, turn["session_id"], purpose, providers)
    if not permitted and not audit:
        return {"status":"unavailable"}
    rows = [dict(r) for r in db.execute("""SELECT e.id,e.role,e.text,e.created_at,
        EXISTS(SELECT 1 FROM source_exclusions x WHERE x.event_id=e.id) AS excluded
        FROM events e WHERE e.session_id=? AND e.persona_id=? AND e.seq BETWEEN ? AND ?
        AND e.role IN ('user','persona') ORDER BY e.seq""",
        (turn["session_id"],turn["persona_id"],origin["first_seq"],origin["last_seq"]))]
    if target["kind"]=="part" or (target["kind"]=="event" and not any(r["id"]==target["id"] for r in rows)):
        return {"status":"not_found"}
    blocked = not permitted or not rows or any(r["excluded"] for r in rows)
    if blocked and not audit:
        return {"status":"unavailable"}
    for r in rows:
        r.pop("excluded")
        source = origin.get("sources", {}).get(r["id"])
        if source:
            r["created_at"] = source["time"]
        elif r["created_at"] == origin["imported_at"]:
            r["created_at"] = None
    return {"status":"found","target":target,"scope":selected,"turn_id":turn["id"],"session_id":turn["session_id"],
            "batch_id":None,"turn_status":"completed","usable":not blocked,"audit_only":bool(audit),
            "source_order":[r["id"] for r in rows],"coverage":"imported_transcript","members":[r for r in rows if r["role"]=="user"],
            "replies":[r|{"kind":"imported_reply","state":"imported","role":"assistant"} for r in rows if r["role"]=="persona"],
            "delivery":[],"raw_metadata":[],"recorded_at":rows[0]["created_at"] if rows else None,"user_read":"unknown"}


def chronological(group):
    items=group["members"]+group["replies"]
    order=group.get("source_order")
    if order is None:
        return items
    by_id={item["id"]:item for item in items}
    return [by_id[key] for key in order]


def conversation_messages(group):
    return [{"role":"user" if e["role"]=="user" else "assistant","content":e["text"]} for e in chronological(group)]


def page(store,persona_id,session_id,before=None,limit=80,query=None,after=None):
    if type(limit) is not int or not 1<=limit<=200 or before is not None and (type(before) is not int or not 0<before<2**63):
        raise ValueError("invalid_page_size")
    if query is not None and (not isinstance(query,str) or len(query)>256):
        raise ValueError("invalid_query")
    if after is not None and (type(after) is not int or not 0<after<2**63 or before is not None or query is not None):
        raise ValueError("invalid_page_size")
    with store.connection() as db:
        from .workbench_timeline import require_session
        require_session(db,persona_id,session_id)
        meta=read(db,META+session_id)
        if not meta:
            return {"rows":[],"before":None}
        can_use = history_allowed(db,session_id,"dialogue")
        cap = max(limit, 1000) if query else limit
        direction="seq>? ORDER BY seq ASC" if after is not None else "seq<? ORDER BY seq DESC"
        rows = db.execute("""SELECT id,seq,role,text,created_at,EXISTS(
            SELECT 1 FROM source_exclusions x WHERE x.event_id=e.id) AS excluded FROM events e WHERE session_id=?
            AND id LIKE 'imported.%' AND """+direction+" LIMIT ?",
            (session_id,after or before or 2**63-1,cap+1)).fetchall()
        origins = {}
        out = []
        cursor = None
        for idx, row in enumerate(rows[:cap]):
            event = dict(row)
            tid = event["id"].split(".")[1]
            if tid not in origins:
                turn = db.execute("SELECT origin_json FROM turns WHERE id=?", (tid,)).fetchone()
                origins[tid] = json.loads(turn[0]) if turn else {}
            source = origins[tid].get("sources", {}).get(event["id"],{})
            event["referenceable"] = can_use and event["role"]!="note" and not event.pop("excluded",False)
            event.pop("excluded",None)
            event["source"] = {k:v for k,v in source.items() if k != "original"}
            event["text"] = source.get("original",event["text"])
            event["role"] = source.get("role",event["role"])
            event["created_at"] = source.get("time",None if event["created_at"]==meta["imported_at"] else event["created_at"])
            if not query or query.casefold() in event["text"].casefold():
                out.append(event)
            if len(out)==limit or idx==cap-1:
                cursor=event["seq"] if idx+1<len(rows) else None
                break
        if after is not None:
            return {"rows":out,"after":cursor,"before":out[0]["seq"] if out else None}
        return {"rows":list(reversed(out)),"before":cursor}
