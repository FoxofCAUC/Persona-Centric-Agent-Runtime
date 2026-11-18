"""独立表达用途：完整主稿、一次表达请求、单一发布权和已呈现投影。"""
import asyncio
from dataclasses import asdict
import hashlib
import json
import time

from .archive import fingerprint, read_exact, turn_scope
from .expression_assets import ExpressionAssets, ExpressionAssetError, revision_checked
from .expression_candidates import ExpressionCandidates
from .expression_contract import text_hash
from .expression_realizer import JSON_PROTOCOL, build_messages, decode_output_for
from .expression_protocol import ExpressionPayloadError, bubble_plan
from .expression_sources import capture, validate, protected
from .expression_transport import ExpressionHTTPClient
from .expression_persona_draft import PROFILE as PERSONA_PROFILE,build_messages as persona_messages,decode_output as persona_output
from .expression_persona_draft import STRING_SEGMENTS,OBJECT_SEGMENTS
from .models import identifier, timestamp, RuntimeErrorBase
from .turn_input import input_text


DEFAULTS = {'version': 'expression-runtime-v1', 'enabled': False,
            'protocol': JSON_PROTOCOL, 'max_input_characters': 60000, 'max_tokens': 8192,
            'timeout': 120, 'unknown_fallback': True,'segment_transport':OBJECT_SEGMENTS}


def accepted(db, turn_id):
    row = db.execute('SELECT policy_json FROM expression_runtime_turns WHERE turn_id=?', (turn_id,)).fetchone()
    return json.loads(row[0]) if row else {'revision': 0, 'data': {'enabled': False}}


def enabled(db, turn_id):return bool(accepted(db, turn_id)['data'].get('enabled'))


class ExpressionRuntime:
    def __init__(self, store, client=None):
        self.store, self.client = store, client or ExpressionHTTPClient()
        self.assets = ExpressionAssets(store)
        from .expression_utterance import CharacterOutlet
        self.outlet=CharacterOutlet(self)

    def policy(self, persona_id):
        from .expression_assets import persona
        with self.store.connection() as db:
            persona(db, persona_id)
            row = db.execute('SELECT * FROM expression_runtime_policies WHERE persona_id=?', (persona_id,)).fetchone()
            return {'revision': row['revision'], 'data': json.loads(row['data_json'])} if row else {'revision': 0, 'data': dict(DEFAULTS)}

    def configure(self, persona_id, changes, expected_revision, operation_id):
        revision_checked(expected_revision)
        if not isinstance(changes, dict) or set(changes) - (set(DEFAULTS) - {'version'}):raise ExpressionAssetError('expression_runtime_policy_invalid')
        def apply(db):
            row = db.execute('SELECT * FROM expression_runtime_policies WHERE persona_id=?', (persona_id,)).fetchone()
            revision = row['revision'] if row else 0
            if revision != expected_revision:raise ExpressionAssetError('expression_runtime_policy_conflict')
            value = (json.loads(row['data_json']) if row else dict(DEFAULTS)) | changes
            if type(value['enabled']) is not bool or type(value['unknown_fallback']) is not bool or value['protocol'] not in (JSON_PROTOCOL,PERSONA_PROFILE):
                raise ExpressionAssetError('expression_runtime_policy_invalid')
            if value.get('segment_transport',STRING_SEGMENTS) not in (STRING_SEGMENTS,OBJECT_SEGMENTS):raise ExpressionAssetError('expression_runtime_policy_invalid')
            for name, limit in (('max_input_characters', 100000), ('max_tokens', 32768), ('timeout', 600)):
                if type(value[name]) is not int or not 1 <= value[name] <= limit:raise ExpressionAssetError('expression_runtime_capacity_invalid')
            db.execute('INSERT INTO expression_runtime_policies VALUES(?,?,?) ON CONFLICT(persona_id) DO UPDATE SET revision=excluded.revision,data_json=excluded.data_json',
                       (persona_id, revision + 1, json.dumps(value)))
            return {'revision': revision + 1, 'data': value}
        with self.store.connection(write=True) as db:
            return self.assets._operation(db, persona_id, operation_id, 'runtime_policy', [changes, expected_revision], lambda: apply(db))

    def capture_context(self, db, turn, details):
        return {'guard': capture(db, turn, details), 'details': details}

    def save_main(self, turn, logical_attempt_id, value, audit, physical, context, validation_error=None):
        from .reply_batch import validate_reply
        batch = validate_reply(value[0], turn.get('reply_mode_snapshot') or 'legacy_full', turn.get('expression_snapshot')) if validation_error is None else None
        raw = value[0];body = batch.text if batch else None
        with self.store.connection(write=True) as db:
            previous = db.execute('SELECT * FROM expression_main_receipts WHERE logical_attempt_id=?', (logical_attempt_id,)).fetchone()
            if previous:
                if previous['raw_hash'] != text_hash(raw):raise ExpressionAssetError('expression_main_receipt_conflict')
                return previous['id']
            ident = identifier()
            db.execute('INSERT INTO expression_main_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (ident, turn['id'], turn['execution_revision'], logical_attempt_id, physical['id'], int(batch is not None),
                        raw, text_hash(raw), body, text_hash(body) if body is not None else None,
                        json.dumps(asdict(batch), ensure_ascii=False) if batch else None,
                        json.dumps(context, ensure_ascii=False), json.dumps(audit) if audit is not None else None, timestamp()))
            changed = db.execute('UPDATE attempts SET status=\'completed\',usage_json=? WHERE id=? AND turn_id=?',
                                 (json.dumps(audit), logical_attempt_id, turn['id'])).rowcount
            if changed != 1:raise ExpressionAssetError('expression_main_attempt_missing')
            from .scene_policy import frozen as scene_frozen
            if scene_frozen(turn):
                db.execute('UPDATE expression_runtime_turns SET closed_at=coalesce(closed_at,?) WHERE turn_id=?',
                           (timestamp(),turn['id']))
            elif batch:
                draft=dict(db.execute('SELECT * FROM expression_main_receipts WHERE id=?',(ident,)).fetchone())
                self.outlet.register_main(db,turn,draft)
            return ident

    @staticmethod
    def _draft(db, turn_id):
        row = db.execute('SELECT * FROM expression_main_receipts WHERE turn_id=? ORDER BY execution_revision DESC LIMIT 1', (turn_id,)).fetchone()
        if not row or not row['valid']:raise ExpressionAssetError('expression_complete_main_unavailable')
        draft = dict(row)
        if text_hash(draft['body']) != draft['body_hash'] or text_hash(draft['raw_text']) != draft['raw_hash']:
            raise ExpressionAssetError('expression_main_receipt_changed')
        return draft

    def _validate_main(self, db, turn, draft):
        mode = db.execute('SELECT cancelled_at,closed_at FROM expression_runtime_turns WHERE turn_id=?', (turn['id'],)).fetchone()
        if mode and (mode[0] or mode[1]):raise ExpressionAssetError('expression_request_cancelled_or_closed')
        if turn.get('error_code') == 'migration_requires_review':raise ExpressionAssetError('expression_migration_paused')
        validate(self.store, db, turn, json.loads(draft['context_json']))
        from .private_personas import guard_cloud_dispatch
        guard_cloud_dispatch(self.store, turn['persona_id'], 'main', json.loads(turn['model_snapshot']))

    def _candidate_check(self, db, run):
        if run['candidate_snapshot_id']:
            row = db.execute('SELECT * FROM expression_candidate_snapshots WHERE id=?', (run['candidate_snapshot_id'],)).fetchone()
            if not row:raise ExpressionAssetError('expression_candidate_snapshot_missing')
            data = json.loads(row['data_json'])
            owner = dict(db.execute('SELECT * FROM turns WHERE id=?',(run['turn_id'],)).fetchone())
            if data['persona_id'] != owner['persona_id'] or data['scope'] != turn_scope(db,owner):
                raise ExpressionAssetError('expression_candidate_scope_mismatch')
            if fingerprint(data) != row['data_hash']:raise ExpressionAssetError('expression_candidate_snapshot_changed')
            ExpressionCandidates(self.store)._validate(db, data)

    def prepare(self, turn_id):
        with self.store.connection() as db:
            turn = dict(db.execute('SELECT * FROM turns WHERE id=?', (turn_id,)).fetchone())
            prior = db.execute('SELECT * FROM expression_runs WHERE turn_id=?', (turn_id,)).fetchone()
            if prior:return dict(prior)
            draft = self._draft(db, turn_id);self._validate_main(db, turn, draft)
            chosen = accepted(db, turn_id)
            protocol=chosen['data'].get('protocol',JSON_PROTOCOL)
            if protocol not in (JSON_PROTOCOL,PERSONA_PROFILE):raise ExpressionAssetError('expression_runtime_protocol_invalid')
            frozen = json.loads(turn['profile_snapshot'])
            if protocol==PERSONA_PROFILE:
                material={'voice':{'core_traits':frozen['core']['data']['core_traits'],'style':frozen['expression']['data']['style']},'draft':draft['body']}
            else:
                role = {k: frozen['core']['data'][k] for k in ('name','core_traits','values_and_boundaries')}
                role['expression_style'] = frozen['expression']['data']['style']
                context = json.loads(draft['context_json']);history = []
                selected = turn_scope(db, turn)
                for item in context['details'].get('history_groups', [])[-2:]:
                    group = read_exact(db, selected, {'kind':'turn','id':item['turn_id']})
                    if group['status'] != 'found':raise ExpressionAssetError('expression_history_unavailable')
                    history.extend({'speaker':'user','text':m['text']} for m in group['members'])
                    history.extend({'speaker':'assistant','text':m['text']} for m in group['replies'])
                from .knowledge_search_notice import read
                notice = read(db, turn)
                material = {'persona':turn['persona_id'],'role_projection':role,'user':input_text(db,turn),'history':history,
                            'already_spoken':[notice['text']] if notice else [],'draft':draft['body'],'protected':protected(draft['body'])}
        if protocol==PERSONA_PROFILE:
            snapshot=None;fixtures={}
            try:messages=persona_messages(material['voice'],material['draft'])
            except ExpressionPayloadError as error:raise ExpressionAssetError(str(error)) from None
        else:
            query = material['user'][:6000] + '\n' + draft['body'][:6000]
            snapshot = ExpressionCandidates(self.store).select(turn['persona_id'], selected, query[:12000])
            candidates = snapshot['items'];material['candidate_ids'] = [item['id'] for item in candidates]
            fixtures = {'assets':[{'id':item['id'],'persona':turn['persona_id'],'situation':item['situation'],'style':item['style']} for item in candidates]}
            messages, _ = build_messages(material, fixtures, JSON_PROTOCOL)
        limits = dict(DEFAULTS) | chosen['data']
        main_model = json.loads(turn['model_snapshot'])
        public = {'base_url':main_model['base_url'],'name':main_model['name'],'max_tokens':limits['max_tokens'],
                  'timeout':limits['timeout'],'_json_output':True,
                  '_dialogue_profile':{'version':'natural-dialogue-v8','seed':turn_id,'temperature':1.0}}
        from .provider_policy import require_generation_provider
        require_generation_provider(public)
        from .provider_compat import build_request
        request = build_request(public,messages)[0]
        with self.store.connection(write=True) as db:
            current = dict(db.execute('SELECT * FROM turns WHERE id=?',(turn_id,)).fetchone());self._validate_main(db,current,draft)
            old = db.execute('SELECT * FROM expression_runs WHERE turn_id=?',(turn_id,)).fetchone()
            if old:return dict(old)
            size=len(json.dumps(request if protocol==PERSONA_PROFILE else messages,ensure_ascii=False))
            state = 'skipped' if size > limits['max_input_characters'] else 'prepared'
            ident = identifier()
            db.execute('''INSERT INTO expression_runs
              (id,turn_id,draft_id,state,policy_json,input_json,request_json,request_hash,model_json,candidate_snapshot_id,error_code)
              VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                       (ident,turn_id,draft['id'],state,json.dumps(chosen),json.dumps({'material':material,'fixtures':fixtures},ensure_ascii=False),
                        json.dumps(request,ensure_ascii=False),fingerprint(request),json.dumps(public),snapshot['id'] if snapshot else None,
                        'expression_input_capacity' if state=='skipped' else None))
            return dict(db.execute('SELECT * FROM expression_runs WHERE id=?',(ident,)).fetchone())

    def record_response(self, run_id, attempt_id, raw):
        digest = hashlib.sha256(raw).hexdigest()
        with self.store.connection(write=True) as db:
            row = db.execute('SELECT * FROM expression_runs WHERE id=? AND attempt_id=?',(run_id,attempt_id)).fetchone()
            if not row:raise ExpressionAssetError('expression_response_attempt_mismatch')
            if row['raw_response'] is not None:
                if row['raw_response_hash'] != digest:raise ExpressionAssetError('expression_response_conflict')
                return
            db.execute('UPDATE expression_runs SET raw_response=?,raw_response_hash=? WHERE id=?',(raw,digest,run_id))

    async def execute(self, turn_id, model):
        from .expression_publication import ExpressionPublication
        publisher = ExpressionPublication(self)
        try:run = self.prepare(turn_id)
        except RuntimeErrorBase as error:
            return publisher.fallback(turn_id,str(error))
        if run['raw_response'] is not None:return self.finalize(run['id'])
        with self.store.connection() as db:
            publication=db.execute('SELECT state FROM expression_publications WHERE turn_id=?',(turn_id,)).fetchone()
            if publication:return {'state':publication['state'],'new_call':False}
        if run['attempt_id'] or run['state'] != 'prepared':
            if run['state'] == 'unknown' and (dict(DEFAULTS)|json.loads(run['policy_json'])['data'])['unknown_fallback']:
                return publisher.fallback(turn_id,'expression_result_unknown')
            if run['state'] in ('failed','skipped','stale'):return publisher.fallback(turn_id,run['error_code'])
            return {'state':run['state'],'new_call':False}
        public = json.loads(run['model_json'])
        if (model['base_url'],model['name']) != (public['base_url'],public['name']):return publisher.fallback(turn_id,'expression_model_changed')
        live_model = public | {'encrypted_key':model['encrypted_key']}
        try:
            from .private_personas import guard_cloud_dispatch
            guard_cloud_dispatch(self.store,self._turn(turn_id)['persona_id'],'expression',live_model)
            with self.store.connection(write=True) as db:
                fresh = dict(db.execute('SELECT * FROM expression_runs WHERE id=?',(run['id'],)).fetchone())
                if fresh['attempt_id'] or fresh['state']!='prepared':return {'state':fresh['state'],'new_call':False}
                turn = dict(db.execute('SELECT * FROM turns WHERE id=?',(turn_id,)).fetchone());draft=self._draft(db,turn_id);self._validate_main(db,turn,draft);self._candidate_check(db,fresh)
                current = db.execute('SELECT revision,data_json FROM expression_runtime_policies WHERE persona_id=?',(turn['persona_id'],)).fetchone()
                chosen = json.loads(run['policy_json'])
                if not current or current['revision']!=chosen['revision'] or not json.loads(current['data_json'])['enabled']:
                    raise ExpressionAssetError('expression_runtime_policy_changed')
                attempt = identifier()
                db.execute('UPDATE expression_runs SET state=\'dispatched\',attempt_id=?,started_epoch=? WHERE id=?',(attempt,time.time(),run['id']))
        except RuntimeErrorBase as error:return publisher.fallback(turn_id,str(error))
        try:
            request = json.loads(run['request_json'])
            if fingerprint(request)!=run['request_hash']:raise ExpressionAssetError('expression_request_changed')
            received = await self.client.complete(live_model,request['messages'],save_raw=lambda raw:self.record_response(run['id'],attempt,raw))
            with self.store.connection(write=True) as db:db.execute('UPDATE expression_runs SET transport_json=? WHERE id=?',(json.dumps(received['diagnostics']),run['id']))
        except BaseException as error:
            with self.store.connection(write=True) as db:
                saved = db.execute('SELECT raw_response FROM expression_runs WHERE id=?',(run['id'],)).fetchone()[0]
                if saved is None:
                    diagnostic=getattr(error,'diagnostics',{});unsent=diagnostic.get('request_headers_sent') is False and diagnostic.get('response_headers_received') is False
                    known = unsent or (diagnostic.get('response_headers_received') is True and diagnostic.get('http_status',200)!=200)
                    db.execute('UPDATE expression_runs SET state=?,transport_json=?,error_code=? WHERE id=? AND state!=\'cancelled\'',
                               ('failed' if known else 'unknown',json.dumps(diagnostic),'expression_not_dispatched' if unsent else 'expression_transport_unknown' if not known else 'expression_request_failed',run['id']))
            if isinstance(error,asyncio.CancelledError):
                if saved is not None:self.finalize(run['id'])
                raise
            if saved is None:
                if known or (dict(DEFAULTS)|json.loads(run['policy_json'])['data'])['unknown_fallback']:
                    return publisher.fallback(turn_id,'expression_not_dispatched' if unsent else 'expression_result_unknown')
                return {'state':'unknown','new_call':None}
        return self.finalize(run['id'])

    def finalize(self, run_id):
        from .expression_publication import ExpressionPublication
        publisher = ExpressionPublication(self)
        fallback = None
        with self.store.connection(write=True) as db:
            row = dict(db.execute('SELECT * FROM expression_runs WHERE id=?',(run_id,)).fetchone())
            raw = bytes(row['raw_response']) if row['raw_response'] is not None else None
            if raw is None:return {'state':row['state'],'new_call':False}
            if hashlib.sha256(raw).hexdigest()!=row['raw_response_hash']:raise ExpressionAssetError('expression_response_changed')
            if row['result_json']:
                return {'state':row['state'],'result':json.loads(row['result_json']),'new_call':False}
            try:data=json.loads(raw)
            except (ValueError,TypeError):data={}
            from .provider_compat import usage_fields
            usage=usage_fields(data.get('usage') if isinstance(data,dict) else None,deepseek=True)
            db.execute('UPDATE expression_runs SET usage_json=? WHERE id=?',(json.dumps(usage) if usage is not None else None,run_id))
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(row['turn_id'],)).fetchone());draft=self._draft(db,turn['id'])
            try:
                self._validate_main(db,turn,draft);self._candidate_check(db,row)
                policy=db.execute('SELECT data_json FROM expression_runtime_policies WHERE persona_id=?',(turn['persona_id'],)).fetchone()
                if not policy or not json.loads(policy[0])['enabled']:raise ExpressionAssetError('expression_runtime_disabled')
                choice=data['choices'][0]
                if choice.get('finish_reason')!='stop' or choice['message'].get('tool_calls'):raise ExpressionPayloadError('expression_completion_unusable')
                inputs=json.loads(row['input_json']);protocol=json.loads(row['policy_json'])['data'].get('protocol',JSON_PROTOCOL)
                if protocol==PERSONA_PROFILE:
                    candidate=persona_output(choice['message']['content'],inputs['material']['draft'],
                        segment_transport=json.loads(row['policy_json'])['data'].get('segment_transport',STRING_SEGMENTS))
                elif protocol==JSON_PROTOCOL:
                    _,candidates=build_messages(inputs['material'],inputs['fixtures'],JSON_PROTOCOL)
                    candidate=decode_output_for(JSON_PROTOCOL,choice['message']['content'],inputs['material'],candidates)
                else:raise ExpressionAssetError('expression_runtime_protocol_invalid')
                publication=db.execute('SELECT * FROM expression_publications WHERE turn_id=?',(turn['id'],)).fetchone()
                if publication:
                    result={'candidate':candidate,'late_evidence_only':True,'publication_id':publication['id']}
                else:
                    published=publisher._publish(db,turn,draft,'artifact',row['id'],candidate['body'],candidate['segments'],'expression_completed')
                    result={'candidate':candidate,'publication_id':published['id'],'late_evidence_only':False}
                db.execute('UPDATE expression_runs SET state=\'completed\',result_json=? WHERE id=?',(json.dumps(result,ensure_ascii=False),run_id))
                return {'state':'completed','result':result}
            except (RuntimeErrorBase,ExpressionPayloadError,KeyError,IndexError,TypeError) as error:
                code=str(error) if isinstance(error,(RuntimeErrorBase,ExpressionPayloadError)) else 'expression_invalid_provider_response'
                state='cancelled' if row['state']=='cancelled' else 'stale' if isinstance(error,ExpressionAssetError) else 'failed'
                db.execute('UPDATE expression_runs SET state=?,result_json=?,error_code=? WHERE id=?',(state,json.dumps({'candidate':None,'error':code}),code,run_id));fallback=code
        return publisher.fallback(row['turn_id'],fallback)

    def _turn(self, turn_id):
        with self.store.connection() as db:
            row=db.execute('SELECT * FROM turns WHERE id=?',(turn_id,)).fetchone()
            if not row:raise ExpressionAssetError('expression_turn_missing')
            return dict(row)

    def inspect(self, turn_id):
        from .dialogue_generation import enabled as direct_enabled
        with self.store.connection() as db:
            if direct_enabled(db,turn_id):
                from .dialogue_outlet import DirectReplies
                return DirectReplies.main_info(db,turn_id)
            mode=db.execute('SELECT * FROM expression_runtime_turns WHERE turn_id=?',(turn_id,)).fetchone()
            if not mode or not json.loads(mode['policy_json'])['data'].get('enabled'):return None
            receipt=db.execute('SELECT id,valid,body_hash FROM expression_main_receipts WHERE turn_id=?',(turn_id,)).fetchone()
            run=db.execute('SELECT id,state,attempt_id,usage_json,error_code FROM expression_runs WHERE turn_id=?',(turn_id,)).fetchone()
            pub=db.execute('SELECT id,revision,state,producer_kind,reason FROM expression_publications WHERE turn_id=?',(turn_id,)).fetchone()
            return {'draft':dict(receipt) if receipt else None,'run':dict(run) if run else None,'publication':dict(pub) if pub else None}

    async def wait_prior(self, turn):
        while True:
            with self.store.connection() as db:
                row=db.execute("""SELECT d.id FROM expression_main_receipts d JOIN turns t ON t.id=d.turn_id
                  JOIN expression_runtime_turns m ON m.turn_id=t.id LEFT JOIN expression_publications p ON p.turn_id=t.id
                  WHERE t.persona_id=? AND t.session_id=? AND t.rowid<(SELECT rowid FROM turns WHERE id=?) AND d.valid=1
                  AND json_extract(m.policy_json,'$.data.enabled')=1
                  AND ((p.id IS NOT NULL AND p.state NOT IN ('closed','complete')) OR
                       (p.id IS NULL AND m.cancelled_at IS NULL AND m.closed_at IS NULL)) LIMIT 1""",
                  (turn['persona_id'],turn['session_id'],turn['id'])).fetchone()
            if not row:
                with self.store.connection() as db:
                    pending=db.execute('''SELECT 1 FROM expression_speech_units u JOIN turns t ON t.id=u.turn_id
                      WHERE u.persona_id=? AND u.session_id=? AND t.rowid<(SELECT rowid FROM turns WHERE id=?)
                      AND (u.phase!='main' OR u.source_kind='dialogue_reply') AND u.closed_at IS NULL LIMIT 1''',(turn['persona_id'],turn['session_id'],turn['id'])).fetchone()
                if not pending:return
            await asyncio.sleep(.05)

    def recover(self):
        """只恢复本地回执／状态；准备中表达由显式resume继续，不重跑主模型。"""
        from .migration import migration_pending
        if migration_pending(self.store.paths):return {'paused':True}
        self.outlet.recover()
        with self.store.connection(write=True) as db:
            for receipt in db.execute('SELECT * FROM expression_main_receipts WHERE valid=1').fetchall():
                turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(receipt['turn_id'],)).fetchone())
                self.outlet.register_main(db,turn,dict(receipt))
            db.execute("UPDATE expression_runs SET state='unknown',error_code='service_restarted' WHERE state='dispatched' AND raw_response IS NULL")
            rows=[dict(r) for r in db.execute('SELECT r.id,r.turn_id,r.raw_response FROM expression_runs r')]
            publications=[dict(r) for r in db.execute('SELECT p.turn_id,d.logical_attempt_id,d.usage_json FROM expression_publications p JOIN expression_main_receipts d ON d.turn_id=p.turn_id WHERE p.state NOT IN (\'closed\',\'complete\')')]
            for row in publications:
                cancelled=db.execute('SELECT cancelled_at FROM expression_runtime_turns WHERE turn_id=?',(row['turn_id'],)).fetchone()[0]
                if cancelled:continue
                db.execute("UPDATE turns SET status='completed',error_code=NULL WHERE id=? AND cancelled_at IS NULL",(row['turn_id'],))
                db.execute("UPDATE attempts SET status='completed',error_code=NULL,usage_json=? WHERE id=?",(row['usage_json'],row['logical_attempt_id']))
        for row in rows:
            if row['raw_response'] is not None:self.finalize(row['id'])
        return {'local_only':True,'new_model_calls':0}
