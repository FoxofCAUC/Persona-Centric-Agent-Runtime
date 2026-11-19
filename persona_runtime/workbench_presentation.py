"""Recover WebView presentation decisions across renderer and service restarts."""
from threading import RLock

from .desktop_client import LocalAPIError, TransportUnknown
from .submissions import SubmissionJournal


class WorkbenchPresentation:
    terminal_errors = {
        "speech_publication_closed", "speech_plan_changed", "speech_receipt_conflict",
        "speech_receipt_identity_mismatch", "speech_utterance_missing", "speech_fragment_missing",
        "speech_presentation_scope_mismatch", "expression_publication_closed",
        "expression_receipt_identity_mismatch", "expression_receipt_conflict",
        "desktop_notice_not_pending", "desktop_notice_already_offered", "search_notice_superseded", "knowledge_request_inactive",
        "search_notice_receipt_conflict", "search_notice_sources_changed", "expression_source_event_changed",
        "expression_presentation_scope_mismatch","expression_utterance_missing","session_unavailable",
        "speech_utterance_closed","speech_parent_missing",
    }

    def __init__(self, config, client):
        self.config = config
        self.client = client
        self.journal = SubmissionJournal(config.paths, "presentation")
        self.lock = RLock()
        self.page = None
        self.active = set()

    def attach(self, page):
        with self.lock:
            if self.page != page:
                self.page = page
                self.active.clear()

    def begin(self, page, claim_id, kind, args):
        with self.lock:
            if page != self.page or kind not in ("notice","speech","legacy"):
                return None
            if not isinstance(claim_id,str) or not 1<=len(claim_id)<=120 or not isinstance(page,str) or len(page)>120:
                return None
            fields = ({"persona_id","session_id","turn_id","notice_id"} if kind=="notice" else
                      {"persona_id","session_id","publication_id","revision","ordinal",
                       "utterance_id" if kind=="speech" else "turn_id"})
            if not isinstance(args,dict) or set(args)!=fields:
                return None
            identity = self.config.load()["instance_id"]
            scope = dict(args,instance=identity,kind=kind)
            if any(row["payload"]["scope"] == scope for row in self.journal.pending()):
                return None
            rid = claim_id
            if rid in self.active:
                return None
            token = "webview-v2." + rid
            self.journal.remember({"request_id":rid,"scope":scope},metadata={"state":"requesting"})
            try:
                if kind == "notice":
                    offer = self.client.manage("chat.notice.preview",**args,presentation_id=rid)
                    if offer.get("published"):
                        self.journal.forget(rid)
                        return None
                    body = offer["text"]
                else:
                    action = "expression.v2.offer" if kind=="speech" else "expression.presentation.offer"
                    offer = self.client.manage(action,**args,presenter_id=token)
                    if not offer.get("lease_id"):
                        self.journal.forget(rid)
                        return None
                    body = offer["body"]
                    self.journal.update(rid, lease={k:offer[k] for k in ("id","lease_id","body_hash")})
                self.journal.update(rid,state="painting")
                self.active.add(rid)
                return {"id":rid,"text":body}
            except LocalAPIError as error:
                if str(error) in self.terminal_errors:
                    self.journal.forget(rid)
                    return None
                raise

    def decide(self, page, rid, state):
        with self.lock:
            if page != self.page or rid not in self.active or state not in ("presented","not_presented","unknown"):
                return False
            self.journal.update(rid,state=state)
            self.active.discard(rid)
            return True

    def reconcile(self):
        with self.lock:
            changed = False
            for entry in self.journal.pending():
                rid, scope = entry["payload"]["request_id"], entry["payload"]["scope"]
                if rid in self.active or scope["instance"] != self.config.load()["instance_id"]:
                    continue
                try:
                    self._deliver(rid,scope,entry)
                except TransportUnknown:
                    continue
                except LocalAPIError as error:
                    if error.status!=400 or str(error)=="invalid_input_or_storage_error":
                        continue
                    if scope["kind"]=="notice":
                        try:
                            self.client.manage("chat.notice.unknown",presentation_id=rid,
                                               **{k:v for k,v in scope.items() if k not in ("instance","kind")})
                        except LocalAPIError as terminal:
                            if str(terminal) not in ("desktop_notice_not_pending","knowledge_scope_changed","desktop_notice_already_offered"):
                                continue
                        except TransportUnknown:
                            continue
                self.journal.forget(rid)
                changed = True
            return changed

    def _deliver(self, rid, scope, entry):
        kind = scope["kind"]
        args = {k:v for k,v in scope.items() if k not in ("instance","kind")}
        state = entry["state"]
        if state=="painting":
            state="unknown"
        if kind=="notice":
            if state in ("requesting","not_presented"):
                self.client.manage("chat.notice.release",**args,presentation_id=rid)
                return
            action = "chat.notice.presented" if state=="presented" else "chat.notice.unknown"
            self.client.manage(action,**args,presentation_id=rid)
            return
        action = "expression.v2." if kind=="speech" else "expression.presentation."
        lease = entry.get("lease")
        principal = {k:args[k] for k in ("persona_id","session_id","utterance_id" if kind=="speech" else "turn_id")}
        principal["presenter_id"]="webview-v2."+rid
        if not lease:
            outstanding=self.client.manage(action+"pending_receipts",**principal)
            lease=next((v for v in outstanding if (v["publication_id"],v["revision"],v["ordinal"])==
                        (args["publication_id"],args["revision"],args["ordinal"])),None)
            if lease is None:
                return
            state="not_presented"
        decision=dict(principal,lease_id=lease["lease_id"],body_hash=lease["body_hash"],receipt_id=rid,state=state,
                      webview=True,publication_id=args["publication_id"],revision=args["revision"],ordinal=args["ordinal"])
        decision["fragment_id" if kind=="speech" else "utterance_id"]=lease["id"]
        self.client.manage(action+"ack",**decision)
