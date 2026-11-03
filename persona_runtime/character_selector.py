"""Audited cloud selection with at most three retries, never a canon writer."""
import asyncio
import json
import sqlite3
import time
from .character_embedding import SemanticFailure, digest
from .character_context import select_context
from .character_policy import read
from .character_runtime import validate_snapshot
from .character_selector_protocol import messages, parse_output, apply_selection, PROMPT_VERSION, encode, prompt_for
from .config import SELECTOR_TIMEOUT_MAX
from .memory_policy import project_capacity_used
from .models import LOCAL_USER_ID, RuntimeErrorBase, identifier, timestamp
from .turn_input import load_turn_input


def probe_material(model):
    """Only these synthetic bytes may bypass a private-author upload grant."""
    pool={'payload':{'roots':{'lore':['probe'],'example':[]},'groups':{'lore:probe':['probe']},
          'sources':[{'kind':'lore','id':'probe','source_hash':digest('人工测试'),'title':'测试偏好','text':'人工测试角色喜欢紫色。'}]}}
    pool['fingerprint']=digest(pool['payload'])
    payload=messages(pool,['测试角色偏好什么颜色？'],'',2,model['max_input_chars'],prompt_version=PROMPT_VERSION)
    return pool,payload


class SelectorService:
    def __init__(self,store,config,client,characters):
        self.store,self.config,self.client,self.characters=store,config,client,characters
        self.busy=False

    def recover(self,conn):
        conn.execute("UPDATE character_selection_attempts SET status='interrupted',error_code='service_restarted',finished_epoch=? WHERE status='running'",(time.time(),))
        conn.execute("UPDATE quota_reservations SET state='released' WHERE state='active'")

    def release(self,turn_id):
        with self.store.connection(write=True) as conn:
            conn.execute("UPDATE quota_reservations SET state='released' WHERE turn_id=? AND state='active'",(turn_id,))

    def list(self,persona_id,turn_id=None):
        with self.store.connection() as conn:
            return [{k:v for k,v in dict(r).items() if k!='model_snapshot'} for r in conn.execute(
                'SELECT * FROM character_selection_attempts WHERE persona_id=? AND (? IS NULL OR turn_id=?) ORDER BY rowid DESC LIMIT 50',(persona_id,turn_id,turn_id))]

    def _start(self,conn,persona_id,turn,model,payload,pool,operation_id=None,*,prompt_version=PROMPT_VERSION):
        from .private_personas import guard_cloud_dispatch
        if turn is not None:
            guard_cloud_dispatch(self.store,persona_id,'selector',model)
        else:
            expected_pool,expected_payload=probe_material(model)
            if pool!=expected_pool or payload!=expected_payload or prompt_version!=PROMPT_VERSION:
                raise RuntimeErrorBase('selector_probe_material_invalid')
        system=prompt_for(prompt_version)
        if not payload or payload[0]!={'role':'system','content':system}:
            raise RuntimeErrorBase('selector_prompt_snapshot_mismatch')
        now=time.time();cfg=self.config.load();limit=cfg['model']['hourly_limit']
        conn.execute("UPDATE quota_reservations SET state='expired' WHERE state='active' AND expires_epoch<=?",(now,))
        if turn:
            current=conn.execute('SELECT * FROM turns WHERE id=?',(turn['id'],)).fetchone()
            if not current or current['status']!='queued' or current['cancelled_at'] or current['execution_revision']!=turn['execution_revision']:
                raise SemanticFailure('selector_request_inactive')
            from .context_builders import policy_for
            expected=read(policy_for(dict(current))).get('selector_contract',{}).get('prompt')
            if expected!=prompt_version:raise RuntimeErrorBase('selector_prompt_snapshot_mismatch')
            validate_snapshot(conn,dict(current))
            from .references import prepare
            prepare(conn,dict(current))
            old=conn.execute('SELECT id FROM character_selection_attempts WHERE turn_id=? AND execution_revision=?',(turn['id'],turn['execution_revision'])).fetchone()
            if old:raise SemanticFailure('selector_already_attempted')
        needed=2 if turn else 1
        if limit and project_capacity_used(conn,now-3600)+needed>limit:raise SemanticFailure('selector_project_quota')
        for field,period in (('hourly_limit',3600),('daily_limit',86400)):
            cap=cfg['character_selector'][field]
            from .generation_retry import call_count
            count=sum(call_count(conn,t,now-period) for t in ('character_selection_attempts','disposition_compile_attempts','situation_observation_attempts'))
            if cap and count>=cap:raise SemanticFailure('selector_quota')
        aid=identifier();tid=turn['id'] if turn else None;revision=turn['execution_revision'] if turn else None
        from .archive import turn_scope
        scope=turn_scope(conn,turn) if turn else {'persona_id':persona_id,'user_id':LOCAL_USER_ID,'channel':'test'}
        conn.execute('INSERT INTO character_selection_attempts (id,turn_id,execution_revision,persona_id,user_id,purpose,operation_id,status,started_epoch,model_snapshot,input_hash,candidate_hash,scope_json,created_at) VALUES(?,?,?,?,?,?,?,\'running\',?,?,?,?,?,?)',
                     (aid,tid,revision,persona_id,LOCAL_USER_ID,'conversation' if turn else 'provider_probe',operation_id,now,encode(model),digest(payload),pool['fingerprint'],encode(scope),timestamp()))
        manifest={'context_id':identifier(),'task_type':'character_selection','purpose':'conversation' if turn else 'provider_probe',
            'turn_id':tid,'selection_attempt_id':aid,'prompt_version':prompt_version,'prompt_hash':digest(system),'input_snapshot_hash':digest(payload),
            'candidate_hash':pool['fingerprint'],'candidate_ids':pool['payload']['roots'],
            'source_hashes':[{k:r[k] for k in ('kind','id','source_hash')} for r in pool['payload']['sources']],
            'scope':scope,'model_name':model['name'],'model_snapshot_hash':digest({k:v for k,v in model.items() if k!='encrypted_key'}),'created_at':timestamp(),'persisted':True}
        from .provider_compat import describe_request
        manifest['provider_request_contract']=describe_request(model|{'_json_output':True,'_character_selection':True})
        raw=encode(manifest)
        if len(raw.encode('utf-8'))>128*1024:raise RuntimeErrorBase('context_manifest_too_large')
        conn.execute('INSERT INTO context_manifests(id,persona_id,user_id,task_type,turn_id,selection_attempt_id,manifest_json,created_at) VALUES(?,?,?,\'character_selection\',?,?,?,?)',
                     (manifest['context_id'],persona_id,LOCAL_USER_ID,tid,aid,raw,timestamp()))
        if turn:
            from .progress import record
            record(conn,tid,'character_selection')
            conn.execute('INSERT INTO quota_reservations VALUES(?,?,?,?,?,\'active\',?,?)',(identifier(),tid,revision,persona_id,LOCAL_USER_ID,now,now+min(SELECTOR_TIMEOUT_MAX,model['timeout'])+60))
        return aid

    async def _call(self,persona_id,turn,model,payload,pool,limit,operation_id=None,*,prompt_version=PROMPT_VERSION):
        from .generation_retry import audited_complete, call_count
        if self.busy:raise SemanticFailure('selector_busy')
        self.busy=True;aid=None;usage=None;answer=None;error=None;pending=None
        try:
            with self.store.connection(write=True) as conn:
                aid=self._start(conn,persona_id,turn,model,payload,pool,operation_id,prompt_version=prompt_version)
                initial_source=digest(validate_snapshot(conn,turn)) if turn else None
            def save(value):
                nonlocal usage
                usage=value
                with self.store.connection(write=True) as conn:
                    changed=conn.execute("UPDATE character_selection_attempts SET usage_json=? WHERE id=? AND status='running'",(encode(value),aid)).rowcount
                    if changed!=1:raise RuntimeErrorBase('selector_retry_audit_invalid')
                if getattr(self,'audit_sink',None):self.audit_sink(value)
            def before_attempt(state):
                with self.store.connection(write=True) as conn:
                    from .private_personas import guard_cloud_dispatch
                    if turn:
                        current=dict(conn.execute('SELECT * FROM turns WHERE id=?',(turn['id'],)).fetchone())
                        if current['status']!='queued' or current['cancelled_at'] or current['execution_revision']!=turn['execution_revision']:
                            raise RuntimeErrorBase('selector_request_inactive')
                        guard_cloud_dispatch(self.store,persona_id,'selector',model)
                        if digest(validate_snapshot(conn,current))!=initial_source:raise RuntimeErrorBase('character_source_changed')
                        from .references import prepare
                        refs,_=prepare(conn,current)
                        members=[m['text'] for m in load_turn_input(conn,current)]
                        if messages(pool,members,refs,limit,model['max_input_chars'],prompt_version=prompt_version)!=payload:
                            raise RuntimeErrorBase('selector_retry_sources_changed')
                    if state['attempts']:
                        now=time.time();cfg=self.config.load();cap=cfg['model']['hourly_limit']
                        if cap and project_capacity_used(conn,now-3600)+1>cap:raise RuntimeErrorBase('selector_project_quota')
                        for field,period in (('hourly_limit',3600),('daily_limit',86400)):
                            cap=cfg['character_selector'][field]
                            count=sum(call_count(conn,t,now-period) for t in ('character_selection_attempts','disposition_compile_attempts','situation_observation_attempts'))
                            if cap and count+1>cap:raise RuntimeErrorBase('selector_quota')
                    if turn:
                        conn.execute("UPDATE quota_reservations SET expires_epoch=? WHERE turn_id=? AND execution_revision=? AND state='active'",(time.time()+min(SELECTOR_TIMEOUT_MAX,model['timeout'])+60,turn['id'],turn['execution_revision']))
            try:
                completion=await audited_complete(self.client,model|{'_json_output':True,'_character_selection':True,'timeout':min(SELECTOR_TIMEOUT_MAX,model['timeout'])},payload,
                    validate=lambda text:parse_output(text,pool,limit),save=save,before_attempt=before_attempt,
                    prior_failures=getattr(self,'retry_prior_failures',0),timeout_code='selector_timeout')
                text,usage=completion;answer=parse_output(text,pool,limit)
            except BaseException as exc:
                error='selector_interrupted' if isinstance(exc,asyncio.CancelledError) else str(exc)
                pending=getattr(exc,'pending_task',None)
                raise
            finally:
                with self.store.connection(write=True) as conn:
                    conn.execute("UPDATE character_selection_attempts SET status=?,finished_epoch=?,selected_json=?,error_code=?,usage_json=? WHERE id=? AND status='running'",
                        ('completed' if answer is not None else 'interrupted' if error=='selector_interrupted' else 'failed',time.time(),encode(answer) if answer is not None else None,error,encode(usage) if usage else None,aid))
            return answer,error,aid
        finally:
            if pending is not None and not pending.done():pending.add_done_callback(lambda _:setattr(self,'busy',False))
            else:self.busy=False

    async def prepare(self,turn_id):
        from .context_builders import policy_for
        from .behavior import resolve_behavior
        with self.store.connection() as conn:
            turn=dict(conn.execute('SELECT * FROM turns WHERE id=?',(turn_id,)).fetchone())
            session=getattr(self,'evaluation_session',None)
            if session:
                row=session.get(self.engine,conn,turn)
                if row and row['arm']=='A':return None
                if row and row['arm']=='C':return session.reuse(self.engine,conn,turn)
                if row and row['arm']=='B' and session.has_material(row):return session.reuse(self.engine,conn,turn)
            policy=read(policy_for(turn))
            if policy['mode']!='hybrid_llm' or turn['channel']=='test':return None
            version=validate_snapshot(conn,turn)
            if version is None:return None
            from .references import prepare
            references,_=prepare(conn,turn)
            members=[m['text'] for m in load_turn_input(conn,turn)]
        limit=2 if (turn.get('reply_mode_snapshot') or 'legacy_full')=='legacy_full' else 4
        scenarios=[s['act'] for s in resolve_behavior('\n\n'.join(members))['signals']] if limit==4 else []
        fallback=select_context(version,'\n\n'.join(members),members=members,scenario=scenarios,example_limit=limit)
        fallback.update(requested_mode='hybrid_llm',effective_mode='lexical',degrade_reason=None)
        started=time.monotonic();aid=None
        # Evaluation recovery must reuse a terminal paid attempt, including the
        # production lexical fallback. Never turn a missing artifact into a new call.
        if getattr(self,'evaluation_session',None):
            with self.store.connection() as conn:
                previous=conn.execute('SELECT * FROM character_selection_attempts WHERE turn_id=? AND execution_revision=?',(turn_id,turn['execution_revision'])).fetchone()
            if previous is not None:
                if previous['status'] not in ('failed','interrupted'):
                    raise RuntimeErrorBase('evaluation_selection_recovery_requires_material')
                result=fallback|{'degrade_reason':previous['error_code'],'selection_attempt_id':previous['id'],'preparation_ms':0}
                self.evaluation_session.capture(self.engine,turn,result)
                return result
        from .progress import record
        with self.store.connection(write=True) as conn:record(conn,turn_id,'character_local')
        try:
            model=json.loads(turn.get('selector_model_snapshot') or '{}')
            if not model.get('enabled') or not model.get('base_url') or not model.get('name'):raise SemanticFailure('selector_not_configured')
            pool=await asyncio.wait_for(asyncio.to_thread(self.characters.local_pool,version,policy,members,scenarios),timeout=1)
            if not any(pool['payload']['roots'].values()):raise SemanticFailure('no_candidates')
            prompt_version=policy['selector_contract']['prompt']
            payload=messages(pool,members,references,limit,model['max_input_chars'],prompt_version=prompt_version)
            answer,error,aid=await self._call(turn['persona_id'],turn,model,payload,pool,limit,prompt_version=prompt_version)
            if error:raise SemanticFailure(error)
            with self.store.connection() as conn:
                current=dict(conn.execute('SELECT * FROM turns WHERE id=?',(turn_id,)).fetchone())
                if current['execution_revision']!=turn['execution_revision'] or current['status']!='queued' or current['cancelled_at']:raise SemanticFailure('selector_request_inactive')
                checked=validate_snapshot(conn,current)
                if digest(checked)!=digest(version):raise RuntimeErrorBase('character_source_changed')
                prepare(conn,current)
            result=apply_selection(version,members,pool,answer,limit)
        except (TimeoutError,asyncio.TimeoutError):result=fallback;result['degrade_reason']='character_semantic_timeout'
        except (sqlite3.Error,OSError):raise RuntimeErrorBase('selector_ledger_storage_failed') from None
        except SemanticFailure as exc:
            from .character_retrieval import FALLBACK_CODES
            reason=str(exc)
            if reason not in FALLBACK_CODES and not reason.startswith(('selector_','provider_')) and reason!='no_candidates':raise
            result=fallback;result['degrade_reason']=reason
        result.setdefault('budgets',{name:sum(len(p['text'])+1 for p in result['parts'] if p['kind'] in kinds) for name,kinds in {'anchor':('anchor',),'dynamic':('fact','lore_group'),'examples':('example',)}.items()})
        result['budgets']['total']=len(result['text'])
        result.update(selection_attempt_id=aid,preparation_ms=round((time.monotonic()-started)*1000,3))
        with self.store.connection(write=True) as conn:
            record(conn,turn_id,'character_fallback' if result['degrade_reason'] else 'character_selected')
        session=getattr(self,'evaluation_session',None)
        if session:session.capture(self.engine,turn,result)
        return result

    async def probe(self,persona_id,operation_id):
        if not isinstance(operation_id,str) or not 1<=len(operation_id)<=200:raise RuntimeErrorBase('invalid_operation_id')
        with self.store.connection() as conn:
            if self.store.current(conn)!=persona_id:raise RuntimeErrorBase('character_selection_conflict')
            old=conn.execute('SELECT * FROM character_selection_attempts WHERE operation_id=?',(operation_id,)).fetchone()
            if old:
                if old['persona_id']!=persona_id:raise RuntimeErrorBase('selector_operation_conflict')
                return {k:v for k,v in dict(old).items() if k!='model_snapshot'}
        model=self.config.load()['character_selector']
        if not model['enabled']:raise RuntimeErrorBase('selector_not_configured')
        pool,payload=probe_material(model)
        answer,error,aid=await self._call(persona_id,None,model,payload,pool,2,operation_id)
        return {'selection_attempt_id':aid,'answer':answer,'error':error}
