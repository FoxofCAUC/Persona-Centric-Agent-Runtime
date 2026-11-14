"""M8.A contracts for the existing OrganizationEngine, not a second executor."""
import asyncio
import hashlib
import json
import time

from .models import LOCAL_USER_ID,RuntimeErrorBase,identifier,timestamp
from .organization import encoded,digest
from .private_personas import provider_identity,guard_cloud_dispatch
from .instance import current_process_identity,process_identity_alive

VERSION='organization-contract-v1'
TRIGGERS=('manual','daily','incremental','sealed','probe','scheduled')
WAITING=('queued','waiting_quota')


class ManagedOrganization:
    def __init__(self,engine):
        self.engine=engine;self.store=engine.store;self.owner=identifier()
        self.process=current_process_identity();self.active_task=None
        self.clock=time.time

    def policy(self,db,pid,create=False):
        row=db.execute('SELECT * FROM organization_policies WHERE persona_id=? ORDER BY revision DESC LIMIT 1',(pid,)).fetchone()
        if row:
            result=dict(row);result['data']=json.loads(result.pop('data_json'));return result
        cfg=self.engine.config.load()
        data={'version':1,'enabled':True,'triggers':list(TRIGGERS),'channels':['desktop','qq'],
              'qq':{'bot':cfg['bot_qq'],'peer':cfg['owner_qq']},
              'provider':provider_identity(self.engine.model())}
        if not create:return {'id':None,'revision':0,'persona_id':pid,'data':data}
        ident=identifier()
        db.execute('INSERT INTO organization_policies VALUES(?,?,?,?,?,?,?)',
                   (ident,pid,1,encoded(data),'initial-'+ident,digest(data),timestamp()))
        return self.policy(db,pid)

    def current_policy(self,persona_id=None):
        with self.store.connection() as db:
            pid=persona_id or self.store.current(db)
            from .persona_management import active
            active(db,pid)
            return self.policy(db,pid)

    def update_policy(self,persona_id,data,expected_version,expected_selected,operation_id):
        if not isinstance(operation_id,str) or not operation_id:raise RuntimeErrorBase('operation_id_required')
        if (not isinstance(data,dict) or set(data)-{'memory_automation','memory_backfill'}!={'version','enabled','triggers','channels','qq','provider'}
            or data['version']!=1 or type(data['enabled']) is not bool
            or 'memory_automation' in data and type(data['memory_automation']) is not bool
            or 'memory_backfill' in data and type(data['memory_backfill']) is not bool
            or not isinstance(data['triggers'],list) or any(t not in TRIGGERS for t in data['triggers'])
            or not isinstance(data['channels'],list) or any(c not in ('desktop','qq') for c in data['channels'])
            or not isinstance(data['qq'],dict) or set(data['qq'])!={'bot','peer'}
            or any(not isinstance(v,str) for v in data['qq'].values())):
            raise RuntimeErrorBase('organization_policy_invalid')
        fp=digest([persona_id,data,expected_version,expected_selected])
        with self.store.connection(write=True) as db:
            old=db.execute('SELECT * FROM organization_policies WHERE operation_id=?',(operation_id,)).fetchone()
            if old:
                if old['request_hash']!=fp:raise RuntimeErrorBase('submission_conflict')
                return dict(old)|{'data':json.loads(old['data_json'])}
            if self.store.current(db)!=expected_selected or expected_selected!=persona_id:
                raise RuntimeErrorBase('persona_selection_conflict')
            from .persona_management import active
            active(db,persona_id)
            current=self.policy(db,persona_id)
            if current['id']!=expected_version:raise RuntimeErrorBase('version_conflict')
            if data['provider']!=provider_identity(self.engine.model()):raise RuntimeErrorBase('organization_model_changed')
            data=dict(data)
            if 'memory_automation' not in data and 'memory_automation' in current['data']:
                data['memory_automation']=current['data']['memory_automation']
            if 'memory_backfill' not in data and 'memory_backfill' in current['data']:
                data['memory_backfill']=current['data']['memory_backfill']
            db.execute('INSERT INTO organization_policies VALUES(?,?,?,?,?,?,?)',
                (identifier(),persona_id,current['revision']+1,encoded(data),operation_id,fp,timestamp()))
            # Pending work must be explicitly rebound to a new policy, not silently enlarged.
            db.execute("UPDATE organization_tasks SET status='blocked_authorization',error_code='organization_policy_changed' WHERE persona_id=? AND status IN ('queued','waiting_quota')",(persona_id,))
            return self.policy(db,persona_id)

    def scope(self,db,payload):
        if payload.get('scope'):return payload['scope']
        result={k:payload.get(k) for k in ('persona_id','user_id','channel')}
        result.update(bot='',peer='')
        if payload.get('channel')=='qq' and payload.get('rows'):
            origins=[json.loads(r[0]) for r in db.execute('SELECT origin_json FROM turns WHERE session_id=? AND rowid IN ('+','.join('?' for _ in payload['rows'])+')',(payload['session_id'],*payload['rows']))]
            pairs={(str(o.get('bot','')),str(o.get('sender',''))) for o in origins}
            if len(pairs)!=1:raise RuntimeErrorBase('organization_scope_mismatch')
            result['bot'],result['peer']=next(iter(pairs))
        return result

    def key(self,db,payload,request_id,trigger):
        if payload.get('knowledge_processing'):return digest(['knowledge_processing',payload['scope'],payload['knowledge_processing']])
        if payload.get('external_knowledge'):
            knowledge=payload['external_knowledge']
            return digest(['public_knowledge',payload['scope'],knowledge['batch_id'],knowledge['stage'],
                           knowledge['document_sha256'],knowledge.get('proposals')])
        if payload.get('proactive'):return digest(['scheduled',payload['scope'],payload['proactive']['occurrence_id']])
        # Event IDs/text revisions normalize legacy and sealed desktop wrappers.
        evidence=sorted((e.get('kind','event'),e['id'],digest(e['text'])) for e in payload.get('events',[]))
        if payload['mode']=='test':evidence=[('probe',request_id,'')]
        rebuild=self.rebuild_root(db,payload,request_id,trigger)
        automatic=payload.get('automatic_memory')
        purpose=[automatic['purpose'],automatic['stage'],automatic.get('previous_fragment'),digest(payload.get('proposals')),payload.get('memory_dependency_hash')] if automatic else None
        return digest([self.scope(db,payload),payload.get('session_id'),payload['mode'],evidence,
                       payload.get('existing_memories',[]),rebuild,purpose] if automatic else
                      [self.scope(db,payload),payload.get('session_id'),payload['mode'],evidence,payload.get('existing_memories',[]),rebuild])

    def rebuild_root(self,db,payload,request_id,trigger):
        if payload.get('rebuild') and trigger in ('manual','sealed'):return request_id
        previous=payload.get('previous') or {}
        c=self.contract(db,previous.get('task_id')) if previous.get('task_id') else None
        return c['data'].get('rebuild_root') if c else None

    def lookup(self,db,payload,request_id,trigger):
        old=db.execute('SELECT * FROM organization_submissions WHERE request_id=?',(request_id,)).fetchone()
        fp=digest([payload,trigger])
        if old:
            if old['request_hash']!=fp:raise RuntimeErrorBase('submission_conflict')
            return dict(db.execute('SELECT * FROM organization_tasks WHERE id=?',(old['task_id'],)).fetchone())
        key=self.key(db,payload,request_id,trigger)
        old=db.execute("SELECT t.* FROM organization_contracts c JOIN organization_tasks t ON t.id=c.task_id WHERE c.work_key=? AND (t.status NOT IN ('stale','cancelled') OR EXISTS(SELECT 1 FROM organization_attempts a WHERE a.task_id=t.id)) ORDER BY t.rowid LIMIT 1",(key,)).fetchone()
        if old:
            db.execute('INSERT INTO organization_submissions VALUES(?,?,?,?,?)',(request_id,old['id'],fp,trigger,timestamp()))
            return dict(old)

    def attach(self,db,task,payload,trigger,legacy=False):
        policy=self.policy(db,task['persona_id'],create=True)
        meta={'version':VERSION,'trigger':trigger,'created_epoch':self.clock(),'next_epoch':0,
              'phase':'legacy' if legacy else 'ready','process':None,'cancel_requested':False,
              'rebuild_root':self.rebuild_root(db,payload,task['request_id'],trigger)}
        db.execute('INSERT OR IGNORE INTO organization_contracts VALUES(?,?,?,?,?)',
            (task['id'],self.key(db,payload,task['request_id'],trigger),encoded(self.scope(db,payload)),policy['id'],encoded(meta)))
        db.execute('INSERT OR IGNORE INTO organization_submissions VALUES(?,?,?,?,?)',
            (task['request_id'],task['id'],digest([payload,trigger]),trigger,timestamp()))

    def register(self,db,payload,request_id,automatic=0,trigger=None):
        trigger=trigger or {0:'manual',1:'incremental',2:'daily'}.get(automatic)
        if trigger not in TRIGGERS:raise RuntimeErrorBase('organization_task_type_not_supported')
        old=self.lookup(db,payload,request_id,trigger)
        if old:return self.engine._view(old)
        if db.execute("SELECT count(*) FROM organization_tasks WHERE status IN ('queued','waiting_quota')").fetchone()[0]>=10:
            raise RuntimeErrorBase('organization_queue_full')
        ident=identifier();now=timestamp()
        model=self.engine.model()
        if payload.get('knowledge_processing',{}).get('policy',{}).get('data',{}).get('model_source')=='main' or payload.get('external_knowledge',{}).get('model_source')=='main':model=dict(self.engine.config.load()['model'])
        if payload.get('knowledge_processing',{}).get('stage') in ('conversation_web_decision',):model=self.engine.routing.model()
        if payload.get('knowledge_processing'):
            from .knowledge_general import processing_model
            model=processing_model(model,payload['knowledge_processing'])
        if payload.get('proactive'):
            from .companion import choose,GENERATION_PROFILE
            model=self.engine.config.load()['model']|{'_companion_profile':choose('minimal_im'),'_generation_profile':GENERATION_PROFILE}
        db.execute('INSERT INTO organization_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (ident,request_id,payload['persona_id'],LOCAL_USER_ID,payload.get('session_id'),payload.get('channel'),payload['mode'],int(automatic),
             payload['fingerprint'],encoded(payload),encoded(model),'queued',None,now,now))
        task=dict(db.execute('SELECT * FROM organization_tasks WHERE id=?',(ident,)).fetchone())
        self.attach(db,task,payload,trigger)
        return self.engine._view(task)

    def rebase(self,db,task,c,payload):
        if payload.get('knowledge_processing'):return payload
        if payload.get('external_knowledge'):return payload
        if payload.get('proactive'):return payload
        """Serialize overlapping accepted work and trim only already completed sources."""
        if payload.get('automatic_memory'):return payload
        if task['mode']=='test' or c['data'].get('rebuild_root') or db.execute('SELECT 1 FROM organization_attempts WHERE task_id=?',(task['id'],)).fetchone():return payload
        covered=set();providers=[]
        for row in db.execute("SELECT * FROM organization_tasks WHERE session_id=? AND id!=? AND status='completed' AND mode IN (?, 'both')",(task['session_id'],task['id'],task['mode'])):
            p=json.loads(row['payload_json'])
            if self.scope(db,p)!=c['scope']:continue
            covered.update((e.get('kind','event'),e['id'],digest(e['text'])) for e in p.get('events',[]));providers.append(row['id'])
        if not covered:return payload
        groups={}
        for e in payload.get('events',[]):groups.setdefault(e['turn'],[]).append(e)
        remaining=[n for n,events in groups.items() if not all((e.get('kind','event'),e['id'],digest(e['text'])) in covered for e in events)]
        if len(remaining)==len(groups):return payload
        # Withdrawn/changed sources are not permission to build a new input.
        from .session_organization import valid
        if not valid(db,payload):raise RuntimeErrorBase('source_expired')
        from .organization import eligible_turns
        if payload.get('source_format')!=2:
            valid_rows={r['ordinal'] for r in eligible_turns(db,task['session_id'])}
            if not set(payload['rows'])<=valid_rows:raise RuntimeErrorBase('source_expired')
        c['data']['reused_tasks']=providers
        if not remaining:
            c['data']['phase']='reused';self.save(db,c)
            db.execute("UPDATE organization_tasks SET status='completed',error_code='existing_work_reused' WHERE id=?",(task['id'],))
            return None
        if payload.get('source_format')==2:
            from .session_organization import SessionOrganization
            fresh=SessionOrganization(self.engine)._preview(db,task['session_id'],c['scope'],task['mode'],False)
            allowed=set(remaining)
            fresh['rows']=[r for r in fresh['rows'] if r in allowed]
            fresh['events']=[e for e in fresh['events'] if e['turn'] in allowed]
            ids={e['id'] for e in fresh['events']}
            fresh['source_refs']=[r for r in fresh['source_refs'] if r['id'] in ids]
        else:
            fresh=self.engine._preview(db,task['session_id'],task['mode'],min(remaining),max(remaining),task['persona_id'])
            allowed=set(remaining)
            fresh['rows']=[r for r in fresh['rows'] if r in allowed]
            fresh['events']=[e for e in fresh['events'] if e['turn'] in allowed]
        fresh['fingerprint']=digest({k:v for k,v in fresh.items() if k not in ('fingerprint','remaining_turns','estimated_batches','excluded_events')})
        if not fresh['rows']:raise RuntimeErrorBase('source_expired')
        c['data'].setdefault('original_payload_hash',digest(payload))
        c['data']['revalidated_payload_hash']=digest(fresh);self.save(db,c)
        db.execute('UPDATE organization_tasks SET payload_json=?,fingerprint=? WHERE id=?',(encoded(fresh),fresh['fingerprint'],task['id']))
        return fresh

    def contract(self,db,tid):
        row=db.execute('SELECT * FROM organization_contracts WHERE task_id=?',(tid,)).fetchone()
        if not row:return None
        result=dict(row);result['data']=json.loads(result.pop('data_json'));result['scope']=json.loads(result.pop('scope_json'));return result

    def save(self,db,contract):
        db.execute('UPDATE organization_contracts SET data_json=?,policy_id=? WHERE task_id=?',
                   (encoded(contract['data']),contract['policy_id'],contract['task_id']))

    def check(self,db,task,contract,payload,check_sources=True):
        if contract['data']['cancel_requested']:raise RuntimeErrorBase('cancelled_by_user')
        if payload.get('proactive'):
            self.engine.proactive.check(db,task,payload)
            return
        from .persona_management import active
        from .session_organization import valid
        active(db,task['persona_id'])
        policy=self.policy(db,task['persona_id']);p=policy['data']
        if not p['enabled'] or contract['policy_id']!=policy['id'] or contract['data']['trigger'] not in p['triggers']:
            raise RuntimeErrorBase('organization_policy_changed')
        model=json.loads(task['model_snapshot'])
        main_source=payload.get('knowledge_processing',{}).get('policy',{}).get('data',{}).get('model_source')=='main' or payload.get('external_knowledge',{}).get('model_source')=='main'
        routing=payload.get('knowledge_processing',{}).get('stage') in ('conversation_web_decision',)
        if routing:
            from .knowledge_processing import model_contract
            if model_contract(model)!=model_contract(self.engine.routing.model()):raise RuntimeErrorBase('organization_model_changed')
        elif main_source:
            from .knowledge_processing import model_contract
            from .knowledge_general import processing_model
            expected=processing_model(self.engine.config.load()['model'],payload.get('knowledge_processing',{}))
            if model_contract(model)!=model_contract(expected):raise RuntimeErrorBase('organization_model_changed')
        elif p['provider']!=provider_identity(model) or p['provider']!=provider_identity(self.engine.model()):
            raise RuntimeErrorBase('organization_model_changed')
        if payload.get('knowledge_processing'):
            selected=self.scope(db,payload)
            if selected!=contract['scope'] or selected['channel'] not in p['channels']:
                raise RuntimeErrorBase('organization_scope_mismatch')
            if selected['channel']=='qq' and (selected['bot'],selected['peer'])!=(p['qq']['bot'],p['qq']['peer']):
                raise RuntimeErrorBase('organization_scope_mismatch')
            guard_cloud_dispatch(self.store,task['persona_id'],'knowledge_router' if routing else 'main' if main_source else 'organization',model)
            if not model['base_url'] or not model['name']:raise RuntimeErrorBase('organization_model_not_configured')
            self.engine.processing.check(db,task,contract,payload)
            return
        if payload.get('external_knowledge'):
            guard_cloud_dispatch(self.store,task['persona_id'],'main' if main_source else 'organization',model)
            if not model['base_url'] or not model['name']:raise RuntimeErrorBase('organization_model_not_configured')
            self.engine.knowledge.check(db,task,contract,payload)
            return
        selected=self.scope(db,payload)
        if selected!=contract['scope'] or selected['channel'] not in p['channels']:
            raise RuntimeErrorBase('organization_scope_mismatch')
        if selected['channel']=='qq' and (selected['bot'],selected['peer'])!=(p['qq']['bot'],p['qq']['peer']):
            raise RuntimeErrorBase('organization_scope_mismatch')
        if task['mode']!='test':
            guard_cloud_dispatch(self.store,task['persona_id'],'organization',model)
            if payload.get('automatic_memory'):
                if not p.get('memory_automation',False):raise RuntimeErrorBase('organization_policy_changed')
                if check_sources:
                    from .automatic_memory import validate_sources
                    validate_sources(db,payload,check_memories=payload['automatic_memory']['stage']=='verify')
            elif check_sources and (payload['guard']!=self.engine.guard(db,task['persona_id'],task['session_id']) or not valid(db,payload)):
                raise RuntimeErrorBase('source_expired')
        if not model['base_url'] or not model['name']:raise RuntimeErrorBase('organization_model_not_configured')
        if contract['data']['cancel_requested']:raise RuntimeErrorBase('cancelled_by_user')

    def inspect(self,db,tid):
        c=self.contract(db,tid)
        if not c:
            task=db.execute('SELECT status,payload_json FROM organization_tasks WHERE id=?',(tid,)).fetchone()
            if task and task['status']=='completed' and json.loads(task['payload_json']).get('private_data_erased') is True:
                count=db.execute('SELECT count(*) FROM organization_attempts WHERE task_id=?',(tid,)).fetchone()[0]
                return {'version':VERSION,'phase':'erased','recovery':'already_committed',
                        'source_data_erased':True,'physical_calls':count}
            return {'version':VERSION,'legacy':True,'recovery':'requires_legacy_audit'}
        count=db.execute('SELECT count(*) FROM organization_attempts WHERE task_id=?',(tid,)).fetchone()[0]
        slot=dict(db.execute("SELECT * FROM organization_execution WHERE id='singleton'").fetchone())
        return {'version':VERSION,'policy_id':c['policy_id'],'trigger':c['data']['trigger'],
            'phase':c['data']['phase'],'next_epoch':c['data']['next_epoch'],'physical_calls':count,
            'cancel_requested':c['data']['cancel_requested'],
            'last_claim':c['data'].get('last_claim'),
            'recovery':('reuse_saved_receipt' if c['data']['phase']=='received' else
                        'audit_required_no_replay' if c['data']['phase'] in ('unknown','legacy') else
                        'already_committed' if c['data']['phase'] in ('committed','reused') else 'recheck_before_dispatch'),
            'lease':{k:slot[k] for k in ('generation','lease_until')} if slot['task_id']==tid else None}

    def recover(self):
        with self.store.connection(write=True) as db:
            for row in db.execute('SELECT * FROM organization_tasks WHERE id NOT IN (SELECT task_id FROM organization_contracts)').fetchall():
                task=dict(row);p=json.loads(task['payload_json'])
                if task['status']=='completed' and p.get('private_data_erased') is True:
                    continue
                if task['mode']=='test':p.update(persona_id=task['persona_id'],user_id=LOCAL_USER_ID,channel='desktop',mode='test',fingerprint=task['fingerprint'])
                self.attach(db,task,p,'probe' if task['mode']=='test' else {0:'manual',1:'incremental',2:'daily'}.get(task['automatic'],'manual'),legacy=True)
            slot=dict(db.execute("SELECT * FROM organization_execution WHERE id='singleton'").fetchone())
            if slot['task_id']:
                c=self.contract(db,slot['task_id'])
                # Expiry is not proof a live owner's transport has stopped.
                if process_identity_alive(c['data'].get('process')):return
            for row in db.execute("SELECT * FROM organization_tasks WHERE status IN ('queued','waiting_quota','running')").fetchall():
                task=dict(row);c=self.contract(db,task['id']);data=c['data']
                attempt=db.execute('SELECT * FROM organization_attempts WHERE task_id=? ORDER BY rowid DESC LIMIT 1',(task['id'],)).fetchone()
                received=db.execute('SELECT 1 FROM organization_receipts WHERE attempt_id=?',(attempt['id'],)).fetchone() if attempt else None
                if received:
                    data['phase']='received';state='queued';error=None
                elif attempt and (attempt['status'] in ('running','interrupted') or data['phase'] in ('dispatching','unknown','legacy')):
                    data['phase']='unknown';state='blocked_unknown';error='organization_effect_unknown'
                    db.execute("UPDATE organization_attempts SET status='interrupted',error_code=coalesce(error_code,'service_restarted') WHERE id=? AND status='running'",(attempt['id'],))
                elif data['phase']=='legacy' and (attempt or task['status']=='running'):
                    data['phase']='unknown'
                    state='blocked_unknown';error='organization_legacy_requires_audit'
                else:
                    state='waiting_quota' if task['status']=='waiting_quota' else 'queued';error=None
                    if data['phase']=='legacy':data['phase']='ready'
                db.execute('UPDATE organization_tasks SET status=?,error_code=? WHERE id=?',(state,error,task['id']));self.save(db,c)
            db.execute("UPDATE organization_reservations SET state='released' WHERE state='active'")
            db.execute("UPDATE organization_execution SET task_id=NULL,owner=NULL,lease_until=0 WHERE id='singleton'")

    def cancel(self,tid):
        from .service import RuntimeService
        with self.store.connection(write=True) as db:
            task=RuntimeService._owned(db,'organization_tasks',tid,self.store.current(db))
            if task['status']=='completed':raise RuntimeErrorBase('task_already_completed')
            c=self.contract(db,tid)
            if c:c['data']['cancel_requested']=True;self.save(db,c)
            db.execute("UPDATE organization_tasks SET status='cancelled',error_code='cancelled_by_user',updated_at=? WHERE id=?",(timestamp(),tid))
        if self.active_task and self.active_task[0]==tid:
            running=self.active_task[1]
            running.get_loop().call_soon_threadsafe(running.cancel,'cancelled_by_user')
        return self.engine.show(tid)

    def retry(self,tid):
        from .migration import ensure_cloud_ready
        ensure_cloud_ready(self.store.paths)
        from .service import RuntimeService
        with self.store.connection(write=True) as db:
            task=dict(RuntimeService._owned(db,'organization_tasks',tid,self.store.current(db)))
            if task.get('error_code')=='migration_requires_review':raise RuntimeErrorBase('迁移前任务保持暂停，请核对已有回执后发起新的请求。')
            if task['status']=='completed':return self.engine._view(task)
            c=self.contract(db,tid)
            if not c:raise RuntimeErrorBase('organization_legacy_requires_audit')
            if c['data']['phase'] in ('unknown','dispatching','legacy') or task['status']=='blocked_unknown':
                raise RuntimeErrorBase('organization_effect_unknown')
            count=db.execute('SELECT count(*) FROM organization_attempts WHERE task_id=?',(tid,)).fetchone()[0]
            if count and c['data']['phase']!='received':raise RuntimeErrorBase('organization_new_position_required')
            if task['status'] in ('running','queued','waiting_quota'):raise RuntimeErrorBase('task_still_active')
            c['policy_id']=self.policy(db,task['persona_id'],create=True)['id'];c['data']['cancel_requested']=False
            self.check(db,task,c,json.loads(task['payload_json']))
            db.execute("UPDATE organization_tasks SET status='queued',error_code=NULL WHERE id=?",(tid,));self.save(db,c)
        return self.engine.show(tid)

    async def heartbeat(self,tid,generation):
        while True:
            await asyncio.sleep(15)
            with self.store.connection(write=True) as db:
                db.execute("UPDATE organization_execution SET lease_until=? WHERE id='singleton' AND task_id=? AND owner=? AND generation=?",(self.clock()+60,tid,self.owner,generation))

    def owns(self,db,tid,generation):
        slot=db.execute("SELECT * FROM organization_execution WHERE id='singleton'").fetchone()
        return slot['task_id']==tid and slot['owner']==self.owner and slot['generation']==generation and slot['lease_until']>self.clock()

    async def run_next(self):
        e=self.engine
        from .migration import migration_pending,background_allowed
        if migration_pending(self.store.paths):return False
        if e.running or e.draining or e.chat.draining:return False
        from .maintenance import require_writable_storage
        try:require_writable_storage(self.store.paths)
        except RuntimeErrorBase:return False
        self.recover()
        task=None;aid=None;generation=None;received=None
        with self.store.connection(write=True) as db:
            foreground = bool(db.execute("SELECT 1 FROM turns WHERE status IN ('queued','running') LIMIT 1").fetchone()
                or db.execute("SELECT 1 FROM input_batches WHERE state IN ('collecting','sealed_waiting_capacity') LIMIT 1").fetchone())
            from .knowledge_wait import required_tasks
            required = required_tasks(db) if foreground else set()
            if foreground and (not required or db.execute("SELECT 1 FROM turns WHERE status='running' LIMIT 1").fetchone()):return False
            slot=db.execute("SELECT * FROM organization_execution WHERE id='singleton'").fetchone()
            if slot['task_id']:return False
            choices=[]
            for row in db.execute("SELECT * FROM organization_tasks WHERE status IN ('queued','waiting_quota') ORDER BY rowid"):
                if foreground and row['id'] not in required:continue
                t=dict(row);c=self.contract(db,t['id']);m=c['data']
                cfg=e.config.load();quota_signature=digest([cfg['model']['hourly_limit'],cfg['organization']['hourly_limit'],cfg['organization']['daily_limit']])
                from .generation_retry import automatic_organization
                if not background_allowed(self.store.paths) and automatic_organization(t):continue
                if t['status']=='waiting_quota' and not automatic_organization(t):m['next_epoch']=0
                if t['status']=='waiting_quota' and m.get('quota_signature')!=quota_signature:m['next_epoch']=0
                if m['next_epoch']>self.clock():continue
                priority=0 if m['trigger'] in ('manual','sealed','probe') or self.clock()-m['created_epoch']>=600 else 1
                p=json.loads(t['payload_json']);a=p.get('automatic_memory') or p.get('external_knowledge',{})
                subpriority=0 if a.get('stage') in ('verify','review') else 1 if a.get('urgency')=='correction' else 2 if a.get('purpose')=='repair' else 4 if a.get('urgency')=='backfill' else 3
                choices.append((priority,m['created_epoch'] if priority==0 else 0,subpriority if priority else 0,m['created_epoch'],t,c))
            if not choices:return False
            *_,task,c=min(choices,key=lambda x:x[:4]);payload=json.loads(task['payload_json'])
            try:
                self.check(db,task,c,payload,check_sources=False)
                payload=self.rebase(db,task,c,payload)
                if payload is None:return True
                self.check(db,task,c,payload)
            except RuntimeErrorBase as error:
                state='stale' if str(error)=='source_expired' else 'blocked_authorization'
                db.execute('UPDATE organization_tasks SET status=?,error_code=? WHERE id=?',(state,str(error),task['id']));return True
            last=db.execute('SELECT * FROM organization_attempts WHERE task_id=? ORDER BY rowid DESC LIMIT 1',(task['id'],)).fetchone()
            if c['data']['phase']=='received' and last:
                received=db.execute('SELECT * FROM organization_receipts WHERE attempt_id=?',(last['id'],)).fetchone();aid=last['id']
                if not received:raise RuntimeErrorBase('organization_receipt_missing')
            else:
                from .quotas import reserve_organization
                reservation,next_epoch=reserve_organization(db,e.config,task,self.clock())
                if not reservation:
                    c['data']['next_epoch']=next_epoch;c['data']['quota_signature']=quota_signature;self.save(db,c)
                    db.execute("UPDATE organization_tasks SET status='waiting_quota' WHERE id=?",(task['id'],));return False
                count=db.execute('SELECT count(*) FROM organization_attempts WHERE task_id=?',(task['id'],)).fetchone()[0]
                if count>=4:
                    db.execute("UPDATE organization_reservations SET state='released' WHERE id=?",(reservation,))
                    db.execute("UPDATE organization_tasks SET status='failed',error_code='provider_retries_exhausted' WHERE id=?",(task['id'],));return True
                model=json.loads(task['model_snapshot'])
                messages=[{'role':'user','content':'Reply with OK.'}] if task['mode']=='test' else e.messages(payload)
                aid=identifier()
                db.execute('SAVEPOINT organization_manifest')
                db.execute("INSERT INTO organization_attempts VALUES(?,?,?,'running',NULL,NULL,?)",(aid,task['id'],self.clock(),task['model_snapshot']))
                try:e.chat.cognitive.reflection(db,task,payload,messages,aid)
                except Exception:
                    db.execute('ROLLBACK TO organization_manifest');db.execute('RELEASE organization_manifest')
                    db.execute("UPDATE organization_reservations SET state='released' WHERE id=?",(reservation,))
                    db.execute("UPDATE organization_tasks SET status='failed',error_code='context_manifest_storage_failed' WHERE id=?",(task['id'],))
                    return True
                db.execute('RELEASE organization_manifest')
                db.execute("UPDATE organization_reservations SET state='consumed',attempt_id=? WHERE id=?",(aid,reservation))
                c['data']['phase']='dispatching'
            generation=slot['generation']+1;c['data']['process']=self.process
            c['data']['last_claim']={'generation':generation,'owner':self.owner,'claimed_epoch':self.clock()}
            db.execute("UPDATE organization_execution SET task_id=?,owner=?,generation=?,lease_until=? WHERE id='singleton'",(task['id'],self.owner,generation,self.clock()+60))
            db.execute("UPDATE organization_tasks SET status='running',error_code=NULL,updated_at=? WHERE id=?",(timestamp(),task['id']));self.save(db,c)
        e.running=True;heart=asyncio.create_task(self.heartbeat(task['id'],generation));self.active_task=(task['id'],asyncio.current_task())
        try:
            from .organization_output import receipt
            if received:
                reply=received['response_text'];audit=json.loads(received['diagnostic_json'])
                if audit.get('response_sha256')!=hashlib.sha256(reply.encode('utf-8')).hexdigest():
                    raise RuntimeErrorBase('organization_receipt_corrupt')
            else:
                from .generation_retry import _one
                if payload.get('knowledge_processing',{}).get('stage') in ('conversation_web_decision',):
                    value=await e.routing.execute(model,payload)
                elif payload.get('knowledge_processing',{}).get('stage') in ('conversation_search','conversation_fetch'):
                    value=await e.processing.general.execute(model,payload)
                else:
                    from .knowledge_general import reasoning_model
                    request_model=reasoning_model(model,plan=payload.get('knowledge_processing',{}).get('stage') in ('conversation_search_plan','conversation_organize'))
                    value=await _one(e.client,request_model|{'_json_output':task['mode'] not in ('test','proactive')},messages,'provider_timeout',paths=self.store.paths)
                reply,usage=value;audit=receipt(reply);audit['status']='received'
                from .generation_retry import safe_diagnostics
                audit['provider']=safe_diagnostics(getattr(value,'diagnostics',{}))
                with self.store.connection(write=True) as db:
                    db.execute('UPDATE organization_attempts SET usage_json=? WHERE id=?',(encoded(usage),aid))
                    db.execute('INSERT INTO organization_receipts VALUES(?,?,?)',(aid,reply,encoded(audit)))
                    if self.owns(db,task['id'],generation):
                        c=self.contract(db,task['id']);c['data']['phase']='received';self.save(db,c)
            # A durable receipt is the recovery boundary. Publication remains the existing transaction.
            with self.store.connection(write=True) as db:
                if not self.owns(db,task['id'],generation):raise RuntimeErrorBase('organization_lease_lost')
                c=self.contract(db,task['id']);self.check(db,task,c,payload)
                if payload.get('proactive'):
                    e.proactive.publish(db,task,payload,reply)
                elif payload.get('knowledge_processing'):
                    e.processing.published(db,task,payload,reply,audit)
                elif payload.get('external_knowledge'):
                    e.knowledge.published(db,task,payload,reply,audit)
                elif task['mode']!='test':
                    summary,candidates=e.validate_output(db,payload,reply,audit)
                    e._publish(db,task,payload,summary,candidates)
                if audit.get('error_code'):
                    audit.setdefault('previous_validation',[]).append({'error_code':audit.pop('error_code'),'error':audit.pop('error',{})})
                audit['status']='published'
                db.execute('UPDATE organization_receipts SET diagnostic_json=? WHERE attempt_id=?',(encoded(audit),aid))
                db.execute("UPDATE organization_attempts SET status='completed',error_code=NULL WHERE id=?",(aid,))
                db.execute("UPDATE organization_tasks SET status='completed',updated_at=? WHERE id=?",(timestamp(),task['id']))
                c['data']['phase']='committed';self.save(db,c)
        except BaseException as error:
            from .generation_retry import safe_diagnostics
            from .provider_compat import usage_fields
            code=str(error) if isinstance(error,RuntimeErrorBase) else 'organization_failed'
            cancelled=isinstance(error,asyncio.CancelledError)
            diagnostics=safe_diagnostics(getattr(error,'diagnostics',None))
            tool=payload.get('knowledge_processing',{}).get('stage') in ('conversation_search','conversation_fetch')
            routing=payload.get('knowledge_processing',{}).get('stage') in ('conversation_web_decision',)
            provider_completed=(not tool and not routing and diagnostics.get('response_headers_received') is True
                and diagnostics.get('finish_reason') in ('length','content_filter','tool_calls','insufficient_system_resource','aborted'))
            usage=getattr(error,'usage',None) if tool else usage_fields(getattr(error,'usage',None),deepseek=True,grok=True)
            with self.store.connection(write=True) as db:
                c=self.contract(db,task['id']);count=db.execute('SELECT count(*) FROM organization_attempts WHERE task_id=?',(task['id'],)).fetchone()[0]
                has_receipt=db.execute('SELECT 1 FROM organization_receipts WHERE attempt_id=?',(aid,)).fetchone()
                denied_tool=tool and code=='web_target_not_public'
                source_read=payload.get('knowledge_processing',{}).get('stage') in ('wiki_search','wiki_fetch')
                permanent_decision=routing and code=='jev_credential_unavailable'
                known_tool=(tool or routing) and (diagnostics.get('http_status') is not None or denied_tool or source_read or permanent_decision)
                if routing and hasattr(error,'native_response'):
                    db.execute('INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO NOTHING',('knowledge.routing.http_receipt.'+aid,encoded({'revision':0,'data':{'body':error.native_response,'status':diagnostics.get('http_status')}})))
                if tool and hasattr(error,'tool_response'):
                    db.execute('INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO NOTHING',('knowledge.web.http_receipt.'+aid,encoded({'revision':0,'data':{'body':error.tool_response,'status':diagnostics.get('http_status')}})))
                safe_retry=(not has_receipt and not cancelled and not denied_tool and not permanent_decision and (source_read and code=='wiki_transport_unavailable' or (tool or routing) and diagnostics.get('http_status') in (408,429,500,502,503,504) or diagnostics.get('request_headers_sent') is False or
                    code=='provider_http_429' and diagnostics.get('http_status')==429))
                unknown=not has_receipt and not safe_retry and not known_tool and not provider_completed
                if provider_completed:c['data']['last_failure']={'code':code,'provider_completed':True,'provider':diagnostics}
                state='cancelled' if c['data']['cancel_requested'] else ('blocked_unknown' if unknown else 'failed')
                if safe_retry and count<4 and not c['data']['cancel_requested']:
                    state='queued';c['data']['next_epoch']=self.clock()+(1,2,4)[count-1]
                c['data']['phase']='received' if has_receipt else ('ready' if safe_retry else 'failed_known' if known_tool or provider_completed else 'unknown')
                if code=='source_expired':state='stale'
                if has_receipt and code in ('organization_policy_changed','private_cloud_provider_not_authorized','organization_model_changed'):state='blocked_authorization'
                if self.owns(db,task['id'],generation):
                    db.execute('UPDATE organization_tasks SET status=?,error_code=?,updated_at=? WHERE id=?',(state,'cancelled_by_user' if c['data']['cancel_requested'] else code,timestamp(),task['id']))
                    self.save(db,c)
                db.execute("UPDATE organization_attempts SET status=?,error_code=?,usage_json=coalesce(usage_json,?) WHERE id=? AND status!='completed'",('interrupted' if unknown else 'failed',code,encoded(usage) if usage is not None else None,aid))
                if has_receipt:
                    audit.update(status=state,error_code=code,error=getattr(error,'diagnostic',{}))
                    db.execute('UPDATE organization_receipts SET diagnostic_json=? WHERE attempt_id=?',(encoded(audit),aid))
            if cancelled and not c['data']['cancel_requested']:raise
        finally:
            heart.cancel()
            try:await heart
            except asyncio.CancelledError:pass
            with self.store.connection(write=True) as db:
                db.execute("UPDATE organization_execution SET task_id=NULL,owner=NULL,lease_until=0 WHERE id='singleton' AND owner=? AND generation=?",(self.owner,generation))
            self.active_task=None;e.running=False
        return True
