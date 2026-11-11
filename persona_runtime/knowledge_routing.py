"""Opt-in native routing on the existing knowledge execution and receipt chain."""
import copy
import hashlib
import json
from urllib.parse import urlsplit

from .archive import fingerprint
from .config import ConfigStore
from .knowledge_processing import model_contract
from .knowledge_routing_contract import VERSION,parse,request
from .knowledge_service import POLICY,REGISTRY,read
from .models import RuntimeErrorBase
from .private_personas import provider_identity

DEFAULT={'enabled':False,'variant':'web_only','min_choice_probability':0.6,'pipeline':VERSION,
         'provider':None,'model_contract':None}

def configured_model(paths):
    main=dict(ConfigStore(paths).load()['model'])
    path=paths.home/'private/knowledge-router/config.json'
    saved=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    gate=saved.get('gate',{})
    valid=(saved.get('base_url')==gate.get('base_url') and saved.get('name')==gate.get('name')
           and gate.get('completed_rounds')==10 and type(gate.get('successes')) is int and 9<=gate['successes']<=10
           and saved.get('encrypted_key') and hashlib.sha256(saved['encrypted_key'].encode()).hexdigest()==gate.get('credential_sha256'))
    origin=urlsplit(saved.get('base_url',''))
    if origin.scheme!='https' or not origin.hostname or origin.username or origin.password or origin.query or origin.fragment:
        valid=False
    main.update(base_url=saved.get('base_url',''),name=saved.get('name',''),encrypted_key=saved.get('encrypted_key',''),
                _routing_gate_hash=fingerprint(gate),_routing_gate_valid=bool(valid))
    return main

def validate_used(db,paths,persona_id,channel,metadata):
    if not metadata:return
    policy=read(db,'routing.'+persona_id,DEFAULT)
    task=db.execute('SELECT persona_id,channel,payload_json FROM organization_tasks WHERE id=?',(metadata.get('task_id'),)).fetchone()
    result=read(db,'processing.result.'+metadata.get('task_id',''),{})['data']
    if (not task or task['persona_id']!=persona_id or task['channel']!=channel or not policy['data']['enabled']
            or policy!=metadata.get('policy') or not result or fingerprint(result)!=metadata.get('result_hash')
            or json.loads(task['payload_json'])['knowledge_processing']['input']['routing_policy']!=policy):
        raise RuntimeErrorBase('knowledge_routing_changed')
    if paths is not None:
        model=configured_model(paths)
        if not model['_routing_gate_valid'] or fingerprint(model_contract(model))!=policy['data']['model_contract']:
            raise RuntimeErrorBase('knowledge_routing_changed')

class KnowledgeRouting:
    def __init__(self,engine):self.engine,self.store=engine,engine.store;self.transport=None

    def model(self):return configured_model(self.store.paths)

    def configuration(self):
        model=self.model()
        return {'base_url':model['base_url'],'name':model['name'],'key_configured':bool(model['encrypted_key']),
                'gate_passed':model['_routing_gate_valid'],'timeout':model['timeout'],'max_input_chars':model['max_input_chars']}

    def policy(self,persona_id):
        self.engine.chat.knowledge.current_policy(persona_id)
        with self.store.connection() as db:return read(db,'routing.'+persona_id,DEFAULT)

    def enabled(self,turn):return self.policy(turn['persona_id'])['data']['enabled']

    def update_policy(self,persona_id,data,expected_revision,operation_id):
        from .private_personas import guard_acceptance_write,guard_cloud_dispatch
        guard_acceptance_write(self.store,persona_id)
        if (not isinstance(data,dict) or set(data)!=set(DEFAULT) or type(data['enabled']) is not bool
                or data['variant'] not in ('local_web','web_only') or type(data['min_choice_probability']) not in (int,float)
                or not 0.5<=data['min_choice_probability']<=1 or data['pipeline']!=VERSION):
            raise RuntimeErrorBase('knowledge_routing_policy_invalid')
        model=self.model() if data['enabled'] else None;value=copy.deepcopy(data)
        if data['enabled']:
            if not model['_routing_gate_valid']:raise RuntimeErrorBase('knowledge_routing_gate_required')
            guard_cloud_dispatch(self.store,persona_id,'knowledge_router',model)
            value.update(provider=provider_identity(model),model_contract=fingerprint(model_contract(model)))
        def apply(db):
            if data['enabled']:
                processor=self.engine.processing;old=read(db,'processing.'+persona_id,processor.defaults())
                db.execute('INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                    ('knowledge.processing.'+persona_id,json.dumps({'revision':old['revision']+1,'data':processor.defaults('main')|{'enabled':True,'prompt_profile':old['data'].get('prompt_profile','legacy_v1')}})))
        return self.engine.chat.knowledge._update('routing.'+persona_id,value,expected_revision,operation_id,request=data,apply=apply)

    def capabilities(self,db,persona_id):
        from .knowledge_web import DEFAULT as WEB
        reader=read(db,'policy.'+persona_id,POLICY)['data']
        web=read(db,'web.policy.'+persona_id,WEB)
        registry=read(db,'registry',REGISTRY)['data']
        local_allowed=read(db,'routing.'+persona_id,DEFAULT)['data']['variant']=='local_web'
        return {'local':bool(local_allowed and reader['enabled'] and registry['active']),
                'web':bool(reader['enabled'] and web['data']['enabled'] and web['data']['topics']),
                'package_sha256':registry['active'] if local_allowed else None,'web_policy_revision':web['revision']}

    def grant(self,db,turn,inputs):
        policy=read(db,'routing.'+turn['persona_id'],DEFAULT)
        model=self.model()
        if (not policy['data']['enabled'] or policy!=inputs['routing_policy'] or not model['_routing_gate_valid']
                or policy['data']['model_contract']!=fingerprint(model_contract(model))
                or inputs['state']['capabilities']!=self.capabilities(db,turn['persona_id'])):
            raise RuntimeErrorBase('knowledge_routing_changed')

    async def execute(self,model,payload):
        body=request(model,payload['knowledge_processing']['input'])
        if self.transport:return await self.transport(model,body)
        from .knowledge_routing_transport import execute
        return await execute(model,body)

    def accept(self,payload,text):
        data=payload['knowledge_processing']['input'];result=parse(text,data['options'])
        floor=data['routing_policy']['data']['min_choice_probability']
        reuse=result['reuse_probability']
        focused=payload['knowledge_processing']['policy']['data'].get('prompt_profile') in ('question_focused_v4','question_focused_v5','question_focused_v6')
        # A fresh retrieval decision does not depend on whether OLD evidence
        # would have sufficed. Keep the original probability for auditing.
        if focused and result['choice']!='none':reuse=0
        if (payload['knowledge_processing']['policy']['data'].get('prompt_profile') in ('question_focused_v5','question_focused_v6')
                and result['choice']=='none' and data['state'].get('clarified_goal',{}).get('queries')==[]):
            # Goal and route agree this is conversation; evidence sufficiency
            # is not a dependency of a conversational reply.
            reuse=0
        reason='knowledge_route_uncertain' if result['probabilities'][result['choice']]<floor else 'knowledge_route_reuse_uncertain' if 1-floor<reuse<floor else None
        if reason:
            if payload['knowledge_processing']['policy']['data'].get('prompt_profile') in ('question_focused_v3','question_focused_v4','question_focused_v5','question_focused_v6'):
                return result|{'decision_status':'uncertain','uncertainty_reason':reason,'reuse':False}
            raise RuntimeErrorBase(reason)
        result['reuse']=reuse>=floor
        if result['reuse'] and (result['choice']!='none' or not data['state']['reusable_evidence']['available']):
            raise RuntimeErrorBase('knowledge_route_reuse_unavailable')
        if result['choice']!='none' and not data['state']['capabilities'][result['choice']]:
            raise RuntimeErrorBase('knowledge_route_path_unavailable')
        return result

    def prepare(self,processor,turn,question,previous_turn_id):
        previous=processor.previous(turn,previous_turn_id) if previous_turn_id else None
        with self.store.connection() as db:
            context=processor.context(db,turn)
            capabilities=self.capabilities(db,turn['persona_id'])
        policy=self.policy(turn['persona_id'])
        options=['none','local','web'] if policy['data']['variant']=='local_web' else ['none','web']
        inputs={'processing_profile':processor.policy(turn['persona_id'])['data'].get('prompt_profile','legacy_v1'),'request_model':self.model()['name'],'options':options,'routing_policy':policy,'state':{'current':question,'recent_context':context['recent_context'],
            'capabilities':capabilities,'reusable_evidence':{'available':bool(previous and previous.get('evidence')),
                'sources':[{k:e.get(k) for k in ('article_title','source_snapshot_at','url')} for e in (previous or {}).get('evidence',[])]}}}
        focused=processor.policy(turn['persona_id'])['data'].get('prompt_profile') in ('question_focused_v3','question_focused_v4','question_focused_v5','question_focused_v6')
        if focused:
            ready=inputs['state']['reusable_evidence']
            ready['result_ready']=ready['available']
            if previous_turn_id and ready['available']:
                with self.store.connection() as db:
                    old=db.execute('SELECT session_id,status,user_event_id FROM turns WHERE id=? AND persona_id=?',(previous_turn_id,turn['persona_id'])).fetchone()
                    text=db.execute('SELECT text FROM events WHERE id=?',(old['user_event_id'],)).fetchone() if old else None
                ready['previous_question']=text[0] if text else None
                ready['previous_reply_in_current_history']=bool(old and old['session_id']==turn['session_id'] and old['status']=='completed')
        result,task_id=processor._call(turn,'route',inputs)
        route_tasks=[task_id];clarification_task=None;goal=None
        goal_input={'question':question,'recent_context':context['recent_context']}
        if inputs['processing_profile'] in ('question_focused_v4','question_focused_v5','question_focused_v6') and result['choice']!='none':
            goal,clarification_task=processor._call(turn,'goal',goal_input)
            if not goal['queries']:result=dict(result,decision_status='uncertain',uncertainty_reason='knowledge_route_goal_conflict')
        if result.get('decision_status')=='uncertain':
            if goal is None:goal,clarification_task=processor._call(turn,'goal',goal_input)
            retry=copy.deepcopy(inputs);retry['state']['clarified_goal']=goal
            retry['state']['previous_uncertainty_reason']=result['uncertainty_reason']
            result,task_id=processor._call(turn,'route_retry',retry);route_tasks.append(task_id)
            if result.get('decision_status')=='uncertain':raise RuntimeErrorBase(result['uncertainty_reason'])
            if not goal['queries'] and result['choice']!='none':raise RuntimeErrorBase('knowledge_route_goal_conflict')
        metadata={'task_id':task_id,'result_hash':fingerprint(result),'policy':policy,'choice':result['choice'],
            'probabilities':result['probabilities'],'confidence':result['confidence'],'reuse':result['reuse'],'response_model':result['response_model']}
        metadata['route_task_ids']=route_tasks
        if clarification_task:metadata['clarification_goal_task_id']=clarification_task
        if result['choice']=='none':
            if result['reuse']:
                # Recheck current evidence after the decision; a cached choice
                # must never revive a withdrawn or expired prior answer.
                previous=processor.previous(turn,previous_turn_id)
                if not previous or not previous.get('evidence'):raise RuntimeErrorBase('knowledge_route_reuse_unavailable')
                return previous|{'routing':metadata}
            return {'state':'not_triggered','evidence':[],'routing':metadata}
        return processor.prepare(turn,question,path=result['choice'])|{'routing':metadata}
