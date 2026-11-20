"""Managed general retrieval plumbing. No source-authority or factual veto layer."""
import copy,json
from .archive import fingerprint,turn_scope
from .models import RuntimeErrorBase,LOCAL_USER_ID
from .private_personas import provider_identity
from .knowledge_general import PROFILE,STAGES,IndependentWebTools,OUTPUT_PROFILE

VERSION='general-conversation-retrieval-v1'
def model_contract(model):return {k:v for k,v in model.items() if k!='encrypted_key'}
def put(db,key,data):
    db.execute('INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('knowledge.processing.'+key,json.dumps({'revision':0,'data':data},ensure_ascii=False)))
def record_used(db,paths,pid,channel,metadata):
    # Historical conversation is not erased because a source date or policy changed.
    from .knowledge_service import read
    row=read(db,'processing.record.'+metadata.get('record_id',''),{})['data']
    if row and (row.get('persona_id')!=pid or row.get('channel')!=channel):raise RuntimeErrorBase('knowledge_scope_changed')

class KnowledgeProcessing:
    def __init__(self,engine):
        self.engine=engine;self.store=engine.store;self.service=engine.chat.knowledge;self.service.processing=self
        self.tools=IndependentWebTools(self)
        from .knowledge_native import NativeRetrieval
        self.native=NativeRetrieval(self)
        from .knowledge_search_notice import SearchNotice
        self.search_notice=SearchNotice(self)
    def model(self,pid=None):return dict(self.engine.config.load()['model'])
    def _model_from_policy(self,policy):return self.model()
    def defaults(self,model_source='main'):
        model=self.model()
        return {'enabled':False,'channels':['desktop'],'provider':provider_identity(model),'model_contract':fingerprint(model_contract(model)),
            'pipeline':VERSION,'prompt_profile':PROFILE,'model_source':'main'}
    def policy(self,pid):
        from .knowledge_service import read
        self.service.current_policy(pid)
        with self.store.connection() as db:return read(db,'processing.'+pid,self.defaults())
    def update_policy(self,pid,data,expected_revision,operation_id):
        from .private_personas import guard_acceptance_write
        guard_acceptance_write(self.store,pid)
        expected=self.defaults()
        if (not isinstance(data,dict) or data.get('prompt_profile')!=PROFILE or data.get('pipeline')!=VERSION
                or type(data.get('enabled')) is not bool or data.get('channels')!=['desktop']
                or data.get('provider')!=expected['provider'] or data.get('model_contract')!=expected['model_contract']):
            raise RuntimeErrorBase('knowledge_processing_policy_invalid')
        return self.service._update('processing.'+pid,data,expected_revision,operation_id)
    def enable_for_persona(self,persona_id,enabled,expected_reader_revision,expected_processing_revision,expected_web_revision,operation_id):
        from .knowledge_service import read,POLICY
        from .knowledge_web import DEFAULT
        from .private_personas import guard_acceptance_write
        guard_acceptance_write(self.store,persona_id)
        if type(enabled) is not bool:raise RuntimeErrorBase('knowledge_policy_invalid')
        if enabled:
            from .migration import ensure_cloud_ready
            ensure_cloud_ready(self.store.paths)
        def apply(db):
            if self.store.current(db)!=persona_id:raise RuntimeErrorBase('persona_selection_conflict')
            old=read(db,'processing.'+persona_id,self.defaults());web=read(db,'web.policy.'+persona_id,DEFAULT)
            if old['revision']!=expected_processing_revision or web['revision']!=expected_web_revision:raise RuntimeErrorBase('version_conflict')
            values={'processing.'+persona_id:{'revision':old['revision']+1,'data':self.defaults()|{'enabled':enabled}},
                'web.policy.'+persona_id:{'revision':web['revision']+1,'data':web['data']|{'enabled':enabled}}}
            if enabled:
                from .knowledge_native import POLICY as NATIVE_POLICY
                native=read(db,'native.policy.'+persona_id,NATIVE_POLICY)
                data={**native['data'],'enabled':True};data.pop('migration_requires_review',None)
                values['native.policy.'+persona_id]={'revision':native['revision']+1,'data':data}
            for key,value in values.items():db.execute('INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('knowledge.'+key,json.dumps(value,ensure_ascii=False)))
        return self.service._update('policy.'+persona_id,{'enabled':enabled,'channels':['desktop']},expected_reader_revision,operation_id,
            request=[persona_id,enabled,expected_reader_revision,expected_processing_revision,expected_web_revision],apply=apply)

    def enabled(self,turn):
        data=self.policy(turn['persona_id'])['data']
        return data.get('enabled') and data.get('prompt_profile')==PROFILE and turn['channel'] in data.get('channels',[])
    def context(self,db,turn):
        from .turn_input import load_turn_input
        return {'scope':turn_scope(db,turn),'input':load_turn_input(db,turn)}
    def _grant(self,db,turn,policy,inputs,stage=None,*,native_route=None):
        from .knowledge_service import read,POLICY
        from .private_personas import guard_cloud_dispatch
        current=read(db,'processing.'+turn['persona_id'],self.defaults())
        reader=read(db,'policy.'+turn['persona_id'],POLICY)['data']
        web=read(db,'web.policy.'+turn['persona_id'],{'enabled':False})['data']
        if current!=policy or not reader['enabled'] or not web.get('enabled') or turn['channel'] not in reader['channels'] or not current['data'].get('enabled') or current['data'].get('prompt_profile')!=PROFILE:
            raise RuntimeErrorBase('knowledge_permission_changed')
        if native_route is None:
            guard_cloud_dispatch(self.store,turn['persona_id'],'main',self.model(),db=db)
        else:
            from .knowledge_native_route import model_for_task, PERMISSION
            spec={'root_turn_id':turn['id'],'stage':stage,'input':inputs,'native_model_route':native_route}
            guard_cloud_dispatch(self.store,turn['persona_id'],PERMISSION,
                                 model_for_task(db,turn['persona_id'],spec,self.engine.config),db=db)
        if stage=='conversation_web_decision':self.engine.routing.grant(turn['persona_id'],db=db)
        if stage in ('conversation_native','conversation_search_notice'):
            if self.native.read_policy(db,turn['persona_id'])!=inputs.get('native_policy'):
                raise RuntimeErrorBase('native_policy_changed')
            from .turn_input import input_text
            from .knowledge_native import public_task
            projection=self.native.projection(db,turn,input_text(db,turn),inputs['native_policy'])
            if projection!=public_task(inputs['public_task']):
                raise RuntimeErrorBase('native_public_task_scope_changed')
            if inputs.get('protocol_parent') or inputs.get('public_context'):
                from .knowledge_native_memory import source_states
                if source_states(db,turn['persona_id'])['revision']!=inputs.get('source_state_revision',0):
                    raise RuntimeErrorBase('native_readable_source_changed')
        if stage=='conversation_native':
            from .tk_notice import authorize_search
            authorize_search(db,self.store,turn)

    def _call(self,turn,stage,inputs):
        from .knowledge_service import read
        from .knowledge_wait import check,mark
        if stage=='conversation_search_notice':
            scene_gate=getattr(self.engine.chat,'await_notice_scene',None)
            if scene_gate:
                try:
                    scene_gate(turn)
                except (RuntimeErrorBase, ValueError, OSError) as error:
                    # End retrieval if the required person or visible preface is unavailable.
                    raise RuntimeErrorBase('knowledge_request_inactive:'+str(error)) from error
        check(self.service,turn)
        if stage not in STAGES:raise RuntimeErrorBase('legacy_retrieval_frozen')
        policy=self.policy(turn['persona_id'])
        from .knowledge_routing import binding
        decision_binding=binding(self.engine.routing.model()) if stage=='conversation_web_decision' else None
        token=fingerprint([turn['id'],turn.get('execution_revision'),stage,inputs,policy,VERSION]+([decision_binding] if decision_binding else []))
        cache_key='processing.cache.'+token
        with self.store.connection(write=True) as db:
            # A primary retry changes its execution number, not the identity of
            # already dispatched retrieval work for the same frozen question.
            context_guard=fingerprint(self.context(db,turn))
            previous=[]
            from .desktop_recovery import contract
            recovery = contract(db, turn) if turn['channel']=='desktop' else None
            for row in db.execute("SELECT * FROM organization_tasks WHERE persona_id=? AND json_extract(payload_json,'$.knowledge_processing.root_turn_id')=? AND json_extract(payload_json,'$.knowledge_processing.stage')=? ORDER BY rowid",(turn['persona_id'],turn['id'],stage)):
                payload=json.loads(row['payload_json']);data=payload['knowledge_processing']
                if ((data['input']==inputs or stage in ('conversation_native','conversation_web_decision','conversation_search_notice')) and data['pipeline']==VERSION
                        and (data.get('decision_binding')==decision_binding or stage=='conversation_web_decision')
                        and data['context_guard']==context_guard and payload['scope']==turn_scope(db,turn)):
                    previous.append(row)
                elif recovery and recovery.get("recovered"):
                    raise RuntimeErrorBase("knowledge_prior_execution_requires_review")
            from .knowledge_native_route import for_new_work
            native_route=(json.loads(previous[-1]['payload_json'])['knowledge_processing'].get('native_model_route')
                          if previous else for_new_work(db,turn,stage))
            self._grant(db,turn,policy,inputs,stage,native_route=native_route)
            resumed=None
            for row in previous:
                if row['status']=='completed':
                    return copy.deepcopy(read(db,'processing.result.'+row['id'],{})['data']),row['id']
                if recovery and recovery.get("recovered") and row["status"] in ("queued","waiting_quota","running"):
                    resumed = {"id":row["id"]}
                    continue
                if row['status'] in ('queued','waiting_quota','running') and stage=='conversation_search_notice':
                    from .dialogue_generation import enabled as direct_enabled
                    contract=self.engine.managed.contract(db,row['id'])
                    if direct_enabled(db,turn['id']) and contract and contract['data']['phase']=='received' and not contract['data']['cancel_requested']:
                        resumed={'id':row['id']}
                        continue
                if row['status']=='failed' or stage in ('conversation_native','conversation_search_notice') and row['status']=='cancelled':
                    contract=self.engine.managed.contract(db,row['id'])
                    if stage in ('conversation_native','conversation_search_notice') and contract and contract['data']['phase']=='received' and not contract['data']['cancel_requested']:
                        if db.execute('SELECT 1 FROM organization_receipts r JOIN organization_attempts a ON a.id=r.attempt_id WHERE a.task_id=?',(row['id'],)).fetchone():
                            db.execute("UPDATE organization_tasks SET status='queued',error_code=NULL WHERE id=?",(row['id'],))
                            resumed={'id':row['id']}
                            continue
                    if contract and contract['data'].get('last_failure',{}).get('provider_completed'):
                        error=RuntimeErrorBase(row['error_code'] or 'retrieval_unavailable');error.task_id=row['id'];raise error
                if stage=='conversation_web_decision' and db.execute('SELECT 1 FROM organization_receipts r JOIN organization_attempts a ON a.id=r.attempt_id WHERE a.task_id=?',(row['id'],)).fetchone():
                    error=RuntimeErrorBase(row['error_code'] or 'jev_web_decision_invalid');error.task_id=row['id'];raise error
            if any(row['status']=='blocked_unknown' or db.execute("SELECT 1 FROM organization_attempts a WHERE a.task_id=? AND a.status IN ('running','interrupted') AND NOT EXISTS(SELECT 1 FROM organization_receipts r WHERE r.attempt_id=a.id)",(row['id'],)).fetchone() for row in previous):
                error=RuntimeErrorBase('organization_effect_unknown');error.task_id=previous[-1]['id'];raise error
            if not resumed:
                for row in previous:
                    contract=self.engine.managed.contract(db,row['id'])
                    attempted=db.execute('SELECT 1 FROM organization_attempts WHERE task_id=?',(row['id'],)).fetchone()
                    if attempted and not (contract and contract['data'].get('last_failure',{}).get('not_sent')):
                        error=RuntimeErrorBase(row['error_code'] or 'knowledge_prior_execution_requires_review')
                        error.task_id=row['id'];raise error
            old=read(db,cache_key,{})['data']
            if old:
                task=db.execute('SELECT status FROM organization_tasks WHERE id=?',(old['task_id'],)).fetchone()
                if task and task['status']=='completed':return copy.deepcopy(read(db,'processing.result.'+old['task_id'],{})['data']),old['task_id']
            data={'stage':stage,'input':inputs,'pipeline':VERSION,'root_turn_id':turn['id'],'context_guard':context_guard,
                'policy':policy,'cache_key':cache_key,'request_profile':OUTPUT_PROFILE,
                **({'decision_binding':decision_binding} if decision_binding else {})}
            if native_route is not None:data['native_model_route']=native_route
            payload={'persona_id':turn['persona_id'],'channel':turn['channel'],'session_id':turn['session_id'],'mode':'both',
                'scope':turn_scope(db,turn),'user_id':LOCAL_USER_ID,
                'knowledge_processing':data,'fingerprint':token,'manual_quota_request':bool(not turn.get('outbound_id') and (turn.get('user_event_id') or turn.get('input_batch_id')))}
            task=resumed or self.engine.managed.register(db,payload,'general-'+token,automatic=1,trigger='incremental')
            key='wait.'+turn['id'];waiting=read(db,'processing.'+key,{})['data']
            ids=waiting.get('task_ids',[]) if waiting.get('execution_revision')==turn.get('execution_revision') else []
            put(db,key,{'execution_revision':turn.get('execution_revision'),'task_ids':list(dict.fromkeys(ids+[task['id']]))})
        mark(self.service,turn,phase_for(stage))
        while True:
            check(self.service,turn)
            with self.store.connection() as db:
                row=db.execute('SELECT status,error_code FROM organization_tasks WHERE id=?',(task['id'],)).fetchone()
                if row['status']=='completed':return copy.deepcopy(read(db,'processing.result.'+task['id'],{})['data']),task['id']
                if stage == 'conversation_search_notice':
                    from .desktop_presentation import pending_speech, PresentationPending
                    speech = pending_speech(db,task['id'])
                    if speech and not speech['closed_at']:
                        raise PresentationPending(turn['id'], task['id'])
                if row['status'] not in ('queued','running','waiting_quota'):
                    error=RuntimeErrorBase(row['error_code'] or 'retrieval_unavailable');error.task_id=task['id'];raise error
            self.service.closed.wait(.1)
    def check(self,db,task,contract,payload):
        data=payload['knowledge_processing'];turn=db.execute('SELECT * FROM turns WHERE id=?',(data['root_turn_id'],)).fetchone()
        if data['stage'] not in STAGES:raise RuntimeErrorBase('legacy_retrieval_frozen')
        if not turn or turn['persona_id']!=task['persona_id'] or turn['cancelled_at'] or turn['status']!='queued' or turn_scope(db,dict(turn))!=payload['scope']:
            raise RuntimeErrorBase('knowledge_scope_changed')
        self._grant(db,dict(turn),data['policy'],data['input'],data['stage'],native_route=data.get('native_model_route'))
        if data['stage'] in ('conversation_search_notice','conversation_native'):
            check_scene = getattr(self.engine.chat, 'scene_for', None)
            if check_scene:
                check_scene(db,dict(turn))
        if data['stage']=='conversation_web_decision':
            from .knowledge_routing import binding
            if binding(self.engine.routing.model())!=data.get('decision_binding'):raise RuntimeErrorBase('organization_model_changed')
        self.tools.check(db,dict(turn),data)
    def published(self,db,task,payload,response,audit):
        output=(self.search_notice.publish(db,task,payload,response,audit) if payload['knowledge_processing']['stage']=='conversation_search_notice'
            else self.native.accept(db,task,payload,response,audit) if payload['knowledge_processing']['stage']=='conversation_native'
            else self.tools.accept(payload,response))
        put(db,'result.'+task['id'],output)
        db.execute('INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('knowledge.'+payload['knowledge_processing']['cache_key'],json.dumps({'revision':0,'data':{'task_id':task['id']}})))
    def prepare(self,turn,*args,**kwargs):
        if turn['channel']=='test' or not turn.get('persona_id'):
            return {'state':'disabled','evidence':[]}
        from .knowledge_service import read
        with self.store.connection() as db:
            retired=[row[0] for row in db.execute("SELECT id FROM organization_tasks WHERE json_extract(payload_json,'$.knowledge_processing.root_turn_id')=? AND json_extract(payload_json,'$.knowledge_processing.stage') IN ('conversation_search_plan','conversation_organize')",(turn['id'],))]
            if retired:
                unknown=db.execute("SELECT 1 FROM organization_attempts a JOIN organization_tasks t ON t.id=a.task_id WHERE json_extract(t.payload_json,'$.knowledge_processing.root_turn_id')=? AND a.status IN ('running','interrupted') AND NOT EXISTS(SELECT 1 FROM organization_receipts r WHERE r.attempt_id=a.id)",(turn['id'],)).fetchone()
                record=read(db,'conversation.record.'+turn['id'],{})['data']
                if (unknown or record.get('persona_id')!=turn['persona_id'] or record.get('channel')!=turn['channel']
                        or not (record.get('sources') or record.get('organized',{}).get('notes'))):
                    raise RuntimeErrorBase('legacy_request_requires_review')
                return {'mode':PROFILE,'state':'ready','record_id':turn['id'],'record_hash':fingerprint(record),
                    'sources':copy.deepcopy(record.get('sources',[])),'organized':copy.deepcopy(record.get('organized',{})),
                    'retrieval':copy.deepcopy(record.get('retrieval',{})),'legacy_receipt_reused':True}
        return self.native.prepare(turn)
    def show(self,pid,turn_id):
        with self.store.connection() as db:
            row=db.execute('SELECT value FROM knowledge_control WHERE key=?',('knowledge.conversation.record.'+turn_id,)).fetchone()
            record=json.loads(row[0])['data'] if row else {}
        if record.get('persona_id')!=pid:return {'state':'not_processed'}
        return {'state':'ready','mode':PROFILE,'knowledge':record}

def phase_for(stage):return {'conversation_web_decision':'knowledge_routing','conversation_search_notice':'knowledge_search_notice','conversation_search_plan':'knowledge_web_plan','conversation_search':'knowledge_search_web','conversation_fetch':'knowledge_fetch_web','conversation_organize':'knowledge_translation','conversation_native':'knowledge_search_web'}.get(stage,'knowledge_goal')
def turn_scope_value(turn):return {'persona_id':turn['persona_id'],'channel':turn['channel']}
