"""Durable chat orchestration; no window or QQ SDK dependencies."""
import asyncio
from collections import defaultdict
import json
import re
import time
from typing import Protocol

import aiohttp

from .models import LOCAL_USER_ID, RuntimeErrorBase, identifier, require_text, timestamp
from .security import unprotect
from .service import RuntimeService
from .progress import record as record_progress, progress


class ModelError(RuntimeErrorBase):
    pass


class ModelClient(Protocol):
    async def complete(self, model: dict, messages: list[dict]) -> tuple[str, dict | None]: ...


class ChatCompletionsClient:
    async def complete(self, model, messages):
        if not model["base_url"] or not model["name"]:
            raise ModelError("model_not_configured")
        headers = {"Content-Type": "application/json"}
        if model["encrypted_key"]:
            headers["Authorization"] = "Bearer " + unprotect(model["encrypted_key"])
        payload = {"model": model["name"], "messages": messages,
                   "max_tokens": model["max_tokens"], "stream": False}
        timeout = aiohttp.ClientTimeout(total=model["timeout"], connect=min(5, model["timeout"]))
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as client:
                async with client.post(model["base_url"].rstrip("/") + "/chat/completions",
                                       headers=headers, json=payload, allow_redirects=False) as response:
                    if response.status != 200:
                        raise ModelError(f"provider_http_{response.status}")
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        raw.extend(chunk)
                        if len(raw) > 2 * 1024 * 1024:
                            raise ModelError("provider_response_too_large")
                    data = json.loads(raw)
                    text = data["choices"][0]["message"]["content"]
                    if not isinstance(text, str) or not text.strip():
                        raise ModelError("provider_empty_response")
                    usage = data.get("usage")
                    if not isinstance(usage, dict):
                        usage = None
                    else:
                        usage = {k: v for k, v in usage.items() if k in (
                            "prompt_tokens", "completion_tokens", "total_tokens") and type(v) is int and v >= 0}
                    return text, usage
        except ModelError:
            raise
        except (TimeoutError, asyncio.TimeoutError):
            raise ModelError("provider_timeout") from None
        except aiohttp.ClientError:
            raise ModelError("provider_network_error") from None
        except (ValueError, KeyError, IndexError, TypeError):
            raise ModelError("provider_invalid_response") from None


def terms(text):
    result = set(re.findall(r"[a-z0-9_]+", text.lower()))
    for segment in re.findall(r"[\u3400-\u9fff]+", text):
        if len(segment) > 1:
            result.update(segment[i:i+2] for i in range(len(segment)-1))
            result.add(segment)
    return result


def recall(query, memories):
    query_terms = terms(query)
    scored = []
    for memory in memories:
        score = len(query_terms & terms(memory["text"]))
        if query.strip() and query.strip().lower() in memory["text"].lower():
            score += 3
        if score:
            scored.append((score, memory["updated_at"], memory["id"], memory))
    scored.sort(key=lambda item: item[:3], reverse=True)
    return [item[3] for item in scored[:8]]


class ChatEngine:
    def __init__(self, store, config, client=None):
        self.store, self.config = store, config
        self.client = client or ChatCompletionsClient()
        self.service = RuntimeService(store)
        self.tasks = {}
        self.locks = defaultdict(asyncio.Lock)
        self.draining = False

    def recover(self):
        with self.store.connection(write=True) as conn:
            for row in conn.execute("SELECT id FROM turns WHERE status IN ('queued','running')").fetchall():
                record_progress(conn, row[0], "interrupted")
            for row in conn.execute("SELECT turn_id FROM deliveries WHERE state='sending'").fetchall():
                record_progress(conn, row[0], "delivery_uncertain")
            conn.execute("UPDATE turns SET status='interrupted', error_code='service_restarted', updated_at=? "
                         "WHERE status IN ('queued','running')", (timestamp(),))
            conn.execute("UPDATE attempts SET status='interrupted', error_code='service_restarted' WHERE status='running'")
            conn.execute("UPDATE deliveries SET state='uncertain', updated_at=? WHERE state='sending'", (timestamp(),))

    @staticmethod
    def _session(conn, persona_id, channel, session_id=None, fresh=False):
        if session_id:
            RuntimeService._owned(conn, "sessions", session_id, persona_id)
            other = conn.execute("SELECT channel FROM channel_sessions WHERE session_id=? AND channel!=?",
                                 (session_id, channel)).fetchone()
            historic_channel = conn.execute("SELECT channel FROM turns WHERE session_id=? AND channel!=? LIMIT 1",
                                              (session_id, channel)).fetchone()
            if other or historic_channel:
                raise RuntimeErrorBase("Session belongs to a different channel")
        else:
            row = conn.execute("SELECT session_id FROM channel_sessions WHERE persona_id=? AND channel=?",
                               (persona_id, channel)).fetchone()
            session_id = row[0] if row and not fresh else None
        if not session_id:
            session_id = identifier()
            conn.execute("INSERT INTO sessions VALUES (?, ?, ?, 'private', ?)",
                         (session_id, persona_id, LOCAL_USER_ID, timestamp()))
        conn.execute("INSERT INTO channel_sessions VALUES (?, ?, ?, ?) ON CONFLICT(persona_id,channel) "
                     "DO UPDATE SET session_id=excluded.session_id", (persona_id, channel, LOCAL_USER_ID, session_id))
        return session_id

    def session(self, channel="desktop", session_id=None, fresh=False):
        if channel not in ("desktop", "qq"):
            raise RuntimeErrorBase("Unsupported channel")
        with self.store.connection(write=True) as conn:
            persona_id = self.service._persona(conn)["id"]
            return {"session_id": self._session(conn, persona_id, channel, session_id, fresh),
                    "persona_id": persona_id, "channel": channel}

    def desktop_sessions(self):
        with self.store.connection() as conn:
            persona_id = self.service._persona(conn)["id"]
            return [dict(row) for row in conn.execute(
                "SELECT s.* FROM sessions s WHERE s.persona_id=? AND s.user_id=? "
                "AND NOT EXISTS (SELECT 1 FROM turns t WHERE t.session_id=s.id AND t.channel='qq') "
                "AND NOT EXISTS (SELECT 1 FROM channel_sessions c WHERE c.session_id=s.id AND c.channel='qq') "
                "ORDER BY s.created_at", (persona_id, LOCAL_USER_ID))]

    async def submit(self, channel, text, request_id, session_id=None, *, test=False, origin=None, persona_id=None):
        if self.draining:
            raise RuntimeErrorBase("service_draining")
        require_text(text, "text")
        require_text(request_id, "request_id")
        if channel not in ("desktop", "qq", "test") or (channel == "test") != test:
            raise RuntimeErrorBase("Unsupported channel")
        if len(text) > 24000 or len(request_id) > 200:
            raise RuntimeErrorBase("Input is too long")
        source_key = channel + ":" + request_id
        with self.store.connection(write=True) as conn:
            old = conn.execute("SELECT id FROM turns WHERE source_key=?", (source_key,)).fetchone()
            if old:
                previous = self._view(conn, old[0])
                if not test and (previous["text"] != text or (persona_id and previous["persona_id"] != persona_id)
                                 or (session_id and previous["session_id"] != session_id)
                                 or (origin is not None and previous["origin"] != origin)):
                    raise RuntimeErrorBase("submission_conflict")
                return previous
            persona_id = None if test else self.service._persona(conn, persona_id)["id"]
            waiting = conn.execute("SELECT count(*) FROM turns WHERE persona_id IS ? AND status='queued'",
                                   (persona_id,)).fetchone()[0]
            if waiting >= 10:
                raise RuntimeErrorBase("queue_full")
            if not test:
                session_id = self._session(conn, persona_id, channel, session_id)
            event_id = None if test else identifier()
            if event_id:
                conn.execute("INSERT INTO events (id,session_id,persona_id,user_id,role,text,created_at) "
                             "VALUES (?,?,?,?, 'user',?,?)",
                             (event_id, session_id, persona_id, LOCAL_USER_ID, text, timestamp()))
            turn_id = identifier()
            model = self.config.load()["model"]
            conn.execute("INSERT INTO turns (id,source_key,persona_id,session_id,channel,user_event_id,reply_event_id,status,error_code,"
                         "model_snapshot,usage_json,created_at,updated_at) VALUES (?,?,?,?,?,?,NULL,'queued',NULL,?,NULL,?,?)",
                         (turn_id, source_key, persona_id, session_id, channel, event_id,
                          json.dumps(model), timestamp(), timestamp()))
            if channel == "qq":
                identity = origin or {"sender": self.config.load()["owner_qq"], "bot": self.config.load()["bot_qq"]}
                conn.execute("UPDATE turns SET origin_json=? WHERE id=?", (json.dumps(identity), turn_id))
                conn.execute("INSERT INTO deliveries VALUES (?, 'pending', ?)", (turn_id, timestamp()))
            record_progress(conn, turn_id, "accepted")
            record_progress(conn, turn_id, "queued")
        self._schedule(turn_id)
        return self.get(turn_id)

    def _schedule(self, turn_id):
        task = asyncio.create_task(self._run(turn_id))
        self.tasks[turn_id] = task
        def finished(completed):
            if self.tasks.get(turn_id) is completed:
                self.tasks.pop(turn_id, None)
        task.add_done_callback(finished)

    @staticmethod
    def _view(conn, turn_id):
        row = conn.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
        if not row:
            raise RuntimeErrorBase("Request not found")
        value = dict(row)
        value.pop("model_snapshot")
        value["origin"] = json.loads(value.pop("origin_json"))
        value["usage"] = json.loads(value.pop("usage_json")) if row["usage_json"] else None
        for field, output in (("user_event_id", "text"), ("reply_event_id", "reply")):
            event = conn.execute("SELECT text FROM events WHERE id=?", (value[field],)).fetchone()
            value[output] = event[0] if event else None
        delivery = conn.execute("SELECT state FROM deliveries WHERE turn_id=?", (turn_id,)).fetchone()
        value["delivery"] = delivery[0] if delivery else None
        value["attempt_count"] = conn.execute("SELECT count(*) FROM attempts WHERE turn_id=?", (turn_id,)).fetchone()[0]
        context = conn.execute("SELECT details_json FROM context_uses WHERE turn_id=? ORDER BY rowid DESC LIMIT 1", (turn_id,)).fetchone()
        value['context'] = json.loads(context[0]) if context else None
        value.update(progress(conn, value))
        value["queue_position"] = conn.execute(
            "SELECT count(*) FROM turns t WHERE t.persona_id IS ? AND (t.status='running' OR "
            "(t.status='queued' AND (SELECT max(a.seq) FROM request_activity a WHERE a.turn_id=t.id "
            "AND a.event IN ('queued','retry_queued')) <= (SELECT max(a.seq) FROM request_activity a "
            "WHERE a.turn_id=? AND a.event IN ('queued','retry_queued'))))",
            (value["persona_id"], turn_id)).fetchone()[0] if value["status"] == "queued" else 0
        return value

    def get(self, turn_id):
        with self.store.connection() as conn:
            return self._view(conn, turn_id)

    def lookup(self, channel, request_id):
        if channel not in ("desktop", "qq"):
            raise RuntimeErrorBase("Unsupported channel")
        with self.store.connection() as conn:
            row = conn.execute("SELECT id FROM turns WHERE source_key=?", (channel + ":" + request_id,)).fetchone()
            return self._view(conn, row[0]) if row else None

    def history(self, session_id):
        with self.store.connection() as conn:
            persona_id = self.service._persona(conn)["id"]
            self.service._owned(conn, "sessions", session_id, persona_id)
            return [self._view(conn, row[0]) for row in conn.execute(
                "SELECT id FROM turns WHERE session_id=? ORDER BY rowid", (session_id,))]

    def history_snapshot(self, session_id, version=None):
        with self.store.connection() as conn:
            persona_id = self.service._persona(conn)["id"]
            self.service._owned(conn, "sessions", session_id, persona_id)
            latest = conn.execute("SELECT coalesce(max(a.seq),0) FROM request_activity a "
                                  "JOIN turns t ON t.id=a.turn_id WHERE t.session_id=?", (session_id,)).fetchone()[0]
            return {"version": latest, "turns": None if version == latest else [self._view(conn, row[0]) for row in conn.execute(
                "SELECT id FROM turns WHERE session_id=? ORDER BY rowid", (session_id,))]}

    def requests(self):
        with self.store.connection() as conn:
            persona_id = self.service._persona(conn)["id"]
            return [self._view(conn, row[0]) for row in conn.execute(
                "SELECT id FROM turns WHERE persona_id=? ORDER BY rowid DESC LIMIT 100", (persona_id,))]

    def _context(self, conn, turn):
        from .context_m4 import build_context
        messages, details = build_context(self, conn, turn)
        turn["_context_details"] = details
        return messages

    async def _run(self, turn_id):
        initial = self.get(turn_id)
        async with self.locks[initial["persona_id"]]:
            attempt_id = None
            try:
                with self.store.connection(write=True) as conn:
                    turn = dict(conn.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone())
                    if turn["status"] != "queued" or turn["cancelled_at"]:
                        return
                    model = json.loads(turn["model_snapshot"])
                    messages = self._context(conn, turn)
                    if not model["base_url"] or not model["name"]:
                        raise ModelError("model_not_configured")
                    from .memory_policy import project_calls
                    count = project_calls(conn, time.time()-3600)
                    if count >= self.config.load()["model"]["hourly_limit"]:
                        raise ModelError("hourly_limit_reached")
                    attempt_id = identifier()
                    conn.execute("INSERT INTO attempts VALUES (?,?,?,'running',NULL,NULL,?)",
                                 (attempt_id, turn_id, time.time(), turn["model_snapshot"]))
                    conn.execute("INSERT INTO context_uses VALUES (?,?,?,?,?,?)", (attempt_id, turn_id, turn["persona_id"], LOCAL_USER_ID,
                                 json.dumps(turn["_context_details"], ensure_ascii=False), timestamp()))
                    conn.execute("UPDATE turns SET status='running', updated_at=? WHERE id=?", (timestamp(), turn_id))
                    record_progress(conn, turn_id, "provider_started", attempt_id)
                reply, usage = await self.client.complete(model, messages)
                require_text(reply, "model reply")
                with self.store.connection(write=True) as conn:
                    record_progress(conn, turn_id, "generated", attempt_id)
                    event_id = identifier() if turn["channel"] != "test" else None
                    if event_id:
                        conn.execute("INSERT INTO events (id,session_id,persona_id,user_id,role,text,created_at) "
                                     "VALUES (?,?,?,?, 'persona',?,?)",
                                     (event_id, turn["session_id"], turn["persona_id"], LOCAL_USER_ID, reply, timestamp()))
                    encoded = json.dumps(usage) if usage is not None else None
                    conn.execute("UPDATE turns SET status='completed',reply_event_id=?,error_code=NULL,usage_json=?,updated_at=? WHERE id=?",
                                 (event_id, encoded, timestamp(), turn_id))
                    conn.execute("UPDATE attempts SET status='completed',usage_json=? WHERE id=?", (encoded, attempt_id))
                    record_progress(conn, turn_id, "persisted", attempt_id)
            except BaseException as error:
                interrupted = isinstance(error, asyncio.CancelledError)
                code = str(error) if isinstance(error, ModelError) else ("request_interrupted" if interrupted else "request_failed")
                with self.store.connection(write=True) as conn:
                    if attempt_id and not conn.execute("SELECT 1 FROM attempts WHERE id=?", (attempt_id,)).fetchone():
                        attempt_id = None
                    record_progress(conn, turn_id, "interrupted" if interrupted else "failed", attempt_id)
                    conn.execute("UPDATE turns SET status=?,error_code=?,updated_at=? WHERE id=?",
                                 ("interrupted" if interrupted else "failed", code, timestamp(), turn_id))
                    if attempt_id:
                        conn.execute("UPDATE attempts SET status='failed',error_code=? WHERE id=?", (code, attempt_id))
                if interrupted:
                    raise

    async def retry(self, turn_id):
        if self.draining:
            raise RuntimeErrorBase("service_draining")
        with self.store.connection(write=True) as conn:
            turn = self._view(conn, turn_id)
            if turn["status"] not in ("failed", "interrupted"):
                return turn
            waiting = conn.execute("SELECT count(*) FROM turns WHERE persona_id IS ? AND status='queued'",
                                   (turn["persona_id"],)).fetchone()[0]
            if waiting >= 10:
                raise RuntimeErrorBase("queue_full")
            conn.execute("UPDATE turns SET status='queued',cancelled_at=NULL,error_code=NULL,model_snapshot=?,updated_at=? WHERE id=?",
                         (json.dumps(self.config.load()["model"]), timestamp(), turn_id))
            record_progress(conn, turn_id, "retry_queued")
        self._schedule(turn_id)
        return self.get(turn_id)

    def cancel_queued(self, turn_id):
        with self.store.connection(write=True) as conn:
            turn = self._view(conn, turn_id)
            if turn["cancelled_at"]:
                return turn
            if turn["status"] != "queued":
                raise RuntimeErrorBase("only_queued_can_cancel")
            now = timestamp()
            conn.execute("UPDATE turns SET status='interrupted',cancelled_at=?,error_code='cancelled_by_user',updated_at=? WHERE id=?",
                         (now, now, turn_id))
            record_progress(conn, turn_id, "cancelled")
            result = self._view(conn, turn_id)
        task = self.tasks.get(turn_id)
        if task:
            task.cancel()
        return result

    def claim_notice(self, turn_id, kind):
        if kind not in ("waiting", "failure"):
            raise RuntimeErrorBase("Unknown service notice")
        with self.store.connection(write=True) as conn:
            turn = self._view(conn, turn_id)
            valid = turn["channel"] == "qq" and not turn["cancelled_at"] and (
                turn["status"] in (("queued", "running") if kind == "waiting" else ("failed", "interrupted")))
            if kind == "waiting" and not self.config.load()["interaction"]["qq_wait_notice"]:
                valid = False
            if not valid:
                return {"claimed": False}
            inserted = conn.execute("INSERT OR IGNORE INTO service_notices VALUES (?,?,?)", (turn_id, kind, timestamp()))
            return {"claimed": inserted.rowcount == 1}

    def delivery(self, turn_id, action):
        with self.store.connection(write=True) as conn:
            turn = self._view(conn, turn_id)
            if turn["channel"] != "qq" or turn["status"] != "completed":
                raise RuntimeErrorBase("Request is not ready for QQ delivery")
            row = conn.execute("SELECT state FROM deliveries WHERE turn_id=?", (turn_id,)).fetchone()
            state = row[0]
            if action == "claim":
                origin = conn.execute("SELECT origin_json FROM turns WHERE id=?", (turn_id,)).fetchone()[0]
                previous = conn.execute(
                    "SELECT 1 FROM turns t LEFT JOIN deliveries d ON d.turn_id=t.id "
                    "WHERE t.channel='qq' AND t.origin_json=? "
                    "AND t.rowid < (SELECT rowid FROM turns WHERE id=?) "
                    "AND (t.status IN ('queued','running') OR (t.status='completed' AND d.state IN ('pending','sending'))) LIMIT 1",
                    (origin, turn_id)).fetchone()
                allowed = state == "pending" and not previous
                if allowed:
                    conn.execute("UPDATE deliveries SET state='sending',updated_at=? WHERE turn_id=?", (timestamp(), turn_id))
                    record_progress(conn, turn_id, "delivery_started")
                return {"claimed": allowed, "state": "sending" if allowed else state}
            if action not in ("sent", "uncertain", "failed") or state != "sending":
                raise RuntimeErrorBase("Invalid delivery transition")
            conn.execute("UPDATE deliveries SET state=?,updated_at=? WHERE turn_id=?", (action, timestamp(), turn_id))
            record_progress(conn, turn_id, {"sent": "delivery_confirmed", "uncertain": "delivery_uncertain", "failed": "delivery_failed"}[action])
            return {"state": action}

    async def wait(self):
        if self.tasks:
            await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
