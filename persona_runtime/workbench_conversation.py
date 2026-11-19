"""Scope-checked desktop chat operations for the approved WebView page."""

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from urllib.parse import quote
import json

from .config import ConfigStore, atomic_json
from .desktop_client import LocalAPIError, LocalClient, TransportUnknown
from .models import LOCAL_USER_ID, RuntimeErrorBase, identifier
from .scene_publication import SOURCE, ScenePublication
from .security import protect, unprotect
from .storage import Store
from .tk_notice import CONTRACT, waiting
from .workbench_history import VISIBLE_TURN, WorkbenchHistory, open_history, require_session

ACK_TERMINAL = {
    "speech_publication_closed", "speech_receipt_conflict",
    "knowledge_scope_changed", "knowledge_request_inactive",
    "desktop_notice_not_pending", "search_notice_superseded",
}


@dataclass
class Presentation:
    ticket: str
    turn_id: str
    scope: dict
    kind: str
    text: str | None
    decision: bool | None = None


class WorkbenchConversation:
    def __init__(self, paths, visible, available):
        self.store = Store(paths)
        self.history = WorkbenchHistory(paths)
        self.config = ConfigStore(paths)
        self.client = LocalClient(self.config)
        self.visible = visible
        self.available = available
        self.lock = RLock()
        self.presenter = identifier()
        self.presentation = None
        self.uncertain_offer = None
        self.draft_path = paths.home / "runtime/webview-drafts.dpapi.json"
        self.draft_revisions = {}

    def _ready(self):
        if not self.available():
            raise RuntimeErrorBase("工作台服务尚未就绪或正在退出。")

    def _check_session(self, persona_id, session_id, *, writable=False, selected=False):
        with open_history(self.store) as db:
            session = require_session(db, persona_id, session_id)
            if writable and (session["state"] == "sealed" or session["current"] not in (None, session_id)):
                raise RuntimeErrorBase("请在当前桌面会话发送消息。")
            if selected and Store.current(db, required=False) != persona_id:
                raise RuntimeErrorBase("请先设为当前人物。")

    def _configured(self):
        model = self.config.public()["model"]
        from .provider_policy import require_generation_provider
        try:
            require_generation_provider(model)
        except ValueError:
            return False
        return model["has_key"]

    def _pending(self, persona_id, session_id):
        if not self.client.journal.path.is_file():
            return None
        return next((row for row in self.client.journal.pending()
                     if (row["payload"].get("persona_id"), row["payload"].get("session_id"))
                     == (persona_id, session_id)), None)

    @staticmethod
    def _pending_view(row):
        if row is None:
            return None
        return {"id": row["payload"]["request_id"], "state": row["state"]}

    def state(self, persona_id, session_id):
        activity = self.history.activity(persona_id, session_id)
        with self.lock:
            pending = self._pending(persona_id, session_id)
            return activity | {
                "configured": self._configured(), "available": self.available(),
                "pending": self._pending_view(pending),
            }

    def activate(self, persona_id, session_id=None, fresh=False):
        with self.lock:
            self._ready()
            if session_id is not None:
                self._check_session(persona_id, session_id, writable=True)
            self._settle_previous()
            # Persona selection is an explicit chat action. Viewing history uses the read bridge.
            self.client.manage("persona.use", persona_id=persona_id)
            result = self.client.manage("session.select", channel="desktop", session_id=session_id, fresh=fresh is True)
            if result["persona_id"] != persona_id:
                raise RuntimeErrorBase("当前人物已经变化，请重新选择。")
            return {"id": result["session_id"]}

    def _drafts(self):
        if not self.draft_path.is_file():
            return {}
        envelope = json.loads(self.draft_path.read_text(encoding="utf-8"))
        if envelope.get("version") != 1:
            raise RuntimeErrorBase("本机草稿格式不兼容。")
        drafts = json.loads(unprotect(envelope["encrypted"]))
        if not isinstance(drafts, dict):
            raise RuntimeErrorBase("本机草稿无法读取。")
        return drafts

    def _store_drafts(self, drafts):
        atomic_json(self.draft_path, {"version": 1, "encrypted": protect(json.dumps(drafts, ensure_ascii=False))})

    def get_draft(self, persona_id, session_id):
        self._check_session(persona_id, session_id)
        with self.lock:
            return self._drafts().get(session_id, "")

    def save_draft(self, persona_id, session_id, text, revision):
        self._check_session(persona_id, session_id)
        if not isinstance(text, str) or len(text) > 24000 or type(revision) is not int or revision < 0:
            raise RuntimeErrorBase("草稿格式不正确。")
        with self.lock:
            if revision <= self.draft_revisions.get(session_id, -1):
                return
            drafts = self._drafts()
            if text != drafts.get(session_id, ""):
                if text:
                    drafts[session_id] = text
                else:
                    drafts.pop(session_id, None)
                self._store_drafts(drafts)
            self.draft_revisions[session_id] = revision

    def _clear_accepted_draft(self, session_id, text, revision):
        if revision is not None and revision < self.draft_revisions.get(session_id, -1):
            return
        drafts = self._drafts()
        if text is not None and drafts.get(session_id) == text:
            drafts.pop(session_id)
            self._store_drafts(drafts)
            if revision is not None:
                self.draft_revisions[session_id] = revision

    def _accepted(self, result, persona_id, session_id, text, revision=None):
        if (result.get("persona_id"), result.get("session_id"), result.get("channel")) != (
            persona_id, session_id, "desktop",
        ):
            raise RuntimeErrorBase("消息受理记录与当前会话不符。")
        try:
            self._clear_accepted_draft(session_id, text, revision)
        except (OSError, ValueError, RuntimeErrorBase):
            return {"state": "accepted", "draft_warning": True}
        return {"state": "accepted"}

    def send(self, persona_id, session_id, text, request_id, revision):
        if (not isinstance(text, str) or not text.strip() or len(text) > 24000
                or not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200
                or type(revision) is not int or revision < 0):
            raise RuntimeErrorBase("消息或请求标识不正确。")
        with self.lock:
            self._ready()
            self._check_session(persona_id, session_id, writable=True, selected=True)
            if not self._configured():
                raise RuntimeErrorBase("请先配置主模型连接。")
            if self._pending(persona_id, session_id):
                raise RuntimeErrorBase("请先核对本会话的未确认消息。")
            self._settle_previous()
            payload = {"persona_id": persona_id, "session_id": session_id, "channel": "desktop",
                       "text": text, "request_id": request_id, "desktop_presentation": CONTRACT}
            try:
                result = self.client.submit_input(payload)
            except TransportUnknown:
                return {"state": "unconfirmed"}
            return self._accepted(result, persona_id, session_id, text, revision)

    def check_submission(self, persona_id, session_id, request_id):
        with self.lock:
            self._check_session(persona_id, session_id)
            row = self._pending(persona_id, session_id)
            if row is not None:
                request_id = row["payload"]["request_id"]
            if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
                return {"state": "none"}
            try:
                result = self.client.lookup({"request_id": request_id})
            except TransportUnknown:
                return {"state": "unconfirmed"}
            if result is None:
                if row is not None:
                    self.client.journal.state(request_id, "not_accepted")
                return {"state": "not_accepted"}
            text = row["payload"]["text"] if row else None
            receipt = self._accepted(result, persona_id, session_id, text)
            if row is not None:
                self.client.journal.forget(request_id)
            return receipt

    def retry_submission(self, persona_id, session_id):
        with self.lock:
            self._ready()
            self._check_session(persona_id, session_id, writable=True, selected=True)
            row = self._pending(persona_id, session_id)
            if not row or row["state"] != "not_accepted":
                raise RuntimeErrorBase("请先核对原请求。")
            if not self._configured():
                raise RuntimeErrorBase("请先配置主模型连接。")
            self._settle_previous()
            payload = row["payload"]
            try:
                result = self.client.submit_input(payload)
            except TransportUnknown:
                return {"state": "unconfirmed"}
            return self._accepted(result, persona_id, session_id, payload["text"])

    def cancel(self, persona_id, session_id, kind, target_id):
        with self.lock:
            self._ready()
            self._check_session(persona_id, session_id, selected=True)
            if not isinstance(target_id, str) or not 1 <= len(target_id) <= 200:
                raise RuntimeErrorBase("停止目标不正确。")
            self._settle_previous()
            encoded_id = quote(target_id, safe="")
            if kind == "turn":
                turn = self.client.call("GET", "/v1/requests/" + encoded_id)
                if (turn["persona_id"], turn["session_id"], turn["channel"]) != (persona_id, session_id, "desktop"):
                    raise RuntimeErrorBase("请求与会话不符。")
                self.client.cancel_request(turn)
            elif kind == "batch":
                self._batch_action(persona_id, session_id, encoded_id, "cancel")
            else:
                raise RuntimeErrorBase("未知停止操作。")

    def _batch_action(self, persona_id, session_id, encoded_id, action):
        path = "/v2/batches/" + encoded_id
        batch = self.client.call("GET", path)
        if (batch["persona_id"], batch["session_id"], batch["channel"]) != (persona_id, session_id, "desktop"):
            raise RuntimeErrorBase("输入批次与会话不符。")
        if batch["turn_id"]:
            raise RuntimeErrorBase("此批输入已生成请求，请刷新会话状态。")
        if action == "resume" and batch["state"] != "interrupted_buffer":
            raise RuntimeErrorBase("只有中断的输入批次可以继续。")
        self.client.call("POST", path + "/" + action, {
            "persona_id": persona_id, "expected_revision": batch["revision"], "operation_id": identifier(),
        })

    def resume_batch(self, persona_id, session_id, batch_id):
        with self.lock:
            self._ready()
            self._check_session(persona_id, session_id, writable=True, selected=True)
            if not self._configured() or self._pending(persona_id, session_id):
                raise RuntimeErrorBase("请先核对未确认消息并配置模型连接。")
            if not isinstance(batch_id, str) or not 1 <= len(batch_id) <= 200:
                raise RuntimeErrorBase("输入批次标识不正确。")
            self._settle_previous()
            self._batch_action(persona_id, session_id, quote(batch_id, safe=""), "resume")

    def _next_presentation(self, persona_id, session_id):
        with open_history(self.store) as db:
            session = require_session(db, persona_id, session_id)
            if (Store.current(db, required=False) != persona_id or session["state"] == "sealed"
                    or session["current"] not in (None, session_id)):
                return None
            # Notice identities remain scoped to canonical desktop turns.
            for notice in waiting(db, session_id):
                if db.execute("SELECT 1 FROM turns WHERE " + VISIBLE_TURN + " AND id=?",
                              (persona_id, session_id, notice["turn_id"])).fetchone():
                    return "notice", notice
            turn = db.execute(
                "SELECT t.id FROM turns t JOIN expression_speech_units u ON u.turn_id=t.id "
                "JOIN expression_speech_publications p ON p.utterance_id=u.id "
                "WHERE t.persona_id=? AND t.session_id=? AND t.channel='desktop' AND u.source_kind=? "
                "AND t.source_key NOT LIKE 'imported.%' AND p.state IN ('pending','partial') ORDER BY t.rowid LIMIT 1",
                (persona_id, session_id, SOURCE),
            ).fetchone()
            if turn is None:
                return None
            speech = ScenePublication.view(db, turn["id"])
            item = next((item for item in speech["items"] if item["state"] != "presented"), None)
            if not item:
                return None
            if speech["visible"]:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(speech["visible"][-1]["presented_at"])
                if age.total_seconds() * 1000 < item["delay_ms"]:
                    return None
            return "scene", {"turn_id": turn["id"], "utterance_id": speech["utterance_id"],
                             "publication_id": speech["publication_id"], "revision": speech["revision"],
                             "ordinal": item["ordinal"]}

    def _lease(self, turn_id, owner, item, body):
        ticket = identifier()
        return Presentation(ticket, turn_id, owner | {
            "fragment_id": item["id"], "lease_id": item["lease_id"],
            "body_hash": item["body_hash"], "receipt_id": ticket,
        }, "scene", body)

    def _find_lost_offer(self):
        if self.uncertain_offer is None:
            return
        turn_id, owner, ordinal, revision = self.uncertain_offer
        leases = self.client.manage("expression.v2.pending_receipts", **owner)
        item = next((item for item in leases if item["ordinal"] == ordinal and item["revision"] == revision), None)
        if item:
            self.presentation = self._lease(turn_id, owner, item, None)
            self.presentation.decision = False
        self.uncertain_offer = None

    def _complete(self):
        item = self.presentation
        if item is None or item.decision is None:
            return
        try:
            if item.kind == "notice":
                if item.decision:
                    self.client.manage("chat.notice.presented", **item.scope)
            else:
                self.client.manage("expression.v2.ack", **item.scope,
                                   state="presented" if item.decision else "not_presented")
        except LocalAPIError as error:
            if str(error) not in ACK_TERMINAL:
                raise
        self.presentation = None

    def _settle_previous(self):
        self._find_lost_offer()
        if self.presentation is not None and self.presentation.decision is None:
            self.presentation.decision = False
        self._complete()

    def offer(self, persona_id, session_id):
        with self.lock:
            self._ready()
            if not self.visible():
                return None
            self._settle_previous()
            target = self._next_presentation(persona_id, session_id)
            if target is None:
                return None
            kind, item = target
            scope = {"persona_id": persona_id, "session_id": session_id}
            turn_id = item["turn_id"]
            if kind == "notice":
                args = scope | {"turn_id": turn_id, "notice_id": item["notice_id"]}
                result = self.client.manage("chat.notice.preview", **args)
                if result.get("published"):
                    return None
                self.presentation = Presentation(identifier(), turn_id, args, kind, result["text"])
            else:
                owner = scope | {"utterance_id": item["utterance_id"], "presenter_id": self.presenter}
                self.uncertain_offer = (turn_id, owner, item["ordinal"], item["revision"])
                try:
                    result = self.client.manage("expression.v2.offer", **owner, publication_id=item["publication_id"],
                                                revision=item["revision"], ordinal=item["ordinal"])
                except LocalAPIError:
                    self.uncertain_offer = None
                    raise
                if result.get("state") != "offered":
                    self.uncertain_offer = None
                    return None
                if not result.get("lease_id"):
                    self._find_lost_offer()
                    self._complete()
                    return None
                self.uncertain_offer = None
                self.presentation = self._lease(turn_id, owner, result, result["body"])
            return {"ticket": self.presentation.ticket, "turn": turn_id, "text": self.presentation.text}

    def acknowledge(self, ticket, shown):
        with self.lock:
            item = self.presentation
            if item is None or item.ticket != ticket:
                return
            if item.decision is None:
                item.decision = shown is True and self.visible() and item.text is not None
            self._complete()

    def settle_on_close(self):
        with self.lock:
            try:
                self._settle_previous()
            except RuntimeErrorBase:
                # Runtime recovery keeps a lost acknowledgement on its existing identity.
                pass
