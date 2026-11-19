"""Scoped, bounded projections for the native desktop chat window."""
import json
import re

from .models import LOCAL_USER_ID, RuntimeErrorBase
from .persona_management import active
from .service import RuntimeService


PAGE_SIZE = 60
PAGE_BYTES = 384 * 1024
SAFE_ERRORS = {
    "desktop_network_wait",
    "desktop_storage_wait",
    "desktop_connection_changed",
    "service_restarted","desktop_reply_superseded","scene_superseded","search_notice_superseded",
    "desktop_presentation_unknown","scene_presentation_unknown","desktop_presentation_unavailable",
    "desktop_settlement_pending","desktop_receipt_requires_review","desktop_source_withdrawn",
    "desktop_instance_changed","model_not_configured","cancelled_by_user","provider_timeout",
    "knowledge_repeated_notice",
    "provider_network_error","provider_effect_unknown","provider_retries_exhausted","knowledge_evidence_changed",
}
NATIVE = "persona_id=? AND session_id=? AND channel='desktop' AND source_key NOT GLOB 'imported.*'"
DESKTOP = """s.persona_id=? AND s.user_id=? AND coalesce(l.channel,'desktop')='desktop'
    AND NOT EXISTS (SELECT 1 FROM turns t WHERE t.session_id=s.id AND t.channel!='desktop')
    AND NOT EXISTS (SELECT 1 FROM channel_sessions c WHERE c.session_id=s.id AND c.channel!='desktop')"""


def selection(store, persona_id):
    with store.connection() as db:
        person = RuntimeService._persona(db, persona_id)
        rows = db.execute(f"""SELECT s.id,s.created_at FROM sessions s LEFT JOIN session_lifecycle l ON l.session_id=s.id
                               WHERE {DESKTOP} ORDER BY s.created_at,s.rowid""", (persona_id,LOCAL_USER_ID))
        sessions = [dict(row) for row in rows]
        head = db.execute("""SELECT session_id FROM session_heads WHERE persona_id=? AND user_id=?
                             AND channel='desktop' AND bot='' AND peer=''""",(persona_id,LOCAL_USER_ID)).fetchone()
        return {"person":person,"sessions":sessions,"active":head[0] if head else None}


def require_session(db,pid,sid):
    active(db,pid)
    if not db.execute(f"""SELECT 1 FROM sessions s LEFT JOIN session_lifecycle l ON l.session_id=s.id
                          WHERE s.id=? AND {DESKTOP}""",(sid,pid,LOCAL_USER_ID)).fetchone():
        raise RuntimeErrorBase("session_unavailable")


def visible_reference(db,pid,sid,event_id):
    if not isinstance(event_id,str):
        return None
    row=db.execute("""SELECT e.* FROM events e WHERE id=? AND session_id=? AND persona_id=? AND user_id=?
                      AND NOT EXISTS(SELECT 1 FROM source_exclusions WHERE event_id=e.id)""",
                   (event_id,sid,pid,LOCAL_USER_ID)).fetchone()
    if not row or row["role"]=="note":
        return None
    if event_id.startswith("imported."):
        from .import_history import history_allowed
        if not history_allowed(db,sid,"dialogue"):
            return None
    return {"id":event_id,"text":row["text"][:180]}


def public_error(error):
    if not error:
        return None
    if error in SAFE_ERRORS or re.fullmatch(r"provider_http_[1-5]\d\d",error):
        return error
    if "unknown" in error or "unconfirmed" in error:
        return "provider_effect_unknown"
    return "request_failed"


def latest_status(store,db,pid,sid):
    row=db.execute(f"SELECT id,error_code FROM turns WHERE {NATIVE} ORDER BY rowid DESC LIMIT 1",(pid,sid)).fetchone()
    if not row:
        return None
    error=public_error(row["error_code"])
    needs_connection=False
    if error in ("provider_http_401","provider_http_403","model_not_configured"):
        from .config import ConfigStore
        current=ConfigStore(store.paths).load()["model"]
        original=json.loads(db.execute("SELECT model_snapshot FROM turns WHERE id=?",(row["id"],)).fetchone()[0] or "{}")
        if error=="model_not_configured":
            needs_connection=not (current["encrypted_key"] and current["base_url"])
        else:
            needs_connection=all(original.get(key)==current.get(key) for key in ("name","base_url","encrypted_key") if key in original)
    return {"id":row["id"],"error_code":error,"needs_connection":needs_connection}


def turn_card(db,row):
    from .turn_input import load_turn_input
    from .scene_publication import ScenePublication
    from .knowledge_search_notice import read as read_notice
    from .knowledge_service import turn_usable
    from .presentation import batch_view
    turn=dict(row)
    tid=turn["id"]
    members=load_turn_input(db,turn)
    card={k:turn[k] for k in ("id","status","reply_event_id","position")}
    card["input_members"]=[{"id":m["id"],"text":m["text"]} for m in members]
    card["search_notice"]=read_notice(db,turn)
    card["desktop_publications"]=ScenePublication.desktop_plans(db,turn)
    card["error_code"]=public_error(turn["error_code"])
    if card['search_notice'] and (turn['status']=='completed' or turn['error_code']=='knowledge_repeated_notice'):
        card['has_retrieved_notes']=bool(db.execute("""SELECT 1 FROM context_uses WHERE turn_id=?
            AND json_extract(details_json,'$.knowledge.record_id')=?
            AND json_extract(details_json,'$.knowledge.translation.notes_characters')>0 LIMIT 1""",(tid,tid)).fetchone())
    if card["reply_event_id"] and turn["status"]=="completed":
        if turn_usable(db,tid):
            event=db.execute("SELECT text FROM events WHERE id=? AND session_id=? AND persona_id=?",
                             (card["reply_event_id"],turn["session_id"],turn["persona_id"])).fetchone()
            if event:
                batch=db.execute("SELECT id FROM reply_batches WHERE turn_id=? ORDER BY created_at DESC LIMIT 1",(tid,)).fetchone()
                segments=[dict(r) for r in db.execute("SELECT * FROM reply_segments WHERE batch_id=? ORDER BY segment_index",(batch[0],))] if batch else []
                displayed=[r[0] for r in db.execute("SELECT text FROM reply_display_messages WHERE batch_id=? ORDER BY display_index",(batch[0],))] if batch else []
                card["reply"]=event[0]
                card["display_reply"]="\n\n".join(displayed) if displayed else batch_view(event[0],segments,historical=True)[0]
        else:
            card.update(error_code="knowledge_evidence_changed",desktop_publications=[],reply_event_id=None)
    card["feedback_available"]=bool(db.execute("""SELECT 1 FROM context_uses WHERE turn_id=?
                                       AND json_extract(details_json,'$.import_style') IS NOT NULL LIMIT 1""",(tid,)).fetchone()) if card["reply_event_id"] else False
    activity=db.execute("SELECT event FROM request_activity WHERE turn_id=? ORDER BY seq DESC LIMIT 1",(tid,)).fetchone()
    card["phase"]=activity[0] if activity else turn["status"]
    return card


def retrieved_notes(store,pid,sid,tid):
    from .knowledge_general import validate_record
    from .knowledge_native_memory import source_states,state_for_source
    from .knowledge_service import POLICY,read,turn_usable
    from .archive import fingerprint
    with store.connection() as db:
        require_session(db,pid,sid)
        row=db.execute("""SELECT c.details_json,t.user_event_id,t.input_batch_id FROM turns t
            JOIN context_uses c ON c.turn_id=t.id WHERE t.id=? AND t.persona_id=? AND t.session_id=? AND t.channel='desktop'
            AND (t.status='completed' OR t.error_code='knowledge_repeated_notice') ORDER BY c.rowid DESC LIMIT 1""",(tid,pid,sid)).fetchone()
        if not row:raise RuntimeErrorBase('retrieved_notes_unavailable')
        policy=read(db,'policy.'+pid,POLICY)['data']
        excluded=db.execute("""SELECT 1 FROM source_exclusions WHERE event_id=? OR event_id IN
            (SELECT event_id FROM input_batch_members WHERE batch_id=?)""",(row['user_event_id'],row['input_batch_id'])).fetchone()
        meta=json.loads(row['details_json']).get('knowledge') or {}
        if excluded or not policy['enabled'] or 'desktop' not in policy['channels'] or meta.get('record_id')!=tid or not turn_usable(db,tid):
            raise RuntimeErrorBase('retrieved_notes_unavailable')
        record=validate_record(db,pid,'desktop',meta)
        note=record.get('organized') or {}
        retrieval=record.get('retrieval') or {}
        if (record.get('session_id')!=sid or record.get('root_turn_id')!=tid or meta.get('record_hash')!=fingerprint(record)
                or not note.get('notes') or note.get('task_id') not in retrieval.get('task_ids',())
                or retrieval.get('search_outcome')!='completed' or not retrieval.get('material_acquired')):
            raise RuntimeErrorBase('retrieved_notes_unavailable')
        states=source_states(db,pid)['data']
        ids=set(note.get('source_ids',[]));sources=[]
        for s in record['sources']:
            if s['id'] not in ids:continue
            state=state_for_source(states,s)
            if state.get('state')=='withdrawn' or state.get('policy_revision',state.get('revision',0))!=s.get('state_revision',0):
                raise RuntimeErrorBase('retrieved_notes_unavailable')
            sources.append({k:s.get(k) for k in ('id','title','url')})
        if not ids or ids!={s['id'] for s in sources}:raise RuntimeErrorBase('retrieved_notes_unavailable')
        return {'notes':note['notes'],'sources':sources}


def page(store,pid,sid,version=None,before=None,after=None,anchor=None):
    from .tk_notice import waiting
    from .scene_policy import read
    from .import_history import META
    for value in (version,before,after,anchor):
        if value is not None and (type(value) is not int or not 0<=value<2**63):
            raise RuntimeErrorBase("invalid_page_cursor")
    if sum(value is not None for value in (before,after,anchor))>1:
        raise RuntimeErrorBase("invalid_page_cursor")
    with store.connection() as db:
        require_session(db,pid,sid)
        latest=db.execute("SELECT coalesce(max(seq),0) FROM request_activity").fetchone()[0]
        meta=read(db,META+sid) or {}
        excluded=db.execute("SELECT coalesce(max(rowid),0) FROM source_exclusions").fetchone()[0]
        from .import_history import history_allowed
        permitted=history_allowed(db,sid,"dialogue") if meta else False
        lifecycle=db.execute("SELECT state,revision FROM session_lifecycle WHERE session_id=?",(sid,)).fetchone()
        response={"version":latest,"pending_notices":waiting(db,sid),"source_version":[meta.get("revision",0),excluded,permitted],
                  "latest":latest_status(store,db,pid,sid),
                  "presentable":store.current(db,required=False)==pid,
                  "session_revision":lifecycle["revision"] if lifecycle else 0,
                  "sealed":bool(lifecycle and lifecycle["state"]=="sealed")}
        from .knowledge_service import POLICY,read as knowledge_read
        reader=knowledge_read(db,'policy.'+pid,POLICY)['data']
        response['public_search_available']=bool(reader['enabled'] and 'desktop' in reader['channels']
            and knowledge_read(db,'web.policy.'+pid,{})['data'].get('enabled'))
        delta=version is not None and version<=latest and all(v is None for v in (before,after,anchor))
        if delta:
            if version==latest:
                return response|{"reset":False,"turns":None}
            changes=db.execute("""SELECT DISTINCT a.turn_id FROM request_activity a CROSS JOIN turns t ON t.id=a.turn_id
                WHERE a.seq>? AND t.persona_id=? AND t.session_id=? AND t.channel='desktop'
                AND t.source_key NOT GLOB 'imported.*' LIMIT ?""",(version,pid,sid,PAGE_SIZE+1)).fetchall()
            if len(changes)<=PAGE_SIZE:
                cards=[]
                for change in changes:
                    row=db.execute("SELECT rowid position,* FROM turns WHERE id=?",(change[0],)).fetchone()
                    cards.append(turn_card(db,row))
                if len(json.dumps(cards,ensure_ascii=False).encode())<=PAGE_BYTES or len(cards)<=1:
                    return response|{"reset":False,"turns":cards or None}
        if anchor is not None:
            following=db.execute(f"SELECT rowid FROM turns WHERE {NATIVE} AND rowid>=? ORDER BY rowid LIMIT ?",
                                 (pid,sid,anchor,PAGE_SIZE//2)).fetchall()
            if following:anchor=following[-1][0]
        position=next((value for value in (before,after,anchor) if value is not None),None)
        clause="" if position is None else " AND rowid"+(">" if after is not None else "<=" if anchor is not None else "<")+"?"
        order="ASC" if after is not None else "DESC"
        params=[pid,sid]+([position] if position is not None else [])
        rows=db.execute(f"SELECT rowid position,id FROM turns WHERE {NATIVE}{clause} ORDER BY rowid {order} LIMIT ?",
                        (*params,PAGE_SIZE+1)).fetchall()
        if not rows and anchor is not None:
            rows=db.execute(f"SELECT rowid position,id FROM turns WHERE {NATIVE} ORDER BY rowid LIMIT ?",(pid,sid,PAGE_SIZE+1)).fetchall()
        cards=[]
        size=0
        for row in rows[:PAGE_SIZE]:
            card=turn_card(db,db.execute("SELECT rowid position,* FROM turns WHERE id=?",(row["id"],)).fetchone())
            size+=len(json.dumps(card,ensure_ascii=False).encode())
            if cards and size>PAGE_BYTES:break
            cards.append(card)
        cards.sort(key=lambda card:card["position"])
        older=newer=None
        if cards:
            lo,hi=cards[0]["position"],cards[-1]["position"]
            if db.execute(f"SELECT 1 FROM turns WHERE {NATIVE} AND rowid<? LIMIT 1",(pid,sid,lo)).fetchone():older=lo
            if db.execute(f"SELECT 1 FROM turns WHERE {NATIVE} AND rowid>? LIMIT 1",(pid,sid,hi)).fetchone():newer=hi
        return response|{"reset":True,"turns":cards,"older":older,"newer":newer}
