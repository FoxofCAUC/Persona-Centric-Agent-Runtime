"""Durable chat orchestration; no window or QQ SDK dependencies."""
import asyncio
from collections import defaultdict
import json
import re
import sqlite3
import time
import math
from typing import Protocol

import aiohttp

from .models import LOCAL_USER_ID, RuntimeErrorBase, identifier, require_text, timestamp
from .security import unprotect
from .service import RuntimeService
from .progress import record as record_progress, progress


class ModelError(RuntimeErrorBase):
    def __init__(self,code,*,usage=None,diagnostics=None):
        super().__init__(code)
        self.usage=usage;self.diagnostics=diagnostics


class ModelCompletion(tuple):
    """Two-value unpacking stays compatible with existing clients and tasks."""
    def __new__(cls,text,usage,diagnostics):
        result=super().__new__(cls,(text,usage));result.diagnostics=diagnostics
        return result


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
        from .reply_protocol import response_format
        fmt=response_format(model.get('_reply_mode'),model.get('_expression_snapshot'))
        if model.get('_disposition_compile'):
            # Reuse the selector's established text transport. Compilation JSON
            # is validated atomically on return; no extra gateway capability
            # (or repair call) is assumed for the selected provider.
            fmt=None
        if fmt:payload['response_format']=fmt
        # One configured end-to-end budget. A hidden shorter connection cap
        # used to fail 60-second requests after five seconds of DNS/TLS work.
        budget = model['timeout']
        timeout = aiohttp.ClientTimeout(total=budget, connect=budget, ceil_threshold=math.inf)
        started = time.monotonic()
        transport = {'phase': 'request_setup', 'request_headers_sent': False,
                     'response_headers_received': False}
        trace = aiohttp.TraceConfig()
        async def connecting(*_): transport['phase'] = 'connecting'
        async def connected(*_): transport['phase'] = 'awaiting_headers'
        async def sent(*_):
            transport['request_headers_sent'] = True
            transport['phase'] = 'awaiting_headers'
        trace.on_connection_create_start.append(connecting)
        trace.on_connection_create_end.append(connected)
        trace.on_request_headers_sent.append(sent)
        def diagnostic():
            return {**transport, 'timeout_seconds': budget,
                    'elapsed_ms': round((time.monotonic()-started)*1000, 3)}
        connector=None
        if model.get('_disposition_compile'):
            # Explicit compiler-only trust bundle. Never disable certificate or
            # hostname verification, and never retry a failed external request.
            import ssl
            import certifi
            connector=aiohttp.TCPConnector(ssl=ssl.create_default_context(cafile=certifi.where()))
        if model.get('_request_deadline') is not None:
            budget=min(budget,max(.01,model['_request_deadline']-time.monotonic()-.1))
            timeout=aiohttp.ClientTimeout(total=budget,connect=budget,ceil_threshold=math.inf)
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False, trace_configs=[trace],connector=connector) as client:
                async with client.post(model["base_url"].rstrip("/") + "/chat/completions",
                                       headers=headers, json=payload, allow_redirects=False) as response:
                    transport.update(phase='reading_body', response_headers_received=True)
                    if response.status != 200:
                        raise ModelError(f"provider_http_{response.status}", diagnostics=diagnostic())
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        raw.extend(chunk)
                        if len(raw) > 2 * 1024 * 1024:
                            raise ModelError("provider_response_too_large")
                    transport['phase'] = 'parsing_response'
                    data = json.loads(raw)
                    choice=data['choices'][0]
                    text = choice["message"]["content"]
                    finish=choice.get('finish_reason')
                    diagnostics={**diagnostic(),'finish_reason':finish if finish in ('stop','length','content_filter','tool_calls','function_call') else 'unknown',
                                 'response_format':'json_schema' if fmt else 'text',
                                 'output_characters':len(text) if isinstance(text,str) else 0}
                    usage = data.get("usage")
                    if not isinstance(usage, dict):
                        usage = None
                    else:
                        usage = {k: v for k, v in usage.items() if k in (
                            "prompt_tokens", "completion_tokens", "total_tokens") and type(v) is int and v >= 0}
                    if not isinstance(text, str) or not text.strip():
                        raise ModelError('provider_content_filtered' if finish=='content_filter' else 'provider_empty_response',usage=usage,diagnostics=diagnostics)
                    return ModelCompletion(text,usage,diagnostics)
        except ModelError:
            raise
        except (TimeoutError, asyncio.TimeoutError):
            code = 'provider_connection_timeout' if transport['phase']=='connecting' else 'provider_timeout'
            raise ModelError(code, diagnostics=diagnostic()) from None
        except aiohttp.ClientError:
            raise ModelError("provider_network_error", diagnostics=diagnostic()) from None
        except (ValueError, KeyError, IndexError, TypeError):
            raise ModelError("provider_invalid_response", diagnostics=diagnostic()) from None


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
        from .cognitive import CognitiveService
        self.cognitive=CognitiveService(store,self)
        self.client = client or ChatCompletionsClient()
        from .character_selector import SelectorService
        self.selector=SelectorService(store,config,self.client,self.cognitive.characters)
        self.service = RuntimeService(store)
        self.tasks = {}
        self.locks = defaultdict(asyncio.Lock)
        self.draining = False

    def recover(self):
        with self.store.connection(write=True) as conn:
            self.selector.recover(conn)
            for row in conn.execute("SELECT id FROM turns WHERE status IN ('queued','running')").fetchall():
                record_progress(conn, row[0], "interrupted")
            for row in conn.execute("SELECT turn_id FROM deliveries WHERE state='sending'").fetchall():
                record_progress(conn, row[0], "delivery_uncertain")
            conn.execute("UPDATE turns SET status='interrupted', error_code='service_restarted', updated_at=? "
                         "WHERE status IN ('queued','running')", (timestamp(),))
            conn.execute("UPDATE attempts SET status='interrupted', error_code='service_restarted' WHERE status='running'")
            conn.execute("UPDATE deliveries SET state='uncertain', updated_at=? WHERE state='sending'", (timestamp(),))
        from .delivery import DeliveryService
        DeliveryService(self.store).recover()

    @staticmethod
    def _session(conn, persona_id, channel, session_id=None, fresh=False, origin=None):
        from .sessions import select
        if fresh:
            raise RuntimeErrorBase('use_session_service_for_new_session')
        return select(conn,persona_id,channel,session_id,origin)

    def session(self, channel="desktop", session_id=None, fresh=False):
        from .sessions import SessionService
        if channel not in ('desktop','qq'):raise RuntimeErrorBase('Unsupported channel')
        cfg=self.config.load();origin={'bot':cfg['bot_qq'],'sender':cfg['owner_qq']} if channel=='qq' else None
        with self.store.connection() as conn:pid=self.service._persona(conn)['id']
        service=SessionService(self.store)
        if fresh:
            current=service.current(pid,channel,(origin or {}).get('bot',''),(origin or {}).get('sender',''))
            if current['session_id']:
                result=service.seal(current['session_id'],pid,current['revision'],identifier(),channel,(origin or {}).get('bot',''),(origin or {}).get('sender',''),reason='new_session')
                if result['state']!='sealed':raise RuntimeErrorBase('session_has_pending_work')
            session_id=None
        with self.store.connection(write=True) as conn:
            return {'session_id':self._session(conn,pid,channel,session_id,origin=origin),'persona_id':pid,'channel':channel}

    def desktop_sessions(self):
        with self.store.connection() as conn:
            persona_id = self.service._persona(conn)["id"]
            return [dict(row) for row in conn.execute(
                "SELECT s.* FROM sessions s WHERE s.persona_id=? AND s.user_id=? "
                "AND NOT EXISTS (SELECT 1 FROM turns t WHERE t.session_id=s.id AND t.channel='qq') "
                "AND NOT EXISTS (SELECT 1 FROM channel_sessions c WHERE c.session_id=s.id AND c.channel='qq') "
                "ORDER BY s.created_at", (persona_id, LOCAL_USER_ID))]

    async def submit(self, channel, text, request_id, session_id=None, *, test=False, origin=None, persona_id=None, test_reply_mode=None):
        if self.draining:
            raise RuntimeErrorBase("service_draining")
        require_text(text, "text")
        require_text(request_id, "request_id")
        if channel not in ("desktop", "qq", "test") or (channel == "test") != test:
            raise RuntimeErrorBase("Unsupported channel")
        if test_reply_mode is not None and (not test or test_reply_mode not in ('natural_structured','context_structured')):
            raise RuntimeErrorBase('invalid_protocol_probe')
        if len(text) > 24000 or len(request_id) > 200:
            raise RuntimeErrorBase("Input is too long")
        if channel=='qq':
            from .qq_testing import QQTesting
            QQTesting(self.store.paths).check_runtime((origin or {}).get('sender',self.config.load()['owner_qq']))
        source_key = channel + ":" + request_id
        with self.store.connection(write=True) as conn:
            if conn.execute("SELECT 1 FROM ingress_receipts WHERE channel=? AND request_id=?",(channel,request_id)).fetchone():
                raise RuntimeErrorBase("v2_submission_exists")
            old = conn.execute("SELECT id FROM turns WHERE source_key=?", (source_key,)).fetchone()
            if old:
                previous = self._view(conn, old[0])
                if test and previous.get('reply_mode_snapshot')!=test_reply_mode:raise RuntimeErrorBase('submission_conflict')
                if not test and (previous["text"] != text or (persona_id and previous["persona_id"] != persona_id)
                                 or (session_id and previous["session_id"] != session_id)
                                 or (origin is not None and previous["origin"] != origin)):
                    raise RuntimeErrorBase("submission_conflict")
                return previous
            persona_id = None if test else self.service._persona(conn, persona_id)["id"]
            from .private_personas import guard_acceptance_write
            guard_acceptance_write(self.store,persona_id)
            waiting = conn.execute("SELECT count(*) FROM turns WHERE persona_id IS ? AND status='queued'",
                                   (persona_id,)).fetchone()[0]
            if waiting >= 10:
                raise RuntimeErrorBase("queue_full")
            if not test:
                session_id = self._session(conn, persona_id, channel, session_id, origin=origin or ({'bot':self.config.load()['bot_qq'],'sender':self.config.load()['owner_qq']} if channel=='qq' else None))
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
            if test_reply_mode:
                conn.execute('UPDATE turns SET reply_mode_snapshot=? WHERE id=?',(test_reply_mode,turn_id))
            if not test:
                from .profile import snapshot
                conn.execute('UPDATE turns SET profile_snapshot=? WHERE id=?',(json.dumps(snapshot(conn,persona_id),ensure_ascii=False),turn_id))
                from .character import head as character_head
                conn.execute('UPDATE turns SET character_snapshot=? WHERE id=?',(json.dumps(character_head(conn,persona_id),ensure_ascii=False),turn_id))
                from .context_policy import current as context_policy
                conn.execute('UPDATE turns SET context_policy_snapshot=?,selector_model_snapshot=?,execution_revision=1 WHERE id=?',(json.dumps(context_policy(conn,persona_id,channel)),json.dumps(self.config.load()['character_selector']),turn_id))
            if channel == "qq":
                identity = origin or {"sender": self.config.load()["owner_qq"], "bot": self.config.load()["bot_qq"]}
                conn.execute("UPDATE turns SET origin_json=? WHERE id=?", (json.dumps(identity), turn_id))
                conn.execute("INSERT INTO deliveries VALUES (?, 'pending', ?)", (turn_id, timestamp()))
            if not test:
                from .delivery import DeliveryService
                DeliveryService(self.store).new_input(conn,event_id,persona_id,session_id,channel,identity if channel=='qq' else {},exclude_turn=turn_id)
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
        value.pop("selector_model_snapshot",None)
        value.pop("model_snapshot")
        value.pop('profile_snapshot',None)
        value.pop('character_snapshot',None)
        value["origin"] = json.loads(value.pop("origin_json"))
        value["usage"] = json.loads(value.pop("usage_json")) if row["usage_json"] else None
        for field, output in (("user_event_id", "text"), ("reply_event_id", "reply")):
            event = conn.execute("SELECT text FROM events WHERE id=?", (value[field],)).fetchone()
            value[output] = event[0] if event else None
        from .turn_input import input_text, load_turn_input
        if value.get("input_batch_id"):
            value["text"]=input_text(conn,value)
            value["input_members"]=load_turn_input(conn,value)
            value["raw_message_count"]=len(value["input_members"])
        delivery = conn.execute("SELECT state FROM deliveries WHERE turn_id=?", (turn_id,)).fetchone()
        value["delivery"] = delivery[0] if delivery else None
        value['selection_attempts']=[{k:v for k,v in dict(r).items() if k!='model_snapshot'} for r in conn.execute('SELECT * FROM character_selection_attempts WHERE turn_id=? ORDER BY rowid',(turn_id,))]
        value["attempt_count"] = conn.execute("SELECT count(*) FROM attempts WHERE turn_id=?", (turn_id,)).fetchone()[0]
        reply_batch=conn.execute('SELECT * FROM reply_batches WHERE turn_id=? ORDER BY created_at DESC LIMIT 1',(turn_id,)).fetchone()
        if reply_batch:
            batch=dict(reply_batch)
            batch['segments']=[dict(r) for r in conn.execute('SELECT segment_index,text,format,content_kind,cancel_if_new_input,wait_ms FROM reply_segments WHERE batch_id=? ORDER BY segment_index',(batch['id'],))]
            for key in ('persona_id','user_id','attempt_id','raw_fingerprint'):batch.pop(key,None)
            value['reply_batch']=batch
        else:value['reply_batch']=None
        from .presentation import batch_view
        value['display_reply'],value['presentation']=batch_view(value['reply'],
            value['reply_batch']['segments'] if value['reply_batch'] else None,historical=True)
        from .chat_display import read as read_display
        value['display_messages']=read_display(conn,reply_batch['id']) if reply_batch else []
        if value['display_messages']:
            value['display_reply']='\n\n'.join(m['text'] for m in value['display_messages'])
            value['presentation']['display_version']=value['display_messages'][0]['transform_version']
            value['presentation']['planned_wait_ms']=sum(m['wait_ms'] for m in value['display_messages'])
        delivery_stream=conn.execute('SELECT s.id,s.state,s.revision,s.blocked_reason,s.blocked_part_id,count(p.id) AS part_count,sum(CASE WHEN p.state="sent" THEN 1 ELSE 0 END) AS sent_count FROM reply_delivery_streams s JOIN reply_delivery_parts p ON p.stream_id=s.id WHERE p.turn_id=? GROUP BY s.id',(turn_id,)).fetchone()
        value['delivery_stream']=dict(delivery_stream) if delivery_stream else None
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
        # Compatibility helper also uses canonical scope and withdrawal rules.
        evidence=None if turn['channel']=='test' else self.cognitive.dialogue.basic_evidence(conn,turn)
        messages, details = self.cognitive.dialogue.render(conn,turn,evidence)
        turn["_context_details"] = details
        return messages

    async def _run(self, turn_id):
        initial = self.get(turn_id)
        if initial['status']!='queued' or initial.get('cancelled_at'):return
        async with self.locks[initial["persona_id"]]:
            attempt_id = None
            usage = None
            reference_manifest = None
            try:
                latest=self.get(turn_id)
                if latest['status']!='queued' or latest['execution_revision']!=initial['execution_revision']:return
                selection=await self.selector.prepare(turn_id)
                prepared=await asyncio.to_thread(self.cognitive.prepare,turn_id,selection)
                with self.store.connection(write=True) as conn:
                    turn = dict(conn.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone())
                    if turn["status"] != "queued" or turn["cancelled_at"] or turn["execution_revision"]!=initial["execution_revision"]:
                        return
                    model = json.loads(turn["model_snapshot"])
                    from .private_personas import guard_cloud_dispatch
                    guard_cloud_dispatch(self.store,turn['persona_id'],'main',model)
                    prepared=self.cognitive.finalize(conn,turn,prepared)
                    messages=prepared['messages'];turn['_context_details']=prepared['details']
                    reference_manifest = turn["_context_details"].get("references")
                    if not model["base_url"] or not model["name"]:
                        raise ModelError("model_not_configured")
                    from .memory_policy import project_capacity_used
                    count = project_capacity_used(conn, time.time()-3600, turn_id)
                    limit = self.config.load()["model"]["hourly_limit"]
                    if limit and count >= limit:
                        raise ModelError("hourly_limit_reached")
                    conn.execute("UPDATE quota_reservations SET state='consumed' WHERE turn_id=? AND execution_revision=? AND state='active'",(turn_id,turn['execution_revision']))
                    attempt_id = identifier()
                    conn.execute("INSERT INTO attempts VALUES (?,?,?,'running',NULL,NULL,?)",
                                 (attempt_id, turn_id, time.time(), turn["model_snapshot"]))
                    context_id=self.cognitive.persist(conn,prepared['manifest'],turn=turn,attempt_id=attempt_id)
                    turn['_context_details']['context_id']=context_id
                    conn.execute("INSERT INTO context_uses VALUES (?,?,?,?,?,?)", (attempt_id, turn_id, turn["persona_id"], LOCAL_USER_ID,
                                 json.dumps(turn["_context_details"], ensure_ascii=False), timestamp()))
                    conn.execute("UPDATE turns SET status='running', updated_at=? WHERE id=?", (timestamp(), turn_id))
                    from .references import record_check
                    record_check(conn,turn,turn["_context_details"].get("references",{}),attempt_id=attempt_id)
                    record_progress(conn, turn_id, "provider_started", attempt_id)
                from .reply_batch import ReplySchemaError, validate_reply
                completion = await self.client.complete(dict(model,_reply_mode=turn.get('reply_mode_snapshot'),_expression_snapshot=turn.get('expression_snapshot')), messages)
                raw_reply, usage = completion
                provider_diagnostics=getattr(completion,'diagnostics',{'finish_reason':'unreported'})
                validation_error=None
                try:
                    if turn.get('reply_mode_snapshot') in ('natural_structured','context_structured') and provider_diagnostics.get('finish_reason')=='length':
                        raise ReplySchemaError('reply_output_truncated')
                    if provider_diagnostics.get('finish_reason')=='content_filter':
                        raise ReplySchemaError('provider_content_filtered')
                    reply_batch=validate_reply(raw_reply,turn.get('reply_mode_snapshot') or 'legacy_full',turn.get('expression_snapshot'))
                    output_validation={'status':'accepted','fallback_reason':reply_batch.fallback_reason,
                                       'schema_version':reply_batch.schema_version,'message_count':len(reply_batch.segments)}
                except ReplySchemaError as error:
                    from .reply_protocol import validation_failure
                    output_validation=validation_failure(raw_reply,turn.get('reply_mode_snapshot') or 'legacy_full',turn.get('expression_snapshot'))
                    if str(error)=='reply_output_truncated':output_validation['reason']='provider_length_limit'
                    if str(error)=='provider_content_filtered':output_validation['reason']='provider_content_filtered'
                    validation_error=error
                # Keep only the output fingerprint for failed validation as well as
                # successful batches; never persist a malformed internal envelope.
                import hashlib
                with self.store.connection(write=True) as conn:
                    details=dict(turn['_context_details'])
                    details['reply_output_fingerprint']=hashlib.sha256(raw_reply.encode('utf-8')).hexdigest()
                    details['reply_provider']=provider_diagnostics
                    details['reply_validation']=output_validation
                    conn.execute('UPDATE context_uses SET details_json=? WHERE attempt_id=?',
                                 (json.dumps(details,ensure_ascii=False),attempt_id))
                if validation_error:raise validation_error
                reply=reply_batch.text
                from dataclasses import asdict
                from .presentation import batch_view
                _, presentation=batch_view(reply,[asdict(s) for s in reply_batch.segments])
                from .chat_display import enabled, plan as display_plan, persist as persist_display
                display_messages=display_plan([asdict(s) for s in reply_batch.segments],turn.get('expression_snapshot')) if enabled(turn.get('expression_snapshot'),reply_batch.mode) else []
                if display_messages:
                    presentation['display_version']=display_messages[0]['transform_version']
                    presentation['display_count']=len(display_messages)
                    presentation['planned_wait_ms']=sum(m['wait_ms'] for m in display_messages)
                with self.store.connection(write=True) as conn:
                    details['presentation']=presentation
                    conn.execute('UPDATE context_uses SET details_json=? WHERE attempt_id=?',
                                 (json.dumps(details,ensure_ascii=False),attempt_id))
                    record_progress(conn, turn_id, "generated", attempt_id)
                    event_id = identifier() if turn["channel"] != "test" else None
                    if event_id:
                        conn.execute("INSERT INTO events (id,session_id,persona_id,user_id,role,text,created_at) "
                                     "VALUES (?,?,?,?, 'persona',?,?)",
                                     (event_id, turn["session_id"], turn["persona_id"], LOCAL_USER_ID, reply, timestamp()))
                        batch_id=identifier()
                        conn.execute('INSERT INTO reply_batches (id,turn_id,attempt_id,persona_id,user_id,schema_version,mode,raw_fingerprint,segment_count,total_chars,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                                     (batch_id,turn_id,attempt_id,turn['persona_id'],LOCAL_USER_ID,reply_batch.schema_version,reply_batch.mode,reply_batch.raw_fingerprint,len(reply_batch.segments),sum(len(item.text) for item in reply_batch.segments),timestamp()))
                        conn.execute('UPDATE reply_batches SET behavior_intent=?,fallback_reason=? WHERE id=?',(reply_batch.behavior_intent,reply_batch.fallback_reason,batch_id))
                        conn.executemany('INSERT INTO reply_segments (batch_id,persona_id,user_id,segment_index,text,format,content_kind,cancel_if_new_input,wait_ms) VALUES (?,?,?,?,?,?,?,?,?)',
                                         [(batch_id,turn['persona_id'],LOCAL_USER_ID,index,item.text,item.format,item.content_kind,int(item.cancel_if_new_input),item.wait_ms) for index,item in enumerate(reply_batch.segments,1)])
                        if display_messages:persist_display(conn,batch_id,turn,display_messages)
                        from .delivery import DeliveryService
                        DeliveryService(self.store).create_for_batch(conn,batch_id,turn)
                    encoded = json.dumps(usage) if usage is not None else None
                    conn.execute("UPDATE turns SET status='completed',reply_event_id=?,error_code=NULL,usage_json=?,updated_at=? WHERE id=?",
                                 (event_id, encoded, timestamp(), turn_id))
                    conn.execute("UPDATE attempts SET status='completed',usage_json=? WHERE id=?", (encoded, attempt_id))
                    record_progress(conn, turn_id, "persisted", attempt_id)
            except BaseException as error:
                interrupted = isinstance(error, asyncio.CancelledError)
                if getattr(error,'usage',None) is not None:usage=error.usage
                from .reply_batch import ReplySchemaError
                from .references import ReferenceError,record_check
                code = str(error) if isinstance(error, (RuntimeErrorBase,ModelError,ReplySchemaError,ReferenceError)) else ("request_interrupted" if interrupted else "request_failed")
                with self.store.connection(write=True) as conn:
                    if attempt_id and not conn.execute("SELECT 1 FROM attempts WHERE id=?", (attempt_id,)).fetchone():
                        attempt_id = None
                    if isinstance(error,ReferenceError):record_check(conn,initial,error.manifest,error=str(error))
                    elif attempt_id is None and reference_manifest:
                        # Valid references were checked before model/limit
                        # preflight failed. Preserve that check after rollback
                        # without creating an attempt or marking sources invalid.
                        manifest={**reference_manifest,'check_status':'passed','execution_blocked':code}
                        record_check(conn,initial,manifest,error=code)
                    if attempt_id is None:
                        try:self.cognitive.blocked(conn,initial,code)
                        except (sqlite3.Error,OSError,ModelError):pass
                    record_progress(conn, turn_id, "interrupted" if interrupted else "failed", attempt_id)
                    encoded=json.dumps(usage) if usage is not None else None
                    conn.execute("UPDATE turns SET status=?,error_code=?,usage_json=?,updated_at=? WHERE id=?",
                                 ("interrupted" if interrupted else "failed", code, encoded, timestamp(), turn_id))
                    if attempt_id:
                        if getattr(error,'diagnostics',None):
                            details=dict(turn.get('_context_details',{}));details['reply_provider']=error.diagnostics
                            details['reply_validation']={'status':'rejected','reason':code}
                            conn.execute('UPDATE context_uses SET details_json=? WHERE attempt_id=?',(json.dumps(details,ensure_ascii=False),attempt_id))
                        conn.execute("UPDATE attempts SET status='failed',error_code=?,usage_json=? WHERE id=?", (code,encoded,attempt_id))
                if interrupted:
                    raise

            finally:
                self.selector.release(turn_id)

    async def retry(self, turn_id):
        if self.draining:
            raise RuntimeErrorBase("service_draining")
        with self.store.connection(write=True) as conn:
            turn = self._view(conn, turn_id)
            from .persona_management import active
            if turn['persona_id']:active(conn,turn['persona_id'])
            if turn["status"] not in ("failed", "interrupted"):
                return turn
            from .sessions import assert_writable
            assert_writable(conn,turn["session_id"])
            waiting = conn.execute("SELECT count(*) FROM turns WHERE persona_id IS ? AND status='queued'",
                                   (turn["persona_id"],)).fetchone()[0]
            if waiting >= 10:
                raise RuntimeErrorBase("queue_full")
            conn.execute("UPDATE turns SET status='queued',cancelled_at=NULL,error_code=NULL,model_snapshot=?,updated_at=? WHERE id=?",
                         (json.dumps(self.config.load()["model"]), timestamp(), turn_id))
            if turn['persona_id']:
                from .profile import snapshot
                conn.execute('UPDATE turns SET profile_snapshot=? WHERE id=?',(json.dumps(snapshot(conn,turn['persona_id']),ensure_ascii=False),turn_id))
                from .character import head as character_head
                conn.execute('UPDATE turns SET character_snapshot=? WHERE id=?',(json.dumps(character_head(conn,turn['persona_id']),ensure_ascii=False),turn_id))
                reply_policy=conn.execute('SELECT p.reply_mode,p.data_json,p.id FROM interaction_policies p JOIN interaction_heads h ON h.version_id=p.id WHERE h.persona_id=? AND h.channel=?',(turn['persona_id'],turn['channel'])).fetchone()
                reply_mode=reply_policy[0] if reply_policy else 'legacy_full'
                from .reply_batch import expression_snapshot
                conn.execute('UPDATE turns SET reply_mode_snapshot=?,expression_snapshot=? WHERE id=?',(reply_mode,json.dumps(json.loads(reply_policy[1]).get('expression') or expression_snapshot(reply_mode,reply_policy[2],version=1) if reply_policy else expression_snapshot(reply_mode,version=1)),turn_id))
                from .context_policy import current as context_policy
                conn.execute('UPDATE turns SET context_policy_snapshot=?,selector_model_snapshot=?,execution_revision=execution_revision+1 WHERE id=?',(json.dumps(context_policy(conn,turn['persona_id'],turn['channel'])),json.dumps(self.config.load()['character_selector']),turn_id))
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
                    "AND NOT EXISTS(SELECT 1 FROM legacy_delivery_dispositions x WHERE x.turn_id=t.id) AND (t.status IN ('queued','running') OR (t.status='completed' AND d.state IN ('pending','sending'))) LIMIT 1",
                    (origin, turn_id)).fetchone()
                abandoned = conn.execute("SELECT 1 FROM legacy_delivery_dispositions WHERE turn_id=?",(turn_id,)).fetchone()
                allowed = state == "pending" and not previous and not abandoned
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
