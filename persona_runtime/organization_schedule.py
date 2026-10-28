"""Persisted local-time daily full scans and optional idle incremental scans."""
import json
import time
from datetime import datetime

from .models import LOCAL_USER_ID, RuntimeErrorBase, identifier, timestamp
from .organization import encoded, eligible_turns


class OrganizationSchedule:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store

    def initialize(self):
        with self.store.connection(write=True) as conn:
            conn.execute("INSERT OR IGNORE INTO daily_updates VALUES ('daily',NULL,?,'scheduled',?,'{}',?)", (LOCAL_USER_ID, time.time()+86400, timestamp()))

    def status(self):
        with self.store.connection() as conn:
            row = dict(conn.execute("SELECT * FROM daily_updates WHERE id='daily'").fetchone())
            row["progress"] = json.loads(row.pop("payload_json"))
            row["next_local_time"] = datetime.fromtimestamp(row["next_epoch"]).astimezone().isoformat(timespec="seconds")
            row["enabled"] = self.engine.config.load()["organization"]["daily_enabled"]
            row["model_ready"] = bool(self.engine.model()['base_url'] and self.engine.model()['name'])
            row["scope"] = "current_selected_persona_at_start"
            row['total_turns'] = sum(s.get('total_turns',0) for s in row['progress'].get('sessions',[]))
            row['remaining_turns'] = 0
            for session in row['progress'].get('sessions',[])[row['progress'].get('index',0):]:
                row['remaining_turns'] += conn.execute(
                    "SELECT count(*) FROM turns t WHERE session_id=? AND status='completed' AND t.rowid>? AND t.rowid<=? "
                    "AND NOT EXISTS (SELECT 1 FROM source_exclusions x WHERE x.event_id IN (t.user_event_id,t.reply_event_id) OR x.event_id IN (SELECT event_id FROM input_batch_members WHERE batch_id=t.input_batch_id))",
                    (session['id'],session['cursor'],session['upper'])).fetchone()[0]
            row["automatic"] = self.automation_status(conn)
            row["excluded"] = [dict(r) for r in conn.execute("SELECT * FROM source_exclusions WHERE persona_id=? ORDER BY created_at DESC", (self.store.current(conn, required=False),))]
            return row

    def automation_status(self, conn):
        row = conn.execute("SELECT * FROM automation WHERE persona_id=?", (self.store.current(conn, required=False),)).fetchone()
        return dict(row) if row else {"enabled": False}

    def set_automatic(self, enabled):
        if type(enabled) is not bool:
            raise RuntimeErrorBase("enabled_must_be_boolean")
        if enabled and not (self.engine.model()["base_url"] and self.engine.model()["name"]):
            raise RuntimeErrorBase("organization_model_not_configured")
        with self.store.connection(write=True) as conn:
            persona_id = self.store.current(conn)
            conn.execute("INSERT INTO automation VALUES (?,?,?,?,?) ON CONFLICT(persona_id) DO UPDATE SET enabled=excluded.enabled,model_fingerprint=excluded.model_fingerprint,enabled_at=excluded.enabled_at",
                         (persona_id, LOCAL_USER_ID, int(enabled), self.engine.model_fingerprint(), timestamp()))
            if enabled:
                for row in conn.execute("SELECT id FROM sessions WHERE persona_id=?", (persona_id,)).fetchall():
                    boundary = conn.execute("SELECT coalesce(max(rowid),0) FROM turns WHERE session_id=?", (row[0],)).fetchone()[0]
                    conn.execute("INSERT INTO organization_progress VALUES (?,?,?,?,?,NULL) ON CONFLICT(session_id) DO UPDATE SET boundary=excluded.boundary,cursor=excluded.cursor,blocked_task=NULL", (row[0], persona_id, LOCAL_USER_ID, boundary, boundary))
            else:
                conn.execute("UPDATE organization_tasks SET status='cancelled',error_code='automation_disabled' WHERE persona_id=? AND automatic=1 AND status IN ('queued','waiting_quota')", (persona_id,))
        return self.status()

    def pause_changed_models(self):
        fingerprint = self.engine.model_fingerprint()
        with self.store.connection(write=True) as conn:
            changed = [r[0] for r in conn.execute("SELECT persona_id FROM automation WHERE enabled=1 AND model_fingerprint!=?", (fingerprint,))]
            for persona_id in changed:
                conn.execute("UPDATE automation SET enabled=0 WHERE persona_id=?", (persona_id,))
                conn.execute("UPDATE organization_tasks SET status='cancelled',error_code='model_changed' WHERE persona_id=? AND automatic=1 AND status IN ('queued','waiting_quota')", (persona_id,))
            # Daily work freezes the configured model. Changing it pauses the
            # cycle until an explicit resume; the next due time remains visible.
            row = conn.execute("SELECT * FROM daily_updates WHERE id='daily'").fetchone()
            payload = json.loads(row["payload_json"])
            if not self.engine.config.load()['organization']['daily_enabled'] and row['status'] in ('running','waiting_quota'):
                conn.execute("UPDATE daily_updates SET status='paused',updated_at=? WHERE id='daily'", (timestamp(),))
                conn.execute("UPDATE organization_tasks SET status='cancelled',error_code='daily_disabled' WHERE automatic=2 AND status IN ('queued','waiting_quota')")
            if payload.get("model_fingerprint") and payload["model_fingerprint"] != fingerprint and row["status"] in ('running','waiting_quota'):
                conn.execute("UPDATE daily_updates SET status='paused_model',updated_at=? WHERE id='daily'", (timestamp(),))
                conn.execute("UPDATE organization_tasks SET status='cancelled',error_code='model_changed' WHERE automatic=2 AND status IN ('queued','waiting_quota')")

    def resume_daily(self):
        if not self.engine.config.load()['organization']['daily_enabled']:
            raise RuntimeErrorBase('daily_update_disabled')
        with self.store.connection(write=True) as conn:
            row = conn.execute("SELECT * FROM daily_updates WHERE id='daily'").fetchone()
            payload = json.loads(row["payload_json"])
            last = payload.get("last_task")
            if last:
                task = conn.execute("SELECT * FROM organization_tasks WHERE id=?", (last,)).fetchone()
                if task["status"] in ('running','queued','waiting_quota'):
                    raise RuntimeErrorBase("task_still_active")
                if task["status"] != 'completed':
                    payload["last_task"] = None
            payload["model_fingerprint"] = self.engine.model_fingerprint()
            conn.execute("UPDATE daily_updates SET status=?,payload_json=?,updated_at=? WHERE id='daily'", ('running' if payload.get('sessions') else 'scheduled', encoded(payload), timestamp()))
        return self.status()

    def run_daily_now(self):
        status = self.status()
        if not status['enabled']:
            raise RuntimeErrorBase('daily_update_disabled')
        if not status['model_ready']:
            raise RuntimeErrorBase('organization_model_not_configured')
        if status['status'] != 'scheduled':
            raise RuntimeErrorBase('daily_cycle_already_active_or_blocked')
        now = time.time()
        with self.store.connection(write=True) as conn:
            conn.execute("UPDATE daily_updates SET next_epoch=? WHERE id='daily'", (now,))
        self.daily_tick(now)
        return self.status()

    def _save(self, state, payload, persona_id=None, next_epoch=None):
        with self.store.connection(write=True) as conn:
            conn.execute("UPDATE daily_updates SET status=?,payload_json=?,updated_at=?,persona_id=coalesce(?,persona_id),next_epoch=coalesce(?,next_epoch) WHERE id='daily'", (state, encoded(payload), timestamp(), persona_id, next_epoch))

    def daily_tick(self, now=None):
        now = time.time() if now is None else now
        row = self.status()
        if not row["enabled"] or row["status"] in ("blocked", "paused_model", "paused"):
            return
        model = self.engine.model()
        if not model["base_url"] or not model["name"]:
            return  # UI shows missing model, never silently reuses the chat model.
        state, payload = row["status"], row["progress"]
        if state == "awaiting_review":
            with self.store.connection() as conn:
                pending = conn.execute("SELECT count(*) FROM candidates WHERE status='pending' AND conflicts_json!='[]' AND persona_id=?", (row["persona_id"],)).fetchone()[0]
            if pending:
                return
            self._save("scheduled", payload)
            return
        if state == "scheduled":
            if now < row["next_epoch"]:
                return
            with self.store.connection() as conn:
                persona = self.store.current(conn, required=False)
                if not persona:
                    return
                sessions = []
                for session in conn.execute("SELECT id FROM sessions WHERE persona_id=? ORDER BY created_at,id", (persona,)):
                    turns = eligible_turns(conn, session[0])
                    if turns:
                        sessions.append({"id": session[0], "upper": turns[-1]["ordinal"], "cursor": 0, "rebuild": True, 'total_turns':len(turns)})
            next_epoch = row["next_epoch"]
            while next_epoch <= now:
                next_epoch += 86400
            payload = {"sessions": sessions, "index": 0, "completed_batches": 0, "last_task": None,
                       "started_local_time": datetime.fromtimestamp(now).astimezone().isoformat(),
                       "model_fingerprint": self.engine.model_fingerprint()}
            self._save("running", payload, persona, next_epoch)
            row["persona_id"] = persona
        if payload.get("last_task"):
            with self.store.connection() as conn:
                task = dict(conn.execute("SELECT * FROM organization_tasks WHERE id=?", (payload["last_task"],)).fetchone())
            if task["status"] in ('queued','running','waiting_quota'):
                if task["status"] == 'waiting_quota':
                    self._save('waiting_quota', payload)
                return
            if task["status"] != 'completed':
                self._save('blocked', payload)
                return
            source = json.loads(task["payload_json"])
            payload["sessions"][payload["index"]]["cursor"] = source["rows"][-1]
            payload["sessions"][payload["index"]]["rebuild"] = False
            payload["completed_batches"] += 1
            payload["last_task"] = None
        while payload["index"] < len(payload["sessions"]):
            session = payload["sessions"][payload["index"]]
            with self.store.connection() as conn:
                # The daily cycle fixes its persona when it starts. A UI switch
                # affects the next cycle, never redirects this one.
                try:
                    preview = self.engine._preview(conn, session["id"], "both", session["cursor"]+1, session["upper"], row["persona_id"], session["rebuild"])
                except RuntimeErrorBase as error:
                    if str(error) != "no_eligible_turns":
                        payload["error"] = str(error)
                        self._save('blocked', payload)
                        return
                    payload["index"] += 1
                    continue
            try:
                task = self.engine.submit(preview, 'daily-'+identifier(), automatic=2, fixed_persona=row["persona_id"])
            except RuntimeErrorBase as error:
                if str(error) in ('organization_queue_full','organization_session_busy'):
                    self._save('running', payload)
                    return
                payload["error"] = str(error)
                self._save('blocked', payload)
                return
            payload["last_task"] = task['id']
            self._save('running', payload)
            return
        with self.store.connection() as conn:
            pending = conn.execute("SELECT 1 FROM candidates WHERE status='pending' AND conflicts_json!='[]' AND persona_id=? LIMIT 1", (row["persona_id"],)).fetchone()
        self._save('awaiting_review' if pending else 'scheduled', payload)

    def incremental_tick(self):
        now = time.time()
        with self.store.connection() as conn:
            latest = conn.execute("SELECT max(updated_at) FROM turns").fetchone()[0]
            if latest:
                last = datetime.fromisoformat(latest).timestamp()
                if now-last < 120:
                    return
            if conn.execute("SELECT 1 FROM organization_tasks WHERE automatic=2 AND status IN ('queued','running','waiting_quota')").fetchone():
                return
            enabled = [dict(r) for r in conn.execute("SELECT * FROM automation WHERE enabled=1")]
        for config in enabled:
            with self.store.connection(write=True) as conn:
                sessions = list(conn.execute("SELECT id FROM sessions WHERE persona_id=?", (config['persona_id'],)))
                choices = []
                for session in sessions:
                    conn.execute("INSERT OR IGNORE INTO organization_progress VALUES (?,?,?,?,?,NULL)", (session[0], config['persona_id'], LOCAL_USER_ID, 0, 0))
                    progress = conn.execute("SELECT * FROM organization_progress WHERE session_id=?", (session[0],)).fetchone()
                    if progress['blocked_task']:
                        continue
                    turns = [t for t in eligible_turns(conn, session[0]) if t['ordinal'] > max(progress['cursor'],progress['boundary'])]
                    if len(turns) >= 20:
                        choices.append((turns[0]['ordinal'], session[0], progress['cursor'], turns[-11]['ordinal']))
                if not choices:
                    continue
                _, session_id, cursor, upper = min(choices)
                try:
                    preview = self.engine._preview(conn, session_id, 'both', cursor+1, upper, config['persona_id'])
                except RuntimeErrorBase:
                    continue
            try:
                self.engine.submit(preview, 'idle-'+identifier(), automatic=1, fixed_persona=config['persona_id'])
            except RuntimeErrorBase:
                continue
            return
