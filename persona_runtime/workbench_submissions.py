"""Local WebView outbox. Transport retries retain the original user intent."""
import json
import time
from threading import RLock

from .desktop_client import LocalAPIError, TransportUnknown
from .desktop_recovery import issue_intent, lookup_intent
from .config import atomic_json
from .security import protect, unprotect
from .models import RuntimeErrorBase
from .storage import Store
from .submissions import SubmissionJournal


class WorkbenchSubmissions:
    def __init__(self, config, client):
        self.config = config
        self.client = client
        self.journal = SubmissionJournal(config.paths, "webview")
        self.lock = RLock()

    def draft(self, pid, sid, update=None):
        from .workbench_timeline import require_session, visible_reference
        path = self.config.paths.home / "runtime/webview-composer.dpapi.json"
        with self.journal._locked():
            instance=self.client.expected_instance(self.config.load())
            values = json.loads(unprotect(json.loads(path.read_text(encoding="utf8"))["encrypted"])) if path.exists() else {}
            key = json.dumps([instance, pid, sid])
            current = values.get(key, {"text":"", "revision":None, "reference":None,"sequence":0})
            changed = update is not None and update["previous"] == current["revision"]
            if changed:
                current = {field:update[field] for field in ("text","revision","reference")}|{"sequence":current.get("sequence",0)+1}
            ref = current.get("reference")
            with Store(self.config.paths).connection() as db:
                if sid:require_session(db,pid,sid)
                else:
                    from .service import RuntimeService
                    RuntimeService._persona(db,pid)
                allowed = visible_reference(db,pid,sid,ref.get("id")) if sid and isinstance(ref,dict) else None
            stored = dict(current,reference={"id":allowed["id"]} if allowed else None)
            if changed or (key in values and stored != values[key]):
                values[key] = stored
                atomic_json(path, {"version":1,"encrypted":protect(json.dumps(values,ensure_ascii=False))})
            return dict(stored,reference=allowed)

    def first_session(self,pid,sid):
        source=self.draft(pid,"")
        if source["text"]:
            target=self.draft(pid,sid)
            if not target["text"]:
                self.draft(pid,sid,dict(source,previous=target["revision"]))

    def location(self,pid,sid,position):
        path=self.config.paths.home/"runtime/webview-reading.dpapi.json"
        with self.journal._locked():
            instance=self.client.expected_instance(self.config.load())
            values=json.loads(unprotect(json.loads(path.read_text(encoding="utf8"))["encrypted"])) if path.exists() else {}
            state=values.setdefault(instance,{"last":None,"sessions":{}})
            if pid is None:
                return state["last"]
            key=json.dumps([pid,sid])
            if position is None:
                saved=state["sessions"].get(key)
                last={"persona_id":pid,"session_id":sid}
                if state["last"]!=last:
                    state["last"]=last
                    atomic_json(path,{"version":1,"encrypted":protect(json.dumps(values,ensure_ascii=False))})
                return saved
            if not isinstance(position,dict) or position.get("kind") not in ("tail","native","import"):
                raise RuntimeErrorBase("invalid_reading_position")
            pos=position.get("position",0)
            offset=position.get("offset",0)
            marker=position.get("id","")
            if (type(pos) is not int or not 0<=pos<2**53 or type(offset) not in (float,int)
                    or not -100000<=offset<=100000 or not isinstance(marker,str) or len(marker)>300):
                raise RuntimeErrorBase("invalid_reading_position")
            saved={"kind":position["kind"],"position":pos,"offset":offset,"id":marker}
            if state["sessions"].get(key)!=saved:
                state["sessions"][key]=saved
                atomic_json(path,{"version":1,"encrypted":protect(json.dumps(values,ensure_ascii=False))})
            return saved

    def submit(self, payload, draft_revision):
        rid = payload["request_id"]
        with self.lock:
            self.client.expected_instance(self.config.load())
            existing = next((r for r in self.journal.pending() if r["payload"]["request_id"] == rid), None)
            if existing is None:
                ticket = issue_intent(Store(self.config.paths), self.config, payload,activate=True)
                self.journal.remember(payload, metadata={"ticket": ticket, "draft_revision": draft_revision})
                existing = {"payload":payload, "ticket":ticket, "draft_revision":draft_revision,"fresh":True}
            elif existing["payload"] != payload:
                raise RuntimeErrorBase("submission_conflict")
            return self._advance(existing)

    def _advance(self, row):
        payload = row["payload"]
        rid = payload["request_id"]
        result = row.get("result")
        state = "accepted" if result else "unconfirmed"
        if not result and time.time() >= row.get("next_check", 0):
            try:
                if row.get("fresh") and self.client.bind_instance:
                    result = self.client.manage("desktop.submission.accept",payload=payload,ticket=row["ticket"])
                else:
                    result = self._recover(row)
                if result:
                    self.journal.update(rid, result=result, state="accepted")
                    state = "accepted"
                else:
                    state = "blocked"
            except (TransportUnknown, LocalAPIError, RuntimeErrorBase) as error:
                if str(error) in ("manual_buffer_full","manual_buffer_scope_changed","input_buffer_policy_changed"):
                    self.journal.forget(rid)
                    return {"id":rid, "state":"rejected", "reason":str(error)}
                transient = isinstance(error, TransportUnknown) or str(error) in ("queue_full","buffer_full","service_draining","local_repair_active")
                state = "unconfirmed" if transient and row.get("state") != "blocked" else "blocked"
            if not result:
                failures = row.get("failures", 0) + 1
                self.journal.update(rid, state=state, failures=failures,
                                    next_check=time.time() + min(30, 2 ** min(failures, 5)))
        elif not result:
            state = row.get("state", state)
        if result and result.get("status") in ("interrupted_buffer","cancelled"):
            state = "stopped"
        if result and result.get("status")=="sealed_waiting_capacity":
            state = "waiting_capacity"
        return {"id":rid, "state":state, "text":payload["text"], "draft_revision":row.get("draft_revision"),
                "awaiting_confirmation":(result or {}).get("status")=="awaiting_confirmation",
                "turn_id":(result or {}).get("turn_id"), "event_id":(result or {}).get("event_id"),
                "batch_id":(result or {}).get("batch_id")}

    def _recover(self,row):
        payload=row["payload"]
        health=self.client.call("GET","/v1/health")
        if health.get("instance_id")!=self.config.load()["instance_id"]:
            raise RuntimeErrorBase("desktop_instance_changed")
        if "desktop_submission_recovery_v1" not in health.get("capabilities",[]):
            with Store(self.config.paths).connection() as db:return lookup_intent(db,payload)
        result=self.client.manage("desktop.submission.status",**payload)
        if result or not row.get("ticket") or row.get("state")=="blocked":return result
        if health.get("draining"):raise RuntimeErrorBase("service_draining")
        return self.client.call("POST","/v2/ingress",payload|{"desktop_ticket":row["ticket"]})

    def reconcile(self, pid, sid):
        with self.lock:
            self.client.expected_instance(self.config.load())
            rows = []
            pending=self.journal.pending()
            updates={}
            with Store(self.config.paths).connection() as db:
                for item in pending:
                    payload=item["payload"]
                    original=item.get("result")
                    if (original and not original.get("turn_id") and (payload.get("persona_id"),payload.get("session_id"))==(pid,sid)):
                        result=lookup_intent(db,payload)
                        if original!=result:
                            change={"result":result,"state":"accepted" if result else "blocked"}
                            updates[payload["request_id"]]=change
                            item.update(change)
            if updates:
                with self.journal._locked():
                    stored=self.journal._read()
                    for item in stored:
                        item.update(updates.get(item["payload"]["request_id"],{}))
                    self.journal._write(stored)
            for item in pending:
                payload = item["payload"]
                in_view = (payload.get("persona_id"), payload.get("session_id")) == (pid, sid)
                if not in_view and item.get("result"):
                    continue
                updated = self._advance(item)
                if in_view:
                    rows.append(updated)
            for item in self.client.journal.pending():
                payload = item["payload"]
                if (payload.get("persona_id"), payload.get("session_id")) != (pid, sid):
                    continue
                state = "blocked"
                try:
                    with Store(self.config.paths).connection() as db:
                        result = lookup_intent(db, payload)
                        if result:
                            state = "accepted"
                except RuntimeErrorBase:
                    result = None
                rows.append({"id":payload["request_id"], "state":state,
                             "text":payload["text"], "turn_id":(result or {}).get("turn_id"),
                             "event_id":(result or {}).get("event_id"), "legacy":True})
            return rows

    def acknowledge(self, pid, sid, ids):
        acknowledged=set(ids)
        with self.lock:
            self.client.expected_instance(self.config.load())
            with self.journal._locked():
                rows=self.journal._read()
                remaining=[]
                for item in rows:
                    payload=item["payload"]
                    result=item.get("result") or {}
                    if (payload["request_id"] in acknowledged and (result.get("turn_id") or result.get("status")=="cancelled")
                            and (payload["persona_id"],payload["session_id"])==(pid,sid)):
                        continue
                    remaining.append(item)
                if len(remaining)!=len(rows):self.journal._write(remaining)
