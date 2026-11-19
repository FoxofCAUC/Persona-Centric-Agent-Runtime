"""Freeze a dyadic transcript and atomically attach it to a writable desktop session."""
import json
from collections import Counter

from .archive import fingerprint, scope
from .corpus_files import digest
from .import_history import META, configured_providers, history_allowed, restore, source_detail
from .import_library import require
from .import_markdown import SYSTEM, assign
from .import_plan import parse_source
from .models import LOCAL_USER_ID, timestamp
from .persona_management import active
from .private_personas import guard_acceptance_write
from .scene_policy import read, save
from .service import RuntimeService
from .sessions import blockers, build_manifest, owned, seal_locked

OP = "persona.history.commit."
SAVED = "persona.history.saved."
OWNER = "persona.history.self."


def _op_id(value):
    require(isinstance(value, str) and 0 < len(value) <= 128, "操作编号无效。")


def _head(db, pid):
    if not pid:
        return None, None
    row = db.execute("""SELECT h.session_id,l.revision,l.activity_revision FROM session_heads h
        JOIN session_lifecycle l ON l.session_id=h.session_id WHERE h.persona_id=? AND h.user_id=?
        AND h.channel='desktop' AND h.bot='' AND h.peer=''""",(pid,LOCAL_USER_ID)).fetchone()
    if row:
        last = db.execute("SELECT max(seq) FROM events WHERE session_id=?",(row[0],)).fetchone()[0]
        return row[0], [row[1],row[2],last]
    return None, None


def _selected(raw, request):
    parsed = parse_source(raw)
    rows = assign(parsed, request["mapping"], request["overrides"])
    start, end = request["first"], request["last"]
    size=len(rows)
    require((start is None or type(start) is int) and (end is None or type(end) is int), "消息范围无效。")
    start, end = (1 if start is None else start), (size if end is None else end)
    items = request["excluded"]
    require(1<=start<=end<=size and isinstance(items,list) and all(type(i) is int and 1<=i<=size for i in items),
            "消息范围或排除项无效。")
    excluded=set(items)
    selected=[r for r in rows[start-1:end] if int(r["id"]) not in excluded
              and (r["role"] != "-" or r["speaker"].casefold() in SYSTEM)]
    require(any(r["role"] in ("A","B") and r["text"] for r in selected), "该范围需要有可接续的发言。")
    names={role: list(dict.fromkeys(r["speaker"] for r in rows if r["role"]==role)) for role in ("A","B")}
    return rows, selected, names


def _source(private, request):
    row=private.execute("SELECT raw,revision,status,mapping,overrides FROM documents WHERE id=?", (request["ident"],)).fetchone()
    require(row and row["status"]!="revoked" and row["revision"]==request["revision"], "聊天原件或归属版本已变化，请重新预览。")
    require(digest(row["raw"])==request["ident"], "聊天原件校验失败。")
    if request["creation"] is not None:
        require(json.loads(row["mapping"])==request["mapping"] and json.loads(row["overrides"])==request["overrides"],
                "建卡的双方归属已变化，请先核对草稿。")
    return row


class HistoryService:
    def __init__(self, store, library, publisher):
        self.store, self.library, self.publisher = store, library, publisher

    def preview(self, *args, **kwargs):
        return self._prepare(*args, **kwargs)[0]

    def _prepare(self, ident, revision, self_role, mapping, overrides=None, first=None, last=None, excluded=None,
                timezone_offset=480, persona_id=None, organize=False, session_id=None, creation=None):
        require(type(revision) is int and revision>0 and self_role in ("A","B") and isinstance(mapping,dict)
                and isinstance(overrides or {},dict), "请确认本人和对方的归属。")
        require(type(organize) is bool and type(timezone_offset) is int and -14*60<=timezone_offset<=14*60,
                "请选择时区和本次整理用途。")
        if creation is not None:
            require(not persona_id and isinstance(creation,dict) and set(creation)=={"plan_id","card_revision","roles","enable_scene"}
                    and type(creation["card_revision"]) is int and type(creation["enable_scene"]) is bool
                    and isinstance(creation["roles"],list) and all(r in ("A","B") for r in creation["roles"])
                    and len(creation["roles"])==len(set(creation["roles"])) and ("B" if self_role=="A" else "A") in creation["roles"],
                    "请选择原聊天中对方的角色卡。")
        require(creation is not None or persona_id, "请选择目标角色。")
        request=dict(ident=ident, revision=revision, self_role=self_role, mapping=mapping, overrides=overrides or {},
                     first=first,last=last,excluded=[] if excluded is None else excluded,timezone_offset=timezone_offset,
                     persona_id=persona_id,organize=organize,session_id=session_id,creation=creation)
        with self.library.db() as private:
            source=_source(private,request)
        all_rows, rows, names=_selected(source["raw"],request)
        kinds={"A":"speaker","B":"speaker"}
        users={"user","用户"}
        assistants={"assistant","助手","deepseek"}
        for role, other in (("A","B"),("B","A")):
            if ({n.casefold() for n in names[role]}<=users and {n.casefold() for n in names[other]}<=assistants):
                require(self_role==role, "该导出已标明用户与助手，请从用户一侧接续。")
                kinds={role:"user",other:"assistant"}
        with self.store.connection() as db:
            if persona_id:
                active(db, persona_id)
                require(self.store.current(db,required=False)==persona_id,"当前角色已切换。")
            if session_id:
                require(persona_id,"请选择原会话的人物。")
                owned(db,session_id,scope(persona_id,"desktop"))
                legacy=read(db,META+session_id)
                require(legacy and legacy.get("version")!=2 and legacy["document"]==ident, "请使用这段历史对应的原文件。")
            sid, rev=_head(db,persona_id)
            identity=fingerprint([(r["id"],r["role"]==self_role) for r in all_rows if r["role"] in ("A","B")])
            previous=read(db,OWNER+ident)
            require(previous is None or previous==identity, "这份原件已确认另一组本人身份，请核对说话人。")
        providers=configured_providers(self.store.paths)
        counts=Counter("note" if r["role"]=="-" else r["role"] for r in rows)
        value={"request":request,"providers":providers,"session_id":sid,"session_revision":rev,"identity":identity}
        return value|{"token":fingerprint(value),"total":len(rows),"source_total":len(all_rows),"names":names,"participant_kinds":kinds,
                      "counts":{r:counts[r] for r in ("A","B","note")},
                      "preview_rows":[{k:v[k] for k in ("id","role","speaker","date","text")} for v in rows[-6:]]},all_rows,rows

    def commit(self, preview, operation_id, confirmed, creation=None):
        _op_id(operation_id)
        require(confirmed is True and isinstance(preview,dict) and isinstance(preview.get("request"),dict),
                "请确认本人身份、目标角色和原话范围。")
        req=preview["request"]
        require(creation==req["creation"],"待保存角色已变化，请重新预览。")
        expected={k:preview[k] for k in ("request","providers","session_id","session_revision","identity")}
        require(preview.get("token")==fingerprint(expected), "历史预览已变化。")
        token=preview["token"]
        with self.store.connection() as db:
            previous=read(db,OP+operation_id)
            if previous:
                require(previous["token"]==token,"操作编号已用于另一份历史预览。")
                return previous["result"]
        fresh,all_rows,chosen=self._prepare(**req)
        require(fresh["token"]==token, "角色、会话或接收模型已变化，请重新预览。")
        names=fresh["names"]
        target="B" if req["self_role"]=="A" else "A"
        ident=req["ident"]
        selection=fingerprint([req["mapping"],req["overrides"],req["self_role"],[r["id"] for r in chosen],req["timezone_offset"]])
        meta={"version":2,"revision":1,"withdrawn":False,"organize":req["organize"],"self_role":req["self_role"],
              "names":names,"participant_kinds":fresh["participant_kinds"],"selection":selection,"timezone_offset":req["timezone_offset"],"providers":fresh["providers"],
              "source_revision":req["revision"],"first":req["first"],"last":req["last"],"excluded":req["excluded"],
              "count":len(chosen)}
        new_people=[]
        with self.store.connection(write=True) as db:
            previous=read(db,OP+operation_id)
            if previous:
                require(previous["token"]==token,"操作编号已用于另一份历史预览。")
                return previous["result"]
            with self.library.db() as private:
                private.execute("BEGIN IMMEDIATE")
                updated=private.execute("SELECT revision,status FROM documents WHERE id=?", (ident,)).fetchone()
                require(updated and updated["revision"]==req["revision"] and updated["status"]!="revoked","导入来源已变化。")
                require(configured_providers(self.store.paths)==fresh["providers"], "接收模型已变化。")
                owner=read(db,OWNER+ident)
                require(owner is None or owner==fresh["identity"],"这份原件已确认另一组本人身份。")
                pid=req["persona_id"]
                if pid:
                    active(db,pid)
                    require(self.store.current(db,required=False)==pid,"当前角色已切换。")
                    guard_acceptance_write(self.store,pid)
                    people=[pid]
                else:
                    from .import_style import PREFIX as STYLE
                    people=[read(db,STYLE+"created."+fingerprint([ident,r])) for r in creation["roles"]]
                    pid=people[creation["roles"].index(target)]
                    if pid:
                        from .import_card_publish import BINDING
                        require(all(people), "所选人物已经部分建立，请到人物菜单导入历史。")
                        cards=self.publisher.jobs.material(ident,revision=req["revision"],plan_id=creation["plan_id"],
                                                           card_revision=creation["card_revision"],db=private)
                        for saved_pid,role in zip(people,creation["roles"]):
                            binding=read(db,BINDING+saved_pid)
                            require(binding and binding["plan"]==creation["plan_id"] and binding["role"]==role
                                    and binding["card_revision"]==creation["card_revision"]
                                    and binding["card_hash"]==fingerprint(cards[role]),
                                    "角色已按另一版草稿保存，请到人物菜单导入历史。")
                    else:
                        new_people=self.publisher.create_in_transaction(db,private,ident,req["revision"],**creation)
                        people=new_people
                        pid=people[creation["roles"].index(target)]
                    require(all(people),"所选角色已部分建立，请在对应人物菜单导入历史。")
                    active(db,pid)
                saved_key=SAVED+fingerprint([ident,pid])
                sid=read(db,saved_key)
                reused=bool(sid)
                if sid:
                    require(read(db,META+sid).get("selection")==selection,"该角色已导入这份记录的另一范围，请打开原会话核对。")
                elif req["session_id"]:
                    sid=req["session_id"]
                    self._adopt(db,pid,sid,ident,target,all_rows,chosen,meta)
                else:
                    if req["persona_id"]:
                        current, rev=_head(db,pid)
                        require((current,rev)==(fresh["session_id"],fresh["session_revision"]), "活动会话已变化，请重新预览。")
                        if current:
                            speech=db.execute("SELECT 1 FROM expression_speech_units WHERE session_id=? AND closed_at IS NULL LIMIT 1",
                                              (current,)).fetchone()
                            require(not blockers(db,current) and not speech,"当前会话还有待处理的输入或回复，请完成或取消后重试。")
                            row=owned(db,current,scope(pid,"desktop"))
                            seal_locked(db,row,build_manifest(db,row),"new_session")
                    else:
                        require(not _head(db,pid)[0],"已创建的角色拥有会话，请从人物菜单接续。")
                    sid=restore(db,pid,ident,target,chosen,meta=meta)
                save(db,OWNER+ident,fresh["identity"])
                save(db,saved_key,sid)
                result={"people":people,"persona_id":pid,"session_id":sid,"reused":reused}
                save(db,OP+operation_id,{"token":token,"result":result})
                db.commit()
        callback=getattr(self.store,"character_committed",None)
        if callback:
            for pid in new_people:
                callback(pid)
        return result

    def _adopt(self,db,pid,sid,ident,target,all_rows,chosen,meta):
        legacy=read(db,META+sid)
        require(legacy and legacy.get("version")!=2 and legacy["document"]==ident and legacy["role"]==target,
                "旧记录中的本人和对方与本次选择不符。")
        selected={r["id"] for r in chosen}
        source={r["id"]:r for r in all_rows if r["role"]!="-" or r["speaker"].casefold() in SYSTEM}
        origins={}
        changed_sources=set()
        events=db.execute("SELECT * FROM events WHERE session_id=? AND persona_id=? AND id LIKE 'imported.%' ORDER BY seq",(sid,pid)).fetchall()
        require([e["id"].split(".")[-1] for e in events]==list(source),"原件与旧历史的消息序号不一致。")
        for e in events:
            _,tid,number=e["id"].split(".")
            row=source[number]
            detail=source_detail(row,target,meta["timezone_offset"])
            if tid not in origins:
                turn=db.execute("SELECT origin_json FROM turns WHERE id=? AND session_id=?",(tid,sid)).fetchone()
                require(turn is not None,"旧历史的回合缺失。")
                origins[tid]=json.loads(turn[0])
            origin=origins[tid]
            old=origin.get("sources",{}).get(e["id"],{})
            require(old.get("original",e["text"])==row["original"] and old.get("role",e["role"])==detail["role"],
                    "旧气泡正文或说话人与原件不符。")
            origin.setdefault("sources",{})[e["id"]]=detail
            role=detail["role"] if row["text"] and number in selected else "note"
            text=row["text"] if role!="note" else row["original"]
            if (text,role,detail["time"] or legacy["imported_at"])!=(e["text"],e["role"],e["created_at"]):
                changed_sources.add(e["id"])
            db.execute("UPDATE events SET role=?,text=?,created_at=? WHERE id=?",
                       (role,text,detail["time"] or legacy["imported_at"],e["id"]))
        for tid,origin in origins.items():
            db.execute("UPDATE turns SET origin_json=? WHERE id=?", (json.dumps(origin,ensure_ascii=False),tid))
        if changed_sources:
            from .automatic_memory import invalidate
            invalidate(db,pid,changed_sources)
            for summary in db.execute("SELECT id,sources_json FROM summaries WHERE persona_id=? AND status='active'",(pid,)).fetchall():
                if changed_sources.intersection(json.loads(summary["sources_json"])):
                    db.execute("UPDATE summaries SET status='stale',reason='source_changed' WHERE id=?",(summary["id"],))
            for candidate in db.execute("SELECT id,sources_json FROM candidates WHERE persona_id=? AND status='pending'",(pid,)).fetchall():
                ids={ref["event_id"] for ref in json.loads(candidate["sources_json"])}
                if changed_sources.intersection(ids):
                    db.execute("UPDATE candidates SET status='stale' WHERE id=?",(candidate["id"],))
        save(db,META+sid,dict(legacy,**meta))
        from .recall_runtime import enqueue_imported_session
        enqueue_imported_session(db,pid,sid)

    def status(self, persona_id, session_id):
        with self.store.connection() as db:
            RuntimeService._owned(db,"sessions",session_id,persona_id)
            meta=read(db,META+session_id)
            if not meta:
                return None
            models=configured_providers(self.store.paths)
            state="legacy" if meta.get("version")!=2 else "withdrawn" if meta.get("withdrawn") else (
                "active" if history_allowed(db,session_id,"dialogue",models["dialogue"]) else "provider_changed")
            memory="off"
            if state=="withdrawn":
                memory="withdrawn"
            elif meta.get("organize"):
                policy=db.execute("SELECT data_json FROM organization_policies WHERE persona_id=? ORDER BY revision DESC LIMIT 1",(persona_id,)).fetchone()
                data=json.loads(policy[0]) if policy else {}
                from .config import ConfigStore
                cfg=ConfigStore(self.store.paths).load()
                org=cfg["organization"]
                model=cfg["model"] if org["reuse_chat"] else org["model"]
                if not (model.get("encrypted_key") and history_allowed(db,session_id,"memory",model) and data.get("enabled")
                        and data.get("memory_automation") and data.get("memory_backfill",True) and "desktop" in data.get("channels",[])):
                    memory="waiting_configuration"
                else:
                    memory=self._memory_state(db,session_id)
            return dict(meta,version=meta.get("version",1),revision=meta.get("revision",0),current_providers=models,
                        usage_state=state,memory_state=memory,session_id=session_id,persona_id=persona_id)

    @staticmethod
    def _memory_state(db,sid):
        # Limit accounting to turns restored from this transcript.
        batches={r[0] for r in db.execute("""SELECT DISTINCT c.batch_id FROM memory_coverage c JOIN turns t ON t.id=c.turn_id
            WHERE t.session_id=? AND t.source_key LIKE 'imported.%' AND c.batch_id IS NOT NULL""",(sid,))}
        states=set()
        for row in db.execute("""SELECT b.id,b.state,t.status,v.status FROM memory_batches b LEFT JOIN organization_tasks t ON t.id=b.extract_task_id
                                LEFT JOIN organization_tasks v ON v.id=b.verify_task_id WHERE b.session_id=?""",(sid,)):
            if row[0] in batches:
                states.update(row[1:])
        if states.intersection({"blocked_unknown","cancelled_unknown","unknown","interrupted"}): return "unknown"
        if states.intersection({"blocked_authorization","history_paused"}): return "waiting_configuration"
        if "waiting_quota" in states: return "waiting_quota"
        if states.intersection({"extracting","verifying","waiting","running","queued"}): return "processing"
        dispositions=Counter(r[0] for r in db.execute("""SELECT c.disposition FROM turns t LEFT JOIN memory_coverage c ON c.turn_id=t.id
            WHERE t.session_id=? AND t.source_key LIKE 'imported.%' AND (t.user_event_id IS NOT NULL OR t.reply_event_id IS NOT NULL)""",(sid,)))
        if dispositions["input_too_long"] or "blocked_input" in states: return "blocked_input"
        if dispositions["processed_with_quarantine"]: return "quarantined"
        return "processed" if dispositions and set(dispositions)<={"processed","no_memory"} else "pending"

    def update(self, persona_id, session_id, expected_revision, action, confirmed=False, expected_providers=None):
        require(type(expected_revision) is int and action in ("pause","organize","withdraw","authorize"),"历史用途操作无效。")
        with self.store.connection(write=True) as db:
            active(db,persona_id)
            require(self.store.current(db,required=False)==persona_id,"当前角色已切换。")
            RuntimeService._owned(db,"sessions",session_id,persona_id)
            meta=read(db,META+session_id)
            require(meta and meta.get("version")==2 and meta["revision"]==expected_revision,"请刷新或重新核对旧记录。")
            require(not meta["withdrawn"],"这份历史已经撤回。")
            if action=="withdraw":
                require(confirmed is True,"请确认撤回历史使用。")
                meta.update(withdrawn=True,organize=False)
                save(db,META+session_id,meta)
                ids=[r[0] for r in db.execute("SELECT id FROM events WHERE session_id=? AND id LIKE 'imported.%'",(session_id,))]
                from .memory_policy import exclude_sources
                exclude_sources(db,persona_id,ids,"history_withdrawn")
            elif action=="pause":
                meta["organize"]=False
            else:
                providers=configured_providers(self.store.paths)
                require(confirmed is True and expected_providers==providers, "请确认当前接收原话的模型。")
                if action=="authorize":
                    meta["providers"]["dialogue"]=providers["dialogue"]
                    meta["providers"]["jev"]=providers["jev"]
                    meta.pop("migration_requires_review",None)
                else:
                    require(not meta.get("migration_requires_review"), "请先确认迁移后的历史用途。")
                    meta["providers"]["memory"]=providers["memory"]
                    meta["organize"]=True
            meta["revision"]+=1
            save(db,META+session_id,meta)
            if action=="authorize":
                from .recall_runtime import enqueue_imported_session
                enqueue_imported_session(db,persona_id,session_id)
        return self.status(persona_id,session_id)
