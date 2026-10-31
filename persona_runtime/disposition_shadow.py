"""Bounded decisions: non-prompt Shadow and process-admitted Assist audit."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import asdict
from pathlib import Path
import json
import time

from .archive import encoded,fingerprint,turn_scope
from .disposition import (SHADOW_VERSION, COMPILER_VERSION, accepted_snapshot, source_catalog,
    status_for, version, DispositionService, fail)
from .disposition_core.contracts import VERSION,validate_input
from .disposition_core.engine import interpret,select_dispositions,decide,load_policy
from .models import LOCAL_USER_ID,identifier,timestamp
from .turn_input import load_turn_input

HEALTH=deque(maxlen=128)


def gap(code):
    HEALTH.append(code)


class ShadowService:
    def __init__(self,store,engine=None):
        self.engine=engine
        self.store=store;self.worker=ThreadPoolExecutor(max_workers=1,thread_name_prefix='pcar-shadow')
        self.future=None;self.closed=False
        self.policy=load_policy(Path(__file__).with_name('disposition_core')/'rules.json')

    def close(self):
        self.closed=True;self.worker.shutdown(wait=True,cancel_futures=True)

    def _compute(self,conn,pid,selected,members,snapshot):
        result={'id':identifier(),'version_id':snapshot.get('version_id'),'status':snapshot.get('status','not_configured'),
                'record':None,'policy':SHADOW_VERSION,'applied_to_prompt':False}
        if snapshot.get('policy')!=SHADOW_VERSION or snapshot.get('kernel')!=VERSION or snapshot.get('compiler')!=COMPILER_VERSION:
            result.update(status='snapshot_invalid',version_id=None);return result
        vid=snapshot.get('version_id')
        if not vid:return result
        v=version(conn,pid,vid)
        if fingerprint(v['document'])!=snapshot.get('document_hash'):
            result.update(status='snapshot_invalid');return result
        state=status_for(conn,pid,vid,current=False)
        if state!='ready':result['status']=state;return result
        doc=v['document'];catalog={r['source_id']:r for r in source_catalog(conn,pid,doc['source_versions'])}
        sources={};definitions=[]
        for d in doc['definitions']:
            refs=[]
            for ref in d['source_refs']:
                r=catalog[ref['source_id']]
                sources[r['source_id']]={'id':r['source_id'],'scope':selected,'kind':r['kind'],'version':r['version_id'],
                    'content_hash':r['content_hash'],'allowed':True,'withdrawn':False}
                refs.append({'id':r['source_id'],'version':r['version_id'],'content_hash':r['content_hash']})
            definitions.append({'id':d['id'],'persona_id':pid,'source_refs':refs,'confirmed':True,'enabled':d['enabled'],
                'triggers':d['triggers'],'preferences':d['preferences'],'priority':d['priority'],'revision':v['revision']})
        raw={'scope':selected,'members':[{'id':m['id'],'text':m['text']} for m in members],
            'sources':list(sources.values()),'definitions':definitions,'context':{'relationship':'neutral','relationship_source_id':'',
                'information_gap':False,'reference_gap':False,'resource_unavailable':False,'boundary_source_id':''},'policy':VERSION}
        checked=validate_input(raw);frame=interpret(checked.members,checked.context,self.policy)
        record=decide(frame,select_dispositions(checked,frame),self.policy).to_dict()
        return result|{'status':'observed','record':record}

    def _turn(self,turn,snapshot):
        with self.store.connection() as conn:
            return self._compute(conn,turn['persona_id'],turn_scope(conn,turn),load_turn_input(conn,turn),snapshot)

    def prepare(self,turn):
        overall=time.monotonic()
        if turn['channel'] not in ('desktop','qq'):return None
        try:context=json.loads(turn.get('context_policy_snapshot') or '{}')
        except (ValueError,TypeError):
            gap('shadow_prepare_failed')
            return {'id':identifier(),'version_id':None,'status':'snapshot_invalid','record':None,'applied_to_prompt':False,'policy':SHADOW_VERSION}
        selected=context.get('disposition')
        assist=context.get('disposition_mode')=='assist' or (isinstance(selected,dict) and selected.get('mode')=='assist')
        if assist:
            self.validate_assist(turn,selected)
            if context.get('disposition_mode')!='assist':
                from .chat import ModelError
                raise ModelError('assist_snapshot_invalid')
        try:
            context=json.loads(turn.get('context_policy_snapshot') or '{}')
            snapshot=context.get('disposition')
            if snapshot is None:return None
            if type(snapshot) is not dict or snapshot.get('mode') not in ('baseline','shadow','assist'):raise ValueError('shadow_snapshot_invalid')
            if snapshot['mode']=='baseline':return None
            if set(snapshot)!=({'mode','policy','kernel','compiler','version_id','status','document_hash'}|({'renderer'} if assist else set())):raise ValueError('shadow_snapshot_invalid')
            result={'id':identifier(),'version_id':None,'status':'unavailable','record':None,'applied_to_prompt':False,'policy':SHADOW_VERSION,**({'mode':'assist'} if assist else {})}
            if self.closed:return result|{'status':'closed'}
            if self.future and not self.future.done():return result|{'status':'busy'}
            started=time.monotonic();self.future=self.worker.submit(self._turn,dict(turn),snapshot)
            try:
                value=self.future.result(timeout=max(0,.150-(time.monotonic()-overall)))
                if time.monotonic()-overall>.150:return result|{'status':'timeout'}
                if assist and value['status'] in ('snapshot_invalid','source_invalid'):
                    from .chat import ModelError
                    raise ModelError('assist_snapshot_invalid')
                return value|{'preparation_ms':round((time.monotonic()-overall)*1000,3),**({'mode':'assist'} if assist else {})}
            except FutureTimeout:return result|{'status':'timeout'}
        except Exception as exc:
            if assist:
                from .chat import ModelError
                if isinstance(exc,ModelError):raise
                from .models import RuntimeErrorBase
                if isinstance(exc,RuntimeErrorBase):raise ModelError('assist_source_invalid') from None
            gap('shadow_prepare_failed')
            return {'id':identifier(),'version_id':None,'status':'snapshot_invalid','record':None,'applied_to_prompt':False,'policy':SHADOW_VERSION,**({'mode':'assist'} if assist else {})}

    def validate_assist(self,turn,snapshot):
        from .chat import ModelError
        from .decision_capsule import require_admission,VERSION as RENDERER
        require_admission(self.engine,turn['persona_id'])
        keys={'mode','policy','kernel','compiler','version_id','status','document_hash','renderer'}
        if (not isinstance(snapshot,dict) or set(snapshot)!=keys or snapshot.get('mode')!='assist' or
            snapshot.get('renderer')!=RENDERER or snapshot.get('policy')!=SHADOW_VERSION or
            snapshot.get('kernel')!=VERSION or snapshot.get('compiler')!=COMPILER_VERSION):
            raise ModelError('assist_snapshot_invalid')
        if ((snapshot['version_id'] is not None and (not isinstance(snapshot['version_id'],str) or not isinstance(snapshot['document_hash'],str) or len(snapshot['document_hash'])!=64 or snapshot['status']!='ready')) or (snapshot['version_id'] is None and snapshot['document_hash'] is not None)):
            raise ModelError('assist_snapshot_invalid')
        return snapshot

    def recheck(self,conn,turn,prepared):
        if prepared is None:return None
        if prepared.get('mode')=='assist':
            from .chat import ModelError
            snap=self.validate_assist(turn,json.loads(turn['context_policy_snapshot']).get('disposition'))
            if snap.get('version_id'):
                v=version(conn,turn['persona_id'],snap['version_id'])
                if fingerprint(v['document'])!=snap['document_hash']:raise ModelError('assist_snapshot_invalid')
                if status_for(conn,turn['persona_id'],snap['version_id'],current=False)=='source_invalid':raise ModelError('assist_source_invalid')
        try:
            current=conn.execute('SELECT execution_revision FROM turns WHERE id=?',(turn['id'],)).fetchone()
            if not current or current[0]!=turn['execution_revision']:return prepared|{'status':'execution_changed','record':None}
            if prepared.get('version_id'):
                status=status_for(conn,turn['persona_id'],prepared['version_id'],current=False)
                if status!='ready':return prepared|{'status':status,'record':None}
            return prepared
        except Exception:
            gap('shadow_recheck_failed');return prepared|{'status':'recheck_failed','record':None,'version_id':None}

    def preview(self,persona_id,members,channel='desktop',bot='',peer='',version_id=None,mode='shadow'):
        from .archive import scope
        if mode not in ('shadow','assist'):fail('invalid_disposition_mode')
        if mode=='assist':
            from .decision_capsule import require_admission
            require_admission(self.engine,persona_id)
        selected=scope(persona_id,channel,bot,peer)
        with self.store.connection() as conn:
            DispositionService(self.store)._check(conn,persona_id)
            snapshot=accepted_snapshot(conn,persona_id,'shadow')
            if version_id:
                v=version(conn,persona_id,version_id)
                snapshot.update(version_id=version_id,status=status_for(conn,persona_id,version_id,current=False),document_hash=fingerprint(v['document']))
            if type(members) is not list or not members or len(members)>20 or any(type(x) is not str or not x.strip() for x in members) or sum(map(len,members))>24000:
                fail('invalid_shadow_input')
            result=self._compute(conn,persona_id,selected,[{'id':f'preview-{i}','text':t} for i,t in enumerate(members)],snapshot)|{'persisted':False}
            if mode=='assist':
                from .decision_capsule import capsule,metadata
                result['mode']='assist';text=capsule(result)
                preview_meta=metadata(result,text);preview_meta.update(would_apply_to_prompt=bool(text),applied_to_prompt=False)
                result.update(capsule_preview=text,capsule=preview_meta,assist_admitted=True)
            return result

    def inspect(self,persona_id,turn_id=None):
        with self.store.connection() as conn:
            DispositionService(self.store)._check(conn,persona_id)
            return [dict(r) for r in conn.execute('SELECT * FROM disposition_decisions WHERE persona_id=? AND (? IS NULL OR turn_id=?) ORDER BY rowid DESC LIMIT 50',
                (persona_id,turn_id,turn_id))]


def persist_shadow(conn,turn,manifest,attempt_id):
    prepared=manifest.pop('_shadow_prepared',None)
    if prepared is None:return
    strict=bool(prepared.get('mode')=='assist' and prepared.get('applied_to_prompt'))
    reference={'record_id':prepared['id'],'status':prepared['status'],'version_id':prepared.get('version_id'),
               'policy':SHADOW_VERSION,'applied_to_prompt':strict,'persisted':True}
    if prepared.get('mode')=='assist':reference.update(prepared.get('capsule',{}))
    conn.execute('SAVEPOINT shadow_audit')
    try:
        conn.execute('INSERT INTO disposition_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (prepared['id'],turn['persona_id'],LOCAL_USER_ID,turn['id'],turn['execution_revision'],attempt_id,
             manifest['context_id'],prepared.get('version_id'),prepared['status'],encoded(prepared),timestamp()))
        conn.execute('RELEASE shadow_audit')
    except Exception:
        conn.execute('ROLLBACK TO shadow_audit');conn.execute('RELEASE shadow_audit')
        if strict:
            from .chat import ModelError
            raise ModelError('decision_record_storage_failed') from None
        gap('shadow_record_failed');reference.update(record_id=None,status='record_failed',persisted=False)
    manifest['shadow']=reference
