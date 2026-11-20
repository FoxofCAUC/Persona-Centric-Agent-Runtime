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
LANES={'background':'organization_execution','foreground':'organization_foreground_execution'}


def chat_root(db,payload):
    root=payload.get('knowledge_processing',{}).get('root_turn_id')
    if root:
        return db.execute("""SELECT id,execution_revision FROM turns WHERE id=? AND persona_id=? AND session_id=?
            AND channel=? AND status IN ('queued','running') AND cancelled_at IS NULL""",
            (root,payload['persona_id'],payload.get('session_id'),payload.get('channel'))).fetchone()


class ManagedOrganization:
    def __init__(self,engine):
        self.engine=engine;self.store=engine.store;self.owner=identifier()
        self.process=current_process_identity();self.active_tasks={}
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
        waiting=db.execute("""SELECT count(*),coalesce(sum(coalesce(json_extract(c.data_json,'$.foreground'),0)=0),0)
            FROM organization_tasks t LEFT JOIN organization_contracts c ON c.task_id=t.id
            WHERE t.status IN ('queued','waiting_quota')""").fetchone()
        foreground=bool(chat_root(db,payload))
        if waiting[0]>=10 or (not foreground and waiting[1]>=8):
            raise RuntimeErrorBase('organization_queue_full')
        ident=identifier();now=timestamp()
        model=self.engine.model()
        if payload.get('knowledge_processing',{}).get('policy',{}).get('data',{}).get('model_source')=='main' or payload.get('external_knowledge',{}).get('model_source')=='main':model=dict(self.engine.config.load()['model'])
        if payload.get('knowledge_processing',{}).get('stage') in ('conversation_web_decision',):model=self.engine.routing.model()
        if payload.get('knowledge_processing',{}).get('stage')=='conversation_search_notice':
            model=json.loads(db.execute('SELECT model_snapshot FROM turns WHERE id=?',
                (payload['knowledge_processing']['root_turn_id'],)).fetchone()[0])
        if payload.get('knowledge_processing'):
            from .knowledge_general import processing_model
            spec=payload['knowledge_processing']
            if 'native_model_route' in spec:
                from .knowledge_native_route import model_for_task
                model=model_for_task(db,payload['persona_id'],spec,self.engine.config)
            model=processing_model(model,spec,config=self.engine.config)
        if payload.get('proactive'):
            from .companion import choose,GENERATION_PROFILE
            model=self.engine.config.load()['model']|{'_companion_profile':choose('minimal_im'),'_generation_profile':GENERATION_PROFILE}
        db.execute('INSERT INTO organization_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (ident,request_id,payload['persona_id'],LOCAL_USER_ID,payload.get('session_id'),payload.get('channel'),payload['mode'],int(automatic),
             payload['fingerprint'],encoded(payload),encoded(model),'queued',None,now,now))
        task=dict(db.execute('SELECT * FROM organization_tasks WHERE id=?',(ident,)).fetchone())
        self.attach(db,task,payload,trigger)
        if foreground:
            db.execute("UPDATE organization_contracts SET data_json=json_set(data_json,'$.foreground',1) WHERE task_id=?",(ident,))
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
        from .ordinary_materials import memory_access
        with memory_access(self.store,db,payload,json.loads(task['model_snapshot'])):
            return self._check(db,task,contract,payload,check_sources)

    def _check(self,db,task,contract,payload,check_sources):
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
            base=self.engine.config.load()['model']
            if payload.get('knowledge_processing',{}).get('stage')=='conversation_search_notice':
                base=json.loads(db.execute('SELECT model_snapshot FROM turns WHERE id=?',
                    (payload['knowledge_processing']['root_turn_id'],)).fetchone()[0])
            spec=payload.get('knowledge_processing',{})
            if 'native_model_route' in spec:
                from .knowledge_native_route import model_for_task
                base=model_for_task(db,task['persona_id'],spec,self.engine.config)
            expected=processing_model(base,spec,config=self.engine.config)
            if model_contract(model)!=model_contract(expected):raise RuntimeErrorBase('organization_model_changed')
        else:
            current_model=self.engine.model()
            from .config import same_credential
            if (not payload.get('proactive') and contract['data'].get('phase')!='received'
                    and not same_credential(model.get('encrypted_key'),current_model.get('encrypted_key'))):
                raise RuntimeErrorBase('organization_account_changed')
            if p['provider']!=provider_identity(current_model):
                raise RuntimeErrorBase('organization_model_changed')
            if p['provider']!=provider_identity(model):
                from .import_memory_scope import has_history
                saved = payload.get('automatic_memory') and contract['data'].get('phase')=='received' and has_history(db,payload)
                if not saved or not db.execute("""SELECT 1 FROM organization_receipts r JOIN organization_attempts a
                        ON a.id=r.attempt_id WHERE a.id=(SELECT id FROM organization_attempts
                        WHERE task_id=? ORDER BY rowid DESC LIMIT 1)""",(task['id'],)).fetchone():
                    raise RuntimeErrorBase('organization_model_changed')
                # This physical call is complete. Only its local publication uses
                # the renewed grant, and the original attempt keeps its provider.
                model=current_model
        if payload.get('knowledge_processing'):
            selected=self.scope(db,payload)
            if selected!=contract['scope'] or selected['channel'] not in p['channels']:
                raise RuntimeErrorBase('organization_scope_mismatch')
            if selected['channel']=='qq' and (selected['bot'],selected['peer'])!=(p['qq']['bot'],p['qq']['peer']):
                raise RuntimeErrorBase('organization_scope_mismatch')
            spec=payload['knowledge_processing']
            from .dialogue_generation import enabled as direct_enabled
            direct_notice=spec.get('stage')=='conversation_search_notice' and direct_enabled(db,spec['root_turn_id'])
            if not direct_notice:
                from .knowledge_native_route import PERMISSION
                kind=(PERMISSION if 'native_model_route' in spec else
                      'knowledge_router' if routing else 'main' if main_source else 'organization')
                guard_cloud_dispatch(self.store,task['persona_id'],kind,model,db=db)
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
            from .import_memory_scope import check_payload, has_history
            if has_history(db,payload) and not self.engine.model().get('encrypted_key'):
                raise RuntimeErrorBase('history_memory_authorization_required')
            check_payload(db,payload,model,p.get('memory_automation',False))
            if {'copy_session_revision','session_privacy_revision'} & (payload.get('automatic_memory') or {}).keys():
                from .copy_session_memory import check_task
                check_task(self.store,db,task,payload,model)
            else:
                guard_cloud_dispatch(self.store,task['persona_id'],'organization',model,db=db)
            if payload.get('automatic_memory'):
                if not p.get('memory_automation',False):raise RuntimeErrorBase('organization_policy_changed')
                if check_sources:
                    from .automatic_memory import validate_sources
                    validate_sources(db,payload,check_memories=payload['automatic_memory']['stage']=='verify',model=model)
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
        slot=db.execute("""SELECT generation,lease_until FROM organization_execution WHERE task_id=?
            UNION ALL SELECT generation,lease_until FROM organization_foreground_execution WHERE task_id=?""",(tid,tid)).fetchone()
        return {'version':VERSION,'policy_id':c['policy_id'],'trigger':c['data']['trigger'],
            'phase':c['data']['phase'],'next_epoch':c['data']['next_epoch'],'physical_calls':count,
            'cancel_requested':c['data']['cancel_requested'],
            'last_claim':c['data'].get('last_claim'),
            'recovery':('reuse_saved_receipt' if c['data']['phase']=='received' else
                        'audit_required_no_replay' if c['data']['phase'] in ('unknown','legacy') else
                        'already_committed' if c['data']['phase'] in ('committed','reused') else 'recheck_before_dispatch'),
            'lease':dict(slot) if slot else None}

    def recover(self):
        with self.store.connection(write=True) as db:
            for row in db.execute('SELECT * FROM organization_tasks WHERE id NOT IN (SELECT task_id FROM organization_contracts)').fetchall():
                task=dict(row);p=json.loads(task['payload_json'])
                if task['status']=='completed' and p.get('private_data_erased') is True:
                    continue
                if task['mode']=='test':p.update(persona_id=task['persona_id'],user_id=LOCAL_USER_ID,channel='desktop',mode='test',fingerprint=task['fingerprint'])
                self.attach(db,task,p,'probe' if task['mode']=='test' else {0:'manual',1:'incremental',2:'daily'}.get(task['automatic'],'manual'),legacy=True)
            live=set()
            for table in LANES.values():
                slot=db.execute(f"SELECT task_id,owner FROM {table} WHERE id='singleton'").fetchone()
                if slot[0]:
                    c=self.contract(db,slot[0])
                    if c and (slot['owner']!=self.owner or slot[0] in self.active_tasks) and process_identity_alive(c['data'].get('process')):
                        live.add(slot[0])
                    else:
                        db.execute(f"UPDATE {table} SET task_id=NULL,owner=NULL,lease_until=0 WHERE id='singleton'")
            for row in db.execute("SELECT * FROM organization_tasks WHERE status IN ('queued','waiting_quota','running')").fetchall():
                task=dict(row);c=self.contract(db,task['id']);data=c['data']
                if task['id'] in live:continue
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
                    if (state=='queued' and data['phase']=='ready' and attempt
                            and attempt['error_code'] in ('organization_network_wait','organization_storage_wait')):
                        error=attempt['error_code']
                    if data['phase']=='legacy':data['phase']='ready'
                db.execute('UPDATE organization_tasks SET status=?,error_code=? WHERE id=?',(state,error,task['id']));self.save(db,c)
            db.execute("""UPDATE organization_reservations SET state='released' WHERE state='active'
                AND NOT EXISTS(SELECT 1 FROM organization_execution WHERE task_id=organization_reservations.task_id)
                AND NOT EXISTS(SELECT 1 FROM organization_foreground_execution WHERE task_id=organization_reservations.task_id)""")

    def cancel(self,tid):
        from .service import RuntimeService
        with self.store.connection(write=True) as db:
            task=RuntimeService._owned(db,'organization_tasks',tid,self.store.current(db))
            if task['status']=='completed':raise RuntimeErrorBase('task_already_completed')
            c=self.contract(db,tid)
            if c:c['data']['cancel_requested']=True;self.save(db,c)
            db.execute("UPDATE organization_tasks SET status='cancelled',error_code='cancelled_by_user',updated_at=? WHERE id=?",(timestamp(),tid))
        self.cancel_active(tid,'cancelled_by_user')
        return self.engine.show(tid)

    def cancel_active(self,tid,reason):
        running=self.active_tasks.get(tid)
        if running:
            def cancel_current():
                if self.active_tasks.get(tid) is running:running.cancel(reason)
            running.get_loop().call_soon_threadsafe(cancel_current)

    def cancel_native_dependents(self,turn_id=None,*,exiting=False):
        """Stop only owned native work; a transport cancellation remains unknown."""
        targets=[]
        with self.store.connection(write=True) as db:
            for row in db.execute("SELECT * FROM organization_tasks WHERE status IN ('queued','running','waiting_quota') AND json_extract(payload_json,'$.knowledge_processing.stage') IN ('conversation_native','conversation_search_notice')").fetchall():
                payload=json.loads(row['payload_json']);root=payload['knowledge_processing']['root_turn_id']
                if turn_id is not None and root!=turn_id:continue
                owner=db.execute('SELECT persona_id FROM turns WHERE id=?',(root,)).fetchone()
                if not owner or owner[0]!=row['persona_id']:continue
                c=self.contract(db,row['id'])
                if not c:continue
                if c['data']['phase']=='received':
                    from .expression_utterance import CharacterOutlet
                    if not CharacterOutlet.available(db) or not db.execute('SELECT 1 FROM expression_speech_contexts WHERE task_id=?',(row['id'],)).fetchone():continue
                targets.append(row['id'])
                if not exiting:
                    c['data']['cancel_requested']=True;self.save(db,c)
                    db.execute("UPDATE organization_tasks SET status='cancelled',error_code='cancelled_by_user' WHERE id=?",(row['id'],))
        for tid in targets:
            self.cancel_active(tid,'application_exit' if exiting else 'root_turn_cancelled')
        return targets

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

    async def heartbeat(self,tid,generation,table):
        while True:
            await asyncio.sleep(15)
            with self.store.connection(write=True) as db:
                db.execute(f"UPDATE {table} SET lease_until=? WHERE id='singleton' AND task_id=? AND owner=? AND generation=?",(self.clock()+60,tid,self.owner,generation))

    async def check_network(self):
        probe=getattr(self.engine.client,'route_reachable',None)
        if not probe:return
        with self.store.connection() as db:
            rows=db.execute("""SELECT t.* FROM organization_tasks t JOIN organization_contracts c ON c.task_id=t.id
                WHERE t.status='queued' AND t.error_code='organization_network_wait'
                AND json_extract(c.data_json,'$.network_probe') AND json_extract(c.data_json,'$.next_epoch')<=?
                ORDER BY json_extract(c.data_json,'$.next_epoch'),t.rowid LIMIT 4""",(self.clock(),)).fetchall()
        for row in rows:
            if self.engine.draining or self.engine.chat.draining:return
            task=dict(row);tid=task['id']
            with self.store.connection() as db:
                c=self.contract(db,tid)
                try:self.check(db,task,c,json.loads(task['payload_json']))
                except RuntimeErrorBase:valid=False
                else:valid=True
            ready=not valid or await probe(json.loads(task['model_snapshot']))
            with self.store.connection(write=True) as db:
                current=self.contract(db,tid)
                if (not current or current['data'].get('cancel_requested') or not current['data'].get('network_probe')
                        or current['data']['next_epoch']!=c['data']['next_epoch']):continue
                if ready:current['data'].update(network_probe=False,next_epoch=0)
                else:
                    n=current['data'].get('route_checks',0)+1
                    current['data'].update(route_checks=n,next_epoch=self.clock()+min(60,2**min(n,6)))
                self.save(db,current)

    def owns(self,db,tid,generation):
        return any(db.execute(f"SELECT 1 FROM {table} WHERE task_id=? AND owner=? AND generation=? AND lease_until>?",
                              (tid,self.owner,generation,self.clock())).fetchone() for table in LANES.values())

    async def run_next(self,lane=None):
        e=self.engine
        from .migration import migration_pending,background_allowed
        if migration_pending(self.store.paths):return False
        if e.draining or e.chat.draining:return False
        from .local_repair import require_open,RepairInProgress
        try:require_open(self.store.paths)
        except RepairInProgress:return False
        from .maintenance import require_writable_storage
        try:require_writable_storage(self.store.paths)
        except RuntimeErrorBase:return False
        with self.store.connection() as db:
            leases=[row[0] for table in LANES.values() for row in
                    db.execute(f"SELECT task_id FROM {table} WHERE task_id IS NOT NULL")]
            needs_recovery=any(tid not in self.active_tasks for tid in leases) or db.execute("""
                SELECT 1 FROM organization_tasks t WHERE status='running'
                AND NOT EXISTS(SELECT 1 FROM organization_execution WHERE task_id=t.id)
                AND NOT EXISTS(SELECT 1 FROM organization_foreground_execution WHERE task_id=t.id) LIMIT 1""").fetchone()
            waiting=db.execute("SELECT 1 FROM organization_tasks WHERE status IN ('queued','waiting_quota') LIMIT 1").fetchone()
            if waiting and not needs_recovery:
                needs_recovery=db.execute("""SELECT 1 FROM organization_tasks t WHERE status IN ('queued','waiting_quota')
                    AND NOT EXISTS(SELECT 1 FROM organization_contracts c WHERE c.task_id=t.id) LIMIT 1""").fetchone()
        if needs_recovery:self.recover()
        elif not waiting:return False
        task=None;aid=None;generation=None;received=None;direct_notice=None;meter=None
        from . import call_limits
        with self.store.connection(write=True) as db:
            active_turns = {row[0] for row in db.execute("""SELECT DISTINCT t.status FROM turns t WHERE t.status IN ('queued','running')
                AND NOT EXISTS(SELECT 1 FROM knowledge_control w WHERE w.key='desktop.wait.'||t.id
                               AND json_extract(w.value,'$.revision')=t.execution_revision
                               AND NOT EXISTS(SELECT 1 FROM knowledge_control n WHERE n.key='knowledge.search_notice.turn.'||t.id
                                              AND json_extract(n.value,'$.data.fully_presented')=1))
                AND NOT EXISTS(SELECT 1 FROM expression_speech_units u JOIN expression_speech_publications p
                    ON p.utterance_id=u.id WHERE t.status='running' AND u.turn_id=t.id AND u.phase='main'
                    AND u.closed_at IS NULL AND p.state IN ('pending','partial','unknown'))""")}
            foreground = bool(active_turns or db.execute("SELECT 1 FROM input_batches WHERE state IN ('collecting','sealed_waiting_capacity') LIMIT 1").fetchone())
            from .knowledge_wait import required_tasks
            required = required_tasks(db,with_roots=True) if foreground or e.foreground_only else {}
            if e.foreground_only and not required:return False
            if lane is None:
                lane='foreground' if required or e.foreground_only else 'background'
                if foreground and not required:return False
            if lane=='background' and e.foreground_only:return False
            table=LANES[lane]
            slot=db.execute(f"SELECT * FROM {table} WHERE id='singleton'").fetchone()
            if slot['task_id']:return False
            cfg=e.config.load()
            if cfg['organization_parallel_tasks']==1:
                other=LANES['background' if lane=='foreground' else 'foreground']
                if db.execute(f"SELECT task_id FROM {other}").fetchone()[0]:return False
                if lane=='background' and any(db.execute("""SELECT 1 FROM organization_tasks t
                        JOIN organization_contracts c ON c.task_id=t.id WHERE t.id=? AND
                        (t.status='queued' OR (t.status='waiting_quota' AND
                        (json_extract(c.data_json,'$.next_epoch')<=? OR EXISTS(
                         SELECT 1 FROM model_call_waits w WHERE w.scope=json_extract(c.data_json,'$.quota_scope') AND w.state='granted'))))""",
                        (tid,self.clock())).fetchone() for tid in required):return False
            quota_signature=digest([cfg['call_limits'],cfg['model']['hourly_limit'],cfg['organization']['hourly_limit'],cfg['organization']['daily_limit']])
            choices=[]
            for row in db.execute("SELECT * FROM organization_tasks WHERE status IN ('queued','waiting_quota') ORDER BY rowid"):
                if (row['id'] in required)!=(lane=='foreground'):continue
                t=dict(row);c=self.contract(db,t['id']);m=c['data']
                if m.get('network_probe') and getattr(e.client,'route_reachable',None):continue
                if lane=='background' and chat_root(db,json.loads(t['payload_json'])):continue
                if m.get("presentation_wait"):
                    visible = db.execute("""SELECT p.state,u.closed_at,u.cancelled_at FROM expression_speech_publications p
                        JOIN expression_speech_units u ON u.id=p.utterance_id WHERE u.id=? AND u.task_id=?""",
                        (m["presentation_wait"],t["id"])).fetchone()
                    if visible and not visible["closed_at"] and not visible["cancelled_at"] and visible["state"] not in ("complete","closed"):
                        continue
                from .generation_retry import automatic_organization
                if not background_allowed(self.store.paths) and automatic_organization(t):continue
                if t['status']=='waiting_quota' and not m.get('quota_scope') and not automatic_organization(t):m['next_epoch']=0
                if t['status']=='waiting_quota' and m.get('quota_signature')!=quota_signature:m['next_epoch']=0
                if t['status']=='waiting_quota' and call_limits.granted(db,m.get('quota_scope')):m['next_epoch']=0
                if m['next_epoch']>self.clock():continue
                priority=0 if m['trigger'] in ('manual','sealed','probe') or self.clock()-m['created_epoch']>=600 else 1
                p=json.loads(t['payload_json']);a=p.get('automatic_memory') or p.get('external_knowledge',{})
                subpriority=0 if a.get('stage') in ('verify','review') else 1 if a.get('urgency')=='correction' else 2 if a.get('purpose')=='repair' else 4 if a.get('urgency')=='backfill' else 3
                choices.append((priority,m['created_epoch'] if priority==0 else 0,subpriority if priority else 0,m['created_epoch'],t,c))
            if not choices:return False
            *_,task,c=min(choices,key=lambda x:x[:4]);payload=json.loads(task['payload_json'])
            scope='organization/'+task['id']
            memory=payload.get('automatic_memory',{})
            if memory.get('batch_id'):scope='memory/'+memory['batch_id']
            if task['id'] in required:scope=call_limits.turn_scope(required[task['id']])
            try:
                self.check(db,task,c,payload,check_sources=False)
                if (not any(payload.get(k) for k in ('knowledge_processing','external_knowledge','proactive'))
                        and not db.execute("SELECT 1 FROM organization_attempts WHERE task_id=?",(task['id'],)).fetchone()):
                    frozen=json.loads(task['model_snapshot']);live=e.model()
                    if frozen.get('timeout')!=live['timeout'] or frozen.get('hourly_limit')!=live['hourly_limit']:
                        frozen.update(timeout=live['timeout'],hourly_limit=live['hourly_limit'])
                        task['model_snapshot']=encoded(frozen)
                        db.execute("UPDATE organization_tasks SET model_snapshot=? WHERE id=?",(task['model_snapshot'],task['id']))
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
                try:require_open(self.store.paths)
                except RepairInProgress:return False
                reservation,next_epoch=reserve_organization(db,e.config,task,self.clock(),waived=call_limits.granted(db,scope))
                model_call=payload.get('knowledge_processing',{}).get('stage') not in ('conversation_search','conversation_fetch','wiki_search','wiki_fetch')
                denial='background'
                if payload.get('knowledge_processing',{}).get('stage')=='conversation_search_notice':
                    model_call=not e.chat.direct.generation.enabled(db,payload['knowledge_processing']['root_turn_id'])
                if reservation and model_call:
                    blocked=call_limits.capacity(db,e.config,scope,self.clock())
                    if blocked:
                        db.execute("UPDATE organization_reservations SET state='released' WHERE id=?",(reservation,))
                        reservation=None;denial,next_epoch=blocked
                if not reservation:
                    call_limits.park(db,scope,denial,next_epoch)
                    c['data'].update(next_epoch=next_epoch,quota_scope=scope,quota_signature=quota_signature);self.save(db,c)
                    db.execute("UPDATE organization_tasks SET status='waiting_quota' WHERE id=?",(task['id'],));return False
                count=db.execute("SELECT count(*) FROM organization_attempts WHERE task_id=? AND coalesce(error_code,'') NOT IN ('organization_network_wait','organization_storage_wait')",(task['id'],)).fetchone()[0]
                if count>=4:
                    db.execute("UPDATE organization_reservations SET state='released' WHERE id=?",(reservation,))
                    db.execute("UPDATE organization_tasks SET status='failed',error_code='provider_retries_exhausted' WHERE id=?",(task['id'],));return True
                model=json.loads(task['model_snapshot'])
                messages=[{'role':'user','content':'Reply with OK.'}] if task['mode']=='test' else e.messages(payload)
                native_request_meta=None
                notice_context=None
                if payload.get('knowledge_processing',{}).get('stage')=='conversation_search_notice':
                    try:notice_context=e.processing.search_notice.context(db,task,payload)
                    except Exception as error:
                        db.execute("UPDATE organization_reservations SET state='released' WHERE id=?",(reservation,))
                        db.execute("UPDATE organization_tasks SET status='failed',error_code=? WHERE id=?",
                            (str(error) if isinstance(error,RuntimeErrorBase) else 'search_notice_context_failed',task['id']))
                        return True
                    messages=notice_context['messages']
                if payload.get('knowledge_processing',{}).get('stage')=='conversation_native':
                    messages,native_request_meta=e.processing.native.messages_for_task(db,task,payload)
                aid=identifier()
                db.execute('SAVEPOINT organization_manifest')
                db.execute("INSERT INTO organization_attempts VALUES(?,?,?,'running',NULL,NULL,?)",(aid,task['id'],self.clock(),task['model_snapshot']))
                try:
                    if notice_context:
                        e.chat.cognitive.persist(db,notice_context['manifest'],task=task,attempt_id=aid)
                        if 'direct_details' in notice_context:
                            direct_notice=e.chat.direct.register(db,notice_context['turn'],'search_notice',model,
                                messages,notice_context['direct_details'],aid,task['id'])
                    else:e.chat.cognitive.reflection(db,task,payload,messages,aid)
                except Exception as error:
                    db.execute('ROLLBACK TO organization_manifest');db.execute('RELEASE organization_manifest')
                    db.execute("UPDATE organization_reservations SET state='released' WHERE id=?",(reservation,))
                    code=str(error) if isinstance(error,RuntimeErrorBase) and str(error) in ('jev_input_budget_exceeded','native_input_budget_exceeded','context_limit_exceeded') else 'context_manifest_storage_failed'
                    db.execute("UPDATE organization_tasks SET status='failed',error_code=? WHERE id=?",(code,task['id']))
                    return True
                if native_request_meta is not None:
                    db.execute('INSERT INTO knowledge_control VALUES(?,?)',('knowledge.native.request.'+aid,
                        encoded({'revision':0,'data':native_request_meta})))
                db.execute('RELEASE organization_manifest')
                if model_call:meter=call_limits.record_call(db,scope,lane,self.clock())
                db.execute("UPDATE organization_reservations SET state='consumed',attempt_id=? WHERE id=?",(aid,reservation))
                c['data']['phase']='dispatching'
            generation=slot['generation']+1;c['data']['process']=self.process
            c['data']['last_claim']={'generation':generation,'owner':self.owner,'claimed_epoch':self.clock()}
            db.execute(f"UPDATE {table} SET task_id=?,owner=?,generation=?,lease_until=? WHERE id='singleton'",(task['id'],self.owner,generation,self.clock()+60))
            db.execute("UPDATE organization_tasks SET status='running',error_code=NULL,updated_at=? WHERE id=?",(timestamp(),task['id']));self.save(db,c)
        context=call_limits.flow.set(scope)
        e.running=True;heart=asyncio.create_task(self.heartbeat(task['id'],generation,table));self.active_tasks[task['id']]=asyncio.current_task()
        try:
            from .organization_output import receipt
            if received:
                reply=received['response_text'];audit=json.loads(received['diagnostic_json'])
                if audit.get('response_sha256')!=hashlib.sha256(reply.encode('utf-8')).hexdigest():
                    raise RuntimeErrorBase('organization_receipt_corrupt')
                if audit.get('native_receipt_key'):
                    from .knowledge_native_store import load_receipt
                    with self.store.connection() as db:
                        load_receipt(db,task,aid,expected_hash=audit.get('native_receipt_sha256'))
            else:
                from .generation_retry import _one
                if payload.get('knowledge_processing',{}).get('stage') in ('conversation_web_decision',):
                    value=await e.routing.execute(model,payload)
                elif payload.get('knowledge_processing',{}).get('stage') in ('conversation_search','conversation_fetch'):
                    value=await e.processing.tools.execute(model,payload)
                elif payload.get('knowledge_processing',{}).get('stage')=='conversation_search_notice':
                    if direct_notice:
                        value=await e.chat.direct.notice_result(direct_notice,model)
                    else:
                        value=await _one(e.chat.client,model,messages,'provider_timeout',paths=self.store.paths)
                elif payload.get('knowledge_processing',{}).get('stage')=='conversation_native':
                    from .chat import ChatCompletionsClient
                    client=e.client
                    if isinstance(client,ChatCompletionsClient):
                        from .deepseek_native_transport import DeepSeekNativeClient
                        from .knowledge_wait import mark
                        with self.store.connection() as db:
                            root_turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(payload['knowledge_processing']['root_turn_id'],)).fetchone())
                        def native_progress(state):
                            mark(e.chat.knowledge,root_turn,{'reading':'knowledge_fetch_web','searching':'knowledge_search_web','composing':'knowledge_translation'}[state])
                        client=DeepSeekNativeClient(ssl_context=client.ssl_context,progress=native_progress)
                    value=await _one(client,model,messages,'provider_native_unknown',paths=self.store.paths)
                else:
                    if {'copy_session_revision','session_privacy_revision'} & (payload.get('automatic_memory') or {}).keys():
                        from .copy_session_memory import check_task
                        from .ordinary_materials import memory_access
                        with self.store.connection() as db, memory_access(self.store,db,payload,model):
                            check_task(self.store,db,task,payload,model)
                    value=await _one(e.client,model|{'_json_output':task['mode'] not in ('test','proactive')},messages,'provider_timeout',paths=self.store.paths)
                reply,usage=value;audit=receipt(reply);audit['status']='received'
                from .generation_retry import safe_diagnostics
                audit['provider']=safe_diagnostics(getattr(value,'diagnostics',{}))
                with self.store.connection(write=True) as db:
                    db.execute('UPDATE organization_attempts SET usage_json=? WHERE id=?',(encoded(usage),aid))
                    if getattr(value,'native',None) is not None:
                        from .knowledge_native_store import save_receipt
                        audit.update(save_receipt(db,task,aid,value.native))
                    db.execute('INSERT INTO organization_receipts VALUES(?,?,?)',(aid,reply,encoded(audit)))
                    if self.owns(db,task['id'],generation):
                        c=self.contract(db,task['id']);c['data']['phase']='received';self.save(db,c)
            if payload.get('knowledge_processing',{}).get('stage')=='conversation_search_notice':
                root_id=payload['knowledge_processing']['root_turn_id']
                with self.store.connection() as db:direct=e.chat.direct.generation.enabled(db,root_id)
                if direct:
                    waiting = await e.chat.direct.present_notice(root_id)
                else:
                    waiting = await e.chat.expression.outlet.organization_received(task['id'],aid,e.config.load()['model'])
                if isinstance(waiting,str):
                    with self.store.connection(write=True) as db:
                        if not self.owns(db,task['id'],generation):
                            raise RuntimeErrorBase("organization_lease_lost")
                        c=self.contract(db,task['id'])
                        c['data'].update(phase='received',presentation_wait=waiting)
                        self.save(db,c)
                        db.execute("UPDATE organization_tasks SET status='queued',updated_at=? WHERE id=?", (timestamp(),task['id']))
                    return True
            # 原始回执和人物呈现均在事务外完成，提交时再核对任务租约。
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
            native=payload.get('knowledge_processing',{}).get('stage')=='conversation_native'
            notice=payload.get('knowledge_processing',{}).get('stage')=='conversation_search_notice'
            native_not_sent=(native or notice) and diagnostics.get('request_headers_sent') is False
            provider_completed=(not tool and not routing and diagnostics.get('response_headers_received') is True
                and (diagnostics.get('finish_reason') in ('length','content_filter','tool_calls','insufficient_system_resource','aborted')
                    or diagnostics.get('native_terminal') in ('completed','failed') or diagnostics.get('provider_terminal_error') is True))
            usage=getattr(error,'usage',None) if tool else usage_fields(getattr(error,'usage',None),deepseek=True,grok=True)
            with self.store.connection(write=True) as db:
                if not db.execute(f"SELECT 1 FROM {table} WHERE task_id=? AND owner=? AND generation=?",
                        (task['id'],self.owner,generation)).fetchone():
                    if cancelled:raise
                    return True
                if getattr(error,'native',None) is not None:
                    from .knowledge_native_store import save_receipt
                    save_receipt(db,task,aid,error.native)
                c=self.contract(db,task['id']);count=db.execute("SELECT count(*) FROM organization_attempts WHERE task_id=? AND coalesce(error_code,'') NOT IN ('organization_network_wait','organization_storage_wait')",(task['id'],)).fetchone()[0]
                has_receipt=db.execute('SELECT 1 FROM organization_receipts WHERE attempt_id=?',(aid,)).fetchone()
                from .generation_retry import pause_before_send
                pause=pause_before_send(code,diagnostics,usage)
                if not has_receipt and not cancelled and not c['data']['cancel_requested'] and pause:
                    retries=c['data'].get('connection_waits',0)+1
                    c['data'].update(connection_waits=retries,next_epoch=self.clock()+min(60,2**min(retries,6)),phase='ready',
                                     network_probe=bool(pause=='network' and not tool and not routing and getattr(e.client,'route_reachable',None)))
                    self.save(db,c)
                    if meter:db.execute("DELETE FROM model_call_meter WHERE id=? AND started=?",meter)
                    reason='organization_'+pause+'_wait'
                    db.execute("UPDATE organization_attempts SET status='failed',error_code=? WHERE id=?",(reason,aid))
                    db.execute("UPDATE organization_tasks SET status='queued',error_code=?,updated_at=? WHERE id=?",(reason,timestamp(),task['id']))
                    return True
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
                if native or notice or code=='provider_official_deepseek_required':safe_retry=False
                known_tool=known_tool or native_not_sent
                if code=='provider_official_deepseek_required':known_tool=True
                unknown=not has_receipt and not safe_retry and not known_tool and not provider_completed
                if provider_completed:c['data']['last_failure']={'code':code,'provider_completed':True,'provider':diagnostics}
                if native_not_sent:c['data']['last_failure']={'code':code,'provider_completed':False,'not_sent':True,'provider':diagnostics}
                state='cancelled' if c['data']['cancel_requested'] else ('blocked_unknown' if unknown else 'failed')
                if safe_retry and count<4 and not c['data']['cancel_requested']:
                    state='queued';c['data']['next_epoch']=self.clock()+(1,2,4)[count-1]
                c['data']['phase']='received' if has_receipt else ('ready' if safe_retry else 'failed_known' if known_tool or provider_completed else 'unknown')
                if code=='source_expired':state='stale'
                if has_receipt and code in ('history_memory_authorization_required','organization_policy_changed','private_cloud_provider_not_authorized','organization_model_changed'):state='blocked_authorization'
                if self.owns(db,task['id'],generation):
                    db.execute('UPDATE organization_tasks SET status=?,error_code=?,updated_at=? WHERE id=?',(state,'cancelled_by_user' if c['data']['cancel_requested'] else code,timestamp(),task['id']))
                    self.save(db,c)
                db.execute("UPDATE organization_attempts SET status=?,error_code=?,usage_json=coalesce(usage_json,?) WHERE id=? AND status!='completed'",('interrupted' if unknown else 'failed',code,encoded(usage) if usage is not None else None,aid))
                if has_receipt:
                    audit.update(status=state,error_code=code,error=getattr(error,'diagnostic',{}))
                    db.execute('UPDATE organization_receipts SET diagnostic_json=? WHERE attempt_id=?',(encoded(audit),aid))
            if cancelled and not c['data']['cancel_requested']:raise
        finally:
            call_limits.flow.reset(context)
            heart.cancel()
            try:
                await asyncio.gather(heart,return_exceptions=True)
            finally:
                self.active_tasks.pop(task['id'],None);e.running=bool(self.active_tasks)
                with self.store.connection(write=True) as db:
                    db.execute(f"UPDATE {table} SET task_id=NULL,owner=NULL,lease_until=0 WHERE id='singleton' AND task_id=? AND owner=? AND generation=?",(task['id'],self.owner,generation))
                    state=db.execute("SELECT status FROM organization_tasks WHERE id=?",(task['id'],)).fetchone()[0]
                    if scope.startswith('organization/') and state not in (*WAITING,'running'):
                        call_limits.finish(db,scope)
                    if scope.startswith('memory/') and db.execute("SELECT state FROM memory_batches WHERE id=?",
                            (memory['batch_id'],)).fetchone()[0]=='completed':
                        call_limits.finish(db,scope)
                if payload.get('knowledge_processing',{}).get('stage')=='conversation_search_notice':
                    e.chat.presentation_wakeup.notify()
        return True
