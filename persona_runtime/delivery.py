"""M6.3 durable, claim-based platform delivery ledger."""
import hashlib
import json
import time
import math

from .models import LOCAL_USER_ID, RuntimeErrorBase, identifier, timestamp
from .qq_text import plain_qq_text

PART_CHARS = 450
TERMINAL = ("sent", "skipped", "resolved")


def _fingerprint(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _split(text):
    return [text[offset:offset + PART_CHARS] for offset in range(0, len(text), PART_CHARS)]


class DeliveryService:
    def __init__(self, store, monotonic=time.monotonic):
        self.store = store
        self.monotonic = monotonic
        if not hasattr(store, '_delivery_waits'):
            store._delivery_waits = {}
        self.waits = store._delivery_waits

    @staticmethod
    def _stream(conn, stream_id):
        row = conn.execute("SELECT * FROM reply_delivery_streams WHERE id=?", (stream_id,)).fetchone()
        if not row:
            raise RuntimeErrorBase("Delivery stream not found")
        return dict(row)

    @staticmethod
    def _scope(stream, persona_id=None, origin=None):
        if persona_id is not None and stream["persona_id"] != persona_id:
            raise RuntimeErrorBase("Delivery record belongs to a different persona")
        if origin is not None and json.loads(stream["origin_json"]) != origin:
            raise RuntimeErrorBase("Delivery target does not match the current QQ identity")

    @staticmethod
    def _operation(conn, operation_id, request_hash):
        row = conn.execute("SELECT request_hash,result_json FROM reply_delivery_operations WHERE operation_id=?",
                           (operation_id,)).fetchone()
        if not row:
            return None
        if row[0] != request_hash:
            raise RuntimeErrorBase("operation_conflict")
        return json.loads(row[1])

    @staticmethod
    def _record(conn, operation_id, stream_id, part_id, action, actor, reason, allow_continue, request_hash, result):
        conn.execute("INSERT INTO reply_delivery_operations VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (operation_id, stream_id, part_id, action, actor, reason, int(bool(allow_continue)), request_hash,
                      json.dumps(result, ensure_ascii=False), timestamp()))

    @staticmethod
    def _refresh(conn, stream_id):
        stream = DeliveryService._stream(conn, stream_id)
        rows = conn.execute("SELECT id,state FROM reply_delivery_parts WHERE stream_id=? ORDER BY stream_seq", (stream_id,)).fetchall()
        blocking = next((row for row in rows if row["state"] in ("failed", "uncertain")), None)
        if blocking:
            state, reason, part_id = "blocked", blocking["state"], blocking["id"]
        elif rows and all(row["state"] in TERMINAL for row in rows):
            state = "skipped" if any(row["state"] == "skipped" for row in rows) else "completed"
            reason = part_id = None
        else:
            state, reason, part_id = "open", None, None
        conn.execute("UPDATE reply_delivery_streams SET state=?,blocked_reason=?,blocked_part_id=?,revision=revision+1,updated_at=? WHERE id=?",
                     (state, reason, part_id, timestamp(), stream_id))
        return DeliveryService._stream(conn, stream_id)

    def recover(self):
        self.waits.clear()
        with self.store.connection(write=True) as conn:
            waiting = conn.execute("SELECT DISTINCT stream_id FROM reply_delivery_parts WHERE state='pending' AND wait_started_at IS NOT NULL").fetchall()
            conn.execute("UPDATE reply_delivery_parts SET wait_started_at=NULL WHERE state='pending'")
            for stream in waiting:
                self._record(conn,identifier(),stream[0],None,'wait_restart','runtime','bounded_wait_restarted',True,
                             _fingerprint({'stream':stream[0]}),{'restarted':True})
            rows = conn.execute("SELECT id,stream_id FROM reply_delivery_parts WHERE state='sending'").fetchall()
            for row in rows:
                conn.execute("UPDATE reply_delivery_parts SET state='uncertain',claim_token=NULL,error=?,updated_at=? WHERE id=?",
                             ("service_restarted_before_delivery_confirmation", timestamp(), row[0]))
                conn.execute("UPDATE reply_delivery_attempts SET state='uncertain',error=?,ended_at=? WHERE part_id=? AND state='sending'",
                             ("service_restarted_before_delivery_confirmation", timestamp(), row[0]))
                self._refresh(conn, row[1])

    def create_for_batch(self, conn, batch_id, turn):
        if turn["channel"] != "qq":
            return None
        origin = json.loads(turn.get("origin_json") or "{}")
        origin_json = json.dumps(origin, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        stream = conn.execute("SELECT id FROM reply_delivery_streams WHERE persona_id=? AND user_id=? AND channel=? "
                              "AND json_extract(origin_json,'$.sender')=? AND json_extract(origin_json,'$.bot')=? "
                              "ORDER BY created_at,id LIMIT 1",
                              (turn["persona_id"], LOCAL_USER_ID, turn["channel"], origin.get('sender'), origin.get('bot'))).fetchone()
        if stream:
            stream_id = stream[0]
            conn.execute("UPDATE reply_delivery_streams SET state='open',revision=revision+1,updated_at=? WHERE id=? AND state IN ('completed','skipped')",
                         (timestamp(), stream_id))
        else:
            stream_id = identifier()
            conn.execute("INSERT INTO reply_delivery_streams VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (stream_id, turn["persona_id"], LOCAL_USER_ID, turn["channel"], origin_json, "open", 1, None, None,
                          timestamp(), timestamp()))
        stream_seq = conn.execute("SELECT coalesce(max(stream_seq),0) FROM reply_delivery_parts WHERE stream_id=?", (stream_id,)).fetchone()[0]
        segments = conn.execute("SELECT * FROM reply_segments WHERE batch_id=? ORDER BY segment_index", (batch_id,)).fetchall()
        from .reply_transport import render_parts
        # Validate the complete batch before creating a single sendable fragment.
        rendered_groups = [(s,render_parts(s['text'],s['content_kind'] or 'chat')[1]) for s in segments]
        part_index = 0
        for segment, texts in rendered_groups:
            for text in texts:
                part_index += 1
                stream_seq += 1
                conn.execute("INSERT INTO reply_delivery_parts (id,stream_id,reply_batch_id,turn_id,persona_id,user_id,semantic_index,part_index,stream_seq,text,text_hash,state,revision,claim_token,attempt,platform_message_id,sent_at,error,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',1,NULL,0,NULL,NULL,NULL,?,?)",
                             (identifier(), stream_id, batch_id, turn["id"], turn["persona_id"], LOCAL_USER_ID,
                              segment['segment_index'], part_index, stream_seq, text, hashlib.sha256(text.encode("utf-8")).hexdigest(), timestamp(), timestamp()))
        latest = conn.execute('SELECT new_input_event_id FROM turns WHERE id=?',(turn['id'],)).fetchone()[0]
        # A batch can remain capacity-blocked while newer inputs are already accepted.
        # Resolve only authoritative same-session events, excluding this batch's members.
        if not latest:
            last_seq = conn.execute("SELECT max(e.seq) FROM events e WHERE e.id=? OR e.id IN (SELECT event_id FROM input_batch_members WHERE batch_id=?)",
                                    (turn['user_event_id'],turn.get('input_batch_id'))).fetchone()[0]
            row = conn.execute("SELECT e.id FROM events e JOIN raw_events r ON r.event_id=e.id WHERE e.session_id=? AND e.persona_id=? AND e.user_id=? AND e.seq>? AND r.channel=? AND r.bot=? AND r.peer=? ORDER BY e.seq DESC LIMIT 1",
                               (turn['session_id'],turn['persona_id'],LOCAL_USER_ID,last_seq,turn['channel'],origin.get('bot',''),origin.get('sender',''))).fetchone()
            latest = row[0] if row else None
        if latest:
            conn.execute('UPDATE turns SET new_input_event_id=? WHERE id=?',(latest,turn['id']))
            self._cancel(conn,batch_id,'new_input',latest)
        return stream_id

    def _cancel(self, conn, batch_id, reason, event_id=None):
        cancelled=[];stream_id=None
        for segment in conn.execute('SELECT segment_index FROM reply_segments WHERE batch_id=? AND cancel_if_new_input=1 AND content_kind=\'chat\'',(batch_id,)).fetchall():
            parts=conn.execute('SELECT * FROM reply_delivery_parts WHERE reply_batch_id=? AND semantic_index=?',(batch_id,segment[0])).fetchall()
            if not parts or any(p['state']!='pending' or p['attempt'] for p in parts):continue
            stream_id=parts[0]['stream_id']
            conn.execute("UPDATE reply_delivery_parts SET state='skipped',revision=revision+1,cancel_reason=?,trigger_event_id=?,updated_at=? WHERE reply_batch_id=? AND semantic_index=?",
                         (reason,event_id,timestamp(),batch_id,segment[0]))
            cancelled.append(segment[0])
        if stream_id:
            before=self._stream(conn,stream_id)
            self._refresh(conn,stream_id)
            if before['state']=='blocked' and before['blocked_reason']=='admin_hold':
                conn.execute("UPDATE reply_delivery_streams SET state='blocked',blocked_reason='admin_hold',blocked_part_id=? WHERE id=?",(before['blocked_part_id'],stream_id))
            self._record(conn,identifier(),stream_id,None,'cancel_optional','runtime' if event_id else 'admin',reason,True,
                         _fingerprint([batch_id,event_id,cancelled]),{'batch_id':batch_id,'semantic_indices':cancelled,'trigger_event_id':event_id})
        return cancelled

    def new_input(self,conn,event_id,persona_id,session_id,channel,origin,exclude_batch=None,exclude_turn=None):
        rows=conn.execute("SELECT * FROM turns WHERE persona_id=? AND session_id=? AND channel=? AND id!=? AND (input_batch_id IS NULL OR input_batch_id!=?)",
                          (persona_id,session_id,channel,exclude_turn or '',exclude_batch or '')).fetchall()
        for row in rows:
            if channel=='qq' and json.loads(row['origin_json'] or '{}')!=origin:continue
            if row['reply_mode_snapshot']!='natural_structured':continue
            conn.execute('UPDATE turns SET new_input_event_id=? WHERE id=?',(event_id,row['id']))
            for batch in conn.execute('SELECT id FROM reply_batches WHERE turn_id=?',(row['id'],)).fetchall():
                self._cancel(conn,batch[0],'new_input',event_id)

    def cancel_optional(self,persona_id,batch_id,expected_revision,operation_id):
        request_hash=_fingerprint([persona_id,batch_id,expected_revision,'cancel_optional'])
        with self.store.connection(write=True) as conn:
            if self.store.current(conn)!=persona_id:raise RuntimeErrorBase('persona_selection_conflict')
            row=conn.execute('SELECT stream_id FROM reply_delivery_parts WHERE reply_batch_id=? AND persona_id=? LIMIT 1',(batch_id,persona_id)).fetchone()
            if not row:raise RuntimeErrorBase('reply_batch_not_found')
            stream=self._stream(conn,row[0]);old=self._operation(conn,operation_id,request_hash)
            if old is not None:return old
            if stream['revision']!=expected_revision:raise RuntimeErrorBase('version_conflict')
            result={'cancelled':self._cancel(conn,batch_id,'admin_cancel_optional')}
            result['revision']=self._stream(conn,row[0])['revision']
            self._record(conn,operation_id,row[0],None,'cancel_optional','admin','explicit_optional_only',True,request_hash,result)
            return result

    def _wait(self,conn,part, *, start=False):
        segment=conn.execute('SELECT wait_ms FROM reply_segments WHERE batch_id=? AND segment_index=?',(part['reply_batch_id'],part['semantic_index'])).fetchone()
        delay=(segment[0] or 0) if segment else 0
        if not delay or part['attempt']:return 0
        group=conn.execute('SELECT state,attempt FROM reply_delivery_parts WHERE reply_batch_id=? AND semantic_index=?',(part['reply_batch_id'],part['semantic_index'])).fetchall()
        if any(p['attempt'] for p in group):return 0
        previous=conn.execute('SELECT state FROM reply_delivery_parts WHERE reply_batch_id=? AND semantic_index=?',(part['reply_batch_id'],part['semantic_index']-1)).fetchall()
        if not previous or not all(p['state']=='sent' for p in previous):return 0
        key=(part['reply_batch_id'],part['semantic_index'])
        # A failed result transaction may have populated memory but rolled back
        # its persisted start. Such a deadline must never shorten a later wait.
        if key in self.waits and part['wait_started_at'] is None:
            self.waits.pop(key,None)
        if key not in self.waits:
            if not start:return delay
            self.waits[key]=self.monotonic()+delay/1000
            conn.execute('UPDATE reply_delivery_parts SET wait_started_at=?,wait_ms=? WHERE reply_batch_id=? AND semantic_index=?',(timestamp(),delay,*key))
        return max(0,math.ceil((self.waits[key]-self.monotonic())*1000-1e-7))

    def _view(self, conn, stream_id):
        stream = self._stream(conn, stream_id)
        stream["origin"] = json.loads(stream.pop("origin_json"))
        stream["parts"] = []
        for row in conn.execute("SELECT * FROM reply_delivery_parts WHERE stream_id=? ORDER BY stream_seq", (stream_id,)):
            part=dict(row);part.pop("claim_token",None)
            metadata=conn.execute('SELECT text,content_kind,cancel_if_new_input,wait_ms FROM reply_segments WHERE batch_id=? AND segment_index=?',(part['reply_batch_id'],part['semantic_index'])).fetchone()
            from .presentation import code_lines
            part['protected_code_lines']=[line for line in code_lines(metadata['text']) if line in part['text']] if metadata else []
            metadata={k:v for k,v in dict(metadata).items() if k!='text'} if metadata else None
            part.update(dict(metadata) if metadata else {})
            part['remaining_ms']=self._wait(conn,part) if part['state']=='pending' else 0
            part["attempts"]=[dict(item) for item in conn.execute(
                "SELECT attempt_no,state,platform_message_id,error,started_at,ended_at FROM reply_delivery_attempts WHERE part_id=? ORDER BY attempt_no",
                (part["id"],))]
            stream["parts"].append(part)
        return stream

    def by_batch(self, batch_id):
        with self.store.connection() as conn:
            row = conn.execute("SELECT stream_id FROM reply_delivery_parts WHERE reply_batch_id=? LIMIT 1", (batch_id,)).fetchone()
            return self._view(conn, row[0]) if row else None

    def get(self, stream_id, *, persona_id=None, origin=None):
        with self.store.connection() as conn:
            stream = self._stream(conn, stream_id)
            self._scope(stream, persona_id, origin)
            return self._view(conn, stream_id)

    def pending(self, *, origin=None, persona_id=None):
        with self.store.connection() as conn:
            rows = conn.execute("SELECT id FROM reply_delivery_streams WHERE state IN ('open','blocked') ORDER BY created_at").fetchall()
            result = []
            for row in rows:
                stream = self._stream(conn, row[0])
                if ((origin is None or json.loads(stream["origin_json"]) == origin)
                        and (persona_id is None or stream["persona_id"] == persona_id)):
                    result.append(self._view(conn, row[0]))
            return result

    def list(self, persona_id):
        with self.store.connection() as conn:
            return [self._view(conn, row[0]) for row in conn.execute(
                "SELECT id FROM reply_delivery_streams WHERE persona_id=? ORDER BY updated_at DESC", (persona_id,))]

    def claim(self, stream_id, part_id, expected_revision, operation_id, *, persona_id=None, origin=None, actor="bridge", capabilities=None):
        request = {"stream_id": stream_id, "part_id": part_id, "expected_revision": expected_revision,"capabilities":capabilities}
        request_hash = _fingerprint(request)
        with self.store.connection(write=True) as conn:
            stream = self._stream(conn, stream_id); self._scope(stream, persona_id, origin)
            old = self._operation(conn, operation_id, request_hash)
            if old is not None:
                return old
            if stream["revision"] != expected_revision:
                raise RuntimeErrorBase("version_conflict")
            part = conn.execute("SELECT * FROM reply_delivery_parts WHERE id=? AND stream_id=?", (part_id, stream_id)).fetchone()
            if not part:
                raise RuntimeErrorBase("Delivery part not found")
            batch=conn.execute('SELECT schema_version FROM reply_batches WHERE id=?',(part['reply_batch_id'],)).fetchone()
            if batch[0]==2 and not {'reply_batch_v2','reply_rhythm_v1'}.issubset(set(capabilities or [])):
                raise RuntimeErrorBase('reply_v2_capability_required')
            first = conn.execute("SELECT id,state FROM reply_delivery_parts WHERE stream_id=? AND state NOT IN ('sent','skipped','resolved') ORDER BY stream_seq LIMIT 1",
                                 (stream_id,)).fetchone()
            if stream["state"] != "open" or part["state"] != "pending" or not first or first["id"] != part_id:
                result = {"claimed": False, "state": part["state"], "revision": stream["revision"],
                          "reason": stream["blocked_reason"] or "another_part_has_priority"}
                self._record(conn, operation_id, stream_id, part_id, "claim", actor, None, False, request_hash, result)
                return result
            remaining=self._wait(conn,part,start=True)
            if remaining:
                result={'claimed':False,'reason':'rhythm_wait','remaining_ms':remaining,'revision':stream['revision'],'state':'pending'}
                self._record(conn,operation_id,stream_id,part_id,'claim',actor,'rhythm_wait',False,request_hash,result)
                return result
            token = identifier()
            conn.execute("UPDATE reply_delivery_parts SET state='sending',revision=revision+1,claim_token=?,attempt=attempt+1,updated_at=? WHERE id=? AND state='pending'",
                         (token, timestamp(), part_id))
            conn.execute("INSERT INTO reply_delivery_attempts (id,part_id,attempt_no,state,platform_message_id,error,started_at,ended_at) VALUES (?,?,?,'sending',NULL,NULL,?,NULL)",
                         (identifier(), part_id, part["attempt"] + 1, timestamp()))
            conn.execute("UPDATE reply_delivery_streams SET revision=revision+1,updated_at=? WHERE id=?", (timestamp(), stream_id))
            stream = self._stream(conn, stream_id)
            result = {"claimed": True, "claim_token": token, "attempt": part["attempt"] + 1,
                      "revision": stream["revision"], "part_revision": part["revision"] + 1,
                      "part_id": part_id, "expires_at": None}
            self._record(conn, operation_id, stream_id, part_id, "claim", actor, None, False, request_hash, result)
            return result

    def result(self, stream_id, part_id, claim_token, status, expected_revision, operation_id, *, platform_message_id=None,
               error=None, persona_id=None, origin=None, actor="bridge"):
        if status not in ("sent", "failed", "uncertain"):
            raise RuntimeErrorBase("Invalid delivery result")
        request = {"stream_id": stream_id, "part_id": part_id, "claim_token": claim_token, "status": status,
                   "platform_message_id": platform_message_id, "error": error, "expected_revision": expected_revision}
        request_hash = _fingerprint(request)
        with self.store.connection(write=True) as conn:
            stream = self._stream(conn, stream_id); self._scope(stream, persona_id, origin)
            old = self._operation(conn, operation_id, request_hash)
            if old is not None:
                return old
            part = conn.execute("SELECT * FROM reply_delivery_parts WHERE id=? AND stream_id=?", (part_id, stream_id)).fetchone()
            if not part or part["revision"] != expected_revision:
                raise RuntimeErrorBase("version_conflict")
            if not part or part["state"] != "sending" or part["claim_token"] != claim_token:
                raise RuntimeErrorBase("claim_token_mismatch")
            conn.execute("UPDATE reply_delivery_parts SET state=?,revision=revision+1,claim_token=NULL,platform_message_id=?,sent_at=?,error=?,updated_at=? WHERE id=?",
                         (status, platform_message_id, timestamp() if status == "sent" else None, error, timestamp(), part_id))
            conn.execute("UPDATE reply_delivery_attempts SET state=?,platform_message_id=?,error=?,ended_at=? WHERE part_id=? AND attempt_no=? AND state='sending'",
                         (status, platform_message_id, error, timestamp(), part_id, part["attempt"]))
            stream = self._refresh(conn, stream_id)
            if status=='sent':
                next_part=conn.execute("SELECT * FROM reply_delivery_parts WHERE reply_batch_id=? AND semantic_index=? AND state='pending' ORDER BY part_index LIMIT 1",(part['reply_batch_id'],part['semantic_index']+1)).fetchone()
                if next_part:self._wait(conn,next_part,start=True)
            result = {"state": status, "revision": stream["revision"], "part_id": part_id}
            self._record(conn, operation_id, stream_id, part_id, "result", actor, error, False, request_hash, result)
            return result

    def resolve(self, stream_id, part_id, action, expected_revision, operation_id, *, persona_id=None, reason=None,
                actor="admin", allow_continue=True):
        if action not in ("resolve", "skip", "retry", "release"):
            raise RuntimeErrorBase("Invalid delivery resolution")
        request = {"stream_id": stream_id, "part_id": part_id, "action": action, "expected_revision": expected_revision,
                   "reason": reason, "allow_continue": bool(allow_continue)}
        request_hash = _fingerprint(request)
        with self.store.connection(write=True) as conn:
            stream = self._stream(conn, stream_id); self._scope(stream, persona_id)
            old = self._operation(conn, operation_id, request_hash)
            if old is not None:
                return old
            if stream["revision"] != expected_revision:
                raise RuntimeErrorBase("version_conflict")
            part = conn.execute("SELECT * FROM reply_delivery_parts WHERE id=? AND stream_id=?", (part_id, stream_id)).fetchone()
            if not part:
                raise RuntimeErrorBase("Delivery part not found")
            if action == "retry":
                if part["state"] not in ("uncertain", "failed"):
                    raise RuntimeErrorBase("part_not_retryable")
                conn.execute("UPDATE reply_delivery_parts SET state='pending',revision=revision+1,claim_token=NULL,error=?,updated_at=? WHERE id=?",
                             (reason or "explicit_retry_may_duplicate", timestamp(), part_id))
                stream = self._refresh(conn, stream_id)
            elif action == "skip":
                if part["state"] not in ("failed", "uncertain"):
                    raise RuntimeErrorBase("part_not_skippable")
                conn.execute("UPDATE reply_delivery_parts SET state='skipped',revision=revision+1,error=?,updated_at=? WHERE reply_batch_id=? AND part_index>=? AND state IN ('pending','failed','uncertain')",
                             (reason or "explicitly_skipped", timestamp(), part["reply_batch_id"], part["part_index"]))
                stream = self._refresh(conn, stream_id)
            elif action == "resolve":
                if part["state"] not in ("uncertain", "failed"):
                    raise RuntimeErrorBase("part_not_resolvable")
                conn.execute("UPDATE reply_delivery_parts SET state='resolved',revision=revision+1,claim_token=NULL,error=?,updated_at=? WHERE id=?",
                             (reason or "explicitly_resolved", timestamp(), part_id))
                stream = self._refresh(conn, stream_id)
            else:
                if stream["state"] != "blocked" or stream["blocked_reason"] != "admin_hold":
                    raise RuntimeErrorBase("blocking_part_must_be_resolved_first")
                stream = self._refresh(conn, stream_id)
            if action != "release" and not allow_continue:
                conn.execute("UPDATE reply_delivery_streams SET state='blocked',blocked_reason='admin_hold',blocked_part_id=?,revision=revision+1,updated_at=? WHERE id=?",
                             (part_id, timestamp(), stream_id))
                stream = self._stream(conn, stream_id)
            resolved_part=conn.execute("SELECT state,attempt FROM reply_delivery_parts WHERE id=?",(part_id,)).fetchone()
            result = {"state": stream["state"], "part_id":part_id,"part_state":resolved_part["state"],
                      "attempt":resolved_part["attempt"],"revision": stream["revision"],
                      "allow_continue": bool(allow_continue)}
            self._record(conn, operation_id, stream_id, part_id, action, actor, reason, allow_continue, request_hash, result)
            return result
