"""通用人物出口：源稿与口吻冻结，主回复兼容31，查前发言独立记账。"""
from .call_limits import resume_on_limit, gate, turn_scope as quota_turn, CallLimitPending
from .local_repair import RepairInProgress
import asyncio
import hashlib
import json
import time

from .archive import fingerprint
from .expression_assets import ExpressionAssetError
from .expression_contract import text_hash
from .expression_persona_draft import PROFILE,build_messages,decode_output
from .expression_persona_draft import STRING_SEGMENTS
from .expression_protocol import ExpressionPayloadError
from .models import identifier,timestamp,RuntimeErrorBase


def encoded(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'))


def require_supported_channel(db,persona_id,channel):
    """未来人物输出须先具备载体适配；当前候选只开放桌面单元。"""
    row=db.execute('SELECT data_json FROM expression_runtime_policies WHERE persona_id=?',(persona_id,)).fetchone()
    policy=json.loads(row[0]) if row else {}
    from .dialogue_generation import available
    direct=db.execute("SELECT json_extract(data_json,'$.enabled') FROM dialogue_generation_policies WHERE persona_id=?",(persona_id,)).fetchone() if available(db) else None
    if channel!='desktop' and ((policy.get('enabled') and policy.get('protocol')==PROFILE) or (direct and direct[0])):
        raise ExpressionAssetError('character_outlet_channel_not_enabled')


class CharacterOutlet:
    def __init__(self,runtime):self.runtime,self.store=runtime,runtime.store

    @staticmethod
    def available(db):return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='expression_speech_units'").fetchone())

    def freeze_notice(self,db,turn,task,context):
        from .expression_runtime import accepted
        policy=accepted(db,turn['id'])
        if not self.available(db) or not policy['data'].get('enabled') or policy['data'].get('protocol')!=PROFILE:return False
        source=db.execute('SELECT persona_id,session_id,payload_json FROM organization_tasks WHERE id=?',(task['id'],)).fetchone()
        spec=json.loads(source['payload_json']).get('knowledge_processing',{}) if source else {}
        if not source or (source['persona_id'],source['session_id'])!=(turn['persona_id'],turn['session_id']) or spec.get('root_turn_id')!=turn['id'] or spec.get('stage')!='conversation_search_notice':
            raise ExpressionAssetError('speech_context_source_scope')
        frozen=json.loads(turn['profile_snapshot'])
        voice={'core_traits':frozen['core']['data']['core_traits'],'style':frozen['expression']['data']['style']}
        values=(task['id'],turn['id'],'search_notice',encoded(policy),encoded(voice),encoded(context))
        old=db.execute('SELECT * FROM expression_speech_contexts WHERE task_id=?',(task['id'],)).fetchone()
        if old:
            if tuple(old)[:6]!=values:raise ExpressionAssetError('speech_context_conflict')
        else:db.execute('INSERT INTO expression_speech_contexts VALUES(?,?,?,?,?,?,?)',(*values,timestamp()))
        return True

    def _register(self,db,turn,phase,kind,source_id,source_hash,body,policy,voice,context,task_id=None):
        if (kind,phase) not in (('main_receipt','main'),('organization_receipt','search_notice'),('dialogue_reply','main'),('dialogue_reply','search_notice'),('scene_reply','main'),('maibot_private_reply','main'),('maibot_private_reply','search_notice')):
            raise ExpressionAssetError('speech_source_adapter_required')
        old=db.execute('SELECT * FROM expression_speech_units WHERE source_kind=? AND source_id=?',(kind,source_id)).fetchone()
        if old:
            if (old['turn_id'],old['phase'],old['source_hash'],old['body_hash'])!=(turn['id'],phase,source_hash,text_hash(body)):
                raise ExpressionAssetError('speech_source_conflict')
            return dict(old)
        if db.execute('SELECT 1 FROM expression_speech_units WHERE turn_id=? AND phase=?',(turn['id'],phase)).fetchone():
            raise ExpressionAssetError('speech_stage_already_owned')
        uid=identifier()
        db.execute('INSERT INTO expression_speech_units VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
          (uid,turn['id'],phase,kind,source_id,source_hash,turn['persona_id'],turn['session_id'],task_id,body,text_hash(body),
           encoded(policy),encoded(voice),encoded(context),None,None,timestamp()))
        return dict(db.execute('SELECT * FROM expression_speech_units WHERE id=?',(uid,)).fetchone())

    def register_main(self,db,turn,draft):
        from .expression_runtime import accepted
        policy=accepted(db,turn['id'])
        if not self.available(db) or policy['data'].get('protocol')!=PROFILE:return None
        frozen=json.loads(turn['profile_snapshot'])
        return self._register(db,turn,'main','main_receipt',draft['id'],draft['raw_hash'],draft['body'],policy,
          {'core_traits':frozen['core']['data']['core_traits'],'style':frozen['expression']['data']['style']},json.loads(draft['context_json']))

    def unit(self,db,utterance_id):
        row=db.execute('SELECT * FROM expression_speech_units WHERE id=?',(utterance_id,)).fetchone()
        if not row:raise ExpressionAssetError('speech_utterance_missing')
        return dict(row)

    def validate(self,db,unit):
        if unit['cancelled_at'] or unit['closed_at']:raise ExpressionAssetError('speech_utterance_closed')
        root=db.execute('SELECT * FROM turns WHERE id=?',(unit['turn_id'],)).fetchone()
        if not root:raise ExpressionAssetError('speech_parent_missing')
        turn=dict(root)
        if unit['source_kind']=='maibot_private_reply':
            return self.ordinary.validate_publication(db,turn,unit)
        if unit['source_kind']=='dialogue_reply':
            return self.direct.validate_publication(db,unit)
        if unit['source_kind']=='scene_reply':
            return self.scene.validate(db,turn,unit)
        mode=db.execute('SELECT cancelled_at,closed_at FROM expression_runtime_turns WHERE turn_id=?',(unit['turn_id'],)).fetchone()
        if mode and (mode[0] or mode[1]):raise ExpressionAssetError('speech_parent_closed')
        from .expression_sources import validate
        validate(self.store,db,turn,json.loads(unit['context_json']))
        if text_hash(unit['body'])!=unit['body_hash']:raise ExpressionAssetError('speech_source_changed')
        if unit['source_kind']=='organization_receipt':
            source=db.execute('SELECT response_text FROM organization_receipts WHERE attempt_id=?',(unit['source_id'],)).fetchone()
            if not source or fingerprint(source[0])!=unit['source_hash']:raise ExpressionAssetError('speech_source_changed')
            task=db.execute('SELECT status,error_code FROM organization_tasks WHERE id=?',(unit['task_id'],)).fetchone()
            if not task or task['status'] in ('cancelled','stale','blocked_authorization') or task['error_code']=='migration_requires_review':
                raise ExpressionAssetError('speech_source_task_inactive')
        from .private_personas import guard_cloud_dispatch
        guard_cloud_dispatch(self.store,turn['persona_id'],'main',json.loads(turn['model_snapshot']))
        return turn

    def _policy_check(self,db,unit):
        policy=json.loads(unit['policy_json'])
        current=db.execute('SELECT revision,data_json FROM expression_runtime_policies WHERE persona_id=?',(unit['persona_id'],)).fetchone()
        if not current or current['revision']!=policy['revision'] or not json.loads(current['data_json'])['enabled']:
            raise ExpressionAssetError('expression_runtime_policy_changed')

    def prepare_notice(self,task_id,attempt_id):
        with self.store.connection(write=True) as db:
            context=db.execute('SELECT * FROM expression_speech_contexts WHERE task_id=?',(task_id,)).fetchone()
            if not context:return None
            root=dict(db.execute('SELECT * FROM turns WHERE id=?',(context['turn_id'],)).fetchone())
            source=db.execute('SELECT * FROM organization_receipts WHERE attempt_id=?',(attempt_id,)).fetchone()
            if not source:raise ExpressionAssetError('speech_source_receipt_missing')
            owner=db.execute('SELECT task_id FROM organization_attempts WHERE id=?',(attempt_id,)).fetchone()
            if not owner or owner[0]!=task_id:raise ExpressionAssetError('speech_source_receipt_scope')
            audit=json.loads(source['diagnostic_json'])
            if audit.get('provider',{}).get('finish_reason') in ('length','content_filter'):raise ExpressionAssetError('speech_source_incomplete')
            from .reply_batch import validate_reply
            body=validate_reply(source['response_text'],'legacy_full').text
            return self._register(db,root,'search_notice','organization_receipt',attempt_id,fingerprint(source['response_text']),body,
               json.loads(context['policy_json']),json.loads(context['voice_json']),json.loads(context['context_json']),task_id)

    async def organization_received(self,task_id,attempt_id,model):
        unit=self.prepare_notice(task_id,attempt_id)
        if unit is None:return False
        await self.execute(unit['id'],model)
        with self.store.connection() as db:
            fresh=self.unit(db,unit['id'])
            pub=db.execute('SELECT state FROM expression_speech_publications WHERE utterance_id=?',(unit['id'],)).fetchone()
            if pub and pub['state']=='complete':return True
            if not pub or fresh['cancelled_at'] or fresh['closed_at'] or pub['state']=='closed':
                raise ExpressionAssetError('speech_notice_not_fully_presented')
        return unit['id']

    @resume_on_limit
    async def execute(self,utterance_id,model):
        from .expression_utterance_publication import SpeechPublication
        publisher=SpeechPublication(self)
        with self.store.connection() as db:
            unit=self.unit(db,utterance_id)
        if unit['phase']=='main':return await self.runtime.execute(unit['turn_id'],model)
        try:
            with self.store.connection(write=True) as db:
                unit=self.unit(db,utterance_id);turn=self.validate(db,unit)
                pub=db.execute('SELECT state FROM expression_speech_publications WHERE utterance_id=?',(utterance_id,)).fetchone()
                if pub:return {'state':pub[0],'new_call':False}
                run=db.execute('SELECT * FROM expression_speech_runs WHERE utterance_id=?',(utterance_id,)).fetchone()
                if not run:
                    policy=json.loads(unit['policy_json'])['data']
                    frozen=json.loads(turn['model_snapshot'])
                    public={k:frozen[k] for k in ('base_url','name')}
                    public.update(max_tokens=policy.get('max_tokens',8192),timeout=policy.get('timeout',120),_json_output=True,
                        _dialogue_profile={'version':'natural-dialogue-v8','seed':utterance_id,'temperature':1.0})
                    from .provider_policy import require_generation_provider
                    require_generation_provider(public)
                    from .provider_compat import build_request
                    request=build_request(public,build_messages(json.loads(unit['voice_json']),unit['body']))[0]
                    if len(encoded(request))>min(60000,policy.get('max_input_characters',60000)):
                        raise ExpressionAssetError('speech_request_capacity')
                    rid=identifier()
                    db.execute('INSERT INTO expression_speech_runs(id,utterance_id,state,request_json,request_hash,model_json) VALUES(?,?,?,?,?,?)',
                        (rid,utterance_id,'prepared',encoded(request),text_hash(encoded(request)),encoded(public)))
                    run=db.execute('SELECT * FROM expression_speech_runs WHERE id=?',(rid,)).fetchone()
                run=dict(run)
                if run['raw_response'] is not None:pass
                elif run['attempt_id'] or run['state']!='prepared':
                    if run['state'] in ('failed','unknown','skipped','stale') and (run['state']!='unknown' or json.loads(unit['policy_json'])['data'].get('unknown_fallback',True)):
                        pass
                    else:return {'state':run['state'],'new_call':False}
                else:
                    self._policy_check(db,unit)
                    public=json.loads(run['model_json'])
                    if (model['base_url'],model['name'])!=(public['base_url'],public['name']):raise ExpressionAssetError('expression_model_changed')
                    from .private_personas import guard_cloud_dispatch
                    guard_cloud_dispatch(self.store,unit['persona_id'],'expression',public|{'encrypted_key':model['encrypted_key']})
                    gate(db,self.store,'expression',quota_turn(turn))
                    attempt=identifier()
                    db.execute("UPDATE expression_speech_runs SET state='dispatched',attempt_id=?,started_epoch=? WHERE id=?",(attempt,time.time(),run['id']))
        except (CallLimitPending,RepairInProgress):raise
        except (RuntimeErrorBase,ExpressionPayloadError) as error:return publisher.fallback(utterance_id,str(error))
        if run['raw_response'] is not None:return self.finalize(run['id'])
        if run['attempt_id']:return publisher.fallback(utterance_id,run['error_code'] or 'expression_result_unknown')
        live=json.loads(run['model_json'])|{'encrypted_key':model['encrypted_key']}
        def received(raw):
            digest=hashlib.sha256(raw).hexdigest()
            with self.store.connection(write=True) as db:
                prior=db.execute('SELECT attempt_id,raw_response,raw_hash FROM expression_speech_runs WHERE id=?',(run['id'],)).fetchone()
                if prior['attempt_id']!=attempt:raise ExpressionAssetError('speech_response_attempt_mismatch')
                if prior['raw_response'] is not None:
                    if prior['raw_hash']!=digest:raise ExpressionAssetError('speech_response_conflict')
                else:db.execute('UPDATE expression_speech_runs SET raw_response=?,raw_hash=? WHERE id=?',(raw,digest,run['id']))
        try:
            response=await self.runtime.client.complete(live,json.loads(run['request_json'])['messages'],save_raw=received)
            with self.store.connection(write=True) as db:
                db.execute('UPDATE expression_speech_runs SET transport_json=? WHERE id=?',(encoded(response.get('diagnostics',{})),run['id']))
        except BaseException as error:
            diagnostic=getattr(error,'diagnostics',{})
            unsent=diagnostic.get('request_headers_sent') is False and diagnostic.get('response_headers_received') is False
            known=unsent or (diagnostic.get('response_headers_received') is True and diagnostic.get('http_status',200)!=200)
            with self.store.connection(write=True) as db:
                raw=db.execute('SELECT raw_response FROM expression_speech_runs WHERE id=?',(run['id'],)).fetchone()[0]
                if raw is None:db.execute("UPDATE expression_speech_runs SET state=CASE WHEN state='cancelled' THEN state ELSE ? END,transport_json=?,error_code=? WHERE id=?",
                    ('failed' if known else 'unknown',encoded(diagnostic|{'physical_outcome':'not_dispatched' if unsent else 'failed' if known else 'unknown'}),
                     'expression_not_dispatched' if unsent else 'expression_transport_unknown',run['id']))
            if isinstance(error,asyncio.CancelledError):raise
            if raw is None:
                if known or json.loads(unit['policy_json'])['data'].get('unknown_fallback',True):return publisher.fallback(utterance_id,'expression_transport_unknown')
                return {'state':'unknown','new_call':None}
        return self.finalize(run['id'])

    def finalize(self,run_id):
        from .expression_utterance_publication import SpeechPublication
        publisher=SpeechPublication(self);fallback=None
        with self.store.connection(write=True) as db:
            run=dict(db.execute('SELECT * FROM expression_speech_runs WHERE id=?',(run_id,)).fetchone());unit=self.unit(db,run['utterance_id'])
            raw=bytes(run['raw_response']) if run['raw_response'] is not None else None
            if raw is None:return {'state':run['state'],'new_call':False}
            if hashlib.sha256(raw).hexdigest()!=run['raw_hash']:raise ExpressionAssetError('speech_response_changed')
            if run['result_json']:return {'state':run['state'],'new_call':False}
            try:data=json.loads(raw)
            except (ValueError,TypeError):data={}
            from .provider_compat import usage_fields
            usage=usage_fields(data.get('usage') if isinstance(data,dict) else None,deepseek=True)
            db.execute('UPDATE expression_speech_runs SET usage_json=? WHERE id=?',(encoded(usage) if usage is not None else None,run_id))
            try:
                choice=data['choices'][0]
                if choice.get('finish_reason')!='stop' or choice['message'].get('tool_calls'):raise ExpressionAssetError('speech_completion_unusable')
                artifact=decode_output(choice['message']['content'],unit['body'],
                    segment_transport=json.loads(unit['policy_json'])['data'].get('segment_transport',STRING_SEGMENTS))
                pub=db.execute('SELECT id FROM expression_speech_publications WHERE utterance_id=?',(unit['id'],)).fetchone()
                late=bool(pub or unit['cancelled_at'] or unit['closed_at'])
                if not late:
                    self.validate(db,unit);self._policy_check(db,unit)
                    publisher.publish(db,unit,'artifact',artifact['body'],artifact['segments'],'expression_completed')
                result={'artifact':artifact,'late_evidence_only':late}
                db.execute("UPDATE expression_speech_runs SET state=?,result_json=? WHERE id=?",('cancelled' if unit['cancelled_at'] else 'completed',encoded(result),run_id))
                return {'state':'completed','late_evidence_only':late}
            except (RuntimeErrorBase,ExpressionPayloadError,KeyError,IndexError,TypeError) as error:
                fallback=str(error) if isinstance(error,(RuntimeErrorBase,ExpressionPayloadError)) else 'speech_provider_response_invalid'
                db.execute('UPDATE expression_speech_runs SET state=?,result_json=?,error_code=? WHERE id=?',
                    ('cancelled' if unit['cancelled_at'] else 'failed',encoded({'artifact':None,'error':fallback}),fallback,run_id))
        return publisher.fallback(unit['id'],fallback)

    def cancel_root(self,turn_id,reason='cancelled_by_user'):
        from .expression_utterance_publication import SpeechPublication
        with self.store.connection() as db:
            if not self.available(db):return
            ids=[r[0] for r in db.execute("SELECT id FROM expression_speech_units WHERE turn_id=? AND (phase!='main' OR source_kind IN ('dialogue_reply','scene_reply'))",(turn_id,))]
        for uid in ids:SpeechPublication(self).cancel(uid,reason)

    def recover(self):
        from .expression_utterance_publication import SpeechPublication
        with self.store.connection(write=True) as db:
            if not self.available(db):return
            db.execute("UPDATE expression_speech_runs SET state='unknown',error_code='service_restarted' WHERE state='dispatched' AND raw_response IS NULL")
            db.execute("UPDATE expression_speech_items SET state='unknown' WHERE state='offered'")
            db.execute("UPDATE expression_speech_publications SET state='unknown' WHERE id IN (SELECT publication_id FROM expression_speech_items WHERE state='unknown') AND state NOT IN ('closed','complete')")
            rows=[dict(r) for r in db.execute('SELECT r.*,u.turn_id FROM expression_speech_runs r JOIN expression_speech_units u ON u.id=r.utterance_id')]
        for row in rows:
            if row['raw_response'] is not None:self.finalize(row['id'])
            elif row['state']=='unknown':SpeechPublication(self).fallback(row['utterance_id'],'expression_result_unknown')
        return {'new_model_calls':0}
