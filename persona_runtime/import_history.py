"""Restore original bubbles into an ordinary writable desktop session, with source provenance."""
from datetime import datetime, timedelta, timezone
import json

from .archive import encoded
from .models import LOCAL_USER_ID, identifier, timestamp
from .sessions import select

PREFIX = "imported."


def restore(db,pid,document,target,rows):
    sid=select(db,pid,"desktop")
    pending=[]
    roles_seen=set()
    last_occurrence={r['role']:i for i,r in enumerate(rows)}
    last_paired=min(last_occurrence['A'],last_occurrence['B'])
    imported_at=timestamp()
    tid=identifier()

    def flush():
        if not pending:
            return
        users=[r for r in pending if r[1]=="user"]
        replies=[r for r in pending if r[1]=="persona"]
        origin={"import_document":document,"target_role":target,
                "first_seq":pending[0][3],"last_seq":pending[-1][3],"imported_at":imported_at}
        db.execute("""INSERT INTO turns(id,source_key,persona_id,session_id,channel,user_event_id,reply_event_id,
            status,model_snapshot,created_at,updated_at,origin_json)
            VALUES(?,?,?,?,'desktop',?,?,'completed','{}',?,?,?)""",
            (tid,PREFIX+tid,pid,sid,users[0][0] if users else None,replies[-1][0] if replies else None,
             pending[0][2],pending[-1][2],encoded(origin)))
        pending.clear()

    for index,r in enumerate(rows):
        role="persona" if r["role"]==target else "user" if r["role"]!="-" else "note"
        if role=="note" and r["speaker"].casefold() not in ("系统","系统消息","system","通知"):
            continue
        if len(roles_seen)==2 and index<=last_paired and ((role=='user' and pending[-1][1]=='persona') or len(pending)>=64):
            flush()
            roles_seen.clear()
            tid=identifier()
        eid=PREFIX+tid+"."+r["id"]
        at=imported_at
        if r["date"]:
            at=datetime.fromisoformat(r["date"]).replace(tzinfo=timezone(timedelta(hours=8))).isoformat()
        seq=db.execute("""INSERT INTO events(id,session_id,persona_id,user_id,role,text,created_at)
            VALUES(?,?,?,?,?,?,?)""",(eid,sid,pid,LOCAL_USER_ID,role,r["original"],at)).lastrowid
        if role!="note":
            roles_seen.add(role)
            pending.append((eid,role,at,seq))
    flush()
    from .scene_policy import save
    save(db,"persona.import_history."+sid,{"document":document,"role":target,"imported_at":imported_at})
    return sid


def group(db, turn, selected, target, audit=False):
    origin=json.loads(turn["origin_json"])
    rows=[dict(r) for r in db.execute("""SELECT e.id,e.role,e.text,e.created_at,
        EXISTS(SELECT 1 FROM source_exclusions x WHERE x.event_id=e.id) AS excluded
        FROM events e WHERE e.session_id=? AND e.persona_id=? AND e.seq BETWEEN ? AND ?
        AND e.role IN ('user','persona') ORDER BY e.seq""",
        (turn["session_id"],turn["persona_id"],origin["first_seq"],origin["last_seq"]))]
    if target["kind"]=="part" or (target["kind"]=="event" and not any(r["id"]==target["id"] for r in rows)):
        return {"status":"not_found"}
    blocked=any(r.pop("excluded") for r in rows)
    if blocked and not audit:
        return {"status":"unavailable"}
    for r in rows:
        r.pop("excluded",None)
    return {"status":"found","target":target,"scope":selected,"turn_id":turn["id"],"session_id":turn["session_id"],
            "batch_id":None,"turn_status":"completed","usable":not blocked,"audit_only":bool(audit),
            "source_order":[r["id"] for r in rows],"coverage":"imported_transcript","members":[r for r in rows if r["role"]=="user"],
            "replies":[r|{"kind":"imported_reply","state":"imported","role":"assistant"} for r in rows if r["role"]=="persona"],
            "delivery":[],"raw_metadata":[],"recorded_at":turn["created_at"],"user_read":"unknown"}


def chronological(group):
    items=group["members"]+group["replies"]
    order=group.get("source_order")
    if order is None:
        return items
    by_id={item["id"]:item for item in items}
    return [by_id[key] for key in order]


def conversation_messages(group):
    return [{"role":"user" if e["role"]=="user" else "assistant","content":e["text"]} for e in chronological(group)]


def page(store,persona_id,session_id,before=None,limit=80):
    from .service import RuntimeService
    from .scene_policy import read
    if type(limit) is not int or not 1<=limit<=200:
        raise ValueError("invalid_page_size")
    with store.connection() as db:
        RuntimeService._owned(db,"sessions",session_id,persona_id)
        meta=read(db,"persona.import_history."+session_id)
        if not meta:
            return {"rows":[],"before":None}
        rows=[dict(r) for r in db.execute("""SELECT id,seq,role,text,created_at FROM events WHERE session_id=?
            AND id LIKE 'imported.%' AND seq<? ORDER BY seq DESC LIMIT ?""",
            (session_id,before if before is not None else 2**63-1,limit+1))]
        cursor=rows[limit-1]["seq"] if len(rows)>limit else None
        for r in rows:
            if r["created_at"]==meta["imported_at"]:
                r["created_at"]=None
        return {"rows":list(reversed(rows[:limit])),"before":cursor}
