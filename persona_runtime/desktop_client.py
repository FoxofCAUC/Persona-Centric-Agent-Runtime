"""Synchronous local API client for background desktop tasks."""
import json
import http.client
import urllib.error
import urllib.request
from urllib.parse import quote

from .models import RuntimeErrorBase
from .submissions import SubmissionJournal


class LocalAPIError(RuntimeErrorBase):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


class TransportUnknown(RuntimeErrorBase):
    pass


class LocalClient:
    def __init__(self, config):
        self.config = config
        self.journal = SubmissionJournal(config.paths, "desktop")

    def call(self, method, path, value=None, timeout=8):
        cfg = self.config.load()
        request = urllib.request.Request(f"http://127.0.0.1:{cfg['service_port']}" + path,
            data=json.dumps(value,ensure_ascii=False).encode("utf-8") if value is not None else None, method=method,
            headers={"Authorization": "Bearer " + self.config.token(), "Content-Type": "application/json"})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read(4096)).get("error", "local_api_error")
            except (ValueError, OSError):
                detail = "local_api_error"
            raise LocalAPIError(str(detail), error.code) from None
        except (OSError, ValueError, http.client.HTTPException):
            raise TransportUnknown("人格服务尚未连接或响应未确认；发送过的消息请先找回受理结果，不要换 ID 重发。") from None

    def manage(self, action, **args):
        return self.call("POST", "/v1/manage", {"action": action, "args": args})

    def lookup(self, payload):
        try:
            return self.call("GET", "/v1/submissions/desktop/" + quote(payload["request_id"], safe=""))
        except LocalAPIError as error:
            if error.status == 404 and str(error) == "submission_not_found":
                return None
            raise

    def submit(self, payload):
        # Persist the intent first. Repeated clicks on the same unconfirmed draft reuse its ID.
        payload = self.journal.remember(payload, match_draft=True)
        try:
            result = self.lookup(payload)
        except TransportUnknown:
            self.journal.state(payload["request_id"], "unconfirmed")
            raise
        # A failed lookup says nothing about whether a prior POST was accepted.
        # Preserve its encrypted intent even for authorization/configuration errors.
        if result is None:
            try:
                result = self.call("POST", "/v1/chat", payload)
            except LocalAPIError as error:
                if str(error) in ("queue_full", "service_draining", "submission_conflict"):
                    self.journal.forget(payload["request_id"])
                raise
            except TransportUnknown:
                self.journal.state(payload["request_id"], "unconfirmed")
                raise
        self.journal.forget(payload["request_id"])
        return result

    def reconcile(self):
        result = []
        for row in self.journal.pending():
            turn = self.lookup(row["payload"])
            if turn:
                self.journal.forget(row["payload"]["request_id"])
            else:
                self.journal.state(row["payload"]["request_id"], "not_accepted")
            result.append({"payload": row["payload"], "turn": turn})
        return result
